"""主机密钥（known_hosts）管理。

**本模块不依赖 Qt。** 它只认一个 ``Callable[[HostKeyRequest], bool]`` ——
「问不问用户、用户答了什么」由 UI 层决定。这里管的是「怎么算指纹、
怎么存、怎么比对」。

为什么需要这个模块
------------------

原实现是 ``client.set_missing_host_key_policy(paramiko.AutoAddPolicy())``，
外加**既没** ``load_system_host_keys()`` **也没** ``load_host_keys()**。
后果有两层：

1. paramiko **连"比对"这一步都不存在** —— 用户 ``~/.ssh/known_hosts`` 里
   已有的条目根本没被读进来。
2. ``AutoAddPolicy`` 对未知主机是「无条件接受」。能在这条网络路径上冒充
   该主机的人（ARP/DNS 欺骗、恶意网关、被劫持的 Wi-Fi）就能完成一次
   中间人攻击，**静默地**读走所有会话流量；用户用密码认证时还能顺手收走
   口令。界面上不会有任何异常。

顺带一提：``paramiko 5.0`` 的 ``SSHClient.__init__`` 里默认就是
``self._policy = RejectPolicy()`` —— **paramiko 本来是安全的**，MITM 是
我们主动降级出来的。

信任库：双轨读、单轨写
----------------------

=========================  ====  ====  ==================================
文件                       读    写    理由
=========================  ====  ====  ==================================
``~/.ssh/known_hosts``     是    否    用户 OpenSSH/PuTTY 认可的事实。
                                   读它能直接复用既有信任、免掉一次确认；
                                   不写它是不想去碰用户 home 下的权限
                                   （域环境重定向常让 ``.ssh`` 不可写），
                                   也不和用户自己的严格策略互相覆盖。
``%APPDATA%/MavisSSH/``    是    是    MavisSSH 自己加的信任。与
``known_hosts``                   ``sessions.json`` / ``settings.json`` /
                                   ``secret.key`` 同目录，符合「一个配置
                                   目录」的心智，卸载即清空。
=========================  ====  ====  ==================================

格式用 **OpenSSH 格式**：换来 ``ssh-keygen -F`` / ``ssh-keyscan`` 互通，
用户能自己看、自己导入。paramiko 的 ``HostKeys`` 直接支持读写该格式
（已实测：``hk.save()`` 产出 OpenSSH 格式，``hk.load()`` 能读回）。

写入注意
--------

**必须用 ``client.save_host_keys()`` 而不是 ``HostKeys.save()``。**
前者写盘之前会**重读一次文件**以合并别的 SSH 客户端的并发写入；
后者直接覆盖，会把别人的条目弄丢。
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import paramiko

from ..core.settings import CONFIG_DIR

#: MavisSSH 自己的信任库。与 sessions.json 同目录。
KNOWN_HOSTS_FILENAME = "known_hosts"


def known_hosts_path(config_dir: Path | None = None) -> Path:
    """MavisSSH 自己的 known_hosts 路径。"""
    base = Path(config_dir) if config_dir is not None else Path(CONFIG_DIR)
    return base / KNOWN_HOSTS_FILENAME


def sha256_fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH 的 SHA256 指纹（``ssh-keygen -lf`` 那一串）。

    形如 ``SHA256:hXQ2mVQ0hkFmXK1oQ5X2Q0e1M7vJ2xZ0rTgN9pLdC4s``。

    **用 SHA256 而不是 MD5**：MD5 只出现在老工单/老截图里，而且它本身
    已被证明可碰撞 —— 拿它当信任凭据是不安全的。MD5 只作为**兼容行**
    附在下面（见 :func:`md5_fingerprint`），让用户能核对历史资料。
    """
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def md5_fingerprint(key: paramiko.PKey) -> str:
    """MD5 指纹，**冒号分隔**（``aa:bb:cc:…``）。

    冒号分隔是 PuTTY / WinSCP 的格式。历史上这里返回的是**无分隔符的
    32 位 hex**（``aabbcc…``）—— 那样用户拿手上的资料**没法目视比对**，
    因为大家的资料都是带冒号的。

    MD5 只作兼容行，不作信任凭据。
    """
    raw = key.get_fingerprint()
    hexs = "".join("%02x" % b for b in raw)
    return ":".join(hexs[i:i + 2] for i in range(0, len(hexs), 2))


