# 发布公告（可直接发布的文案）

本文件是**面向社区发布**的文案，按平台长度分了三档。三档是同一份内容的三种
压缩方式，请**按需取用，不要三档一起发**。

| 档位 | 字符数 | 适合平台 |
|---|---|---|
| [A 短版](#a-短版约-400-字符) | ~400 | X / Twitter、微博、Mastodon |
| [B 标准版](#b-标准版约-900-字符) | ~900 | V2EX、Linux.do、掘金、知乎、Reddit |
| [C 长版](#c-长版约-1800-字符) | ~1800 | 博客、公众号、Newsletter |

链接统一为：

```
仓库    https://github.com/AlbertD-byte/MavisSSH
Release https://github.com/AlbertD-byte/MavisSSH/releases/tag/v0.2.0
```

---

## A 短版（约 400 字符）

```
MavisSSH 0.2.0 开源了 —— 一个用 Python + PyQt6 写的跨平台桌面 SSH 客户端。

xterm 兼容终端、SFTP、端口转发（本地转发 + SOCKS5）、资源监控、lrzsz 传输，
会话分组、命令片段库、欢迎页快速连接。

安全是重写它的原因：默认接受主机密钥是很多 SSH 客户端的通病，本项目把校验
做成了真正的流程 —— SHA-256 指纹确认、信任一次 / 记住并信任、已信任主机管理。
本次发布还修掉了 8 个安全问题，包括 lrzsz 远端命令注入和 proxy_command 的
本地命令执行面。

GPL-3.0-only，欢迎提 Issue 和 PR。

https://github.com/AlbertD-byte/MavisSSH
```

---

## B 标准版（约 900 字符）

```markdown
**MavisSSH 0.2.0 开源发布** —— 一个用 Python + PyQt6 写的跨平台桌面 SSH 客户端。

### 它能做什么

- xterm 兼容终端（基于 pyte，支持 ANSI / 256 色 / 光标控制）
- SFTP 文件管理 —— 本地与远端并排，上传 / 下载
- 端口转发 —— 本地转发（-L）与 SOCKS5 代理（-D），每条规则带实时连接数
- 资源监控 —— CPU、内存、磁盘、网络、进程排行
- lrzsz / ZMODEM 传输（rz / sz）
- 会话分组、命令片段库、标签页右键菜单、欢迎页快速连接卡片
- 深色 / 浅色主题、会话持久化、多标签页
- 可打包成 Windows 安装向导（NSIS）

### 为什么重写它：安全

市面上大量 SSH 客户端默认接受任何主机密钥 —— 中间人冒充服务器时不会有任何
提示。MavisSSH 把主机密钥校验做成了真正的流程：显示 SHA-256 指纹、可「信任
一次」、可「记住并信任」、已信任主机列表可增删导入。

0.2.0 一共修掉 8 个安全问题，其中两个是中间人级别的：

- **主机密钥无条件接受** —— 原实现用 AutoAddPolicy 且不加载系统信任库
- **lrzsz 远端命令注入** —— 路径未加引号；`conn.exec` 与交互式 shell 的
  `sendall` 两个注入点都已堵上
- 另外删除了 `proxy_command`（`Popen(shell=True)` 本地命令执行面）、
  显式禁用了弱算法、隧道监听非回环地址时要求确认

### 工程质量

- 58 套离线判据接进统一门禁，每套独立进程、独立超时；不引入 pytest
- GitHub Actions CI（Windows runner）跑门禁 + ruff，配置模板见 `docs/ci.yml`
- 判据全部按「钉性质不钉字面量」写，元断言带区分度
- 已知限制写在文档里：macOS / Linux 尚未真机验证

GPL-3.0-only。Windows 优先，macOS / Linux 可运行但未完整验证。欢迎提 Issue
和 PR。

仓库：https://github.com/AlbertD-byte/MavisSSH
Release：https://github.com/AlbertD-byte/MavisSSH/releases/tag/v0.2.0
```

---

## C 长版（约 1800 字符）

```markdown
**MavisSSH 0.2.0 开源发布**

一个用 Python + PyQt6 写的跨平台桌面 SSH 客户端。
GPL-3.0-only · https://github.com/AlbertD-byte/MavisSSH

---

### 为什么又是一个 SSH 客户端

Windows 上做运维，现成的选择无非是：装个闭源客户端、或者切到 WSL。
MavisSSH 想说明这两件事都不必 —— 一个原生窗口，能连、能传文件、能开隧道、
能看资源。

但真正驱动这次重写的是**安全**。

### 安全：把主机密钥校验做成流程，而不是勾选项

大量 SSH 客户端默认接受任何主机密钥。这在实践中意味着中间人攻击不会有
任何提示 —— 你连上的那台机器，可能不是你以为的那台。

MavisSSH 的做法是：握手时显示远端的 SHA-256 指纹，由你决定「信任一次」还是
「记住并信任」；已信任主机列表可以查看、增删、导入导出。拒绝之后连接直接失败，
而不是悄悄放行。

### 0.2.0 修掉的 8 个安全问题

| 问题 | 影响 |
|---|---|
| 主机密钥无条件接受 | 中间人可冒充服务器 |
| lrzsz 远端命令注入 | 路径未加引号，可注入远端命令 |
| `proxy_command` | `Popen(shell=True)`，本地命令执行面 |
| 弱算法未禁用 | SHA-1 等弱项仍可协商 |
| 隧道暴露面无提示 | 监听 `0.0.0.0` 时全网可达且无提示 |
| 认证重输不收敛 | 可被用于耗尽服务端资源 |
| 日志泄漏凭据片段 | 日志中出现可用的敏感信息 |
| 依赖下限过松 | `paramiko>=3.4` 会残留带漏洞的 4.x |

其中「首次信任必失败」这个缺陷值得一提：paramiko 的 `save_host_keys()` 会先
重读文件，而第一次信任时文件还不存在。**离线判据测不出来** —— 判据用的是
内存里的信任表，`save_host_keys` 根本不会被调到。它是真机联调抓出来的。

### 功能

xterm 兼容终端（pyte，ANSI / 256 色 / 光标控制）、SFTP 文件管理、端口转发
（本地 `-L` 与 SOCKS5 `-D`，带实时连接数）、资源监控（CPU / 内存 / 磁盘 /
网络 / 进程排行）、lrzsz 传输、会话分组、命令片段库、标签页右键菜单、
欢迎页快速连接卡片、深浅双主题、Windows 安装向导（NSIS）。

### 工程质量

- **58 套离线判据**接进统一门禁 `_runall.py`，每套独立进程、独立 180 秒
  期限。某套卡住只影响它自己
- **不引入 pytest** —— 全部是独立可执行的 Python 脚本，在没配开发环境的
  机器上也能复跑
- **GitHub Actions CI** 在 Windows runner 上跑门禁 + `ruff`，失败自动上传日志。
  配置模板随仓库发布在 `docs/ci.yml`，复制到 `.github/workflows/ci.yml` 即可启用
- 判据统一按「**钉性质不钉字面量**」写，并且每条元断言都有区分度 ——
  老实现必须在至少一批输入上露馅，元断言自身也要能被反向验证
- 设计决策与踩过的坑都写在 `docs/ARCHITECTURE.md` 里，包括那些「说错了比
  做少了更坏」的地方

### 已知限制

诚实地说：macOS / Linux 的代码路径已处理，并有专门的判据守着，但**尚未在
真实系统上跑通**。Windows 是唯一完整验证过的平台。屏幕阅读器支持有限。
门禁只在 Windows 上跑过，因为部分判据基于字体度量。

### 参与

欢迎提 Issue 和 PR。仓库里也写了怎么用 MiniMax Code 接手这个项目 —— 判据、
架构文档和测试报告都是按它的协作约定组织的。

仓库：https://github.com/AlbertD-byte/MavisSSH
Release：https://github.com/AlbertD-byte/MavisSSH/releases/tag/v0.2.0
```

---

## D 英文版

### D.1 标准版

```markdown
**MavisSSH 0.2.0 is open source** — a cross-platform desktop SSH client written
in Python and PyQt6.

### What it does

- xterm-compatible terminal (pyte — ANSI, 256 colour, cursor control)
- SFTP file management, local and remote side by side
- Port forwarding: local (`-L`) and SOCKS5 (`-D`), with live connection counts
- Resource monitoring: CPU, memory, disk, network, process ranking
- lrzsz / ZMODEM transfer (`rz` / `sz`)
- Session groups, command snippets, tab context menu, quick-connect cards
- Dark and light themes, session persistence, multi-tab
- Ships as a Windows installer (NSIS)

### Why it was rewritten: security

Many SSH clients accept any host key by default, which means a man-in-the-middle
can impersonate your server without any warning. MavisSSH makes host-key
verification a real workflow: show the SHA-256 fingerprint, trust once, trust and
remember, and manage the trusted-host list.

This release fixes eight security issues, two of them man-in-the-middle class:
unconditional host-key acceptance (the old code used `AutoAddPolicy` and never
loaded the system trust store) and lrzsz remote command injection (both
injection points — `conn.exec` and the interactive shell's `sendall` — are now
closed). It also removes `proxy_command`, disables weak algorithms explicitly,
and warns before a tunnel binds a non-loopback address.

### Engineering

- 58 offline criteria suites behind one gate, each in its own process with its
  own deadline; no pytest
- GitHub Actions CI on a Windows runner (config template ships as `docs/ci.yml`)
- Criteria are written to pin *properties*, never literal values, and every
  meta-assertion is verified to have discriminating power
- Known limitations are documented: macOS / Linux are not yet verified on real
  hardware

GPL-3.0-only. Windows first; macOS and Linux run but are not fully verified.
Issues and pull requests are welcome.

Repository: https://github.com/AlbertD-byte/MavisSSH
Release: https://github.com/AlbertD-byte/MavisSSH/releases/tag/v0.2.0
```

### D.2 短版

```
MavisSSH 0.2.0 is open source — a cross-platform desktop SSH client in
Python + PyQt6.

xterm terminal, SFTP, port forwarding, resource monitoring, lrzsz transfer,
session groups, command snippets.

Security is why it exists: host-key verification is a real workflow here
(SHA-256 fingerprint, trust once / trust and remember, manageable trusted list),
and this release fixes eight security issues — including unconditional host-key
acceptance and lrzsz remote command injection.

GPL-3.0-only. Issues and PRs welcome.
https://github.com/AlbertD-byte/MavisSSH
```

---

## 发布检查清单

发布前逐项确认：

- [ ] 完整门禁 `_runall.py` 全绿（不是「判据全绿但退出码非零」）
- [ ] `ruff check .` 无输出
- [ ] `docs/img/` 下 5 张截图能正常打开，README 首屏那张能正常渲染
- [ ] Release 资产已上传，且附带 SHA-256 校验和
- [ ] `CHANGELOG.md` 的「未发布」小节已并入 0.2.0
- [ ] 仓库 About 区的 description / topics 已按 `ABOUT.md` 填写
- [ ] 社交预览图已上传（1280 × 640）
- [ ] 旧的私有 Gitea 远端已从仓库配置中移除，`origin` 改指 GitHub
- [ ] `git log` 中不出现真实 IP、真实用户名与提交者邮箱之外的痕迹
