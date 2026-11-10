"""Why does the tab close button not respond to a real click?

_tabbtn.py drove the button with QToolButton.click(), which emits `clicked`
directly and therefore never exercises mouse hit-testing. A button that is
positioned somewhere the pointer cannot land still "works" under click().
This posts real press/release events at the glyph instead, on a real
MainWindow, so a hit-testing or z-order fault shows up.

Checks:
  1. a real click at the button's centre closes that tab
  2. the tab bar actually receives the press (hit test says the button is on
     top at that point)
  3. hovering a tab that lost its connection and then clicking still closes
     it -- the user's exact sequence
  4. closing one of several tabs closes that one, not its neighbour
  5. the welcome tab has no button and is never closed
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets, sip

from sshclient.core import settings
from sshclient.core.settings import SavedSession
from sshclient.remote.connection import ConnectionInfo
from sshclient.ui import main_window, tab_close

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def _dismiss_modals():
    """Close any modal dialog the code under test opens.

    `_Conn.open_sftp` raises on purpose, and `SessionTab.attach` answers that
    with a *modal* `QMessageBox.warning`. `QMessageBox::exec` runs a nested
    event loop, so a timer still fires inside it -- without this the test
    blocks forever on a dialog nobody is there to click. It has been hanging
    here since the file was written, not just since the sidebar work.
    """
    w = QtWidgets.QApplication.activeModalWidget()
    if w is not None:
        w.close()


_dismiss_timer = QtCore.QTimer()
_dismiss_timer.timeout.connect(_dismiss_modals)
_dismiss_timer.start(50)

tmp = pathlib.Path(tempfile.mkdtemp())
store = settings.SessionStore(tmp / "sessions.json")
st = settings.AppSettings()
st._path = tmp / "settings.json"   # 别写到用户真实的配置里去
st.theme = "dark"
mw = main_window.MainWindow(st, store)

host = QtWidgets.QWidget()
lay = QtWidgets.QVBoxLayout(host)
lay.setContentsMargins(0, 0, 0, 0)
lay.addWidget(mw)
host.resize(1280, 800)
host.show()


def spin(ms=250):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


spin(300)


def close_btn_on(tabs, i):
    bar = tabs.tabBar()
    r = bar.tabRect(i)
    on = [b for b in bar.findChildren(tab_close.TabCloseButton)
          if b.isVisible() and r.contains(b.geometry().center())]
    return on[0] if len(on) == 1 else None


def real_click(btn):
    """Press and release the left button at the glyph's centre, in screen
    coordinates -- exactly what the user's mouse does."""
    c = btn.mapToGlobal(btn.rect().center())
    for etype in (QtCore.QEvent.Type.MouseButtonPress,
                  QtCore.QEvent.Type.MouseButtonRelease):
        ev = QtGui.QMouseEvent(etype, QtCore.QPointF(btn.rect().center()),
                               QtCore.QPointF(c), QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.KeyboardModifier.NoModifier)
        app.sendEvent(btn, ev)
    app.processEvents()


# ---------------------------------------------------------------- 1 + 2
print("=== A. a real click closes the tab ===")
fake = [None]


class _Ch:
    def __init__(self):
        import socket
        self.rx, self.tx = socket.socketpair()
        self.closed = False

    def fileno(self):
        return self.rx.fileno()

    def recv_ready(self):
        return True

    def recv(self, n):
        return self.rx.recv(n)

    def send(self, d):
        return len(d)

    def settimeout(self, t):
        pass

    def shutdown_write(self):
        pass

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self, host):
        self.ch = _Ch()
        self.host = host

    @property
    def info(self):
        return ConnectionInfo(host=self.host, port=22, username="deploy",
                             fingerprint="x")

    def open_shell(self, term=None, cols=80, rows=24):
        return self.ch

    def open_sftp(self):
        raise OSError("no sftp in this test")

    def exec(self, cmd, timeout=None):
        return (b"", "")

    def close(self):
        self.ch.close()

    def set_tunnel_callbacks(self, on_error=None, on_event=None):
        pass

    def add_tunnel(self, spec, enabled=True):
        return spec

    def set_tunnel_enabled(self, port, enabled=True):
        return enabled

    def remove_tunnel(self, port):
        return False

    def tunnel_specs(self):
        return []

    def tunnel_states(self):
        return {}

    def tunnel_connections(self):
        return {}


_CONNS = []


def add_session(name, host_):
    c = _Conn(host_)
    saved = SavedSession(name=name, host=host_, port=22, username="deploy",
                         auth_method="password", password="x")
    mw._on_connect_ok(c, saved)
    spin(200)
    # 登记起来给收尾关 socket。5 个 socketpair 全漏着不管，虽然不至于崩，
    # 但 fd 是有限资源，而"顺手清掉"和"忘了清"之间只差一个列表。
    _CONNS.append(c)
    return c


add_session("A", "10.0.0.1")
add_session("B", "10.0.0.2")
spin(200)
check("two session tabs plus welcome", mw.tabs.count() == 3,
      f"count={mw.tabs.count()}")

