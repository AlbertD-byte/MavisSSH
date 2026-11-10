"""Which face draws the characters the chosen terminal font does not have.

Two related things, both of which are invisible in a screenshot unless you
know what you are looking at:

  * `SimSun-ExtB` / `SimSun-ExtG` are fixed pitch and pass the Latin width
    check, so nothing else rejects them. They cover only the Supplementary
    Ideographic Plane, though: `QRawFont.supportsCharacter` says False for A,
    for 中, for every everyday code point. Offering them in a terminal picker
    is offering a face that is empty at every character anyone will type.

  * A Latin terminal face owns no 中. Qt substitutes on its own, so CJK was
    never missing -- but hashing the rendered bitmap shows what it picks:
    `QFont("Monaco")` draws 中 byte-for-byte identically to 宋体, SimSun,
    NSimSun and 新宋体 alike. A Song face is a document face; next to a
    terminal's sans-serif Latin it reads as the wrong tool. The fallback is
    specified rather than inherited.

Hashing the bitmap is the only assertion here that actually discriminates. Two
CJK faces both produce "some CJK on screen", so an eyeball test passes against
the old code. `is`/identity checks on the font object do not work either --
QFont is a value type, and the substituted glyph never appears in
`QFont.family()`. What does differ is the set of pixels.

The regression that matters most is the quiet one: naming extra families must
not disturb the Latin, or the column grid shears and the whole terminal looks
broken. That is checked byte-for-byte too.
"""
import os
import sys

from PyQt6 import QtGui, QtWidgets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtGui import QFontDatabase

from sshclient.core import fonts
from sshclient.core.settings import AppSettings
from sshclient.ui.design.v1 import palette
from sshclient.ui.settings_dialog import SettingsDialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
bad: list[str] = []

HEI = "黑体"          # 黑体
SONG = "宋体"         # 宋体 -- the face Qt picks when nobody says otherwise
HAN = "中文"
LATIN = "Aéw root@box"


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r}"
          + ("" if ok else f" (want {want!r})"))
    if not ok:
        bad.append(f"{name}: got {got!r}, want {want!r}")


def truthy(name, got):
    ok = bool(got)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r}")
    if not ok:
        bad.append(f"{name}: {got!r}")


def bitmap(font, text):
    """SHA of the rendered text -- the fingerprint of which face drew it."""
    img = QtGui.QImage(260, 48, QtGui.QImage.Format.Format_RGB32)
    img.fill(QtGui.QColor("#000000"))
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing, False)
    p.setPen(QtGui.QColor("#ffffff"))
    p.setFont(font)
    p.drawText(4, 34, text)
    p.end()
    return img


def fingerprint(font, text):
    img = bitmap(font, text)
    return bytes(img.bits().asstring(img.sizeInBytes()))


def legacy(size, family):
    """The font mono_font() used to build: one family, no fallback chain."""
    f = QtGui.QFont()
    f.setFamily(family)
    f.setPointSize(size)
    f.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferFullHinting)
    return f


# ------------------------------------------------- the supplementary-plane faces
print("=== SimSun-ExtB / SimSun-ExtG are filtered out of the picker ===")
installed = set(QFontDatabase.families())
default = fonts.mono_font_families(include_all=False)
everything = fonts.mono_font_families(include_all=True)
print(f"  default={len(default)}  include_all={len(everything)}")

for name in ("SimSun-ExtB", "SimSun-ExtG"):
    if name not in installed:
        print(f"  (skip {name}: not installed here)")
        continue
    truthy(f"{name} is installed but hidden by default", name not in default)
    truthy(f"{name} comes back with 显示全部字体", name in everything)
    raw = QtGui.QRawFont.fromFont(QtGui.QFont(name, 11))
    truthy(f"{name} really is fixed pitch (why nothing else caught it)",
           QFontDatabase.isFixedPitch(name))
    truthy(f"{name} has no 中 (why it is useless in a terminal)",
           not raw.supportsCharacter(ord("中")))

# the filter must not have taken anything else with it
check("nothing is added by the filter, only removed",
      [f for f in default if f not in everything], [])
for keep in ("NSimSun", "新宋体", "SimHei", HEI):
    if keep in installed:
        truthy(f"{keep} is still offered by default", keep in default)

# a saved SimSun-ExtB must stay selectable, flagged as filtered -- the face is
# installed, only hidden, and saying "unavailable" would be a lie
st_e = AppSettings(theme="light", term_font_family="SimSun-ExtB")
if "SimSun-ExtB" in installed:
    dlg_e = SettingsDialog(st_e, palette.get_palette("light"))
    label = dlg_e.font_combo.currentText()
    print(f"  saved SimSun-ExtB shows as {label!r}")
    check("it is still the selected entry", dlg_e.font_combo.currentData(),
          "SimSun-ExtB")
    truthy("flagged as filtered, not missing", "已过滤" in label)
    dlg_e.close()

