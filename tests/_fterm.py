"""终端功能总测：按键字节 / 回滚历史 / PTY 尺寸 / 解码 / 查询应答 / 选区光标 / 像素配色。

范围：``sshclient/ui/terminal_widget.py``（主体）与 ``main_window.py`` 里终端相关的
按键字节、PTY 尺寸节流、Tab 补全接线。**不连真服务器** —— 远端"收到哪些字节"
一律用假通道（``FakeChannel``）断言。

几条自定的规矩：

* 判据钉在**屏幕上看得见 / 远端收到哪些字节**上。渲染类量像素，量像素的探针
  **先自校验**（``M2``：先在已知有内容的格子验扫描器能命中），否则「没画出来」
  和「探针瞎了」分不开。
* 判据红了先复核（源码 + 独立实测），确认是真缺陷就**保留红**，在输出里带上
  实测/期望，不把阈值改软、不 skip、不 ``if False``。
* 异步一律 ``wait_until``，没有固定 sleep 判成败。

跑法::

    $env:PYTHONIOENCODING="utf-8"; .\\.venv\\Scripts\\python.exe tests\\_fterm.py

退出码：0 = 全过；1 = 有 FAIL；2 = 无 FAIL 但有判据因前提不成立没评上。
"""
import hashlib
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# **必须先于 tests.fonts 导入**：fonts.py 里是 setdefault('offscreen')，
# 而 offscreen 下 QFontDatabase.families() 返回 0 —— 字体/像素判据会恒真。
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from tests.fonts import boot, snap  # noqa: E402

app = boot([])

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from sshclient.core import fonts as core_fonts  # noqa: E402
from sshclient.ui import main_window  # noqa: E402
from sshclient.ui import terminal_widget as TW  # noqa: E402


def _dismiss_modal():
    """SessionTab.attach 在拿不到 SFTP 时会弹模态 msgbox（QMessageBox.exec
    是个不返回的嵌套事件循环），不自动关掉的话整个脚本卡死。"""
    mw = QtWidgets.QApplication.activeModalWidget()
    if mw is not None:
        mw.close()


_DISMISS = QtCore.QTimer()
_DISMISS.timeout.connect(_dismiss_modal)
_DISMISS.start(50)

NO = QtCore.Qt.KeyboardModifier.NoModifier
CTRL = QtCore.Qt.KeyboardModifier.ControlModifier
ALT = QtCore.Qt.KeyboardModifier.AltModifier
SHIFT = QtCore.Qt.KeyboardModifier.ShiftModifier
K = QtCore.Qt.Key

PASS, FAIL, SKIP = [], [], []
_OWNED: list = []


def check(name, ok, detail=""):
    """红的就是发现。detail 只在红的时候打，方便直接抄进报告。"""
    line = "  [ok]   " + name if ok else "  [FAIL] " + name + "   <- %s" % detail
    (PASS if ok else FAIL).append(name if ok else name + "   <- " + str(detail))
    print(line)
    return bool(ok)


def skip(name, why):
    """前提不成立 —— 这条判据没评上，退出码算 2。"""
    SKIP.append(name + "   <- " + str(why))
    print("  [skip] " + name + "   <- " + str(why))
    return False


def section(title):
    print("\n=== " + title + " ===")


# ---------------------------------------------------------------------------
# 基础件：事件循环 / 按键 / 假通道
# ---------------------------------------------------------------------------
def pump(ms=40):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.004)


def wait_until(pred, ms=1500, step=0.006):
    """轮询到 pred 为真或超时。返回是否等到。"""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        if pred():
            app.processEvents()
            return True
        app.processEvents()
        time.sleep(step)
    return bool(pred())


class FakeChannel:
    """假的 paramiko.Channel。只记「远端收到了什么」，不发真流量。"""

    def __init__(self):
        self.sent = bytearray()
        self.resizes = []
        self.closed = False

    def sendall(self, data):
        self.sent.extend(data)

    def resize_pty(self, width=80, height=24, width_pixels=0, height_pixels=0):
        if self.closed:
            raise OSError("channel closed")
        self.resizes.append((width, height))

    def fileno(self):
        return -1


def new_term(width=900, height=520, font_size=None):
    w = TW.TerminalWidget()
    if font_size:
        w.set_font(core_fonts.mono_font(font_size))
    w.resize(width, height)
    w.show()
    ch = FakeChannel()
    w.bind_channel(ch, manage_notifier=False)
    w.set_resize_handler(lambda c, r: ch.resize_pty(width=c, height=r))
    pump(50)
    _OWNED.append((w, None))
    return w, ch


def press(w, key, mods=NO, text=""):
    ev = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, mods, text, False, 1)
    QtWidgets.QApplication.sendEvent(w, ev)


def wheel(w, dy):
    ev = QtGui.QWheelEvent(
        QtCore.QPointF(20.0, 20.0), QtCore.QPointF(20.0, 20.0),
        QtCore.QPoint(0, 0), QtCore.QPoint(0, dy),
        QtCore.Qt.MouseButton.NoButton, NO,
        QtCore.Qt.ScrollPhase.NoScrollPhase, False)
    QtWidgets.QApplication.sendEvent(w, ev)


def click(w, col, row):
    """单击 = 按下 + 松开。**必须成对发** —— 清选区只发生在 mouseReleaseEvent。"""
    px = w._pad_x + col * w._char_w + 1
    py = w._pad_y + row * w._char_h + 1
    QtWidgets.QApplication.sendEvent(w, QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(px, py),
        QtCore.QPointF(px, py), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton, NO))
    QtWidgets.QApplication.sendEvent(w, QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonRelease, QtCore.QPointF(px, py),
        QtCore.QPointF(px, py), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.NoButton, NO))


def drag(w, c0, r0, c1, r1):
    """左键按下 → 移动 → 松开（跨行拖选也走这条路）。"""
    def at(col, row):
        return (w._pad_x + col * w._char_w + 1,
                w._pad_y + row * w._char_h + 1)

    x, y = at(c0, r0)
    QtWidgets.QApplication.sendEvent(w, QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonPress, QtCore.QPointF(x, y),
        QtCore.QPointF(x, y), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.LeftButton, NO))
    x, y = at(c1, r1)
    QtWidgets.QApplication.sendEvent(w, QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseMove, QtCore.QPointF(x, y),
        QtCore.QPointF(x, y), QtCore.Qt.MouseButton.NoButton,
        QtCore.Qt.MouseButton.LeftButton, NO))
    QtWidgets.QApplication.sendEvent(w, QtGui.QMouseEvent(
        QtCore.QEvent.Type.MouseButtonRelease, QtCore.QPointF(x, y),
        QtCore.QPointF(x, y), QtCore.Qt.MouseButton.LeftButton,
        QtCore.Qt.MouseButton.NoButton, NO))


def clip():
    return QtWidgets.QApplication.clipboard()


def clip_set(text):
    """写剪贴板，**并确认它真的落下了**再返回。

    Windows 的剪贴板走 OLE 延迟渲染：`setText()` 只是把数据交给 Qt，
    真正落到系统剪贴板要经过一次事件循环。于是「写进去 → 立刻读回来」
    或「写进去 → 立刻让产品去粘贴」时，机器繁忙下可能读到**旧值或空串**。

    **原来的写法是 `setText()` + 一次 `pump(20)`，然后就当它落下了。**
    那是**猜**。实测 5 次里红 1 次，红的是
    `Ctrl+C 有选区`（报 `剪贴板=''`）—— 而那条断言期望的正是「产品把
    选区复制进去」。

    **真正的竞态在这里**：哨兵那次 `setText("")` 和产品那次
    `setText("hello")` **都排在 OLE 队列里**。机器繁忙时**哨兵那次
    反而后落地**，把 'hello' 覆盖成 ''。于是判据读到空串，
    看起来像「复制没生效」，实际是**上一次写入追上了**。

    所以这里改成：**反复写 + 反复读，直到读到的值就是要写的值**
    （有上限，别无限等）。写稳了，后面的写入才是最后一次落地的那次。

    **这不是产品的复制粘贴坏了** —— 选区在、`copy_selection` 也被调到了，
    只是系统没确认那次写入。
    """
    for _ in range(20):
        clip().setText(text)
        pump(20)
        if clip().text() == text:
            return
    # 实在落不下就明说，而不是让后面的判据莫名其妙地红
    raise AssertionError("剪贴板写不下去：想要 %r，实际 %r"
                         % (text, clip().text()))


def clip_wait(expected: str, tries: int = 20) -> bool:
    """等剪贴板变成 *expected*（泵事件循环），返回是否等到了。

    用于「产品刚写进去 → 判据要读回来」的场景：写入是 OLE 延迟渲染，
    读得太早可能拿到旧值。
    """
    for _ in range(tries):
        if clip().text() == expected:
            return True
        pump(20)
    return clip().text() == expected


def screen_text(term):
    return "".join(term._screen.display)


