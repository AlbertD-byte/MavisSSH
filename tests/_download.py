"""下载流程：先校验远程路径 → 选保存目录 → 才真的发 `sz`。

用户要的顺序（工具栏的目录按钮已去掉）：

    点下载
      ├─ 输入框为空      -> 提示「请输入远程下载路径」
      ├─ 远端路径不存在  -> 提示「远程文件不存在」+ 让他检查路径
      ├─ 探测问不出结果  -> **放行**（不能把「问不到」说成「不存在」）
      └─ 存在            -> 弹「选择下载保存目录」-> 取消即止步 / 选定才发 sz

**「探测失败要放行」是最容易写反的一条**：把一次问不出来的结果说成「文件
不存在」，用户看到的提示是「请检查路径」，他只会去改路径、不会重试 ——
一次网络抖动就变成一次劝退。
"""
from __future__ import annotations

import os
import pathlib
import shlex
import socket
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from PyQt6 import QtWidgets  # noqa: E402
from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QMessageBox, QVBoxLayout, QWidget  # noqa: E402

from sshclient.core import (
    lrzsz,  # noqa: E402
    settings,  # noqa: E402
)
from sshclient.core.settings import SavedSession  # noqa: E402
from sshclient.remote.connection import ConnectionInfo  # noqa: E402
from sshclient.ui import main_window as MW  # noqa: E402
from sshclient.ui import msgbox  # noqa: E402

app = QWidget.__init__ and QtWidgets.QApplication(sys.argv)
FAIL, OK = [], 0


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


# ============================================================ 纯函数层
print("\n=== remote_words：每个路径恰好一个 word，路径数据不带任何语法 ===")
# **这一组判据原来钉的是「`~` 路径不加引号」那条有缺陷的规则。**
# 那条规则让 `~` 路径里的 `;` `|` `>` 被远端 bash 二次解释（注入）。
# 现在 `~` 走远端 `${HOME}` 插值展开，路径整体始终是一个安全的 word。
check("绝对路径原样", lrzsz.remote_words(["/var/log/a.log"]) == "/var/log/a.log",
      lrzsz.remote_words(["/var/log/a.log"]))
check("带空格的路径补上引号（否则远端收到 2 个参数）",
      lrzsz.remote_words(["my file.txt"]) == "'my file.txt'",
      lrzsz.remote_words(["my file.txt"]))
check("~ 路径用远端 ${HOME} 插值（不是原样裸奔，也不是被引号抑制）",
      lrzsz.remote_words(["~/docs/t.md"]) == '"${HOME}"/docs/t.md',
      lrzsz.remote_words(["~/docs/t.md"]))
check("混合：~ 用插值、空格路径加引号",
      lrzsz.remote_words(["~/a.txt", "my b.txt"]) == '"${HOME}"/a.txt \'my b.txt\'',
      lrzsz.remote_words(["~/a.txt", "my b.txt"]))
check("~ 单独一个时也是 ${HOME}",
      lrzsz.remote_words(["~"]) == '"${HOME}"',
      lrzsz.remote_words(["~"]))
# 元断言：**老实现必须红**（否则上面几条恒真，判据没有区分度）
import shlex as _shlex  # noqa: E402


def _old_join(paths):
    """老实现（已修掉的那条规则），用来证明判据确实能区分。"""
    return " ".join(p if p.startswith("~") else _shlex.quote(p) for p in paths)


check("元断言：老实现对 ~ 路径不加引号（所以上面几条有区分度）",
      _old_join(["~/docs/t.md"]) == "~/docs/t.md",
      _old_join(["~/docs/t.md"]))
check("元断言：老实现与新实现对 ~ 路径产出不同",
      _old_join(["~/docs/t.md"]) != lrzsz.remote_words(["~/docs/t.md"]),
      "old=%r new=%r" % (_old_join(["~/docs/t.md"]),
                          lrzsz.remote_words(["~/docs/t.md"])))

print("\n=== check_remote_paths ===")


class ExecConn:
    def __init__(self, out="", boom=None):
        self.out, self.boom, self.calls = out, boom, []

    def exec(self, command, timeout=None):
        self.calls.append(command)
        if self.boom:
            raise self.boom
        return self.out, ""


c = ExecConn("MAVISP 1 0\nMAVISP 0 1\nMAVISP done\n")
r = lrzsz.check_remote_paths(c, ["/a/x", "/a/y"])
check("存在的判 True、不存在的判 False",
      r == {"/a/x": True, "/a/y": False}, str(r))
check("命令里两个路径都问到了",
      "/a/x" in c.calls[0] and "/a/y" in c.calls[0], c.calls[0])

