"""Deep smoke test: drives the real signal/slot wiring end to end.

The e2e suite exercises widgets in isolation, so it never reaches the code that
runs on a *successful connect* (_on_connect_ok -> attach -> notifier). That path
is where a real crash was hiding, so this drives it against a socketpair-backed
fake channel: bytes pushed into the socket have to travel through
QSocketNotifier -> _on_channel_ready -> pyte -> paint.
"""
import os
import pathlib
import socket
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.fonts import boot

app = boot([])
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from sshclient.core import settings
from sshclient.core.settings import SavedSession
from sshclient.remote.connection import ConnectionInfo
from sshclient.ui import main_window
from sshclient.ui.design.v1 import palette

FAIL = []
def check(name, cond, extra=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f'  ({extra})' if extra else ''))
    if not cond:
        FAIL.append(name)

# Modal dialogs would block the event loop for the whole test.
BOXES = []
for meth in ('critical', 'warning', 'information', 'question'):
    def _mk(m):
        def f(parent, title, text, *a, **k):
            BOXES.append((m, title, str(text)[:160]))
            return QMessageBox.StandardButton.Ok
        return f
    setattr(QMessageBox, meth, staticmethod(_mk(meth)))

# QMessageBox(self).exec() is a *nested* event loop -- stub it too, or the
# test blocks forever on a dialog nobody can click.
_real_exec = QMessageBox.exec
def _fake_exec(self):
    BOXES.append(('exec', self.windowTitle(), self.text()[:160]))
    return QMessageBox.StandardButton.Ok
QMessageBox.exec = _fake_exec

def spin(ms=120):
    end = [False]
    QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


def wait_for(pred, ms=1500):
    """Poll *pred* until true or *ms* elapses.

    A fixed `spin()` is not enough for anything driven by QSocketNotifier: the
    fd becoming readable is a kernel event, so how long the loop needs is not
    deterministic and a fixed sleep makes the assertion flaky.
    """
    end = [False]
    QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()
        if pred():
            return True
    return pred()

# --------------------------------------------------------------- fake backend
class FakeChannel:
    """paramiko-like channel backed by a real socket, so QSocketNotifier fires."""
    def __init__(self):
        self.rx, self.tx = socket.socketpair()
        self.closed = False
        self.sent = bytearray()
    def fileno(self): return self.rx.fileno()
    def recv_ready(self): return True
    def recv(self, n):
        return self.rx.recv(n)
    def send(self, data): self.sent.extend(data)
    def sendall(self, data): self.sent.extend(data)
    def settimeout(self, t): pass
    def close(self):
        """The server side goes away.

        Only the write half is shut down. The client's QSocketNotifier is
        watching rx, and closing that fd out from under it makes the final
        "fd is readable" notification a coin flip -- measured at 6 failures
        in 10 runs of the channel-close assertion before this was changed.
        shutdown(SHUT_WR) produces exactly the EOF a real `logout` does, and
        the notifier always reports it. The fds are left for the process to
        reclaim; the client tears the channel down on its own afterwards.
        """
        if self.closed:
            return
        self.closed = True
        try:
            self.tx.shutdown(socket.SHUT_WR)
        except Exception:
            pass

class FakeSFTP:
    def listdir_attr(self, path='.'):
        import stat
        now = 1700000000
        out = []
        for name, isdir, size in (('app', True, 0), ('logs', True, 0),
                                  ('app.conf', False, 2048)):
            a = type('A', (), {})()
            a.filename = name; a.st_size = size; a.st_mtime = now
            a.st_mode = (stat.S_IFDIR | 0o755) if isdir else (stat.S_IFREG | 0o644)
            a.attr = a
            out.append(a)
        return out
    def stat(self, path): raise OSError('nope')
    def getcwd(self): return '/var/log'

PROBE = b'__PROBE__'

