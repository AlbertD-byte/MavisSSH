"""Does a sidebar row's content actually land inside its own selection highlight?

`_sideitem.py` measures the host line against `row.height()` and that check has
always passed -- the host line does fit inside the row widget. The row widget
is what overflows: `QListWidget::item` used to carry `padding: 9px 10px` on
top of `SessionItem`'s own `setContentsMargins(10, 8, 10, 8)`, giving 19px of
inset at each end of a row pinned to `setFixedHeight(58)`. Each row then spilled
11px past its own item and the second text line was sliced by the highlight.
Measuring the row against itself cannot see that; the reference has to be
`visualItemRect`, the rectangle Qt actually paints the selection into.

Selected rows are the ones that matter -- an unselected row has no highlight to
clip it, so the symptom only shows once a row is picked.
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
    SavedSession(name="生产 Web-01", host="10.0.0.5", port=22, username="deploy",
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

lst = mw.session_list
bad = []


def in_viewport_coords(child, row, lst):
    """A child label's rect expressed in the list viewport's coordinates.

    `row.geometry()` is already viewport-relative, but a child's geometry() is
    relative to the row, so the row's own offset has to be added back before
    the two can be compared at all.
    """
    g = child.geometry()
    return g.translated(row.pos())


print(f"rows = {lst.count()}   viewport height = {lst.viewport().height()}")
# **只量会话行。** 断的是「会话行的内容装进它自己的选中高亮里没有」，
# 分组标题行不是会话行，它既不能被选中、也没有 `session` 属性。
for i in sidebar_rows.session_rows(lst):
    item = lst.item(i)
    row = lst.itemWidget(item)
    if row is None:
        bad.append(f"row {i}: no item widget")
        continue

    # select it: the highlight is the thing that does the clipping
    lst.setCurrentRow(i)
    app.processEvents()

    sel = lst.visualItemRect(item)          # what Qt paints the selection into
    rrect = row.geometry()                  # where the row widget actually sits
    print(f"\nrow {i} '{row.session.name}'  selected={lst.currentRow() == i}")
    print(f"  selection rect : {sel.getRect()}")
    print(f"  row widget rect: {rrect.getRect()}")

    if rrect.top() < sel.top() or rrect.bottom() > sel.bottom():
        bad.append(
            f"row {i}: row widget {rrect.getRect()} escapes its selection rect "
            f"{sel.getRect()} by {sel.top() - rrect.top()}px top / "
            f"{rrect.bottom() - sel.bottom()}px bottom")

    for name, obj in (("name", row.findChild(QtWidgets.QLabel, "sessionName")),
                      ("host", row.findChild(QtWidgets.QLabel, "sessionHost"))):
        if obj is None:
            bad.append(f"row {i}: {name} label missing")
            continue
        g = in_viewport_coords(obj, row, lst)
        inside = (sel.top() <= g.top() and g.bottom() <= sel.bottom()
                  and sel.left() <= g.left() and g.right() <= sel.right())
        print(f"  {name:<5} y={g.top():>4}..{g.bottom():<4} "
              f"x={g.left():>4}..{g.right():<4}  inside highlight: {inside}")
        if not inside:
            over_b = g.bottom() - sel.bottom()
            over_t = sel.top() - g.top()
            bad.append(
                f"row {i}: {name} line is cut by the selection highlight "
                f"({over_b:+d}px bottom / {over_t:+d}px top)")

    # the row's own bottom margin should also stay inside -- a row pinned to
    # 58px with an 8px inner margin cannot afford to be pushed down at all
    bottom_margin = row.contentsMargins().bottom()
    if rrect.height() - bottom_margin > sel.height():
        bad.append(
            f"row {i}: usable content height {rrect.height() - bottom_margin} "
            f"exceeds the selection height {sel.height()}")

print()
if bad:
    print("ROWS DO NOT FIT THEIR HIGHLIGHT:")
    for b in bad:
        print("  -", b)
else:
    print("every row's content sits inside its own selection highlight")

# a zoomed render so the numbers can be checked against what is drawn
lst.setCurrentRow(sidebar_rows.session_row(lst))
app.processEvents()
shot = lst.grab()
rect = QtCore.QRect(0, 0, shot.width(), min(shot.height(), 150))
zoom = shot.copy(rect).scaled(rect.width() * 3, rect.height() * 3,
                              QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                              QtCore.Qt.TransformationMode.FastTransformation)
out = str(pathlib.Path(__file__).with_name("_rowfit.png"))
print("saved", out, zoom.save(out, "PNG"))

sys.exit(1 if bad else 0)
