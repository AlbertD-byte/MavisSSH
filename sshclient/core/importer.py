"""从其它 SSH 客户端导入连接。

首批覆盖三个源：

* **OpenSSH `~/.ssh/config`** —— 纯文本、跨平台、任何装了 OpenSSH 的机器
  都有。**优先读它。**
* **PuTTY** —— 注册表 `HKCU\\Software\\SimonTatham\\PuTTY\\Sessions`
* **WinSCP** —— 注册表 `HKCU\\Software\\Martin Prikryl\\WinSCP 2\\Sessions`

PuTTY 与 WinSCP 共用同一个解析器：两者是同一份 PuTTY 代码血统，会话的值名
（`HostName` / `PortNumber` / `UserName` / `PublicKeyFile`）完全一致，只有
判协议用的那个字段不同。

## 三条硬规矩

**1. 密码一律不导。** 不是省事，是搬不动：PuTTY/WinSCP 压根不存密码；Xshell
用 DPAPI 或 Master Password；MobaXterm 是 AES + 本机 Windows 凭据；FinalShell
是 DES/AES。**没有一个能跨机解密。** 反过来说，去逆向别家客户端的密码加密
在合规上也不干净。所以导入的上限就是「主机 + 端口 + 用户名 + 私钥路径」，
剩下的让用户自己填。

**2. `.ppk` 不能当私钥用。** PuTTY/WinSCP 的 `PublicKeyFile` 指向 `.ppk`，
那是 PuTTY 自己的格式，paramiko 读不了（`.ppk` 开头是
`PuTTY-User-Key-File-2:` 而不是 OpenSSH 的 `-----BEGIN ... PRIVATE KEY-----`）。
遇到它**不写进 `key_filename`** —— 填一个读不了的路径进去，比留空更糟：界面上
显示「密钥登录」，一点就报错。改为留空 + 在备注里说明。

**3. 路径一律多候选探测。** 网上资料对同一个客户端的路径说法经常互相矛盾
（FinalShell 一个就有 `~/.finalshell`、`%APPDATA%\\finalshell`、
`%LOCALAPPDATA%\\finalshell` 三种说法），而且**几乎每个客户端都允许用户
改路径**。所以这里对每个源都准备若干候选位置，逐个探测；一个都没有时返回
空列表，由调用方提示用户——而不是猜一个路径然后静默地导入 0 条。
"""
from __future__ import annotations

import glob
import os
import pathlib
import sys
import urllib.parse
from dataclasses import dataclass, field

from .settings import SavedSession


@dataclass
class Candidate:
    """一条从别处读到的连接，尚未变成 :class:`SavedSession`。"""

    source: str
    label: str
    name: str
    host: str
    port: int = 22
    username: str = ""
    key_filename: str = ""
    #: 给用户看的提醒（.ppk 不可用、私钥文件不存在……），会并进会话备注
    note: str = ""


@dataclass
class Source:
    """一个可导入的来源，附带它当前能读到的条数。"""

    key: str
    label: str
    where: str
    candidates: list[Candidate] = field(default_factory=list)


# ====================================================================== OpenSSH

def default_ssh_config_path() -> pathlib.Path:
    return pathlib.Path.home() / ".ssh" / "config"


def _expand_token(token: str) -> str | None:
    """展开 `~/.ssh/config` 里的路径 token。解不出来返回 ``None``。"""
    home = pathlib.Path.home()
    out = token
    if "%d" in out:
        out = out.replace("%d", str(home / ".ssh"))
    if "%u" in out:
        out = out.replace("%u", os.environ.get("USERNAME") or os.environ.get("USER") or "")
    if "%l" in out:
        import socket
        out = out.replace("%l", socket.gethostname())
    if "%" in out:
        # 还剩 %h / %r / %i 之类。硬凑出来的是一个必然不存在的路径。
        return None
    return out


def _identity_candidates(values: list[str]) -> tuple[str, str]:
    """从若干条 ``IdentityFile`` 里挑一个能用的。

    返回 ``(路径, 说明)``。OpenSSH 会按顺序逐个尝试，我们只有一个
    ``key_filename`` 字段，所以取第一个**真实存在**的。
    """
    first_bad = ""
    for raw in values:
        token = raw.strip().strip('"')
        if not token:
            continue
        expanded = _expand_token(token)
        if expanded is None:
            first_bad = first_bad or f"私钥 {token} 含连接时才能确定的 %h/%r，没法定位"
            continue
        p = pathlib.Path(os.path.expanduser(expanded))
        if p.exists():
            return str(p), ""
        first_bad = first_bad or f"私钥文件不存在：{p}"
    return "", first_bad


def _split_kv(line: str) -> tuple[str, str]:
    """`Port 22` / `Port=22` 都认。关键字大小写不敏感。"""
    for sep in ("=", " ", "\t"):
        idx = line.find(sep)
        if idx > 0:
            return line[:idx].strip().lower(), line[idx + 1:].strip()
    return "", ""


