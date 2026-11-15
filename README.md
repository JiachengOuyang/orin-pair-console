# Orin Pair Console

![version](https://img.shields.io/badge/version-0.10.0-blue)
![python](https://img.shields.io/badge/python-3.x-3776AB?logo=python&logoColor=white)
![platform](https://img.shields.io/badge/platform-Windows%20%7C%20Ubuntu-lightgrey)
![dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)

Windows 与 Ubuntu / Orin NX 之间的可视化 SSH 配对工具。控制端扫描设备、输入 6 位配对码，程序自动生成密钥并写好 ssh 配置；被控端显示配对码、管理公钥和网络。支持 USB 直连、局域网和 Tailscale，纯 Python 标准库，零第三方依赖。

## 功能概览

- **控制端**（Windows，或 Ubuntu 桌面）：扫描 Orin、输码配对、测试连接、更新主机指纹、复制 ssh 命令、解除配对。
- **被控端**（Ubuntu / Orin）：显示配对码、环境准备、检测 USB / mDNS、设置静态 IP、放行防火墙、安装 / 登录 / 重启 Tailscale。
- **两端都能解除配对**：被控端删公钥，控制端删 `Host orin` 段；控制端私钥保留。
- **运行数据隔离**：状态、密钥、ssh 配置、`known_hosts`、日志都在 `app/runtime/`，不写真实的 `~/.ssh`。
- **安全设计**：Host 白名单、POST 请求标识、配对码统一节流、令牌只存哈希、日志不记配对码/令牌/密码。

## 最短启动

### 被控端（Ubuntu / Orin）

```bash
./start.sh
```

页面里选“被控端”。普通用户即可完成配对；改网络或装环境时，在页面「环境准备」里切换为 root。

### 控制端（Windows）

```bat
start.bat
```

页面里选“控制端”，扫描或手填 Orin 地址，输入被控端显示的 6 位配对码。端口：选择页 `8770`，控制端 `8766`（仅本机），被控端 `8765`。

## 文档分工

| 文档 | 适合谁 | 写的是什么 |
|-|-|-|
| 本页 | 第一次打开仓库的人 | 项目定位、功能概览、最短启动、文档导航 |
| [app/README.md](app/README.md) | 实际部署、配置和排错 | 完整操作、配对流程、文件与权限、Tailscale、演示模式、安全边界、验证记录、常见问题、变更日志 |
| [app/agent远程连接-优化版.md](app/agent远程连接-优化版.md) | 想手工搭 ssh 通道或改代码 | 手工 ssh/agent 配置、CLAUDE.md 与命令白名单、ROS 2，附录 A 是程序实现技术说明 |

## 仓库导航

| 路径 | 说明 |
|-|-|
| `start.sh` / `start.bat` | Linux / Windows 启动入口 |
| `app/pair_console.py` | 主程序：HTTP 服务、配对协议、ssh 配置、网络、Tailscale |
| `app/pairui/` | 网页界面：角色选择、控制端、被控端 |
| `app/setup.sh` | 命令行环境准备 |
| `app/runtime/` | 运行时数据，已被 `.gitignore` 排除 |

## 版本

当前版本 **0.10.0**（新标签尚未创建，上一个标签是 `v0.9.0`）。
