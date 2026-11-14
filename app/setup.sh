#!/usr/bin/env bash
# ============================================================
# Orin NX 侧一键配置（Ubuntu 22.04 / JetPack 5.x-6.x）
#
# 做的事：
#   1. 装并启动 sshd、tmux；可选装 avahi（用于 orin.local）
#   2. 复用现有账号（默认 sudo 的发起用户），补 dialout，可选取 sudo
#   3. 预建 ~/.ssh，权限 700/600
#   4. ufw 已启用时，按需放行 22 端口
#   5. --harden：收口 sshd（关密码登录、AllowUsers）
#   6. --audit ：给远端命令记 syslog
#
# 不包含：改 IP。静态直连 / USB device / ICS / Tailscale
# 都有把自己锁在门外的风险，按文档第 2 章单独做。
#
# 用法：
#   sudo bash setup.sh
#   sudo bash setup.sh --sudo --lan 192.168.1.0/24
#   sudo bash setup.sh --harden     # 公钥装好且验证免密之后，再单独跑
#   sudo bash setup.sh --audit      # 可选：命令审计
# ============================================================

set -euo pipefail

TARGET_USER="${SUDO_USER:-}"
WITH_SUDO=0
WITH_AVAHI=1
WITH_HARDEN=0
WITH_AUDIT=0
LAN_CIDR=""

usage() {
  cat <<'EOF'
用法: sudo bash setup.sh [选项]

  --user NAME     要配置的已有账号，默认 sudo 的发起用户
  --sudo          把该账号加入 sudo 组（默认不加）
  --no-avahi      不安装 avahi（默认安装，用于 orin.local）
  --lan CIDR      放行该网段访问 22 端口，例如 192.168.1.0/24
  --harden        收口 sshd：关密码登录，只允许目标账号（公钥验证通过后再用）
  --audit         把目标账号的登录 shell 换成记日志的包装脚本
  -h, --help      显示本帮助

注意：本脚本不修改任何网络/IP 配置。
EOF
}

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '[警告] %s\n' "$*" >&2; }
die()  { printf '[错误] %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)    TARGET_USER="${2:?--user 需要账号名}"; shift 2 ;;
    --sudo)    WITH_SUDO=1; shift ;;
    --no-avahi) WITH_AVAHI=0; shift ;;
    --lan)     LAN_CIDR="${2:?--lan 需要网段，例如 192.168.1.0/24}"; shift 2 ;;
    --harden)  WITH_HARDEN=1; shift ;;
    --audit)   WITH_AUDIT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *)         die "未知参数: $1（用 --help 查看）" ;;
  esac
done

[[ $EUID -eq 0 ]] || die "请用 sudo 运行：sudo bash $0"
command -v apt-get >/dev/null 2>&1 || die "这不是 Debian/Ubuntu 系系统"

if [[ -z "$TARGET_USER" ]]; then
  die "请用 sudo 从普通用户运行，或用 --user 指定一个已存在的账号"
fi
if [[ "$TARGET_USER" == "root" ]]; then
  die "不把配置写进 root 的家目录；请用普通用户运行 sudo，或用 --user 指定其他账号"
fi
id -u "$TARGET_USER" >/dev/null 2>&1 || die "账号 $TARGET_USER 不存在；本脚本不再自动建号，请先创建或换 --user"

# shellcheck disable=SC1091
. /etc/os-release
log "系统 ${PRETTY_NAME:-unknown}，架构 $(uname -m)"

# ---------- 1. 软件包 ----------
export DEBIAN_FRONTEND=noninteractive

log "更新软件源"
apt-get update || die "apt update 失败。Orin 可能没联网：先用 USB-C/网线把网络接上，或跳过本脚本手工装包。"

log "安装 openssh-server、tmux"
apt-get install -y openssh-server tmux

if [[ $WITH_AVAHI -eq 1 ]]; then
  log "安装 avahi-daemon、avahi-utils（提供 orin.local 与 avahi-resolve）"
  apt-get install -y avahi-daemon avahi-utils
  systemctl enable --now avahi-daemon
fi

log "启动并启用 ssh"
systemctl enable --now ssh
systemctl is-active --quiet ssh || die "sshd 没起来，检查：journalctl -u ssh --no-pager -n 50"

# ---------- 2. 账号与 ssh 目录 ----------
log "使用现有账号 $TARGET_USER（不新建账号）"

if getent group dialout >/dev/null 2>&1; then
  usermod -aG dialout "$TARGET_USER"
  log "已把 $TARGET_USER 加入 dialout（串口/CAN/GPIO 需要）"
else
  warn "系统里没有 dialout 组，跳过串口权限"
fi

if [[ $WITH_SUDO -eq 1 ]]; then
  usermod -aG sudo "$TARGET_USER"
  log "已把 $TARGET_USER 加入 sudo 组"
else
  log "未加 sudo 组（需要时再执行 usermod -aG sudo $TARGET_USER）"
fi

TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || die "找不到 $TARGET_USER 的家目录"
TARGET_GROUP="$(id -gn "$TARGET_USER")"

install -d -m 700 -o "$TARGET_USER" -g "$TARGET_GROUP" "$TARGET_HOME/.ssh"
if [[ ! -f "$TARGET_HOME/.ssh/authorized_keys" ]]; then
  install -m 600 -o "$TARGET_USER" -g "$TARGET_GROUP" /dev/null "$TARGET_HOME/.ssh/authorized_keys"