def extract_port(hostname: str) -> int:
    """从 known_hosts 键名取端口。

    OpenSSH 的约定是「非 22 端口用 ``[host]:port`` 括起来」。
    """
    if hostname.startswith("[") and "]:" in hostname:
        try:
            return int(hostname.rsplit("]:", 1)[1])
        except ValueError:
            pass
    return 22


@dataclass(frozen=True)
class HostKeyRequest:
    """「这台主机我��认识，要不要信？」的一次请求。

    字段说明：

    - ``hostname`` —— paramiko 传进来的键名，**原样使用**做 known_hosts 键。
      自己拼会与 paramiko 内部的查表不对称（非 22 端口时尤其容易错）。
    - ``key_type`` —— ``ssh-ed25519`` / ``rsa-sha2-512`` 之类。
    - ``fingerprint`` —— SHA256 指纹。
    - ``key`` —— paramiko 的公钥对象，**接受时要拿它写进 known_hosts**。
    """

    hostname: str
    port: int
    key_type: str
    fingerprint: str
    key: paramiko.PKey

    @property
    def md5_fingerprint(self) -> str:
        """MD5 兼容行（冒号分隔）。"""
        try:
            return md5_fingerprint(self.key)
        except Exception:      # noqa: BLE001 —— 取不到就当没有，不该炸掉整个连接
            return ""

    def display(self) -> str:
        """给用户看的多行文本（确认框里用）。"""
        lines = [
            "主机：%s" % self.hostname,
            "类型：%s" % self.key_type,
            "指纹：%s" % self.fingerprint,
        ]
        md5 = self.md5_fingerprint
        if md5:
            lines.append("      MD5（兼容旧资料）：%s" % md5)
        return "\n".join(lines)


#: 用户的信任决策。
#:
#: - ``TRUST`` —— 信任并记住（写进 known_hosts）
#: - ``TRUST_ONCE`` —— 仅此一次（**不写盘**，下次连接还会问）
#: - ``REJECT`` —— 拒绝
TRUST = "trust"
TRUST_ONCE = "trust_once"
REJECT = "reject"


class HostKeyRejected(paramiko.SSHException):
    """用户拒绝了未知主机的主机密钥。

    单独一个类型（而不是笼统的 ``SSHException``）是为了让上层能把它
    翻译成「用户主动取消」而不是「网络不可达」—— 这两者的下一步动作
    完全不同，而「用户拒绝」这个现象**恰恰是中间人攻击想让人看到的**，
    写成「请检查网络和防火墙」会把人带偏。
    """


class PromptHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """未知主机 → 问用户；按决策决定写不写 known_hosts。

    ``prompt`` 收到 :class:`HostKeyRequest`，返回 :data:`TRUST` /
    :data:`TRUST_ONCE` / :data:`REJECT`。

    **「宽松模式」不要复用本类。** 写盘等于把当时的指纹固化成「可信」，
    之后这台机器被换钥就再也发现不了了 —— 那正是中间人攻击想要的效果。
    要「无条件接受」就另设一个显式的开关，不要走这条路。
    """

    def __init__(self, prompt: Callable[[HostKeyRequest], str]) -> None:
        self._prompt = prompt

    def missing_host_key(self, client, hostname, key):  # noqa: D102 — paramiko 接口
        req = HostKeyRequest(
            hostname=hostname,
            port=extract_port(hostname),
            key_type=key.get_name(),
            fingerprint=sha256_fingerprint(key),
            key=key,
        )
        decision = self._prompt(req)
        if decision not in (TRUST, TRUST_ONCE):
            # **抛专属异常，别让调用方把它当成「网络不可达」。**
            # 主机是通的、密钥也对得上（paramiko 把异常抛出来了），
            # 只是用户点了拒绝 —— 而「用户拒绝」恰恰是中间人攻击想让
            # 用户看到的现象，写成「请检查网络和防火墙」会把人带偏。
            raise HostKeyRejected(
                "您没有接受 %s 的主机密钥，连接已取消。\n\n"
                "指纹：%s（%s）\n\n"
                "如果这台主机确实是您要连的，可以重新连接并核对指纹后再信任。"
                % (hostname, req.fingerprint, req.key_type))
        if decision == TRUST_ONCE:
            # **「仅此一次」：刻意不写盘。** 这一条连接信任，下次连接
            # 仍然是未知主机，还会再问 —— 这正是「仅此一次」该有的行为。
            return
        # 写进「可写」那套（_host_keys）。system 那套是只读的。
        # 手法与 paramiko 自己的 AutoAddPolicy 相同。
        client._host_keys.add(hostname, key.get_name(), key)
        if client._host_keys_filename is not None:
            # ⚠ **必须先确保文件存在。**
            #
            # paramiko 的 `save_host_keys()` 内部会**先重读一次那个文件**
            # （为了合并别的 SSH 客户端的并发写入）：
            #
            #     def save_host_keys(self, filename):
            #         if self._host_keys_filename is not None:
            #             self.load_host_keys(self._host_keys_filename)   # ← 这里
            #         with open(filename, "w") as f: ...
            #
            # 而**第一次信任时那个文件还不存在** → `load_host_keys` 抛
            # `FileNotFoundError` → 整个握手失败，用户看到的是
            # 「连接失败」，而他明明点了「信任并记住」。
            #
            # 这个坑**离线判据测不到**（那些判据用的是内存表，
            # `save_host_keys` 不会被调到），是**真机联调**抓出来的。
            path = Path(client._host_keys_filename)
            if not path.exists():
                try:
                    path.touch()
                except OSError:
                    pass
            client.save_host_keys(client._host_keys_filename)


class AcceptOnceHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """未知主机 → 问用户；**接受就一定不写盘**。

    保留这个类是为了「完全不写盘」这种用法：调用方明确知道这台主机
    只信任这一次，连 ``_host_keys`` 内存表里都不留。

    日常的「信任并记住 / 仅此一次 / 取消」三态请用
    :class:`PromptHostKeyPolicy` —— 它按用户的选择决定写不写盘。
    """

    def __init__(self, prompt: Callable[[HostKeyRequest], bool]) -> None:
        self._prompt = prompt

    def missing_host_key(self, client, hostname, key):  # noqa: D102 — paramiko 接口
        req = HostKeyRequest(
            hostname=hostname,
            port=extract_port(hostname),
            key_type=key.get_name(),
            fingerprint=sha256_fingerprint(key),
            key=key,
        )
        if not self._prompt(req):
            raise paramiko.SSHException(
                "用户未接受主机密钥：%s（%s %s）"
                % (hostname, req.key_type, req.fingerprint))
        # **刻意不写盘。** 这一次信任只对这一条连接有效。


@dataclass(frozen=True)
class TrustedHost:
    """信任库里的一条记录（给「已信任主机」面板用）。"""

    hostname: str
    key_type: str
    fingerprint: str

    def display(self) -> str:
        return "%s  %s  %s" % (self.hostname, self.key_type, self.fingerprint)


def list_trusted_hosts(config_dir: Path | None = None) -> list[TrustedHost]:
    """列出 MavisSSH 自己信任库里的条目。

    **只读自己的那份**（``%APPDATA%/MavisSSH/known_hosts``）——
    用户 ``~/.ssh/known_hosts`` 是他的，我们不列也不改。

    读不出来（文件不存在 / 格式坏）就返回空列表 —— 这是个管理面板，
    不该因为信任库有问题就打不开。
    """
    path = known_hosts_path(config_dir)
    if not path.is_file():
        return []
    out: list[TrustedHost] = []
    try:
        hk = paramiko.HostKeys(str(path))
    except Exception:      # noqa: BLE001 —— 面板不该因为文件坏而崩
        return []
    for hostname, keys in hk.items():
        for key_type, key in keys.items():
            try:
                fp = sha256_fingerprint(key)
            except Exception:      # noqa: BLE001
                fp = ""
            out.append(TrustedHost(hostname, key_type, fp))
    out.sort(key=lambda t: (t.hostname, t.key_type))
    return out


