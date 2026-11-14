#!/usr/bin/env bash
# 统一入口：自动检测系统，打开角色选择页
#   ./start.sh              控制端 / 被控端二选一
#   sudo ./start.sh         直接以 root 启动（平时不必；页面里也能切 root）
#   ./start.sh --demo       演示模式，不改真实系统
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "缺少 python3，请先执行：sudo apt install -y python3" >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "提示：当前是普通用户，控制端、被控端都能正常配对；要改网络，在被控端页面里输入密码切到 root 即可"
fi

exec python3 "$PWD/app/pair_console.py" "$@"
