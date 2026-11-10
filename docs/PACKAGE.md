# 包结构梳理

> **已执行。** 本文先记录重构前的现状与问题，再记录选定方案与执行结果。
> 数字都来自实测，不是估计。

结果是量的，不是看的：重构前 `sshclient/` 下 14 个模块、7738 行全部平铺，
依赖图**无环**，两个汇聚点是 `theme`（7 个模块依赖它）和 `tunnel`（5 个）。

**方向本身是对的，问题出在归组**——同一个文件里塞了几件不相干的事。选了方案 B
（按层次分子包），已落地：

| | 重构前 | 重构后 |
|---|---|---|
| 布局 | 14 个模块平铺在 `sshclient/` | `core/` 6 个、`remote/` 2 个、`ui/` 13 个 |
| `theme.py` | 1164 行，混配色/字体/控件/510 行 QSS | 拆成 `core/palette.py`、`core/fonts.py`、`ui/qss.py`、`ui/tab_close.py` |
| `main_window.py` | 1510 行，5 个类 | 拆出 `ui/sidebar.py`(233)、`ui/controls.py`(34)，剩 1271 行两个类 |
| 测试字体辅助 | `sshclient/_testfonts.py`（发布包内） | `tests/fonts.py`（包外） |
| 依赖图 | 无环 | 无环，且方向严格单向向内 |

`core/` 不依赖 paramiko / pyte —— 这一条是**实测**的：屏蔽这两个包后
`core/` 全部 6 个模块照常导入。`core/fonts.py` 例外地需要 PyQt6，因为字体解析
的输入就是 `QFontDatabase`；README 与 `core/__init__.py` 都如实写明了这一点，
没有写成一条不成立的口号。

### 三个已确认的决策

1. **不留 `theme.py` 兼容门面。** 一次改干净，不留 re-export 的聚合模块。
2. **`_testfonts.py` 移出包外**，而不是留在 `sshclient/_testhelpers/`。
3. **不拆 `SessionTab` / `MainWindow` 这两个上帝类。** 见下。

### 后来追加的两项

- **测试代码整体归入 `tests/` 包**（原 30 个 `_*.py` 平铺在仓库根）。搬完顺带
  修掉 7 处硬编码的 `D:\workspaces\...` 绝对路径——仓库换个位置测试就全废。
  现在每个脚本自己把项目根加进 `sys.path`，从任何目录跑都可以。
- **`core/` 新增 `crypto.py`（落盘加密）与 `logs.py`（日志）**。两者都只用
  标准库加 `cryptography`，不引入 Qt / paramiko / pyte，因此 `core/` 的
  "不需要网络就能测"这条契约不受影响。

---

## 一、现状（重构前）

### 模块清单（按行数）

| 模块 | 行数 | 顶层类 | 内部依赖 |
|---|---:|---|---|
| `main_window.py` | 1510 | SessionItem, CompactSessionItem, WelcomePage, SessionTab, MainWindow | 9 个模块 |
| `theme.py` | 1164 | Palette, TabCloseButton, TabCloseController | 无 |
| `terminal_widget.py` | 907 | _Screen, TerminalWidget | theme |
| `sftp_panel.py` | 546 | FileEntry, Remote/LocalFileSystem, FilePanel, SftpPanel | theme |
| `tunnel_panel.py` | 521 | _SwitchDelegate, _AddDialog, TunnelPanel | connection, theme, tunnel |
| `monitor_panel.py` | 518 | 6 个数据类 + Bar + MonitorPanel | connection, theme |
| `tunnel.py` | 460 | TunnelError, TunnelSpec, Forward | 无 |
| `icons.py` | 415 | （40 个小绘制函数 + 分发器） | theme |
| `connect_dialog.py` | 376 | _Field, _TunnelRulesEditor, ConnectDialog | theme, tunnel |
| `connection.py` | 364 | 4 个数据类 + SSHConnection | tunnel |
| `settings_dialog.py` | 323 | SettingsDialog | settings, theme |
| `lrzsz.py` | 319 | ZmodemEvent/Pump/Detector | 无 |
| `settings.py` | 232 | SavedSession, SessionStore, AppSettings | tunnel |
| `_testfonts.py` | 83 | （测试辅助） | 无 |

### 依赖图

