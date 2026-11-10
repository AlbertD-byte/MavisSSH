"""Screenshots for the SFTP and monitor panels."""
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.fonts import _HOSTS, boot

app = boot([])
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from sshclient.ui import monitor_panel, sftp_panel
from sshclient.ui.design.v1 import palette
from sshclient.ui.design.v1.qss import build_qss

OUT = pathlib.Path(ROOT)
p = palette.get_palette('dark')

def shoot(w, name, W, H, pal):
    host = QWidget(); host.setStyleSheet(build_qss(pal))
    lay = QVBoxLayout(host); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(w)
    host.resize(W, H); host.show(); w.show()
    for _ in range(5): app.processEvents()
    w.repaint(); host.repaint(); app.processEvents()
    path = OUT / name
    host.grab().toImage().save(str(path))
    print('saved', path.name)
    _HOSTS.append(host)

# --- SFTP ---
class A:
    def __init__(s, n, m, sz, mt): s.filename=n; s.st_mode=m; s.st_size=sz; s.st_mtime=mt
class FakeSftp:
    def listdir_attr(self, path):
        print('    listdir_attr(%r) -> 7' % path)
        return [
            A('logs', 0o40755, 0, 1755000000),
            A('app', 0o40755, 0, 1755000100),
            A('nginx', 0o40755, 0, 1755000200),
            A('access.log', 0o100644, 48239104, 1755001000),
            A('error.log', 0o100644, 1920000, 1755002000),
            A('deploy.sh', 0o100755, 812, 1755003000),
            A('app.conf', 0o100644, 2048, 1755004000),
        ]

    def normalize(self, x):
        print('    normalize(%r) -> /var/log' % x)
        return '/var/log'

    def stat(self, x):
        raise OSError()

    def get(self, *a, **k):
        pass

    def put(self, *a, **k):
        pass

sp = sftp_panel.SftpPanel(FakeSftp(), p)
sp.local.cd(ROOT)
sp.remote.refresh()
app.processEvents()
print('local rows:', sp.local.model.rowCount(), ' remote rows:', sp.remote.model.rowCount())
shoot(sp, '_sftp_dark.png', 1240, 620, p)
print('after shoot -> local rows:', sp.local.model.rowCount(), ' remote rows:', sp.remote.model.rowCount())

# --- Monitor ---
FAKE = open(os.path.join(ROOT, '_probe_sample.txt'), encoding='utf-8').read() \
    if pathlib.Path(os.path.join(ROOT, '_probe_sample.txt')).exists() else ''
if not FAKE:
    FAKE = '\n'.join([
        '##HOST##', 'prod-web-01',
        '##OS##', 'Linux 5.15.0-91-generic x86_64',
        '##UPTIME##', 'up 12 days,  4:31,  2 users,  load average: 0.42, 0.38, 0.31',
        '##LOAD##', '0.42 0.38 0.31 2/341 8891',
        '##CPU##', '%Cpu(s):  8.3 us,  2.1 sy,  0.0 ni, 89.4 id,  0.2 wa,  0.0 hi',
        '##MEM##', '              total        used        free      shared  buff/cache   available',
        'Mem:           7900        3120        1800         120        2980        4400',
        '##DISK##', 'Filesystem      Size  Used Avail Use% Mounted on',
        '/dev/sda1        50G   21G   27G  45% /',
        '/dev/sdb1       200G  182G   18G  91% /data',
        'tmpfs           1.0G   64M  936M   7% /tmp',
        '##NET##',
        '  eth0: 918273645 901234    0    0    0     0          0         0 445566778 334455    0    0    0     0       0          0',
        '##PROC##', '   PID USER     %CPU %MEM     ELAPSED COMMAND',
        '  1201 root      8.3  4.1   12:04:11 python3',
        '  1455 nginx     2.1  1.8  40:22:03 nginx',
        '  2210 deploy    0.4  0.2  00:02:11 bash',
        '  3390 root      0.2  0.1  00:00:04 ps',
        '##END##', ''])

class FakeConn:
    def exec(self, cmd, timeout=None): return (FAKE, '')

mp = monitor_panel.MonitorPanel(FakeConn(), 60000, p)
snap = monitor_panel.parse_snapshot(FAKE)
mp._apply_snapshot(snap)
shoot(mp, '_monitor_dark.png', 1240, 760, p)
mp.stop()
print('done')
