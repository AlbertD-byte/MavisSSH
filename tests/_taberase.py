"""Tab 补全往 shell 发的退格数：按字符还是按字节，取决于远端 locale。

**为什么这条不是「无脑改字节数」。** GNU readline 在 **UTF-8 locale 下按
字符**退格，只有非 UTF-8 locale 才是按字节。原来无条件用 `len(word)`
（字符数）：UTF-8 远端全对，`LANG=C` / `POSIX` 的远端上**一个汉字要退 3 次
却只发 1 次**，补全退不干净，后面的字留在原地。

实测本机（`localhost:22`）的 locale 是 `C.UTF-8`，所以这条在多数机器上
**看不出问题** —— 它是隐患不是现网故障。而**改成无条件字节数更糟**：那在
UTF-8（也就是绝大多数）上会退过头，多半把光标打到行首。

所以正确形态是「问一次远端 locale，按结果算」。

Checks:
  1. UTF-8 远端：退格数 = 字符数（**这一条锁住常见路径不被改坏**）
  2. 非 UTF-8 远端：退格数 = UTF-8 字节数
  3. 纯 ASCII 词：两种 locale 下**必须一致**（改法不能动普通路径）
  4. locale 只问一次，第二回走缓存
  5. `locale charmap` 不可用 / 问不到时按 UTF-8 算（宁可少退不吃命令）
  6. `LANG=en_US.UTF-8` 这类也要认（不能只看 == "utf-8"）
  7. 两处 payload 都用上了同一个宽度
"""
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshclient.ui.main_window import SessionTab

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def tab(charmap: str, fail: bool = False):
    """造一个只带补全相关方法的 SessionTab 替身。"""
    t = SessionTab.__new__(SessionTab)
    t._remote_utf8 = None
    t._conn = types.SimpleNamespace(
        exec=lambda cmd, timeout=5: (("" if fail else charmap), ""))
    return t


print("=== 退格数 =?= 远端是不是 UTF-8 locale ===")
w_cn = "文档"          # 2 个字 = 6 个 UTF-8 字节

t8 = tab("C.UTF-8")
check("UTF-8 远端：按字符退（这一条锁住常见路径）", t8._erase_width(w_cn) == 2,
      "%d（字符数）" % t8._erase_width(w_cn))

tn = tab("ANSI_X3.4-1968")
check("非 UTF-8 远端：按字节退", tn._erase_width(w_cn) == 6,
      "%d（字节数）" % tn._erase_width(w_cn))

print()
print("=== 纯 ASCII 词：两种 locale 下必须一致（改法不能动普通路径）===")
a8, an = tab("C.UTF-8"), tab("ANSI_X3.4-1968")
for w in ("start", "a.log", "src/", "x"):
    check("ASCII %-8r 两种 locale 都是 %d 个退格" % (w, len(w)),
          a8._erase_width(w) == an._erase_width(w) == len(w),
          "%d / %d" % (a8._erase_width(w), an._erase_width(w)))

print()
print("=== locale 只问一次 ===")
calls = {"n": 0}


def counting_exec(cmd, timeout=5):
    calls["n"] += 1
    return ("C.UTF-8", "")


tc = tab("")
tc._conn = types.SimpleNamespace(exec=counting_exec)
tc._erase_width("文")
tc._erase_width("文档")
tc._erase_width("x")
check("问过 3 次只打 1 次网络", calls["n"] == 1, "实际 %d 次" % calls["n"])

print()
print("=== 问不到时按 UTF-8 算（宁可少退，不吃命令）===")
tf = tab("", fail=True)
check("locale 命令失败 -> 按字符退", tf._erase_width(w_cn) == 2,
      "%d" % tf._erase_width(w_cn))
tq = tab("")
check("输出为空（老系统没这命令）-> 按字符退", tq._erase_width(w_cn) == 2,
      "%d" % tq._erase_width(w_cn))

print()
print("=== locale 字符串的判定 ===")
for cm, want in (("C.UTF-8", True), ("en_US.UTF-8", True), ("UTF-8", True),
                 ("utf8", True), ("ANSI_X3.4-1968", False),
                 ("POSIX", False), ("C", False)):
    got = tab(cm)._remote_is_utf8()
    check("charmap=%-16r 判为 UTF-8=%s" % (cm, want), got is want, str(got))

print()
print("=== 两处 payload 都用上了同一个宽度 ===")
import inspect

src = inspect.getsource(SessionTab._local_complete)
n = src.count("self._erase_width(word)")
old = src.count('"\\x7f" * len(word)')
check("唯一补全处 2 处都走 _erase_width", n == 2 and old == 0,
      "_erase_width=%d 处, 残留 len(word)=%d 处" % (n, old))

print()
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("通过 %d 项，失败 0 项" % len(PASS))
sys.exit(0)
