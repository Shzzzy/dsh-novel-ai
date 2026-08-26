#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# dsh-novel-ai 自救脚本 (self-heal)
#
# 场景: Agent / 会话 / 进程崩溃导致服务中断后, 一键恢复现场。
# 自动完成: 清理残留进程与陈旧 PID → 拉起引擎+静态服务+转发 → 健康检查 → 报告。
#
# 用法:
#   bash scripts/self-heal.sh           检测并恢复 (幂等: 已在运行则复用)
#   bash scripts/self-heal.sh --force   强制干净重启 (先停后启)
#   bash scripts/self-heal.sh --status  只报告状态, 不执行任何动作
#   bash scripts/self-heal.sh --verify  恢复后跑全接口自检 (verify.js)
#
# 退出码: 0 = 恢复成功/已就绪, 1 = 恢复失败
# ═══════════════════════════════════════════════════════════════════════════
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
LAUNCHER="node $PLUGIN_DIR/lib/launcher.js"
VERIFY="node $PLUGIN_DIR/lib/verify.js"

# 与 launcher.js 保持一致的数据目录
DATA_ROOT="${DSH_HOME:-$HOME/.novel-ai}/novel-ai"
PID_FILE="$DATA_ROOT/launcher.pid"
ENGINE_PID_FILE="$DATA_ROOT/engine.pid"
REPORT_FILE="$PLUGIN_DIR/self-heal-report.json"

ENGINE_PORT=8765
FORWARD_PORT=8766
WEB_PORT_START=5173

MODE="heal"   # heal | force | status | verify

for arg in "$@"; do
  case "$arg" in
    --force)  MODE="force" ;;
    --status) MODE="status" ;;
    --verify) MODE="verify" ;;
  esac
done

# ── 工具函数 ─────────────────────────────────────────────────────────────────
log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] ⚠ %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

# 端口探测: timeout 1s 防止 TCP 连接阻塞 (不可用端口会等系统超时)
port_in_use() {
  ( timeout 1 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" ) 2>/dev/null && return 0 || return 1
}

pid_alive() { kill -0 "$1" 2>/dev/null; }

# 清理陈旧的 PID 文件 (记录在案的进程已死)
clean_stale_pids() {
  for f in "$PID_FILE" "$ENGINE_PID_FILE"; do
    [[ -f "$f" ]] || continue
    local p
    p="$(cat "$f" 2>/dev/null | tr -d '[:space:]')"
    if [[ -n "$p" ]] && ! pid_alive "$p"; then
      warn "清除陈旧 PID 文件: $f (pid=$p 已不存在)"
      rm -f "$f"
    fi
  done
}

# 清理无主引擎孤儿进程 (引擎在跑但 launcher 进程已死, 且无 PID 记录)
clean_orphans() {
  local orphans
  orphans="$(pgrep -f "engine/main.py" 2>/dev/null || true)"
  [[ -z "$orphans" ]] && return 0
  local eng_pid=""
  [[ -f "$ENGINE_PID_FILE" ]] && eng_pid="$(cat "$ENGINE_PID_FILE" 2>/dev/null | tr -d '[:space:]')"
  for p in $orphans; do
    if [[ "$p" != "$eng_pid" ]]; then
      warn "终止孤儿引擎进程 pid=$p (无主/未被记录)"
      kill -9 "$p" 2>/dev/null || true
    fi
  done
}

# ── 状态报告 ─────────────────────────────────────────────────────────────────
report_status() {
  local engine="✗ 未运行" web="✗ 未运行" fwd="✗ 未运行"
  port_in_use "$ENGINE_PORT"  && engine="✓ 运行中"
  port_in_use "$FORWARD_PORT" && fwd="✓ 运行中"
  local web_port=0
  for p in $(seq "$WEB_PORT_START" $((WEB_PORT_START + 9))); do
    if port_in_use "$p"; then web_port=$p; break; fi
  done
  [[ "$web_port" -gt 0 ]] && web="✓ 运行中 (端口 $web_port)"

  log "── Novel AI 状态 ──────────────────────────────"
  log "  引擎   : $engine   (端口 $ENGINE_PORT)"
  log "  转发   : $fwd      (端口 $FORWARD_PORT)"
  log "  界面   : $web"
  log "  数据目录: $DATA_ROOT"
  log "───────────────────────────────────────────────"
  [[ -n "${web_port:-0}" && "$web_port" -gt 0 ]] && log "  访问: http://localhost:$web_port"

  # 写报告文件
  local url="null"
  [[ "$web_port" -gt 0 ]] && url="http://localhost:$web_port"
  cat > "$REPORT_FILE" <<EOF
{
  "time": "$(date -Iseconds)",
  "mode": "$MODE",
  "engine": $(port_in_use "$ENGINE_PORT" && echo true || echo false),
  "forward": $(port_in_use "$FORWARD_PORT" && echo true || echo false),
  "webPort": $web_port,
  "url": "$url"
}
EOF
}

# ── 主流程 ───────────────────────────────────────────────────────────────────
case "$MODE" in
  status)
    report_status
    exit 0
    ;;
  verify)
    timeout 120 $VERIFY   # 超时保护: 自检最长 120s, 防异常挂死
    exit $?
    ;;
esac

log "Novel AI 自救脚本启动 (mode=$MODE)"

# 1. 清理陈旧 PID 与孤儿进程
clean_stale_pids
clean_orphans

# 2. force 模式: 先彻底停止
if [[ "$MODE" == "force" ]]; then
  log "force 模式: 先停止现有服务"
  $LAUNCHER stop >/dev/null 2>&1 || true
  sleep 2
  # 停止后兜底清理
  for p in $(pgrep -f "engine/main.py" 2>/dev/null || true); do
    warn "兜底终止残留引擎 pid=$p"; kill -9 "$p" 2>/dev/null || true
  done
fi

# 3. 拉起全套服务 (launcher 设计为前台常驻, 此处用 setsid 放入后台独立会话)
log "启动全套服务 (引擎 + 静态 + 转发 + 开窗)..."
mkdir -p "$DATA_ROOT/logs"
NODE_BIN="$(command -v node || echo node)"
setsid "$NODE_BIN" "$PLUGIN_DIR/lib/launcher.js" start \
  > "$DATA_ROOT/logs/self-heal-launch.log" 2>&1 < /dev/null &
disown 2>/dev/null || true
log "启动器已后台运行 (pid=$!), 等待健康..."

# 4. 健康确认 (最多等 20 秒)
log "健康确认中..."
for _ in $(seq 1 20); do
  if port_in_use "$ENGINE_PORT" && port_in_use "$FORWARD_PORT"; then
    break
  fi
  sleep 1
done

# 5. 报告
report_status

if port_in_use "$ENGINE_PORT" && port_in_use "$FORWARD_PORT"; then
  log "✅ 自救完成: Novel AI 已恢复运行"
  [[ "$MODE" == "verify" ]] && { timeout 120 $VERIFY; }  # 超时保护
  exit 0
else
  warn "❌ 自救失败: 服务未就绪, 请查看 $DATA_ROOT/logs/engine.log"
  exit 1
fi
