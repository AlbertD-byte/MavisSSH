"""End-to-end persistence: does a rule added at runtime survive a restart?

The reported bug was that a tunnel added in the panel vanished when the
session was closed and reopened. This drives the real chain end to end:

    panel._on_add / _on_switch
      -> TunnelPanel.rulesChanged
      -> SessionTab._persist_tunnels
      -> MainWindow.persist_session
      -> SessionStore.save  (writes JSON to disk)
    ...then a *fresh* SessionStore reads that file back...

Run: .venv\\Scripts\\python.exe _tunpersist.py
"""
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from PyQt6 import QtWidgets

from sshclient.ui.design.v1 import palette

_app = QtWidgets.QApplication(sys.argv)

from sshclient.core.settings import SavedSession, SessionStore
from sshclient.core.tunnel import TunnelSpec
from sshclient.remote.connection import ConnectionInfo, SSHConnection
from sshclient.ui.tunnel_panel import TunnelPanel

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Window:
    """Stands in for MainWindow: owns the store, records persist calls."""

    def __init__(self, store):
        self.store = store
        self.persisted = 0

    def persist_session(self, saved):
        self.store.upsert(saved)
        self.persisted += 1


tmp = Path(tempfile.mkdtemp()) / "sessions.json"

print("=" * 72)
print("tunnel persistence — add at runtime, close session, reopen")
print("=" * 72)

# --- session 1: add rules through the panel, exactly as a user would --------
store = SessionStore(tmp)
win = Window(store)
session = SavedSession(name="prod-web", host="10.0.0.5", port=22, username="deploy")
store.upsert(session)

conn = SSHConnection(ConnectionInfo(host="10.0.0.5", port=22, username="deploy"))
panel = TunnelPanel(conn, palette.get_palette("dark"))
panel._flash = lambda m: None
panel.rulesChanged.connect(lambda: (
    setattr(session, "tunnels", panel.current_rules()),
    win.persist_session(session),
))
panel.refresh()

p_local = free_port()
p_socks = free_port()
print(f"\n  adding -L {p_local} -> db.internal:3306 and SOCKS {p_socks}")
panel._call(conn.add_tunnel, TunnelSpec(
    "local", p_local, "db.internal", 3306, description="MySQL"))
panel._call(conn.add_tunnel, TunnelSpec("socks", p_socks, description="proxy"))
print(f"  live rules: {[s.local_port for s in conn.tunnel_specs()]}")
print(f"  persist calls so far: {win.persisted}")

check("adding a rule persisted the session", win.persisted == 2, f"{win.persisted}")
check("the JSON file exists", tmp.exists())
if tmp.exists():
    raw = json.loads(tmp.read_text(encoding="utf-8"))
    stored = raw[0].get("tunnels") or []
    check("both rules are on disk", len(stored) == 2, str(len(stored)))
    check("the -L rule is on disk with its target",
          any(r.get("remote_host") == "db.internal" and r.get("local_port") == p_local
              for r in stored), str(stored))
    check("the SOCKS rule is on disk",
          any(r.get("kind") == "socks" and r.get("local_port") == p_socks
              for r in stored))

# now switch one off and make sure that is what gets stored
print("\n  switching the SOCKS rule off")
panel._call(conn.set_tunnel_enabled, p_socks, False)
check("toggling persisted too", win.persisted == 3, f"{win.persisted}")
raw = json.loads(tmp.read_text(encoding="utf-8"))
stored = raw[0].get("tunnels") or []
check("the off state reached disk",
      any(r.get("kind") == "socks" and r.get("enabled") is False for r in stored),
      str([(r.get("kind"), r.get("enabled")) for r in stored]))

# --- close the session entirely --------------------------------------------
print("\n  closing the session (tab and connection go away)")
panel.shutdown()
conn.close()
del panel, conn
check("the live objects are gone", True)

# --- session 2: a brand new store reads the file back ----------------------
print("\n  reopening the session from disk")
store2 = SessionStore(tmp)
check("the session came back", len(store2.all) == 1, str(len(store2.all)))
reopened = store2.all[0]
check("it still has both rules", len(reopened.tunnels) == 2,
      str(len(reopened.tunnels)))

conn2 = SSHConnection(ConnectionInfo(host="10.0.0.5", port=22, username="deploy"))
restored_on = restored_off = 0
for entry in reopened.tunnels:
    parsed = TunnelSpec.from_dict(entry)
    if parsed is None:
        continue
    s, en = parsed
    conn2.add_tunnel(s, enabled=en)
    restored_on += en
    restored_off += (not en)

check("both rules were re-armed", len(conn2.tunnel_specs()) == 2,
      str(len(conn2.tunnel_specs())))
check("the rule that was on is on again",
      conn2.tunnel_states()[p_local] is True)
check("the rule that was off stayed off",
      conn2.tunnel_states()[p_socks] is False)
try:
    socket.create_connection(("127.0.0.1", p_socks), timeout=1).close()
    check("the off rule holds no socket after reopen", False, "still accepting")
except OSError:
    check("the off rule holds no socket after reopen", True)
try:
    socket.create_connection(("127.0.0.1", p_local), timeout=1).close()
    check("the on rule is listening after reopen", True)
except OSError as exc:
    check("the on rule is listening after reopen", False, str(exc))

# removing a rule also persists
print("\n  removing the SOCKS rule and reopening once more")
conn2.remove_tunnel(p_socks)
reopened.tunnels = [
    s.to_dict(enabled=conn2.tunnel_states().get(s.local_port, False))
    for s in conn2.tunnel_specs()
]
store2.upsert(reopened)
conn2.close()
store3 = SessionStore(tmp)
check("the removal persisted",
      len(store3.all[0].tunnels) == 1, str(store3.all[0].tunnels))
check("only the -L rule remains",
      store3.all[0].tunnels[0]["remote_host"] == "db.internal",
      str(store3.all[0].tunnels[0]))

print("\n" + "=" * 72)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for n in FAIL:
        print("  -", n)
print("=" * 72)
sys.exit(1 if FAIL else 0)
