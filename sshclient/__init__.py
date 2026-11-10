"""Top-level package for the SSH client.

Copyright (C) 2026  Albert D

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

**许可证为什么是 GPL-3.0 而不是 MIT。** 本项目直接依赖 PyQt6，而 PyQt6
Riverbank 的授权是 **GPLv3 / 商业双许可**，不是 LGPL —— Qt 的 LGPL 那套只
给 PySide6 和 Qt for Python 用。所以用 MIT 分发会与 PyQt6 的条款直接冲突。
想改成宽松许可证只有两条路：换成 LGPL 的 PySide6，或买 Riverbank 商业授权。
"""

#: **版本号的唯一真相。** 原来这里写 0.1.0，而 `main.py` 的
#: `setApplicationVersion` 和关于框各写一份 0.2.0 —— 三处两个值，
#: 打包发版时改一处就漏两处，用户在「关于」里看到的版本与实际构建出来的
#: 包对不上。**其余地方一律从这里读，不要再写第二份字面量。**
__version__ = "0.2.0"
__app_name__ = "MavisSSH"
