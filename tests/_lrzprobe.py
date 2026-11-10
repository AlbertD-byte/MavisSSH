"""远端 lrzsz 探测 + 缺 lrzsz 时的 SFTP 回退。

判据全部落在**生产路径真的做了什么**上：通道上写了哪些字节、SFTP 那边排出
的任务表长什么样。刻意不去手工塞 ``tab._lrzsz_probe`` 然后直接调
``_ensure_lrzsz()`` —— 那样只验了那个函数自己，调用点漏接一次照样全绿。

所以这里一律走 ``tab._on_upload()`` / ``tab._on_download()``。
"""
from __future__ import annotations

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
from PyQt6.QtWidgets import QMessageBox, QVBoxLayout, QWidget

from sshclient.core import lrzsz, settings
from sshclient.core.settings import SavedSession
from sshclient.remote.connection import ConnectionInfo
from sshclient.ui import main_window as MW
from sshclient.ui import msgbox

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail))


# --------------------------------------------------------------- 1 探测解析

print("\n[1] probe_remote 的输出解析")

BOTH = "MAVISZ sz /usr/bin/sz\nMAVISZ rz /usr/bin/rz\nMAVISZ done\n"
ONLY_SZ = "MAVISZ sz /usr/bin/sz\nMAVISZ done\n"
NONE = "MAVISZ done\n"
TRUNCATED = "MAVISZ sz /usr/bin/sz\n"          # 没有 done，超时被砍了


class ExecConn:
    """只提供 probe_remote 需要的那点表面。"""

    def __init__(self, out="", boom=None):
        self.out, self.boom, self.calls = out, boom, 0

    def exec(self, command, timeout=None):
        self.calls += 1
        if self.boom:
            raise self.boom
        return self.out, ""


p = lrzsz.probe_remote(ExecConn(BOTH))
check("两个都在：ok/sz/rz 全真，usable 为真",
      (p.ok, p.sz, p.rz, p.usable) == (True, True, True, True), repr(p))

p = lrzsz.probe_remote(ExecConn(ONLY_SZ))
check("只有 sz：rz 认成假，usable 为假",
      (p.ok, p.sz, p.rz, p.usable) == (True, True, False, False), repr(p))

p = lrzsz.probe_remote(ExecConn(NONE))
check("两个都没有：ok 仍为真（这次问出结果了），usable 为假",
      (p.ok, p.sz, p.rz, p.usable) == (True, False, False, False), repr(p))

p = lrzsz.probe_remote(ExecConn(TRUNCATED))
check("输出被截断（没有 done）→ 记成探测失败，不是'没装'",
      (p.ok, p.usable) == (False, True), repr(p))

p = lrzsz.probe_remote(ExecConn(boom=TimeoutError("timed out")))
check("exec 抛异常 → 探测失败且 usable 仍为真（不拦着用户）",
      (p.ok, p.usable) == (False, True), repr(p))

p = lrzsz.probe_remote(ExecConn("命令里有杂音\nMAVISZ done\n"))
check("输出里有别的行也不影响判定", (p.ok, p.sz) == (True, False), repr(p))

c1 = ExecConn(BOTH)
lrzsz.probe_remote(c1)
check("一次探测只发一次 exec（不为了 sz/rz 各问一次）", c1.calls == 1,
      "发了 %d 次" % c1.calls)

# --------------------------------------------------------------- 2 提示文案

print("\n[2] 提示内容")

box_texts = []


class FakeBtn:
    def __init__(self, text):
        self.text = text


class FakeBox:
    def __init__(self, pick="sftp"):
        self.pick = pick
        self.buttons = {}
        self.parts = []

    def setIcon(self, i):
        pass

    def setText(self, t):
        self.parts.append(t)

    def setInformativeText(self, t):
        self.parts.append(t)

    def setDetailedText(self, t):
        self.parts.append(t)

    def addButton(self, text, role=None):
        b = FakeBtn(text)
        self.buttons[text] = b
        return b

    def setDefaultButton(self, b):
        self.default = b

    def exec(self):
        want = "改用 SFTP 传输" if self.pick == "sftp" else "先不传，我去装 lrzsz"
        # 这个假框也接 msgbox.warning 之类的普通弹窗，那些框上没有这两个按钮。
        # 没有就当作"没点中任何自定义按钮"，别在这里炸——炸了会掩盖真正的判据。
        self._clicked = self.buttons.get(want)
        return 0

    def clickedButton(self):
        return self._clicked

    @property
    def body(self):
        return "\n".join(self.parts)


# --------------------------------------------------------------- 脚手架

