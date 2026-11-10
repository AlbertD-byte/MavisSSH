"""The sidebar collapse: does toggling it actually produce a 56px icon rail?

Driving notes, learned the hard way twice on this project:

  * Click through a real QMouseEvent at the button's centre. `btn.click()` is a
    shortcut straight to the signal and skips hit testing entirely, so a
    toggle that is laid out off-screen or under another widget still "works".
  * Assert on the *consumer*: `sidebar.width()` and what is painted in the
    rail, not on `settings.sidebar_collapsed`. A flag that flips while the
    sidebar stays 268px wide is the exact failure this feature could ship.
  * Measure both states on the same MainWindow instance. Building a fresh
    window per state would hide any state that only one of them fails to
    restore.

The compact rows are held to the same rule as the expanded ones -- a row that
escapes its own selection highlight is the bug that was fixed in e0f8fdb, and
the 40px rail rows are a third of the width, so the same arithmetic breaks
harder there.
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import settings
from sshclient.core.settings import SavedSession
from sshclient.ui import main_window, sidebar
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
st.theme = "light"
st.save = lambda: None          # never touch the real %APPDATA% file
mw = main_window.MainWindow(st, store)
host = QtWidgets.QWidget()
lay = QtWidgets.QVBoxLayout(host)
lay.setContentsMargins(0, 0, 0, 0)
lay.addWidget(mw)
host.resize(1280, 820)
host.show()

bad = []


def settle(ms=260):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


def real_click(widget):
    """A genuine press/release pair at the widget's centre, in screen coords
    -- the same event pair a user's mouse produces."""
    local = widget.rect().center()
    glob = widget.mapToGlobal(local)
    for kind in (QtCore.QEvent.Type.MouseButtonPress,
                 QtCore.QEvent.Type.MouseButtonRelease):
        ev = QtGui.QMouseEvent(kind, QtCore.QPointF(local), QtCore.QPointF(glob),
                               QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.KeyboardModifier.NoModifier)
        app.sendEvent(widget, ev)
    settle(200)


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r} (want {want!r})")
    if not ok:
        bad.append(f"{name}: got {got!r}, want {want!r}")


def report_geometry(tag):
    """Print and assert the rail's layout invariants."""
    print(f"\n--- {tag} ---")
    lst = mw.session_list
    w = mw.sidebar.width()
    print(f"  sidebar width = {w}   list viewport width = {lst.viewport().width()}")

    for i in sidebar_rows.session_rows(lst):
        item = lst.item(i)
        row = lst.itemWidget(item)
        lst.setCurrentRow(i)
        app.processEvents()
        sel = lst.visualItemRect(item)
        rg = row.geometry()
        escapes = rg.top() < sel.top() or rg.bottom() > sel.bottom()
        print(f"  row{i} {type(row).__name__:<18} sel={sel.getRect()} "
              f"row={rg.getRect()} escapes={escapes}")
        if escapes:
            bad.append(f"{tag}: row {i} escapes its selection rect by "
                       f"{sel.top() - rg.top()}px top / "
                       f"{rg.bottom() - sel.bottom()}px bottom")
        if sel.right() > lst.viewport().width():
            bad.append(f"{tag}: row {i} selection {sel.getRect()} runs past the "
                       f"{lst.viewport().width()}px viewport")

        # The 40px badge has to fit the rail row, and every child has to sit
        # inside the row -- a child pushed past the edge is how the host line
        # got sliced in e0f8fdb. QFrame included: the compact badge is a
        # QFrame, and a label-only sweep misses the one child that can
        # actually be wider than a rail row.
        for child in row.findChildren(QtWidgets.QWidget):
            if not isinstance(child, (QtWidgets.QFrame, QtWidgets.QLabel)):
                continue
            if child.parent() is not row:
                continue        # nested inside the badge, sized with it
            cg = child.geometry()
            over = cg.left() < 0 or cg.top() < 0 \
                or cg.right() > row.width() or cg.bottom() > row.height()
            print(f"        child {type(child).__name__}"
                  f"{child.objectName()!r} {cg.getRect()}"
                  f"{'  <-- OVERFLOWS' if over else ''}")
            if over:
                bad.append(f"{tag}: row {i} child "
                           f"{type(child).__name__}{child.objectName()!r} "
                           f"{cg.getRect()} escapes row {row.width()}x{row.height()}")

    # In the rail everything has to sit on the centre line. The sidebar frame
    # carries a 1px border, so content centres inside 55px and lands 1px left
    # of the geometric centre of 56 -- that is correct, not a bug. Past 2px
    # means something was laid out flush to an edge: a stretch factor in a
    # vertical QBoxLayout buys height, not width, so a pinned 40px button
    # lands on the left unless it is told to centre itself.
    #
    # Expanded is left-aligned on purpose, so this only applies to the rail.
    if w != sidebar.SIDEBAR_W_COLLAPSED:
        return w
    mid = mw.sidebar.width() / 2.0

    def offset(widget):
        g = widget.mapTo(mw.sidebar, QtCore.QPoint(0, 0))
        return g.x() + widget.width() / 2.0 - mid

    subjects = [("toggle", mw._fold_btn), ("new button", mw._new_btn),
                ("edit button", mw._side_edit_btn),
                ("delete button", mw._side_del_btn)]
    if lst.count():
        subjects.append(("session badge", lst.itemWidget(
            sidebar_rows.session_item(lst))
            .findChild(QtWidgets.QFrame, "compactBadge")))
    for nm, widget in subjects:
        if widget is None:
            continue
        d = offset(widget)
        print(f"  {nm:<14} centre offset {d:+.1f}px")
        if abs(d) > 2.0:
            bad.append(f"{tag}: {nm} is {d:+.1f}px off the rail centre line")
    return w


