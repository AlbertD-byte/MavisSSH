"""滚回历史时重绘的代价：必须与「往上滚了多少行」无关。

用户报的是「终端 `cat` 一个 314KB 的文件，用右侧滚动条滚动，整个页面直接
卡死」。实测（真实窗口 1600x900，网格 197x52，灌 4000 行 = 历史 5000 行）：

    改前                          改后
    offset=0       78 ms         83 ms
    offset=500    694 ms         86 ms
    offset=1000  1337 ms         84 ms
    offset=2000  2799 ms         81 ms
    offset=3000  3896 ms         80 ms
    offset=4000  5979 ms         83 ms

## 根因

`paintEvent` 的历史分支原来把 `history.top` **整份**解成文本（几千行），
再用 `[-offset:]` 切尾巴，然后把切出来的几千行**全部**塞进 `view_lines`。
屏幕上只看得见 `rows` 行。而上限逻辑**只写在实时屏那半**：

    for row, line_text in enumerate(self._screen.display):
        if len(view_lines) >= rows:      # ← 只有这里有上限
            break

于是 `len(view_lines) == max(offset, rows)`，几千行每帧照画不误。拖动时
`valueChanged` 连续触发，几秒一帧，事件队列永远排不空。

## 这份判据钉三件事

1. **每帧解码的历史行数 ≤ 屏幕行数** —— 确定性不变量，不依赖计时。
2. **可见内容逐像素不变** —— 这次是纯性能修正，不是行为变更。用「把
   `_history_view_span` 换回旧算法」复现改前的取行区间，两边渲染出来的
   图必须一模一样。这同时也是元断言：旧区间不只是慢，它就是改前的样子。
3. **拖动全程单帧耗时** —— 报绝对值，判据用「不随 offset 增长」而不是
   固定毫秒阈值（后者在机器负载下会自己红）。
"""
import os
import pathlib
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import fonts
from sshclient.ui import terminal_widget as tw
from sshclient.ui.design.v1 import palette

#: 一条足够长的日志行：灌进去能把内容顶出可视区，堆成几千行历史。
PAYLOAD = ("\x1b[32mINFO \x1b[0m2026-10-05 15:23:02.431 "
           "[http-nio-8080-exec-9] ERROR o.a.a.c.C.C. - "
           "Servlet.service() for servlet [dispatcherServlet] in context "
           "with path [/vod] threw exception [Request processing failed]\r\n")
FILL_LINES = 2600

bad = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r} (want {want!r})")
    if not ok:
        bad.append(f"{name}: got {got!r}, want {want!r}")


