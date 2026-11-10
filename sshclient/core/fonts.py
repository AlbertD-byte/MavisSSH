"""字体解析与策展清单。

两套字体分工：界面用比例字体，终端用等宽字体并带上 CJK 兜底。
策展规则与排除理由都写在这里，换机器或换字体安装结果时只改这个文件。
"""
from __future__ import annotations

from PyQt6 import QtGui
from PyQt6.QtGui import QFontDatabase

# Interface face. This list does double duty: `_first_available` walks it for
# the "自动选择" default, and `ui_font_families` hands the installed members to
# the settings dialog, best first. It is hand-curated on purpose -- a machine
# here reports 410 installed families, 365 of them proportional, so "every
# installed proportional face" is not an option in a combo box, and the tail of
# that list is display faces (Algerian, Bauhaus 93, Berlin Sans FB) that
# nobody wants to read a settings dialog in. Anything the user picked that is
# *not* on this list still shows up in the dialog, flagged unavailable,
# rather than being dropped -- same rule as the terminal face.
_UI_CANDIDATES = [
    # Windows 11's own UI face first, then its classic predecessor
    "Segoe UI Variable Text", "Segoe UI",
    # the CJK UI faces, which is what this app is mostly read in
    "Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans SC",
    "Source Han Sans SC", "Source Han Sans CN", "HarmonyOS Sans SC",
    "PingFang SC",
    # SimHei is deliberately absent. Qt reports it fixed-pitch, and it is:
    # its ASCII is grid-even, so picking it renders the whole interface like a
    # code editor -- the exact thing the app moved away from when it stopped
    # setting Consolas app-wide. The YaHei and Noto faces cover Simplified
    # Chinese properly without that.
    # general-purpose sans, in rough order of how often they are worth picking
    "Inter", "Roboto", "Noto Sans", "Open Sans", "Lato", "IBM Plex Sans",
    "Fira Sans", "Ubuntu",
    # last resorts -- present on nearly anything
    "DejaVu Sans", "Arial", "Helvetica Neue",
]


#: Faces a terminal falls back to when the user's Latin face owns no glyph for
#: a code point -- CJK above all.
#:
#: Without this list Qt substitutes on its own, so CJK is never missing. What
#: it picks, however, is 宋体: measured by hashing the rendered bitmap,
#: ``QFont("Monaco")`` draws 中 byte-for-byte identically to 宋体, SimSun,
#: NSimSun and 新宋体 alike. That is a Song face -- fine in a document,
#: visibly wrong next to a terminal's sans-serif Latin. 黑体 is the requested
#: substitute; SimHei is the same file under its Latin name and only one of
#: the two is registered depending on the Windows edition, so both are listed.
#:
#: **必须跨平台**：这张表原本只有 Windows 的中文字体，于是 macOS（PingFang /
#: Hiragino）和 Linux（Noto CJK / 文泉驿）上 `cjk_fallback_families()` 恒返回
#: 空列表，缺字兜底**静默失效**——不报错，只是功能没了。所以三家都要有代表。
#: 顺序即优先级：黑体在 Windows 上仍是首选，PingFang / Noto CJK 是另两家的对位。
CJK_FALLBACK_FAMILIES = (
    # Windows
    "黑体", "SimHei",
    # macOS
    "PingFang SC", "Heiti SC", "Hiragino Sans GB", "Songti SC",
    # Linux
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC",
    "WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Droid Sans Fallback",
    # Windows 兜底
    "NSimSun", "新宋体",
)


def cjk_fallback_families() -> list[str]:
    """The installed members of :data:`CJK_FALLBACK_FAMILIES`, in order."""
    installed = set(QFontDatabase.families())
    return [f for f in CJK_FALLBACK_FAMILIES if f in installed]


# Terminal face. Ubuntu Mono leads because it is what this machine actually has
# installed for the current user (per-user, %LOCALAPPDATA%\Microsoft\Windows\
# Fonts, registered under HKCU) and Qt reports it as an exact fixed-pitch
# family. The rest stay as fallbacks so the app still renders on a machine
# without it.
_MONO_CANDIDATES = [
    "Ubuntu Mono", "Cascadia Mono", "Cascadia Code", "JetBrains Mono",
    "Consolas", "Sarasa Mono SC", "Noto Sans Mono", "DejaVu Sans Mono",
    "Menlo", "Courier New",
]


