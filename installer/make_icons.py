"""从 ``sshclient.ui.logo`` 导出打包要用的图标文件。

**Logo 的唯一真相是 :func:`sshclient.ui.logo.paint_logo`**，这里的 PNG / ICO /
ICNS 全部由它渲染出来。刻意不另存一份 SVG：两份真相必然漂，而图标漂了
没人看得出来——等到应用图标和标题栏对不上时，已经不知道该信哪个了。

用法（仓库根目录）::

    python installer/make_icons.py            # 全部平台都生成
    python installer/make_icons.py --ico      # 只出 Windows 的 .ico

产物落在 ``installer/icons/``。这些是**构建产物**，随时可以重新生成，所以
不进版本库（见 .gitignore）。

> 目录**不能**叫 ``packaging/``：PyPI 上有个同名的常用库，paramiko 和
> PyInstaller 都 import 它。同名目录会让 ``import packaging`` 解析到本仓库
> 的文件，把真库整个遮蔽掉——踩过一次。
"""
from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt6 import QtCore, QtGui, QtWidgets

# **QApplication 必须在 import 任何 sshclient.ui 的东西之前建好。**
# `logo_pixmap()` 里要造 QPixmap，而 QPixmap 早于 QApplication 会被 Qt 拒绝
# （"Must construct a QGuiApplication before a QPixmap"），随后整个进程
# 0xC0000409 硬崩、连 flush 都不做——看起来像"没报错的死掉"，很难查。
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from sshclient.core.palette import get_palette
from sshclient.ui import logo

OUT = ROOT / "installer" / "icons"

# 各平台真正会用到的尺寸。ICO 想要的那几档是 Windows 任务栏/DPI 决定的，
# 不是"看着顺"就随手加。
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
PNG_SIZES = [16, 24, 32, 48, 64, 128, 256, 512]
ICNS_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _render(n: int, pal) -> QtGui.QPixmap:
    return logo.logo_pixmap(n, top=pal.accent_hover, bottom=pal.accent)


def write_pngs(pal) -> list[pathlib.Path]:
    made = []
    for n in PNG_SIZES:
        path = OUT / ("mavisssh-%d.png" % n)
        _render(n, pal).save(str(path), "PNG")
        made.append(path)
    # 一张 1024 的母版，给设计侧改图用
    big = OUT / "mavisssh-1024.png"
    _render(1024, pal).save(str(big), "PNG")
    made.append(big)
    return made


def write_ico(pal) -> pathlib.Path:
    """.ico 里塞多档尺寸。

    QPixmap.save 支持 ICO，但**只会写一档**——所以这里手工拼 ICO 容器
    （6 字节头 + 每档 16 字节目录项 + 每档一张 PNG）。Windows 会按需要挑档，
    缩放才不会糊。
    """
    import struct
    blobs = []
    for n in ICO_SIZES:
        pm = _render(n, pal)
        data = QtCore.QBuffer()
        data.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        pm.save(data, "PNG")
        blobs.append((n, bytes(data.data())))
        data.close()

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = 6 + 16 * len(blobs)
    entries, payload = b"", b""
    for n, blob in blobs:
        side = 0 if n >= 256 else n          # 256 在 ICO 目录里记 0
        entries += struct.pack("<BBBBHHII", side, side, 0, 0, 1, 32,
                               len(blob), offset)
        offset += len(blob)
        payload += blob
    path = OUT / "mavisssh.ico"
    path.write_bytes(header + entries + payload)
    return path


def write_icns(pal) -> pathlib.Path:
    """.icns（macOS）。

    Qt 的 ICO 写入器不产 ICNS，而 ICNS 的格式是"magic + 长度 + 类型块"，
    每块一个 PNG。类型码是尺寸的固定映射，见 Apple 的 sIconTypes。
    """
    import struct
    TYPES = {16: "icp4", 32: "icp5", 64: "icp8", 128: "ic07", 256: "ic08",
             512: "ic09", 1024: "ic10"}
    blocks = []
    for n in ICNS_SIZES:
        t = TYPES.get(n)
        if not t:
            continue
        data = QtCore.QBuffer()
        data.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        _render(n, pal).save(data, "PNG")
        blob = bytes(data.data())
        data.close()
        blocks.append(t.encode("ascii") + struct.pack(">I", len(blob) + 8) + blob)
    body = b"".join(blocks)
    path = OUT / "mavisssh.icns"
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ico", action="store_true", help="只出 Windows .ico")
    ap.add_argument("--icns", action="store_true", help="只出 macOS .icns")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    pal = get_palette("dark")

    made = []
    if args.ico:
        made.append(write_ico(pal))
    elif args.icns:
        made.append(write_icns(pal))
    else:
        made.extend(write_pngs(pal))
        made.append(write_ico(pal))
        made.append(write_icns(pal))

    for p in made:
        print("  %-34s %8d 字节" % (p.relative_to(ROOT), p.stat().st_size))
    return 0


if __name__ == "__main__":
    sys.exit(main())
