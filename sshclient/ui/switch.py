"""A reusable pill-and-knob switch, plus the one painter both it and the tree
delegate draw with.

**Why this lives in its own module.** The toggle glyph already existed, but only
as a :class:`~PyQt6.QtWidgets.QStyledItemDelegate` inside ``tunnel_panel.py`` —
a delegate is not a widget, so the connect dialog could not reuse it. Re-drawing
the pill inside a dialog is how two toggles drift apart (one ends up 36px, the
other 38px, and only on one theme). So the *painting* moves here and both call
sites go through :func:`paint_switch`.

**Sizes are in physical pixels and do NOT follow ``ui_font_size``.** The pill is
a glyph, not body text: at 18pt it must not grow into a 50px-wide lozenge that
dwarfs the label next to it. Same reasoning as ``qss._ARROW_PX``. The switch
does keep a minimum *hit* area of 44px tall via :attr:`Switch.MIN_H`, because a
21px-tall click target is genuinely hard to hit and that is an accessibility
requirement, not a styling one.

**Colour comes from the passed-in :class:`Palette`, never from
``option.widget.palette()`` or ``QPalette``.** No ``setPalette`` call exists
anywhere in this repo: the whole UI is driven by QSS, and QSS only writes the
``Base`` / ``Window`` roles — ``Accent`` / ``Mid`` stay system colours forever.
Reading them yields the same system blue on every theme. See the long note on
``_SwitchDelegate`` for the measured evidence.
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from .design.v1.palette import Palette

#: Pill geometry in physical pixels (see the module docstring on scaling).
PILL_W = 38
PILL_H = 21
#: Knob diameter: the pill height minus 3px of track showing on each side.
KNOB_D = PILL_H - 6


def paint_switch(painter: QtGui.QPainter, rect: QtCore.QRect, checked: bool,
                 palette: Palette, opacity: float = 1.0) -> None:
    """Draw the pill-and-knob toggle into *rect*, centred.

    *opacity* is applied to whatever is still selected, so callers can grey the
    whole glyph (the delegate does this for the "off" track) in one call
    instead of re-deriving colours.
    """
    p = palette
    # On = accent (follows theme and the user's accent choice); off =
    # border_strong; the knob takes the surface colour, so it reads as a hole
    # punched out of the track rather than a bead glued on top.
    #
    # **关闭态为什么不是 `text_muted`。** 原来用的是 `text_muted`，它在白
    # 卡片上对底 **CR 6.01** —— 数字本身过 AA，但轨道是 38×21 的**实心色块**
    # 而 muted 文字是 1-2px 的笔画，**面积差两个数量级**，于是同一行里
    # 「一块深灰」压着「一行灰字」，用户报的就是这条 SOCKS 行「不协调」。
    #
    # `border_strong` 是本仓既有的中性描边色，语义正好是「一个控件的轮廓」：
    # 实测对白卡片 **CR 4.02** —— 明显看得出是关闭态，但不抢旁边的文字。
    # （候选实测见 `_ptrack.py`：`border` 只有 2.68，太淡到认不出这是个
    # 开关；`text_dim` 10.34，比原来还重。）
    track = QtGui.QColor(p.accent if checked else p.border_strong)
    knob = QtGui.QColor(p.bg_surface)

    painter.save()
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    if opacity < 1.0:
        painter.setOpacity(opacity)
    x = rect.x() + (rect.width() - PILL_W) // 2
    y = rect.y() + (rect.height() - PILL_H) // 2
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(track)
    painter.drawRoundedRect(QtCore.QRectF(x, y, PILL_W, PILL_H),
                            PILL_H / 2.0, PILL_H / 2.0)
    kx = x + 3 if checked else x + PILL_W - KNOB_D - 3
    painter.setBrush(knob)
    painter.drawEllipse(QtCore.QRectF(kx, y + 3, KNOB_D, KNOB_D))
    painter.restore()


class Switch(QtWidgets.QAbstractButton):
    """A two-state toggle: :attr:`checked` replaces the "0 means off" idiom.

    **Why a switch rather than a checkbox.** The connect dialog used to encode
    "SOCKS5 off" as port ``0`` with a special-value text of ``不启用``. That
    hides the state behind a number whose meaning is only written in a hint
    next to it, and it silently swallows a typo'd port. A real control states
    the thing directly.

    The knob does not move while dragging: this is a state, not a value, and
    the press-then-release semantics of :class:`QAbstractButton` already
    cancel on a drag away from the widget.

    **Disabled greys the whole glyph, not just the track.** Qt only sends us
    ``isEnabled()``; the delegate's "off is 0.55 opacity" trick does not
    compose with a disabled-but-checked switch, where the knob would stay
    full-strength on a muted track.
    """

    #: Minimum click height. 21px is a hard target to hit; 44 is the accepted
    #: minimum touch/pointer target and costs nothing in a dialog row because
    #: the row is already taller than that.
    MIN_H = 44

    def __init__(self, checked: bool = False, palette: Palette | None = None,
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(bool(checked))
        self._palette = palette
        self.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    # -- palette ------------------------------------------------------------

    def set_palette_tokens(self, palette: Palette) -> None:
        """Hand the token set in. Called on theme change by the owner.

        Not :meth:`QWidget.setPalette` — see the module docstring.
        """
        self._palette = palette
        self.update()

    def _tokens(self) -> Palette:
        return self._palette

    # -- painting -----------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        tokens = self._tokens()
        if tokens is None:
            super().paintEvent(event)
            return
        # Disabled → dim everything; the knob would otherwise stay full
        # strength on a muted track and read as "still on".
        opacity = 0.4 if not self.isEnabled() else 1.0
        paint_switch(QtGui.QPainter(self), self.rect(), self.isChecked(),
                     tokens, opacity)

    # -- geometry -----------------------------------------------------------

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        return QtCore.QSize(PILL_W, self.MIN_H)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 (Qt naming)
        return QtCore.QSize(PILL_W, self.MIN_H)

    def sizePolicy(self) -> QtWidgets.QSizePolicy:  # noqa: N802 (Qt naming)
        return (QtWidgets.QSizePolicy.Policy.Fixed,
                QtWidgets.QSizePolicy.Policy.Fixed)