c2 = ExecConn("MAVISP 1 0\n")                # 没有 done = 输出被截断
check("输出被截断（没有 done）-> None，交给调用方放行",
      lrzsz.check_remote_paths(c2, ["/a/x"]) is None,
      str(lrzsz.check_remote_paths(c2, ["/a/x"])))

c3 = ExecConn(boom=TimeoutError("链路超时"))
check("exec 抛异常 -> None，不是「都不存在」",
      lrzsz.check_remote_paths(c3, ["/a/x"]) is None,
      str(lrzsz.check_remote_paths(c3, ["/a/x"])))

c4 = ExecConn("MAVISP 1 0\nMAVISP done\n")
r4 = lrzsz.check_remote_paths(c4, ["/root/my file.txt"])
check("带空格的路径能对上（序号协议天然不受空格影响）",
      r4 == {"/root/my file.txt": True}, str(r4))

# `~` 是这一整段里最关键的一条：远端会**先展开**再判存在，回显的永远是
# `/home/...` 而不是用户写的 `~/...`。所以探测协议必须传序号而不是回传路径 ——
# 回传路径时 `~` 那一项永远对不上，`.get(t, False)` 会把**每一个 ~ 路径**
# 都误报成「远程文件不存在」。
c5 = ExecConn("MAVISP 1 0\nMAVISP done\n")
r5 = lrzsz.check_remote_paths(c5, ["~/docs/t.md"])
check("`~` 路径靠序号能正确对上（不会因远端展开成 /home/... 而失配）",
      r5 == {"~/docs/t.md": True}, str(r5))
check("`~` 路径用远端 ${HOME} 插值展开（不是被单引号包住而失效）",
      '"${HOME}"/docs/t.md' in c5.calls[0] and " '~/docs/t.md'" not in c5.calls[0],
      c5.calls[0])
# 元断言：证明上面那条不是恒绿。同样的远端输出交给**回传路径的老协议**去解析，
# `~` 那一项必然查不到 —— 而调用方会把查不到当「不存在」，也就是老实现会把
# 每一个 ~ 路径都拦下来。真跑一遍老解析，而不是只嘴上说说。
_old_way = {}
for _line in "MAVISP 1 0\nMAVISP done\n".splitlines():
    if _line.strip() == "MAVISP done":     # 与真解析器一样，先短路 done
        break
    if _line.startswith("MAVISP "):
        _flag, _, _rest = _line[len("MAVISP") + 1:].partition(" ")
        _old_way[_rest] = _flag == "1"
check("元断言：老协议确实对不上（键是序号 '0'，不是 ~/docs/t.md）",
      _old_way == {"0": True} and _old_way.get("~/docs/t.md", False) is False,
      str(_old_way))
check("空路径列表直接返回 {}（不白跑一次 exec）",
      lrzsz.check_remote_paths(c4, []) == {})

# ============================================================ 界面流程
print("\n=== 界面流程 ===")


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
        except OSError:
            return b""

    def sendall(self, d):
        self.sent.extend(d)

    def settimeout(self, t):
        pass

    def resize_pty(self, *a, **k):
        pass

    def close(self):
        self.closed = True


class FakeSFTP:
    def listdir_attr(self, path="."):
        return []

    def stat(self, p):
        raise FileNotFoundError(2, "No such file", p)

    def getcwd(self):
        return "/srv/app"

    def listdir(self, path=""):
        return []

    def open(self, *a, **k):
        raise OSError("不上传")

    def isdir(self, p):
        return False


PROBE_OK = ("MAVISZ sz /usr/bin/sz\nMAVISZ rz /usr/bin/rz\nMAVISZ done\n")


def _asked_paths(command):
    """从 `for p in <paths>; do` 里把被问到的路径原样取回来。

    **注意 `${HOME}` 的还原。** 新实现里 `~` 开头���路径是
    `"${HOME}"/rest` 这种**变量插值**形式（`~` 由远端自己展开），
    远端 bash 看到的字面量是 `${HOME}/rest`，不是 `~/rest`。

    这里要把 `${HOME}` 还原成 `~` 再交给 `self.exists` 比对 ——
    否则桩拿 `${HOME}/docs/t.md` 去比 `~/docs/t.md`，永远对不上，
    每一条都会被判成「远端文件不存在」。
    """
    head, sep, _tail = command.partition("; do")
    assert sep, "探测命令结构变了：%r" % command
    listed = head.split("for p in ", 1)[-1]
    out = []
    for w in shlex.split(listed):
        if w == "${HOME}":
            out.append("~")
        elif w.startswith("${HOME}/"):
            out.append("~/" + w[len("${HOME}/"):])
        else:
            out.append(w)
    return out


