# 第三方组件与许可声明

本文件是 [README.md](README.md)「许可」一节的展开，供**分发时随包附带**。
协议全文都在 [`LICENSES/`](LICENSES/)。

> 本文记录的是**事实与条款义务**，不是法律意见。要把本项目用于闭源产品，
> 请让你的法务确认一遍，尤其是 PyQt6 那一条。

## 一、本项目自身

| 项 | 值 |
|---|---|
| 名称 | MavisSSH |
| 版权 | Copyright © 2026 Albert D |
| 许可 | **GPL-3.0-only**（全文：[`LICENSE`](LICENSE)，副本：[`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt)） |

**为什么不是 MIT / Apache-2.0。** 本项目直接依赖 **PyQt6**，其 Python 绑定的
元数据写的是 `License-Expression: GPL-3.0-only`（见 `.venv` 里
`pyqt6-*.dist-info/METADATA`），并另有一条 Riverbank 商业授权路线。
Qt 那套宽松的 LGPL 条款**只给 PySide6 / Qt for Python**，PyQt6 不在其列。
所以用 MIT 分发会与 PyQt6 的条款直接冲突 —— 换宽松协议只有两条路：

1. 买 Riverbank 商业授权（<https://www.riverbankcomputing.com/commercial/license>）；
2. 换成 LGPL 的 PySide6 —— 评估过程见
   [`docs/PYSIDE6_MIGRATION.md`](docs/PYSIDE6_MIGRATION.md)（当前结论：不迁移）。

**GPL 没有「内部使用豁免」。** 公司内部使用、不对外分发，不触发源码开放义务；
但只要把编译产物**交给公司外的人**（客户、合作方、外包团队、公开下载），
就要按 GPL-3.0 提供完整对应源码。这条是最容易被误判的地方。

## 二、运行时依赖

各组件的许可均取自**本机实际安装的发行包元数据**，不是抄上游 README。

| 组件 | 版本 | 许可 | 全文 |
|---|---|---|---|
| PyQt6 | 6.11.0 | **GPL-3.0-only** 或 Riverbank 商业授权 | [`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt) |
| PyQt6-Qt6（Qt 运行时） | 6.11.1 | LGPL-3.0（部分模块 GPL-3.0） | [`LICENSES/LGPL-3.0.txt`](LICENSES/LGPL-3.0.txt) |
| paramiko | 5.0.0 | LGPL-2.1 | [`LICENSES/LGPL-2.1.txt`](LICENSES/LGPL-2.1.txt) |
| pyte | 0.8.2 | LGPL-3.0 | [`LICENSES/LGPL-3.0.txt`](LICENSES/LGPL-3.0.txt) |
| cryptography | 50.0.0 | Apache-2.0 **或** BSD-3-Clause | [`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt)、[`LICENSES/BSD-3-Clause.txt`](LICENSES/BSD-3-Clause.txt) |
| cffi / pycparser（cryptography 的传递依赖） | — | MIT / BSD-3-Clause | 随各自发行包提供 |

LGPL 组件在 Python 里是**以库的形式被导入使用**，未被静态链接进本项目的
可执行文件代码里；打包（PyInstaller）只是把它们放进产物，调用方向仍然是
「本项目 → 库」，用户可以用替换库文件的方式升级它们。

## 三、随仓库附带的二进制（`bin/`）

`bin/` 下这三个文件**不是本项目编译的**，是第三方预编译产物：

| 文件 | 来源 | 许可 |
|---|---|---|
| `bin/rz.exe`、`bin/sz.exe` | [trzsz/lrzsz-win32](https://github.com/trzsz/lrzsz-win32) 发布版（用 MSYS2 编译的 lrzsz 0.12.x） | GPL-2.0-or-later（lrzsz 源码头写的是「version 2, or (at your option) any later version」） |
| `bin/msys-2.0.dll` | MSYS2 的 `msys2-runtime`（Cygwin POSIX 模拟层） | GPL-3.0-or-later |

两个都是 GPL 系，与本项目的 GPL-3.0 **方向兼容**（「or later」条款允许以
GPLv3 分发）。若要替换成自己编译的版本，义务不变。细节与移除方式见
[`bin/THIRD_PARTY_NOTICE.md`](bin/THIRD_PARTY_NOTICE.md)。

> macOS / Linux 上这三个文件不会被使用（解析器只认 `.exe`），程序改从 `PATH`
> 找系统包管理器装的 `lrzsz`。

## 四、构建期依赖（不进产物）

| 组件 | 许可 | 说明 |
|---|---|---|
| PyInstaller | GPL-2.0-or-later **附带例外条款** | 该例外明确允许用 PyInstaller 打包并分发非自由甚至商业程序。它只在构建机上运行，不进入产物 |
| ruff | MIT | 仅开发期 |

## 五、分发时必须做的事

把 MavisSSH 的二进制发给任何人（包括内部给同事、放进公司镜像、做安装包）
时，以下几件是协议要求的，缺一件就可能构成违约：

1. **保留版权与许可声明** —— 保留源码里的 `Copyright (C) 2026 Albert D` 头，
   并在安装目录里放本项目 `LICENSE` 与本文件；
2. **提供 GPL-3.0 全文** —— [`LICENSE`](LICENSE) / `LICENSES/GPL-3.0.txt`；
   附带的 GPL-2.0 / LGPL 组件对应全文见 `LICENSES/`；
3. **提供完整对应源码** —— 最省事的方式是分发时一并给出本仓库的完整源码
   （`git archive` 出的 tarball 即可）。若走二进制下载渠道，则按 GPL-3.0
   第 6(d) 条在二进制旁边给出获取源码的书面说明。
   **在 GitHub 上发布时**：每个 Release 附上
   `https://github.com/AlbertD-byte/MavisSSH/archive/refs/tags/v<版本>.tar.gz`
   这个源码包，并在 Release 说明里写明它就是该二进制的对应源码 ——
   这一句是 GPL-3.0 第 6(d) 条要求的「书面获取途径」；
4. **不得加额外限制** —— 不得对源码再加专利条款、不得要求额外交许、不得改名换姓。
5. **不得声称官方背书** —— 第三方组件的名字只用于归属声明。

已经这么做了：

- 安装向导在**许可页**展示 `LICENSE`（`installer/mavisssh.nsi` 的
  `MUI_PAGE_LICENSE`），用户必须先同意才继续；
- `LICENSES/` 与本文件随仓库分发。

## 六、改协议时怎么办

本文件的每一条都指向一个具体依据（安装包元数据、上游仓库、官方条款页）。
**改依赖版本就要复核本文件** —— 尤其是 PyQt6 的许可表达式，它是 GPLv3
这一选择的唯一硬约束。如果哪天 PyQt6 的免费版换了许可（例如随 Qt 6.9+
提供了 LGPL 选项），先读官方说明，再决定是否换协议，不要凭记忆改。