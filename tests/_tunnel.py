"""Port forwarding tests: SOCKS5 parsing, the relay, and rule lifecycle.

No SSH server is needed. ``Forward`` takes an ``open_channel`` callable, so we
inject a plain socket that stands in for a paramiko ``direct-tcpip`` channel.
That exercises the real code path end to end:

    client -> SOCKS5 handshake -> Forward._serve -> handle_accepted
           -> open_channel (injected) -> _pump relay -> echo server

Run: .venv\\Scripts\\python.exe _tunnel.py
"""
import os
import socket
import struct
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sshclient.core import tunnel
from sshclient.core.tunnel import Forward, TunnelError, TunnelSpec, validate
from sshclient.ui.design.v1 import palette

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _dial(port):
    """Stand-in for ``open_direct_channel``: returns a real socket.

    ``Forward`` only relies on the paramiko Channel interface (recv/sendall/
    close/settimeout), which a socket satisfies, so the whole relay path runs
    without an SSH server.
    """
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def _always_fail(host, port):
    """Stand-in for an unreachable remote target."""
    raise OSError(f"connection refused to {host}:{port}")


def _raises(fn):
    """True if *fn* raises TunnelError/ConnectionError rather than returning."""
    try:
        fn()
    except (TunnelError, ConnectionError, OSError):
        return True
    return False


# ---------------------------------------------------------------- echo server
class EchoServer:
    """Minimal TCP echo server used as the 'remote' side of a tunnel."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        self.sock.settimeout(0.4)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.hits += 1
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn):
        try:
            conn.settimeout(5.0)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            conn.close()

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def socks5_connect(sock, host, port, atyp="domain"):
    """Perform a SOCKS5 CONNECT and return the server's reply bytes."""
    sock.sendall(bytes([0x05, 0x01, 0x00]))              # VER, 1 method, NO AUTH
    reply = sock.recv(2)                                  # method selection
    assert reply == bytes([0x05, 0x00]), reply

    if atyp == "ipv4":
        addr = bytes([0x01]) + socket.inet_aton(host)
    elif atyp == "ipv6":
        addr = bytes([0x04]) + socket.inet_pton(socket.AF_INET6, host)
    else:
        raw = host.encode()
        addr = bytes([0x03, len(raw)]) + raw
    sock.sendall(bytes([0x05, 0x01, 0x00]) + addr + struct.pack("!H", port))
    return sock.recv(10)


print("=" * 70)
print("A. validate()")
print("=" * 70)
for spec, ok in (
    (TunnelSpec("local", 13306, "127.0.0.1", 3306), True),
    (TunnelSpec("local", 0, "h", 1), False),
    (TunnelSpec("local", 70000, "h", 1), False),
    (TunnelSpec("local", 1080, "", 80), False),
    (TunnelSpec("local", 1080, "h", 0), False),
    (TunnelSpec("socks", 1080), True),
    (TunnelSpec("nope", 1080), False),
):
    try:
        validate(spec)
        got = True
    except TunnelError:
        got = False
    check(f"{spec.kind}:{spec.local_port} valid={got}", got == ok)

print("\n" + "=" * 70)
print("B. SOCKS5 request parsing (all ATYP forms)")
print("=" * 70)
# The greeting and the CONNECT request are staged together in the socket
# buffer: parse() reads the greeting, writes the method-selection reply (which
# nobody has to drain), then reads the request. Driving it from this thread
# avoids the deadlock you get from waiting on the reply first.
GREETING = bytes([0x05, 0x01, 0x00])


def parse_with(payload):
    a, b = socket.socketpair()
    a.settimeout(5)
    try:
        b.sendall(GREETING + payload)
        return tunnel.parse_socks5_request(a)
    finally:
        a.close()
        b.close()


req4 = bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton("10.1.2.3") + struct.pack("!H", 8080)
check("ATYP=IPv4 decoded", parse_with(req4) == ("10.1.2.3", 8080), str(parse_with(req4)))

raw = b"db.internal"
reqd = bytes([0x05, 0x01, 0x00, 0x03, len(raw)]) + raw + struct.pack("!H", 5432)
check("ATYP=domain decoded", parse_with(reqd) == ("db.internal", 5432), str(parse_with(reqd)))

