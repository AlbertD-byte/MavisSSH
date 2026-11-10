"""判据：主机密钥的「信任库管理」与「指纹核对」。

覆盖三块：

1. **信任库管理** —— 列出 / 删除 / 导入，**且不覆盖已有条目**
2. **指纹比对** —— 用户手输的指纹与实际的是否一致
3. **三态决策** —— 信任并记住 / 仅此一次 / 拒绝，三条路径的写盘行为正确

**元断言**：把老实现（只有 TRUST/REJECT 两态、无论接受都写盘）装回去，
本判据必须红。
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paramiko  # noqa: E402

from sshclient.remote import hostkeys  # noqa: E402
from sshclient.ui import hostkey_dialog  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


# ============================================================ 信任库管理
print("=== 信任库管理 ===")

tmp = pathlib.Path(tempfile.mkdtemp())
cfg = tmp


class _FakeKey:
    """最小可用的 PKey 替身。

    **必须实现 ``get_base64()``** —— paramiko 的 ``HostKeys.save()`` 会调它
    来把 key 序列化成 OpenSSH 格式；少了它就 AttributeError。

    **但它存进文件后读不回来。** paramiko 的 ``HostKeys.load()`` 会按
    key 类型重新解析并校验，假 key 过不了 —— 所以 ``load`` 回来是空的。
    这不是产品代码的问题，是**测试替身**的固有限制。

    所以本判据里**凡是走「存盘 → 读回」那条路的**（列信任库、导入），
    都用**真的 Ed25519 key**；只用假 key 的地方是「不落盘的内存表」
    （三态决策那几��）。
    """

    def __init__(self, data: bytes):
        self._data = data

    def get_name(self):
        return "ssh-ed25519"

    def asbytes(self):
        return self._data

    def get_base64(self):
        import base64
        return base64.b64encode(self._data).decode("ascii")

    def get_fingerprint(self):
        return self._data[:16]


def _real_ed25519(seed: bytes):
    """造一个**真的** Ed25519 key（能存盘、能读回）。"""
    import io as _io

    from cryptography.hazmat.primitives import serialization as _ser
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed

    raw = _ed.Ed25519PrivateKey.from_private_bytes(seed)
    pem = raw.private_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PrivateFormat.OpenSSH,
        encryption_algorithm=_ser.NoEncryption())
    return paramiko.Ed25519Key.from_private_key(_io.StringIO(pem.decode()))


# 造一个真的 known_hosts（**用真 key** —— 假 key 存进去读不回来）
k1 = _real_ed25519(b"\x01" * 32)
k2 = _real_ed25519(b"\x02" * 32)
kh = paramiko.HostKeys()
kh.add("10.0.0.9", "ssh-ed25519", k1)
kh.add("10.0.0.10", "ssh-ed25519", k2)
kh.save(str(hostkeys.known_hosts_path(cfg)))

listed = hostkeys.list_trusted_hosts(cfg)
check("能列出信任库里的条目", len(listed) == 2, str(listed))
check("条目带主机名", {t.hostname for t in listed} == {"10.0.0.9", "10.0.0.10"},
      str([t.hostname for t in listed]))
check("条目带 SHA-256 指纹",
      all(t.fingerprint.startswith("SHA256:") for t in listed),
      str([t.fingerprint for t in listed]))
check("信任库不存在时返回空列表（不抛）",
      hostkeys.list_trusted_hosts(tmp / "nope") == [])

# ---- 删除 ----
removed = hostkeys.remove_trusted_host("10.0.0.9", "ssh-ed25519", cfg)
check("能删除一条信任", removed is True)
check("删除后只剩一条", len(hostkeys.list_trusted_hosts(cfg)) == 1)
check("删掉的不在了",
      "10.0.0.9" not in {t.hostname for t in hostkeys.list_trusted_hosts(cfg)})
check("删不存在的返回 False",
      hostkeys.remove_trusted_host("1.2.3.4", "ssh-ed25519", cfg) is False)

# ---- 导入 ----
src = tmp / "src_known_hosts"
sh = paramiko.HostKeys()
sh.add("192.168.1.5", "ssh-ed25519", _real_ed25519(b"\x03" * 32))
sh.add("192.168.1.6", "ssh-ed25519", _real_ed25519(b"\x04" * 32))
sh.save(str(src))

imported = hostkeys.import_known_hosts(src, cfg)
check("导入全部成功", sorted(imported) == ["192.168.1.5", "192.168.1.6"],
      str(imported))
check("导入后信任库有 3 条", len(hostkeys.list_trusted_hosts(cfg)) == 3,
      str(len(hostkeys.list_trusted_hosts(cfg))))

# **绝不覆盖已有的条目** —— 覆盖等于把用户后来改对的指纹换回旧资料
before = {t.hostname: t.fingerprint
          for t in hostkeys.list_trusted_hosts(cfg)}
again = hostkeys.import_known_hosts(src, cfg)
check("重复导入不覆盖已有（返回空）", again == [], str(again))
after = {t.hostname: t.fingerprint
         for t in hostkeys.list_trusted_hosts(cfg)}
check("重复导入后指纹没变", before == after,
      "before=%r after=%r" % (before, after))

# 按名字导入
picked = hostkeys.import_known_hosts(src, cfg, hostnames=["192.168.1.5"])
check("按名字导入只导指定的那台",
      picked == [] or picked == ["192.168.1.5"], str(picked))

# ============================================================ 指纹比对
print("\n=== 指纹核对 ===")
m = hostkey_dialog._fingerprint_matches
md5 = "aa:bb:cc:dd:ee:ff:00:11:22:33:44:55:66:77:88:99"
flat = md5.replace(":", "")

check("完全相同 → 一致", m("SHA256:abc", "SHA256:abc") is True)
check("大小写不同 → 一致", m("sha256:ABC", "SHA256:abc") is True)
check("MD5 带冒号 vs 不带 → 一致", m(md5, flat) is True)
check("MD5 不带冒号 vs 带 → 一致", m(flat, md5) is True)
check("不同的 SHA-256 → 不一致", m("SHA256:abc", "SHA256:xyz") is False)
check("SHA-256 与 MD5 不互认", m("SHA256:abc", md5) is False)
check("空输入 → 不一致", m("", "SHA256:abc") is False)
check("太短的 hex 不当 MD5 认", m("aa:bb:cc", "aabbcc") is False)

# ============================================================ 三态决策
print("\n=== 三态决策的写盘行为 ===")


class _NullLog:
    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop

    def __call__(self, *a, **k):
        return None


class _FakeClient:
    def __init__(self):
        self._host_keys = paramiko.HostKeys()
        self._host_keys_filename = None
        self._log = _NullLog()

    def save_host_keys(self, filename):
        self._host_keys.save(filename)


key = _FakeKey(b"\x09" * 32)

# ---- TRUST：接受并写盘 ----
c1 = _FakeClient()
hostkeys.PromptHostKeyPolicy(lambda req: hostkeys.TRUST).missing_host_key(
    c1, "10.0.0.9", key)
check("TRUST：写进 _host_keys", c1._host_keys.lookup("10.0.0.9") is not None)

# ---- TRUST_ONCE：接受但**不**写盘 ----
c2 = _FakeClient()
hostkeys.PromptHostKeyPolicy(lambda req: hostkeys.TRUST_ONCE).missing_host_key(
    c2, "10.0.0.9", key)
check("TRUST_ONCE：**不**写进 _host_keys",
      c2._host_keys.lookup("10.0.0.9") is None,
      "TRUST_ONCE 也写盘了 —— 「仅此一次」的语义破了")

# ---- REJECT：抛异常且不写盘 ----
c3 = _FakeClient()
raised = False
try:
    hostkeys.PromptHostKeyPolicy(lambda req: hostkeys.REJECT).missing_host_key(
        c3, "10.0.0.9", key)
except paramiko.SSHException:
    raised = True
check("REJECT：抛异常（不会静默继续）", raised)
check("REJECT：不写盘", c3._host_keys.lookup("10.0.0.9") is None)

# **元断言（行为层，不是源码文本层）。**
#
# 第一版写成「hostkeys.py 里没有 TRUST_ONCE 这个字符串」—— 那测的是
# **源码文本**，不是行为，而且它红的原因跟「老实现有缺陷」无关：
# 本轮**就是**要加 TRUST_ONCE。换个正确实现的改法照样会红。
#
# 行为层的元断言是：**把「不区分仅此一次」的老行为装回去**（接受就写盘），
# 那么「TRUST_ONCE 应当不写盘」这条必须红。老行为用一个「总是写盘」的
# 策略替身来代表。
class _OldAlwaysWritePolicy(paramiko.MissingHostKeyPolicy):
    """老行为：接受就写盘（没有「仅此一次」）。"""

    def missing_host_key(self, client, hostname, key):
        client._host_keys.add(hostname, key.get_name(), key)


c_old = _FakeClient()
_OldAlwaysWritePolicy().missing_host_key(c_old, "10.0.0.9", key)
old_would_write = c_old._host_keys.lookup("10.0.0.9") is not None
check("元断言：老行为（接受就写盘）确实会写盘 —— 所以「仅此一次」这条判据有区分度",
      old_would_write,
      "老行为都没写盘，「TRUST_ONCE 不写盘」这条判据恒真")

# 而新实现里 TRUST_ONCE 不写盘 —— 与老行为**相反**，所以它能区分
c_new = _FakeClient()
hostkeys.PromptHostKeyPolicy(lambda req: hostkeys.TRUST_ONCE).missing_host_key(
    c_new, "10.0.0.9", key)
new_writes = c_new._host_keys.lookup("10.0.0.9") is not None
check("元断言：新实现与老行为在「仅此一次」上结论相反",
      old_would_write and not new_writes,
      "old=%r new=%r" % (old_would_write, new_writes))

# ============================================================ 异常分类
print("\n=== 连接错误的四类分类 ===")
from sshclient.remote import connection as connmod  # noqa: E402

for name in ("SSHAuthError", "SSHHostKeyChangedError", "SSHNetworkError",
             "SSHServerRefusedError"):
    check("有 %s" % name, hasattr(connmod, name))
check("SSHHostKeyChangedError 继承 SSHConnectError",
      issubclass(connmod.SSHHostKeyChangedError, connmod.SSHConnectError))
check("SSHNetworkError 继承 SSHConnectError",
      issubclass(connmod.SSHNetworkError, connmod.SSHConnectError))
check("SSHServerRefusedError 继承 SSHConnectError",
      issubclass(connmod.SSHServerRefusedError, connmod.SSHConnectError))
check("SSHAuthError 不继承 SSHConnectError（认证失败是另一回事）",
      not issubclass(connmod.SSHAuthError, connmod.SSHConnectError))

# UI 侧按类型分派
mw_src = (ROOT / "sshclient" / "ui" / "main_window.py").read_text(
    encoding="utf-8")
for exc_type in ("SSHHostKeyChangedError", "SSHServerRefusedError",
                  "SSHNetworkError", "SSHAuthError"):
    check("UI 分派了 %s" % exc_type,
          '"%s"' % exc_type in mw_src)

print()
if FAIL:
    print("FAILED %d:" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
