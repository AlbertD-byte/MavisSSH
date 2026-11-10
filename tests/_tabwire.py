"""Does Tab reach the channel through the REAL wiring?

_comptest called SessionTab._on_tab() directly, which skips two things the
running app depends on: attach() installing the tab handler, and Qt actually
delivering a Tab key event to the terminal widget. Both are exercised here.
"""
import os
import pathlib
import socket
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.fonts import boot

app = boot([])
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, Qt, QTimer
from PyQt6.QtGui import QKeyEvent
from PyQt6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from sshclient.core import settings
from sshclient.core.settings import SavedSession
from sshclient.remote.connection import ConnectionInfo
from sshclient.ui import main_window

FAIL = []
def check(name, cond, extra=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f'  ({extra})' if extra else ''))
    if not cond:
        FAIL.append(name)

for m in ('critical', 'warning', 'information', 'question'):
    setattr(QMessageBox, m, staticmethod(
        lambda p, t, x, *a, **k: QMessageBox.StandardButton.Ok))
QMessageBox.exec = lambda self: QMessageBox.StandardButton.Ok


class FakeChannel:
    def __init__(self):
        self.rx, self.tx = socket.socketpair()
        self.rx.settimeout(0.1)
        self.closed = False
        self.sent = bytearray()
        self.inbox = bytearray()
    def fileno(self): return self.rx.fileno()
    def recv(self, n):
        if self.inbox:
            out = bytes(self.inbox[:n]); del self.inbox[:n]
            return out
        try: return self.rx.recv(n)
        except Exception: return b''
    def sendall(self, d): self.sent.extend(d)
    def settimeout(self, t): pass
    def close(self): self.closed = True

class FakeSFTP:
    def listdir_attr(self, path='.'):
        import stat
        out = []
        for name in ('app.conf', 'app.log'):
            a = type('A', (), {})()
            a.filename = name; a.st_size = 1; a.st_mtime = 0
            a.st_mode = stat.S_IFREG | 0o644
            out.append(a)
        return out
    def stat(self, p): raise OSError('nope')
    def getcwd(self): return '/var/log'

class FakeConn:
    def __init__(self):
        self._info = ConnectionInfo(host='10.0.0.5', port=22, username='deploy')
        self.ch = FakeChannel(); self.sftp = FakeSFTP()
    @property
    def info(self): return self._info
    def open_shell(self, term=None, cols=80, rows=24): return self.ch
    def open_sftp(self): return self.sftp
    def exec(self, cmd, timeout=None): return (b'', b'')
    def close(self): self.ch.close()

def spin(ms):
    end = [False]
    QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()

tmp = pathlib.Path(tempfile.mkdtemp())
store = settings.SessionStore(tmp / 's.json')
st = settings.AppSettings()
st._path = tmp / 'settings.json'   # 别写到用户真实的配置里去
st.theme = 'dark'
mw = main_window.MainWindow(st, store)
host = QWidget(); host.setStyleSheet('background:#0d0f14')
lay = QVBoxLayout(host); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(mw)
host.resize(1200, 800); host.show(); spin(200)

print('=== real attach installs the tab handler ===')
conn = FakeConn()
saved = SavedSession(name='t', host='10.0.0.5', port=22, username='deploy',
                     auth_method='password', password='x')
mw._on_connect_ok(conn, saved)
spin(300)
tab = [mw.tabs.widget(i) for i in range(mw.tabs.count())
       if isinstance(mw.tabs.widget(i), main_window.SessionTab)][0]
check('tab handler installed by attach()', tab.terminal._tab_callback is not None)
check('terminal has a channel', tab.terminal._channel is conn.ch)
check('session is live', tab._state_kind == 'live', tab._state_kind)

print('=== the terminal actually has keyboard focus ===')
tw = tab.terminal
tw.setFocus()
spin(80)
app.setActiveWindow(mw)
spin(80)
check('terminal is the focus widget', app.focusWidget() is tw,
      type(app.focusWidget()).__name__ if app.focusWidget() else 'None')

print('=== a real Tab key event reaches the channel ===')
# Regression: QWidget::event() spends Tab on focusNextPrevChild() *before*
# keyPressEvent, so with the session toolbar (buttons + the ZMODEM path edit)
# in the focus chain Tab jumped to the next widget and completion never ran.
n = len(conn.ch.sent)
ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Tab,
               Qt.KeyboardModifier.NoModifier, '\t')
app.sendEvent(tw, ev)
spin(60)
sent = bytes(conn.ch.sent[n:])
check('Tab forwarded to the channel', sent == b'\t', repr(sent))
check('fallback armed after Tab', tab._completion_timer is not None)
check('focus stayed on the terminal', app.focusWidget() is tw,
      type(app.focusWidget()).__name__ if app.focusWidget() else 'None')

print('=== Shift+Tab also survives the toolbar ===')
n = len(conn.ch.sent)
app.sendEvent(tw, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Backtab,
                            Qt.KeyboardModifier.ShiftModifier, ''))
spin(60)
check('Backtab forwarded', bytes(conn.ch.sent[n:]) == b'\x1b[Z',
      repr(bytes(conn.ch.sent[n:])))
check('focus still on the terminal', app.focusWidget() is tw)

print('=== Tab is NOT stolen when focus is elsewhere ===')
mw.sz_target_edit.setFocus() if hasattr(mw, 'sz_target_edit') else tab.sz_target_edit.setFocus()
spin(60)
check('focus moved to the path edit', app.focusWidget() is tab.sz_target_edit,
      type(app.focusWidget()).__name__ if app.focusWidget() else 'None')
tw.setFocus(); spin(40)

print('=== toolbar buttons are still reachable by Tab ===')
# outside the terminal, Qt focus traversal must keep working
before = app.focusWidget()
app.sendEvent(tw, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Tab,
                            Qt.KeyboardModifier.NoModifier, '\t'))
spin(50)
check('terminal keeps focus (Tab stays ours)', app.focusWidget() is tw,
      type(app.focusWidget()).__name__ if app.focusWidget() else 'None')

print('=== letters still work (baseline sanity) ===')
n = len(conn.ch.sent)
for ch in 'ls':
    app.sendEvent(tw, QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_A,
                                Qt.KeyboardModifier.NoModifier, ch))
spin(60)
check('letters forwarded', bytes(conn.ch.sent[n:]) == b'ls', repr(bytes(conn.ch.sent[n:])))

# -------------------------------------------------- teardown
# 收尾不是"整洁问题"，是**稳定性问题**。这里建了 MainWindow、SessionTab
# 和一对 socket，全都不关就让解释器退出的话，Qt 对象的寿命会比 QApplication
# 还长；退出过程中要是有一个待触发的回调摸到正在销毁的 C++ 对象，整个进程
# 就是 0xC0000005。
#
# 而且它是**偶发**的：改动之前实测 30 次崩 1 次，崩的那次断言全绿、结论行
# 照打，只是进程在最后一步死了。判据对不对跟它一点关系没有，但一个每三十
# 轮误报一次的套件，迟早被人当成"偶发，不看"。
#
# `close()` 只是发 closeEvent，**不回收**；`deleteLater()` 之后还得
# `sendPostedEvents(DeferredDelete)` 才真的析构。
conn.ch.close()
mw.close()
host.close()
mw.deleteLater()
host.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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
if FAIL:
    print(f'RESULT: {len(FAIL)} FAILURES -> {FAIL}')
    sys.exit(1)
print('RESULT: REAL TAB WIRING OK')