def _first_available(candidates: list[str]) -> str:
    """Pick the first candidate the system actually has installed.

    ``QFont.setFamilies`` does not reliably fall back when the *first* entry is
    missing, so resolve explicitly. Returns an empty string if none match,
    which lets Qt apply its own default.
    """
    from PyQt6.QtGui import QFontDatabase
    installed = set(QFontDatabase.families())
    for name in candidates:
        if name in installed:
            return name
    return ""


#: **界面字体的基线字重。**
#:
#: 之前没设，用的是 QFont 的默认 Normal(400)。在 Windows 的
#: Segoe UI Variable / 微软雅黑 上，11pt 的 Normal 偏细 —— 整屏看下去
#: 「发飘」，尤其菜单栏和次要文字。
#:
#: Medium(500) 是那个「加厚了但还没到 Bold」的档：正文仍然可读，而标题、
#: 数值这类本来就该抢眼的东西（QSS 里已给 7 个 role 设了 600）会
#: 跟正文拉开一档。
#:
#: **别设成 Bold(700)。** 整个界面都 Bold 会变成「加粗版海报」——
#: 字重是分层的，正文就该比标题轻。
UI_WEIGHT = QtGui.QFont.Weight.Medium


def ui_font(size: int = 10, family: str = "",
            weight: QtGui.QFont.Weight = UI_WEIGHT) -> QtGui.QFont:
    """The interface face -- every label, menu and button outside the terminal.

    Mirrors :func:`mono_font`: *family* wins when it names something the
    machine actually has, otherwise the first installed candidate is used, so a
    saved value that has since been uninstalled degrades instead of silently
    rendering in Qt's own fallback.

    *weight* —— 字重。默认 :data:`UI_WEIGHT`（Medium），比 Qt 的默认
    Normal 厚一档 —— 见那个常量的说明。
    """
    f = QtGui.QFont()
    resolved = family if _is_installed(family) else _first_available(_UI_CANDIDATES)
    if resolved:
        f.setFamily(resolved)
    f.setPointSize(size)
    f.setWeight(weight)
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferFullHinting)
    return f


def ui_font_families() -> list[str]:
    """Interface faces offered in the settings dialog, best first.

    Curated rather than exhaustive -- see ``_UI_CANDIDATES``. The order is the
    offer order, so Segoe UI Variable Text leads on Windows 11 and 微软雅黑
    follows it.
    """
    from PyQt6.QtGui import QFontDatabase

    installed = set(QFontDatabase.families())
    out: list[str] = []
    seen: set[str] = set()
    for name in _UI_CANDIDATES:
        if name in installed and name not in seen:
            out.append(name)
            seen.add(name)
    return out


def mono_font(size: int = 11, family: str = "") -> QtGui.QFont:
    """The terminal face.

    *family* wins when it names a font the system actually has; otherwise the
    first installed candidate is used, so a stale saved value (the font was
    uninstalled, or the config was hand-edited) degrades instead of rendering
    in Qt's proportional fallback and breaking the grid.

    The chosen family is followed by the installed CJK fallbacks, so a glyph
    the Latin face does not own is drawn in 黑体 rather than in whatever Song
    face Qt would reach for on its own. Metrics still come from the *first*
    family alone, which is what keeps the column grid even.
    """
    f = QtGui.QFont()
    resolved = family if _is_installed(family) else _first_available(_MONO_CANDIDATES)
    fallbacks = [x for x in cjk_fallback_families() if x != resolved]
    if resolved:
        f.setFamilies([resolved, *fallbacks])
    f.setPointSize(size)
    f.setStyleHint(QtGui.QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferFullHinting)
    return f