def feed(term, text):
    term.write(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# 像素探针（先自校验再用，见 M2）
# ---------------------------------------------------------------------------
def cell_rect(term, col, row, cols=1, rows=1):
    return (term._pad_x + col * term._char_w,
            term._pad_y + row * term._char_h,
            term._char_w * cols, term._char_h * rows)


def count_near(img, rect, rgb, tol=40):
    r0, g0, b0 = rgb[0], rgb[1], rgb[2]
    x0, y0, w, h = rect
    n = 0
    for y in range(max(0, y0), min(img.height(), y0 + h)):
        for x in range(max(0, x0), min(img.width(), x0 + w)):
            c = img.pixelColor(x, y)
            if abs(c.red() - r0) <= tol and abs(c.green() - g0) <= tol \
                    and abs(c.blue() - b0) <= tol:
                n += 1
    return n


def rgb(name):
    c = QtGui.QColor(name)
    return (c.red(), c.green(), c.blue())


def dominant(img, rect, bgname):
    """格子里出现最多的**非背景**色（取样足够密，只在 8x16 的小格上跑）。"""
    x0, y0, w, h = rect
    tally = {}
    for y in range(max(0, y0), min(img.height(), y0 + h)):
        for x in range(max(0, x0), min(img.width(), x0 + w)):
            nm = img.pixelColor(x, y).name()
            if nm != bgname:
                tally[nm] = tally.get(nm, 0) + 1
    if not tally:
        return ""
    return max(tally.items(), key=lambda kv: kv[1])[0]


def fingerprint(img, rect=None):
    """量化到 16 级后取指纹 —— 抗抗锯齿噪声，但配色/字形变化一定变。"""
    if rect is not None:
        img = img.copy(int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3]))
    acc = []
    for y in range(0, img.height(), 2):
        for x in range(0, img.width(), 2):
            c = img.pixelColor(x, y)
            acc.append("%x%x%x" % (c.red() >> 4, c.green() >> 4, c.blue() >> 4))
    return hashlib.md5("".join(acc).encode()).hexdigest()


# ===========================================================================
# 1. 按键 -> 字节
# ===========================================================================
def section_keys():
    section("1. 按键 -> 字节（假通道断言远端收到了什么）")
    w, ch = new_term()

    # M1 元断言：假通道本身是活的（否则后面几十条全恒假）
    ch.sent.clear()
    press(w, K.Key_A, NO, "a")
    check("M1 假通道自校验：一次按键确实收到了字节", bytes(ch.sent) == b"a",
          "实测 %r" % bytes(ch.sent))

    KEYS = [
        ("Enter", K.Key_Return, NO, "", b"\r"),
        ("Backspace", K.Key_Backspace, NO, "", b"\x7f"),
        ("Ctrl+Backspace", K.Key_Backspace, CTRL, "", b"\x08"),
        ("Alt+Backspace", K.Key_Backspace, ALT, "", b"\x1b\x7f"),
        ("Esc", K.Key_Escape, NO, "", b"\x1b"),
        ("Up", K.Key_Up, NO, "", b"\x1b[A"),
        ("Down", K.Key_Down, NO, "", b"\x1b[B"),
        ("Right", K.Key_Right, NO, "", b"\x1b[C"),
        ("Left", K.Key_Left, NO, "", b"\x1b[D"),
        ("Ctrl+Left", K.Key_Left, CTRL, "", b"\x1b[1;5D"),
        ("Alt+Left", K.Key_Left, ALT, "", b"\x1b[1;3D"),
        ("Ctrl+Home", K.Key_Home, CTRL, "", b"\x1b[1;5H"),
        ("Delete", K.Key_Delete, NO, "", b"\x1b[3~"),
        ("Insert", K.Key_Insert, NO, "", b"\x1b[2~"),
        ("PgUp", K.Key_PageUp, NO, "", b"\x1b[5~"),
        ("PgDn", K.Key_PageDown, NO, "", b"\x1b[6~"),
        ("F1", K.Key_F1, NO, "", b"\x1bOP"),
        ("F2", K.Key_F2, NO, "", b"\x1bOQ"),
        ("F3", K.Key_F3, NO, "", b"\x1bOR"),
        ("F4", K.Key_F4, NO, "", b"\x1bOS"),
        ("F5", K.Key_F5, NO, "", b"\x1b[15~"),
        ("F12", K.Key_F12, NO, "", b"\x1b[24~"),
        ("Ctrl+A", K.Key_A, CTRL, "", b"\x01"),
        ("Ctrl+D", K.Key_D, CTRL, "", b"\x04"),
        ("Ctrl+Z", K.Key_Z, CTRL, "", b"\x1a"),
        ("Alt+B", K.Key_B, ALT, "", b"\x1bb"),
        ("Alt+F", K.Key_F, ALT, "", b"\x1bf"),
        ("Alt+D", K.Key_D, ALT, "", b"\x1bd"),
        ("Tab", K.Key_Tab, NO, "\t", b"\t"),
        ("Shift+Tab(Backtab)", K.Key_Backtab, NO, "", b"\x1b[Z"),
        ("Shift+Tab(Tab+Shift)", K.Key_Tab, SHIFT, "", b"\x1b[Z"),
    ]
    for name, key, mods, text, want in KEYS:
        ch.sent.clear()
        press(w, key, mods, text)
        got = bytes(ch.sent)
        check("按键 %s -> %r" % (name, want), got == want,
              "实测 %r，期望 %r" % (got, want))

    ch.sent.clear()
    press(w, K.Key_L, NO, "l")
    press(w, K.Key_S, NO, "s")
    check("普通字符 ls -> b'ls'（逐字符，不吞不改）", bytes(ch.sent) == b"ls",
          "实测 %r" % bytes(ch.sent))

    # --- Tab 不被焦点吃掉 -------------------------------------------------
    section("1b. Tab 的焦点归属")
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    edit = QtWidgets.QLineEdit()
    term = TW.TerminalWidget()
    lay.addWidget(edit)
    lay.addWidget(term)
    host.resize(900, 600)
    host.show()
    pump(120)
    term.setFocus()
    pump(40)
    fch = FakeChannel()
    term.bind_channel(fch, manage_notifier=False)
    fch.sent.clear()
    if QtWidgets.QApplication.focusWidget() is not term:
        skip("Tab 不被焦点吞（焦点仍在终端）",
             "平台没给终端焦点（focus=%r），前提不成立"
             % QtWidgets.QApplication.focusWidget())
    else:
        press(term, K.Key_Tab)
        check("Tab 之后焦点仍在终端（没被 QWidget::event 的 focusNextPrevChild 抢走）",
              QtWidgets.QApplication.focusWidget() is term,
              "焦点跑到 %r" % QtWidgets.QApplication.focusWidget())
        check("Tab 字节确实送到了远端（没被当成焦点切换吃掉）",
              bytes(fch.sent) == b"\t", "实测 %r" % bytes(fch.sent))
    host.close()
    host.deleteLater()
    pump(20)

    # --- 复制 / 粘贴 ------------------------------------------------------
    section("1c. Ctrl+C / Ctrl+Shift+C / Ctrl+V")
    feed(w, "hello clipboard row\r\n")
    pump(20)
    clip_set("PRESET-CLIP")
    ch.sent.clear()
    press(w, K.Key_C, CTRL, "")
    check("Ctrl+C 无选区 = 0x03（SIGINT）", bytes(ch.sent) == b"\x03",
          "实测 %r" % bytes(ch.sent))
    check("Ctrl+C 无选区时没动剪贴板", clip().text() == "PRESET-CLIP",
          "剪贴板=%r" % clip().text())

    # **清空剪贴板要放在拖选之前。**
    #
    # `clip_set` 现在会反复写、反复读直到写入落地（可能泵好几百毫秒）。
    # 如果把它放在 drag 和 press **之间**，那么长一段泵事件循环里，
    # 终端的光标闪烁/刷新定时器可能触发，**把选区清掉** —— 于是产品
    # 复制到的是空串，判据读到 ''，看起来像「复制没生效」。
    #
    # 放在拖选之前就没有这个问题：拖选紧跟 press，中间没有长 pump。
    clip_set("")
    ch.sent.clear()
    drag(w, 0, 0, 4, 0)
    check("拖选后确实有选区", w.has_selection(), "has_selection()=%r" % w.has_selection())
    ch.sent.clear()
    press(w, K.Key_C, CTRL, "")
    # 复制是 OLE 延迟渲染，写入要经过一次事件循环才落地 —— 等它落地，
    # 而不是读一次就下结论（见 clip_set 的 docstring 里那段竞态分析）。
    got = clip_wait("hello")
    check("Ctrl+C 有选区 = 复制，远端一个字节都没收到",
          bytes(ch.sent) == b"" and got,
          "实测发出 %r，剪贴板=%r" % (bytes(ch.sent), clip().text()))

    ch.sent.clear()
    clip_set("UNTOUCHED")
    press(w, K.Key_C, SHIFT | CTRL, "")
    check("Ctrl+Shift+C 无条件复制（没误发 0x03）", bytes(ch.sent) == b"",
          "实测发出 %r" % bytes(ch.sent))

    # 怀疑：copy_selection 不清选区锚点，Ctrl+C 只看 has_selection()
    ch.sent.clear()
    clip_set("SENTINEL")
    press(w, K.Key_C, CTRL, "")
    check("复制过一次之后，Ctrl+C 应当发 0x03 而不是再复制一次",
          bytes(ch.sent) == b"\x03",
          "实测发出 %r，剪贴板=%r（选区锚点没被 copy_selection 清掉，"
          "has_selection() 仍为真）" % (bytes(ch.sent), clip().text()))

    # 粘贴是「当场读剪贴板」—— 产品在按键那一刻就读，所以要保证的是
    # **press 之前那次写入已经落地**。新的 `clip_set` 做到了这一点
    # （它会反复写、反复读，直到读到的就是要写的）。
    clip_set("pasted-one")
    ch.sent.clear()
    press(w, K.Key_V, CTRL, "")
    check("Ctrl+V 粘贴单行文本", bytes(ch.sent) == b"pasted-one",
          "实测 %r" % bytes(ch.sent))

    clip_set("line1\r\nline2\nline3")
    ch.sent.clear()
    press(w, K.Key_V, SHIFT | CTRL, "")
    check("Ctrl+Shift+V 粘贴，换行统一成 \\r", bytes(ch.sent) == b"line1\rline2\rline3",
          "实测 %r" % bytes(ch.sent))

    # --- AltGr ------------------------------------------------------------
    section("1d. AltGr 与 IME")
    # Windows 上 AltGr 报成 Ctrl+Alt（Qt 的 GroupSwitch 只在 X11 出现）。
    ch.sent.clear()
    press(w, K.Key_Q, CTRL | ALT, "@")     # 德语布局 AltGr+Q = '@'
    check("AltGr(=Ctrl+Alt)+可打印字符送出原字符而不是控制码",
          bytes(ch.sent) == b"@",
          "实测 %r（keyPressEvent 的 'Ctrl + letter' 分支先命中，"
          "Q-0x40=0x11）" % bytes(ch.sent))

    ch.sent.clear()
    ime = QtGui.QInputMethodEvent("zhongwen", [])
    ime.setCommitString("中文")
    QtWidgets.QApplication.sendEvent(w, ime)
    got = bytes(ch.sent)
    check("IME 提交中文：送到远端的是 UTF-8 的中文，不是拼音串",
          got == "中文".encode("utf-8"), "实测 %r" % got)
    feed(w, "中文")          # 模拟远端回显
    pump(20)
    check("IME 提交的内容回显后真的上屏", "中文" in screen_text(w),
          "屏幕=%r" % screen_text(w)[:40])

    # --- 传输期闸门 -------------------------------------------------------
    section("1e. 传输期输入闸门 / 已关闭通道")
    w.set_input_blocked(True)
    ch.sent.clear()
    press(w, K.Key_A, NO, "a")
    check("闸门打开时普通按键远端一个字节都没有", bytes(ch.sent) == b"",
          "实测 %r" % bytes(ch.sent))
    ch.sent.clear()
    press(w, K.Key_Tab, NO, "\t")
    check("闸门打开时 Tab 不进载荷流", bytes(ch.sent) == b"",
          "实测 %r" % bytes(ch.sent))
    clip_set("PASTE-WHILE-BLOCKED")
    ch.sent.clear()
    press(w, K.Key_V, CTRL, "")
    check("闸门打开时 Ctrl+V 不进载荷流", bytes(ch.sent) == b"",
          "实测 %r" % bytes(ch.sent))
    w.set_input_blocked(False)

    ch.closed = True
    ch.sent.clear()
    try:
        press(w, K.Key_Return, NO, "")
        press(w, K.Key_A, NO, "a")
        crashed = False
    except Exception as exc:                      # noqa: BLE001
        crashed = True
        err = exc
    check("通道已关闭时按键不抛异常", not crashed,
          "抛了 %r" % (err if crashed else None))
    check("通道已关闭时不再往通道写", bytes(ch.sent) == b"",
          "实测 %r" % bytes(ch.sent))
    ch.closed = False
    return w, ch


