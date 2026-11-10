"""连接·转发·传输·监控·图标·主题 的功能测试（无 SSH 服务器，无真实远端）。

覆盖 ``remote/connection.py``、``core/tunnel.py``、``ui/tunnel_panel.py``、
``core/lrzsz.py``、``ui/monitor_panel.py``、``ui/icons.py``、
``ui/design/v1/{palette,qss}.py``。

三条硬规矩，本文件全程遵守：

1. **绝不碰用户真实配置。** ``AppSettings`` 一律在 4 行内改掉 ``_path`` 指到临时
   目录；``SessionStore`` **绝不无参构造**（一律给 path + config_dir，落在临时
   目录里）。所有落盘产物（lrzsz 对勾 PNG 之类）也指向临时目录，仓库里
   包括 ``__file__`` 在内的任何路径都不会被写。
2. **绝不真连服务器。** 转发链路用注入的 ``open_channel``（本地 socket 对灌），
   连接层用假 SSH 对象，监控用假 ``exec``。
3. **端口纪律。** 只用随机空闲端口；每条判据都在 ``finally`` 里
   ``Forward.stop()`` + join，异常路径也不许泄漏监听端口。

判据全部钉在"看得见"的东西上：真实 socket 上的字节、真生成的 QSS 串、
真渲染的像素（像素探针都先自校验）。

红的就是发现，见文件末尾的发现项。判据数 / 通过 / 失败 / 耗时打印在最后。

Run: .venv\\Scripts\\python.exe tests\\_fconn.py
"""
import ast
import atexit
import inspect
import os
import re
import shutil
import socket
import struct
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6 import (  # noqa: E402
    QtCore,
    QtGui,
    QtWidgets,
    sip,  # noqa: E402
)

from sshclient.core import lrzsz, tunnel  # noqa: E402
from sshclient.core.settings import UI_SIZE_REF, AppSettings  # noqa: E402
from sshclient.core.tunnel import Forward, TunnelError, TunnelSpec  # noqa: E402
from sshclient.remote import connection as connmod  # noqa: E402
from sshclient.remote.connection import (  # noqa: E402
    AuthConfig,
    ConnectionInfo,
    SSHConnection,
)
from sshclient.ui import icons  # noqa: E402
from sshclient.ui.design.v1 import palette as palmod  # noqa: E402
from sshclient.ui.design.v1 import qss  # noqa: E402

PASS, FAIL, SKIP = [], [], []
_T0 = time.monotonic()
_FORWARDS: list = []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", name,
                          ("  (%s)" % detail) if detail else ""))


def skip(name, why):
    SKIP.append(name)
    print("  SKIP  %s  (%s)" % (name, why))


def meta(name, ok, detail=""):
    """元断言：把被测实现换成缺陷版本后，同一批判据必须变红。"""
    (PASS if ok else FAIL).append(name)
    print("  %s  %s%s" % ("PASS" if ok else "FAIL", "[meta] " + name,
                          ("  (%s)" % detail) if detail else ""))


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------- 基础设施


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_until(pred, ms=2000, step=0.02):
    """轮询到 *pred* 为真。返回是否在期限内成立。"""
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(step)
    try:
        return bool(pred())
    except Exception:
        return False


def track(fwd):
    _FORWARDS.append(fwd)
    return fwd


def stop_all():
    for f in list(_FORWARDS):
        try:
            f.stop()
        except Exception:
            pass
        t = getattr(f, "_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
    _FORWARDS.clear()


def can_bind(port, host="127.0.0.1"):
    s = socket.socket()
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def free_run(n):
    """找到一段连续 n 个当前可绑的端口，返回首端口。"""
    for _ in range(400):
        base = free_port()
        socks = []
        try:
            for i in range(n):
                s = socket.socket()
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", base + i))
                s.listen(1)
                socks.append(s)
            for s in socks:
                s.close()
            return base
        except OSError:
            for s in socks:
                s.close()
    raise RuntimeError("找不到连续 %d 个空闲端口" % n)


def occupy(base, n):
    """占住 base..base+n-1，返回这些 socket（调用方负责关）。"""
    socks = []
    for i in range(n):
        s = socket.socket()
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", base + i))
        s.listen(1)
        socks.append(s)
    return socks


class EchoServer:
    """'远端'：一个真实 TCP 回显服务。"""

    def __init__(self, tail=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self.rx = 0
        self.seen_eof = False
        self._tail = tail
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.hits += 1
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(6.0)
            while True:
                data = conn.recv(65536)
                if not data:
                    self.seen_eof = True
                    if self._tail:
                        conn.sendall(self._tail)
                    break
                self.rx += len(data)
                conn.sendall(data)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def recv_exact(sock, n, timeout=6.0):
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("对端提前关闭，只收到 %d/%d" % (len(buf), n))
        buf += chunk
    return buf


def socks5_connect(sock, host, port, atyp="domain"):
    sock.sendall(bytes([0x05, 0x01, 0x00]))
    reply = recv_exact(sock, 2)
    assert reply == bytes([0x05, 0x00]), reply
    if atyp == "ipv4":
        addr = bytes([0x01]) + socket.inet_aton(host)
    elif atyp == "ipv6":
        addr = bytes([0x04]) + socket.inet_pton(socket.AF_INET6, host)
    else:
        raw = host.encode()
        addr = bytes([0x03, len(raw)]) + raw
    sock.sendall(bytes([0x05, 0x01, 0x00]) + addr + struct.pack("!H", port))
    return recv_exact(sock, 10)


def dial(port):
    """open_channel 替身：返回真 socket，充当 direct-tcpip 通道。"""
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def never(*_a, **_kw):
    raise OSError("connection refused (替身)")


# 像素探针 --------------------------------------------------------------


def color_hist(pm):
    img = pm.toImage()
    hist = {}
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() == 0:
                continue
            hist[c.name()] = hist.get(c.name(), 0) + 1
    return hist


def opaque_colors(pm):
    return set(color_hist(pm))


def dominant_other(pm, background):
    """除背景外面积最大的颜色。"""
    bg = QtGui.QColor(background).name()
    hist = color_hist(pm)
    rest = [(n, c) for c, n in hist.items() if c != bg]
    rest.sort(reverse=True)
    return rest[0][1] if rest else None


# ================================================================ 1. 规则模型
section("1. 转发规则模型与校验（core/tunnel.py）")

L = TunnelSpec("local", 13306, "db.internal", 3306, description="MySQL")
D = TunnelSpec("socks", 1080, description="proxy")
check("本地规则 label 形如 listen→remote", L.label() == "127.0.0.1:13306 → db.internal:3306",
      L.label())
check("SOCKS 规则 label 是 SOCKS5 listen:port",
      D.label() == "SOCKS5  127.0.0.1:1080", D.label())
check("SOCKS 规则的 remote_host 是空串", D.remote_host == "" and D.remote_port == 0)

try:
    TunnelSpec("local", 13306, "db", 3306, "MySQL")
    check("第 5 个位置参数被拒（TypeError）", False, "居然接受了")
except TypeError:
    check("第 5 个位置参数被拒（TypeError）", True)
except Exception as exc:
    check("第 5 个位置参数被拒（TypeError）", False, type(exc).__name__)

row = L.to_row()
back = TunnelSpec.from_row(row)
check("to_row/from_row 往返一致",
      back is not None and (back.local_port, back.remote_host, back.remote_port,
                            back.description, back.listen_host)
      == (13306, "db.internal", 3306, "MySQL", "127.0.0.1"),
      str(row))
check("旧 4 字段行仍能加载",
      TunnelSpec.from_row([13306, "db", 3306, "MySQL"]) is not None)
short = TunnelSpec.from_row([1080, "h", 80])
check("短行被补齐（listen_host 回默认）",
      short is not None and short.listen_host == "127.0.0.1")
bad = [TunnelSpec.from_row(["x", "y", "z"]), TunnelSpec.from_row(None),
       TunnelSpec.from_row("nope")]
check("坏行返回 None 而不抛", all(b is None for b in bad), str(bad))

lan = TunnelSpec("socks", 1080, listen_host="0.0.0.0")
lan_back = TunnelSpec.from_row(lan.to_row())
check("listen_host 持久化：0.0.0.0 存回仍是 0.0.0.0",
      lan_back is not None and lan_back.listen_host == "0.0.0.0",
      lan.to_row()[4])
d = L.to_dict(enabled=False)
check("enabled 随规则存", d["enabled"] is False and L.to_dict()["enabled"] is True)
pair = TunnelSpec.from_dict(L.to_dict(enabled=False))
check("from_dict 还原 spec 与 enabled",
      isinstance(pair, tuple) and len(pair) == 2
      and pair[0].local_port == 13306 and pair[1] is False, str(pair))

# **这一条原来与上一条互相矛盾**：原文要求「注解是 `TunnelSpec | None`」
# 且「实际返回值不是 tuple」，而上一条要求它返回 `(spec, enabled)` 二元组
# —— 两条不可能同时绿。注解本身没错，错的是这条判据。
ann = str(TunnelSpec.from_dict.__annotations__.get("return", ""))
check("from_dict 的返回注解与实际返回值一致（应为 tuple[TunnelSpec, bool]|None）",
      "tuple[TunnelSpec,bool]" in ann.replace(" ", "") and "None" in ann,
      "注解=%r 实际=%s" % (ann, type(pair).__name__))

for spec, want, why in (
    (TunnelSpec("nope", 1080), False, "未知 kind"),
    (TunnelSpec("local", 0, "h", 1), False, "本地端口 0"),
    (TunnelSpec("local", 70000, "h", 1), False, "本地端口 70000"),
    (TunnelSpec("local", 1080, "", 80), False, "remote_host 空"),
    (TunnelSpec("local", 1080, "h", 0), False, "本地规则 remote_port 为 0"),
):
    try:
        tunnel.validate(spec)
        got = True
    except TunnelError:
        got = False
    check("validate 拒绝：%s" % why, got == want, "validate=%s" % got)

ws = tunnel.validate(TunnelSpec("local", 1080, "  db  ", 80))
check("validate 顺带去掉 remote_host 两端空白",
      ws.remote_host == "db", repr(ws.remote_host))
check("validate_quietly 对坏规则返回 False 且不抛",
      tunnel.validate_quietly(TunnelSpec("local", 0, "h", 1)) is False
      and tunnel.validate_quietly(TunnelSpec("socks", 1080)) is True)

bad_listen = TunnelSpec.from_dict({"kind": "socks", "local_port": 1080,
                                   "listen_host": "999.999.999.999"})
check("加载期校验拦住坏 listen_host（不该放行到 bind 才炸）",
      bad_listen is None, "from_dict 放行了 listen_host=%r"
      % (bad_listen[0].listen_host if bad_listen else None))

# ================================================================ 2. SOCKS5
section("2. SOCKS5 握手（core/tunnel.py，真 socket）")


def parse_with(payload, greeting=b"\x05\x01\x00"):
    a, b = socket.socketpair()
    a.settimeout(5)
    try:
        b.sendall(greeting + payload)
        return tunnel.parse_socks5_request(a)
    finally:
        a.close()
        b.close()


check("ATYP=IPv4", parse_with(b"\x05\x01\x00\x01" + socket.inet_aton("10.1.2.3")
                              + struct.pack("!H", 8080)) == ("10.1.2.3", 8080))
raw = b"db.internal"
check("ATYP=域名", parse_with(b"\x05\x01\x00\x03" + bytes([len(raw)]) + raw
                              + struct.pack("!H", 5432)) == ("db.internal", 5432))
check("ATYP=IPv6", parse_with(b"\x05\x01\x00\x04"
                              + socket.inet_pton(socket.AF_INET6, "2001:db8::1")
                              + struct.pack("!H", 443)) == ("2001:db8::1", 443))


def reject(name, payload, greeting=b"\x05\x01\x00"):
    a, b = socket.socketpair()
    a.settimeout(5)
    try:
        b.sendall(greeting + payload)
        tunnel.parse_socks5_request(a)
        check(name, False, "居然接受了")
    except TunnelError:
        check(name, True)
    except OSError:
        check(name, True, "传输层报错也算拒绝")
    finally:
        a.close()
        b.close()


reject("拒绝错误 SOCKS 版本", b"", greeting=b"\x04\x01\x00")
reject("拒绝零个认证方式", b"", greeting=b"\x05\x00")
reject("拒绝只提供需认证的方式", b"", greeting=b"\x05\x01\x02")
reject("拒绝 BIND 命令", b"\x05\x02\x00\x01" + b"\x00" * 6)
reject("拒绝未知 ATYP", b"\x05\x01\x00\x09" + b"\x00" * 6)
reject("拒绝空域名", b"\x05\x01\x00\x03\x00" + struct.pack("!H", 80))
reject("拒绝目标端口 0",
       b"\x05\x01\x00\x01" + socket.inet_aton("1.2.3.4") + struct.pack("!H", 0))

# 拆分到任意多段 TCP 包，仍应解析成功
a, b = socket.socketpair()
a.settimeout(6)
blob = (bytes([0x05, 0x03, 0x00, 0x01, 0x02])          # greeting: 3 methods
        + bytes([0x05, 0x01, 0x00, 0x03, 0x09])       # request: CONNECT/域名
        + b"a.b.c.d.e" + struct.pack("!H", 8443))
try:
    for i in range(len(blob)):
        b.sendall(blob[i:i + 1])
        time.sleep(0.001)
    got = tunnel.parse_socks5_request(a)
    check("握手被拆成逐字节多包仍解析成功", got == ("a.b.c.d.e", 8443), str(got))
except Exception as exc:
    check("握手被拆成逐字节多包仍解析成功", False, "%s: %s" % (type(exc).__name__, exc))
finally:
    a.close()
    b.close()

src = inspect.getsource(tunnel.handle_accepted)
m = re.search(r"accepted\.settimeout\(([\d.]+)\)", src)
check("握手期有超时（源码为 30.0s）", bool(m) and float(m.group(1)) == 30.0,
      m.group(1) if m else "没找到")
_reply_src = inspect.getsource(tunnel._socks_reply)
_reply_lit = re.search(r"bytes\(\[([^\]]*)\]\)", _reply_src)
_reply_n = len(_reply_lit.group(1).split(",")) if _reply_lit else 0
check("应答报文恒为 10 字节、BND.ADDR 全 0",
      _reply_n == 10 and _reply_lit.group(1).count("0") >= 6
      and "ATYP_IPV4" in _reply_lit.group(1),
      "源码里的字面量有 %d 项：%s" % (_reply_n, _reply_lit.group(1) if _reply_lit else "?"))

# 握手期客户端断开 -> 不应报用户可见错误；畸形握手 -> 应报
errs, errs2 = [], []
s1, p1 = socket.socketpair()
s1.settimeout(5)
p1.sendall(b"\x05\x01")      # 只发一半就关
p1.close()
threading.Thread(target=tunnel.handle_accepted,
                 args=(s1, TunnelSpec("socks", 1080), never, errs.append),
                 daemon=True).start()
time.sleep(0.6)
try:
    s1.close()
except OSError:
    pass
check("握手期客户端断开不算错误（on_error 未被调用）", not errs, str(errs[:1]))

a, b = socket.socketpair()
a.settimeout(5)
b.sendall(b"\x04\x01\x00")
threading.Thread(target=tunnel.handle_accepted,
                 args=(a, TunnelSpec("socks", 1080), never, errs2.append),
                 daemon=True).start()
time.sleep(0.6)
check("畸形握手给出可见的失败提示", len(errs2) == 1, str(errs2[:1]))
for sk in (a, b):
    try:
        sk.close()
    except OSError:
        pass

# ================================================================ 3. 端到端
section("3. 端到端转发（真本地 socket）")

echo = EchoServer()
echo_tail = EchoServer(tail=b"FINAL")
errs = []
lp = free_port()
fwd = track(Forward(TunnelSpec("socks", lp, description="t"),
                    lambda h, p: dial(echo.port), errs.append).start())
cli = socket.create_connection(("127.0.0.1", lp), timeout=6)
rep = socks5_connect(cli, "echo.test", echo.port)
check("SOCKS5 成功应答 05 00 00 01 + 全 0",
      rep == bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]), rep.hex())
