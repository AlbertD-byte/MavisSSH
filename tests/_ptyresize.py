"""终端尺寸变化要同步到远端 PTY。

README 一直写着「窗口尺寸变化会同步到远端 PTY（列数 × 行数）」，但
`TerminalWidget.set_resize_handler` 从写下来到今天**从来没有被调用过** ——
远端拿到的永远是 `open_shell` 里写死的 100x30。缩放窗口时 `less`、`vim`、
`top` 全都按错的宽度重排。

这个脚本量三件事：

  1. 真的缩放终端，通道上要收到对应尺寸的 `resize_pty`
  2. 节流是真的在限流（连着甩 40 次尺寸，只发得出很少几个 window-change），
     而不是逐次照发
  3. 通道断了之后不再去 resize（`resize_pty` 带 `@open_only`，此时抛异常；
     而 PyQt6 里槽抛异常是整个进程 fast-fail，不是只打一行日志）

跑法::

    $env:PYTHONIOENCODING="utf-8"; .\\.venv\\Scripts\\python.exe tests\\_ptyresize.py
"""
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.remote.connection import ConnectionInfo
from sshclient.ui import main_window

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


# ---------------------------------------------------------------------------
# 换字号之后，网格必须与新的字符高度同步
#
# **判据落在不变量上，不落在「看起来没裁」上。** 现象是「内容超过窗口后
# 底部被裁掉若干行，要多次回车才看得到」，根因是 `set_font()` 只调了
# `_update_metrics()`（重算 `_char_h`）却没重算 `rows` —— `rows` 是用**旧**
# `_char_h` 从控件高度算出来的，而绘制时每行占**当前** `_char_h`，最后一行
# 就落到控件下沿之外。
# 关键是 `resizeEvent` 救不了：终端在布局里，改字号时控件尺寸常常不变，
# 于是压根不触发 resize。
#
# 所以这里直接量那条等式：`oy + rows*char_h <= height`。
# ---------------------------------------------------------------------------
def _grid_fits(t) -> tuple[bool, str]:
    need = t._pad_y * 2 + t._rows * t._char_h
    return need <= t.height(), "需要高=%d 控件高=%d char_h=%d rows=%d" % (
        need, t.height(), t._char_h, t._rows)


def _check_grid_resync():
    print("\n=== 换字号后网格是否与字符高度同步 ===")
    from sshclient.core import fonts
    from sshclient.ui.terminal_widget import TerminalWidget

    t = TerminalWidget()
    t.resize(900, 500)
    t._recompute_grid()
    ok, detail = _grid_fits(t)
    check("初始网格与字符高度自洽", ok, detail)

    # 逐档放大字号。**每档都要成立**——只测最后一档会漏掉中间某档失步。
    for size in (11, 14, 18, 22, 28):
        t.set_font(fonts.mono_font(size, "Consolas"))
        ok, detail = _grid_fits(t)
        check("mono_font(%d) 之后网格仍自洽" % size, ok, detail)

    # 反向对照：模拟真正的「过期」情形。
    # **方向别搞反。** `rows` 是按当时的 `char_h` 从控件高度算的，字高**变小**
    # 时算出的行数**更多**；后来字高变大，这批过期的行就装不下了 ——
    # 这正是「底部被裁」的方向。代入更小的字高只会让要求更宽松，永远成立，
    # 那种元断言是恒真的（实测踩过）。
    t.set_font(fonts.mono_font(18, "Consolas"))
    old_h = 22                                   # 假装 rows 是用这个字高算的
    stale_rows = (t.height() - t._pad_y * 2) // old_h
    stale_need = t._pad_y * 2 + stale_rows * t._char_h
    check("元断言：用旧（小）字高算出的过期行，装不进当前字高（否则上面恒真）",
          stale_need > t.height(),
          "旧字高 %d 算出 %d 行，按当前字高 %d 需要高=%d 控件高=%d"
          % (old_h, stale_rows, t._char_h, stale_need, t.height()))

    # 宽度方向同理：换字体后列数也不能是旧的
    t.set_font(fonts.mono_font(11, "Consolas"))
    w_need = t._pad_x * 2 + t._cols * t._char_w
    check("列数同样与字宽同步", w_need <= t.width(),
          "需要宽=%d 控件宽=%d" % (w_need, t.width()))
    t.deleteLater()


def _dismiss():
    w = QtWidgets.QApplication.activeModalWidget()
    if w is not None:
        w.close()


_dismiss_timer = QtCore.QTimer()
_dismiss_timer.timeout.connect(_dismiss)
_dismiss_timer.start(50)


class _Ch:
    """假的 paramiko.Channel，只实现 SessionTab.attach 用到的那几个方法。"""

    def __init__(self):
        self.rx, self.tx = socket.socketpair()
        self.closed = False
        self.resizes = []        # (cols, rows)

    def fileno(self):
        return self.rx.fileno()

    def recv_ready(self):
        return True

    def recv(self, n):
        return self.rx.recv(n)

    def send(self, d):
        return len(d)

    def settimeout(self, t):
        pass

    def resize_pty(self, width=80, height=24, width_pixels=0, height_pixels=0):
        if self.closed:
            raise OSError("channel closed")
        self.resizes.append((width, height))

    def shutdown_write(self):
        pass

    def close(self):
        self.closed = True
        self._sent_reshutdown = True

    def __getattr__(self, name):
        # attach / apply_settings 还会摸 active / eof_received 等；给中性值，
        # 免得测试因为缺一个假属性就崩（真 PyQt6 里槽抛异常 = 进程 fast-fail）
        if name in ("active", "eof_received", "eof_sent", "exit_status"):
            return 0
        raise AttributeError(name)


