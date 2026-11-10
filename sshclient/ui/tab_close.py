"""标签页的关闭按钮。

QSS 里的 QTabBar::close-button 子控件一旦被样式化，Qt 就会丢掉原生图标；
而用 setTabButton 装进去的控件又完全无视子控件边距。剩下的办法只有自己
画——所以这个类既画按钮，也负责把每个标签的按钮摆到该在的位置。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from .design.v1.palette import text_on


class TabCloseButton(QtWidgets.QToolButton):
    """The close control on a session tab, as a real widget.

    Two Qt behaviours make the built-in button unusable here:

    1. A stylesheet that sets any box property on ``QTabBar::close-button``
       (``border-radius`` for a hover highlight is enough) makes Qt stop
       drawing the native glyph and put nothing in its place -- the tab
       simply loses its close button. Verified in _closebtn.py.
    2. When a stylesheet is active, the ``option.rect`` handed to a
       ``QProxyStyle.drawPrimitive`` override is not a reliable source for
       positioning, so a clamp computed from it lands in the wrong place.

    A plain widget sidesteps both: the geometry, the hover state and the hit
    area are entirely ours, and the glyph is drawn at a known offset from the
    tab's right edge rather than wherever the style engine decided to put it.
    """

    _SIZE = 18
    _GLYPH = 3.5

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self._palette = palette
        self._hover = False
        self.setFixedSize(self._SIZE, self._SIZE)
        self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
        self.setToolTip("关闭会话")
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)

    def set_palette_colors(self, palette: Palette) -> None:
        self._palette = palette
        self.update()

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def _glyph_color(self) -> str:
        """✕ 的颜色。

        **填充态下必须按填充色自己算黑白**（`palette.text_on`）。
        原来用的是 `text_on_accent` —— 那是按 **accent** 推的，拿它压
        `danger` 属于**跨族借用**（`design/v1/__init__.py` 的硬约束 1）：
        浅色主题下它是近黑 `#0d0f14`，压 `#C02626` 只有 **3.237**，
        而同一个填充用白字是 **5.921**。借用不会让数值变差到「有测试会红」，
        所以只能就地按目标填充色算。

        写成方法而不是就地算，是为了让判据能**独立验这一条**：
        `tests/_filltext.py` 直接问「这个按钮会用哪个颜色画 ✕」，
        再拿它跟**实际的填充色**算对比度。
        """
        if not (self._hover or self.isDown()):
            return self._palette.text_dim
        fill = QtGui.QColor(self._palette.danger)
        if self.isDown():
            fill = fill.darker(115)
        return text_on(fill.name())

    def paintEvent(self, event) -> None:
        p = self._palette
        active = self._hover or self.isDown()
        qp = QtGui.QPainter(self)
        qp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        if active:
            fill = QtGui.QColor(p.danger)
            if self.isDown():
                fill = fill.darker(115)
            qp.setPen(QtCore.Qt.PenStyle.NoPen)
            qp.setBrush(QtGui.QBrush(fill))
            qp.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0, 0, -0.5, -0.5), 4, 4)
        pen = qp.pen()
        pen.setWidthF(1.4)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        pen.setColor(QtGui.QColor(self._glyph_color()))
        qp.setPen(pen)
        qp.setPen(pen)
        d = self._GLYPH
        c = QtCore.QPointF(self.rect().center())
        qp.drawLine(QtCore.QPointF(c.x() - d, c.y() - d), QtCore.QPointF(c.x() + d, c.y() + d))
        qp.drawLine(QtCore.QPointF(c.x() - d, c.y() + d), QtCore.QPointF(c.x() + d, c.y() - d))
        qp.end()


#: set ``True`` on a tab page to keep :class:`TabCloseController` from giving
#: it a close button (the welcome page, which cannot be closed)
NO_CLOSE = "tabNoClose"


class TabCloseController(QtCore.QObject):
    """Owns the close widget on every closable tab of a ``QTabWidget``.

    Three Qt behaviours rule out every obvious approach:

    1. Any *box* property on ``QTabBar::close-button`` -- ``border-radius`` for
       a hover highlight is enough -- makes Qt stop drawing the native glyph
       and put nothing in its place, so the tab simply loses its close
       button. Verified in _closebtn.py.
    2. For a widget installed with ``QTabBar::setTabButton`` both the
       subcontrol margins *and* the tab's own right padding are ignored: it
       lands 1px from the tab border whatever is asked for. Sweeping
       padding-right from 8 to 32px moved the tab width every time and the
       gap not at all.
    3. Even after moving it, QTabBar re-runs its own tab layout on every paint
       and puts the widget back on the border, so ``setTabButton`` cannot be
       used at all.

    The buttons are therefore plain children of the tab bar, positioned here,
    and the strip they live in is reserved by the tab's ``padding-right`` --
    which is the one thing the stylesheet does control. The labels are elided
    against that padding, so :data:`PAD_RIGHT` and the QSS must agree or the
    text would slide under the glyph. _tabbtn.py guards the result.

    Buttons are matched to tabs by geometry rather than by a captured index,
    which keeps a click correct after tabs are closed, reordered or rebuilt.

    **A button's position is always derived from its own tab, never clamped
    to a "safe area".** The earlier version clamped the x into the strip left
    free by the two native scroll buttons, which kept the glyph off them but
    ignored *which tab it belonged to*: the rightmost tab's X got pulled to
    the left of its own tab, landing on its neighbour. A tab whose button
    does not fit in the visible strip now simply has no button until it does.
    """

    #: air between the glyph and the tab's right border
    INSET = 10
    #: must match the ``padding-right`` of QTabBar::tab in build_qss
    PAD_RIGHT = 34

    def __init__(self, tabs: QtWidgets.QTabWidget,
                 palette: Palette, parent=None):
        super().__init__(parent if parent is not None else tabs)
        self._tabs = tabs
        self._bar = tabs.tabBar()
        self._palette = palette
        self._buttons: list[TabCloseButton] = []
        self._busy = False
        self._bar.installEventFilter(self)
        # A drag-reorder updates tabRect() straight away and emits no layout
        # event at all, so the paint hook alone would leave the buttons on the
        # previous tab for a frame.
        self._bar.tabMoved.connect(lambda *_: self.sync())

    # ------------------------------------------------------------------ public

    def sync(self) -> None:
        """Create, drop and place the close widget for every tab."""
        if self._busy:
            return
        self._busy = True
        try:
            slots = [i for i in range(self._tabs.count())
                     if self._tabs.widget(i).property(NO_CLOSE) is not True]
            if len(slots) != len(self._buttons):
                # Every button is interchangeable, so a count change is the
                # only thing that has to force a rebuild -- a reorder keeps
                # them all valid, just over a different label.
                self._rebuild(slots)
            bar_w = self._bar.width()
            # **可视区要把两端滚动按钮的占位扣掉。** QTabBar 溢出时两端各放
            # 一条原生滚动按钮（``ScrollLeftButton`` / ``ScrollRightButton``），
            # 可 ``tabRect(i).right()`` **不**扣除这条占位 —— 它返回的是按
            # 「整个宽度」算的右沿。
            vis_left = self._scroll_strip_left()
            vis_right = bar_w - self._scroll_strip_right()
            for i, btn in zip(slots, self._buttons):
                r = self._bar.tabRect(i)
                # tab 整个滚出可视区：按钮跟着消失，滚回来时再出现。
                if r.isEmpty() or r.right() < vis_left or r.left() > vis_right:
                    btn.hide()
                    continue
                x = r.right() - self.INSET - btn.width() + 1
                y = r.top() + (r.height() - btn.height()) // 2
                # **按钮只跟着自己的 tab 走，不做安全区钳位。**
                #
                # 原来这里有一层 ``safe_left`` / ``safe_right`` 钳位，本意是
                # 「别让 X 压到 `<` `>` 上」。但钳位只管 x 不管归属，于是
                # 最右边那个 tab 的 X 被拉到**自己 tab 的左边外面**：
                # 实测 tab4 横跨 958-1188，它的 X 被摆到 x=952 —— 那个位置
                # 属于 tab3，X 就贴错了 tab。
                #
                # 改成**位置永远由 tab 决定**，把「会不会被滚动按钮压住」交给
                # 下面那个可见性判断：整个按钮都在可视区内就画，否则不画。
                # 这样「X 属于哪个 tab」是恒等式，不存在贴错的可能；用户
                # 要的「被遮挡的 tab 的 X 要等展开才显示」也就自然成立了。
                if x < vis_left or x + btn.width() > vis_right:
                    btn.hide()
                    continue
                if not btn.isVisible():
                    btn.show()
                if btn.x() != x or btn.y() != y:
                    btn.move(x, y)
                # 原生滚动按钮与关闭按钮都是 bar 的子控件，同层时后加的在上，
                # 而原生那两条是 QTabBar 构造时就建好的。滚动/重排时 Qt 会重
                # 排子控件，这里每次同步都抬一次，保证 X 不被 `>` 压住。
                btn.raise_()
        finally:
            self._busy = False

    def _scroll_strip_left(self) -> int:
        """`<` 滚动按钮占的宽度（不可见时为 0）。"""
        # 滚动按钮的 objectName 是 tab_scroll.py 那边约定的；这里只读不装配。
        for btn in self._bar.findChildren(QtWidgets.QToolButton, "ScrollLeftButton"):
            if btn.isVisible():
                return btn.width()
        return 0

    def _scroll_strip_right(self) -> int:
        for btn in self._bar.findChildren(QtWidgets.QToolButton, "ScrollRightButton"):
            if btn.isVisible():
                return btn.width()
        return 0

    def set_palette(self, palette: Palette) -> None:
        self._palette = palette
        for btn in self._buttons:
            btn.set_palette_colors(palette)
        self.sync()

    def eventFilter(self, obj, event):
        # Paint is the only hook that sees every structural change. The bar
        # posts its LayoutRequest to its *parent* when a tab is inserted or
        # removed, so an event filter on the bar catches neither; and a
        # reorder emits nothing at all. Paint also runs after the layout, so
        # tabRect() is current -- and since the buttons are plain children,
        # QTabBar never re-positions them behind our back.
        if obj is self._bar and event.type() == QtCore.QEvent.Type.Paint:
            self.sync()
        return super().eventFilter(obj, event)

    # ----------------------------------------------------------------- private

    def _rebuild(self, slots) -> None:
        for btn in self._buttons:
            btn.setParent(None)
            btn.deleteLater()
        self._buttons = []
        for _ in slots:
            btn = TabCloseButton(self._palette, self._bar)
            btn.set_palette_colors(self._palette)
            btn.clicked.connect(self._emit_close)
            self._buttons.append(btn)
        # **不在这里 `raise_()`。** z 序由 `sync()` 每次同步时抬 —— 原生滚动
        # 按钮是 QTabBar 构造时就建好的子控件，滚动时 Qt 会重排子控件，只在
        # 建按钮时抬一次不够。

    def _emit_close(self, _checked: bool = False) -> None:
        btn = self.sender()
        if not isinstance(btn, TabCloseButton):
            return
        centre = btn.mapTo(self._bar, btn.rect().center())
        for i in range(self._tabs.count()):
            if self._bar.tabRect(i).contains(centre):
                # On the QTabWidget, *not* on the bar. Both classes carry a
                # tabCloseRequested signal and they are not the same signal:
                # the bar's only reaches slots wired to the bar, and
                # MainWindow listens on the QTabWidget's. Emitting on the bar
                # closed nothing, and the click test missed it because it had
                # connected its own probe to the bar as well.
                self._tabs.tabCloseRequested.emit(i)
                return