settle(400)

# ---------------------------------------------------------------- expanded
w_exp = report_geometry("expanded (268px)")
check("expanded sidebar width", w_exp, sidebar.SIDEBAR_W)
check("expanded row height", mw.session_list.itemWidget(
    sidebar_rows.session_item(mw.session_list)).height(), sidebar.ROW_H)
check("brand title visible", mw._brand_title.isVisible(), True)
check("new button text", mw._new_btn.text(), "新建连接")
check("side buttons labelled",
      (mw._side_edit_btn.text(), mw._side_del_btn.text()), ("编辑", "删除"))
check("footer is horizontal", mw._foot.direction(),
      QtWidgets.QBoxLayout.Direction.LeftToRight)
check("menu action label", mw._fold_action.text(), "折叠侧边栏")
check("setting not yet flipped", st.sidebar_collapsed, False)

# ---------------------------------------------------------------- collapse
print("\n>>> real click on the toggle")
real_click(mw._fold_btn)
w_col = report_geometry("collapsed (56px)")
check("collapsed sidebar width", w_col, sidebar.SIDEBAR_W_COLLAPSED)
check("setting persisted to settings", st.sidebar_collapsed, True)
check("compact row type", type(mw.session_list.itemWidget(
    sidebar_rows.session_item(mw.session_list))).__name__, "CompactSessionItem")
check("compact row height", mw.session_list.itemWidget(
    sidebar_rows.session_item(mw.session_list)).height(), sidebar.ROW_H_COMPACT)
check("brand title hidden", mw._brand_title.isVisible(), False)
check("brand logo hidden", mw._brand_logo.isVisible(), False)
check("new button is glyph only", mw._new_btn.text(), "")
check("new button pinned square", mw._new_btn.size().width(),
      mw._new_btn.size().height())
check("side buttons are glyph only",
      (mw._side_edit_btn.text(), mw._side_del_btn.text()), ("", ""))
check("side buttons stacked", mw._foot.direction(),
      QtWidgets.QBoxLayout.Direction.TopToBottom)
check("side buttons still clickable",
      (mw._side_edit_btn.isEnabled(), mw._side_del_btn.isEnabled()), (True, True))
check("side buttons kept in the rail",
      all(mw.sidebar.rect().contains(btn.geometry().center())
          for btn in (mw._side_edit_btn, mw._side_del_btn)), True)
check("menu action label follows state", mw._fold_action.text(), "展开侧边栏")
check("row count preserved", mw.session_list.count(), len(store.all))

# the toggle must not have moved: same x in both states is the whole point
toggle_x = mw._fold_btn.mapTo(mw.sidebar, QtCore.QPoint(0, 0)).x()
check("toggle x is still the left gutter", 0 < toggle_x < 24, True)

# name/host are gone from the rail -- they live in the tooltip now
row0 = mw.session_list.itemWidget(sidebar_rows.session_item(mw.session_list))
tip = sidebar_rows.session_item(mw.session_list).toolTip()
print(f"  row0 tooltip = {tip!r}")
if "localhost" not in tip or "22" not in tip:
    bad.append(f"collapsed row tooltip does not carry name and port: {tip!r}")

# ---------------------------------------------------------------- expand back
print("\n>>> real click on the toggle again")
real_click(mw._fold_btn)
w_back = report_geometry("expanded again")
check("re-expanded sidebar width", w_back, sidebar.SIDEBAR_W)
check("setting persisted back", st.sidebar_collapsed, False)
check("row type restored", type(mw.session_list.itemWidget(
    sidebar_rows.session_item(mw.session_list))).__name__, "SessionItem")