class FakeChannel:
    def __init__(self):
        self.rx, self.tx = socket.socketpair()
        self.rx.settimeout(0.1)
        self.closed = False
        self.sent = bytearray()

    def fileno(self):
        return self.rx.fileno()

    def recv(self, n):
        try:
            return self.rx.recv(n)
        except Exception:
            return b""

    def sendall(self, d):
        self.sent.extend(d)          # ← 判据就量这里

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True


class FakeSFTP:
    def listdir_attr(self, path="."):
        return []

    def stat(self, p):
        # **必须抛 `FileNotFoundError`，不是裸 `OSError`。**
        # v3 规范要求「不存在」用 status 2 / ENOENT 表达，paramiko 的
        # `_convert_status` 把它映射成 `IOError(errno.ENOENT, ...)`，
        # 在 Python 3 上就是 `FileNotFoundError`。
        # 而 `RemoteFileSystem._remote_exists` 只把 `FileNotFoundError`
        # 当成「确实没有」—— 其它异常一律算「问不到」，宁可跳过也不能
        # 替用户决定覆盖。所以这个替身要是抛裸 `OSError`，产品会正确地
        # 判定「问不到」，然后**所有上传都不发生**：那是替身不称职，
        # 不是产品坏。
        raise FileNotFoundError(2, "No such file", p)

    def getcwd(self):
        return "/var/log"

    def listdir(self, path=""):
        return []

    def open(self, *a, **k):
        raise OSError("测试不上传")

    def isdir(self, p):
        return False


class FakeConn:
    """exec 走 canned 输出（探测/pwd 用），sendall 记进 channel。"""

    def __init__(self, probe_out, pwd="/srv/app", boom=None):
        self._info = ConnectionInfo(host="10.0.0.5", port=22, username="deploy")
        self.ch = FakeChannel()
        self.sftp = FakeSFTP()
        self.probe_out = probe_out
        self.pwd = pwd
        self.boom = boom
        self.exec_calls = []

    @property
    def info(self):
        return self._info

    def open_shell(self, term=None, cols=80, rows=24):
        return self.ch

    def open_sftp(self):
        return self.sftp

    # attach() 里会建隧道面板和监控面板，它们要这两个方法。给个能用的实现，
    # 免得测试一开场就弹一个「端口转发不可用」的框把真正的判据盖掉。
    def add_tunnel(self, spec, enabled=True):
        pass

    def remove_tunnel(self, spec):
        pass

    def exec(self, command, timeout=None):
        self.exec_calls.append(command)
        if self.boom:
            raise self.boom
        if "MAVISZ" in command:
            return self.probe_out, ""
        if command.strip() == "pwd":
            return self.pwd + "\n", ""
        return "", ""

    def close(self):
        self.ch.close()


def spin(ms):
    end = [False]
    QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


tmp = pathlib.Path(tempfile.mkdtemp())
STORE = settings.SessionStore(tmp / "sessions.json")
st = settings.AppSettings()
st._path = STORE.path.parent / 'settings.json'
st.theme = "dark"
win = MW.MainWindow(st, STORE)
host = QWidget()
lay = QVBoxLayout(host)
lay.setContentsMargins(0, 0, 0, 0)
lay.addWidget(win)
host.resize(1200, 800)
host.show()
spin(200)

for m in ("critical", "warning", "information", "question"):
    setattr(QMessageBox, m, staticmethod(
        lambda p, t, x, *a, **k: QMessageBox.StandardButton.Ok))
QMessageBox.exec = lambda self: QMessageBox.StandardButton.Ok

_real_build = msgbox._build
_real_open_files = msgbox.open_files
_real_ask_dir = msgbox.ask_dir

STATE = {"pick": "sftp", "files": [str(tmp / "a.txt"), str(tmp / "b.txt")]}
SFTP_JOBS = []


def spy_build(*a, **k):
    """替掉真框，但**把文案原样接进去**。

    早先这个函数直接 new 一个空框，提示文案一条都没进去，于是
    "提示里给了安装命令" 那条判据读到的 body 是空的——判据自己断了线，
    量出来的"没有安装命令"是假的。
    """
    box = FakeBox(STATE["pick"])
    for text in a[3:4]:                      # _build(parent, icon, title, text, …)
        if text:
            box.setText(text)
    box.title = a[2] if len(a) > 2 else ""
    for key in ("informative", "detailed"):
        if k.get(key):
            box.setInformativeText(k[key])
    box_texts.append(box)
    return box


def spy_open_files(*a, **k):
    return list(STATE["files"]), ""


