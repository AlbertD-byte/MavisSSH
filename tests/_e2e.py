"""End-to-end verification: boot -> build UI -> exercise every panel -> theme switch."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests import sidebar_rows
from tests.fonts import boot, ink_count, snap

app = boot([])
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from sshclient.core import fonts, lrzsz, settings
from sshclient.ui import (
    connect_dialog,
    icons,
    main_window,
    monitor_panel,
    sftp_panel,
    terminal_widget,
)
from sshclient.ui.design.v1 import palette, qss

FAIL = []
_dialogs = []
def check(label, cond, extra=''):
    if cond:
        print(f'  PASS  {label}')
    else:
        print(f'  FAIL  {label} {extra}')
        FAIL.append(label)

# Headless runs must never block on a modal box; record instead of exec.
from PyQt6.QtWidgets import QMessageBox

QMessageBox.exec = lambda self: _dialogs.append(self.windowTitle())
QMessageBox.information = lambda *a, **k: _dialogs.append('info')
QMessageBox.warning = lambda *a, **k: _dialogs.append('warn')
QMessageBox.critical = lambda *a, **k: _dialogs.append('critical')

print('=== 1. imports ===')
from PyQt6.QtWidgets import QLabel

check('all modules import', True)

print('=== 2. design system ===')
p = palette.get_palette('dark')
sheet = qss.build_qss(p)
check('qss generated', len(sheet) > 5000, f'({len(sheet)} chars)')
check('ui font resolves', bool(fonts.ui_font().family()), fonts.ui_font().family())
check('mono font resolves', bool(fonts.mono_font().family()), fonts.mono_font().family())
check('ansi16 dark', len(palette.ansi_palette('dark')) == 16)
check('xterm256', len(palette.xterm256_palette()) == 240)

print('=== 3. icons ===')
check('icon set size', len(icons.ICONS) >= 30, f'({len(icons.ICONS)})')
missing = []
for n in icons.ICONS:
    pm = icons.icon_pixmap(n, p.text, 20)
    img = pm.toImage()
    nz = sum(1 for y in range(20) for x in range(20) if img.pixelColor(x, y).alpha() > 10)
    if nz < 8:
        missing.append(n)
check('every icon draws', not missing, str(missing))

print('=== 4. settings + store (isolated temp file) ===')
import pathlib
import tempfile

_tmpdir = pathlib.Path(tempfile.mkdtemp(prefix='mavisssh_e2e_'))
st = settings.SessionStore(_tmpdir / 'sessions.json')
check('store loads', isinstance(st.all, list), f'({len(st.all)} sessions)')
check('store is isolated', str(st.path).startswith(str(_tmpdir)), str(st.path))

print('=== 5. monitor parser ===')
fake = '''##HOST##
prod-web-01
##OS##
Linux 5.15.0-generic x86_64
##UPTIME##
up 12 days,  4:31
##LOAD##
0.42 0.38 0.31 2/341 8891
##CPU##
%Cpu(s):  8.3 us,  2.1 sy,  0.0 ni, 89.4 id,  0.2 wa
##MEM##
              total        used        free      shared  buff/cache   available
Mem:           7900        3120        1800         120        2980        4400
##DISK##
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1        50G   21G   27G  45% /
tmpfs           1.0G   64M  936M   7% /tmp
/dev/sdb1       200G  182G   18G  91% /data
##NET##
Inter-|   Receive                          |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth0: 918273645 901234    0    0    0     0          0         0 445566778 334455    0    0    0     0       0          0
##PROC##
   PID USER     %CPU %MEM     ELAPSED COMMAND
  1201 root      8.3  4.1   12:04:11 python3
  1455 nginx      2.1  1.8  40:22:03 nginx
  2210 deploy     0.4  0.2  00:02:11 bash
##END##
'''
s = monitor_panel.parse_snapshot(fake)
check('host parsed', s.host.name == 'prod-web-01', s.host.name)
check('cpu computed', 5 < s.cpu.used < 20, f'{s.cpu.used:.1f}%')
check('mem computed', 30 < s.mem.percent < 50, f'{s.mem.percent:.1f}%')
check('disks parsed', len(s.disks) == 3, f'({len(s.disks)})')
check('procs parsed (header skipped)', len(s.procs) == 3, f'({len(s.procs)})')
check('net parsed', s.net.rx_bytes == 918273645, str(s.net.rx_bytes))

print('=== 6. terminal widget ===')
tw = terminal_widget.TerminalWidget()
tw.resize(800, 300)
app.processEvents()
tw.write(b'root@prod-01:/srv# \x1b[32mls\x1b[0m\r\ntotal 48\r\n')
app.processEvents()
# force a resize AFTER writing -- this used to wipe the screen
tw.resize(900, 340)
app.processEvents()
tw.repaint(); app.processEvents()
check('content survives resize', 'root@prod-01' in ''.join(tw._screen.display),
      repr(''.join(tw._screen.display).strip()[:40]))
img = snap(tw, p.term_bg)
_ink = ink_count(img, p.term_bg)
check('terminal paints', _ink > 400, f'({_ink} px)')
tw.apply_palette(palette.get_palette('light'))
check('terminal light palette', tw._bg.name().lower() == palette.get_palette('light').term_bg.lower(),
      tw._bg.name())

print('=== 7. zmodem detection ===')
# 这几条量的是「认出有个头」，**不是**方向。`B0`（ZRQINIT）是 rz 和 sz 共用的
# 头 —— 真机实测两个命令发的都是 `**\x18B0…` —— 方向根本不在字节里。
check('rz/sz 共用的等待头被认出', lrzsz.find_zmodem_trigger(b'**\x18B00000000000000') is True)
check('normal text ignored', lrzsz.find_zmodem_trigger(b'hello world') is False)
check('zmodem header', lrzsz.looks_like_zmodem_header(b'\x18B00'))
check('rz binary found', lrzsz.resolve_binary('rz') is not None, str(lrzsz.resolve_binary('rz')))
check('sz binary found', lrzsz.resolve_binary('sz') is not None, str(lrzsz.resolve_binary('sz')))

# Direction mapping: the remote command and the local tool are always
# opposites. Remote `rz` receives, so uploading must spawn the local `sz`.
check('upload spawns local sz', lrzsz.ZmodemPump.LOCAL_TOOL['upload'] == 'sz')
check('download spawns local rz', lrzsz.ZmodemPump.LOCAL_TOOL['download'] == 'rz')

# SSH preserves no message boundaries, so the 4 magic bytes can be split.
det = lrzsz.ZmodemDetector()
check('no trigger on partial header', det.feed(b'rz waiting**\x18') is False)
check('trigger on continuation', det.feed(b'B00000000000000') is True,
      str(det.feed(b'')))
det2 = lrzsz.ZmodemDetector()
check('split across 3 chunks', det2.feed(b'x*') is False and det2.feed(b'*\x18') is False
      and det2.feed(b'B0abc') is True)
check('detector reset clears tail',
      (lambda d: (d.feed(b'\x18'), d.reset(), d.feed(b'B0abc') is False)[-1])(
          lrzsz.ZmodemDetector()))

print('=== 8. connect dialog ===')
dlg = connect_dialog.ConnectDialog(palette=p)
dlg.host_edit.setText('10.0.0.5'); dlg.user_edit.setText('deploy')
dlg.password_edit.setText('s3cret')
d = dlg.result_dict()
check('password flow', d['auth_method'] == 'password' and d['host'] == '10.0.0.5', str(d))
dlg.auth_tabs.setCurrentIndex(1)
dlg.key_path_edit.setText(r'C:\keys\id_ed25519')
d2 = dlg.result_dict()
check('key flow', d2['auth_method'] == 'key' and d2['key_filename'].endswith('id_ed25519'), str(d2))
# Empty host must be rejected -- the stubbed box records the attempt.
_shown = _dialogs
dlg.host_edit.setText('')
dlg.user_edit.setText('deploy')
dlg.accept()
check('empty host not accepted', bool(_shown), str(_shown))
dlg.host_edit.setText('10.0.0.5')

print('=== 9. main window ===')
_st = settings.AppSettings()
_st._path = _tmpdir / 'settings.json'   # 别写到用户真实的配置里去
mw = main_window.MainWindow(_st, st)
mw.resize(1280, 820)
mw.show()
for _ in range(4):
    app.processEvents()
check('window visible', mw.isVisible())
check('welcome tab present', mw.tabs.count() >= 1, f'({mw.tabs.count()})')
check('sidebar sized', mw.sidebar.width() == 268, str(mw.sidebar.width()))
check('qss applied', len(mw.styleSheet()) > 5000, f'({len(mw.styleSheet())})')

# add a saved session and confirm the row renders
from sshclient.core.settings import SavedSession

s1 = SavedSession(name='生产 Web-01', host='10.0.0.5', port=22, username='deploy',
                  auth_method='key', key_filename=r'C:\keys\id_ed25519')
mw.store.upsert(s1)
mw._reload_session_list()
app.processEvents()
# **行数算「会话行 + 分组标题行」。** 分组功能上线之前这里就是会话数，
# 现在哪怕只有一条会话，上面也有一行「默认」标题 —— 数 `count()` 会多 1，
# 而多出来的那一行不是会话。
check('session row added', len(sidebar_rows.session_rows(mw.session_list)) == 1,
      f'({mw.session_list.count()} 行 / {len(sidebar_rows.session_rows(mw.session_list))} 会话)')
row_widget = mw.session_list.itemWidget(sidebar_rows.session_item(mw.session_list))
check('row widget has labels', row_widget is not None and
      row_widget.findChild(QLabel, 'sessionName') is not None)
check('row shows host', row_widget is not None and
      row_widget.findChild(QLabel, 'sessionHost').text() == '10.0.0.5:22',
      row_widget.findChild(QLabel, 'sessionHost').text() if row_widget else '?')

print('=== 10. session tab (no live channel) ===')
tab = main_window.SessionTab(mw)
mw.tabs.addTab(tab, 'deploy@10.0.0.5:22')
app.processEvents()
check('session tab created', tab.terminal is not None)
check('sub-tab = terminal', tab.sub_tabs.count() == 1, f'({tab.sub_tabs.count()})')
tab.clear_btn.click(); tab.copy_btn.click(); tab.paste_btn.click()
check('toolbar buttons safe when idle', True)
tab.apply_palette(palette.get_palette('light'))
check('session tab palette applied', True)

print('=== 11. sftp panel ===')
class FakeAttr:
    def __init__(s, n, d, sz, mt, m):
        s.filename=n; s.st_mode=m; s.st_size=sz; s.st_mtime=mt
class FakeSftp:
    def listdir_attr(s, p):
        return [FakeAttr('logs', 0o40755, 0, 1700000000, 0o40755),
                FakeAttr('app.log', 0o100644, 20480, 1700000100, 0o100644),
                FakeAttr('deploy.sh', 0o100755, 812, 1700000200, 0o100755)]
    def normalize(s, p): return '/var/log'
    def stat(s, p): raise OSError('n/a')
    def get(s, *a, **k): pass
    def put(s, *a, **k): pass
sp = sftp_panel.SftpPanel(FakeSftp(), p)
sp.resize(1100, 500)
app.processEvents()
check('remote listed 3 rows', sp.remote.model.rowCount() == 3, f'({sp.remote.model.rowCount()})')
check('count label set', '\u4e2a\u76ee\u5f55' in sp.remote.count_label.text()
      and '\u4e2a\u6587\u4ef6' in sp.remote.count_label.text(), sp.remote.count_label.text())
check('local panel has rows', sp.local.model.rowCount() > 0, f'({sp.local.model.rowCount()})')
sp.apply_palette(palette.get_palette('light'))
check('sftp palette applied', True)

print('=== 12. monitor panel (fake conn) ===')
class FakeConn:
    # **这里原来写的是 `FAKE`（大写），一个从未定义过的名字。**
    # 后果不是测试红 —— `MonitorPanel` 的探测跑在 worker 线程里，NameError
    # 被吞成 `snap.error`，而这段测试紧接着直接拿 `parse_snapshot(fake)`
    # 的结果调 `_apply_snapshot(s)`，所以**探测这条路径一次都没被真正测到**，
    # 面板只是被喂了一份手工构造的快照。
    def exec(self, cmd, timeout=None): return (fake, '')
mp = monitor_panel.MonitorPanel(FakeConn(), 60000, p)
mp.resize(1100, 640)
app.processEvents()
mp._apply_snapshot(s)
check('cpu bar updated', mp.cpu_bar._percent > 0, f'({mp.cpu_bar._percent:.1f})')
check('mem bar updated', mp.mem_bar._percent > 0, f'({mp.mem_bar._percent:.1f})')
check('disk rows', mp.disk_table.rowCount() == 3, f'({mp.disk_table.rowCount()})')
check('proc rows', mp.proc_table.rowCount() == 3, f'({mp.proc_table.rowCount()})')
check('rx label', mp.rx_label.text().endswith(('MB', 'GB', 'KB', 'B')), mp.rx_label.text())
check('host label', mp.host_label.text() == 'prod-web-01', mp.host_label.text())
mp.apply_palette(palette.get_palette('light'))
check('monitor palette applied', True)
mp.stop()

print('=== 13. theme switch ===')
# **判据从「找某个十六进制」改成「量变化量」。**
#
# 原来两条钉的是历史字面值（浅色 `f4f6f9`、深色 `0d0f14`）。换成从 palette
# 取当前值之后，浅色那条对了，**深色那条仍然是恒绿的** ——
# 本次换配色把浅色 `text_on_accent` 定成 `#0d0f14`，而深色 `bg_base` 也是
# `#0d0f14`，于是**浅色样式表里本来就有这串十六进制**。「切回深色」那条于是
# 在**根本没切**的时候照样绿。
#
# 这是元断言抓出来的，不是想出来的：把「切之前」和「切之后」的样式表各存一份
# 比相等，实测 `build_qss` 对同一主题**逐字确定**、两个主题**确实不同**，
# 于是「切之前 ≠ 切之后」在没切时为 False —— **能**抓到回归。
#
# 判据量的本意是「切主题真的重建了样式表」，那就量重建这件事本身，
# 而不是量「样式表里碰巧出现了哪一串字符」。
_dark0 = mw.styleSheet()
mw.apply_theme('light')
app.processEvents()
_light1 = mw.styleSheet()
check('切到浅色后样式表真的换了（不是恒绿判据）', _light1 != _dark0,
      '切前 %d 字 / 切后 %d 字' % (len(_dark0), len(_light1)))
check('浅色底色真的进了新样式表',
      palette.get_palette('light').bg_base.lower() in _light1.lower(),
      palette.get_palette('light').bg_base)
mw.apply_theme('dark')
app.processEvents()
_dark2 = mw.styleSheet()
check('切回深色后样式表又换了（没卡在浅色）', _dark2 != _light1,
      '浅色 %d 字 / 深色 %d 字' % (len(_light1), len(_dark2)))
check('no crash across switches', True)

print('=== 14. terminal colour fidelity (survives resize replay) ===')
# Regression: _capture()/_sgr() used to re-emit every colour as "38;5;<index>",
# which made pyte hand back a bare hex string on replay. 256-colour and
# truecolour cells were washed to white (unknown name -> index 7) and the eight
# basic colours lost the themed palette. Showing the widget forces a resize, so
# this exercises the capture -> replay -> paint path end to end.
from sshclient.ui.terminal_widget import TerminalWidget, _color_to_hex

ct = TerminalWidget()
ct.resize(700, 120)
ct.write(b'\x1b[31mR\x1b[0m\x1b[38;5;208mO\x1b[0m\x1b[38;2;255;100;200mP\x1b[0m'
         b'\x1b[48;5;24mB\x1b[0m')
chost = QWidget(); chost.setStyleSheet(f'background:{p.bg_base}')
clay = QVBoxLayout(chost); clay.setContentsMargins(0, 0, 0, 0); clay.addWidget(ct)
chost.resize(700, 120); chost.show(); ct.show()
for _ in range(4): app.processEvents()

fgs = [ct._screen.buffer[0][c].fg for c in range(4)]
bgs = [ct._screen.buffer[0][3].bg]
ansi_dark = palette.ansi_palette('dark')
check('named red survives resize as a name', fgs[0] == 'red', repr(fgs[0]))
check('256-colour survives resize', fgs[1] == 'ff8700', repr(fgs[1]))
check('truecolour survives resize', fgs[2] == 'ff64c8', repr(fgs[2]))
check('256 background survives resize', bgs[0] == '005f87', repr(bgs[0]))
check('named red maps to themed colour',
      _color_to_hex('red', '#fff', ansi_dark) == ansi_dark[1], ansi_dark[1])
check('bare hex gets its # restored',
      _color_to_hex('ff8700', '#fff', ansi_dark) == '#ff8700')
check('bold promotes to bright variant',
      _color_to_hex('green', '#fff', ansi_dark, bold=True) == ansi_dark[10], ansi_dark[10])
for bad in ('notacolour', 'zz', 999):
    if _color_to_hex(bad, '#ffffff', ansi_dark) != '#ffffff':
        FAIL.append(f'fallback: {bad!r} did not fall back to default')
check('unknown values fall back to default fg', True)
chost.close()

print('=== 14b. wide (CJK) characters survive the capture -> replay round trip ===')
# Found by the live run (tests/_live.py, a real WSL2 box with a real PTY).
# pyte stores a wide character (CJK / full-width) as TWO cells: the glyph, then a
# continuation cell whose data is "".  Both _capture() and the paint loop used to
# turn that continuation into a space.  That alone is cosmetic -- but
# _resize_screen() feeds _capture()'s output back into pyte, so **every window
# resize permanently added one space between every pair of wide characters**:
#     1 resize  -> "中 文 宽 度 测 试"
#     4 resizes -> "中    文    宽    度    测    试"
# _capture() is the text model; the paint loop is what the user actually sees.
import re as _re

_SGR_RE = _re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
CJK = '中文宽度测试'
cw = TerminalWidget()
cwhost = QWidget(); cwlay = QVBoxLayout(cwhost)
cwlay.setContentsMargins(0, 0, 0, 0); cwlay.addWidget(cw)
cwhost.resize(700, 120); cwhost.show(); cw.show()
for _ in range(4): app.processEvents()
cw.write(CJK.encode('utf-8') + b'\r\n')
for _ in range(3): app.processEvents()


def cjk_line(w):
    for raw in w._capture():
        plain = _SGR_RE.sub('', raw).strip()
        if CJK[0] in plain:
            return plain
    return None


check('wide characters land as-is, no space injected',
      cjk_line(cw) == CJK, repr(cjk_line(cw)))

# Resizing is what runs capture -> replay, so this is the part that used to rot.
for w, h in ((900, 160), (1100, 200), (760, 140)):
    cw.resize(w, h); cwhost.resize(w, h)
    for _ in range(4): app.processEvents()
check('three resizes later the line is still exactly the six characters',
      cjk_line(cw) == CJK, repr(cjk_line(cw)))

# Negative control: the pre-fix _capture, verbatim, not a paraphrase.
_real_capture = TerminalWidget._capture


def _pre_fix_capture(self):
    if self._screen is None:
        return []
    default = self._default_char()
    out = []
    for row, line_text in enumerate(self._screen.display):
        char_dict = self._screen.buffer.get(row, {})
        pieces = []
        state = None
        for col in range(len(line_text)):
            ch = char_dict.get(col) or default
            data = ch.data if ch.data and ch.data != "\x00" else " "   # <- the bug
            key = tuple(getattr(ch, k, None) for k in self._ATTR_KEYS)
            if key != state:
                pieces.append(self._sgr(ch))
                state = key
            pieces.append(data)
        out.append("".join(pieces).rstrip(" ") + "\x1b[0m")
    return out


try:
    TerminalWidget._capture = _pre_fix_capture
    cw.resize(1300, 220); cwhost.resize(1300, 220)
    for _ in range(4): app.processEvents()
    got = cjk_line(cw)
    check('negative control: the pre-fix capture really does grow the line',
          got is not None and got != CJK and got.count(CJK[0] + ' ') > 0, repr(got))
finally:
    TerminalWidget._capture = _real_capture
# **重放是破坏性的。** 装回缺陷版跑的那一次已经把 `中 文 宽 度…` 当成
# 字面文本写进 pyte 缓冲区了，把方法换回来并不能撤销它 —— 旧代码的破坏
# 不可逆，只能重画。所以这里重新写一遍，再量「恢复之后还会不会增长」。
cw.write(b'\x1b[2J\x1b[H')
cw.write(CJK.encode('utf-8') + b'\r\n')
for _ in range(3): app.processEvents()
check('重新写一遍之后还是那六个字',
      cjk_line(cw) == CJK, repr(cjk_line(cw)))
cw.resize(1300, 220); cwhost.resize(1300, 220)
for _ in range(4): app.processEvents()
check('恢复真实现之后缩放不再撑开这一行',
      cjk_line(cw) == CJK, repr(cjk_line(cw)))
cwhost.close()

print('=== 14c. host fingerprint is actually populated ===')
# Found by the live run: paramiko 5 dropped `paramiko.util.hexlify`, the old
# inline call raised AttributeError, and a bare `except: pass` swallowed it --
# so the fingerprint stayed "" forever and the log said "主机指纹=未知" with
# nothing ever complaining. This section runs offline against stubs, because
# the only thing that used to "prove" this worked was the very log line that
# stayed unchanged while it was broken.
import paramiko as _paramiko

from sshclient.remote import connection as _conn


class _StubKey:
    fingerprint = 'SHA256:STUBSTUBSTUBSTUBSTUBSTUBSTUBSTUBSTUB'


class _OldKey:
    """A pre-fingerprint-attr key: only the MD5 bytes are available."""
    def get_fingerprint(self):
        return b'\xde\xad\xbe\xef'


class _BrokenTransport:
    def get_remote_server_key(self):
        raise RuntimeError('no key')


class _StubTransport:
    def __init__(self, key):
        self._key = key

    def get_remote_server_key(self):
        return self._key


check('fingerprint comes back in the readable SHA256: form',
      _conn.server_fingerprint(_StubTransport(_StubKey())) == _StubKey.fingerprint)
check('a key without .fingerprint still yields something (MD5 hex fallback)',
      len(_conn.server_fingerprint(_StubTransport(_OldKey()))) > 0)
check('a transport that cannot tell us yields "" instead of raising',
      _conn.server_fingerprint(_BrokenTransport()) == '')
check('the modern path does not go through paramiko.util.hexlify at all',
      _StubKey.fingerprint == _conn.server_fingerprint(_StubTransport(_StubKey()))
      and not hasattr(_paramiko.util, 'hexlify'),
      'paramiko.util.hexlify exists: %s' % hasattr(_paramiko.util, 'hexlify'))
# Negative control: the pre-fix expression, verbatim, really is broken here.
try:
    _broken = _paramiko.util.hexlify(_StubKey().get_fingerprint())
    check('negative control: the pre-fix expression fails on this paramiko',
          False, 'it did not raise (%r)' % (_broken,))
except Exception as _e:
    check('negative control: the pre-fix expression fails on this paramiko',
          True, '%s: %s' % (type(_e).__name__, _e))

print('=== 15. full-window screenshots ===')
host = QWidget(); host.resize(1280, 820); host.setStyleSheet(f'background:{p.bg_base}')
lay = QVBoxLayout(host); lay.setContentsMargins(0,0,0,0); lay.addWidget(mw)
host.show()
for _ in range(5): app.processEvents()
mw.repaint(); app.processEvents()
out = os.path.join(ROOT, '_ui_dark.png')
host.grab().toImage().save(out)
print('  saved', out)

print()
if FAIL:
    print(f'RESULT: {len(FAIL)} FAILURES -> {FAIL}')
    sys.exit(1)
print('RESULT: ALL CHECKS PASSED')
