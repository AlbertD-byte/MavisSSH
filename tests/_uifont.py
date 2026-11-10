"""The interface font picker: which faces it offers and what picking one does.

Three things are easy to get wrong and none of them show up in a screenshot:

  * `ui_font(size, family)` has to *degrade* when the saved family is gone.
    A stale value that names an uninstalled face must fall back to the
    automatic pick, not render in Qt's own default -- that is the same
    contract mono_font has, and the same reason.
  * The list must not be "every installed family". This machine has 410, 365
    of them proportional, and the tail of that is display faces. What is
    asserted is that what we offer is installed, deduped, and in offer order.
  * The dialog previews by setting the *application* font, so cancelling has to
    put the original face back. A cancel that leaves the window wearing a
    rejected font is a bug that looks like a feature working.

The cancel path is driven through the real open_settings() round trip, with
the dialog rejecting itself on a timer, so the accept/cancel branch actually
executes rather than being hand-called.
"""
import os
import pathlib
import sys
import tempfile

from PyQt6 import QtCore, QtGui, QtWidgets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6.QtGui import QFontDatabase

from sshclient.core import fonts
from sshclient.core import settings as settings_mod
from sshclient.core.settings import AppSettings
from sshclient.ui.design.v1 import palette
from sshclient.ui.settings_dialog import SettingsDialog, open_settings

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
bad: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r}" + ("" if ok else
                                                              f" (want {want!r})"))
    if not ok:
        bad.append(f"{name}: got {got!r}, want {want!r}")


def truthy(name, got):
    ok = bool(got)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r}")
    if not ok:
        bad.append(f"{name}: {got!r}")


def settle(ms=220):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


# ------------------------------------------------------------- resolution
print("=== ui_font(size, family) resolution ===")
families = fonts.ui_font_families()
print(f"  offered: {families}")
truthy("the list is not empty", families)
truthy("it stays short -- curated, not all 410 installed",
       3 <= len(families) <= 25)
truthy("every offered face is actually installed",
       all(f in set(QFontDatabase.families()) for f in families))
truthy("no duplicates", len(families) == len(set(families)))
truthy("the offer keeps the curated order (not alphabetised)",
       families == [f for f in fonts._UI_CANDIDATES if f in set(families)])

# Not `QFontDatabase.isFixedPitch`, which is the mirror image of the trap in
# mono_font_families(): it reports False for most good UI faces, and True for
# some genuinely bad ones -- SimHei's ASCII is grid-even, so the flag is True
# and the face would render the whole window like a code editor. What actually
# matters is the thing that flag is a proxy for: proportional Latin.
truthy("every offered face is proportional in latin", all(
    not fonts._grid_even(QtGui.QFont(f, 11)) for f in families))
truthy("no offered face is a terminal monospace", all(
    f not in set(fonts._MONO_CANDIDATES) for f in families))

if families:
    first = families[0]
    check("auto pick lands on the first candidate",
          fonts.ui_font(11, "").family(), first)
    check("an explicit installed family wins",
          fonts.ui_font(11, first).family(), first)
    check("size is carried through", fonts.ui_font(15, first).pointSize(), 15)

    other = families[-1] if len(families) > 1 else first
    check("a different installed family is honoured",
          fonts.ui_font(11, other).family(), other)

    # the degradation case: a face that is definitely not installed
    check("a stale family degrades to the automatic pick",
          fonts.ui_font(11, "No Such Face 12345").family(), first)
    check("an empty family degrades the same way",
          fonts.ui_font(11, "").family(), first)
    check("stale family still gets the requested size",
          fonts.ui_font(14, "No Such Face 12345").pointSize(), 14)
else:
    bad.append("no interface families offered at all")

# ------------------------------------------------------------- persistence
print("\n=== AppSettings round trip ===")
tmp = pathlib.Path(tempfile.mkdtemp())
tmpfile = tmp / "settings.json"
st = AppSettings()
st.ui_font_family = families[-1] if families else ""
st.ui_font_size = 13
st.save_to(tmpfile)
back = AppSettings.load_from(tmpfile)
check("family survives save/load", back.ui_font_family, st.ui_font_family)
check("size survives save/load", back.ui_font_size, 13)
check("default is empty (auto)", AppSettings().ui_font_family, "")

