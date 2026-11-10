"""Test-only helper: load system fonts into Qt so offscreen renders show real glyphs.

Qt's `offscreen` platform plugin does not populate the font database on its own,
so every screenshot-based assertion would silently measure nothing. This is NOT
needed on a real desktop with a native platform plugin -- it exists purely so
offscreen verification is meaningful.

The font directories below are per-platform on purpose. Hardcoding
`C:\\Windows\\Fonts` (as this used to) made `QFontDatabase.families()` return
zero on macOS and Linux, which turns every "did anything render?" assertion
into a tautology rather than a failure -- a test suite that quietly tests
nothing is worse than one that is absent.
"""
import glob
import os
import sys

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PyQt6.QtGui import QFontDatabase
from PyQt6.QtWidgets import QApplication


def _font_dirs() -> list[str]:
    """Where this platform keeps its font files. Missing ones are skipped."""
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return [
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts"),
            os.path.join(local, "Microsoft", "Windows", "Fonts") if local else "",
        ]
    if sys.platform == "darwin":
        return [
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            "/Library/Fonts",
            os.path.join(home, "Library", "Fonts"),
        ]
    # Linux / BSD：系统级在前，用户级在后，后者优先级更高但这里只求"有就行"
    return [
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.join(home, ".fonts"),
        os.path.join(home, ".local", "share", "fonts"),
    ]


_FONT_DIRS = [d for d in _font_dirs() if d and os.path.isdir(d)]

_loaded = 0


def load_system_fonts() -> int:
    global _loaded
    if _loaded:
        return _loaded
    for d in _FONT_DIRS:
        # 递归：Linux 的字体常常按厂商/字重分层，不在顶层。
        for pat in ("**/*.ttf", "**/*.ttc", "**/*.otf", "**/*.otc"):
            for path in glob.glob(os.path.join(d, pat), recursive=True):
                if QFontDatabase.addApplicationFont(path) != -1:
                    _loaded += 1
    return _loaded


def boot(argv=None):
    app = QApplication.instance() or QApplication(argv or sys.argv)
    load_system_fonts()
    return app


_HOSTS: list = []


def snap(widget, bg: str = "#0b0e14"):
    """Render *widget* inside a real host window and return the QImage.

    Grabbing a never-shown top-level offscreen widget yields an empty paint
    (Qt never runs a real backing store for it), which silently makes every
    "did anything render?" assertion useless. Parenting it into a mapped host
    window forces a genuine paint pass.

    The host is retained in a module-level list -- destroying it would delete
    the widget it now owns, breaking any later use of the same instance.
    """
    from PyQt6.QtWidgets import QVBoxLayout, QWidget

    host = QWidget()
    host.setStyleSheet(f"background-color: {bg};")
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(widget)
    host.resize(widget.width(), widget.height())
    host.show()
    widget.show()          # required: without this the backing store stays empty
    app = QApplication.instance()
    for _ in range(4):
        app.processEvents()
    widget.repaint()
    host.repaint()
    app.processEvents()
    img = host.grab().toImage()
    _HOSTS.append(host)   # keep alive
    return img


def ink_count(img, bg: str = "#0b0e14", step: int = 1):
    """Count pixels that differ from the background colour."""
    from PyQt6.QtGui import QColor
    bgc = QColor(bg)
    n = 0
    for y in range(0, img.height(), step):
        for x in range(0, img.width(), step):
            if img.pixelColor(x, y) != bgc:
                n += 1
    return n
