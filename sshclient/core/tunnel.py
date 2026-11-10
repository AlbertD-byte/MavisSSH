"""SSH port forwarding: local forwards (``-L``) and a SOCKS5 proxy (``-D``).

:paramiko`'s ``Transport.request_port_forward`` only opens a listening socket
on the local side and hands the caller the raw stream; the SOCKS5 protocol
itself is **not** implemented by paramiko. For a dynamic forward the remote
destination is not known until the client has sent its SOCKS5 CONNECT
request, so this module speaks enough of RFC 1928 to pull that destination
out of the first bytes and then open the matching ``direct-tcpip`` channel.

Data path for one accepted connection::

    local app  <--socket-->  accepted_channel  <--pump thread-->  remote target

The accept loop lives in :class:`~sshclient.connection.TunnelManager`; this
module owns the per-connection work: SOCKS5 negotiation, channel opening and
the bidirectional relay.
"""
from __future__ import annotations

import ipaddress
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .logs import get_logger

log = get_logger("mavisssh.tunnel")

# --- SOCKS5 constants (RFC 1928) ------------------------------------------
SOCKS_VERSION = 0x05
AUTH_NONE = 0x00
CMD_CONNECT = 0x01
ATYP_IPV4 = 0x01
ATYP_DOMAIN = 0x03
ATYP_IPV6 = 0x04
REPLY_SUCCEEDED = 0x00
REPLY_GENERAL_FAILURE = 0x01
REPLY_HOST_UNREACHABLE = 0x04
REPLY_CMD_NOT_SUPPORTED = 0x07
REPLY_ATYP_NOT_SUPPORTED = 0x08

# Relay buffer. 64 KiB matches a typical paramiko recv() and keeps syscall
# count low without holding much memory per live connection.
_RECV_SIZE = 65536


class TunnelError(Exception):
    """Raised when a forwarding rule is invalid or cannot be started."""


@dataclass
class TunnelSpec:
    """One forwarding rule.

    ``kind="local"`` maps ``127.0.0.1:local_port`` to
    ``remote_host:remote_port`` *through the SSH server* — the same shape as
    ``ssh -L local_port:remote_host:remote_port``.

    ``kind="socks"`` opens a SOCKS5 proxy on ``127.0.0.1:local_port``; the
    destination is chosen per connection by the client, like ``ssh -D``.

    ``listen_host`` and ``description`` are keyword-only on purpose. With them
    positional, ``TunnelSpec("local", 13306, "db", 3306, "MySQL")`` silently
    binds a listener to the host ``"MySQL"`` and fails with a DNS error that
    looks nothing like the mistake it is.
    """

    kind: str                      # "local" | "socks"
    local_port: int
    remote_host: str = ""          # local forwards only
    remote_port: int = 0           # local forwards only
    listen_host: str = field(default="127.0.0.1", kw_only=True)
    description: str = field(default="", kw_only=True)

    def label(self) -> str:
        if self.kind == "socks":
            return f"SOCKS5  {self.listen_host}:{self.local_port}"
        host = self.remote_host or "?"
        return f"{self.listen_host}:{self.local_port} → {host}:{self.remote_port}"

    def to_row(self) -> list:
        """Legacy 4/5-field row shape —— **只用来给 `from_row` 造往返测试的输入。**

        **生产侧没有任何调用点，这是有意的，不是漏掉的。**
        老格式只有 `from_row` 在读（`core/settings.py` 迁移旧会话时用）；
        写出去的形状早就换成了 `to_dict`（带 `kind` / `enabled` / 完整
        `listen_host`），老格式表达不了开关状态和多条 SOCKS 规则。

        那为什么还留着？因为**没有它就没法验证 `from_row` 真的读得懂
        老格式** —— 手写一行老数据的字面量只是「照着解析器写输入」，
        解析器改错了照样绿。这里保留一个对称的构造器，往返才有意义。
        """
        return [self.local_port, self.remote_host, self.remote_port,
                self.description, self.listen_host]

    @classmethod
    def from_row(cls, row) -> TunnelSpec | None:
        """Rebuild from a legacy row, or ``None`` if the row is unusable."""
        try:
            fields = list(row) + ["", 0, "", "127.0.0.1"]
            port, host, rport, desc, listen = fields[:5]
            return cls(
                kind="local",
                local_port=int(port),
                remote_host=str(host or ""),
                remote_port=int(rport or 0),
                description=str(desc or ""),
                listen_host=str(listen or "127.0.0.1"),
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self, enabled: bool = True) -> dict:
        """Full round-trip form stored on :attr:`SavedSession.tunnels`.

        Carries the on/off state as well: a rule the user switched off must
        come back off, not silently re-enabled on the next connect.
        """
        return {
            "kind": self.kind,
            "local_port": int(self.local_port),
            "remote_host": self.remote_host,
            "remote_port": int(self.remote_port),
            "listen_host": self.listen_host,
            "description": self.description,
            "enabled": bool(enabled),
        }

    @classmethod
    def from_dict(cls, data) -> tuple[TunnelSpec, bool] | None:
        """Rebuild from :meth:`to_dict`. Returns ``(spec, enabled)`` or ``None``.

        The two-value return is because the enabled flag belongs to the saved
        rule, not to the spec: a spec describes *what* to forward, while
        enabled says whether this session should be listening right now.
        """
        if not isinstance(data, dict):
            return None
        try:
            spec = cls(
                kind=str(data.get("kind") or "local"),
                local_port=int(data.get("local_port") or 0),
                remote_host=str(data.get("remote_host") or ""),
                remote_port=int(data.get("remote_port") or 0),
                listen_host=str(data.get("listen_host") or "127.0.0.1"),
                description=str(data.get("description") or ""),
            )
        except (TypeError, ValueError):
            return None
        if not validate_quietly(spec):
            return None
        return spec, bool(data.get("enabled", True))


