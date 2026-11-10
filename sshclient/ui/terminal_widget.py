"""Terminal widget that renders a pyte screen to a QWidget.

The widget is *not* responsible for talking to SSH -- it only handles the
byte stream between the user and a :class:`paramiko.Channel` exposed by
:meth:`bind_channel`. Decoupling lets the same widget be used for the lrzsz
"console" too.
"""
from __future__ import annotations

import codecs
import itertools
from collections.abc import Callable

import pyte
from PyQt6 import QtCore, QtGui, QtWidgets

from ..core.fonts import mono_font
from .design.v1.palette import ansi_palette, get_palette, xterm256_palette

# ---------------------------------------------------------------------------
# Color helpers


#: 256 色的 16–255 段。**这里原来自己建了一份** `_build_xterm_256()`，
#: 与 `palette.xterm256_palette()` 逐字符相同 —— 两份真相，其中一份
#: （palette 那份）全仓零消费者。删掉自己那份，改用 palette 的。
#: 索引记得减 16（见 `_color_to_hex` 里那段注释）。
_XTERM256 = xterm256_palette()

#: 滚轮一格走多少行。**滚动条的 singleStep 必须跟它一致** ——
#: QScrollBar 默认是 1，不对齐就是「拖条一格一行、滚轮一格三行」两套速度。
_WHEEL_STEP = 3
#: 可视滚动条的宽度。必须 ≤ `_pad_x`，才能整个藏进右边距里不压字。
_SB_WIDTH = 10

# The canonical SGR code for each name. Replaying a name as "38;5;<index>"
# instead would make pyte hand back a *hex* string, which then bypasses the
# themed palette and paints a hard xterm colour -- so names are re-emitted as
# real 30-37 / 90-97 codes to survive the round-trip intact.
_NAMED_SGR = {
    "black": 30, "red": 31, "green": 32, "brown": 33, "yellow": 33,
    "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
    "brightblack": 90, "brightred": 91, "brightgreen": 92,
    "brightbrown": 93, "brightyellow": 93, "brightblue": 94,
    "brightmagenta": 95, "brightcyan": 96, "brightwhite": 97,
}


def _sgr_color(base: int, val) -> str | None:
    """Build the SGR fragment that reproduces *val* for fg (*base* 38) or bg (48).

    Returns None when the colour is the terminal default, so the caller can omit
    it entirely.
    """
    if isinstance(val, int):
        return f"{base};5;{val}"
    if not isinstance(val, str):
        return None
    cs = val.strip().lower()
    if cs in ("default", "foreground", "background", ""):
        return None
    code = _NAMED_SGR.get(cs)
    if code is not None:
        return f"{base - 8};{code}"          # 38->30, 48->40
    hexpart = cs.removeprefix("#")
    if len(hexpart) == 6 and all(ch in "0123456789abcdef" for ch in hexpart):
        try:
            r, g, b = int(hexpart[0:2], 16), int(hexpart[2:4], 16), int(hexpart[4:6], 16)
        except ValueError:
            return None
        return f"{base};2;{r};{g};{b}"
    return None


# Characters that only carry SGR styling -- stripped when testing for blanks.
_SGR_CHARS = "\x1b[0123456789;m"


def _pad_row(text: str, width: int) -> str:
    """Right-pad / truncate *text* to *width* characters."""
    if len(text) >= width:
        return text[:width]
    return text + " " * (width - len(text))


def _history_row_text(raw, placeholder: str) -> str:
    """把 pyte 历史里的一行解成字符串。

    **这是历史行解码的唯一一份实现。** 绘制（`paintEvent`）和复制
    （`selected_text`）原来各写了一遍，而两份已经漂移过：绘制那边早就修好了
    `StaticDefaultDict` 那个坑，复制那边还是 `for b in raw: chr(b)`（迭代映射
    给出的是列号，复制出来是一串控制字符）；反过来，复制那边后来补了宽字符
    占位格的正确处理，绘制那边没跟上。同一个坑修一半、改一次只改一处，是
    这段代码反复出问题的根源，所以收口到这里，用 `placeholder` 参数表达两条
    路径**有意**不同的那一处。

    - **pyte 0.8 的 `history.top` 存的是 `StaticDefaultDict`（列号 -> Char），
      不是 bytearray。** 要取的是 `values()` 里每个 `Char.data`。
    - `placeholder=" "` 用于绘制：补出来的空格和空白像素完全一样，且
      `paintEvent` 规定 `data != " "` 就不画，所以无害。
    - `placeholder=""` 用于复制：pyte 把宽字符写成两格（头格放字，紧跟那格
      `data == ""` 当占位），补进去的空格会真的进剪贴板，用户粘出来看到的是
      「中 文 宽」。行尾补白由 `_pad_row` 负责。
    """
    if isinstance(raw, (bytes, bytearray)):
        # **不能用 errors="replace"**：它永远不抛异常，下面这行 latin-1
        # 兜底就成了死码，历史行照样全是豆腐块。与 _decode 同一个道理。
        try:
            return bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(raw).decode("latin-1")
    try:
        cells = raw.values()
    except AttributeError:
        cells = ()
    return "".join((getattr(ch, "data", None) or placeholder) for ch in cells)


# pyte stores colours in three shapes, depending on how the escape was written:
#   * a *name*  -- "red", "brightcyan", "default" (see pyte.graphics.FG_ANSI /
#                  FG_AIXTERM; note it spells yellow as "brown")
#   * an *int*  -- 0-255, only when set programmatically
#   * a *hex str* -- "ff8700" WITHOUT a leading "#", for 38;5;N and 38;2;R;G;B
# A bare "ff8700" is not a valid QColor, so passing it straight through silently
# painted those cells black (invisible on a dark background).
_ANSI_NAMES = {
    "black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
    "brightblack": 8, "brightred": 9, "brightgreen": 10, "brightbrown": 11,
    "brightyellow": 11, "brightblue": 12, "brightmagenta": 13,
    "brightcyan": 14, "brightwhite": 15,
}
_DEFAULT_NAMES = ("default", "foreground", "background", "")


def _color_to_hex(c, default_hex: str = "#ffffff", ansi: list[str] | None = None,
                  bold: bool = False) -> str:
    """Resolve a pyte colour value to a ``#rrggbb`` string for the theme.

    *ansi* is the 16-entry themed palette; when a cell is bold we promote the
    eight base names to their bright counterpart, the way a real terminal does.
    Anything unrecognised falls back to *default_hex* so the cell stays legible.
    """
    palette = ansi if ansi is not None else _XTERM256

    if isinstance(c, int):
        idx = c
    elif isinstance(c, str):
        cs = c.strip().lower()
        if cs in _DEFAULT_NAMES:
            return default_hex
        if cs in _ANSI_NAMES:
            idx = _ANSI_NAMES[cs]
        elif len(cs) == 6 and all(ch in "0123456789abcdef" for ch in cs):
            return "#" + cs          # pyte drops the "#" -- put it back
        else:
            return default_hex
    else:
        return default_hex

    if bold and idx < 8:
        idx += 8
    if 0 <= idx < 16:
        return palette[idx] if len(palette) > idx else default_hex
    if 16 <= idx < 256:
        if idx < len(palette):
            return palette[idx]
        # **索引要减 16。** `xterm256_palette()` 的第 0 项对应 xterm 的 16
        # （表里只放 16–255 那 240 色）。原来这里写的是 `_XTERM256[idx]`，
        # 于是**整段 16–255 都差 16**（取到的 16 其实是 32 号色），而
        # `idx >= 240` 时直接 IndexError。
        #
        # 之所以一直没被发现：pyte 交给本函数的是 6 位十六进制**字符串**，
        # 在 `:132-133` 那一支就 `return "#" + cs` 了，根本走不到这个分支。
        # 也就是说 256 色的 int 形式**从来没被真正用过** —— 顺带说明
        # 本文件原来那份 `_build_xterm_256()` 与 `palette` 里那张表逐字符
        # 相同、而 `palette` 那张表全仓零消费者，不是巧合。
        return _XTERM256[idx - 16]
    return default_hex


# 上面这个函数在 paintEvent 里是**每格调两次**的。一个 26x100 的屏就是五千多次，
# 而一次绘制实测 60ms —— 界面事件循环被它占住，**按键响应**跟着发木。
# （原来说「光标闪烁和输入发木」，但这个控件**没有光标闪烁**：全文件零 QTimer、
#  零闪烁相位，光标是一个恒定的实心块。`Char.blink` 那个字段是 pyte 的 SGR 属性，
#  同样没人消费。发木的只有输入那一半。）
#
# 取值空间是**极小**的：16 个 ANSI 色 + default + 偶尔的 #rrggbb，加一个
# bold 开关，命中集合就那么几十条。所以缓存之后那五千多次 Python 层字符串
# 处理塌成几十次字典查找。
#
# **换主题必须清空** —— 缓存键里没有 ansi 调色板，而 ansi 是随主题变的
# （见 apply_palette）。
_HEX_CACHE: dict = {}
_QCOLOR_CACHE: dict = {}


