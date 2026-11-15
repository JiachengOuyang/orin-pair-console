#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Orin Pair Console
=================
Windows <-> Orin NX 的可视化配对控制台。

- 默认角色选择：自动检测系统，选择控制端或被控端
- 控制端：扫描对端、输入配对码、写 ~/.ssh/config、测试连接、解除配对
- 被控端：显示配对码、管理 authorized_keys、配置本机网络

只用 Python 标准库；界面是本地网页，由本进程直接提供。
"""

from __future__ import annotations

import argparse
import atexit
import base64
import binascii
import collections
import hashlib
import ipaddress
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import pwd
except ImportError:  # Windows
    pwd = None

VERSION = "0.10.0"
APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "pairui"
RUNTIME_DIR = APP_DIR / "runtime"
TMP_DIR = RUNTIME_DIR / "tmp"
ORIN_PORT = 8765
WIN_PORT = 8766
CHOOSER_PORT = 8770

# ssh 配置里 HostName / User 只允许这些字符，防止远端把换行或指令塞进配置
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
USER_RE = re.compile(r"^[A-Za-z0-9._-]+$")

START_MARK = "# >>> orin-pair managed >>>"
END_MARK = "# <<< orin-pair managed <<<"

LOG = collections.deque(maxlen=300)
LOG_LOCK = threading.Lock()
VERBOSE = False
LOG_FILE_MAX = 2 * 1024 * 1024  # 单个日志文件上限，超出后轮换成 .1
_LOG_FILE = {"path": None, "fh": None, "size": 0, "chown": None}


class Throttled(Exception):
    """配对码尝试过于频繁，HTTP 层返回 429。"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _open_log_file_locked() -> None:
    """（在 LOG_LOCK 内）打开或重开日志文件；失败时只输出到终端。"""
    path = _LOG_FILE["path"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a", encoding="utf-8", newline="\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if _LOG_FILE["chown"] is not None and hasattr(os, "chown"):
            try:
                os.chown(path, *_LOG_FILE["chown"])
            except OSError:
                pass
    except OSError as exc:
        _LOG_FILE["fh"] = None
        print(f"[提示] 日志文件不可用（{exc}），日志只输出到终端", flush=True)
        return
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    _LOG_FILE.update({"fh": fh, "size": size})


def _rotate_log_locked() -> None:
    """（在 LOG_LOCK 内）把当前日志改名为 <name>.1，再重开一个空文件。"""
    path = _LOG_FILE["path"]
    fh = _LOG_FILE["fh"]
    _LOG_FILE["fh"] = None
    if fh is not None:
        try:
            fh.close()
        except OSError:
            pass
    try:
        if path.exists():
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    _open_log_file_locked()


def open_log_file(path, chown=None):
    """开始把日志追加写入 path；chown 是 root 写回目标账号用的 (uid, gid)。"""
    path = Path(path)
    with LOG_LOCK:
        old = _LOG_FILE["fh"]
        _LOG_FILE.update({"path": path, "fh": None, "size": 0, "chown": chown})
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        _open_log_file_locked()
        if _LOG_FILE["fh"] is not None and _LOG_FILE["size"] >= LOG_FILE_MAX:
            _rotate_log_locked()
    return path


def close_log_file() -> None:
    with LOG_LOCK:
        fh = _LOG_FILE["fh"]
        _LOG_FILE["fh"] = None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


def _write_log_locked(entry) -> None:
    """（在 LOG_LOCK 内）追加一行到日志文件，写满则轮换。"""
    fh = _LOG_FILE["fh"]
    if fh is None:
        return
    line = f"[{entry['full']}] {entry['m']}\n"
    size = len(line.encode("utf-8"))
    try:
        if _LOG_FILE["size"] + size > LOG_FILE_MAX:
            _rotate_log_locked()
            fh = _LOG_FILE["fh"]
            if fh is None:
                return
        fh.write(line)
        fh.flush()
        _LOG_FILE["size"] += size
    except OSError:
        _LOG_FILE["fh"] = None
        try:
            fh.close()
        except OSError:
            pass


def log(message: str) -> None:
    entry = {"t": time.strftime("%H:%M:%S"), "m": message, "full": now_str()}
    with LOG_LOCK:
        LOG.append(entry)
        _write_log_locked(entry)
    print(f"[{entry['t']}] {message}", flush=True)


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def is_windows() -> bool:
    return os.name == "nt"


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def current_user() -> str:
    """当前登录用户；非 root 的被控端默认写自己的 authorized_keys。"""
    try:
        if pwd is not None and hasattr(os, "getuid"):
            return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        pass
    return os.environ.get("USER") or os.environ.get("USERNAME") or "user"


def run(cmd, timeout=20, env=None):
    """执行命令，返回 (returncode, stdout, stderr)。shell=False。"""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=full_env,
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(str(c) for c in cmd)}"
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


