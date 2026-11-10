"""判据：主机密钥验证 —— 未知主机不能被静默接受。

**钉的是什么。** 原实现是 ``client.set_missing_host_key_policy(
paramiko.AutoAddPolicy())``，而且**既没** ``load_system_host_keys()``
**也没** ``load_host_keys()``：

* 用户 ``~/.ssh/known_hosts`` 里的条目根本没被读进来 → paramiko 连「比对」
  这步都不存在；
* 未知主机被**无条件接受** → 任何能在这条网络路径上冒充主机的人
  （ARP/DNS 欺骗、恶意网关、被劫持的 Wi-Fi）都能完成一次中间人攻击，
  静默读走会话流量，用密码认证时还能收走口令。

**元断言（最重要）**：把老实现装回去，本判据必须红。

跑法：
    .venv\\Scripts\\python.exe tests\\_fhostkey.py
"""
from __future__ import annotations

import io as _io
import os
import pathlib
import re as _re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import paramiko  # noqa: E402
import paramiko as _pk  # noqa: E402
from cryptography.hazmat.primitives import serialization as _ser  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519  # noqa: E402

from sshclient.remote import connection as connmod  # noqa: E402
from sshclient.remote import hostkeys  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


print("=== 策略：新代码不能是 AutoAddPolicy ===")

src = pathlib.Path(connmod.__file__).read_text(encoding="utf-8")
hk_src = pathlib.Path(hostkeys.__file__).read_text(encoding="utf-8")

# **只看代码、不看注释。** 注释里会提到 AutoAddPolicy（解释「原来是怎么错的、
# 为什么错」），那是**应该保留**的说明，不是代码。
code_only = _re.sub(r'""".*?"""', "", src, flags=_re.S)
code_only = _re.sub(r"#[^\n]*", "", code_only)
hk_code_only = _re.sub(r'""".*?"""', "", hk_src, flags=_re.S)
hk_code_only = _re.sub(r"#[^\n]*", "", hk_code_only)

check("代码里不再使用 AutoAddPolicy",
      "AutoAddPolicy" not in code_only,
      "代码里仍有 AutoAddPolicy")
check("代码里有 RejectPolicy（没给回调时的兜底）",
      "RejectPolicy" in code_only)
check("代码里有 PromptHostKeyPolicy（问用户）",
      "PromptHostKeyPolicy" in code_only)
# **load_system_host_keys 在 hostkeys.py 里**（connection.py 调的是封装
# `load_host_keys(client)`）—— 查错文件的话这条会恒红。
check("hostkeys.py 里有 load_system_host_keys（读用户 known_hosts）",
      "load_system_host_keys" in hk_code_only)
check("hostkeys.py 里有 load_host_keys（读 MavisSSH 自己那份）",
      "client.load_host_keys" in hk_code_only)
check("connection.py 确实调了 load_host_keys",
      "load_host_keys(client" in code_only,
      "connection.py 里没有调用 load_host_keys")
# **别钉住完整的调用串。** 原来写的是 `load_host_keys(client)` —— 精确匹配
# 一个参数的调用形式。后来为了**测试隔离**加了两个参数
# （config_dir / use_system_hosts），这个断言就红了 —— 红的不是产品，
# 是判据把「调用形式」当成了「性质」。
#
# 性质是「connect() 必须走 load_host_keys 装信任库」，跟传几个参数无关。
check("connection.py 确实调了 load_host_keys（不钉参数个数）",
      "load_host_keys(client" in code_only)

print("\n=== BadHostKeyException 必须被单独接住 ===")
# `BadHostKeyException` 是 `SSHException` 的子类 —— 不单独接住就会被
# `except (paramiko.SSHException, OSError)` 吞成泛泛的「连接失败」。
check("BadHostKeyException 确实是 SSHException 的子类（前提）",
      issubclass(paramiko.BadHostKeyException, paramiko.SSHException))
check("源码里有独立的 BadHostKeyException 分支",
      "except paramiko.BadHostKeyException" in src)