# a settings.json written before this field existed must still load
tmpfile.write_text('{"theme": "light", "ui_font_size": 14}', encoding="utf-8")
old = AppSettings.load_from(tmpfile)
check("an older file without the field loads", old.ui_font_family, "")
check("and keeps the fields it does have", old.ui_font_size, 14)

# A BOM must not read as "all defaults". PowerShell's `Set-Content -Encoding
# UTF8` writes one, and main() used to save right after loading, so a BOM was
# enough to overwrite the user's real preferences with defaults on the next
# start. That is exactly what happened to this machine's config.
st_kept = AppSettings(theme="dark", term_font_family="Monaco", scrollback=10000,
                      monitor_interval_ms=1000)
st_kept.save_to(tmpfile)
with open(tmpfile, "ab") as fh:
    # prepend a real BOM the way PowerShell does
    raw = tmpfile.read_bytes()
    tmpfile.write_bytes(b"\xef\xbb\xbf" + raw)
bommed = AppSettings.load_from(tmpfile)
check("a BOM-prefixed file still parses", bommed.theme, "dark")
check("BOM file keeps the saved family", bommed.term_font_family, "Monaco")
check("BOM file keeps a non-default value", bommed.scrollback, 10000)

# and the destructive half: a file that truly cannot be read must not be
# silently replaced by defaults. Read it, then confirm the bytes on disk are
# untouched -- main.py no longer calls save() after load, and that is the only
# thing standing between an unparseable file and a lost config.
bad_file = tmp / "broken.json"
bad_file.write_bytes(b"\xef\xbb\xbf{ this is not json at all")
broken = AppSettings.load_from(bad_file)
check("an unparseable file yields defaults", broken.theme, AppSettings().theme)
before_bytes = bad_file.read_bytes()
_ = AppSettings.load_from(bad_file)
check("reading it again does not rewrite it", bad_file.read_bytes(), before_bytes)
truthy("and the file is still the user's problem to see, not silently rewritten",
       b"not json" in bad_file.read_bytes())

# ------------------------------------------------------------- the dialog
print("\n=== the dialog ===")
st = AppSettings(theme="light", ui_font_size=11, ui_font_family="")
dlg = SettingsDialog(st, palette.get_palette("light"))
truthy("the combo exists", dlg.ui_font_combo is not None)
check("default lands on 自动选择", dlg.ui_font_combo.currentData(), "")
check("自动选择 is the first entry", dlg.ui_font_combo.itemText(0), "自动选择")
check("the combo offers what theme lists",
      [dlg.ui_font_combo.itemText(i) for i in range(1, dlg.ui_font_combo.count())],
      families)
check("size prefilled from settings", dlg.ui_size.value(), 11)

if families:
    # picking a face is reflected in result_settings
    target = families[-1]
    dlg.ui_font_combo.setCurrentIndex(dlg.ui_font_combo.findData(target))
    check("picking a face is carried into the result",
          dlg.result_settings().ui_font_family, target)
    dlg.ui_size.setValue(15)
    check("and so is the size", dlg.result_settings().ui_font_size, 15)
else:
    target = ""
    dlg.ui_size.setValue(15)

# a saved face that is not on the list must stay visible
st2 = AppSettings()
st2.ui_font_family = "Some Face That Was Uninstalled"
dlg2 = SettingsDialog(st2, palette.get_palette("light"))
texts = [dlg2.ui_font_combo.itemText(i) for i in range(dlg2.ui_font_combo.count())]
truthy("an uninstalled saved face is listed, flagged",
       any("Some Face That Was Uninstalled" in t and "不可用" in t for t in texts))
check("and it is what the dialog comes up showing",
      dlg2.ui_font_combo.currentData(), "Some Face That Was Uninstalled")
