# Orin Pair Console

![version](https://img.shields.io/badge/version-0.9.0-blue)
![python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Ubuntu-lightgrey)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

Windows 与 Ubuntu / Orin NX 之间的可视化 SSH 配对工具。控制端扫描设备、输入 6 位配对码，程序自动生成密钥并写好 ssh 配置；被控端显示配对码、管理公钥和网络。支持 USB 直连、局域网和 Tailscale，纯 Python 标准库，零第三方依赖。

## 能做什么

- **控制端**（Windows，或 Ubuntu 桌面）：扫描 Orin、输码配对、测试连接、更新主机指纹、复制 ssh 命令、解除配对。
- **被控端**（Ubuntu / Orin）：显示配对码、环境准备、检测 USB / mDNS、设置静态 IP、放行防火墙、安装和登录 Tailscale。
- **两端都能解除配对**：被控端删公钥，控制端删 `Host orin` 段；控制端私钥保留。
- **运行数据隔离**：状态、密钥、ssh 配置、`known_hosts`、日志都在 `app/runtime/`，不写真实的 `~/.ssh`。
- **安全设计**：Host 白名单、POST 请求标识、配对码统一节流、令牌只存哈希、日志不记配对码/令牌/密码。

## 快速开始

### 被控端（Ubuntu / Orin）

```bash
./start.sh
```

打开页面后选“被控端”。普通用户就能完成配对；要改网络或装环境时，在页面「环境准备」里切换为 root。

### 控制端（Windows）

```bat
start.bat
```

打开页面后选“控制端”，扫描或手填 Orin 地址，输入被控端页面上的 6 位配对码，点“完成配对”。

端口分配：角色选择 `8770`，控制端 `8766`（只监听 `127.0.0.1`），被控端 `8765`。

## 目录结构

| 路径 | 说明 |
|-|-|
| `start.sh` / `start.bat` | Linux / Windows 启动入口 |
| `app/pair_console.py` | 主程序：HTTP 服务、配对协议、ssh 配置、网络、Tailscale |
| `app/pairui/` | 网页界面：角色选择、控制端、被控端 |
| `app/setup.sh` | 命令行环境准备（sshd、tmux、avahi、`.ssh` 权限、dialout） |
| `app/README.md` | 完整功能、配置、安全边界、验证记录和变更日志 |
| `app/agent远程连接-优化版.md` | 手工搭建 agent ssh 通道，附录 A 为实现技术说明 |

## 运行数据

`app/runtime/` 保存状态、密钥、ssh 配置、`known_hosts` 和日志，已在 `.gitignore` 中排除，不会提交到仓库。演示和测试可以用 `--state-dir` / `--ssh-dir` 指到临时目录，避免污染真实配置。

## 文档

- [完整使用说明](app/README.md)
- [agent 远程连接与实现细节](app/agent远程连接-优化版.md)

## 版本

当前版本 **0.9.0**，对应标签 `v0.9.0`。