```
theme  ← 7 个模块（connect_dialog, icons, main_window, monitor_panel,
         settings_dialog, sftp_panel, terminal_widget, tunnel_panel）
tunnel ← 5 个模块（connect_dialog, connection, main_window, settings,
         tunnel_panel）
```

`tunnel` 与 `theme` 是叶子（不依赖任何内部模块），其余依赖都指向这两个，
**没有环**。这个骨架本身是健康的。

---

## 二、四个具体问题

### 1. `main_window.py` 1510 行里装着两个上帝类和三个侧边栏控件

```
  26  _label / _icon_button / _side_button / _pin / _unpin_width / _release
  97  SessionItem          47 行
 146  CompactSessionItem   71 行
 223  WelcomePage          43 行
 272  SessionTab          627 行   ← 终端+SFTP+监控+隧道四个子页签 + 工具栏 + 状态机
 913  MainWindow          598 行   ← 窗口骨架 + 侧边栏 + 菜单 + 设置接入
```

五个类没有一条共同的生命周期：前三者是侧边栏的一行，后两者分别是标签页和
窗口。`SessionTab` 627 行尤其扎眼——它同时是容器、状态机和工具栏持有者。

### 2. `theme.py` 一件事都不纯粹

```
  15-133    配色 token：Palette / DARK / LIGHT / THEMES / get_palette / ansi_palette
 169-420    字体策略：ui_font / mono_font / 策展清单 / 排除表
 424-620    两个真正的控件：TabCloseButton(QToolButton) + TabCloseController(QObject)
 626-1137   build_qss() —— 510 行 QSS 字符串
1138-1164  工具函数：qcolor / blend / apply
```

**一个"设计系统"模块里放着 QWidget** 是最明确的越界：`TabCloseController`
依赖标签栏几何、字体度量和边距，跟调色板毫无关系。`build_qss` 那 510 行
字符串本身也不是"theme"，是"皮肤"。

### 3. `_testfonts.py` 住在被发布的包里

```python
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')   # 进程级全局副作用
```

只要有人 import 它，整个进程的 Qt 平台就被切成离屏。它是测试辅助，却放在
应用包 `sshclient/` 里——没人会预期在产品代码目录里埋这个。

### 4. 协议层和界面层混在同一个文件

| 模块 | 纯逻辑部分（无 Qt 控件） | 界面部分 |
|---|---|---|
| `sftp_panel.py` | FileEntry, RemoteFileSystem, LocalFileSystem（39-177） | FilePanel, SftpPanel（178-546） |
| `monitor_panel.py` | 6 个数据类 + `parse_snapshot`（59-246） | Bar, MonitorPanel（249-510） |
| `tunnel.py` | TunnelSpec, validate, parse_socks5_request | Forward, handle_accepted |

这些是**好拆**的：左边能单独测（`_stress.py` 已经在测 `Forward`/`TunnelSpec`
了），右边是 Qt 胶水。

### 顺带：两个小问题

- `tunnel_panel.py` 带 **UTF-8 BOM**。Python 容忍（PEP 263），但任何按
  `utf-8` 解码的工具都会把 `U+FEFF` 当正文第一个字符——写梳理脚本时就被它
  绊了一下。
- 「动态 import / 反射」扫描命中了 6 个模块，看着危险，**全是误报**：命中的是
  `QDialog.exec()` 和 `SSHConnection.exec()`，不是内置 `exec()`。好消息是
  拆包不存在静默爆炸的动态依赖。

---

## 三、候选方案

### 方案 A：单层扁平，只按职责改名与拆文件（推荐）

不引入子包，14 → 17 个文件。

```
sshclient/
  palette.py        配色 token（Palette/DARK/LIGHT/get_palette/ansi/xterm256/qcolor/blend/apply）
  fonts.py          字体解析与策展清单
  qss.py            build_qss
  icons.py          不变
  tab_close.py      TabCloseButton / TabCloseController        ← 从 theme 移出
  settings.py       不变
  connection.py     不变
  lrzsz.py          不变
  terminal.py       TerminalWidget + _Screen + 颜色解析
  sidebar.py        SessionItem / CompactSessionItem / WelcomePage   ← 从 main_window 移出
  session_tab.py    SessionTab                                     ← 从 main_window 移出
  main_window.py    MainWindow                                     ← 只剩窗口
  sftp_panel.py     不变
  monitor_panel.py  不变
  tunnel_panel.py   不变
  connect_dialog.py 不变
  settings_dialog.py不变
```

