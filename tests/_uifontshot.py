"""Render the appearance page of the settings dialog in a few faces.

The assertions in _uifont.py prove the *rules* (resolution, round trip, cancel
restores). They cannot say whether the picker looks like it belongs in this
app, so draw it: the same page with three different faces, which is the only
way to see whether a proportional CJK face still fits the 268px sidebar and
the card grid the way Segoe UI Variable Text does.
"""
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core import fonts
from sshclient.core.settings import AppSettings
from sshclient.ui.design.v1 import palette
from sshclient.ui.settings_dialog import SettingsDialog

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

PAL = palette.get_palette("dark")
SHOTS = [
    ("_uifont_auto.png", ""),
    ("_uifont_cjk.png", "Microsoft YaHei UI"),
    ("_uifont_latin.png", "Arial"),
]


def settle(ms=280):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


for name, family in SHOTS:
    st = AppSettings(theme="dark", ui_font_size=11, ui_font_family=family)
    dlg = SettingsDialog(st, PAL)
    dlg.resize(520, 460)
    dlg.show()
    settle()
    out = str(pathlib.Path(__file__).with_name(name))
    print("saved", out, dlg.grab().save(out, "PNG"),
          f"(ui font = {dlg.font().family()!r})")
    dlg.close()
    settle(60)
    # the preview mutated the application font; put the default back so the
    # next shot starts from the same place
    app.setFont(fonts.ui_font(11, ""))
    settle(60)