def check_true(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        bad.append(f"{name}  {detail}")


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
w = tw.TerminalWidget()
w.resize(1200, 760)
w.set_font(fonts.mono_font(11))
w.apply_palette(palette.get_palette("dark"))
w.show()


def settle(ms=120):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


settle(300)

t0 = time.perf_counter()
for _ in range(FILL_LINES // 60):
    w.write((PAYLOAD * 60).encode("utf-8"))
    app.processEvents()
settle(200)
print(f"灌入 {FILL_LINES} 行耗时 {time.perf_counter() - t0:.1f}s")
h = len(w._screen.history.top)
print(f"网格 {w._cols}x{w._rows}   历史 {h} 行   可滚 {w._history_len()} 行")
check_true("历史确实攒到了几千行（否则这条判据量的是空场景）", h > 2000,
           f"h={h}")

# --------------------------------------------------- 每帧解码多少行历史
_real_decode = tw._history_row_text
counter = {"n": 0}


def counting(raw, placeholder):
    counter["n"] += 1
    return _real_decode(raw, placeholder)


tw._history_row_text = counting


def paint(offset):
    w._scroll_offset = offset
    w._sync_sb()
    pix = QtGui.QPixmap(w.size())
    w.render(pix)
    counter["n"] = 0
    t = time.perf_counter()
    w.render(pix)
    el = (time.perf_counter() - t) * 1000
    return el, counter["n"], pix.toImage()


OFFSETS = [0, 1, 5, w._rows - 1, w._rows, w._rows + 1, 200,
           500, 1200, h - 2, h - 1]
OFFSETS = [o for o in OFFSETS if 0 <= o <= w._history_len()]

print("\n--- 每帧解码的历史行数 / 单帧耗时 ---")
times = {}
for off in OFFSETS:
    ms, decoded, _img = paint(off)
    times[off] = ms
    print(f"  offset={off:<5} 解码历史行 {decoded:>4}  (屏幕 {w._rows} 行)  "
          f"单帧 {ms:7.1f} ms")
    # 屏幕只显示 rows 行，多解一行都是白做。
    check_true(f"offset={off} 每帧解码行数不超过屏幕行数",
               decoded <= w._rows, f"{decoded} > {w._rows}")

tw._history_row_text = _real_decode

# ---------------------------------------------- 耗时不随 offset 增长
base = min(times[o] for o in OFFSETS if o > 0)
worst = max(times[o] for o in OFFSETS if o > 0)
print(f"\n  非零 offset 单帧：最快 {base:.1f} ms  最慢 {worst:.1f} ms  "
      f"比值 {worst / base:.2f}x")
check_true("单帧耗时不随 offset 增长（不随历史量增长）",
           worst <= base * 3.0, f"{worst / base:.2f}x")
check_true("拖到历史深处单帧仍在可交互范围（< 400ms）", worst < 400.0,
           f"{worst:.1f} ms")

# ------------------------------- 可见内容逐像素不变（纯性能修正的证据）
# 把 `_history_view_span` 换回改前的算法：start 一样、stop 改成历史末尾，
# 于是 view_lines 又变成几千行 —— 屏幕上能看见的前 rows 行应当完全一致。
#
# **只比网格之内，也就是最后一行文字的下沿。** 改前那个区间在
# `offset > rows` 时会多塞一行进 `view_lines`，而绘制循环没有
# `row >= rows` 的断点，于是**网格之下那圈留白里也会被画上一行历史**
# （实测 offset=44 / rows=43 时多出一整行文字的像素）。裁剪边界必须是
# `pad_y + rows*char_h` 而不是 `pad_y*2 + rows*char_h` —— 后者把多出来那
# 行的上半截也圈了进来，比对就会因为**改前多画的**那部分而红。
grid_bottom = w._pad_y + w._rows * w._char_h
print(f"\n--- 改前取行区间 vs 改后：网格内(0..{grid_bottom}px)可见内容是否一致 ---")
_orig_span = tw.TerminalWidget._history_view_span


def _old_span(self, rows):
    h = len(self._screen.history.top)
    start = max(0, h - self._scroll_offset)
    return start, h          # 改前：切完尾巴把剩下的全塞进 view_lines


def crop(img):
    return QtGui.QImage(img.copy(0, 0, img.width(), grid_bottom))


for off in [o for o in OFFSETS if o > 0][:6]:
    _, _, img_new = paint(off)
    tw.TerminalWidget._history_view_span = _old_span
    try:
        _, _, img_old = paint(off)
    finally:
        tw.TerminalWidget._history_view_span = _orig_span
    a, b = crop(img_new), crop(img_old)
    same = a == b
    diff = sum(1 for y in range(grid_bottom)
               for x in range(0, a.width(), 3)
               if a.pixel(x, y) != b.pixel(x, y))
    print(f"  offset={off:<5} 网格内逐像素相同={same}   抽样不同像素 {diff}")
    check_true(f"offset={off} 网格内可见内容与改前一致（纯性能修正）", same)

# 改前多画的那一行不该被当成「保留」—— 网格之下就是留白，不该有文字。
# 钉成判据，免得以后谁为了别的理由把行数上限去掉时无人察觉。
print("\n--- 网格之下不该有文字 ---")
deep = min(OFFSETS[-1], w._history_len())
_, _, img = paint(deep)
bg = img.pixelColor(5, img.height() - 3).name()
dirty = sum(1 for y in range(grid_bottom, img.height())
            for x in range(0, img.width(), 2)
            if img.pixelColor(x, y).name() != bg)
print(f"  offset={deep}：网格之下 {img.height() - grid_bottom}px 里 "
      f"{dirty} 个非背景像素（背景 {bg}）")
check_true("网格之下是干净的留白", dirty == 0, f"{dirty} 个脏像素")

# ------------------------------------------------------------- 元断言
# 上面那条「相同」用的是**改前会走的那段取行逻辑**。如果它恒绿，
# 就分不清「两版真的画了一样的东西」和「两版画的都是空白」。所以显式确认
# 改前的区间确实会多做很多活 —— 否则第 1 条判据就成了摆设。
print("\n--- 元断言：改前的区间确实会多解几千行 ---")
tw.TerminalWidget._history_view_span = _old_span
counter["n"] = 0
tw._history_row_text = counting
try:
    ms_old, _, _ = paint(max(OFFSETS))
finally:
    tw.TerminalWidget._history_view_span = _orig_span
    tw._history_row_text = _real_decode
new = times[max(OFFSETS)]
print(f"  同一 offset 下：改前区间解码 {counter['n']} 行 / {ms_old:.1f} ms，"
      f"改后 {w._rows} 行 / {new:.1f} ms")
check_true("元断言：改前的区间会解出远超一屏的行", counter["n"] > w._rows * 5,
           f"{counter['n']} 行")
check_true("元断言：改前的区间明显更慢", ms_old > new * 2,
           f"{ms_old:.1f} ms vs {new:.1f} ms")

# --------------------------------------------------------- 选区复制同一病灶
# `selected_text` 原来也把几千行历史全解一遍，然后只用到选区那几十行 ——
# 「滚回历史 → 选中 → Ctrl+C」会再卡一次。两处现在共用同一个区间函数。
print("\n--- 选区复制：解出来的历史行数也必须有界 ---")
w._scroll_offset = min(2000, w._history_len())
w._sync_sb()
w._sel_anchor = (0, 0)
w._sel_cur = (5, w._rows - 1)
counter["n"] = 0
tw._history_row_text = counting
try:
    txt = w.selected_text()
finally:
    tw._history_row_text = _real_decode
print(f"  滚回 {w._scroll_offset} 行后全选：解出 {counter['n']} 行，"
      f"取到 {len(txt.splitlines())} 行")
check_true("复制路径解码行数不超过屏幕行数", counter["n"] <= w._rows,
           f"{counter['n']} > {w._rows}")
check_true("复制确实拿到了内容", len(txt.strip()) > 0)

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("scrollback repaint: all checks passed")
sys.exit(1 if bad else 0)