check("and result_settings keeps it verbatim",
      dlg2.result_settings().ui_font_family, "Some Face That Was Uninstalled")

# ------------------------------------------- the live preview, and undoing it
print("\n=== live preview, and cancel puts it back ===")

before_family = app.font().family()
before_size = app.font().pointSize()

# Drive the real open_settings() round trip, with the dialog rejecting itself.
# The dialog is passed in rather than built here: open_settings creates its
# own privately and then exec()s it, so without the hook a test has no way to
# reach the very dialog whose accept/cancel branch is under test.
holder = SettingsDialog(AppSettings(ui_font_size=11, ui_font_family=""),
                         palette.get_palette("light"))
if target:
    holder.ui_font_combo.setCurrentIndex(holder.ui_font_combo.findData(target))
    settle(120)
    check("choosing a face previews it on the whole app",
          app.font().family(), target)
check("the size previews too", app.font().pointSize(), holder.ui_size.value())

app_before = AppSettings(ui_font_size=before_size, ui_font_family="")
QtCore.QTimer.singleShot(350, holder.reject)
value = open_settings(None, app_before, palette.get_palette("light"), dlg=holder)
check("a cancelled dialog returns None", value, None)
check("cancelling restores the original face", app.font().family(), before_family)
check("cancelling restores the original size", app.font().pointSize(), before_size)
holder.close()

# ------------------------------------- the terminal list, and its filter
print("\n=== the terminal font list and 显示全部字体 ===")
everything = fonts.mono_font_families(include_all=True)
default = fonts.mono_font_families(include_all=False)
print(f"  include_all={len(everything)}  default={len(default)}")
truthy("the default list is shorter", len(default) < len(everything))
truthy("but never longer", len(default) <= len(everything))
check("every default entry is also in the full list",
      [f for f in default if f not in everything], [])
check("nothing is added by the filter, only removed",
      [f for f in default if f not in everything], [])
# Everything the filter drops has to match a stated reason. This used to
# re-list the exclusion tokens here, and adding SimSun-ExtB/G to the real
# list made this assertion go red -- a second copy of one list, drifting.
# Ask the predicate that owns the list instead, and print what it dropped so
# the reason for each entry stays visible.
dropped = [f for f in everything if f not in default]
print(f"  filtered out: {dropped}")
truthy("everything dropped matches a stated exclusion reason",
       all(fonts._mono_is_excluded(f) for f in dropped))
truthy("and none of them is silently kept back out of the list",
       not [f for f in dropped if not fonts._mono_is_excluded(f)])
# the choices a Chinese-reading user might deliberately make must survive
for keep in ("NSimSun", "新宋体", "SimHei", "黑体"):
    if keep in everything:
        truthy(f"{keep} is kept by default", keep in default)
truthy("the recommended candidates are never filtered out",
       all(f in default for f in fonts._MONO_CANDIDATES
           if f in everything))

st_t = AppSettings(theme="light", term_font_family="")
dt = SettingsDialog(st_t, palette.get_palette("light"))
check("the dialog starts filtered",
      dt.show_all_fonts.isChecked(), False)
combo_families = [dt.font_combo.itemText(i)
                  for i in range(1, dt.font_combo.count())]
check("the combo starts on the default list", combo_families, default)

# toggling must widen the list *and* keep the selection
dt.font_combo.setCurrentIndex(dt.font_combo.findData("Consolas"))
dt.show_all_fonts.setChecked(True)
check("ticking 显示全部 widens the list",
      dt.font_combo.count() - 1, len(everything))
check("and keeps the selection", dt.font_combo.currentData(), "Consolas")
dt.show_all_fonts.setChecked(False)
check("unticking narrows it back", dt.font_combo.count() - 1, len(default))
check("and still keeps the selection", dt.font_combo.currentData(), "Consolas")

