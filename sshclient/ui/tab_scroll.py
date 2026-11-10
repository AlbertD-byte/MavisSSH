"""标签栏两端的翻页按钮：把原生的空白按钮画成 `<` 和 `>`。

## 现状：那两个按钮本来就是空的

标签一多，QTabBar 会在两端各留出一条 `QToolButton`（objectName 分别是
`ScrollLeftButton` / `ScrollRightButton`）。**Qt 没有为它们画出任何东西** ——
QSS 里 `QTabBar {{ qproperty-drawBase: 0; background: transparent; }}` 让整个
标签栏走 `QStyleSheetStyle`，而它不画这两个子控件。用户截图里两个红框圈到的
空方块就是它们。所以这不是"换个图标"，是原位留了个什么都没画的按钮。

## 为什么接管绘制，而不是换成自己的控件

PyQt6 里换控件这条路是**死的**，实测过三条：

1. `QTabBar.ButtonPosition` 只暴露 `LeftSide` / `RightSide`，滚动按钮那两个
   只能用整数值构造无名枚举成员 —— 而 `setTabButton` 传进去**静默不生效**
   （第一次探针里量到的 20px 让位其实是原生按钮自己留的，不是新控件）。
2. `QTabBar.scrollLeft()` / `scrollRight()` **在 PyQt6 里不存在**；
   名字叫 `scroll` 的那个是 `QWidget.scroll(dx, dy)`，滚的是控件自己。
3. 装上自定义控件后，Qt 原本那根「按钮 `clicked()` → `scrollLeft()`」的
   连接随原生控件一起没了，而上面两条正好堵死了补救的可能 —— 按钮会变成
   能看不能点的摆设。

在 `QTabBar` 上挂 `QProxyStyle` 也没用：QSS 的子控件是 `QStyleSheetStyle`
自己画的，控件级代理只截得到 `CE_PushButton*` 那两个元素。

所以**保留原生按钮，只把绘制接管过来**：Qt 继续负责定位、显隐、以及点击后
滚动，本类只在 `Paint` 事件上画一个 `<` / `>`。这样滚动行为一点没变。

（`tab_close.py` 里「`setTabButton` 装进去的控件会被 QTabBar 挪回边框」的坑
同样不适用于这里，因为根本没用 `setTabButton`。）
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from .design.v1.palette import Palette

#: QTabBar 内部两个滚动按钮的 objectName。它们是 QTabBar 构造时就建好的
#: 子控件（标签少时只是被隐藏），所以构造完就能按名字找到。
_LEFT = "ScrollLeftButton"
_RIGHT = "ScrollRightButton"

_GLYPH = {_LEFT: "<", _RIGHT: ">"}


class TabScrollPainter(QtCore.QObject):
    """把 QTabBar 原生滚动按钮的空画布画成 ``<`` / ``>``。

    用事件过滤吃掉这两个按钮的 ``Paint``，自己画完就不让原生/QSS 再画一遍
    —— 否则样式表那层会先画一个什么都没有的按钮框，再被我们盖上去。
    """

    def __init__(self, tabs: QtWidgets.QTabWidget, palette: Palette,
                 parent=None):
        super().__init__(parent if parent is not None else tabs)
        self._palette = palette
        self._enabled = True
        self._bar = tabs.tabBar()
        self._buttons: dict[str, QtWidgets.QToolButton] = {}
        for name in (_LEFT, _RIGHT):
            btn = self._bar.findChild(QtWidgets.QToolButton, name)
            if btn is None:          # pragma: no cover - 取决于 Qt 版本
                continue
            self._buttons[name] = btn
            btn.installEventFilter(self)

    def set_palette(self, palette: Palette) -> None:
        self._palette = palette
        for btn in self._buttons.values():
            btn.update()

    def set_paint_enabled(self, on: bool) -> None:
        """切回原生的 Paint 路径 —— 给元断言用，平常别碰。

        关掉后这两个按钮的 Paint 会照原生/QSS 那条路走回来，画不出 `<` `>`。
        判据里用它对照「我们画了」和「我们没画」两个版本，验证像素判据量的是
        画字形这件事而不是恒绿。
        """
        self._enabled = bool(on)
        for btn in self._buttons.values():
            btn.update()

    # ------------------------------------------------------------------ paint

    def eventFilter(self, obj, event):
        if self._enabled and event.type() == QtCore.QEvent.Type.Paint:
            for name, btn in self._buttons.items():
                if btn is obj:
                    self._paint(btn, _GLYPH[name])
                    return True        # 吃掉：不让样式表再画一遍空按钮
        return super().eventFilter(obj, event)

    def _paint(self, btn: QtWidgets.QToolButton, glyph: str) -> None:
        p = self._palette
        qp = QtGui.QPainter(btn)
        qp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = btn.rect()
        # **底子必须自己铺。** 吃掉 Paint 之后 Qt 不再擦背景，不铺的话上一帧
        # 的残影会一直留着 —— 滚动时按钮跨着标签移动，残影会拖成一条。
        qp.fillRect(rect, QtGui.QColor(p.bg_base))

        active = btn.isEnabled() and btn.underMouse()
        if active:
            fill = QtGui.QColor(p.bg_elevated)
            if btn.isDown():
                fill = fill.darker(112)
            qp.setPen(QtCore.Qt.PenStyle.NoPen)
            qp.setBrush(QtGui.QBrush(fill))
            qp.drawRoundedRect(QtCore.QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                               6, 6)

        if not btn.isEnabled():
            colour = p.text_muted
        elif btn.isDown():
            colour = p.text
        elif active:
            colour = p.text
        else:
            colour = p.text_dim
        qp.setPen(QtGui.QColor(colour))
        font = QtGui.QFont(qp.font())
        # 字形比标签文字小一号：`<` `>` 是尖括号，不缩一点会显得比标签还重。
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1.5))
        qp.setFont(font)
        qp.drawText(rect, QtCore.Qt.AlignmentFlag.AlignCenter, glyph)
        qp.end()


def install_tab_scroll_buttons(tabs: QtWidgets.QTabWidget,
                               palette: Palette) -> TabScrollPainter:
    """接管标签栏两端滚动按钮的绘制。返回那个 painter（换主题时要给它上色）。"""
    return TabScrollPainter(tabs, palette)
