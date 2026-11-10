"""Stability / stress profile for the tunnel layer.

Functional correctness is covered by _tunnel.py. This file asks the questions
that only show up under repetition:

  A. concurrency   -- 50 simultaneous connections, each getting its own payload
  B. leaks         -- thread and handle counts must not grow across cycles
  C. big transfer  -- multi-MB through the relay, checks for backpressure stalls
  D. half-close    -- client shutdown(WR) must not kill the return path
  E. churn         -- add/remove the same port repeatedly
  F. mutation race -- add/remove while traffic is in flight
  G. counter       -- active count returns to exactly 0, never goes negative

Handle counting uses the Win32 GetProcessHandleCount on Windows and /proc/self/fd
(Linux) or /dev/fd (macOS) elsewhere, rather than psutil: on all three a socket
is a kernel object, so the count tracks leaked fds directly and needs no extra
dependency.

Run: .venv\\Scripts\\python.exe tests\\_stress.py
"""
import os
import socket
import struct
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sshclient.core.tunnel import Forward, TunnelSpec

# 句柄计数的实现按平台分。Windows 用 Win32 的 GetProcessHandleCount；POSIX 上
# 改数 /proc/self/fd（Linux）或 /dev/fd（macOS）。
# **必须在模块顶层就分流**：`ctypes.WinDLL` 和 `ctypes.wintypes` 在非 Windows
# 上根本不存在，之前在这里无条件下标会让整个脚本 import 就抛
# AttributeError —— 于是 macOS/Linux 上这套压力测试压根跑不起来。
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes

    # Explicit signatures: the default c_int types truncate the 64-bit handle.
    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _K32.GetProcessHandleCount.argtypes = [
        ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
    _K32.GetProcessHandleCount.restype = ctypes.wintypes.BOOL
    _K32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
elif os.path.isdir("/proc/self/fd"):
    _K32 = None
else:                                   # macOS: /dev/fd
    _K32 = None

#: POSIX 上数 fd 用哪个目录；None 表示这个平台没有可用的来源。
_FD_DIR = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else (
    "/dev/fd" if os.path.isdir("/dev/fd") else None)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def handles() -> int:
    """Count this process's open kernel objects (sockets/fds).

    The ctypes signatures matter on Windows: with the default ``c_int`` types
    the 64-bit pseudo-handle from GetCurrentProcess gets truncated and the call
    silently fails, leaving the counter pinned at 0 -- which would make every
    leak check below pass for the wrong reason.
    :func:`handles_are_measurable` proves the counter actually moves before
    any result is believed, on every platform.
    """
    if sys.platform == "win32":
        count = ctypes.wintypes.DWORD(0)
        ok = _K32.GetProcessHandleCount(
            _K32.GetCurrentProcess(), ctypes.byref(count))
        if not ok:
            raise OSError(
                f"GetProcessHandleCount failed: {ctypes.get_last_error()}")
        return count.value
    if _FD_DIR is None:
        raise OSError("本平台没有可用的 fd 计数来源（/proc/self/fd 与 /dev/fd 都没有）")
    return len(os.listdir(_FD_DIR))


def handles_are_measurable() -> bool:
    """Sanity-check the counter by opening sockets that must raise it.

    Without this, a broken handle query looks exactly like a clean process.
    """
    base = handles()
    held = [socket.socket() for _ in range(40)]
    for s in held:
        s.bind(("127.0.0.1", 0))
    try:
        opened = handles()
    finally:
        for s in held:
            s.close()
    time.sleep(0.3)
    return opened >= base + 40 and handles() <= base + 2


def threads() -> int:
    return threading.active_count()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def settle(seconds=0.8, until=None) -> None:
    """等到「收尾真的走完」并且「句柄数不再变化」，再让调用方去量。

    **为什么不能只 sleep 一个固定时间。** 原来这里只有
    `gc.collect(); sleep(0.8); gc.collect()` —— 那是在**猜**收尾要多久。
    端口转发的 ``stop()`` 现在会一并关掉**已建立的连接**（这是「停规则就
    该真停」的修复目标），它带一个 1.5 秒的等待预算，收尾时间于是超过
    那个固定睡眠：测量点落在收尾**还没走完**的当口，凭空多算 1 个句柄
    （Windows 的 ``GetProcessHandleCount`` 把线程句柄也算进去，所以一个
    还没退完的服务线程就是 +1）。

    **为什么不能只等 `until` 条件。** 条件成立只说明逻辑收尾了，句柄真正
    还给操作系统还要一会儿 —— 只等条件就量，量出来的是一个**偏早**的数，
    同样不作数。（第一版就犯了这个错：条件一成立直接跳过 sleep，反而量早了。）

    泄漏判据的成立条件是「收完之后还剩多少」，所以这里等的是**句柄数稳住**：
    连续两次读数（间隔 80ms）相同才算稳。整体有上限，不会挂死。
    """
    import gc
    gc.collect()
    if until is not None:
        cond_deadline = time.monotonic() + max(seconds, 2.0)
        while time.monotonic() < cond_deadline and not until():
            time.sleep(0.02)
    deadline = time.monotonic() + max(seconds, 2.0)
    stable, last = 0, -1
    while time.monotonic() < deadline and stable < 2:
        time.sleep(0.08)
        gc.collect()
        now = handles()
        stable = stable + 1 if now == last else 0
        last = now


# --------------------------------------------------------------------------
# servers
# --------------------------------------------------------------------------


class EchoServer:
    """Echoes everything back, one thread per connection."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(128)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        #: **还在服务中的连接数。** 收尾判据要等它归零 —— 见 ``_echo`` 的注释。
        self.live = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            with self._lock:
                self.hits += 1
                # **必须在这里加，不能等 _echo 起来再加。** 线程 start 到
                # 真正进入 _echo 之间有一个窗口，那窗口里 `live` 还是 0，
                # 收尾判据会以为「已经没人管这些连接了」而提前放行。
                self.live += 1
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        try:
            conn.settimeout(20.0)
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            # **线程退出不是句柄归零。** Windows 的 GetProcessHandleCount
            # 把线程句柄和 socket 句柄一起数，而这条线程的 socket 真正还给
            # 内核是在 close() 之后 —— 所以 `live` 要在 close() **之后**才减。
            with self._lock:
                self.live -= 1

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class ResponderServer:
    """Reads until EOF, *then* answers.

    This is the half-close shape: a client that finishes its request and
    half-closes the socket must still get a response back. HTTP/1.0, MySQL and
    Redis all behave this way, and it is exactly what a naive relay breaks.
    """

    def __init__(self, reply: bytes):
        self.reply = reply
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(10.0)
            got = 0
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                got += len(data)
            conn.sendall(self.reply)
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


# --------------------------------------------------------------------------
# SOCKS5 client helper
# --------------------------------------------------------------------------


def socks5_open(listen_port: int, target_host: str, target_port: int,
                timeout: float = 20.0) -> socket.socket:
    """Connect through a SOCKS5 forward and return a ready socket."""
    s = socket.create_connection(("127.0.0.1", listen_port), timeout=timeout)
    s.settimeout(timeout)
    s.sendall(bytes([0x05, 0x01, 0x00]))
    if s.recv(2) != bytes([0x05, 0x00]):
        raise OSError("SOCKS5 method selection refused")
    raw = target_host.encode()
    s.sendall(bytes([0x05, 0x01, 0x00, 0x03, len(raw)]) + raw
              + struct.pack("!H", target_port))
    reply = s.recv(10)
    if len(reply) < 2 or reply[1] != 0x00:
        raise OSError(f"SOCKS5 CONNECT refused: {reply.hex()}")
    return s


def recv_exactly(s: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError(f"short read {len(buf)}/{n}")
        buf.extend(chunk)
    return bytes(buf)


def _dial(port: int) -> socket.socket:
    """Stand-in for open_direct_channel.

    paramiko.Channel exposes recv/sendall/close/settimeout/shutdown/fileno,
    all of which a socket also has, so this satisfies the same contract the
    real channel does.
    """
    return socket.create_connection(("127.0.0.1", port), timeout=20)


print("=" * 74)
print("MavisSSH tunnel — stability profile")
print("=" * 74)

# Gate every leak assertion below on the counter actually working.
if not handles_are_measurable():
    print("  FAIL  handle counter is not measurable -- leak checks would be vacuous")
    print("=" * 74)
    sys.exit(2)
check("handle counter is measurable (self-calibration)", True,
      "40 sockets moved it and closing them restored it")

base_h, base_t = handles(), threads()
print(f"\nbaseline: {base_h} handles, {base_t} threads")

# ==========================================================================
print("\n=== A. concurrency: 50 simultaneous connections ===")
# ==========================================================================
echo = EchoServer()
listen = free_port()
fwd = Forward(TunnelSpec("socks", listen), lambda h, p: _dial(echo.port)).start()

# **这两条连接的基线必须在服务器起来之后取。**
# 原来 A 段那两条「no … leak」判据拿的是上面那个 `base_h` —— 取自 EchoServer
# 和 Forward **都还没建**的那一行。于是它在比「什么都没开」和
# 「Forward + EchoServer 两个监听 socket 各自的 accept 线程都开着」：
# **两个本来就不该相等的状态**。实测差 13，其中绝大部分就是这 4 个
# （2 个监听 socket + 2 个 accept 线程），另有几条是刚关掉的连接还没还给内核。
#
# **这不是泄漏，这一点有独立证据**（红的就是发现，但要先分清是哪一种红）：
#   · 后面的重复判据全平 —— 4 轮 `140/3 → 140/3 → 140/3 → 140/3`；
#   · churn / storm / registry 三处都是 `145 -> 145`，一动不动；
#   · 文件末尾那条 `process returned to a clean handle count (127 -> 131)` 是绿的；
#   · 单独把「起 Forward → 50 连接 → 停」整条生命周期连跑 3 轮，句柄数
#     逐轮 131 / 131 / 131 —— **恒定，不随连接数增长**。
# 也就是说不按连接累积。判据比错了对象，不是产品漏了句柄。
pre_h, pre_t = handles(), threads()
print(f"        servers up, before traffic: {pre_h} handles, {pre_t} threads")

N = 50
PAYLOAD_REPEATS = 409          # repetitions of the per-connection marker
results: dict[int, bytes] = {}
errors: list[str] = []


def marker(idx: int) -> bytes:
    return f"conn-{idx:03d}:".encode()


def worker(idx: int) -> None:
    try:
        s = socks5_open(listen, "echo.test", echo.port)
        body = marker(idx) * PAYLOAD_REPEATS
        s.sendall(body)
        results[idx] = recv_exactly(s, len(body))
        s.close()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"conn {idx}: {type(exc).__name__}: {exc}")


t0 = time.perf_counter()
threads_list = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in threads_list:
    t.start()
for t in threads_list:
    t.join(timeout=30)
elapsed = time.perf_counter() - t0

check(f"all {N} concurrent connections succeeded", len(results) == N,
      f"{len(results)}/{N} ok" + (f"; first error: {errors[0]}" if errors else ""))
check("no connection raised", not errors, "; ".join(errors[:2]))

# Compare against the length actually sent, not a hand-computed constant:
# "conn-000:" is 9 bytes, not 10, and a wrong expectation here reads exactly
# like data loss.
bad = []
for idx, got in sorted(results.items()):
    want_len = len(marker(idx)) * PAYLOAD_REPEATS
    if len(got) != want_len or got != marker(idx) * PAYLOAD_REPEATS:
        bad.append(f"conn {idx}: len={len(got)}/{want_len} "
                   f"prefix={got[:9]!r} want={marker(idx)!r}")
check("every payload came back intact and unmixed", not bad,
      f"{len(results) - len(bad)}/{len(results)} clean" +
      ("; " + "; ".join(bad[:3]) if bad else ""))
check("remote accepted every connection", echo.hits >= N, f"hits={echo.hits}")
total_bytes = sum(len(v) for v in results.values())
print(f"        {N} connections, {total_bytes} bytes verified in {elapsed:.2f}s "
      f"({total_bytes / elapsed / 1024 / 1024:.1f} MiB/s aggregate)")

# **收尾条件只等「连接这一侧」收干净，不能等「没有 tunnel 线程」。**
# 原来那条 `not any(t.name.startswith("tunnel-") and t.is_alive())` 在这一段
# **永远不成立**：A 段**故意不调 `fwd.stop()`**（Forward 要一直活到 D 段），
# 所以它的 accept 线程本就该一直开着。于是 settle 白等满 2 秒的 `cond_deadline`
# 就放行 —— 这个「等」是个空动作，量到的是一个**量早**的点。
# 要等的是**每条连接的服务线程**（名字带 `-conn`）和 echo 那一侧。
settle(until=lambda: (fwd.active_connections == 0
                      and echo.live == 0
                      and not any(
                          t.name.endswith("-conn") and t.is_alive()
                          for t in threading.enumerate())))
mid_h, mid_t = handles(), threads()
_live = [(t.name, t.daemon) for t in threading.enumerate()]
_conn_threads = [n for n, _d in _live if n.endswith("-conn")]
print(f"        after settle: {mid_h} handles, {mid_t} threads "
      f"(echo.live={echo.live} 连接线程={_conn_threads})")
check("no thread leak after fan-in", mid_t <= pre_t + 2, f"{pre_t} -> {mid_t}")
check("no handle leak after fan-in", mid_h <= pre_h + 12, f"{pre_h} -> {mid_h}")
check("收尾判据等到了连接这一侧真的收干净（否则上面那两条量的是量早）",
      fwd.active_connections == 0 and echo.live == 0 and not _conn_threads
      and fwd.running,
      f"active={fwd.active_connections} echo.live={echo.live} "
      f"连接线程={_conn_threads} fwd.running={fwd.running}")
check("Forward 的 accept 线程这一段是**故意**活着的（所以不能拿它当泄漏）",
      fwd.running and any(n == f"tunnel-{listen}" for n, _d in _live),
      f"running={fwd.running} live={_live}")
check("active count settled at 0", fwd.active_connections == 0,
      f"active={fwd.active_connections}")

# ==========================================================================
print("\n=== B. repetition: does anything grow monotonically? ===")
# ==========================================================================
samples = []
for cycle in range(4):
    ts = [threading.Thread(target=worker, args=(100 + cycle * 10 + i,))
          for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)
    settle(0.5)
    samples.append((handles(), threads()))
print("        cycle -> (handles, threads):",
      ", ".join(f"{h}/{t}" for h, t in samples))
handle_series = [h for h, _ in samples]
check("handle count does not trend upward",
      handle_series[-1] <= handle_series[0] + 10,
      f"{handle_series[0]} -> {handle_series[-1]}")
check("thread count does not trend upward",
      samples[-1][1] <= samples[0][1] + 2,
      f"{samples[0][1]} -> {samples[-1][1]}")
check("active count still 0", fwd.active_connections == 0,
      f"active={fwd.active_connections}")

# ==========================================================================
print("\n=== C. large transfer: 8 MiB through the relay ===")
# ==========================================================================
SIZE = 8 * 1024 * 1024
try:
    s = socks5_open(listen, "echo.test", echo.port, timeout=90)
    block = bytes(range(256)) * 256            # 64 KiB, non-repeating enough
    t0 = time.perf_counter()
    sent = threading.Event()

    def sender():
        try:
            for _ in range(SIZE // len(block)):
                s.sendall(block)
            sent.set()
        except OSError as exc:
            errors.append(f"sender: {exc}")

    st = threading.Thread(target=sender, daemon=True)
    st.start()
    got = recv_exactly(s, SIZE)
    st.join(timeout=30)
    dt = time.perf_counter() - t0
    check("8 MiB round-tripped", len(got) == SIZE, f"{len(got)} bytes")
    check("8 MiB content intact", got == block * (SIZE // len(block)),
          "byte-for-byte compare")
    print(f"        {SIZE / dt / 1024 / 1024:.1f} MiB/s over loopback "
          f"(no stall, no truncation)")
    s.close()
except Exception as exc:  # noqa: BLE001
    check("8 MiB round-tripped", False, f"{type(exc).__name__}: {exc}")
settle()
check("active count 0 after big transfer", fwd.active_connections == 0,
      f"active={fwd.active_connections}")

# ==========================================================================
print("\n=== D. half-close: reply must survive client shutdown(WR) ===")
# ==========================================================================
resp = ResponderServer(b"HELLO-FROM-REMOTE-AFTER-EOF")
listen2 = free_port()
fwd2 = Forward(TunnelSpec("local", listen2, "resp.test", resp.port),
               lambda h, p: _dial(p)).start()
try:
    c = socket.create_connection(("127.0.0.1", listen2), timeout=20)
    c.settimeout(20)
    c.sendall(b"REQUEST-BODY-THEN-HALF-CLOSE")
    c.shutdown(socket.SHUT_WR)                  # client is done writing
    got = c.recv(64)
    check("response arrives after the client half-closes",
          got == b"HELLO-FROM-REMOTE-AFTER-EOF", got.decode(errors="replace"))
    c.close()
except Exception as exc:  # noqa: BLE001
    check("response arrives after the client half-closes", False,
          f"{type(exc).__name__}: {exc}")
settle()

# a full close (not half) must also not hang the relay
try:
    c2 = socket.create_connection(("127.0.0.1", listen2), timeout=20)
    c2.settimeout(20)
    c2.sendall(b"x" * 10)
    c2.close()
    settle(0.5)
    check("abrupt client close does not wedge the forward",
          fwd2.active_connections == 0, f"active={fwd2.active_connections}")
    c3 = socket.create_connection(("127.0.0.1", listen2), timeout=20)
    c3.settimeout(20)
    c3.sendall(b"again")
    c3.shutdown(socket.SHUT_WR)      # ResponderServer answers on EOF, so it
    check("forward still serves new clients afterwards",   # must half-close too
          c3.recv(64) == b"HELLO-FROM-REMOTE-AFTER-EOF")
    c3.close()
except Exception as exc:  # noqa: BLE001
    check("forward still serves new clients afterwards", False,
          f"{type(exc).__name__}: {exc}")
resp.stop()

# ==========================================================================
print("\n=== E. churn: add/remove the same port 30x ===")
# ==========================================================================
churn_port = free_port()
h_before = handles()
for i in range(30):
    f = Forward(TunnelSpec("socks", churn_port), lambda h, p: _dial(echo.port))
    f.start()
    f.stop()
settle(1.0)
h_after = handles()
check("churn leaks no handles", h_after <= h_before + 8,
      f"{h_before} -> {h_after}")
check("churn leaks no threads", threads() <= base_t + 4, f"{base_t} -> {threads()}")

# rapid-fire connections against a forward that is being torn down
victim = Forward(TunnelSpec("socks", free_port()), lambda h, p: _dial(echo.port)).start()
vp = victim.spec.local_port


def hammer() -> None:
    for _ in range(40):
        try:
            s = socket.create_connection(("127.0.0.1", vp), timeout=1)
            s.close()
        except OSError:
            pass


ht = [threading.Thread(target=hammer) for _ in range(4)]
for t in ht:
    t.start()
time.sleep(0.05)
victim.stop()                                   # pull the listener mid-storm
for t in ht:
    t.join(timeout=15)
settle(1.0)
check("stop() during a connection storm is clean",
      victim.active_connections == 0, f"active={victim.active_connections}")
check("stop() during a storm leaks nothing", handles() <= h_after + 10,
      f"{h_after} -> {handles()}")

# ==========================================================================
print("\n=== F. registry mutation while traffic flows ===")
# ==========================================================================
reg_port = free_port()
reg = Forward(TunnelSpec("socks", reg_port), lambda h, p: _dial(echo.port)).start()
stop_flag = threading.Event()
# **三桶，不能合成一个 `bad`。** 原来只有 `bad`，而它把两件性质完全不同的事
# 混在一起：收到**错的字节**（真损坏）和收到 **`b""`（干净 EOF）**。
# 后者是 F 段剧本的**预期后果**：这一段故意在流量中途 `stop()`，而 `stop()`
# 的既定语义就是「停 = 真停」、会掐掉已建立的连接（见 `Forward._drop_connections`
# 的取舍说明）。连接在 `sendall` 和 `recv` 之间被掐，干净 FIN 让 `recv` 返回
# `b""` —— 这不是数据被写坏了，是连接被按约定关掉了。
#
# **这个混淆还有一处自相矛盾**：同样的事件若以 RST 形式出现，会被
# `except OSError: pass` 放过；只以 FIN 形式出现就被记成 `bad`。也就是说
# **TCP 拆除时序决定这条判据会不会红** —— 实测 6 次跑红 1 次、12 轮复现里
# 2 次「坏」全是 `b""`，而**收到错字节 0 次**（864 次成功往返）。
# 所以这里拆成三桶，损坏那一桶单独要求为 0。
counter = {"n": 0, "wrong": 0, "eof": 0, "other": 0}
_samples: list = []


def traffic() -> None:
    while not stop_flag.is_set():
        try:
            s = socks5_open(reg_port, "echo.test", echo.port, timeout=5)
            s.sendall(b"ping")
            got = s.recv(16)
            if got != b"ping":
                if got == b"":
                    counter["eof"] += 1          # 被 stop() 掐掉的预期结果
                else:
                    counter["wrong"] += 1        # **这才是数据损坏**
                    _samples.append(got)
            s.close()
            counter["n"] += 1
        except OSError:
            pass
        except Exception as exc:               # noqa: BLE001
            counter["other"] += 1
            _samples.append(type(exc).__name__)


tt = threading.Thread(target=traffic, daemon=True)
tt.start()
time.sleep(0.4)
reg.stop()
time.sleep(0.4)
reg.start()                                    # same Forward object, restarted
time.sleep(0.4)
reg.stop()
stop_flag.set()
tt.join(timeout=10)
settle(1.0)
check("traffic ran during mutation", counter["n"] > 0, f"{counter['n']} round-trips")
check("no corrupted exchange under mutation", counter["wrong"] == 0 and counter["other"] == 0,
      f"错字节 {counter['wrong']} + 异常 {counter['other']}，共 {counter['n']} 次往返"
      + ("；样本=%r" % (_samples[:3],) if _samples else "")
      + f"（另有 {counter['eof']} 次是 stop() 中途掐连接的干净 EOF，按约定不算损坏）")
# 元断言：把 echo 换成**故意回错字节**的，这一条必须变红 —— 否则它恒绿，
# 和「把 bad 整个删掉」没有区别。
_real_dial = _dial


# **签名必须是两个参数**：`Forward` 按 `(host, port)` 调 dialer（见上面
# `lambda h, p: _dial(echo.port)`）。写成单参会 TypeError，于是每条连接都
# 建不起来 —— 那是探针的错，不是产品的错。
def _corrupt_dial(_host, port):
    return _CorruptingSock(_real_dial(port))


class _CorruptingSock:
    """在 `put` 的回程上把字节换掉 —— 模拟真损坏。"""

    def __init__(self, s):
        self._s = s

    def sendall(self, data):
        self._s.sendall(b"XXX")

    def recv(self, _n):
        return self._s.recv(_n)

    def settimeout(self, t=None):
        self._s.settimeout(t)

    def close(self):
        self._s.close()

    def fileno(self):
        return self._s.fileno()


_cport = free_port()
_corrupt = Forward(TunnelSpec("socks", _cport), _corrupt_dial).start()
_bad = {"wrong": 0, "n": 0}
_stop2 = threading.Event()


def _ctraffic() -> None:
    while not _stop2.is_set():
        try:
            s = socks5_open(_cport, "echo.test", echo.port, timeout=5)
            s.sendall(b"ping")
            if s.recv(16) != b"ping":
                _bad["wrong"] += 1
            s.close()
            _bad["n"] += 1
        except OSError:
            pass
        except Exception:                       # noqa: BLE001
            pass


_t2 = threading.Thread(target=_ctraffic, daemon=True)
_t2.start()
time.sleep(0.3)
_stop2.set()
_t2.join(timeout=10)
_corrupt.stop()
check("元断言：把回程字节换掉之后「no corrupted exchange」**确实**会红"
      "（所以上面那条不是恒绿）", _bad["wrong"] > 0,
      "错字节 %d / 共 %d 次往返" % (_bad["wrong"], _bad["n"]))
check("active count settled at 0", reg.active_connections == 0,
      f"active={reg.active_connections}")
check("registry churn leaked nothing", handles() <= h_after + 12,
      f"{h_after} -> {handles()}")

# ==========================================================================
print("\n=== G. counter never goes negative ===")
# ==========================================================================
neg = []
for _ in range(30):
    try:
        s = socks5_open(listen, "echo.test", echo.port, timeout=5)
    except OSError:
        continue
    if fwd.active_connections < 0:
        neg.append(fwd.active_connections)
    s.close()
settle()
check("counter never observed negative", not neg, str(neg[:3]))
check("counter ends at exactly 0", fwd.active_connections == 0,
      f"active={fwd.active_connections}")

# --------------------------------------------------------------------------
for f in (fwd, fwd2, reg):
    try:
        f.stop()
    except Exception:
        pass
echo.stop()
settle(1.0)
final_h, final_t = handles(), threads()
print(f"\nfinal: {final_h} handles (baseline {base_h}), {final_t} threads (baseline {base_t})")
check("process returned to a clean handle count", final_h <= base_h + 12,
      f"{base_h} -> {final_h}")
check("process returned to a clean thread count", final_t <= base_t + 2,
      f"{base_t} -> {final_t}")

print("\n" + "=" * 74)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILURES:")
    for name in FAIL:
        print("  -", name)
print("=" * 74)
sys.exit(1 if FAIL else 0)