def _strip_comment(line: str) -> str:
    """去掉行尾注释。OpenSSH 里 `#` 前要有空白才算注释。"""
    out = []
    for ch_i, ch in enumerate(line):
        if ch == "#" and (ch_i == 0 or line[ch_i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).strip()


def _read_includes(value: str, base: pathlib.Path) -> list[pathlib.Path]:
    """解析 ``Include`` 的参数。多个用空格/引号分隔，各自可以是 glob。

    **相对路径按 OpenSSH 的规则解析**：先看 ``~/.ssh/``，再看**包含它的那
    个文件所在目录**，最后才当普通路径。不这么解析的话
    ``Include extra.txt`` 在 CWD 不是 ``~/.ssh`` 时就静悄悄地什么都找不到
    （实测第一版就栽在这，判据直接少了一条）。
    """
    out = []
    for raw in value.split():
        token = raw.strip('"')
        if not token:
            continue
        token = (_expand_token(token) or token).replace("\\", "/")
        seen: list[str] = []
        for root in (pathlib.Path.home() / ".ssh", base.parent,
                     pathlib.Path.cwd()):
            cand = os.path.expanduser(str(root / token))
            if cand not in seen:
                seen.append(cand)
        for pat in seen:
            for hit in glob.glob(pat):
                p = pathlib.Path(hit)
                if p.is_file():
                    out.append(p)
    return out


def read_ssh_config(path: pathlib.Path | None = None,
                     _depth: int = 0,
                     _seen: set | None = None) -> list[Candidate]:
    """读 OpenSSH 的 ``~/.ssh/config``。

    只导出**具名**的 Host 块。`Host *` 这类通配块是给所有连接兜底的默认值，
    不是一个连接 —— 把它导进来会凭空多一条叫 `*` 的假会话。
    """
    path = pathlib.Path(path) if path else default_ssh_config_path()
    if not path.is_file() or _depth > 4:
        return []
    seen = _seen if _seen is not None else set()
    try:
        key = str(path.resolve())
    except OSError:
        key = str(path)
    if key in seen:          # Include 互相套进来时会绕圈
        return []
    seen.add(key)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    out: list[Candidate] = []
    patterns: list[str] = []
    host = user = ""
    port = 22
    identities: list[str] = []

    def flush():
        nonlocal host, user, port, identities
        if patterns:
            key_path, why = _identity_candidates(identities)
            note = why
            for pat in patterns:
                if any(c in pat for c in "*?!"):
                    continue        # 通配/取反，不是具体连接
                out.append(Candidate(
                    source="ssh-config",
                    label="OpenSSH 配置",
                    # 没有 HostName 时，Host 后面那个词本身就是地址
                    name=pat,
                    host=host or pat,
                    port=port,
                    username=user,
                    key_filename=key_path,
                    note=note,
                ))
        host = user = ""
        port = 22
        identities = []

    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line:
            continue
        key, value = _split_kv(line)
        if not key:
            continue
        if key == "host":
            flush()
            patterns = value.split()
            continue
        if key == "include":
            for sub in _read_includes(value, path):
                out.extend(read_ssh_config(sub, _depth + 1, seen))
            continue
        if not patterns:
            continue        # 文件开头的全局默认值，没有归属
        # **OpenSSH 是「先到先得」**：同一个关键字第一次出现就定死了，
        # 后面再写一遍不生效。所以这里一律不覆盖已有值。
        if key == "hostname" and not host:
            host = value
        elif key == "user" and not user:
            user = value
        elif key == "port" and port == 22:
            try:
                p = int(value)
                if 1 <= p <= 65535:
                    port = p
            except ValueError:
                pass
        elif key == "identityfile":
            identities.append(value)
    flush()
    return out


# ====================================================================== 注册表

#: PuTTY / WinSCP 会话值名。键名全小写，因为注册表本身大小写不敏感。
_VAL_HOST = "hostname"
_VAL_PORT = "portnumber"
_VAL_USER = "username"
_VAL_KEY = "publickeyfile"

#: 各自判协议用的字段与「算 SSH」的值集合。PuTTY 存 `Protocol=ssh` 字符串，
#: WinSCP 存 `FSProtocol` 的 DWORD（0=SCP、2=SFTP，两者都跑在 SSH 上）。
_PUTTY_ROOT = r"Software\SimonTatham\PuTTY\Sessions"
_WINSCP_ROOT = r"Software\Martin Prikryl\WinSCP 2\Sessions"
_WINSCP_SSH_PROTOCOLS = {0, 2}


def _is_ssh(values: dict, source: str) -> bool:
    """会话是不是 SSH。判不出来（没有协议字段）时按是处理 —— 宁可多导一条
    让人删，也不要静悄悄少导一条用户以为在的。"""
    if source == "putty":
        proto = values.get("protocol")
        if proto is None:
            return True
        return str(proto).strip().lower() == "ssh"
    proto = values.get("fsprotocol")
    if proto is None:
        return True
    try:
        return int(proto) in _WINSCP_SSH_PROTOCOLS
    except (TypeError, ValueError):
        return True


def _registry_sessions(root: str, label: str, source: str) -> list[Candidate]:
    """读 PuTTY / WinSCP 的会话子键。非 Windows 或没有这个键就是空列表。"""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []
    try:
        root_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, root)
    except OSError:
        return []

    out: list[Candidate] = []
    with root_key:
        idx = 0
        while True:
            try:
                sub = winreg.EnumKey(root_key, idx)
            except OSError:
                break
            idx += 1
            # 「Default Settings」是默认值模板，不是连接。注册表键名里的
            # 空格存成了 %20，所以这里比对的是编码后的形式。
            if sub.lower().startswith("default%20settings"):
                continue
            try:
                with winreg.OpenKey(root_key, sub) as k:
                    values = _read_reg_values(k)
            except OSError:
                continue
            if not _is_ssh(values, source):
                continue
            host = str(values.get(_VAL_HOST, "")).strip()
            if not host:
                continue
            port = 22
            try:
                p = int(values.get(_VAL_PORT, 22))
                if 1 <= p <= 65535:
                    port = p
            except (TypeError, ValueError):
                pass
            key_path, note = "", ""
            raw_key = str(values.get(_VAL_KEY, "")).strip()
            if raw_key:
                # **`.ppk` 是 PuTTY 自己的私钥格式，paramiko 读不了。**
                if raw_key.lower().endswith(".ppk"):
                    note = (f"原客户端用的是 .ppk 私钥（{pathlib.Path(raw_key).name}），"
                            f"MavisSSH 用不了这种格式，已留空 —— 请改用 OpenSSH 私钥"
                            f"或密码登录。")
                elif pathlib.Path(raw_key).exists():
                    key_path = raw_key
                else:
                    note = f"原客户端记的私钥文件不存在：{raw_key}"
            out.append(Candidate(
                source=source,
                label=label,
                name=urllib.parse.unquote(sub),
                host=host,
                port=port,
                username=str(values.get(_VAL_USER, "")).strip(),
                key_filename=key_path,
                note=note,
            ))
    return out


