# 迁移清单：PyQt6 → PySide6

> **状态**：已完成 spike 验证。**结论是不迁移**（见 §0/§1）。
> 本文作为**将来真要迁移时的证据与清单**保留 —— 触发条件见 §1.3。
> **最后更新**：2026-10-13
> **依据**：`tests/_probe_pyside_*.py` 三个探针在两个真实 venv 下的实测输出，
> 不是文档推断。凡是没量到的，本文明确标了「未验证」。

---

## 0. 结论（一句话）

> **默认结论：不迁移。** 唯一支撑迁移的理由是许可证，而在「不对外分发」
> 的前提下许可证不构成问题；迁移要付出 1–2 天 + 140 个测试文件的回归，
> 换来一个当前用不上的收益。
>
> 本文保留，作为**将来真要迁移时的证据与清单**。下面 §1 是核实过的判据，
> §4.1 是唯一可能推翻结论的未验证项（LGPL 与 onefile）。

---

## 1. 为什么评估 / 为什么默认不迁

项目直接依赖 PyQt6。PyQt6 是 Riverbank 的 **GPLv3 / 商业双许可**，
不是 LGPL——Qt 的 LGPL 那套只给 PySide6 用。

> 这个结论已经落到仓库里了：本项目按 **GPL-3.0-only** 发布（`LICENSE`），
> 协议全文与第三方归属在 [`LICENSES/`](../LICENSES/) 与
> [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)，README 的「许可」一节
> 是使用者视角的同一件事。**真要换成宽松协议，先读那份声明**，别凭本文的印象动手。

### 1.1 官方判据（已核实）