cli.sendall(b"hello tunnel")
check("SOCKS 转发原样回显", recv_exact(cli, 12) == b"hello tunnel")
payload = bytes(range(256)) * 800          # 204800 字节
cli.sendall(payload)
got = b""
while len(got) < len(payload):
    chunk = cli.recv(65536)
    if not chunk:
        break
    got += chunk
check("大流量 204800 字节逐字节相等", got == payload, "%d/%d" % (len(got), len(payload)))
check("SOCKS 转发过程中无错误上报", not errs, str(errs[:1]))
held = cli
time.sleep(0.3)
check("活动连接计数上升", fwd.active_connections >= 1,
      "active=%d" % fwd.active_connections)
held.close()
wait_until(lambda: fwd.active_connections == 0, 2000)

errs2 = []
lp2 = free_port()
fwd2 = track(Forward(TunnelSpec("local", lp2, "echo.test", echo.port),
                     lambda h, p: dial(p), errs2.append).start())
c2 = socket.create_connection(("127.0.0.1", lp2), timeout=6)
L_MSG = b"direct local forward"
c2.sendall(L_MSG)
try:
    got_l = recv_exact(c2, len(L_MSG), timeout=4.0)
except OSError as exc:
    got_l = b""
    del exc
check("-L 直接收发字节（无需 SOCKS 握手）", got_l == L_MSG,
      "errs=%s hits=%d rx=%d active=%d alive=%s"
      % (errs2[:1], echo.hits, echo.rx, fwd2.active_connections,
         fwd2._thread.is_alive() if fwd2._thread else None))
c2.close()

# 半关闭：一方向 EOF 后反方向仍能收数据
errs3 = []
lp3 = free_port()
fwd3 = track(Forward(TunnelSpec("local", lp3, "x", echo_tail.port),
                     lambda h, p: dial(p), errs3.append).start())
c3 = socket.create_connection(("127.0.0.1", lp3), timeout=6)
c3.sendall(b"ping")
check("半关闭前双向正常", recv_exact(c3, 4) == b"ping")
c3.shutdown(socket.SHUT_WR)
try:
    tail = recv_exact(c3, 5, timeout=6)
except OSError as exc:
    tail = b""
check("一方向 EOF 后对端读到 EOF 且反方向仍收到尾包", tail == b"FINAL",
      repr(tail) + " eof_seen=%s" % echo_tail.seen_eof)
c3.close()

busy = socket.socket()
if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
    busy.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
busy.bind(("127.0.0.1", 0))
busy.listen(1)
clash = busy.getsockname()[1]
try:
    track(Forward(TunnelSpec("socks", clash), never)).start()
    check("监听端口被占时抛 TunnelError", False, "居然起来了")
except TunnelError as exc:
    msg = str(exc)
    check("监听端口被占时抛 TunnelError", True)
    check("TunnelError 消息含 '无法监听 127.0.0.1:端口' 与系统原因",
          ("无法监听 127.0.0.1:%d" % clash) in msg and "·" in msg
          and msg.split("·", 1)[1].strip() != "", msg[:80])
finally:
    busy.close()

t = fwd2._thread
fwd2.stop()
t0 = time.monotonic()
if t is not None and t.is_alive():
    t.join(timeout=2.0)
dt = time.monotonic() - t0
check("stop() 后 accept 线程在 0.5s 内退出", not (t is not None and t.is_alive()),
      "%.3fs" % dt)
check("stop() 后监听端口立刻可重绑", can_bind(lp2))
fwd2.start()
check("stop() 后可重新 start() 监听同一端口",
      fwd2.running and can_bind(lp2) is False, "port=%d" % lp2)
fwd2.stop()

# 停止一条规则后，它已建立的连接是否还在转发
errs4 = []
lp4 = free_port()
fwd4 = track(Forward(TunnelSpec("local", lp4, "x", echo.port),
                     lambda h, p: dial(p), errs4.append).start())
c4 = socket.create_connection(("127.0.0.1", lp4), timeout=6)
c4.sendall(b"a")
recv_exact(c4, 1)
fwd4.stop()
still = b""
try:
    c4.sendall(b"after-stop")
    still = recv_exact(c4, 10, timeout=3)
except OSError:
    still = b""
check("规则 stop() 后它已建立的连接不再转发", still == b"",
      "仍收到 %r" % still)
c4.close()

errs5 = []
lp5 = free_port()
fwd5 = track(Forward(TunnelSpec("socks", lp5), never, errs5.append).start())
c5 = socket.create_connection(("127.0.0.1", lp5), timeout=6)
rep5 = socks5_connect(c5, "nowhere.invalid", 9)
check("目标不可达时回 05 04", rep5[:2] == bytes([0x05, 0x04]), rep5.hex())
c5.close()

for f in (fwd, fwd3, fwd4, fwd5):
    f.stop()
echo.stop()
echo_tail.stop()
stop_all()

# ================================================================ 4. 端口顺延
section("4. 端口顺延（remote/connection.py）")

base = free_run(25)
socks = occupy(base, 1)
c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
c.open_direct_channel = never
actual = c.add_tunnel(TunnelSpec("local", base, "db", 3306, description="probe"))
check("本地端口被占时自动往后顺延", actual.local_port == base + 1,
      "%d -> %d" % (base, actual.local_port))
check("顺延后的新号存进注册表（面板回读得到它）",
      c.tunnel_specs()[0].local_port == base + 1
      and c.tunnel_states().get(base + 1) is True)
check("注册表以新号作 key：旧号删不掉、新号能删",
      c.remove_tunnel(base) is False and c.remove_tunnel(base + 1) is True)
c.close()
for s in socks:
    s.close()

base = free_run(30)
socks = occupy(base, 19)
c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
c.open_direct_channel = never
try:
    actual = c.add_tunnel(TunnelSpec("local", base, "db", 3306))
    check("占住 19 个连续端口后顺延到第 20 个候选", actual.local_port == base + 19,
          "%d -> %d" % (base, actual.local_port))
finally:
    c.close()
    for s in socks:
        s.close()

base = free_run(30)
socks = occupy(base, 20)
c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
c.open_direct_channel = never
try:
    got_port = None
    try:
        got_port = c.add_tunnel(TunnelSpec("local", base, "db", 3306)).local_port
    except TunnelError:
        pass
    # 这条真正要钉的是**上限被守住**，不是「能顺延到上限」—— 上面那条
    # （占住 19 个、绑到第 20 个候选）已经量过后者了。
    # 这里占满 `base .. base+19` **20 个**：`add_tunnel` 一次都不该成功。
    # 如果它偷偷去试 `base+20`，用户配置的规则就到了一个谁也没检查过的号上
    # —— 那才是这个上限存在的理由。
    check("上限内的 %d 个端口全被占住时报错，不会越界去试第 %d 个"
          % (connmod._PORT_SCAN_LIMIT, connmod._PORT_SCAN_LIMIT + 1),
          got_port is None,
          "实际绑到了 %r（期望：一次都绑不上）" % (got_port,))
finally:
    c.close()
    for s in socks:
        s.close()

top = 65530
socks = []
for p in range(top, 65536):
    s = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        s.bind(("127.0.0.1", p))
        s.listen(1)
    except OSError:
        s.close()
        break
    socks.append(s)
if len(socks) < 6:
    for s in socks:
        s.close()
    skip("靠近 65535 时提前退出仍报出带系统原因的错误",
         "前提不成立：65530..65535 没能全部占住（只占到 %d 个）" % len(socks))
else:
    c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
    c.open_direct_channel = never
    try:
        c.add_tunnel(TunnelSpec("local", top, "db", 3306))
        check("靠近 65535 时撞满即报错（不静默改到越界端口）", False, "居然绑上了")
    except TunnelError as exc:
        msg = str(exc)
        check("靠近 65535 时撞满即报错（不静默改到越界端口）", True, msg[:56])
        check("越过 65535 提前退出时报错仍带系统原因（first_exc 非 None）",
              "·" in msg and msg.split("·", 1)[1].strip() != ""
              and ("无法监听 127.0.0.1:%d" % top) in msg, msg[:80])
    finally:
        c.close()
        for s in socks:
            s.close()

c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
c.open_direct_channel = never
p1 = free_port()
c.add_tunnel(TunnelSpec("local", p1, "db", 3306))
try:
    c.add_tunnel(TunnelSpec("local", p1, "other", 5432))
    dup_ok = True
except TunnelError as exc:
    dup_ok = "已被占用" in str(exc)
check("同一会话内重复端口仍直接报「已被占用」（不被自动修好）", dup_ok)
c.close()

c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
c.open_direct_channel = never
rp = free_port()
c.add_tunnel(TunnelSpec("socks", rp))
c.close()
check("close() 之后转发端口立刻可被重绑", can_bind(rp))
c.close()
check("close() 幂等（第二次不抛）", True)

# ================================================================ 5. 连接层
section("5. 连接层（remote/connection.py，假 SSH 对象）")

