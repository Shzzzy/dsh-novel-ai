"""Novel AI — FastAPI 桥接层

提供 REST + WebSocket 端点，连接 React 前端和 Python Agent 引擎。
"""

import sys
import os
import yaml
from pathlib import Path

# 确保项目根在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import json

from agents.base import AgentConfig
from agents.skeleton_agent import SkeletonAgent
from engine.connection_manager import manager as conn_mgr
from brains.experience_book import (
    init_all_experience_books, ExperienceLifecycle,
    AGENT_BOOKS, PollutionGuard,
    PhaseDetector, BossDeconcretizationFilter,
    _boss_to_dict,
)

import uuid

from brains.learning_zone import get_learning_zone, CATEGORIES as LEARNING_CATEGORIES
from engine.settings_manager import (
    get_settings, Settings, LLMConfig, AGENT_ROLES, ROLE_RECOMMENDATIONS,
    PROVIDER_PRESETS, SettingsManager,
)

app = FastAPI(title="Novel AI Engine", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/health")
async def health():
    mgr = get_connection_manager()
    return {
        "status": "ok",
        "version": "0.1.0",
        "active_connections": len(mgr.connections),
    }

# ── 全局状态 ──
sessions: dict[str, dict] = {}  # novel_id → {answers, round, ...}

# ── 后台心跳 ──
_background_tasks_started = False


async def _start_background_tasks():
    global _background_tasks_started
    if _background_tasks_started:
        return
    _background_tasks_started = True
    asyncio.create_task(conn_mgr.start_ping_loop(interval=15))


# ═══════════════════════════════════════════════════════════════
# REST API
# ═══════════════════════════════════════════════════════════════

class NextQuestionRequest(BaseModel):
    novel_id: str
    summary: str = ""
    target_words: int = 0
    answers: dict[str, str] = {}  # {question: answer}
    round_number: int = 0
    total_rounds: int = 12
    excluded_directions: list[str] = []   # §6.2.2 排除已生成方向
    api_key: str = ""
    model: str = "deepseek-chat"

class NextQuestionResponse(BaseModel):
    question: str
    transition: str
    suggestions: list[str]   # 3 regular
    wild_suggestion: str     # 第四便签

class SkeletonRequest(BaseModel):
    novel_id: str
    answers: dict[str, str]
    summary: str = ""
    target_words: int = 0
    api_key: str = ""
    model: str = "deepseek-chat"

class GeneratedEvent(BaseModel):
    id: str
    order: int
    title: str
    description: str
    type: str = "key"

class GeneratedPlot(BaseModel):
    id: str
    order: int
    title: str
    description: str

class SkeletonResponse(BaseModel):
    events: list[GeneratedEvent]
    plots: list[GeneratedPlot]
    total_chapters: int = 0  # P1-04: 全书总章数 (= len(plots))


class CreateNovelRequest(BaseModel):
    title: str = "未命名"
    summary: str = ""
    style_template_id: str = ""
    target_words: int = 0


class CreateNovelResponse(BaseModel):
    novel_id: str
    brain_path: str
    status: str


class StyleAnalyzeRequest(BaseModel):
    sample_text: str = ""


class StyleAnalyzeResponse(BaseModel):
    name: str
    tags: list[str]
    style_prompt: str
    parameters: dict = {}


class AnalyzeOutlineRequest(BaseModel):
    outline: str = ""
    target_words: int = 50000


@app.get("/api/health")
async def health():
    await _start_background_tasks()
    return {
        "status": "ok",
        "engine": "Novel AI v0.1",
        "ws": conn_mgr.stats(),
    }


@app.post("/api/skeleton/analyze-outline")
async def analyze_outline(req: AnalyzeOutlineRequest):
    """大纲解析 —— 提取人物/冲突/世界观/未展开线索 (§6.2 大纲锚定)"""
    from agents.skeleton_agent import SkeletonAgent

    config = AgentConfig(name="skeleton", model="deepseek-chat", api_key="",
                         provider="deepseek", temperature=0.5)
    agent = SkeletonAgent(config)
    analysis = await agent.analyze_outline(req.outline, req.target_words)
    return analysis


@app.post("/api/style/analyze", response_model=StyleAnalyzeResponse)
async def analyze_style(req: StyleAnalyzeRequest):
    """文风准备——分析上传的范文文本，生成文风参数卡片 (§6.1 [2])

    有 API Key 时调用 LLM 进行深度分析；无 API Key 时使用启发式统计回退。
    """
    from agents.style_agent import StyleAgent

    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    config = AgentConfig(
        name="style",
        model="deepseek-chat",
        api_key=api_key,
        provider="deepseek",
        temperature=0.3,
    )
    agent = StyleAgent(config)
    template = await agent.analyze_style(req.sample_text)

    return StyleAnalyzeResponse(
        name=template.name,
        tags=template.tags,
        style_prompt=template.stylePrompt,
        parameters=template.parameters,
    )


@app.get("/api/style/templates")
async def list_style_templates():
    """列出所有可用文风模板（内置预设 + 设计文档 §9.2 定义的结构）。"""
    from agents.style_agent import STYLE_DEFAULTS

    templates = []
    for tid, style_prompt in STYLE_DEFAULTS.items():
        name = tid.replace("style-", "").replace("-", " ").title()
        templates.append({
            "id": tid,
            "name": name,
            "stylePrompt": style_prompt,
            "tags": ["内置"],
            "parameters": {},
        })

    return {"templates": templates}


def _init_novel_gbrain(novel_id: str, style_template_id: str = ""):
    """DESIGN_DOC §19.2: 创建小说的完整 GBrain 初始化序列。

    1. mkdir pages/ 子目录
    2. gbrain init (SQLite FTS5 建表)
    3. 填写初始 pipeline-state.yaml
    """
    import uuid
    from brains.gbrain_wrapper import brain_path_for, brain_write_page

    bp = brain_path_for(novel_id)

    # 确保子目录存在(含 agent/sessions/ 用于过程记忆 §5.1)
    for sub in ("character", "canon", "world", "event", "content",
                 "style", "context", "agent"):
        os.makedirs(os.path.join(bp, "pages", sub), exist_ok=True)
    # 方案A: 过程记忆 sessions 子目录
    for agent_name in ("writer", "reviewer", "context", "character", "canon"):
        os.makedirs(os.path.join(bp, "pages", "agent", "sessions", agent_name), exist_ok=True)

    # 写初始 pipeline-state.yaml (含 sessions 状态)
    pipeline_state = {
        "pipeline": {
            "novel_id": novel_id,
            "current_chapter": 0,
            "chapters": {},
            "created_at": str(uuid.uuid4()),
        },
        "sessions": {
            "initialized": True,
            "agents_with_memory": [],
        },
    }
    import yaml
    agent_dir = os.path.join(bp, "pages", "agent")
    os.makedirs(agent_dir, exist_ok=True)
    with open(os.path.join(agent_dir, "pipeline-state.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(pipeline_state, f, allow_unicode=True)

    # 写初始事件/情节骨架标记
    brain_write_page(bp, "event", "skeleton-init",
        "骨架初始状态",
        f"novel_id: {novel_id}\ncreated: True\nevents: []"
    )

    return bp


@app.post("/api/novels", response_model=CreateNovelResponse)
async def create_novel(req: CreateNovelRequest):
    """创建新小说, 初始化 GBrain 实例。"""
    import uuid
    novel_id = f"novel-{uuid.uuid4().hex[:8]}"
    bp = _init_novel_gbrain(novel_id, req.style_template_id)
    return CreateNovelResponse(
        novel_id=novel_id,
        brain_path=bp,
        status="ready",
    )


@app.post("/api/skeleton/next-question", response_model=NextQuestionResponse)
async def next_question(req: NextQuestionRequest):
    """骨架工坊——生成下一轮问题 + 便签建议"""
    config = AgentConfig(
        name="skeleton",
        model=req.model,
        api_key=req.api_key,
        provider="deepseek",
        temperature=0.7,
    )
    agent = SkeletonAgent(config)

    outline_analysis = req.answers.get("__outline_analysis__")
    if outline_analysis:
        try:
            outline_analysis = json.loads(outline_analysis) if isinstance(outline_analysis, str) else outline_analysis
        except (json.JSONDecodeError, TypeError):
            outline_analysis = None

    question, suggestions, wild = await agent.generate_dynamic_question(
        previous_answers=req.answers,
        round_number=req.round_number,
        total_rounds=req.total_rounds,
        target_words=req.target_words,
        excluded_directions=req.excluded_directions,
        outline_analysis=outline_analysis,
    )

    # 确保至少有 3 条常规建议
    while len(suggestions) < 3:
        suggestions.append("请分享更多关于这个方向的思考。")
    regular = suggestions[:3]

    # 第四便签——LLM生成的天马行空建议
    if not wild or len(wild) < 20:
        wild = "换个角度——如果你笔下这个角色的核心欲望不是复仇，而是某种更深层的东西？也许是连他自己都没意识到的东西。"

    # 过渡语
    # 骨架 Agent 人设: 乐观大局观 + "画饼"技能
    transitions = [
        "好的。让我根据你说的来推一下——这个方向很有意思。",
        "有意思。这个方向让我想到了一些之前没注意到的事——这个骨架越来越有味道了。",
        "收到。前面聊得够深了，现在把它们串起来——这个结构，稳了。",
        "好，这一轮的设定确认了。我已经能看到这本书大卖的样子了——接下来，",
    ]
    transition = transitions[req.round_number % len(transitions)]

    return NextQuestionResponse(
        question=question,
        transition=transition,
        suggestions=regular,
        wild_suggestion=wild,
    )


@app.post("/api/skeleton/generate", response_model=SkeletonResponse)
async def generate_skeleton(req: SkeletonRequest):
    """骨架工坊——从累积答案生成事件和情节"""
    config = AgentConfig(
        name="skeleton",
        model=req.model,
        api_key=req.api_key,
        provider="deepseek",
        temperature=0.7,
    )
    agent = SkeletonAgent(config)

    events, plots_map = await agent.generate_skeleton(
        answers=req.answers,
        summary=req.summary,
        target_words=req.target_words,
    )

    evts = [
        GeneratedEvent(
            id=e.id, order=e.order, title=e.title,
            description=e.description, type=e.type or "key",
        )
        for e in events
    ]

    plts = []
    for event_id, plot_list in plots_map.items():
        for p in plot_list:
            plts.append(GeneratedPlot(
                id=p.id, order=p.order, title=p.title,
                description=p.description,
            ))

    return SkeletonResponse(events=evts, plots=plts, total_chapters=len(plts))


# ═══════════════════════════════════════════════════════════════
# WebSocket — Agent 实时通信
# ═══════════════════════════════════════════════════════════════

@app.websocket("/ws/agent")
async def agent_websocket(ws: WebSocket):
    await ws.accept()
    await _start_background_tasks()

    conn_id = uuid.uuid4().hex[:12]
    conn = conn_mgr.add(ws, conn_id)
    novel_id: Optional[str] = None

    async def _send(typ: str, agent: str, text: str, **kw):
        await conn_mgr.send_json(conn_id, {"type": typ, "agent": agent, "text": text, **kw})

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

            # 收到任何消息都视为活跃
            conn_mgr.reset_idle(conn_id)

            if msg_type == "subscribe":
                novel_id = data.get("novel_id", "unknown")
                conn_mgr.subscribe(conn_id, novel_id)
                await _send("agent_log", "orchestrator",
                            f">>> WebSocket 已连接: {novel_id} <<<")

            elif msg_type == "ping":
                await conn_mgr.send_json(conn_id, {"type": "pong"})

            elif msg_type == "run_chapter":
                # 启动写作管线 — 流式返回生成内容
                from agents.base import AgentConfig
                from agents.orchestrator import Orchestrator
                from sync.sync_layer import SyncLayer
                from models.novel import Chapter, Plot, Event, ContextPackage
                from agents.context_agent import ContextAgent
                from brains.gbrain_wrapper import brain_path_for

                novel_id = data.get("novel_id", novel_id or "unknown")
                chapter_order = data.get("chapter_order", 1)
                chapter_title = data.get("chapter_title", f"第{chapter_order}章")
                event_title = data.get("event_title", "")
                plot_title = data.get("plot_title", "")
                event_type = data.get("event_type", "key")
                target_words = data.get("target_words", 50000)
                style_template_id = data.get("style_template_id", "")
                api_key = data.get("api_key", "")
                model = data.get("model", "deepseek-chat")
                review_type_override = data.get("review_type", None)  # 前端手动覆盖
                total_chapters = data.get("total_chapters", 0)  # P1-04: 全书总章数
                events_data = data.get("events", [])              # P1-07: 伏笔冷却检测

                await _send("agent_log", "orchestrator",
                            f">>> 启动写作管线: 第{chapter_order}章 <<<",
                            emoji="🎯", color="#e6ddd0")

                # 构建上下文 — api_key 贯穿所有 Agent
                ak = api_key or data.get("apiKey", "")
                config = AgentConfig(name="orchestrator", model=model,
                                     api_key=ak, provider="deepseek")
                sync_layer = SyncLayer()

                chapter = Chapter(novel_id=novel_id, order=chapter_order,
                                  title=chapter_title)
                plot = Plot(title=plot_title)
                event = Event(title=event_title, type=event_type)
                context_pkg = ContextPackage()

                # 上下文组装
                try:
                    ctx_agent = ContextAgent(config)
                    bp = brain_path_for(novel_id)
                    context_pkg = await ctx_agent.fetch_from_gbrain(
                        bp, event, chapter
                    )
                    await _send("agent_log", "context",
                                f"上下文组装完成: {len(context_pkg.key_beats)} 个节拍",
                                emoji="🔎", color="#7eb8da")
                except Exception:
                    await _send("agent_log", "context",
                                "上下文组装失败, 使用默认上下文",
                                emoji="🔎", color="#7eb8da")

                # ── P1-07: 伏笔冷却期检测 ──
                if events_data and len(events_data) > 0:
                    try:
                        from engine.foreshadowing_tracker import (
                            ForeshadowingTracker, CooldownConfig
                        )
                        tracker = ForeshadowingTracker()
                        cooldown_alerts = tracker.from_events(
                            events_data, chapter_order
                        )
                        context_pkg.cooldown_alerts = [
                            {
                                "level": a.level,
                                "foreshadowing_id": a.foreshadowing_id,
                                "description": a.foreshadowing_description,
                                "type": a.cooldown_type,
                                "message": a.message,
                                "hint_count": a.hint_count,
                                "required_hints": a.required_hints,
                            }
                            for a in cooldown_alerts
                        ]
                        # 推送告警给前端 agent discussion
                        for alert in cooldown_alerts:
                            await _send(
                                "agent_log", "context",
                                f"🚨 冷却期告警: {alert.message}",
                                emoji="⏳", color="#f0a060"
                            )
                        if not cooldown_alerts:
                            await _send(
                                "agent_log", "context",
                                f"✅ 伏笔冷却期检查通过（{len(tracker._items)} 条伏笔）",
                                emoji="✅", color="#7eb8da"
                            )
                    except Exception as e:
                        logger.warning("Foreshadowing cooldown check failed: %s", e)

                # 获取文风
                from agents.style_agent import StyleAgent, STYLE_DEFAULTS
                style_prompt = STYLE_DEFAULTS.get(
                    style_template_id,
                    "自然流畅的中文叙事风格。"
                )

                # 启动管线
                orchestrator = Orchestrator(config, sync_layer)
                if brain_path_for(novel_id):
                    try:
                        from engine.pipeline_state import PipelineStateManager
                        state_mgr = PipelineStateManager(brain_path_for(novel_id))
                        orchestrator.set_pipeline_state(state_mgr)
                        # WebSocket pipeline state change events
                        _captured_novel = novel_id
                        import asyncio as _asyncio
                        def _on_pipeline_change(chapter_id, old_status, new_status, data):
                            try:
                                _asyncio.create_task(conn_mgr.broadcast_json(
                                    _captured_novel, {
                                        "type": "pipeline_state_change",
                                        "chapter_id": chapter_id,
                                        "old_status": old_status,
                                        "new_status": new_status,
                                        "data": data,
                                    },
                                    exclude=conn_id,
                                ))
                            except Exception:
                                pass
                        state_mgr.set_on_state_change(_on_pipeline_change)
                    except Exception:
                        pass
                    try:
                        from brains.session_memory import SessionMemory
                        sm = SessionMemory(brain_path_for(novel_id))
                        orchestrator.set_session_memory(sm)
                    except Exception:
                        pass

                ch_words_estimate = 3500 if target_words <= 100000 else 2800

                try:
                    # 审核级别: 前端覆盖 > 自动判定 > 默认 full
                    if review_type_override and review_type_override in ("full", "quick", "skip"):
                        review_type = review_type_override
                        await _send("agent_log", "orchestrator",
                                    f"审核级别: {review_type} (手动覆盖)",
                                    emoji="🎚️", color="#c4a460")
                    else:
                        from agents.orchestrator import determine_review_type
                        review_type = determine_review_type(
                            chapter_order=chapter_order,
                            event_type=event_type,
                        )
                        review_labels = {"full": "🔴 完整审核", "quick": "🟡 快速审核", "skip": "🟢 跳过审核"}
                        await _send("agent_log", "orchestrator",
                                    f"审核级别: {review_labels.get(review_type, review_type)} (自动判定)",
                                    emoji="📋", color="#e6ddd0")

                    async for event_data in orchestrator.run_pipeline(
                        chapter=chapter, plot=plot, event=event,
                        context_pkg=context_pkg, style_prompt=style_prompt,
                        review_type=review_type,
                        target_words=target_words,
                        expected_chapter_words=ch_words_estimate,
                    ):
                        agent = event_data.get("agent", "orchestrator")
                        text = event_data.get("text", "")

                        if agent == "writer" and text.startswith("> 生成中"):
                            continue
                        elif agent == "writer" and not text.startswith(">"):
                            await _send("chapter_token", agent, text)
                        else:
                            await _send("agent_log", agent, text,
                                        emoji=event_data.get("emoji", ""),
                                        color=event_data.get("color", ""))

                    await _send("pipeline_complete", "orchestrator",
                                f"第{chapter_order}章 完成",
                                chapter_content=chapter.content,
                                word_count=chapter.word_count,
                                is_novel_complete=(total_chapters > 0 and chapter_order >= total_chapters))

                except Exception as e:
                    await _send("agent_log", "orchestrator",
                                f"⚠️ 管线异常: {str(e)[:100]}",
                                emoji="⚠️")
                    await _send("pipeline_complete", "orchestrator",
                                f"第{chapter_order}章 部分完成",
                                chapter_content=chapter.content or "",
                                word_count=chapter.word_count or 0,
                                error=str(e)[:100],
                                is_novel_complete=False)

            elif msg_type == "ping":
                await conn_mgr.send_json(conn_id, {"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        conn_mgr.remove(conn_id)


# ═══════════════════════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════════════════════

def _init_global_brains():
    """初始化公用层 ~/.novel-ai/global-brains/rules/ — 存放跨小说验证的写作原则。"""
    import os
    global_rules_dir = os.path.expanduser("~/.novel-ai/global-brains/rules/pages")
    os.makedirs(global_rules_dir, exist_ok=True)
    seed_file = os.path.join(global_rules_dir, "cross_novel_principles.md")
    if not os.path.exists(seed_file):
        with open(seed_file, "w", encoding="utf-8") as f:
            f.write("""# 跨小说写作原则 (公用层)

> 本文件存放被多本小说验证过的写作原则。每条原则来自具体写作经验，但已剥离具体人名/情节。
> 新小说启动时，Agent 自动从此处检索最相关原则。

## 已验证原则 (confidence >= 8)

*暂无——当某条经验在2本以上小说中被验证有效后，自动从 writer.book 提升到此。*

## 种子原则 (来自行业公认写作技法)

### 展现而非告知
- 规则: 情感高潮处，用"角色对物件的小动作"代替"情绪词"。如不写"她很紧张"→写"她把茶盏端起来，没喝，又放下"。
- 适用范围: 所有文风、所有章节类型
- 来源: 行业金律

### 对话个性
- 规则: 每句对话同时做到两件事——推进剧情 + 揭示说话者性格。写完遮住名字，能分辨谁在说话。
- 适用范围: 所有文风
- 来源: 行业金律

### 章末钩子
- 规则: 每章结尾必须留一个"读者今晚睡不着"的悬念——未完成的动作/未说出口的话/或突然出现的威胁。禁止"沉沉睡去""弯了弯嘴角"。
- 适用范围: 所有文风、所有章节
- 来源: 行业金律
""")


def get_session_memory_for_novel(novel_id: str):
    """获取指定小说的 SessionMemory 实例 (方案A)。

    每次调用返回新的 SessionMemory 对象, 但底层 SQLite 数据库共享
    (通过 brain_path_for 定位)。
    """
    from brains.gbrain_wrapper import brain_path_for
    from brains.session_memory import SessionMemory
    bp = brain_path_for(novel_id)
    return SessionMemory(bp)


# ═══════════════════════════════════════════════════════════════
# 经验本审阅 API (§5.7.5 第三道闸门 — 用户可审查面板)
# ═══════════════════════════════════════════════════════════════

# 懒加载全局经验本实例
_experience_books = None
_experience_lifecycle = None


def _get_experience_books():
    global _experience_books
    if _experience_books is None:
        _experience_books = init_all_experience_books()
    return _experience_books


def _get_lifecycle():
    global _experience_lifecycle
    if _experience_lifecycle is None:
        _experience_lifecycle = ExperienceLifecycle(_get_experience_books())
    return _experience_lifecycle


@app.get("/api/experience/agents")
async def list_experience_agents():
    """列出所有 Agent 经验本概览"""
    books = _get_experience_books()
    agents = []
    for agent in AGENT_BOOKS:
        book = books[agent]
        agents.append({
            "agent": agent,
            "label": {
                "writer": "写作 Agent",
                "reviewer": "审核 Agent",
                "skeleton": "骨架 Agent",
                "context": "上下文 Agent",
                "character": "人物 Agent",
                "canon": "正典 Agent",
                "style": "文风 Agent",
            }.get(agent, agent),
            "total": book.count(),
            "active": book.count("active"),
            "unverified": book.count("unverified"),
            "archived": book.count("archived"),
            "deprecated": book.count("deprecated"),
        })
    return {"agents": agents}


@app.get("/api/experience/{agent}/entries")
async def list_experience_entries(
    agent: str,
    status: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
):
    """列出指定 Agent 的经验条目"""
    books = _get_experience_books()
    if agent not in books:
        return {"error": f"Unknown agent: {agent}"}, 404

    book = books[agent]

    if search and search.strip():
        entries = book.search(search, limit=limit, status_filter=status or None)
    else:
        entries = book.list_all(status or None)
        # 手动分页
        entries = entries[offset:offset + limit]

    return {
        "agent": agent,
        "entries": [
            {
                "id": e.id,
                "type": e.type,
                "insight": e.insight,
                "confidence": e.confidence,
                "status": e.status,
                "validated_count": e.validated_count,
                "tags": e.tags,
                "severity": e.severity,
                "source": e.source,
                "cross_referenced_from": e.cross_referenced_from,
                "discovered_in": e.discovered_in,
                "avoidance": e.avoidance,
                "created_at": e.created_at,
                "last_updated": e.last_updated,
                "last_triggered": e.last_triggered,
            }
            for e in entries
        ],
        "total": len(entries),
    }


@app.post("/api/experience/{agent}/entries/{entry_id}/validate")
async def validate_experience_entry(agent: str, entry_id: str):
    """验证一条经验（用户确认有效）"""
    books = _get_experience_books()
    if agent not in books:
        return {"error": f"Unknown agent: {agent}"}, 404

    lc = _get_lifecycle()
    try:
        lc.validate_experience(agent, entry_id)
        updated = books[agent].get_entry(entry_id)
        if updated is None:
            return {"error": "Entry not found"}, 404
        return {
            "ok": True,
            "entry": {
                "id": updated.id,
                "confidence": updated.confidence,
                "validated_count": updated.validated_count,
                "status": updated.status,
            },
        }
    except Exception as e:
        return {"error": str(e)}, 500


@app.post("/api/experience/{agent}/entries/{entry_id}/deprecate")
async def deprecate_experience_entry(agent: str, entry_id: str):
    """废弃一条经验（用户认为无效或过时）"""
    books = _get_experience_books()
    if agent not in books:
        return {"error": f"Unknown agent: {agent}"}, 404

    lc = _get_lifecycle()
    try:
        lc.deprecate(agent, entry_id)
        updated = books[agent].get_entry(entry_id)
        if updated is None:
            return {"error": "Entry not found"}, 404
        return {
            "ok": True,
            "entry": {
                "id": updated.id,
                "status": updated.status,
            },
        }
    except Exception as e:
        return {"error": str(e)}, 500


@app.get("/api/experience/{agent}/stats")
async def experience_agent_stats(agent: str):
    """单个 Agent 经验本统计 + 修剪建议"""
    books = _get_experience_books()
    if agent not in books:
        return {"error": f"Unknown agent: {agent}"}, 404

    lc = _get_lifecycle()
    book = books[agent]
    return {
        "agent": agent,
        "total": book.count(),
        "active": book.count("active"),
        "unverified": book.count("unverified"),
        "archived": book.count("archived"),
        "deprecated": book.count("deprecated"),
        "prune_suggestions": lc.get_prune_suggestions(agent),
    }


@app.get("/api/experience/export")
async def export_experience_for_review():
    """导出所有经验本供审计（防污染检查）"""
    books = _get_experience_books()
    return PollutionGuard.export_for_review(books)


# ═══════════════════════════════════════════════════════════════
# BOSS 交互经验 API (§5.7.11 — P2-07)
# ═══════════════════════════════════════════════════════════════


class BossRecordRequest(BaseModel):
    agent_name: str
    boss_message: str
    agent_response: str = ""
    chapter_order: int = 0
    total_chapters: int = 0


@app.post("/api/boss/record")
async def record_boss_interaction(body: BossRecordRequest):
    """录制一次 BOSS 交互经验（§5.7.11）"""
    books = _get_experience_books()
    if body.agent_name not in books:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {body.agent_name}")

    # 检测当前情绪相态
    detector = PhaseDetector(total_chapters_hint=body.total_chapters or 50)
    phase = detector.detect(
        chapter_order=body.chapter_order,
        total_chapters=body.total_chapters,
    )

    book = books[body.agent_name]
    entry = book.record_boss_interaction(
        body.boss_message, body.agent_response, phase
    )
    if entry is None:
        return {
            "ok": False,
            "skipped": True,
            "reason": "包含情节偏好内容，已按去具体化规则过滤",
        }

    return {
        "ok": True,
        "entry": _boss_to_dict(entry),
        "phase": phase,
    }


@app.get("/api/boss/interactions/{agent}")
async def list_boss_interactions(agent: str):
    """列出指定 Agent 的 BOSS 交互经验"""
    books = _get_experience_books()
    if agent not in books:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")

    book = books[agent]
    entries = book.get_boss_interactions()
    return {
        "agent": agent,
        "total": len(entries),
        "entries": [_boss_to_dict(e) for e in entries],
    }


@app.delete("/api/boss/interactions/{agent}/{entry_id}")
async def delete_boss_interaction(agent: str, entry_id: str):
    """删除一条 BOSS 交互经验"""
    books = _get_experience_books()
    if agent not in books:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")

    success = books[agent].delete_boss_entry(entry_id)
    if success:
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Entry not found")


# ═══════════════════════════════════════════════════════════════
# Canon 冲突面板 API (§7.1.9 — P2-14)
# ═══════════════════════════════════════════════════════════════

from brains.canon_store import (
    get_conflicts,
    resolve_conflict,
    delete_conflict,
    get_facts,
    get_canon_stats,
    CanonConflictRecord,
)


@app.get("/api/canon/{novel_id}/conflicts")
async def list_canon_conflicts(novel_id: str, resolved: str = "all"):
    """列出小说所有 Canon 冲突。

    Query params:
        resolved: "all" | "unresolved" | "resolved" (default "all")
    """
    conflicts = get_conflicts(novel_id)

    if resolved == "unresolved":
        conflicts = [c for c in conflicts if not c.resolved]
    elif resolved == "resolved":
        conflicts = [c for c in conflicts if c.resolved]

    stats = get_canon_stats(novel_id)

    return {
        "novel_id": novel_id,
        "stats": stats,
        "conflicts": [c.to_dict() for c in conflicts],
    }


@app.get("/api/canon/{novel_id}/facts")
async def list_canon_facts(novel_id: str):
    """列出小说所有 Canon 事实"""
    facts = get_facts(novel_id)
    return {
        "novel_id": novel_id,
        "facts": [f.__dict__ if hasattr(f, '__dict__') else f for f in facts],
    }


@app.get("/api/canon/{novel_id}/stats")
async def canon_stats(novel_id: str):
    """Canon 统计摘要（用于侧边栏徽标）"""
    return get_canon_stats(novel_id)


class ResolveConflictRequest(BaseModel):
    resolution_note: str = ""
    maintain_fact: str = ""  # "a" | "b" | "merge" | ""


@app.post("/api/canon/{novel_id}/conflicts/{conflict_id}/resolve")
async def resolve_canon_conflict(
    novel_id: str,
    conflict_id: str,
    body: ResolveConflictRequest,
):
    """标记冲突为已解决"""
    result = resolve_conflict(
        novel_id=novel_id,
        conflict_id=conflict_id,
        resolution_note=body.resolution_note,
        maintain_fact=body.maintain_fact,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Conflict not found")
    return {"ok": True, "conflict": result.to_dict()}


@app.delete("/api/canon/{novel_id}/conflicts/{conflict_id}")
async def delete_canon_conflict(novel_id: str, conflict_id: str):
    """删除冲突记录"""
    ok = delete_conflict(novel_id, conflict_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Conflict not found")
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════
# AI 创作学习区 API (§15)
# ═══════════════════════════════════════════════════════════════

class AddLearningMaterialRequest(BaseModel):
    title: str
    category: str = "其他"
    content: str


class UpdateLearningMaterialRequest(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    content: Optional[str] = None


@app.get("/api/learning/categories")
async def learning_categories():
    """获取所有分类"""
    return {"categories": LEARNING_CATEGORIES}


@app.get("/api/learning")
async def learning_list(q: str = "", category: str = ""):
    """搜索 + 分类筛选"""
    zone = get_learning_zone()
    materials = zone.list_materials(
        category=category if category else None,
        q=q if q else None,
    )
    return {"materials": [m.to_dict() for m in materials]}


@app.get("/api/learning/stats")
async def learning_stats():
    """跨小说统计"""
    zone = get_learning_zone()
    return zone.get_stats()


@app.get("/api/learning/{material_id}")
async def learning_get(material_id: str):
    """获取单条资料"""
    zone = get_learning_zone()
    material = zone.get_material(material_id)
    if not material:
        raise HTTPException(status_code=404, detail="资料不存在")
    return material.to_dict()


@app.post("/api/learning")
async def learning_add(req: AddLearningMaterialRequest):
    """新增学习资料"""
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="标题不能为空")
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="内容不能为空")
    zone = get_learning_zone()
    material = zone.add_material(
        title=req.title.strip(),
        category=req.category,
        content=req.content,
    )
    return material.to_dict()


@app.put("/api/learning/{material_id}")
async def learning_update(material_id: str, req: UpdateLearningMaterialRequest):
    """更新学习资料"""
    zone = get_learning_zone()
    material = zone.update_material(
        material_id=material_id,
        title=req.title,
        category=req.category,
        content=req.content,
    )
    if not material:
        raise HTTPException(status_code=404, detail="资料不存在")
    return material.to_dict()


@app.delete("/api/learning/{material_id}")
async def learning_delete(material_id: str):
    """删除学习资料"""
    zone = get_learning_zone()
    if not zone.delete_material(material_id):
        raise HTTPException(status_code=404, detail="资料不存在")
    return {"ok": True}


# ⚠️ __main__ 入口块已移动到文件末尾。
# 原因: uvicorn.run() 会阻塞进程, 若放在文件中部, 其后定义的路由永远不会注册 (404 bug)。
# 此修复同时应用于 dsh-novel-ai 插件运行时 与 源项目 engine/main.py。

# ═══════════════════════════════════════════════════════════════
# 章节版本历史 API — P2-11 (§16)
# ═══════════════════════════════════════════════════════════════

from brains.chapter_version_store import get_version_store


class SaveVersionRequest(BaseModel):
    content: str
    word_count: int = 0
    title: str = ""
    label: str = ""


@app.get("/api/novels/{novel_id}/chapters/{chapter_id}/versions/stats")
async def version_stats(novel_id: str, chapter_id: str):
    """版本统计"""
    store = get_version_store(novel_id, chapter_id)
    return store.stats()


@app.get("/api/novels/{novel_id}/chapters/{chapter_id}/versions")
async def list_versions(novel_id: str, chapter_id: str):
    """列出章节所有版本（仅元数据，不含正文）"""
    store = get_version_store(novel_id, chapter_id)
    metas = store.get_versions()
    return {"versions": [m.to_dict() for m in metas]}


@app.post("/api/novels/{novel_id}/chapters/{chapter_id}/versions")
async def save_version_manual(
    novel_id: str, chapter_id: str, req: SaveVersionRequest
):
    """手动保存版本快照"""
    store = get_version_store(novel_id, chapter_id)
    version_id = store.save_version(
        content=req.content,
        word_count=req.word_count,
        title=req.title,
        source="manual",
        label=req.label or "手动快照",
    )
    if not version_id:
        return {"ok": True, "skipped": True, "reason": "内容未变化"}
    v = store.get_version(version_id)
    if v:
        return {**v.to_dict(), "version_id": version_id}
    return {"ok": True, "version_id": version_id}


@app.get("/api/novels/{novel_id}/chapters/{chapter_id}/versions/{version_id}")
async def get_version(novel_id: str, chapter_id: str, version_id: str):
    """获取完整版本（含正文）"""
    store = get_version_store(novel_id, chapter_id)
    v = store.get_version(version_id)
    if not v:
        raise HTTPException(status_code=404, detail="版本不存在")
    return v.to_dict()


@app.get(
    "/api/novels/{novel_id}/chapters/{chapter_id}/versions/{version_id_a}/diff/{version_id_b}"
)
async def diff_versions(
    novel_id: str, chapter_id: str, version_id_a: str, version_id_b: str
):
    """对比两个版本的差异"""
    store = get_version_store(novel_id, chapter_id)
    result = store.diff_versions(version_id_a, version_id_b)
    if result is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    return result


@app.post(
    "/api/novels/{novel_id}/chapters/{chapter_id}/versions/{version_id}/restore"
)
async def restore_version(novel_id: str, chapter_id: str, version_id: str):
    """恢复到指定版本"""
    store = get_version_store(novel_id, chapter_id)
    new_id = store.restore_version(version_id)
    if new_id is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    new_version = store.get_version(new_id)
    return new_version.to_dict() if new_version else {"ok": True, "version_id": new_id}


# ═══════════════════════════════════════════════════════════════
# 设置 API (§12) — LLM Provider 配置、API Key 管理、连接测试
# ═══════════════════════════════════════════════════════════════

class LLMConfigUpdate(BaseModel):
    """Per-agent LLM config update request."""
    provider: Optional[str] = None
    api_key: Optional[str] = None
    api_base: Optional[str] = None
    model: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None


class BatchLLMUpdate(BaseModel):
    """Batch update: apply base config to multiple roles."""
    base: LLMConfigUpdate
    roles: Optional[list[str]] = None  # None = all roles


class UserPrefsUpdate(BaseModel):
    theme: Optional[str] = None
    font_size: Optional[int] = None
    language: Optional[str] = None
    auto_save_interval: Optional[int] = None


@app.get("/api/settings/llm")
async def get_llm_configs():
    """Return all LLM configs (API keys masked)."""
    mgr = get_settings()
    configs = mgr.get_all_configs_safe()
    return {
        "configs": configs,
        "roles": {role: rec for role, rec in ROLE_RECOMMENDATIONS.items()},
        "providers": PROVIDER_PRESETS,
    }


@app.put("/api/settings/llm/{role}")
async def update_llm_config(role: str, req: LLMConfigUpdate):
    """Update LLM config for one agent role."""
    if role not in AGENT_ROLES:
        raise HTTPException(status_code=400, detail=f"未知角色: {role}。可用: {AGENT_ROLES}")

    updates = req.model_dump(exclude_none=True)
    mgr = get_settings()
    cfg = mgr.update_llm_config(role, updates)
    return {
        "ok": True,
        "role": role,
        "config": {
            "provider": cfg.provider,
            "api_key_set": bool(cfg.api_key),
            "api_base": cfg.api_base,
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "top_p": cfg.top_p,
        },
    }


@app.put("/api/settings/llm")
async def batch_update_llm(req: BatchLLMUpdate):
    """Batch apply LLM config to multiple roles."""
    base = req.base.model_dump(exclude_none=True)
    mgr = get_settings()
    mgr.update_all_llm(base, req.roles)
    return {
        "ok": True,
        "applied_roles": req.roles or AGENT_ROLES,
        "fields": list(base.keys()),
    }


@app.post("/api/settings/llm/test/{role}")
async def test_llm_connection(role: str):
    """Test LLM connection for a specific role."""
    if role not in AGENT_ROLES:
        raise HTTPException(status_code=400, detail=f"未知角色: {role}")

    mgr = get_settings()
    cfg = mgr.get_llm_config(role)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"未找到 {role} 的配置")

    result = await SettingsManager.test_connection(cfg)
    return {"role": role, **result}


@app.post("/api/settings/llm/test")
async def test_all_connections():
    """Test all configured LLM connections."""
    mgr = get_settings()
    results = {}
    tasks = []

    for role in AGENT_ROLES:
        cfg = mgr.get_llm_config(role)
        if cfg and cfg.api_key:
            tasks.append((role, SettingsManager.test_connection(cfg)))

    if not tasks:
        return {"total": 0, "results": {}, "ok": False, "error": "没有已配置 API Key 的角色"}

    for role, task in tasks:
        results[role] = await task

    ok_count = sum(1 for r in results.values() if r.get("ok"))
    return {
        "total": len(tasks),
        "ok_count": ok_count,
        "results": results,
        "ok": ok_count == len(tasks),
    }


@app.get("/api/settings/prefs")
async def get_user_prefs():
    """Get user preferences."""
    mgr = get_settings()
    from dataclasses import asdict
    return asdict(mgr.get_user_prefs())


@app.put("/api/settings/prefs")
async def update_user_prefs(req: UserPrefsUpdate):
    """Update user preferences."""
    mgr = get_settings()
    mgr.update_user_prefs(req.model_dump(exclude_none=True))
    from dataclasses import asdict
    return asdict(mgr.get_user_prefs())


# ═══════════════════════════════════════════════════════════════
# 入口 (置于文件末尾, 确保所有路由注册后再启动服务)
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    _init_global_brains()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")