def _valid_listen_host(host: str) -> bool:
    """判断 listen_host 是不是 ``bind()`` 认得的地址。

    放行主机名是有意为之：``socket.bind`` 本来就接受主机名（``localhost``
    一直能用），加载期校验不该把一个原本跑得通的配置判死。但 ``"999.999.999.999"``
    这种**只由数字和点组成、又不是合法 IP** 的串必须挡下来 —— 它在 ``bind()``
    那里只会变成一句用户读不懂的 ``getaddrinfo`` 错误，而且报错时机远晚于
    出错的那一刻。
    """
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return not all(c.isdigit() or c == "." for c in host)


def is_exposed_address(host: str) -> bool:
    """这个监听地址会不会**对网络上其他机器可见**。

    ``127.0.0.1`` / ``::1`` 是回环，只有本机能连 —— 安全。
    ``0.0.0.0``（IPv4 任意地址）、``::``（IPv6 任意地址）是「所有网卡」——
    **同一网段的任何人都能连进来**。

    为什么单独判一下：UI 里加规则时有 tooltip 提示「0.0.0.0 = 局域网可连」，
    那是**用户主动选的**，没问题。但规则也可以从 ``sessions.json`` 加载
    （``from_dict``）—— 而那个文件在用户目录里、还会被拷贝导入。于是
    **一份拷贝来的会话文件能静默带上 ``0.0.0.0``**，而那条 tooltip 提示
    根本没出现在这个场景里。加载路径上没有提示 = 用户不知情 = 暴露。

    所以启动监听时要把这件事**说出来**（写日志），并让 UI 弹一次确认。
    """
    h = str(host or "").strip()
    if not h:
        return False
    if h in ("0.0.0.0", "::", "*"):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        # 主机名（如 "localhost"）不是通配地址
        return False
    return not ip.is_loopback


def validate(spec: TunnelSpec) -> TunnelSpec:
    """Check a spec and return it normalised.

    Raises :class:`TunnelError` with a user-presentable message.
    """
    if spec.kind not in ("local", "socks"):
        raise TunnelError(f"未知的转发类型: {spec.kind!r}")
    host = str(spec.listen_host or "").strip()
    if not _valid_listen_host(host):
        # 加载期必须拦。``from_dict`` 只走 ``validate_quietly``，漏在这里的
        # 坏值会一路活到 ``bind()`` 才炸，而那时用户看到的是「端口被占 /
        # 地址不可用」这类系统消息，根本指不到是自己把监听地址写坏了。
        raise TunnelError(f"监听地址不合法: {spec.listen_host!r}")
    spec.listen_host = host
    if not (1 <= spec.local_port <= 65535):
        raise TunnelError(f"本地端口超出范围: {spec.local_port}")
    if spec.kind == "local":
        if not spec.remote_host:
            raise TunnelError("本地端口转发需要填写远程目标地址")
        if not (1 <= spec.remote_port <= 65535):
            raise TunnelError(f"远程端口超出范围: {spec.remote_port}")
        spec.remote_host = spec.remote_host.strip()
    return spec