fi
chown -R "$TARGET_USER":"$TARGET_GROUP" "$TARGET_HOME/.ssh"
chmod 700 "$TARGET_HOME/.ssh"
chmod 600 "$TARGET_HOME/.ssh/authorized_keys"
log "已准备 $TARGET_HOME/.ssh（700/600）"

# ---------- 3. 防火墙 ----------
if command -v ufw >/dev/null 2>&1; then
  if ufw status 2>/dev/null | head -n1 | grep -q 'active'; then
    if [[ -n "$LAN_CIDR" ]]; then
      if ufw allow from "$LAN_CIDR" to any port 22 proto tcp; then
        log "ufw 已放行 $LAN_CIDR -> 22/tcp"
      else
        warn "ufw 放行失败，请手工检查：sudo ufw status verbose"
      fi
    else
      warn "ufw 已启用，但没给 --lan；22 端口可能被拦。手工放行：sudo ufw allow from <你的网段>/24 to any port 22 proto tcp"
    fi
  else
    log "ufw 未启用，跳过"
  fi
else
  log "没装 ufw，跳过"
fi

# ---------- 4. 可选：收口 sshd ----------
if [[ $WITH_HARDEN -eq 1 ]]; then
  log "收口 sshd"
  [[ -s "$TARGET_HOME/.ssh/authorized_keys" ]] || die "--harden 要求 $TARGET_USER 的 authorized_keys 里已有公钥；否则会把自己锁在门外"

  if [[ ! -t 0 ]]; then
    warn "非交互模式执行 --harden：请确认你保留了一条已连上的 ssh 会话或串口 console"
  fi

  CONF="/etc/ssh/sshd_config.d/10-agent.conf"
  HAD_CONF=0
  if [[ -f "$CONF" ]]; then
    cp -a "$CONF" "$CONF.qoder.bak"
    HAD_CONF=1
  fi

  cat > "$CONF" <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
X11Forwarding no
AllowUsers $TARGET_USER
EOF
  chmod 644 "$CONF"

  if sshd -t; then
    systemctl reload ssh
    log "sshd 配置已重载"
  else
    if [[ $HAD_CONF -eq 1 ]]; then
      mv -f "$CONF.qoder.bak" "$CONF"
    else
      rm -f "$CONF"
    fi
    die "sshd -t 校验失败，已恢复原配置，未重载。检查 /etc/ssh/sshd_config.d/"
  fi
  rm -f "$CONF.qoder.bak"

  EFF_PW="$(sshd -T 2>/dev/null | awk '$1=="passwordauthentication"{print $2}' || true)"
  if [[ "$EFF_PW" != "no" ]]; then
    warn "实际生效的 PasswordAuthentication 是 ${EFF_PW:-未知}。检查 /etc/ssh/sshd_config.d/ 里更靠前的文件，或 cloud-init 是否覆盖了 sshd 配置"
  fi
  EFF_USERS="$(sshd -T 2>/dev/null | awk '$1=="allowusers"{$1=""; print}' || true)"
  log "sshd 生效值：PasswordAuthentication=$EFF_PW，AllowUsers=${EFF_USERS:-未限制}"
fi

# ---------- 5. 可选：命令审计 ----------
if [[ $WITH_AUDIT -eq 1 ]]; then
  log "安装命令审计包装 shell"
  cat > /usr/local/bin/agent-shell <<'EOF'
#!/bin/bash
logger -t agent-remote -- "${SSH_ORIGINAL_COMMAND:-interactive}"
exec /bin/bash "$@"
EOF
  chmod 755 /usr/local/bin/agent-shell
  usermod -s /usr/local/bin/agent-shell "$TARGET_USER"
  log "已把 $TARGET_USER 的登录 shell 换成 /usr/local/bin/agent-shell"
  warn "需要重新登录才生效；改完请测一次 scp/rsync。撤回：usermod -s /bin/bash $TARGET_USER"
fi

# ---------- 6. 汇总 ----------
SSH_STATE="$(systemctl is-active ssh 2>/dev/null || true)"
ADDRS="$(hostname -I 2>/dev/null | tr -s ' ' '\n' | sed '/^$/d' | sed 's/^/  /' || true)"

cat <<EOF

------------------------------------------------------------
配置完成
  账号        : $TARGET_USER
  家目录      : $TARGET_HOME
  ssh 服务    : ${SSH_STATE:-unknown}
  本机地址    :
$ADDRS
EOF

if [[ $WITH_AVAHI -eq 1 ]]; then
  MDNS="$(avahi-resolve -n "$(hostname).local" 2>/dev/null | awk '{print $2}' || true)"
  printf '  mDNS        : %s\n' "${MDNS:-解析失败，稍后重试 avahi-resolve -n $(hostname).local}"
fi

cat <<EOF
------------------------------------------------------------
下一步：

  用 Orin Pair 配对时不需要手动装公钥：直接跑被控端，目标账号默认就是 $TARGET_USER。

  或者手动配免密（在 Windows 的 Git Bash 里）：
  1) 生成无口令密钥（已有可跳过）
     ssh-keygen -t ed25519 -C 'agent@windows' -f ~/.ssh/id_ed25519 -N ''

  2) 装公钥（会提示输一次密码；地址换成上面实际的那个）
     ssh $TARGET_USER@<地址> 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' < ~/.ssh/id_ed25519.pub

  3) 验证免密
     ssh $TARGET_USER@<地址> 'echo link-ok'

  4) 免密验证通过后，如果要收口 sshd，在 Orin 上再跑一次：
     sudo bash $0 --harden
EOF
