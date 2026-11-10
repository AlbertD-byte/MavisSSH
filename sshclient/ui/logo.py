"""MavisSSH 的 Logo。

**这个模块是 Logo 的唯一真相。** `packaging/make_icons.py` 里的 PNG / ICO /
ICNS 全部由 :func:`paint_logo` 导出来，不是另画的——两份真相必然漂，而图标
漂了没人看得出来。

形状：一个圆角方块底 + 里面一个 ``>_`` 提示符。下划线那一笔**从尖角的折线
里接出来**，是同一个手势画完的，所以它同时读成"命令提示符"和"往右连出去的一
条线"——这正是这个程序干的事。

两档画法，用同一个函数：

- ``plate=True``：整块底 + 挖空字形，给应用图标、任务栏、关于框用
- ``plate=False``：只画字形，单色，给标题栏那个 16px 的小标记用

**原来还有第三档（圆角描边框 + 字形），已经删掉。**
它全仓零调用点 —— 包括 `installer/make_icons.py` —— 而 docstring 给它写的
用途「嵌在浅色背景上的场合」在这个仓里**根本不存在**：浅色主题下标题栏那个
小标记走的是第二档配 `glyph=palette.accent`。
留着一支没人调、也没人说得清什么时候该用的画法，比删掉它更糟：下一个
人看到它会以为这是一个可选的现成方案。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui

from . import icons as _icons  # noqa: F401  —— 保持与图标系统同一套 import 风格


def _gradient(p: QtGui.QPainter, box: QtCore.QRectF, top: str, bottom: str) -> None:
    grad = QtGui.QLinearGradient(box.topLeft(), box.bottomLeft())
    grad.setColorAt(0.0, QtGui.QColor(top))
    grad.setColorAt(1.0, QtGui.QColor(bottom))
    p.fillRect(box, grad)


def paint_logo(p: QtGui.QPainter, size: int, *,
               plate: bool = True,
               top: str = "#6ba1ff", bottom: str = "#2f6fe0",
               glyph: str = "#ffffff") -> None:
    """把 Logo 画进以 (0, 0)-(size, size) 为坐标的区域。

    p 的当前变换会被使用，函数**不**改动 pen/brush 之外的其它状态。
    """
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    s = float(size)
    box = QtCore.QRectF(0, 0, s, s)

    if plate is True:
        _gradient(p, box, top, bottom)
        pen_color = glyph
    else:
        pen_color = glyph

    # --- 字形：>_ -------------------------------------------------------
    # **尖角和下划线必须分开。** 早先把下划线从尖角那个折点接出去，想让它
    # "一笔画完、顺便读成往右连出去的一条线"——结果三段连成一个粗折线，
    # 128px 下读出来是对勾，不是提示符。`>_` 的可辨识度全在那个**缺口**上。
    p.setPen(QtGui.QPen(QtGui.QColor(pen_color),
                        max(1.0, s * 0.082),          # 线宽
                        QtCore.Qt.PenStyle.SolidLine,
                        QtCore.Qt.PenCapStyle.RoundCap,
                        QtCore.Qt.PenJoinStyle.RoundJoin))

    # 尖角：左上 → 右中 → 左下，尖端朝右。纵向略高于中心，给下划线腾出下面一行。
    left = s * 0.295
    apex_x = s * 0.475
    upper_y = s * 0.285
    mid_y = s * 0.455
    lower_y = s * 0.625

    path = QtGui.QPainterPath()
    path.moveTo(left, upper_y)
    path.lineTo(apex_x, mid_y)
    path.lineTo(left, lower_y)
    p.drawPath(path)

    # 下划线：尖角右边、同一基线上，中间留出缺口
    p.drawLine(QtCore.QPointF(s * 0.565, lower_y),
               QtCore.QPointF(s * 0.735, lower_y))

    p.restore()


def logo_pixmap(size: int, *, plate: bool = True,
                top: str = "#6ba1ff", bottom: str = "#2f6fe0",
                glyph: str = "#ffffff",
                dpr: float = 1.0) -> QtGui.QPixmap:
    """给标题栏、窗口图标用的现成图。带 devicePixelRatio，支持高分屏。"""
    pm = QtGui.QPixmap(int(size * dpr), int(size * dpr))
    pm.fill(QtCore.Qt.GlobalColor.transparent)
    p = QtGui.QPainter(pm)
    p.scale(dpr, dpr)
    paint_logo(p, size, plate=plate, top=top, bottom=bottom, glyph=glyph)
    p.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def window_icon(palette) -> QtGui.QIcon:
    """窗口 / 任务栏图标。浅色主题用浅一档的底色，深色主题用高亮那档。"""
    top = getattr(palette, "accent_hover", None) or palette.accent
    icon = QtGui.QIcon()
    for n in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(n, top=top, bottom=palette.accent))
    return icon