# **下载方向的对称件，以前是漏的。** `_on_download`
# （`main_window.py:1199`）在确认远端路径存在之后会**无条件**弹
# 「选择下载保存目录」的模态框；判据进程里没有人能点它，`exec()` 一直等，
# 整个脚本挂到超时，报告只写一句 TIMEOUT。
# 上面 `open_files` 早就替掉了、唯独这条漏了，于是同一份文件里上传能跑、
# 下载会挂，而挂不挂取决于前面 `check_remote_paths` 走没走到那条早退 ——
# 表现成间歇性挂死，比必现更难查。
ASK_DIR_CALLS = {"n": 0}


def spy_ask_dir(*a, **k):
    """返回测试自己的临时目录 —— 必须是一个**真实存在**的目录，
    因为替身放行之后下载会一路走到 SFTP 回退，那边要往它底下拼路径。"""
    ASK_DIR_CALLS["n"] += 1
    return str(tmp)


class FakeTerminal:
    """**只桩 input_line()**，其余方法都转发给真终端。

    一开始是整个替换掉 `tab.terminal`，结果 `set_input_blocked` 之类的方法
    立刻 AttributeError —— 终端还有一大堆别的地方在用。判据只需要提示符那一
    行，没必要把整个部件换掉。
    """

    def __init__(self, real, prompt):
        self._real = real
        self.prompt = prompt

    def input_line(self):
        return self.prompt, len(self.prompt)

    def __getattr__(self, name):
        return getattr(self._real, name)


def set_prompt(tab, prompt):
    tab.terminal = FakeTerminal(tab.terminal, prompt)


def attach(probe_out, pwd="/srv/app", prompt=None):
    """造一个真的 SessionTab，接上假连接，并记下 SFTP 排出来的任务表。

    走 ``MainWindow._on_connect_ok`` 这条真实建标签的路，而不是自己 new 一个
    ``SessionTab`` —— 后者会漏掉 attach() 里那一堆初始化。
    """
    before = {id(win.tabs.widget(i)) for i in range(win.tabs.count())}
    conn = FakeConn(probe_out, pwd)
    saved = SavedSession(name="t", host="10.0.0.5", port=22, username="deploy",
                         auth_method="password", password="x")
    win._on_connect_ok(conn, saved)
    spin(250)
    fresh = [win.tabs.widget(i) for i in range(win.tabs.count())
             if id(win.tabs.widget(i)) not in before]
    if not fresh:
        return None, conn
    tab = fresh[0]
    if prompt is not None:
        set_prompt(tab, prompt)
    sftp = tab._sftp
    sftp._start_transfers = (lambda jobs, direction, what, rp, rd, skipped:
                             SFTP_JOBS.append((direction, list(jobs), rd)))
    return tab, conn


msgbox._build = spy_build
msgbox.open_files = spy_open_files
msgbox.ask_dir = spy_ask_dir

# --------------------------------------------------------------- 3 生产路径

print("\n[3] 走真实入口：远端有 lrzsz")

tab, _ = attach(BOTH)
if tab is None:
    print("  没能建出 SessionTab，生产路径几组没法评")
    raise SystemExit(1)
set_prompt(tab, "deploy@localhost:/srv/app$ ")
tab._reset_zmodem_state()
ch = tab._channel
n0 = len(ch.sent)
SFTP_JOBS.clear()
box_texts.clear()
tab._on_upload()
spin(120)
check("有 lrzsz 时不会弹「远端没有 lrzsz」的框", not box_texts,
      "弹了 %d 次" % len(box_texts))
check("有 lrzsz 时真的往通道写了 rz", bytes(ch.sent[n0:]).startswith(b"rz"),
      repr(bytes(ch.sent[n0:])))
check("有 lrzsz 时不走 SFTP", not SFTP_JOBS, str(len(SFTP_JOBS)))

print("\n[4] 走真实入口：远端没装 lrzsz")

# 让**探测真的重新跑一遍**得出"没装"，而不是手工塞 tab._lrzsz_probe ——
# 后者只验了拿到结论之后的那半段，从探测到提示这一整条链就没被评到。
tab._conn.probe_out = NONE
tab._reset_zmodem_state()
ch = tab._channel
n0 = len(ch.sent)
SFTP_JOBS.clear()
box_texts.clear()
STATE["pick"] = "sftp"
tab._on_upload()
spin(200)
check("没装时弹了框（提示用户安装）", len(box_texts) == 1, "弹了 %d 次" % len(box_texts))
if box_texts:
    body = box_texts[0].body
    check("提示里给出了安装命令", "lrzsz" in body and "apt-get" in body
          and "yum" in body, repr(body[:90]))
    check("提示里说清了能改用 SFTP",
          "改用 SFTP" in "\n".join(box_texts[0].buttons),
          str(list(box_texts[0].buttons)))
    check("提示里也说清了可以先不传（去装 lrzsz）",
          "先不传" in "\n".join(box_texts[0].buttons),
          str(list(box_texts[0].buttons)))
