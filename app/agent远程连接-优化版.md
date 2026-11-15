# agent 远程连接（优化版）

> 适用：Windows 10/11 + Git Bash；Orin NX，JetPack 5.x / 6.x，Ubuntu 22.04；Claude Code 已装好，模型 API 已配通。

> 本文第 0–8 章讲“手工把 agent 通道配通”。文末附录 A 单独讲 Orin Pair Console 这个可视化配对工具用到的技术、协议和安全设计，改代码或排查工具本身的问题时可以直接跳到附录。

# 0. 先说清楚这套方案在干什么

你现在的情况是：Windows 上的 Claude Code 已装好、模型 API 已配通，Orin NX 上的 Ubuntu 22.04 也已装好。剩下的事情只有一件——**把两台机器之间连成一条 agent 能用的通道**。

Claude Code 本身没有“远程执行”这个功能。它能在 Orin 上跑东西，是因为它调用的始终是本地 shell，而本地 shell 里有一条 ssh 命令。所以整篇文章的目标可以归结为一句话：把 ssh 配成一条**不用输密码、不会弹交互、不会卡住**的通道，然后让 agent 只通过这条通道干活。

| 位置 | 跑什么 | 说明 |
|-|-|-|
| 云端 | 模型 API | 只负责推理和生成命令，不接触任何一台机器 |
| Windows | Claude Code 进程、Git Bash | 决定下一步做什么，并通过 ssh 把命令发出去 |
| Orin NX（Ubuntu 22.04） | sshd | 真正执行命令、跑 ROS 2、操作硬件的地方 |

> 这里配不好，表现出来的不是报错，而是“agent 半天没反应”。因为 ssh 一旦停下来等密码或等确认，agent 是不会去敲键盘的。

三条铁律，后面所有配置都服务于它们：**免密**（不输密码）、**不交互**（不问 yes/no）、**不阻塞**（连不上就快速失败，而不是挂住）。

> 命名提醒：本文用 `dev` 作为 Orin 上的专用账号名。不要叫 `agent`，否则“agent 执行的命令”和“agent 这个账号”在文中会混成一团，排错时容易看错行。

# 1. 最短路径（约 10 分钟）

大多数人不需要读完第二章的所有分支。先按物理连接选一条链路：

- 开发套件、手边有 USB-C 数据线 → **USB device 模式**，地址恒为 `192.168.55.1`，最稳。
- 两台机器在同一个局域网 → 用 **mDNS**，直接连 `orin.local`，不用锁 IP。
- 只有一根网线、也不在同一局域网 → **静态直连**。

本文所有示例统一用别名 `orin`，它在第 3 章配置。最短路径如下：

**第 1 步，Orin 上开 sshd、建账号**（串口或临时接显示器操作）：

```bash
sudo apt update
sudo apt install -y openssh-server tmux
sudo systemctl enable --now ssh
sudo adduser dev
sudo usermod -aG dialout dev
```

**第 2 步，Windows 的 Git Bash 里生成密钥**（不设口令，原因见 3.2）：

```bash
ssh-keygen -t ed25519 -C 'agent@windows' -f ~/.ssh/id_ed25519 -N ''
```

**第 3 步，把公钥装到 Orin**（只需输一次密码）：

```bash
ssh dev@192.168.55.1 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' < ~/.ssh/id_ed25519.pub
```

**第 4 步，写 `~/.ssh/config`**（内容见第 3 章，把 `HostName` 换成你实际用的地址）。

**第 5 步，验证**：`ssh orin 'echo link-ok'`，不提示密码、直接打印 `link-ok`，就说明通道成立。

> 注意：如果你打算用静态 IP 或 Tailscale，务必**先保留一条能进 Orin 的路**（串口、USB device 或显示器），再改网络配置。改网络把自己锁在门外是这份文档最常见的事故。

# 2. Orin NX 侧：把 ssh 服务开出来

## 2.1 确认 ssh 服务已安装并启动

JetPack 标准镜像一般自带，但被裁剪过的镜像、或出厂只烧了最小系统的情况不一定有。

```bash
sudo apt update
sudo apt install -y openssh-server
sudo systemctl enable --now ssh
systemctl status ssh --no-pager
```

看到 **active (running)** 就算成功。

> 这里有个顺序陷阱：`apt install` 需要外网。如果 Orin 当前没联网、又确实没装 openssh-server，先别急着断网配静态 IP——用 USB device 模式、串口或临时插网线把包装上，再开始后面的网络实验。

到这一步 Orin 已经可以被连了，但还缺两件事：地址要稳定，账号要干净。

## 2.2 把地址固定下来：按优先级选一种

先说结论：**优先用名字而不是 IP**。能解析 `orin.local` 的话，地址变了也不用改任何配置。下面按推荐顺序排列，越靠前越省事。

### 2.2.1 首选：mDNS，用 orin.local 代替 IP

前提：两端在同一广播域（同一路由器、ICS 共享、直连都算），且 Orin 上装了 avahi。

```bash
sudo apt install -y avahi-daemon
sudo systemctl enable --now avahi-daemon
avahi-resolve -n "$(hostname).local"
```

然后在 Windows 上验证：

```powershell
ping orin.local
```

能解析出地址，就把 `~/.ssh/config` 里的 `HostName` 写成 `orin.local`。之后 DHCP 换地址、换网口，ssh 都不用改。

> 限制：Windows 自带的 mDNS 解析偶尔会被 VPN 客户端或防火墙拦住；局域网里也不能有两台同名机器。解析失败就退回下面的 IP 方案，不要在这上面耗太久。

### 2.2.2 开发套件首选：USB device 模式，地址恒为 192.168.55.1

Orin NX 开发套件的 USB-C 口（就是刷机用的那个）默认带一路 USB 虚拟网卡，Jetson 侧固定是 `192.168.55.1`，Windows 侧通常自动拿到 `192.168.55.100`。

Orin 上确认：

```bash
ip -4 addr show l4tbr0
```

Windows 上确认多出来一块网卡，并在同一网段：

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like '192.168.55.*' }
```

如果没有自动获取，手工给那块网卡配 `192.168.55.100/24`（无网关）：

```powershell
$nic = (Get-NetAdapter -Physical | Where-Object { $_.InterfaceDescription -like '*USB*' -or $_.InterfaceDescription -like '*RNDIS*' } | Select-Object -First 1).Name
Set-NetIPInterface -InterfaceAlias $nic -Dhcp Disabled
New-NetIPAddress -InterfaceAlias $nic -IPAddress 192.168.55.100 -PrefixLength 24
```

优点：不依赖任何外部网络、地址永不变、插上就能用。限制：只有带 USB device 口的载板才行；长时间跑重负载时仍建议用开发套件的 DC 供电，不要只靠 USB。

### 2.2.3 一根网线直连：两端手工配静态 IP

没有路由器、也没有 USB 口时，这是最稳的做法。现在的网卡基本都支持 auto-MDIX，普通网线直连即可，不需要交叉线。

**Orin 这一侧**：

```bash
nmcli connection show          # 先确认真实连接名
sudo nmcli connection modify 'Wired connection 1' \
  ipv4.method manual \
  ipv4.addresses 192.168.1.50/24
sudo nmcli connection up 'Wired connection 1'
```

直连没有网关，不写 gateway。设 DNS 也没用（没有出口），不要照抄网上那些加 `8.8.8.8` 的命令。

动手前先存一份存档，方便回滚：

```bash
nmcli -g ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.dns \
  connection show 'Wired connection 1' > ~/orin-net-backup.txt
