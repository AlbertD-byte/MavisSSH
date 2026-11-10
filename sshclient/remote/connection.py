"""SSH connection management.

Wraps :mod:`paramiko` to provide a clean, thread-safe interface for the UI
to open interactive shells, SFTP sessions, port forwardings, etc.
"""
from __future__ import annotations

import binascii
import os
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import paramiko

from ..core import tunnel
from ..core.logs import get_logger
from ..core.tunnel import TunnelSpec
from . import hostkeys
from .hostkeys import HostKeyRequest, PromptHostKeyPolicy, load_host_keys

log = get_logger("mavisssh.connection")

#: 主动禁用的弱算法。类别名是 paramiko Transport 的规范名：
#: ``kex`` / ``host_key`` / ``encryption`` / ``mac`` / ``compression``。
#:
#: **paramiko 5.0.0 的默认偏好已经不错**（kex 无 sha1、host_key 无
#: ``ssh-rsa``/``ssh-dss``），但 ``3des-cbc`` 和四个 MD5/SHA-1 的 MAC
#: 还在默认里，所以要显式禁掉。
#:
#: ⚠ **只能砍掉更弱的替代项，不能砍掉唯一的强项。** paramiko #2126 上有人
#: 为绕开 #2083 写了个「禁用 GEX」的 workaround，结果在 Win/macOS（没有
#: ``/etc/ssh/moduli``）上把与旧客户端的 KEX 交集清空 → 握手直接失败。
#: 所以 ``curve25519-sha256@libssh.org`` / ``ecdh-sha2-*`` /
#: ``diffie-hellman-group14-sha256`` / ``diffie-hellman-group-exchange-sha256``
#: **一个都不许进这个列表**。
_DISABLED_ALGORITHMS: dict[str, tuple[str, ...]] = {
    # KEX 的 SHA-1 家族。5.0.0 的 _preferred_kex 里已无，仍写上以防回退。
    "kex": (
        "diffie-hellman-group1-sha1",
        "diffie-hellman-group14-sha1",
        "diffie-hellman-group-exchange-sha1",
    ),
    # 主机密钥的 SHA-1 签名 / DSA
    "host_key": ("ssh-rsa", "ssh-dss"),
    # 64 位块或已退役的对称加密
    "encryption": ("3des-cbc", "arcfour", "arcfour256",
                   "blowfish-cbc", "cast128-cbc", "des-cbc"),
    # MD5 / SHA-1 的 MAC
    "mac": ("hmac-md5", "hmac-md5-96", "hmac-sha1", "hmac-sha1-96"),
}

#: 本地端口被占时最多**尝试**多少个端口。20 次足够把一段连续的数据库端口
#: 跳过去，又不至于把用户带到一个完全无关的号上（65535 之前随机撞号）。
#:
#: **注意这是「尝试次数」，不是「往后顺延多少个」。** 循环是
#: `for step in range(_PORT_SCAN_LIMIT)`，候选为 `local_port + step`
#: 且 `step == 0` 时就是原端口，所以最多往后顺延 ``19`` 个。
#: （这里原来写的是「顺延 20 个」，与代码差 1 —— `add_tunnel` 的 docstring
#: 说的是「上限 N 次」，那份是对的。）
_PORT_SCAN_LIMIT = 20

#: SFTP 通道的**静默**超时（秒）。见 :meth:`SSHConnection.open_sftp` 里那段
#: 长注释 —— 为什么要设、为什么是「静默」而不是「总时长」、为什么必须预先设、
#: 为什么是 8 而不是 20，都写在那里。**改这个数之前先读那段。**
_SFTP_STALL_SECONDS = 8.0


class SSHAuthError(Exception):
    """Raised when authentication fails (bad password / key, etc)."""


class SSHConnectError(Exception):
    """Raised when the connection itself fails (host unreachable, timeout)."""