def _read_reg_values(key) -> dict:
    """把一个子键的值读成 ``{小写名: 值}``，DWORD 统一成 int。"""
    import winreg
    out = {}
    idx = 0
    while True:
        try:
            name, value, _typ = winreg.EnumValue(key, idx)
        except OSError:
            break
        idx += 1
        if isinstance(value, bytes):
            try:
                value = value.decode("utf-8", "replace")
            except Exception:                      # noqa: BLE001
                continue
        out[name.strip().lower()] = value
    return out


# ====================================================================== 汇总

def sources(ssh_config: pathlib.Path | None = None) -> list[Source]:
    """列出所有已知来源及其当前读到的条数。读不到就是 0 条，不是报错。"""
    cfg = pathlib.Path(ssh_config) if ssh_config else default_ssh_config_path()
    return [
        Source("ssh-config", "OpenSSH 配置", str(cfg), read_ssh_config(cfg)),
        Source("putty", "PuTTY", _registry_label(_PUTTY_ROOT),
               _registry_sessions(_PUTTY_ROOT, "PuTTY", "putty")),
        Source("winscp", "WinSCP", _registry_label(_WINSCP_ROOT),
               _registry_sessions(_WINSCP_ROOT, "WinSCP", "winscp")),
    ]


def _registry_label(root: str) -> str:
    return f"注册表 HKCU\\{root}" if sys.platform == "win32" else "注册表（仅 Windows）"


def to_session(c: Candidate) -> SavedSession:
    """候选 -> 会话。**这里永不写密码。**"""
    s = SavedSession()
    s.name = c.name or (f"{c.username}@{c.host}" if c.username else c.host)
    s.host = c.host
    s.port = c.port
    s.username = c.username
    if c.key_filename:
        s.auth_method = "key"
        s.key_filename = c.key_filename
    else:
        s.auth_method = "password"
    if c.note:
        s.notes = c.note
    return s


def merge(store, candidates: list[Candidate]) -> tuple[list[SavedSession], int]:
    """把候选并进 *store*，返回 ``(新增的会话, 跳过的重复数)``。

    判重按 ``(host, port, username)``，不按名字 —— 同一个服务器存两遍对用户
    没有任何好处，而用户真正在意的恰恰是「别重复」。
    """
    existing = {(s.host.strip().lower(), int(s.port), s.username.strip().lower())
                for s in store.all}
    added: list[SavedSession] = []
    skipped = 0
    for c in candidates:
        key = (c.host.strip().lower(), int(c.port), c.username.strip().lower())
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        s = to_session(c)
        store.upsert(s)
        added.append(s)
    return added, skipped
