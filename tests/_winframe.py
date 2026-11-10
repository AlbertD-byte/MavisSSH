"""窗口外框 + 终端可视滚动条：为什么这两条判据这么写。

## N1 窗口外框

**QSS 的 `border` 在这里画不出来**，实测（400x300 无边框窗口，1/2/3px 三档）：
给 `QMainWindow` 加 `QMainWindow { border: Npx solid X }` 之后，四条边的像素
**全是底色，命中 0 px**。机制是 `setCentralWidget` 把 central 满铺在
`(0,0,W,H)`，而应用级 `QWidget { background-color: root_bg }` 让它不透明
—— 边框被自己的 central 盖光。所以只能自绘浮层。

判据落在**像素**上：8 个角/边采样点必须都等于 `border_strong`。
「控件存在」「setGeometry 调过了」这类断言在这里是恒真的 —— 实现
完全可以把颜色设错、把画笔宽度写成 0、或者只画三条边，测试照样绿。

**为什么不能用 `accent`。** `_CLAMP["light"]` 上限 0.97 明确放行近白，
用户把强调色调成 `#ffffff` 时它与 `bg_base` 的对比度塌到 1.083 ——
边框直接消失，且没有任何测试会红。`border_strong`（浅 3.167 / 深 3.398）
是唯一在任意强调色下都稳的。

## N2 终端滚动条

**必须浮层，不进布局。** `_recompute_grid` 是 `(width - pad_x*2) // char_w`，
实测 12px 的条放进布局正好掉 1 列 → 触发 `resize_pty` → 远端已输出的内容
reflow。用户只是滚一下历史，不该改列数。

**方向是反的**：`_scroll_offset` 是「离底部多少行」（0 = 底部），
`QScrollBar.value` 是「越大越靠下」。不显式翻转就是「拖到底 = 跳到历史最老」。

**双向同步是最容易漏的一半。** 只做 offset → 条，拖条时画面纹丝不动而条
还在动，看起来像条坏了。判据必须量「拖条之后 `_scroll_offset` 真的变了」。

另外两条容易出错的：range 必须跟着 `set_scrollback` 走（否则滑块到底了
offset 没到底）；`singleStep` 必须等于滚轮一格的 3 行（默认 1 会两套速度打架）。

Checks:
  1. 深浅两套主题 × 两种尺寸，外框 8 个采样点全部命中
  2. 外框不吃鼠标事件（否则最外 1px 的缩放带和贴边分屏手势会失效）
  3. 外框宽度 ≤ RESIZE_MARGIN（不能压掉缩放带）
  4. 换主题后外框颜色跟着换
  5. 滚动条浮在右边距里，不压字
  6. 滚历史**不改变** cols / rows（防「布局占位」回归）
  7. 拖滚动条 → `_scroll_offset` 真的跟着变（防「只测控件存在」）
  8. offset → 条 也是通的（另一半方向）
  9. `set_scrollback` 后 range 跟着走
 10. `singleStep` == 滚轮一格的行数
 11. 方向翻转了（value=0 表示在底部）
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pathlib
import tempfile

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import settings as S
from sshclient.ui import main_window as MW
from sshclient.ui.terminal_widget import _WHEEL_STEP, TerminalWidget

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mavis_frame_"))
_store = S.SessionStore(_tmp / "sessions.json")


def _win(theme, size):
    st = S.AppSettings()
    st._path = _tmp / ("s_%s_%dx%d.json" % (theme, *size))
    st.theme = theme
    w = MW.MainWindow(st, _store)
    w.resize(*size)
    w.show()
    app.processEvents()
    return w


def _shot(w):
    img = QtGui.QImage(w.size(), QtGui.QImage.Format.Format_RGB32)
    img.fill(0)
    w.render(img)
    return img


# --------------------------------------------------------------------------
# N1 窗口外框
# --------------------------------------------------------------------------
print("\n=== N1 窗口外框（像素判据，不是「控件存在」）===")
# **采样点必须避开圆角。**
#
# 外框画的是 `drawRoundedRect`（`window_frame.py`，`CORNER_RADIUS`）——
# 四个角是**圆弧**，不是直角。实测角上 (0,0) 是纯黑（透明区透出底板），
# 沿上边走到 x=5 才回到纯描边色 #5f6a84。
#
# 原来这里把 8 个点（含四个角）都要求命中描边色，那是在**量直角矩形**，
# 而产品画的是圆角矩形 —— 于是这四条永远红。判据测错了对象。
#
# 现在分成两件事：
#   ① **四条直边**上的点必须命中描边色（采样点取距角足够远的位置）；
#   ② **四个角**必须**不是**描边色 —— 这正是「确实是圆角」的证据，
#      而它以前是被当成失败条件的。
for theme in ("dark", "light"):
    for size in ((1000, 700), (1360, 860)):
        w = _win(theme, size)
        img = _shot(w)
        p = w.palette_
        want = p.border_strong.lower()
        W, H = w.width(), w.height()
        # 距角 12px 处取点：远大于圆角半径，直边上一定是纯描边色。
        _inset = 12
        edge_pts = {"上": (W // 2, 0), "下": (W // 2, H - 1),
                    "左": (0, H // 2), "右": (W - 1, H // 2),
                    "上(近左)": (_inset, 0), "上(近右)": (W - 1 - _inset, 0),
                    "下(近左)": (_inset, H - 1), "下(近右)": (W - 1 - _inset, H - 1)}
        got = {k: QtGui.QColor(img.pixelColor(x, y)).name().lower()
               for k, (x, y) in edge_pts.items()}
        miss = [k for k, v in got.items() if v != want]
        check("%s %dx%d 外框直边 %d 个采样点全命中 %s"
              % (theme, W, H, len(edge_pts), p.border_strong),
              not miss, "缺 " + "、".join(miss) if miss else "%d/%d"
              % (len(edge_pts), len(edge_pts)))
        # 角上**不是**描边色 —— 证明画的是圆角而不是方角。
        corner_pts = {"左上": (0, 0), "右上": (W - 1, 0),
                      "左下": (0, H - 1), "右下": (W - 1, H - 1)}
        corner_got = {k: QtGui.QColor(img.pixelColor(x, y)).name().lower()
                      for k, (x, y) in corner_pts.items()}
        not_rounded = [k for k, v in corner_got.items() if v == want]
        check("%s %dx%d 外框四角是圆角（角上不是描边色）" % (theme, W, H),
              not not_rounded, "角上竟是描边色：" + "、".join(not_rounded))
        mid = QtGui.QColor(img.pixelColor(W // 2, H // 2)).name().lower()
        check("%s %dx%d 外框没有把中间整块盖死" % (theme, W, H), mid != want, mid)
        w.close()

w = _win("dark", (1000, 700))
check("外框不吃鼠标事件（否则缩放带和贴边分屏手势失效）",
      w._frame.testAttribute(QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents))
from sshclient.ui.titlebar import RESIZE_MARGIN

check("外框宽度不超过缩放带（%dpx）" % RESIZE_MARGIN,
      getattr(w._frame, "_width", 1) <= RESIZE_MARGIN, "环宽 1px")

w.apply_theme("light")
app.processEvents()
p = w.palette_
img = _shot(w)
W, H = w.width(), w.height()
edge_ok = QtGui.QColor(img.pixelColor(W // 2, 0)).name().lower() == p.border_strong.lower()
check("换主题后外框颜色跟着换（浅色 %s）" % p.border_strong, edge_ok)
w.close()

# 元断言：把颜色换成底色，那 8 个点必须**不再**命中 —— 抓不到就说明上面恒真
w = _win("dark", (1000, 700))
w._frame._color = w.palette_.bg_base
w._frame.update()
app.processEvents()
img = _shot(w)
W, H = w.width(), w.height()
same = QtGui.QColor(img.pixelColor(W // 2, 0)).name().lower() == w.palette_.bg_base.lower()
check("元断言：把描边色换成底色后不再命中（否则上面几条恒真）", same)
w.close()


# --------------------------------------------------------------------------
# N2 终端滚动条
# --------------------------------------------------------------------------
print("\n=== N2 终端可视滚动条 ===")
t = TerminalWidget()
t.resize(900, 500)
t._recompute_grid()
t._place_sb()

text_right = t._pad_x + t._cols * t._char_w
check("滚动条整个落在右边距里，不压字",
      t._sb.geometry().left() >= text_right,
      "条 x=%d 文本右界=%d" % (t._sb.geometry().left(), text_right))
check("滚动条不占布局（父级没有 layout，子控件不参与）",
      t.layout() is None, "layout=%r" % (t.layout(),))

before = (t._cols, t._rows)
for _ in range(30):
    t._scroll_offset = min(t._scrollback_limit, t._scroll_offset + _WHEEL_STEP)
    t._sync_sb()
t._recompute_grid()
check("滚历史不改变 cols / rows（防布局占位回归）",
      (t._cols, t._rows) == before,
      "%s -> %s" % (before, (t._cols, t._rows)))

t._scroll_offset = 0
t._sync_sb()
t._sync_sb_range()
# **先灌历史，否则下面这些测不到东西。**
#
# 滚动条的范围是「**实际**有多少行历史可以往上滚」
# （`terminal_widget._history_len()` = `min(len(history.top), _scrollback_limit)`），
# 不是设置里那个 scrollback 数字。刚 new 出来的控件历史是空的 → range [0,0]
# → 拖条当然不产生任何 offset 变化。于是原来那几条「拖条 → offset 跟着变」
# 在空历史上量，量到的是「没有历史可滚」，不是「条与 offset 没联动」。
#
# 灌 200 行进去，让 range 真的有上界。
for _i in range(200):
    t.write(("line %d\r\n" % _i).encode("utf-8"))
t._sync_sb_range()
_have = t._history_len()
check("灌入历史后确实有可滚的历史", _have > 0, "history_len=%d" % _have)

t._scroll_offset = 0
t._sync_sb()
t._sb.setValue(min(137, t._sb.maximum()))
check("拖滚动条 → _scroll_offset 真的跟着变（防「只测控件存在」）",
      t._scroll_offset == t._sb.value() and t._scroll_offset > 0,
      "offset=%d value=%d" % (t._scroll_offset, t._sb.value()))

t._scroll_offset = min(321, t._sb.maximum())
t._sync_sb()
check("另一半方向：offset → 滚动条",
      t._sb.value() == t._scroll_offset,
      "value=%d offset=%d" % (t._sb.value(), t._scroll_offset))

# **range 跟着实际历史行数走，不是写死的设置值。**
#
# 原来这里断言 `maximum == 2000`（= 设置里的 scrollback 数）。但实现已经改成
# 「范围跟着**实际**历史行数走」（`terminal_widget._sync_sb_range` 的注释
# 写明了这个改动的原因：历史被 maxlen 截断时，条不该还显示能滚很远）。
# 于是设置值与 range 不再相等 —— **产品是对的，判据过期了**。
#
# 现在钉性质：range 上界 == 实际可滚的历史行数，且**不超过**设置上限。
check("滚动条 range 跟着实际历史行数走（不是写死的设置值）",
      t._sb.maximum() == t._history_len() and t._sb.maximum() <= t._scrollback_limit,
      "maximum=%d history_len=%d limit=%d"
      % (t._sb.maximum(), t._history_len(), t._scrollback_limit))

t.set_scrollback(2000)
t._sync_sb_range()
check("set_scrollback(2000) 后 limit 跟着走，且 range 仍等于实际历史行数",
      t._scrollback_limit == 2000 and t._sb.maximum() == t._history_len(),
      "maximum=%d history_len=%d limit=%d"
      % (t._sb.maximum(), t._history_len(), t._scrollback_limit))
t.set_scrollback(100)
t._sync_sb_range()
check("调小上限后 offset 被夹住且 range 同步",
      t._sb.maximum() == t._history_len() and t._scroll_offset <= t._sb.maximum(),
      "maximum=%d offset=%d history_len=%d"
      % (t._sb.maximum(), t._scroll_offset, t._history_len()))

check("singleStep 与滚轮一格的行数一致（默认 1 会两套速度打架）",
      t._sb.singleStep() == _WHEEL_STEP,
      "singleStep=%d 滚轮=%d" % (t._sb.singleStep(), _WHEEL_STEP))

t._scroll_offset = 0
t._sync_sb()
check("方向已翻转：offset=0（贴底部）时 value 也是 0",
      t._sb.value() == 0 and t._sb.invertedAppearance(),
      "value=%d inverted=%s" % (t._sb.value(), t._sb.invertedAppearance()))
t._sb.setValue(0)
check("拖到 value=0 就是回到最新，不是跳到历史最老",
      t._scroll_offset == 0, "offset=%d" % t._scroll_offset)

print()
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("通过 %d 项，失败 0 项" % len(PASS))
sys.exit(0)