class SSHHostKeyChangedError(SSHConnectError):
    """Raised when the host key **changed** relative to what we recorded.

    **必须与普通的连接失败分开。** 「密钥变了」既可能是服务器重装了，
    也可能是**中间人攻击** —— 混成「连接失败」的话，用户只会去查网络，
    而真正的风险（有人在中间读你的流量和口令）就悄无声息地过去了。

    继承自 :class:`SSHConnectError` 是为了不破坏既有调用方对它的假设
    （``except SSHConnectError`` 仍然能接住），但 UI 层要**单独**处理它。
    """


class SSHNetworkError(SSHConnectError):
    """Raised when the host could not be reached at all.

    与「连上了但认证没过」「服务器主动拒绝」分开：这三类的**下一步动作
    完全不一样** —— 网络不可达要去查网络/防火墙，认证失败要改口令，
    服务器拒绝要问管理员。混成一句「无法建立 SSH 连接」的话，用户
    只能靠猜。
    """


class SSHServerRefusedError(SSHConnectError):
    """Raised when the server answered but refused to serve us.

    比如 ``sshd`` 的 ``MaxStartups`` 限流、``AllowUsers`` 拒绝、
    或者认证方式不被允许 —— 与「连不上」是两回事。
    """


class SSHHostKeyRejectedError(SSHConnectError):
    """Raised when the **user declined** to trust an unknown host.

    这**不是**「网络不可达」—— 主机是通的，密钥也对得上（paramiko 把
    异常抛出来了），只是用户点了「拒绝」。

    分开是因为**下一步动作完全不同**：网络不可达要去查网络/防火墙，
    而用户拒绝意味着「他看了指纹、觉得不对」—— **这恰恰是中间人攻击
    想让用户看到的现象**，所以绝对不能被写成一句「连不上，请检查网络」。
    """


@dataclass
class AuthConfig:
    """User-supplied authentication parameters."""

    host: str
    port: int = 22
    username: str = ""
    # Password
    password: str | None = None
    # SSH key
    key_filename: str | None = None
    key_passphrase: str | None = None
    # Misc
    timeout: float = 15.0
    keepalive: int = 20
    auth_timeout: float | None = None
    # **没有 proxy_command。** 原来有这个字段，接到 `paramiko.ProxyCommand` ——
    # 而那玩意内部是 `Popen(cmd, shell=True)`，**本机任意命令执行**。
    # 它当时是死代码（生产侧没有任何地方给它赋值），但死代码不等于安全：
    # 这个字段是「迟早要接」的（内网跳板机场景），而 sessions.json 在用户
    # 目录、还会被拷贝导入 —— 哪天有人给它接上导入路径，就是一个远程执行面。
    # 真要支持跳板机，正确形态是 **argv 列表 + 自己 Popen(shell=False)**，
    # 不是自由文本。届时重新加这个字段，并让 UI 原文回显待执行命令再确认。
    allow_agent: bool = False  # do not silently pull from ssh-agent by default


def _auth_kind(cfg: AuthConfig) -> str:
    """给日志用的认证方式描述。**只看用哪个字段，不碰值本身。**"""
    if cfg.key_filename:
        return "key" if not cfg.password else "key+password"
    if cfg.password:
        return "password"
    if cfg.allow_agent:
        return "agent"
    return "none"


@dataclass
class ConnectionInfo:
    """Metadata describing an active connection."""

    host: str
    port: int
    username: str
    fingerprint: str = ""


@dataclass
class _Rule:
    """A configured forwarding rule and whether it is currently switched on.

    ``forward`` stays ``None`` until the rule is first enabled, so a rule saved
    while disabled costs nothing and holds no socket.
    """

    spec: TunnelSpec
    enabled: bool = False
    forward: tunnel.Forward | None = None


