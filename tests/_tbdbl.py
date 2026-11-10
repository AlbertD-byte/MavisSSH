"""标题栏双击：自己切换最大化，不依赖系统转发，且**绝不双切换**。

用户报「直接点右上角按钮没问题了，但双击标题栏长条位置不一定生效」。

## 为什么"不一定生效"

`handle_native` 原来只列了 `WM_NCLBUTTONDOWN` / `WM_NCLBUTTONUP`，
**`WM_NCLBUTTONDBLCLK` 是漏的**。双击完全靠返回 None 之后 Qt 把它转发给
`DefWindowProc` 才生效 —— 那条路实测是通的（DBLCLK+HTCAPTION →
`SC_MAXIMIZE`），但它有两层间接：

1. 得靠 QWidgetWindow 在「未处理」时转发，那是 Qt 的内部行为；
2. 第一次按下会先进系统的 `SC_MOVE` 拖动循环（`DragDetect` + 模态循环），
   双击成不成立取决于它有没有正常退出。

用户看到的"不一定"就落在这两层上：**多数时候靠转发能成，偶尔那条路没走通。**

## 判据为什么钉「我们自己处理」而不是「最终生效」

**旧版在这个测试环境里也会生效**（转发给 DefWindowProc 那条路是通的），
所以"双击后窗口最大化了"这条判据是恒绿的，区分不出修没修。

真正要钉的是三个事实：

1. `toggle_maximize` 被调用**恰好一次** —— 证明是我们自己这条分支；
2. 返回 `(True, 0)` —— 证明消息**没有**再转发给 DefWindowProc；
3. 整条消息序列里**不出现** `WM_SYSCOMMAND SC_MAXIMIZE` —— 这是**双切换
   的直接证据**。转发那条路会发 SC_MAXIMIZE，我们自己不会再发一次；两条路
   同时生效就是"最大化又还原"，用户看到的就是"按了没反应"。

## 判据钉什么

1. 完整双击序列 → 最大化；已最大化时同一序列 → 还原（toggle）
2. `toggle_maximize` 恰好被调 1 次，返回 `(True, 0)`
3. 消息序列里没有 SC_MAXIMIZE / SC_RESTORE（无系统路叠加 = 无双切换）
4. 标题栏顶部 y=2 也能双击（上一轮死区修复在双击路径上同样生效）
5. 弹框（`kinds=(CLOSE,)`）双击不崩、不切换
6. `HTMAXBUTTON` 的 DBLCLK **不**切换 —— 切换只挂 DOWN，同样防双切换
"""
import ctypes
import os
import pathlib
import sys
import tempfile
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import settings
from sshclient.ui import main_window
from sshclient.ui import titlebar as tb_mod

if sys.platform != "win32":                       # pragma: no cover
    print("只对 Windows 有效")
    sys.exit(0)

bad = []

WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN = 0x00A1
WM_NCLBUTTONUP = 0x00A2
WM_NCLBUTTONDBLCLK = 0x00A3
WM_SYSCOMMAND = 0x0112
SC_MAXIMIZE, SC_RESTORE, SC_MOVE = 0xF030, 0xF120, 0xF010

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                                wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def settle(ms=200):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}"
          + (f"  {detail}" if detail else ""))
    if not ok:
        bad.append(f"{name}  {detail}")


LOG = []
CALLS = []
HANDLED = []

_orig_native_event = tb_mod.native_event


class _Probe(main_window.MainWindow):
    """真正的 Qt 子类覆盖 nativeEvent —— 虚方法只能这样插进去。

    **MainWindow 必须自己当顶层窗口**：塞进 host 里的话顶层 HWND 是 host
    的，`MainWindow.nativeEvent` 一条消息都收不到（第一版探针消息数全 0
    就是这个原因，不是产品 bug）。判据要发真消息，就更需要它当顶层。
    """

    def nativeEvent(self, event_type, message):
        r = _orig_native_event(self, message)
        msg = wintypes.MSG.from_address(int(message))
        LOG.append((msg.message, int(msg.wParam)))
        HANDLED.append((msg.message, r))
        return r


tmp = pathlib.Path(tempfile.mkdtemp())
store = settings.SessionStore(tmp / "s.json")
store._items = []
st = settings.AppSettings()
st.theme = "light"
st.save = lambda: None
mw = _Probe(st, store)
mw.setMinimumSize(400, 300)
mw.setGeometry(160, 160, 1280, 800)
mw.show()
settle(700)

tb = mw.titlebar
hwnd = int(mw.winId())

# toggle_maximize 计数 —— 判据的核心
_orig_toggle = tb_mod.TitleBar.toggle_maximize


def spy_toggle(self):
    CALLS.append(self is tb)
    return _orig_toggle(self)


tb_mod.TitleBar.toggle_maximize = spy_toggle


def lp(x, y):
    return ((y & 0xFFFF) << 16) | (x & 0xFFFF)


def run_double_click(wx, wy):
    """发一遍 Windows 真实双击序列，返回 (之前, 之后)。"""
    del LOG[:]
    del CALLS[:]
    del HANDLED[:]
    gp = mw.mapToGlobal(QtCore.QPoint(wx, wy))
    l = lp(gp.x(), gp.y())
    before = mw.isMaximized()
    for msg in (WM_NCMOUSEMOVE, WM_NCLBUTTONDOWN, WM_NCLBUTTONUP,
                WM_NCLBUTTONDBLCLK):
        user32.SendMessageW(hwnd, msg, 2, l)
        settle(50)
    settle(250)
    return before, mw.isMaximized()