check("有专属异常类型 SSHHostKeyChangedError",
      hasattr(connmod, "SSHHostKeyChangedError"))
# **顺序**：BadHostKeyException 的 except 必须排在 SSHException 之前。
i_bad = src.find("except paramiko.BadHostKeyException")
i_gen = src.find("except (paramiko.SSHException, OSError)")
check("BadHostKeyException 的分支排在 SSHException 之前",
      i_bad != -1 and i_gen != -1 and i_bad < i_gen,
      "bad@%d generic@%d" % (i_bad, i_gen))
# 它要继承 SSHConnectError —— 既有调用方的 `except SSHConnectError` 仍能接住
check("SSHHostKeyChangedError 继承 SSHConnectError（不破坏既有调用方）",
      issubclass(connmod.SSHHostKeyChangedError, connmod.SSHConnectError))

print("\n=== 指纹格式 ===")

class _FakeKey:
    """最小可用的 PKey 替身。"""

    def __init__(self, name="ssh-ed25519", data=b"x" * 32):
        self._name = name
        self._data = data

    def get_name(self):
        return self._name

    def asbytes(self):
        return self._data

    def get_fingerprint(self):
        return bytes(range(16))


k = _FakeKey()
fp = hostkeys.sha256_fingerprint(k)
check("SHA256 指纹形如 SHA256:<base64>",
      fp.startswith("SHA256:") and len(fp) > 20, fp)
check("SHA256 指纹不带 padding 的 '='",
      not fp.endswith("="), fp)
md5 = hostkeys.md5_fingerprint(k)
check("MD5 兼容行是冒号分隔的 16 组",
      md5.count(":") == 15, md5)
check("MD5 每组两位 hex",
      all(len(g) == 2 for g in md5.split(":")), md5)

print("\n=== known_hosts 路径与端口解析 ===")
check("known_hosts 在配置目录下",
      hostkeys.known_hosts_path().name == "known_hosts",
      str(hostkeys.known_hosts_path()))
check("默认端口是 22",
      hostkeys.extract_port("10.0.0.9") == 22)
check("非 22 端口从 [host]:port 里取",
      hostkeys.extract_port("[10.0.0.9]:2222") == 2222)
check("畸形键名不炸，回落 22",
      hostkeys.extract_port("[bad") == 22)

print("\n=== 策略行为（真跑 paramiko 的 MissingHostKeyPolicy）===")


class _NullLog:
    """paramiko 的 policy 会往 ``client._log`` 上调 ``debug``/``info`` 等方法。

    给真的 ``Logger`` 也不够 —— paramiko 内部把它当**可调用对象**用
    （``Logger`` 不是可调用的），会报 "'Logger' object is not callable"。
    所以给一个「什么方法都接得住」的替身。
    """

    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop

    def __call__(self, *a, **k):
        return None


class _FakeClient:
    """最小可用的 SSHClient 替身。"""

    def __init__(self):
        self._host_keys = paramiko.HostKeys()
        self._host_keys_filename = None
        self._log = _NullLog()

    def save_host_keys(self, filename):
        self._host_keys.save(filename)


asked = []


def _prompt(req):
    asked.append(req)
    # **返回三态决策字符串，不是 bool。** prompt 的契约是
    # TRUST / TRUST_ONCE / REJECT —— 返回 `True` 不在允许集合里，
    # 会被当成拒绝而抛异常（不是「静默当成接受」）。
    # 「仅此一次」的完整行为在 tests/_fhostkey2.py 里量。
    return hostkeys.TRUST


client = _FakeClient()
policy = hostkeys.PromptHostKeyPolicy(_prompt)
policy.missing_host_key(client, "10.0.0.9", k)
check("未知主机会问用户", len(asked) == 1, str(asked))
check("请求里带着 SHA256 指纹",
      asked and asked[0].fingerprint.startswith("SHA256:"),
      asked[0].fingerprint if asked else "")
