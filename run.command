#!/bin/sh
set -eu
TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$TASK_ROOT"
if [ ! -x "$TASK_ROOT/.venv/bin/python" ]; then
  printf '%s\n' '请先在项目目录运行 uv sync，或直接运行 uv run launcher.py。' >&2
  exit 1
fi
exec "$TASK_ROOT/.venv/bin/python" -X utf8 "$TASK_ROOT/launcher.py" "$@"
