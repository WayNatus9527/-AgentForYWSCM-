#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  echo '请先按 README 创建虚拟环境并安装 requirements-dev.txt。'
  exit 1
fi
simulation_root="${SIMULATOR_ROOT:-$PWD/.local/simulator}"
exec .venv/bin/python -m simulator --root "$simulation_root" "$@"
