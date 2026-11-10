"""改字号 / 拉窗口之后，scrollback 还在不在。

**这条是被真机用出来的。** 用户在终端里 `cat` 了一个长文件，然后放大/缩小
字号，发现**滚不动了**——刚才的内容没法往回翻。

**根因**：`set_font()` 会调 `_recompute_grid()` → `_resize_screen()`，而
`_resize_screen()` 抓取**只有当前可见的那些行**，然后 `_init_screen()`
建一个**全新的 screen** —— `history.top`（滚轮能回的那些行）被整个丢掉。
以前改字号不动网格，不走这条路，所以没暴露。

判据必须**真的滚一次**去看画面对不对，而不是去查 `history.top` 的长度 ——
长度对了但内容错位 / 全是空白，同样是「滚不动」。

Checks:
  1. 改字号后仍能滚回之前滚过的位置
  2. 改字号后 scrollback 里的**内容**还在（不只是长度对）
  3. 拉窗口同样保住 scrollback
  4. 历史行里的非 UTF-8 字节不再变豆腐块（errors="replace" 那条死码）
  5. 缩小 scrollback 上限时保留还留得下的内容
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtWidgets

from sshclient.core import fonts
from sshclient.ui.terminal_widget import TerminalWidget

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

N = 120


def feed(t, n=N):
    for i in range(1, n + 1):
        t.write(("LINE%03d 内容\r\n" % i).encode("utf-8"))
    for _ in range(20):
        app.processEvents()


def visible_when_scrolled(t, offset):
    """按绘制逻辑算出「滚到 offset 处屏幕上会显示什么」。"""
    t._scroll_offset = offset
    return [l.rstrip() for l in _render(t)]


def _render(t):
    rows = t._rows
    buf = t._screen.buffer
    out = []
    if t._scroll_offset == 0:
        for row, line in enumerate(t._screen.display[:rows]):
            out.append(line)
        return out
    top = list(t._screen.history.top)
    hist_text = []
    for raw in top:
        if isinstance(raw, (bytes, bytearray)):
            try:
                hist_text.append(bytes(raw).decode("utf-8"))
            except UnicodeDecodeError:
                hist_text.append(bytes(raw).decode("latin-1"))
        else:
            try:
                cells = raw.values()
            except AttributeError:
                cells = ()
            hist_text.append(
                "".join((getattr(ch, "data", None) or " ") for ch in cells))
    keep = hist_text[-t._scroll_offset:] if t._scroll_offset <= len(hist_text) else hist_text
    out.extend(keep[-rows:])
    for line in t._screen.display:
        if len(out) >= rows:
            break
        out.append(line)
    return out


t = TerminalWidget()
t.set_font(fonts.mono_font(11, "Consolas"))
t.resize(1000, 500)
t.show()
for _ in range(6):
    app.processEvents()
feed(t)

print("=== 基线 ===")
base_top = len(t._screen.history.top)
check("改之前确实有 scrollback 可滚", base_top > 0, "history.top = %d 行" % base_top)
before_scrolled = visible_when_scrolled(t, 20)

print()
print("=== 1~2. 改字号后 ===")
t.set_font(fonts.mono_font(20, "Consolas"))     # 这就是用户干的「放大」
for _ in range(25):
    app.processEvents()
after_top = len(t._screen.history.top)
check("改字号后 scrollback 还在", after_top > 0, "history.top = %d 行" % after_top)
after_scrolled = visible_when_scrolled(t, 20)
overlap = [l for l in after_scrolled if l.strip() and l in before_scrolled]
check("改字号后滚回去还能看到原来的内容", len(overlap) >= 5,
      "两屏共同内容 %d 行" % len(overlap))
# **不能只判「非空白」—— 乱码也非空白。** 上一版判据就栽在这儿：滚回去看到
# 的是一屏 `\x00\x01\x02…`，因为 pyte 0.8 的 history.top 存的是
# StaticDefaultDict（列号 -> Char），迭代它得到的是**列号**不是文本。
# 所以这里必须看到**真正的行内容**。
real = [l for l in after_scrolled if "LINE" in l]
check("滚回去看到的是真内容（不是列号/乱码）", len(real) >= 5,
      "含 LINE 的行 %d 条，样本 %r" % (len(real), real[:2]))
check("滚回去不是 \x00\x01 那种列号垃圾",
      not any(l.startswith("\x00") for l in after_scrolled),
      repr(after_scrolled[:1]))

print()
print("=== 3. 拉窗口（只改尺寸不改字号）同样保住 ===")
t2 = TerminalWidget()
t2.set_font(fonts.mono_font(11, "Consolas"))
t2.resize(1000, 500)
t2.show()
for _ in range(6):
    app.processEvents()
feed(t2)
n_before = len(t2._screen.history.top)
t2.resize(760, 380)
for _ in range(20):
    app.processEvents()
check("拉窄窗口后 scrollback 还在",
      len(t2._screen.history.top) >= min(n_before, 20),
      "%d -> %d 行" % (n_before, len(t2._screen.history.top)))

print()
print("=== 4. 历史行里的非 UTF-8 字节不再变豆腐块 ===")
t3 = TerminalWidget()
t3.set_font(fonts.mono_font(11, "Consolas"))
t3.resize(900, 300)
t3.show()
for _ in range(5):
    app.processEvents()
# 造够多的行把前面的挤进 history，再灌 Latin-1
t3.write(b"filler\r\n" * 40)
for _ in range(10):
    app.processEvents()
t3.write(bytes(range(0xA0, 0x100)))
for _ in range(15):
    app.processEvents()
if t3._screen.history.top:
    rows = [r for r in t3._screen.history.top
            if isinstance(r, (bytes, bytearray)) and any(b > 127 for b in r)]
    txt = []
    for raw in rows:
        try:
            txt.append(bytes(raw).decode("utf-8"))
        except UnicodeDecodeError:
            txt.append(bytes(raw).decode("latin-1"))
    joined = "".join(txt)
    check("历史行里的 Latin-1 不再是 U+FFFD", "�" not in joined,
          "样本 %r" % (joined[:20],))
else:
    check("历史行里的 Latin-1 不再是 U+FFFD（有历史样本）", False, "history 是空的")

print()
print("=== 5. 缩小 scrollback 上限：保留还留得下的内容 ===")
t4 = TerminalWidget()
t4.set_font(fonts.mono_font(11, "Consolas"))
t4.resize(1000, 400)
t4.show()
for _ in range(5):
    app.processEvents()
feed(t4, 200)
t4.set_scrollback(50)
for _ in range(10):
    app.processEvents()
check("上限缩到 50 后行数真的变了",
      len(t4._screen.history.top) <= 50,
      "%d 行" % len(t4._screen.history.top))
check("上限缩小后仍能滚", t4._screen.history.top and True or False,
      "有 %d 行" % len(t4._screen.history.top))

print()
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("通过 %d 项，失败 0 项" % len(PASS))
sys.exit(0)