# ===========================================================================
# 2. 回滚与历史
# ===========================================================================
MARK = "HISTMARK7"
LIVE = "LIVEPROMPT7"
LATIN = "¡¢£¤¥¦"


def fill_history(term, marker=MARK, latin=LATIN, lines=60):
    """灌够输出把内容顶进 pyte 历史。返回 (历史行数, marker 在历史里的下标)。"""
    feed(term, marker + "\r\n")
    feed(term, "ascii-row-2\r\n")
    feed(term, latin + "\r\n")          # 非法 UTF-8，落进历史
    for i in range(lines):
        feed(term, "LINE%03d\r\n" % i)
    pump(30)
    top = list(term._screen.history.top)
    texts = ["".join((getattr(ch, "data", None) or "") for ch in line.values())
             for line in top]
    idx = next((i for i, t in enumerate(texts) if marker in t), -1)
    return len(top), idx, texts


def section_history():
    section("2. 回滚与历史")
    w, _ch = new_term()
    nhist, midx, texts = fill_history(w)
    check("历史确实攒下了行（灌输出把内容顶出可视区）", nhist > 10,
          "history.top %d 行，marker 在第 %d 行" % (nhist, midx))
    if nhist <= 10:
        skip("回滚/历史其余判据", "历史没攒起来，前提不成立")
        return w

    top_text = "".join(texts[:3])
    check("历史行是真实文本而不是 \\x00\\x01\\x02 列号",
          "\x00" not in top_text and MARK in top_text,
          "前三行=%r" % top_text[:40])

    rows = w._rows
    section("2a. 滚轮步长与方向")
    w._scroll_offset = 0
    wheel(w, 120)
    up1 = w._scroll_offset
    check("滚轮上滚一格上移 3 行", up1 == TW._WHEEL_STEP,
          "offset=%d，_WHEEL_STEP=%d" % (up1, TW._WHEEL_STEP))
    wheel(w, -120)
    check("滚轮下滚一格回退 3 行", w._scroll_offset == 0,
          "offset=%d" % w._scroll_offset)
    # M7 元断言：方向判据不是恒真的（同一个事件反过来必须减小）
    wheel(w, 120)
    wheel(w, -120)
    check("M7 方向判据对照：同样幅度反向滚，offset 确实变小",
          w._scroll_offset == 0, "offset=%d" % w._scroll_offset)
    check("滚动条 singleStep 与滚轮步长一致（两套速度的坑）",
          w._sb.singleStep() == TW._WHEEL_STEP,
          "singleStep=%d" % w._sb.singleStep())

    section("2b. 新输出不拉回底部 / 上限")
    wheel(w, 120)
    before = w._scroll_offset
    feed(w, "AFTER-SCROLL-OUTPUT\r\n")
    pump(20)
    check("上滚进历史后新输出不会把视图拉回底部", w._scroll_offset == before,
          "滚后=%d，输出后=%d" % (before, w._scroll_offset))
    check("新输出确实上屏了（没被吞）", "AFTER-SCROLL-OUTPUT" in screen_text(w),
          "屏幕=%r" % screen_text(w)[:40])

    # **另起一个终端**，不要往 `w` 里灌。上面 `w` 的选区/剪贴板/历史标记
    # 判据都依赖它现有的内容，在这儿堆几百行会把后面全带崩。
    sw, _swch = new_term()
    for i in range(120):
        feed(sw, "PUSH%03d\r\n" % i)
        pump(2)
    sw.set_scrollback(50)
    pump(20)
    for _ in range(120):
        wheel(sw, 120)
    # **上界是「实际有多少行历史」，不是「保留上限」。** 原来这两条钉的是
    # `offset == 50` / `sb.maximum() == _scrollback_limit`，而那时真实历史只有
    # 36 行 —— 条宣称能回滚 50 行、画面上只有 36 行可看，正是「条和画面对不上」。
    # 「不会滚过头」的意图不变，只是上界换成了真实的那一个。
    check("回滚上限 = 实际历史行数（不会滚过头，也不会虚报）",
          sw._scroll_offset == sw._history_len(),
          "offset=%d，实际历史=%d" % (sw._scroll_offset, sw._history_len()))
    check("实际历史不超过 set_scrollback 设的保留上限",
          sw._history_len() <= sw._scrollback_limit,
          "实际历史=%d，保留上限=%d" % (sw._history_len(), sw._scrollback_limit))
    check("改 set_scrollback 后滚动条范围跟着**实际历史**走",
          sw._sb.maximum() == sw._history_len(),
          "sb.maximum=%d，实际历史=%d" % (sw._sb.maximum(), sw._history_len()))
    # 元断言：老写法（钉保留上限）必然对不上，否则上面等于没改。
    # **必须挑一个「实际历史 < 保留上限」的场景**：上面那个 sw 历史正好被
    # 截到 50（= 上限），两边巧合相等，绿得没有意义。用户实际遇到的是
    # 默认 5000 上限下只滚出几十行 —— 那种场景才让老判据露馅。
    mw, _mwch = new_term()
    for i in range(60):
        feed(mw, "FEW%03d\r\n" % i)
        pump(2)
    pump(20)
    check("元断言：写死 _scrollback_limit 的老判据在「历史远小于上限」时会红",
          mw._history_len() < mw._scrollback_limit
          and mw._sb.maximum() != mw._scrollback_limit,
          "实际历史=%d 保留上限=%d sb.max=%d"
          % (mw._history_len(), mw._scrollback_limit, mw._sb.maximum()))

    # 回滚视口几何。**判据按 xterm 语义**：回滚 N 行 = 一个恰好 rows 行的窗口，
    # 它的**底边**也跟着上移 N 行（既不是钉死在实时屏底边，也不是「一进历史
    # 就只剩最老那几行」）。用每行内容都不同的 V00..V28 来量，行号即判据。
    vw, _ = new_term()
    vrows = vw._rows
    for i in range(vrows + 12):
        feed(vw, "PAD%03d\r\n" % i)          # 先攒出历史（必须超过一屏才会滚）
    feed(vw, "\x1b[2J")                      # 2J 只清可见屏，历史留着
    for i in range(vrows):
        feed(vw, "\x1b[%d;1HV%02d" % (i + 1, i))
    feed(vw, "\x1b[1;1H")                    # 光标挪走，别污染底行指纹
    pump(40)
    n_buf = len(list(vw._screen.history.top))
    bottom = cell_rect(vw, 0, vrows - 1, 10, 1)

    def ref_fp(text):
        """同一字号下把 text 画在第 0 行、光标停在下一行时的指纹。"""
        r, _ = new_term()
        feed(r, text + "\r\n")
        pump(20)
        fp = fingerprint(snap(r), cell_rect(r, 0, 0, 10, 1))
        r.close()
        r.deleteLater()
        return fp

    vw._scroll_offset = 0
    vw.update()
    got0 = fingerprint(snap(vw), bottom)
    check("回滚 0 行：视口底行就是实时屏最后一行",
          got0 == ref_fp("V%02d" % (vrows - 1)),
          "底行应显示 V%02d" % (vrows - 1))
    vw._scroll_offset = 6
    vw.update()
    got6 = fingerprint(snap(vw), bottom)
    check("回滚 6 行：视口底边上移到实时屏往上第 6 行（xterm 语义）",
          got6 == ref_fp("V%02d" % (vrows - 1 - 6)),
          "底行应显示 V%02d，实测指纹=%s 与之不同" % (vrows - 1 - 6, got6[:10]))

    vw._scroll_offset = n_buf
    vw.update()
    fp_clamped = fingerprint(snap(vw))
    vw._scroll_offset = n_buf + 40
    vw.update()
    img = snap(vw)
    blank = 0
    for r in range(vrows):
        n = count_near(img, cell_rect(vw, 0, r, vw._cols, 1), rgb(vw._bg.name()), 6)
        if n < vw._cols * vw._char_w * 0.6:
            blank += 1
    check("回滚超出历史长度时被钳位（画面不再变化，也不出现整行空白）",
          fingerprint(img) == fp_clamped and blank < vrows // 3,
          "历史 %d 行；offset=%d 与 %d 画面 %s / %s，整行空白 %d"
          % (n_buf, n_buf, n_buf + 40, fp_clamped[:8],
             fingerprint(img)[:8], blank))

    section("2c. 历史行画出来的是不是原样（像素指纹）")

    def hist_find(term, needle):
        """**每次现算**：灌新输出会改变 len(history.top)，拿旧的行数反算
        offset 会把别的行当成目标行（那样比出来的差异是测试自己的错）。"""
        top = list(term._screen.history.top)
        for i, line in enumerate(top):
            txt = "".join((getattr(c, "data", None) or "") for c in line.values())
            if needle in txt:
                return i, len(top)
        return -1, len(top)

    # M3 元断言：同一帧在「原实现」与「故意画错的实现」下指纹必须不同
    m3ref, _ = new_term()
    feed(m3ref, MARK + "\r\n")
    pump(20)
    mark_ref = fingerprint(snap(m3ref), cell_rect(m3ref, 0, 0, 10, 1))
    m3ref._fg = QtGui.QColor("#ff00ff")
    m3ref.update()
    fp_wrong = fingerprint(snap(m3ref), cell_rect(m3ref, 0, 0, 10, 1))
    check("M3 元断言：故意改坏前景色后指纹必变（否则下面判据恒真）",
          fp_wrong != mark_ref,
          "改色后=%s 正常=%s" % (fp_wrong[:10], mark_ref[:10]))
    m3ref.close()
    m3ref.deleteLater()
    pump(20)

    midx2, nhist2 = hist_find(w, MARK)
    if midx2 < 0:
        skip("历史行画出来与实时一致", "历史里找不到 %s" % MARK)
    else:
        w._scroll_offset = nhist2 - midx2
        w.update()
        hist_fp = fingerprint(snap(w), cell_rect(w, 0, 0, 10, 1))
        check("历史里的 %s 画出来和实时画出来一致（历史行是真实文本）" % MARK,
              hist_fp == mark_ref,
              "历史指纹=%s 实时指纹=%s offset=%d/%d" % (hist_fp[:10],
                                                     mark_ref[:10],
                                                     w._scroll_offset, nhist2))

    lat_idx, nlat = hist_find(w, LATIN)
    lref, _ = new_term()
    feed(lref, LATIN + "\r\n")
    pump(20)
    lat_ref = fingerprint(snap(lref), cell_rect(lref, 0, 0, 6, 1))
    if lat_idx < 0:
        skip("非 UTF-8 的历史行滚回来仍是可读字符", "历史里找不到 %r" % LATIN)
    else:
        w._scroll_offset = nlat - lat_idx
        w.update()
        lat_hist = fingerprint(snap(w), cell_rect(w, 0, 0, 6, 1))
        check("非 UTF-8 的历史行滚回来仍是可读字符、不是豆腐块",
              lat_hist == lat_ref,
              "历史指纹=%s 正确渲染指纹=%s offset=%d/%d"
              % (lat_hist[:10], lat_ref[:10], w._scroll_offset, nlat))

    section("2d. 回滚后拖选复制")
    midx3, nhist3 = hist_find(w, MARK)
    w._scroll_offset = nhist3 - midx3 if midx3 >= 0 else nhist3
    drag(w, 0, 0, 12, 0)
    copied = w.selected_text()
    check("回滚后拖选复制得到的是原文",
          MARK in copied and "\x00" not in copied,
          "实测 %r" % copied[:32])
    check("回滚后复制出来的文本里没有列号序列",
          "\x00" not in copied and "\x01" not in copied,
          "实测 %r" % copied[:32])

    # 宽字符行（live 路径与历史路径各来一次）
    ww, _ = new_term()
    feed(ww, "中文宽字符\r\n")
    pump(20)
    drag(ww, 0, 0, 4, 0)                     # 4 格覆盖 5 个汉字 = 10 列
    live_copy = ww.selected_text()
    check("行尾宽字符的占位格不进剪贴板（live 路径）",
          live_copy == "中文宽字符", "实测 %r" % live_copy)

    w2, _ = new_term()
    feed(w2, "中文宽字符\r\n")
    for i in range(40):
        feed(w2, "FILL%03d\r\n" % i)
    pump(20)
    w2._scroll_offset = len(list(w2._screen.history.top))
    drag(w2, 0, 0, 4, 0)
    hist_copy = w2.selected_text()
    check("历史行里的宽字符没有多余空格（复制出来的应是 5 个汉字）",
          hist_copy == "中文宽字符", "实测 %r" % hist_copy)
    w2.close()
    w2.deleteLater()
    pump(10)

    section("2e. 改字号 / 拉窗口后还能滚回之前的内容")
    w3, _ = new_term()
    feed(w3, "FONTMARK42\r\n")
    for i in range(40):
        feed(w3, "JUNK%03d\r\n" % i)
    pump(20)
    n_before = len(list(w3._screen.history.top))
    f_before = next((i for i, line in enumerate(w3._screen.history.top)
                     if "FONTMARK42" in "".join(
                         (getattr(c, "data", None) or "") for c in line.values())),
                    -1)
    w3.set_font(core_fonts.mono_font(20))
    pump(40)
    n_after = len(list(w3._screen.history.top))
    f_after = next((i for i, line in enumerate(w3._screen.history.top)
                    if "FONTMARK42" in "".join(
                        (getattr(c, "data", None) or "") for c in line.values())),
                   -1)
    check("改字号后之前的内容还留在历史里",
          n_after > 0 and f_after >= 0 and "FONTMARK42" in (
              "".join((getattr(c, "data", None) or "")
                      for c in list(w3._screen.history.top)[f_after].values())
              if f_after >= 0 else ""),
          "改字号前后历史行数 %d -> %d，FONTMARK42 下标 %d -> %d"
          % (n_before, n_after, f_before, f_after))
    check("改字号后仍能滚到 FONTMARK42 那一行（按 offset 反算并画出同样的像素）",
          f_after >= 0 and _fp_at_history_row(w3, f_after, "FONTMARK42") is not False,
          "见下一条的指纹对照")

    # 具体内容判据：滚到那一行画出来的像素 == 实时画「FONTMARK42」的像素
    got = _fp_at_history_row(w3, f_after, "FONTMARK42")
    ref2, _ = new_term(font_size=20)         # **必须同字号**：格子尺寸不同，
    feed(ref2, "FONTMARK42\r\n")             # 指纹本来就不可比
    pump(20)
    want = fingerprint(snap(ref2), cell_rect(ref2, 0, 0, 10, 1))
    check("滚回 FONTMARK42 那一行，画面内容与实时渲染逐像素一致",
          got is not False and got == want,
          "历史指纹=%s 实时指纹=%s（字号 20）" % (str(got)[:10], want[:10]))
    ref2.close()
    ref2.deleteLater()

    section("2f. 滚动条")
    w.set_scrollback(2000)
    cols_before = w._cols
    wheel(w, 120)
    check("滚动条出现/变化时终端列数不变（滚动条不吃宽度）",
          w._cols == cols_before, "cols %d -> %d" % (cols_before, w._cols))
    w._scroll_offset = 0
    w._sync_sb()
    check("滑块在底部 = 实时画面", w._sb.value() == 0 and w._scroll_offset == 0,
          "sb=%d offset=%d" % (w._sb.value(), w._scroll_offset))
    w._sb.setValue(9)
    pump(20)
    check("滑块往上 = 往历史（方向没反）", w._scroll_offset == 9,
          "sb=%d offset=%d" % (w._sb.value(), w._scroll_offset))
    fp_bottom = fingerprint(snap(w))
    w._sb.setValue(21)
    pump(20)
    fp_moved = fingerprint(snap(w))
    check("拖动滑块后画面同步变化（不是只有条在动）",
          w._scroll_offset == 21 and fp_bottom != fp_moved,
          "offset=%d 指纹 %s -> %s" % (w._scroll_offset, fp_bottom[:8],
                                     fp_moved[:8]))
    w._scroll_offset = 0
    return w