def validate_quietly(spec: TunnelSpec) -> bool:
    """Non-raising :func:`validate`, for load paths that skip bad entries."""
    try:
        validate(spec)
    except TunnelError:
        return False
    return True


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    """Read exactly *count* bytes or raise ``ConnectionError``."""
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("SOCKS 握手期间连接被关闭")
        buf.extend(chunk)
    return bytes(buf)


def parse_socks5_request(sock: socket.socket) -> tuple[str, int]:
    """Negotiate SOCKS5 and return the requested ``(host, port)``.

    Performs the method selection (we only offer "no authentication") and
    reads one CONNECT request, resolving the address for all three ``ATYP``
    forms. Raises :class:`TunnelError` for anything we cannot honour.
    """
    # --- greeting: VER, NMETHODS, METHODS ---
    head = _recv_exactly(sock, 2)
    if head[0] != SOCKS_VERSION:
        raise TunnelError(f"不是 SOCKS5 请求 (VER=0x{head[0]:02x})")
    nmethods = head[1]
    if nmethods == 0:
        raise TunnelError("SOCKS5 客户端未提供认证方式")
    methods = _recv_exactly(sock, nmethods)
    if AUTH_NONE not in methods:
        raise TunnelError("仅支持无认证的 SOCKS5")

    sock.sendall(bytes([SOCKS_VERSION, AUTH_NONE]))

    # --- request: VER, CMD, RSV, ATYP, ADDR, PORT ---
    head = _recv_exactly(sock, 4)
    if head[0] != SOCKS_VERSION:
        raise TunnelError(f"请求版本错误 (VER=0x{head[0]:02x})")
    if head[1] != CMD_CONNECT:
        # Tell the client properly before giving up, so it reports a useful
        # error instead of a generic protocol violation.
        _socks_reply(sock, REPLY_CMD_NOT_SUPPORTED)
        raise TunnelError("仅支持 SOCKS5 CONNECT")
    atyp = head[3]

    if atyp == ATYP_IPV4:
        host = str(ipaddress.IPv4Address(_recv_exactly(sock, 4)))
    elif atyp == ATYP_DOMAIN:
        length = _recv_exactly(sock, 1)[0]
        if length == 0:
            raise TunnelError("SOCKS5 域名为空")
        host = _recv_exactly(sock, length).decode("utf-8", "replace")
    elif atyp == ATYP_IPV6:
        host = str(ipaddress.IPv6Address(_recv_exactly(sock, 16)))
    else:
        _socks_reply(sock, REPLY_ATYP_NOT_SUPPORTED)
        raise TunnelError(f"不支持的地址类型 (ATYP=0x{atyp:02x})")

    port = struct.unpack("!H", _recv_exactly(sock, 2))[0]
    if not (1 <= port <= 65535):
        raise TunnelError(f"SOCKS5 目标端口无效: {port}")
    return host, port


def _socks_reply(sock: socket.socket, code: int) -> None:
    """Best-effort SOCKS5 reply; failures here are not worth propagating."""
    try:
        sock.sendall(bytes([SOCKS_VERSION, code, 0x00, ATYP_IPV4, 0, 0, 0, 0, 0, 0]))
    except OSError:
        pass


