"""PipelineStateManager — 管线状态机的唯一写入者。

DESIGN_DOC §19.5: 编排器专用检查点管理器, 支持崩溃恢复。
每个 Agent 完成操作后, 编排器立刻写检查点到 gbrain。
"""

import os
import yaml
from datetime import datetime
from typing import Optional


class PipelineStateManager:
    """管线状态管理器。三个硬约束的核心执行者。

    硬约束:
    1. 读写屏障: 下章Phase A必须等上章Phase B完成
    2. 单写者: 只有编排器写 pipeline-state.yaml
    3. 增量上下文刷新: 预加载的上下文在写作前做diff
    """

    def __init__(self, brain_path: str = ""):
        self.brain_path = brain_path
        self.state = self._load_or_init()
        self._on_state_change = None  # optional callback(chapter_id, old_status, new_status, data)

    def set_on_state_change(self, callback):
        """注册状态变更回调。回调签名: (chapter_id, old_status, new_status, data: dict)

        支持同步回调和异步回调 (async def)。
        回调在 transition/block/mark_stale 成功时被调用。
        回调异常不会影响管线执行。
        """
        self._on_state_change = callback

    def _invoke_callback(self, chapter_id: str, old_status: str,
                         new_status: str, data: dict):
        """安全调用状态变更回调 (兼容 sync 和 async)。"""
        if not self._on_state_change:
            return
        import asyncio
        try:
            result = self._on_state_change(chapter_id, old_status, new_status, data)
            if asyncio.iscoroutine(result):
                # 如果在事件循环中, 调度执行
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    pass  # 无事件循环, 忽略异步回调
        except Exception:
            pass  # 回调失败不影响管线

    # ── 状态加载 ──

    @staticmethod
    def _ensure_dict(loaded) -> dict:
        """防御: YAML 可能解析出非 dict (字符串/数字等)"""
        if isinstance(loaded, dict):
            return loaded
        return {"pipeline": {"chapters": {}}}

    def _load_or_init(self) -> dict:
        path = self._state_path()
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return self._ensure_dict(yaml.safe_load(f))
            except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
                import logging
                logger = logging.getLogger("novel-ai")
                logger.error("管线状态加载失败: %s — %s", path, str(e))
                # 尝试从备份恢复
                backup_path = path + ".bak"
                if os.path.exists(backup_path):
                    try:
                        with open(backup_path, "r", encoding="utf-8") as f:
                            state = self._ensure_dict(yaml.safe_load(f))
                        logger.warning("管线状态从备份恢复: %s", backup_path)
                        return state
                    except Exception:
                        logger.error("备份恢复也失败: %s", backup_path)
                else:
                    logger.warning("无备份文件, 使用空白状态")
        return {"pipeline": {"chapters": {}}}

    def _state_path(self) -> Optional[str]:
        if not self.brain_path:
            return None
        return os.path.join(self.brain_path, "pages", "agent", "pipeline-state.yaml")

    def _persist(self):
        path = self._state_path()
        if not path:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 持久化前备份现有文件
        if os.path.exists(path):
            bak = path + ".bak"
            try:
                import shutil
                shutil.copy2(path, bak)
            except OSError:
                pass
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.dump(self.state, f, allow_unicode=True)
        os.replace(tmp, path)  # 原子写入

    # ── 章节状态操作 ──

    def _ensure_chapter(self, chapter_id: str) -> dict:
        ch = self.state["pipeline"]["chapters"].setdefault(chapter_id, {})
        ch.setdefault("status", "IDLE")
        ch.setdefault("checkpoints", {})
        ch.setdefault("review_round", 0)
        ch.setdefault("review_type", "quick")
        return ch

    # 状态转移 DAG: from → allowed_to
    TRANSITION_GRAPH = {
        "IDLE":                {"CONTEXT_READY"},
        "CONTEXT_READY":       {"WRITING", "BLOCKED"},
        "WRITING":             {"REVIEWING", "BLOCKED"},
        "REVIEWING":           {"COMPLETE", "COMPLETE_WITH_WARNINGS", "BLOCKED"},
        "COMPLETE":            set(),
        "COMPLETE_WITH_WARNINGS": {"STALE"},
        "BLOCKED":             {"CONTEXT_READY", "WRITING", "REVIEWING"},  # 解阻塞
        "STALE":               {"CONTEXT_READY"},  # 重写
    }

    VALID_STATUSES = set(TRANSITION_GRAPH.keys())

    def transition(self, chapter_id: str, new_status: str) -> bool:
        """状态转移: IDLE → CONTEXT_READY → WRITING → REVIEWING → COMPLETE

        Returns True on success, False on invalid transition.
        """
        if new_status not in self.VALID_STATUSES:
            return False

        ch = self._ensure_chapter(chapter_id)
        current = ch.get("status", "IDLE")

        if current not in self.TRANSITION_GRAPH:
            return False

        allowed = self.TRANSITION_GRAPH.get(current, set())
        if new_status not in allowed:
            return False

        ch["status"] = new_status
        self._persist()

        # 触发状态变更回调
        self._invoke_callback(chapter_id, current, new_status, {
            "checkpoints": list(ch.get("checkpoints", {}).keys()),
            "block_reason": ch.get("block_reason"),
            "review_type": ch.get("review_type"),
            "review_round": ch.get("review_round"),
        })

        return True

    def record_checkpoint(self, chapter_id: str, name: str,
                          context_snapshot: Optional[dict] = None):
        """记录检查点。用于崩溃恢复。

        name: 'context_ready', 'writing_done', 'review_r1_done', 'canon_done' 等
        context_snapshot: 该轮的关键上下文, 序列化到 gbrain
        """
        ch = self._ensure_chapter(chapter_id)
        ch["checkpoints"][name] = {
            "timestamp": datetime.now().isoformat(),
        }

        if context_snapshot:
            cp_dir = self._checkpoint_dir()
            if cp_dir:
                os.makedirs(cp_dir, exist_ok=True)
                cp_file = os.path.join(cp_dir, f"{chapter_id}_{name}.yaml")
                with open(cp_file, "w", encoding="utf-8") as f:
                    yaml.dump(context_snapshot, f, allow_unicode=True)
                ch["checkpoints"][name]["snapshot_path"] = cp_file

        self._persist()

    def _checkpoint_dir(self) -> Optional[str]:
        if not self.brain_path:
            return None
        return os.path.join(self.brain_path, "pages", "agent", "checkpoints")

    def set_review_round(self, chapter_id: str, round_num: int):
        ch = self._ensure_chapter(chapter_id)
        ch["review_round"] = round_num
        self._persist()

    def set_review_type(self, chapter_id: str, review_type: str):
        ch = self._ensure_chapter(chapter_id)
        ch["review_type"] = review_type
        self._persist()

    def persist(self):
        """公开持久化接口"""
        self._persist()

    def mark_stale(self, chapter_id: str):
        """标记下游章节为过期——因上游变更需重写"""
        ch = self._ensure_chapter(chapter_id)
        old_status = ch.get("status", "IDLE")
        ch["status"] = "STALE"
        self._persist()

        self._invoke_callback(chapter_id, old_status, "STALE", {})

    def block(self, chapter_id: str, reason: str = ""):
        """阻塞管线——Canon冲突/严重审核问题"""
        ch = self._ensure_chapter(chapter_id)
        old_status = ch.get("status", "IDLE")
        ch["status"] = "BLOCKED"
        ch["block_reason"] = reason
        self._persist()

        self._invoke_callback(chapter_id, old_status, "BLOCKED", {"block_reason": reason})

    # ── 崩溃恢复 ──

    def recover(self, chapter_id: str) -> dict:
        """从 gbrain 检查点恢复, 不依赖 hermes-agent session。

        返回恢复指令: {action, round?, context?}
        """
        ch = self.state["pipeline"]["chapters"].get(chapter_id, {})
        status = ch.get("status", "IDLE")
        cps = ch.get("checkpoints", {})

        if status == "COMPLETE" or status == "COMPLETE_WITH_WARNINGS":
            return {"action": "already_complete"}

        if status == "BLOCKED":
            return {"action": "still_blocked", "reason": ch.get("block_reason", "")}

        if "canon_done" in cps:
            return {"action": "complete_chapter"}
        if "review_r3_done" in cps:
            return {"action": "run_character_and_canon"}
        if "review_r2_done" in cps:
            ctx = self._load_snapshot(chapter_id, "review_r2_done")
            return {"action": "continue_review", "round": 3, "context": ctx}
        if "review_r1_done" in cps:
            ctx = self._load_snapshot(chapter_id, "review_r1_done")
            return {"action": "continue_review", "round": 2, "context": ctx}
        if "writing_done" in cps:
            return {"action": "start_review", "round": 1}
        if "context_ready" in cps:
            return {"action": "start_writing"}
        return {"action": "start_from_beginning"}

    def get_status(self, chapter_id: str) -> str:
        """查询章节状态。不存在返回 IDLE。"""
        ch = self.state["pipeline"]["chapters"].get(chapter_id, {})
        return ch.get("status", "IDLE")

    def get_checkpoints(self, chapter_id: str) -> dict:
        """查询章节检查点 (按名称索引)。"""
        ch = self.state["pipeline"]["chapters"].get(chapter_id, {})
        return ch.get("checkpoints", {})

    def list_pending(self, novel_id: str) -> list:
        """列出指定小说中所有非终端状态的章节 ID。

        返回章节 ID 列表 (按章节序排序),
        这些章节需要恢复或继续执行。
        跳过 IDLE (未开始) 和 COMPLETE/COMPLETE_WITH_WARNINGS (已完成)。
        """
        pending = []
        for ch_id, ch_data in self.state["pipeline"]["chapters"].items():
            if not ch_id.startswith(novel_id):
                continue
            status = ch_data.get("status", "IDLE")
            if status in ("COMPLETE", "COMPLETE_WITH_WARNINGS", "IDLE"):
                continue
            pending.append(ch_id)
        pending.sort(key=lambda x: int(x.split("-")[-1]) if "-" in x else 0)
        return pending

    def _load_snapshot(self, chapter_id: str, checkpoint_name: str) -> dict:
        cp_dir = self._checkpoint_dir()
        if not cp_dir:
            return {}
        cp_file = os.path.join(cp_dir, f"{chapter_id}_{checkpoint_name}.yaml")
        if os.path.exists(cp_file):
            with open(cp_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        return {}

    # ── 章节级 barrier ──

    def is_chapter_unlocked(self, chapter_id: str) -> bool:
        """检查上一章是否已完成 Phase B (COMPLETE/COMPLETE_WITH_WARNINGS)"""
        chapters = self.state["pipeline"].get("chapters", {})
        try:
            num = int(chapter_id.replace("ch-", ""))
        except ValueError:
            return True

        if num <= 1:
            return True

        prev_id = f"ch-{num - 1:03d}"
        prev = chapters.get(prev_id)
        if prev is None:
            return True  # 前序章节不存在 → 没有屏障
        prev_status = prev.get("status", "IDLE")
        return prev_status in ("COMPLETE", "COMPLETE_WITH_WARNINGS")