def _fp_at_history_row(term, idx, marker):
    """滚到历史第 idx 行落在视口第 0 行，取该行指纹。"""
    n = len(list(term._screen.history.top))
    if idx < 0 or idx >= n:
        return False
    term._scroll_offset = n - idx
    term.update()
    return fingerprint(snap(term), cell_rect(term, 0, 0, 10, 1))


# ===========================================================================
# 3. PTY 尺寸
# ===========================================================================
class _Conn:
    """够 SessionTab.attach 用的假连接（形状照 tests/_ptyresize.py）。"""

    def __init__(self):
        self.ch = FakeChannel()
        self.open_size = None

    @property
    def info(self):
        from sshclient.remote.connection import ConnectionInfo
        return ConnectionInfo(host="10.0.0.9", port=22, username="deploy",
                             fingerprint="x")

    def open_shell(self, term=None, cols=80, rows=24):
        self.open_size = (cols, rows)
        return self.ch

    def open_sftp(self):
        raise OSError("no sftp in this test")

    def exec(self, cmd, timeout=None):
        return ("", "")

    def close(self):
        self.ch.closed = True

    def set_tunnel_callbacks(self, on_error=None, on_event=None):
        pass

    def add_tunnel(self, spec, enabled=True):
        return spec

    def set_tunnel_enabled(self, port, enabled=True):
        return enabled

    def remove_tunnel(self, port):
        return False

    def tunnel_specs(self):
        return []

    def tunnel_states(self):
        return {}

    def tunnel_connections(self):
        return {}