class FakeConn:
    def __init__(self, exists, boom=None, silent=()):
        self._info = ConnectionInfo(host="h", port=22, username="u")
        self.ch, self.sftp = FakeChannel(), FakeSFTP()
        self.exists, self.boom, self.exec_calls = exists, boom, []
        # silent 里的路径**一个字都不回**，但结尾照打 done。模拟「远端答完了，
        # 唯独这几条的行我们这边没解析出来」——问不到，不等于不存在。
        self.silent = set(silent)

    @property
    def info(self):
        return self._info

    def open_shell(self, term=None, cols=80, rows=24):
        return self.ch

    def open_sftp(self):
        return self.sftp

    def add_tunnel(self, spec, enabled=True):
        pass

    def remove_tunnel(self, spec):
        pass

    def set_tunnel_callbacks(self, *a, **k):
        pass

    def tunnel_specs(self):
        return []

    def tunnel_states(self):
        return {}

    def tunnel_connections(self):
        return {}

    def exec(self, command, timeout=None):
        self.exec_calls.append(command)
        if self.boom:
            raise self.boom
        if "MAVISZ" in command:
            return PROBE_OK, ""
        if "MAVISP" in command:
            # 回的是**序号**，而且必须对命令里问到的每一条都回 —— 真实远端
            # 就是一个 `for` 循环，少回一条就不是同一个语义了。
            asked = _asked_paths(command)
            return "".join("MAVISP %d %d\n" % (1 if p in self.exists else 0, i)
                           for i, p in enumerate(asked) if p not in self.silent
                           ) + "MAVISP done\n", ""
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
st._path = tmp / "settings.json"
st.theme = "dark"
win = MW.MainWindow(st, STORE)
host = QWidget()
lay = QVBoxLayout(host)
lay.setContentsMargins(0, 0, 0, 0)
lay.addWidget(win)
host.resize(1200, 800)
host.show()
spin(200)

# 弹框全部打桩，把「弹了什么、文案是什么」记下来
WARNINGS, DIRS = [], []


def fake_warning(parent, title, text, *a, **k):
    WARNINGS.append((title, text))
    return QMessageBox.StandardButton.Ok


def fake_ask_dir(parent, title, directory="", *a, **k):
    DIRS.append((title, directory))
    return STATE["pick_dir"]


STATE = {"pick_dir": None}
msgbox.warning = fake_warning
msgbox.ask_dir = fake_ask_dir


def attach(exists, boom=None, silent=()):
    before = {id(win.tabs.widget(i)) for i in range(win.tabs.count())}
    conn = FakeConn(exists, boom, silent)
    win._on_connect_ok(conn, SavedSession(
        name="t", host="h", port=22, username="u",
        auth_method="password", password="x"))
    spin(250)
    fresh = [win.tabs.widget(i) for i in range(win.tabs.count())
             if id(win.tabs.widget(i)) not in before]
    return (fresh[0] if fresh else None), conn


def run_case(tag, text, exists, pick_dir, boom=None, silent=()):
    WARNINGS.clear()
    DIRS.clear()
    STATE["pick_dir"] = pick_dir
    global LAST_TAB
    LAST_TAB = None
    tab, conn = attach(exists, boom, silent)
    if tab is None:
        check("%s：标签建起来了" % tag, False)
        return
    LAST_TAB = tab
    tab.sz_target_edit.setText(text)
    tab._on_download()
    spin(30)
    sent = conn.ch.sent.decode("utf-8", "replace")
    print("  [%s] 警告=%s 目录框=%d 发出=%r"
          % (tag, [w[0] for w in WARNINGS], len(DIRS), sent[:60]))
    return WARNINGS[:], DIRS[:], sent


# 每次 run_case 建的标签，供断言落盘目录之类的会话内状态用
LAST_TAB = None


print("\n--- ① 输入框为空 ---")
w, d, sent = run_case("空输入", "", {"/a.txt": True}, str(tmp))
check("弹了「请输入远程下载路径」",
      any("请输入远程下载路径" in x[0] for x in w), str(w))
check("没有弹目录框（还没到那一步）", not d, str(d))
check("没有往通道写任何东西", sent == "", repr(sent))

print("\n--- ② 远端路径不存在 ---")
w, d, sent = run_case("不存在", "/a/nope.txt", {"/a.txt": True}, str(tmp))
check("弹了「远程文件不存在」",
      any("远程文件不存在" in x[0] for x in w), str(w))
