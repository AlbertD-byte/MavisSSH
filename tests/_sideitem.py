"""Measure the sidebar row: is the badge and the two-line block centred?

The report is "left side content is not vertically centred". Both halves of
the row are suspects -- the 32px badge is added with AlignTop, and the
name+host pair is a plain QVBoxLayout with no centering -- so measure the
rects instead of eyeballing a screenshot.
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core import settings
from sshclient.core.settings import SavedSession
from sshclient.ui import main_window
from tests import sidebar_rows

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
tmp = pathlib.Path(tempfile.mkdtemp())
store = settings.SessionStore(tmp / "sessions.json")
store._items = [
    SavedSession(name="localhost", host="localhost", port=22, username="root",
                 auth_method="password", password="x"),
    SavedSession(name="prod-host", host="203.0.113.10", port=22, username="deploy",
                 auth_method="key"),
]
st = settings.AppSettings()
st._path = tmp / 'settings.json'
st.theme = "light"
mw = main_window.MainWindow(st, store)
host = QtWidgets.QWidget()
lay = QtWidgets.QVBoxLayout(host)
lay.setContentsMargins(0, 0, 0, 0)
lay.addWidget(mw)
host.resize(1280, 820)
host.show()

end = [False]
QtCore.QTimer.singleShot(400, lambda: end.__setitem__(0, True))
while not end[0]:
    app.processEvents()

bad = []
lst = mw.session_list
print(f"session_list viewport height = {lst.viewport().height()}")
print(f"rows = {lst.count()}")
# **只量会话行。** 这个脚本要断的是「会话行里的徽标和两行文字居不居中」，
# 分组标题行不是会话行，它没有 `session` 属性 —— 遍历 `range(count())`
# 会直接 AttributeError 崩在这里（症状看着像「行控件类型不对」）。
for i in sidebar_rows.session_rows(lst):
    item = lst.item(i)
    row = lst.itemWidget(item)
    if row is None:
        continue
    # row.rect() is row-local; child geometry() is row-local too. Comparing
    # row.geometry() (viewport coords) against a child rect was off by the
    # row's own offset in the list and produced nonsense for every row but 0.
    g = row.rect()
    badge = None
    name = row.findChild(QtWidgets.QLabel, "sessionName")
    hostlab = row.findChild(QtWidgets.QLabel, "sessionHost")
    # the badge is the only fixed 32x32 child label
    for c in row.findChildren(QtWidgets.QLabel):
        if c.width() == 32 and c.height() == 32:
            badge = c
            break

    def midy(r):
        return r.y() + r.height() / 2.0

    row_mid = midy(g)
    print(f"\nrow {i} '{row.session.name}'  height={g.height()}")
    print(f"  row mid = {row_mid:.1f}")
    for nm, w in (("badge", badge), ("name", name), ("host", hostlab)):
        if w is None:
            print(f"  {nm:<9} MISSING")
            continue
        rg = w.geometry()
        d = midy(rg) - row_mid
        print(f"  {nm:<9} y={rg.y():>3} h={rg.height():>3} mid={midy(rg):>6.1f} "
              f"offset_from_row_mid={d:+.1f}")

    # the two text lines as one block
    if name is not None and hostlab is not None:
        top = min(name.geometry().y(), hostlab.geometry().y())
        bot = max(name.geometry().y() + name.geometry().height(),
                  hostlab.geometry().y() + hostlab.geometry().height())
        block_mid = (top + bot) / 2.0
        print(f"  text block spans y={top}..{bot}  mid={block_mid:.1f}  "
              f"offset={block_mid - row_mid:+.1f}")
        print(f"  slack: above={top:.1f}  below={g.height() - bot:.1f}")
        if abs(block_mid - row_mid) > 1.5:
            bad.append(f"row {i}: text block off-centre by "
                       f"{block_mid - row_mid:+.1f}px")
    if badge is not None:
        d = midy(badge.geometry()) - row_mid
        if abs(d) > 1.5:
            bad.append(f"row {i}: badge off-centre by {d:+.1f}px")
        # and the badge should line up with the text block, not just the row
        if name is not None and hostlab is not None:
            bm = midy(badge.geometry())
            tm = (midy(name.geometry()) + midy(hostlab.geometry())) / 2.0
            if abs(bm - tm) > 1.5:
                bad.append(f"row {i}: badge centre is {bm - tm:+.1f}px off the "
                           f"text block centre")

# does the second line get clipped by the row?
for i in range(lst.count()):
    row = lst.itemWidget(lst.item(i))
    if row is None:
        continue
    h = row.findChild(QtWidgets.QLabel, "sessionHost")
    if h is None:
        continue
    rg = h.geometry()
    if rg.y() + rg.height() > row.height():
        bad.append(f"row {i}: the host line is clipped "
                   f"(needs {rg.y() + rg.height()} of {row.height()})")

print()
if bad:
    print("NOT CENTRED:")
    for b in bad:
        print("  -", b)
else:
    print("all rows centred")

# a zoomed render, so the numbers above can be checked against what is drawn
shot = lst.grab()
rect = QtCore.QRect(0, 0, shot.width(), min(shot.height(), 140))
zoom = shot.copy(rect).scaled(rect.width() * 3, rect.height() * 3,
                              QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                              QtCore.Qt.TransformationMode.FastTransformation)
out = str(pathlib.Path(__file__).with_name("_side.png"))
print("saved", out, zoom.save(out, "PNG"))

sys.exit(1 if bad else 0)