def section_pty():
    section("3. PTY 尺寸（控件级）")
    w, ch = new_term()
    c0, r0 = w._cols, w._rows
    ch.resizes.clear()
    w.resize(1300, 760)
    pump(60)
    check("拉宽窗口后列数增加", w._cols > c0, "%d -> %d" % (c0, w._cols))
    check("拉宽窗口后远端收到了对应 resize_pty",
          bool(ch.resizes) and ch.resizes[-1] == (w._cols, w._rows),
          "收到 %r，网格 %dx%d" % (ch.resizes[-3:], w._cols, w._rows))

    n = len(ch.resizes)
    w.resize(1300, 760)
    pump(40)
    check("回同一尺寸不重发 resize", len(ch.resizes) == n,
          "重发 %d 次" % (len(ch.resizes) - n))

    feed(w, "RESIZE-KEEPS-CONTENT-9\r\n")
    pump(20)
    w.resize(900, 420)
    pump(60)
    check("缩放不丢内容", "RESIZE-KEEPS-CONTENT-9" in screen_text(w),
          "屏幕=%r" % screen_text(w)[:40])

    w.set_font(core_fonts.mono_font(22))
    pump(60)
    need = w._pad_y * 2 + w._rows * w._char_h
    check("改字号后最后一行完整可见（grid 与 char_h 自洽）", need <= w.height(),
          "需要高=%d 控件高=%d char_h=%d rows=%d"
          % (need, w.height(), w._char_h, w._rows))
    check("改字号后内容一字不差",
          "RESIZE-KEEPS-CONTENT-9" in screen_text(w),
          "屏幕=%r" % screen_text(w)[:40])

    # 最小尺寸要用**新建**的控件量：上面已经换过字号，用当前 metrics 算期望
    # 会得到一个永远对不上的数（_set_size 只在 __init__ / set_font 时按当时
    # 的 char_w/char_h 算过一次）。
    fresh, _ = new_term()
    minw = fresh._char_w * 80 + fresh._pad_x * 2
    minh = fresh._char_h * 24 + fresh._pad_y * 2
    check("最小尺寸 = 80x24 格 + 边距",
          fresh.minimumWidth() == minw and fresh.minimumHeight() == minh,
          "minimum=(%d,%d) 期望=(%d,%d)" % (fresh.minimumWidth(),
                                          fresh.minimumHeight(), minw, minh))

    w._teardown_channel()
    ch.resizes.clear()
    w.resize(1400, 800)
    pump(60)
    check("通道断开后不再 resize", len(ch.resizes) == 0,
          "断开后又发出 %d 次" % len(ch.resizes))

    section("3b. PTY 尺寸（SessionTab 级：连接后同步 / 节流）")
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    tab = main_window.SessionTab()
    lay.addWidget(tab)
    host.resize(1100, 700)
    host.show()
    pump(250)                       # 先让布局把终端尺寸定下来
    conn = _Conn()
    tab.attach(conn)
    pump(60)
    term = tab.terminal
    conn.ch.resizes.clear()
    # **不要写死 sleep。** 那次「连接后主动同步一次」是
    # `_on_terminal_resize` → 80ms 单发 QTimer → `_send_pty_resize` 驱动的
    # （`main_window.py:799-814`），而触发它的 resize 事件本身要等布局把
    # 终端网格定下来才来。机器繁忙时「布局 → resize → 80ms 定时器」整条链
    # 会超过原来那个 `pump(300)`，于是判据红的是**等得不够久**，
    # 不是「没同步」。实测：安静时 3/3 绿，全量回归负载下红过一次，报的是
    # 「一个字节的 resize_pty 都没收到」。项目自己的 `wait_until`
    # 就在下一条判据里用着。
    _synced = wait_until(lambda: bool(conn.ch.resizes), 3000)
    sent = list(conn.ch.resizes)
    # **比的是同步落地那一刻的当前网格**，不是 attach 后 60ms 就读下的那个 ——
    # 后者可能还是布局没定完的旧值，拿它当期望值会把「布局还没稳」
    # 误报成「同步发错了尺寸」。
    check("连接建立后主动把当前网格尺寸同步给远端一次",
          _synced and sent[-1] == (term._cols, term._rows),
          "open_shell 开在 %r，同步后收到 %r，终端当前网格是 %dx%d"
          % (conn.open_size, sent, term._cols, term._rows))
    # 元断言：**判据不是恒绿的。** `wait_until` 对一条永远不会成立的条件
    # 必须返回 False —— 否则上面那个「等到了」恒为真，这条判据就废了。
    # （它原本靠一个写死的 `pump(300)` 成立；换成等待之后必须单独证明
    # 这个等待不是空转。）
    check("元断言：wait_until 对永不成立的条件返回 False"
          "（所以上面那个「等到了」不是恒真）",
          wait_until(lambda: False, 200) is False)

    conn.ch.resizes.clear()
    for i in range(40):
        term.resize(600 + i * 7, 400 + i * 3)
        app.processEvents()
    wait_until(lambda: len(conn.ch.resizes) > 0, 800)
    pump(200)
    burst = len(conn.ch.resizes)
    check("连甩 40 次尺寸，节流后没有逐次照发", 0 < burst < 40,
          "实际发出 %d 次" % burst)
    check("节流后发出的仍是最新网格尺寸",
          bool(conn.ch.resizes) and conn.ch.resizes[-1] == (term._cols, term._rows),
          "最后=%r 网格=%dx%d" % (conn.ch.resizes[-1:] , term._cols, term._rows))

    tab._on_channel_closed()
    pump(80)
    conn.ch.resizes.clear()
    term.resize(1350, 850)
    pump(200)
    check("断开后不再 resize（SessionTab 侧）", len(conn.ch.resizes) == 0,
          "断开后又发出 %d 次" % len(conn.ch.resizes))

    conn2 = _Conn()
    tab2 = main_window.SessionTab()
    host.layout().addWidget(tab2)
    tab2.attach(conn2)
    pump(200)
    first = list(conn2.ch.resizes)
    tab2.terminal.resize(1000, 640)
    wait_until(lambda: len(conn2.ch.resizes) > len(first), 900)
    check("重连后第一次尺寸变化确实发出去了",
          len(conn2.ch.resizes) > len(first),
          "重连后收到 %d 次，最后=%r" % (len(conn2.ch.resizes),
                                       conn2.ch.resizes[-1:]))
    tab2._on_channel_closed()
    pump(60)
    tab2.close()
    host.close()
    host.deleteLater()
    pump(20)


# ===========================================================================
# 4. 解码
# ===========================================================================
def section_decode():
    section("4. 解码")
    w, _ = new_term()
    feed(w, "latin1:")
    feed_raw = LATIN.encode("latin-1")
    w.write(feed_raw)
    pump(20)
    txt = screen_text(w)
    check("非 UTF-8（Latin-1）输出显示成可读字符",
          LATIN in txt, "屏幕=%r" % txt[:30])
    check("Latin-1 输出里没有 U+FFFD 豆腐块", "�" not in txt,
          "屏幕=%r" % txt[:30])

    w2, _ = new_term()
    zh = "中".encode("utf-8")
    w2.write(zh[:1])
    pump(10)
    mid = "".join(w2._screen.display)
    w2.write(zh[1:])
    pump(20)
    after = "".join(w2._screen.display)
    check("一个汉字被 TCP 拆两半到达时屏幕上是完整的一个字",
          "中" in after and "中" not in mid and "�" not in after,
          "前半包后=%r，补齐后=%r" % (mid[:12], after[:12]))

    w3, _ = new_term()
    s = "测".encode("utf-8")
    w3.write(s[:2])
    pump(10)
    check("增量解码器跨包保留状态（2/3 字节时不吐替换字符、也不上屏）",
          "�" not in "".join(w3._screen.display)
          and "测" not in "".join(w3._screen.display),
          "屏幕=%r" % "".join(w3._screen.display)[:12])
    w3.write(s[2:])
    pump(20)
    check("补上最后一个字节后一次上屏成完整的「测」",
          "测" in "".join(w3._screen.display),
          "屏幕=%r" % "".join(w3._screen.display)[:12])


