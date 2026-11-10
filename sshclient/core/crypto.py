"""落盘加密：把 SavedSession 里的敏感字段变成密文。

**密钥**：一个随机生成的 Fernet 密钥，存放在用户配置目录下的 `secret.key`，
和 `sessions.json` 在同一个目录。

这就是本方案的安全边界，说白了：密钥文件和数据文件在同一个用户账户下，
所以它防的是「随手打开 sessions.json 就看到明文密码」，
**不防**同账户下的其他进程或拿到该账户文件副本的人。要更强就得用系统
钥匙串（Windows DPAPI）或每次启动输口令——那两条路的取舍写在
`docs/ARCHITECTURE.md`，这里不重复。

`enc:v1:` 前缀既是版本号，也是「这个字段是密文」的标记。解密时看到没有
前缀的值就原样返回，所以旧的明文 `sessions.json` 不需要专门的迁移步骤：
读的时候照常能用，下一次保存时自然变成密文。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
KEY_FILENAME = "secret.key"

#: 会被加密的字段。`port` 是 int，存盘时转成字符串再加密。
SENSITIVE = ("host", "port", "username", "password",
             "key_filename", "key_passphrase")

_lock = threading.Lock()
_fernet: Fernet | None = None


class CryptoUnavailable(RuntimeError):
    """密钥拿不到时抛出。调用方必须**继续用明文**而不是丢数据。"""


def key_path(config_dir: Path) -> Path:
    return Path(config_dir) / KEY_FILENAME


def _load_or_create(path: Path) -> Fernet:
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise CryptoUnavailable(f"密钥文件是空的：{path}")
        try:
            return Fernet(raw.encode("ascii"))
        except Exception as exc:
            raise CryptoUnavailable(f"密钥文件无法解析：{path}（{exc}）") from exc

    # 首次使用：先落盘再返回。顺序很重要——如果先返回后写盘，进程在两者
    # 之间崩掉，就会留下一份用着内存里密钥、而磁盘上那份永远读不回来的数据。
    raw = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(raw.decode("ascii"), encoding="utf-8")
    os.replace(tmp, path)
    return Fernet(raw)


def get_fernet(config_dir: Path) -> Fernet:
    """取（或首次创建）全局那一份密钥。线程安全。"""
    global _fernet
    if _fernet is not None:
        return _fernet
    with _lock:
        if _fernet is None:
            _fernet = _load_or_create(key_path(config_dir))
    return _fernet


def reset_for_tests() -> None:
    """只给测试用：丢掉进程内缓存的密钥。"""
    global _fernet
    with _lock:
        _fernet = None


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value, config_dir: Path) -> str:
    """加密单个字段。空值不产生密文——空着比 `enc:v1:xxx` 好读。"""
    if value is None or value == "":
        return ""
    text = str(value)
    if is_encrypted(text):          # 幂等：别把已加密的再加密一层
        return text
    token = get_fernet(config_dir).encrypt(text.encode("utf-8")).decode("ascii")
    return PREFIX + token


def decrypt(value, config_dir: Path) -> str:
    """解密单个字段。没有前缀就当明文原样返回（旧的明文文件靠这条兼容）。"""
    if value is None:
        return ""
    text = str(value)
    if not is_encrypted(text):
        return text
    blob = text[len(PREFIX):].encode("ascii")
    try:
        return get_fernet(config_dir).decrypt(blob).decode("utf-8")
    except InvalidToken as exc:
        # 只有两种可能：密钥换了，或者密文被手改过。无论哪种都**不能**
        # 当成空值糊弄过去——那等于静默把用户的密码清空。
        raise CryptoUnavailable(
            "密文解不开：密钥文件与数据不匹配，或数据已被改动") from exc


def seal(record: dict, config_dir: Path) -> dict:
    """把一条会话记录里的敏感字段加密，返回新 dict（不改原对象）。"""
    out = dict(record)
    for field in SENSITIVE:
        if field in out:
            out[field] = encrypt(out[field], config_dir)
    return out


def unseal(record: dict, config_dir: Path) -> dict:
    """解密一条会话记录。`port` 会还原成 int——存盘时被转成了字符串。"""
    out = dict(record)
    for field in SENSITIVE:
        if field in out and is_encrypted(out[field]):
            out[field] = decrypt(out[field], config_dir)
    if "port" in out:
        out["port"] = _as_port(out["port"])
    return out


def _as_port(value) -> int:
    """端口还原成 int。容错：明文老文件里它本来就是 int 或数字字符串。"""
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 22


def dump(records: list, config_dir: Path) -> str:
    """把整份会话表序列化成加密后的 JSON 文本。"""
    return json.dumps([seal(r, config_dir) for r in records],
                      indent=2, ensure_ascii=False)


def load(text: str, config_dir: Path) -> list:
    """解析并解密一份 JSON 文本。文件损坏时抛 ValueError，交给调用方决定。"""
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("会话文件的顶层不是数组")
    return [unseal(d, config_dir) for d in data if isinstance(d, dict)]