def _clear_color_caches() -> None:
    _HEX_CACHE.clear()
    _QCOLOR_CACHE.clear()


def _hex(c, default_hex: str, ansi: list, bold: bool) -> str:
    """带缓存的 :func:`_color_to_hex`。"""
    key = (c, default_hex, bold)
    hit = _HEX_CACHE.get(key)
    if hit is None:
        hit = _color_to_hex(c, default_hex, ansi, bold=bold)
        _HEX_CACHE[key] = hit
    return hit


def _qcolor(hex_str: str) -> QtGui.QColor:
    """带缓存的 ``QColor(hex)``。QColor 构造一次要解析字符串，绘制里是每格两次。"""
    q = _QCOLOR_CACHE.get(hex_str)
    if q is None:
        q = QtGui.QColor(hex_str)
        _QCOLOR_CACHE[hex_str] = q
    return q


# ---------------------------------------------------------------------------
# Screen with history (a thin shim that combines pyte.Screen + history tracking)


class _Screen(pyte.HistoryScreen):
    """A thin pyte.HistoryScreen with history. Rendering is driven by the
    owning widget (no pyqtSignal here because pyte's ``__getattribute__``
    proxy would hide it)."""


# ---------------------------------------------------------------------------
# The widget itself


class TerminalWidget(QtWidgets.QWidget):
    """A QWidget that renders a pyte screen and forwards key input."""

    #: **声明了但全仓没有任何 `connect`、也没有任何 `emit`。** 留着是因为它是
    #: 一条「远端发来关闭请求时该怎么回应」的合理扩展点，但今天它是死的。
    #: 别照着它写代码以为接上了。
    requestClose = QtCore.pyqtSignal()

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_KeyCompression, False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_InputMethodEnabled, True)

        self._char_w = 8
        self._char_h = 16
        self._pad_x = 12   # left/right breathing room
        self._pad_y = 8    # top/bottom breathing room
        self._font = mono_font(11)
        self._scrollback_limit = 5000
        self.setFont(self._font)

        # Start from the shared theme so a freshly built widget is never
        # stuck on a hardcoded palette.
        self._theme = "dark"
        self._bg = QtGui.QColor()
        self._fg = QtGui.QColor()
        self._cursor_color = QtGui.QColor()
        self._selection_bg = QtGui.QColor()
        self._selection_fg = QtGui.QColor()
        self._ansi = ansi_palette("dark")
        self.apply_palette(get_palette("dark"))

        self._screen: _Screen | None = None
        self._stream: pyte.Stream | None = None
        self._channel = None  # paramiko.Channel, optional

        self._input_callback: Callable[[bytes], None] | None = None
        # 终端查询被拆包时留着和下一块拼起来
        self._query_tail: bytes = b""
        #: 通道被传输占着时为 True，:meth:`_send` 一律拒发。见那里的说明。
        self._input_blocked = False
        self._close_callback: Callable[[], None] | None = None
        self._resize_callback: Callable[[int, int], None] | None = None
        self._tab_callback: Callable[[], None] | None = None

        # Selection state
        self._sel_anchor: tuple[int, int] | None = None  # (x, y) screen coords
        self._sel_cur: tuple[int, int] | None = None
        self._selecting = False
        self._mouse_pressed = False
        self._scroll_offset = 0  # > 0 = scrolled into history
        # 跨块 UTF-8 增量解码器（见 _decode）。**必须在 __init__ 里建好** ——
        # 缺了它，`_decode` 里那句 `if self._dec is None` 会抛 AttributeError，
        # 而 write() 把异常吞掉，结果是**屏幕上什么都没有且不报错**。
        self._dec = None

        # 可视滚动条。**浮层，不进布局。** 放进 QHBoxLayout 当兄弟控件会吃掉
        # 宽度，而 `_recompute_grid` 是 `(width - pad_x*2) // char_w`，实测
        # 12px 的条正好掉 1 列 → 触发 `resize_pty` → 远端已输出的内容 reflow。
        # 用户只是滚了一下历史，不该改列数。
        # 位置落在 `pad_x` 那 12px 右边距里，不压字（实测 x=790 ≥ 文本右界 788）。
        self._sb = QtWidgets.QScrollBar(QtCore.Qt.Orientation.Vertical, self)
        # **光标必须自己设，否则会白拿终端的 I-beam。** Qt 的
        # `QWidget::cursor()` 在本控件没有显式光标时，会沿父链往上找**最近一个**
        # 显式设过光标的祖先 —— 滚动条是终端的子控件，而下面 `__init__` 末尾有
        # `self.setCursor(IBeamCursor)`（终端里选文本就该是 I 型），于是滚动条
        # 把那个 I 型继承了去，拖条时指针显示成文本编辑的 I 型。
        # 代价是对称的：改本控件的光标必须同时改这里，反过来也一样。
        self._sb.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        # **方向是反的，必须显式翻转。** `_scroll_offset` 的语义是「离底部
        # 多少行」（0 = 在底部），而 QScrollBar 的 value 是「越大越靠下」。
        # 不处理就是「拖到底 = 跳到历史最老」，和用户预期正好反。
        self._sb.setInvertedAppearance(True)
        self._sb.setSingleStep(_WHEEL_STEP)
        self._sb.valueChanged.connect(self._on_sb_value)
        # 上次同步时的历史行数。write() 每块输出都调一次，靠它避免无脑同步。
        self._sb_history_len = 0
        self._sync_sb()

        self.setMouseTracking(True)
        # **度量必须先于 `_set_size`。** 原来顺序是 `_set_size(80, 24)` →
        # `_init_screen` → `_update_metrics()`，于是 `_set_size` 里那句
        # `setMinimumSize` 用的是 `__init__` 开头写死的占位值 `_char_h = 16`，
        # 而真实字高是 17 —— 80×24 的下限被算成 400px 而不是 424px。
        # `_recompute_grid` 随后会按真实字高重算行数，所以看不到内容被裁，
        # 只是「最小尺寸」这个下限本身没生效（窗口能被拖得比 80×24 还小）。
        # 先量后设，下限才是真的下限。
        self._update_metrics()
        self._set_size(80, 24)
        self._init_screen(80, 24)
        self.setCursor(QtCore.Qt.CursorShape.IBeamCursor)

    def apply_palette(self, palette) -> None:
        """Adopt a theme palette. Called on construction and on theme switch."""
        from .design.v1.palette import (
            Palette,  # local import to avoid a cycle at module load
        )
        assert isinstance(palette, Palette)
        self._theme = palette.name
        self._bg = QtGui.QColor(palette.term_bg)
        self._fg = QtGui.QColor(palette.term_fg)
        self._cursor_color = QtGui.QColor(palette.term_cursor)
        self._selection_bg = QtGui.QColor(palette.term_selection_bg)
        self._selection_fg = QtGui.QColor(palette.term_selection_fg)
        self._ansi = ansi_palette(palette.name)
        # ansi 调色板刚换过，_HEX_CACHE 的键里没有它，必须清空，否则深浅切换
        # 之后终端还在用上一套主题的颜色。
        _clear_color_caches()
        self.update()

    # ------------------------------------------------------------------ public

    def bind_channel(self, channel, on_close: Callable[[], None] | None = None,
                     manage_notifier: bool = True) -> None:
        """Bind a paramiko channel; reads are pulled asynchronously.

        Set *manage_notifier* to False when the owner already watches the
        channel fd itself. Two QSocketNotifiers on one socket both fire and
        race for the same bytes: the loser reads b"" and reports a spurious
        disconnect, so exactly one notifier may exist per channel.
        """
        self._channel = channel
        self._close_callback = on_close
        self._input_callback = None
        self.reset_terminal()

        if not manage_notifier:
            self._notifier = None
            return

        # QSocketNotifier is the cleanest way to bridge Unix file descriptors
        # to Qt's event loop. paramiko's Channel exposes fileno().
        try:
            notifier = QtCore.QSocketNotifier(
                channel.fileno(),
                QtCore.QSocketNotifier.Type.Read,
                self,
            )
            notifier.setEnabled(True)
            notifier.activated.connect(self._on_channel_ready)
            self._notifier = notifier
        except Exception:
            self._notifier = None

    def set_input_handler(self, callback: Callable[[bytes], None]) -> None:
        """覆盖「按键 -> 通道」这条默认桥接。

        **零调用者，而且 docstring 原来那句「used by lrzsz」是假的。**
        ZMODEM 走的是 `set_input_blocked()` 那个闸门加 `_send()`，
        并没有把「输入桥」整个换掉。要真接上 lrzsz 的 console 复用，
        得先有人调它。
        """
        self._input_callback = callback

    def set_tab_handler(self, callback: Callable[[], None] | None) -> None:
        """Intercept Tab (used to add a local completion fallback)."""
        self._tab_callback = callback

    def input_line(self) -> tuple[str, int]:
        """Return ``(text, cursor_col)`` for the line the cursor sits on.

        Only the visible row is inspected, which is what a shell prompt line
        needs -- long wrapped commands are rare and the remote still owns the
        authoritative state.
        """
        if not self._screen:
            return "", 0
        y = self._screen.cursor.y
        row = self._screen.display[y] if 0 <= y < len(self._screen.display) else ""
        return row.rstrip(), self._screen.cursor.x

    def screen_rows(self) -> tuple[list[str], int]:
        """可见屏幕的**纯文本**行，加上光标所在行号。

        **不复用 `_capture()`。** 它给的是带 SGR 转义、用来回放给 pyte 的那份，
        这里的消费者是人眼级判断（`main_window` 靠它认出用户刚敲的命令），
        转义串混进来只会把「这一行是不是命令」这件事判错。

        只取**可见**行：滚动缓冲区里那些更早的行离当前等待点更远，看它们
        只会把上一条 `sz` 认成这一次的。

        宽字符的占位格 ``data == ""`` 必须输出空串而不是空格 —— 同一个坑见
        `_capture()` 里那段注释。
        """
        if not self._screen:
            return [], 0
        default = self._default_char()
        has_buffer = hasattr(self._screen, "buffer")
        rows: list[str] = []
        for row, line_text in enumerate(self._screen.display):
            char_dict = self._screen.buffer.get(row, {}) if has_buffer else {}
            cells: list[str] = []
            for col in range(len(line_text)):
                ch = char_dict.get(col) or default
                raw = ch.data
                cells.append(" " if (not raw or raw == "\x00") else raw)
            rows.append("".join(cells).rstrip())
        return rows, self._screen.cursor.y

    def _decode(self, data: bytes) -> str:
        """把通道字节解成文本。

        **不能无条件 ``errors="replace"``。** 实测：远端用非 UTF-8 字符集
        （比如 Latin-1）输出字符表时 —— 就是用户截的那一串 0x21~0x7E 后面
        接着 0xA0~0xF7 —— ASCII 段完好，非 ASCII 段**整段变成 U+FFFD**；
        而字体没有 U+FFFD 的字形，界面上就是**一片豆腐块**。真实终端会
        按当前字符集把它们正常显示，复制出来也完全正常 —— 正是「复制出来
        没问题、显示是乱的」这个现象。

        所以改成：**先按 UTF-8 增量解，失败的那一段退回 latin-1。**
        latin-1 把 0x00~0xFF 一一对应到 U+0000~U+00FF，**永不失败、
        也不丢字节**，最坏结果是显示成「读不懂的那几个字符」，而不是
        整齐的方块。

        **必须是增量解码器。** UTF-8 的一个多字节字符可能被 TCP 拆包，
        逐块独立解码会把一个汉字劈成两半 —— 那比替换字符更难看。
        """
        if self._dec is None:
            self._dec = codecs.getincrementaldecoder("utf-8")()
        try:
            return self._dec.decode(data)
        except UnicodeDecodeError:
            # **关键：把还没解出来的那几个字节留在解码器里**，别丢 ——
            # 它们可能是被拆包的多字节字符的前半截。
            tail = self._dec.getstate()[0]
            self._dec.reset()
            # 坏的那一段连同残留一起按 latin-1 走：1 字节 = 1 字符，不丢。
            return (tail + data).decode("latin-1")

    def set_close_handler(self, callback: Callable[[], None]) -> None:
        self._close_callback = callback

    def set_resize_handler(self, callback: Callable[[int, int], None]) -> None:
        self._resize_callback = callback

    def write(self, data: bytes) -> None:
        """Feed raw terminal data from the remote side."""
        if not self._stream or not self._screen:
            return
        # **先把终端查询挑出来应答掉，剩下的才喂给 pyte。**
        # 不挑的话 pyte 会把 ESC[c / ESC[6n 当可见内容画出来，而远端那边
        # 干等一个永远不来的答复。
        data = self._answer_queries(data)
        if not data:
            self.update()
            return
        # HistoryScreen is a write-only target; pyte writes through it
        try:
            self._stream.feed(self._decode(data))
        except Exception:
            pass
        # **内容长了，滚动条的范围就得跟着长。** 只在真的变了才同步 ——
        # write() 每一块远端输出都调一次，这里是无脑调用的热路径。
        n = self._history_len()
        if n != self._sb_history_len:
            self._sync_sb_range()
        # Coalesce repaints.
        self.update()

    # -------------------------------------------------- 终端查询（DA / DSR）
    #
    # **不回话，TUI 就没法开工。** 很多 TUI 一启动先问终端"你是什么型号、
    # 支持哪些能力"，等一个答复；等不到就判定"不认识这台终端"，于是走
    # 兜底分支——把自己的字符集/能力清单当普通文本打到屏幕上，而且每次
    # 重绘顶栏就再打一遍。用户看到的就是"页面变型 + 顶部多出很多行数据"。
    #
    # 查询可能被 TCP 拆包，所以尾巴要留着和下一块拼起来；反过来，**普通输出
    # 后面紧跟着查询**这种也不能漏，所以尾巴在"这批数据不是查询开头"时就丢掉。
    _QUERY_TAIL_MAX = 32

    def _answer_queries(self, data: bytes) -> bytes:
        """挑出终端查询、逐个应答，剩下的原样返回给 pyte。"""
        if not data:
            return b""
        buf = self._query_tail + data
        out = bytearray()
        i = 0
        n = len(buf)
        while i < n:
            esc = buf.find(b"\x1b", i)
            if esc < 0:
                out += buf[i:]
                i = n
                break
            out += buf[i:esc]
            reply = self._match_query(buf, esc)
            if reply is None:            # 不是查询，原样留给 pyte
                out += b"\x1b"
                i = esc + 1
                continue
            if reply[0] == "incomplete":
                # **查询被 TCP 拆包了。** 注意 out 里此刻只有 ESC **之前**的
                # 正常输出（ESC 还没追加进去），所以这里不用回退 out——
                # 早先写成 out = out[:len(out) - (esc - i)]，把 abc 这种
                # 真输出一起截掉了。直接跳到尾巴就够了。
                i = esc
                break
            consumed, answer = reply
            if answer and self._channel is not None and not self._input_blocked:
                # 回话**也要看传输闸门**：查询是远端在等，不回它就卡在那；
                # 但闸门开着时通道属于 sz/rz，这时候写进去会被当成载荷。
                try:
                    self._channel.sendall(answer)
                except Exception:
                    pass
            i = esc + consumed
        # 结尾留一小段尾巴给"查询被拆包"用；不是查询开头就不留。
        tail_candidate = bytes(buf[i:])
        if tail_candidate.startswith(b"\x1b") and \
                len(tail_candidate) <= self._QUERY_TAIL_MAX:
            self._query_tail = tail_candidate
        else:
            self._query_tail = b""
        return bytes(out)

    def _match_query(self, buf: bytes, i: int):
        """``buf[i]`` 起是不是一条终端查询。

        返回 ``(consumed, answer)``；**还没收全**时返回 ``("incomplete", b"")``，
        调用方据此把已经吐出去的 ESC 收回来。
        """
        n = len(buf)
        if i + 1 >= n:
            return "incomplete", b""
        c = buf[i + 1:i + 2]
        # CSI 类
        if c == b"[":
            j = i + 2
            while j < n and not (0x40 <= buf[j] <= 0x7E):
                j += 1
            if j >= n:
                return "incomplete", b""        # 还没收全
            body = buf[i + 2:j]
            final = buf[j:j + 1]
            if final == b"c" and not body.startswith(b">"):
                # DA1：报成 VT220 + 132 列 + 彩色，和 xterm-256color 对得上
                return (j - i + 1, b"\x1b[?62;1;2;6;9;15;22c")
            if final == b"c" and body.startswith(b">"):
                # DA2：VT220
                return (j - i + 1, b"\x1b[>0;95;0c")
            if final == b"n" and body in (b"6", b"?"):
                # DSR 光标位置 / 设备状态。TUI 靠它定位光标，也靠它判断
                # 屏幕是不是真的在应答。
                row = self._screen.cursor.y + 1 if self._screen else 1
                col = self._screen.cursor.x + 1 if self._screen else 1
                return (j - i + 1, b"\x1b[%d;%dR" % (row, col))
            if final == b"n" and body == b"5":
                return (j - i + 1, b"\x1b[0n")
            if final == b"u" and body in (b"?", b""):
                # kitty 键盘协议查询：不支持就照 xterm 回一个空的
                return (j - i + 1, b"\x1b[?0u")
            return None
        # ESC ( 0 / ESC ) 0：字符集切换。认下来但不回话（xterm 也这么做）
        if c in (b"(", b")") and i + 2 < n and buf[i + 2:i + 3] == b"0":
            return 3, b""
        return None

    def reset_terminal(self) -> None:
        cols, rows = self._cols, self._rows
        self._init_screen(cols, rows)

    def set_font(self, font: QtGui.QFont) -> None:
        self._font = font
        self.setFont(font)
        self._update_metrics()
        # **字符尺寸变了，行列数必须跟着重算。**
        # `rows` 是 `_recompute_grid` 用**当时的** `_char_h` 从控件高度算出来的，
        # 而绘制时每一行占的是**当前**的 `_char_h`，最后一行落在
        # `oy + rows*char_h`。两者不同步时那一行就画到了控件下沿之外 ——
        # 现象是内容超过窗口后**底部被裁掉若干行**，字号调得越大丢得越多，
        # 而 `resizeEvent` 不会来救：终端在布局里，字号变了控件尺寸常常不变，
        # 于是压根不触发 resize，`_recompute_grid` 也就不会跑。
        self._recompute_grid()
        self._set_size(cols=self._cols, rows=self._rows)
        self.update()

    def set_scrollback(self, lines: int) -> None:
        """设置**实际保留**多少行历史，而不只是滚轮能回滚多远。

        原来这里只改 `_scrollback_limit`（滚轮上限），而 pyte 的
        `HistoryScreen(history=5000)` 是 `_init_screen` 里**写死**的 ——
        于是用户在设置里把滚动缓冲调到 200000 行，滚轮上限是变了，
        **实际还是只留 5000 行**，往上滚一点就没了。设置形同虚设。

        现在两边一起改：`_scrollback_limit` 决定滚轮能走多远，
        `screen.history` 决定真的留多少。用 `_replace` 带新 maxlen 重建
        那两个 deque，**还留得下的内容原样保留**（缩小时只丢多出来的部分），
        不会因为改了设置就把当前会话的屏幕内容清空。
        """
        try:
            lines = max(0, int(lines))
        except (TypeError, ValueError):
            lines = 5000
        self._scrollback_limit = lines
        self._scroll_offset = min(self._scroll_offset, lines)
        screen = getattr(self, "_screen", None)
        if screen is not None:
            import collections
            h = screen.history
            screen.history = h._replace(
                top=collections.deque(h.top, maxlen=lines),
                bottom=collections.deque(h.bottom, maxlen=lines),
                size=lines,
            )
        # 上限变了，滚动条的 range 必须跟着走 —— 不同源就会出现
        # 「滑块已经到底，但 _scroll_offset 没到底」的错位。
        sb = getattr(self, "_sb", None)
        if sb is not None:
            self._sync_sb_range()
        self.update()

    def set_theme(self, dark: bool) -> None:
        """Legacy shim -- kept so older callers keep working."""
        self.apply_palette(get_palette("dark" if dark else "light"))

    # ------------------------------------------------------------------ sizes

    def _update_metrics(self) -> None:
        fm = QtGui.QFontMetrics(self._font)
        self._char_w = max(1, fm.horizontalAdvance("M"))
        self._char_h = max(1, fm.height())
        # Baseline offset within a cell: center the glyphs vertically.
        self._ascent = fm.ascent() + (self._char_h - fm.height()) // 2

    def sizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(
            self._char_w * 80 + self._pad_x * 2,
            self._char_h * 24 + self._pad_y * 2,
        )

    def _set_size(self, cols: int, rows: int) -> None:
        # Snap widget size to a whole number of cells plus padding.
        w = max(1, self._char_w) * cols + self._pad_x * 2
        h = max(1, self._char_h) * rows + self._pad_y * 2
        self.setMinimumSize(w, h)
        self.resize(w, h)

    def _recompute_grid(self) -> None:
        """Resize the underlying screen when the widget size changes.

        Uses ``pyte.Screen.resize`` rather than building a fresh screen:
        re-creating it would silently discard everything already rendered, so a
        window resize would wipe the output of whatever the user just ran.
        """
        if self._char_w <= 0 or self._char_h <= 0 or self._screen is None:
            return
        w = max(20, (self.width() - self._pad_x * 2) // self._char_w)
        h = max(5, (self.height() - self._pad_y * 2) // self._char_h)
        if (w, h) == (self._cols, self._rows):
            return
        self._cols, self._rows = w, h
        self._resize_screen(w, h)
        self.update()
        if self._channel is not None and self._resize_callback:
            self._resize_callback(w, h)

    # ------------------------------------------------------------- resizing
    #
    # pyte's own HistoryScreen.resize() shrinks the height by deleting lines
    # from the *top* of the screen, which erases exactly the command output the
    # user just ran whenever the window gets shorter. We therefore snapshot
    # the visible cells (attributes included) and replay them into a fresh
    # screen, so a resize is visually lossless.

    _ATTR_KEYS = ("fg", "bg", "bold", "italics", "underscore", "reverse")

    def _sgr(self, ch) -> str:
        """Build the SGR escape that reproduces *ch*'s appearance."""
        parts = ["0"]
        if ch.bold:
            parts.append("1")
        if ch.italics:
            parts.append("3")
        if ch.underscore:
            parts.append("4")
        if ch.reverse:
            parts.append("7")
        for key, base in (("fg", 38), ("bg", 48)):
            frag = _sgr_color(base, getattr(ch, key, "default"))
            if frag:
                parts.append(frag)
        return "\x1b[" + ";".join(parts) + "m"

    def _capture(self) -> list[str]:
        """Snapshot the visible screen as SGR-annotated text lines."""
        if self._screen is None:
            return []
        default = self._default_char()
        out: list[str] = []
        for row, line_text in enumerate(self._screen.display):
            char_dict = self._screen.buffer.get(row, {}) if hasattr(self._screen, "buffer") else {}
            pieces: list[str] = []
            state = None
            for col in range(len(line_text)):
                ch = char_dict.get(col) or default
                raw = ch.data
                # **宽字符（中文/全角）在 pyte 里占两格**：头格放字，紧跟
                # 那一格 data="" 当占位。拿空格顶它，_capture() 就不忠实了 ——
                # 而 _resize_screen() 恰恰是拿 _capture() 的结果回灌进 pyte，
                # 于是**每缩放一次，汉字之间就永久多一个空格**。实测连缩 4 次：
                # 「中文宽度测试」变成「中    文    宽    度    测    试」。
                # 占位格就该什么都不输出。
                # 选中复制走的是 pyte 自己的 display（见 selected_text），
                # 本来就没有这个问题，所以只这两处要改。
                if raw == "":
                    data = ""
                elif raw and raw != "\x00":
                    data = raw
                else:
                    data = " "
                key = tuple(getattr(ch, k, None) for k in self._ATTR_KEYS)
                if key != state:
                    pieces.append(self._sgr(ch))
                    state = key
                pieces.append(data)
            # Drop the trailing padding: replayed verbatim it would push the
            # restored cursor to the far right of the line instead of just
            # past the last visible glyph. (Must happen *before* appending the
            # reset -- rstrip cannot see through a trailing "\x1b[0m".)
            out.append("".join(pieces).rstrip(" ") + "\x1b[0m")
        return out

    def _resize_screen(self, cols: int, rows: int) -> None:
        if self._screen is None or self._stream is None:
            self._init_screen(cols, rows)
            return
        # **先把 scrollback 存下来。** _init_screen 建的是一个全新 screen，
        # `history.top` 会被整个丢掉 —— 而 `_capture()` 只抓**当前可见**的那些行。
        # 结果就是：改一次字号 / 拉一次窗口，滚轮就没东西可滚了，用户看到的是
        # 「刚才 cat 出来的内容一下就滚不动了」。这是加 _recompute_grid 到
        # set_font 时带进来的：以前改字号不动网格，所以不走这条路。
        old_history = self._screen.history
        saved = self._capture()
        self._init_screen(cols, rows)
        if old_history.top:
            import collections
            self._screen.history = self._screen.history._replace(
                top=collections.deque(old_history.top,
                                     maxlen=self._scrollback_limit),
                bottom=collections.deque(old_history.bottom,
                                         maxlen=self._scrollback_limit),
                size=self._scrollback_limit,
            )
        if not saved:
            return
        # Drop trailing blank rows first: a freshly used terminal keeps its
        # output at the TOP with empty rows below, so blindly taking the last
        # `rows` lines would keep all the padding and discard the content.
        while saved and not saved[-1].strip(_SGR_CHARS + " "):
            saved.pop()
        if not saved:
            return
        keep = saved[-rows:]
        self._stream.feed("\r\n".join(keep))
        self._scroll_offset = 0
        self.update()

    def _init_screen(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        # 用**设置里的**保留量，不是写死的 5000。`set_scrollback` 同时改
        # 滚轮上限和这里，两边必须同源，否则又变成「上限能设、留不下」。
        self._screen = _Screen(cols, rows, history=self._scrollback_limit)
        self._stream = pyte.Stream(self._screen)
        # pyte uses unicode; HistoryScreen writes chars; cursor lives in self._screen.cursor
        self._scroll_offset = 0
        # Note: redraws are triggered by the owning widget via update() after write()
        self.update()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._recompute_grid()
        self._place_sb()

    # ------------------------------------------------------------------ input

    def _send(self, data: bytes) -> bool:
        """把输入字节送到通道。**返回是否真的送出去了。**

        ``_input_blocked`` 是**唯一**的收口：ZMODEM 跑的时候通道属于
        ``sz``/``rz``，这时候任何按键、粘贴、补全都不得往里写——写进去的
        字节会被当成传输载荷。之前这个闸门只做在片段库那一条路上，于是
        Tab、Ctrl+V、随便敲个字符都能从别的门溜进去（评审实测：传输期按
        Tab 会把 ``b"\\t"`` 送进载荷流）。一个收口比 N 个补丁可靠。
        """
        if self._input_blocked:
            return False
        if self._input_callback:
            self._input_callback(data)
            return True
        if self._channel is not None and not self._channel.closed:
            try:
                self._channel.sendall(data)
                return True
            except Exception:
                return False
        return False

    def set_input_blocked(self, blocked: bool) -> None:
        """传输期间锁住输入。**由 SessionTab 跟着通道归属调。**

        这里必须连**按键**一起挡，而不只是挡 :meth:`_send`：传输中按 Tab
        时补全逻辑还会去读屏幕内容、回发退格，全是同一条通道上的操作。
        """
        self._input_blocked = bool(blocked)

    # xterm modifier parameter: 1 + shift(1) + alt(2) + ctrl(4)
    _MOD_BITS = (
        (QtCore.Qt.KeyboardModifier.ShiftModifier, 1),
        (QtCore.Qt.KeyboardModifier.AltModifier, 2),
        (QtCore.Qt.KeyboardModifier.ControlModifier, 4),
    )

    def _xterm_mod(self, mods) -> int:
        """1 when unmodified (omit the field), else the xterm modifier sum."""
        n = 1
        for flag, bit in self._MOD_BITS:
            if mods & flag:
                n += bit
        return n

    def _cursor_seq(self, final: str, mods) -> bytes | None:
        """CSI sequence for an arrow/Home/End key, honouring Ctrl/Alt/Shift.

        readline needs the modifier field to tell `\\x1b[D` (move one) apart
        from `\\x1b[1;5D` (move one word); without it Ctrl+Left behaves like a
        plain Left and editing around a completion goes wrong.
        """
        n = self._xterm_mod(mods)
        if n == 1:
            return f"\x1b[{final}".encode()
        return f"\x1b[1;{n}{final}".encode()

    def event(self, e: QtCore.QEvent) -> bool:
        """Swallow Tab before QWidget can spend it on focus traversal.

        QWidget::event() runs `focusNextPrevChild()` for Tab/Backtab *before*
        keyPressEvent, so in a window that has any other focusable widget (the
        session toolbar has buttons and a line edit) Tab moved focus away and
        never reached the completion path. Handling it here keeps Tab ours.
        """
        if e.type() == QtCore.QEvent.Type.KeyPress:
            if e.key() in (QtCore.Qt.Key.Key_Tab, QtCore.Qt.Key.Key_Backtab):
                self.keyPressEvent(e)
                return True
        return super().event(e)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        text = event.text()
        key = event.key()
        mods = event.modifiers()

        # Shift+Tab must reach the remote as Backtab (ESC [ Z) so bash/zsh can
        # cycle candidates; the plain Tab branch below would otherwise swallow it.
        if key == QtCore.Qt.Key.Key_Backtab or (
            key == QtCore.Qt.Key.Key_Tab and mods & QtCore.Qt.KeyboardModifier.ShiftModifier
        ):
            self._send(b"\x1b[Z")
            return

        if key == QtCore.Qt.Key.Key_Return or key == QtCore.Qt.Key.Key_Enter:
            self._send(b"\r")
            return
        if key == QtCore.Qt.Key.Key_Backspace:
            if mods & QtCore.Qt.KeyboardModifier.ControlModifier:
                self._send(b"\x08")          # readline: kill previous word
            elif mods & QtCore.Qt.KeyboardModifier.AltModifier:
                self._send(b"\x1b\x7f")      # meta-DEL
            else:
                self._send(b"\x7f")
            return
        if key == QtCore.Qt.Key.Key_Tab:
            if self._tab_callback is not None:
                self._tab_callback()
            else:
                self._send(b"\t")
            return
        if key == QtCore.Qt.Key.Key_Escape:
            self._send(b"\x1b")
            return
        if key == QtCore.Qt.Key.Key_Up:
            self._send(self._cursor_seq("A", mods)); return
        if key == QtCore.Qt.Key.Key_Down:
            self._send(self._cursor_seq("B", mods)); return
        if key == QtCore.Qt.Key.Key_Right:
            self._send(self._cursor_seq("C", mods)); return
        if key == QtCore.Qt.Key.Key_Left:
            self._send(self._cursor_seq("D", mods)); return
        if key == QtCore.Qt.Key.Key_Home:
            self._send(self._cursor_seq("H", mods)); return
        if key == QtCore.Qt.Key.Key_End:
            self._send(self._cursor_seq("F", mods)); return
        if key == QtCore.Qt.Key.Key_Delete:
            self._send(self._cursor_seq("3~", mods)); return
        if key == QtCore.Qt.Key.Key_Insert:
            self._send(b"\x1b[2~"); return
        if key == QtCore.Qt.Key.Key_PageUp:
            self._send(b"\x1b[5~"); return
        if key == QtCore.Qt.Key.Key_PageDown:
            self._send(b"\x1b[6~"); return
        if key == QtCore.Qt.Key.Key_F1: self._send(b"\x1bOP"); return
        if key == QtCore.Qt.Key.Key_F2: self._send(b"\x1bOQ"); return
        if key == QtCore.Qt.Key.Key_F3: self._send(b"\x1bOR"); return
        if key == QtCore.Qt.Key.Key_F4: self._send(b"\x1bOS"); return
        if key == QtCore.Qt.Key.Key_F5: self._send(b"\x1b[15~"); return
        if key == QtCore.Qt.Key.Key_F6: self._send(b"\x1b[17~"); return
        if key == QtCore.Qt.Key.Key_F7: self._send(b"\x1b[18~"); return
        if key == QtCore.Qt.Key.Key_F8: self._send(b"\x1b[19~"); return
        if key == QtCore.Qt.Key.Key_F9: self._send(b"\x1b[20~"); return
        if key == QtCore.Qt.Key.Key_F10: self._send(b"\x1b[21~"); return
        if key == QtCore.Qt.Key.Key_F11: self._send(b"\x1b[23~"); return
        if key == QtCore.Qt.Key.Key_F12: self._send(b"\x1b[24~"); return

        # **AltGr 必须抢在下面 Ctrl+C / Ctrl+letter 两支前面。**
        #
        # 各平台上 AltGr 报成什么组合并不统一：Windows 报成 Ctrl+Alt
        # （Qt 的 GroupSwitch 只在 X11 出现），macOS 是 AltGraph 修饰位，
        # Linux/Wayland 上常见 Ctrl+Alt 或 AltGraph。共同点是**它不是 Ctrl**，
        # 用户是在要一个字符，不是要 ^Q。
        #
        # 原来「Ctrl + letter」那一支只看 Ctrl 位、不看 Alt 也不看 text()，
        # 于是德语布局的 AltGr+Q('@') 被算成 Q-0x40=0x11 发了出去；
        # 而下面 Alt+词的分支又显式排除了 Ctrl —— 两支之间没有缝。
        #
        # **判据是「有没有可打印的 text()」，不是「有没有按到键」。**
        # 真·Ctrl+Q 在 Qt 里 text() 是 \x11（控制码），落到下面照旧发 0x11，
        # 一行为都不变；只有当平台确实吐出了一个可打印字符时，才按字符发。
        # 所以纯 Ctrl、纯 Alt、Ctrl+Alt+方向键这些既有行为都不会被改到。
        if (mods & QtCore.Qt.KeyboardModifier.ControlModifier
                and mods & QtCore.Qt.KeyboardModifier.AltModifier
                and text and all(ch >= " " for ch in text)):
            self._send(text.encode("utf-8"))
            return

        # Ctrl+C / Ctrl+V. In a shell Ctrl+C is SIGINT, so it only copies when
        # something is actually selected -- making it copy unconditionally
        # would cost the user their interrupt, which is the single most-used
        # keystroke in an SSH session. Ctrl+Shift+C is the unambiguous copy.
        if mods & QtCore.Qt.KeyboardModifier.ControlModifier:
            shift = bool(mods & QtCore.Qt.KeyboardModifier.ShiftModifier)
            if key == QtCore.Qt.Key.Key_C and shift:
                self.copy_selection()
                return
            if key == QtCore.Qt.Key.Key_V:
                # Ctrl+Shift+V lands here too; both paste.
                self.paste_clipboard()
                return
            if key == QtCore.Qt.Key.Key_C:
                if self.has_selection():
                    self.copy_selection()
                    return
                self._send(b"\x03")          # SIGINT / ETX
                return

        # Ctrl + letter
        if mods & QtCore.Qt.KeyboardModifier.ControlModifier and key >= 0x41 and key <= 0x5a:
            self._send(bytes([key - 0x40]))
            return

        # Alt / Meta word motions readline expects (ESC b / ESC f)
        if mods & QtCore.Qt.KeyboardModifier.AltModifier and not mods & QtCore.Qt.KeyboardModifier.ControlModifier:
            low = chr(key).lower() if 0x41 <= key <= 0x5A or 0x61 <= key <= 0x7A else ""
            if low == "b":
                self._send(b"\x1bb"); return
            if low == "f":
                self._send(b"\x1bf"); return
            if low == "d":
                self._send(b"\x1bd"); return

        if text:
            self._send(text.encode("utf-8"))

    def inputMethodEvent(self, event: QtGui.QInputMethodEvent) -> None:
        commit = event.commitString()
        if commit:
            self._send(commit.encode("utf-8"))
        # **提交串发得出去了，但拼音阶段的 preedit 一个像素都不显示。**
        #
        # 原来这里写的是「Preedit handled by QPainter via the standard path」——
        # 而那条「标准路径」恰恰是 `QWidget::inputMethodEvent`：Qt 默认会
        # `update()` 并由 `paintEvent` 画出 `preeditString()`。本类**整个覆写**了
        # `inputMethodEvent`（从不调 `super()`）、又**整个覆写**了 `paintEvent`
        # （从不画 preedit），全仓也没有 `inputMethodQuery` / `ImCursorRectangle`
        # 的覆写 —— 所以候选窗连位置提示都拿不到。
        # `WA_InputMethodEnabled` 还开着，于是拼音输入期间终端区完全没反馈，
        # 直到提交。
        # **这是已知限制，不是待办**：要画 preedit 得自己在 `paintEvent` 里
        # 调一次 `QTextLayout`，并实现 `inputMethodQuery` 给候选窗定位。
        # 在那之前，这里至少别再宣称有一条不存在的路径。

    # ------------------------------------------------------------------ mouse

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        self.setFocus(QtCore.Qt.FocusReason.MouseFocusReason)
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._selecting = True
            x, y = self._pixel_to_cell(event.position().x(), event.position().y())
            self._sel_anchor = (x, y)
            self._sel_cur = (x, y)
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        if self._selecting:
            x, y = self._pixel_to_cell(event.position().x(), event.position().y())
            self._sel_cur = (x, y)
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._selecting:
            self._selecting = False
            x, y = self._pixel_to_cell(event.position().x(), event.position().y())
            self._sel_cur = (x, y)
            if self._sel_anchor == self._sel_cur:
                # Treat as a click, clear selection
                self._sel_anchor = None
                self._sel_cur = None
            self.update()
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QtGui.QWheelEvent) -> None:
        delta = event.angleDelta().y()
        # 上界用**实际**历史行数，不是 scrollback_limit。写死上限的话，用户在
        # 只有 20 行历史时连滚 20 下就到了 5000 的假边界，滑块早撞顶了而
        # `view_lines` 那边只能拿 20 行出来 —— 条和画面对不上。
        n = self._history_len()
        if delta > 0:
            self._scroll_offset = min(n, self._scroll_offset + _WHEEL_STEP)
        elif delta < 0:
            self._scroll_offset = max(0, self._scroll_offset - _WHEEL_STEP)
        self._sync_sb()
        self.update()

    # ------------------------------------------------------------------ 滚动条

    def _place_sb(self) -> None:
        """把条放进 `pad_x` 那道右边距里，**不进布局**。

        进布局会吃掉宽度 → `_recompute_grid` 少算列 → `resize_pty` 跟着变，
        用户滚一下历史就把远端网格改了。
        """
        self._sb.setGeometry(
            self.width() - self._pad_x, self._pad_y,
            _SB_WIDTH, max(1, self.height() - self._pad_y * 2))
        self._sb.raise_()
        self._sync_sb_range()

    def _history_len(self) -> int:
        """现在**实际**有多少行历史可以往上滚。

        原来滚动条的范围直接写死 ``_scrollback_limit``（5000），于是条上界
        和真实内容完全脱钩：实测灌 50 行（历史 23 行）和灌 236 行，range 都是
        ``[0, 5000]``、滑块都是 584px 里的 18px 那一条 —— 内容从 23 行涨到
        236 行，十倍的变化在界面上**一点都看不出来**，条永远像坏的。
        """
        screen = getattr(self, "_screen", None)
        hist = getattr(screen, "history", None)
        if hist is None:
            return 0
        return min(len(hist.top), self._scrollback_limit)

    def _history_view_span(self, rows: int) -> tuple[int, int]:
        """滚回历史时，这一屏**要显示**的历史行在 `history.top` 里的区间。

        返回 ``(start, stop)``，半开区间。

        **为什么需要它。** 原来这里是先把 `history.top` 整份解成文本（几千行），
        再用 ``hist_text[-offset:]`` 切尾巴，最后把切出来的那几千行**全部**
        塞进 `view_lines` 才画。屏幕上只看得见 `rows` 行，几千行纯属白做 ——
        而且是每帧一次。

        `cat` 一个 314KB 文件（实测灌进去 5000 行历史）后拖滚动条，单帧实测：

            offset=0     78 ms
            offset=500   694 ms
            offset=3000  3896 ms
            offset=4000  5979 ms

        拖动时 `valueChanged` 连续触发，几秒一帧，事件队列永远排不空 ——
        症状就是用户报的「整个页面直接卡死」。

        区间怎么算：整个可视缓冲是 `history.top + 实时屏`；往上滚 `offset` 行
        之后，这一屏是该缓冲**末尾往上第 `offset + rows` 行到第 `offset` 行**。
        展开就是「从 `h - offset` 开始取」：

        ==================  =========================  ==================
        情形              start                        取几行
        ==================  =========================  ==================
        ``offset <= rows``  ``h - offset``               ``offset``（历史末尾）
        ``offset > rows``   ``h - offset``               ``rows``
        ``offset > h``      ``0``                        ``min(rows, h)``
        ==================  =========================  ==================

        三种情况合起来就是下面两行。**取的行数和屏幕能显示的行数一致**，与
        旧实现的可见输出逐格相同 —— 这是一次纯性能修正，不是行为变更。
        """
        top = self._screen.history.top
        h = len(top)
        start = max(0, h - self._scroll_offset)
        return start, start + min(self._scroll_offset, rows, h - start)

    def _sync_sb_range(self) -> None:
        # 范围跟着**实际**历史行数走，不再是写死的 scrollback_limit。
        n = self._history_len()
        # **顺手把 offset 夹回真实范围。** 范围一旦跟着内容变窄（set_scrollback
        # 把上限调小、或者历史被 maxlen 截掉），`_scroll_offset` 就可能大于新的
        # 上界。而 `_sync_sb` 里 setValue 会被 Qt 静默夹住、且信号是 block 掉的，
        # 于是 offset 一直挂在越界值上 —— 条和画面对不上，且永远没人纠正。
        if self._scroll_offset > n:
            self._scroll_offset = n
        self._sb.setRange(0, n)
        self._sb.setPageStep(max(1, self._rows))
        self._sb.setSingleStep(_WHEEL_STEP)
        self._sb_history_len = n
        # **没有可滚的历史就不画这条。** range [0,0] 时 Qt 会画一根占满整槽的
        # 滑块，看着像「永远滚到底」，其实是无内容可滚 —— 那是误导，不如不出现。
        # 隐藏而不是置灰：置灰的条仍然占着右边距那 12px 的视觉重量。
        self._sb.setVisible(n > 0)
        self._sync_sb()

    def _sync_sb(self) -> None:
        """把 `_scroll_offset` 推进滚动条。**回写时必须 blockSignals** ——
        否则 `valueChanged` → `_on_sb_value` → 又改 `_scroll_offset`，
        两个方向互相触发。

        `setInvertedAppearance(True)` 已经把显示方向翻过来了，所以
        value 直接等于 offset（0 = 底部 = 滑块在底部）。
        """
        sb = self._sb
        sb.blockSignals(True)
        try:
            if sb.value() != self._scroll_offset:
                sb.setValue(self._scroll_offset)
        finally:
            sb.blockSignals(False)

    def _on_sb_value(self, value: int) -> None:
        """拖条 → 改 offset。**这是双向同步里最容易漏的一半**：
        只做 `_sync_sb`（offset → 条）的话，拖条时画面纹丝不动，
        而条的位置还在变 —— 看起来像条坏了。"""
        v = max(0, min(int(value), self._history_len()))
        if v == self._scroll_offset:
            return
        self._scroll_offset = v
        self.update()

    def _pixel_to_cell(self, px: float, py: float) -> tuple[int, int]:
        x = int((px - self._pad_x) // self._char_w)
        y = int((py - self._pad_y) // self._char_h)
        return max(0, x), max(0, y)

    # ------------------------------------------------------------------ channel

    def _on_channel_ready(self, _fd: int = 0) -> None:
        ch = self._channel
        if ch is None or ch.closed:
            return
        try:
            data = ch.recv(65536)
        except Exception:
            data = b""
        if not data:
            # Channel closed
            self._teardown_channel()
            return
        self.write(data)

    def _teardown_channel(self) -> None:
        if getattr(self, "_notifier", None):
            try:
                self._notifier.setEnabled(False)
            except Exception:
                pass
            self._notifier = None
        if self._close_callback:
            try:
                self._close_callback()
            except Exception:
                pass
        self._channel = None

    # ------------------------------------------------------------------ paint

    def _default_char(self) -> pyte.screens.Char:
        """Build an empty default Char to fill in unfilled cells."""
        import pyte.screens
        return pyte.screens.Char(
            data=" ", fg="default", bg="default",
            bold=False, italics=False, underscore=False,
            strikethrough=False, reverse=False, blink=False,
        )

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, False)
        p.fillRect(self.rect(), self._bg)

        if not self._screen:
            return

        cols, rows = self._cols, self._rows
        buf = self._screen.buffer  # defaultdict[int, dict[int, Char]]

        # pyte 0.8: HistoryScreen.buffer holds `rows` lines indexed 0..rows-1.
        # When scrolled back into history, history.top (a deque of bytearrays)
        # holds older lines (text only, no attributes).

        # Build the list of (row_text, char_dict_for_attrs) for the visible area.
        # When _scroll_offset == 0 we are at the bottom (live view).
        # When _scroll_offset > 0 we have scrolled up into history.
        view_lines: list[tuple[str, dict[int, pyte.screens.Char]]] = []
        default_char = self._default_char()
        if self._scroll_offset == 0:
            # Live: use screen.display for text + buffer for attributes
            for row, line_text in enumerate(self._screen.display):
                char_dict = buf.get(row, {})
                view_lines.append((line_text, char_dict))
        else:
            # History view: combine history.top + current buffer.
            # history.top stores older lines as bytes; we just decode.
            #
            # **只解这一屏要显示的那几行。** 原来这里是 `list(history.top)` →
            # 逐行全解 → `[-offset:]` → 整份塞进 `view_lines`，上限逻辑只写在
            # 下面实时屏那个循环里（`if len(view_lines) >= rows: break`），
            # 历史这半没有 —— 于是 view_lines 长度是 `max(offset, rows)` 而不是
            # `rows`，几千行每帧照画不误。`cat` 大文件后拖滚动条必卡死，
            # 数字与复现见 `_history_view_span` 的 docstring。
            start, stop = self._history_view_span(rows)
            top = self._screen.history.top
            # islice 而不是切片：deque 的下标索引是 O(n)，切一刀就得从头走
            # 几千步；islice 只走 start+count 步。
            for raw in itertools.islice(top, start, stop):
                view_lines.append((_pad_row(_history_row_text(raw, " "),
                                            cols), {}))
            # Pad with the live view below
            for row, line_text in enumerate(self._screen.display):
                if len(view_lines) >= rows:
                    break
                view_lines.append((line_text, buf.get(row, {})))
            # Pad with empty rows if we don't have enough
            while len(view_lines) < rows:
                view_lines.append((" " * cols, {}))

        # Selection rectangles, one per row (in grid coords, y in [0, rows)).
        # **不是一整块矩形**：首行从拖动起点列起、末行到光标列止，中间每行
        # 铺满整行宽。整块矩形画出来就是用户截图里那个参差的蓝块。
        #
        # **列区间一律向 `_sel_row_span` 取，绘制和复制共用同一个。** 原来
        # 这里自己写了一遍三分支，而那个 `else` 兜住了「单行选区」——
        # 单行时上面两个条件都要求 `!= `，于是落到 else 画成**整行**，
        # 而复制走 `_sel_row_slice` 的单行分支只取选中的那几列。
        # 后果是用户拖一个词的高亮铺满整行，复制出来却是对的：
        # 「看着不像选中」与「复制结果」对不上，既没法目视确认选了哪一段，
        # 也没法靠肉眼判断该往哪边多拖一点。
        sel_rows: list[tuple[int, int, int, int]] = []
        if self._sel_anchor and self._sel_cur:
            y1, y2, xt, xb = self._sel_span()
            for row in range(y1, y2 + 1):
                left, right = self._sel_row_span(row, y1, y2, xt, xb)
                sel_rows.append((left, row, right, row + 1))
        # 按行查表，别在每格里线性扫整张列表 —— 一屏 4000 多格时那个开销是
        # 实打实的，渲染直接掉一截。
        sel_by_row = {r[1]: r for r in sel_rows}

        # Cursor (only when at the bottom / live view)
        cur_x = self._screen.cursor.x
        cur_y = self._screen.cursor.y
        # **远端要求藏光标时就真的不画。** ESC[?25l / ?25h（DECTCEM）由 pyte
        # 记在 `cursor.hidden` 上，而绘制条件原来只看「滚动偏移是不是 0」和
        # 「是不是光标那一行」—— 数据是对的，我们压根没消费，于是 vim / less /
        # htop / tmux 一发这条序列，光标块就继续压住一个字符（实测隐藏前后
        # 那一格都是 136 个光标色像素）。**光标可见性与「在哪一行」是两件事**，
        # 合成一个 show_cursor 就够了。
        cursor_visible = not self._screen.cursor.hidden

        base_font = QtGui.QFont(self._font)
        p.setFont(base_font)
        ox, oy = self._pad_x, self._pad_y

        # 这两个在整帧里是常量，原来却**每格调一次**（QColor.name() 要拼字符串）。
        fg_name = self._fg.name()
        bg_name = self._bg.name()
        # QFont 的 8 种组合缓存一次，而不是每格新建一个 QFont。
        font_cache: dict = {}
        cur_font = base_font
        cur_fg = None

        for row, (line_text, char_dict) in enumerate(view_lines):
            # If we're scrolled back into history, hide the live cursor.
            show_cursor = (self._scroll_offset == 0 and cursor_visible
                           and row == cur_y)
            for col in range(cols):
                char = char_dict.get(col, default_char)
                raw = char.data
                # 宽字符的占位格（data=""）**不要画**。画一个空格会在汉字右边
                # 凭空多出一整格空白，屏幕上看起来就是「每个汉字后面跟个空格」。
                # 理由与 _capture() 里那段一样：data="" 是 pyte 标记的
                # 「这一格被左边那个宽字符占了」，不是「这一格是空的」。
                data = "" if raw == "" else (raw or " ")
                # If we don't have attrs (history line), pull from line_text
                if not char_dict and col < len(line_text):
                    data = line_text[col] or ""

                # Resolve colors
                fg = _qcolor(_hex(char.fg, fg_name, self._ansi, bold=char.bold))
                bg = _qcolor(_hex(char.bg, bg_name, self._ansi, False))
                if char.reverse:
                    fg, bg = bg, fg

                # Apply font attrs
                if char.bold or char.italics or char.underscore:
                    fkey = (char.bold, char.italics, char.underscore)
                    f = font_cache.get(fkey)
                    if f is None:
                        f = QtGui.QFont(base_font)
                        f.setBold(char.bold)
                        f.setItalic(char.italics)
                        f.setUnderline(char.underscore)
                        font_cache[fkey] = f
                    if f is not cur_font:
                        p.setFont(f)
                        cur_font = f

                cx = ox + col * self._char_w
                cy = oy + row * self._char_h

                # Background
                if bg != self._bg:
                    p.fillRect(cx, cy, self._char_w, self._char_h, bg)

                # Selection
                srow = sel_by_row.get(row)
                if srow and srow[0] <= col < srow[2]:
                    p.fillRect(cx, cy, self._char_w, self._char_h, self._selection_bg)
                    fg = self._selection_fg

                # Foreground
                # **颜色没变就别重设 pen。** 原来每格都 `p.setPen(fg)`，而空屏
                # 上 4000 多格设的全是同一个颜色——这一下就是 4000 多次无谓的
                # Qt 调用。
                if fg is not cur_fg:
                    p.setPen(fg)
                    cur_fg = fg
                # **空格不用画。** pyte 把空白格的 data 填成 " "，它在这里
                # 是真值，所以原来**整屏每一个空格都在 drawText**——一屏 4400
                # 个格子就是 4400 次画一个什么都看不见的字符。实测真实 Windows
                # 平台上 paintEvent 要 55~63 ms，而空屏和满屏**一样慢**，因为
                # 花的力气全在这些空格上；**按键发木就是这么来的**。
                # （原文还写了「光标闪烁」，但这里没有闪烁实现，见文件上方注释。）
                #
                # 唯一例外是带下划线的空格：它下面那道线是看得见的。
                if data and (data != " " or char.underscore):
                    p.drawText(cx, cy + self._ascent, data)

            # 回到普通字体。**原来这里是每格调一次 `p.font() != base_font`**
            # ——一次 QFont 比较，换成上面那个局部变量就够了。
            if cur_font is not base_font:
                p.setFont(base_font)
                cur_font = base_font

            if show_cursor:
                p.fillRect(
                    ox + cur_x * self._char_w,
                    oy + row * self._char_h,
                    self._char_w,
                    self._char_h,
                    self._cursor_color,
                )

        p.end()

    # ------------------------------------------------------------------ copy

    def _sel_span(self) -> tuple[int, int, int, int]:
        """选区的 ``(首行, 末行, 首行起列, 末行止列)``。

        **按行取，但首行从拖动的起点列起、末行到光标所在列止**，中间每行都是
        完整一行。用户要的就是这个：「从某行任意位置开始，按行读到结束行
        光标选中的任意位置」。

        首行/末行是按**谁在上下**决定的，不是按锚点固定的：往上拖的时候
        锚点行在下、光标行在上，两者的角色要互换 —— 钉死成「锚点永远在首行」
        的话，反向拖出来的就是反的。

        绘制和复制都从这里取 —— 两边各算一遍必然漂移，而漂移的现场表现是
        「高亮的是这三行，复制出来的是另外三行」，肉眼完全看不出哪里错。
        """
        ax, ay = self._sel_anchor
        cx, cy = self._sel_cur
        if ay < cy:
            return ay, cy, ax, cx
        if ay > cy:
            return cy, ay, cx, ax
        # **同一行时列也要按「谁在左」定。** 原来这里只有 `ay <= cy` 一个
        # 判断，于是从右往左拖同一行得到的是 `(xt, xb) = (12, 0)`：
        # 复制切成 `line[12:1]` 是**空串**，高亮的 `12 <= col < 1` 是一格
        # 都不画 —— 拖了等于没拖。多行拖动没这个问题，因为首末行的角色是
        # 按行定的、与列的左右无关；只有单行把列当成了有向区间。
        return ay, ay, min(ax, cx), max(ax, cx)

    def _sel_row_span(self, row: int, y1: int, y2: int,
                      x_top: int, x_bot: int) -> tuple[int, int]:
        """选区里第 *row* 行覆盖的列区间 ``[left, right)``。

        **高亮和复制唯一的一份列区间算法。** 两边各写一遍必然漂移，而漂移
        的现场是「高亮的是整行、复制出来是那一段」—— 复制其实是对的，所以
        用户看到的是一个对不上的画面，既没法确认选了哪一段，也没法定出该往
        哪边再拖一点。

        * 单行（``y1 == y2``）→ ``x_top`` 到 ``x_bot`` 闭区间（含光标格）。
        * 首行 → 从起点列到行尾。
        * 末行 → 行首到光标列。
        * 中间行 → 整行宽。
        """
        if y1 == y2:
            # **单行必须单独判。** 不判的话下面两个分支都不命中（都要求
            # `y1 != y2`），直接落到整行 —— 原来绘制那份就是这个下场。
            return x_top, x_bot + 1
        if row == y1:
            return x_top, self._cols
        if row == y2:
            return 0, x_bot + 1
        return 0, self._cols

    def _sel_row_slice(self, line: str, row: int,
                       y1: int, y2: int, x_top: int, x_bot: int) -> str:
        """选区里第 *row* 行该取的那一段。整行取，最后统一 rstrip。"""
        left, right = self._sel_row_span(row, y1, y2, x_top, x_bot)
        return line[left:right]

    def has_selection(self) -> bool:
        """Cheap "is anything selected" test.

        :meth:`selected_text` walks the whole selection and builds the string;
        the Ctrl+C branch only needs to know whether one exists.
        """
        return bool(self._sel_anchor and self._sel_cur and self._screen)

    def selected_text(self) -> str:
        if not self.has_selection():
            return ""
        y1, y2, xt, xb = self._sel_span()
        out: list[str] = []
        if self._scroll_offset == 0:
            # Use display for text -- it's the bottom rows as strings
            for row in range(y1, y2 + 1):
                if row >= len(self._screen.display):
                    break
                # 首行从起点列起、末行到光标列止、中间整行 —— 见 _sel_span。
                # 行尾填充的空格由 rstrip 剥掉。
                out.append(self._sel_row_slice(
                    self._screen.display[row], row, y1, y2, xt, xb).rstrip(" "))
        else:
            # scrolled back: use history (text only)
            #
            # **和 paintEvent 用同一个区间、同一个解码函数。** 这一段原来
            # 是画屏那段的一份拷贝：把几千行历史全解一遍，然后只用到
            # `range(y1, y2 + 1)` 那几十行。于是「滚回历史 → 选中 → Ctrl+C」
            # 会再卡一次，症状和滚动条那次一模一样。而两份拷贝已经漂移过
            # （见 `_history_row_text` 的 docstring），所以连解码一起收口。
            start, stop = self._history_view_span(self._rows)
            hist_keep = [_history_row_text(raw, "")
                         for raw in itertools.islice(
                             self._screen.history.top, start, stop)]
            for row in range(y1, y2 + 1):
                if row < len(hist_keep):
                    line = _pad_row(hist_keep[row], self._cols)
                else:
                    live_row = row - len(hist_keep)
                    if live_row < len(self._screen.display):
                        line = self._screen.display[live_row]
                    else:
                        line = " " * self._cols
                # 同 live 路径：首尾按列、中间整行；`_pad_row` 补的满宽由 rstrip 剥掉。
                out.append(self._sel_row_slice(
                    line, row, y1, y2, xt, xb).rstrip(" "))
        # **每一行单独 `rstrip(" ")`，不是只处理最后一行。**
        #
        # 根因：pyte 的 `display[row]` **每行都补齐到屏幕宽度**，所以
        # `len(line)` 恒等于 `self._cols`，上面那个 `xe = min(x2+1, len(line),
        # self._cols)` 里的 `len(line)` 永远不生效 —— **每一行都截到同样
        # 列数**。历史那条路更明确：`_pad_row()` 直接补到满宽。
        # 于是短行的尾部空格被一起带进剪贴板，复制出来是一个**矩形块**，
        # 而不是用户以为的「一行一行」。
        #
        # **为什么不区分「程序真的打印了尾随空格」和「pyte 补的填充」：**
        # pyte 不记录「本行真实长度」，两者在 display 里逐字节相同，没有
        # 任何信息可以区分。唯一的可选做法就是全剥。
        #
        # 代价要说清楚：选中 `ls -l` / 表格这类**靠空格对齐**的输出时，
        # 行与行之间的列间空格会被剥掉，粘出来不再对齐。绝大多数终端
        # （xterm / iTerm2 / Windows Terminal）的「复制选中」也是这个取舍 ——
        # 绝大多数场景要的是内容，不是等宽矩阵。用户明确要的是按行复制。
        #
        # 只剥**行尾**：行首（`xs` 之后那段）保持原样，选区从中间起时开头
        # 的空格是有意义的。
        return "\n".join(out)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if not text:
            return
        QtWidgets.QApplication.clipboard().setText(text)
        # **复制完必须清掉选区。** Ctrl+C 那一支只看 `has_selection()`：
        # 有选区就复制、没有才发 `^C`（README 承诺的「只有确实存在选区时
        # 才会被复制行为吃掉」）。而原来复制完不动锚点，于是**复制一次
        # 之后，在下一次点击之前 Ctrl+C 全是静默重拷，`0x03` 发不出去** ——
        # 用户想中断一条跑飞的命令，结果又复制了一遍。
        self._sel_anchor = None
        self._sel_cur = None
        self.update()

    def paste_clipboard(self) -> None:
        text = QtWidgets.QApplication.clipboard().text()
        if text:
            self._send(text.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8"))
