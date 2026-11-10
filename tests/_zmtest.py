"""Does ZmodemPump forward the local process's output while it is still running?

A BufferedReader.read(n) blocks until n bytes arrive or EOF. ZMODEM packets are
small, so a pump that uses read() never transmits during a session -- the bytes
only surface after the process exits, by which time the remote has timed out.
"""
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 替身脚本是 tests/ 的同级文件，不是项目根的。
FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_fake_rzsz.py')


class FakeChannel:
    def __init__(self):
        self.rx, self.tx = socket.socketpair()
        self.tx.settimeout(0.2)
        self.closed = False
    def fileno(self): return self.rx.fileno()
    def sendall(self, data): self.tx.sendall(data)
    def settimeout(self, t): pass
    def close(self):
        self.closed = True
        try: self.rx.close(); self.tx.close()
        except Exception: pass


from sshclient.core.lrzsz import ZmodemPump

ch = FakeChannel()
got = bytearray()

def reader():
    # read the peer end: the pump writes with ch.sendall() -> ch.tx
    ch.rx.settimeout(0.2)
    while not ch.closed:
        try:
            d = ch.rx.recv(4096)
        except Exception:
            continue
        if not d:
            break
        got.extend(d)

threading.Thread(target=reader, daemon=True).start()

logs = []
pump = ZmodemPump(channel=ch, direction='upload', binary=sys.executable,
                  files=[], cwd=os.path.dirname(FAKE),
                  on_log=lambda s: logs.append(s))

import subprocess


def start_fake():
    pump._proc = subprocess.Popen([sys.executable, FAKE, 'reply'],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, cwd=os.path.dirname(FAKE))
    print('  spawned stand-in, pid', pump._proc.pid)
    for name in ('_pump_proc_to_channel', '_pump_stderr', '_wait_proc'):
        th = threading.Thread(target=getattr(pump, name), daemon=True)
        th.start()
        print(f'  started {name}')

pump.start = start_fake
pump.start()
time.sleep(0.3)
# route the remote's bytes in the way the app does: the QSocketNotifier is the
# single reader of the channel and calls feed()
pump.feed(b'\x18')
print('  fed 1 byte to the stand-in')

first = None
t0 = time.time()
while time.time() - t0 < 6:
    if got:
        first = time.time() - t0
        break
    time.sleep(0.05)

alive = pump._proc.poll() is None
print()
print(f'local binary still running when output appeared: {alive}')
print('first output after: ' + (f'{first:.2f}s' if first is not None else 'NEVER within 6s'))
print(f'bytes forwarded: {len(got)}  {bytes(got[:32])!r}')
print('pump log:', logs[:5])
pump.stop()

if first is None:
    print('RESULT: FAIL -- pump does not forward output until the process exits')
    sys.exit(1)
print('RESULT: PASS -- output flows while the process is alive')