def run_with_input(cmd, text, timeout=20):
    """执行命令并把 text 写进 stdin（用于 sudo -S 校验密码）。"""
    try:
        p = subprocess.run(
            [str(c) for c in cmd],
            input=text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {' '.join(str(c) for c in cmd)}"
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def write_text_lf(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    # 先以 600 建临时文件再替换，避免 umask 把状态/配置写成 644/664
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def runtime_path(kind: str) -> Path:
    """程序目录下的 runtime/<kind>；目录不可写时退回 ~/.orin-pair/<kind>。"""
    try:
        base = RUNTIME_DIR / kind
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        fallback = Path.home() / ".orin-pair" / kind
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def cleanup_temp_files() -> None:
    """清掉临时文件；状态、密钥、ssh 配置和备份都保留，供下次继续使用。"""
    targets = []
    try:
        if TMP_DIR.is_dir():
            targets.extend(p for p in TMP_DIR.glob("orin-pair-*") if p.is_file())
        targets.extend(
            p for p in Path(tempfile.gettempdir()).glob("orin-pair-*.conf") if p.is_file()
        )
        if RUNTIME_DIR.is_dir():
            targets.extend(p for p in RUNTIME_DIR.rglob("*.tmp") if p.is_file())
    except OSError:
        pass
    for path in targets:
        try:
            path.unlink()
        except OSError:
            pass


def prepare_runtime() -> None:
    """建立 runtime/tmp，并在启动时清掉上一次遗留的临时文件。"""
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(TMP_DIR, 0o700)
    except OSError:
        pass
    cleanup_temp_files()


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pubkey_fingerprint(pubkey: str) -> str:
    parts = pubkey.strip().split()
    if len(parts) < 2:
        raise ValueError("公钥格式不正确")
    blob = base64.b64decode(parts[1], validate=True)
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def clean_name(name: str) -> str:
    name = re.sub(r"[^\w .:@-]", "_", (name or "").strip())
    name = re.sub(r"\s+", " ", name).strip()
    return (name or "windows")[:64]


def loopback(ip: str) -> bool:
    raw = (ip or "").split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(raw)
        if addr.version == 6 and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        return addr.is_loopback
    except ValueError:
        return raw in ("127.0.0.1", "::1", "localhost")


def host_header_name(value: str) -> str:
    """从 Host 头里取出主机名（去掉端口，处理 IPv6 方括号）。"""
    value = (value or "").strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end != -1 else value
    if value.count(":") == 1:
        name, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit():
            return name
    return value


def is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address((name or "").split("%", 1)[0])
        return True
    except ValueError:
        return False


_HOST_CACHE = {"at": 0.0, "key": "", "names": frozenset()}


def allowed_host_names(bind_host: str) -> frozenset:
    """本机页面允许的 Host 名。

    只信任 IP 字面量、本机主机名和本机地址，避免 DNS rebinding 把外部域名
    解析到 127.0.0.1 后冒充本机页面。DHCP 换地址时最多 30 秒后生效。
    """
    now = time.time()
    if _HOST_CACHE["key"] == bind_host and now - _HOST_CACHE["at"] < 30:
        return _HOST_CACHE["names"]
    names = {"localhost", "127.0.0.1", "::1"}
    host = socket.gethostname().lower()
    if host:
        names.add(host)
        names.add(host + ".local")
    try:
        fqdn = socket.getfqdn().lower()
        if fqdn:
            names.add(fqdn)
    except OSError:
        pass
    if bind_host and bind_host not in ("0.0.0.0", "::"):
        names.add(bind_host.lower())
    names.update(ip.lower() for ip in local_ipv4s())
    result = frozenset(names)
    _HOST_CACHE.update({"at": now, "key": bind_host, "names": result})
    return result


def detect_os():
    """返回当前系统信息，供角色选择页展示和判断可用角色。"""
    if is_windows():
        try:
            build = int(platform.version().split(".")[-1])
        except (ValueError, IndexError):
            build = 0
        label = "Windows 11" if build >= 22000 else f"Windows {platform.release()}"
        return {"id": "windows", "label": label, "detail": platform.platform()}
    if is_linux():
        pretty = ""
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if line.startswith("PRETTY_NAME="):
                    pretty = line.split("=", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass
        return {
            "id": "linux",
            "label": pretty or "Linux",
            "detail": f"{platform.system()} {platform.release()} · {platform.machine()}",
        }
    return {"id": "other", "label": platform.system() or sys.platform, "detail": platform.platform()}


def local_ipv4s():
    """尽力列出本机非回环 IPv4，失败就返回空列表。"""
    found = []
    if is_linux():
        rc, out, _ = run(["hostname", "-I"], timeout=4)
        if rc == 0:
            for item in out.split():
                raw = item.split("/", 1)[0]
                try:
                    addr = ipaddress.ip_address(raw)
                except ValueError:
                    continue
                if addr.version == 4 and not addr.is_loopback:
                    found.append(raw)
    else:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                raw = info[4][0]
                if not raw.startswith("127."):
                    found.append(raw)
        except OSError:
            pass
    seen = set()
    result = []
    for ip in found:
        if ip not in seen:
            seen.add(ip)
            result.append(ip)
    return result


_TS_CACHE = {"at": 0.0, "data": None}


def _tailscale_exe():
    """找 tailscale 命令行；可用 ORIN_PAIR_TAILSCALE 覆盖路径。"""
    exe = os.environ.get("ORIN_PAIR_TAILSCALE") or shutil.which("tailscale")
    if exe:
        return exe
    if is_windows():
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if not base:
                continue
            candidate = Path(base) / "Tailscale" / "tailscale.exe"
            if candidate.exists():
                return str(candidate)
    return None


def _first_ipv4(values) -> str:
    for value in values or []:
        raw = str(value).split("/", 1)[0]
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if addr.version == 4:
            return raw
    return ""


def tailscale_info(force: bool = False):
    """只读读取本机 Tailscale 状态，结果缓存 6 秒。

    只调用 `tailscale status --json`，不改任何配置、不安装、不登录。
    普通用户读不到守护进程状态时，把错误原文带回去，由页面提示用 root
    或设置 operator（sudo tailscale set --operator=$USER）。
    """
    now = time.time()
    cached = _TS_CACHE["data"]
    if cached is not None and not force and now - _TS_CACHE["at"] < 6:
        return {**cached, "peers": list(cached["peers"])}

    info = {
        "available": False,
        "up": False,
        "state": "",
        "ip": "",
        "auth_url": "",
        "hostname": "",
        "dns_name": "",
        "peers": [],
        "health": [],
        "error": "",
    }
    exe = _tailscale_exe()
    if exe:
        info["available"] = True
        rc, out, err = run([exe, "status", "--json"], timeout=5)
        data = None
        if rc == 0 and out:
            try:
                data = json.loads(out)
            except ValueError:
                data = None
        if isinstance(data, dict):
            info["state"] = str(data.get("BackendState") or "")
            info["up"] = info["state"] == "Running"
            info["auth_url"] = str(data.get("AuthURL") or "")
            self_node = data.get("Self") or {}
            info["ip"] = _first_ipv4(self_node.get("TailscaleIPs"))
            info["hostname"] = str(self_node.get("HostName") or "")
            info["dns_name"] = str(self_node.get("DNSName") or "").rstrip(".")
            health = data.get("Health") or []
            if isinstance(health, list):
                info["health"] = [str(item)[:200] for item in health[:5]]
            peers = []
            for peer in (data.get("Peer") or {}).values():
                if not isinstance(peer, dict) or not peer.get("Online"):
                    continue
                ip = _first_ipv4(peer.get("TailscaleIPs"))
                if not ip:
                    continue
                peers.append(
                    {
                        "hostname": str(peer.get("HostName") or ip),
                        "ip": ip,
                        "os": str(peer.get("OS") or ""),
                    }
                )
            peers.sort(key=lambda item: item["hostname"].lower())
            info["peers"] = peers
        else:
            lines = (err or out or "").strip().splitlines()
            info["error"] = lines[-1][:200] if lines else "tailscale status 执行失败"
    _TS_CACHE.update({"at": now, "data": info})
    return {**info, "peers": list(info["peers"])}


_TS_UP = {"proc": None, "at": 0.0}


def tailscale_login(timeout: float = 10.0):
    """拉起 `tailscale up`，尽量把授权链接带回来。

    没有 authkey，登录仍然要在浏览器里完成；这里只负责启动客户端、
    等 status 里出现 AuthURL，再把链接交给页面。
    """
    exe = _tailscale_exe()
    if not exe:
        return {"ok": False, "error": "没找到 tailscale 命令，先安装"}

    info = tailscale_info(force=True)
    if info.get("up"):
        return {"ok": True, "already": True, "state": info.get("state")}

    proc = _TS_UP.get("proc")
    if proc is None or proc.poll() is not None:
        try:
            proc = subprocess.Popen(
                [exe, "up"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:
            return {"ok": False, "error": f"启动 tailscale up 失败：{exc}"}
        _TS_UP.update({"proc": proc, "at": time.time()})
        log("已启动 tailscale up，等待授权链接")

    deadline = time.time() + timeout
    state = info.get("state") or ""
    url = ""
    while time.time() < deadline:
        data = tailscale_info(force=True)
        state = data.get("state") or state
        url = data.get("auth_url") or ""
        if data.get("up") or url or proc.poll() is not None:
            break
        time.sleep(0.4)

    if url:
        return {"ok": True, "url": url, "state": state}
    if state == "Running":
        return {"ok": True, "already": True, "state": state}

    output = ""
    if proc.poll() is not None and proc.stdout is not None:
        try:
            output = (proc.stdout.read() or "").strip()
        except OSError:
            output = ""
    if output:
        lines = [line for line in output.splitlines() if line.strip()]
        return {"ok": False, "error": "\n".join(lines[-3:])[-500:], "state": state}
    return {
        "ok": True,
        "url": "",
        "state": state,
        "note": "已拉起 tailscale up，但还没拿到授权链接；稍等几秒刷新，或确认能访问 login.tailscale.com。",
    }


def tailscale_restart(wait: float = 20.0):
    """修复卡在 NoState / starting 的 Tailscale。

    Windows 先尝试拉起托盘程序（不需要 UAC，常见于睡眠恢复后守护进程
    没有后端）；仍没恢复再弹 UAC 重启服务。Linux 需要 root，重启
    tailscaled。最后等 `tailscale status` 回到可用状态。
    """
    exe = _tailscale_exe()
    if not exe:
        return {"ok": False, "error": "没找到 tailscale 命令，先安装"}

    info = tailscale_info(force=True)
    if info.get("up"):
        return {"ok": True, "already": True, "state": info.get("state")}

    if is_windows():
        gui = Path(exe).with_name("tailscale-ipn.exe")
        if gui.exists():
            try:
                subprocess.Popen([str(gui)])
            except OSError:
                pass
            else:
                log("已启动 Tailscale 托盘程序，等待服务恢复")
                deadline = time.time() + min(wait, 10.0)
                while time.time() < deadline:
                    time.sleep(1.0)
                    data = tailscale_info(force=True)
                    state = data.get("state") or ""
                    if state and state != "NoState":
                        log(f"Tailscale 状态已恢复：{state}")
                        return {
                            "ok": True,
                            "state": state,
                            "message": "已启动 Tailscale 托盘程序，服务已恢复",
                        }
        try:
            import ctypes

            params = ('-NoProfile -NonInteractive -WindowStyle Hidden -Command '
                      '"Restart-Service -Name Tailscale -Force"')
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe", params, None, 0)
        except Exception as exc:
            return {"ok": False, "error": f"启动重启失败：{exc}"}
        if rc <= 32:
            return {"ok": False, "error": "重启没有启动，可能取消了 UAC 授权"}
    else:
        if not is_root():
            return {"ok": False, "error": "重启 tailscaled 需要 root：先在「环境准备」里切换为 root"}
        rc, out, err = run(["systemctl", "restart", "tailscaled"], timeout=30)
        if rc != 0:
            msg = (err or out or "").strip()
            return {"ok": False, "error": (msg[-500:] or "systemctl restart tailscaled 失败")}

    log("已请求重启 Tailscale 服务")
    deadline = time.time() + wait
    state = ""
    while time.time() < deadline:
        time.sleep(1.0)
        data = tailscale_info(force=True)
        state = data.get("state") or ""
        if state and state != "NoState":
            break

    if state and state != "NoState":
        log(f"Tailscale 服务已重启，当前状态：{state}")
        return {"ok": True, "state": state, "message": "Tailscale 服务已重启"}
    return {
        "ok": True,
        "started": True,
        "state": state,
        "note": "已请求重启 Tailscale 服务；如果刚在 UAC 里确认，稍等几秒刷新页面再登录。"
                "仍显示未就绪时，重启系统或修复安装 Tailscale。",
    }


def tailscale_install_linux():
    """用官方脚本安装（调用方保证是 root）。"""
    if _tailscale_exe():
        return {"ok": True, "already": True}
    rc, out, err = run(
        ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
        timeout=900,
        env={"DEBIAN_FRONTEND": "noninteractive"},
    )
    if not _tailscale_exe():
        msg = (err or out or "").strip()
        return {"ok": False, "error": (msg[-800:] or "安装脚本执行失败")}
    run(["systemctl", "enable", "--now", "tailscaled"], timeout=30)
    return {"ok": True, "output": (out or err)[-800:]}


def tailscale_install_windows():
    """用 winget 安装；会弹 UAC，安装过程不在本进程里。"""
    if _tailscale_exe():
        return {"ok": True, "already": True}
    winget = shutil.which("winget")
    if not winget:
        return {"ok": False, "error": "没找到 winget；可从 https://tailscale.com/download 下载安装包"}
    try:
        import ctypes

        params = (
            "install --id Tailscale.Tailscale -e "
            "--accept-package-agreements --accept-source-agreements"
        )
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", winget, params, None, 1)
    except Exception as exc:
        return {"ok": False, "error": f"启动安装失败：{exc}"}
    if rc <= 32:
        return {"ok": False, "error": "安装没有启动，可能取消了 UAC 授权"}
    return {"ok": True, "started": True}


def port_free(bind: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((bind, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def url_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def parse_host_port(value: str, default_port: int = ORIN_PORT):
    value = (value or "").strip()
    if not value:
        raise ValueError("地址为空")
    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        value = parsed.netloc or parsed.path
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            raise ValueError("IPv6 地址格式不正确")
        host = value[1:end]
        rest = value[end + 1 :]
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else default_port
        return host, port
    if value.count(":") == 1:
        host, maybe_port = value.rsplit(":", 1)
        if maybe_port.isdigit():
            return host, int(maybe_port)
    return value, default_port


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(method: str, url: str, payload=None, timeout=6, headers=None):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper())
    # 两端程序之间的调用也要带请求标识（POST 接口统一要求）
    req.add_header("X-Orin-Pair", "1")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, {"ok": False, "error": "响应不是 JSON"}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"ok": False, "error": f"HTTP {exc.code}"}
        return exc.code, body
    except Exception as exc:
        return 0, {"ok": False, "error": str(exc)}


# --------------------------------------------------------------------------
# Orin 侧
# --------------------------------------------------------------------------
class OrinService:
    def __init__(self, args):
        self.args = args
        self.demo = bool(args.demo)
        if not is_linux() and not self.demo:
            raise SystemExit("Orin 角色需要在 Linux/Orin 上运行；在本机测试请加 --demo")

        default_state = runtime_path("orin")
        self.state_dir = Path(args.state_dir) if args.state_dir else default_state
        self.state_path = self.state_dir / "state.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state_dir, 0o700)
        except OSError:
            pass

        if is_linux():
            # root 默认用 sudo 发起用户；普通用户默认自己
            default_user = current_user()
            if is_root():
                default_user = os.environ.get("SUDO_USER") or "dev"
            self.target_user = args.target_user or default_user
        else:
            self.target_user = args.target_user or os.environ.get("USERNAME", "user")
        if args.target_home:
            self.target_home = Path(args.target_home)
        elif self.demo:
            self.target_home = self.state_dir / "demo-home"
        else:
            self.target_home = self._lookup_home(self.target_user)

        self.state = load_json(
            self.state_path,
            {"code": None, "code_created": None, "clients": [], "network": {}},
        )
        self.state.setdefault("clients", [])
        self.state.setdefault("network", {})
        self._lock = threading.Lock()
        self._fail_count = 0
        self._fail_by_ip = {}
        self._code_next_at = 0.0
        self._code_lock = threading.Lock()
        self._net_cache = None
        self._net_cache_at = 0.0
        self._ensure_code()

        self.log_path = self.state_dir / "pair-console.log"
        chown = None
        if is_linux() and is_root() and pwd is not None:
            try:
                ent = pwd.getpwnam(self.target_user)
                chown = (ent.pw_uid, ent.pw_gid)
            except KeyError:
                chown = None
        open_log_file(self.log_path, chown=chown)
        log(f"Orin 控制台启动：{self.state_path}")

    # ---- 基础状态 ----
    def _lookup_home(self, user: str) -> Path:
        if pwd is not None and is_linux():
            try:
                return Path(pwd.getpwnam(user).pw_dir)
            except KeyError:
                return Path("/home") / user
        return Path(os.environ.get("USERPROFILE", str(Path.home())))

    def user_exists(self) -> bool:
        if pwd is None or not is_linux():
            return True
        try:
            pwd.getpwnam(self.target_user)
            return True
        except KeyError:
            return False

    def _save(self) -> None:
        save_json(self.state_path, self.state)
        # root 运行时把 state 还给目标账号，否则之后普通用户再启动会读不到
        if is_root() and is_linux() and pwd is not None:
            try:
                ent = pwd.getpwnam(self.target_user)
                os.chown(self.state_dir, ent.pw_uid, ent.pw_gid)
                os.chown(self.state_path, ent.pw_uid, ent.pw_gid)
            except (KeyError, OSError):
                pass

    def _ensure_code(self, force: bool = False) -> None:
        code = str(self.state.get("code") or "")
        if force or len(code) != 6 or not code.isdigit():
            self.state["code"] = f"{secrets.randbelow(10 ** 6):06d}"
            self.state["code_created"] = now_str()
            self._save()

    def rotate_code(self) -> str:
        with self._code_lock:
            self._fail_count = 0
            self._fail_by_ip.clear()
            self._code_next_at = 0.0
        with self._lock:
            self._ensure_code(force=True)
            return self.state["code"]

    def check_code(self, code) -> bool:
        expected = str(self.state.get("code") or "")
        return bool(expected) and secrets.compare_digest(expected, str(code or ""))

    @staticmethod
    def _attempt_slot(failures: int) -> float:
        return min(0.3 + 0.2 * max(failures, 1), 2.0)

    def verify_code(self, code, source_ip: str = "", rotate_on_fail: bool = True) -> bool:
        """所有入口统一校验配对码：共享节流闸门、失败计数和自动换码。

        配对、查看状态、解除配对都走这里，避免某个接口成为无限速的爆破口。
        尝试过密时抛 Throttled，由 HTTP 层返回 429；连续失败按来源 IP 换码。
        查看状态这类只读轮询不触发换码，否则一个留在旧码上的页面会每分钟把码换掉。
        """
        code = str(code or "")
        now = time.monotonic()
        with self._code_lock:
            wait = self._code_next_at - now
            if wait > 2.0:
                raise Throttled()
            self._code_next_at = max(now, self._code_next_at) + self._attempt_slot(
                self._fail_count + 1
            )
            ok = bool(code) and self.check_code(code)
            rotate = False
            if ok:
                self._fail_count = 0
                self._code_next_at = 0.0
                if source_ip:
                    self._fail_by_ip.pop(source_ip, None)
            else:
                self._fail_count += 1
                if source_ip and rotate_on_fail:
                    self._fail_by_ip[source_ip] = self._fail_by_ip.get(source_ip, 0) + 1
                    if len(self._fail_by_ip) > 256:
                        self._fail_by_ip.clear()
                        self._fail_by_ip[source_ip] = 1
                    rotate = self._fail_by_ip[source_ip] >= 8
        if wait > 0:
            time.sleep(wait)
        if rotate:
            with self._lock:
                self._ensure_code(force=True)
            with self._code_lock:
                self._fail_by_ip.clear()
                self._fail_count = 0
            log("配对码连续验证失败，已自动更换")
        return ok

    # ---- 系统探测 ----
    def addresses(self):
        if self.demo:
            return [{"ifname": "l4tbr0", "cidr": "192.168.55.1/24"}]
        rc, out, _ = run(["ip", "-4", "-o", "addr", "show"])
        result = []
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 4:
                    continue
                match = re.search(r"inet\s+(\S+)", line)
                if not match:
                    continue
                cidr = match.group(1)
                if cidr.startswith("127."):
                    continue
                result.append({"ifname": parts[1].rstrip(":"), "cidr": cidr})
        return result

    def sshd_active(self) -> bool:
        if self.demo:
            return True
        rc, out, _ = run(["systemctl", "is-active", "ssh"], timeout=5)
        return rc == 0 and out.strip() == "active"

    def avahi_active(self) -> bool:
        if self.demo:
            return True
        rc, out, _ = run(["systemctl", "is-active", "avahi-daemon"], timeout=5)
        return rc == 0 and out.strip() == "active"

    def ufw_status(self) -> str:
        if self.demo:
            return "inactive (demo)"
        rc, out, err = run(["ufw", "status"], timeout=5)
        if rc == 127:
            return "not installed"
        first = (out or err).splitlines()[0] if (out or err) else "unknown"
        return first.strip()

    # ---- 环境检查 ----
    @staticmethod
    def _has_cmd(name: str) -> bool:
        if is_windows():
            return False
        if shutil.which(name):
            return True
        return any((Path(p) / name).exists() for p in ("/usr/sbin", "/usr/bin", "/sbin", "/bin"))

    def _in_group(self, group: str) -> bool:
        if self.demo:
            return True
        rc, out, _ = run(["id", "-nG", self.target_user], timeout=5)
        return rc == 0 and group in out.split()

    def _ssh_dir_ok(self) -> bool:
        if self.demo:
            return True
        directory = self.target_home / ".ssh"
        try:
            if not directory.is_dir():
                return False
            if (directory.stat().st_mode & 0o777) != 0o700:
                return False
            key = directory / "authorized_keys"
            if key.exists() and (key.stat().st_mode & 0o777) != 0o600:
                return False
            return True
        except OSError:
            return False

    def env_info(self) -> dict:
        return {
            "sshd": self.sshd_active(),
            "sshd_installed": self.demo or self._has_cmd("sshd"),
            "tmux": self.demo or self._has_cmd("tmux"),
            "avahi": self.avahi_active(),
            "avahi_installed": self.demo or self._has_cmd("avahi-daemon"),
            "ssh_dir_ok": self._ssh_dir_ok(),
            "dialout": self._in_group("dialout"),
        }

    def run_setup(self):
        """页面内的一键准备：装 sshd/tmux/avahi、启用服务、准备 .ssh、加入 dialout。"""
        if self.demo:
            return {
                "ok": True,
                "steps": [{"name": "演示模式", "ok": True, "message": "不改真实系统"}],
            }
        if not is_linux():
            return {"ok": False, "error": "只有 Linux 被控端需要准备环境"}
        if not is_root():
            return {"ok": False, "error": "准备环境需要 root：在页面里切换为 root 后再点"}

        steps = []

        def add(name, ok, message=""):
            steps.append({"name": name, "ok": bool(ok), "message": message})

        need = []
        if not self._has_cmd("sshd"):
            need.append("openssh-server")
        if not self._has_cmd("tmux"):
            need.append("tmux")
        if not self._has_cmd("avahi-daemon") or not self._has_cmd("avahi-resolve"):
            need.extend(["avahi-daemon", "avahi-utils"])
        if need:
            rc, _, err = run(["apt-get", "update"], timeout=300)
            add("apt-get update", rc == 0, err[-300:])
            if rc != 0:
                return {"ok": False, "steps": steps}
            rc, _, err = run(
                ["apt-get", "install", "-y"] + need,
                timeout=900,
                env={"DEBIAN_FRONTEND": "noninteractive"},
            )
            add("安装 " + " ".join(need), rc == 0, err[-300:])
            if rc != 0:
                return {"ok": False, "steps": steps}
        else:
            add("软件包", True, "openssh-server / tmux / avahi 已安装")

        run(["systemctl", "enable", "--now", "ssh"], timeout=30)
        sshd = self.sshd_active()
        add("sshd", sshd, "" if sshd else "没起来，检查 journalctl -u ssh")

        run(["systemctl", "enable", "--now", "avahi-daemon"], timeout=30)
        avahi = self.avahi_active()
        add("avahi", avahi, "" if avahi else "没起来；不影响配对，只影响 orin.local")

        rc, _, _ = run(["getent", "group", "dialout"], timeout=5)
        if rc != 0:
            add("dialout", True, "系统里没有这个组，跳过")
        elif self._in_group("dialout"):
            add("dialout", True, "已在组里")
        else:
            rc, _, err = run(["usermod", "-aG", "dialout", self.target_user], timeout=20)
            add("dialout", rc == 0, err[-300:] or ("已加入，重新登录生效" if rc == 0 else ""))

        self._ensure_auth_file()
        ssh_dir = self._ssh_dir_ok()
        add("ssh 目录", ssh_dir, "" if ssh_dir else "权限没设上，检查家目录归属")

        self._invalidate_net()
        return {"ok": all(step["ok"] for step in steps), "steps": steps}

    def tailscale_install(self):
        """页面按钮：用官方脚本装 Tailscale（需要 root）。"""
        if self.demo:
            return {"ok": True, "output": "演示模式：不执行安装"}
        if not is_linux():
            return {"ok": False, "error": "只有 Linux 被控端需要在这里安装"}
        if not is_root():
            return {"ok": False, "error": "安装 Tailscale 需要 root：先在「环境准备」里切换为 root"}
        log("开始安装 Tailscale")
        result = tailscale_install_linux()
        if result.get("ok"):
            log("Tailscale 安装完成")
        else:
            log("Tailscale 安装失败：" + str(result.get("error", ""))[:200])
        return result

    def tailscale_up(self):
        """页面按钮：拉起 tailscale up，返回授权链接。"""
        if self.demo:
            return {"ok": True, "url": "", "note": "演示模式：不执行登录"}
        return tailscale_login()

    def tailscale_restart(self):
        """页面按钮：重启 tailscaled，处理 NoState / starting 卡住。"""
        if self.demo:
            return {"ok": True, "state": "NeedsLogin", "message": "演示模式：不执行重启"}
        return tailscale_restart()

    def _nmcli_connections(self):
        if self.demo:
            return (
                [
                    {"name": "Wired connection 1", "device": "eth0", "type": "802-3-ethernet"},
                    {"name": "l4tbr0", "device": "l4tbr0", "type": "bridge"},
                ],
                "Wired connection 1",
            )
        rc, out, _ = run(["nmcli", "-t", "-f", "NAME,DEVICE,TYPE", "connection", "show"], timeout=8)
        connections = []
        if rc == 0:
            for line in out.splitlines():
                parts = line.split(":")
                if len(parts) < 3:
                    continue
                connections.append({"name": parts[0], "device": parts[1], "type": parts[2]})
        rc2, out2, _ = run(["nmcli", "-t", "-f", "NAME,DEVICE", "connection", "show", "--active"], timeout=8)
        active = ""
        if rc2 == 0 and out2:
            active = out2.splitlines()[0].split(":")[0]
        return connections, active

    def network_info(self):
        if self._net_cache is not None and time.time() - self._net_cache_at < 3:
            return self._net_cache
        connections, active = self._nmcli_connections()
        hn = socket.gethostname().split(".")[0].lower()
        info = {
            "connections": connections,
            "active_connection": active,
            "addresses": self.addresses(),
            "mdns_name": f"{hn}.local",
            "sshd": self.sshd_active(),
            "avahi": self.avahi_active(),
            "ufw": self.ufw_status(),
        }
        self._net_cache = info
        self._net_cache_at = time.time()
        return info

    def status(self, full: bool = True):
        data = {
            "ok": True,
            "app": "orin-pair",
            "role": "orin",
            "version": VERSION,
            "demo": self.demo,
            "hostname": socket.gethostname(),
            "target_user": self.target_user,
            "target_home": str(self.target_home),
            "user_exists": self.user_exists(),
            "is_root": is_root(),
            "network": {
                "method": self.state["network"].get("method"),
                "applied_at": self.state["network"].get("applied_at"),
                "has_backup": bool(self.state["network"].get("backup")),
            },
            "clients": [
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "fingerprint": c.get("fingerprint"),
                    "paired_at": c.get("paired_at"),
                    "last_seen": c.get("last_seen"),
                }
                for c in self.state.get("clients", [])
            ],
            "logs": list(LOG)[-40:],
            "log_path": str(self.log_path),
            "tailscale": tailscale_info(),
        }
        data.update(self.network_info())
        data.update(self.env_info())
        if full:
            data["code"] = self.state.get("code")
            data["code_created"] = self.state.get("code_created")
        return data

    # ---- authorized_keys ----
    def _auth_file(self) -> Path:
        return self.target_home / ".ssh" / "authorized_keys"

    def _ensure_auth_file(self) -> Path:
        path = self._auth_file()
        directory = path.parent
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        if not path.exists():
            path.touch(mode=0o600)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if is_root() and is_linux() and pwd is not None:
            try:
                ent = pwd.getpwnam(self.target_user)
                os.chown(directory, ent.pw_uid, ent.pw_gid)
                os.chown(path, ent.pw_uid, ent.pw_gid)
            except (KeyError, OSError):
                pass
        return path

    def _read_auth_lines(self):
        path = self._auth_file()
        if not path.exists():
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    @staticmethod
    def _comment_parts(line: str):
        parts = line.split(None, 2)
        if len(parts) < 3:
            return None
        return parts[2]

    def _remove_auth_lines(self, client_id=None, name=None, fingerprint=None) -> int:
        lines = self._read_auth_lines()
        kept = []
        removed = 0
        for line in lines:
            comment = self._comment_parts(line)
            if not comment or not comment.startswith("orin-pair:"):
                kept.append(line)
                continue
            hit = False
            if client_id and comment.startswith(f"orin-pair:{client_id}"):
                hit = True
            elif name:
                tail = comment.split(" ", 1)[1] if " " in comment else ""
                if tail == name:
                    hit = True
            if not hit and fingerprint:
                try:
                    if pubkey_fingerprint(line) == fingerprint:
                        hit = True
                except Exception:
                    pass
            if hit:
                removed += 1
            else:
                kept.append(line)
        if removed:
            write_text_lf(self._auth_file(), "\n".join(kept).rstrip() + "\n")
            # 原子写会换掉 inode，重新修权限和属主
            self._ensure_auth_file()
        return removed

    def _append_auth_line(self, pubkey: str, comment: str) -> None:
        path = self._ensure_auth_file()
        parts = pubkey.strip().split()
        line = f"{parts[0]} {parts[1]} {comment}"
        existing = path.read_text(encoding="utf-8", errors="replace").rstrip()
        text = (existing + "\n" if existing else "") + line + "\n"
        write_text_lf(path, text)
        # 原子写会换掉 inode，重新修权限和属主
        self._ensure_auth_file()

    # ---- 配对 ----
    def pair(self, name: str, pubkey: str, code: str, remote_ip: str = ""):
        name = clean_name(name)
        pubkey = (pubkey or "").strip()
        if not pubkey or len(pubkey) > 8192:
            return {"ok": False, "error": "公钥为空或过长"}
        if not re.match(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|sk-ssh-ed25519@openssh\.com)\s+[A-Za-z0-9+/=]+", pubkey):
            return {"ok": False, "error": "公钥格式不正确"}
        try:
            fingerprint = pubkey_fingerprint(pubkey)
        except Exception as exc:
            return {"ok": False, "error": f"公钥解析失败：{exc}"}
        if not self.demo and not is_root() and self.target_user != current_user():
            return {
                "ok": False,
                "error": "普通用户只能写自己的 authorized_keys；"
                         f"请用 --target-user {current_user()} 或改用 sudo 启动",
            }
        if not self.user_exists():
            return {
                "ok": False,
                "error": f"目标账号 {self.target_user} 不存在，请在页面「环境准备」里安装 / 修复，或运行 app/setup.sh；也可用 --target-user 指定",
            }

        if not self.verify_code(code, remote_ip):
            return {"ok": False, "error": "配对码不正确"}

        with self._lock:
            # 校验和写入之间码可能已被轮换（例如另一台设备刚配对成功）
            if not self.check_code(code):
                return {"ok": False, "error": "配对码不正确"}

            # 同名或同指纹的旧记录一并清掉，避免留下失效钥匙
            stale = [
                c for c in self.state["clients"]
                if c.get("name") == name or c.get("fingerprint") == fingerprint
            ]
            for old in stale:
                self._remove_auth_lines(client_id=old.get("id"))
            if stale:
                self.state["clients"] = [c for c in self.state["clients"] if c not in stale]

            client_id = secrets.token_hex(4)
            token = secrets.token_urlsafe(24)
            self._remove_auth_lines(client_id=client_id, name=name, fingerprint=fingerprint)
            self._append_auth_line(pubkey, f"orin-pair:{client_id} {name}")

            client = {
                "id": client_id,
                "name": name,
                "fingerprint": fingerprint,
                "token_hash": sha256_hex(token),
                "paired_at": now_str(),
                "last_seen": now_str(),
                "remote_ip": remote_ip,
            }
            self.state["clients"].append(client)
            self._ensure_code(force=True)
            self._save()
            log(f"新设备配对：{name}（{fingerprint}）")
            return {
                "ok": True,
                "user": self.target_user,
                "client_id": client_id,
                "token": token,
                "fingerprint": fingerprint,
                "hostname": socket.gethostname(),
            }

    def _find_client(self, client_id: str):
        for client in self.state.get("clients", []):
            if client.get("id") == client_id:
                return client
        return None

    def _token_ok(self, client, token) -> bool:
        if not client or not token:
            return False
        return secrets.compare_digest(str(client.get("token_hash") or ""), sha256_hex(str(token)))

    def unpair(self, client_id: str, token=None, local=False, code_ok=False):
        with self._lock:
            client = self._find_client(client_id)
            if not client:
                return {"ok": False, "error": "找不到这个配对记录"}
            if not local and not code_ok and not self._token_ok(client, token):
                return {"ok": False, "error": "配对令牌无效"}
            removed = self._remove_auth_lines(client_id=client_id)
            self.state["clients"] = [c for c in self.state["clients"] if c.get("id") != client_id]
            self._save()
            log(f"解除配对：{client.get('name')}（移除 {removed} 行公钥）")
            return {"ok": True, "removed_lines": removed}

    def whoami(self, client_id: str, token):
        with self._lock:
            client = self._find_client(client_id)
            if not self._token_ok(client, token):
                return {"ok": True, "valid": False}
            client["last_seen"] = now_str()
            self._save()
            return {"ok": True, "valid": True, "name": client.get("name"), "fingerprint": client.get("fingerprint")}

    # ---- 网络 ----
    def _invalidate_net(self):
        self._net_cache = None

    def _nmcli_get(self, connection: str, key: str) -> str:
        rc, out, _ = run(["nmcli", "-g", key, "connection", "show", connection], timeout=8)
        if rc != 0:
            return ""
        return out.strip().replace("\n", ",")

    def _ensure_avahi(self):
        if self.demo:
            return True, "demo"
        if not shutil.which("avahi-daemon"):
            if not is_root():
                return False, "未安装 avahi，请先在页面「环境准备」里点安装 / 修复（或 sudo bash app/setup.sh）"
            rc, _, err = run(
                ["apt-get", "install", "-y", "avahi-daemon", "avahi-utils"],
                timeout=180,
                env={"DEBIAN_FRONTEND": "noninteractive"},
            )
            if rc != 0:
                return False, f"安装 avahi 失败：{err[-300:]}"
        rc, _, err = run(["systemctl", "enable", "--now", "avahi-daemon"], timeout=20)
        if rc != 0:
            return False, f"启动 avahi 失败：{err[-300:]}"
        return True, "ok"

    def apply_network(self, params):
        method = (params.get("method") or "").strip()
        if method not in ("usb", "mdns", "manual"):
            return {"ok": False, "error": "不支持的连接方式"}

        if self.demo:
            self.state["network"] = {"method": method, "applied_at": now_str(), "backup": None}
            self._save()
            self._invalidate_net()
            return {"ok": True, "message": f"[演示] 已选择 {method}", "address": "192.168.55.1"}

        if method == "usb":
            found = [a for a in self.addresses() if a["cidr"].startswith("192.168.55.")]
            if not found:
                return {
                    "ok": False,
                    "error": "没检测到 192.168.55.1。确认 USB-C 数据线接在刷机口，且载板支持 USB device 模式。",
                }
            self.state["network"] = {"method": "usb", "applied_at": now_str(), "backup": None}
            self._save()
            self._invalidate_net()
            log("连接方式：USB device（192.168.55.1）")
            return {"ok": True, "message": "USB device 已就绪", "address": found[0]["cidr"].split("/")[0]}

        if method == "mdns":
            ok, message = self._ensure_avahi()
            if not ok:
                return {"ok": False, "error": message}
            host = socket.gethostname().split(".")[0].lower()
            rc, out, err = run(["avahi-resolve", "-4", "-n", f"{host}.local"], timeout=8)
            if rc != 0 or not out:
                return {
                    "ok": False,
                    "error": f"mDNS 解析失败：{err or out}。稍等几秒再试；确认防火墙没有拦 5353/udp。",
                }
            address = out.split()[-1]
            self.state["network"] = {"method": "mdns", "applied_at": now_str(), "backup": None}
            self._save()
            self._invalidate_net()
            log(f"连接方式：mDNS（{host}.local -> {address}）")
            return {"ok": True, "message": "mDNS 已就绪", "address": address, "mdns_name": f"{host}.local"}

        # manual
        connection = (params.get("connection") or "").strip()
        address = (params.get("address") or "").strip()
        gateway = (params.get("gateway") or "").strip()
        if not connection:
            return {"ok": False, "error": "请选择要修改的连接"}
        if "/" not in address:
            address += "/24"
        try:
            iface = ipaddress.ip_interface(address)
        except ValueError as exc:
            return {"ok": False, "error": f"地址格式不正确：{exc}"}
        if gateway:
            try:
                ipaddress.ip_address(gateway)
            except ValueError:
                return {"ok": False, "error": "网关格式不正确"}
        if not is_root():
            return {"ok": False, "error": "改网络需要 root，请用 sudo 重新运行"}

        connections, _ = self._nmcli_connections()
        if connection not in [c["name"] for c in connections]:
            return {"ok": False, "error": f"找不到连接 {connection}，先用 nmcli connection show 确认真实名字"}

        backup = {
            "connection": connection,
            "method": self._nmcli_get(connection, "ipv4.method"),
            "addresses": self._nmcli_get(connection, "ipv4.addresses"),
            "gateway": self._nmcli_get(connection, "ipv4.gateway"),
            "dns": self._nmcli_get(connection, "ipv4.dns"),
        }

        rc, out, err = run(
            ["nmcli", "connection", "modify", connection,
             "ipv4.method", "manual",
             "ipv4.addresses", str(iface),
             "ipv4.gateway", gateway],
            timeout=20,
        )
        if rc != 0:
            return {"ok": False, "error": f"nmcli modify 失败：{err or out}"}
        rc, out, err = run(["nmcli", "connection", "up", connection], timeout=40)
        if rc != 0:
            return {"ok": False, "error": f"nmcli up 失败：{err or out}"}

        time.sleep(1.0)
        self._invalidate_net()
        found = [a for a in self.addresses() if a["cidr"] == str(iface)]
        self.state["network"] = {
            "method": "manual",
            "applied_at": now_str(),
            "backup": backup,
        }
        self._save()
        log(f"连接方式：静态 IP（{connection} -> {iface}）")
        return {
            "ok": True,
            "message": f"{connection} 已设为 {iface}" + ("" if found else "，但地址还没出现在网卡上，稍等或检查连接状态"),
            "address": str(iface.ip),
        }

    def revert_network(self):
        if self.demo:
            self.state["network"] = {}
            self._save()
            self._invalidate_net()
            return {"ok": True, "message": "[演示] 已还原"}
        backup = (self.state.get("network") or {}).get("backup")
        if not backup:
            return {"ok": False, "error": "没有可还原的网络改动（USB/mDNS 模式本身不改 IP）"}
        if not is_root():
            return {"ok": False, "error": "改网络需要 root，请用 sudo 重新运行"}
        connection = backup.get("connection")
        if not connection:
            return {"ok": False, "error": "备份里没有连接名，无法还原"}

        if backup.get("method") == "auto":
            cmd = ["nmcli", "connection", "modify", connection,
                   "ipv4.method", "auto", "ipv4.addresses", "", "ipv4.gateway", ""]
        else:
            cmd = ["nmcli", "connection", "modify", connection,
                   "ipv4.method", "manual",
                   "ipv4.addresses", backup.get("addresses") or "",
                   "ipv4.gateway", backup.get("gateway") or ""]
        rc, out, err = run(cmd, timeout=20)
        if rc != 0:
            return {"ok": False, "error": f"还原失败：{err or out}"}
        rc, out, err = run(["nmcli", "connection", "up", connection], timeout=40)
        if rc != 0:
            return {"ok": False, "error": f"重新激活失败：{err or out}"}

        self.state["network"] = {}
        self._save()
        self._invalidate_net()
        log(f"已还原网络配置：{connection}")
        return {"ok": True, "message": f"{connection} 已还原"}

    def allow_firewall(self, cidr=""):
        if self.demo:
            return {"ok": True, "message": "[演示] 已放行"}
        if not is_root():
            return {"ok": False, "error": "改防火墙需要 root"}
        rc, out, err = run(["ufw", "status"], timeout=8)
        if rc == 127:
            return {"ok": True, "message": "没有安装 ufw，不需要放行"}
        status_line = ((out or err).splitlines() or [""])[0].lower()
        if not re.search(r"\bactive\b", status_line):
            return {"ok": True, "message": "ufw 未启用，不需要放行"}
        if not cidr:
            candidates = [a["cidr"] for a in self.addresses() if "/" in a["cidr"]]
            if not candidates:
                return {"ok": False, "error": "本机没有可用地址，无法推断网段"}
            cidr = str(ipaddress.ip_interface(candidates[0]).network)
        try:
            network = str(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            return {"ok": False, "error": "网段格式不正确"}
        rc, out, err = run(
            ["ufw", "allow", "from", network, "to", "any", "port", "22", "proto", "tcp"],
            timeout=20,
        )
        if rc != 0:
            return {"ok": False, "error": f"ufw 放行失败：{err or out}"}
        log(f"ufw 已放行 {network} -> 22/tcp")
        return {"ok": True, "message": f"已放行 {network} -> 22/tcp"}


# --------------------------------------------------------------------------
# Windows 侧
# --------------------------------------------------------------------------
class WindowsService:
    def __init__(self, args):
        self.args = args
        self.demo = bool(args.demo)
        base = None
        if not args.state_dir or not args.ssh_dir:
            base = runtime_path("controller")
        self.state_dir = Path(args.state_dir) if args.state_dir else base
        self.state_path = self.state_dir / "state.json"
        self.ssh_dir = Path(args.ssh_dir) if args.ssh_dir else base / "ssh"
        self.config_path = self.ssh_dir / "config"
        self.key_path = self.ssh_dir / "id_ed25519"
        self.state = load_json(self.state_path, {})
        self._lock = threading.Lock()
        self.ssh, self.keygen = self._find_ssh_tools()
        self.log_path = self.state_dir / "pair-console.log"
        open_log_file(self.log_path)
        log(f"Windows 控制台启动：{self.state_path}")

    def _find_ssh_tools(self):
        override_ssh = os.environ.get("ORIN_PAIR_SSH")
        override_keygen = os.environ.get("ORIN_PAIR_SSH_KEYGEN")
        if override_ssh:
            return override_ssh, override_keygen or str(Path(override_ssh).with_name("ssh-keygen.exe"))
        if is_windows():
            candidates = []
            for base in (
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                os.environ.get("LOCALAPPDATA", ""),
            ):
                if not base:
                    continue
                candidates.append(Path(base) / "Git" / "usr" / "bin" / "ssh.exe")
                candidates.append(Path(base) / "Programs" / "Git" / "usr" / "bin" / "ssh.exe")
            for candidate in candidates:
                if candidate.exists():
                    keygen = candidate.with_name("ssh-keygen.exe")
                    return str(candidate), str(keygen) if keygen.exists() else shutil.which("ssh-keygen")
        return shutil.which("ssh"), shutil.which("ssh-keygen")

    # ---- 状态 ----
    def status(self):
        if not self.ssh:
            # 安装完成后不用重启程序，下一次刷新就能看到
            self.ssh, self.keygen = self._find_ssh_tools()
        paired = bool(self.state.get("client_id"))
        return {
            "ok": True,
            "app": "orin-pair",
            "role": "windows",
            "version": VERSION,
            "demo": self.demo,
            "paired": paired,
            "host": self.state.get("host", ""),
            "port": self.state.get("port", ORIN_PORT),
            "user": self.state.get("user", ""),
            "client_id": self.state.get("client_id", ""),
            "fingerprint": self.state.get("fingerprint", ""),
            "paired_at": self.state.get("paired_at", ""),
            "last_test": self.state.get("last_test"),
            "remote_state": self.state.get("remote_state", "unknown"),
            "key_path": str(self.key_path),
            "key_exists": self.key_path.exists(),
            "ssh_dir": str(self.ssh_dir),
            "ssh_config": str(self.config_path),
            "ssh_config_exists": self.config_path.exists(),
            "managed_block": self._has_managed_block(),
            "ssh_block": self._managed_text(),
            "ssh_found": bool(self.ssh),
            "ssh_path": self.ssh or "",
            "tailscale": tailscale_info(),
            "logs": list(LOG)[-40:],
            "log_path": str(self.log_path),
        }

    # ---- SSH 客户端安装 ----
    def ssh_install(self):
        """页面按钮：用 Windows 可选功能安装 OpenSSH 客户端，会弹 UAC。"""
        if self.demo:
            return {"ok": True, "started": True, "note": "演示模式：不执行安装"}
        if self.ssh:
            return {"ok": True, "already": True, "ssh_path": self.ssh}
        try:
            import ctypes
            params = ('-NoProfile -NonInteractive -Command '
                      '"Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0"')
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe", params, None, 1)
        except Exception as exc:
            return {"ok": False, "error": f"启动安装失败：{exc}"}
        if rc <= 32:
            return {"ok": False, "error": "安装没有启动，可能取消了 UAC 授权"}
        log("已启动 OpenSSH 客户端安装，等待安装完成")
        deadline = time.time() + 25
        while time.time() < deadline:
            self.ssh, self.keygen = self._find_ssh_tools()
            if self.ssh:
                log(f"OpenSSH 客户端已安装：{self.ssh}")
                return {"ok": True, "output": "OpenSSH 客户端已安装", "ssh_path": self.ssh}
            time.sleep(1.0)
        return {
            "ok": True,
            "started": True,
            "note": "安装已在后台进行（可能要从 Windows Update 下载），完成后刷新页面；"
                    "如果一直找不到，可改装 Git for Windows。",
        }

    # ---- Tailscale 安装 / 登录 ----
    def tailscale_install(self):
        """页面按钮：用 winget 安装，会弹 UAC。"""
        if self.demo:
            return {"ok": True, "started": True, "note": "演示模式：不执行安装"}
        return tailscale_install_windows()

    def tailscale_up(self):
        """页面按钮：拉起 tailscale up，返回授权链接。"""
        if self.demo:
            return {"ok": True, "url": "", "note": "演示模式：不执行登录"}
        return tailscale_login()

    def tailscale_restart(self):
        """页面按钮：先拉起托盘程序，必要时再重启 Tailscale 服务。"""
        if self.demo:
            return {"ok": True, "state": "NeedsLogin", "message": "演示模式：不执行重启"}
        return tailscale_restart()

    def _has_managed_block(self) -> bool:
        return bool(self._managed_text())

    def _managed_text(self) -> str:
        if not self.config_path.exists():
            return ""
        text = self.config_path.read_text(encoding="utf-8", errors="replace")
        pattern = re.compile(
            r"(?ms)^" + re.escape(START_MARK) + r".*?^" + re.escape(END_MARK) + r"\s*$"
        )
        match = pattern.search(text)
        return match.group(0) if match else ""

    # ---- 扫描 ----
    def scan(self, host_value: str = ""):
        candidates = []
        tailscale = tailscale_info()
        if host_value.strip():
            host, port = parse_host_port(host_value.strip())
            candidates.append((host, port, "manual"))
        else:
            if self.demo:
                candidates.append(("127.0.0.1", ORIN_PORT, "demo"))
            candidates.append(("192.168.55.1", ORIN_PORT, "usb"))
            candidates.append(("orin.local", ORIN_PORT, "mdns"))
            if tailscale.get("up"):
                # 只探在线对端，最多 8 个，避免一次扫描拖太久
                for peer in tailscale.get("peers", [])[:8]:
                    candidates.append((peer["ip"], ORIN_PORT, "tailscale"))
        seen = set()
        unique = []
        for host, port, source in candidates:
            key = (host.lower(), port)
            if key not in seen:
                seen.add(key)
                unique.append((host, port, source))

        results = []
        tried = []

        def probe(item):
            host, port, source = item
            start = time.time()
            status, body = http_json(
                "GET", f"http://{url_host(host)}:{port}/api/ping", timeout=2.0
            )
            elapsed = int((time.time() - start) * 1000)
            if status == 200 and body.get("app") == "orin-pair" and body.get("role") == "orin":
                return {
                    "host": host,
                    "port": port,
                    "hostname": body.get("hostname", ""),
                    "version": body.get("version", ""),
                    "latency_ms": elapsed,
                    "source": source,
                }, None
            return None, {"host": host, "port": port, "error": body.get("error") or f"HTTP {status}"}

        with ThreadPoolExecutor(max_workers=8) as pool:
            for result, error in pool.map(probe, unique):
                if result:
                    results.append(result)
                elif error:
                    tried.append(error)

        results.sort(key=lambda r: r["latency_ms"])
        log(f"扫描完成：找到 {len(results)} 个 Orin")
        return {
            "ok": True,
            "results": results,
            "tried": tried,
            "tailscale": {
                "available": bool(tailscale.get("available")),
                "up": bool(tailscale.get("up")),
                "peers": len(tailscale.get("peers") or []),
            },
        }

    # ---- 密钥与 config ----
    def _ensure_key(self):
        if not self.keygen:
            raise RuntimeError("没找到 ssh-keygen。请安装 Git for Windows，或设置 ORIN_PAIR_SSH_KEYGEN")
        self.ssh_dir.mkdir(parents=True, exist_ok=True)
        if not is_windows():
            try:
                os.chmod(self.ssh_dir, 0o700)
            except OSError:
                pass
        if self.key_path.exists() and self.key_path.with_suffix(".pub").exists():
            return
        if self.key_path.exists() and not self.key_path.with_suffix(".pub").exists():
            rc, out, err = run([self.keygen, "-y", "-f", str(self.key_path)], timeout=20)
            if rc == 0 and out:
                write_text_lf(self.key_path.with_suffix(".pub"), out + "\n")
                return
            raise RuntimeError(f"已有私钥但读不出公钥：{err or out}")
        rc, out, err = run(
            [self.keygen, "-t", "ed25519", "-C", "agent@windows",
             "-f", str(self.key_path), "-N", ""],
            timeout=30,
        )
        if rc != 0:
            raise RuntimeError(f"ssh-keygen 失败：{err or out}")
        log(f"已生成密钥：{self.key_path}")

    def _block(self, host: str, user: str) -> str:
        # 地址和用户名会原样写进 ssh 配置，必须先挡住换行和控制字符
        if not HOST_RE.match(host or ""):
            raise RuntimeError("地址包含不支持的字符，已拒绝写入 ssh 配置")
        if not USER_RE.match(user or ""):
            raise RuntimeError("SSH 用户名包含不支持的字符，已拒绝写入 ssh 配置")
        identity = str(self.key_path).replace("\\", "/")
        if " " in identity:
            identity = f'"{identity}"'
        known = (self.ssh_dir / "known_hosts").as_posix()
        if " " in known:
            known = f'"{known}"'
        lines = [
            START_MARK,
            "Host orin",
            f"    HostName {host}",
            f"    User {user}",
            f"    IdentityFile {identity}",
            "    IdentitiesOnly yes",
            "    BatchMode yes",
            "    RequestTTY no",
            "    ConnectTimeout 8",
            "    ServerAliveInterval 30",
            "    ServerAliveCountMax 3",
            "    StrictHostKeyChecking accept-new",
            f"    UserKnownHostsFile {known}",
            "    HostKeyAlias orin",
            END_MARK,
        ]
        return "\n".join(lines) + "\n"

    @staticmethod
    def _strip_managed(text: str) -> str:
        pattern = re.compile(
            r"(?ms)^" + re.escape(START_MARK) + r".*?^" + re.escape(END_MARK) + r"\s*\n?"
        )
        return pattern.sub("", text)

    @staticmethod
    def _conflict(text: str) -> bool:
        for line in text.splitlines():
            match = re.match(r"^\s*Host\s+(.+?)\s*(?:#.*)?$", line, re.IGNORECASE)
            if match and "orin" in match.group(1).split():
                return True
        return False

    def _write_config(self, host: str, user: str):
        existing = ""
        if self.config_path.exists():
            existing = self.config_path.read_text(encoding="utf-8", errors="replace")
        stripped = self._strip_managed(existing)
        if self._conflict(stripped):
            raise RuntimeError(
                f"{self.config_path} 里已经有另一个 Host orin；请先改名或删掉它，避免 ssh 用到旧配置"
            )
        if existing and not self.config_path.with_name("config.orin-pair.bak").exists():
            shutil.copy2(self.config_path, self.config_path.with_name("config.orin-pair.bak"))
        new_text = (stripped.rstrip() + "\n\n" if stripped.strip() else "") + self._block(host, user)
        write_text_lf(self.config_path, new_text)
        log(f"已写入 SSH 配置：{self.config_path}")

    def _remove_config_block(self) -> bool:
        if not self.config_path.exists():
            return False
        existing = self.config_path.read_text(encoding="utf-8", errors="replace")
        if START_MARK not in existing:
            return False
        stripped = self._strip_managed(existing).rstrip()
        write_text_lf(self.config_path, stripped + "\n" if stripped else "")
        log(f"已从 SSH 配置移除 orin 段：{self.config_path}")
        return True

    def _test_ssh(self):
        if self.demo:
            return True, "[demo] link-ok"
        if not self.ssh:
            return False, "没找到 ssh 可执行文件"
        if not self.config_path.exists():
            return False, "SSH 配置不存在"
        rc, out, err = run(
            [self.ssh, "-F", str(self.config_path).replace("\\", "/"),
             "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "orin", "echo link-ok"],
            timeout=25,
        )
        text = (out + ("\n" + err if err else "")).strip()
        return rc == 0 and "link-ok" in out, text

    def _test_via_temp(self, host: str, user: str):
        if self.demo:
            return True, "[demo] link-ok"
        if not self.ssh:
            return False, "没找到 ssh 可执行文件"
        temp_path = None
        try:
            try:
                TMP_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            fd, temp_path = tempfile.mkstemp(
                prefix="orin-pair-",
                suffix=".conf",
                dir=str(TMP_DIR) if TMP_DIR.is_dir() else None,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self._block(host, user))
            rc, out, err = run(
                [self.ssh, "-F", temp_path.replace("\\", "/"),
                 "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 "orin", "echo link-ok"],
                timeout=25,
            )
            text = (out + ("\n" + err if err else "")).strip()
            return rc == 0 and "link-ok" in out, text
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    # ---- 配对 / 解除 ----
    def pair(self, host_value: str, code: str, name: str = ""):
        try:
            host, port = parse_host_port(host_value)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not (1 <= port <= 65535):
            return {"ok": False, "error": "端口必须在 1-65535 之间"}
        if not HOST_RE.match(host):
            return {"ok": False, "error": "地址包含不支持的字符"}
        code = str(code or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            return {"ok": False, "error": "配对码是 6 位数字"}
        try:
            self._ensure_key()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        pub_path = self.key_path.with_suffix(".pub")
        if not pub_path.exists():
            return {"ok": False, "error": f"公钥文件不存在：{pub_path}"}
        pubkey = pub_path.read_text(encoding="utf-8", errors="replace").strip()
        name = clean_name(name or socket.gethostname())

        status, body = http_json(
            "POST",
            f"http://{url_host(host)}:{port}/api/pair",
            {"code": code, "name": name, "pubkey": pubkey},
            timeout=12,
        )
        if status != 200 or not body.get("ok"):
            return {"ok": False, "error": body.get("error") or f"配对失败（HTTP {status}）"}

        user = body.get("user") or "dev"
        client_id = body.get("client_id")
        token = body.get("token")
        fingerprint = body.get("fingerprint") or ""

        try:
            self._write_config(host, user)
        except Exception as exc:
            # 配置写不进去就把远端刚装的钥匙撤掉，避免留下孤儿记录
            http_json(
                "POST",
                f"http://{url_host(host)}:{port}/api/unpair",
                {"client_id": client_id, "token": token},
                timeout=5,
            )
            return {"ok": False, "error": str(exc)}

        test_ok, test_output = self._test_via_temp(host, user)
        with self._lock:
            self.state = {
                "host": host,
                "port": port,
                "user": user,
                "client_id": client_id,
                "token": token,
                "fingerprint": fingerprint,
                "paired_at": now_str(),
                "last_test": {"ok": test_ok, "output": test_output, "at": now_str()},
                "remote_state": "unknown",
            }
            save_json(self.state_path, self.state)
        log(f"配对成功：{host}:{port}，用户 {user}，SSH 测试 {'通过' if test_ok else '未通过'}")
        return {
            "ok": True,
            "host": host,
            "port": port,
            "user": user,
            "fingerprint": fingerprint,
            "client_id": client_id,
            "test_ok": test_ok,
            "test_output": test_output,
        }

    def test(self):
        test_ok, output = self._test_ssh()
        with self._lock:
            if self.state:
                self.state["last_test"] = {"ok": test_ok, "output": output, "at": now_str()}
                save_json(self.state_path, self.state)
        log(f"SSH 测试：{'通过' if test_ok else '失败'}")
        return {"ok": True, "test_ok": test_ok, "output": output}

    def remote_check(self):
        if not self.state.get("client_id"):
            return {"ok": True, "state": "unpaired"}
        host = self.state.get("host")
        port = self.state.get("port", ORIN_PORT)
        status, body = http_json(
            "POST",
            f"http://{url_host(host)}:{port}/api/whoami",
            {"client_id": self.state.get("client_id"), "token": self.state.get("token")},
            timeout=3,
        )
        if status == 0:
            state = "unreachable"
        elif body.get("valid"):
            state = "valid"
        else:
            state = "invalid"
        with self._lock:
            if self.state:
                self.state["remote_state"] = state
                save_json(self.state_path, self.state)
        return {"ok": True, "state": state}

    def unpair(self):
        host = self.state.get("host")
        port = self.state.get("port", ORIN_PORT)
        client_id = self.state.get("client_id")
        token = self.state.get("token")
        remote_ok = False
        remote_message = ""
        if host and client_id:
            status, body = http_json(
                "POST",
                f"http://{url_host(host)}:{port}/api/unpair",
                {"client_id": client_id, "token": token},
                timeout=5,
            )
            remote_ok = status == 200 and bool(body.get("ok"))
            remote_message = body.get("error") or ("远端公钥已移除" if remote_ok else "")
        removed = self._remove_config_block()
        with self._lock:
            self.state = {}
            save_json(self.state_path, {})
        if remote_ok:
            log("已解除配对（远端公钥已移除）")
        else:
            log(f"已解除配对（远端未撤销：{remote_message or '无响应'}，本地配置已清理）")
        cleanup_temp_files()
        return {
            "ok": True,
            "remote_ok": remote_ok,
            "removed_config": removed,
            "message": remote_message or ("本地配置已清理" if not remote_ok else "已解除"),
        }

    def cleanup_local(self):
        removed = self._remove_config_block()
        with self._lock:
            self.state = {}
            save_json(self.state_path, {})
        log("远端已解除，本地配置已清理")
        cleanup_temp_files()
        return {"ok": True, "removed_config": removed}

    # ---- 主机指纹 ----
    def _known_host_fingerprint(self) -> str:
        path = self.ssh_dir / "known_hosts"
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        for line in lines:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "orin":
                try:
                    return pubkey_fingerprint(" ".join(parts[1:]))
                except (ValueError, binascii.Error):
                    return ""
        return ""

    def update_host_key(self):
        """清掉本地 known_hosts 里 orin 的旧指纹，再连一次让 ssh 记录当前指纹。"""
        if not self.state.get("client_id"):
            return {"ok": False, "error": "还没有配对，先完成配对再更新指纹"}
        if not self.demo:
            if not self.config_path.exists():
                return {"ok": False, "error": "SSH 配置不存在，请先重新配对"}
            if not self.ssh:
                return {"ok": False, "error": "没找到 ssh 可执行文件"}

        known = self.ssh_dir / "known_hosts"
        removed = 0
        try:
            if known.exists():
                text = known.read_text(encoding="utf-8", errors="replace")
                kept = []
                for line in text.splitlines(keepends=True):
                    if line.split()[:1] == ["orin"]:
                        removed += 1
                    else:
                        kept.append(line)
                if removed:
                    write_text_lf(known, "".join(kept))
        except OSError as exc:
            return {"ok": False, "error": f"读写 known_hosts 失败：{exc}"}

        if self.demo:
            log(f"演示模式：已清除本地 orin 指纹（{removed} 条），未实际连接")
            return {
                "ok": True,
                "removed": removed,
                "fingerprint": "",
                "message": "演示模式：已清除本地指纹记录，未实际连接",
            }

        result = self.test()
        if not result.get("test_ok"):
            output = result.get("output") or "无输出"
            log(f"更新主机指纹失败（旧记录已清除 {removed} 条）：{output[:200]}")
            return {
                "ok": False,
                "removed": removed,
                "output": output,
                "error": "旧指纹已清除，但重新连接失败：" + output,
            }

        fingerprint = self._known_host_fingerprint()
        log(f"已更新 orin 主机指纹：{fingerprint or '未知'}（替换 {removed} 条旧记录）")
        return {
            "ok": True,
            "removed": removed,
            "fingerprint": fingerprint,
            "message": "已更新指纹" + (f"（替换 {removed} 条旧记录）" if removed else ""),
        }


# --------------------------------------------------------------------------
# 角色选择
# --------------------------------------------------------------------------
class ChooserService:
    """统一入口：只负责展示本机系统和可用角色，选完把进程切换成对应角色。"""

    def __init__(self, args):
        self.args = args
        self.demo = bool(args.demo)
        self.detected = detect_os()
        log(f"角色选择页启动：{self.detected['label']}")

    def roles(self):
        linux = is_linux()
        root = is_root()

        # 非 root 也允许进被控端：只能配对，网络设置不可用
        controlled_enabled = linux
        controlled_note = ""
        if not linux:
            controlled_reason = "被控端目前只支持 Ubuntu / Linux"
        elif not self.demo and not root:
            controlled_reason = ""
            controlled_note = "普通用户模式：可以配对；改网络、装环境可在页面里填密码切到 root"
        else:
            controlled_reason = ""

        control_enabled = True
        control_reason = ""
        if linux and root and not getattr(self.args, "ssh_dir", None):
            control_enabled = False
            control_reason = "当前是 root，控制端会写 root 的 ~/.ssh；请用普通用户运行 ./start.sh"

        return [
            {
                "id": "controller",
                "title": "控制端",
                "summary": "扫描被控端、输入配对码、管理 ssh 配置",
                "enabled": control_enabled,
                "reason": control_reason,
                "port": WIN_PORT,
            },
            {
                "id": "controlled",
                "title": "被控端",
                "summary": "显示配对码、管理公钥、配置本机网络",
                "enabled": controlled_enabled,
                "reason": controlled_reason,
                "note": controlled_note,
                "port": ORIN_PORT,
            },
        ]

    def status(self, local=False):
        return {
            "ok": True,
            "app": "orin-pair",
            "role": "auto",
            "version": VERSION,
            "demo": self.demo,
            "local": local,
            "hostname": socket.gethostname(),
            "os": self.detected,
            "is_root": is_root(),
            "addresses": local_ipv4s(),
            "roles": self.roles(),
            "port": self.args.port or CHOOSER_PORT,
        }


# --------------------------------------------------------------------------
# HTTP 服务
# --------------------------------------------------------------------------
class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, role: str, service):
        self.role = role
        self.service = service
        super().__init__(addr, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "OrinPair/" + VERSION
    sys_version = ""

    # ---- 基础响应 ----
    def log_message(self, fmt, *args):
        if VERBOSE:
            super().log_message(fmt, *args)

    def _send_json(self, status: int, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _send_file(self, path: Path, content_type: str):
        try:
            raw = path.read_bytes()
        except OSError:
            self._send_json(404, {"ok": False, "error": "文件不存在"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if path.suffix == ".html":
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'self'",
            )
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 64 * 1024:
            raise ValueError("请求体过大")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype != "application/json":
            raise ValueError("Content-Type 必须是 application/json")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"JSON 解析失败：{exc}")

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urllib.parse.urlparse(origin).netloc == host

    def _host_ok(self) -> bool:
        """Host 必须指向本机，挡住 DNS rebinding 用外部域名冒充本机页面。"""
        host = self.headers.get("Host")
        if not host:
            return True  # HTTP/1.0 或非浏览器客户端
        name = host_header_name(host)
        if not name:
            return False
        if is_ip_literal(name):
            return True
        return name in allowed_host_names(self.server.server_address[0])

    def _local(self) -> bool:
        return loopback(self.client_address[0])

    def _orin_authorized(self, body=None) -> bool:
        if self.server.role != "orin":
            return False
        if self._local():
            return True
        code = self.headers.get("X-Pair-Code")
        if not code and isinstance(body, dict):
            code = body.get("code")
        if not code:
            return False
        return self.server.service.verify_code(
            code, self.client_address[0], rotate_on_fail=False
        )

    # ---- 路由 ----
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            self._send_json(421, {"ok": False, "error": "Host 不是本机地址，已拒绝"})
            return
        try:
            if path == "/":
                if self.server.role == "auto":
                    page = "choose.html"
                elif self.server.role == "orin":
                    page = "orin.html"
                else:
                    page = "windows.html"
                self._send_file(UI_DIR / page, "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if path.startswith("/static/"):
                rel = urllib.parse.unquote(path[len("/static/"):])
                target = (UI_DIR / rel).resolve()
                if not target.is_relative_to(UI_DIR.resolve()) or not target.is_file():
                    self._send_json(404, {"ok": False, "error": "资源不存在"})
                    return
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                    ctype += "; charset=utf-8"
                self._send_file(target, ctype)
                return
            if path == "/api/ping":
                service = self.server.service
                self._send_json(200, {
                    "ok": True,
                    "app": "orin-pair",
                    "role": self.server.role,
                    "version": VERSION,
                    "hostname": socket.gethostname(),
                    "demo": getattr(service, "demo", False),
                })
                return
            if path == "/api/status":
                self._handle_status()
                return
            self._send_json(404, {"ok": False, "error": "接口不存在"})
        except Throttled:
            self._send_json(429, {"ok": False, "error": "尝试过于频繁，请稍后再试"})
        except Exception as exc:
            log(f"GET {path} 出错：{exc}")
            if VERBOSE:
                traceback.print_exc()
            self._send_json(500, {"ok": False, "error": str(exc)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            self._send_json(421, {"ok": False, "error": "Host 不是本机地址，已拒绝"})
            return
        # 自定义头必须由页面脚本显式带上；普通跨站表单带不了
        if self.headers.get("X-Orin-Pair") != "1":
            self._send_json(403, {"ok": False, "error": "缺少请求标识"})
            return
        if not self._origin_ok():
            self._send_json(403, {"ok": False, "error": "来源不合法"})
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        try:
            if self.server.role == "auto":
                self._post_auto(path, body)
            elif self.server.role == "windows":
                self._post_windows(path, body)
            else:
                self._post_orin(path, body)
        except Throttled:
            self._send_json(429, {"ok": False, "error": "尝试过于频繁，请稍后再试"})
        except Exception as exc:
            log(f"POST {path} 出错：{exc}")
            if VERBOSE:
                traceback.print_exc()
            self._send_json(500, {"ok": False, "error": str(exc)})

    # ---- Orin 接口 ----
    def _handle_status(self):
        service = self.server.service
        local = self._local()
        if self.server.role == "auto":
            self._send_json(200, service.status(local=local))
            return
        if self.server.role == "orin":
            authorized = self._orin_authorized()
            if not authorized:
                self._send_json(401, {
                    "ok": False,
                    "role": "orin",
                    "auth_required": True,
                    "error": "需要配对码",
                })
                return
            data = service.status(full=local)
            data["local"] = local
            self._send_json(200, data)
            return
        if self.server.role == "windows" and not local:
            self._send_json(403, {"ok": False, "error": "控制端只接受本机请求"})
            return
        data = service.status()
        data["local"] = local
        self._send_json(200, data)
    def _post_orin(self, path: str, body):
        service: OrinService = self.server.service
        if path == "/api/pair":
            result = service.pair(
                name=body.get("name", ""),
                pubkey=body.get("pubkey", ""),
                code=body.get("code", ""),
                remote_ip=self.client_address[0],
            )
            self._send_json(200 if result.get("ok") else 403, result)
            return
        if path == "/api/whoami":
            result = service.whoami(body.get("client_id", ""), body.get("token", ""))
            self._send_json(200, result)
            return
        if path == "/api/unpair":
            # 三条路：本机页面、配对码（远程页面）、配对令牌（控制端）。
            # 令牌是否正确、是不是自己那台，由 service.unpair 再校验。
            local = self._local()
            token = body.get("token")
            code = self.headers.get("X-Pair-Code") or body.get("code")
            code_ok = False
            if not local and code:
                code_ok = self._orin_authorized(body)
            if not local and not code_ok and not (body.get("client_id") and token):
                self._send_json(403, {"ok": False, "error": "需要配对码、配对令牌或本机访问"})
                return
            result = service.unpair(
                body.get("client_id", ""),
                token=token,
                local=local and not token,
                code_ok=code_ok,
            )
            self._send_json(200 if result.get("ok") else 403, result)
            return
        if path == "/api/setup":
            if not self._local():
                self._send_json(403, {"ok": False, "error": "环境准备只能在 Orin 本机的页面上执行"})
                return
            result = service.run_setup()
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path in ("/api/tailscale/install", "/api/tailscale/up", "/api/tailscale/restart"):
            # 本机直接放行；远程页面需要配对码（和读状态、远程解除同一凭证）
            if not self._local() and not self._orin_authorized(body):
                self._send_json(403, {"ok": False, "error": "需要本机访问或配对码"})
                return
            if path.endswith("/install"):
                result = service.tailscale_install()
            elif path.endswith("/up"):
                result = service.tailscale_up()
            else:
                result = service.tailscale_restart()
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path == "/api/relaunch-root":
            if not self._local():
                self._send_json(403, {"ok": False, "error": "切换 root 只能在 Orin 本机的页面上操作"})
                return
            self._relaunch_root(body)
            return
        if path in (
            "/api/network/apply",
            "/api/network/revert",
            "/api/firewall/allow22",
            "/api/code/refresh",
        ):
            if not self._local():
                self._send_json(403, {"ok": False, "error": "这个操作只能在 Orin 本机的页面上执行"})
                return
        if path == "/api/code/refresh":
            code = service.rotate_code()
            log("已更换配对码")
            self._send_json(200, {"ok": True, "code": code})
            return
        if path == "/api/network/apply":
            result = service.apply_network(body)
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path == "/api/network/revert":
            result = service.revert_network()
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path == "/api/firewall/allow22":
            result = service.allow_firewall(body.get("cidr", ""))
            self._send_json(200 if result.get("ok") else 400, result)
            return
        self._send_json(404, {"ok": False, "error": "接口不存在"})

    # ---- 页面内切换 root ----
    def _root_argv(self):
        """以 root 重启时给新进程的参数（不含 sudo 本身）。"""
        service: OrinService = self.server.service
        args = service.args
        host, port = self.server.server_address[:2]
        python = sys.executable or shutil.which("python3") or "python3"
        argv = [
            python,
            str(APP_DIR / "pair_console.py"),
            "--role", "orin",
            "--bind", str(host),
            "--port", str(int(port)),
            "--no-open",
        ]
        if VERBOSE:
            argv.append("--verbose")
        for attr, flag in (
            ("state_dir", "--state-dir"),
            ("target_user", "--target-user"),
            ("target_home", "--target-home"),
        ):
            value = getattr(args, attr, None)
            if value:
                argv.extend([flag, str(value)])
        return argv

    def _relaunch_root(self, body):
        service: OrinService = self.server.service
        if service.demo:
            self._send_json(400, {"ok": False, "error": "演示模式不需要切换 root"})
            return
        if not is_linux():
            self._send_json(400, {"ok": False, "error": "只有 Linux 被控端需要 root"})
            return
        if is_root():
            self._send_json(400, {"ok": False, "error": "当前已经是 root"})
            return
        sudo = shutil.which("sudo")
        if not sudo:
            self._send_json(400, {"ok": False, "error": "系统里没有 sudo，无法切换；请直接用 root 启动"})
            return

        password = str((body or {}).get("password") or "")
        if password:
            rc, _, err = run_with_input([sudo, "-S", "-v", "-p", ""], password + "\n", timeout=15)
            if rc != 0:
                msg = "密码不正确" if re.search(r"incorrect password|Sorry", err, re.I) else (err or "sudo 校验失败")
                self._send_json(403, {"ok": False, "error": f"sudo 验证失败：{msg}"})
                return
        else:
            rc, _, _ = run([sudo, "-n", "-v"], timeout=8)
            if rc != 0:
                self._send_json(403, {
                    "ok": False,
                    "error": "sudo 需要密码：请在输入框里填登录密码再试（免密 sudo 可以留空）",
                })
                return

        argv = self._root_argv()
        self._send_json(200, {"ok": True, "message": "正在以 root 重启，页面会自动恢复…"})
        threading.Thread(
            target=self._exec_root,
            args=(sudo, argv, password),
            daemon=True,
        ).start()

    @staticmethod
    def _exec_root(sudo, argv, password):
        """把当前进程替换成 root 下的新实例；端口随 exec 释放，避免占用冲突。"""
        time.sleep(0.5)
        try:
            if password:
                read_fd, write_fd = os.pipe()
                os.write(write_fd, (password + "\n").encode("utf-8"))
                os.close(write_fd)
                os.dup2(read_fd, 0)
                os.close(read_fd)
                os.execv(sudo, [sudo, "-S", "-p", ""] + argv)
            else:
                os.execv(sudo, [sudo, "-n"] + argv)
        except Exception as exc:  # pragma: no cover
            log(f"切换 root 失败：{exc}")

    # ---- 角色选择接口 ----
    def _post_auto(self, path: str, body):
        service: ChooserService = self.server.service
        if path != "/api/choose":
            self._send_json(404, {"ok": False, "error": "接口不存在"})
            return
        role = str((body or {}).get("role") or "").strip()
        options = {item["id"]: item for item in service.roles()}
        option = options.get(role)
        if not option:
            self._send_json(400, {"ok": False, "error": "角色不正确"})
            return
        if not option["enabled"]:
            self._send_json(403, {"ok": False, "error": option["reason"] or "这个角色当前不可用"})
            return
        if role == "controller" and not self._local():
            self._send_json(403, {
                "ok": False,
                "error": "控制端只能在本机打开：请在你要操作的那台机器上运行控制台",
            })
            return

        port = option["port"]
        bind = "127.0.0.1" if role == "controller" else ("127.0.0.1" if service.demo else "0.0.0.0")
        if not port_free(bind, port):
            self._send_json(409, {
                "ok": False,
                "error": f"端口 {port} 已被占用，先退出占用它的程序再试",
            })
            return

        argv = self._role_argv(role, bind, port)
        log(f"切换角色：{'控制端' if role == 'controller' else '被控端'}，端口 {port}")
        self._send_json(200, {
            "ok": True,
            "role": role,
            "port": port,
            "url": f"http://127.0.0.1:{port}/",
        })
        threading.Thread(target=self._exec_role, args=(argv,), daemon=True).start()

    def _role_argv(self, role: str, bind: str, port: int):
        service: ChooserService = self.server.service
        args = service.args
        python = sys.executable or shutil.which("python3") or "python3"
        argv = [
            python,
            str(APP_DIR / "pair_console.py"),
            "--role", "windows" if role == "controller" else "orin",
            "--bind", bind,
            "--port", str(port),
            "--no-open",
        ]
        if service.demo:
            argv.append("--demo")
        if VERBOSE:
            argv.append("--verbose")
        for attr, flag in (
            ("state_dir", "--state-dir"),
            ("target_user", "--target-user"),
            ("target_home", "--target-home"),
            ("ssh_dir", "--ssh-dir"),
        ):
            value = getattr(args, attr, None)
            if value:
                argv.extend([flag, str(value)])
        return argv

    @staticmethod
    def _exec_role(argv):
        time.sleep(0.4)
        try:
            os.execv(argv[0], argv)
        except Exception as exc:  # pragma: no cover
            log(f"切换角色失败：{exc}")

    # ---- Windows 接口 ----
    def _post_windows(self, path: str, body):
        if not self._local():
            self._send_json(403, {"ok": False, "error": "Windows 控制台只接受本机请求"})
            return
        service: WindowsService = self.server.service
        if path == "/api/scan":
            result = service.scan(body.get("host", ""))
            self._send_json(200, result)
            return
        if path == "/api/pair":
            result = service.pair(
                host_value=body.get("host", ""),
                code=body.get("code", ""),
                name=body.get("name", ""),
            )
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path == "/api/test":
            result = service.test()
            self._send_json(200, result)
            return
        if path == "/api/unpair":
            result = service.unpair()
            self._send_json(200, result)
            return
        if path == "/api/cleanup":
            result = service.cleanup_local()
            self._send_json(200, result)
            return
        if path == "/api/remote_check":
            result = service.remote_check()
            self._send_json(200, result)
            return
        if path in ("/api/tailscale/install", "/api/tailscale/up", "/api/tailscale/restart"):
            if path.endswith("/install"):
                result = service.tailscale_install()
            elif path.endswith("/up"):
                result = service.tailscale_up()
            else:
                result = service.tailscale_restart()
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path == "/api/ssh/install":
            result = service.ssh_install()
            self._send_json(200 if result.get("ok") else 400, result)
            return
        if path == "/api/hostkey/refresh":
            result = service.update_host_key()
            self._send_json(200 if result.get("ok") else 400, result)
            return
        self._send_json(404, {"ok": False, "error": "接口不存在"})


# --------------------------------------------------------------------------
# 启动
# --------------------------------------------------------------------------
def open_window(url: str, role: str, disabled: bool):
    if disabled:
        return
    try:
        if role in ("windows", "auto") and is_windows():
            edges = [
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            ]
            for exe in edges:
                if exe.exists():
                    subprocess.Popen(
                        [str(exe), f"--app={url}", "--window-size=1180,860", "--no-first-run"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
            chromes = [
                Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
                Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            ]
            for exe in chromes:
                if exe.exists():
                    subprocess.Popen(
                        [str(exe), f"--app={url}", "--window-size=1180,860", "--no-first-run"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    return
            webbrowser.open(url)
            return
        if role in ("orin", "auto") and is_linux():
            if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
                return
            for cmd in (
                ["chromium", f"--app={url}"],
                ["chromium-browser", f"--app={url}"],
                ["google-chrome", f"--app={url}"],
            ):
                if shutil.which(cmd[0]):
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
            webbrowser.open(url)
    except Exception as exc:
        log(f"自动打开窗口失败：{exc}")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Windows <-> Orin NX 可视化配对控制台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 pair_console.py                    # 进入角色选择页（推荐）\n"
            "  sudo python3 pair_console.py --role orin    # 直接作为被控端\n"
            "  python3 pair_console.py --role windows      # 直接作为控制端\n"
            "  本机演示：加 --demo，并指定不同的 --state-dir\n"
        ),
    )
    parser.add_argument(
        "--role",
        default="auto",
        choices=["auto", "orin", "windows"],
        help="auto 进入角色选择页；orin 被控端；windows 控制端",
    )
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--bind", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--target-user", default=None, help="Orin 侧专用账号；root 默认 sudo 发起用户，普通用户默认自己")
    parser.add_argument("--target-home", default=None, help="覆盖目标账号家目录（调试用）")
    parser.add_argument("--ssh-dir", default=None, help="Windows 侧 ~/.ssh 的替代目录（调试用）")
    parser.add_argument("--demo", action="store_true", help="演示/测试模式，不改真实系统")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器窗口")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv=None):
    global VERBOSE
    args = build_parser().parse_args(argv)
    VERBOSE = bool(args.verbose)

    if not UI_DIR.is_dir():
        raise SystemExit(f"缺少界面目录：{UI_DIR}")

    prepare_runtime()
    atexit.register(cleanup_temp_files)
    atexit.register(close_log_file)

    if args.role == "auto":
        service = ChooserService(args)
        port = args.port or CHOOSER_PORT
        bind = args.bind or "0.0.0.0"
    elif args.role == "orin":
        service = OrinService(args)
        port = args.port or ORIN_PORT
        bind = args.bind or ("127.0.0.1" if args.demo else "0.0.0.0")
    else:
        service = WindowsService(args)
        port = args.port or WIN_PORT
        bind = args.bind or "127.0.0.1"

    try:
        server = AppServer((bind, port), args.role, service)
    except OSError as exc:
        raise SystemExit(f"端口 {port} 绑定失败：{exc}")

    title = {"auto": "选择本机角色", "orin": "被控端", "windows": "控制端"}[args.role]
    print("")
    print(f"  Orin Pair Console v{VERSION}  ·  {title}")
    print("  " + "-" * 52)
    print(f"  监听      : {bind}:{port}")
    if args.role == "auto":
        print(f"  本机访问  : http://127.0.0.1:{port}")
        for ip in local_ipv4s():
            print(f"  局域网    : http://{ip}:{port}")
        print(f"  系统      : {service.detected['label']}")
        print("  说明      : 控制端仅限本机；被控端普通用户可配对，改网络需 sudo")
        if args.demo:
            print("  模式      : 演示（不改真实网络）")
    elif args.role == "orin":
        print(f"  本机访问  : http://127.0.0.1:{port}")
        for addr in service.addresses():
            ip = addr["cidr"].split("/")[0]
            if not ip.startswith("127."):
                print(f"  局域网    : http://{ip}:{port}")
        print(f"  配对码    : {service.state.get('code')}")
        print(f"  目标账号  : {service.target_user} ({service.target_home})")
        print(f"  日志文件  : {service.log_path}")
        if not is_root() and not args.demo:
            print(f"  模式      : 普通用户（公钥写入 {service.target_user}；改网络可在页面里切换 root）")
        if args.demo:
            print("  模式      : 演示（不改真实网络）")
    else:
        print(f"  访问      : http://127.0.0.1:{port}")
        print(f"  SSH 工具  : {service.ssh or '未找到'}")
        print(f"  私钥      : {service.key_path}")
        print(f"  SSH 配置  : {service.config_path}")
        print(f"  日志文件  : {service.log_path}")
        if args.demo:
            print("  模式      : 演示（跳过真实 ssh 测试）")
    print("  Ctrl+C 退出")
    print("")

    url = f"http://127.0.0.1:{port}/"
    open_window(url, args.role, args.no_open)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        server.server_close()
        cleanup_temp_files()


if __name__ == "__main__":
    main()