check("**没往通道写 rz**（否则远端回一句 command not found）",
      bytes(ch.sent[n0:]) == b"", repr(bytes(ch.sent[n0:])))
check("改用 SFTP 时排出了上传任务", len(SFTP_JOBS) == 1, str(SFTP_JOBS))
if SFTP_JOBS:
    direction, jobs, target = SFTP_JOBS[0]
    check("回退任务是 upload", direction == "upload", direction)
    check("**落到 shell 的 CWD**（和 ZMODEM 语义对齐）", target == "/srv/app",
          target)
    check("两个文件都在任务表里", len(jobs) == 2, str(jobs))
    check("目标是 CWD + basename，不是把整条本地路径拼过去",
          all(j[2] == "/srv/app/" + os.path.basename(j[1]) for j in jobs),
          str([j[2] for j in jobs]))

print("\n[5] 走真实入口：没装 + 用户选择先不传")

tab._conn.probe_out = NONE
tab._reset_zmodem_state()
ch = tab._channel
n0 = len(ch.sent)
SFTP_JOBS.clear()
box_texts.clear()
STATE["pick"] = "cancel"
tab._on_upload()
spin(120)
check("用户选「先不传」时同样一个字都不往通道写",
      bytes(ch.sent[n0:]) == b"", repr(bytes(ch.sent[n0:])))
check("用户选「先不传」时不偷偷替他走 SFTP", not SFTP_JOBS, str(SFTP_JOBS))

print("\n[6] 下载方向")

tab._conn.probe_out = NONE
tab._reset_zmodem_state()
ch = tab._channel
n0 = len(ch.sent)
SFTP_JOBS.clear()
box_texts.clear()
STATE["pick"] = "sftp"
tab.sz_target_edit.setText("/srv/app/data/a.log \"/srv/app/my file.txt\"")
ASK_DIR_CALLS["n"] = 0
tab._on_download()
spin(200)
check("元断言：下载的目录框替身确实被调到了", ASK_DIR_CALLS["n"] >= 1,
      "被调 %d 次 —— 挂错地方的话下面全是在替一条没走过的路径背书"
      % ASK_DIR_CALLS["n"])
check("下载方向没装时也不往通道写 sz", bytes(ch.sent[n0:]) == b"",
      repr(bytes(ch.sent[n0:])))
check("下载方向也走 SFTP", len(SFTP_JOBS) == 1, str(SFTP_JOBS))
if SFTP_JOBS:
    direction, jobs, target = SFTP_JOBS[0]
    check("回退任务是 download", direction == "download", direction)
    check("**带空格的路径按 basename 落盘**，没有拼出一串子目录",
          [j[2] for j in jobs] == [os.path.join(target, "a.log"),
                                   os.path.join(target, "my file.txt")],
          str([j[2] for j in jobs]))

print("\n[7] 缓存与失效")

# 这一组是本轮真机验出来的那个 bug 的钉子。
set_prompt(tab, "deploy@localhost:/opt/data/logs$ ")
tab._conn.probe_out = NONE
tab._reset_zmodem_state()
SFTP_JOBS.clear()
STATE["pick"] = "sftp"
tab._on_upload()
spin(150)
check("**落点跟着交互 shell 的提示符走**（不跟着登录 shell 的家目录走）",
      bool(SFTP_JOBS) and SFTP_JOBS[0][2] == "/opt/data/logs",
      str(SFTP_JOBS[0][2] if SFTP_JOBS else None))
set_prompt(tab, "deploy@localhost:/srv/app$ ")
tab._conn.probe_out = NONE
tab._reset_zmodem_state()
SFTP_JOBS.clear()
tab._on_upload()
spin(150)
check("提示符里的 CWD 变了，落点跟着变（不是缓存住第一次的）",
      bool(SFTP_JOBS) and SFTP_JOBS[0][2] == "/srv/app",
      str(SFTP_JOBS[0][2] if SFTP_JOBS else None))