cat ~/orin-net-backup.txt
```

四行依次是 method、addresses、gateway、dns。这份文件只在改配置期间用，恢复完就删。

**Windows 这一侧必须一起配。**直连时没有 DHCP，Windows 那块网卡会自己落进 `169.254.x.x`，和 `192.168.1.50` 不在同一网段，ping 必然不通。用管理员权限打开 PowerShell，先让脚本认出接 Orin 的以太网卡：

```powershell
$nic = (Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' -and $_.Status -eq 'Up' } | Select-Object -First 1).Name
$nic | Out-File "$env:USERPROFILE\orin-nic-name.txt" -Encoding utf8
Get-Content "$env:USERPROFILE\orin-nic-name.txt"
```

输出是一行网卡名，比如“以太网 2”。后面所有命令都从这个文件读名字。如果输出为空，或者有多块网口都插着线，先运行 `Get-NetAdapter` 看一眼，把对的那个名字写进同一个文件。

如果这块网卡原本是手工静态地址，先把原值记下来（原本是自动获取就跳过）：

```powershell
$nic = Get-Content "$env:USERPROFILE\orin-nic-name.txt"
Get-NetIPConfiguration -InterfaceAlias $nic | ConvertTo-Json -Depth 5 |
  Out-File "$env:USERPROFILE\orin-net-win.json" -Encoding utf8
```

再写地址：

```powershell
$nic = Get-Content "$env:USERPROFILE\orin-nic-name.txt"
Set-NetIPInterface -InterfaceAlias $nic -Dhcp Disabled
# 若这块网卡还挂着 APIPA 地址，先清掉，否则 New-NetIPAddress 可能报“对象已存在”
Get-NetIPAddress -InterfaceAlias $nic -AddressFamily IPv4 |
  Where-Object { $_.PrefixOrigin -eq 'WellKnown' } |
  Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceAlias $nic -IPAddress 192.168.1.60 -PrefixLength 24
Get-NetIPAddress -InterfaceAlias $nic -AddressFamily IPv4
```

配完后 Windows 会把这块网卡标成“未识别的网络”“无 Internet 访问”，直连场景下这是正常现象，不影响 ssh。

> 两端掩码必须一致（都是 /24），IP 只差最后一段。配错一端的表现是 ping 不通、ssh 直接超时——先 ping，再怀疑 sshd。

**怎么检验**：地址 → 连通 → 端口与登录，三步，哪一步不过都别让 agent 上手。

**第一步，确认地址生效。**Orin 上：

```bash
ip -4 addr show | grep 192.168.1.50
nmcli -g ipv4.method connection show 'Wired connection 1'
nmcli device status
```

应该看到 `192.168.1.50/24`、方法为 `manual`、对应网口状态为 `connected`。不要用 `ip link show eth0` 判断——Jetson 上接口名不一定是 `eth0`，也可能是 `enP8p1s0` 或 `l4tbr0`，`nmcli device status` 才是可靠的。

**第二步，双向 ping。**Windows 打过去：

```powershell
ping 192.168.1.50 -n 10
```

Orin 打回来：

```bash
ping -c 4 192.168.1.60
```

十次丢包为 0 说明网线、网口、IP 都没问题。偶尔丢一两个包，先换网线，别急着改配置。

> 一个容易误判的组合：Windows ping Orin 通，Orin ping Windows 不通。这多半不是网络问题，而是 Windows 防火墙默认拦 ICMP 入站；ssh 往往照样能连（22 端口是出站连接）。要验证可以临时放行：

```powershell
netsh advfirewall firewall add rule name="allow-icmpv4" protocol=icmpv4 dir=in action=allow
```

**第三步，验 22 端口和登录。**

```powershell
Test-NetConnection 192.168.1.50 -Port 22
```

看到 `TcpTestSucceeded : True` 就是通的。然后回 Git Bash：

```bash
ssh dev@192.168.1.50 'echo link-ok'
```

这一步先直接用地址，不要用别名——别名要等第 3 章配好 `~/.ssh/config` 才存在。不提示密码、打印 `link-ok`，说明链路、sshd、密钥三样全齐。

**怎么复原**：手动改回原样，别指望重启。静态 IP 是写进连接配置的，重启只是再应用一次同一份配置。

Orin 侧改回自动获取：

```bash
sudo nmcli connection modify 'Wired connection 1' ipv4.method auto ipv4.addresses '' ipv4.gateway ''
sudo nmcli connection up 'Wired connection 1'
hostname -I
```

如果原本就是手动地址，把存档里的值填回去：

```bash
sed -n '1,4p' ~/orin-net-backup.txt     # 先看一眼，确认行序
sudo nmcli connection modify 'Wired connection 1' \
  ipv4.method manual \
  ipv4.addresses "$(sed -n 2p ~/orin-net-backup.txt)" \
  ipv4.gateway "$(sed -n 3p ~/orin-net-backup.txt)"
sudo nmcli connection up 'Wired connection 1'
```

> 这招假定原配置只有一个地址。原来配了多个地址或策略路由，就照着 `cat` 出来的原值手写回去，别硬套 sed。

Windows 侧：原本是 DHCP 的，直接改回自动；原本是静态的，用前面存的 JSON 把地址和网关写回。

```powershell
$nic = Get-Content "$env:USERPROFILE\orin-nic-name.txt"
Get-NetIPAddress -InterfaceAlias $nic -AddressFamily IPv4 |
  Where-Object { $_.PrefixOrigin -eq 'Manual' } |
  Remove-NetIPAddress -Confirm:$false -ErrorAction SilentlyContinue
Set-NetIPInterface -InterfaceAlias $nic -Dhcp Enabled
Set-DnsClientServerAddress -InterfaceAlias $nic -ResetServerAddresses
ipconfig /release; ipconfig /renew; ipconfig /all
```

原本是静态的情况，从存档里确认原来的地址、前缀长度、网关，再用 `New-NetIPAddress` 写回（有网关就带 `-DefaultGateway`）。

两端都恢复后删临时文件：

```bash
rm -f ~/orin-net-backup.txt
```

```powershell
Remove-Item "$env:USERPROFILE\orin-nic-name.txt","$env:USERPROFILE\orin-net-win.json" -ErrorAction SilentlyContinue
```

### 2.2.4 让 Windows 共享网络，Orin 照旧自动获取

Windows 本身能上网（连着 Wi-Fi 或另一个网口）时，可以用系统自带的“Internet 连接共享”临时当路由器。好处是 Orin 不用改配置，还能顺便装依赖、拉代码。

Win+R 输入 `ncpa.cpl` → 右键能上网的网卡（一般是 WLAN）→ 属性 → 共享 → 勾选“允许其他网络用户通过此计算机的 Internet 连接来连接” → “家庭网络连接”选接 Orin 的以太网卡 → 确定。

Windows 会把那块以太网卡设成 `192.168.137.1` 并起 DHCP。Orin 侧保持自动获取即可：

```bash
sudo nmcli connection modify 'Wired connection 1' ipv4.method auto
sudo nmcli connection up 'Wired connection 1'
hostname -I
```

**怎么检验**

```powershell
ipconfig | findstr 192.168.137
```

```bash
ip -4 addr show | grep 192.168.137
```

Orin 先 ping Windows，这一步同时会在 Windows 上留下邻居记录：

```bash
ping -c 4 192.168.137.1
```

再把 Orin 地址从邻居表里取出来（过滤掉不可达条目，避免取到脏记录）：

```powershell
$orin = (Get-NetNeighbor -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -like '192.168.137.*' -and
                 $_.IPAddress -ne '192.168.137.1' -and
                 $_.State -notin @('Unreachable','Incomplete') } |
  Select-Object -First 1).IPAddress