Riverbank [License FAQ](https://www.riverbankcomputing.com/commercial/license-faq) 第一条原话：

> **Do I need a commercial PyQt license?**
> It depends entirely on **what license you are going to use to distribute your
> application**. If your license is incompatible with the GPL then you need a
> commercial PyQt license.

判据是**用什么协议分发**，不是「有没有用」。

> ⚠️ 网上流传「Riverbank FAQ 明确要求内部部署也要买商业授权」的说法，
> **官方 FAQ 里没有这句话**。以官方文本为准，不要采信二手转述。

### 1.2 「不对外分发」的边界比想象的窄

GPLv3 的触发点是 **convey**（§0：任何让其他方能获得副本的传播行为），
不是「使用」。真正安全的只有：

| 场景 | 触发 GPL 义务 |
|---|---|
| 只在自己机器上跑 | 否 |
| 私有仓库，不 push 到任何他人可访问处 | 否 |
| 把安装包/构建产物拷给同事 | **是** — 组织内部传递也算 convey |
| 自建 Gitea，仓库**私有** | 否 |
| 自建 Gitea，仓库**公开** | **是** — 公开可获取即构成 convey，**无人下载也算** |
| 挂网盘 / 官网供下载 | **是** |

> **MavisSSH 的实际状态**：本项目以 **GPL-3.0-only** 授权公开，
> 仓库完整源码与 `LICENSE` 均随代码分发。仓库的托管位置不影响结论 ——
> 按上表，公开仓库即构成 convey，而公开时以 GPL-3.0 授权且源码齐全
> **已经合规**，不需要迁移，也不需要购买商业授权。
>
> 唯一仍需注意的是**二进制分发**：把安装包或可执行文件交给公司外的人，
> 同样构成 convey，必须随包提供完整对应源码（见
> [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)）。
>
> 差别只在于：公开的话 `LICENSE` 那份 GPL-3.0 文本是**必须**的（已随仓库分发）；
> 私有的话它只是无害的保险。
>
> **本项目已按「公开」这一行处理**：仓库以 GPL-3.0-only 授权、源码齐全、
> `LICENSE` 与 `THIRD_PARTY_NOTICES.md` 随包分发。

### 1.3 什么情况下这个结论会翻转

只有一种：**将来要对外分发闭源版本**（给客户交付、公开下载、卖）。
那时才需要 PySide6 或 Riverbank 商业授权 二选一 —— 而那时再迁也来得及，
`docs/PYSIDE6_MIGRATION.md` 就是那时候的施工图。

---

## 2. 已验证：这些不是风险

| 项 | 实测结果 |
|---|---|
| `QAction` | 代码里已是 `QtGui.QAction`（Qt6 写法），**没有** `QtWidgets.QAction` 那个经典断裂 |
| 枚举作用域 | 代码已是全限定式（`Qt.FocusPolicy.StrongFocus`），PySide6 同样接受 |
| `pytest-qt` / `qtbot` | **完全没用**，测试都是独立脚本 —— 省掉整块 fixture 重写 |
| `QVariant` / `QString` | 零命中 |
| `exec_()` | `tests/_fwin.py:306` 是本地 mock 函数名，与绑定无关 |
| **真实模块加载** | `sys.modules` 重定向后，**26/26 生产模块在 PySide6 下加载成功** |
| **槽内抛异常** | 两边**都不 fast-fail**，异常均走 `sys.excepthook`，进程存活、退出码 0 |

### 2.1 关于 `sip.isdeleted()`（原判为高危，实测通过）

生产代码 5 处 use-after-free 守卫，用 `sip.isdeleted()` 判「C++ 对象是否已死」。
PySide6 对应物是 `shiboken6.isValid()`。探针在 11 个生命周期场景上逐行对比：

| 场景 | PyQt6/sip | PySide6/shiboken |
|---|---|---|
| F1 新建对象存活 | 存活 | 存活 |
| A1 子控件存活 | 存活 | 存活 |
| A2 父析构带走子控件 | 已死 | 已死 |
| K1 C++ 所有权移交后存活 | 存活 | 存活 |
| K2 移交后父析构 | 已死 | 已死 |
| K3 纯 Python 持有存活 | 存活 | 存活 |
| B1 `deleteLater()` 后立刻查 | **存活** | **存活** |
| B2 deferred flush 后 | 已死 | 已死 |
| C1 Python 强引用不阻止销毁 | 已死 | 已死 |
| C2 释放 Python 引用后 | 已死 | 已死 |
| D1 QTimer 销毁 | 已死 | 已死 |

**11/11 一致。** 守卫依赖的行为在两个绑定下相同。

**唯一差异**（下面 §4.1 会再提一次）：

| 输入 | `sip.isdeleted` | `shiboken6.isValid` |
|---|---|---|
| `None` | 抛 `TypeError` | 返回 `False` |
| 非 Qt 对象（如 `'x'`） | 抛 `TypeError` | 返回 `True`（取反后=已死） |

生产代码那 5 处目前**恰好**都靠短路保护挡住了 `None`：

```python
if panel is None or self._dead() or sip.isdeleted(panel):   # 先判 None
if w is None or sip.isdeleted(w):                            # 先判 None
```

迁移时这个短路**必须保留**，否则 `None` 会从「抛异常」变成「静默判死」。

### 2.2 一条已经过期的仓库注释

`tests/_excepthook_probe.py:3` 写着：

> In PyQt6 an exception escaping a slot calls qFatal() and the process dies

**实测不成立**（PyQt6 6.x + Python 3.14）：异常走 `sys.excepthook`，进程存活。
信号槽 / `QTest.mouseClick` / `QTimer.timeout` 三条路径，PyQt6 与 PySide6
**行为完全一致**。

这条注释依赖的是 PyQt6 早期行为。它同时把一个风险消掉了 —— 但注释本身
建议顺手改正，否则下次有人照着它做判据会踩空。

---

## 3. 将来真要迁时的必做项

> 以下**现在不用做**。默认不迁（§0），触发条件见 §1.3。

### 3.1 包名与信号/槽改名（机械，可脚本化）

| 模式 | 出现次数 | 改成 |
|---|---|---|
| `from PyQt6 import ...` | 140 处 / 95 文件 | `from PySide6 import ...` |
| `pyqtSignal` | 31 处 | `Signal` |
| `pyqtSlot` | 8 处 | `Slot` |
| `pyqtProperty` | 0 | `Property`（无命中） |

拆开看：生产代码 **31 处 / 37 文件**，测试代码 **105 处 / 85 文件**。
计数**不含**本文配套的三个迁移探针（它们本身就在比对两个绑定，不算待迁移代码）。

`pyqtSignal`/`pyqtSlot` 集中在 8 个生产文件：
`main_window.py`(6) `sftp_panel.py`(10) `tunnel_panel.py`(3)
`transfer_controller.py`(3) `sidebar.py`(2) `terminal_widget.py`(2)
`transfer_strip.py`(2) `monitor_panel.py`(1)

依赖文件（改一处即可，`pip install PySide6` 后由 pip 处理）：
`requirements.txt`、`requirements-build.txt`、`installer/mavisssh.spec`
（`hiddenimports` 收集的是 paramiko/cryptography，不含 Qt，但 `excludes`
可能要加 `PyQt6`）、`installer/make_icons.py`。

### 3.2 `sip` → `shiboken6`（5 处生产 + 8 个测试文件）

**生产代码（5 处）：**

| 位置 | 用途 |
|---|---|
| `sshclient/ui/sftp_panel.py:22` | `from PyQt6 import ..., sip` |
| `sshclient/ui/sftp_panel.py:1559` | `sip.isdeleted(self._ctrl)` |
| `sshclient/ui/sftp_panel.py:2002` | `sip.isdeleted(self)`（`_dead()`） |
| `sshclient/ui/sftp_panel.py:2007` | `sip.isdeleted(panel)` |
| `sshclient/ui/snippet_panel.py:563-570` | 延迟 import，退出期存活守卫 |

**改成 `not shiboken6.isValid(o)`。注意极性是反的** —— `isValid` 返回
`True = 还活着`，直接当 `isdeleted` 用会让整个判据颠倒。

建议抽一个共用判定函数，而不是在 5 处各写一遍取反 —— 否则将来再有一个绑定
差异要逐处排查。同时它天然是短路保护 `None` 的地方。

**测试代码（8 个文件，26 处 `sip.isdeleted` 判据）：**

`_accent.py` `_comptest.py` `_fconn.py` `_fwin.py` `_lrzprobe.py`
`_snippets.py` `_soak.py` `_tabclose.py` `_tabwire.py`

这些是**判据的判据本身**（验证 C++ 对象真的被销毁），换绑定后判据基础变了，
必须逐条重验，不能只做文本替换。

### 3.3 有一条判据会直接变红

```python
# tests/_readme.py:223
regex("分组头可点：它发 toggled 信号",
      r"toggled\s*=\s*QtCore\.pyqtSignal\(str\)", SIDEBAR_SRC)
```

它拿**正则去 grep 源码文本**。改名后立刻红 —— 不是产品坏了，是判据过期。
同类问题只此一处，但说明了一件事：**判据可以依赖源码的字面写法**，
迁移前应该把所有 grep 源码的判据找出来。

### 3.4 依赖与构建

- `requirements.txt`：`PyQt6>=6.6` → `PySide6>=6.9`
  （实测 PySide6 **6.12.0** 有 cp314 wheel，Python 3.14.5 无阻碍）
- `requirements-build.txt` 注释里写着「PyInstaller 6.0 起对 **PyQt6** 的 hook
  更完整（自动收 Qt 插件目录）」—— 换绑定后要重验 hook 与 `shiboken6` 收集
- `installer/mavisssh.spec`：`excludes` 建议加 `PyQt6`

---

## 4. 未验证 / 需要单独决策

### 4.1 LGPL 动态链接与打包形态的冲突 ⚠️（**仅在未来迁到 PySide6 时才成立**）

**这条 spike 没测。它今天不影响任何决定** —— 因为默认不迁（§0），
而 PyQt6 是 GPL，onefile 打包不受 LGPL 约束。

它是**将来真要迁**时的第一个必答题：
LGPL-3.0 §4(d) 要求「以分发目标代码的方式分发…同时给出替换该库的手段」。
而刚做好的 `installer/mavisssh.nsi` 产出的是 **onefile 安装包**
（`dist\MavisSSH-0.2.0-Setup.exe`），把 Qt 的 DLL 全部塞进 EXE。
onefile 与 LGPL 合规性的关系是有争议的。

Qt 官方为此专门提供了 `pyside6-deploy` 工具链。

**要做的事**：确认 onefile + PySide6 的合规姿态（是否需要改成 onedir + 提供
替换说明/源码获取途径），再决定安装向导形态是否要跟着改。
**这一步要在写代码之前定** —— 它会影响打包方案和 LICENSE 内容。

### 4.2 全量测试回归

生产 37 文件 / 17,445 行；测试 **140 文件 / 34,768 行**（约 2 倍）。
`PyQt6` 导入散布在 95 个文件里，回归面积远大于改动面积。

已知的敏感点：

- `tests/_e2e.py` 偶发崩在打印完 `ALL CHECKS PASSED` **之后**
  （析构期，见 `snippet_panel.py:558` 注释）
- `tests/_soak.py` 的内存探针结论依赖 GC 时机，可能随绑定变化
- `terminal_widget.py:228` 记着一条针对 sip wrapper 属性查找顺序的规避
  （pyte 的 `__getattribute__` 代理会遮蔽信号），**shiboken 下是否仍成立需重验**

### 4.3 打包产物重验

onefile / onedir / NSIS 安装向导三样产物都要在 PySide6 下重新走一遍
`installer/verify.py` 与 `installer/verify_runtime.py`。

---

## 5. 执行顺序建议

```
0. 定 LGPL 合规姿态（§4.1）          ← 不定，后面都可能白做
1. 建迁移分支，保留 PyQt6 可回退
2. 批量改名：包名 + pyqtSignal/Slot    ← 可脚本，先跑 _readme.py 全绿
3. 生产代码 sip → shiboken6            ← 抽共用函数，守住 None 短路
4. 全量测试回归                        ← 大头，8 个 sip 判据文件重点看
5. 修正过期注释（_excepthook_probe.py）
6. 改 LICENSE（GPL-3.0 → 依 §4.1 结论）
7. 重验打包三产物 + 安装向导
8. 更新 README / AGENTS.md 中的 PyQt6 引用
```

---

## 6. 怎么复现本文的数据

两个 venv 各跑一遍，输出 diff：

```powershell
# 旁路 venv，不动主环境
python -m venv .venv-pyside
.\.venv-pyside\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple PySide6 paramiko pyte cryptography

$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe        tests\_probe_pyside_migration.py > pyqt6.txt
.\.venv-pyside\Scripts\python.exe tests\_probe_pyside_migration.py > pyside6.txt
Compare-Object (Get-Content pyqt6.txt) (Get-Content pyside6.txt)

# 真实模块能否加载
.\.venv-pyside\Scripts\python.exe tests\_probe_real_modules.py

# 槽内抛异常的行为
.\.venv\Scripts\python.exe        tests\_probe_pyside_excepthook.py
.\.venv-pyside\Scripts\python.exe tests\_probe_pyside_excepthook.py
```

探针与 `.venv-pyside/` 均已在 `.gitignore` 中。

---

## 7. 踩过的坑（留给下一个做迁移的人）

写这次探针时踩的坑，有三个**都不是 Qt 的问题，是「探针看起来在跑、实际什么都没测」**：

0. **写文档时重数一遍，不要沿用中途的数字。**
   本文初稿写「139 处 / 94 文件」，定稿前重数是「140 处 / 95 文件」——
   差的那 4 处正是**本次新写的探针自己**（它们里也有 `from PyQt6` 字样）。
   一份迁移清单如果带着错数字交付，后面照着它排期就会低估工作量。
   口径也必须写明（见 §3.1）：计不包含探针？

1. **`QApplication.processEvents()` 不处理 deferred delete。**
   DeferredDelete 只在「回到与调用 `deleteLater()` 相同的事件循环层级」时投递。
   不用 `sendPostedEvents(None, QEvent.Type.DeferredDelete)` 的话，
   三个销毁场景全都会误报「还活着」—— 探针测的是一个常量。

2. **自定义 `sys.excepthook` 别吞 stderr。**
   第一版 hook 只记账然后 `return None`，结果脚本「零输出 + 退出码 1」，
   看起来像「Qt 把进程干掉了」，其实是我们自己堵死了唯一的错误通道。
   探针不报告失败，会让人得出「这里会崩」的错误结论。

3. **鼠标按钮枚举在 `Qt` 上，不在 `QTest` 上。**
   `QtTest.QTest.MouseButton` 是 `AttributeError`，而 `PyQt6` 里
   `QTest.MouseButton` 看着就像该在那儿。两边都是 `Qt.MouseButton.LeftButton`。

以及一条**判定本身的**坑：

4. **`shiboken6.isValid()` 的极性与 `sip.isdeleted()` 相反。**
   第一版探针直接赋值，把判据整个颠倒：表现是「新建的 widget 判成已死」
   「`None` 判成存活」。这种错误一眼就不对，但**如果只看某一行**，
   很容易被读成「shiboken 更早发现销毁」—— 一个听起来很合理、
   实则完全错误的结论。

---

## 8. 决策记录

| 决定 | 状态 |
|---|---|
| **是否迁移到 PySide6** | **否**（默认不迁，理由见 §1） |
| 触发重新评估的条件 | 将来要对外分发闭源版本（§1.3） |
| `LICENSE` 保持 GPL-3.0 | 已定。私有仓库时是无害保险，公开仓库时是必需 |
| NSIS 安装向导仍用 onefile | 已定。onefile 的 LGPL 顾虑只在**迁到 PySide6 后**才成立 |
| 若将来迁，LICENSE 改成什么 | 待 §4.1 结论 |
| 若将来迁，安装向导是否仍用 onefile | 待 §4.1 结论 |