# a saved face that is *filtered out* must stay selected, flagged as filtered
# rather than unavailable -- it is installed, just hidden
st_h = AppSettings(theme="light", term_font_family="楷体")
dh = SettingsDialog(st_h, palette.get_palette("light"))
h_label = dh.font_combo.currentText()
print(f"  saved-but-filtered face shows as {h_label!r}")
check("it is still the selected entry", dh.font_combo.currentData(), "楷体")
truthy("flagged as filtered, not missing", "已过滤" in h_label)
check("and result_settings keeps it", dh.result_settings().term_font_family,
      "楷体")

# a face that is genuinely gone stays flagged the other way
st_g = AppSettings(theme="light", term_font_family="No Such Mono Face")
dg = SettingsDialog(st_g, palette.get_palette("light"))
truthy("a genuinely uninstalled face says so",
       "当前不可用" in dg.font_combo.currentText())
dh.close()
dg.close()
dt.close()

# ------------------------------------------------- MainWindow actually applies
print("\n=== MainWindow.apply_settings pushes it out ===")
from sshclient.ui import main_window as mw_mod

tmpdir = pathlib.Path(tempfile.mkdtemp())
store = settings_mod.SessionStore(tmpdir / "sessions.json")
win = mw_mod.MainWindow(AppSettings(theme="light"), store)
host = QtWidgets.QWidget()
lay = QtWidgets.QVBoxLayout(host)
lay.setContentsMargins(0, 0, 0, 0)
lay.addWidget(win)
host.resize(1100, 760)
host.show()
settle(350)

edited = AppSettings(theme="light", ui_font_size=12, ui_font_family=target)
win.apply_settings(edited)
settle(250)
check("apply_settings sets the app font family", app.font().family(), target)
check("apply_settings sets the app font size", app.font().pointSize(), 12)
# the docstring claimed "adopt" and the body did not; it used to be the
# caller's job, which meant any other caller silently did nothing
check("apply_settings adopts the settings object",
      win.settings.ui_font_family, target)
check("and the size", win.settings.ui_font_size, 12)
host.close()

print()
print("=== QSS 的 font-size 单位：生成出来必须是 pt ===")
#
# **为什么这条要钉。** `font-size: 13px` 让 Qt 走 `setPixelSize()`，
# `pointSizeF()` 返回 **-1**。后果有两个：本仓多处从 pointSize 派生尺寸时
# 只能各写各的补丁；以及 **Qt 自己在展开 QComboBox 弹层时会
# `setPointSize(-1)` 并打出警告**（见本节末尾）。
#
# **钉的是「pt」这个性质，不是某个具体数字** —— 但基准字号那条额外把换算
# 系数锁死成 0.75，因为「数字照抄」是这次改动最像、也最贵的错：
# `13pt` 渲染出来大 31%（实测高 21 vs 16），那是一次看得见的界面翻车。

import re  # noqa: E402

from sshclient.core.settings import UI_SIZE_REF  # noqa: E402
from sshclient.ui.design.v1 import qss  # noqa: E402

_FONT_RE = re.compile(r"font-size:\s*([0-9.]+)(px|pt)\b")
_sheet_ref = qss.build_qss(palette.get_palette("dark"), ui_size=UI_SIZE_REF)
_sheet_big = qss.build_qss(palette.get_palette("dark"), ui_size=18)

for _tag, _sheet in (("11pt", _sheet_ref), ("18pt", _sheet_big)):
    _vals = _FONT_RE.findall(_sheet)
    _left = [v for v, u in _vals if u == "px"]
    truthy(f"[{_tag}] 表里确实有 font-size", _vals)
    check(f"[{_tag}] 表里一处 px 都不剩", _left, [])
    truthy(f"[{_tag}] 换算后的值至少有一个 pt", [u for _, u in _vals] == ["pt"] * len(_vals))

# **换算系数锁死。** 基准是 `QWidget { font-size: 13px }`，13 × 0.75 = 9.75。
# 把 _FONT_PX_TO_PT 改成 1.0（数字照抄）这里立刻红。
truthy("基准字号换算成 9.75pt（系数 0.75，不是照抄）",
       "font-size: 9.75pt" in _sheet_ref)