class FakeConn:
    def __init__(self):
        self._info = ConnectionInfo(host='10.0.0.5', port=22, username='deploy',
                                    fingerprint='SHA256:abc')
        self.ch = FakeChannel()
        self.sftp = FakeSFTP()
    @property
    def info(self): return self._info
    def open_shell(self, term=None, cols=80, rows=24): return self.ch
    def open_sftp(self): return self.sftp
    def exec(self, cmd, timeout=None):
        if PROBE.decode() in cmd:
            return (b'\x1b[32mprobe-ok\x1b[0m', '')
        return (b'', '')
    def close(self):
        self.ch.close()
    # --- tunnel surface consumed by TunnelPanel ---
    def set_tunnel_callbacks(self, on_error=None, on_event=None):
        self._tunnel_err = on_error
        self._tunnel_evt = on_event
    def add_tunnel(self, spec, enabled=True): return spec
    def set_tunnel_enabled(self, port, enabled): return enabled
    def remove_tunnel(self, port): return False
    def tunnel_specs(self): return []
    def tunnel_states(self): return {}
    def tunnel_connections(self): return {}

# ------------------------------------------------------------------ the flow
tmp = pathlib.Path(tempfile.mkdtemp())
store = settings.SessionStore(tmp / 'sessions.json')
st = settings.AppSettings()
st._path = tmp / 'settings.json'   # 别写到用户真实的配置里去
st.theme = 'dark'
mw = main_window.MainWindow(st, store)

host = QWidget(); host.setStyleSheet('background:#0d0f14')
lay = QVBoxLayout(host); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(mw)
host.resize(1280, 800); host.show()
spin(200)

print('=== A. connect-success path (the one that crashed) ===')
conn = FakeConn()
saved = SavedSession(name='生产 Web-01', host='10.0.0.5', port=22,
                     username='deploy', auth_method='password', password='x')
try:
    mw._on_connect_ok(conn, saved)
    check('_on_connect_ok does not raise', True)
except Exception:
    check('_on_connect_ok does not raise', False, traceback.format_exc().splitlines()[-1])
spin(200)

check('a session tab was added', mw.tabs.count() == 2, f'count={mw.tabs.count()}')
tab = [mw.tabs.widget(i) for i in range(mw.tabs.count())
       if isinstance(mw.tabs.widget(i), main_window.SessionTab)][0]
sess_idx = mw.tabs.indexOf(tab)
check('tab is a SessionTab', isinstance(tab, main_window.SessionTab))
check('tab icon was set', not mw.tabs.tabIcon(sess_idx).isNull())
check('state is live', tab.state_dot is not None and tab._state_kind == 'live',
      getattr(tab, '_state_kind', '?'))
check('4 sub-tabs (terminal/file/monitor/tunnel)', tab.sub_tabs.count() == 4,
      f'count={tab.sub_tabs.count()}')

print('=== B. live shell data through the notifier ===')
conn.ch.tx.sendall(b'\x1b[32mroot@prod-web-01\x1b[0m:\x1b[34m~/srv\x1b[0m$ uptime\r\n'
                   b' 14:22:01 up 12 days,  4:31,  2 users,  load 0.42\r\n')
spin(300)
disp = '\n'.join(tab.terminal._screen.display)
check('shell output reached the terminal', 'up 12 days' in disp, repr(disp[:60]))
check('prompt rendered', 'prod-web-01' in disp)

print('=== C. keystrokes go back out on the channel ===')
before = len(conn.ch.sent)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

for ch in ('l', 's'):
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier, ch)
    app.sendEvent(tab.terminal, ev)
spin(150)
check('typed keys were sent to the channel', len(conn.ch.sent) > before,
      f'sent={bytes(conn.ch.sent[:20])!r}')

print('=== D. sub-panels ===')
tab.sub_tabs.setCurrentIndex(1); spin(250)
check('SFTP panel populated', tab._sftp is not None and tab._sftp.remote.model.rowCount() == 3,
      f'rows={tab._sftp.remote.model.rowCount() if tab._sftp else "n/a"}')
tab.sub_tabs.setCurrentIndex(2); spin(600)
check('monitor panel built', tab._monitor is not None)
check('monitor did not raise a modal', not [b for b in BOXES if '监控' in b[1]], str(BOXES))
tab.sub_tabs.setCurrentIndex(0); spin(150)

