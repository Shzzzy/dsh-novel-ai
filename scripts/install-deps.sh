#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# dsh-novel-ai 依赖安装脚本
# 用法:
#   bash scripts/install-deps.sh          自动探测 Python 并安装引擎依赖
#   NOVEL_AI_PYTHON=/path/to/python bash scripts/install-deps.sh  指定解释器
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_DIR="$(dirname "$SCRIPT_DIR")/runtime"

# 探测 Python 解释器 (与 launcher.js 的探测顺序一致)
detect_python() {
  if [[ -n "${NOVEL_AI_PYTHON:-}" ]]; then
    echo "$NOVEL_AI_PYTHON"
    return
  fi
  for cand in /home/host/miniconda3/bin/python3 python3 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      echo "$cand"
      return
    fi
  done
  echo ""
}

PY="$(detect_python)"
if [[ -z "$PY" ]]; then
  echo "✗ 未找到 Python 解释器，请设置 NOVEL_AI_PYTHON 后重试" >&2
  exit 1
fi

echo "使用 Python: $PY"
echo "安装目录:   $RUNTIME_DIR"
echo "── 安装引擎依赖 ──"
"$PY" -m pip install -r "$RUNTIME_DIR/requirements.txt"

echo "── 校验关键模块 ──"
"$PY" - <<'EOF'
mods = ['fastapi', 'uvicorn', 'pydantic', 'websockets', 'httpx', 'yaml', 'jinja2']
missing = []
for m in mods:
    try:
        __import__(m)
    except ImportError:
        missing.append(m)
if missing:
    print(f"✗ 缺失: {', '.join(missing)}")
    raise SystemExit(1)
print("✓ 引擎依赖全部就绪")
EOF

echo "完成。现在可以启动: node lib/launcher.js start"
