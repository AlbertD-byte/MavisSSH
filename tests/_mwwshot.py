"""Screenshot the real MainWindow so the tab bar is judged in situ.

_tabvisual.py renders a synthetic QTabWidget with the same stylesheet; this
one runs the actual window, so the sidebar, the holder inset and the pane
below the tabs are all present.
"""
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core.settings import AppSettings, SessionStore
from sshclient.ui import tab_close
from sshclient.ui.main_window import MainWindow, SessionTab


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    # keep the real user config out of it
    os.environ.setdefault("MAVISSSH_SETTINGS", os.path.abspath("_shot_settings.json"))

    store = SessionStore(pathlib.Path(os.path.abspath("_shot_sessions.json")))
    settings = AppSettings()
    settings.theme = sys.argv[1] if len(sys.argv) > 1 else "dark"
    mw = MainWindow(settings, store)
    mw.resize(1280, 560)
    mw.show()

    labels = ["root@10.0.0.5:22", "deploy@build-07.example.com:2222", "ops@db.internal"]
    for lab in labels:
        tab = SessionTab(mw)
        tab.apply_palette(mw.palette_)
        idx = mw.tabs.addTab(tab, lab)
        mw._tab_close.sync()
    mw.tabs.setCurrentIndex(2)

    for _ in range(40):
        app.processEvents()

    out = f"_mw_{settings.theme}.png"
    mw.grab().save(out, "PNG")
    bar = mw.tabs.tabBar()
    for i in range(mw.tabs.count()):
        r = bar.tabRect(i)
        bs = [b for b in bar.findChildren(tab_close.TabCloseButton)
              if b.isVisible() and r.contains(b.geometry().center())]
        g = bs[0].geometry() if bs else None
        print(f"tab {i} '{mw.tabs.tabText(i)}' right={r.right()} "
              f"btn={g.x() if g else '-'} gap={r.right() - g.right() if g else '-'}")
    print("saved", out)

    # and a 5x crop of the right end of the selected tab. bar.tabRect() is in
    # the bar's coordinates; the grab is in the window's, so map across.
    r = bar.tabRect(2)
    tl = bar.mapTo(mw, QtCore.QPoint(r.left(), r.top()))
    br = bar.mapTo(mw, QtCore.QPoint(r.right() + 1, r.bottom() + 1))
    box = QtCore.QRect(br.x() - 92, tl.y() - 4, 92, (br.y() - tl.y()) + 8)
    img = mw.grab().copy(box)
    img.scaled(box.width() * 5, box.height() * 5,
               QtCore.Qt.AspectRatioMode.KeepAspectRatio,
               QtCore.Qt.TransformationMode.FastTransformation
               ).save(f"_mw_{settings.theme}_zoom.png", "PNG")
    mw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
