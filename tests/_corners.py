"""无边框窗口：四角全都能缩放 + 缩放光标跟随方向。

四角曾经少一个 —— `hit_test` 的判定顺序是「按钮 -> 缩放边框」，而右上角被
最小化/关闭按钮盖着，永远返回 `HTCLIENT`（实测另外三角都正常）。

光标全靠不到系统：本窗口样式实测 `0x960B0000`，**没有 `WS_THICKFRAME`**，
而 DefWindowProc 只在带 thickframe 的窗口上才给 `HTLEFT` 这类命中码套 resize
光标 —— 和「系统不起缩放循环」是同一个根因。全仓搜不到一处 resize 光标设置，
结果就是八个边角拖得动、却全程一个普通箭头。

**顶边例外：它不是缩放区，是标题栏。** 见下面「四条边的判定」那段。

可执行：`python -u tests/_corners.py`
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


class _CURSORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]

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


tmp = pathlib.Path(os.environ.get("TEMP", ".")) / "mavisprobe_corner"
tmp.mkdir(parents=True, exist_ok=True)
store = settings.SessionStore(tmp / "s.json")
st = settings.AppSettings()
st._path = tmp / "set.json"
st.theme = "dark"

win = MW.MainWindow(st, store)
win.resize(1280, 820)
win.show()
app.processEvents()

tb = win.titlebar
W, H = win.width(), win.height()
g = T.CORNER_GRAB
E = QtCore.Qt.Edge

print("\n=== 极角让出的尺寸比整条缩放边框小 ===")
check("CORNER_GRAB(%d) < RESIZE_MARGIN(%d)（按钮只少一小块，不是整条让出）"
      % (g, T.RESIZE_MARGIN), 0 < g < T.RESIZE_MARGIN)

print("\n=== 八个边角全部可缩放 ===")
corners = [("左上角", (1, 1), T.HTTOPLEFT, E.TopEdge | E.LeftEdge),
           ("右上角", (W - 2, 1), T.HTTOPRIGHT, E.TopEdge | E.RightEdge),
           ("左下角", (1, H - 2), T.HTBOTTOMLEFT, E.BottomEdge | E.LeftEdge),
           ("右下角", (W - 2, H - 2), T.HTBOTTOMRIGHT, E.BottomEdge | E.RightEdge)]
for nm, (x, y), code, edges in corners:
    hit = T.hit_test(win, tb, QtCore.QPoint(x, y))
    check("%s -> %d 且映射到正确的边组合" % (nm, code),
          hit == code and T.RESIZE_EDGE_FOR_CODE.get(hit) == edges,
          "hit=%d mapped=%s" % (hit, T.RESIZE_EDGE_FOR_CODE.get(hit)))

print("\n=== 四条边的判定：左/右/下可缩放，**顶边交给标题栏** ===")
# **顶边不再是缩放区**（commit f7f1a97「修标题栏顶部 7 行死区」）。
# 标题栏是 `setGeometry(0, 0, W, 38)` 满宽铺在窗口最上面，窗口顶边 100%
# 落在标题栏里；而 `y <= RESIZE_MARGIN(6) -> HTTOP` 的判定排在标题栏**前面**，
# 于是标题栏内 y=0..6 这 7 行（共约 7945px，占 18%）被「拖顶边改高度」吃掉：
# 双击那里不会最大化，按住拖也不是移动窗口 —— 用户报的「双击时灵时不灵」
# 就是这条。顶边缩放和标题栏拖动**物理上互斥**（同一批像素），只能选一个。
#
# 本条判据原来写的是 `("上边中", (W // 2, 1), T.HTTOP)`，编码的是**已被
# 主动移除**的行为 —— 于是 f7f1a97 之后它一直是红的（实测 21 项失败）。
# 一条红的判据比没有判据更糟：它让人以为仓库是坏的，或者让人学会无视红灯。
for nm, (x, y), code in [("上边中", (W // 2, 1), T.HTCAPTION),
                         ("左边", (1, H // 2), T.HTLEFT),
                         ("右边", (W - 2, H // 2), T.HTRIGHT),
                         ("下边中", (W // 2, H - 1), T.HTBOTTOM)]:
    hit = T.hit_test(win, tb, QtCore.QPoint(x, y))
    check("%s -> %d" % (nm, code), hit == code, "hit=%d" % hit)

check("顶边在**缩放边框之内**也仍然是标题栏（不是 HTTOP）",
      T.hit_test(win, tb, QtCore.QPoint(W // 2, T.RESIZE_MARGIN - 1))
      == T.HTCAPTION)
check("缩放映射表里没有 HTTOP（顶边已经不改高度了）",
      T.HTTOP not in T.RESIZE_EDGE_FOR_CODE)
check("HTCAPTION 不配光标（顶边要能拖窗口，光标不能抢）",
      T.CURSOR_FOR_CODE.get(T.HTCAPTION) is None)

print("\n=== 让出极角没有伤到标题栏按钮 ===")
for b in tb.buttons:
    cx_tb = b.geometry().center()
    pt = tb.mapTo(win, QtCore.QPoint(cx_tb.x(), cx_tb.y()))
    hit = T.hit_test(win, tb, pt)
    # **max 按钮返回的是 HTMAXBUTTON 而不是 HTCLIENT** —— 那是既有的正确行为
    # （为了让 Win11 贴边分屏走系统那条路），不是缺陷。把两个按钮混成一个
    # 期望值，判据就会逼着代码去改坏它。
    want = T.HTMAXBUTTON if b.kind == T.MAX else T.HTCLIENT
    check("%s 按钮中心判定为 %d（仍按按钮处理，没被缩放分支吃掉）"
          % (b.kind, want), hit == want, "按钮中心 hit=%d" % hit)
    check("%s 按钮不在极角让出范围内" % b.kind,
          not (pt.x() >= W - g and pt.y() <= g)
          and not (pt.x() <= g and pt.y() <= g),
          "按钮中心在窗口坐标 (%d,%d)" % (pt.x(), pt.y()))
# 元断言：右上角那个点**确实落在关闭按钮的矩形里** —— 这正是极角那一层
# 存在的理由。钉住这个事实，将来谁把按钮挪开或改小，这一层就该被撤掉。
_tr_pt = QtCore.QPoint(W - 2, 1)
_in_btn = any(b.geometry().contains(tb.mapFrom(win, _tr_pt))
              for b in tb.buttons)
check("元断言：右上角那个点本来落在按钮矩形内（极角这一层不是白加的）",
      _in_btn)

print("\n=== 缩放光标按方向给 ===")
want_cursor = {T.HTLEFT: T.IDC_SIZEWE, T.HTRIGHT: T.IDC_SIZEWE,
               T.HTBOTTOM: T.IDC_SIZENS,
               T.HTTOPLEFT: T.IDC_SIZENWSE, T.HTBOTTOMRIGHT: T.IDC_SIZENWSE,
               T.HTTOPRIGHT: T.IDC_SIZENESW, T.HTBOTTOMLEFT: T.IDC_SIZENESW}
for code, cid in want_cursor.items():
    check("命中码 %d -> 光标 %d" % (code, cid),
          T.CURSOR_FOR_CODE.get(code) == cid,
          "实得 %s" % T.CURSOR_FOR_CODE.get(code))
check("缩放光标表正好 7 条（左/右/下 + 四角，顶边已移除）",
      len(T.CURSOR_FOR_CODE) == 7, str(len(T.CURSOR_FOR_CODE)))
check("缩放映射表正好 7 条（与光标表一一对应）",
      set(T.CURSOR_FOR_CODE) == set(T.RESIZE_EDGE_FOR_CODE))
check("四角拿到的都是对角光标（用户要的 <->）",
      all(T.CURSOR_FOR_CODE[c] in (T.IDC_SIZENWSE, T.IDC_SIZENESW)
          for c in (T.HTTOPLEFT, T.HTTOPRIGHT, T.HTBOTTOMLEFT, T.HTBOTTOMRIGHT)))
check("客户区/标题栏**不**配光标（放行给 Qt，否则整个界面的光标都被夺走）",
      T.CURSOR_FOR_CODE.get(T.HTCLIENT) is None
      and T.CURSOR_FOR_CODE.get(T.HTCAPTION) is None
      and T.CURSOR_FOR_CODE.get(T.HTMAXBUTTON) is None)

print("\n=== 命中 -> 光标 的完整链路（逐条消息，确定性）===")
# **不做「挪真实指针再读 GetCursorInfo」那种端到端量法**：窗口没被激活时
# Windows 压根不送 WM_NCHITTEST（实测 _last_ht 全程为 None），量出来的是
# 「消息没来」，不是产品的行为，那是环境限制不是缺陷。改成直接发消息，并用
# 探针捕获**真正被 SetCursor 设上去的句柄**。
import ctypes as _c  # noqa: E402
from ctypes import wintypes  # noqa: E402

_expect = {code: int(T._user32.LoadCursorW(None, ctypes.c_void_p(cid)) or 0)
           for code, cid in T.CURSOR_FOR_CODE.items()}
_arrow = int(T._user32.LoadCursorW(None, ctypes.c_void_p(T.IDC_ARROW)) or 0)


def _lp(x: int, y: int) -> int:
    """把**屏幕**坐标按 Windows 的 ``WM_NCHITTEST`` lParam 约定打包。

    低 16 位 x、高 16 位 y，都是**有符号** 16 位 —— 负坐标（显示器在主屏
    左侧时）必须按有符号塞回去，直接 `x & 0xFFFF` 会把负数变成大正数。
    """
    return ((x & 0xFFFF) | ((y & 0xFFFF) << 16)) & 0xFFFFFFFF


def send(msg_val, screen_pt=None):
    m = wintypes.MSG()
    m.message = msg_val
    m.wParam = 0
    if screen_pt is None:
        m.lParam = 0
    else:
        gp = win.mapToGlobal(QtCore.QPoint(*screen_pt))
        m.lParam = _lp(gp.x(), gp.y())
    return MW.MainWindow.nativeEvent(
        win, QtCore.QByteArray(b"windows_generic"), _c.addressof(m))

_sent = []
_real_set = T._user32.SetCursor
T._user32.SetCursor = lambda h: (_sent.append(int(h or 0)), _real_set(h))[1]

# **坐标走 lParam，不再猴补 `QCursor.pos()`。**
# 这条判据原来接管 `QtGui.QCursor.pos` 来「钉住鼠标位置」——那是
# `_hittest_pos` 还在读 `QCursor.pos()` 时的做法。f7f1a97 已经把它换成读
# lParam（Windows 发这条消息时带的就是「正在问的那个点」，而 `QCursor.pos()`
# 回答的是「鼠标此刻在哪」；两者在鼠标移动时不是一回事，Windows 又会缓存
# 我们返回的码去处理随后的点击 —— 「时灵时不灵」就是这么来的）。
# 于是猴补 `QCursor.pos` **完全失效**：判据量到的全是真实指针位置，
# 除坐标恰好蒙对的那几个，其余 21 项全红。判据没跟上实现，比判据红更糟。
# WM_SETCURSOR 的 lParam 用的是 hit-test code 而不是坐标，这里照旧传 0。
for nm, (x, y), code in (("左边", (1, H // 2), T.HTLEFT),
                         ("上边", (W // 2, 1), T.HTCAPTION),
                         ("左上角", (1, 1), T.HTTOPLEFT),
                         ("右上角", (W - 2, 1), T.HTTOPRIGHT),
                         ("左下角", (1, H - 2), T.HTBOTTOMLEFT),
                         ("右下角", (W - 2, H - 2), T.HTBOTTOMRIGHT),
                         ("右边", (W - 2, H // 2), T.HTRIGHT),
                         ("下边", (W // 2, H - 1), T.HTBOTTOM)):
    r_ht = send(T.WM_NCHITTEST, (x, y))
    check("%s：WM_NCHITTEST 判成 %d" % (nm, code),
          bool(r_ht and r_ht[1] == code), "实得 %s" % (r_ht,))
    check("%s：命中码被记进 _last_ht" % nm,
          getattr(win, "_last_ht", None) == code,
          "_last_ht=%s" % getattr(win, "_last_ht", None))
    _sent.clear()
    r_cur = send(T.WM_SETCURSOR)
    if code in _expect:
        check("%s：WM_SETCURSOR 被接管" % nm,
              bool(r_cur and r_cur[0]), str(r_cur))
        check("%s：真正设上去的光标是 %d" % (nm, _expect[code]),
              _sent and _sent[-1] == _expect[code],
              "期望句柄=%d 实设=%s 箭头=%d" % (_expect[code], _sent, _arrow))
    else:
        # 顶边是 HTCAPTION，不在光标表里 —— **必须放行**：一个 resize 光标
        # 都不要设，否则光标形状会暗示「这里能改高度」，而它其实是拖窗口。
        check("%s：WM_SETCURSOR 不接管（顶边留给拖动）" % nm,
              r_cur is None or not r_cur[0], str(r_cur))
        check("%s：不该设任何光标（顶边留给拖动）" % nm, not _sent, str(_sent))
T._user32.SetCursor = _real_set

print("\n=== 非缩放区：放行给 Qt，绝不夺走整个界面的光标 ===")
for code, nm in ((T.HTCLIENT, "客户区"), (T.HTCAPTION, "标题栏"),
                 (T.HTMAXBUTTON, "最大化按钮")):
    win._last_ht = code
    _sent.clear()
    r2 = send(T.WM_SETCURSOR)
    check("%s：放行（返回未接管）" % nm, r2 is None or not r2[0], str(r2))
    check("%s：一个光标都没设（终端 IBeam / 按钮手型还在）" % nm,
          not _sent, str(_sent))

win.close()
app.processEvents()
print("\n通过 %d 项，失败 %d 项" % (OK, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：%s" % f)
    sys.exit(1)