$orin | Out-File "$env:USERPROFILE\orin-ip.txt" -Encoding utf8
ping $orin -n 4
Test-NetConnection $orin -Port 22 -InformationLevel Quiet
```

`Test-NetConnection` 返回 `True` 就是端口通。登录测试统一放在 Git Bash 里做，不要混用 PowerShell 的 ssh（原因见 3.1）：

```bash
ssh dev@192.168.137.20 'echo link-ok'
```

**怎么复原**：Orin 接回路由器即可；Windows 侧取消共享勾选，再把网卡放回自动获取：

```powershell
$nic = (Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' -and $_.Status -eq 'Up' } | Select-Object -First 1).Name
Set-NetIPInterface -InterfaceAlias $nic -Dhcp Enabled
Set-DnsClientServerAddress -InterfaceAlias $nic -ResetServerAddresses
ipconfig /renew
```

> 三个限制：网段固定 `192.168.137.0/24`，家里 Wi-Fi 恰好也是这段就会冲突；ICS 会覆盖这块网卡原有设置，别和 2.2.3 的静态 IP 混用；Windows 侧必须能上网。

### 2.2.5 兜底：两端都用链路本地地址

一根网线直连、两边都不配 IP 时，Windows 会分 `169.254.x.x`（APIPA），Ubuntu 也会分链路本地地址，两端其实已经能通，只是地址不好记。

> 胜在零配置，适合临时看一眼状态；代价是重启后地址可能变，而且没有网关、没有外网。当兜底，不要当长期方案。

检验三步：

```bash
ip -4 addr show | grep 169.254
```

```powershell
ipconfig | findstr 169.254
```

拿到 Orin 的地址后（全篇唯一必须手抄地址的地方，因为此时还没有任何通道能传文件）：

```powershell
ping -n 4 169.254.x.x
Test-NetConnection 169.254.x.x -Port 22
```

```bash
ssh dev@169.254.x.x 'echo link-ok'
```

ping 通但 ssh 超时，回 2.1 看 sshd，再看 2.5 的防火墙。

复原：什么都不用做，拔线接回原网络即可。唯一要留神的是残留——之前用过 2.2.3 或 2.2.4，必须先把那边的静态设置或共享撤掉，否则链路本地地址可能不出现。

### 2.2.6 异地：用 Tailscale 虚拟组网

Windows 在家、Orin 在实验室，拉不了网线时，让两端各自上网、组进同一个虚拟局域网。以 Tailscale 为例：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

Windows 装客户端登录同一账号，两端各得一个 `100.x.x.x` 的固定地址，ssh 直接走它。穿透由客户端负责，不需要公网 IP，也不用在路由器上做端口映射。前提是 Orin 能主动访问外网。

检验：

```bash
tailscale status
tailscale ping 100.x.x.x
```

看到 `direct` 表示打洞成功，`relay` 表示走中继（能用但慢）。

Orin Pair Console v0.6.0 起会读取这个状态：被控端页面「远程组网」卡片显示本机 `100.x` 地址和在线对端，控制端扫描会自动把在线对端加进候选并标 `Tailscale`，不用手填地址。安装和登录也做成页面按钮：被控端需要 root（先在「环境准备」里切换），点「安装 Tailscale」执行官方脚本，点「登录」给出授权链接；Windows 端安装走 winget、会弹 UAC。平时仍是只读检测（`tailscale status --json`）；普通用户读不到状态时先 `sudo tailscale set --operator=$USER`，或以 root 启动程序。

复原：临时断开 `sudo tailscale down` / `tailscale down`；彻底退出账号用 `logout`；要恢复开机自启：

```bash
sudo systemctl enable --now tailscaled
```

> 断开后 ssh 必然不通，记得把 `~/.ssh/config` 和 CLAUDE.md 里的 `100.x` 地址一起改掉，否则 agent 会一直往一个不存在的地址重试。

### 2.2.7 额外留一手：串口 console

改网络配置把自己锁在门外是常事。Orin NX 的载板或 40-pin 上有调试串口，接 USB-TTL 到 Windows，用 PuTTY 以 115200 8N1 连上去就能进 console。不用于日常控制，但值得提前接好、验证一次。

### 2.2.8 选型表

| 方式 | 需要外网 | Orin 要改配置 | Windows 要配 | 适合 |
|-|-|-|-|-|
| mDNS（2.2.1） | 装 avahi 时需要 | 装 avahi | 无 | 同局域网，想一劳永逸 |
| USB device（2.2.2） | 不需要 | 不用 | 通常不用 | 开发套件、手边调试 |
| 静态直连（2.2.3） | 不需要 | 要 | 要 | 固定工位、长期使用 |
| Windows 共享（2.2.4） | Windows 侧要能上网 | 不用 | 开共享 | 临时调试，还要装依赖 |
| 链路本地（2.2.5） | 不需要 | 不用 | 不用 | 看一眼状态、应急 |
| Tailscale（2.2.6） | 两端都要能上网 | 装客户端 | 装客户端 | 异地、跨网络；Orin Pair 可自动发现对端 |

选法：长期摆在手边，用 2.2.2 或 2.2.1；只想快点连上看一眼，用 2.2.5；要让它上网装包，用 2.2.4；两台机器不在同一网络，只能 2.2.6。

> 别被“都不需要路由器”这类说法误导：2.2.1 和 2.2.6 都需要两端能正常上网，只是不需要你去路由器后台做端口映射。

## 2.3 建一个专用账号，别用 root

agent 会执行它自己生成的命令，也可能会犯错。**权限越小，一次错的代价越小**。单开一个账号，不要拿日常账号或 root 顶上去。

```bash
sudo adduser dev
sudo usermod -aG dialout dev
# sudo 组按需，见下
```

**dialout** 不要漏。Orin 上跑串口、CAN、GPIO 的脚本经常需要它，漏了之后 agent 会报 `Permission denied: /dev/ttyTHS1`，而且很难一眼看出是权限问题而不是代码问题。

sudo 组按需要加：只做运行、日志、调试，不加更安全；需要装依赖、改服务，再加。加了 sudo 就等于把这台机器交给了模型，务必配合第 5 章的审计和备份。

## 2.4 确认防火墙没有拦 22 端口

```bash
sudo ufw status
```

只有 ufw 处于 active 时才需要放行。放行时尽量限定来源网段，而不是对全世界开：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
```

## 2.5 收口 sshd（建议做，但别把自己锁外面）

密钥登录验证通过之后再改。改之前**保留一个已经连上的会话**，并用 `sshd -t` 校验，避免配置写错后重新登录失败。

```bash
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config
echo 'AllowUsers dev' | sudo tee /etc/ssh/sshd_config.d/10-agent.conf
sudo sshd -t && sudo systemctl reload ssh
```

关掉密码登录之后，暴力破解基本失去意义；`AllowUsers` 则保证只有这一个账号能进来。

> 如果重启后设置又变回去了，检查 `/etc/ssh/sshd_config.d/` 里的其他文件和 cloud-init，Jetson 镜像里后者可能会覆盖 sshd 配置。

# 3. Windows 侧：把 ssh 配成免密、不阻塞

## 3.1 只用 Git Bash，不要混用两套 ssh

Claude Code 执行 Bash 命令时用的是 **Git Bash** 那套环境。所以密钥、config 都要用 Git Bash 里的 ssh 来生成和管理，不要再单独用 Windows 原生 OpenSSH，否则会出现“PowerShell 能连、agent 连不上”的分裂状态；原生 ssh 对私钥 ACL 要求也更严，常见的报错是 `bad permissions`。

> 因此本文所有 ssh / scp 命令都放在 Git Bash 里执行。前面 PowerShell 段落只用来改网卡配置。

## 3.2 生成密钥：不要设口令

```bash
ssh-keygen -t ed25519 -C 'agent@windows' -f ~/.ssh/id_ed25519 -N ''
```

`-N ''` 表示空口令。原因很直接：`BatchMode yes` 会禁止一切交互，密钥一旦带口令，agent 永远等不到输入，只会得到 `Permission denied`。私钥的保护靠文件权限和磁盘加密，不靠口令。

如果已经有带口令的密钥，去掉口令：

```bash
ssh-keygen -p -f ~/.ssh/id_ed25519 -N ''
```

路径就是 `C:\Users\你的用户名\.ssh\id_ed25519`。

## 3.3 把公钥装到 Orin

```bash
ssh dev@192.168.55.1 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys' < ~/.ssh/id_ed25519.pub
```

两句 chmod 别省。`authorized_keys` 权限过宽（比如 644 且目录 777），sshd 会直接拒绝这把钥匙，报出来的却是 `publickey` 验证失败，很容易误判成“钥匙装错了”。

装完可以用 `ssh -i ~/.ssh/id_ed25519 dev@<地址> 'echo ok'` 先单独验一次，再配 config。

## 3.4 写 ~/.ssh/config

别名的作用是让命令变短、让参数统一。在 Git Bash 里编辑 `~/.ssh/config`（没有就新建，别用记事本存成带 BOM 的 UTF-8，建议用 `nano`）：

```text
Host orin
    HostName 192.168.55.1
    User dev
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    BatchMode yes
    RequestTTY no
    ConnectTimeout 8
    ServerAliveInterval 30
    ServerAliveCountMax 3
    StrictHostKeyChecking accept-new
    HostKeyAlias orin
```

逐个说关键项：

- **BatchMode yes**：禁止一切交互式提问。密钥失效时立刻失败，agent 能看到明确报错；不加它，ssh 会停下来等密码，命令永久挂住。
- **ConnectTimeout 8**：连不上时 8 秒内失败。没有它，host 掉线后每条命令都可能卡几十秒，agent 会以为任务还在跑。
- **ServerAliveInterval 30 + ServerAliveCountMax 3**：防止 Orin 休眠、网线松一下之后连接变僵尸，表现是“命令执行了但没有任何输出”。
- **IdentitiesOnly yes**：不让 ssh 挨个试本机所有密钥，避免偶尔拿错钥匙。
- **RequestTTY no**：agent 不需要终端，显式关掉，避免远端程序等输入。
- **HostKeyAlias orin**：主机指纹按别名记，而不是按 IP。IP 变了（DHCP 换地址、换链路）不会触发 `Host key verification failed`。这一条和“固定 IP”是两回事，建议都留。
- **StrictHostKeyChecking accept-new**：首次连接自动记住指纹，不用人工确认。它只对“新主机”生效，对“同一地址换了机器”仍然会拦，这是好事。

## 3.5 验证这条通道

```bash
ssh orin 'hostname && uname -m'
ssh orin 'cat /etc/nv_tegra_release'
ssh -T orin 'df -h /'
```

三条都必须满足：能连上、不提示密码、有正常输出。任何一条要你输密码，就停在这里修密钥，不要往下做——后面所有问题都会被它放大。

连不上时先看 `ssh -v orin 'true'` 的输出，它会告诉你卡在解析、TCP、认证还是密钥交换。

# 4. 让 Claude Code 真的按“远程”方式干活

通道通了，但 agent 并不知道哪台是远端。它默认在自己所在的 Windows 里执行命令，于是会出现“让它看 Orin 的温度，它在本机跑 df”。解决这个问题靠两份配置文件，不靠每次提问重复解释。

## 4.1 在项目根目录写 CLAUDE.md

Claude Code 每一轮都会读项目里的 CLAUDE.md。把约定写死在这里，等于给它一份长期行为规范：

```markdown
# 远程设备约定
- 目标机：Orin NX（Ubuntu 22.04），SSH 别名 `orin`
- 需要在目标机执行的一切命令，都写成 `ssh orin '命令'` 的形式
- 统一用 `ssh orin`，不要写成 `ssh -T orin` 或 `ssh dev@IP`，权限白名单只认这个前缀
- 不要在本机（Windows）执行 apt / pip / ros2 / systemctl / nvidia-smi
- 不使用交互式命令（vim、top、less、htop）；查文件用 `ssh orin 'sed -n 1,80p 路径'`
- 远端命令一律不加 sudo；需要提权的操作先停下来问我
- 环境变量显式 source，不依赖登录 shell：
  `ssh orin 'source /opt/ros/humble/setup.bash && ...'`
- 长任务放 tmux：`ssh orin 'tmux new -d -s job "命令"'`
- 传文件用 scp 或 rsync，不要用 cat 拼接；写入远端新目录前先 `ssh orin 'mkdir -p 目录'`
- 同一条失败命令最多重试两次，仍失败就停下来报告 stderr
```

最后一条很实用：agent 常见的坏习惯是对着同一个错误反复重试，把日志刷满。

## 4.2 用权限白名单放行 ssh——以及它的真实边界

默认情况下 Claude Code 每执行一条 Bash 命令都要你点确认。正确做法是只放行 ssh 这一族命令。在项目里建 `.claude/settings.json`：

```json
{
  "permissions": {
    "allow": [
      "Bash(ssh orin:*)",
      "Bash(scp:*)",
      "Bash(rsync:*)"
    ]
  }
}
```

规则按命令前缀匹配，所以 `ssh orin '...'` 会自动放行，而本机的 `rm`、`dd`、`apt` 仍然逐条确认。

但必须说清楚它**不**做什么：

- 它只减少弹窗，不缩小爆炸半径。`ssh orin 'sudo rm -rf /'` 同样匹配 `ssh orin:*`，一样会被放行。
- 前缀是字面匹配，`ssh -T orin` 不匹配 `ssh orin:*`，会重新弹确认——这也是 CLAUDE.md 要求统一写法的原因。
- 真正的边界在远端：专用账号、不给 sudo、镜像备份、命令审计。白名单只是第一层。

> 不要图省事用 `--dangerously-skip-permissions`。那等于把删除、格式化、改系统服务的决定权整体交给模型。Orin 是机器人主控，一次误操作可能要重刷镜像才能恢复。

## 4.3 第一次跑起来，重点看它怎么动手

在项目目录执行 `claude`，然后输入：

```text
ssh 到 orin 上看一下磁盘空间和 CPU 温度
```

合格的表现：它先执行 `ssh orin 'df -h'` 和读 `/sys/class/thermal` 的命令，再给结论。不合格的表现：它在本机执行 `df`。出现后者，说明 CLAUDE.md 没被读到——通常是没放在项目根目录，或者当前工作目录不是那个项目。

## 4.4 传文件与跑长任务

```bash
ssh orin 'mkdir -p ~/work'
scp -r ./src orin:~/work/
rsync -avz --exclude '.git' ./ orin:~/work/
```

> **Git Bash 不自带 rsync**（`scp` 有，`rsync` 没有）。要么先单独装 rsync（MSYS2 的 `pacman -S rsync`，或用 cwRsync 之类），要么就只用 `scp`。不装的话，白名单里放行 rsync 也没用，只会得到 `command not found`。

`run_in_background` 这类本地后台在 ssh 场景下靠不住：ssh 会话一断，远端子进程跟着被杀。长任务必须挂在 tmux 或 nohup 里：

```bash
ssh orin 'tmux new -d -s run "python3 main.py 2>&1 | tee ~/run.log"'
ssh orin 'tmux capture-pane -pt run'
ssh orin 'tmux kill-session -t run'
```

`capture-pane` 这一步很关键——它让 agent 能“看”到远端终端的输出，而不是只能等你贴日志。

> 命令里如果本身带双引号，外层再套一对双引号会打架。这种情况把命令写成远端脚本文件（`scp` 上去再 `ssh orin 'bash ~/run.sh'`），别在引号上硬凑。

## 4.5 ROS 2 与环境变量：`bash -lc` 不是万能的

非交互式 ssh 不会读 `~/.bashrc` 里给交互 shell 准备的那部分，所以 `ros2` 会报 `command not found`。表现很像“这台机器没装 ROS”，其实只是没有 source。

但要注意：**`bash -lc` 也救不了 `~/.bashrc`**。Ubuntu 默认的 `.bashrc` 开头就有“非交互就 return”的判断：

```bash
case $- in
    *i*) ;;
      *) return;;
esac
```

登录 shell 虽然会去 source `.bashrc`，但一进门就被这行挡住了，写在文件末尾的 ROS 环境变量根本不会加载。所以：

- **显式 source 最可靠**，每次都写在命令里：

```bash
ssh orin 'source /opt/ros/humble/setup.bash && ros2 node list'
ssh orin 'source /opt/ros/humble/setup.bash && source ~/ws/install/setup.bash && ros2 topic list'
```

- 想少写一点，就把 source 放进 `~/.profile`（登录 shell 会读），或者建一个 `~/ros_env.sh`，命令开头 `source ~/ros_env.sh`。
- 不建议去改 `.bashrc` 里那个交互判断，副作用比收益大。

把这条规则写进 CLAUDE.md，比每次问“为什么 ros2 找不到”省事得多。

## 4.6 可选：给远端加一层命令审计

白名单管不住远端执行什么，能兜底的是日志。把专用账号的登录 shell 换成一个只记录、不改行为的包装脚本：

```bash
sudo tee /usr/local/bin/agent-shell >/dev/null <<'EOF'
#!/bin/bash
logger -t agent-remote -- "${SSH_ORIGINAL_COMMAND:-interactive}"
exec /bin/bash "$@"
EOF
sudo chmod 755 /usr/local/bin/agent-shell
sudo usermod -s /usr/local/bin/agent-shell dev
```

之后每条通过 ssh 进来的命令都会进 syslog，用 `journalctl -t agent-remote` 就能回溯 agent 到底执行过什么。改完记得重新登录，并测一次 `scp`/`rsync`，确认没把文件传输弄坏；有问题用 `sudo usermod -s /bin/bash dev` 撤回。

# 5. 三种部署形态怎么选

前面讲的是“同一形态下怎么找地址”，这里讲的是整体形态，别混在一起。

| 形态 | 适合什么情况 | 代价 |
|-|-|-|
| Windows 原生 + Git Bash + ssh（本文方案） | 已经装好 Claude Code，只想尽快跑起来 | 每条远端命令都要带 ssh 前缀，引号和路径要写对 |
| Windows + WSL2 + ssh | 需要 rsync、tmux、scp 全套 Linux 工具链，或脚本里大量 Linux 写法 | 多一层 WSL；NAT 模式下访问局域网设备偶尔要额外处理 |
| 反向：Claude Code 装在 Orin，Windows 用 ssh+tmux 连进去 | 主要工作在 Orin 上，且希望断网也不中断任务 | Orin 上要再装 Node 和 Claude Code；模型响应受 Orin 负载影响 |

你现在的状况，第一种最省事；如果后面发现 rsync 的 `--exclude`、shell 引号老是出问题，再迁到第二种。

# 6. 常见问题对照

| 现象 | 原因 | 处理 |
|-|-|-|
| 命令发出后一直没输出，也不报错 | ssh 在等密码或等主机指纹确认 | config 加 `BatchMode yes`、`ConnectTimeout`；用 `ssh -v` 看卡在哪一步 |
| `Permission denied (publickey)` | 公钥没进 authorized_keys，或权限过宽 | `chmod 700 ~/.ssh`、`chmod 600 ~/.ssh/authorized_keys` |
| 密钥明明对，还是 publickey 失败 | 密钥带了口令，BatchMode 下无法输入 | `ssh-keygen -p -f ~/.ssh/id_ed25519 -N ''` |
| `Connection timed out` / `No route to host` | IP 变了、不在同一网段、或 ufw 拦了 22 | `hostname -I` 重取地址；`ufw status` |
| `Connection refused` | sshd 没起，或监听端口不是 22 | `systemctl status ssh`；`ss -ltnp \| grep 22` |
| `Host key verification failed` | 地址被别的机器占用，或重装过系统 | `ssh-keygen -R <地址>`；config 加 `HostKeyAlias orin` |
| 连上后很快断 / `Broken pipe` | 网卡节能、Wi-Fi 漫游、NAT 超时 | 开 `ServerAliveInterval`；设备管理器里关掉网卡“允许计算机关闭此设备以节约电源” |
| PowerShell 里 ssh 报 `bad permissions` | Windows 原生 ssh 对私钥 ACL 要求严 | 改在 Git Bash 里执行；或用 icacls 收紧私钥权限 |
| `ros2`、`python` command not found | 非交互 shell 没 source，`bash -lc` 也不够 | 显式 `source`，或写进 `~/.profile` |
| agent 执行的命令其实跑在本机 | CLAUDE.md 没放项目根目录，或 cwd 不对 | 确认文件位置，并明确说“在 orin 上执行” |
| `rsync: command not found` | Git Bash 不自带 rsync | 单独装，或改用 scp |
| 日志里中文乱码 | 远端 locale 不是 UTF-8 | 命令前加 `LANG=C.UTF-8`，或改远端 locale |
| API 报 401 | 环境变量只在当前会话有效 | 写进用户级环境变量或 `~/.bashrc`，新开终端用 `env \| grep` 核对 |

# 7. 安全边界

这套方案给了 agent 一台真实机器的执行权，限制要提前想好，别等出事再补：

- **专用账号，不给 root。**sudo 组按实际需要决定，能不加就不加。
- **先做系统备份。**Orin 刷一次镜像成本很高，动手前至少保留一份可回刷的镜像或 eMMC 备份。
- **白名单只放 ssh、scp、rsync，但别指望它防住远端。**远端要配合专用账号、无 sudo、命令审计；确实需要 sudo 时，用 `/etc/sudoers.d/` 只放行具体命令，不要 `NOPASSWD: ALL`。
- **通电的设备会真动。**涉及电机、云台、推进器的命令，先空载或拆桨测试，再上真实负载。
- **密钥和 API Key 不进版本库。**不要把 `sk-` 开头的 key 写进 CLAUDE.md 或项目里的任何文件。
- **私钥不留口令是权衡。**换来的是自动化和稳定性，代价是本机被拿到就等于钥匙被拿到；至少保证 Windows 账户有密码、磁盘加密开启、`.ssh` 目录权限只给自己。

# 8. 回滚清单

按顺序撤，别只撤一半：

1. 网络：按 2.2 各节的“怎么复原”恢复两端，删掉 `~/orin-net-backup.txt`、`orin-nic-name.txt`、`orin-net-win.json`。
2. 别名：删掉 `~/.ssh/config` 里的 `Host orin` 整段。
3. agent 约定：删掉项目的 CLAUDE.md 远程约定，或整个 `.claude/settings.json` 的 allow 项。
4. 远端钥匙：从 `~/.ssh/authorized_keys` 删掉对应那一行；不再需要账号就 `sudo deluser --remove-home dev`。
5. 审计或加固：`sudo usermod -s /bin/bash dev` 撤包装 shell；sshd 改动从 `sshd_config.bak` 恢复后 `sudo sshd -t && sudo systemctl reload ssh`。
6. 虚拟组网：`sudo tailscale logout`、`sudo systemctl disable --now tailscaled`。
7. 最后确认：从 Windows 执行 `ssh dev@<原地址> 'true'` 已经连不上，说明通道确实撤干净了（如果你本来就不想让它能连）。

**总结一下：**Orin 侧开 sshd、优先用 USB device 或 mDNS 拿到稳定地址、建专用账号并收口 sshd；Windows 侧用 Git Bash 生成无口令密钥、装到 authorized_keys、在 config 里开 BatchMode 和 ConnectTimeout；然后用 CLAUDE.md 规定“一切远端操作走 ssh orin”，用 settings.json 只放行 ssh 一族命令，并接受“白名单不是沙箱”这个事实。最后一定要先手工执行 `ssh orin` 确认免密登录，再让 agent 接手。

---

# 附录 A：Orin Pair Console 用到的技术

前面是把同一套 ssh 通道手工配出来；Orin Pair Console 把这件事做成两个页面：Windows 上点扫描、输码，Ubuntu/Orin 上显示配对码和管理公钥。下面按“请求怎么走、数据存在哪、哪些命令真正被执行”拆开讲。

## A.1 技术栈：一个 Python 文件加一个浏览器

| 层面 | 用的东西 | 为什么这样选 |
|-|-|-|
| 语言/运行时 | Python 3，**只用标准库** | 目标机器只要有 `python3`，不用 pip、不用联网装依赖；整个目录拷过去就能跑 |
| HTTP 服务 | `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` | 一个进程同时提供页面和 JSON API，每个请求一个线程，长操作（装包、ssh 测试）不会堵住页面轮询 |
| 页面 | 手写 HTML/CSS/JS，放在 `app/pairui/` | 不引入 Qt、Electron 或前端框架；Windows 上直接用 Edge/Chrome 的 `--app` 窗口打开 |
| 调系统命令 | `subprocess.run` / `Popen` | ssh、ssh-keygen、nmcli、systemctl、apt-get、ufw、tailscale、winget 都是系统里现成的 |
| 两端通信 | `urllib.request` 发 JSON over HTTP | 控制端 → 被控端、控制端 → 本机 Tailscale 状态都走这个，不需要额外 RPC 库 |
| 随机与摘要 | `secrets`、`hashlib`、`base64` | 生成配对码/令牌，算公钥和主机指纹 |
| 并发 | `threading`、`concurrent.futures.ThreadPoolExecutor` | 扫描时最多 8 个候选地址并发探活，结果按延迟排序 |
| 权限提升 | `sudo -S`（Linux）、`ShellExecuteW(..., "runas", ...)`（Windows） | 只在用户点按钮时触发；Linux 用 stdin 传密码，Windows 弹 UAC |

没有数据库、没有 Web 框架、没有前端构建。运行状态就是 `app/runtime/` 下几个 JSON 和文本文件，删掉对应目录即回到初始状态。

## A.2 三个角色、三个端口

| 角色 | 默认端口 | 监听地址 | 职责 |
|-|-|-|-|
| 选择页 `auto` | 8770 | `0.0.0.0` | 判断系统、选择控制端/被控端；远程打开只能选被控端 |
| 控制端 `windows` | 8766 | `127.0.0.1` | 扫描、配对、生成密钥、写 ssh 配置、测试连接、更新指纹 |
| 被控端 `orin` | 8765 | `0.0.0.0` | 显示配对码、管理 `authorized_keys`、改网络、准备环境、Tailscale |

选择角色不是再启动一个子进程，而是把当前进程 `os.execv` 成对应角色：PID 不变、终端不换、Ctrl+C 仍然退出同一个程序。端口在 exec 时释放，所以选择页的 8770 不会和角色页冲突。页面提交选择后先收到 200，再由前端跳转到新端口。

在页面里“切换为 root”也是同一套思路：先用 `sudo -S -v` 校验密码，再用 `os.execv(sudo, ["sudo", "-S", ...python, --role, orin...])` 原地替换进程；页面每 1.2 秒 ping 一次 `/api/ping`，等到新实例起来后自动刷新。

## A.3 页面和接口

页面没有模板引擎，服务端只做三件事：按角色返回对应 HTML、把 `app/pairui/` 当静态目录、处理 `/api/*`。前端用原生 `fetch` 调接口，被控端每 5 秒、控制端每 6 秒轮询 `/api/status`（有弹窗或输入框聚焦时跳过）；控制端另每 12 秒调一次 `/api/remote_check` 判断远端是否已解除。确认框用 `<dialog>`，提示用页面内的 toast。

所有 POST 都带一个自定义头：

```js
headers['X-Orin-Pair'] = '1';
```

普通跨站表单无法带这个头，配合下面的 Host 校验，可以挡住大部分“恶意网页借本机浏览器调接口”的情况。

常用接口：

| 接口 | 角色 | 作用 |
|-|-|-|
| `GET /api/ping` | 两端 | 扫描探活，返回 app 标识、角色、版本、主机名 |
| `GET /api/status` | 两端 | 页面渲染所需的全部状态；Orin 远程访问需要配对码，且不返回配对码本身 |
| `POST /api/pair` | Orin | 收配对码、设备名、公钥，写 `authorized_keys` |
| `POST /api/whoami` | Orin | 用 `client_id + token` 校验远端配对是否还有效 |
| `POST /api/unpair` | 两端 | 删除公钥和记录；Orin 接受本机、配对码或配对令牌 |
| `POST /api/scan` | 控制端 | 探测 `192.168.55.1`、`orin.local`、Tailscale 在线对端 |
| `POST /api/test` | 控制端 | 用受管配置执行 `ssh orin echo link-ok` |
| `POST /api/hostkey/refresh` | 控制端 | 删除 `known_hosts` 里 `orin` 的行并重新握手 |
| `POST /api/ssh/install` | 控制端 | 用 Windows 可选功能装 OpenSSH Client |
| `POST /api/tailscale/install`、`/up`、`/restart` | 两端 | 安装 Tailscale、拉起登录并返回授权链接、修复卡在 `NoState` 的服务 |
| `POST /api/network/apply`、`/revert` | Orin | 改/还原静态 IP、检测 mDNS/USB |
| `POST /api/setup` | Orin | 装 sshd/tmux/avahi、修 `.ssh` 权限、加 dialout |
| `POST /api/relaunch-root` | Orin | 校验 sudo 密码后原地以 root 重启 |
| `POST /api/code/refresh` | Orin | 更换 6 位配对码 |

## A.4 配对协议

配对不是 SSH 登录，而是两端各跑一段 HTTP：

1. 被控端生成 6 位数字码（`secrets.randbelow`），保存在 `state.json`，本机页面显示。
2. 控制端把 `{code, name, pubkey}` POST 到 `/api/pair`；`pubkey` 来自本机 `id_ed25519.pub`。
3. 被控端校验码后，把公钥追加到目标账号的 `~/.ssh/authorized_keys`，行尾带标记：

   ```
   orin-pair:<client_id> <设备名>
   ```

4. 被控端返回 `client_id`（8 位十六进制）和 `token`（24 字节 URL-safe 随机串）。**令牌只存 SHA-256**，`state.json` 里没有明文。
5. 配对成功后配对码立刻轮换；新码只显示在被控端本机页面上，配对响应里不再返回，避免远程页面或日志顺手拿到。
6. 控制端写受管 ssh 配置，再用一份临时配置做真实 SSH 测试。配置写入失败时会调用一次远端解除，把刚装的公钥撤掉，避免远端留下一条用不上的授权。

之后：

- 控制端用 `client_id + token` 调 `/api/whoami` 判断“Orin 可达 / 已解除 / 不可达”；
- 解除配对时，控制端带令牌通知被控端删公钥；令牌只能撤销自己那台；
- 远程打开被控端页面时，用 `X-Pair-Code` 头证明自己知道当前码，可查看状态、解除配对、装 / 登录 / 重启 Tailscale，但改网络、换码、切 root 仍只限本机。

配对码校验只有一个入口 `verify_code()`，所有接口共用同一套节流：失败后按 `0.3 + 0.2 × 失败次数` 秒排队（上限 2 秒），尝试过密直接 429；同一来源连续失败 8 次会自动换码。只读轮询（`/api/status`）只受节流限制，不累计换码次数，否则一个停在旧码上的页面会把码反复换掉。

## A.5 SSH 通道与主机指纹

控制端不碰真实 `~/.ssh`，所有东西在 `app/runtime/controller/`：

- **密钥**：`ssh/id_ed25519`，没有就用 `ssh-keygen -t ed25519 -N ''` 生成；已有则复用。
- **配置**：`ssh/config` 里只维护一段带标记的 `Host orin`：

  ```
  # >>> orin-pair managed >>>
  Host orin
      HostName <地址>
      User <用户>
      IdentityFile <私钥路径>
      IdentitiesOnly yes
      BatchMode yes
      RequestTTY no
      ConnectTimeout 8
      ServerAliveInterval 30
      ServerAliveCountMax 3
      StrictHostKeyChecking accept-new
      UserKnownHostsFile <known_hosts 路径>
      HostKeyAlias orin
  # <<< orin-pair managed <<<
  ```

  `BatchMode` 保证不会再等密码或 yes/no；`ConnectTimeout 8` 保证连不上时 8 秒内失败；`IdentitiesOnly` 避免 agent 里别的钥匙被乱试；`HostKeyAlias orin` 让不同 IP 共用一条指纹记录，换地址不会变成“未知主机”。
- **备份与冲突**：首次改写前把原文件复制成同目录的 `config.orin-pair.bak`（只备份一次，不覆盖）；如果去掉受管段后还剩其他 `Host ... orin ...` 行，直接报错让你先处理，不会硬写第二份 `Host orin` 让 ssh 的行为变得不确定。
- **主机指纹**：配对测试时写 `ssh/known_hosts`，不再写真实 `~/.ssh/known_hosts`。因为 `accept-new` 只接受没见过的主机，Orin 重装或换机后指纹变化会直接拒绝连接，所以页面提供「更新指纹」：删掉 `orin` 开头的行，再用同一份配置连一次，让 ssh 记录新指纹，并把新指纹算成 `SHA256:…` 显示出来。只动 `orin` 的行，其他主机记录保留。
- **测试**：`ssh -F <config> -o BatchMode=yes -o ConnectTimeout=8 orin echo link-ok`，25 秒超时；`link-ok` 既验证认证成功，也验证远端命令真的执行了。
- **解除**：只删本机受管配置段并清空 `state.json`，私钥和 `known_hosts` 保留；远端公钥由被控端按令牌删除。远端拒绝或不可达时，本地仍清理，但页面会写明“远端未撤销”。

Windows 上找 `ssh` 的顺序是：`ORIN_PAIR_SSH` 环境变量 → `Program Files\Git\usr\bin` → `Program Files (x86)\Git\usr\bin` → `%LOCALAPPDATA%\Programs\Git\usr\bin` → `PATH`；`ssh-keygen` 取同目录或 `ORIN_PAIR_SSH_KEYGEN`。没找到时页面会提示，并提供「安装 SSH 客户端」按钮。

## A.6 被控端做了哪些系统操作

被控端页面上的按钮最终都落到 `subprocess.run`；只读检测普通用户也能跑，特权操作（装包、改网络、防火墙、Tailscale、切 root）执行前会检查 root，失败信息原样带回页面：

| 动作 | 实际命令/操作 |
|-|-|
| 列地址 | `ip -4 -o addr show`，过滤回环 |
| mDNS | 读 `systemctl is-active avahi-daemon`，用 `avahi-resolve -4 -n <hostname>.local` 确认能解析 |
| USB 检测 | 只探测 `192.168.55.1:8765`，不改 IP |
| 静态 IP | `nmcli connection modify <连接> ipv4.method manual ipv4.addresses … ipv4.gateway …`，再 `nmcli connection up`；原 method/addresses/gateway/dns 存进 state 供还原 |
| 防火墙 | `ufw status`；按本机当前地址推断网段，`ufw allow from <网段> to any port 22 proto tcp` |
| 环境准备 | 缺什么装什么：`apt-get update` 后 `apt-get install -y` + 缺少的包（`openssh-server` / `tmux` / `avahi-daemon` / `avahi-utils`）；分别 `systemctl enable --now ssh`、`systemctl enable --now avahi-daemon`；`usermod -aG dialout <账号>`；准备 `~/.ssh` 700/600 |
| 切 root | `sudo -S -v` 校验密码（密码走 stdin，不落盘、不进日志），随后 `exec` 成 root 实例 |
| Tailscale | 官方 `install.sh`、`systemctl enable --now tailscaled`、`tailscale up`、`systemctl restart tailscaled`；Windows 对应 `winget`、`tailscale up`、拉起 `tailscale-ipn.exe`、`Restart-Service Tailscale` |

`app/setup.sh` 和页面「安装 / 修复」做的是同一套常规步骤；脚本额外提供 `--harden`（sshd 加固）和 `--audit`（命令审计），页面不会自动做这两件，避免把远程访问改坏。

## A.7 Tailscale 集成

- **检测是只读的**：执行 `tailscale status --json`，解析 `BackendState`、`Self`、`Peer`、`AuthURL`、`Health`，结果缓存 6 秒。页面只展示客户端是否安装、是否连接、本机 `100.x` 地址和在线对端。
- **控制端扫描**：如果 Tailscale 已连接，把在线对端（最多 8 个）加入候选，结果里标 `Tailscale`；仍然可以手填地址。
- **安装**：
  - Ubuntu：在 root 下执行 `curl -fsSL https://tailscale.com/install.sh | sh`，再启用 `tailscaled`；
  - Windows：用 `winget install --id Tailscale.Tailscale -e`，通过 `ShellExecuteW(..., "runas", ...)` 弹 UAC 启动。
- **登录**：后台 `Popen(["tailscale", "up"])`，最多轮询 10 秒，从状态 JSON 里拿 `AuthURL` 返回给页面；页面把授权链接显示成可点的超链接，登录仍然必须由你在浏览器里用同一账号完成。
- **卡在 NoState / starting 时**：页面把登录按钮禁用并显示「重启 Tailscale 服务」。Windows 先 `Popen` 拉起同目录的 `tailscale-ipn.exe`（睡眠恢复后常见后端没起来，这一步不需要 UAC），等 10 秒；仍没恢复再以管理员权限执行 `Restart-Service Tailscale -Force`。Ubuntu 需要 root，执行 `systemctl restart tailscaled`。恢复后按 `BackendState` 判断，`NeedsLogin` 就可以继续登录。
- **权限**：普通用户读不到守护进程状态时，页面提示 `sudo tailscale set --operator=$USER` 或以 root 启动。
- **凭证**：被控端的安装 / 登录 / 重启接口允许本机访问，或带正确配对码的远程页面，方便 Orin 没接显示器时从 Windows 操作。

命令行位置可以用 `ORIN_PAIR_TAILSCALE` 覆盖；默认顺序是环境变量 → `PATH` → Windows 的 `Program Files\Tailscale\tailscale.exe`。

## A.8 文件、权限和日志

所有运行数据在 `app/runtime/`（按 `pair_console.py` 所在目录计算，不依赖当前工作目录）；目录不可写时退回 `~/.orin-pair/<角色>`。用 `--state-dir` / `--ssh-dir` 可以整体挪走，演示和测试靠这个隔离。

```
app/runtime/
├── tmp/                      # 配对临时 ssh 配置，启动/退出/解除时清理
├── controller/
│   ├── state.json            # 配对主机、用户、client_id、token，600
│   ├── pair-console.log      # 运行日志，600；写满 2 MB 轮换成 .1
│   └── ssh/
│       ├── id_ed25519(.pub)  # 无口令私钥，600
│       ├── config            # 追加受管 Host orin 段，原有内容保留
│       └── known_hosts       # 只记 orin 的主机指纹
└── orin/
    ├── state.json            # 配对码、设备列表（只存 token 哈希）、网络备份，600/700
    └── pair-console.log
```

`authorized_keys` 特殊：它必须留在目标账号的 `~/.ssh/` 下，sshd 才读得到，所以不在 `runtime/` 里。被控端只追加带 `orin-pair:<id>` 标记的行，解除时也只删对应行。

写入方式：

- JSON 先写同目录 `.tmp` 再 `os.replace`，避免断电留下半个文件；
- 文本（config、known_hosts、authorized_keys）用 `os.open(..., 0o600)` 建临时文件再替换，避免 umask 把它们写成 644/664；
- 状态目录 700，`state.json`、私钥、`known_hosts`、日志 600；root 写回时把状态和日志的归属改回目标账号，避免普通用户下次读不到；
- 启动、退出、解除配对都会清 `runtime/tmp`、`runtime/**/*.tmp` 和系统临时目录里的 `orin-pair-*.conf`。

日志同时存在于两处：进程内 `deque(maxlen=300)`（页面显示最近 40 条）和 `pair-console.log`（完整、追加）。文件单条带日期，写满 2 MB 轮换一次，只保留 `.1`；只记操作和路径，不记配对码、令牌和密码。

## A.9 安全设计逐条对应

请求进来先过四道闸门，再进业务：

1. **Host 白名单**：`Host` 只接受 IP 字面量、`localhost`、本机主机名、`<主机名>.local`、本机 FQDN、绑定地址和本机当前 IPv4；其他域名一律 421。这样即使恶意域名解析到 `127.0.0.1`（DNS rebinding），请求里的 `Host` 也对不上。
2. **POST 标识**：必须带 `X-Orin-Pair: 1`；有 `Origin` 时还要求与 `Host` 同源。
3. **角色权限**：
   - 控制端 API 只接受回环地址；即使手动 `--bind 0.0.0.0`，远程请求也是 403；
   - 被控端状态、解除、Tailscale 安装 / 登录 / 重启：本机，或带当前配对码的远程页面；
   - 环境准备、改网络、换码、切 root：只接受本机。
4. **配对码节流**：共用 `verify_code()`，失败延时递增、过密 429、同来源 8 次失败自动换码；配对成功响应不返回新码。

其他约束：

- 地址、端口、远端用户名写进 ssh 配置前都做字符集校验，防止换行注入额外指令；
- 令牌只存哈希，比较用 `secrets.compare_digest`，且只能操作自己那条配对记录；
- `known_hosts` 隔离在 `runtime/`，不污染真实 `~/.ssh`；
- 密码只在 `/api/relaunch-root` 的本机请求里出现，经 `sudo -S` 的 stdin 传入，不写文件、不进日志；
- 演示模式不执行安装、不改网络、不写真实 ssh。

## A.10 演示模式与测试方法

```bash
# 两个终端，完整走一遍配对
python app/pair_console.py --role orin --demo --no-open --port 8765
python app/pair_console.py --role windows --demo --no-open --port 8766
```

演示模式的边界：

- 被控端允许在 Windows 上启动，返回假地址（`192.168.55.1/24`），跳过真实网络和 ssh；
- 控制端扫描会额外试 `127.0.0.1:8765`，所以同一台机器能跑通“扫描 → 配对 → 测试 → 解除”；
- Tailscale 状态仍然真实读取（本机装了就能看到自动发现效果），安装 / 登录 / 重启按钮只返回提示；
- 数据默认仍写 `app/runtime/`，要隔离就加 `--state-dir` / `--ssh-dir`。

开发时的验证手段：`ast.parse` 查 Python 语法、`node --check` 查 JS；接口用临时端口的两个演示实例走 HTTP；Tailscale 状态、安装/登录、SSH 安装分支用打桩（monkeypatch `tailscale_info`、`_find_ssh_tools`、`ShellExecuteW`）验证，避免真的弹 UAC 或改系统。真实环境验证过：VMware Ubuntu 22.04 上普通用户配对/解除、Windows 与真实 Tailscale 对端 `<Orin 的 100.x 地址>` 配对并 `link-ok`。USB device / mDNS 实际连通、静态 IP 应用与还原、防火墙放行、真实缺 ssh 的安装路径仍未在实机验证，细节见 `app/README.md` 的“真实环境验证”。

## A.11 已知限制

- **页面是明文 HTTP**：局域网内配对码、令牌、公钥可被嗅探；走 Tailscale 时外层有 WireGuard 加密，但页面本身仍是 HTTP。只在可信网络使用。
- **私钥不设口令**：这是免交互的代价，本机被拿到就等于钥匙被拿到。
- **控制端只能本机用**：浏览器和程序在同一台 Windows 上；远程打开控制端页面会被拒。
- **Windows 不能当被控端**：被控端依赖 sshd、nmcli、avahi、systemd、ufw，都是 Linux 侧能力。
- **部分操作需要 root**：装环境、改网络、装 Tailscale、切 root；普通用户只能配对、换码、解除。
- **一次只记一个配对目标**：控制端的 `state.json` 只保存当前 Orin；换目标要重新配对或先解除。
- **「更新指纹」先删后连**：如果删掉旧指纹后连不上 Orin，旧记录不会自动恢复，需要等 Orin 可达后再点一次。
- **没有后台服务**：程序跟着终端走，Ctrl+C 就退出；不会注册成 systemd 服务或 Windows 服务。
- **日志有上限**：内存 300 条、文件 2 MB + 一份 `.1`，需要长期留档要自己接日志采集。
- **不做文件传输**：只负责把 ssh 通道和配置准备好，scp/rsync/agent 的远程执行仍走普通 ssh 命令。
