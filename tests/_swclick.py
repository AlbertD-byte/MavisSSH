"""Reproduce the crash on clicking a tunnel switch.

Synthesises a real mouse click on the switch column so the whole chain runs
the way it does in the app:

    QTreeView mouse handling -> _SwitchDelegate.editorEvent
      -> TunnelPanel._on_switch -> SSHConnection.set_tunnel_enabled
      -> TunnelPanel.refresh() (rebuilds every row)

Run: .venv\\Scripts\\python.exe _swclick.py
"""
import os
import socket
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.ui.design.v1 import palette

# Real windows platform: the offscreen one hides system fonts, but more
# importantly we want the same view/event machinery the app uses.
_app = QtWidgets.QApplication(sys.argv)

from sshclient.core.tunnel import TunnelSpec
from sshclient.remote.connection import ConnectionInfo, SSHConnection
from sshclient.ui.tunnel_panel import TunnelPanel


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def select_row(panel: TunnelPanel, row: int) -> None:
    """Select a row the way a real click would (QTreeView has no selectRow)."""
    sm = panel.view.selectionModel()
    sm.select(panel.model.index(row, 0),
              QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
              | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    _app.processEvents()


def click_switch(panel: TunnelPanel, row: int) -> None:
    """Send a real press+release at the centre of the switch cell."""
    view = panel.view
    view.resize(900, 240)
    idx = panel.model.index(row, 0)
    rect = view.visualRect(idx)
    local = QtCore.QPointF(rect.center())
    glob = QtCore.QPointF(view.viewport().mapToGlobal(rect.center()))
    for kind in (QtCore.QEvent.Type.MouseButtonPress,
                 QtCore.QEvent.Type.MouseButtonRelease):
        ev = QtGui.QMouseEvent(
            kind, local, glob,
            QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.MouseButton.LeftButton
            if kind == QtCore.QEvent.Type.MouseButtonPress
            else QtCore.Qt.MouseButton.NoButton,
            QtCore.Qt.KeyboardModifier.NoModifier)
        QtWidgets.QApplication.sendEvent(view.viewport(), ev)
    _app.processEvents()


print("=== building a session with two rules ===")
conn = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
panel = TunnelPanel(conn, palette.get_palette("dark"))
panel.resize(900, 240)
panel.show()
_app.processEvents()

p1 = free_port()
p2 = free_port()
conn.add_tunnel(TunnelSpec("local", p1, "db.internal", 3306, description="MySQL"))
conn.add_tunnel(TunnelSpec("socks", p2, description="proxy"))
panel.refresh()
_app.processEvents()
print(f"  rules: {[s.local_port for s in conn.tunnel_specs()]}")
print(f"  states: {conn.tunnel_states()}")
print(f"  visible: {panel.isVisible()}  rows: {panel.model.rowCount()}")

print("\n=== click the switch on row 0 (turn off) ===")
click_switch(panel, 0)
print(f"  states after: {conn.tunnel_states()}")
assert conn.tunnel_states()[p1] is False, "switch did not turn off"
print("  -> turned off OK")

print("\n=== click the switch on row 0 again (turn back on) ===")
click_switch(panel, 0)
print(f"  states after: {conn.tunnel_states()}")
assert conn.tunnel_states()[p1] is True, "switch did not turn back on"
print("  -> turned on OK")

print("\n=== rapid toggling (20 clicks, no settling) ===")
for i in range(20):
    click_switch(panel, 0)
    if (i + 1) % 5 == 0:
        print(f"  after {i + 1:2d} clicks: states={conn.tunnel_states()}")

print("\n=== toggling a row that is not row 0 ===")
click_switch(panel, 1)
print(f"  states: {conn.tunnel_states()}")
click_switch(panel, 1)
print(f"  states: {conn.tunnel_states()}")

print("\n=== click with a row selected, then remove ===")
select_row(panel, 0)
_app.processEvents()
click_switch(panel, 1)
print(f"  states: {conn.tunnel_states()}")

print("\n=== toggling while a connection is live on the other rule ===")
held = socket.create_connection(("127.0.0.1", p2), timeout=5) if conn.tunnel_states()[p2] else None
if held:
    held.settimeout(5)
    held.sendall(bytes([0x05, 0x01, 0x00]))
    held.recv(2)
    raw = b"127.0.0.1"
    held.sendall(bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton("127.0.0.1")
                 + struct.pack("!H", 1))       # will fail to connect, that is fine
    time.sleep(0.3)
    for i in range(6):
        click_switch(panel, 0)
    print(f"  survived toggling with a live connection: states={conn.tunnel_states()}")
    held.close()

print("\n=== switching off then deleting the rule ===")
click_switch(panel, 0)
print(f"  off: {conn.tunnel_states()}")
select_row(panel, 0)
panel._on_remove()
print(f"  after remove: specs={conn.tunnel_specs()}")
click_switch(panel, 0)
print("  clicking a now-removed row must not crash")

print("\n=== a row selected, then toggled (this is the reported crash) ===")
select_row(panel, 0)
print(f"  selection active: {panel.view.selectionModel().hasSelection()}")
before = conn.tunnel_states()[conn.tunnel_specs()[0].local_port]
click_switch(panel, 0)
after = conn.tunnel_states()[conn.tunnel_specs()[0].local_port]
print(f"  {before} -> {after}")
assert before is not after, "toggle with a selection did nothing"
print("  -> survived: selection is preserved across the rebuild")

panel.shutdown()
conn.close()
print("\nRESULT: survived the whole sequence")