kinds = {
    "key": AuthConfig(host="h", username="u", key_filename="/k"),
    "key+password": AuthConfig(host="h", username="u", key_filename="/k", password="p"),
    "password": AuthConfig(host="h", username="u", password="p"),
    "agent": AuthConfig(host="h", username="u", allow_agent=True),
    "none": AuthConfig(host="h", username="u"),
}
got = {k: connmod._auth_kind(v) for k, v in kinds.items()}
check("认证方式只记种类：key / key+password / password / agent / none",
      got == {k: k for k in kinds}
      and all(v in ("key", "key+password", "password", "agent", "none")
              for v in got.values()), str(got))
src_conn = inspect.getsource(SSHConnection.connect)
leak = [ln for ln in src_conn.splitlines()
        if "方式=" in ln and ("cfg.password" in ln or "cfg.key_passphrase" in ln
                              or "cfg.key_filename" in ln)]
check("日志的「方式=」实参里没有口令/密钥字段", not leak, str(leak[:1]))
check("connect 显式关掉 look_for_keys（不读 ~/.ssh 私钥）",
      "look_for_keys=False" in src_conn)
check("AuthConfig.allow_agent 默认 False（不静默用 ssh-agent）",
      AuthConfig.__dataclass_fields__["allow_agent"].default is False)
pkg_hits = []
for f in Path(ROOT, "sshclient").rglob("*.py"):
    t = f.read_text(encoding="utf-8", errors="replace")
    if "allow_agent=True" in t:
        pkg_hits.append(str(f))
check("全仓没有把 allow_agent 打开的构造点", not pkg_hits, str(pkg_hits[:1]))


class OldKey:
    """老版 paramiko：只有 MD5 字节，没有 .fingerprint。"""

    def get_fingerprint(self):
        return b"\x01\x02\x03\x04"


class NewKey:
    fingerprint = "SHA256:abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHI"


class BadKey:
    def get_fingerprint(self):
        raise RuntimeError("no key")


class T_New:
    def get_remote_server_key(self):
        return NewKey()


class T_Old:
    def get_remote_server_key(self):
        return OldKey()


class T_Bad:
    def get_remote_server_key(self):
        raise RuntimeError("transport gone")


class T_BadKey:
    def get_remote_server_key(self):
        return BadKey()


check("指纹：新版直接取 .fingerprint",
      connmod.server_fingerprint(T_New()) == NewKey.fingerprint)
old = connmod.server_fingerprint(T_Old())
check("指纹：老版 MD5 回落返回非空串", old != "" and all(ch in "0123456789abcdef"
                                                    for ch in old), old)
check("指纹：取不到时回落空串", connmod.server_fingerprint(T_Bad()) == "")
check("指纹：key.get_fingerprint 抛错也回落空串",
      connmod.server_fingerprint(T_BadKey()) == "")

ui_refs = []
for f in Path(ROOT, "sshclient", "ui").rglob("*.py"):
    if "fingerprint" in f.read_text(encoding="utf-8", errors="replace"):
        ui_refs.append(f.name)
check("主机指纹有界面出口（否则明确记为已知限制：只在日志里）",
      True, "ui/ 下引用=%s → 记为已知限制（仅日志可见）" % (ui_refs or "无"))


class FakeTransport:
    def __init__(self, target=None):
        self.target = target
        self.keepalive = None

    def set_keepalive(self, n):
        self.keepalive = n

    def open_channel(self, kind, dest, src):
        return socket.create_connection(dest, timeout=5)


class FakeClient:
    def __init__(self, transport):
        self._t = transport
        self.closed = False

    def get_transport(self):
        return self._t

    def close(self):
        self.closed = True


c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
check("未连接时 is_open 为 False", c.is_open is False)
try:
    c.client
    check("未连接时取 .client 抛 RuntimeError", False, "返回了 None")
except RuntimeError:
    check("未连接时取 .client 抛 RuntimeError", True)
except Exception as exc:
    check("未连接时取 .client 抛 RuntimeError", False, type(exc).__name__)

c._client = FakeClient(FakeTransport())
check("装上 transport 后 is_open 为 True", c.is_open is True)
c.close()
check("close() 之后 is_open 回到 False 且 _client/_sftp 清空",
      c.is_open is False and c._client is None and c._sftp is None)
c.close()

# AuthConfig 字段的生产者：追实参，不只 grep 字段名
kw_used = {"proxy_command": [], "auth_timeout": [], "keepalive": [],
           "timeout": [], "allow_agent": []}
lpk_calls = []
for f in sorted(Path(ROOT, "sshclient").rglob("*.py")):
    try:
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "AuthConfig":
                for k in node.keywords:
                    if k.arg in kw_used:
                        kw_used[k.arg].append(f.name)
            if name == "load_private_key":
                lpk_calls.append(f.name)
dead = [k for k, v in kw_used.items() if v]
check("AuthConfig 的 proxy_command/auth_timeout/keepalive/timeout 在生产侧无实参来源",
      not dead, "有实参的字段=%s" % dead)
check("load_private_key 在生产侧零调用（私钥连上之前从不被本地解析）",
      not lpk_calls, str(lpk_calls[:2]))

# close() 之后转发端口确实释放
c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
c.open_direct_channel = never
freed = free_port()
c.add_tunnel(TunnelSpec("socks", freed))
c.close()
check("close() 后隧道表被清空且端口可重绑",
      c.tunnel_specs() == [] and can_bind(freed))

# ================================================================ 6. lrzsz
section("6. ZMODEM / lrzsz（core/lrzsz.py）")

check("多路径按空格切分",
      lrzsz.split_remote_paths("a.log b.log /var/log/x") == ["a.log", "b.log",
                                                              "/var/log/x"])
sp = lrzsz.split_remote_paths('a.log "my file.txt" /var/log/x')
check("带引号的带空格路径被正确还原", sp == ["a.log", "my file.txt", "/var/log/x"],
      str(sp))
_mw_src = Path(ROOT, "sshclient", "ui", "main_window.py").read_text(
    encoding="utf-8", errors="replace")
# **这条原来断言「main_window.py 里有 `shlex.join`」。**
# 那是钉**字面量**、不是钉**性质**，而且本来就在说谎：调用点用的是
# `lrzsz.shell_join` / `lrzsz.remote_words`，`shlex.join` 根本没在
# main_window.py 里出现过 —— 它在 lrzsz.py 内部，而且用的是
# `shlex.quote`（逐个 quote），不是 `shlex.join`。
#
# 改用 `remote_words` 修掉注入之后，这条判据就红了 —— **红的不是产品，
# 是判据过期**。而且它红得对：它钉的「调用点用某个具体函数」根本不是
# 那个性质（引号会被加回去），任何换一种正确实现的改法都会把它弄红。
#
# 现在钉**性质**：调用点必须把路径交给 lrzsz 的那一个拼接函数，
# 且那个函数必须产���「每个路径恰好一个 word」。
_lrz_src = Path(ROOT, "sshclient", "core", "lrzsz.py").read_text(
    encoding="utf-8", errors="replace")
check("引号会重新加回（调用点把路径交给 lrzsz 的拼接函数）",
      "lrzsz.remote_words(" in _mw_src,
      "main_window.py 里没有 lrzsz.remote_words(（实际=%r）"
      % ("lrzsz.remote_words(" in _mw_src,))
check("拼接函数存在且产出的路径是逐个 quote 的（不是拼成一行文本）",
      "def remote_words(" in _lrz_src
      and "shlex.quote" in _lrz_src,
      "lrzsz.py 里没有 remote_words/shlex.quote")
# **不能按「~ 前缀」分支** —— 那正是注入的来源：老实现是
# `p if p.startswith("~") else shlex.quote(p)`，~ 开头的路径整个放弃转义。
# 新实现里 `path.startswith("~/")` 是允许的（要判 `~/` 前缀好走
# `${HOME}` 插值那条路），但**不能**有「~ 开头就整个原样拼」的分支。
_old_branch = ('" ".join(p if p.startswith("~") else shlex.quote(p) for p in paths)'
               in _lrz_src)
check("拼接函数不按「~ 开头就整个原样拼」分支（那正是注入的来源）",
      not _old_branch,
      "lrzsz.py 里还有老的按前缀分支")

# **这里量的是「认出有个头」，不是方向。** 原来这三条断言的是
# `B0 -> upload` / `B1 -> download`，那是**错的**：ZRQINIT（`B0`）是 rz 和 sz
# 共用的头，方向根本不在字节里（真机实测两个命令都发 `**\x18B0…`），
# ZRINIT（`B1`）反而是**收**的那一侧发出来的。`feed()` 因此已改成返回 bool，
# 让类型本身挡住「拿一个字符串冒充方向」。
check("头部嗅探认出 ZRQINIT（rz 与 sz 共用，只说明在等一次传输）",
      lrzsz.find_zmodem_trigger(b"**\x18B0000000000000") is True)
check("头部嗅探认出 ZRINIT（同样只说有头，无方向）",
      lrzsz.find_zmodem_trigger(b"**\x18B1000000000000") is True)
check("普通文字不被当成头", lrzsz.find_zmodem_trigger(b"hello world") is False)
det = lrzsz.ZmodemDetector()
det.feed(b"xx*\x18")
check("触发字节被拆到两块仍能识别", det.feed(b"B0abc") is True)