def remove_trusted_host(hostname: str, key_type: str | None = None,
                        config_dir: Path | None = None) -> bool:
    """从信任库里删掉一条（或某台主机的全部）记录。

    返回 True 表示确实删掉了什么。
    """
    path = known_hosts_path(config_dir)
    if not path.is_file():
        return False
    try:
        hk = paramiko.HostKeys(str(path))
    except Exception:      # noqa: BLE001
        return False
    # **用 `hk.items()`，不是 `hk.entries`。** paramiko 的 `HostKeys` 是
    # `MutableMapping`（`entries` 是 pycrypto 老版本的 API，paramiko 里
    # 没有这个属性）—— 写成 `hk.entries` 会 AttributeError。
    if hostname not in hk:
        return False
    if key_type:
        keys = hk.get(hostname)
        if not keys or key_type not in keys:
            return False
        del keys[key_type]
        if not keys:
            hk.pop(hostname, None)
    else:
        hk.pop(hostname, None)
    hk.save(str(path))
    return True


def import_known_hosts(src: Path, config_dir: Path | None = None,
                       hostnames: list[str] | None = None) -> list[str]:
    """从别的 known_hosts 文件里导入条目。

    *src* —— 来源文件（通常是用户的 ``~/.ssh/known_hosts``）。
    *hostnames* —— 要导入哪些；None = 全部。

    返回**实际导入的**主机名列表（没导进去的不算）。

    **绝不覆盖已有的条目。** 已经在信任库里的主机不重复导入 ——
    覆盖等于把用户「后来改过的正确指纹」换回一份旧资料。
    """
    dst = known_hosts_path(config_dir)
    if not src.is_file():
        return []
    try:
        source = paramiko.HostKeys(str(src))
    except Exception:      # noqa: BLE001
        return []
    try:
        target = paramiko.HostKeys(str(dst)) if dst.is_file() else paramiko.HostKeys()
    except Exception:      # noqa: BLE001
        target = paramiko.HostKeys()

    imported: list[str] = []
    for hostname, keys in source.items():
        if hostnames is not None and hostname not in hostnames:
            continue
        added = False
        existing = target.get(hostname) or {}
        for key_type, key in keys.items():
            if key_type in existing:
                continue          # 已有，不覆盖
            target.add(hostname, key_type, key)
            added = True
        if added:
            imported.append(hostname)
    if imported:
        target.save(str(dst))
    return imported


def load_host_keys(client: paramiko.SSHClient,
                   config_dir: Path | None = None,
                   use_system: bool = True) -> Path | None:
    """把两个 known_hosts 都装进 *client*。

    返回 MavisSSH 自己那份的路径（没装成功则 None）。

    ``load_system_host_keys()`` 读用户 ``~/.ssh/known_hosts``（Windows 上是
    ``%USERPROFILE%\\.ssh\\known_hosts``）—— paramiko 内部已经把「文件不
    存在」的 IOError 吃掉了。

    ``load_host_keys()`` 读我们自己的；它会设 ``_host_keys_filename``，
    后续的 ``save_host_keys()`` 靠它决定写去哪。首次运行时文件不存在，
    那个 IOError 也要吃掉（但 ``_host_keys_filename`` 已经设上了）。

    *use_system* —— False 时**不读**用户 ``~/.ssh/known_hosts``。
    正常连接必须为 True（复用用户既有信任是设计意图），但**测试要能
    隔离**：真机验证要测「未知主机」那条路径，而用户的 known_hosts 里
    往往已经有那台机器 —— 不隔离的话 paramiko 直接匹配上了，
    ``missing_host_key`` **根本不会被调用**，测出来的「没问」是假象。
    """
    if use_system:
        client.load_system_host_keys()

    path = known_hosts_path(config_dir)
    try:
        client.load_host_keys(str(path))
    except IOError:
        pass
    else:
        # **落盘后收紧权限。** known_hosts 里有用户信任的指纹，
        # 但它本身不是秘密（同 known_hosts 惯例）—— 不过目录里还放着
        # secret.key，权限松了会连累那边，所以顺手 chmod。
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
    return None