req6 = (bytes([0x05, 0x01, 0x00, 0x04])
        + socket.inet_pton(socket.AF_INET6, "2001:db8::1") + struct.pack("!H", 443))
check("ATYP=IPv6 decoded", parse_with(req6) == ("2001:db8::1", 443), str(parse_with(req6)))

check("port 0 rejected", _raises(lambda: parse_with(
    bytes([0x05, 0x01, 0x00, 0x01]) + socket.inet_aton("1.2.3.4") + struct.pack("!H", 0))),
    "port 0 is out of range")

print("\n" + "=" * 70)
print("C. SOCKS5 rejection paths")
print("=" * 70)


def expect_reject(name, payload, greeting=GREETING):
    a, b = socket.socketpair()
    a.settimeout(5)
    try:
        b.sendall(greeting + payload)
        try:
            tunnel.parse_socks5_request(a)
            check(name, False, "accepted but should have been rejected")
        except TunnelError as exc:
            check(name, True, str(exc)[:44])
        except OSError as exc:
            # A truncated request stalls until the socket times out rather than
            # EOF-ing. socket.timeout subclasses OSError, and handle_accepted
            # catches OSError, so this is still reported as a handshake failure.
            check(name, True, f"transport: {exc}"[:44])
    finally:
        a.close()
        b.close()