truthy("18pt 那张缩放后是 15.75pt（先按 px 缩放再换算）",
       "font-size: 15.75pt" in _sheet_big)

# **只换 font-size，长度属性一律留在 px。** 全换会把 padding / 圆角 / min-height
# 一起放大 1.33 倍，而它们是按 13px 字号量出来的。
for _prop in ("padding", "min-height", "border-radius", "margin"):
    _got = re.findall(r"\b%s:\s*([0-9.]+)(px|pt)\b" % _prop, _sheet_ref)
    check(f"{_prop} 仍然是 px（没被一起换成 pt）",
          sorted({u for _, u in _got}), ["px"])

# **真实控件读出来不是 -1。** 这是整件事的目的，字符串匹配只证明表对了，
# 这条证明 Qt 真的按 pt 解了。
_host = QtWidgets.QWidget()
_host.setStyleSheet(_sheet_ref)
_lay = QtWidgets.QVBoxLayout(_host)
_plain = QtWidgets.QLabel("普通标签", _host)
_plain.setProperty("role", "muted")
_lay.addWidget(_plain)
_host.show()
settle(200)
truthy("普通标签的 pointSizeF() 不再是 -1", _plain.font().pointSizeF() > 0)
# **钉「muted 档的字号 = muted 基础 px 字号 × 换算系数」，不钉具体数字。**
#
# 原来这里写的是 `== 9.0`（12px × 0.75）—— 一个**具体字号**。后来界面字号
# 体系整体调过（muted 从 12px 变成 14px），这里就红了，而**产品并没有坏**：
# 换算本身依然正确，只是它换算的输入变了。钉具体数字的判据必然随实现漂移。
#
# 现在改成从**样式表本身**把 muted 档的 px 字号读出来，再乘换算系数，
# 断言控件实际拿到的 pointSizeF 与之一致。这样：
#   · 换算系数被改坏 → 红（这才是要抓的）
#   · muted 基础字号被调整 → 仍然绿（正确行为）
#   · muted 档的 pt 值被手抄成别的数 → 红
_muted_pt = None
for _m in re.finditer(r'QLabel\[role="muted"\][^{]*\{([^}]*)\}', _sheet_ref):
    _fs = re.search(r"font-size:\s*([0-9.]+)pt", _m.group(1))
    if _fs:
        _muted_pt = float(_fs.group(1))
        break
truthy("样式表里找得到 muted 档的 pt 字号", _muted_pt is not None)
# muted 档在样式表里是 14px × 0.75 = 10.5pt；用**系数**核对而不是硬编码。
check("muted 标签的 pointSizeF() 与样式表一致",
      _plain.font().pointSizeF(), _muted_pt)
truthy("像素字号确实没被设上", _plain.font().pixelSize() == -1)

# **元断言：证明上面那条「与样式表一致」不是恒绿的假货。**
# 把样式表里 muted 档的 pt 值挪动一格，判据必须会红 —— 否则「控件读数
# 和表里的数字一样」这种断言可以永远成立，什么都测不到。
_muted_broken = (_muted_pt or 0) + 0.5
truthy("元断言：样式表里 muted 档的 pt 挪动一格后，判据必须会红",
       abs(_plain.font().pointSizeF() - _muted_broken) >= 1e-6,
       )

# **渲染尺寸与换算前逐像素一致。** 这一条挡「照抄数字」那种改法：
# `13px` 与 `9.75pt` 的高度和中文 advance 必须完全相等。
# 不比高度也不比 advance 的话，13pt 那档（高 21）能从缝里溜过去。
_metrics = {}
for _decl in ("font-size: 13px;", "font-size: 9.75pt;", "font-size: 13pt;"):
    _w = QtWidgets.QWidget()
    _w.setStyleSheet("QWidget { %s }" % _decl)
    _l = QtWidgets.QLabel("中文样例 AaBb", _w)
    QtWidgets.QVBoxLayout(_w).addWidget(_l)
    _w.show()
    app.processEvents()
    _fm = _l.fontMetrics()
    _metrics[_decl] = (_fm.height(), _fm.horizontalAdvance("中文样例"))
    _w.close()
    app.processEvents()
