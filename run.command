#!/bin/sh
set -eu
TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$TASK_ROOT"
if [ ! -x "$TASK_ROOT/.venv/bin/python" ]; then
  printf '%s\n' '请先使用 Python 3.14 创建 .venv 并安装 requirements.txt。' >&2
  exit 1
fi
exec "$TASK_ROOT/.venv/bin/python" -X utf8 "$TASK_ROOT/launcher.py" "$@"