check("brand title visible again", mw._brand_title.isVisible(), True)
check("new button text restored", mw._new_btn.text(), "新建连接")
check("side button widths freed",
      mw._side_edit_btn.maximumWidth() > 1000, True)
check("footer is horizontal again", mw._foot.direction(),
      QtWidgets.QBoxLayout.Direction.LeftToRight)
check("menu action label restored", mw._fold_action.text(), "折叠侧边栏")

# ---------------------------------------------------------------- Ctrl+B
# Previously this called mw.toggle_sidebar() directly, which proves the method
# works and proves nothing about the shortcut: the QAction, its key sequence,
# its shortcut context and the menu entry could all be wrong and this would
# stay green. Drive a real key event through the window instead.
print("\n>>> Ctrl+B shortcut")
from PyQt6 import QtTest


def press_ctrl_b():
    QtTest.QTest.keyClick(mw, QtCore.Qt.Key.Key_B, QtCore.Qt.KeyboardModifier.ControlModifier)
    settle(220)


press_ctrl_b()
check("Ctrl+B collapses", mw.sidebar.width(), sidebar.SIDEBAR_W_COLLAPSED)
check("Ctrl+B row type is compact", type(mw.session_list.itemWidget(
    sidebar_rows.session_item(mw.session_list))).__name__, "CompactSessionItem")
press_ctrl_b()
check("Ctrl+B expands", mw.sidebar.width(), sidebar.SIDEBAR_W)
check("Ctrl+B row type is full", type(mw.session_list.itemWidget(
    sidebar_rows.session_item(mw.session_list))).__name__, "SessionItem")

# the menu action is the same shortcut, reachable without the keyboard
mw._fold_action.trigger()
settle(200)
check("视图 menu action collapses", mw.sidebar.width(),
      sidebar.SIDEBAR_W_COLLAPSED)
check("action text follows the state", mw._fold_action.text(), "展开侧边栏")
mw._fold_action.trigger()
settle(200)
check("视图 menu action expands", mw.sidebar.width(), sidebar.SIDEBAR_W)

# ------------------------------------------------------- selection survives
print("\n>>> selection survives a collapse round-trip")
mw.session_list.setCurrentRow(sidebar_rows.session_row(mw.session_list, 1))
settle(120)
before = mw.session_list.currentItem()
before_id = before.data(QtCore.Qt.ItemDataRole.UserRole) if before else None
mw.toggle_sidebar()
settle(150)
mw.toggle_sidebar()
settle(150)
after = mw.session_list.currentItem()
after_id = after.data(QtCore.Qt.ItemDataRole.UserRole) if after else None
check("selected session kept across the round trip", after_id, before_id)

# ------------------------------------------------------- live state dot
# The rail has no room for names, so the green dot is the only way to see
# what is connected. It is a sibling of the badge, not a child: inside, the
# centred icon would shrink and the badge radius would clip it.
print("\n>>> live dot")
mw.set_sidebar_collapsed(True, persist=False)
settle(200)
row1 = mw.session_list.itemWidget(sidebar_rows.session_item(mw.session_list, 1))
row0 = mw.session_list.itemWidget(sidebar_rows.session_item(mw.session_list))
check("dot hidden for an idle session", row0._dot.isVisible(), False)
row1.set_live(True)
settle(150)
check("dot shown for a live session", row1._dot.isVisible(), True)
check("dot still inside the row", row1._dot.geometry().right() <= row1.width(),
      True)
check("dot sits at the badge's corner", row1._dot.x() > row1.width() / 2, True)
check("tooltip names the state", "已连接" in row1.toolTip(), True)
check("idle tooltip has no state", "已连接" in row0.toolTip(), False)
row1.set_live(False)
settle(150)
check("dot hides again", row1._dot.isVisible(), False)
mw.set_sidebar_collapsed(False, persist=False)
settle(150)

# ---------------------------------------------------------------- renders
for collapsed in (False, True):
    mw.set_sidebar_collapsed(collapsed, persist=False)
    settle(200)
    tag = "collapsed" if collapsed else "expanded"
    out = str(pathlib.Path(__file__).with_name(f"_fold_{tag}.png"))
    print("saved", out, mw.sidebar.grab().save(out, "PNG"))
mw.set_sidebar_collapsed(False, persist=False)

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("sidebar collapse: all checks passed")
sys.exit(1 if bad else 0)