expect_reject("wrong SOCKS version", b"", greeting=bytes([0x04, 0x01, 0x00]))
expect_reject("no methods offered", b"", greeting=bytes([0x05, 0x00]))
expect_reject("only auth methods offered", b"", greeting=bytes([0x05, 0x01, 0x02]))
expect_reject("BIND command refused",
              bytes([0x05, 0x02, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
expect_reject("unknown ATYP refused",
              bytes([0x05, 0x01, 0x00, 0x09, 0, 0, 0, 0, 0, 0]))
expect_reject("empty domain refused",
              bytes([0x05, 0x01, 0x00, 0x03, 0]) + struct.pack("!H", 80))
expect_reject("truncated request refused",
              bytes([0x05, 0x01, 0x00, 0x01, 10]))

print("\n" + "=" * 70)
print("D. end-to-end relay through a SOCKS5 forward")
print("=" * 70)
echo = EchoServer()
listen = free_port()
errors = []
spec = TunnelSpec("socks", listen, description="test")
fwd = Forward(spec, lambda h, p: _dial(echo.port), errors.append).start()
check("forward started", fwd.running)
probe = socket.create_connection(("127.0.0.1", listen), timeout=3)
probe.close()          # must close, or it lingers in the accept loop
time.sleep(0.3)

cli = socket.create_connection(("127.0.0.1", listen), timeout=5)
cli.settimeout(5)
reply = socks5_connect(cli, "echo.test", echo.port, atyp="domain")
check("SOCKS5 reply is success", reply[:2] == bytes([0x05, 0x00]), reply.hex())
cli.sendall(b"hello tunnel")
check("payload echoed back", cli.recv(64) == b"hello tunnel")
cli.sendall(b"x" * 200000)
big = b""
while len(big) < 200000:
    chunk = cli.recv(65536)
    if not chunk:
        break
    big += chunk
check("200 KB round-trips intact", big == b"x" * 200000, f"{len(big)} bytes")
cli.close()
time.sleep(0.4)
check("remote saw the connection", echo.hits >= 1, f"hits={echo.hits}")
check("no errors reported", not errors, "; ".join(errors[:2]))
check("active count returned to 0", fwd.active_connections == 0,
      f"active={fwd.active_connections}")

# The live-connection counter is what the panel's 连接数 column renders, so it
# has to actually climb while a connection is held open -- not just return to
# zero afterwards.
held = socket.create_connection(("127.0.0.1", listen), timeout=5)
held.settimeout(5)
socks5_connect(held, "echo.test", echo.port)
time.sleep(0.4)
check("active count climbs while held", fwd.active_connections == 1,
      f"active={fwd.active_connections}")
held.close()
time.sleep(0.5)
check("active count drops on close", fwd.active_connections == 0,
      f"active={fwd.active_connections}")

print("\n" + "=" * 70)
print("E. end-to-end relay through a local (-L) forward")
print("=" * 70)
echo2 = EchoServer()
listen2 = free_port()
errors2 = []
# Dial the port the *rule* supplies, not a hardcoded one: that is what proves
# the forward is pinned to its own host:port.
fwd2 = Forward(TunnelSpec("local", listen2, "echo.test", echo2.port),
               lambda h, p: _dial(p), errors2.append).start()
cli2 = socket.create_connection(("127.0.0.1", listen2), timeout=5)
cli2.settimeout(5)
cli2.sendall(b"direct local forward")
check("no SOCKS handshake needed", cli2.recv(64) == b"direct local forward")
cli2.close()
time.sleep(0.3)
check("pinned to the rule's host:port", echo2.hits == 1, f"hits={echo2.hits}")
check("no errors reported", not errors2, "; ".join(errors2[:2]))

print("\n" + "=" * 70)
print("F. unreachable remote is reported, not swallowed")
print("=" * 70)
errors3 = []
fwd3 = Forward(TunnelSpec("socks", free_port()), _always_fail, errors3.append).start()
port3 = fwd3.spec.local_port
cli3 = socket.create_connection(("127.0.0.1", port3), timeout=5)
cli3.settimeout(5)
socks5_connect(cli3, "nowhere.invalid", 9)
time.sleep(0.5)
cli3.close()
check("failure surfaced to on_error", len(errors3) == 1, errors3[0][:56] if errors3 else "none")
check("message names the target", "nowhere.invalid:9" in (errors3[0] if errors3 else ""),
      errors3[0][:56] if errors3 else "none")

print("\n" + "=" * 70)
print("G. lifecycle: port clash, stop, restart")
print("=" * 70)
busy = socket.socket()
busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
busy.bind(("127.0.0.1", 0))
busy.listen(1)
clash_port = busy.getsockname()[1]
try:
    Forward(TunnelSpec("socks", clash_port), _always_fail).start()
    check("port clash rejected", False, "start() should have raised")
except TunnelError as exc:
    check("port clash rejected", True, str(exc)[:44])
busy.close()

fwd2.stop()
time.sleep(0.3)
check("stop() ends the accept loop", not fwd2.running)
try:
    socket.create_connection(("127.0.0.1", listen2), timeout=1)
    check("listener released after stop", False, "still accepting")
except OSError:
    check("listener released after stop", True)

fwd2.start()
time.sleep(0.2)
check("restart works", fwd2.running)
fwd2.stop()

fwd.stop()
fwd3.stop()
echo.stop()
echo2.stop()
check("all forwards stopped", True)

print("\n" + "=" * 70)
print("H. spec row round-trip (settings persistence)")
print("=" * 70)
s = TunnelSpec(kind="local", local_port=13306, remote_host="db.internal",
               remote_port=3306, description="MySQL")
back = TunnelSpec.from_row(s.to_row())
check("round-trips through settings row",
      (back.local_port, back.remote_host, back.remote_port, back.description)
      == (13306, "db.internal", 3306, "MySQL"),
      f"{back.local_port}/{back.remote_host}/{back.remote_port}/{back.description!r}")
check("legacy 4-field row still loads",
      TunnelSpec.from_row([13306, "db.internal", 3306, "MySQL"]) is not None)
check("malformed row returns None", TunnelSpec.from_row(["x", "y", "z"]) is None)
check("short row is padded", TunnelSpec.from_row([1080, "h", 80]) is not None)
# listen_host must survive persistence, or a non-loopback rule silently
# narrows back to 127.0.0.1 on reload.
lan = TunnelSpec(kind="socks", local_port=1080, listen_host="0.0.0.0")
row = lan.to_row()
restored = TunnelSpec(kind="socks", local_port=row[0], listen_host=row[4])
check("listen_host persisted for socks", restored.listen_host == "0.0.0.0", row[4])

print("\n" + "=" * 70)
print("I. TunnelPanel rendering (stub registry over real Forwards)")
print("=" * 70)
from PyQt6 import QtWidgets

from sshclient.ui.tunnel_panel import _SWITCH_ROLE as SWITCH_ROLE
from sshclient.ui.tunnel_panel import TunnelPanel


class StubRegistry:
    """Implements exactly the tunnel surface TunnelPanel consumes."""

    def __init__(self, echo_port):
        self._lock = threading.Lock()
        self._rules: dict[int, tuple[tunnel.TunnelSpec, Forward | None, bool]] = {}
        self._echo_port = echo_port
        self._on_error = None

    def set_tunnel_callbacks(self, on_error=None, on_event=None):
        self._on_error = on_error

    def add_tunnel(self, spec, enabled=True):
        spec = tunnel.validate(spec)
        with self._lock:
            if spec.local_port in self._rules:
                raise TunnelError(f"本地端口 {spec.local_port} 已被占用")
            fwd = Forward(spec, lambda h, p: _dial(self._echo_port), self._on_error)
            if enabled:
                fwd.start()
            self._rules[spec.local_port] = (spec, fwd, enabled)
        return spec

    def set_tunnel_enabled(self, local_port, enabled):
        with self._lock:
            entry = self._rules.get(local_port)
            if entry is None:
                raise TunnelError(f"没有本地端口为 {local_port} 的转发规则")
            spec, fwd, on = entry
            if enabled == on:
                return on
            if enabled:
                fwd.start()                      # may raise
            else:
                fwd.stop()
            self._rules[local_port] = (spec, fwd, enabled)
            return enabled

    def remove_tunnel(self, local_port):
        with self._lock:
            entry = self._rules.pop(local_port, None)
        if entry is None:
            return False
        if entry[1] is not None:
            entry[1].stop()
        return True

    def tunnel_specs(self):
        with self._lock:
            return [s for s, _, _ in self._rules.values()]

    def tunnel_states(self):
        with self._lock:
            return {p: on for p, (_, _, on) in self._rules.items()}

    def tunnel_connections(self):
        with self._lock:
            return {p: (f.active_connections if on and f else 0)
                    for p, (_, f, on) in self._rules.items()}


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
pal = palette.get_palette("dark")
echo3 = EchoServer()
stub = StubRegistry(echo3.port)
panel = TunnelPanel(stub, pal)
panel._flash = lambda m: None
panel.refresh()
check("empty state visible with no rules", panel.empty_label.isVisibleTo(panel))
check("list hidden with no rules", not panel.view.isVisibleTo(panel))

stub.add_tunnel(TunnelSpec("local", free_port(), "db.internal", 3306, description="MySQL"))
stub.add_tunnel(TunnelSpec("socks", free_port(), description="proxy"))
panel.refresh()
check("empty state hidden once rules exist", not panel.empty_label.isVisibleTo(panel))
check("list visible once rules exist", panel.view.isVisibleTo(panel))
check("two rows rendered", panel.model.rowCount() == 2, f"rows={panel.model.rowCount()}")
check("six columns (switch + five fields)", panel.model.columnCount() == 6,
      f"cols={panel.model.columnCount()}")


def cells(row):
    return [panel.model.item(row, c).text() for c in range(6)]


row0, row1 = cells(0), cells(1)
check("switch column is empty text, state lives in the role",
      row0[0] == "" and panel.model.item(0, 0).data(SWITCH_ROLE) is True, str(row0))
check("local row shows both endpoints",
      row0[2].startswith("127.0.0.1:") and row0[3] == "db.internal:3306", str(row0))
check("socks row says client picks the target", row1[3] == "由客户端指定", str(row1))
check("description column filled", row0[5] == "MySQL" and row1[5] == "proxy", str(row0[5]))
check("both rules start switched on",
      all(panel.model.item(r, 0).data(SWITCH_ROLE) for r in (0, 1)))
check("summary counts rules and enabled state",
      "2" in panel.count_label.text() and "2 条开启" in panel.count_label.text(),
      panel.count_label.text())

# ---- the switch itself ----------------------------------------------------
socks_port = stub.tunnel_specs()[1].local_port
panel._on_switch(socks_port, False)
check("switch off is reflected in the model",
      panel.model.item(1, 0).data(SWITCH_ROLE) is False)
check("switch off is reflected in the registry",
      stub.tunnel_states()[socks_port] is False)
check("off rule still keeps its row", panel.model.rowCount() == 2)
check("off rule reports no connections", cells(1)[4] == "—", cells(1)[4])
try:
    socket.create_connection(("127.0.0.1", socks_port), timeout=1)
    check("switching off releases the local port", False, "still accepting")
except OSError:
    check("switching off releases the local port", True)
check("summary reflects one rule now off",
      "1 条开启" in panel.count_label.text(), panel.count_label.text())

panel._on_switch(socks_port, True)
check("switching back on restores the port",
      panel.model.item(1, 0).data(SWITCH_ROLE) is True)
held2 = socket.create_connection(("127.0.0.1", socks_port), timeout=5)
held2.settimeout(5)
socks5_connect(held2, "echo.test", echo3.port)
time.sleep(0.4)
panel._refresh_counts()
cell = cells(1)[4]
label = panel.count_label.text()
check("连接数 cell ticks up", cell == "1", f"cell={cell}")
check("summary agrees with the cell", "1 个活动连接" in label, label)
held2.close()
time.sleep(0.6)
panel._refresh_counts()
check("counter returns to zero", cells(1)[4] == "0", cells(1)[4])

# toggling to the state it is already in must be a harmless no-op
panel._on_switch(socks_port, True)
check("idempotent toggle keeps it on",
      panel.model.item(1, 0).data(SWITCH_ROLE) is True)

# removing a rule is distinct from switching it off
victim = stub.tunnel_specs()[0].local_port
check("remove reports success", stub.remove_tunnel(victim) is True)
panel.refresh()
check("row removed from the model", panel.model.rowCount() == 1, f"rows={panel.model.rowCount()}")
check("removing again reports False", stub.remove_tunnel(victim) is False)

panel.shutdown()
stub.remove_tunnel(socks_port)
echo3.stop()
check("panel torn down", True)

print("\n" + "=" * 70)
print("J. SSHConnection switch semantics (real registry, no transport)")
print("=" * 70)
from sshclient.remote.connection import ConnectionInfo, SSHConnection

real = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
sw_port = free_port()
real.add_tunnel(TunnelSpec("local", sw_port, "db.internal", 3306, description="MySQL"))
check("rule starts enabled", real.tunnel_states()[sw_port] is True)
try:
    socket.create_connection(("127.0.0.1", sw_port), timeout=1).close()
    check("enabled rule accepts connections", True)
except OSError as exc:
    check("enabled rule accepts connections", False, str(exc))

# the whole point of the switch: config survives, the port does not
real.set_tunnel_enabled(sw_port, False)
check("state reads back as off", real.tunnel_states()[sw_port] is False)
check("rule is still configured", len(real.tunnel_specs()) == 1)
check("spec is unchanged by the switch",
      real.tunnel_specs()[0].remote_host == "db.internal"
      and real.tunnel_specs()[0].description == "MySQL")
# **这里原本是个竞态。** 上面那次 `create_connection(...).close()` 一返回，
# 服务端 accept 线程可能还没把计数减回去，紧接着断言 `== 0` 就会红——
# 实测 4 轮里红 1 轮。它量到的不是产品行为，是"我跑得够快"。
# 该断的是：连接关掉之后，计数**最终**会落回 0。所以等它落定再断言，
# 而且必须有上限，否则真出问题时这里会挂住而不是报红。
_deadline = time.time() + 3
while (time.time() < _deadline
       and real.tunnel_connections().get(sw_port, 0) != 0):
    time.sleep(0.05)
check("disabled rule reports zero connections", real.tunnel_connections()[sw_port] == 0,
      "3 秒后计数仍是 %s" % real.tunnel_connections().get(sw_port))
try:
    socket.create_connection(("127.0.0.1", sw_port), timeout=1).close()
    check("disabled rule released the port", False, "still accepting")
except OSError:
    check("disabled rule released the port", True)

# while it is off, the port is free for something else to take
squatter = socket.socket()
squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
squatter.bind(("127.0.0.1", sw_port))
squatter.listen(1)
check("another process can take the port while the rule is off", True)
try:
    real.set_tunnel_enabled(sw_port, True)
    check("re-enabling fails while the port is squatted", False, "should have raised")
except TunnelError as exc:
    check("re-enabling fails while the port is squatted", True, str(exc)[:40])
check("rule stays off after a failed enable", real.tunnel_states()[sw_port] is False)
squatter.close()
time.sleep(0.2)

real.set_tunnel_enabled(sw_port, True)
check("re-enabling succeeds once the port frees up",
      real.tunnel_states()[sw_port] is True)
check("idempotent enable is a no-op", real.set_tunnel_enabled(sw_port, True) is True)
check("idempotent disable is a no-op",
      (real.set_tunnel_enabled(sw_port, False), real.set_tunnel_enabled(sw_port, False))[-1]
      is False)
try:
    real.set_tunnel_enabled(1, True)
    check("toggling an unknown rule raises", False, "should have raised")
except TunnelError:
    check("toggling an unknown rule raises", True)

# add while disabled: registers without binding anything
idle_port = free_port()
real.add_tunnel(TunnelSpec("socks", idle_port, description="lazy"), enabled=False)
check("add_tunnel(enabled=False) registers without binding",
      real.tunnel_states()[idle_port] is False)
try:
    socket.create_connection(("127.0.0.1", idle_port), timeout=1).close()
    check("disabled-at-birth rule holds no socket", False, "unexpectedly listening")
except OSError:
    check("disabled-at-birth rule holds no socket", True)

# 端口被占时的语义**变了**：原来直接抛错，现在往后顺延。
# 下面两条分别盯住新契约的两端 —— 顺延成功，以及整段都占满时如实报错
# 且不留半条规则（这一条是老判据要保住的性质，只是触发场景变了）。
from sshclient.remote.connection import _PORT_SCAN_LIMIT

clash = socket.socket()
clash.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
clash.bind(("127.0.0.1", 0))
clash.listen(1)
clash_port = clash.getsockname()[1]
before = len(real.tunnel_specs())
moved = real.add_tunnel(TunnelSpec("socks", clash_port))
check("单端口被占时自动顺延到下一个可用端口",
      moved.local_port != clash_port and moved.local_port in real.tunnel_states(),
      f"{clash_port} -> {moved.local_port}")
check("顺延之后规则确实登记了", len(real.tunnel_specs()) == before + 1,
      f"{before} -> {len(real.tunnel_specs())}")
real.remove_tunnel(moved.local_port)
clash.close()

# 把整段扫描范围全占满：这时必须如实抛错，且不留半条规则
hog = []
squat_base = None
try:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    squat_base = probe.getsockname()[1]
    probe.close()
    for i in range(_PORT_SCAN_LIMIT + 2):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            s.bind(("127.0.0.1", squat_base + i))
            s.listen(1)
            hog.append(s)
        except OSError:
            s.close()
    before = len(real.tunnel_specs())
    try:
        real.add_tunnel(TunnelSpec("socks", squat_base))
        check("整段端口全被占时如实抛错", False, "should have raised")
    except TunnelError:
        check("整段端口全被占时如实抛错", True)
    check("失败后不留半条规则", len(real.tunnel_specs()) == before,
          f"{before} -> {len(real.tunnel_specs())}")
finally:
    for s in hog:
        s.close()

check("remove clears the rule entirely", real.remove_tunnel(sw_port) is True)
check("removed rule is gone", len(real.tunnel_specs()) == 1)
real.close()
check("close() clears every rule", real.tunnel_specs() == [])

print("\n" + "=" * 70)
print("K. persistence: rules survive a session round-trip")
print("=" * 70)
from sshclient.core.settings import SavedSession

# round-trip a spec through the dict form, including the off state
spec_on = TunnelSpec("local", 13306, "db.internal", 3306, description="MySQL")
spec_off = TunnelSpec("socks", 1080, description="proxy")
d_on = spec_on.to_dict(enabled=True)
d_off = spec_off.to_dict(enabled=False)
back_on = TunnelSpec.from_dict(d_on)
back_off = TunnelSpec.from_dict(d_off)
check("local rule round-trips", back_on is not None and back_on[0].remote_host == "db.internal"
      and back_on[1] is True, str(back_on[0] if back_on else None))
check("off state round-trips", back_off is not None and back_off[1] is False,
      str(back_off[1] if back_off else None))
check("kind survives", back_off is not None and back_off[0].kind == "socks")
check("description survives", back_on is not None and back_on[0].description == "MySQL")
check("malformed dict is rejected", TunnelSpec.from_dict({"kind": "local"}) is None)
check("non-dict is rejected", TunnelSpec.from_row(["x", "y"]) is None)

# the panel's rule set is what gets written back
echo4 = EchoServer()
stub2 = StubRegistry(echo4.port)
panel2 = TunnelPanel(stub2, palette.get_palette("dark"))
panel2._flash = lambda m: None
pa = free_port()
pb = free_port()
stub2.add_tunnel(TunnelSpec("local", pa, "db.internal", 3306, description="MySQL"))
stub2.add_tunnel(TunnelSpec("socks", pb, description="proxy"))
panel2.refresh()
stub2.set_tunnel_enabled(pb, False)
rules = panel2.current_rules()
check("current_rules returns both rules", len(rules) == 2, str(len(rules)))
check("off rule is included with enabled=False",
      any(r["kind"] == "socks" and r["enabled"] is False for r in rules), str(rules))
check("on rule is included with enabled=True",
      any(r["local_port"] == pa and r["enabled"] is True for r in rules))

# save -> reload -> the rules and their states come back
saved = SavedSession(name="s", host="h", tunnels=rules)
reloaded = SavedSession.from_dict(saved.to_dict())
check("SavedSession round-trips the rule set", len(reloaded.tunnels) == 2,
      str(len(reloaded.tunnels)))
check("off state survives the session round-trip",
      any(r["kind"] == "socks" and r["enabled"] is False for r in reloaded.tunnels),
      str(reloaded.tunnels))

# re-arming a fresh connection from the stored rules restores both, with the
# off one registered but not listening. Release the stub's listeners first:
# they still hold the ports, and the whole point is a *fresh* connection.
panel2.shutdown()
stub2.remove_tunnel(pa)
stub2.remove_tunnel(pb)
echo4.stop()

fresh = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
armed_on = armed_off = 0
for entry in reloaded.tunnels:
    parsed = TunnelSpec.from_dict(entry)
    if parsed is None:
        continue
    s, en = parsed
    fresh.add_tunnel(s, enabled=en)
    if en:
        armed_on += 1
    else:
        armed_off += 1
check("re-arming restores both rules", len(fresh.tunnel_specs()) == 2,
      str(len(fresh.tunnel_specs())))
check("the on rule listens again", fresh.tunnel_states()[pa] is True)
check("the off rule stays off", fresh.tunnel_states()[pb] is False)
try:
    socket.create_connection(("127.0.0.1", pb), timeout=1).close()
    check("the off rule holds no socket after re-arming", False, "still accepting")
except OSError:
    check("the off rule holds no socket after re-arming", True)
fresh.close()

# legacy session files migrate to the new shape
legacy = SavedSession.from_dict({
    "name": "old", "host": "h", "port": 22, "username": "u",
    "tunnel_local": [[13306, "db.internal", 3306, "MySQL", "127.0.0.1"]],
    "tunnel_socks": 1080,
})
check("legacy session migrates to 2 rules", len(legacy.tunnels) == 2,
      str(legacy.tunnels))
check("legacy local rule kept its fields",
      legacy.tunnels[0]["remote_host"] == "db.internal"
      and legacy.tunnels[0]["local_port"] == 13306, str(legacy.tunnels[0]))
check("legacy socks rule migrated",
      any(r["kind"] == "socks" and r["local_port"] == 1080 for r in legacy.tunnels))
check("migrated rules default to enabled",
      all(r["enabled"] is True for r in legacy.tunnels))
check("new field wins when both are present",
      len(SavedSession.from_dict({
          "tunnels": [{"kind": "socks", "local_port": 9, "enabled": False}],
          "tunnel_local": [[1, "x", 2]],
          "tunnel_socks": 3,
      }).tunnels) == 1)
check("session with no tunnels loads cleanly",
      SavedSession.from_dict({"name": "plain", "host": "h"}).tunnels == [])

print("\n" + "=" * 70)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
print("=" * 70)
sys.exit(1 if FAIL else 0)
