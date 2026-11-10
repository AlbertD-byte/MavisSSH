"""Regression test for the session tab close button.

The bug this locks down: the glyph sat 1px from the tab's right border
("X 贴着右边的 tab 框"). Neither QSS margin nor QSS padding moved it -- see
_tabpad.py, which swept padding-right 8..32px and measured a constant 1px gap.

Checks, over both themes and three window widths:
  1. a closable tab owns exactly one TabCloseButton
  2. the button is fully inside its tab rect
  3. the right gap equals TabCloseController.INSET
  4. the button is vertically centred in the tab
  5. the tab reserves enough width for the label not to reach the glyph
  6. the gap survives a tab move (tabs are movable)
  7. a non-closable tab (the welcome page) has no button
  8. closing tab 0 re-targets the remaining buttons -- a captured index would
     close the wrong tab once the indices shifted
  9. clicking the button emits tabCloseRequested for the tab that owns it
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtGui, QtWidgets

from sshclient.ui import tab_close
from sshclient.ui.design.v1 import palette, qss

TOL = 1.0
LABELS = ["one@10.0.0.1:22", "two@10.0.0.2:2222"]
SYM = "长标签-user@very-long-hostname.example.com:22"


def build(p, closable):
    tabs = QtWidgets.QTabWidget()
    tabs.setObjectName("sessionTabs")
    tabs.setTabsClosable(False)
    tabs.setMovable(True)
    tabs.setDocumentMode(True)
    tabs.setStyleSheet(qss.build_qss(p))
    for lab in LABELS:
        page = QtWidgets.QLabel(lab)
        if not closable:
            page.setProperty(tab_close.NO_CLOSE, True)
        tabs.addTab(page, lab)
    holder = QtWidgets.QWidget()
    hl = QtWidgets.QVBoxLayout(holder)
    hl.setContentsMargins(16, 12, 16, 0)
    hl.addWidget(tabs, 1)
    ctrl = tab_close.TabCloseController(tabs, p, holder)
    return holder, tabs, ctrl


def btn_of(bar, i):
    """The close button sitting on tab *i*, or None if there isn't exactly one."""
    r = bar.tabRect(i)
    cands = [b for b in bar.findChildren(tab_close.TabCloseButton)
             if b.isVisible() and r.contains(b.geometry().center())]
    return cands[0] if len(cands) == 1 else None