def server_fingerprint(transport: Any) -> str:
    """远端主机指纹，人可读的 ``SHA256:...``。取不到就返回空串。

    **别用 `paramiko.util.hexlify`：paramiko 5.0 里那个名字已经没了。**
    实测真连一台 OpenSSH 9.6 时，原先内联的那句抛 AttributeError，又被
    外面的 `except: pass` 静默吞掉，于是指纹**一直**是空的，日志里长期打
    「主机指纹=未知」而没有任何报错。新版直接给 `key.fingerprint`，老版本
    才退回自己算 MD5 十六进制。

    抽成函数是为了能离线测：这一条曾经只靠一句日志证明它活着，而那句日志
    恰恰是它失效时唯一没变的东西。
    """
    try:
        key = transport.get_remote_server_key()
    except Exception as exc:  # noqa: BLE001
        log.debug("取主机指纹失败：%s: %s", type(exc).__name__, exc)
        return ""
    fp = getattr(key, "fingerprint", None)
    if fp:
        return str(fp)
    try:
        # 老版本没有 .fingerprint，只有 MD5 字节。**用 binascii 而不是
        # paramiko.util.hexlify** —— 后者在 paramiko 5 里已经没了，所以照旧
        # 写的话这条回退路径在真机上同样是死的（只是被 except 盖住看不出来）。
        return binascii.hexlify(key.get_fingerprint()).decode()
    except Exception as exc:  # noqa: BLE001
        log.debug("取主机指纹失败：%s: %s", type(exc).__name__, exc)
        return ""