# ===========================================================================
# 5. 终端查询应答
# ===========================================================================
def section_queries():
    section("5. 终端查询应答")
    w, ch = new_term()
    CASES = [
        (b"\x1b[c", b"\x1b[?62;1;2;6;9;15;22c", "DA1"),
        (b"\x1b[>c", b"\x1b[>0;95;0c", "DA2"),
        (b"\x1b[5n", b"\x1b[0n", "DSR 设备状态"),
        (b"\x1b[?u", b"\x1b[?0u", "kitty 键盘协议查询"),
    ]
    for raw, want, name in CASES:
        ch.sent.clear()
        w.write(raw)
        check("%s 应答字节正确" % name, bytes(ch.sent) == want,
              "实测 %r，期望 %r" % (bytes(ch.sent), want))
        check("%s 没有被画到屏幕上" % name,
              w._screen.display[0].strip() == "",
              "第 0 行=%r" % w._screen.display[0][:40])

    feed(w, "abc\r\n")
    ch.sent.clear()
    w.write(b"\x1b[6n")
    cpr = bytes(ch.sent)
    check("DSR 光标位置应答应答 CPR",
          cpr.startswith(b"\x1b[") and cpr.endswith(b"R")
          and cpr[2:] == b"%d;%dR" % (w._screen.cursor.y + 1,
                                     w._screen.cursor.x + 1),
          "实测 %r，光标=(%d,%d)" % (cpr, w._screen.cursor.y, w._screen.cursor.x))

    ch.sent.clear()
    w.write(b"mid\x1b")
    w.write(b"[c")
    check("查询被 TCP 拆两半也照样应答",
          bytes(ch.sent) == b"\x1b[?62;1;2;6;9;15;22c",
          "实测 %r" % bytes(ch.sent))
    check("拆包那批的普通文本照常上屏", "mid" in "".join(w._screen.display),
          "第 0 行=%r" % w._screen.display[0][:30])

    ch.sent.clear()
    w.write(b"\x1b[32mgreen\x1b[0m\r\n")
    check("查询混在正常输出里时普通文本与 SGR 都不被吃",
          "green" in "".join(w._screen.display) and not ch.sent,
          "屏幕=%r 实发=%r" % ("".join(w._screen.display)[:30], bytes(ch.sent)))

    w.set_input_blocked(True)
    ch.sent.clear()
    w.write(b"\x1b[c")
    check("闸门打开时不回话（回话会被当成传输载荷）", not ch.sent,
          "实测 %r" % bytes(ch.sent))
    w.set_input_blocked(False)

    ch.sent.clear()
    w.write(b"\x1b(B\x1b)0")
    check("字符集切换 ESC(B 认下来但不上屏也不回话",
          not ch.sent and w._screen.display[-1].strip() == "",
          "实发=%r 末行=%r" % (bytes(ch.sent), w._screen.display[-1][:30]))


# ===========================================================================
# 6. 选区与光标
# ===========================================================================
def section_selection():
    section("6. 选区与光标")
    w, ch = new_term()
    feed(w, "SELECTROW-ABCDEFG\r\n")
    feed(w, "second row here\r\n")
    pump(30)

    drag(w, 0, 0, 2, 0)                  # 单行选区：第 0 列 -> 第 2 列 = 3 个字符
    check("左键拖选产生选区", w.has_selection() and w.selected_text() == "SEL",
          "选区文本=%r" % w.selected_text())

    click(w, 10, 1)
    check("单击（不拖）清掉选区", not w.has_selection(),
          "has_selection()=%r" % w.has_selection())

    drag(w, 0, 0, 5, 1)
    cross = w.selected_text()
    check("跨行选区在剪贴板里以 \\n 分隔（首行整段、末行到光标列）",
          cross == "SELECTROW-ABCDEFG\nsecond", "实测 %r" % cross)

    drag(w, 0, 0, 8, 0)
    check("复制内容与屏幕逐字一致（不多余空格/换行）",
          w.selected_text() == "SELECTROW",
          "实测 %r，屏幕首行=%r" % (w.selected_text(), w._screen.display[0][:12]))

    # ---- 6a. 按行复制：短行的填充空格不进剪贴板 ---------------------------
    #
    # 根因：pyte 的 `display[row]` **每行都补齐到屏幕宽度**，所以
    # `len(line)` 恒等于 `_cols`，`xe = min(x2+1, len(line), _cols)` 里那个
    # `len(line)` **永远不生效** —— 每行都截到同样列数。历史那条路更明确：
    # `_pad_row()` 直接补到满宽。于是短行的尾部空格被一起带进剪贴板，
    # 复制出来是一个**矩形块**，而不是用户以为的「一行一行」。
    # 修法是每段 `rstrip(" ")`（见 `terminal_widget.selected_text`）。
    section("6a. 选区按行复制（短行的填充空格不进剪贴板）")
    rw, _ = new_term()
    feed(rw, "AAA\r\nBBBBB\r\nC\r\n")
    pump(30)
    drag(rw, 0, 0, 9, 2)                     # 从第 0 列拖到第 9 列第 2 行
    rtxt = rw.selected_text()
    check("跨行选区复制出来是一行一行，不是矩形块",
          rtxt == "AAA\nBBBBB\nC", "实测 %r" % rtxt)

    # 元断言：屏幕上这 3 行**确实**都被补到满宽，且真实长度各不相同 ——
    # 所以上面那条绿不是因为「碰巧选到内容末尾」，而是真的剥掉了填充。
    raws = [rw._screen.display[i][0:10] for i in range(3)]
    check("元断言：这三行在屏幕上都是补满宽的（所以 rstrip 不是空转）",
          all(len(s) == 10 and s.endswith(" ") for s in raws)
          and [len(s.rstrip(" ")) for s in raws] == [3, 5, 1],
          "屏幕三行=%r" % (raws,))

    # ---- 6c. 选区按行：首行从起点列起、末行到光标列止、中间整行 ---------
    #
    # 用户要的是「从某行任意位置开始，按行读到结束行光标选中的任意位置」。
    # 最早的矩形选区每一行都只高亮到终点那一列（屏幕上是一块参差的色块），
    # 改成全整行又丢掉了「从任意列起 / 到任意列止」的能力。
    section("6c. 选区按行：首行从起点列起、末行到光标列止、中间整行")
    lw, _ = new_term()
    feed(lw, "ALPHA-BRAVO-CHARLIE\r\n")
    feed(lw, "DELTA-ECHO-FOXTROT\r\n")
    feed(lw, "GOLF-HOTEL-INDIA\r\n")
    pump(30)
    drag(lw, 6, 0, 10, 2)                  # 第 6 列（第 0 行）-> 第 10 列（第 2 行）
    # 末行**含光标那一格**，所以是 `[0:11]` 共 11 个字符。
    check("首行从起点列起、中间整行、末行到光标列止（含光标格）",
          lw.selected_text() == "BRAVO-CHARLIE\nDELTA-ECHO-FOXTROT\nGOLF-HOTEL-",
          "实测 %r" % lw.selected_text())
    check("反向拖（从下往上）首尾角色互换，不是原样照搬",
          (drag(lw, 10, 2, 6, 0),
           lw.selected_text() == "BRAVO-CHARLIE\nDELTA-ECHO-FOXTROT\nGOLF-HOTEL-")[1],
          "实测 %r" % lw.selected_text())
    drag(lw, 6, 1, 10, 1)
    check("单行选区 = 从起点列到光标列（不是整行）",
          lw.selected_text() == "ECHO-", "实测 %r" % lw.selected_text())
    drag(lw, 6, 0, 10, 1)
    check("两行选区也是首尾按列、中间无整行",
          lw.selected_text() == "BRAVO-CHARLIE\nDELTA-ECHO-",
          "实测 %r" % lw.selected_text())
    # 元断言：整行化（上一版的做法）在这个场景下**确实**给出别的答案
    whole = [lw._screen.display[r].rstrip(" ") for r in range(3)]
    check("元断言：整行化会多带出首尾两段的文本（所以上面有区分度）",
          whole != ["BRAVO-CHARLIE", "DELTA-ECHO-FOXTROT", "GOLF-HOTEL-"]
          and "ALPHA-BRAVO-CHARLIE" in whole,
          "整行版=%r" % (whole,))
    # 元断言：最早的矩形写法切出来的是碎片，三者互不相同
    block = [lw._screen.display[r][6:11].rstrip(" ") for r in range(2)]
    check("元断言：矩形写法切出的是碎片（与上面两种都不同）",
          block == ["BRAVO", "ECHO-"], "矩形版=%r" % (block,))
    # 高亮形状：首行左边留白、中间行铺满
    drag(lw, 6, 0, 10, 2)
    lw._update_metrics()
    pump(10)
    img = lw.grab().toImage()
    y_top = int(lw._pad_y + 0.5 * lw._char_h)
    y_mid = int(lw._pad_y + 1.5 * lw._char_h)
    y_out = int(lw._pad_y + 5.5 * lw._char_h)     # 选区外那一行
    x_c0 = int(lw._pad_x + 0.5 * lw._char_w)
    x_c6 = int(lw._pad_x + 6.5 * lw._char_w)
    check("首行第 0 列**不**高亮（首行从第 6 列才开始）",
          img.pixel(x_c0, y_top) == img.pixel(x_c0, y_out),
          "首行第0列=%s 选区外=%s" % (img.pixel(x_c0, y_top), img.pixel(x_c0, y_out)))
    check("首行第 6 列高亮", img.pixel(x_c6, y_top) != img.pixel(x_c6, y_out))
    check("中间行第 0 列高亮（中间行是完整一行）",
          img.pixel(x_c0, y_mid) != img.pixel(x_c0, y_out))
    lw.close()
    lw.deleteLater()
    pump(10)

    # 元断言（反向）：不剥尾空格重算同一块选区，**确实**是矩形块 ——
    # 证明上面那条判据对缺陷态敏感，不是恒绿。
    naive = "\n".join(rw._screen.display[i][0:10] for i in range(3))
    check("元断言：不剥尾空格时同一块选区就是矩形块（缺陷态确实会被抓住）",
          naive == "AAA       \nBBBBB     \nC         " and naive != rtxt,
          "不剥=%r" % naive)

    # 走真实剪贴板：确认剥完的结果真的到了系统剪贴板，而不只是 selected_text。
    # 用 wait_until 而不是「写完立刻读」——Windows 的 OLE 延迟渲染要泵一次
    # 事件循环才确认写入（见 clip_set 的 docstring）。
    rw.copy_selection()
    got_clip = wait_until(lambda: clip().text() == "AAA\nBBBBB\nC", 800)
    check("按行复制的内容真的落到剪贴板", got_clip, "实测 %r" % clip().text())

    # 回滚路径：`_pad_row` 把历史行补满宽，同一个毛病，必须一起修。
    hw, _ = new_term()
    feed(hw, "AAA\r\nBBBBB\r\nC\r\n")
    for i in range(40):
        feed(hw, "FILL%03d\r\n" % i)
    pump(30)
    hw._scroll_offset = len(list(hw._screen.history.top))
    drag(hw, 0, 0, 9, 2)
    htxt = hw.selected_text()
    check("回滚后按行复制也是一行一行（_pad_row 补的满宽要被剥掉）",
          htxt == "AAA\nBBBBB\nC", "实测 %r" % htxt)

    section("6b. 像素：选区底色与反色 / 光标")
    pw, _ = new_term()
    feed(pw, "\x1b[32mSELECTED-TEXT\x1b[0m\r\n")
    pump(30)
    bg = pw._bg.name()
    img = snap(pw)
    drag(pw, 0, 0, 7, 0)
    sel_img = snap(pw)
    selbg = count_near(sel_img, cell_rect(pw, 0, 0, 8, 1),
                       rgb(pw._selection_bg.name()), 12)
    check("选区有底色（选中的格子被选中底色盖住）",
          selbg > 0.5 * pw._char_w * pw._char_h,
          "选中底色像素=%d，格面积=%d"
          % (selbg, pw._char_w * pw._char_h))
    check("选区内文字是反色（前景被换成 selection_fg）",
          dominant(sel_img, cell_rect(pw, 0, 0, 8, 1), pw._selection_bg.name())
          in (pw._selection_fg.name(), pw._selection_bg.name())
          or count_near(sel_img, cell_rect(pw, 0, 0, 8, 1),
                        rgb(pw._selection_fg.name()), 12) > 0,
          "selection_fg=%s 前景实测=%s"
          % (pw._selection_fg.name(),
             dominant(sel_img, cell_rect(pw, 0, 0, 8, 1), pw._selection_bg.name())))
    check("拖动前同一片区域是终端底色（对照：选区底色不是本来就有的）",
          count_near(img, cell_rect(pw, 0, 0, 8, 1), rgb(pw._selection_bg.name()),
                     12) == 0,
          "未选中的格子里选中底色像素=%d"
          % count_near(img, cell_rect(pw, 0, 0, 8, 1),
                       rgb(pw._selection_bg.name()), 12))

    cur, _ = new_term()
    feed(cur, "ABC")
    pump(30)
    img = snap(cur)
    cimg = cur._cursor_color.name()
    at3 = count_near(img, cell_rect(cur, 3, 0, 1, 1), rgb(cimg), 10)
    at0 = count_near(img, cell_rect(cur, 0, 0, 1, 1), rgb(cimg), 10)
    check("光标是实心块（整格填满 cursor 色）",
          at3 > 0.7 * cur._char_w * cur._char_h,
          "光标格色像素=%d，格面积=%d" % (at3, cur._char_w * cur._char_h))
    check("光标位置随输出右移（ABC 之后落在第 3 格，不在第 0 格）",
          at3 > 0 and at0 == 0, "第3格=%d 第0格=%d" % (at3, at0))

    hide, _ = new_term()
    feed(hide, "XYZ")
    pump(30)
    before = count_near(snap(hide), cell_rect(hide, 3, 0, 1, 1),
                        rgb(hide._cursor_color.name()), 10)
    hide.write(b"\x1b[?25l")
    pump(30)
    after = count_near(snap(hide), cell_rect(hide, 3, 0, 1, 1),
                       rgb(hide._cursor_color.name()), 10)
    check("远端发 ESC[?25l 后 pyte 侧确实认为光标已隐藏",
          hide._screen.cursor.hidden is True,
          "cursor.hidden=%r" % hide._screen.cursor.hidden)
    check("远端发 ESC[?25l 后我们真的把光标藏起来了",
          before > 0 and after == 0,
          "隐藏前光标像素=%d，隐藏后=%d" % (before, after))

    for widget in (pw, cur, hide):
        widget.close()
        widget.deleteLater()
    pump(20)


