#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# runtime 同步脚本 —— 从源项目同步 Python 引擎到插件 runtime/
#
# 用法:
#   bash scripts/sync-runtime.sh [源项目路径]
#   默认源: /home/host/deepseek_harmness/new_writing
#
# 同步内容: agents/ brains/ engine/ models/ sync/ requirements.txt conftest.py
# 自动处理:
#   1. 排除 __pycache__ / *.pyc
#   2. 自动应用 __main__ 入口块修复 (uvicorn.run 必须在文件末尾,
#      否则其后路由永不注册 → 404 bug)
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
SRC="${1:-/home/host/deepseek_harmness/new_writing}"
DST="$PLUGIN_DIR/runtime"

[[ -d "$SRC/engine" ]] || { echo "✗ 源项目无效: $SRC" >&2; exit 1; }

echo "同步: $SRC → $DST"

# 1. 同步核心目录与文件
for dir in agents brains engine models sync; do
  rsync -a --delete \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='*.pyo' \
    --exclude='.pytest_cache' \
    "$SRC/$dir/" "$DST/$dir/"
  echo "  ✓ $dir/"
done
cp "$SRC/requirements.txt" "$SRC/conftest.py" "$DST/"

# 2. 自动应用 __main__ 修复 (幂等: 已在末尾则跳过)
/home/host/miniconda3/bin/python3 - "$DST/engine/main.py" << 'EOF'
import sys

path = sys.argv[1]
with open(path, encoding='utf-8') as f:
    content = f.read()

block = '''if __name__ == "__main__":
    _init_global_brains()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")'''

# 已在末尾 → 无需处理
if content.rstrip().endswith('log_level="info")'):
    print('  ✓ __main__ 块已在文件末尾, 无需修复')
else:
    if block in content:
        content = content.replace(block + '\n', '')
        content = content.rstrip() + '\n\n\n' + block + '\n'
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print('  ✓ __main__ 块已移动至文件末尾 (修复 404 路由 bug)')
    else:
        print('  ⚠ 未找到标准 __main__ 块, 请手动检查 main.py')
EOF

echo "完成。同步后建议运行: node --test tests/unit/ && node lib/verify.js"