class SSHConnection:
    """Holds a :class:`paramiko.SSHClient` and helpers around it."""

    def __init__(self, info: ConnectionInfo):
        self._info = info
        self._client: paramiko.SSHClient | None = None
        self._sftp: paramiko.SFTPClient | None = None
        self._lock = threading.RLock()
        # 单独一把锁串行化 exec_command。paramiko 的 Transport.open_channel
        # 不是并发安全的，而资源监控每 2 秒就在一个新线程上开一次 channel；
        # 两次轮询撞在一起会让传输层状态错乱（对端回 MSG_IGNORE 指向一个
        # 它那边已经不存在的 channel，整条连接就掉了）。
        self._exec_lock = threading.Lock()
        # local_port -> configured rule. Guarded by _lock.
        self._tunnels: dict[int, _Rule] = {}
        self._on_tunnel_error: Callable[[str], None] | None = None
        self._on_tunnel_event: Callable[[Any, str], None] | None = None

    # ------------------------------------------------------------------ public

    @property
    def info(self) -> ConnectionInfo:
        return self._info

    @property
    def client(self) -> paramiko.SSHClient:
        if self._client is None:
            raise RuntimeError("connection not open")
        return self._client

    @property
    def is_open(self) -> bool:
        return self._client is not None and self._client.get_transport() is not None

    def open_shell(self, term: str = "xterm-256color", cols: int = 80, rows: int = 24) -> paramiko.Channel:
        """Open an interactive shell channel.

        Returns the underlying :class:`paramiko.Channel`; reading/writing the
        channel drives the terminal.
        """
        ch = self.client.get_transport().open_session(timeout=10)
        ch.get_pty(term=term, width=cols, height=rows)
        ch.invoke_shell()
        return ch

    def open_sftp(self) -> paramiko.SFTPClient:
        with self._lock:
            if self._sftp is None:
                self._sftp = self.client.open_sftp()
                # **给 SFTP 通道一个静默超时 —— 这是全盘唯一的上界来源。**
                #
                # paramiko 的 `_read_response` 是 `while True: _read_packet()`，
                # `_read_all` 对 Channel 走 `sock.recv(n)`，**原本没有任何预算**。
                # 于是黑洞型断网（对端丢包不回 RST）会让 `put` 无限期吊在 recv 上；
                # 而 `upload()` 的主体跑在 `with self._held():` 里，锁也就永不放，
                # 面板的 `listdir` / 刷新 / 后续传输**整个哑到会话结束**。
                #
                # 通道读写两侧都读 `Channel.timeout`：`recv` 是
                # `in_buffer.read(n, self.timeout)`，写侧 `_wait_for_send_window`
                # 同理。所以**一处设置同时管住读写**。
                #
                # **是「静默」上限，不是「总时长」上限。** 每次 recv 各自计时，
                # 数据一来即重置 —— 慢速大文件（评审实测 6 次请求各 1.5s RTT、
                # 总 9.62s）不会被掐。
                #
                # **必须在这里预先设，不能事后救。** `BufferedPipe.read` 在
                # **进入时**捕获 timeout 参数：实测「挂住 1 秒后再
                # `settimeout(3.0)`，30 秒仍未返回、锁也没放开」。
                #
                # **8 而不是 20。** pipelining 下等待是「队列深度 × 单请求
                # 延迟」而不是单请求延迟：评审实测每请求 2.7s 的**健康**服务器，
                # 4MB 上传 9.88s 就被掐 —— 0.9N 已经出事，N=20 也只买到 1.0s/请求。
                # 评审实测用户可见的哑窗口是 **2N**（put 的 N + 清理线程再持锁 N），
                # 取 8 是「哑 16s、失败上界 40s」这个平衡。
                #
                # **不打��兄弟通道**：同一 Transport 上的另外两条 channel 的
                # `timeout` 仍是 None（评审实测），所以隧道转发
                # （`tunnel.py` 明确 `settimeout(None)`）与 ZMODEM 走的 shell
                # channel 都不受影响。
                self._sftp.get_channel().settimeout(_SFTP_STALL_SECONDS)
        return self._sftp

    def open_direct_channel(self, host: str, port: int) -> paramiko.Channel:
        """Open a ``direct-tcpip`` channel to ``host:port`` *via* this session.

        This is the outbound half of a forwarding rule: paramiko asks the
        server to make the connection and hands the bytes back to us.
        """
        transport = self.client.get_transport()
        return transport.open_channel("direct-tcpip", (host, port), ("127.0.0.1", 0))

    # ------------------------------------------------------------------ tunnels

    def set_tunnel_callbacks(
        self,
        on_error: Callable[[str], None] | None = None,
        on_event: Callable[[Any, str], None] | None = None,
    ) -> None:
        """Install callbacks for tunnel failures and state changes.

        Both are invoked from tunnel worker threads, so the UI side must hop
        back onto the Qt thread before touching widgets.
        """
        self._on_tunnel_error = on_error
        self._on_tunnel_event = on_event

    def add_tunnel(self, spec: TunnelSpec, enabled: bool = True) -> TunnelSpec:
        """Register one forwarding rule.

        With ``enabled=False`` the rule is remembered but no listener is bound,
        so the local port stays free until it is switched on. The listener is
        bound synchronously when enabling, so a port clash surfaces here rather
        than in a background thread.

        **返回的是实际绑上的那条 spec**，它可能已经不是传进来的那个了 ——
        端口被占时见 :meth:`_start_rule` 的自动改端口。调用方要拿返回值
        去回写持久化，否则用户存的配置和实际监听的端口会长期不一致。
        """
        spec = tunnel.validate(spec)
        with self._lock:
            if spec.local_port in self._tunnels:
                raise tunnel.TunnelError(f"本地端口 {spec.local_port} 已被占用")
            rule = _Rule(spec)
            if enabled:
                try:
                    spec = self._start_rule(rule)
                except Exception:
                    # Do not leave a half-registered rule behind when the bind
                    # fails: the caller sees a clean failure either way.
                    raise
            # **key 用的是实际绑上的端口。** 先插再改会留下一条 key 指向旧
            # 端口的僵尸项，remove_tunnel / set_tunnel_enabled 就再也找不到
            # 它，而监听端口是占着的 —— 那个端口再也放不掉。
            self._tunnels[spec.local_port] = rule
        return spec

    def set_tunnel_enabled(self, local_port: int, enabled: bool) -> bool:
        """Switch one rule on or off, keeping its configuration.

        Returns the state the rule ended up in. Switching off releases the
        local port; switching on rebinds it and can fail if something else
        took the port in the meantime, in which case the rule stays off.

        **开启时可能自动改端口**（同 :meth:`_start_rule`），改了就重新 key。
        """
        with self._lock:
            rule = self._tunnels.get(local_port)
            if rule is None:
                raise tunnel.TunnelError(f"没有本地端口为 {local_port} 的转发规则")
            if enabled == rule.enabled:
                return rule.enabled
            if enabled:
                actual = self._start_rule(rule)   # may raise; rule stays disabled
                if actual.local_port != local_port:
                    del self._tunnels[local_port]
                    self._tunnels[actual.local_port] = rule
            else:
                self._stop_rule(rule)
            return rule.enabled

    def remove_tunnel(self, local_port: int) -> bool:
        """Forget one rule entirely. Returns True if it existed."""
        with self._lock:
            rule = self._tunnels.pop(local_port, None)
        if rule is None:
            return False
        if rule.forward is not None:
            rule.forward.stop()
            rule.forward = None
        return True

    def tunnel_specs(self) -> list[TunnelSpec]:
        """All configured rules, in the order they were added."""
        with self._lock:
            return [r.spec for r in self._tunnels.values()]

    def tunnel_states(self) -> dict[int, bool]:
        """Map local_port -> whether the rule is currently switched on."""
        with self._lock:
            return {p: r.enabled for p, r in self._tunnels.items()}

    def tunnel_connections(self) -> dict[int, int]:
        """Map local_port -> live connections; disabled rules report 0."""
        with self._lock:
            return {p: (f.active_connections if (f := r.forward) else 0)
                    for p, r in self._tunnels.items()}

    def _start_rule(self, rule: _Rule) -> TunnelSpec:
        """Bind the listener for *rule*. Caller holds the lock.

        **绑不上就往后顺延端口，并把改过的端口回写进 ``rule.spec``。**
        返回真正绑上的那条 spec。

        为什么要这个：两条不同的会话（比如生产库和预发库）都会照惯例写
        13306 指向自己那台。原来的行为是后连的那个直接绑不上，规则留在
        界面上却**一个端口都不监听** —— 用户看到规则是「开」的，实际没生效，
        而且没有任何提示说清是谁占了。现在自动改到 13307 并回写，
        两条能同时跑，端口号也确实变成可用的那个。

        **顺延而不是「探一个空闲端口再绑」**：探测和绑定之间有窗口，
        别的进程随时可能插进来，那就会变成「显示 13307、实际绑的是别的」。
        这里的做法是**真的去 bind**，绑上了就是它的，不存在这个窗口。

        上限 :data:`_PORT_SCAN_LIMIT` 次；全撞了就抛最后一次的错误 ——
        宁可让用户看到真实原因，也不静默改到一个离谱的端口。
        """
        if rule.forward is not None:
            rule.forward.start()      # 重启同一条规则，端口不会再变
            rule.enabled = True
            return rule.spec

        first_exc: Exception | None = None
        for step in range(_PORT_SCAN_LIMIT):
            spec = rule.spec
            if step:
                candidate = spec.local_port + step
                if candidate > 65535:
                    break
                spec = replace(spec, local_port=candidate)
            fwd = tunnel.Forward(
                spec, self.open_direct_channel,
                self._on_tunnel_error, self._on_tunnel_event,
            )
            try:
                fwd.start()
            except tunnel.TunnelError as exc:
                if first_exc is None:
                    first_exc = exc
                continue
            # 绑上了才写回 —— 失败的候选端口不能留在 spec 里。
            rule.spec = spec
            rule.forward = fwd
            rule.enabled = True
            return spec
        raise first_exc or tunnel.TunnelError(
            f"无法监听 {rule.spec.listen_host}:{rule.spec.local_port}")

    @staticmethod
    def _stop_rule(rule: _Rule) -> None:
        if rule.forward is not None:
            rule.forward.stop()
        rule.enabled = False

    def _stop_all_tunnels(self) -> None:
        with self._lock:
            forwards = list(self._tunnels.values())
            self._tunnels.clear()
        for rule in forwards:
            if rule.forward is not None:
                try:
                    rule.forward.stop()
                except Exception:
                    pass
            rule.forward = None
            rule.enabled = False

    def exec(self, command: str, timeout: float | None = 10.0) -> tuple[str, str]:
        """Run *command* and return (stdout, stderr).

        Useful for resource monitoring probes.

        channel **一定要自己关掉**：``exec_command`` 超时抛异常时 paramiko 不会
        收尾，留下一个半死不活的 channel。之后对端还在往这个 channel 上发
        东西，两边编号对不上，传输层就会收到 "ieof packet referred to
        nonexistent channel N" 并把整条连接拆掉。
        """
        with self._exec_lock:
            chan = None
            try:
                stdin, stdout, stderr = self.client.exec_command(command, timeout=timeout)
                chan = stdout.channel
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                return out, err
            finally:
                if chan is not None:
                    try:
                        chan.close()
                    except Exception:
                        pass

    def close(self) -> None:
        self._stop_all_tunnels()
        with self._lock:
            try:
                if self._sftp is not None:
                    self._sftp.close()
            except Exception:
                pass
            try:
                if self._client is not None:
                    self._client.close()
            except Exception:
                pass
            self._sftp = None
            self._client = None
        # _info 在 __init__ 里就一定存在，所以这里不必再判空。
        log.info("已断开  %s@%s:%d",
                 self._info.username, self._info.host, self._info.port)

    def __enter__(self) -> SSHConnection:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ factory

    @staticmethod
    def connect(cfg: AuthConfig,
                host_key_prompt: Callable[[HostKeyRequest], str] | None = None,
                config_dir: Path | None = None,
                use_system_hosts: bool = True) -> SSHConnection:
        """Open a new SSH connection using *cfg*.

        Translates paramiko's various exception types into our own so the UI
        can show a friendly message.

        *host_key_prompt* —— 遇到**未知主机**时的回调，返回
        :data:`hostkeys.TRUST` / :data:`hostkeys.TRUST_ONCE` /
        :data:`hostkeys.REJECT`。传 None 则一律**拒绝**（等价
        ``RejectPolicy``）。

        *config_dir* —— 信任库所在目录。默认是用户配置目录
        （``%APPDATA%\\MavisSSH``）。**测试要传临时目录** ——
        否则真机验证会把测试痕迹写进用户真实的 known_hosts，
        而且「密钥变更」那一步得去改用户的文件。

        *use_system_hosts* —— 是否读用户 ``~/.ssh/known_hosts``。默认 True
        （复用用户既有信任是设计意图）。**测试要传 False** ——
        真机验证要测「未知主机」那条路径，而用户 known_hosts 里往往
        已经有那台机器；不隔离的话 paramiko 直接匹配上了，
        ``missing_host_key`` **根本不会被调用**，测出来的「没问」是假象。

        **这个回调会在连接工作线程上被同步调用**（paramiko 的
        ``MissingHostKeyPolicy.missing_host_key()`` 是同步的）。所以它
        **不能在那个线程里弹 Qt 控件** —— UI 层要自己用
        ``QMetaObject.invokeMethod(..., QueuedConnection)`` 把问题送到主线程，
        再在这里等结果。详见 ``main_window.py`` 的 ``_ask_host_key``。
        """
        client = paramiko.SSHClient()

        # ---- 主机密钥：先装信任库，再定策略 ----
        #
        # 原来是 `set_missing_host_key_policy(paramiko.AutoAddPolicy())`，
        # **且既没 `load_system_host_keys()` 也没 `load_host_keys()`**：
        # 用户 `~/.ssh/known_hosts` 里的条目根本没被读进来，paramiko 连
        # 「比对」这一步都不存在；而 AutoAddPolicy 又对未知主机无条件接受。
        # 结果是任何能在这条网络路径上冒充主机的人（ARP/DNS 欺骗、恶意网关、
        # 被劫持的 Wi-Fi）都能完成一次中间人攻击，**静默地**读走会话流量，
        # 用密码认证时还能顺手收走口令，界面上没有任何异常。
        #
        # 顺带一提：**paramiko 5.0 的默认策略本来就是 `RejectPolicy`**
        # （`SSHClient.__init__` 里 `self._policy = RejectPolicy()`）——
        # 那个 MITM 是我们自己主动降级出来的，不是 paramiko 的行为。
        #
        # 现在：装两个信任库（用户那份只读，我们那份读写），
        # 未知主机问用户。
        load_host_keys(client, config_dir, use_system=use_system_hosts)

        if host_key_prompt is not None:
            # **写不写盘由 prompt 的返回值决定**（TRUST / TRUST_ONCE / REJECT），
            # 不是在这里预先选策略类 —— 「仅此一次」是用户**在弹窗里**才
            # 做出的决定，那时握手已经开始了，策略类早就选好了。
            client.set_missing_host_key_policy(PromptHostKeyPolicy(host_key_prompt))
        else:
            # 没给回调 = 没人能回答「要不要信」→ 只能拒。
            # 静默接受是最糟的退路。
            client.set_missing_host_key_policy(paramiko.RejectPolicy())

        # **恒为 None。** 保留这个形参是因为它是 paramiko `connect()` 的公开
        # 参数，将来要接跳板机时用得上（届时要传的是自己 Popen 出来的
        # socket，不是 `paramiko.ProxyCommand` —— 那玩意是 `shell=True`）。
        sock: socket.socket | None = None
        try:
            # **不再有 proxy_command 分支。** 见 AuthConfig 里那段说明 ——
            # `paramiko.ProxyCommand` 内部是 `Popen(cmd, shell=True)`，
            # 留着这个字段就是一个等着被接上的本机命令执行面。
            client.connect(
                hostname=cfg.host,
                port=cfg.port,
                username=cfg.username,
                password=cfg.password,
                key_filename=cfg.key_filename,
                passphrase=cfg.key_passphrase,
                timeout=cfg.timeout,
                auth_timeout=cfg.auth_timeout,
                allow_agent=cfg.allow_agent,
                look_for_keys=False,  # we manage key auth explicitly
                sock=sock,
                disabled_algorithms=_DISABLED_ALGORITHMS,
            )
        except hostkeys.HostKeyRejected as exc:
            # **必须排在 SSHException 之前** —— HostKeyRejected 是它的子类。
            # 「用户拒绝」不是「网络不可达」：主机是通的，密钥也对得上，
            # 只是用户点了拒绝。写成「请检查网络和防火墙」会把人带偏。
            client.close()
            log.info("用户拒绝接受主机密钥  %s@%s:%d",
                     cfg.username, cfg.host, cfg.port)
            raise SSHHostKeyRejectedError(str(exc)) from exc
        except paramiko.BadHostKeyException as exc:
            # ⚠ **必须排在 `except (paramiko.SSHException, OSError)` 之前。**
            #   `BadHostKeyException` 是 `SSHException` 的**子类**（已核实 MRO），
            #   所以不单独接住的话，它会被下面那个 except 吞成一句泛泛的
            #   「连接失败」—— 而「密钥变了」既可能是服务器重装，也可能是
            #   **中间人攻击**。混在一起，用户只会去查网络，真正的风险就
            #   悄无声息地过去了。
            client.close()
            # **属性名是 `key`，不是 `got_key`。** paramiko 的
            # `BadHostKeyException.__init__(hostname, got_key, expected_key)`
            # 把 got_key 存成 `self.key` —— 写 `got_key` 的话日志里
            # 永远是「实际=None」，而那正是**最该看见**的那个值。
            log.error("主机密钥已变更  %s@%s:%d  期望=%s  实际=%s",
                      cfg.username, cfg.host, cfg.port,
                      getattr(exc, "expected_key", None),
                      getattr(exc, "key", None))
            raise SSHHostKeyChangedError(
                "%s:%d 的主机密钥与已记录的不一致。"
                "可能是服务器重装或密钥轮换，也可能是中间人攻击 —— "
                "请先与管理员核对指纹再决定是否更新。"
                % (cfg.host, cfg.port)) from exc
        except paramiko.AuthenticationException as exc:
            client.close()
            # 只记「是谁、用的哪种认证」，绝不记密码或私钥口令。
            log.warning("认证失败  %s@%s:%d  方式=%s  原因=%s",
                        cfg.username, cfg.host, cfg.port,
                        _auth_kind(cfg), exc or "authentication failed")
            raise SSHAuthError(str(exc) or "authentication failed") from exc
        except (paramiko.SSHException, OSError) as exc:
            client.close()
            log.warning("连接失败  %s@%s:%d  原因=%s",
                        cfg.username, cfg.host, cfg.port, exc or "connection failed")
            # **分类到「网络不可达」还是「服务器拒绝」。**
            #
            # paramiko 的异常面很窄（`SSHException` 下面只有
            # `AuthenticationException` / `BadHostKeyException` /
            # `ChannelException` 等几个），「连不上」与「连上了但不给你
            # 开门」都落在 `SSHException` 或 `OSError` 里。所以只能按
            # **消息内容**细分 —— 不好，但比让用户对着一句
            # 「Error reading SSH protocol banner」猜要强。
            #
            # 两类的**下一步动作完全不同**：网络不可达 → 查网络/防火墙；
            # 服务器拒绝 → 问管理员（可能是 MaxStartups 限流或 AllowUsers）。
            text = str(exc or "")
            low = text.lower()
            refused = (
                "banner" in low
                or "kex" in low
                or "negoti" in low
                or "not permitted" in low
                or "maxstartups" in low
            )
            if refused:
                raise SSHServerRefusedError(
                    "%s:%d 拒绝了这次连接：%s\n\n"
                    "服务器有响应但不接受这次连接 —— 常见原因是 sshd 的 "
                    "MaxStartups 限流、AllowUsers/DenyUsers 拒绝，"
                    "或者协商不出共同的加密算法。"
                    % (cfg.host, cfg.port, text or "无详细信息")) from exc
            raise SSHNetworkError(
                "%s:%d 连不上：%s\n\n"
                "请检查网络是否可达、端口是否正确、是否有防火墙拦着。"
                % (cfg.host, cfg.port, text or "无详细信息")) from exc

        transport = client.get_transport()
        try:
            transport.set_keepalive(cfg.keepalive)
        except Exception:
            pass

        info = ConnectionInfo(
            host=cfg.host,
            port=cfg.port,
            username=cfg.username,
            fingerprint=server_fingerprint(transport),
        )

        conn = SSHConnection(info)

        conn = SSHConnection(info)
        conn._client = client
        log.info("已连接  %s@%s:%d  方式=%s  主机指纹=%s",
                 cfg.username, cfg.host, cfg.port, _auth_kind(cfg),
                 info.fingerprint or "未知")
        return conn


# ---------------------------------------------------------------------------
# Helpers


def load_private_key(path: str, passphrase: str | None = None) -> paramiko.PKey:
    """Load an SSH private key from *path* trying common formats.

    Raises :class:`paramiko.PasswordRequiredException` if a passphrase is
    needed and none was given.
    """
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(path)
    last_err: Exception | None = None
    for loader in (
        paramiko.RSAKey.from_private_key_file,
        paramiko.Ed25519Key.from_private_key_file,
        paramiko.ECDSAKey.from_private_key_file,
        paramiko.DSSKey.from_private_key_file,
    ):
        try:
            return loader(path, password=passphrase)
        except paramiko.PasswordRequiredException:
            raise
        except paramiko.SSHException as exc:
            last_err = exc
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
    raise paramiko.SSHException(f"could not parse private key: {last_err}")