# ===========================================================================
# 7. 像素：配色 / 宽字符
# ===========================================================================
def section_pixels():
    section("7. 像素：配色与宽字符")
    w, _ = new_term()
    bgname = w._bg.name()

    # M2 元断言：扫描器必须在已知有内容处命中，且在空白处不命中
    feed(w, "M")
    pump(30)
    img = snap(w)
    ink = count_near(img, cell_rect(w, 0, 0, 1, 1), rgb(bgname), 4)
    check("M2 元断言：像素扫描器在有字符的格子里能命中墨迹",
          ink < w._char_w * w._char_h and ink > 0.15 * w._char_w * w._char_h,
          "第0格非背景像素=%d 格面积=%d" % (ink, w._char_w * w._char_h))
    check("M2 元断言：像素扫描器在空白格子上不命中（否则「没画出来」分不开）",
          count_near(img, cell_rect(w, 40, 0, 1, 1), rgb(bgname), 4)
          == w._char_w * w._char_h,
          "第40格非背景像素=%d" % count_near(img, cell_rect(w, 40, 0, 1, 1),
                                           rgb(bgname), 4))

    feed(w, "\x1b[2J\x1b[H")
    feed(w, "\x1b[31mR\x1b[32mG\x1b[34mB\x1b[90mD\x1b[1;31mE"
            "\x1b[38;5;196mF\x1b[38;2;255;100;200mG\x1b[7mH\x1b[0m ")
    pump(30)
    img = snap(w)
    ansi = w._ansi

    # 字形很细、又被抗锯齿混过：格子里多数非背景像素是「笔色×背景」的混合色，
    # 按「出现最多的颜色」或「全部像素最近色」都会判错（实测红字被量成绿）。
    # 所以只看**字形核心**（离背景最远的那一半像素），再比谁最近。
    def core_nearest(img, rect, bgname, candidates, frac=0.5):
        bgc = QtGui.QColor(bgname)
        qs = {c: QtGui.QColor(c) for c in candidates}
        pts = []
        for y in range(max(0, rect[1]), min(img.height(), rect[1] + rect[3])):
            for x in range(max(0, rect[0]), min(img.width(), rect[0] + rect[2])):
                p = img.pixelColor(x, y)
                d = ((p.red() - bgc.red()) ** 2 + (p.green() - bgc.green()) ** 2
                     + (p.blue() - bgc.blue()) ** 2)
                if d > 9:
                    pts.append((d, p))
        if not pts:
            return ""
        pts.sort(key=lambda t: t[0], reverse=True)
        core = pts[:max(1, int(len(pts) * frac))]
        sums = {}
        for c, q in qs.items():
            sums[c] = sum((p.red() - q.red()) ** 2 + (p.green() - q.green()) ** 2
                          + (p.blue() - q.blue()) ** 2 for _d, p in core)
        return min(sums.items(), key=lambda kv: kv[1])[0]

    def near(col, im, cands):
        # **背景必须当场取，不能用 section 开头捕获的那个 `bgname`。**
        # 下面 M4 会切主题，切完背景就变了，而闭包里的 `bgname` 还是旧主题
        # 的 —— 拿旧背景当基准去筛「非背景像素」，筛进去的其实是**背景本身**。
        # 实测：亮色那一帧本该量出 #cf222e，拿旧背景 #0b0e14 去筛之后，核心
        # 像素从 31 涨到 68（多出来的全是浅色背景），最近色成了 #ff7b72 ——
        # 和深色那帧一模一样，M4 于是恒红。
        return core_nearest(im, cell_rect(w, col, 0, 1, 1), w._bg.name(), cands)

    r, g, b = ansi[1], ansi[2], ansi[4]
    got_r, got_g, got_b = near(0, img, [r, g, b]), near(1, img, [r, g, b]), \
        near(2, img, [r, g, b])
    check("16 色前景（红/绿/蓝）按主题 ANSI 调色板上色",
          got_r == r and got_g == g and got_b == b,
          "实测 %s/%s/%s 期望 %s/%s/%s" % (got_r, got_g, got_b, r, g, b))

    # M4 元断言：颜色判据不是恒真的（故意换主题后同一帧必变色）
    # **两次取样必须用同一个候选集。** 原来亮色那次多塞了一个 `w._ansi[1]`
    # （切过主题后它已经是**亮色**的 ansi 红了），深色那次只有 3 个 ——
    # 「在 4 个里最近」和「在 3 个里最近」本身就不可比。
    # `r/g/b` 是 `new_term()` 建好时（深色）的取值。
    a_light = TW.ansi_palette("light")
    cands_m4 = [r, g, b, a_light[1], a_light[2], a_light[4]]
    w.apply_palette(TW.get_palette("light"))
    pump(20)
    light_got = near(0, snap(w), cands_m4)
    w.apply_palette(TW.get_palette("dark"))
    pump(20)
    img = snap(w)
    dark_got = near(0, img, cands_m4)
    check("M4 元断言：换主题后同一格的实测颜色必变（否则颜色判据恒真）",
          light_got != dark_got,
          "亮色主题=%s 深色主题=%s" % (light_got, dark_got))
    # 「变了」还不够：得**各自落在本主题的 ANSI 红**上。背景取错的时候两次
    # 会落到同一个候选，看上去「没变」；但换个错法也可能碰巧「变了」而两
    # 次都不对 —— 只钉「不相等」分不开这两种情况。
    check("M4 元断言：两次各自落到本主题的 ANSI 红（不是量歪了碰巧不同）",
          light_got == a_light[1] and dark_got == r,
          "亮=%s(期望 %s) 深=%s(期望 %s)" % (light_got, a_light[1],
                                            dark_got, r))

    bright = ansi[8]
    check("亮色 90-97 走的是亮色板而不是普通色",
          near(3, img, [bright, r]) == bright,
          "90 前景实测=%s（候选 %s / %s）" % (near(3, img, [bright, r]),
                                            bright, r))
    check("加粗把基础色提升为亮色",
          near(4, img, [ansi[9], r]) == ansi[9],
          "粗体红实测=%s（候选 %s / %s）" % (near(4, img, [ansi[9], r]),
                                            ansi[9], r))
    check("反显：格背景被填成反显前的颜色，字迹换成反显前的背景色",
          count_near(img, cell_rect(w, 7, 0, 1, 1), rgb("#ff64c8"), 6)
          > 0.3 * w._char_w * w._char_h,
          "反显格底色像素=%d，格面积=%d（期望 > %.0f）"
          % (count_near(img, cell_rect(w, 7, 0, 1, 1), rgb("#ff64c8"), 6),
             w._char_w * w._char_h, 0.3 * w._char_w * w._char_h))
    check("256 色索引 196 应为 xterm 立方体的纯红 #ff0000",
          near(5, img, ["#ff0000", r]) == "#ff0000",
          "实测=%s（候选 #ff0000 / %s）" % (near(5, img, ["#ff0000", r]), r))
    check("真彩色 38;2;255;100;200 -> #ff64c8",
          near(6, img, ["#ff64c8", r]) == "#ff64c8",
          "实测=%s" % near(6, img, ["#ff64c8", r]))

    # 背景色：整格填色（不看字，只看格子被涂成什么）。期望值走产品自己的
    # 「pyte bg 名 -> 主题 ANSI 色」映射表 —— 这里要验的是**填色这条管子
    # 接没接上**（pyte 报的名字有没有真的被画出来），颜色映射本身由上面的
    # 前景判据负责。若 pyte 压根没认 104/100，pyte_bg 就是 default，
    # 那这一格不参与判据（而不是判它红）。
    feed(w, "\x1b[2J\x1b[H")
    feed(w, "\x1b[41m \x1b[44m \x1b[100m \x1b[104m \x1b[0m")
    feed(w, "\x1b[9;20H")                 # 光标挪走，别把光标块当成填色
    pump(30)
    img = snap(w)
    area = w._char_w * w._char_h
    pyte_bg = [str(w._screen.buffer[0][c].bg) for c in range(4)]
    want = [w._ansi[TW._ANSI_NAMES[n]] if n in TW._ANSI_NAMES else None
            for n in pyte_bg]
    fills = [count_near(img, cell_rect(w, c, 0, 1, 1), rgb(want[c]), 6)
             if want[c] else -1 for c in range(4)]
    judged = [(c, want[c], fills[c]) for c in range(4) if want[c]]
    check("16 色背景整格被填成「pyte 报的 bg 名」对应的主题 ANSI 色",
          bool(judged) and all(f > 0.9 * area for _c, _w, f in judged),
          "pyte 报的 bg=%r 主题色=%r 实测填色像素=%r（格面积=%d）"
          % (pyte_bg, want, fills, area))
    check("M8 元断言：没上背景色的格子不含这些填色（否则上面那条恒真）",
          count_near(img, cell_rect(w, 8, 0, 1, 1), rgb(ansi[1]), 6) == 0,
          "空白格里的红色填色像素=%d"
          % count_near(img, cell_rect(w, 8, 0, 1, 1), rgb(ansi[1]), 6))

    # 未知色值：不能整格变黑，要退回默认前景色
    feed(w, "\x1b[2J\x1b[H")
    feed(w, "UNK")
    pump(30)
    cell = w._screen.buffer[0][0]
    w._screen.buffer[0][0] = cell._replace(fg="not-a-real-color")
    img = snap(w)
    unk_got = core_nearest(img, cell_rect(w, 0, 0, 1, 1), bgname,
                           [w._fg.name(), "#000000"])
    check("未知色值退回默认前景色（不是整格变黑/不可见）",
          unk_got == w._fg.name(),
          "实测=%s，默认前景=%s" % (unk_got, w._fg.name()))
    w._screen.buffer[0][0] = cell._replace(fg="black")
    img2 = snap(w)
    black_got = core_nearest(img2, cell_rect(w, 0, 0, 1, 1), bgname,
                             [w._fg.name(), "#000000"])
    check("M6 元断言：把前景换成 black 后测量结果确实变（否则上面那条恒真）",
          black_got != unk_got, "black=%s 未知色=%s" % (black_got, unk_got))
    w._screen.buffer[0][0] = cell

    # 下划线的空格：空格平时不画，但这条线要看得见（实测一道 1px 的线，
    # 正好 char_w 个像素 —— 不是把格子填满）
    feed(w, "\x1b[2J\x1b[H")
    feed(w, "\x1b[4m \x1b[0m\x1b[4m  \x1b[0m\x1b[4m   \x1b[0m")
    feed(w, "\x1b[9;20H")                 # 光标挪到别行，别污染这一行的读数
    pump(30)
    img = snap(w)

    def ink(col):
        r0, c0, cw, chh = cell_rect(w, col, 0, 1, 1)
        return cw * chh - count_near(img, (r0, c0, cw, chh), rgb(bgname), 4)

    u1, plain = ink(0), ink(8)
    check("带下划线的空格仍然画得出那道线（一道细线，不是填满整格）",
          w._char_w <= u1 <= 4 * w._char_h,
          "下划线格非背景像素=%d（char_w=%d char_h=%d）"
          % (u1, w._char_w, w._char_h))
    check("M5 元断言：同一行里没下划线的空格一个像素都没有（探针看得见空白）",
          plain == 0, "普通空格格非背景像素=%d" % plain)

    # 宽字符：**网格占两格 + 字间不多空格**。不量「墨迹宽度」—— 那量的是
    # 字体的墨迹外框，不是网格分配；改成「宽字符对」与「宽字符+空格」两帧
    # 指纹必须不同：真多插了一格空白的话这两帧会长得一模一样。
    ww, _ = new_term()
    feed(ww, "中中")
    pump(30)
    row = ww._screen.display[0]
    check("两个宽字符在网格里正好占 4 列（占位格折叠掉了）",
          row.startswith("中中") and len(row) == ww._cols - 2,
          "display 首行=%r len=%d cols=%d" % (row[:12], len(row), ww._cols))
    check("宽字符占位格存在（pyte 用 data=\"\" 标记，后续判据的前提）",
          "" in row and row.replace(" ", "") == "中中",
          "display 首行=%r" % row[:12])
    fp_wide = fingerprint(snap(ww), cell_rect(ww, 0, 0, 6, 1))
    ww.close()
    ww.deleteLater()
    ws, _ = new_term()
    feed(ws, "中 中")                 # 手工插一格空白的样子
    pump(30)
    fp_spaced = fingerprint(snap(ws), cell_rect(ws, 0, 0, 6, 1))
    ws.close()
    ws.deleteLater()
    check("宽字符之间没有多插一格空白（与「中 中」逐像素不同）",
          fp_wide != fp_spaced,
          "「中中」=%s 「中 中」=%s" % (fp_wide[:10], fp_spaced[:10]))
    pump(20)


# ===========================================================================
def main() -> int:
    t0 = time.monotonic()
    print("终端功能总测  platform=%s" % os.environ.get("QT_QPA_PLATFORM"))

    for fn in (section_keys, section_history, section_pty, section_decode,
               section_queries, section_selection, section_pixels):
        try:
            fn()
        except Exception as exc:                  # noqa: BLE001
            # 一段崩了不能把后面几十条判据一起吞掉 —— 记成 FAIL 继续跑。
            check("段落 %s 跑完（未抛异常）" % fn.__name__, False, repr(exc))

    # 收尾：close -> deleteLater -> 冲掉 DeferredDelete，否则退出偶发 0xC0000005
    for widget, _parent in _OWNED:
        try:
            widget.close()
            widget.deleteLater()
        except Exception:                          # noqa: BLE001
            pass
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    pump(60)

    dt = time.monotonic() - t0
    print("\n通过 %d 项，失败 %d 项，没评上 %d 项，用时 %.1fs"
          % (len(PASS), len(FAIL), len(SKIP), dt))
    for f in FAIL:
        print("  FAIL " + f)
    for s in SKIP:
        print("  SKIP " + s)
    if FAIL:
        return 1
    if SKIP:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
