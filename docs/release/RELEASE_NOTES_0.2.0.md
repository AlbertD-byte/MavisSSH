# MavisSSH 0.2.0 —— 首次公开发布

> 跨平台桌面 SSH 客户端：xterm 兼容终端、SFTP、端口转发、资源监控与 lrzsz 传输。
> Python + PyQt6 编写，主机密钥校验**真实生效**。

**发布日期**：2026-11-10
**许可证**：GPL-3.0-only
**仓库**：https://github.com/AlbertD-byte/MavisSSH

---

## 这是什么

MavisSSH 是一个用 Python + PyQt6 写的桌面 SSH 客户端，覆盖从「连上去敲命令」
到「传文件、开隧道、看资源」这一整条日常运维链路。

它不是 Terminal 的复刻，也不多做云厂商的编排层。它想解决的是一件很具体的事：
**当你在 Windows 上做运维时，是否需要为了一个终端而切到 WSL，或者去用一个
闭源客户端**。答案是多数时候不需要。

**写它的第二个理由是安全。** 市面上大量 SSH 客户端默认接受任何主机密钥。
MavisSSH 把主机密钥校验做成了真正的流程——显示 SHA-256 指纹、可信任一次、
可记住并信任、可管理已信任列表——而不是一个默认勾上的复选框。

---

## 本版亮点

### 新增

- **主机密钥管理**：SHA-256 指纹确认、「信任一次」与「记住并信任」、已信任主机
  列表的增删与导入（`sshclient/remote/hostkeys.py`）
- **会话分组**：可下拉选也可自由输入，分组段可折叠，标签标题改为 `分组名-会话名`
- **从其他终端导入连接**：支持 OpenSSH 配置 / PuTTY / WinSCP，**密码一律不导**
- **命令片段库**：增删改，内置只读命令集，危险命令自检（覆盖包装词与解释器载荷）
- **标签页右键菜单**：关闭当前 / 关闭其他 / 关闭右侧
- **欢迎页快速连接卡片**：最多 5 条，点击即连
- **Windows 安装向导**（NSIS）：可选安装目录、可选桌面快捷方式、控制面板可卸载、
  中英双语、许可证页展示 GPL-3.0 全文
- **隧道暴露面提示**：新增 `is_exposed_address()`，监听地址非回环时需用户确认

### 安全

这一版修复了 8 个安全问题。其中前两项属于**中间人攻击**级别：

| 问题 | 影响 | 处理 |
|---|---|---|
| **主机密钥无条件接受** | 原实现使用 `AutoAddPolicy` 且未加载系统信任库，**任何中间人都能冒充服务器** | 改为显式校验 + 指纹确认流程 |
| **lrzsz 远端命令注入** | 路径拼接未加引号，可注入远端命令 | 路径逐个加引号，`~` 交由远端 `${HOME}` 展开；`conn.exec` 与交互式 shell 的 `sendall` **两个注入点都堵上了** |
| **`proxy_command` 本地命令执行** | `Popen(shell=True)` 构成任意本地命令执行面 | **删除该特性** |
| **弱算法未禁用** | 默认算法偏好里仍可协商到 SHA-1 等弱项 | 显式配置 `disabled_algorithms`，保留唯一强项 |
| **隧道暴露面未提示** | 监听 `0.0.0.0` 时端口对全网可达且无任何提示 | 非回环监听地址需用户确认 |
| **认证重输不收敛** | 失败后反复重试可被用于耗尽服务端资源 | 收敛重试次数 |
| **日志泄漏敏感信息** | 日志中会写入可用的凭据片段 | 凭据字段脱敏 |
| **依赖下限过松** | `paramiko>=3.4` 会让老环境里残留带漏洞的 4.x | 收紧为 `paramiko>=5.0,<6` |

