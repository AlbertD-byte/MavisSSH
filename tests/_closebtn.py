"""Which QSS property on ::close-button kills the default icon?

tabsClosable reserves space for the close button, but any styling on
QTabBar::close-button can make Qt stop drawing the native glyph and draw
nothing in its place. This renders the same tab bar under several QSS
variants and measures whether the close-button area actually has ink in it,
so the fix targets the real cause instead of a guess.

Run: .venv\\Scripts\\python.exe _closebtn.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

_app = QtWidgets.QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def spin(ms=120):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        _app.processEvents()


BASE_TAB = """
QTabBar::tab {
    background: transparent; color: #cccccc;
    border: 1px solid transparent; border-bottom: none;
    border-top-left-radius: 9px; border-top-right-radius: 9px;
    border-bottom-left-radius: 0px; border-bottom-right-radius: 0px;
    padding: 8px 8px 8px 18px; margin-right: 6px;
}
QTabBar::tab:selected { background: #1e2129; border-color: #2b303b; color: #ffffff; }
"""

VARIANTS = {
    "no close-button rule at all": "",
    "only subcontrol-position": """
QTabBar::close-button { subcontrol-position: right; }
""",
    "subcontrol-position + margins": """
QTabBar::close-button {
    subcontrol-position: right; margin-left: 8px; margin-right: 8px;
}
""",
    "margins + border-radius (current)": """
QTabBar::close-button {
    subcontrol-position: right; margin-left: 8px; margin-right: 8px;
    border-radius: 4px;
}
""",
    "margins + border-radius + hover bg": """
QTabBar::close-button {
    subcontrol-position: right; margin-left: 8px; margin-right: 8px;
    border-radius: 4px;
}
QTabBar::close-button:hover { background-color: #e5484d; }
""",
    "generic selector (unnamespaced)": """
QTabBar { qproperty-drawBase: 0; }
QTabBar::close-button { subcontrol-position: right; margin-right: 8px; }
""",
}


def close_button_has_ink(qss: str) -> tuple[bool, int]:
    """Render one tab and report whether the close-button area has ink."""
    tabs = QtWidgets.QTabWidget()
    tabs.setTabsClosable(True)
    tabs.setDocumentMode(True)
    tabs.addTab(QtWidgets.QLabel("x"), "deploy@10.0.0.5:22")
    tabs.resize(400, 120)
    tabs.setStyleSheet(BASE_TAB + qss)
    tabs.show()
    spin(140)

    bar = tabs.tabBar()
    rect = bar.tabRect(0)
    # The close button hugs the right edge of the tab, inside the border.
    size = bar.tabButtonSize(0) if hasattr(bar, "tabButtonSize") else None
    if size and size.isValid():
        btn = rect.adjusted(rect.width() - size.width() - 12, 4, -8, -size.height() - 4)
    else:
        btn = rect.adjusted(rect.width() - 34, 6, -10, rect.height() - 6)

    img = tabs.grab().toImage()
    distinct = set()
    for yy in range(max(0, btn.top()), min(img.height(), btn.bottom())):
        for xx in range(max(0, btn.left()), min(img.width(), btn.right())):
            distinct.add(img.pixel(xx, yy))
    tabs.hide()
    tabs.deleteLater()
    spin(40)
    # More than a couple of shades means something was drawn there.
    return len(distinct) > 3, len(distinct)


print("=== does the close button actually draw? ===")
results = {}
for name, qss in VARIANTS.items():
    ok, shades = close_button_has_ink(qss)
    results[name] = ok
    print(f"  {name:<42} drawn={ok}  distinct shades={shades}")

check("baseline (no rule) draws the icon",
      results["no close-button rule at all"])
check("margins alone do NOT kill the icon",
      results["subcontrol-position + margins"])
check("border-radius KILLS the icon (the current bug)",
      not results["margins + border-radius (current)"],
      "this is what the stylesheet had")
check("dropping border-radius restores it",
      results["subcontrol-position + margins"])

print("\n" + "=" * 68)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for n in FAIL:
    print("  -", n)
print("=" * 68)
sys.exit(1 if FAIL else 0)