def _pump(src, dst) -> None:
    """Relay bytes from *src* to *dst* until EOF, then shut *dst* down.

    ``shutdown(SHUT_WR)`` rather than ``close()`` matters: it sends a clean
    half-close so the peer sees EOF, while leaving the read side usable for
    the opposite direction's remaining data.
    """
    try:
        while True:
            data = src.recv(_RECV_SIZE)
            if not data:
                break
            dst.sendall(data)
    except (OSError, AttributeError):
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def handle_accepted(
    accepted: socket.socket,
    spec: TunnelSpec,
    open_channel: Callable[[str, int], object],
    on_error: Callable[[str], None] | None = None,
    *,
    on_channel: Callable[[object], None] | None = None,
) -> None:
    """Serve one accepted connection. Runs on its own thread.

    *open_channel* must open a ``direct-tcpip`` channel to ``(host, port)``
    and return an object with ``recv``/``sendall``/``close``.

    *on_channel* is handed the channel as soon as it is open. The caller
    (:class:`Forward`) needs it: shutting a rule down has to reach **both**
    ends of the connection, and only this side knows the remote end exists.
    """
    def fail(message: str) -> None:
        if on_error is not None:
            on_error(message)

    accepted.settimeout(30.0)
    try:
        if spec.kind == "socks":
            # The destination is only known once the client has spoken SOCKS.
            host, port = parse_socks5_request(accepted)
            if spec.remote_host:
                # A local forward with a pinned host ignores the SOCKS host.
                host = spec.remote_host
                port = spec.remote_port
        else:
            host, port = spec.remote_host, spec.remote_port
    except ConnectionError:
        # The peer went away mid-handshake. Port scanners, port scanners'
        # half-open probes and health checks all do this constantly, so it is
        # not worth a user-visible error — just drop the connection.
        _close(accepted)
        return
    except (TunnelError, OSError) as exc:
        fail(f"{spec.label()} · 握手失败: {exc}")
        _close(accepted)
        return

    try:
        channel = open_channel(host, port)
    except Exception as exc:  # noqa: BLE001 -- remote refusals are expected
        code = REPLY_HOST_UNREACHABLE if spec.kind == "socks" else None
        if code is not None:
            _socks_reply(accepted, code)
        fail(f"{spec.label()} → {host}:{port} · 连接失败: {exc}")
        _close(accepted)
        return

    if on_channel is not None:
        on_channel(channel)

    if spec.kind == "socks":
        _socks_reply(accepted, REPLY_SUCCEEDED)

    accepted.settimeout(None)
    try:
        channel.settimeout(None)
    except Exception:
        pass

    up = threading.Thread(target=_pump, args=(accepted, channel), daemon=True)
    down = threading.Thread(target=_pump, args=(channel, accepted), daemon=True)
    up.start()
    down.start()
    up.join()
    down.join()
    _close(channel)
    _close(accepted)


def _close(obj) -> None:
    try:
        obj.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Accept loop
# ---------------------------------------------------------------------------


@dataclass
class _Connection:
    """一条已建立连接的资源账本。

    停规则时要把这条连接一次收干净，只记本地 socket 是不够的：另一个方向的
    泵线程卡在 ``channel.recv()`` 上，光关本地 socket 叫不醒它，服务线程
    不退出，``active_connections`` 也落不到 0。
    """

    client: socket.socket                  # 本地这一端
    channel: object | None = None          # 已打开的 direct-tcpip 通道
    thread: threading.Thread | None = None  # 跑这条连接的服务线程


