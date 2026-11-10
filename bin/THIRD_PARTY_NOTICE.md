# bin/ 里的第三方预编译程序

这三个文件**不是本项目编译的**，是第三方发布版，原样附在仓库里，方便
Windows 用户开箱即用地做 ZMODEM 传输。它们的作者与著作权属于各自上游，
本项目只负责分发。

| 文件 | 上游 | 版本 | 许可 |
|---|---|---|---|
| `rz.exe` | [trzsz/lrzsz-win32](https://github.com/trzsz/lrzsz-win32)（上游项目 lrzsz，<http://www.ohse.de/uwe/software/lrzsz.html>） | 0.12.21rc | GPL-2.0-or-later |
| `sz.exe` | 同上 | 0.12.21rc | GPL-2.0-or-later |
| `msys-2.0.dll` | MSYS2 的 `msys2-runtime`（Cygwin POSIX 模拟层，<https://www.cygwin.com/>） | 随 lrzsz-win32 发布版 | GPL-3.0-or-later |

协议全文：[`../LICENSES/GPL-2.0.txt`](../LICENSES/GPL-2.0.txt)、
[`../LICENSES/GPL-3.0.txt`](../LICENSES/GPL-3.0.txt)。整体归属声明见
[`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md)。

## 校验值（本次随包的具体文件）

```
e3536bfd5aa5b39f097f924130878085  msys-2.0.dll
1fe86e319a4dac97ee017060fac2c5bb  rz.exe
24a9b2ee59febe56eab1a84713c3fb97  sz.exe
```

升级到新版本时请一并更新本文件。

## 为什么可以这样分发

lrzsz 的源码头写的是「either version 2, or (at your option) any later
version」，即 **GPL-2.0-or-later**；`msys-2.0.dll` 是 GPL-3.0-or-later。
两者都允许以 GPL-3.0 分发，与 MavisSSH 自身的 GPL-3.0-only 同向，没有
「GPLv2-only 与 GPLv3 不兼容」那类问题。

## 分发时要一起做的事

随二进制分发这三个文件时，需要：

1. 附上 GPL-2.0 与 GPL-3.0 全文（`LICENSES/` 里已备好）；
2. 保留 lrzsz 与 Cygwin / MSYS2 的著作权声明；
3. 提供这些二进制的**对应源码**，或给出明确的获取途径：
   - lrzsz：<http://www.ohse.de/uwe/software/lrzsz.html>
   - lrzsz-win32（Windows 预编译）：<https://github.com/trzsz/lrzsz-win32>
   - MSYS2 / msys2-runtime：<https://www.msys2.org/>（源码随 MSYS2 仓库提供）

想彻底免除这一摊义务，就**别附带这三个文件**：删掉 `bin/` 后程序仍可用，
Windows 上会改从 `PATH` 找 `rz` / `sz`（例如用 MSYS2 或
[scoop install lrzsz](https://github.com/ScoopInstaller/Extras#lrzsz)
自行安装），找不到时面板会明确告诉用户缺哪个二进制。
macOS / Linux 本来就走系统包管理器，不受本文件影响。