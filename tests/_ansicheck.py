"""Render a full ANSI colour swatch in both themes for visual verification."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.fonts import boot

app = boot([])
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from sshclient.core import fonts
from sshclient.ui.design.v1 import palette
from sshclient.ui.terminal_widget import TerminalWidget

out = os.path.join(ROOT, '_ansi_swatch.png')
hosts = []

for tn in ('dark', 'light'):
    pal = palette.get_palette(tn)
    t = TerminalWidget()
    t._font = fonts.mono_font(13)
    t.setFont(t._font)
    t._update_metrics()
    t.apply_palette(pal)
    t.resize(980, 190)
    t.write(
        b'normal:  \x1b[30mbl\x1b[0m \x1b[31mrd\x1b[0m \x1b[32mgr\x1b[0m \x1b[33mye\x1b[0m '
        b'\x1b[34mbl\x1b[0m \x1b[35mma\x1b[0m \x1b[36mcy\x1b[0m \x1b[37mwh\x1b[0m\r\n'
        b'bright:  \x1b[90mbl\x1b[0m \x1b[91mrd\x1b[0m \x1b[92mgr\x1b[0m \x1b[93mye\x1b[0m '
        b'\x1b[94mbl\x1b[0m \x1b[95mma\x1b[0m \x1b[96mcy\x1b[0m \x1b[97mwh\x1b[0m\r\n'
        b'bold:    \x1b[1;31mrd\x1b[0m \x1b[1;32mgr\x1b[0m \x1b[1;34mbl\x1b[0m '
        b'\x1b[1;33mye\x1b[0m \x1b[1;36mcy\x1b[0m\r\n'
        b'256:     \x1b[38;5;208mo208\x1b[0m \x1b[38;5;46mg046\x1b[0m \x1b[38;5;196mr196\x1b[0m '
        b'\x1b[38;5;33mb033\x1b[0m \x1b[38;5;129mg129\x1b[0m\r\n'
        b'true:    \x1b[38;2;255;100;200mpink\x1b[0m \x1b[38;2;100;200;255mblue\x1b[0m '
        b'\x1b[38;2;255;200;100morange\x1b[0m \x1b[38;2;120;255;140mmint\x1b[0m\r\n'
        b'bg:      \x1b[41;97m redbg \x1b[0m \x1b[42;30m grnbg \x1b[0m \x1b[44;97m bluebg \x1b[0m '
        b'\x1b[100;97m graybg \x1b[0m\r\n'
        b'reverse: \x1b[7m reversed \x1b[0m  underline: \x1b[4munderlined\x1b[0m  '
        b'256bg: \x1b[48;5;24m tealbg \x1b[0m\r\n'
    )
    host = QWidget(); host.setStyleSheet(f'background:{pal.bg_base};padding:6px')
    lay = QVBoxLayout(host); lay.setContentsMargins(0, 0, 0, 0); lay.addWidget(t)
    host.resize(980, 200); host.show(); t.show()
    for _ in range(6):
        app.processEvents()
    t.repaint(); host.repaint(); app.processEvents()
    img = host.grab().toImage()
    hosts.append(host)
    if tn == 'dark':
        dark_img = img
    else:
        light_img = img
        img.save(out)

# stack both themes into one file
from PyQt6.QtGui import QImage, QPainter

combo = QImage(980, dark_img.height() + light_img.height(), QImage.Format.Format_RGB32)
combo.fill(0)
p = QPainter(combo)
p.drawImage(0, 0, dark_img)
p.drawImage(0, dark_img.height(), light_img)
p.end()
combo.save(out)
print('saved', out)
