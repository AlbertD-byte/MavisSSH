"""终端选区：高亮的列范围必须与复制出来的列范围**逐格一致**。

用户报「选中复制某行的某些字符，无法精准选中」，截图里选中的是
``HIGHLIGHTS.md``。量那张图的高亮底色（``#5e93d6``）的 x 范围是 **12..1045**，
也就是**整行宽度** —— 而不是 13 个字符宽度。

## 两个缺陷，同一个「两份实现」病灶

1. **单行选区的高亮是整行。** `paintEvent` 里那份三分支写成

       if row == y1 and row != y2:      # 首行
       elif row == y2 and row != y1:    # 末行
       else:                            # ← 单行掉进这里
           (0, row, self._cols, ...)

   单行时前两个条件都要求 `!=`，于是落到 `else` 画成整行；而复制走
   `_sel_row_slice` 的单行分支只取选中的那几列。**复制其实是对的**，所以
   用户看到的是一个对不上的画面：既没法目视确认选了哪一段，也没法靠肉眼
   判断该往哪边再拖一点 —— 这就是「无法精准选中」。

2. **从右往左拖同一行，复制出空串。** `_sel_span` 原来只有一个 `ay <= cy`
   判断，同一行时列没归一化，于是 `(xt, xb) = (12, 0)`，切成 `line[12:1]`。
   多行拖动没这个问题（首末行按行定，与列的左右无关），只有单行把列当成了
   有向区间。

## 判据钉的是「同一个函数的两个消费者」

不钉某一行的具体像素，而是钉一条不变量：**高亮覆盖的列集合 == 复制取到的
字符集合**，在所有起止列组合上成立。这条一旦成立，上面两类缺陷都不可能存在
——不管将来是绘制还是复制改了，只要两边还共用 `_sel_row_span`。
再用像素判据把「看起来对不对」也钉住（用户就是靠眼睛发现问题的）。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import fonts
from sshclient.ui import terminal_widget as tw
from sshclient.ui.design.v1 import palette

#: 每一列一个可辨认的字符 —— 复制出来能逐字对照到具体列，
#: 用 ``abc`` 这种真实文本做判据，列与字符的对应是隐式的。
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

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
w.resize(900, 520)
w.set_font(fonts.mono_font(11))
w.apply_palette(palette.get_palette("dark"))
w.show()


def settle(ms=150):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


w.write(ALPHABET.encode("ascii") + b"\r\n")
w.write(b"row-one\r\n")
w.write(b"row-two\r\n")
settle(250)
COLS = w._cols
print(f"网格 {w._cols}x{w._rows}")
check_true("第 0 行就是那串可辨认的字符",
           w._screen.display[0][:len(ALPHABET)] == ALPHABET,
           repr(w._screen.display[0][:20]))


def select(ax, ay, cx, cy):
    w._sel_anchor = (ax, ay)
    w._sel_cur = (cx, cy)
    return w._sel_span()


def covered_cols(y1, y2, xt, xb):
    """高亮覆盖的列集合（用绘制真正调的那个函数算）。"""
    out = set()
    for row in range(y1, y2 + 1):
        left, right = w._sel_row_span(row, y1, y2, xt, xb)
        out.update(range(left, right))
    return out


# ------------------------------------------------------- 1. 单行：高亮 == 复制
print("\n--- 单行选区：高亮列集合必须等于复制出来的字符 ---")
PAIRS = [(0, 12), (0, 13), (0, 0), (3, 8), (12, 0), (8, 3), (5, 5),
         (0, 20), (20, 0), (0, 60), (60, 0), (17, 42), (42, 17)]
mismatch = []
for a, b in PAIRS:
    y1, y2, xt, xb = select(a, 0, b, 0)
    cols = covered_cols(y1, y2, xt, xb)
    got = w.selected_text()
    want = "".join(ALPHABET[c] for c in sorted(cols) if c < len(ALPHABET))
    ok = got == want
    if not ok:
        mismatch.append(f"拖 {a}->{b}: 高亮列 {min(cols)}..{max(cols)} 得到 {got!r}，"
                        f"应当是 {want!r}")
    print(f"  拖 {a:>2}->{b:<2} 高亮列 [{min(cols):>2}..{max(cols):<2}] "
          f"({len(cols):>2} 列)  复制={got!r}")
check_true("单行：高亮列集合 == 复制出的字符（全部组合）", not mismatch,
           "; ".join(mismatch))

select(0, 0, 12, 0)
check("拖 0->12", w.selected_text(), ALPHABET[0:13])
select(12, 0, 0, 0)
check("从右往左拖 12->0（同一行）", w.selected_text(), ALPHABET[0:13])
check_true("从右往左拖不再得到空串", w.selected_text() != "")

# --------------------------------------------- 2. 多行：首行/末行/中间行不变
print("\n--- 多行选区：语义不回归 ---")
select(0, 0, 3, 1)
span = w._sel_span()
check("首行从起点列起到行尾", w._sel_row_span(0, *span), (0, w._cols))
check("末行到光标列止（含光标格）", w._sel_row_span(1, *span), (0, 4))
select(3, 1, 0, 0)
span = w._sel_span()
check("反向拖：首末角色互换", span, (0, 1, 0, 3))
check("反向拖首行高亮", w._sel_row_span(0, *span), (0, w._cols))
check("反向拖末行高亮", w._sel_row_span(1, *span), (0, 4))
select(0, 0, 5, 2)
span = w._sel_span()
check("三行选区：中间行整行宽", w._sel_row_span(1, *span), (0, w._cols))
check("三行选区：末行到光标列", w._sel_row_span(2, *span), (0, 6))

# ------------------------------------------------------------- 3. 像素判据
# 用户就是靠眼睛发现问题的，所以像素那一层也得钉。
print("\n--- 像素：高亮底色只出现在选中的列上 ---")
sel_bg = w._selection_bg.name()


def columns_with_sel_bg(ay):
    w._sel_anchor = (3, ay)
    w._sel_cur = (7, ay)
    settle(60)
    img = w.grab().toImage()
    # **取格子靠上的那一行，不要取格子正中。** 选区底色是整格 `fillRect`，
    # 而格子正中压着字形 —— 那一像素是选中态的**前景色**或字形的抗锯齿边，
    # 于是判据量到的是「有几个字形恰好没盖住正中」，而不是高亮范围。
    # 实测取正中时 5 列里只有 1 列命中，元断言也跟着失效。
    y = w._pad_y + w._char_h * ay + 2
    got = []
    for c in range(COLS):
        x = w._pad_x + int((c + 0.5) * w._char_w)
        got.append(img.pixelColor(x, y).name() == sel_bg)
    return [c for c, on in enumerate(got) if on]


hit = columns_with_sel_bg(0)
print(f"  选中 3..7，底色 {sel_bg} 出现在列 {hit}")
check("底色精确覆盖 3..7（不多不少）", hit, [3, 4, 5, 6, 7])
check("第 0 列没有被选中", 0 in hit, False)
check("第 12 列（`C`）没有被选中", 12 in hit, False)

hit2 = columns_with_sel_bg(0)
check("同一选区两次渲染一致（可重复）", hit2, hit)

# ---------------------------------------------------------------- 4. 元断言
# 把单行的高亮改回「整行」，上面那条像素判据**必须**转红。
# 否则它分不清「高亮真的只盖住了 3..7」和「判据压根没量高亮」。
print("\n--- 元断言：把单行高亮改回整行，像素判据会红 ---")
_orig = tw.TerminalWidget._sel_row_span


def _old_row_span(self, row, y1, y2, x_top, x_bot):
    if y1 == y2:                       # 改前：单行掉进 else，画成整行
        return 0, self._cols
    if row == y1:
        return x_top, self._cols
    if row == y2:
        return 0, x_bot + 1
    return 0, self._cols


tw.TerminalWidget._sel_row_span = _old_row_span
try:
    hit_old = columns_with_sel_bg(0)
finally:
    tw.TerminalWidget._sel_row_span = _orig
print(f"  改回整行后底色出现在 {len(hit_old)} 列（原样 {len(hit)} 列）")
check_true("元断言：改回整行后第 0 列也被涂上", 0 in hit_old)
check_true("元断言：覆盖列数明显变多", len(hit_old) > len(hit) + 10,
           f"{len(hit_old)} vs {len(hit)}")

w._sel_anchor = None
w._sel_cur = None

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("selection columns: all checks passed")
sys.exit(1 if bad else 0)