#: Substrings that mark a fixed-pitch family as unfit for a terminal. Matched
#: case-insensitively against the family name.
#:
#: They get in because the only test applied is `QFontDatabase.isFixedPitch`,
#: and a CJK *print* face is fixed pitch by construction -- every Han glyph
#: occupies the same em box. 楷体 / 隶书 / 幼圆 / 仿宋 are calligraphy and
#: document faces; their Latin and punctuation are cut for letterheads, not
#: for a shell prompt. The DOS rasters (Fixedsys, OCR A, and the family
#: literally named "Terminal") are fixed-width bitmaps and cannot scale to a
#: modern display.
#:
#: SimSun-ExtB / SimSun-ExtG are the hardest case in this list, and the reason
#: the exclusion exists at all. They are fixed pitch (every glyph sits in an em
#: box) and they pass the Latin width cross-check, so nothing else rejects
#: them -- but they cover only the *Supplementary Ideographic Plane* (U+20000
#: and up). `QRawFont.supportsCharacter` says False for 中, for A, for every
#: everyday code point; selecting one gives a terminal that is nothing but
#: empty boxes. Measured, not assumed.
#:
#: Deliberately *not* filtered: SimSun / NSimSun / 新宋体 and 黑体. Those are
#: legible and 新宋体 is literally the fixed-pitch member of the SimSun family,
#: built for Chinese console output. Filtering a choice a Chinese-reading user
#: might reasonably make is not this list's job; the one-line rule here is
#: "unambiguously wrong", not "subjective".
_MONO_EXCLUDE = (
    # CJK print / calligraphy faces
    "楷", "KaiTi", "隶", "LiSu", "幼圆", "YouYuan", "仿宋", "FangSong",
    # Supplementary-plane-only faces: fixed pitch, but empty at every BMP point
    "SimSun-ExtB", "SimSun-ExtG",
    # DOS-era bitmaps
    "Fixedsys", "OCR", "Terminal",
)


def _mono_is_excluded(name: str) -> bool:
    low = name.lower()
    return any(tok.lower() in low for tok in _MONO_EXCLUDE)


def mono_font_families(include_all: bool = False) -> list[str]:
    """Monospaced families offered in the settings dialog, best first.

    The predicate is :meth:`QFontDatabase.isFixedPitch`, not
    ``QFont.fixedPitch()``: the latter reports False for *every* family on
    this platform, real monospaced ones included, so trusting it would put
    Arial and Segoe UI in the list -- both of which have uniform digits and
    would pass a naive "are the digits the same width" test.

    The width cross-check stays anyway, for the case where the database says
    fixed pitch but the face Qt actually resolves is not; that shears the
    whole column grid and is far more annoying than a missing entry.

    ``include_all`` drops the ``_MONO_EXCLUDE`` filter, which is what the
    dialog's "显示全部字体" checkbox turns on: nothing is removed from the
    machine, the default list just stops advertising faces that are wrong for
    a terminal.
    """
    from PyQt6.QtGui import QFontDatabase

    installed = set(QFontDatabase.families())
    found: list[str] = [n for n in _MONO_CANDIDATES if n in installed]
    seen = {_base_family(n) for n in found}
    probe = QtGui.QFont()
    for name in sorted(installed):
        base = _base_family(name)
        if base in seen:
            # "Cascadia Code SemiBold" and friends resolve to the same grid as
            # "Cascadia Code"; offering each weight as its own terminal font
            # turns the picker into noise.
            continue
        if not QFontDatabase.isFixedPitch(name):
            continue
        # The _MONO_CANDIDATES that lead the list are the recommended terminal
        # faces and are never filtered -- the exclusion list is about the
        # tail, and skipping it here would hide a face the user already has
        # as their saved choice.
        if not include_all and name not in _MONO_CANDIDATES \
                and _mono_is_excluded(name):
            continue
        probe.setFamily(name)
        probe.setPointSize(11)
        if not _grid_even(probe):
            continue
        seen.add(base)
        found.append(name)
    return found


_VARIANT_SUFFIXES = (
    " ExtraLight", " SemiLight", " SemiBold", " ExtraBold", " Thin",
    " Medium", " Bold", " Black", " Heavy", " Light", " Italic",
)


def _base_family(name: str) -> str:
    for suffix in _VARIANT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)].strip()
    return name


def _grid_even(font: QtGui.QFont) -> bool:
    """Every character of a sample string must share one advance width."""
    fm = QtGui.QFontMetrics(font)
    return len({fm.horizontalAdvance(ch) for ch in "0iWM l."}) == 1


def _is_installed(family: str) -> bool:
    if not family:
        return False
    from PyQt6.QtGui import QFontDatabase
    return family in set(QFontDatabase.families())