print(f"  渲染对照：{_metrics}")
check("9.75pt 与 13px 渲染完全一致（换了个单位，界面一像素没动）",
      _metrics["font-size: 9.75pt;"], _metrics["font-size: 13px;"])
truthy("13pt 确实更大（所以数字不能照抄，这条判据才不是恒绿）",
       _metrics["font-size: 13pt;"][0] > _metrics["font-size: 13px;"][0])
_host.close()

# --- 元断言：把换算摘掉，上面必须红 -----------------------------------
_real_font_re, _real_to_pt = qss._FONT_SIZE_RE, qss._font_size_to_pt
_real_factor = qss._FONT_PX_TO_PT
try:
    qss._font_size_to_pt = lambda s: s        # 换算整个不做事
    _s = qss.build_qss(palette.get_palette("dark"), ui_size=UI_SIZE_REF)
    _n = len([1 for _, u in _FONT_RE.findall(_s) if u == "px"])
    check("元断言：换算摘掉之后「表里一处 px 都不剩」会红", _n > 0, True)
finally:
    qss._font_size_to_pt = _real_to_pt
try:
    qss._FONT_PX_TO_PT = 1.0                  # 数字照抄
    _s = qss.build_qss(palette.get_palette("dark"), ui_size=UI_SIZE_REF)
    check("元断言：系数改成 1.0 之后「基准 9.75pt」会红",
          "font-size: 9.75pt" in _s, False)
    check("元断言：系数改成 1.0 之后「渲染一致」也不再成立",
          "font-size: 13pt" in _s, True)
finally:
    qss._FONT_PX_TO_PT = _real_factor
# 对照：还原之后必须回绿，否则上面两条红是环境问题不是被测问题
_s = qss.build_qss(palette.get_palette("dark"), ui_size=UI_SIZE_REF)
check("元断言：还原之后回到 9.75pt（证明上面那些红是被测代码造成的）",
      "font-size: 9.75pt" in _s, True)

# --- 警告本身：展开 QComboBox 弹层不该再报 setPointSize(-1) -----------
# **判的是「一条都不剩」，不是「变少了」。**
#
# 注意 Qt 的信息处理函数是**全局**的，装上去之后所有 Qt 消息都会被截走，
# 所以这一段跑完必须还原 —— 否则后面若有 Qt 报错，会因为处理函数已经被换掉
# 而**一个字都不打印**，判据看起来全绿其实什么都没看见。
_seen: list[str] = []
_prev = QtCore.qInstallMessageHandler(
    lambda mode, ctx, msg: _seen.append(msg) if "setPointSize" in msg else None)


def _popup_roundtrip(sheet: str) -> list[str]:
    _seen.clear()
    _h = QtWidgets.QWidget()
    _h.setStyleSheet(sheet)
    _box = QtWidgets.QComboBox(_h)
    _box.addItems(["甲", "乙", "丙"])
    QtWidgets.QVBoxLayout(_h).addWidget(_box)
    _h.show()
    app.processEvents()
    _box.showPopup()
    for _ in range(8):
        app.processEvents()
    _box.hidePopup()
    app.processEvents()
    _h.close()
    app.processEvents()
    return list(_seen)


try:
    _with_pt = _popup_roundtrip(_sheet_ref)
    # 对照组：同一段代码、同一个弹层，样式表换回 px 就**必须**报出来。
    # 没有这条，上面那条可能是「这个弹层压根不触发」而空绿。
    _with_px = _popup_roundtrip("QWidget { font-size: 13px; }")
finally:
    QtCore.qInstallMessageHandler(_prev)
print(f"  弹层警告：pt={_with_pt} px={_with_px}")
check("展开 QComboBox 弹层不再报 setPointSize（pt 样式表）", _with_pt, [])
truthy("对照：同样这段代码，px 样式表下确实报得出来（所以上面不是空绿）",
       _with_px)

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print("interface font: all checks passed")
sys.exit(0)
