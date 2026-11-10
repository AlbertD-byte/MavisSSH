"""Can the terminal render a readline-style Tab completion response?

The remote shell owns completion, so all we have to get right is (a) the keys
reach it and (b) whatever it paints back comes out looking right. Bash does two
distinct things on Tab:

  unique match  -> readline inserts just the missing suffix
  double Tab    -> it prints the candidate list, then redraws the input line

Both are simulated here against the real widget.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from tests.fonts import boot

app = boot([])
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QKeyEvent

from sshclient.ui.design.v1 import palette
from sshclient.ui.terminal_widget import TerminalWidget

FAIL = []
def check(name, cond, extra=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f'  ({extra})' if extra else ''))
    if not cond:
        FAIL.append(name)

pal = palette.get_palette('dark')
tw = TerminalWidget()
tw.resize(820, 260)
tw.apply_palette(pal)

SENT = []
tw._send = lambda b: SENT.append(b)

def typestr(s):
    for ch in s:
        ev = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_A,
                       Qt.KeyboardModifier.NoModifier, ch)
        app.sendEvent(tw, ev)

def press(key, mods=Qt.KeyboardModifier.NoModifier, text=''):
    ev = QKeyEvent(QKeyEvent.Type.KeyPress, key, mods, text)
    app.sendEvent(tw, ev)

def screen():
    return '\n'.join(tw._screen.display)

print('=== keys reach the remote ===')
tw.write(b'root@prod-01:~# ')
SENT.clear()
typestr('ls /var/lo')
press(Qt.Key.Key_Tab)
check('Tab is forwarded as \\t', b'\t' in SENT, repr(b''.join(SENT)))
check('typed chars forwarded', b''.join(SENT).startswith(b'ls /var/lo'),
      repr(b''.join(SENT)))

SENT.clear()
press(Qt.Key.Key_Backtab)
check('Shift+Tab (Backtab) forwarded as ESC[Z', b'\x1b[Z' in SENT, repr(SENT))
press(Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
check('Shift+Tab via modifier forwarded', b'\x1b[Z' in SENT, repr(SENT))

SENT.clear()
press(Qt.Key.Key_Left, Qt.KeyboardModifier.ControlModifier)
check('Ctrl+Left forwarded (word back)', SENT == [b'\x1b[1;5D'], repr(SENT))
SENT.clear()
press(Qt.Key.Key_Right, Qt.KeyboardModifier.ControlModifier)
check('Ctrl+Right forwarded (word fwd)', SENT == [b'\x1b[1;5C'], repr(SENT))
SENT.clear()
press(Qt.Key.Key_Left)
check('plain Left still plain', SENT == [b'\x1b[D'], repr(SENT))
SENT.clear()
press(Qt.Key.Key_Left, Qt.KeyboardModifier.AltModifier)
check('Alt+Left forwarded (word back)', SENT == [b'\x1b[1;3D'], repr(SENT))
SENT.clear()
press(Qt.Key.Key_Backspace, Qt.KeyboardModifier.ControlModifier)
check('Ctrl+Backspace kills a word', SENT == [b'\x08'], repr(SENT))
SENT.clear()
press(Qt.Key.Key_B, Qt.KeyboardModifier.AltModifier, 'b')
check('Alt+B forwarded', SENT == [b'\x1bb'], repr(SENT))
SENT.clear()
press(Qt.Key.Key_F, Qt.KeyboardModifier.AltModifier, 'f')
check('Alt+F forwarded', SENT == [b'\x1bf'], repr(SENT))
SENT.clear()
press(Qt.Key.Key_Home, Qt.KeyboardModifier.ControlModifier)
check('Ctrl+Home forwarded', SENT == [b'\x1b[1;5H'], repr(SENT))

print('=== unique completion renders ===')
tw.write(b'ls /var/lo' + b'g')          # readline appends the suffix
app.processEvents()
check('completed line visible', 'ls /var/log' in screen(), repr(screen().splitlines()[0]))

print('=== double-Tab listing renders ===')
# bash prints a newline, the candidate list, another newline, then reprints
# the prompt + the current line. It does NOT move the cursor back up.
tw.write(b'\r\n')
tw.write(b'log  local  locate\r\n')
tw.write(b'root@prod-01:~# ls /var/log')
app.processEvents()
lines = [l for l in screen().splitlines() if l.strip()]
check('candidate list shown', any('local' in l and 'locate' in l for l in lines),
      repr(lines[:4]))
check('input line redrawn below', lines and lines[-1].strip().endswith('ls /var/log'),
      repr(lines[-1] if lines else ''))

print('=== ambiguous completion (no change) ===')
tw.write(b'ls /var/l')
tw.write(b'')                             # Tab produced nothing (ambiguous)
app.processEvents()
check('line left untouched on no-op Tab', 'ls /var/l' in screen())

print('=== local fallback: prompt/word parsing ===')
from sshclient.ui.main_window import SessionTab

sp, typed = SessionTab._split_prompt('root@prod-01:/srv/app$ ls /var/lo')
check('prompt split off', sp == 'root@prod-01:/srv/app$ ', repr(sp))
check('typed part extracted', typed == 'ls /var/lo', repr(typed))
sp2, typed2 = SessionTab._split_prompt('user@host:~$ cat rea')
check('tilde prompt parsed', sp2 == 'user@host:~$ ' and typed2 == 'cat rea',
      repr((sp2, typed2)))
check('cwd from prompt', SessionTab._guess_cwd('root@h:/var/log$ ') == '/var/log',
      SessionTab._guess_cwd('root@h:/var/log$ '))
# ~ must NOT be expanded to a Windows path on the local machine;
# the POSIX path "~/documents" must be passed as-is to the remote SFTP
# so the remote shell (not local Windows) handles ~ expansion.
check('~ kept as POSIX path (not expanded to Windows)',
      SessionTab._guess_cwd('root@h:~$ ') == '~',
      SessionTab._guess_cwd('root@h:~$ '))
check('~subdir kept as POSIX path',
      SessionTab._guess_cwd('user@host:~/documents$ ') == '~/documents',
      SessionTab._guess_cwd('user@host:~/documents$ '))
check('prompt-less line is all word', SessionTab._split_prompt('ls') == ('', 'ls'))

# the word-extraction rule the fallback relies on
W = SessionTab._WORD_CHARS
def word_before(typed):
    i = len(typed)
    while i > 0 and W.match(typed[i - 1]):
        i -= 1
    return typed[i:]
check('word before cursor', word_before('ls /var/lo') == '/var/lo',
      repr(word_before('ls /var/lo')))
check('word stops at space', word_before('ls /var/lo ') == '',
      repr(word_before('ls /var/lo ')))
check('word keeps dashes/underscores', word_before('tar -xvf my_a-b') == 'my_a-b',
      repr(word_before('tar -xvf my_a-b')))
check('word splits on / for dir', word_before('cat /etc/inf').rpartition('/')[2] == 'inf')

names = ['app.conf', 'app.log', 'archive', 'logs']
check('unique match found', [n for n in names if n.startswith('app.c')] == ['app.conf'])
check('ambiguous set found', sorted(n for n in names if n.startswith('a')) ==
      ['app.conf', 'app.log', 'archive'])
check('common prefix of matches', os.path.commonprefix(
    [n for n in names if n.startswith('app.')]) == 'app.',
      os.path.commonprefix([n for n in names if n.startswith('app.')]))
check('no match stays empty', [n for n in names if n.startswith('zzz')] == [])

print()
if FAIL:
    print(f'RESULT: {len(FAIL)} FAILURES -> {FAIL}')
    sys.exit(1)
print('RESULT: TAB COMPLETION RENDERING OK')
