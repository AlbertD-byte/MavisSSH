"""终端滚动条：范围必须跟着**实际**历史行数走，而不是写死的保留上限。

原来 `setRange(0, _scrollback_limit)` 上界恒为 5000，于是条和真实内容完全
脱钩：实测灌 50 行（历史 23 行）与灌 236 行，range 都是 [0,5000]、滑块都是
584px 里的 18px —— 内容涨了十倍，界面上一点都看不出来，条永远像坏的。

可执行：`python -u tests/_sbscale.py`
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from PyQt6 import QtCore, QtWidgets  # noqa: E402

app = QtWidgets.QApplication(sys.argv)

from sshclient.ui.terminal_widget import TerminalWidget  # noqa: E402

FAIL: list[str] = []
OK = 0


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


w = TerminalWidget()
w.resize(900, 600)
w.show()
app.processEvents()


def thumb_pct() -> float:
    """滑块实际占了槽高的百分之多少 —— 直接扫**画出来的**像素，
    不反推 QStyle 公式（那条路上 PyQt6 的枚举名就叫不对）。"""
    img = w._sb.grab().toImage()
    bg = img.pixel(1, 1)
    hit = [y for y in range(img.height())
           if any(img.pixel(x, y) != bg for x in range(img.width()))]
    if not hit:
        return 0.0
    return 100.0 * (max(hit) - min(hit) + 1) / img.height()


def feed(n):
    w.write(("\n".join("line-%d" % i for i in range(n))).encode("utf-8"))
    app.processEvents()


print("\n=== 范围跟着实际内容走 ===")
check("刚建好时没有历史，range 上界是 0（不是 5000）",
      w._sb.maximum() == 0, w._sb.maximum())

feed(50)
n50 = len(w._screen.history.top)
check("灌 50 行后 range 上界 == 实际历史行数 %d" % n50,
      w._sb.maximum() == n50, "actual=%d sb=%d" % (n50, w._sb.maximum()))
p50 = thumb_pct()

feed(200)
n200 = len(w._screen.history.top)
check("灌到 200 行后 range 上界 == 实际历史 %d" % n200,
      w._sb.maximum() == n200, "actual=%d sb=%d" % (n200, w._sb.maximum()))
p200 = thumb_pct()

print("\n=== 滑块大小真的随内容变化（不是恒定那条）===")
check("内容从 %d 行涨到 %d 行，滑块从 %.1f%% 缩到 %.1f%%"
      % (n50, n200, p50, p200), p200 < p50, "%.1f vs %.1f" % (p50, p200))
# 元断言：证明上面那条不是恒绿——只有 20 行历史时滑块必须明显大于 5000 行时
feed(3000)
p3000 = thumb_pct()
check("元断言：历史撑满时滑块比刚才小得多（%.1f%% < %.1f%%）"
      % (p3000, p200), p3000 < p200, "%.1f vs %.1f" % (p3000, p200))

print("\n=== 没有可滚的历史就隐藏 ===")
w._screen.history.top.clear()
w._sync_sb_range()
app.processEvents()
check("历史清空后 range 归 0", w._sb.maximum() == 0, w._sb.maximum())
check("历史清空后滚动条隐藏（不是画一根占满的滑块骗人）",
      not w._sb.isVisible())
feed(60)
app.processEvents()
check("内容一多又自动出现", w._sb.isVisible() and w._sb.maximum() > 0,
      "visible=%s max=%d" % (w._sb.isVisible(), w._sb.maximum()))

print("\n=== 滚轮与拖条的夹取上界是真实历史，不是 5000 ===")
n = w._history_len()
for _ in range(500):
    w._scroll_offset = min(n, w._scroll_offset + 3)
w._sync_sb()
check("猛滚 500 下之后 offset == 实际历史 %d" % n,
      w._scroll_offset == n, w._scroll_offset)
check("滑块随之到顶（拖到最老那一端）", w._sb.value() == w._sb.maximum(),
      "%d vs %d" % (w._sb.value(), w._sb.maximum()))

# 元断言：老写法（写死上限）在这一条上必然越界
check("元断言：写死 _scrollback_limit 的老夹取会冲过头",
      w._sb.maximum() < w._scrollback_limit
      and min(w._scrollback_limit, w._scroll_offset + 3) > w._sb.maximum(),
      "sb.max=%d limit=%d" % (w._sb.maximum(), w._scrollback_limit))

print("\n=== offset 不会挂在越界值上 ===")
w._scroll_offset = w._scrollback_limit          # 人为造一个越界 offset
w._sync_sb_range()
check("范围收窄时把 offset 夹回来（否则条和画面对不上）",
      w._scroll_offset <= w._history_len(),
      "offset=%d hist=%d" % (w._scroll_offset, w._history_len()))

print("\n=== 默认停在底部（终端惯例，用户拍板）===")
w._scroll_offset = 0
w._sync_sb()
app.processEvents()
img = w._sb.grab().toImage()
bg = img.pixel(1, 1)
hit = [y for y in range(img.height())
       if any(img.pixel(x, y) != bg for x in range(img.width()))]
check("在实时视图时滑块贴着槽底",
      hit and max(hit) >= img.height() - 15,
      "thumb bottom=%d of %d" % (max(hit) if hit else -1, img.height()))

print("\n=== 光标：终端里是 I 型，滚动条上是箭头 ===")
IBEAM = QtCore.Qt.CursorShape.IBeamCursor
ARROW = QtCore.Qt.CursorShape.ArrowCursor
CROSS = QtCore.Qt.CursorShape.CrossCursor


def cursor_of(widget):
    """解析后的光标形状。注意 `cursor()` 是**沿父链解析**的，见下面元断言。"""
    try:
        return widget.cursor().shape()
    except Exception:                                   # noqa: BLE001
        return None


check("终端本控件仍是 I 型（终端里选文本要 I-beam）",
      cursor_of(w) == IBEAM, str(cursor_of(w)))
check("滚动条上是箭头，不是 I-beam（用户报「拖条时是编辑态的 I」）",
      cursor_of(w._sb) == ARROW, str(cursor_of(w._sb)))

# 元断言：滚动条的光标必须是**它自己**设的，不能是继承来的。
# Qt 的 `QWidget::cursor()` 在本控件没有显式光标时会沿父链找最近一个显式
# 设过光标的祖先，所以「把终端本体的光标也改成箭头」同样能让上面那条变绿 ——
# 代价是终端里选文本没了 I 型。这里把父控件临时换成别的形状再量一次：
# 继承来的会跟着变，自己设的纹丝不动。
keep = w.cursor()
w.setCursor(CROSS)
app.processEvents()
parent_now, sb_now = cursor_of(w), cursor_of(w._sb)
w.setCursor(keep)
app.processEvents()
check("元断言：父控件换成 CrossCursor 确实生效（尺子量得到变化，不是空转）",
      parent_now == CROSS, str(parent_now))
check("元断言：滚动条纹丝不动 —— 光标是它自己的，不是从终端继承的",
      sb_now == ARROW, "父控件换成 Cross 后，滚动条变成 %s" % sb_now)
check("还原后终端回到 I 型", cursor_of(w) == IBEAM, str(cursor_of(w)))

w.close()
app.processEvents()
print("\n通过 %d 项，失败 %d 项" % (OK, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：%s" % f)
    sys.exit(1)