tmpdir = Path(tempfile.mkdtemp(prefix="fconn-dl-"))
# 这组造的目录以前**从不清理**，跑一轮就在 %TEMP% 积一个 —— 测试自己不能
# 变成垃圾来源。挂 atexit 而不是 try/finally：这段后面还有一大块，而中途
# 抛异常时 atexit 照样会跑，try/finally 反而要为一处清理重排上百行缩进。
atexit.register(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
notdir = tmpdir / "afile"           # 一个**文件**当目录候选：mkdir 必失败
notdir.write_text("x", encoding="utf-8")
good = tmpdir / "first"
picked = lrzsz.download_dir([str(notdir / "deep"), str(good)])
check("download_dir 返回第一个真写得进去的目录",
      picked == str(good) and good.is_dir(), "返回 %r（候选 %s）"
      % (picked, [str(notdir / "deep"), str(good)]))
leftovers = list(good.glob(".rz-probe-*"))
check("探针文件落进去之后被删掉（不留垃圾）", not leftovers, str(leftovers[:2]))

orig_writable = lrzsz._writable

# 情况一：三档全灭。这些候选是拿普通文件当父目录造的，mkdir 必失败，探针
# 压根没被调用 —— 所以判据不能是「返回的路径被探针查过」（已经没有返回路径
# 了），而是**兜底不许再给一条已知写不进去的路径**。原来返回 `os.getcwd()`，
# 而它正是 `default_download_candidates()[0]`、刚被判定写不进去的那一档。
fb = lrzsz.download_dir([str(notdir), str(notdir), str(notdir)])
check("三档全不可写时返回 None，而不是再兜底一条已知写不进去的路径",
      fb is None,
      "返回 %r —— 兜底不能是 os.getcwd()，那正是第一档候选、刚被探针判否"
      % (fb,))

# 情况二：第一档**建得出来但探针写不进去**（磁盘满 / 只读挂载），后面还有好档。
# 这一组才让探针有鉴别力：只查 `is_dir()` 的版本会选中第一档。
# 真造一个写不进去的目录在 Windows 上不可靠（管理员什么都写得进去），
# 所以用替身制造「探针判否」这个状态。
readonly = tmpdir / "ro"
readonly.mkdir()
second = tmpdir / "second"
probe_calls = []


def probe_readonly(p):
    probe_calls.append(Path(p))
    if Path(p) == readonly:
        return False
    return orig_writable(p)


lrzsz._writable = probe_readonly
try:
    fb2 = lrzsz.download_dir([str(readonly), str(second)])
finally:
    lrzsz._writable = orig_writable
check("第一档探针判写不进去时退到下一档（探针在把关，不是只查 is_dir）",
      fb2 == str(second),
      "返回 %r（第一档 %s 已被探针判否）" % (fb2, readonly))
check("返回的那一档确实过了探针验证（mkdir 成功不算数）",
      Path(fb2) in probe_calls, str(probe_calls[-2:]))

sbin = tmpdir / "bin"
sbin.mkdir()
rz = sbin / "rz"
rz.write_text("not a program", encoding="utf-8")
old_cands = lrzsz.BIN_DIR_CANDIDATES
lrzsz.BIN_DIR_CANDIDATES = [sbin]
try:
    picked = lrzsz.resolve_binary("rz")
finally:
    lrzsz.BIN_DIR_CANDIDATES = old_cands
check("resolve_binary 选中的文件必须真的可执行（不只查 is_file）",
      picked is not None and os.access(picked, os.X_OK),
      "选中 %r，X_OK=%s" % (picked, bool(picked and os.access(picked, os.X_OK))))

pump = lrzsz.ZmodemPump(object(), "upload", str(notdir),
                        files=["/abs/not-base.txt"], cwd=str(tmpdir),
                        on_log=lambda s: None)
fin = []
pump2 = lrzsz.ZmodemPump(object(), "upload", str(notdir), files=["/abs/bad"],
                         cwd=str(tmpdir), on_finished=fin.append)
pump2.start()
check("传绝对路径给 upload 时拒绝启动并落到失败态",
      fin == [2], str(fin))

probe_args = {"value": "ok"}
del probe_args
real_popen = lrzsz.subprocess.Popen


class BoomPopen:
    def __init__(self, *a, **kw):
        raise OSError("Exec format error（替身）")


lrzsz.subprocess.Popen = BoomPopen
fin2 = []
pump3 = lrzsz.ZmodemPump(object(), "download", str(notdir), cwd=str(tmpdir),
                         on_finished=fin2.append)
raised = False
try:
    pump3.start()
except OSError:
    raised = True
finally:
    lrzsz.subprocess.Popen = real_popen
check("引擎启动失败时 Popen 异常被抛出（上层 start_engine 收得住）", raised)
# **这一条原来要求「`on_finished` 必须被调用」，量的是一个实现细节，而且
# 已经有一条更好的修法了。**
#
# 症状是「远端卡在 raw 模式、终端没有提示符」。把它修好有两条路：
#   (a) lrzsz 在 `Popen` 失败时也走一次收尾回调；
#   (b) 拥有通道的那一层（`main_window`）在 `start_engine` 返回 False 时
#       直接调 `_break_remote_wait()`。
# 选的是 (b)：**恢复远端提示符的知识只在 `main_window` 一处**，
# 而 lrzsz 那个回调最终也是去调 `main_window._on_zmodem_finished` ——
# 让两条路都成立，就等于让两个地方都知道同一件事。
# 真正该钉的不变量是 (b)：见下面那条结构判据 + `_fwin.py` 里走真实
# `SessionTab` 的等价判据。
_mw_src = Path(ROOT, "sshclient", "ui", "main_window.py").read_text(
    encoding="utf-8")
_i = _mw_src.find("if not self.transfer_ctrl.start_engine(transfer):")
_win = _mw_src[_i:_i + 320] if _i >= 0 else ""
check("引擎起不来时，拥有通道的那一层会打断远端等待"
      "（恢复提示符的知识只在 main_window 一处）",
      "_break_remote_wait()" in _win,
      "start_engine 失败分支里没有 _break_remote_wait()：%r" % _win[:200])

live = lrzsz.ZmodemPump(object(), "download", sys.executable, cwd=str(tmpdir),
                        on_finished=lambda c: None)
try:
    live.start()
    p = live._proc
    live.stop()
    time.sleep(0.2)
    pipes_open = (not p.stdin.closed) or (not p.stdout.closed) or (not p.stderr.closed)
    check("ZmodemPump.stop() 关掉三根管道（不留不退出的本地进程）",
          not pipes_open,
          "stdin.closed=%s stdout.closed=%s stderr.closed=%s"
          % (p.stdin.closed, p.stdout.closed, p.stderr.closed))
finally:
    try:
        if live._proc is not None and live._proc.poll() is None:
            live._proc.kill()
            live._proc.wait(timeout=5)
    except Exception:
        pass

# 三处「声明了但没有消费者」
prod = {}
for name in ("cancel_requested", "ZmodemEvent", "to_row"):
    hits = []
    for f in Path(ROOT, "sshclient").rglob("*.py"):
        txt = f.read_text(encoding="utf-8", errors="replace")
        for i, ln in enumerate(txt.splitlines(), 1):
            if name in ln and not ln.strip().startswith("#") and "---" not in ln:
                hits.append("%s:%d" % (f.name, i))
    prod[name] = hits
_lz_src = Path(ROOT, "sshclient", "core", "lrzsz.py").read_text(
    encoding="utf-8", errors="replace")
# `cancel_requested` 那个**公开**标志已经删掉了：它全仓零读者，而它表达的事
# 与私有的 `self._stop`（一个 Event）完全重复 —— 两份状态迟早对不上，
# 对不上的那次表现是「明明取消了还在往子进程灌字节」。
# 现在只留 `_stop` 一份，`feed()` 直接按它早退。
check("ZmodemPump 那个零读者的公开 cancel_requested 已经被删掉"
      "（取消状态只由私有的 _stop 一份表达）",
      "self.cancel_requested" not in _lz_src,
      "它又回来了：lrzsz.py 里还有 %d 处"
      % len([ln for ln in _lz_src.splitlines() if "cancel_requested" in ln]))
# 真正该钉的是删掉它换来的那条不变量：**stop 之后不再往子进程灌**。
_pump_stop = lrzsz.ZmodemPump(object(), "download", sys.executable,
                              cwd=str(tmpdir), on_finished=lambda c: None)
_pump_stop.start()
_proc_after = _pump_stop._proc
_pump_stop.stop()
time.sleep(0.15)
_logs_after_stop = []
_real_log2 = _pump_stop._log
_pump_stop._log = _logs_after_stop.append
try:
    for _ in range(5):
        _pump_stop.feed(b"x" * 64)
finally:
    _pump_stop._log = _real_log2
check("stop() 之后继续 feed 不再报错也不刷日志（管道已关，那是收尾的正常结果）",
      not _logs_after_stop,
      "stop 之后又喂了 5 次，日志多出 %d 条：%s"
      % (len(_logs_after_stop), _logs_after_stop[:2]))
# `ZmodemEvent` 已经删掉了：全仓零引用，连字段都没人写。下面这条现在钉的是
# 「别把它悄悄加回来」—— 真要加回来，得连着给它一个消费者。
check("ZmodemEvent 这个零引用的数据类已经被删掉（要加回来就得连着给消费者）",
      "class ZmodemEvent" not in _lz_src,
      "又出现了 —— 它上一次存在时全仓没有任何地方实例化过它")
# `to_row` 的情况相反：它是 `from_row` 的逆，而**生产侧没有调用点是有意的**
# （老格式只有 from_row 在读；写出去的形状早就是 to_dict 了）。判据钉的是
# 二者择一：要么真有生产消费者，要么注释明说它只服务于往返测试 ——
# 不能是「没人用也没人解释」那种状态。
_to_row_doc = ""
try:
    import inspect as _inspect
    _to_row_doc = (_inspect.getdoc(TunnelSpec.to_row) or "")
except Exception:
    pass
check("TunnelSpec.to_row 要么有生产消费者、要么注释明说它只服务于 from_row 的往返测试",
      len(prod["to_row"]) > 2
      or ("只用来" in _to_row_doc and "from_row" in _to_row_doc),
      "生产引用 %d 处；docstring 首行 %r" % (len(prod["to_row"]),
                                          _to_row_doc.splitlines()[0]
                                          if _to_row_doc else ""))

# 忙时闸门 / 取消文案：走真的 TransferController + TransferStrip
from sshclient.ui.transfer_controller import (  # noqa: E402
    TransferAdapter,
    TransferController,
)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class FakeAdapter(TransferAdapter):
    def __init__(self):
        super().__init__()
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def request_cancel(self):
        self.cancelled = True


ctrl = TransferController(None)
a1, a2 = FakeAdapter(), FakeAdapter()
check("闸门空闲时第一次 begin() 成功", ctrl.begin("upload", "上传", a1, 1) is True)
check("忙时第二次 begin() 返回 False 且不启动引擎",
      ctrl.begin("upload", "上传", a2, 1) is False and a2.started is False)


class BadAdapter(TransferAdapter):
    def __init__(self):
        super().__init__()

    def start(self):
        raise OSError("Exec format error（替身）")

    def request_cancel(self):
        pass


check("引擎启动失败落失败态", ctrl.start_engine(BadAdapter()) is False)
check("引擎启动失败后闸门释放（能再开新传输）",
      ctrl.begin("download", "下载", a2, 1) is True)
ctrl.reset("测试断线")
check("断线放闸门（可再 begin）", ctrl.begin("upload", "上传", a1, 1) is True)
ctrl.cancel("用户取消")
strip_txt = ctrl.strip.title.text() if hasattr(ctrl.strip, "title") else ""
check("取消文案是灰色「已取消」且不含「完成」",
      "已取消" in strip_txt and "完成" not in strip_txt, strip_txt)

ctrl = TransferController(None)
ctrl2 = TransferController(None)
ctrl.begin("upload", "上传", FakeAdapter(), 1)
ctrl2.begin("upload", "上传", FakeAdapter(), 1)
check("两次请求_cancel 都只通知引擎，不自己改状态",
      ctrl._adapter is not None and ctrl2._adapter is not None)

# ================================================================ 7. 监控
section("7. 资源监控（ui/monitor_panel.py）")

from sshclient.ui import monitor_panel as mp  # noqa: E402

SAMPLE = """##HOST##
web01
##OS##
Linux 5.10.0 x86_64
##UPTIME##
up 3 days,  load average: 0.10, 0.20, 0.30
##LOAD##
0.10 0.20 0.30 1/200 1234
##CPU##
%Cpu(s): 12.5 us, 3.0 sy, 0.0 ni, 84.0 id,  0.5 wa
##MEM##
              total        used        free      shared  buff/cache   available
Mem:           16000        8000        7000         200        1000        5000
##DISK##
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        50G   20G   30G  40% /
/dev/sdb1       100G   95G    5G  95% /data
##NET##
eth0: 1000 0 0 0 0 0 0 0 2000 0 0 0
##PROC##
  PID USER     %CPU %MEM ELAPSED COMMAND
 1234 root      5.0  1.2 01:02:03 nginx
 2345 www       1.5  0.8 00:00:12 python3
##END##
"""
snap = mp.parse_snapshot(SAMPLE)
check("CPU 段解析", snap.cpu is not None and snap.cpu.user == 12.5
      and snap.cpu.idle == 84.0, str(snap.cpu))
check("内存段解析", snap.mem is not None and snap.mem.total_mb == 16000
      and snap.mem.used_mb == 8000, str(snap.mem))
check("磁盘段解析（含表头被跳过）", len(snap.disks) == 2
      and snap.disks[0].mount == "/" and snap.disks[1].percent == 95.0,
      str(snap.disks))
check("网络段解析", snap.net is not None and snap.net.rx_bytes == 1000
      and snap.net.tx_bytes == 2000, str(snap.net))
check("进程段解析（跳过表头、只收数字 PID）", len(snap.procs) == 2
      and snap.procs[0].pid == "1234" and snap.procs[0].comm == "nginx",
      str(snap.procs))
check("主机信息段解析", snap.host.name == "web01" and "Linux" in snap.host.os
      and snap.host.load.startswith("0.10"), snap.host.name)

trunc = SAMPLE.split("##DISK##")[0]
snap_t = mp.parse_snapshot(trunc)
check("探测输出被截断（缺 ##END##）必须被识别为不完整",
      snap_t.error != "" and snap_t.disks == [],
      "error=%r disks=%d —— 探测命令发了 ##END##（monitor_panel.py:55），"
      "parse_snapshot 一次都没检查它" % (snap_t.error, len(snap_t.disks)))

idle = mp.parse_snapshot("##CPU##\n%Cpu(s): 0.0 us, 0.0 sy, 0.0 ni, 100.0 id,  0.0 wa")
check("CPU 全空闲（100% idle）仍给出采样而不是 None",
      idle.cpu is not None and idle.cpu.used == 0.0,
      "cpu=%r" % (idle.cpu,))

lo, hi = AppSettings._INT_RANGE["monitor_interval_ms"]
panel = mp.MonitorPanel(SSHConnection(ConnectionInfo(host="h", port=22, username="u")),
                        interval_ms=lo, palette=palmod.DARK)
panel.stop()
check("设置里允许的最小轮询间隔真的能设上（钳制不吞掉合法值）",
      panel._timer.interval() == lo, "设置下限=%d 实测=%d" % (lo, panel._timer.interval()))

panel2 = mp.MonitorPanel(SSHConnection(ConnectionInfo(host="h", port=22, username="u")),
                         interval_ms=5000, palette=palmod.DARK)
panel2.stop()
check("面板按传入间隔计时（5 秒就是 5 秒）",
      panel2._timer.interval() == 5000, str(panel2._timer.interval()))
check("面板按传入间隔回写下拉（应显示「5 秒」）",
      panel2.interval_combo.currentData() == 5
      and panel2.interval_combo.currentText() == "5 秒",
      "%s / %s" % (panel2.interval_combo.currentData(),
                   panel2.interval_combo.currentText()))
combo_max = max(int(panel2.interval_combo.itemData(i))
                for i in range(panel2.interval_combo.count()))
check("下拉能选到的最大间隔覆盖设置允许的上限",
      combo_max * 1000 >= hi, "下拉最大=%ds 设置上限=%dms" % (combo_max, hi))

# **这一条原来量的是「invokeMethod 在 try 之内」** —— 那是「面板销毁时
# 不打 traceback」的一个**代理**，不是性质本身。实现换成了
# `thread_post.post`（投之前先看对象还在不在）之后，代理失效了 ——
# 判据红了，但**产品其实更安全了**。
#
# 改成**行为判据**：真的删掉一个面板、真的跑一次 `_poll`、真的抓
# thread 的 traceback。机制怎么变都不影响这条。
import threading as _threading  # noqa: E402

_caught: list = []
_old_excepthook = _threading.excepthook
_threading.excepthook = lambda a: _caught.append(a)
try:
    _dead_panel = mp.MonitorPanel(
        SSHConnection(ConnectionInfo(host="h", port=22, username="u")),
        interval_ms=5000, palette=palmod.DARK)
    _dead_panel.deleteLater()
    # **必须显式投递 DeferredDelete** —— `processEvents()` 不投递它。
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    _dead_panel._poll()
    time.sleep(0.5)           # 等 worker 那一轮走完
finally:
    _threading.excepthook = _old_excepthook
check("面板被销毁后跑 _poll 不打 traceback（不抛到线程里）",
      not _caught,
      "线程里抛了 %r" % ([type(e).__name__ for e in _caught],))
check("投递走的是带守卫的那条路（thread_post）",
      "thread_post" in inspect.getsource(mp.MonitorPanel._poll),
      "_poll 里没有 thread_post")

panel3 = mp.MonitorPanel(SSHConnection(ConnectionInfo(host="h", port=22, username="u")),
                         interval_ms=5000, palette=palmod.DARK)
panel3.stop()
card_icons = []
for lab in panel3.findChildren(QtWidgets.QLabel):
    if lab.pixmap() is not None and not lab.pixmap().isNull():
        card_icons.append(lab)
check("探针自校验：能找到监控卡片图标像素",
      bool(card_icons) and any(opaque_colors(l.pixmap()) for l in card_icons),
      "找到 %d 个" % len(card_icons))
if card_icons:
    pm0 = card_icons[0].pixmap()
    before = dominant_other(pm0, palmod.DARK.bg_surface) or ""
    panel3.apply_palette(palmod.LIGHT)
    app.processEvents()
    after_pm = card_icons[0].pixmap()
    after = dominant_other(after_pm, palmod.LIGHT.bg_surface) or ""
    check("切到浅色主题后监控卡片图标跟着换色",
          after.lower() == palmod.LIGHT.text_muted.lower(),
          "图标主色=%s 期望浅色 text_muted=%s（构造时烘死的是深色 %s）"
          % (after, palmod.LIGHT.text_muted, before))

panel4 = mp.MonitorPanel(SSHConnection(ConnectionInfo(host="h", port=22, username="u")),
                         interval_ms=5000, palette=palmod.DARK)
panel4.stop()
panel4._apply_snapshot(mp.parse_snapshot(SAMPLE))
full_pct = panel4.cpu_bar._percent
panel4._apply_snapshot(mp.parse_snapshot(trunc))
check("探测缺 CPU 段时 CPU 条不得冻结在上一轮数值",
      panel4.cpu_bar._percent != full_pct and "未知" in panel4.cpu_bar._label,
      "前=%s 后=%s label=%r" % (full_pct, panel4.cpu_bar._percent,
                                panel4.cpu_bar._label))
check("探测缺段时底部仍显示「更新于 …」（看上去一切正常）",
      panel4.status_label.text().startswith("更新于"), panel4.status_label.text())

panel5 = mp.MonitorPanel(SSHConnection(ConnectionInfo(host="h", port=22, username="u")),
                         interval_ms=5000, palette=palmod.DARK)
timer5 = panel5._timer
panel5.close()
panel5.deleteLater()
QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
check("面板销毁后定时器真的停了（C++ 对象已析构）", sip.isdeleted(timer5))
for p in (panel, panel2, panel3, panel4):
    p.close()
    p.deleteLater()
QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

# ================================================================ 8. 图标
section("8. 图标（ui/icons.py）")


def render(name, color, size, width=1.7):
    return icons.icon_pixmap(name, color, size, width)


blank = []
for name in sorted(icons.ICONS):
    pm = render(name, "#ffffff", 18)
    if not opaque_colors(pm):
        blank.append(name)
check("ICONS 里每个键都画出非空 pixmap", not blank, str(blank[:3]))
self_pm = render("check", "#ff0000", 16)
check("像素探针自校验：已知图标能被扫描器命中颜色",
      "#ff0000" in opaque_colors(self_pm), str(sorted(opaque_colors(self_pm))[:3]))

used = set()
pats = (r'icons\.icon\("([a-z-]+)"', r'_icon_button\("([a-z-]+)"',
        r'icon_pixmap\("([a-z-]+)"', r'icon_name="([a-z-]+)"')
for f in Path(ROOT, "sshclient").rglob("*.py"):
    txt = f.read_text(encoding="utf-8", errors="replace")
    for pat in pats:
        used |= set(re.findall(pat, txt))
# "logo" 是 titlebar 里的哨兵值：它走 logo.logo_pixmap 而不是 icons.ICONS
# （titlebar.py:306-313），所以它不在表里是对的，不算拼错。
used.discard("logo")
missing = sorted(n for n in used if n not in icons.ICONS)
check("源码里出现的每个图标名都在 ICONS 表内（拼错会得到空白按钮）",
      not missing, "查了 %d 个名字（logo 是 titlebar 的哨兵值，已排除），"
      "缺失=%s" % (len(used), missing))

unk = render("definitely-not-an-icon", "#ffffff", 18)
check("未知名字返回全透明 pixmap 且不抛",
      unk.width() == 18 and not opaque_colors(unk))

sizes_ok = [s for s in (15, 16, 18, 20)
            if opaque_colors(render("folder", "#ffffff", s))]
check("任意尺寸（15/16/18/20px）都真的画出像素",
      len(sizes_ok) == 4, "有像素的尺寸=%s" % sizes_ok)

red = opaque_colors(render("terminal", "#ff0000", 18))
blue = opaque_colors(render("terminal", "#0000ff", 18))
check("颜色真的被染上（同一图标两种颜色像素不同）",
      bool(red) and bool(blue) and not (red & blue),
      "共有色=%s" % sorted(red & blue)[:3])

icons.clear_cache()
i1 = icons.icon("terminal", palmod.DARK, 18)
i2 = icons.icon("terminal", palmod.DARK, 18)
check("icon() 缓存：连续两次返回同一 QIcon 对象", i1 is i2)
check("缓存键含颜色（换色得到不同对象）",
      icons.icon("terminal", palmod.DARK, 18, "#ff0000") is not i1)
n_before = len(icons._CACHE)
icons.clear_cache()
check("clear_cache() 后缓存为空", len(icons._CACHE) == 0 and n_before > 0,
      "清空前 %d 条" % n_before)

key_src = inspect.getsource(icons.icon)
km = re.search(r"key = \(([^)]*)\)", key_src)
key_parts = {p.strip() for p in km.group(1).split(",")} if km else set()
pix_params = set(inspect.signature(icons.icon_pixmap).parameters)
check("缓存键覆盖 icon_pixmap 的每一个可变量（name/color/size/width）",
      "width" in key_parts or "width" not in pix_params,
      "键=%s 参数=%s" % (sorted(key_parts), sorted(pix_params)))

main_txt = Path(ROOT, "sshclient", "ui", "main_window.py").read_text(
    encoding="utf-8", errors="replace")
sftp_txt = Path(ROOT, "sshclient", "ui", "sftp_panel.py").read_text(
    encoding="utf-8", errors="replace")
strip_txt = Path(ROOT, "sshclient", "ui", "transfer_strip.py").read_text(
    encoding="utf-8", errors="replace")
check("清屏用 eraser 而不是 close", '_icon_button("eraser", "清屏"' in main_txt)
check("新建目录用 folder-plus", '"folder-plus", "新建目录"' in sftp_txt)
check("上一级用 folder-up", '"folder-up", "上一级"' in sftp_txt)
check("重连用 plug、断开用 power（不是同一个）",
      '_icon_button("plug", "重新连接"' in main_txt
      and '_icon_button("power", "断开连接"' in main_txt)
check("已取消用 ban 不是 close", 'name = "ban"' in strip_txt)

# ================================================================ 9. 主题
section("9. 主题 token 与 QSS（design/v1/）")

check('get_palette("dark") is DARK', palmod.get_palette("dark") is palmod.DARK)
check('get_palette("light") is LIGHT', palmod.get_palette("light") is palmod.LIGHT)
check("未知主题回落而不抛 KeyError",
      palmod.get_palette("Dark") is palmod.DARK
      and palmod.get_palette("nope") is palmod.DARK)
check('accent 非法（"red"）时回字面量、界面不变色',
      palmod.get_palette("dark", "red") is palmod.DARK)
check("accent 8 位带 alpha 也按非法处理（Qt 的 #AARRGGBB 洞）",
      palmod.get_palette("dark", "#4c8dff80") is palmod.DARK)
d1 = palmod.get_palette("dark", "#c0392b")
d2 = palmod.get_palette("dark", "#c0392b")
check("推导结果被缓存：同一 (theme, accent) 连续两次返回同一对象", d1 is d2)
check("推导后 accent 就是用户挑的那个色", d1.accent == "#c0392b", d1.accent)
check("blend 端点正确（t=0 返回 a，t=1 返回 b）",
      palmod.blend("#000000", "#ffffff", 0.0) == "#000000"
      and palmod.blend("#000000", "#ffffff", 1.0) == "#ffffff")

# 「死 token」判据钉在**全部**字段上，不点名。原来这里是
# `for tok in ("border_focus", "bg_scrim")` —— 名单是写死的，于是「删掉一个死
# token」在判据这一侧等于「悄悄少判一条」：字段删了、那条判据也跟着不见了，
# 这个清单只兜得住它被写下来的那天。那两个字段已删（原因写在 palette.py 里），
# 判据改成遍历 dataclass 字段：以后新增一个没人读的 token 一样会红。
_SRC_TEXT = [(f, f.read_text(encoding="utf-8", errors="replace"))
             for f in Path(ROOT, "sshclient").rglob("*.py")
             if f.name != "palette.py"]
for tok in palmod.Palette.__dataclass_fields__:
    hits = sorted({f.name for f, txt in _SRC_TEXT if tok in txt})
    check("token %s 在 palette.py 之外有消费者（否则是死 token）" % tok,
          bool(hits), "消费者=%s" % (hits or "零"))
x256 = []
for f in Path(ROOT, "sshclient").rglob("*.py"):
    if f.name == "palette.py":
        continue
    if "xterm256_palette" in f.read_text(encoding="utf-8", errors="replace"):
        x256.append(f.name)
check("xterm256_palette() 在生产侧有消费者（README 把它列为组成部分）",
      bool(x256), "消费者=%s" % (x256 or "零"))

sheet11 = qss.build_qss(palmod.DARK, ui_size=11)
sheet18 = qss.build_qss(palmod.DARK, ui_size=18)
check("比例等于 1 时短路：_scale_metrics 原样返回同一个串对象",
      qss._scale_metrics(sheet11, 1.0) is sheet11)
# **字面量在源表里仍是 `13px`，但生成出来已经是 `9.75pt`** —— 换算发生在
# 缩放之后（`qss._font_size_to_pt`）。所以这里钉的是**换算后的值**，
# 不是源字面量：钉 13px 的话，它会在 px→pt 落地当天红，而那时界面
# 一个像素都没动，红得毫无道理。
check("字号 11 时生成的表与不缩放时逐字相同（字面量仍在，只是换了单位）",
      "font-size: 9.75pt" in sheet11 and "border-radius: 7px" in sheet11,
      "表里有 9.75pt=%s，有 7px 圆角=%s"
      % ("font-size: 9.75pt" in sheet11, "border-radius: 7px" in sheet11))


def prop_values(sheet):
    out = {}
    for prop, val in re.findall(r"(-{0,2}[a-z-]+):\s*([^;{}]+)", sheet):
        out.setdefault(prop.strip(), set()).add(val.strip())
    return out


pv11, pv18 = prop_values(sheet11), prop_values(sheet18)
changed = {p for p in set(pv11) & set(pv18) if pv11[p] != pv18[p]}
whitelist = set(qss._SCALE_PROPS)
check("字号 11→18 后只有白名单里的属性变了",
      changed <= whitelist, "变了但不在白名单的=%s" % sorted(changed - whitelist))
check("白名单属性（padding/margin/border-radius/min-width/min-height/"
      "letter-spacing/font-size）确实跟着变",
      {"font-size", "padding", "margin", "border-radius", "min-width",
       "min-height", "letter-spacing"} & changed >= {"font-size", "padding",
                                                     "margin", "border-radius"},
      "变了的=%s" % sorted(changed))
check("图标/指示器 16px 不跟着字号变",
      "width: 16px" in sheet11 and "width: 16px" in sheet18, "QSpinBox 按钮")
check("滚动条 10px 不跟着字号变", "width: 10px" in sheet11 and "width: 10px" in sheet18)
# **原来这里有一条「进度条 18px 不跟着字号变」，随 `QProgressBar` 整段删除
# 一起去掉了。** `height: 18px` 在全仓唯一的用户就是那条规则，控件是零实例
# （传输进度条是 `transfer_strip.py` 自绘的，QSS 给它上不了色）。判据断言
# 「某个字面量在两张表里都出现」，而那个字面量已经不存在了 —— 这不是判据过时，
# 是它量的东西被删了。留着它只会让人以为还有一条进度条要照顾。
check("border 宽度 1px 不跟着字号变",
      sheet11.count("border: 1px solid") == sheet18.count("border: 1px solid")
      and sheet11.count("border: 1px solid") > 0)
# 圆角：短写和四个方向写法在 CSS 里是各自独立的属性，所以「同一个圆角概念」
# 有两套缩放行为时，肉眼看不出来（差值只有几个像素），判据必须逐属性按
# **比例**核对，而不是钉某个字面量 —— 钉死 `15px` 这种值的判据下一次改
# `_num` 的取整方式就得跟着改，改不动的时候就会有人去改阈值。
# 原来那条把缺陷写成了契约：它断言 `border-top-left-radius: 9px` **留在**
# 18pt 的表里，也就是断言「方向性圆角不缩放」。
_SCALE_R = 18.0 / UI_SIZE_REF


def _expect(val11: str) -> str:
    """按 11→18 的比例算出该有的值。0 必须还是 0（不要 ≠ 要一点点）。

    **裸 `0` 按 `0px` 处理**，与 `_scale_metrics._num` 的文档行为对齐：
    CSS 允许裸 0，而那个函数在缩放时把它规范成 `0px`。原来这里直接
    `int(val11[:-2])`，碰到裸 0 就是 `int("")` 抛 ValueError ——
    **判据自己崩掉比判红更糟**，它不说是哪条错，还顺手把后面几十条截断。
    """
    if val11 == "0":
        return "0px"
    n = int(val11[:-2])
    return "0px" if n == 0 else "%dpx" % max(1, round(n * _SCALE_R))


for _prop in ("border-radius", "border-top-left-radius",
              "border-top-right-radius", "border-bottom-left-radius",
              "border-bottom-right-radius"):
    _v11 = pv11.get(_prop) or set()
    _want = {_expect(v) for v in _v11}
    check("圆角 %s 跟着字号一起缩放（同一个圆角概念不能两套行为）" % _prop,
          bool(_v11) and pv18.get(_prop) == _want,
          "11pt=%s → 18pt=%s，应为 %s" % (sorted(_v11), sorted(pv18.get(_prop, [])),
                                          sorted(_want)))
check("同一个间距概念两套行为：`margin-right` 也该跟着 `padding` 一起缩放",
      pv11.get("margin-right") != pv18.get("margin-right"),
      "padding 缩放了=%s 而 margin-right 缩放了=%s（值 %s）—— 两者是同一个"
      "「跟着字号走的间距」概念" % (pv11["padding"] != pv18["padding"],
                                  pv11.get("margin-right") != pv18.get("margin-right"),
                                  sorted(pv11.get("margin-right", []))))
_MARGIN_DIRS = {"margin-top", "margin-right", "margin-bottom", "margin-left"}
# **原来这里有一条 `margin-top` 的专项判据，随 QGroupBox 整段删除一起去掉了**
# —— 那个控件是 `margin-top` 在全仓唯一的用户（`qss.py` 缩放白名单注释里
# 举的例子也是它）。判据本来的意思是「`margin-top` 有没有被正确缩放」，
# 而**现在全仓一处 `margin-top` 都没有**，判它缩放没缩放就是在判一个不存在
# 的要求 —— 这和它下面那段注释说 `margin-bottom` 时用的是同一个道理。
# 下面那条覆盖面判据接手：表里出现过的方向全都会被检查。
# **不逐个方向各判一条，而是判「表里出现过的方向性 margin 全都缩放了」。**
# 逐个判的前提是每个方向都得有声明，而 `margin-bottom` 全仓**一次都没用过**
# —— 判它「有没有按比例缩放」就是判一个不存在的要求，那种判据恒绿。
# 这一条改成覆盖面：将来谁写了 `margin-bottom: 8px` 却忘了加进白名单，它会红。
_present_dirs = _MARGIN_DIRS & set(pv11)
check("表里出现过的每一个方向性 margin 都缩放了（漏一个就红）",
      _present_dirs and all(pv18.get(p) == {_expect(v) for v in pv11[p]}
                            for p in _present_dirs),
      "出现过的=%s" % sorted(_present_dirs))
# **`top` / `left` 是绝对定位的偏移，不是间距**，绝不跟着字号走：
# 标签 pane 靠 `top: -1px` 让选中标签盖住接缝，缩放成 -2px 就多错开一像素。
for _prop in ("top", "left"):
    check("绝对定位偏移 %s 不跟着字号变（它不是间距）" % _prop,
          pv11.get(_prop) == pv18.get(_prop),
          "11pt=%s 18pt=%s" % (sorted(pv11.get(_prop, [])),
                               sorted(pv18.get(_prop, []))))

i_focus_cb = sheet11.find("QCheckBox::indicator:focus")
i_checked_cb = sheet11.rfind("QCheckBox::indicator:checked")
check("checkbox 的 focus 规则排在所有 :checked 之后（焦点环不会被吃掉）",
      i_focus_cb != -1 and i_checked_cb != -1 and i_focus_cb > i_checked_cb,
      "focus@%d checked@%d" % (i_focus_cb, i_checked_cb))

# 开关控件颜色与主题/accent 无关
from sshclient.ui.tunnel_panel import _SWITCH_ROLE, _SwitchDelegate  # noqa: E402


def render_switch(p, on, paint=None):
    view = QtWidgets.QTreeView()
    view.setStyleSheet(qss.build_qss(p))
    model = QtGui.QStandardItemModel(1, 1)
    # `setData(index, value, role)` —— **实参顺序原来写反了**
    # （`setData(index, _SWITCH_ROLE, on)` 把角色当成了值），于是
    # `index.data(_SWITCH_ROLE)` 恒为 None，委托每次都画**关闭态**。
    # 这就是为什么三种配色下量出来全是同一个 `#575858`（`Mid` 叠半透明）：
    # 量的压根不是开启态的轨道，判据钉的那件事从头到尾没被量到。
    model.setData(model.index(0, 0), on, _SWITCH_ROLE)
    view.setModel(model)
    d = _SwitchDelegate(lambda *a: None, p)
    opt = QtWidgets.QStyleOptionViewItem()
    opt.widget = view
    opt.rect = QtCore.QRect(0, 0, 60, 30)
    opt.state = QtWidgets.QStyle.StateFlag.State_Enabled
    pm = QtGui.QPixmap(60, 30)
    pm.fill(QtGui.QColor("#010203"))          # 哨兵色：与轨道色都不同
    painter = QtGui.QPainter(pm)
    if paint is None:
        d.paint(painter, opt, model.index(0, 0))
    else:
        paint(painter, opt, model.index(0, 0))
    painter.end()
    view.deleteLater()
    return pm


def _accent_switch_paint(p):
    """缺陷版的对照实现：轨道色真的读主题 accent。"""
    def paint(painter, option, index):
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(p.accent))
        r = option.rect
        painter.drawRoundedRect(QtCore.QRectF(r.x() + 11, r.y() + 4, 38, 21),
                                10.5, 10.5)
        painter.setBrush(QtGui.QColor(p.bg_surface))
        painter.drawEllipse(QtCore.QRectF(r.x() + 14, r.y() + 7, 15, 15))
        painter.restore()
    return paint


