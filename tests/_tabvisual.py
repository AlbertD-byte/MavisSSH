"""Render the session tab bar so the result can actually be looked at.

Saves a full shot plus an 6x crop around the selected tab's right edge, where
the glyph used to touch the border.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.ui import tab_close
from sshclient.ui.design.v1 import palette, qss

LABELS = ["root@10.0.0.1:22", "deploy@build-07.example.com:2222", "ops@db.internal:22"]


def shot(tname, p):
    tabs = QtWidgets.QTabWidget()
    tabs.setObjectName("sessionTabs")
    tabs.setTabsClosable(False)
    tabs.setMovable(True)
    tabs.setDocumentMode(True)
    tabs.setStyleSheet(qss.build_qss(p))
    for lab in LABELS:
        page = QtWidgets.QLabel(lab)
        page.setStyleSheet("background: transparent;")
        tabs.addTab(page, lab)
    welcome = QtWidgets.QLabel("欢迎")
    welcome.setProperty(tab_close.NO_CLOSE, True)
    tabs.insertTab(0, welcome, "欢迎")

    host = QtWidgets.QWidget()
    hl = QtWidgets.QVBoxLayout(host)
    hl.setContentsMargins(0, 0, 0, 0)
    # same inset the MainWindow uses
    inner = QtWidgets.QWidget()
    il = QtWidgets.QVBoxLayout(inner)
    il.setContentsMargins(16, 12, 16, 0)
    il.addWidget(tabs, 1)
    hl.addWidget(inner, 1)
    ctrl = tab_close.TabCloseController(tabs, p, host)

    host.resize(1180, 420)
    host.setStyleSheet(f"background: {p.bg_base};")
    host.show()
    tabs.setCurrentIndex(2)
    for _ in range(12):
        QtWidgets.QApplication.instance().processEvents()
    ctrl.sync()

    bar = tabs.tabBar()
    full = host.grab()
    full.save(f"_vis_{tname}.png", "PNG")

    r = bar.tabRect(2)
    box = QtCore.QRect(r.right() - 78, max(0, r.top() - 6), 80, r.height() + 12)
    crop = full.copy(box).scaled(
        box.width() * 6, box.height() * 6,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.FastTransformation)
    crop.save(f"_vis_{tname}_zoom.png", "PNG")

    # the seam: bottom corners of the selected tab against the pane
    seam = QtCore.QRect(r.x() - 10, r.top(), r.width() + 20, r.height() + 14)
    seam_img = full.copy(seam).scaled(
        seam.width() * 3, seam.height() * 3,
        QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.FastTransformation)
    seam_img.save(f"_vis_{tname}_seam.png", "PNG")
    print(f"{tname}: bar rect={bar.rect()} tab2 rect={r.x()},{r.y()} {r.width()}x{r.height()}")
    for i in range(tabs.count()):
        bs = [b for b in bar.findChildren(tab_close.TabCloseButton)
              if b.isVisible() and bar.tabRect(i).contains(b.geometry().center())]
        g = bs[0].geometry() if bs else None
        print(f"   tab {i} '{tabs.tabText(i)}' right={bar.tabRect(i).right()} "
              f"button={g.x() if g else None} gap="
              f"{bar.tabRect(i).right() - g.right() if g else None}")
    host.close()


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    for t in ("dark", "light"):
        shot(t, palette.get_palette(t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
