"""终端滚动条 / 窗口拉伸 / 复制 —— 这一轮新增行为的钉桩。

窗口拉伸：无边框窗口实测样式是 WS_POPUP，**没有 WS_THICKFRAME**，而 Windows
只在带 thickframe 的窗口上对 HTLEFT 这类返回值起 SC_SIZE 缩放循环 ——
`hit_test` 边缘码全对（10/11/12/15/13·14·16·17），拖边缘却一点反应都没有。

可执行：`python -u tests/_edgeresize.py`
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from ctypes import wintypes  # noqa: E402

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

app = QtWidgets.QApplication(sys.argv)

from sshclient.core import settings  # noqa: E402
from sshclient.ui import main_window as MW  # noqa: E402
from sshclient.ui import titlebar as T  # noqa: E402

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


tmp = pathlib.Path(os.environ.get("TEMP", ".")) / "mavisprobe_edge"
tmp.mkdir(parents=True, exist_ok=True)
store = settings.SessionStore(tmp / "s.json")
st = settings.AppSettings()
st._path = tmp / "set.json"
st.theme = "dark"

print("\n=== 先钉住「为什么以前拖不动」这个事实 ===")
win = MW.MainWindow(st, store)
win.show()
app.processEvents()

GWL_STYLE = -16
WS_THICKFRAME = 0x00040000
style = ctypes.windll.user32.GetWindowLongW(int(win.winId()), GWL_STYLE)
check("窗口没有 WS_THICKFRAME（这正是系统不接管边缘缩放的原因）",
      not (style & WS_THICKFRAME), "style=0x%08X" % (style & 0xFFFFFFFF))

print("\n=== 七条边的命中码都接上了缩放映射（**顶边除外**）===")
tb = win.titlebar
W, H = win.width(), win.height()
E = QtCore.Qt.Edge
# **顶边不再是可以缩放的那一条。** 标题栏满宽铺在窗口最上面，顶边 100%
# 落在标题栏里，而 `y <= RESIZE_MARGIN` 的判定排在标题栏前面，于是标题栏内
# 顶部 7 行被「拖顶边改高度」吃掉 —— 双击那里不会最大化、拖着也不是移窗口，
# 用户报的「双击时灵时不灵」就是这条。顶边缩放和标题栏拖动物理上互斥，
# 选了标题栏（见 titlebar.hit_test 第 2 步、commit f7f1a97）。
# 本条判据原来写着 `("上边", ..., T.HTTOP, E.TopEdge)`，编码的是**已被主动
# 移除**的行为，于是从那以后一直红着。一条红的判据比没有判据更糟。
want = {
    "左边": ((1, H // 2), T.HTLEFT, E.LeftEdge),
    "右边": ((W - 2, H // 2), T.HTRIGHT, E.RightEdge),
    "下边": ((W // 2, H - 1), T.HTBOTTOM, E.BottomEdge),
    "左上角": ((1, 1), T.HTTOPLEFT, E.TopEdge | E.LeftEdge),
    "左下角": ((1, H - 1), T.HTBOTTOMLEFT, E.BottomEdge | E.LeftEdge),
    "右下角": ((W - 2, H - 1), T.HTBOTTOMRIGHT, E.BottomEdge | E.RightEdge),
}
for nm, (pt, code, edges) in want.items():
    hit = T.hit_test(win, tb, QtCore.QPoint(*pt))
    check("%s：命中码 %d 且映射到正确的边组合" % (nm, code),
          hit == code and T.RESIZE_EDGE_FOR_CODE.get(hit) == edges,
          "pt=%s hit=%d mapped=%s" % (pt, hit, T.RESIZE_EDGE_FOR_CODE.get(hit)))
# 顶边归标题栏：它既不进缩放映射，也不是客户区 —— 它是 HTCAPTION。
check("顶边中段 = HTCAPTION（拖窗口，不是改高度）",
      T.hit_test(win, tb, QtCore.QPoint(W // 2, 1)) == T.HTCAPTION,
      "hit=%d" % T.hit_test(win, tb, QtCore.QPoint(W // 2, 1)))
check("缩放映射表里没有 HTTOP",
      T.HTTOP not in T.RESIZE_EDGE_FOR_CODE)
# 但**四个角仍然含 TopEdge** —— 极角那 6×6 见方是要能拉高度的，
# 缩放区没有被整个删掉。
check("四个角仍然带 TopEdge（角上还能拉高）",
      all(T.RESIZE_EDGE_FOR_CODE[c] & E.TopEdge
          for c in (T.HTTOPLEFT, T.HTTOPRIGHT)))
# 右上角原先够不到：那 6px 顶边整个落在标题栏里，右上角又被最小化/关闭
# 按钮占着，「按钮优先于缩放边框」的顺序让四角里少一个。用户后来明确要求
# **四个角都要能缩放**，于是最外侧 CORNER_GRAB 见方让给缩放角（见
# titlebar.hit_test 与 tests/_corners.py）。按钮本体仍然照旧。
hit_tr = T.hit_test(win, tb, QtCore.QPoint(W - 2, 1))
check("右上角让给缩放角了（四角都要能缩放）",
      hit_tr == T.HTTOPRIGHT, "hit=%d" % hit_tr)
check("右上角让出范围之外仍是关闭按钮（按钮没被整条吃掉）",
      T.hit_test(win, tb, QtCore.QPoint(W - T.CORNER_GRAB - 1, 1)) == T.HTCLIENT,
      str(T.hit_test(win, tb, QtCore.QPoint(W - T.CORNER_GRAB - 1, 1))))
check("右上角的边组合接上了缩放循环",
      T.RESIZE_EDGE_FOR_CODE.get(T.HTTOPRIGHT)
      == E.TopEdge | E.RightEdge)
check("映射表正好 7 条，没有多余也没有缺失（顶边已移出）",
      len(T.RESIZE_EDGE_FOR_CODE) == 7, str(len(T.RESIZE_EDGE_FOR_CODE)))

print("\n=== 缩放几何：拖哪边改哪边 ===")
g0 = QtCore.QRect(100, 200, 800, 600)
p0 = QtCore.QPoint(500, 500)
r = T.edge_resize_rect(g0, p0, QtCore.QPoint(560, 500), E.RightEdge)
check("往右拖 60：右边 +60，左边不动",
      r.left() == g0.left() and r.right() == g0.right() + 60,
      "%d,%d %dx%d" % (r.left(), r.top(), r.width(), r.height()))
r = T.edge_resize_rect(g0, p0, QtCore.QPoint(380, 500), E.LeftEdge)
check("往左拖 120：左边 -120，右边不动",
      r.left() == g0.left() - 120 and r.right() == g0.right(),
      "%d,%d %dx%d" % (r.left(), r.top(), r.width(), r.height()))
r = T.edge_resize_rect(g0, p0, QtCore.QPoint(500, 560), E.BottomEdge)
check("往下拖 60：下边 +60，上边不动",
      r.top() == g0.top() and r.bottom() == g0.bottom() + 60,
      "%d,%d %dx%d" % (r.left(), r.top(), r.width(), r.height()))
r = T.edge_resize_rect(g0, p0, QtCore.QPoint(500, 380), E.TopEdge)
check("往上拖 120：上边 -120，下边不动",
      r.top() == g0.top() - 120 and r.bottom() == g0.bottom(),
      "%d,%d %dx%d" % (r.left(), r.top(), r.width(), r.height()))
r = T.edge_resize_rect(g0, p0, QtCore.QPoint(560, 560), E.RightEdge | E.BottomEdge)
check("往右下拖：两条边同时长，**左上角钉住不动**",
      r.left() == g0.left() and r.top() == g0.top()
      and r.right() == g0.right() + 60 and r.bottom() == g0.bottom() + 60,
      "%d,%d %dx%d" % (r.left(), r.top(), r.width(), r.height()))
r = T.edge_resize_rect(g0, p0, p0, E.LeftEdge | E.RightEdge)
check("没动光标时几何不变（按一下不改变窗口）",
      r == g0, "%d,%d %dx%d" % (r.left(), r.top(), r.width(), r.height()))
# 元断言：老式「无脑 setGeometry(x, y, w+dx, h+dy)」在左边拖动时是错的
bad = QtCore.QRect(g0)
bad.setWidth(g0.width() + 120)      # 只加宽，不管左边界
check("元断言：只改宽高、不管左边界的老写法在拖左边时是错的",
      bad.left() != (g0.left() - 120),
      "老写法 left=%d，正确应为 %d" % (bad.left(), g0.left() - 120))

print("\n=== 默认小窗口保持原样 ===")
check("默认尺寸仍是 1280x820",
      (win.width(), win.height()) == (1280, 820),
      "%dx%d" % (win.width(), win.height()))
check("最小尺寸仍是 1020x660（还能拖小，但不会被拖没）",
      (win.minimumSize().width(), win.minimumSize().height()) == (1020, 660),
      "%dx%d" % (win.minimumSize().width(), win.minimumSize().height()))
# 真按最小尺寸走一遍 resize()，确认夹取生效（能拖到最小，但不会更小）
win.resize(400, 300)
app.processEvents()
check("拖到 400x300 被夹回最小尺寸 1020x660",
      (win.width(), win.height()) == (1020, 660),
      "%dx%d" % (win.width(), win.height()))
win.resize(1600, 1000)
app.processEvents()
check("能往大拖（1600x1000 生效）",
      (win.width(), win.height()) == (1600, 1000),
      "%dx%d" % (win.width(), win.height()))

print("\n=== 非客户区按下确实会起缩放循环 ===")
# **必须真的推进一次缩放。** 上一轮只钉了「定时器起来了」就收工，结果
# `resize(QRect)` 的 TypeError 一路漏到真机才炸 —— 启动 ≠ 生效，中间隔着
# 整个 tick。这里直接把光标钉死、`_left_button_down` 打成「按着」，再调
# `resize_tick`，是确定性的，不用去和事件循环较劲。
win._edge_timer = None
# 先回到一个**确定能设进去**的尺寸再取基准 —— 上面刚把窗口拖到 1600x1000，
# 那可能超过虚拟屏幕而不被采纳，拿它当基准会算出对不上的期望值。
win.resize(1280, 820)
app.processEvents()
g0 = QtCore.QRect(win.geometry())
_real_down = T._left_button_down
T._left_button_down = lambda: True
_old_cursor = QtGui.QCursor.pos()
try:
    # ---------------------------------------------------- 七条边/角的拖拽
    # **位置必须一起量。** 上一版只断言 `win.width()`，而只调 `resize()`
    # 也会改宽度 —— 于是一条「拖左边却缩右半边」的实现照样是绿的，判据名
    # 还写着「拖左边是右边长，和原生一致」，把 bug 记成了基准。
    #
    # 原生行为是一条恒等式：**被拖的那条边移动 dx/dy，没被拖的边一个像素都
    # 不许动。** 顶边中段归标题栏拖动（见上），所以这里穷举 7 条边/角。
    win.setGeometry(g0)
    DRAG = [
        ("右边", T.HTRIGHT, 90, 0),
        ("左边", T.HTLEFT, -90, 0),
        ("下边", T.HTBOTTOM, 0, 90),
        ("右上角", T.HTTOPRIGHT, 90, -60),
        ("左上角", T.HTTOPLEFT, -90, -60),
        ("右下角", T.HTBOTTOMRIGHT, 90, 90),
        ("左下角", T.HTBOTTOMLEFT, -90, 90),
    ]
    def _sweep(tick, verbose=True):
        """跑一遍 7 条边/角，返回违反「拖哪跟哪、对面钉住」的边名。"""
        broke = []
        for nm, code, dx, dy in DRAG:
            win.setGeometry(g0)
            app.processEvents()
            base = QtCore.QRect(win.geometry())
            c0 = base.center()
            edges = T.RESIZE_EDGE_FOR_CODE[code]
            QtGui.QCursor.setPos(QtCore.QPoint(c0.x() + dx, c0.y() + dy))
            tick(win, base, c0, edges)
            app.processEvents()
            got = QtCore.QRect(win.geometry())
            want = (
                base.left() + dx if edges & E.LeftEdge else base.left(),
                base.top() + dy if edges & E.TopEdge else base.top(),
                base.right() + dx if edges & E.RightEdge else base.right(),
                base.bottom() + dy if edges & E.BottomEdge else base.bottom(),
            )
            have = (got.left(), got.top(), got.right(), got.bottom())
            ok = have == want
            if verbose:
                check("%s：拖 (%+d,%+d) 后四条边「拖哪跟哪、对面钉住」"
                      % (nm, dx, dy), ok,
                      "实际(l,t,r,b)=%s 期望=%s 基准=(%d,%d,%d,%d)"
                      % (have, want, base.left(), base.top(),
                         base.right(), base.bottom()))
            if not ok:
                broke.append(nm)
        return broke

    _sweep(T.resize_tick)

    # 元断言：换回旧实现（只 resize、不给位置）。它必须**恰好**在涉及左/上的
    # 那 4 条上红 —— 少一条说明这条恒等式不够锐，多一条说明它把本来没坏的
    # 右边/下边也误伤了。
    def _old_tick(w, start_geom, start_pos, e):
        r = T.edge_resize_rect(start_geom, start_pos, QtGui.QCursor.pos(), e)
        w.resize(r.width(), r.height())
        return True

    _real_tick = T.resize_tick
    try:
        T.resize_tick = _old_tick
        old_broke = set(_sweep(_old_tick, verbose=False))
    finally:
        T.resize_tick = _real_tick
    want_broke = {"左边", "右上角", "左上角", "左下角"}
    check("元断言：旧实现恰好在涉及左/上的 4 条上违反恒等式",
          old_broke == want_broke,
          "旧实现坏在 %s，期望正好 %s"
          % (sorted(old_broke), sorted(want_broke)))

    # ------------------------------------------------- 顶到最小尺寸边停住
    # 宽度已经被夹在最小值上，左边界若还跟着光标走，窗口就成了「宽度纹丝
    # 不动、位置一路平移」—— 看着像在滑，不像在缩。
    win.setGeometry(g0)
    app.processEvents()
    base = QtCore.QRect(win.geometry())
    mw, mh = win.minimumWidth(), win.minimumHeight()
    small = T.edge_resize_geometry(
        base, base.center(),
        QtCore.QPoint(base.center().x() + 400, base.center().y()),
        E.LeftEdge, mw, mh)
    check("顶到最小宽：左边界反推成「右边界 − 最小宽」（边停住，不是滑）",
          small.left() == base.right() - mw + 1 and small.width() == mw,
          "l=%d 宽=%d，期望 l=%d 宽=%d"
          % (small.left(), small.width(), base.right() - mw + 1, mw))
    top_small = T.edge_resize_geometry(
        base, base.center(),
        QtCore.QPoint(base.center().x(), base.center().y() + 400),
        E.TopEdge, mw, mh)
    check("顶到最小高：上边界同理",
          top_small.top() == base.bottom() - mh + 1 and top_small.height() == mh,
          "t=%d 高=%d，期望 t=%d 高=%d"
          % (top_small.top(), top_small.height(),
             base.bottom() - mh + 1, mh))
    # 没顶到最小尺寸时，「反推」必须与「光标位移」恒等，否则这条反推本身就
    # 是个新的偏差源。
    plain = T.edge_resize_geometry(
        base, base.center(), QtCore.QPoint(base.center().x() - 90,
                                           base.center().y()),
        E.LeftEdge, mw, mh)
    check("没顶到最小尺寸时，反推与光标位移恒等",
          plain.left() == base.left() - 90,
          "l=%d，期望 %d" % (plain.left(), base.left() - 90))

    # ------------------------------------------------------------- 松手
    win.setGeometry(g0)
    app.processEvents()
    # 松手后必须报告「结束」，否则定时器停不下来、窗口会一直跟着光标跑
    T._left_button_down = lambda: False
    again = T.resize_tick(win, g0, g0.center(), E.RightEdge)
    check("左键松开时报告结束（否则定时器停不下来）", again is False)
    check("松手后窗口不再变化",
          QtCore.QRect(win.geometry()) == g0,
          "%dx%d @ (%d,%d)" % (win.width(), win.height(), win.x(), win.y()))
finally:
    T._left_button_down = _real_down
    QtGui.QCursor.setPos(_old_cursor)
    win.setGeometry(g0)
    app.processEvents()

# 真发一条 WM_NCLBUTTONDOWN(wParam=HTLEFT) 进去，看定时器有没有起来。
# 消息体要在本进程里现造：wintypes.MSG.from_address 吃的是裸指针。
msg = wintypes.MSG()
msg.message = T.WM_NCLBUTTONDOWN
msg.wParam = T.HTLEFT
res = MW.MainWindow.nativeEvent(
    win, __import__("PyQt6.QtCore", fromlist=["QByteArray"]).QByteArray(b"windows_generic"),
    ctypes.addressof(msg))
check("WM_NCLBUTTONDOWN(HTLEFT) 被吃掉（返回 handled）",
      bool(res and res[0]), str(res))
check("缩放定时器已经起来",
      getattr(win, "_edge_timer", None) is not None)
if getattr(win, "_edge_timer", None) is not None:
    win._edge_timer.stop()
    win._edge_timer.deleteLater()
    win._edge_timer = None

msg2 = wintypes.MSG()
msg2.message = T.WM_NCLBUTTONDOWN
msg2.wParam = T.HTCAPTION
res2 = MW.MainWindow.nativeEvent(
    win, __import__("PyQt6.QtCore", fromlist=["QByteArray"]).QByteArray(b"windows_generic"),
    ctypes.addressof(msg2))
check("标题栏（HTCAPTION）**不**被缩放分支吃掉，仍走系统拖动",
      res2 is not None and not res2[0], str(res2))

win.close()
app.processEvents()
print("\n通过 %d 项，失败 %d 项" % (OK, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：%s" % f)
    sys.exit(1)