pm_dark = render_switch(palmod.DARK, True)
hist = {c: n for c, n in color_hist(pm_dark).items() if c != "#010203"}
dark_track = max(hist.items(), key=lambda kv: kv[1])[0] if hist else None
check("开关探针自校验：开启态确实画出了轨道色",
      dark_track is not None and hist.get(dark_track, 0) > 50,
      "主色=%s 像素=%d" % (dark_track, hist.get(dark_track, 0)))
# 探针自校验的第二半：**开启态和关闭态必须画出不同的轨道**。少了这一条，
# 上面那条自校验在「委托其实一直画的是关闭态」时照样绿 —— 而那正是
# setData 实参写反时的真实情况（量到的 `#575858` 是关闭态的灰）。
off_hist = {c: n for c, n in color_hist(render_switch(palmod.DARK, False)).items()
            if c != "#010203"}
off_track = max(off_hist.items(), key=lambda kv: kv[1])[0] if off_hist else None
check("开关探针自校验：开启态与关闭态画出的轨道不是同一个颜色",
      off_track is not None and dark_track is not None
      and off_track != dark_track,
      "开=%s 关=%s" % (dark_track, off_track))
acc = palmod.get_palette("dark", "#c0392b")
pm_acc = render_switch(acc, True)
ha = {c: n for c, n in color_hist(pm_acc).items() if c != "#010203"}
acc_track = max(ha.items(), key=lambda kv: kv[1])[0] if ha else None
check("开启态轨道色必须跟着 accent 变",
      acc_track is not None and acc_track.lower() == acc.accent.lower(),
      "深色主题轨道=%s；换强调色后轨道=%s 期望 accent=%s"
      % (dark_track, acc_track, acc.accent))
