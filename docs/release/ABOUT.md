# GitHub 仓库元数据（About 区 / 社交预览）

本文件是**可直接复制粘贴**的仓库元数据。GitHub 仓库设置页对应位置：

| GitHub 位置 | Settings 里的字段 |
|---|---|
| 仓库首页右上角 | Description |
| 仓库首页右侧栏 | Topics（最多 20 个） |
| 仓库首页右侧栏 | Website |
| 仓库首页右侧栏 | Social preview（社交预览图） |

---

## 1. Description（仓库简介）

GitHub 简介上限 **350 字符**。下面两条分别是中英文版，建议按仓库主语言二选一；
若希望中英文读者都能在第一眼看到要点，用第 2 条（它一行里两种语言都点了名）。

### 1.1 中文版（120 字符）

```
跨平台桌面 SSH 客户端：xterm 兼容终端、SFTP、端口转发、资源监控与 lrzsz 传输，基于 Python + PyQt6 编写，主机密钥校验真实生效。
```

### 1.2 中英混排版（156 字符，推荐）

```
A cross-platform desktop SSH client in Python + PyQt6 — xterm terminal, SFTP, port forwarding, resource monitoring and lrzsz transfer. 跨平台桌面 SSH 客户端，主机密钥校验真实生效。
```

### 1.3 纯英文版（117 字符）

```
A cross-platform desktop SSH client in Python + PyQt6 — xterm terminal, SFTP, port forwarding, resource monitoring and lrzsz transfer.
```

---

## 2. Topics（标签，最多 20 个）

GitHub 的 topic 会直接影响仓库被检索到的方式。以下 16 个覆盖了
「技术栈 / 领域 / 用途 / 平台」四个维度，且都与仓库里真实存在的东西对应：

```
ssh
ssh-client
terminal
xterm
sftp
port-forwarding
lrzsz
zmodem
python
pyqt6
paramiko
gui
cross-platform
networking
security
windows
```

补充候选（按需替换，不要超过 20 个）：

| 候选 | 适用场景 |
|---|---|
| `ssh-tunnel` | 想让隧道相关仓库能找到它 |
| `remote-shell` | 想覆盖「远程 shell」关键词 |
| `sftp-client` | SFTP 是核心功能之一，可单独占位 |
| `qtcustomwidget` | 界面是自绘的，值得挂上 |
| `mitm` | **不建议** —— 那是漏洞标签，不是功能标签 |

> 不建议挂 `windows-only`：本项目在 macOS / Linux 上同样能跑，README「跨平台」
> 一节有逐项对照表。标签与文档不一致比没有标签更糟。

---

## 3. Website

项目暂无独立官网，直接指向仓库自身即可：

```
https://github.com/AlbertD-byte/MavisSSH
```

---

## 4. Social preview（社交预览图）

GitHub 的社交预览图尺寸为 **1280 × 640**。在仓库的
`Settings → General → Social preview` 上传。

制作规范（照做即可，不用设计稿）：

| 项 | 取值 |
|---|---|
| 画布 | 1280 × 640，PNG |
| 底色 | 深色主题底色 `#101319`（与 `sshclient/ui/design/v1/palette.py` 的 `bg_base` 一致） |
| 主图 | `docs/img/screenshot-main-dark.png`，**右侧**摆放，左侧留白放文字 |
| 左侧文字 | 第一行 `MavisSSH`（72px），第二行 `跨平台桌面 SSH 客户端`（28px），第三行 `Python · PyQt6 · GPL-3.0-only`（20px） |
| 文字颜色 | 主标题 `#e9edf4`（`text`），副标题 `#868f9e`（`text_muted`） |
| 圆角与阴影 | 与应用内弹框一致（圆角 + 极轻投影），**不要**加渐变 |

> 图上的色值必须取自 `palette.py`，不要手调。这套配色有实测契约
> （正文对比度 ≥ 4.5、底色间 ΔE ≥ 3.0），手调出来的图和软件本身对不上，
> 比不做还伤。详见 `tests/_accent.py` 与 `tests/_uia11y.py`。

---

## 5. 仓库首页顶部的一句话（README 首屏）

README 现在的首屏已经包含徽章、主界面截图与功能速览，无需改动。首次发布时
确认下列三项齐全：

- [ ] 4 个徽章（License / Python / PyQt6 / Platform）均能渲染
- [ ] `docs/img/screenshot-main-dark.png` 在 GitHub 上能正常显示
- [ ] 首屏可点的 `[快速开始](#快速开始)` 锚点能跳到对应小节
