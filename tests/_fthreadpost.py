"""判据：工作线程往已销毁的控件投递时，必须安静地放弃而不是抛异常。

**这个判据复现的是真机上崩过的那一条**：

    Exception in thread Thread-137 (worker):
      File ".../sshclient/ui/monitor_panel.py", line 522, in worker
        QtCore.QMetaObject.invokeMethod(
      RuntimeError: wrapped C/C++ object of type MonitorPanel has been deleted

根因：面板是 SessionTab 的子控件，关标签时 `w.deleteLater()` 连带把它销毁；
而 worker 可能正卡在 `conn.exec(PROBE_CMD, timeout=10)` 里最多 10 秒，
醒来再往那个已销毁的对象上投，就炸了。

判据的做法：**真的删掉一个控件，再从工作线程投一次** ——
改之前这一步必然抛 RuntimeError。
"""
from __future__ import annotations

import os
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from sshclient.ui import thread_post  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class _Target(QtCore.QObject):
    """带一个真 slot 的目标 —— `invokeMethod` 只能调 slot，
    `setObjectName` 那种普通方法它找不到（会报
    "No such method QWidget::setObjectName"）。"""

    def __init__(self):
        super().__init__()
        self.got: list = []

    @QtCore.pyqtSlot(object)
    def take(self, v):
        self.got.append(v)


def flush_deferred() -> None:
    """**必须显式投递 DeferredDelete。**

    `QApplication.processEvents()` **不投递 DeferredDelete** —— Qt 只在
    「回到与调用 `deleteLater()` 相同的事件循环层级」时才投递它。所以第一版
    判据用 processEvents 等删除，控件根本没被销毁，
    `sip.isdeleted` 返回 False，于是「已销毁时安静放弃」那条根本没测到。
    """
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    QtWidgets.QApplication.processEvents()


print("=== 1. 控件还活着：投递应当成功 ===")
alive = _Target()
ok = thread_post.post(alive, "take", QtCore.Q_ARG(object, "hello"))
check("活着的控件投递返回 True", ok is True, "实际 %r" % (ok,))
flush_deferred()
check("值真的送到了", alive.got == ["hello"], str(alive.got))

print("\n=== 2. 控件被销毁：投递必须安静地放弃 ===")
dead = _Target()
dead.deleteLater()
flush_deferred()
check("对象确实已被析构（sip.isdeleted 为真）",
      thread_post.is_alive(dead) is False,
      "is_alive=%r" % thread_post.is_alive(dead))

raised = []


def _worker():
    try:
        thread_post.post(dead, "take", QtCore.Q_ARG(object, "x"))
    except BaseException as exc:      # noqa: BLE001
        raised.append(exc)


t = threading.Thread(target=_worker)
t.start()
t.join(5)
check("工作线程里的投递**没有抛异常**", not raised,
      "抛了 %s: %s" % (type(raised[0]).__name__, raised[0]) if raised else "")
check("投递返回 False（表示「对象没了，放弃」）",
      thread_post.post(dead, "take", QtCore.Q_ARG(object, "x")) is False)

print("\n=== 3. 老写法确实会炸（证明判据有区分度）===")
raised2 = []
try:
    QtCore.QMetaObject.invokeMethod(
        dead, "take", QtCore.Qt.ConnectionType.QueuedConnection,
        QtCore.Q_ARG(object, "x"))
except BaseException as exc:      # noqa: BLE001
    raised2.append(exc)
check("**不用守卫的老写法会抛 RuntimeError**",
      bool(raised2) and isinstance(raised2[0], RuntimeError),
      "实际 %r" % (raised2,))

print("\n=== 4. 三处调用点都用了守卫 ===")
# **用 ast 找真实的调用**，不要用「行里有没有这个字符串」——
# 注释与文档字符串里提到 invokeMethod 的文字会造成误报
# （main_window.py 里就有一句历史说明提到它）。ast 天然只看代码。
import ast  # noqa: E402


def bare_invoke_calls(path: pathlib.Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute)
                and f.attr == "invokeMethod"):
            continue
        # 是不是 QMetaObject.invokeMethod
        base = f.value
        if not (isinstance(base, ast.Attribute)
                and base.attr == "QMetaObject"):
            continue
        # 第一个实参是不是 self
        if node.args and isinstance(node.args[0], ast.Name) \
                and node.args[0].id == "self":
            out.append((node.lineno, ast.unparse(node)[:70]))
    return out


for f in ("sshclient/ui/monitor_panel.py",
          "sshclient/ui/tunnel_panel.py",
          "sshclient/ui/main_window.py"):
    src = (ROOT / f).read_text(encoding="utf-8")
    base = os.path.basename(f)
    check("%s 用了 thread_post.post" % base, "thread_post.post" in src)
    bare = bare_invoke_calls(ROOT / f)
    check("%s 没有裸的 invokeMethod(self, ...)" % base, not bare, str(bare[:2]))

print("\n=== 5. MonitorPanel 的停止标志 ===")
from sshclient.ui.monitor_panel import MonitorPanel  # noqa: E402

check("MonitorPanel 有 stop()", hasattr(MonitorPanel, "stop"))
src = (ROOT / "sshclient" / "ui" / "monitor_panel.py").read_text(encoding="utf-8")
check("stop() 会置 _stopped（让在飞的那一轮放弃）",
      "self._stopped = True" in src)
check("worker 投之前会看 _stopped", "if self._stopped:" in src)
check("set_interval_ms 会清掉 _stopped（换间隔 = 继续跑）",
      "self._stopped = False" in src)

print("\n=== 6. 主窗口关闭时要唤醒在等的主机密钥请求 ===")
# **不唤醒的后果**：连接工作线程卡在 `ask.event.wait(timeout=120)` 上，
# 而那个 wait 只有主线程跑 `_on_host_key_ask` 才会被 set。主窗口一关，
# 那个槽永远不跑 → worker 干等 120 秒，连接线程挂着不退。
mw_src = (ROOT / "sshclient" / "ui" / "main_window.py").read_text(
    encoding="utf-8")
check("MainWindow 维护 _host_key_asks 集合",
      "self._host_key_asks" in mw_src)
check("closeEvent 里会 set 那些 event",
      "ask.event.set()" in mw_src)
check("closeEvent 里把 accept 置 False（没人回答 = 拒绝）",
      "ask.accept = False" in mw_src)
check("_ask_host_key 投递失败时直接回拒绝",
      "return hostkeys.REJECT" in mw_src)

print()
if FAIL:
    print("FAILED %d:" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