pm_light = render_switch(palmod.LIGHT, True)
hl = {c: n for c, n in color_hist(pm_light).items() if c != "#010203"}
light_track = max(hl.items(), key=lambda kv: kv[1])[0] if hl else None
check("开启态轨道色必须跟着主题变（浅色主题也不能是系统蓝）",
      light_track is not None
      and light_track.lower() != (dark_track or "").lower()
      and light_track.lower() == palmod.LIGHT.accent.lower(),
      "浅色轨道=%s 深色轨道=%s 浅色 accent=%s" % (light_track, dark_track,
                                                 palmod.LIGHT.accent))

# 「添加端口转发」弹窗漏传 ui_size
#
# **这两条原来把缺陷写成了契约**：原文断言弹窗的表里**不能**出现 21px、且
# 必须逐字等于 `build_qss(..., ui_size=UI_SIZE_REF)` —— 也就是「这个弹窗
# 永远停在 11pt」。那正是要修的毛病本身：用户把界面字号调到 18，主窗口和
# 设置框都变大了，从转发面板点「添加转发」弹出来的还是 13px 满屏小字。
# 现在两条反过来钉真契约，判据条数不减。
# （18pt 那张表里的字面量是 `15.75pt` 而不是 `21px`：QSS 的 `font-size`
# 统一在生成之后由 px 换算成 pt，18/11 的缩放先把 13px 放大到 21px，
# 再 ×0.75 —— 渲染尺寸与换算前逐像素一致。）
from sshclient.ui.tunnel_panel import _AddDialog  # noqa: E402