check("请求里带着 key_type",
      asked and asked[0].key_type == "ssh-ed25519",
      asked[0].key_type if asked else "")
check("接受后写进了 _host_keys",
      client._host_keys.lookup("10.0.0.9") is not None)

# 拒绝时必须抛异常 —— 不能「既不信任也不报错」地继续
client2 = _FakeClient()
policy2 = hostkeys.PromptHostKeyPolicy(lambda req: hostkeys.REJECT)
raised = False
try:
    policy2.missing_host_key(client2, "10.0.0.9", k)
except paramiko.SSHException:
    raised = True
check("用户拒绝时抛异常（不会静默继续）", raised)
check("用户拒绝时不写进 _host_keys",
      client2._host_keys.lookup("10.0.0.9") is None)

print("\n=== 弱算法禁用 ===")
_dis = connmod._DISABLED_ALGORITHMS
check("有 _DISABLED_ALGORITHMS", isinstance(_dis, dict) and bool(_dis))
for cat in ("kex", "host_key", "encryption", "mac"):
    check("含 %s 类别" % cat, cat in _dis, str(list(_dis)))

# **正确判据是「不能砍掉唯一强项」，不是「不能命中默认偏好」。**
# `3des-cbc` 和四个弱 MAC 命中 paramiko 的默认偏好**正是我们要禁的**；
# 而 paramiko #2126 的教训是：禁掉某个类别里**唯一**的强项，会让
# Win/macOS（无 /etc/ssh/moduli）上与旧客户端的 KEX 交集清空 → 握手直接失败。
#
# 所以这里钉两件事：
#   (1) 每个类别禁完之后**还剩下强项**；
#   (2) 具体的强项算法**一个都不在**禁用列表里。
STRONG = {
    "kex": ("curve25519-sha256@libssh.org", "ecdh-sha2-nistp256",
            "ecdh-sha2-nistp384", "ecdh-sha2-nistp521",
            "diffie-hellman-group14-sha256",
            "diffie-hellman-group-exchange-sha256",
            "diffie-hellman-group16-sha512"),
    "host_key": ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
                 "ecdsa-sha2-nistp521", "rsa-sha2-512", "rsa-sha2-256"),
    "encryption": ("aes128-ctr", "aes192-ctr", "aes256-ctr",
                   "aes128-gcm@openssh.com", "aes256-gcm@openssh.com"),
    "mac": ("hmac-sha2-256", "hmac-sha2-512",
            "hmac-sha2-256-etm@openssh.com",
            "hmac-sha2-512-etm@openssh.com"),
}
for cat, strong in STRONG.items():
    dis = set(_dis.get(cat, ()))
    banned_strong = [s for s in strong if s in dis]
    check("禁用列表里没有 %s 的强项" % cat, not banned_strong,
          "误砍: %r" % (banned_strong,))

# 3des-cbc 与四个弱 MAC 必须在禁用列表里
check("3des-cbc 已禁", "3des-cbc" in _dis.get("encryption", ()))
for m in ("hmac-md5", "hmac-md5-96", "hmac-sha1", "hmac-sha1-96"):
    check("%s 已禁" % m, m in _dis.get("mac", ()))

print("\n=== 元断言：把老实现装回去，本判据必须红 ===")
# 老实现 = AutoAddPolicy + 不加载 known_hosts + 没有 BadHostKeyException 分支。
# **必须真的替换得上**，否则这条元断言是恒绿的假货 —— 第一版就是因为
# 替换目标字符串与源码里的实际写法（缩进/换行）不一致，替换没生效，
# 而「老实现没有 load_system_host_keys」那一条恰好因为源码里本来就有
# 这句而红了 —— **红的原因跟它声称要测的东西无关**，纯巧合。
old_src = code_only.replace("PromptHostKeyPolicy(host_key_prompt)",
                             "paramiko.AutoAddPolicy()")
