"""左侧边栏：会话列表的行、折叠态的方块行、欢迎页，以及它们的尺寸常量。

只管「一个行长什么样、有多大」，不管摆放——那是 `MainWindow` 的事。
所以依赖方向是 main_window -> sidebar，反过来不成立。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from ..core.settings import DEFAULT_GROUP, SavedSession
from . import icons, logo
from .controls import _label, _set_icon
from .design.v1.palette import Palette


def _side_button(text: str, icon_name: str, tooltip: str,
                 palette: Palette) -> QtWidgets.QPushButton:
    btn = QtWidgets.QPushButton(text)
    btn.setProperty("variant", "side")
    # 走 _set_icon 而不是裸 setIcon：裸的不会把图标名挂在按钮上，
    # MainWindow.apply_theme 里的 _retheme_icon_button 拿不到名字就直接 return，
    # 于是「编辑」「删除」两个按钮在 Ctrl+T 之后仍留着旧调色板染的图标。
    # 症状是纯视觉的（深色图标落在浅色底上），没有任何报错。
    #
    # **这里原来还有个 `accent: bool = False`**，用它在 `side` / `sideAccent`
    # 两个 variant 之间切、并顺手给图标上 `text_on_accent` 色。它是死的：
    # 全仓两个调用点（编辑 / 删除）都没传，默认 False。配套的
    # `[variant="sideAccent"]` 规则也因此没有任何像素。
    # **只删 QSS 留参数比现状更糟** —— 有人传 `accent=True` 会拿到一个
    # 匹配不到任何规则的 variant，按钮无声掉回裸 QPushButton 外观，不报任何错。
    _set_icon(btn, icon_name, palette, 16)
    btn.setIconSize(QtCore.QSize(16, 16))
    btn.setToolTip(tooltip)
    btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
    return btn


def _pin(widget: QtWidgets.QWidget, width: int, height: int) -> None:
    widget.setFixedSize(width, height)


def _unpin_width(widget: QtWidgets.QWidget) -> None:
    """Undo a `setFixedWidth` or one axis of a `setFixedSize`.

    `setFixedSize` has no inverse -- it clamps the minimum and the maximum
    together -- so growing a widget back out means clearing both by hand.
    The height is deliberately left alone: the sidebar chrome keeps its 36px
    in both states, only the width follows the sidebar.
    """
    widget.setMinimumWidth(0)
    widget.setMaximumWidth(QtWidgets.QWIDGETSIZE_MAX)


def _release(widget: QtWidgets.QWidget) -> None:
    """Undo a `setFixedSize` completely, back to sizing from the layout."""
    widget.setMinimumSize(0, 0)
    widget.setMaximumSize(QtWidgets.QWIDGETSIZE_MAX, QtWidgets.QWIDGETSIZE_MAX)
# ---------------------------------------------------------------------------
# Sidebar geometry
# ---------------------------------------------------------------------------

SIDEBAR_W = 268
SIDEBAR_W_COLLAPSED = 56
# Row heights. The expanded row's own insets live in SessionItem; these two
# numbers have to match QListWidget.setSpacing + the ::item margin, which is
# horizontal-only since the overflow fix -- a vertical inset here would push
# the row out of its own item again.
ROW_H = 58
ROW_H_COMPACT = 40
# The group header's own row height. Deliberately not ROW_H/2: it carries no
# badge and no second text line, so it only has to hold one 12px label plus
# breathing room on both sides. 30 is the smallest that still reads as a
# separator between two sessions instead of a third, shorter session row.
GROUP_H = 30
ROW_RADIUS = 10
# The live dot in the rail. Named because its placement is derived from the
# badge geometry rather than from the layout -- see CompactSessionItem.
COMPACT_DOT = 9


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def grouped_sessions(sessions):
    """把会话按分组分段并排好序，返回 ``[(分组名, [会话…]), …]``。

    纯函数：不碰 Qt，所以排序规则可以被判据单独钉住。

    **没有分组名的会话归到 :data:`DEFAULT_GROUP`，不是散在列表末尾。**
    用户要的是「所有没分组的都归到一个默认分组下」，而一段没有标题的
    尾巴恰恰是最容易被忽略的地方：列表一长，它就被推出屏幕，用户既不知道
    那些会话存在，也不知道自己漏了没给它们分组。给它一个正常的头之后，
    「我没分组的会话都在这儿」变成一眼可见的事实。

    **键恒为字符串，不再有 ``None``。** 上一版用 ``None`` 区分「这一段
    有标题」和「这一段没有」，现在默认分组有了自己的名字，两种情况的差别
    已经由 ``DEFAULT_GROUP`` 这个键表达完毕 —— 留着 ``None`` 等于同一件
    事有两种写法，而调用方 ``if group is not None`` 少改一处就成了「默认
    分组不画头」。折叠态（不画任何头）由调用方自己传 ``[(None, …)]``，
    那里的 ``None`` 含义不同，且是调用方就地构造的，不经过本函数。

    **默认分组排最后。** 空分组是「还没整理」，有名字的分组是「整理过了」；
    新的连接默认落在默认分组里，放在列表末尾意味着它不会插到用户已经
    维护好的分组中间去。

    组内按显示名排序，`id` 兜底：两个会话可以叫同一个名字（不同分组，
    或者同分组内用户手滑），没有这个兜底时 Python 的稳定排序会保留
    `store.all` 的顺序 —— 也就是插入顺序，于是「重新打开软件，同名会话
    的上下会变」。分组头行靠行号回填选中项，行序一变选中就跳。
    """
    buckets: dict[str, list] = {}
    for s in sessions:
        buckets.setdefault(s.group_name(), []).append(s)

    def rows(items):
        return sorted(items, key=lambda s: (s.base_name().casefold(), s.id))

    named = sorted((k for k in buckets if k != DEFAULT_GROUP),
                   key=lambda k: (k.casefold(), k))
    out = [(k, rows(buckets[k])) for k in named]
    if DEFAULT_GROUP in buckets:
        out.append((DEFAULT_GROUP, rows(buckets[DEFAULT_GROUP])))
    return out


class GroupHeaderItem(QtWidgets.QWidget):
    """一行分组标题：折叠箭头 + 分组名 + 这个分组里的会话数。

    **整行可点，点了切换折叠。** 所以它必须明确自己**除了切换什么都不做**：
    选中、右键、删除都走不到这里，那些由
    ``MainWindow._reload_session_list`` 在 ``QListWidgetItem`` 那一层用
    flags 和 ``UserRole`` 挡住 —— 放在这里做只会得到一个「能点、点了没反应」
    的控件，和另一个「点了却连会话都选不中」的控件。

    **箭头是必要的，不是装饰。** 只靠「整行可点」的话用户完全看不出这里能
    交互；`GroupHeaderItem` 之前是个纯文字行，加了折叠能力而不加指示，
    等于把一个可点的死像素放在列表里。

    **tooltip 刻意不在这里设。** 它由 `MainWindow._add_group_header` 设在
    `QListWidgetItem` 上 —— 和会话行同一条路。两处都设就是一个新的
    「两句话要同步」的地方，而它们的文案迟早会长得不一样。
    """

    #: 点一下这一行就切换折叠。**带分组名发出去**，调用方不必用闭包把它
    #: 绑死在某一个实例上（重建列表时每个头都是新对象，绑死了就得重绑）。
    toggled = QtCore.pyqtSignal(str)

    def __init__(self, group: str, count: int, palette: Palette, parent=None,
                 expanded: bool = True):
        super().__init__(parent)
        self.group = group
        self._palette = palette
        self._expanded = True

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 10, 0)
        lay.setSpacing(6)

        self._arrow = QtWidgets.QLabel()
        # objectName 是给判据用的：这一行里有两个 QLabel，而
        # `findChildren(QLabel)` 的返回顺序是控件树遍历顺序 —— 那是 Qt 的
        # 实现细节。箭头控件换个位置就会弄红一条与功能无关的判据。
        self._arrow.setObjectName("groupArrow")
        self._arrow.setFixedSize(14, 14)
        lay.addWidget(self._arrow, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        name = _label(group, "groupName")
        name.setObjectName("groupName")
        # **Ignored, not the default Preferred.** 默认策略下 QBoxLayout 空间
        # 不够时按 sizeHint 比例回缩，而「会话数」那 3 个字符是这行里最不该
        # 消失的东西。Ignored 让这一格在收缩时第一个让步：名字被裁掉，数字
        # 永远留着。（268px 侧栏大约能放下 35 个汉字，只有病态的分组名才走
        # 到这条路，但走到的代价不该是「分组有几个会话」变成半个括号。）
        name.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                           QtWidgets.QSizePolicy.Policy.Preferred)
        lay.addWidget(name, 1)

        cnt = _label(str(count), "groupCount")
        cnt.setObjectName("groupCount")
        lay.addWidget(cnt, 0)

        # 箭头在最后 set 的：它要读 _expanded，而那个值由它自己设。
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.set_expanded(expanded)

    def set_expanded(self, expanded: bool) -> None:
        """切箭头。**不碰列表** —— 真的收起/展开由 MainWindow 重建列表做。

        两边都做会打架：这一行是 `setItemWidget` 装进去的，列表清空时它先
        被销毁，哪一方再动另一方就是操作一个已经不在树上的控件。
        """
        self._expanded = bool(expanded)
        self._arrow.setPixmap(icons.icon_pixmap(
            "chevron_down" if self._expanded else "chevron-right",
            self._palette.text_muted, 14, 1.7))
        self._arrow.setToolTip("点击折叠" if self._expanded else "点击展开")

    @property
    def expanded(self) -> bool:
        return self._expanded

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        """左键 = 切换折叠；其余按键照常往上走（右键因此仍然什么都不会发生）。

        **左键这一支 accept 之后不回 super。** 折叠之后选中项按 ``UserRole``
        回填，而分组头压根没有 ``UserRole`` —— 这一行唯一的交互就是切换，
        所以让事件**在这里落地**，别再往上冒给 ``QListWidget`` 去走它那套
        「选中一个 item」的逻辑。判据见 ``tests/_group.py`` 第 10 节的
        「点分组头不动选中态」。
        """
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.toggled.emit(self.group)
            event.accept()
            return
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Session list row
# ---------------------------------------------------------------------------


class SessionItem(QtWidgets.QWidget):
    """One row in the sidebar: icon + name + endpoint + auth badge."""

    def __init__(self, session: SavedSession, palette: Palette, parent=None):
        super().__init__(parent)
        self.session = session
        self._palette = palette
        self._selected = False

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(10)

        badge = QtWidgets.QLabel()
        badge.setFixedSize(32, 32)
        badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        auth_icon = "key" if session.auth_method == "key" else "user"
        badge.setPixmap(icons.icon_pixmap(auth_icon, palette.accent_graphic, 18))
        badge.setStyleSheet(
            f"background-color: {palette.accent_soft};"
            f"border-radius: 8px;"
        )
        # AlignVCenter, not AlignTop: the two text lines below span 42px of the
        # 58px row, so a top-aligned badge left the second line hanging 10px
        # below the icon with empty space beside it.
        lay.addWidget(badge, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)

        col = QtWidgets.QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        name = _label(session.base_name(), "sessionName")
        name.setObjectName("sessionName")
        col.addWidget(name)

        host = _label(f"{session.host}:{session.port}", "sessionHost")
        host.setObjectName("sessionHost")
        col.addWidget(host)

        lay.addLayout(col, 1)

    def refresh(self, palette: Palette) -> None:
        """Re-tint after a theme switch."""
        self._palette = palette
        name = self.findChild(QtWidgets.QLabel, "sessionName")
        if name:
            name.setText(self.session.base_name())
        host = self.findChild(QtWidgets.QLabel, "sessionHost")
        if host:
            host.setText(f"{self.session.host}:{self.session.port}")
        self._tint_host()

    def set_selected(self, on: bool) -> None:
        """Swap the endpoint's colour when this row becomes the current one.

        The row is a ``setItemWidget`` child, so it sits **outside** the
        ``::item`` box: Qt does not push ``:selected`` down onto descendants
        and ``QListWidget::item:selected QLabel[...]`` never matches. The
        colour therefore has to be pushed from code.

        Why it matters: the selected row's fill is ``accent_soft``.
        ``text_muted`` on it measures **4.89 (light) / 4.48 (dark)** — the
        dark value is just below the 4.5 that WCAG AA asks of 11px body
        text, and the light one only clears it by a hair. ``text_dim``
        measures 8.40 / 6.68 on the same fill, so the endpoint steps up one
        tier for exactly as long as the row is selected.

        **The numbers moved.** This used to read 4.29 / 4.33 for
        ``text_muted`` and 4.86 / 5.79 for ``text_dim``; a later palette
        pass lightened ``accent_soft`` and re-solved the ramp, so light now
        clears 4.5 outright while dark is at 4.48. The step-up is kept in
        both themes — it costs nothing and it keeps the two themes
        symmetric, which matters more than the light theme strictly
        needing it.
        """
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self._tint_host()

    def _tint_host(self) -> None:
        """Push the endpoint's colour **only while this row is selected**.

        Unselected we clear the inline sheet on purpose: the resting colour
        then comes from the one place that owns it -- the
        ``QLabel[role="sessionHost"]`` rule in ``qss.py`` -- instead of a
        second copy pinned onto the widget that a later QSS tweak would not
        reach.
        """
        host = self.findChild(QtWidgets.QLabel, "sessionHost")
        if host is None:
            return
        host.setStyleSheet(
            f"color: {self._palette.text_dim};" if self._selected else "")


class CompactSessionItem(QtWidgets.QWidget):
    """A sidebar row in the 56px rail: the auth badge and nothing else.

    56px leaves room for one 40px square and nothing more, so the name and
    the endpoint move to the item's tooltip. The auth badge is what tells two
    otherwise identical squares apart, and a live session gets a green dot --
    in the rail there is no other way to see what is actually connected.

    The layout adds no horizontal margins of its own. The list's rail-state
    ::item rule keeps a 2px gutter, so the 40px box centres itself inside the
    44px row; any margin here as well would push it back out of its own item,
    which is the same overflow the expanded rows used to have.

    Nothing but the badge goes into that layout. See `__init__` for why the
    live dot is a positioned child instead -- a second layout member makes
    the badge's centring depend on whether the session is connected.
    """

    def __init__(self, session: SavedSession, palette: Palette, parent=None):
        super().__init__(parent)
        self.session = session
        self._palette = palette
        self._selected = False

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self._badge = QtWidgets.QFrame()
        self._badge.setObjectName("compactBadge")
        self._badge.setFixedSize(ROW_H_COMPACT, ROW_H_COMPACT)
        inner = QtWidgets.QHBoxLayout(self._badge)
        inner.setContentsMargins(0, 0, 0, 0)

        self._icon = QtWidgets.QLabel()
        self._icon.setFixedSize(18, 18)
        inner.addWidget(self._icon, 0, QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._badge, 0, QtCore.Qt.AlignmentFlag.AlignCenter)

        # **The dot is deliberately NOT in `lay`.**
        #
        # It used to be `lay.addWidget(self._dot, 0, AlignRight|AlignTop)`
        # next to the badge. A QHBoxLayout hands out width in the order the
        # widgets were added, and a *visible* widget takes its share while a
        # hidden one takes none -- so the badge's centring depended on whether
        # the session happened to be connected. Measured on the real rail:
        # badge centre 28.0px with the dot hidden, **26.0px with it shown**,
        # i.e. the icon sat 2px left on exactly the connected rows and the
        # column of SSH glyphs was visibly ragged.
        #
        # So it is a plain child of the row, never a layout member, and
        # `_place_dot` pins it to the badge's top-right corner. Two rules
        # fall out of that for free: the badge now owns the layout outright
        # and so is centred on every row, live or not, and the dot tracks
        # the badge instead of standing still while the badge slides out
        # from under it.
        self._dot = QtWidgets.QLabel(self)
        self._dot.setObjectName("compactDot")
        self._dot.setFixedSize(COMPACT_DOT, COMPACT_DOT)
        self._dot.setVisible(False)
        # Watch the **badge**, not the row. A resizeEvent on the row covers
        # "the whole rail got wider", but not "the badge slid within a row
        # that did not change size" -- and the dot has to follow the badge
        # in that case too, or it ends up pointing at empty rail.
        self._badge.installEventFilter(self)
        self._place_dot()

        self.refresh(palette)
        self.set_live(False)

    def eventFilter(self, obj, ev):  # noqa: N802 - Qt naming
        if obj is self._badge and ev.type() in (
                QtCore.QEvent.Type.Move, QtCore.QEvent.Type.Resize):
            self._place_dot()
        return super().eventFilter(obj, ev)

    def _place_dot(self) -> None:
        """Pin the live dot to the badge's top-right corner.

        Inside the badge rather than floating past it: the rail row is
        exactly as tall as the badge, so there is no room to hang a dot
        *above*, and the visible 2px gutter either side of the badge belongs
        to the ``::item`` box. Corner-inside is the only placement that
        stays inside the row -- which this project's sidebar tests assert --
        while still reading as "on this icon".

        The 1px vertical inset keeps the dot off the badge's rounded corner
        pixel; horizontally it is flush with the badge's right edge.
        """
        b = self._badge.geometry()
        self._dot.move(b.x() + b.width() - self._dot.width(), b.y() + 1)

    def refresh(self, palette: Palette) -> None:
        """Re-tint after a theme switch."""
        self._palette = palette
        auth = "key" if self.session.auth_method == "key" else "user"
        self._icon.setPixmap(icons.icon_pixmap(auth, palette.accent_graphic, 18))
        self._retint()

    def _retint(self) -> None:
        """One place that owns both fills, so selection cannot desync.

        The rail's ``::item:selected`` fill is gone (see ``qss.py``): a 40px
        badge inside a 44px row leaves a 2px gutter on each side, and an
        item background shows through that gutter as two vertical bars
        flanking the badge -- the user was looking straight at those. With
        the item fill gone the badge itself has to carry the selected state,
        or a collapsed sidebar would show nothing at all about which session
        is current. It steps up to ``accent_soft`` -- the same fill the
        expanded row's badge uses, so the two states agree.

        The dot's ring is painted in whatever the badge fill currently is,
        otherwise it would keep a rail-coloured halo while the badge under it
        had gone blue.
        """
        p = self._palette
        fill = p.accent_soft if self._selected else p.bg_elevated
        self.setStyleSheet(
            f"QFrame#compactBadge {{"
            f"  background: {fill};"
            f"  border: 1px solid {p.border};"
            f"  border-radius: {ROW_RADIUS}px;"
            f"}}"
            f"QLabel#compactDot {{"
            f"  background: {p.success};"
            f"  border: 2px solid {fill};"
            f"  border-radius: 5px;"
            f"}}"
        )

    def set_selected(self, on: bool) -> None:
        """Carry the current row's state on the badge fill.

        ``MainWindow._sync_session_row_tints`` already probes every row for
        ``set_selected`` and calls it when present, so the rail picked this
        up without a change there.
        """
        on = bool(on)
        if on == self._selected:
            return
        self._selected = on
        self._retint()

    def set_live(self, live: bool) -> None:
        self._dot.setVisible(bool(live))
        label = self.session.base_name()
        tip = f"{label}  ·  {self.session.host}:{self.session.port}"
        self.setToolTip(f"{tip}  ·  已连接" if live else tip)


# ---------------------------------------------------------------------------
# Welcome page


class WelcomePage(QtWidgets.QWidget):
    newRequested = QtCore.pyqtSignal()
    #: 用户点了某条会话（参数是会话 id）
    sessionRequested = QtCore.pyqtSignal(str)

    #: 「快速连接」卡片最多列几条。**多了就成列表了** —— 欢迎页要的是
    #: 一眼能看完的入口，不是第二个侧栏。
    QUICK_MAX = 5

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self._palette = palette
        self._quick: list[str] = []          # 卡片上正在显示的会话 id

        lay = QtWidgets.QVBoxLayout(self)
        lay.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(0)

        mark = QtWidgets.QLabel()
        mark.setObjectName("welcomeMark")
        mark.setPixmap(logo.logo_pixmap(56, top=palette.accent_hover,
                                        bottom=palette.accent))
        mark.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(mark)
        lay.addSpacing(22)

        lay.addWidget(_label("MavisSSH", "welcomeTitle"), 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(8)
        lay.addWidget(_label("终端 · 文件 · 资源监控 · lrzsz", "welcomeSub"), 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        lay.addSpacing(28)

        cta = QtWidgets.QPushButton("  新建连接")
        cta.setProperty("variant", "primary")
        # **高度交给 QSS 的 `role="cta"`，不再 `setFixedHeight(38)`。**
        # QSS 的 `min-height` 会顶掉 `setFixedHeight`（QPushButton 的纵向
        # size policy 是 Fixed，布局直接采用样式表算出的 sizeHint）——
        # 实测加了通用那条 `CONTROL_H` 之后这个 hero 按钮从 38 掉到 34，
        # 和表单里其它按钮一样大，就不再是 hero 了。见 qss.py 里的
        # `QPushButton[role="cta"]`。
        cta.setProperty("role", "cta")
        cta.setIcon(icons.icon("plus", palette, 16, palette.text_on_solid))
        cta.setIconSize(QtCore.QSize(16, 16))
        cta.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        cta.setMinimumWidth(150)
        cta.clicked.connect(self.newRequested)
        lay.addWidget(cta, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        lay.addSpacing(18)
        lay.addWidget(_label("或使用左侧会话列表 · Ctrl+N", "welcomeHint"), 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

        # ---- 快速连接卡片 ----
        # **欢迎页原来只有中间那一坨，左边一大片空白。** 加一张列出已保存
        # 会话的卡片，既填了空白，又给一个「点一下就连」的入口。
        #
        # **不用「最近」这个词** —— SavedSession 里没有「上次使用时间」字段，
        # 按什么排序都是假的。叫「快速连接」，列的就是已保存的会话。
        self._quick_card = QtWidgets.QFrame()
        self._quick_card.setProperty("role", "card")
        ql = QtWidgets.QVBoxLayout(self._quick_card)
        ql.setContentsMargins(18, 14, 18, 14)
        ql.setSpacing(6)
        ql.addWidget(_label("快速连接", "section"))
        self._quick_list = QtWidgets.QVBoxLayout()
        self._quick_list.setSpacing(2)
        ql.addLayout(self._quick_list)
        self._quick_empty = _label("还没有保存的会话 · Ctrl+N 新建", "welcomeHint")
        ql.addWidget(self._quick_empty)
        self._quick_card.setMaximumWidth(420)
        lay.addSpacing(26)
        lay.addWidget(self._quick_card, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)

    # ------------------------------------------------------------------ quick

    def set_sessions(self, sessions) -> None:
        """刷新「快速连接」卡片。

        *sessions* —— ``SavedSession`` 列表（顺序由调用方定）。
        """
        # 清掉旧行。
        #
        # **`setParent(None)` 不能省。** `takeAt(0)` 只把条目从布局里拿走，
        # 控件**仍然是卡片的子控件**，还占着位置、还画在那儿。
        # `deleteLater()` 也不行 —— DeferredDelete 要等事件循环转到那个层级
        # 才投递，而 `set_sessions` 常常是在**一串同步调用里**被调好几次
        # （建窗口一次、刷侧栏又一次），那期间旧按钮全都还在。
        # 结果就是 5 行新按钮叠在 15 行旧按钮上 —— 文字互相压住。
        #
        # `setParent(None)` 立刻把它从父控件摘掉（于是立刻不再绘制、
        # 不再占布局），`deleteLater()` 只是回收内存。
        while self._quick_list.count():
            item = self._quick_list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._quick = []
        items = list(sessions)[:self.QUICK_MAX]
        self._quick_empty.setVisible(not items)
        for s in items:
            row = QtWidgets.QPushButton(
                "  %s    %s@%s:%d"
                % (s.display_name(), s.username, s.host, s.port))
            row.setProperty("variant", "side")
            row.setCursor(QtGui.QCursor(QtCore.Qt.CursorShape.PointingHandCursor))
            row.setToolTip("点击连接 %s" % s.display_name())
            row.clicked.connect(
                lambda _=False, sid=s.id: self.sessionRequested.emit(sid))
            self._quick_list.addWidget(row)
            self._quick.append(s.id)

    def repaint_theme(self, palette: Palette) -> None:
        self._palette = palette
        mark = self.findChild(QtWidgets.QLabel, "welcomeMark")
        if mark:
            mark.setPixmap(logo.logo_pixmap(56, top=palette.accent_hover,
                                            bottom=palette.accent))
        cta = self.findChild(QtWidgets.QPushButton)
        if cta:
            cta.setIcon(icons.icon("plus", palette, 16, palette.text_on_solid))