def pump(app, n=8):
    for _ in range(n):
        app.processEvents()


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    fails = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    for tname in ("dark", "light"):
        p = palette.get_palette(tname)
        inset = tab_close.TabCloseController.INSET
        for width in (1020, 1280, 1600):
            tag = f"{tname}/{width}"

            # --- 1..5 with two closable tabs -----------------------------
            host, tabs, ctrl = build(p, closable=True)
            host.resize(width, 820)
            host.show()
            pump(app)
            bar = tabs.tabBar()

            for i in range(tabs.count()):
                b = btn_of(bar, i)
                check(isinstance(b, tab_close.TabCloseButton),
                      f"{tag}: tab {i} has no TabCloseButton ({b!r})")
                if not isinstance(b, tab_close.TabCloseButton):
                    continue
                r = bar.tabRect(i)
                g = b.geometry()
                check(g.left() >= r.left() and g.right() <= r.right(),
                      f"{tag}: tab {i} button outside tab rect "
                      f"tab={r.x()},{r.width()} btn={g.x()},{g.width()}")
                gap = r.right() - g.right()
                check(abs(gap - inset) <= TOL,
                      f"{tag}: tab {i} right gap {gap} != INSET {inset}")
                top = g.top() - r.top()
                bot = r.bottom() - g.bottom()
                check(abs(top - bot) <= 2,
                      f"{tag}: tab {i} not centred top={top} bottom={bot}")

                fm = QtGui.QFontMetrics(bar.font())
                need = (19 + fm.horizontalAdvance(tabs.tabText(i))
                        + 4 + b.width() + inset + 1)
                check(r.width() >= need,
                      f"{tag}: tab {i} width {r.width()} < {need} needed for "
                      f"label + glyph (label would slide under the X)")

            # --- 6 tab move ---------------------------------------------
            bar.moveTab(0, 1)
            pump(app)
            for i in range(tabs.count()):
                r, g = bar.tabRect(i), btn_of(bar, i).geometry()
                check(abs((r.right() - g.right()) - inset) <= TOL,
                      f"{tag}: tab {i} right gap {r.right() - g.right()} broke "
                      f"after moveTab")

            # --- 8 index shift on removal --------------------------------
            got = []

            def _rec(i):
                got.append(i)

            tabs.tabCloseRequested.connect(_rec)
            before = [tabs.tabText(i) for i in range(tabs.count())]
            tabs.removeTab(0)
            pump(app)
            check([tabs.tabText(i) for i in range(tabs.count())] == before[1:],
                  "removeTab(0) did not shift labels as expected")
            for i in range(tabs.count()):
                b = btn_of(bar, i)
                check(isinstance(b, tab_close.TabCloseButton),
                      f"{tag}: tab {i} lost its button after a removal")
                if isinstance(b, tab_close.TabCloseButton):
                    r, g = bar.tabRect(i), b.geometry()
                    check(abs((r.right() - g.right()) - inset) <= TOL,
                          f"{tag}: tab {i} gap {r.right() - g.right()} wrong "
                          f"after a removal")

            # --- 9 click targets the owning tab -------------------------
            # Listen on the QTabWidget, not on the bar. Both have a
            # tabCloseRequested signal and they are different signals; probing
            # the bar's would have passed while the controller emitted the
            # bar's and MainWindow never heard it.
            want = tabs.tabText(0)
            target = btn_of(bar, 0)
            # index 0 is now the tab that used to be 1; a stale captured index
            # would have emitted 0 for the *old* owner, which is now gone
            target.click()
            pump(app, 3)
            check(got == [0],
                  f"{tag}: clicking the button on '{want}' emitted {got}, "
                  f"expected [0]")
            tabs.tabCloseRequested.disconnect(_rec)

            host.close()
            pump(app, 3)

            # --- 7 non-closable welcome tab ------------------------------
            host2, tabs2, ctrl2 = build(p, closable=False)
            host2.resize(width, 820)
            host2.show()
            pump(app)
            for i in range(tabs2.count()):
                check(btn_of(tabs2.tabBar(), i) is None,
                      f"{tag}: non-closable tab {i} got a close button")
            host2.close()
            pump(app, 3)

    # --- long label must still fit, and the glyph must not cover it -----
    for tname in ("dark", "light"):
        p = palette.get_palette(tname)
        tabs = QtWidgets.QTabWidget()
        tabs.setObjectName("sessionTabs")
        tabs.setTabsClosable(False)
        tabs.setMovable(True)
        tabs.setDocumentMode(True)
        tabs.setStyleSheet(qss.build_qss(p))
        tabs.addTab(QtWidgets.QLabel("x"), SYM)
        host = QtWidgets.QWidget()
        hl = QtWidgets.QVBoxLayout(host)
        hl.setContentsMargins(16, 12, 16, 0)
        hl.addWidget(tabs, 1)
        tab_close.TabCloseController(tabs, p, host)
        host.resize(1020, 820)
        host.show()
        pump(app)
        bar = tabs.tabBar()
        r, g = bar.tabRect(0), btn_of(bar, 0).geometry()
        check(abs((r.right() - g.right()) - tab_close.TabCloseController.INSET) <= TOL,
              f"{tname}/long: right gap {r.right() - g.right()}")
        # the tab may not be squeezed below its own minimum
        check(r.width() >= g.width() + tab_close.TabCloseController.INSET + 24,
              f"{tname}/long: tab {r.width()}px too narrow for label+glyph")
        host.close()
        pump(app, 3)

    print()
    if fails:
        print(f"FAIL ({len(fails)})")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: tab close button placement (9 checks x 2 themes x 3 widths)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
