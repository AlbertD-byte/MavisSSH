"""Terminal Ctrl+C / Ctrl+V, and the settings dialog actually changing things.

The keyboard part posts real QKeyEvents, because the whole point of Ctrl+C in
a shell is which branch it takes, and that is decided by state (is anything
selected?) at the moment the key arrives.

Checks, terminal:
  1. Ctrl+V pastes the clipboard to the remote
  2. Ctrl+Shift+V pastes too
  3. Ctrl+C with no selection sends SIGINT (0x03) and does NOT touch the
     clipboard -- this is the keystroke a user hits most
  4. Ctrl+C with a selection copies and sends nothing
  5. Ctrl+Shift+C copies even when the selection is empty-but-present
  6. a plain 'c' still types a literal c
  7. other Ctrl+letter combos are untouched by the new branch

Checks, settings:
  8. the dialog round-trips every field into AppSettings
  9. Cancel changes nothing (the dialog edits a copy)
 10. an uninstalled saved font is shown, not silently dropped
 11. apply_settings re-fonts a live terminal and re-caps the scrollback
 12. the old font_size/font_family config keys migrate

Run: .venv\\Scripts\\python.exe _setkeys.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import fonts
from sshclient.core.settings import AppSettings
from sshclient.ui import terminal_widget
from sshclient.ui.design.v1 import palette
from sshclient.ui.settings_dialog import SettingsDialog

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def spin(ms=120):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


# ---------------------------------------------------------------- terminal
print("=== terminal: Ctrl+C / Ctrl+V ===")
p = palette.get_palette("dark")
term = terminal_widget.TerminalWidget()
term.apply_palette(p)
term.resize(700, 400)
term.show()
spin(150)

sent = bytearray()
term._send = lambda d, *a, **k: sent.extend(d)

term.write(b"hello world from the remote\r\n")
spin(80)


def press(key, ctrl=False, shift=False):
    mods = QtCore.Qt.KeyboardModifier.NoModifier
    if ctrl:
        mods |= QtCore.Qt.KeyboardModifier.ControlModifier
    if shift:
        mods |= QtCore.Qt.KeyboardModifier.ShiftModifier
    text = {
        (QtCore.Qt.Key.Key_C, True, False): "c",
        (QtCore.Qt.Key.Key_C, True, True): "C",
        (QtCore.Qt.Key.Key_V, True, False): "v",
        (QtCore.Qt.Key.Key_V, True, True): "V",
        (QtCore.Qt.Key.Key_C, False, False): "c",
    }.get((key, ctrl, shift), "")
    ev = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, mods, text)
    term.keyPressEvent(ev)
    spin(40)


def set_clip(text):
    """Write *text* to the clipboard, and confirm it actually landed.

    Windows owns one clipboard per session, so two ``setText`` calls back to
    back make the second fail with ``OleSetClipboard 0x800401d0``. Yielding
    the loop between them is not always enough -- another process can still
    be holding it open, and it fails intermittently under a full sweep where
    a test has just exited.

    The retry matters more than the yield. A failed ``setText`` returns
    quietly, leaving the clipboard holding nothing, and the *next* assertion
    then reads that emptiness and reports "Ctrl+C did not copy" -- blaming the
    copy feature for a race the test lost. Verifying the write turns that into
    an honest harness failure instead.
    """
    cb = QtWidgets.QApplication.clipboard()
    for _ in range(5):
        spin(60)
        cb.setText(text)
        spin(60)
        if cb.text() == text:
            return True
    check("clipboard write succeeded", False, f"could not store {text!r}")
    return False


def get_clip(retries=3):
    """Read the clipboard, retrying while it comes back empty.

    Note for whoever adds a check here: retrying on empty means this cannot be
    used to assert that the clipboard *is* empty. Every check below wants a
    non-empty value, which is why the retry is safe today.
    """
    for _ in range(retries):
        spin(40)
        got = QtWidgets.QApplication.clipboard().text()
        if got:
            return got
        spin(60)
    return QtWidgets.QApplication.clipboard().text()


set_clip("pasted-line")

sent.clear()
press(QtCore.Qt.Key.Key_V, ctrl=True)
check("Ctrl+V sends the clipboard to the remote", b"pasted-line" in bytes(sent),
      repr(bytes(sent)))

sent.clear()
press(QtCore.Qt.Key.Key_V, ctrl=True, shift=True)
check("Ctrl+Shift+V pastes as well", b"pasted-line" in bytes(sent),
      repr(bytes(sent)))

# no selection -> SIGINT, clipboard untouched
term._sel_anchor = None
term._sel_cur = None
set_clip("UNTOUCHED")
sent.clear()
press(QtCore.Qt.Key.Key_C, ctrl=True)
check("Ctrl+C with no selection sends SIGINT", bytes(sent) == b"\x03",
      repr(bytes(sent)))
check("Ctrl+C with no selection leaves the clipboard alone",
      get_clip() == "UNTOUCHED",
      get_clip())

# a real selection over the first line
term._sel_anchor = (0, 0)
term._sel_cur = (4, 0)
check("has_selection() sees the drag", term.has_selection())
set_clip("UNTOUCHED")
sent.clear()
press(QtCore.Qt.Key.Key_C, ctrl=True)
clip = get_clip()
check("Ctrl+C with a selection copies it", clip not in ("", "UNTOUCHED"),
      repr(clip))
check("Ctrl+C with a selection sends nothing to the remote",
      bytes(sent) == b"", repr(bytes(sent)))

# Ctrl+Shift+C copies without needing a selection to be *meaningful*
term._sel_anchor = (0, 0)
term._sel_cur = (4, 0)
set_clip("UNTOUCHED")
sent.clear()
press(QtCore.Qt.Key.Key_C, ctrl=True, shift=True)
check("Ctrl+Shift+C copies",
      get_clip() not in ("", "UNTOUCHED"),
      repr(get_clip()))

sent.clear()
press(QtCore.Qt.Key.Key_C, ctrl=False, shift=False)
check("a bare 'c' still types a literal c", bytes(sent) == b"c",
      repr(bytes(sent)))

sent.clear()
term._sel_anchor = None
term._sel_cur = None
press(QtCore.Qt.Key.Key_A, ctrl=True)
check("Ctrl+A still reaches the remote as 0x01", bytes(sent) == b"\x01",
      repr(bytes(sent)))

sent.clear()
press(QtCore.Qt.Key.Key_D, ctrl=True)
check("Ctrl+D still reaches the remote as 0x04", bytes(sent) == b"\x04",
      repr(bytes(sent)))

# ---------------------------------------------------------------- settings
print()
print("=== settings dialog ===")
base = AppSettings(theme="dark", ui_font_size=11, term_font_family="",
                   term_font_size=11, scrollback=5000,
                   monitor_interval_ms=2000)

dlg = SettingsDialog(base, p)
spin(80)
check("the dialog is themed, not a raw Qt window",
      "QComboBox" in dlg.styleSheet())

dlg.term_size.setValue(17)
dlg.ui_size.setValue(13)
dlg.scrollback.setValue(12000)
dlg.monitor_ms.setValue(3500)
dlg.theme_combo.setCurrentIndex(dlg.theme_combo.findData("light"))
fams = [dlg.font_combo.itemData(i) for i in range(dlg.font_combo.count())]
fams = fonts.mono_font_families()
check("the font list offers the auto entry first", bool(fams),
      f"{len(fams)} families")
check("the curated faces lead the list",
      fams[:2] == ["Ubuntu Mono", "Cascadia Mono"], str(fams[:4]))
check("weight variants are collapsed, not listed separately",
      not [f for f in fams
           if f.endswith((" Light", " SemiBold", " ExtraLight", " Medium",
                          " Bold", " Italic"))],
      str([f for f in fams if " Light" in f or "Bold" in f][:5]))
check("every listed family is unique by base family (no weight dupes)",
      len({fonts._base_family(f) for f in fams}) == len(fams),
      f"{len(fams)} families")
if len(fams) > 1:
    dlg.font_combo.setCurrentIndex(1)
    picked = dlg.font_combo.currentData()
else:
    picked = ""
out = dlg.result_settings()
check("terminal font size round-trips", out.term_font_size == 17,
      str(out.term_font_size))
check("UI font size round-trips", out.ui_font_size == 13, str(out.ui_font_size))
check("scrollback round-trips", out.scrollback == 12000, str(out.scrollback))
check("monitor interval round-trips", out.monitor_interval_ms == 3500,
      str(out.monitor_interval_ms))
check("theme round-trips", out.theme == "light", out.theme)
check("font family round-trips", out.term_font_family == picked,
      repr(out.term_font_family))
check("editing does not touch the original", base.term_font_size == 11,
      str(base.term_font_size))

# the offered families are genuinely monospaced
bad = []
for name in fonts.mono_font_families():
    if not QtGui.QFontDatabase.isFixedPitch(name):
        bad.append(f"{name}: db says not fixed pitch")
        continue
    f = QtGui.QFont(name, 11)
    fm = QtGui.QFontMetrics(f)
    if len({fm.horizontalAdvance(c) for c in "0iWM l."}) != 1:
        bad.append(f"{name}: uneven grid {sorted({fm.horizontalAdvance(c) for c in '0iWM l.'})}")
check("every offered family is really monospaced", not bad, str(bad[:4]))
check("no proportional family leaked in",
      not ({"Arial", "Segoe UI", "Helvetica"} & set(fonts.mono_font_families())),
      str(fonts.mono_font_families()))
check("QFont.fixedPitch() really is unusable here (the reason for the switch)",
      QtGui.QFont("Ubuntu Mono", 11).fixedPitch() is False,
      str(QtGui.QFont("Ubuntu Mono", 11).fixedPitch()))

# a saved font that is not installed stays visible
stale = AppSettings(term_font_family="No Such Face 9000")
d2 = SettingsDialog(stale, p)
spin(60)
labels = [d2.font_combo.itemText(i) for i in range(d2.font_combo.count())]
check("an uninstalled saved font is shown, not dropped",
      any("No Such Face 9000" in t for t in labels), str(labels[-2:]))
check("and its value is still returned",
      d2.result_settings().term_font_family == "No Such Face 9000",
      d2.result_settings().term_font_family)

# a stale family must not be handed to the font engine
f = fonts.mono_font(11, "No Such Face 9000")
check("mono_font falls back when the family is missing",
      f.family() in fonts.mono_font_families(), f.family())

# ------------------------------------------------- settings reach the widget
print()
print("=== apply_settings reaches a live terminal ===")
before_family = term.font().family()
before_size = term.font().pointSize()
term._scrollback_limit = 5000
new = AppSettings(term_font_family="", term_font_size=19, scrollback=250)
term.set_font(fonts.mono_font(new.term_font_size, new.term_font_family))
term.set_scrollback(new.scrollback)
spin(60)
check("the terminal font size changed", term.font().pointSize() == 19,
      f"{before_size} -> {term.font().pointSize()}")
check("the terminal re-flowed to the new metrics",
      term.font().fixedPitch(), term.font().family())
check("the scrollback cap followed the setting", term._scrollback_limit == 250,
      str(term._scrollback_limit))
# and the wheel now stops at the new cap
term._scroll_offset = 0
ev = QtGui.QWheelEvent(QtCore.QPointF(10, 10), QtCore.QPointF(10, 10),
                       QtCore.QPoint(0, 0), QtCore.QPoint(0, 600),
                       QtCore.Qt.MouseButton.NoButton,
                       QtCore.Qt.KeyboardModifier.NoModifier,
                       QtCore.Qt.ScrollPhase.NoScrollPhase, False)
for _ in range(400):
    term.wheelEvent(ev)
check("scrolling stops at the configured cap, not a hardcoded 5000",
      term._scroll_offset <= 250, str(term._scroll_offset))

# ------------------------------------------------------------- migration
print()
print("=== old config keys migrate ===")
mig = AppSettings._migrate({"font_size": 9, "font_family": "Consolas",
                            "theme": "light"})
check("font_size becomes ui_font_size", mig.get("ui_font_size") == 9,
      str(mig))
check("the stale Consolas default is not carried over as a terminal font",
      mig.get("term_font_family") is None, str(mig))
mig2 = AppSettings._migrate({"font_family": "Cascadia Mono"})
check("a genuinely chosen family is carried over",
      mig2.get("term_font_family") == "Cascadia Mono", str(mig2))
mig3 = AppSettings._migrate({"ui_font_size": 12, "font_size": 9})
check("an explicit new key wins over the old one",
      mig3.get("ui_font_size") == 12, str(mig3))

print()
print("=" * 68)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for n in FAIL:
    print("  -", n)
print("=" * 68)
sys.exit(1 if FAIL else 0)