old_src = old_src.replace("load_host_keys(client)", "pass  # 老实现不加载任何 known_hosts")
# **老实现还要把 BadHostKeyException 分支删掉** —— 那是这次一并加的。
# 不删的话「老实现没有该分支」这条会红，而它红的原因跟判据声称要测的
# 「老实现会吞掉密钥变更」无关 —— 那种红是假货。
old_no_badkey = old_src.replace(
    "except paramiko.BadHostKeyException as exc:",
    "except paramiko.AuthenticationException as exc:  # 老实现没有这个分支")
old_no_badkey = _re.sub(
    r"except paramiko\.AuthenticationException as exc:  # 老实现没有这个分支:"
    r"(?:.|\n)*?raise SSHHostKeyChangedError\(",
    "except paramiko.AuthenticationException as exc:\n            raise SSHAuthError(str(exc))",
    old_no_badkey)

# **判据只查「使用处」，不查 import 行。** import 里出现
# `PromptHostKeyPolicy` 是正常的（它还是模块的公开接口），第一版按
# "整份源码里都不许出现"来查，结果 import 行让替换看起来"没生效"——
# 其实是判据写得太粗。
old_use = [l for l in old_src.splitlines()
           if "set_missing_host_key_policy" in l]
check("元断言：替换真的生效了（老实现用的是 AutoAddPolicy）",
      any("AutoAddPolicy" in l for l in old_use),
      "实际: %r" % (old_use,))
check("元断言：老实现没有用 PromptHostKeyPolicy",
      not any("PromptHostKeyPolicy" in l for l in old_use),
      "实际: %r" % (old_use,))
check("元断言：老实现不加载任何 known_hosts",
      "load_system_host_keys" not in old_src
      and "load_host_keys(client)" not in old_src)

# **行为层才是关键**：老实现里密钥变更会被 `except (SSHException, OSError)`
# 吞成普通连接失败 —— 这才是「告警失效」的真凭据。
#
# `BadHostKeyException(hostname, key)` 要一个真的 PKey；`Ed25519Key` 没有
# 类方法 `generate()`（那是 RSAKey 的写法），得用 `from_private_key`。
_raw = _ed25519.Ed25519PrivateKey.generate()
_pem = _raw.private_bytes(
    encoding=_ser.Encoding.PEM,
    format=_ser.PrivateFormat.OpenSSH,
    encryption_algorithm=_ser.NoEncryption())
# **`from_private_key` 要的是文件对象或路径，不是 bytes** —— 传 bytes 会
# 在 paramiko 内部 `_read_private_key` 里炸成
# "'bytes' object has no attribute 'readlines'"。
_real_key = _pk.Ed25519Key.from_private_key(_io.StringIO(_pem.decode()))

# `BadHostKeyException(hostname, got_key, expected_key)` —— **三个**参数，
# 少一个就 TypeError（paramiko 5.0 的签名）。
bad = _pk.BadHostKeyException("10.0.0.9", _real_key, _real_key)
caught_as = None
try:
    try:
        raise bad
    except (_pk.SSHException, OSError) as exc:      # 老实现的兜底 except
        caught_as = type(exc).__name__
except Exception as exc:
    caught_as = "escaped: %s" % type(exc).__name__
check("元断言：老实现会把 BadHostKeyException 归到通用 SSHException 分支",
      caught_as == "BadHostKeyException", "实际: %r" % (caught_as,))
check("元断言：新实现里它是独立分支（源码里有专门的 except）",
      "except paramiko.BadHostKeyException as exc:" in code_only)

# 行为层：老策略对未知主机**不抛异常、也不问用户** —— 这才是真正要防的
old_client = _FakeClient()
paramiko.AutoAddPolicy().missing_host_key(old_client, "10.0.0.9", k)
check("元断言：老策略不抛异常（所以用户完全不知情）", True)
check("元断言：老策略不问用户（asked 里只有新策略那一次）",
      len(asked) == 1,
      "asked=%r" % (asked,))

print()
if FAIL:
    print("FAILED %d:" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