bar = mw.tabs.tabBar()
idx = 1
btn = close_btn_on(mw.tabs, idx)
check("tab 1 has exactly one close button", btn is not None)
if btn is not None:
    r = bar.tabRect(idx)
    check("button sits inside its tab",
          r.contains(btn.geometry().center()),
          f"tab={r.x()},{r.y()} {r.width()}x{r.height()} "
          f"btn={btn.geometry().x()},{btn.geometry().y()} "
          f"{btn.width()}x{btn.height()}")
    # what does the window think is under that point?
    gp = btn.mapToGlobal(btn.rect().center())
    under = bar.childAt(bar.mapFromGlobal(gp) - bar.rect().topLeft())
    check("the tab bar's own hit test finds the button",
          under is btn, f"childAt={type(under).__name__}")
    hit = mw.childAt(mw.mapFromGlobal(gp))
    check("the window routes the point to the button, not the tab or pane",
          hit is btn, f"childAt={type(hit).__name__}")

    before = mw.tabs.count()
    real_click(btn)
    spin(300)
    check("a real mouse click removed the tab", mw.tabs.count() == before - 1,
          f"count {before} -> {mw.tabs.count()}")

# ------------------------------------------------------- 3: the user's path
print("=== B. disconnect, then click the X (the reported sequence) ===")
conn = add_session("C", "10.0.0.3")
spin(200)
idx = [i for i in range(mw.tabs.count())
       if isinstance(mw.tabs.widget(i), main_window.SessionTab)][-1]
check("tab is labelled with the session before the drop",
      mw.tabs.tabText(idx) == "C", mw.tabs.tabText(idx))
# 标签写的是**会话名**，不是 `用户名@主机:端口`（分组功能把它改成了
# `分组名-会话名`，见 main_window._on_connect_ok；那串地址还在侧栏行里）。
# 这里原来断言 `"10.0.0.3" in tabText`，等于把旧标签格式当成了基准。

# the remote goes away, exactly like typing `exit`
try:
    conn.ch.tx.shutdown(1)   # SHUT_WR
except Exception:
    pass
spin(600)
check("tab was renamed to 已断开 after the drop",
      mw.tabs.tabText(idx) == "已断开", mw.tabs.tabText(idx))

btn = close_btn_on(mw.tabs, idx)
check("the disconnected tab still has a close button", btn is not None)
if btn is not None:
    before = mw.tabs.count()
    real_click(btn)
    spin(300)
    check("clicking X on the disconnected tab removed it",
          mw.tabs.count() == before - 1,
          f"count {before} -> {mw.tabs.count()}")

# -------------------------------------------------- 4: closes the right one
print("=== C. closes the tab that was clicked, not a neighbour ===")
add_session("D", "10.0.0.4")
add_session("E", "10.0.0.5")
spin(200)
labels = [mw.tabs.tabText(i) for i in range(mw.tabs.count())]
print(f"  tabs now: {labels}")
target = next(i for i, t in enumerate(labels) if t == "D")
btn = close_btn_on(mw.tabs, target)
check("target tab has a close button", btn is not None)
if btn is not None:
    real_click(btn)
    spin(300)
    now = [mw.tabs.tabText(i) for i in range(mw.tabs.count())]
    check("the clicked tab is gone",
          not any(t == "D" for t in now), str(now))
    check("its neighbours survived",
          any(t == "E" for t in now), str(now))

# --------------------------------------------------------------- 5: welcome
print("=== D. the welcome tab ===")
check("welcome tab is still there", mw.tabs.widget(0) is mw.welcome)
check("welcome tab has no close button",
      close_btn_on(mw.tabs, 0) is None)

# --------------------------------------------------------------- teardown
# 收尾不是"整洁问题"，是**稳定性问题**，而且这里有两个具体来源：
#
# 1. `_dismiss_timer` 是**模块级、没有 parent** 的 QTimer，每 50ms 触发一次。
#    不 stop 掉它，它会一直活到解释器退出——那时 QApplication 已经没了，
#    而它还在调 `QApplication.activeModalWidget()`，摸到的可能是一个正在
#    销毁的 C++ 对象。
# 2. `mw` / `host` 和 5 个 socketpair 全都不关，Qt 对象活得比 QApplication 长。
#
# 症状是退出时 `0xC0000005`，而且**偶发**：改动之前实测 30 次崩 1 次，崩的
# 那次 15 条断言全绿、结论行照打，只是进程在最后一步死了。判据对不对跟它
# 一点关系没有，但一个每三十轮误报一次的套件，迟早被人当成"偶发，不看"。
#
# `close()` 只是发 closeEvent，**不回收**；`deleteLater()` 之后还得
# `sendPostedEvents(DeferredDelete)` 才真的析构。
_dismiss_timer.stop()
for c in _CONNS:
    c.ch.close()
mw.close()
host.close()
mw.deleteLater()
host.deleteLater()
QtCore.QCoreApplication.sendPostedEvents(
    None, QtCore.QEvent.Type.DeferredDelete)


def _isdeleted(obj) -> bool:
    """C++ 对象是不是真没了。

    **用 sip.isdeleted() 判，不用"看着没了"、不用内存曲线。** `close()` 只发
    closeEvent，`removeTab()` 只从布局里摘掉，两者都不回收——量到"看不见了"
    只能证明它藏起来了，证明不了它被销毁了。
    """
    try:
        return bool(sip.isdeleted(obj))
    except RuntimeError:
        return True          # 已经访问不到了，那就是没了


# 崩溃的原因是 Qt 对象活过 QApplication，所以把这件事**变成可判定的**，
# 而不是靠"跑了 30 次没崩"这种概率说法——那种说法下次就验证不到了。
check('收尾之后 MainWindow 真的被销毁了（不是只 close）', _isdeleted(mw))
check('收尾之后 host 真的被销毁了', _isdeleted(host))

print()
print("=" * 68)
print(f"{len(PASS)} passed, {len(FAIL)} failed")
for n in FAIL:
    print("  -", n)
print("=" * 68)
sys.exit(1 if FAIL else 0)