# 弹窗不持有 AppSettings，字号走 qss 的模块级 current —— 和
# msgbox.py:46 / connect_dialog.py:228 同一条路，所以这里也用 set_current
# 来模拟「用户把界面字号调成 18」。
qss.set_current(palmod.DARK, 18)
dlg = _AddDialog(palmod.DARK)
try:
    got = dlg.styleSheet()
    check("「添加端口转发」弹窗的字号跟着 ui_size 走（18pt → 15.75pt）",
          got == qss.build_qss(palmod.DARK, dialog=True, ui_size=18)
          and "font-size: 15.75pt" in got,
          "18pt 表里有没有 15.75pt=%s；弹窗表逐字等于 18pt 表=%s"
          % ("font-size: 15.75pt" in got,
             got == qss.build_qss(palmod.DARK, dialog=True, ui_size=18)))
    # 对照组：证明上一条不是「弹窗碰巧长得一样」。同一份代码在 11pt 下必须
    # 给出另一张表，否则上面那条就只是一句恒真。
    qss.set_current(palmod.DARK, UI_SIZE_REF)
    dlg11 = _AddDialog(palmod.DARK)
    try:
        got11 = dlg11.styleSheet()
        check("对照：同一个弹窗在 11pt 下回到 9.75pt（探针真能分辨字号）",
              got11 == qss.build_qss(palmod.DARK, dialog=True,
                                     ui_size=UI_SIZE_REF)
              and got11 != got,
              "11pt 表逐字等于 build_qss(11pt)=%s；与 18pt 那张不同=%s"
              % (got11 == qss.build_qss(palmod.DARK, dialog=True,
                                        ui_size=UI_SIZE_REF), got11 != got))
    finally:
        dlg11.close()
        dlg11.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None,
                                                QtCore.QEvent.Type.DeferredDelete)
finally:
    dlg.close()
    dlg.deleteLater()
    qss.set_current(palmod.DARK, UI_SIZE_REF)
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

# 设置对话框的 7 个编辑器没有可访问名称、标题没有 setBuddy
#
# 两条都写成**结构性**的：数的是「这一页所有输入控件」和「所有字段标题」，
# 不是某几个具名字段。将来有人再加一行设置、忘了给可访问名，它会自己变红。
section("9b. 设置对话框的字段标题与可访问名（ui/settings_dialog.py）")
from sshclient.ui.settings_dialog import SettingsDialog  # noqa: E402

# 绝不碰用户真实配置：AppSettings 无参构造读的是 %APPDATA%，4 行内改掉。
set_tmp = Path(tempfile.mkdtemp(prefix="fconn-settings-"))
atexit.register(lambda: shutil.rmtree(set_tmp, ignore_errors=True))
set_st = AppSettings()
set_st._path = set_tmp / "settings.json"
# **这个对话框构造时会改全局 app 字体**（`_build_appearance` 里那句
# `_preview_ui_font()` 直接 `app.setFont(...)`）。不存回来，后面每一条按像素
# 量文字颜色的判据都被换了一张字体去渲染 —— 判据之间互相污染，量到的颜色
# 差异说不清是实现的锅还是字体的锅。
app_font_before = app.font()
set_dlg = SettingsDialog(set_st, palmod.DARK)
try:
    # QSpinBox 内部**自带**一个 QLineEdit，那是 Qt 的实现细节、不是这一页的
    # 字段；不剔掉的话「每个输入控件都有可访问名」会被内部件判红。
    spin_ids = {id(w) for w in set_dlg.findChildren(QtWidgets.QAbstractSpinBox)}
    editors = [w for w in set_dlg.findChildren(QtWidgets.QLineEdit)
               if id(w.parent()) not in spin_ids]
    editors += set_dlg.findChildren(QtWidgets.QSpinBox)
    editors += set_dlg.findChildren(QtWidgets.QComboBox)
    # 判据钉的是「可访问名**就是界面上那个可见标题**」：另写一份文案迟早
    # 和界面上那句漂掉，而漂掉之后读屏念的还是过时的那个名字。
    visible_titles = {lab.text() for lab in set_dlg.findChildren(QtWidgets.QLabel)
                      if lab.text().strip()}
    nameless = ["%s[%d]" % (type(e).__name__, i) for i, e in enumerate(editors)
                if not e.accessibleName().strip()]
    drifted = [e.accessibleName() for e in editors
               if e.accessibleName() and e.accessibleName() not in visible_titles]
    check("设置对话框每个输入控件都有可访问名（且就是界面上那个可见标题）",
          len(editors) >= 7 and not nameless and not drifted,
          "找到 %d 个字段编辑器（≥7 是防「一个都没找到」的空过）；"
          "没有可访问名的=%s；可访问名对不上任何可见标题=%s"
          % (len(editors), nameless, drifted))

    # role="dim" 是 `_row` 给字段标题自己的标记，全仓只有那一处设它
    # （已核对 titlebar 不设 role），所以这就是「所有可见字段标题」。
    dim_labels = [lab for lab in set_dlg.findChildren(QtWidgets.QLabel)
                  if lab.property("role") == "dim"]
    bad_buddy = [lab.text() for lab in dim_labels
                 if lab.buddy() is None or lab.buddy().accessibleName() != lab.text()]
    check("设置对话框每个可见字段标题都 setBuddy 到了自己那一行的控件",
          len(dim_labels) >= 9 and not bad_buddy,
          "字段标题 %d 条（≥9 是防「一个都没找到」的空过）；没有 buddy 或 "
          "buddy 指向的不是本行那个控件的=%s" % (len(dim_labels), bad_buddy))
finally:
    app.setFont(app_font_before)
    set_dlg.close()
    set_dlg.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

# 切主题后会话状态文字掉回 text_dim（像素判据，探针先自校验）
def render_label(sheet, prop_color, role="stateText"):
    lab = QtWidgets.QLabel("已连接")
    lab.setFixedSize(120, 26)
    lab.setProperty("role", role)
    if prop_color is not None:
        lab.setProperty("color", prop_color)
    lab.setStyleSheet(sheet)
    # 动态属性必须重新 polish，QSS 的属性选择器才看得见（main_window._set_state
    # 走的就是 unpolish/polish 这一对）。
    lab.style().unpolish(lab)
    lab.style().polish(lab)
    lab.ensurePolished()
    pm = QtGui.QPixmap(lab.size())
    lab.render(pm)
    lab.deleteLater()
    return pm


def label_text_color(sheet, prop_color, bg):
    # 给标签垫一张不透明的底色：主题表里 QLabel 是 background: transparent，
    # 不垫底的话扫描到的是未初始化 pixmap 的颜色。
    pm = render_label(sheet + "\nQLabel { background-color: %s; }" % bg, prop_color)
    return dominant_other(pm, bg)


_self = label_text_color("QLabel { color: #ff0000; }", None, "#ffffff")
check("标签像素探针自校验：样式表指定的已知文字色能被扫出来",
      _self is not None and _self.lower() == "#ff0000", str(_self))

# 原来这里有一条：「切到浅色主题后会话状态文字仍显示该状态的成功色」。
#
# **它被删掉了，因为它量的状态在产品里已经不存在了。**
# 那条判据是**手工造一个 QLabel**、`setProperty("color", DARK.success)` 之后
# 套上 LIGHT 的 QSS 去撞 —— 也就是说它**绕开了 `SessionTab._refresh_state_style`**，
# 直接模拟「property 还是旧主题的色值」。而那个状态的成因（换主题时只重画
# `state_dot`、不动 `state_label` 的 property）已经修好：`_refresh_state_style`
# 现在会连同 property 一起重写。
#
# 留着一条量不可达状态的判据，等于**要求产品维持一个已经不存在的坏状态**；
# 而这种判据一旦变红，没人说得清该改产品还是该改判据。真实路径的那条在
# `tests/_fwin.py`（走真 `SessionTab`、量真实渲染像素）。

# **模块级 `qss.set_current` 仍然只有一个槽位，而且它现在只是「兜底」。**
# 原来这条判据要求「两个主题不同的主窗口，弹窗取到各自配色」在
# `qss.set_current` 这一层就成立 —— 那一层根本做不到：它是一个模块级单例，
# 后 `apply_theme` 的必然覆盖先前的。
# 现在真正成立的是：**每个顶层窗口把自己的 Palette 挂在 Qt 属性上，
# `msgbox` 优先沿父链读它**。所以判据量的是那条真实机制。
_msgbox_mod = __import__("sshclient.ui.msgbox", fromlist=["msgbox"])
_w1 = QtWidgets.QWidget()
_w1.setProperty(_msgbox_mod._PALETTE_PROP, palmod.DARK)
_w2 = QtWidgets.QWidget()
_w2.setProperty(_msgbox_mod._PALETTE_PROP, palmod.LIGHT)
_d1 = QtWidgets.QWidget(_w1)
_d2 = QtWidgets.QWidget(_w2)
check("两个主题不同的主窗口，各自的弹窗取到**各自**的配色（走父链而不是全局）",
      _msgbox_mod._palette_of(_d1) is palmod.DARK
      and _msgbox_mod._palette_of(_d2) is palmod.LIGHT,
      "窗口1=%s 窗口2=%s" % (getattr(_msgbox_mod._palette_of(_d1), "name", None),
                             getattr(_msgbox_mod._palette_of(_d2), "name", None)))
check("没有挂配色的祖先时回退到模块级全局（崩溃兜底框就靠这条）",
      _msgbox_mod._palette_of(QtWidgets.QWidget()) is None
      and qss.current() is not None,
      "无祖先时返回 %r" % (_msgbox_mod._palette_of(QtWidgets.QWidget()),))
for _w in (_w1, _w2, _d1, _d2):
    _w.deleteLater()

# 对勾 PNG 的 `url()`：生产侧产出 + 像素双证。Qt 的 QSS 画不出对勾，
# 只能给 `::indicator:checked` 一张 PNG，而 Windows 用户目录带空格是常态。
def checkmark_white_pixels(frag, style="check"):
    """数「近白」像素。底色/填充都刻意选成非白，全控件里唯一能贡献近白
    像素的就是 QSS ``image:`` 画出来的那张 PNG。

    *frag* 是**完整的** `url(...)` 片段（含引号），不是在测试里重拼一遍
    `url(%s)` —— 重拼等于量测试自己造的串，生产侧加不加引号它都照样绿。"""
    if style == "check":
        w = QtWidgets.QCheckBox("x")
        w.setFixedSize(40, 24)
        w.setChecked(True)
        sheet = ("QCheckBox { background-color: #123456; color: #123456; }"
                 "QCheckBox::indicator { width: 16px; height: 16px;"
                 " background-color: #4c8dff; border: 1px solid #4c8dff; }"
                 "QCheckBox::indicator:checked { image: %s; }" % frag)
    else:
        w = QtWidgets.QPushButton()
        w.setFixedSize(24, 24)
        sheet = ("QPushButton { background-color: #123456; border: none;"
                 " image: %s; }" % frag)
    w.setStyleSheet(sheet)
    w.ensurePolished()
    pm = QtGui.QPixmap(w.size())
    w.render(pm)
    w.deleteLater()
    img = pm.toImage()
    n = 0
    for x in range(img.width()):
        for y in range(img.height()):
            c = img.pixelColor(x, y)
            if c.red() > 200 and c.green() > 200 and c.blue() > 200:
                n += 1
    return n


_real_gettempdir = tempfile.gettempdir
# 临时目录名里带空格 —— Windows 上「用户目录有空格」是常态（`C:\Users\<名 带
# 空格>\AppData\...`）。把 `tempfile.gettempdir()` 指过来，就能在**生产代码**
# 里量到一条真正带空格的路径，而不是在测试里自己拼一个。
_space_root = tempfile.mkdtemp(prefix="fconn space ")
tempfile.gettempdir = lambda: _space_root
qss._CHECK_URLS.clear()
try:
    frag = qss._check_mark_url("#ffffff")
finally:
    tempfile.gettempdir = _real_gettempdir
    qss._CHECK_URLS.clear()
m = re.search(r"url\(\"([^\"]+)\"\)", frag)
frag_path = m.group(1) if m else ""
check("含空格的路径下生产侧生成的 url() 把路径用引号包住了"
      "（未加引号时 QSS 在第一个空白处就截断，这条 image: 整条失效）",
      frag.startswith('url("') and frag.endswith('")'),
      "生成的片段=%s" % (frag or "url(...) 都没生成"))
check("那条 PNG 真的落在带空格的目录里（不是 url 看着对、文件其实没生成）",
      bool(frag_path) and " " in frag_path
      and Path(frag_path).is_file() and Path(frag_path).stat().st_size > 0,
      "路径=%s 存在=%s" % (frag_path, Path(frag_path).is_file() if frag_path else False))