> 完整的安全说明、支持版本与报告流程，见 [`SECURITY.md`](https://github.com/AlbertD-byte/MavisSSH/blob/main/SECURITY.md)。

### 无障碍

这一版同时修了三处**真实的 AA 失败**，以及一个更要紧的问题：**无障碍判据
从来没跑过**。

- **深色主题 `danger` 破 AA**：原值 `#f35555` 压在 `bg_hover_row`
  （监控面板「磁盘热区」那一行被鼠标悬停时的底色）上只有 **4.428**。
  提亮到 `#f65958` 后是 **4.603**，色距 ΔE00 仅 **1.035**（JND 约 2.3），
  肉眼与原色无差
- **监控面板的百分比标签压在状态色填充上**：深色主题下只有
  **2.163 / 2.150 / 2.865**，三档状态色**全破 AA**。
  现在标签底下垫一块与轨道同色的胶囊，标签永远压在对比度已知的底上
- **标题栏与标签页的 ✕ 借用了 `text_on_accent`** —— 那是按 accent 推的，
  压 `danger` 属于跨族借用（浅色主题下 3.237）。改为按填充色自己算黑白
- **`tests/_uia11y.py` 从未进门禁** —— 它不在 `_runall.py` 的 `TESTS` 元组里，
  CI 也不会跑它。**门禁不跑，等于没有门禁**
- **判据表本身量错了地方**：上一轮那条 `danger/bg_dialog`（文字档）量的
  是一对**永不相邻**的底色 —— 弹框里根本没有 danger 文字。
  现在每条判据都对应真实使用点，图形档与文字档分开

### 修复

- **修复真机崩溃**：worker 醒来后往已销毁的控件投递 → `RuntimeError`。
  新增 `sshclient/ui/thread_post.py` 统一做存活检查
- 修复主窗口关闭时连接 worker 干等 120 秒
- 修复「首次信任必失败」：paramiko 的 `save_host_keys()` 会先重读文件，首次信任时
  文件尚不存在 → `FileNotFoundError` → 握手失败。**该缺陷离线判据测不到**，
  是真机联调抓出来的
- 修复弹框主按钮丢失实心填充（只剩描边，看起来像「默认被选中」）
- 修复连接框编辑已有会话时主按钮写「连接」（应为「保存」）
- 修复标签栏溢出时关闭按钮被滚动按钮覆盖
- 修复终端滚回历史时整页卡死（单帧 6 秒 → 60ms）
- 修复拖左边 / 上边缩放时窗口不跟光标走
- 修复标签关闭按钮贴错标签、窄栏下凭空消失
- 修复「从其他终端导入」一点就崩

### 文档与工程

- **58 套离线判据**接入统一门禁 `_runall.py`，每套独立进程、独立 180 秒期限
- **GitHub Actions CI**：Windows runner 上跑门禁 + `ruff` 检查，失败自动上传日志。
  配置模板随仓库发布在 [`docs/ci.yml`](https://github.com/AlbertD-byte/MavisSSH/blob/main/docs/ci.yml)，
  复制到 `.github/workflows/ci.yml` 即可启用
- **零第三方测试框架**：全部判据是独立可执行的 Python 脚本，在没配开发环境的
  机器上也能复跑
- **判据只钉性质不钉字面量**：本轮修好了 8 套「钉死了绝对像素 / 具体色值 /
  源码文本」的判据 —— 换一种正确实现就会红的那种
- **判据的元断言有区分度**：每条元断言都指名道姓说明「老实现在哪一批输入上
  会露馅」，并配一条反向对照
- 补齐社区文件：`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`SECURITY.md`、
  `CHANGELOG.md`、Issue / PR 模板

---

## 安装

### Windows（推荐：安装向导）

从 [Releases](https://github.com/AlbertD-byte/MavisSSH/releases) 下载
`MavisSSH-0.2.0-Setup.exe`，双击安装。

### 从源码运行

需要 **Python 3.10+**。一律使用项目内的虚拟环境——应用不应依赖 `PATH` 上
碰巧是哪个 Python。

```console
git clone https://github.com/AlbertD-byte/MavisSSH.git
cd MavisSSH
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python run.py
```

macOS / Linux 用 `.venv/bin/python` 代替。

---

## 已知限制

诚实起见，以下几点在本次发布时**尚未在真机上完整验证**：

- **macOS / Linux 未经真机验证**。代码路径已做跨平台处理（配置目录、CJK 兜底
  字体、`rz`/`sz` 的来源差异），并有 `tests/_portable.py` 专门守这些地方，
  但发布时尚未在真实系统上跑通。Windows 是唯一经过完整验证的平台。
- **屏幕阅读器支持有限**。界面为自绘，语义标注尚未补全。
- **lrzsz 传输需要远端自行安装 `lrzsz`**（`sz` / `rz` 在 `PATH` 里）。
  未安装时相关功能会明确跳过，而不是假装成功。
- **SFTP 面板的文件列表按钮不受离线判据覆盖**——它需要一个真实的
  `SFTPClient` 才能建起来，由 `tests/_live.py` 对真服务器覆盖。
- **门禁只在 Windows 上跑过**。部分判据基于字体度量，跨平台的数字不一致。

设计取舍、踩过的坑与未验证项的完整记录，见
[`docs/ARCHITECTURE.md`](https://github.com/AlbertD-byte/MavisSSH/blob/main/docs/ARCHITECTURE.md)。

---

## 参与贡献

欢迎提交 Issue 与 Pull Request。

- 报告 Bug：请用 [Bug 模板](https://github.com/AlbertD-byte/MavisSSH/blob/main/.github/ISSUE_TEMPLATE/bug_report.yml)，
  附上复现步骤与 `logs/mavisssh.log` 中的相关片段（**注意先检查日志里有没有
  敏感信息**）
- 提交 PR：见 [`CONTRIBUTING.md`](https://github.com/AlbertD-byte/MavisSSH/blob/main/CONTRIBUTING.md)，那里写明了**怎么跑
  门禁**和**判据该怎么写才对**
- 参与讨论：请遵守 [`CODE_OF_CONDUCT.md`](https://github.com/AlbertD-byte/MavisSSH/blob/main/CODE_OF_CONDUCT.md)

### 用 MiniMax Code 开发

本仓库的判据、架构文档与测试报告都按 **MiniMax Code（MCode）** 的协作约定
组织，新接手的人可以直接让 MCode 接手。详见 README 的
[「使用 MiniMax Code 进行开发」](https://github.com/AlbertD-byte/MavisSSH/blob/main/README.md#使用-minimax-code-进行开发) 一节。

---

## 许可

**GNU General Public License v3.0 only（GPL-3.0-only）**。

之所以不用 MIT / Apache-2.0，是因为本项目分发时会携带
`bin/rz.exe`、`bin/sz.exe` 与 `msys-2.0.dll` 等第三方二进制——它们不是
paramiko 的一部分，但在分发包内。GPL-3.0 的第 6(d) 条允许为二进制提供
**书面获取途径**，我们用 GitHub Release 的源码 tarball 满足这一条。
`THIRD_PARTY_NOTICES.md` 收录了全部第三方归属声明，`LICENSES/` 下有 6 份
协议原文。

**使用与二次分发请注意**：GPL-3.0 是 copyleft 协议。如果你修改本项目并对外
**分发**（无论是否收费），必须以 GPL-3.0 公开你的修改版，并保留本项目的
版权声明与许可证。

---

## 校验

安装包与源码 tarball 均提供 SHA-256 校验和，发布在对应的 Release 资产中：

```console
Get-FileHash .\MavisSSH-0.2.0-Setup.exe -Algorithm SHA256
```

**最后一行写字的人**：项目维护者 · 2026-11-10
