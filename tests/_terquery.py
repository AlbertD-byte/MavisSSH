"""终端查询（DA / DSR）：必须应答，否则 TUI 开不了工。

**症状**：远端跑 TUI / 某些 CLI 一进去就"页面变型"，顶部多出很多行
``!#$%&'()*+,-./0123…`` 这样的字符集清单，而且每次重绘顶栏就再打一遍。

**根因**：这些程序一启动先问终端"你是什么型号、支持哪些能力"，等一个答复。
原来的 TerminalWidget 只把远端数据喂给 pyte，从不回话——于是对面判定
"不认识这台终端"，走兜底分支把自己的字符集清单当普通文本打到屏幕上。

判据除了"有回话"，还钉了**回话本身没被画到屏幕上**：查询混在输出流里，
不挑出来的话 pyte 会把 `ESC[c` 当可见内容画出来。

拆包那条也是真跑过的：查询可能被 TCP 切成两半。早先一版在"还没收全"时
把 ESC 吐给了 pyte 又不留尾巴，结果是对面照样等不到、屏幕上还多两行乱码。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.fonts import boot

app = boot([])
from sshclient.ui import terminal_widget as TW

PASS, FAIL = [], []
def check(n, ok, d=""):
    (PASS if ok else FAIL).append(n)
    print(("  [ok] " if ok else "  [FAIL] ")+n+("" if ok else "   <- %s"%d))

tmp = pathlib.Path(tempfile.mkdtemp())
w = TW.TerminalWidget(); w.resize(800, 500); w.show()
for _ in range(10): app.processEvents()

class Ch:
    def __init__(self): self.sent = bytearray()
    def sendall(self, d): self.sent.extend(d)
ch = Ch(); w.bind_channel(ch)

CASES = [
    (b"\x1b[c",                       b"\x1b[?62;1;2;6;9;15;22c", "DA1"),
    (b"\x1b[>c",                      b"\x1b[>0;95;0c",           "DA2"),
    (b"\x1b[6n",                      None,                       "DSR 光标位置"),
    (b"\x1b[5n",                      b"\x1b[0n",                 "DSR 设备状态"),
    (b"\x1b[?u",                      b"\x1b[?0u",                "kitty 键盘查询"),
]
for raw, want, name in CASES:
    ch.sent.clear()
    w.write(raw)
    if want is None:
        ok = ch.sent.startswith(b"\x1b[") and ch.sent.endswith(b"R")
    else:
        ok = bytes(ch.sent) == want
    check("%s 有回话" % name, ok, repr(bytes(ch.sent)))
    check("%s 没有被画到屏幕上" % name,
          w._screen.display[0].strip() == "", repr(w._screen.display[0][:40]))

print("\n-- 查询被 TCP 拆包 --")
ch.sent.clear()
w.write(b"abc\x1b")          # 半条
w.write(b"[c")
check("拆成两半的 DA1 也认得出来",
      bytes(ch.sent) == b"\x1b[?62;1;2;6;9;15;22c", repr(bytes(ch.sent)))
check("拆包那次的 abc 仍然上屏", "abc" in w._screen.display[0],
      repr(w._screen.display[0][:30]))

print("\n-- 普通输出不能被吃掉 --")
ch.sent.clear()
w.write(b"hello world\r\n")
check("普通文本原样上屏", "hello world" in "".join(w._screen.display),
      repr("".join(w._screen.display)[:60]))
check("普通文本没触发回话", not ch.sent, repr(bytes(ch.sent)))

print("\n-- 含 ESC 的正常输出 --")
ch.sent.clear()
w.write(b"\x1b[32mgreen\x1b[0m\r\n")
check("SGR 颜色序列照常上屏（不被当查询剥掉）",
      "green" in "".join(w._screen.display), repr("".join(w._screen.display)[:50]))
check("SGR 没触发回话", not ch.sent, repr(bytes(ch.sent)))

print("\n-- 传输闸门开着时不回话 --")
ch.sent.clear()
w._input_blocked = True
w.write(b"\x1b[c")
check("闸门开着时查询不写进通道（会变成传输载荷）", not ch.sent, repr(bytes(ch.sent)))
w._input_blocked = False

print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
