"""判据：隧道监听地址的暴露面 + 主机密钥与注入的回归。

三个部分：

1. **暴露面** —— ``is_exposed_address`` 要正确区分回环 / 任意地址 / 主机名。
2. **主机密钥** —— 委托给 ``tests/_fhostkey.py``（不重复写一遍）。
3. **注入** —— 委托给 ``tests/_fshelljoin.py``（不重复写一遍）。

**元断言**：把「老实现」（没有 ``is_exposed_address``）装回去，第一部分必须红。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sshclient.core import tunnel  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


print("=== 暴露面判定 ===")

# 回环 —— 安全
for h in ("127.0.0.1", "127.0.0.53", "::1"):
    check("回环地址不算暴露: %s" % h,
          tunnel.is_exposed_address(h) is False)

# 任意地址 —— 暴露
for h in ("0.0.0.0", "::"):
    check("任意地址算暴露: %s" % h,
          tunnel.is_exposed_address(h) is True)

# 具体的非回环地址 —— 也是暴露（不只是通配符）
for h in ("192.168.1.10", "10.0.0.5", "fe80::1"):
    check("具体非回环地址算暴露: %s" % h,
          tunnel.is_exposed_address(h) is True)

# 主机名 / 空 —— 不算
for h in ("localhost", "", "example.com"):
    check("主机名/空不算暴露: %r" % h,
          tunnel.is_exposed_address(h) is False)

print("\n=== 绑定时要把暴露面说出来 ===")
src = pathlib.Path(tunnel.__file__).read_text(encoding="utf-8")
check("tunnel.py 里有 is_exposed_address",
      "def is_exposed_address" in src)
check("bind 之后有暴露面的告警日志",
      "is_exposed_address" in src and "局域网可见" in src)

print("\n=== 元断言：老实现（没有这个判定）必须红 ===")
# 老实现 = 既有函数名被改掉，**告警块也整段删掉**。
# 第一版只改了函数名就断言「没有告警」—— 告警那行还在，红的原因是
# 替换没删干净，不是「老实现没有这个能力」。**元断言的替换必须真的
# 把能力拿掉**，否则它测的是替换的副作用。
import re as _re

old_src = src.replace("def is_exposed_address", "def _gone_is_exposed_address")
# **按行删告警块，不靠正则。** 那块里有跨行的中文格式串，正则匹配
# `local_port\)\)\n` 对不上（第一版就是这么失手的）。而且正则失败时
# 判据会**因为替换没删干净而红** —— 红的��因跟它声称要测的东西无关。
_lines = old_src.splitlines(keepends=True)
_out = []
_skip = False
for _l in _lines:
    if "if is_exposed_address(self.spec.listen_host):" in _l:
        _skip = True
        continue
    if _skip:
        if "self.spec.listen_host, self.spec.local_port)" in _l:
            _skip = False
        continue
    _out.append(_l)
old_src = "".join(_out)
check("元断言：老实现里没有这个函数",
      "def is_exposed_address" not in old_src)
check("元断言：老实现里也没有暴露面的告警",
      "局域网可见" not in old_src,
      "告警块没删干净 —— 元断言测的是替换副作用而不是能力缺失")
# 行为层：老实现根本没有判定能力，所以「0.0.0.0 会被当成正常地址」
check("元断言：老实现无法识别 0.0.0.0 的暴露面",
      not hasattr(tunnel, "_gone_is_exposed_address"))

print("\n=== 委托：主机密钥判据 ===")
for script in ("_fhostkey.py", "_fshelljoin.py"):
    p = ROOT / "tests" / script
    if not p.is_file():
        check("委托脚本存在: %s" % script, False, str(p))
        continue
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    r = subprocess.run([sys.executable, str(p)], cwd=str(ROOT),
                       capture_output=True, text=True, env=env)
    check("委托判据全绿: %s" % script, r.returncode == 0,
          (r.stdout or "")[-600:] + (r.stderr or "")[-400:])

print()
if FAIL:
    print("FAILED %d:" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