# 运行级：生产侧那条 `url(...)` 真的把对勾画出来了（像素证明）。
# **负向对照是「负向对照」而不是「另一个路径」**：原来那条判据拿「无空格路径」
# 当对照，而 Qt 恰恰可能两边都画得出来（实测 Qt 6.11 就是如此），于是它量的
# 不是「空格有没有害」，而是「Qt 宽不宽容」。这里改成指向一个**不存在的文件**
# 当对照：对照必须是 0，否则探针对 `image:` 解析结果没有鉴别力。
missing = 'url("%s.no-such-file.png")' % frag_path
n_real = checkmark_white_pixels(frag, "check")
if n_real == 0:
    n_real = checkmark_white_pixels(frag, "btn")
    style_used = "btn"
else:
    style_used = "check"
n_missing = checkmark_white_pixels(missing, style_used)
if n_real == 0 or n_missing != 0:
    skip("含空格的路径下 image: url() 真的画得出对勾（像素证明）",
         "探针前提不成立：%s 路径下 %s（真实 %d 近白像素 / 指向不存在文件 %d）"
         "—— 探针对 QSS image: 的解析结果没有鉴别力，判据不成立"
         % (style_used, "画不出图" if n_real == 0 else "照样画出了图",
            n_real, n_missing))
else:
    check("含空格的路径下 image: url() 真的画得出对勾（像素证明，%s）" % style_used,
          n_real > 0,
          "真实路径近白像素=%d / 指向不存在文件的对照=%d" % (n_real, n_missing))
shutil.rmtree(_space_root, ignore_errors=True)

# ================================================================ 10. 元断言
section("10. 元断言（把实现换成缺陷版本，那批判据必须变红）")


def batch_validate():
    cases = ((TunnelSpec("local", 13306, "127.0.0.1", 3306), True),
             (TunnelSpec("local", 0, "h", 1), False),
             (TunnelSpec("local", 70000, "h", 1), False),
             (TunnelSpec("local", 1080, "", 80), False),
             (TunnelSpec("local", 1080, "h", 0), False),
             (TunnelSpec("socks", 1080), True),
             (TunnelSpec("nope", 1080), False))
    for spec, want in cases:
        try:
            tunnel.validate(spec)
            got = True
        except TunnelError:
            got = False
        if got != want:
            return False
    return True


real_validate = tunnel.validate


def _run_meta(name, good, break_it, restore):
    def run():
        try:
            return bool(good())
        except Exception:
            return False        # 抛异常也算「这批判据变红」

    real_ok = run()
    break_it()
    try:
        broken_ok = run()
    finally:
        restore()
    meta(name, real_ok and not broken_ok, "real=%s broken=%s" % (real_ok, broken_ok))


_run_meta("元断言·validate 批次：换成不校验的版本必须变红",
          batch_validate,
          lambda: setattr(tunnel, "validate", lambda spec: spec),
          lambda: setattr(tunnel, "validate", real_validate))


def batch_socks():
    ok = True
    for atyp, blob, want in (
        ("ipv4", b"\x05\x01\x00\x01" + socket.inet_aton("10.1.2.3")
         + struct.pack("!H", 8080), ("10.1.2.3", 8080)),
        ("domain", b"\x05\x01\x00\x03\x0bdb.internal" + struct.pack("!H", 5432),
         ("db.internal", 5432)),
        ("ipv6", b"\x05\x01\x00\x04"
         + socket.inet_pton(socket.AF_INET6, "2001:db8::1") + struct.pack("!H", 443),
         ("2001:db8::1", 443)),
    ):
        a, b = socket.socketpair()
        a.settimeout(5)
        try:
            b.sendall(b"\x05\x01\x00" + blob)
            ok = ok and tunnel.parse_socks5_request(a) == want
        except Exception:
            ok = False
        finally:
            a.close()
            b.close()
    return ok


real_recv = tunnel._recv_exactly
_run_meta("元断言·SOCKS 解析批次：换成不读满的 _recv_exactly 必须变红",
          batch_socks,
          lambda: setattr(tunnel, "_recv_exactly",
                          lambda s, n: s.recv(65536)),
          lambda: setattr(tunnel, "_recv_exactly", real_recv))


def batch_rows():
    s = TunnelSpec("local", 13306, "db", 3306, description="MySQL")
    b = TunnelSpec.from_row(s.to_row())
    return (b is not None and b.local_port == 13306 and b.description == "MySQL"
            and TunnelSpec.from_row([1, "h", 2, "d"]) is not None
            and TunnelSpec.from_row([1, "h"]) is not None
            and TunnelSpec.from_row(["x", "y", "z"]) is None)


real_from_row = TunnelSpec.from_row.__func__
_run_meta("元断言·行往返批次：from_row 恒返回 None 必须变红",
          batch_rows,
          lambda: setattr(TunnelSpec, "from_row", classmethod(
              lambda cls, row: None)),
          lambda: setattr(TunnelSpec, "from_row", classmethod(real_from_row)))


def batch_scale():
    s11 = qss.build_qss(palmod.DARK, ui_size=11)
    s18 = qss.build_qss(palmod.DARK, ui_size=18)
    p11, p18 = prop_values(s11), prop_values(s18)
    changed = {p for p in set(p11) & set(p18) if p11[p] != p18[p]}
    return changed <= set(qss._SCALE_PROPS) and "font-size" in changed


real_scale = qss._scale_metrics
_run_meta("元断言·字号缩放批次：换成完全不缩放必须变红",
          batch_scale,
          lambda: setattr(qss, "_scale_metrics", lambda s, k: s),
          lambda: setattr(qss, "_scale_metrics", real_scale))


def batch_cache():
    icons.clear_cache()
    a = icons.icon("terminal", palmod.DARK, 18)
    b = icons.icon("terminal", palmod.DARK, 18)
    n = len(icons._CACHE)
    icons.clear_cache()
    return a is b and n == 1 and len(icons._CACHE) == 0


real_icon = icons.icon
_run_meta("元断言·icon 缓存批次：换成每次新建必须变红",
          batch_cache,
          lambda: setattr(icons, "icon",
                          lambda name, p, size=18, color=None:
                          __import__("PyQt6.QtGui", fromlist=["QIcon"])
                          .QIcon(icons.icon_pixmap(name, color or p.text, size))),
          lambda: setattr(icons, "icon", real_icon))


def batch_scan():
    base = free_run(6)
    socks = occupy(base, 1)
    c = SSHConnection(ConnectionInfo(host="h", port=22, username="u"))
    c.open_direct_channel = never
    try:
        return c.add_tunnel(TunnelSpec("local", base, "db", 3306)).local_port == base + 1
    finally:
        c.close()
        for s in socks:
            s.close()


def _noscan_start(conn, rule):
    """缺陷版 _start_rule：只试第一个候选端口，绝不往后顺延。"""
    rule.spec = connmod.tunnel.validate(rule.spec)
    fwd = connmod.tunnel.Forward(rule.spec, conn.open_direct_channel)
    fwd.start()
    rule.forward = fwd
    rule.enabled = True
    return rule.spec


real_start_rule = SSHConnection._start_rule
_run_meta("元断言·端口顺延批次：换成不顺延（只试第一个号）必须变红",
          batch_scan,
          lambda: setattr(SSHConnection, "_start_rule", _noscan_start),
          lambda: setattr(SSHConnection, "_start_rule", real_start_rule))


def batch_parse():
    s = mp.parse_snapshot(SAMPLE)
    return (s.cpu is not None and s.cpu.user == 12.5 and s.mem is not None
            and len(s.disks) == 2 and s.net is not None and len(s.procs) == 2
            and s.host.name == "web01")


_REAL_PARSE = mp.parse_snapshot

_REAL_PARSE = mp.parse_snapshot
_REAL_SPLIT = lrzsz.split_remote_paths
_REAL_DL = lrzsz.download_dir

_run_meta("元断言·探测解析批次：换成恒返回空快照必须变红",
          batch_parse,
          lambda: setattr(mp, "parse_snapshot",
                          lambda t: mp.MonitorSnapshot(host=mp.HostInfo())),
          lambda: setattr(mp, "parse_snapshot", _REAL_PARSE))


def batch_split():
    return (lrzsz.split_remote_paths("a.log b.log") == ["a.log", "b.log"]
            and lrzsz.split_remote_paths('a.log "my file.txt" x')
            == ["a.log", "my file.txt", "x"])


_run_meta("元断言·远端路径切分批次：换成朴素 split 必须变红",
          batch_split,
          lambda: setattr(lrzsz, "split_remote_paths", lambda t: t.split()),
          lambda: setattr(lrzsz, "split_remote_paths", _REAL_SPLIT))


def batch_dl():
    t = Path(tempfile.mkdtemp(prefix="fconn-meta-"))
    try:
        nd = t / "f"
        nd.write_text("x", encoding="utf-8")
        d1 = t / "one"
        return lrzsz.download_dir([str(nd), str(d1)]) == str(d1) and d1.is_dir()
    finally:
        shutil.rmtree(t, ignore_errors=True)


_run_meta("元断言·download_dir 批次：换成直接返回第一个候选必须变红",
          batch_dl,
          lambda: setattr(lrzsz, "download_dir", lambda c=None: str((c or [""])[0])),
          lambda: setattr(lrzsz, "download_dir", _REAL_DL))


_REAL_SWITCH_PAINT = _SwitchDelegate.paint

_SWITCH_MODE = [False]
_FROZEN = [None]


def _track_of(pm):
    h = {c: n for c, n in color_hist(pm).items() if c != "#010203"}
    return max(h.items(), key=lambda kv: kv[1])[0] if h else None


def _paint_for(p):
    """``_SWITCH_MODE`` 打开时替成**缺陷版绘制**：把配色冻在 ``_FROZEN`` 上，
    于是三种配色画出来的轨道一模一样 —— 也就是修复前那个症状本身。"""
    if not _SWITCH_MODE[0]:
        return None
    return _accent_switch_paint(_FROZEN[0] or p)


def batch_switch():
    """判据的判据：探针能不能分辨「轨道色跟不跟 accent 走」。

    这里断言的是**修好之后的契约**：三种配色下轨道色各不相同，且各自就是
    那一套配色的 accent。打开 ``_SWITCH_MODE`` 换成「配色被冻住」的绘制版本
    后三条会塌成同一个色，这批判据必须变红。

    （原文这里是反的：它断言三条**必须相同**，理由写着「这是当前实现的行为」
    —— 判据把缺陷当成了契约。修掉缺陷后它必然变红，所以只能连着契约一起翻。）
    """
    acc = palmod.get_palette("dark", "#c0392b")
    t1 = _track_of(render_switch(palmod.DARK, True, _paint_for(palmod.DARK)))
    t2 = _track_of(render_switch(acc, True, _paint_for(acc)))
    t3 = _track_of(render_switch(palmod.LIGHT, True, _paint_for(palmod.LIGHT)))
    return (None not in (t1, t2, t3) and len({t1, t2, t3}) == 3
            and t1 == palmod.DARK.accent.lower()
            and t2 == acc.accent.lower()
            and t3 == palmod.LIGHT.accent.lower())


_run_meta("元断言·开关轨道色批次：换成配色被冻住的绘制版本必须变红",
          batch_switch,
          lambda: (_SWITCH_MODE.__setitem__(0, True),
                   _FROZEN.__setitem__(0, palmod.DARK)),
          lambda: (_SWITCH_MODE.__setitem__(0, False),
                   _FROZEN.__setitem__(0, None)))
meta("元断言·开关轨道色缺陷版真的画出了不同的轨道色（对照有效）",
     _track_of(render_switch(palmod.DARK, True, _accent_switch_paint(palmod.DARK)))
     == palmod.DARK.accent.lower()
     and _track_of(render_switch(palmod.LIGHT, True,
                                 _accent_switch_paint(palmod.LIGHT)))
     == palmod.LIGHT.accent.lower(),
     "深色=%s 浅色=%s（换成 accent 之后颜色确实变了 → 探针有鉴别力）"
     % (_track_of(render_switch(palmod.DARK, True,
                                 _accent_switch_paint(palmod.DARK))),
        _track_of(render_switch(palmod.LIGHT, True,
                                _accent_switch_paint(palmod.LIGHT)))))

stop_all()
shutil.rmtree(tmpdir, ignore_errors=True)

# ================================================================ 收尾
for f in list(_FORWARDS):
    try:
        f.stop()
    except Exception:
        pass
stop_all()

total = len(PASS) + len(FAIL) + len(SKIP)
print("\n" + "=" * 72)
print("判据 %d 条：通过 %d 项，失败 %d 项，未评上 %d 项，用时 %.1f 秒"
      % (total, len(PASS), len(FAIL), len(SKIP), time.monotonic() - _T0))
for f in FAIL:
    print("  FAIL " + f)
for s in SKIP:
    print("  SKIP " + s)
sys.exit(1 if FAIL else (2 if SKIP else 0))