check("提示里点名了那条路径（用户才知道改哪个）",
      any("/a/nope.txt" in x[1] for x in w), str(w))
check("**没有**弹目录框（路径不对就不该让用户白挑目录）", not d, str(d))
check("没有往通道写任何东西", sent == "", repr(sent))

print("\n--- ③ 远端存在 + 选了目录 ---")
w, d, sent = run_case("存在", "/a/yes.txt", {"/a/yes.txt": True}, str(tmp))
check("弹了「选择下载保存目录」",
      any("保存目录" in x[0] for x in d), str(d))
check("没有报路径错", not w, str(w))
check("往通道写了 sz（带换行）", sent.startswith("sz /a/yes.txt\n"), repr(sent))
check("选了目录后记住了它",
      LAST_TAB is not None and getattr(LAST_TAB, "_dl_target_dir", None) == str(tmp),
      repr(getattr(LAST_TAB, "_dl_target_dir", "<无标签>")))

print("\n--- ④ 用户取消目录框 ---")
w, d, sent = run_case("取消", "/a/yes.txt", {"/a/yes.txt": True}, None)
check("弹了目录框", bool(d), str(d))
check("取消后**没有**往通道写任何东西", sent == "", repr(sent))
check("取消后没有把空值写进 _dl_target_dir（下次还能沿用旧目录）",
      getattr(LAST_TAB, "_dl_target_dir", None) is None,
      repr(getattr(LAST_TAB, "_dl_target_dir", "<无标签>")))

print("\n--- ⑤ 探测失败必须放行（不能误报「不存在」）---")
w, d, sent = run_case("探测炸了", "/a/yes.txt", {"/a/yes.txt": True},
                      str(tmp), boom=TimeoutError("链路超时"))
check("**没有**报「远程文件不存在」（问不到 ≠ 不存在）",
      not any("不存在" in x[0] for x in w), str(w))
check("仍然走到目录框那一步", bool(d), str(d))
check("并把 sz 发了出去", sent.startswith("sz /a/yes.txt\n"), repr(sent))

print("\n--- ⑤' 远端答了 done，但这条路径一行都没回 ---")
w, d, sent = run_case("没回答", "/a/silent.txt", {"/a/silent.txt": True},
                      str(tmp), silent=("/a/silent.txt",))
check("**没有**报「不存在」（问不到 ≠ 不存在，.get(t) 不是 False）",
      not any("不存在" in x[0] for x in w), str(w))
check("仍然放行到目录框", bool(d), str(d))
check("并把 sz 发了出去", sent.startswith("sz /a/silent.txt\n"), repr(sent))

print("\n--- ⑤\" ~ 路径（远端会展开，回显的是 /home/...）---")
w, d, sent = run_case("家目录", "~/docs/t.md", {"~/docs/t.md": True}, str(tmp))
check("**没有**把 ~ 路径误报成「不存在」（远端回的是展开后的路径）",
      not any("不存在" in x[0] for x in w), str(w))
check("放行到目录框", bool(d), str(d))
check("发出去的 sz 里 ~ 走远端 ${HOME} 插值（不是裸 ~，也不是被单引号包住而失效）",
      sent.startswith('sz "${HOME}"/docs/t.md\n'), repr(sent))

print("\n--- ⑥ 工具栏不再有那个目录按钮 ---")
# 注意：工具栏是 SessionTab 自己的，不是 MainWindow 的。挂在 MainWindow 上
# 查会**恒绿**——压根没人定义过那个名字，绿得毫无意义。
tab, _ = attach({"/a/yes.txt": True})
check("SessionTab 上没有 dl_dir_btn 了", not hasattr(tab, "dl_dir_btn"))
check("SessionTab 上也没有 _on_choose_dl_dir 这个入口了",
      not hasattr(MW.SessionTab, "_on_choose_dl_dir"))
icons = [getattr(b, "_icon_name", None) for b in tab._toolbar_icon_buttons]
# 钉**完整集合**，而不是「里面没有 folder」——后者在读错对象（拿到空列表
# 或别的按钮表）时同样会绿，等于什么都没断言。
check("工具栏图标按钮就是这 8 个（不含 folder）",
      icons == ["plug", "power", "upload", "download",
                "copy", "paste", "eraser", "snippet"], str(icons))
check("sz 目标输入框还在工具栏上（没被误删）",
      tab.sz_target_edit is not None and
      tab.sz_target_edit.parent() is tab, repr(tab.sz_target_edit))

win.close()
spin(50)
print("\n通过 %d 项，失败 %d 项" % (OK, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：%s" % f)
    sys.exit(1)