def syscommands():
    return [wp for (m, wp) in LOG
            if m == WM_SYSCOMMAND and (wp & 0xFFF0) in (SC_MAXIMIZE, SC_RESTORE)]


# ============================================================ 1 + 2 + 3
print("=== A. 正常窗口双击标题栏 → 最大化 ===")
wx, wy = 700, 19
print(f"  标题栏点 窗口坐标=({wx},{wy}) "
      f"hit_test={tb_mod.hit_test(mw, tb, QtCore.QPoint(wx, wy))}（应为 2=HTCAPTION）")
b, a = run_double_click(wx, wy)
check("双击后窗口最大化了", b is False and a is True, f"{b} -> {a}")
check("toggle_maximize 被调用恰好 1 次（我们自己这条分支）",
      len(CALLS) == 1, f"{len(CALLS)} 次")

dbl = [r for (m, r) in HANDLED if m == WM_NCLBUTTONDBLCLK]
check("DBLCLK 被我们吃掉（返回 (True, 0)，没转发给 DefWindowProc）",
      len(dbl) == 1 and dbl[0] == (True, 0), f"{dbl}")
check("消息序列里没有 SC_MAXIMIZE/SC_RESTORE（无双切换）",
      syscommands() == [], f"{[hex(x) for x in syscommands()]}")

# ============================================================ 1 的 toggle
print("\n=== B. 已最大化时双击 → 还原（toggle） ===")
check("前提：当前是最大化", mw.isMaximized() is True)
b, a = run_double_click(wx, wy)
check("双击后还原了", b is True and a is False, f"{b} -> {a}")
check("toggle_maximize 被调用恰好 1 次", len(CALLS) == 1, f"{len(CALLS)} 次")
check("仍然没有 SC_MAXIMIZE/SC_RESTORE", syscommands() == [],
      f"{[hex(x) for x in syscommands()]}")

# ============================================================ 4
print("\n=== C. 标题栏顶部 y=2 也能双击（上一轮死区修复） ===")
b, a = run_double_click(wx, 2)
check("顶部 y=2 双击也能最大化", b is False and a is True, f"{b} -> {a}")
check("顶部 y=2 也只调用 1 次 toggle", len(CALLS) == 1, f"{len(CALLS)} 次")

# ============================================================ 6
print("\n=== D. 最大化按钮的 DBLCLK 不切换（切换只挂 DOWN，防双切换） ===")
if mw.isMaximized():
    mw.showNormal()
    settle(250)
del CALLS[:]
del HANDLED[:]
mg = tb.btn_max.geometry()
gp = mw.mapToGlobal(mg.center())
l = lp(gp.x(), gp.y())
b = mw.isMaximized()
user32.SendMessageW(hwnd, WM_NCLBUTTONDBLCLK, 9, l)
settle(250)
check("最大化按钮收到 DBLCLK 不切换（否则一次双击等于切换两次）",
      mw.isMaximized() == b and len(CALLS) == 0, f"{b} -> {mw.isMaximized()}")
user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, 9, l)
settle(250)
check("对照：DOWN 仍然切换（单点最大化按钮没坏）",
      mw.isMaximized() != b and len(CALLS) == 1,
      f"{b} -> {mw.isMaximized()}  toggle {len(CALLS)} 次")
if mw.isMaximized():
    mw.showNormal()
    settle(250)

# ============================================================ 5
print("\n=== E. 弹框（kinds=(CLOSE,)）双击不崩、不切换 ===")
# 和 inject_chrome / attach_chrome 对弹框的调用一致：只有关闭键，btn_max 恒 None
from sshclient.ui.design.v1.palette import get_palette


class _ProbeDialog(QtWidgets.QDialog):
    """弹框自己要覆盖 nativeEvent 才会走 handle_native —— MainWindow 那个
    方法只挂在主窗口上，弹框的消息根本不经过它（上一版判据就是漏了这点，
    判据自己没红，还差点让我把一段正确代码当成 bug 改掉）。

    `nativeEvent` 是 Qt 虚方法，必须写在**真正的 Qt 子类**上才插得进去。
    """

    def __init__(self):
        super().__init__()
        self.tb = None
        self.msgs = []

    def nativeEvent(self, event_type, message):
        self.msgs.append(int(message))
        if self.tb is None:
            return False, 0
        return tb_mod.native_event(self, message)


dlg = _ProbeDialog()
dlg.setGeometry(300, 300, 400, 200)
dtb = tb_mod.TitleBar(dlg, get_palette("light"), dlg, title="探针",
                      kinds=(tb_mod.CLOSE,))
dlg.tb = dtb
check("弹框标题栏没有最大化按钮", dtb.btn_max is None)
CALLS.clear()
dlg.show()
settle(400)
gpd = dlg.mapToGlobal(QtCore.QPoint(200, 10))
ld = lp(gpd.x(), gpd.y())
# 发给**弹框自己的 HWND**，让消息真的经过它的 nativeEvent -> handle_native
user32.SendMessageW(int(dlg.winId()), WM_NCLBUTTONDBLCLK, 2, ld)
settle(250)
check("弹框确实收到了 DBLCLK（前提成立，否则后面都是恒绿）",
      len(dlg.msgs) >= 1, f"{len(dlg.msgs)} 条消息")
check("弹框双击不调 toggle（弹框不能最大化）", len(CALLS) == 0,
      f"{len(CALLS)} 次")
check("弹框窗口也没被最大化", dlg.isMaximized() is False)
dlg.close()

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b_ in bad:
        print("  -", b_)
else:
    print("titlebar double click: all checks passed")
sys.exit(1 if bad else 0)
