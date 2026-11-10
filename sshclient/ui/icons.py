"""Vector icon set drawn with QPainterPath.

Keeping icons in-house means they inherit the theme color, stay crisp at any
DPI, and match the rest of the visual language instead of pulling in the
platform's icon theme.
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui

from .design.v1.palette import Palette

_CACHE: dict[tuple[str, str, int], QtGui.QIcon] = {}


# ---------------------------------------------------------------------------
# Path helpers. All icons are authored in a 24x24 box.


def _line(x1, y1, x2, y2):
    def fn(p: QtGui.QPainterPath) -> None:
        p.moveTo(x1, y1)
        p.lineTo(x2, y2)
    return fn


def _poly(*pts):
    def fn(p: QtGui.QPainterPath) -> None:
        p.moveTo(*pts[0])
        for pt in pts[1:]:
            p.lineTo(*pt)
        p.closeSubpath()
    return fn


def _ellipse(x, y, w, h):
    def fn(p: QtGui.QPainterPath) -> None:
        p.addEllipse(QtCore.QRectF(x, y, w, h))
    return fn


def _arc(x, y, w, h, start, sweep):
    def fn(p: QtGui.QPainterPath) -> None:
        p.moveTo(x, y)
        p.arcTo(QtCore.QRectF(x, y, w, h), start, sweep)
    return fn


def _rrect(x, y, w, h, r):
    def fn(p: QtGui.QPainterPath) -> None:
        p.addRoundedRect(QtCore.QRectF(x, y, w, h), r, r)
    return fn


# ---------------------------------------------------------------------------
# Icon definitions: fn() -> (stroked_path | None, filled_path | None)


def _terminal():
    return _p_stroked_only([
        _line(5, 7, 10, 12), _line(10, 12, 5, 17), _line(12, 18, 20, 18),
    ])


def _folder():
    return _p_stroked_only([
        _line(3, 20, 3, 6), _line(3, 6, 9.5, 6), _line(9.5, 6, 12, 9),
        _line(12, 9, 21, 9), _line(21, 9, 21, 20), _line(21, 20, 3, 20),
    ])


def _upload():
    return _p_stroked_only([
        _line(12, 16, 12, 4), _line(7, 9, 12, 4), _line(12, 4, 17, 9),
        _line(4, 20, 20, 20),
    ])


def _download():
    return _p_stroked_only([
        _line(12, 4, 12, 16), _line(7, 11, 12, 16), _line(12, 16, 17, 11),
        _line(4, 20, 20, 20),
    ])


def _chart():
    stroked = _combine([
        _line(3.5, 3.5, 3.5, 20.5),
        _line(3.5, 20.5, 20.5, 20.5),
    ])
    filled = _combine([
        _rrect(6.5, 12, 3.4, 8.5, 1.1),
        _rrect(11.6, 6, 3.4, 14.5, 1.1),
        _rrect(16.7, 14, 3.4, 6.5, 1.1),
    ])
    return stroked, filled


def _copy():
    return _p_stroked_only([
        _poly((9, 3), (20, 3), (20, 19), (18, 19)),
        _poly((15, 6), (4, 6), (4, 21), (15, 21), (15, 6)),
    ])


def _paste():
    return _p_stroked_only([
        _poly((6, 5), (18, 5), (18, 21), (6, 21), (6, 5)),
        _poly((9, 2), (15, 2), (15, 5), (9, 5), (9, 2)),
        _line(9, 11, 15, 11), _line(9, 15, 15, 15),
    ])


def _trash():
    return _p_stroked_only([
        _line(4, 7, 20, 7), _line(9, 7, 9, 4.5), _line(15, 7, 15, 4.5),
        _line(9, 4.5, 15, 4.5),
        _poly((6, 7), (7, 21), (17, 21), (18, 7)),
        _line(10, 11, 10, 17), _line(14, 11, 14, 17),
    ])


def _refresh():
    stroked = _combine([_arc(4, 4, 16, 16, 70, 250)])
    filled = _combine([_poly((19.5, 2.5), (20.5, 9), (14, 8))])
    return stroked, filled


def _plus():
    return _p_stroked_only([_line(12, 5, 12, 19), _line(5, 12, 19, 12)])


def _pencil():
    return _p_stroked_only([
        _poly((4, 20), (4, 16), (15.5, 4.5), (19.5, 8.5), (8, 20), (4, 20)),
        _line(13.5, 6.5, 17.5, 10.5),
    ])


def _power():
    return _p_stroked_only([
        _line(12, 3, 12, 11),
        _arc(6, 6, 12, 12, -55, 110),
    ])


def _plug():
    return _p_stroked_only([
        _line(9, 3, 9, 9), _line(15, 3, 15, 9),
        _poly((6, 9), (18, 9), (18, 12.5), (6, 12.5), (6, 9)),
        _line(12, 12.5, 12, 21),
    ])


def _up():
    return _p_stroked_only([_line(12, 20, 12, 5), _line(6, 11, 12, 5), _line(12, 5, 18, 11)])


#: 下拉框 / 步进器的箭头。**折线而不是实心三角**：本文件其余图标全是
#: 1.7px 描边语言，实心块在这个尺寸下会显得比旁边的文字重。
def _chevron_down():
    return _p_stroked_only([_poly((5, 9.5), (12, 16), (19, 9.5))])


def _chevron_up():
    return _p_stroked_only([_poly((5, 14.5), (12, 8), (19, 14.5))])


def _key():
    stroked = _combine([
        _ellipse(3, 9, 9, 9),
        _line(12, 13.5, 21, 13.5),
        _line(17.5, 13.5, 17.5, 17),
        _line(20.5, 13.5, 20.5, 16),
    ])
    return stroked, None


def _lock():
    return _p_stroked_only([
        _poly((5, 11), (19, 11), (19, 21), (5, 21), (5, 11)),
        _arc(7, 4, 10, 10, 0, 180),
    ])


def _user():
    filled = _combine([_ellipse(8, 4, 8, 8), _rrect(4, 14, 16, 8, 5)])
    return None, filled


def _server():
    stroked = _combine([
        _poly((3, 4), (21, 4), (21, 10), (3, 10), (3, 4)),
        _poly((3, 14), (21, 14), (21, 20), (3, 20), (3, 14)),
    ])
    filled = _combine([_ellipse(6, 6.2, 2, 2), _ellipse(6, 16.2, 2, 2)])
    return stroked, filled


def _globe():
    p = _combine([
        _ellipse(3, 3, 18, 18),
        _line(3, 12, 21, 12),
    ])
    # Single inner meridian -- two made it read as a solid disc.
    p.addEllipse(QtCore.QRectF(7, 3, 10, 18))
    return p, None


def _clock():
    stroked = _combine([_ellipse(3, 3, 18, 18)])
    filled = _combine([_line(11.2, 6.5, 11.2, 12.8), _line(11.2, 12.8, 16, 15)])
    return stroked, filled


def _close():
    return _p_stroked_only([_line(6, 6, 18, 18), _line(18, 6, 6, 18)])


def _check():
    return _p_stroked_only([_poly((5, 12.5), (10, 17.5), (19, 7))])


def _chevron_right():
    return _p_stroked_only([_poly((9, 5), (16, 12), (9, 19))])


def _search():
    p = _combine([_ellipse(4, 4, 12, 12), _line(14.5, 14.5, 20, 20)])
    return p, None


def _file():
    stroked = _combine([
        _poly((6, 3), (14, 3), (18, 7), (18, 21), (6, 21), (6, 3)),
        _poly((14, 3), (14, 7), (18, 7)),
    ])
    return stroked, None


def _cpu():
    stroked = _combine([
        _rrect(7, 7, 10, 10, 1.5),
        _line(10, 3, 10, 7), _line(14, 3, 14, 7),
        _line(10, 17, 10, 21), _line(14, 17, 14, 21),
        _line(3, 10, 7, 10), _line(3, 14, 7, 14),
        _line(17, 10, 21, 10), _line(17, 14, 21, 14),
    ])
    return stroked, None


def _memory():
    stroked = _combine([
        _rrect(3, 7, 18, 10, 1.5),
        _line(7, 17, 7, 21), _line(11, 17, 11, 21), _line(15, 17, 15, 21), _line(19, 17, 19, 21),
    ])
    filled = _combine([_rrect(6, 10, 12, 4, 1)])
    return stroked, filled


def _disk():
    stroked = _combine([
        _ellipse(12, 12, 20, 20),
        _ellipse(12, 12, 8, 8),
    ])
    return stroked, None


def _network():
    stroked = _combine([
        _rrect(3, 4, 18, 6, 1.5),
        _rrect(3, 14, 18, 6, 1.5),
        _line(12, 10, 12, 14),
    ])
    filled = _combine([_ellipse(6, 6, 1.8, 1.8), _ellipse(6, 16, 1.8, 1.8)])
    return stroked, filled


def _list():
    stroked = _combine([
        _line(9, 6, 20, 6), _line(9, 12, 20, 12), _line(9, 18, 20, 18),
    ])
    filled = _combine([_ellipse(4.5, 5, 1.8, 1.8), _ellipse(4.5, 11, 1.8, 1.8), _ellipse(4.5, 17, 1.8, 1.8)])
    return stroked, filled


def _snippet():
    """shell 提示符 + 光标：一条**还没执行**的命令。

    选它是因为这个功能的核心承诺就是"插进去、不回车"。`list` 说的是
    "一组条目"，`chevron-right` 说的是"下一级"，都不说"命令行"；这个图标
    画的就是终端上你正要敲的那一行的样子。
    """
    stroked = _combine([
        _poly((4, 5), (10, 12), (4, 19)),    # >
        _line(12, 19, 20, 19),                # 光标下划线
    ])
    return stroked, None


def _sun():
    stroked = _combine([_ellipse(5, 5, 14, 14)])
    for i in range(8):
        import math
        a = math.radians(i * 45)
        x1, y1 = 12 + 8.5 * math.cos(a), 12 + 8.5 * math.sin(a)
        x2, y2 = 12 + 11 * math.cos(a), 12 + 11 * math.sin(a)
        stroked.addPath(_combine([_line(x1, y1, x2, y2)]))
    return stroked, None


def _moon():
    # Crescent built from two explicit arcs rather than an odd-even fill,
    # which produced a solid disc at small sizes.
    p = QtGui.QPainterPath()
    p.moveTo(15.5, 4.2)
    p.arcTo(QtCore.QRectF(3.5, 3.5, 17, 17), 300, -240)   # outer edge
    p.closeSubpath()
    return p, None


def _link():
    stroked = _combine([
        _arc(3, 9, 9, 9, 90, 180),
        _arc(12, 9, 9, 9, 270, 180),
        _line(9, 13.5, 15, 13.5),
    ])
    return stroked, None


def _dots():
    filled = _combine([_ellipse(5, 11, 2, 2), _ellipse(11, 11, 2, 2), _ellipse(17, 11, 2, 2)])
    return None, filled


# ---------------------------------------------------------------------------
# 语义图标：名字要能替按钮说话
#
# 这一组是为「贴错语义」补的。原来清屏用的是 close（一个 ✕），而同一个工具栏
# 旁边就是关标签的 ✕ —— 用户会把「清屏」读成「关窗口」。新建目录用一个光秃秃
# 的 +, 上一级用一个光秃秃的 ↑（很多应用里那是「回顶部」）。加新图标比在
# 按钮上写小字便宜。


def _eraser():
    """清屏。用橡皮而不是 ✕：✕ 说的是"关掉"，橡皮说的是"擦掉"。"""
    return _p_stroked_only([
        _line(3, 20.5, 20, 20.5),                      # 底座
        _poly((6, 16), (13, 9), (19, 9), (19, 14), (11, 20), (6, 20), (6, 16)),
        _line(9.5, 12.5, 15.5, 12.5),                  # 橡皮中间那道分界
    ])


def _folder_plus():
    """新建目录。目录 + 加号，光秃秃的 + 说不清要建什么。"""
    return _p_stroked_only([
        _line(2.5, 19, 2.5, 6), _line(2.5, 6, 8.5, 6), _line(8.5, 6, 11, 8.5),
        _line(11, 8.5, 15, 8.5), _line(15, 8.5, 15, 14),
        _line(19.5, 11, 19.5, 18), _line(16, 14.5, 23, 14.5),
    ])


def _folder_up():
    """上一级。目录里一个朝上的箭头，别用光秃秃的 ↑（那是"回顶部"）。"""
    return _p_stroked_only([
        _line(3, 20, 3, 6), _line(3, 6, 9.5, 6), _line(9.5, 6, 12, 9),
        _line(12, 9, 21, 9), _line(21, 9, 21, 20), _line(21, 20, 3, 20),
        _line(12, 18, 12, 12.5), _line(9.5, 15, 12, 12.5), _line(12, 12.5, 14.5, 15),
    ])


def _ban():
    """已取消。圆圈加斜杠——"被禁止/没发生"，比 ✕ 更像状态而不是动作。"""
    stroked = _combine([
        _ellipse(3.5, 3.5, 17, 17),
        _line(7.5, 16.5, 16.5, 7.5),
    ])
    return stroked, None


def _gear():
    """设置。环 + 8 根短辐条，15px 下也还认得出是齿轮。"""
    stroked = _combine([_ellipse(3.5, 3.5, 17, 17), _ellipse(9.2, 9.2, 5.6, 5.6)])
    for i in range(8):
        import math
        a = math.radians(i * 45)
        stroked.addPath(_combine([
            _line(12 + 6.2 * math.cos(a), 12 + 6.2 * math.sin(a),
                  12 + 11 * math.cos(a), 12 + 11 * math.sin(a)),
        ]))
    return stroked, None


def _info():
    stroked = _combine([
        _ellipse(3.5, 3.5, 17, 17),
        _line(12, 11.8, 12, 16.5),     # "i" 的竖
    ])
    filled = _combine([_ellipse(11, 7, 2, 2)])   # "i" 的点
    return stroked, filled


def _sidebar():
    """折叠侧边栏。带一条左侧栏的面板，比一串圆点更像"侧边栏"。"""
    stroked = _combine([
        _rrect(3, 4.5, 18, 15, 2),
        _line(9.5, 4.5, 9.5, 19.5),
    ])
    filled = _combine([_rrect(4.6, 6.2, 3.8, 11.6, 1.2)])
    return stroked, filled


# --- helpers used above ------------------------------------------------------


def _combine(fns) -> QtGui.QPainterPath:
    """Merge several sub-path builders into one QPainterPath."""
    p = QtGui.QPainterPath()
    for fn in fns:
        fn(p)
    return p


def _p_stroked_only(fns) -> tuple[QtGui.QPainterPath, None]:
    return _combine(fns), None


ICONS = {
    "terminal": _terminal,
    "folder": _folder,
    "file": _file,
    "upload": _upload,
    "download": _download,
    "chart": _chart,
    "copy": _copy,
    "paste": _paste,
    "trash": _trash,
    "refresh": _refresh,
    "plus": _plus,
    "pencil": _pencil,
    "power": _power,
    "plug": _plug,
    "up": _up,
    "chevron_down": _chevron_down,
    "chevron_up": _chevron_up,
    "key": _key,
    "lock": _lock,
    "user": _user,
    "server": _server,
    "globe": _globe,
    "clock": _clock,
    "close": _close,
    "check": _check,
    "chevron-right": _chevron_right,
    "search": _search,
    "cpu": _cpu,
    "memory": _memory,
    "disk": _disk,
    "network": _network,
    "list": _list,
    "snippet": _snippet,
    "sun": _sun,
    "moon": _moon,
    "link": _link,
    "dots": _dots,
    "eraser": _eraser,
    "folder-plus": _folder_plus,
    "folder-up": _folder_up,
    "ban": _ban,
    "gear": _gear,
    "info": _info,
    "sidebar": _sidebar,
}


# ---------------------------------------------------------------------------
# Rendering


def icon_pixmap(name: str, color: str, size: int = 18, width: float = 1.7) -> QtGui.QPixmap:
    """Render *name* into a transparent pixmap tinted with *color*."""
    builder = ICONS.get(name)
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    if builder is None:
        return pm

    stroked, filled = builder()

    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

    scale = size / 24.0
    p.scale(scale, scale)

    pen = QtGui.QPen(QtGui.QColor(color))
    pen.setWidthF(width)
    pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(QtCore.Qt.PenJoinStyle.RoundJoin)

    if filled is not None:
        p.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
        p.setBrush(QtGui.QColor(color))
        p.drawPath(filled)

    if stroked is not None:
        p.setPen(pen)
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawPath(stroked)

    p.end()
    return pm


def icon(name: str, palette: Palette, size: int = 18, color: str | None = None,
         width: float = 1.7) -> QtGui.QIcon:
    """Return a themed QIcon, cached per every argument.

    **`width` 原来不在签名里，于是也不在缓存键里。** 今天所有调用都走默认
    1.7，所以看不出问题；但 `icon_pixmap` 是暴露 `width` 的，一旦有人从
    `icon()` 传一个非默认值，键就会撞上默认笔宽那条缓存，**拿到笔宽不对
    的图标，而且不报任何错**。与其在注释里写「小心别传」，不如让键
    真的覆盖 `icon_pixmap` 的每一个可变量 —— 加参数的人不必记得同步改键，
    因为改键就在他手边这一行。
    """
    key = (name, color or palette.text, size, width)
    cached = _CACHE.get(key)
    if cached is None:
        cached = QtGui.QIcon(icon_pixmap(name, color or palette.text, size))
        _CACHE[key] = cached
    return cached


def clear_cache() -> None:
    _CACHE.clear()