理由：依赖图已经是单向无环的，**分层没问题，坏的只是归组**。再套子包只会把
14 个文件变成 30 个、多加一层 `__init__.py`，却不改变任何一条依赖。

### 方案 B：按层次分子包

```
sshclient/
  core/    palette.py fonts.py settings.py
  ssh/     connection.py lrzsz.py
  ui/      qss.py icons.py tab_close.py terminal.py sftp.py
           monitor.py tunnel_panel.py dialogs/ sidebar.py session_tab.py
  main_window.py
  testhelpers/ fonts.py
```

只有当模块数继续增长（>25）时这一层才值回它的样板成本。现在放还太早。

### 两个方案都不解决的

`SessionTab`（627 行）和 `MainWindow`（598 行）**是类太大，不是文件太大**。
把它们搬到独立文件只是让问题更显眼，并不修复。是否拆这两个类，是另一个
量级的决定，且会改动大量测试断言的落点。建议**先搬文件，跑一轮回归，再单独
评估**——不要在同一个改动里既搬又拆。

---

## 四、改动量（实测）

---

## 五、执行结果

拆 `theme` + 拆 `main_window` 合计：

- **35 条 import 语句**要改，分布在 20 个文件里
- 改动最集中的：`main_window.py`(5)、`terminal_widget.py`(4)、`_e2e.py`(3)、
  `connect_dialog.py`(3)
- 引用点最多的符号：`get_palette` 46、`Palette` 38、`SessionTab` 34、
  `MainWindow` 29、`mono_font` 26、`ui_font` 23
- 加上 `_testfonts` 搬家要改 12 个测试脚本的 `from sshclient._testfonts import`

全是机械改动，没有一处**有意**涉及行为。风险主要在**漏改**，而本仓库有 20 套独立
测试脚本 + `_readme.py` 文档守卫，漏改会在回归里立刻现形——这是拆分能安全
做的主要保障。20/20 全绿。

### 搬迁过程中被回归逮到的 4 个真 bug

这些都是纯搬迁本身带出来的、不是新写的代码里的：

1. `from sshclient import settings, ...` 形式的导入被漏改——批量脚本只匹配了
   `from sshclient.settings import X` 一种形态。
2. `from sshclient.core.settings import settings` 语义错误：模块不是包，不能
   当子模块导入。正确写法是 `from sshclient.core import settings`。
3. `core/lrzsz.py` 的 `_add_default_bin()` 用 `Path(__file__).parent.parent`
   数跳数找 `bin/`，搬进子包后指向 `sshclient/bin`，错了一层。改成向上查找
   `bin/` 或 `main.py` 的循环，与包深度无关。`_e2e` 的
   `rz/sz binary found` 一项当场变红。
4. 批量脚本把补上的导入插到了 `from __future__ import annotations` **之前**，
   直接 SyntaxError；相对层级又多算了一层。改成全绝对路径 + 插在首个导入块。

### 顺带修掉的陈旧内容

- `ui/design/v1/palette.py` 的 `qcolor()` / `blend()` 改为函数内延迟导入 QtGui，
  `core/` 因此真的不需要 Qt（此前 README 写的是「不依赖 PyQt6」，**不成立**）。
- `ui/design/v1/qss.py` 的 `build_qss()` 改为延迟导入 `TabCloseController`——它只用到一个
  常量 `PAD_RIGHT`，不该因为一个数字就把 Qt 拖进导入期。
- `ui/main_window.py` 与 `ui/sftp_panel.py` 的 `paramiko` 收进 `TYPE_CHECKING`
  守卫：它们只在类型标注里用到 paramiko，运行时不需要。
- 删掉 `sshclient/widgets/`——一个 607c767 引入、写着 "Empty package marker"、
  从未被任何代码引用的空壳包。
- 清掉 4 处随搬迁变成死代码的导入（`Optional`、`field`、两处 `QtGui`）。

### 仍然没做的

`SessionTab`（627 行）和 `MainWindow`（598 行）留在同一个文件里。这是**故意**的：
它们互相持有（窗口建标签、标签回调窗口），拆到两个文件只会把同一份耦合换成
一条 import 边，读代码时反而要来回跳。**类太大不等于文件太大**，搬文件不修复
这个问题；是否拆这两个类是另一个量级的决定，会改动大量测试断言的落点，应当
单独评估。