tab._reset_zmodem_state()
conn = FakeConn(NONE)
tab._conn = conn
tab._ensure_lrzsz("upload")
tab._ensure_lrzsz("upload")
tab._ensure_lrzsz("download")
probes = [c for c in conn.exec_calls if "MAVISZ" in c]
check("连点多次只探测一次（缓存住了）", len(probes) == 1, "探测了 %d 次" % len(probes))
tab._reset_zmodem_state()
tab._conn = conn
tab._lrzsz_probe.usable if tab._lrzsz_probe else None
tab._ensure_lrzsz("upload")
probes2 = [c for c in conn.exec_calls if "MAVISZ" in c]
check("重连（_reset_zmodem_state）后重新探测，不会沿用旧结论",
      len(probes2) == 2, "探测了 %d 次" % len(probes2))

print("\n[8] 探测失败不拦人")

tab._reset_zmodem_state()
tab._conn = FakeConn("", boom=TimeoutError("boom"))
box_texts.clear()
ch = tab._channel
n0 = len(ch.sent)
action = tab._ensure_lrzsz("upload")
check("探测问不出结果时按可用处理（不弹框）",
      action == "zmodem" and not box_texts, "action=%s" % action)
STATE["pick"] = "sftp"
tab._on_upload()
spin(120)
check("探测失败时仍然照旧走 ZMODEM（写 rz）",
      bytes(ch.sent[n0:]).startswith(b"rz"), repr(bytes(ch.sent[n0:])))

# --------------------------------------------------------------- 9 元断言

print("\n[9] 元断言：把缺陷装回去，判据必须变红")

# 9a 要抓的是「检查漏接了」这个真实缺陷——有人把 _ensure_lrzsz 那两行删掉，
# 或者接线接错了地方。把它变成永远说"可以用"，就等价于没这个检查。
_orig_ensure = MW.SessionTab._ensure_lrzsz
MW.SessionTab._ensure_lrzsz = lambda self, direction: "zmodem"
tab._conn.probe_out = NONE
tab._reset_zmodem_state()
ch = tab._channel
n0 = len(ch.sent)
box_texts.clear()
STATE["pick"] = "sftp"
tab._on_upload()
spin(150)
mut_bytes = bytes(ch.sent[n0:])
MW.SessionTab._ensure_lrzsz = _orig_ensure
check("元断言：把检查摘掉之后真的会往通道写 rz"
      "（所以上面'没写 rz'不是恒真）",
      mut_bytes.startswith(b"rz"), repr(mut_bytes))

# 9b 上面「截断输出不算数」靠的是 MAVISZ done 这个收尾标记。同样的内容，
# 只差这一个标记，结论就翻过来——所以那条判据量的是标记，不是碰巧。
r2 = lrzsz.probe_remote(ExecConn(TRUNCATED + "MAVISZ done\n"))
check("元断言：同样的内容补上 done 标记就被采信了（所以上面靠的是这个标记）",
      (r2.ok, r2.sz) == (True, True), repr(r2))

# 9c 「探测失败不拦人」也不能是恒真：塞一个"确实没装且探测成功"的结论，
# 同一条判据必须给出相反的答案。
_ok = MW.SessionTab._ensure_lrzsz
tab._reset_zmodem_state()
tab._lrzsz_probe = lrzsz.RemoteProbe(sz=False, rz=False, ok=True)
box_texts.clear()
STATE["pick"] = "cancel"
mut_action = tab._ensure_lrzsz("upload")
tab._lrzsz_probe = lrzsz.RemoteProbe(ok=False, error="boom")
box_texts.clear()
mut_action2 = tab._ensure_lrzsz("upload")
check("元断言：同一个判据在'确实没装'和'探测失败'下给出不同答案",
      mut_action == "cancel" and mut_action2 == "zmodem",
      "%s / %s" % (mut_action, mut_action2))

# --------------------------------------------------------------- 收尾

def _isdeleted(obj):
    try:
        return bool(sip.isdeleted(obj))
    except RuntimeError:
        return True


for i in range(win.tabs.count()):
    t = win.tabs.widget(i)
    if not isinstance(t, MW.SessionTab):      # 欢迎页不是 SessionTab
        continue
    t._reset_zmodem_state()
    if t._channel is not None:
        t._channel.close()
win.close()
host.close()
win.deleteLater()
host.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
check("收尾之后 MainWindow 真的被销毁了（不是只 close）", _isdeleted(win))
check("收尾之后 host 真的被销毁了", _isdeleted(host))

msgbox._build = _real_build
msgbox.open_files = _real_open_files
msgbox.ask_dir = _real_ask_dir

print()
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL " + f)
sys.exit(1 if FAIL else 0)