class _Conn:
    def __init__(self):
        self.ch = _Ch()

    @property
    def info(self):
        return ConnectionInfo(host="10.0.0.9", port=22, username="deploy",
                             fingerprint="x")

    def open_shell(self, term=None, cols=80, rows=24):
        self.open_size = (cols, rows)
        return self.ch

    def open_sftp(self):
        raise OSError("no sftp in this test")

    def exec(self, cmd, timeout=None):
        return ("", "")

    def close(self):
        self.ch.close()

    def set_tunnel_callbacks(self, on_error=None, on_event=None):
        pass

    def add_tunnel(self, spec, enabled=True):
        return spec

    def set_tunnel_enabled(self, port, enabled=True):
        return enabled

    def remove_tunnel(self, port):
        return False

    def tunnel_specs(self):
        return []

    def tunnel_states(self):
        return {}

    def tunnel_connections(self):
        return {}


def spin(ms=120):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def build():
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    tab = main_window.SessionTab()
    lay.addWidget(tab)
    host.resize(1100, 700)
    host.show()
    conn = _Conn()
    tab.attach(conn)
    spin(200)
    return host, tab, conn


print("=== A. 真的把 resize_pty 发出去 ===")
host, tab, conn = build()
term = tab.terminal
conn.ch.resizes.clear()

for w, h in ((900, 600), (1200, 500), (700, 800)):
    term.resize(w, h)
    spin(200)

sent = conn.ch.resizes
check("缩放后通道收到了 resize_pty", len(sent) > 0, "收到 %d 次 %r" % (len(sent), sent[:4]))
check("尺寸是终端实际格子数而不是写死的 100x30",
      all(size != (100, 30) for size in sent) and bool(sent),
      "最后一次=%r" % ((sent[-1] if sent else None),))
if sent:
    cols, rows = sent[-1]
    grid_cols, grid_rows = term._cols, term._rows
    check("最后一次 resize 与当前格子数一致",
          (cols, rows) == (grid_cols, grid_rows),
          "发出 %r 实际 %r" % ((cols, rows), (grid_cols, grid_rows)))

print("\n=== B. 节流真的在限流 ===")
conn.ch.resizes.clear()
before = len(conn.ch.resizes)
for i in range(40):
    term.resize(600 + i * 7, 400 + i * 3)
    app.processEvents()          # 不 spin 等待，制造"连着甩"的场景
spin(400)
burst = len(conn.ch.resizes) - before
check("连甩 40 次尺寸没有逐次照发", burst < 40, "实际发出 %d 次" % burst)
check("节流后仍然发出了最新尺寸", burst >= 1, "%d 次" % burst)

print("\n=== C. 通道断了之后不再 resize ===")
tab._on_channel_closed()
spin(120)
conn.ch.resizes.clear()
term.resize(1300, 900)
spin(300)
check("断开后再缩放不会去碰已关闭的通道", len(conn.ch.resizes) == 0,
      "发出 %d 次" % len(conn.ch.resizes))
check("断开时 resize 回调已摘掉",
      tab.terminal._resize_callback is None,
      repr(tab.terminal._resize_callback))

print("\n=== D. 重连后不能沿用上一条会话的「已发送」记录 ===")
# 新通道的 PTY 开在 open_shell 的写死尺寸上。若 _sent_pty_size 被沿用，
# 尺寸恰好和上次一样的那一次就会被当成"没变"跳过，新会话的远端就停在
# 错误尺寸上——而这在"重连后不碰窗口"时才会发生。
conn2 = _Conn()
tab2 = main_window.SessionTab()
host.layout().addWidget(tab2)
tab2.attach(conn2)
spin(300)
# 不看 _sent_pty_size 本身：重连后立刻发一次纠正是**应该**发生的（PTY 开在
# 写死的 100x30，而终端真实尺寸不是它）。要守的是"这次纠正没有被抑制"，
# 也就是新会话的通道确实收到了 resize_pty。
check("重连后新通道确实收到 resize_pty（没有被上一条会话的记录抑制）",
      len(conn2.ch.resizes) >= 1,
      "发出 %d 次，最后=%r" % (len(conn2.ch.resizes),
                              conn2.ch.resizes[-1] if conn2.ch.resizes else None))
tab2.terminal.resize(900, 600)
spin(300)
check("重连后继续缩放仍然有效", len(conn2.ch.resizes) >= 2,
      "共 %d 次" % len(conn2.ch.resizes))
tab2._on_channel_closed()
spin(80)
tab2.close()

host.close()
host.deleteLater()
app.processEvents()

_check_grid_resync()

print()
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("通过 %d 项，失败 0 项" % len(PASS))
sys.exit(0)