class Forward:
    """Accept loop for a single rule, bound to its own local listener.

    Paramiko's ``Transport.accept`` cannot be used here: it pools every
    locally-forwarded port onto one shared queue and the accepted
    :class:`~paramiko.Channel` does not reveal which listener it came from
    (``Channel.getpeername`` returns the *main transport's* peer, not the
    local port). Binding our own listener per rule keeps the rule -> socket
    -> channel mapping exact, which is what error messages and the UI need.
    """

    def __init__(
        self,
        spec: TunnelSpec,
        open_channel: Callable[[str, int], object],
        on_error: Callable[[str], None] | None = None,
        on_event: Callable[[Forward, str], None] | None = None,
    ):
        self.spec = spec
        self._open_channel = open_channel
        self._on_error = on_error
        self._on_event = on_event
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._conns: dict[threading.Thread, _Connection] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> Forward:
        """Bind the listener and spawn the accept loop.

        Binding happens here (not in the thread) so a port clash raises in
        the caller's context and can be reported to the user.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # The two platforms need *opposite* options here.
        #   Windows: SO_REUSEADDR lets a second socket bind a port that is
        #     already listening, and the newer one steals the traffic. That
        #     would let two MavisSSH instances both claim 13306 and send
        #     connections to whichever bound last, so ask for exclusivity.
        #   POSIX:   SO_REUSEADDR is what lets us rebind a port still sitting
        #     in TIME_WAIT after a tunnel closes; it does not permit two
        #     simultaneous listeners.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.spec.listen_host, self.spec.local_port))
            sock.listen(64)
            sock.settimeout(0.5)
        except OSError as exc:
            _close(sock)
            raise TunnelError(
                f"无法监听 {self.spec.listen_host}:{self.spec.local_port} · {exc}"
            ) from exc
        # **监听地址对不对外可见，要说出来。** 规则可能来自 sessions.json
        # （那份文件在用户目录、还会被拷贝导入），加载路径上**没有** UI 里
        # 那句「0.0.0.0 = 局域网可连」的提示 —— 静默暴露就是这样发生的。
        if is_exposed_address(self.spec.listen_host):
            log.warning("隧道监听在 %s:%d —— 该地址对局域网可见，"
                        "同一网段的人可以连进来",
                        self.spec.listen_host, self.spec.local_port)
        self._sock = sock
        self._stop.clear()          # a stopped Forward must be restartable
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"tunnel-{self.spec.local_port}")
        self._thread.start()
        self._emit("started")
        return self

    def stop(self) -> None:
        self._stop.set()
        _close(self._sock)
        self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._drop_connections()
        self._emit("stopped")

    # -- state -------------------------------------------------------------

    @property
    def active_connections(self) -> int:
        # 数账本本身，不另记一个计数器：连接一被关掉、服务线程一退出，计数
        # 必须跟着回落。两个数各自维护迟早会对不上（旧实现就靠一个单独的
        # 计数器加减，连接被切断了计数还挂着）。
        with self._lock:
            return len(self._conns)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- internals ---------------------------------------------------------

    def _emit(self, kind: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(self, kind)
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                client, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break                     # listener closed
            thread = threading.Thread(target=self._serve, args=(client,),
                                     daemon=True,
                                     name=f"tunnel-{self.spec.local_port}-conn")
            if self._adopt(client, thread):
                thread.start()
            else:
                _close(client)
                break                     # stop() 正好撞在 accept 返回之后

    def _adopt(self, client: socket.socket,
               thread: threading.Thread) -> bool:
        """把刚 accept 出来的连接登记进账本。返回是否登记成功。

        登记前再判一次 ``_stop``：从 ``accept()`` 返回到走到这里之间可能
        已经 ``stop()``，那种连接不会再有人去关它，就地关掉更省事；也不能
        放进账本 —— 那样 ``active_connections`` 会一直挂在一没人管的连接上。
        """
        with self._lock:
            if self._stop.is_set():
                return False
            self._conns[thread] = _Connection(client, thread=thread)
        return True

    def _note_channel(self, thread: threading.Thread, channel) -> None:
        """把刚打开的远端通道记到这条连接的账本上（见 :class:`_Connection`）。"""
        with self._lock:
            conn = self._conns.get(thread)
            if conn is not None:
                conn.channel = channel

    def _serve(self, client: socket.socket) -> None:
        me = threading.current_thread()
        try:
            handle_accepted(
                client, self.spec, self._open_channel, self._on_error,
                on_channel=lambda channel: self._note_channel(me, channel),
            )
        except OSError:
            # 握手期间 stop() 关掉了这条连接：不是错误，别往上抛成
            # 线程异常（面板状态栏会莫名其妙多出一条失败）。
            pass
        finally:
            with self._lock:
                self._conns.pop(me, None)
            self._emit("traffic")

    def _drop_connections(self) -> None:
        """关掉本规则名下已建立的每一条连接，并等它们的服务线程退出。

        **这是有意选的取舍，别当成随手改的。** OpenSSH 的 ``-L`` 只关监听，
        已建立的连接会自己跑到结束 —— 那才是 ``-L`` 的语义，评审时也确认过
        这一点。这里仍然选「停 = 真停」，因为面板把停掉的规则整行画成没有连接
        （连接数列显示「—」），用户据此认为这条规则已经彻底不跑了；内核若继续
        转发，界面给出的「已停止」就与实际还在跑的数据相反，而用户没有别的
        办法看出这个差别。代价是长连接（例如已登录的数据库会话）在停规则时被
        硬切、需要用户自己重连 —— 这是为「界面承诺兑现」付的账。

        用「关 socket」而不是让泵线程去轮询 ``_stop``：泵线程阻塞在 ``recv``
        上，只有对端被关掉才会醒，轮询标志救不了已经卡住的读。
        """
        deadline = time.monotonic() + 1.5     # 与上面等 accept 线程同一预算
        while True:
            with self._lock:
                conns = list(self._conns.values())
            if not conns:
                return
            for conn in conns:
                _close(conn.client)
                _close(conn.channel)
            for conn in conns:
                left = deadline - time.monotonic()
                if left > 0 and conn.thread is not None:
                    conn.thread.join(timeout=left)
            # 到点还没退干净的连接不追了：它自己会从账本里摘掉，
            # active_connections 随之回落。
            if time.monotonic() >= deadline:
                return

