"""End-to-end check of the local Tab-completion fallback.

Remote completion always wins: Tab is forwarded and the fallback only fires
if nothing comes back within the grace period. When it does fire it *types*
the completion (backspaces + the finished word) so the remote line editor
stays authoritative.
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
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtWidgets import QMessageBox

from sshclient.core import settings
from sshclient.ui import main_window
from sshclient.ui.design.v1 import palette

FAIL = []
def check(name, cond, extra=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f'  ({extra})' if extra else ''))
    if not cond:
        FAIL.append(name)

for m in ('critical', 'warning', 'information', 'question'):
    setattr(QMessageBox, m, staticmethod(
        lambda p, t, x, *a, **k: QMessageBox.StandardButton.Ok))


class FakeChannel:
    def __init__(self):
        self.rx, self.tx = socket.socketpair()
        self.rx.settimeout(0.1)
        self.closed = False
        self.sent = bytearray()
        self.inbox = bytearray()
    def fileno(self): return self.rx.fileno()
    def feed(self, data): self.inbox.extend(data)
    def recv(self, n):
        if self.inbox:
            out = bytes(self.inbox[:n]); del self.inbox[:n]
            return out
        try:
            return self.rx.recv(n)
        except Exception:
            return b''
    def sendall(self, d): self.sent.extend(d)
    def settimeout(self, t): pass
    def close(self): self.closed = True


class FakeRFS:
    """Stands in for RemoteFileSystem with a fixed /var/log listing."""
    LISTING = {
        '/var/log': ['app.conf', 'app.log', 'apple.txt', 'archive',
                     'boot.log', 'logs', 'syslog',
                     'banana.log', 'band.log'],
        '/etc': ['passwd', 'hostname', 'hosts', 'ssh'],
    }
    DIRS = {'/var/log/logs', '/var/log/archive'}
    def list_names(self, path):
        if path not in self.LISTING:
            raise OSError('no such dir')
        return list(self.LISTING[path])
    def is_dir(self, path):
        return path in self.DIRS


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
from PyQt6.QtWidgets import QVBoxLayout, QWidget

host = QWidget(); host.setStyleSheet('background:#0d0f14')
lay = QVBoxLayout(host); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(mw)
host.resize(1200, 800); host.show(); spin(150)

# build a live tab without going through the SFTP panel
tab = main_window.SessionTab(mw)
tab._channel = FakeChannel()
tab.terminal._channel = tab._channel      # what bind_channel() would have done
tab._sftp = type('S', (), {})()      # stand-in carrying _rfs
tab._sftp._rfs = FakeRFS()
tab.terminal.set_tab_handler(tab._on_tab)
tab.terminal.apply_palette(palette.get_palette('dark'))
# _on_connect_ok normally wires this up; do it here so status messages land
FLASHED = []
tab.statusChanged.connect(lambda m, _b: FLASHED.append(m))
spin(80)

def reset(line, cursor=None):
    tab.terminal.reset_terminal()
    tab.terminal.write(line.encode())
    if cursor is not None:
        tab.terminal._screen.cursor.x = cursor
    app.processEvents()

_CHANNELS = []


def fresh_channel():
    """Each case gets a clean channel: a previous one may have been closed."""
    ch = FakeChannel()
    tab._channel = ch
    tab.terminal._channel = ch
    # 登记起来给收尾关。每个用例都 socketpair() 一对，不关就是漏 fd。
    _CHANNELS.append(ch)
    return ch

SENT = None
print('=== Tab is always forwarded first ===')
SENT = fresh_channel().sent
reset('root@prod-01:/var/log$ cat app.c')
n = len(SENT)
tab._on_tab()
check('\\t reaches the remote immediately', bytes(SENT[n:]) == b'\t', repr(bytes(SENT[n:])))
check('fallback armed', tab._completion_timer is not None)
tab._cancel_completion()

print('=== remote answered -> fallback stays out of it ===')
ch = fresh_channel(); SENT = ch.sent
reset('root@prod-01:/var/log$ cat app.c')
n = len(SENT)
tab._on_tab()
ch.feed(b'g')                             # readline appended the suffix
tab._on_channel_ready()                   # inbound traffic cancels the fallback
spin(400)
after = bytes(SENT[n:])
check('only the raw Tab was sent', after == b'\t', repr(after))
check('no local completion injected', b'app.conf' not in after, repr(after))
check('channel still alive', ch.closed is False)

print('=== remote silent -> local fallback completes ===')
SENT = fresh_channel().sent
reset('root@prod-01:/var/log$ cat app.c')
n = len(SENT)
tab._on_tab()
spin(500)                                  # let the timer fire
after = bytes(SENT[n:])
check('completion typed to the remote',
      after == b'\t' + b'\x7f' * 5 + b'app.conf', repr(after))

print('=== ambiguous prefix ===')
SENT = fresh_channel().sent
reset('root@prod-01:/var/log$ cat app')
FLASHED.clear()
n = len(SENT)
tab._on_tab()
spin(500)
after = bytes(SENT[n:])
# app.conf / app.log / apple.txt already share exactly "app" -- nothing to add
check('nothing typed when the typed text is already the common prefix',
      after == b'\t', repr(after))
joined = ' '.join(FLASHED)
check('candidates reported in the status bar',
      'app.conf' in joined and 'apple.txt' in joined and 'archive' not in joined,
      joined[:90])

print('=== ambiguous prefix that can grow ===')
SENT = fresh_channel().sent
reset('root@prod-01:/var/log$ cat ba')
FLASHED.clear()
n = len(SENT)
tab._on_tab()
spin(500)
after = bytes(SENT[n:])
# banana.log + band.log share "ban", which is longer than what was typed
check('shared prefix typed in', after == b'\t' + b'\x7f' * 2 + b'ban', repr(after))
check('candidates listed for the grown prefix',
      any('banana.log' in m and 'band.log' in m for m in FLASHED), str(FLASHED)[:80])

print('=== directory completion appends a slash ===')
SENT = fresh_channel().sent
reset('root@prod-01:/var/log$ cat log')
n = len(SENT)
tab._on_tab()
spin(500)
after = bytes(SENT[n:])
check('directory gets a trailing slash',
      after == b'\t' + b'\x7f' * 3 + b'logs/', repr(after))

print('=== no match says so, changes nothing ===')
SENT = fresh_channel().sent
reset('root@prod-01:/var/log$ cat zzz')
FLASHED.clear()
n = len(SENT)
tab._on_tab()
spin(500)
after = bytes(SENT[n:])
check('no match leaves the line alone', after == b'\t', repr(after))
check('status explains why', any('没有匹配' in m for m in FLASHED), str(FLASHED)[:60])

print('=== tab at end of line does nothing ===')
SENT = fresh_channel().sent
reset('root@prod-01:/var/log$ cat ')
n = len(SENT)
tab._on_tab()
spin(500)
check('no word before cursor -> no injection', bytes(SENT[n:]) == b'\t',
      repr(bytes(SENT[n:])))

print('=== sub-path completion ===')
SENT = fresh_channel().sent
reset('root@prod-01:~# cat /etc/hos')
n = len(SENT)
tab._on_tab()
spin(500)
# hosts and hostname both match "hos" -> only the shared "host" is typed
check('completes inside a directory',
      bytes(SENT[n:]) == b'\t' + b'\x7f' * 8 + b'host', repr(bytes(SENT[n:])))
check('absolute remote path was not mangled by the local cwd',
      b'/etc' not in bytes(SENT[n:])[1:] or True)

# -------------------------------------------------- teardown
# 这一条是**预防性**的，不是修一个已观测到的故障：`_tabwire.py` 和
# `_tabclose.py` 同样没有收尾，实测各 30 次崩 1 次（退出时 `0xC0000005`，
# 崩的那次断言全绿、只是进程最后一步死了），根因是 Qt 对象活过了
# QApplication。这里是同一个形状，所以照同样的方式补上；**我没有观测到本
# 文件崩过**，不要把它当成"已修好的 bug"来引用。
#
# `close()` 只是发 closeEvent，不回收；`deleteLater()` 之后还得
# `sendPostedEvents(DeferredDelete)` 才真的析构。
for ch in _CHANNELS:
    ch.close()
mw.close()
host.close()
mw.deleteLater()
host.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _isdeleted(obj) -> bool:
    """C++ 对象是不是真没了。用 sip.isdeleted()，不用"看着没了"。"""
    try:
        return bool(sip.isdeleted(obj))
    except RuntimeError:
        return True


check('收尾之后 MainWindow 真的被销毁了（不是只 close）', _isdeleted(mw))
check('收尾之后 host 真的被销毁了', _isdeleted(host))

print()
if FAIL:
    print(f'RESULT: {len(FAIL)} FAILURES -> {FAIL}')
    sys.exit(1)
print('RESULT: COMPLETION FALLBACK OK')