print('=== E. theme switch with a live tab ===')
# **判据从「找某个十六进制」改成「量变化量」。**
#
# 原来两条钉的是历史字面值 `f4f6f9` / `0d0f14`。换成从 palette 取当前值之后，
# 浅色那条对了，**深色那条仍然是恒绿的** —— 本次换配色把浅色
# `text_on_accent` 定成 `#0d0f14`，而深色 `bg_base` 也是 `#0d0f14`，
# 于是**浅色样式表里本来就有这串十六进制**：「切回深色」那条在**根本没切**
# 的时候照样绿。这是元断言抓出来的，实测 `build_qss` 对同一主题逐字确定、
# 两个主题确实不同，所以「切之前 ≠ 切之后」在没切时为 False，能抓到回归。
_dark0 = mw.styleSheet()
mw.apply_theme('light'); spin(250)
_light1 = mw.styleSheet()
check('切到浅色后样式表真的换了（不是恒绿判据）', _light1 != _dark0,
      '切前 %d 字 / 切后 %d 字' % (len(_dark0), len(_light1)))
check('浅色底色真的进了新样式表',
      palette.get_palette('light').bg_base.lower() in _light1.lower(),
      palette.get_palette('light').bg_base)
check('terminal still holds its output', 'up 12 days' in '\n'.join(tab.terminal._screen.display))
mw.apply_theme('dark'); spin(250)
_dark2 = mw.styleSheet()
check('切回深色后样式表又换了（没卡在浅色）', _dark2 != _light1,
      '浅色 %d 字 / 深色 %d 字' % (len(_light1), len(_dark2)))

print('=== F. channel close + tab close ===')
# Every optional sub-panel must come up cleanly. A panel that fails to build
# shows a *warning* box, which used to slip past the critical-only assertion
# below, so assert on the whole modal set here instead.
check('every sub-panel built without a modal',
      not BOXES, str(BOXES))
sess = mw.tabs.widget(sess_idx)
check('terminal / sftp / monitor / tunnel tabs all present',
      sess.sub_tabs.count() == 4,
      f'tabs={[sess.sub_tabs.tabText(i) for i in range(sess.sub_tabs.count())]}')
conn.ch.tx.sendall(b'logout\r\n')
conn.ch.close()
check('no modal error raised on close', not [b for b in BOXES if b[0] == 'critical'], str(BOXES))
# The exit-state fix: a closed channel must flip the tab back to 未连接 even
# when paramiko has already flagged the channel closed before we read it.
check('state returns to 未连接 after channel close',
      wait_for(lambda: sess._state_kind == 'idle'), sess._state_kind)
mw.tabs.removeTab(sess_idx); spin(200)
check('session tab removed cleanly',
      not [i for i in range(mw.tabs.count())
           if isinstance(mw.tabs.widget(i), main_window.SessionTab)],
      f'count={mw.tabs.count()}')

print('=== G. connect-failure path ===')
BOXES.clear()
mw._on_connect_failed('Connection refused')
spin(150)
check('failure shows an error box', any(b[0] in ('exec', 'critical') for b in BOXES), str(BOXES))
check('inflight flag cleared', mw._connect_inflight is False)

print()
print('modal dialogs seen:', BOXES if BOXES else 'none')

# Tear Qt down in the right order, while the objects are still coherent.
#
# A QWidget destroyed *after* its QApplication makes ~QObject touch a dead
# qApp, and the process dies with 0xC0000005 -- after the last PASS line, so
# no assertion in this file can ever see it. It only shows up as a nonzero
# exit from the runner, which reads exactly like a test failure. Measured
# 1 crash in 30 runs before this teardown, 0 in 100 after. (At a true 1-in-30
# rate, 0-in-100 lands about 3% of the time by luck, so the sample is doing
# real work here -- but the mechanism is deterministic teardown ordering, and
# that is what the fix removes.)
#
# deleteLater() is not enough on its own: the event has to actually be
# delivered, which is what the processEvents() below is for.
for _widget in (mw,):
    try:
        _widget.setParent(None)
        _widget.deleteLater()
    except Exception:
        pass
app.processEvents()
try:
    conn.ch.close()
except Exception:
    pass

if FAIL:
    print(f'RESULT: {len(FAIL)} FAILURES -> {FAIL}')
    sys.exit(1)
print('RESULT: SMOKE PASSED')