# ----------------------------------------------------------------- the fallback
print("\n=== CJK falls back to 黑体, not to the Song face Qt picks ===")
fallbacks = fonts.cjk_fallback_families()
print(f"  installed CJK fallbacks: {fallbacks}")
truthy("at least one CJK fallback is installed", fallbacks)
if HEI in installed:
    truthy("黑体 is the first choice", fallbacks and fallbacks[0] == HEI)

# Faces that own no CJK, so the substitution is actually exercised.
latin_only = [f for f in ("Monaco", "Ubuntu Mono", "Consolas", "Cascadia Mono",
                          "DejaVu Sans Mono")
              if f in installed and not QtGui.QRawFont.fromFont(
                  QtGui.QFont(f, 11)).supportsCharacter(ord("中"))]
print(f"  faces with no 中 of their own: {latin_only}")
truthy("this machine has such a face to test with", latin_only)

for name in latin_only:
    for pt in (9, 11, 14):
        f = fonts.mono_font(pt, name)
        got = fingerprint(f, HAN)
        if HEI in installed:
            check(f"{name} {pt}pt draws {HAN} in {HEI}",
                  got == fingerprint(legacy(pt, HEI), HAN), True)
        if SONG in installed:
            truthy(f"{name} {pt}pt does not draw it in {SONG}",
                   got != fingerprint(legacy(pt, SONG), HAN))
        # the documented reason for naming a fallback at all
        truthy(f"{name} {pt}pt alone would have drawn {SONG}",
               fingerprint(legacy(pt, name), HAN)
               == fingerprint(legacy(pt, SONG), HAN))

# ------------------------------------------------- and nothing else moved
print("\n=== naming extra families did not disturb anything else ===")
for name in latin_only:
    for pt in (9, 11, 14):
        f = fonts.mono_font(pt, name)
        check(f"{name} {pt}pt: the chosen family is still first",
              f.family(), name)
        check(f"{name} {pt}pt: latin is byte-identical to before",
              fingerprint(f, LATIN), fingerprint(legacy(pt, name), LATIN))
        check(f"{name} {pt}pt: the cell width did not change",
              QtGui.QFontMetrics(f).horizontalAdvance("M"),
              QtGui.QFontMetrics(legacy(pt, name)).horizontalAdvance("M"))
        truthy(f"{name} {pt}pt: the grid is still even", fonts._grid_even(f))
        # A substituted Han occupies two cells (pyte writes it that way), so a
        # face whose Han is wider than two cells would shear every column.
        cells = 2 * QtGui.QFontMetrics(f).horizontalAdvance("M")
        han = QtGui.QFontMetrics(QtGui.QFont(HEI, pt)).horizontalAdvance("中") \
            if HEI in installed else 0
        truthy(f"{name} {pt}pt: a substituted 中 fits its two cells",
               han <= cells)

# a face that already owns CJK must come through unchanged
if HEI in installed:
    f = fonts.mono_font(11, HEI)
    check("base=黑体 keeps 黑体 first", f.family(), HEI)
    check("base=黑体 does not list itself twice", f.families().count(HEI), 1)
    check("base=黑体 still draws 中文 in 黑体",
          fingerprint(f, HAN), fingerprint(legacy(11, HEI), HAN))

# the degradation path: a stale saved family still has to get a fallback
stale = fonts.mono_font(11, "No Such Mono Face 12345")
auto = fonts.mono_font(11, "")
check("a stale family degrades to the automatic pick",
      stale.family(), auto.family())
truthy("and still carries the CJK fallback",
       set(fonts.cjk_fallback_families()) & set(stale.families()))

# ------------------------------------------------ it reaches the real terminal
print("\n=== and the terminal widget actually paints it ===")
from sshclient.ui.terminal_widget import TerminalWidget


def terminal_bitmap(family):
    w = TerminalWidget()
    w.set_font(fonts.mono_font(11, family))
    w.resize(420, 60)
    w._recompute_grid()
    w._stream = __import__("pyte").Stream(w._screen)
    w._stream.feed(HAN)
    w.update()
    app.processEvents()
    return w, w.grab().toImage()


if latin_only:
    name = latin_only[0]
    w_new, img_new = terminal_bitmap(name)
    check("the widget's own font carries the fallback",
          HEI in w_new.font().families() or HEI in w_new._font.families(), True)

    # the same widget built the old way, for the pixels to be compared against.
    # set_font() rather than a hand-rolled assignment, so the only difference
    # between the two widgets is how the QFont was built.
    w_old = TerminalWidget()
    w_old.set_font(legacy(11, name))
    w_old.resize(420, 60)
    w_old._recompute_grid()
    w_old._stream = __import__("pyte").Stream(w_old._screen)
    w_old._stream.feed(HAN)
    w_old.update()
    app.processEvents()
    img_old = w_old.grab().toImage()

    check("the grid is the same size in both",
          (w_new._cols, w_new._rows), (w_old._cols, w_old._rows))
    truthy("but the CJK pixels differ -- the face really changed",
           img_new != img_old)
    if HEI in installed:
        truthy("and they are not the Song face's pixels",
               fingerprint(w_new.font(), HAN) == fingerprint(legacy(11, HEI), HAN))

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print("glyph fallback: all checks passed")
sys.exit(0)
