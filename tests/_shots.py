"""Screenshot harness: renders the key screens in both themes for review."""
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.fonts import _HOSTS, boot

app = boot([])
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from sshclient.core import settings
from sshclient.core.settings import SavedSession
from sshclient.ui import connect_dialog, main_window
from sshclient.ui.design.v1 import palette

OUT = pathlib.Path(ROOT)

def shoot(widget, name, w, h, theme_name):
    p = palette.get_palette(theme_name)
    host = QWidget(); host.setStyleSheet(f'background:{p.bg_base}')
    lay = QVBoxLayout(host); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(widget)
    host.resize(w, h); host.show(); widget.show()
    for _ in range(5): app.processEvents()
    widget.repaint(); host.repaint(); app.processEvents()
    path = OUT / name
    host.grab().toImage().save(str(path))
    print('saved', path.name)
    _HOSTS.append(host)

import tempfile

tmp = pathlib.Path(tempfile.mkdtemp())

for tn in ('dark', 'light'):
    p = palette.get_palette(tn)
    store = settings.SessionStore(tmp / f'sessions_{tn}.json')
    for s in [
        SavedSession(name='生产 Web-01', host='10.0.0.5', port=22, username='deploy',
                     auth_method='key', key_filename=r'C:\keys\id_ed25519'),
        SavedSession(name='测试环境', host='192.168.1.50', port=22, username='root',
                     auth_method='password', password='x'),
    ]:
        store.upsert(s)

    st = settings.AppSettings()
    st._path = store.path.parent / 'settings.json'
    st.theme = tn
    win = main_window.MainWindow(st, store)
    shoot(win, f'_ui_{tn}.png', 1280, 820, tn)

    dlg = connect_dialog.ConnectDialog(win, palette=p)
    dlg.name_edit.setText('生产 Web-01'); dlg.host_edit.setText('10.0.0.5')
    dlg.user_edit.setText('deploy'); dlg.auth_tabs.setCurrentIndex(1)
    dlg.key_path_edit.setText(r'C:\Users\deploy\.ssh\id_ed25519')
    shoot(dlg, f'_dlg_{tn}.png', 500, 620, tn)

    # Session tab with terminal content
    tab = main_window.SessionTab(win)
    tab.sub_tabs.setTabText(0, '终端')
    tab.terminal.resize(900, 400)
    tab.apply_palette(p)   # whole tab: re-tints toolbar icons, not just the terminal
    tab.terminal.write(b'root@prod-web-01:/srv# \x1b[32mls -la\x1b[0m\r\n'
                       b'drwxr-xr-x  12 root root 4096 \x1b[1;34mAug 21 10:22\x1b[0m  \x1b[00;32msrc\x1b[0m\r\n'
                       b'-rw-r--r--   1 root root  812 \x1b[00;31mdeploy.sh\x1b[0m\r\n'
                       b'\r\n\x1b[1mroot@prod-web-01\x1b[0m:\x1b[0;34m~/srv\x1b[0m$ ')
    tab._set_state('live')
    shoot(tab, f'_tab_{tn}.png', 1000, 520, tn)

print('done')
