"""SFTP 文件管理与文件传输的功能测试：布局、导航、刷新、增删、上传下载、
传输状态条、忙时闸门。

**判据钉在「用户看得见的行为」上。** 「类里有没有那个 if」「信号签名对不对」
只有当它本身就是契约时才当判据（例如失败信号必须带**原异常对象**，因为下游
要打 `type(exc).__name__`）；其余一律写成「点了之后界面上发生了什么」。

**异步一律事件驱动。** 假 fs 的慢操作卡在 `threading.Event` 上，判据等的是
那个事件，不是 `sleep`。等待统一走 `wait_until(pred, ms)`。

**元断言（负向对照）至少 6 条。** 每条都把被测实现换成缺陷版本，再跑同一段
判据，断言它变红；做不到的判据在下面写明了原因。没有元断言的判据可能恒真。

**绝不碰用户数据。** 本脚本不构造 `AppSettings`、不构造 `SessionStore`；
所有上传/下载的落点都在 `%TEMP%` 下的一次性目录里，跑完删掉。`download`
是「先写 `<name>.part` 再 `os.replace`」，把目标指到仓库里任何一个文件都会
在失败路径上把测试源码本身覆盖掉 —— 这个坑仓库里踩过一次。

退出码：0 全过 / 1 有 FAIL / 2 有判据因前提不成立没评上（见 `SKIP`）。

Checks 覆盖的主题：
  1. 双栏布局、中间按钮列、四列表头、计数标签、行高/隔行/缩进
  2. 目录导航：双击 / 回车进目录、点文件无动作、路径栏跳转、相对路径改写
  3. 「上一级」：远程根守卫、本地盘符根守卫（不能跳进进程工作目录）、UNC
  4. 远程 `~` 展开、`~user` 不乱猜、家目录取不到时早失败
  5. 刷新：本地同步、远程异步「读取中…」、过期结果丢弃、起线程抛异常不留痕
  6. 读取失败：「读取失败」+ tooltip 里是原异常类名；本地目录消失同样报错
  7. 排序：目录在前、表头切升降序
  8. 选中/多选/删除按钮启停、重新填表后重算
  9. 新建目录：路径拼接、离开 GUI 线程、在飞时禁用、成功/失败反馈、连点只起一个
  10. 删除：确认框、目录不递归、多选部分失败
  11. 上传下载：无选中提示、跳过目录、逐个覆盖确认（两个按路径入口）
  12. `.part` 原子下载：中途有临时件、成功后消失、中途失败原文件不动
  13. 上传侧直传（已知残留风险，只记录不判失败）
  14. 传输状态条：空闲隐藏、开始态、不定态、多文件文案、等权百分比、三种收尾
  15. 忙时闸门：第二次 begin 拒绝且不起引擎、引擎启动失败放闸门、断线放闸门
  16. 闸门拒绝必须出声（红，见结论）
  17. 传输期按钮禁用、传完刷新目标侧、只在全成功时刷新、切目录后不覆盖
  18. 面板在传输途中被析构
 19. 抢锁超时连**拿到锁之后的那次往返**一起管住；超时不许变成假的 `False`
 20. Delete 键删除所选（走按钮那条路，含确认框，且不变成立地改名）
"""
import inspect
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from sshclient.ui import msgbox
from sshclient.ui import sftp_panel as sp
from sshclient.ui import transfer_strip as ts_mod
from sshclient.ui.design.v1.palette import get_palette
from sshclient.ui.transfer_controller import TransferController
from sshclient.ui.transfer_strip import TransferStrip

#: 成功/取消的停留时长是**模块级**常量（transfer_strip.py:30），不是类属性。
#: 写进类里会 AttributeError —— 这条就是判据自己写错的现场。
SUCCESS_LINGER_MS = ts_mod.SUCCESS_LINGER_MS

PASS, FAIL, SKIP = [], [], []
PAL = None


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}"
          + (f"  ({detail})" if detail else ""))


def skip(name, why):
    """前提不成立时用这个：既不算通过也不算失败，退出码落到 2。"""
    SKIP.append("%s —— %s" % (name, why))
    print(f"  SKIP  {name}  ({why})")


def section(title):
    print("\n=== %s ===" % title)


def pump(ms=250):
    """在 GUI 线程上把事件循环转够这么久。"""
    app = QtWidgets.QApplication.instance()
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.002)


def wait_until(pred, ms=2000):
    app = QtWidgets.QApplication.instance()
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.002)
    return pred()


def reap(*widgets):
    """Qt 对象要走 close → deleteLater → sendPostedEvents，否则退出时偶发 0xC0000005。"""
    for w in widgets:
        if w is None:
            continue
        try:
            w.close()
        except RuntimeError:
            continue
        try:
            w.deleteLater()
        except RuntimeError:
            pass
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


def names(panel):
    return [panel.model.item(r, 0).text() for r in range(panel.model.rowCount())]


#: 每一个一次性目录都在这里登记，`main()` 收尾统一删。
#: 早先靠各个 section 把自己建的目录 return 出去再删，漏掉几个就在 %TEMP%
#: 里一次次堆着（实测跑十轮堆了 110 个）。登记制不依赖谁记得回传。
TMP_DIRS = []


def workdir(tag):
    d = tempfile.mkdtemp(prefix="_fsftp_%s_" % tag)
    TMP_DIRS.append(d)
    return d


def entry(name, path=None, is_dir=False, size=1, mtime=0.0, mode=0o100644):
    return sp.FileEntry(name=name, path=path or ("/tmp/" + name),
                        is_dir=is_dir, size=size, mtime=mtime, mode=mode)


# ---------------------------------------------------------------------------
# 替身。**只实现被测到的那个方法**：面板真去调一个没实现的方法会 AttributeError，
# 而那会伪装成「产品坏了」。所以这里每份替身都按它服务的那条判据裁剪过。
# ---------------------------------------------------------------------------


class StaticFS:
    """同步列目录的假 fs，永远返回同一批条目。"""

    def __init__(self, items, exc=None):
        self.items = items
        self.exc = exc
        self.calls = []

    def listdir(self, cwd):
        self.calls.append(cwd)
        if self.exc is not None:
            raise self.exc
        return list(self.items)


class ScriptedFS:
    """每次 listdir 按剧本走：可以卡在闸门上，也可以直接抛异常。

    ``steps`` 里每一项是 ``(entries, gate, exc)``。
    """

    def __init__(self, steps):
        self.steps = steps
        self.entered = []
        self.calls = 0

    def listdir(self, cwd):
        i = self.calls
        self.calls += 1
        entries, gate, exc = self.steps[min(i, len(self.steps) - 1)]
        self.entered.append(threading.Event())
        self.entered[-1].set()
        if gate is not None:
            gate.wait(5)
        if exc is not None:
            raise exc
        return list(entries)


class GateOpFS:
    """慢操作（mkdir/remove）卡在闸门上，用来量 GUI 线程有没有被挡住。"""

    def __init__(self, exc=None, gate=None):
        self.gate = gate or threading.Event()
        self.entered = threading.Event()
        self.thread_name = None
        self.exc = exc
        self.made = []
        self.removed = []
        self.rmdired = []
        self.listdir_calls = 0

    def listdir(self, cwd):
        self.listdir_calls += 1
        return []

    def mkdir(self, path):
        self.thread_name = threading.current_thread().name
        self.entered.set()
        self.gate.wait(5)
        if self.exc is not None:
            raise self.exc
        self.made.append(path)

    def remove(self, path):
        self.thread_name = threading.current_thread().name
        self.entered.set()
        self.gate.wait(5)
        if self.exc is not None:
            raise self.exc
        self.removed.append(path)

    def rmdir(self, path):
        self.thread_name = threading.current_thread().name
        self.entered.set()
        self.gate.wait(5)
        if self.exc is not None:
            raise self.exc
        # **不转给 remove()**：转过去的话 `removed` 里会同时出现目录，
        # 「文件走 remove、目录走 rmdir」那条判据就没法判了。
        self.rmdired.append(path)


class _Attr:
    def __init__(self, filename, st_mode, st_size, st_mtime):
        self.filename = filename
        self.st_mode = st_mode
        self.st_size = st_size
        self.st_mtime = st_mtime


class _Stat:
    def __init__(self, st_mode, st_size):
        self.st_mode = st_mode
        self.st_size = st_size


class PaneSftp:
    """SFTPClient 的替身：**按目录存文件**，所以「切到别的目录」是能测出来的。

    只实现 `RemoteFileSystem` 真正会转发的那些方法。传输可用 `put_gate`
    卡住第一个 put（后面的照传），`put_exc` 让它抛。
    """

    def __init__(self, home="/home/u", tree=None):
        self.home = home
        self.tree = tree if tree is not None else {home: {"r1.txt": b"R1"}}
        self.calls = []
        self.puts = []
        self.gets = []
        self.put_gate = None
        self.put_entered = threading.Event()
        self.put_exc = None
        self.get_hook = None

    def normalize(self, path):
        self.calls.append(("normalize", path))
        return self.home if path == "." else path

    def listdir_attr(self, path):
        self.calls.append(("listdir_attr", path))
        out = []
        for name in sorted(self.tree.get(path, {})):
            data = self.tree[path][name]
            out.append(_Attr(name, 0o100644, len(data), 1757000000.0))
        return out

    def stat(self, path):
        self.calls.append(("stat", path))
        # 目录必须能 stat 得出来：`download_paths` 的跳过判定走的是
        # `is_dir()` → `stat().st_mode`，stat 一抛就当「不是目录」，
        # 于是远程目录会被当成文件排队下载 —— 那是替身不称职，不是产品坏。
        if path in self.tree:
            return _Stat(0o40755, 0)
        name = path.rsplit("/", 1)[-1]
        base = path.rsplit("/", 1)[0] or "/"
        if base in self.tree and name in self.tree[base]:
            return _Stat(0o100644, len(self.tree[base][name]))
        raise FileNotFoundError(2, "No such file", path)

    def put(self, local, remote, callback=None):
        self.calls.append(("put", remote))
        self.puts.append((local, remote))
        if self.put_gate is not None:
            self.put_entered.set()
            self.put_gate.wait(5)
        base = remote.rsplit("/", 1)[0] or "/"
        name = remote.rsplit("/", 1)[-1]
        # **先按 `"wb"` 的语义把目标截断，再谈失败。**
        # paramiko 打开远端文件就是截断式的，所以「传到一半断网」的真相是
        # **目标已经被开了**。这个替身原来在写任何东西之前就抛，于是
        # 「失败后目标完好无损」在替身上恒成立 —— 用它当元断言的对照组，
        # 无论直传还是临时件方案都会绿，那条判据就等于没有。
        self.tree.setdefault(base, {})[name] = b""
        if self.put_exc is not None:
            raise self.put_exc
        with open(local, "rb") as f:
            data = f.read()
        self.tree[base][name] = data
        if callback:
            callback(len(data), len(data))

    def get(self, remote, local, callback=None):
        self.calls.append(("get", remote))
        self.gets.append((remote, local))
        base = remote.rsplit("/", 1)[0] or "/"
        data = self.tree.get(base, {}).get(remote.rsplit("/", 1)[-1], b"")
        if self.get_hook is not None:
            self.get_hook(local)
            return
        with open(local, "wb") as f:
            f.write(data)
        if callback:
            callback(len(data), len(data))

    # -- 上传现在走「远端临时件 + 改名」，替身必须真的有这三个方法 -----
    # 没有它们就是「替身不称职」，症状会伪装成「产品把文件传丢了」。

    def _move(self, src, dst):
        s_base = src.rsplit("/", 1)[0] or "/"
        s_name = src.rsplit("/", 1)[-1]
        d_base = dst.rsplit("/", 1)[0] or "/"
        d_name = dst.rsplit("/", 1)[-1]
        if s_base not in self.tree or s_name not in self.tree[s_base]:
            raise FileNotFoundError(2, "No such file", src)
        self.tree.setdefault(d_base, {})[d_name] = self.tree[s_base].pop(s_name)

    def rename(self, src, dst):
        """标准 v3：**目标必须不存在**，存在就要报错。"""
        self.calls.append(("rename", src, dst))
        d_base = dst.rsplit("/", 1)[0] or "/"
        if dst.rsplit("/", 1)[-1] in self.tree.get(d_base, {}):
            raise FileExistsError(17, "File exists", dst)
        self._move(src, dst)

    def posix_rename(self, src, dst):
        """v3 扩展：允许覆盖。"""
        self.calls.append(("posix_rename", src, dst))
        self._move(src, dst)

    def remove(self, path):
        """远端删一个文件。删不掉要**抛 FileNotFoundError** —— 上传的失败清理
        靠这个区分「清掉了」与「没清掉」（前者静默，后者要说给用户）。"""
        self.calls.append(("remove", path))
        base = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        if base in self.tree and name in self.tree[base]:
            del self.tree[base][name]
            return
        raise FileNotFoundError(2, "No such file", path)

    def mkdir(self, path):
        self.calls.append(("mkdir", path))
        self.tree.setdefault(path, {})

    def rmdir(self, path):
        self.calls.append(("rmdir", path))
        self.tree.pop(path, None)


class Engine:
    """只满足 `TransferAdapter` 协议的最小引擎：记不记起过，一眼能看出来。"""

    def __init__(self, started=None, name="engine"):
        self.started = started if started is not None else []
        self.name = name
        self.cancelled = 0

    def start(self):
        self.started.append(self.name)

    def request_cancel(self):
        self.cancelled += 1


class NoGuardController(TransferController):
    """元断言用的缺陷版：把「第一个终态说了算」整条守卫拆掉。

    **两处都要拆。** 守卫是「`_adapter is None` 就提前 return」**加上**
    收尾时把 `_adapter` 置 None；只拆掉前半截的话，随后的 `succeed` 照样撞上
    「没有在传的批次」被挡回去，元断言会假绿 —— 那正是它自己要去证伪的东西。
    """

    def _finish(self, apply, title, detail, tip=""):
        if tip:
            self.strip.setToolTip(tip)
        apply(title, detail)          # 既不判 None，也不清 _adapter


class NoBusyController(TransferController):
    """元断言用的缺陷版：忙时不禁用按钮。"""

    def busyChangedEmit(self, _v):
        pass


def build_panel(sftp, ctrl=None, host=None, flash=None):
    """按 main_window 的接法搭一个 SftpPanel：会话级控制器 + 注入的 flash。"""
    ctrl = ctrl if ctrl is not None else TransferController(PAL, host)
    panel = sp.SftpPanel(sftp, PAL, host, ctrl=ctrl, flash=flash)
    return panel, ctrl


# ===========================================================================
# 1. 双栏布局 / 表头 / 计数标签 / 行高
# ===========================================================================

def t_layout():
    section("1 双栏布局与表格")
    panel = sp.SftpPanel(PaneSftp(), PAL, None, flash=lambda m: None)
    split = panel.layout().itemAt(0).layout()
    check("1 布局是三段：本地 | 中间按钮列 | 远程",
          split.count() == 3 and split.itemAt(0).widget() is panel.local
          and split.itemAt(2).widget() is panel.remote,
          "count=%d" % split.count())
    mid = split.itemAt(1).widget()
    check("1 中间按钮列宽度固定 150", mid.width() == 150, "width=%d" % mid.width())
    check("1 两个传输按钮的最小宽度是 120",
          panel.up_btn.minimumWidth() == 120 and panel.down_btn.minimumWidth() == 120)
    check("1 四列表头是「名称/大小/修改时间/权限」",
          [sp.FilePanel.HEADERS] == [["名称", "大小", "修改时间", "权限"]]
          and [panel.local.model.headerData(i, QtCore.Qt.Orientation.Horizontal)
               for i in range(4)] == ["名称", "大小", "修改时间", "权限"],
          str([panel.local.model.headerData(i, QtCore.Qt.Orientation.Horizontal)
               for i in range(4)]))
    check("1 行高统一 + 隔行底色 + 零缩进 + 不画树装饰",
          panel.local.view.uniformRowHeights()
          and panel.local.view.alternatingRowColors()
          and panel.local.view.indentation() == 0
          and not panel.local.view.rootIsDecorated(),
          "indent=%d" % panel.local.view.indentation())
    check("1 多选行 + 不可就地编辑",
          panel.local.view.selectionMode()
          == QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection
          and panel.local.view.editTriggers()
          == QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)

    d = workdir("count")
    for n in ("f1.txt", "f2.txt"):
        with open(os.path.join(d, n), "wb") as f:
            f.write(b"x")
    os.mkdir(os.path.join(d, "sub"))
    panel.local.cd(d)
    check("1 计数标签是「1 个目录 · 2 个文件」",
          panel.local.count_label.text() == "1 个目录 · 2 个文件",
          panel.local.count_label.text())
    check("1 目录排在文件前面",
          names(panel.local)[0] == "sub", str(names(panel.local)))
    check("1 文件名带 tooltip（窄栏里被截断时还能看全名）",
          panel.local.model.item(1, 0).toolTip() == "f1.txt",
          panel.local.model.item(1, 0).toolTip())
    check("1 目录的大小列留空、文件填紧凑写法",
          panel.local.model.item(0, 1).text() == ""
          and panel.local.model.item(1, 1).text() == "1B",
          "%r/%r" % (panel.local.model.item(0, 1).text(),
                     panel.local.model.item(1, 1).text()))
    reap(panel)
    return d


# ===========================================================================
# 2. 目录导航
# ===========================================================================

def t_navigation():
    section("2 目录导航")
    fs = StaticFS([entry("dirA", "/tmp/dirA", True, mode=0o40755),
                   entry("f1.txt", "/tmp/f1.txt")])
    fp = sp.FilePanel("remote", fs, PAL)
    fp.cd("/tmp")
    wait_until(lambda: fp.model.rowCount() == 2)
    before = fp.current_path()

    fp.view.activated.emit(fp.model.index(0, 0))
    pump(60)
    check("2 回车（activated）能进目录", fp.current_path() == "/tmp/dirA",
          fp.current_path())

    fp.cd("/tmp")
    wait_until(lambda: fp.model.rowCount() == 2)
    fp.view.doubleClicked.emit(fp.model.index(1, 0))
    pump(60)
    check("2 双击文件不进目录（也不报错）",
          fp.current_path() == "/tmp" and fp.model.rowCount() == 2,
          fp.current_path())

    fp.path_edit.setText("/tmp/dirA/sub")
    fp.path_edit.returnPressed.emit()
    pump(60)
    check("2 路径栏回车跳到那个目录", fp.current_path() == "/tmp/dirA/sub",
          fp.current_path())
    check("2 切目录后路径栏跟着更新",
          fp.path_edit.text() == fp.current_path(),
          "%r vs %r" % (fp.path_edit.text(), fp.current_path()))

    d = workdir("nav")
    os.mkdir(os.path.join(d, "kid"))
    lp = sp.FilePanel("local", sp.LocalFileSystem(), PAL)
    lp.cd("kid")
    check("2 本地侧相对路径被改写成绝对路径",
          lp.current_path() == os.path.abspath(os.path.join(os.getcwd(), "kid")),
          lp.current_path())
    lp.path_edit.setText(d)
    lp.path_edit.returnPressed.emit()
    check("2 路径栏回车也能在本地侧跳转", lp.current_path() == d, lp.current_path())

    seen = []
    fp.pathChanged.connect(seen.append)
    fp.cd("/tmp/dirA")
    pump(40)
    check("2 切目录会向外广播新路径", seen and seen[-1] == "/tmp/dirA", str(seen[-1:]))
    reap(fp, lp)
    return d


# ===========================================================================
# 3. 「上一级」
# ===========================================================================

def t_go_up():
    section("3 上一级")
    fp = sp.FilePanel("remote", StaticFS([]), PAL)
    fp.cd("/")
    pump(40)
    fp.go_up()
    pump(40)
    check("3 远程根目录点上一级停在 /", fp.current_path() == "/",
          repr(fp.current_path()))
    fp.cd("/tmp")
    pump(40)
    fp.go_up()
    pump(40)
    check("3 远程 /tmp 上一级是 /", fp.current_path() == "/", repr(fp.current_path()))
    fp.cd("/tmp/x")
    pump(40)
    fp.go_up()
    pump(40)
    check("3 远程 /tmp/x 上一级是 /tmp", fp.current_path() == "/tmp",
          repr(fp.current_path()))

    drive_root = os.path.splitdrive(tempfile.gettempdir())[0] + "\\"
    lp = sp.FilePanel("local", sp.LocalFileSystem(), PAL)
    lp.cd(drive_root)
    check("3 本地先落在盘符根上", lp.current_path() == drive_root,
          repr(lp.current_path()))
    lp.go_up()
    pump(40)
    check("3 盘符根点上一级停在原地，不跳进进程工作目录",
          lp.current_path() == drive_root and lp.current_path() != os.getcwd(),
          "cwd=%r 而进程工作目录是 %r" % (lp.current_path(), os.getcwd()))
    lp.cd(drive_root + os.path.basename(tempfile.gettempdir()))
    lp.go_up()
    pump(40)
    check("3 盘符下第一层上一级回到盘符根", lp.current_path() == drive_root,
          repr(lp.current_path()))
    reap(fp, lp)


def t_unc():
    section("3b UNC 路径")
    if os.name != "nt":
        skip("3 UNC 共享根点上一级停在原地",
             "ntpath 语义只在 Windows 上成立（本机 os.name=%s）" % os.name)
        return
    lp = sp.FilePanel("local", sp.LocalFileSystem(), PAL)
    unc = r"\\server\share"
    lp._cwd = unc
    lp.go_up()
    pump(40)
    check("3 UNC 共享根点上一级停在原地",
          lp.current_path() == unc, repr(lp.current_path()))
    lp._cwd = unc + r"\sub"
    lp.go_up()
    pump(40)
    # **比对时要去掉尾分隔符。** `ntpath.dirname(r"\\server\share\sub")` 返回的是
    # `\\server\share\`（带尾反斜杠），而 `os.path.normpath` 对 UNC 也不会去掉它，
    # 所以「字符串完全相等」这条判据物理上不可能成立 —— 而用户其实**已经**回到
    # 共享根了。这是判据写错，不是产品缺陷。
    check("3 UNC 共享下第一层上一级回到共享根（尾分隔符除外）",
          lp.current_path().rstrip("\\/") == unc, repr(lp.current_path()))
    lp.go_up()
    pump(40)
    check("3 UNC 共享根点上一级不再往上跳",
          lp.current_path().rstrip("\\/") == unc, repr(lp.current_path()))
    reap(lp)


# ===========================================================================
# 4. 远程 ~ 展开
# ===========================================================================

class TildeSftp:
    def __init__(self, home="/home/deploy", home_fails=False):
        self.home = home
        self.home_fails = home_fails
        self.sent = []

    def normalize(self, path):
        self.sent.append(("normalize", path))
        if self.home_fails:
            raise OSError("channel busy")
        return self.home if path == "." else path

    def listdir_attr(self, path):
        self.sent.append(("listdir_attr", path))
        if path.startswith("~"):
            raise FileNotFoundError(2, "No such file", path)
        return [_Attr("a", 0o100644, 1, 0.0)]


def t_tilde():
    section("4 远程 ~ 展开")
    fake = TildeSftp()
    fp = sp.FilePanel("remote", sp.RemoteFileSystem(fake), PAL)
    fp.cd("~/workspaces")
    wait_until(lambda: fp.model.rowCount() == 1)
    asked = [c[1] for c in fake.sent if c[0] == "listdir_attr"]
    check("4 路径栏里的 ~ 被展开成远端家目录后才发给 SFTP",
          asked == ["/home/deploy/workspaces"], str(asked))
    check("4 展开成功时列表照常显示", fp.count_label.text() == "0 个目录 · 1 个文件",
          fp.count_label.text())

    fake2 = TildeSftp()
    fp2 = sp.FilePanel("remote", sp.RemoteFileSystem(fake2), PAL)
    fp2.cd("~root/x")
    wait_until(lambda: fp2.count_label.text() == "读取失败")
    asked2 = [c[1] for c in fake2.sent if c[0] == "listdir_attr"]
    check("4 ~user 不乱猜，原样交给 SFTP 报错",
          asked2 == ["~root/x"], str(asked2))
    check("4 ~user 展开不了时如实报「读取失败」",
          fp2.count_label.text() == "读取失败", fp2.count_label.text())

    fake3 = TildeSftp(home_fails=True)
    fp3 = sp.FilePanel("remote", sp.RemoteFileSystem(fake3), PAL)
    t0 = time.monotonic()
    fp3.cd("~/x")
    ok = wait_until(lambda: fp3.count_label.text() == "读取失败", 1500)
    dt = time.monotonic() - t0
    asked3 = [c[1] for c in fake3.sent if c[0] == "listdir_attr"]
    check("4 家目录取不到时早失败而不是卡住",
          ok and asked3 == ["~/x"] and dt < 1.0,
          "%.0f ms，送出 %s" % (dt * 1000, asked3))
    reap(fp, fp2, fp3)


# ===========================================================================
# 5/6. 刷新与读取失败
# ===========================================================================

def t_refresh():
    section("5 刷新")
    d = workdir("sync")
    with open(os.path.join(d, "a.txt"), "wb") as f:
        f.write(b"x")
    lp = sp.FilePanel("local", sp.LocalFileSystem(), PAL)
    lp.cd(d)
    check("5 本地侧同步填表：cd 返回时行已经在（不用转事件循环）",
          lp.model.rowCount() == 1, "rows=%d" % lp.model.rowCount())

    gate = threading.Event()
    steps = [([entry("slow.txt", "/tmp/slow.txt")], gate, None)]
    fp = sp.FilePanel("remote", ScriptedFS(steps), PAL)
    fp.cd("/tmp")
    check("5 远程刷新立刻给出「读取中…」中间态",
          fp.count_label.text() == "读取中…", fp.count_label.text())
    check("5 远程刷新在结果回来之前不填表（不显示上一目录的内容）",
          fp.model.rowCount() == 0, "rows=%d" % fp.model.rowCount())
    gate.set()
    wait_until(lambda: fp.model.rowCount() == 1)
    check("5 结果回来后中间态换成计数文案",
          fp.count_label.text() == "0 个目录 · 1 个文件", fp.count_label.text())

    # 过期结果：在第一次还在飞的时候又切了一次目录
    g1, g2 = threading.Event(), threading.Event()
    fs2 = ScriptedFS([([entry("stale.txt", "/a/stale.txt")], g1, None),
                      ([entry("fresh.txt", "/b/fresh.txt")], g2, None)])
    fp2 = sp.FilePanel("remote", fs2, PAL)
    fp2.cd("/a")
    fp2.cd("/b")
    check("5 往返期间又切目录：两次请求都在飞",
          fs2.calls == 2 and fp2.count_label.text() == "读取中…", "calls=%d" % fs2.calls)
    g2.set()
    wait_until(lambda: fp2.model.rowCount() == 1)
    g1.set()
    pump(200)
    check("5 过期的结果被丢掉，不会盖掉新目录的列表",
          names(fp2) == ["fresh.txt"], str(names(fp2)))
    check("5 切完目录路径栏同步到新目录", fp2.path_edit.text() == "/b",
          fp2.path_edit.text())
    reap(lp, fp, fp2)

    section("5b 起线程失败")
    fs3 = StaticFS([])
    fp3 = sp.FilePanel("remote", fs3, PAL)
    real_start = threading.Thread.start

    def boom_start(self, *a, **k):
        raise RuntimeError("can't start new thread")

    threading.Thread.start = boom_start
    threw = None
    try:
        fp3.refresh()
    except BaseException as exc:              # noqa: BLE001
        threw = exc
    finally:
        threading.Thread.start = real_start
    check("5 起线程抛出的异常不外泄回调用方", threw is None,
          type(threw).__name__ if threw else "")
    check("5 起线程失败后标签不留在「读取中…」，而是「读取失败」",
          fp3.count_label.text() == "读取失败", fp3.count_label.text())
    check("5 起线程失败时一次 listdir 都没发出去", fs3.calls == [],
          "calls=%s" % fs3.calls)
    reap(fp3)
    return d


def t_load_failed():
    section("6 读取失败")
    tips = []
    real_tooltip = QtWidgets.QToolTip.showText
    QtWidgets.QToolTip.showText = staticmethod(
        lambda pos, text, *a, **k: tips.append(text))
    try:
        class SftpError(Exception):
            pass

        fs = StaticFS([], exc=SftpError("No such file"))
        fp = sp.FilePanel("remote", fs, PAL)
        fp.cd("/tmp/nope")
        wait_until(lambda: fp.count_label.text() == "读取失败")
        check("6 读取失败时计数标签恰好是「读取失败」",
              fp.count_label.text() == "读取失败", fp.count_label.text())
        check("6 失败后列表是空的，不留着上一目录的内容",
              fp.model.rowCount() == 0, "rows=%d" % fp.model.rowCount())
        check("6 气泡里是原异常类名，不是被包成 RuntimeError",
              bool(tips) and "SftpError" in tips[-1]
              and "RuntimeError" not in tips[-1],
              tips[-1] if tips else "根本没有气泡")

        # 本地侧目录消失：必须报错，不能静默显示「0 个目录 · 0 个文件」
        d = workdir("gone")
        os.rmdir(d)
        lp = sp.FilePanel("local", sp.LocalFileSystem(), PAL)
        lp._cwd = d
        tips.clear()
        lp.refresh()
        check("6 本地目录被删掉时同样报「读取失败」，不是静默 0 个",
              lp.count_label.text() == "读取失败", lp.count_label.text())
        check("6 本地侧失败的气泡里是原异常类名",
              bool(tips) and "FileNotFoundError" in tips[-1],
              tips[-1] if tips else "没有气泡")

        # 元断言：把「目录读不到当空目录」这个旧行为装回去，上面那条必须变红
        class SwallowFS:
            def __init__(self, inner):
                self.inner = inner

            def listdir(self, path):
                try:
                    return self.inner.listdir(path)
                except OSError:
                    return []          # 修复前的行为

        broken = sp.FilePanel("local", SwallowFS(sp.LocalFileSystem()), PAL)
        broken._cwd = d
        broken.refresh()
        check("6 元断言：旧行为（读不到就当空目录）给的是「0 个目录 · 0 个文件」"
              "而不是「读取失败」，所以上一条不是恒真",
              broken.count_label.text() == "0 个目录 · 0 个文件",
              broken.count_label.text())
    finally:
        QtWidgets.QToolTip.showText = real_tooltip
    reap(fp, lp, broken)


def t_sort():
    section("7 排序")
    d = workdir("sort")
    for n in ("b.txt", "a.txt", "10.txt", "2.txt"):
        with open(os.path.join(d, n), "wb") as f:
            f.write(b"1")
    os.mkdir(os.path.join(d, "zdir"))
    os.mkdir(os.path.join(d, "adir"))
    fp = sp.FilePanel("local", sp.LocalFileSystem(), PAL)
    fp.cd(d)
    check("7 目录排在文件前面",
          names(fp) == ["adir", "zdir", "10.txt", "2.txt", "a.txt", "b.txt"],
          str(names(fp)))

    fp.view.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)
    pump(40)
    asc = names(fp)
    fp.view.sortByColumn(0, QtCore.Qt.SortOrder.DescendingOrder)
    pump(40)
    desc = names(fp)
    check("7 点名称表头能切升序/降序",
          asc == sorted(asc, key=str.lower) and desc == list(reversed(asc)),
          "asc=%s desc=%s" % (asc, desc))
    check("7 表头自己记着当前排序列",
          fp.view.header().sortIndicatorSection() == 0,
          str(fp.view.header().sortIndicatorSection()))
    # 元断言：换成按大小排，同一串输入下结论必须变 —— 证明上一条量的是排序本身
    fp.view.sortByColumn(1, QtCore.Qt.SortOrder.AscendingOrder)
    pump(40)
    by_size = names(fp)
    check("7 元断言：改按大小排结果就变，说明上一条不是恒真",
          by_size != desc and fp.view.header().sortIndicatorSection() == 1,
          str(by_size))
    reap(fp)
    return d


# ===========================================================================
# 8. 选中 / 删除按钮
# ===========================================================================

def t_selection():
    section("8 选中与删除按钮")
    fp = sp.FilePanel("remote", StaticFS(
        [entry("a.txt", "/tmp/a.txt"), entry("b.txt", "/tmp/b.txt")]), PAL)
    fp.cd("/tmp")
    wait_until(lambda: fp.model.rowCount() == 2)
    check("8 没选中时「删除所选」是禁用的", not fp.delete_btn.isEnabled())
    fp.view.selectionModel().select(
        fp.model.index(0, 0),
        QtCore.QItemSelectionModel.SelectionFlag.Select
        | QtCore.QItemSelectionModel.SelectionFlag.Rows)
    pump(40)
    check("8 选中一行后「删除所选」可用", fp.delete_btn.isEnabled())
    check("8 选中带出的是那一行的条目",
          [e.name for e in fp.selected_entries()] == ["a.txt"],
          str([e.name for e in fp.selected_entries()]))
    fp.view.selectAll()
    pump(40)
    check("8 多选带出两行且按行号排序",
          [e.name for e in fp.selected_entries()] == ["a.txt", "b.txt"],
          str([e.name for e in fp.selected_entries()]))
    fp.populate_from([entry("c.txt", "/tmp/c.txt")])
    pump(40)
    check("8 重新填表后选中清空、按钮跟着回到禁用",
          fp.selected_entries() == [] and not fp.delete_btn.isEnabled(),
          str([e.name for e in fp.selected_entries()]))
    reap(fp)


# ===========================================================================
# 9. 新建目录
# ===========================================================================

def t_mkdir():
    section("9 新建目录")
    # **ask_text 的替换必须覆盖整个函数。** 它是模态对话框，真跑起来会
    # `dlg.exec()` 一直等输入 —— 只在开头一段替换、后面忘了还原，那一段
    # 就会把整个脚本挂死（真踩过）。这里用一个可变的 answer 控制「确认/取消」。
    real_ask = sp.msgbox.ask_text
    real_warn = sp.msgbox.warning
    answer = {"ok": True, "name": "newdir"}
    asks = []
    warns = []

    def fake_ask(parent, title, label, text=""):
        asks.append((title, label))
        return (answer["name"], answer["ok"]) if answer["ok"] else ("", False)

    sp.msgbox.ask_text = fake_ask
    sp.msgbox.warning = lambda parent, title, text, *a, **k: warns.append((title, text))
    try:
        fs = GateOpFS()
        rp = sp.FilePanel("remote", fs, PAL)
        rp.cd("/tmp")
        pump(40)
        flashes = []
        rp._flash = flashes.append
        t0 = time.monotonic()
        rp.mkdir_btn.click()
        t1 = time.monotonic()
        check("9 mkdir 没挡住 GUI 线程", (t1 - t0) < 0.15,
              "%.0f ms" % ((t1 - t0) * 1000))
        wait_until(fs.entered.is_set)
        check("9 mkdir 真的跑在工作线程上",
              fs.thread_name not in (None, threading.current_thread().name),
              str(fs.thread_name))
        check("9 在飞时标签是「处理中…」", rp.count_label.text() == "处理中…",
              rp.count_label.text())
        check("9 在飞时「新建目录」禁用", not rp.mkdir_btn.isEnabled())
        check("9 在飞时「删除所选」也禁用（它抢同一条通道）",
              not rp.delete_btn.isEnabled())
        rp.mkdir_btn.click()                  # 连点第二次（按钮已禁用）
        fs.gate.set()
        wait_until(lambda: rp._op_worker is None)
        check("9 连点两次只起一个 mkdir", fs.made == ["/tmp/newdir"], str(fs.made))
        check("9 成功时走注入的状态条提示", flashes == ["已新建「newdir」"],
              str(flashes))
        check("9 完成后按钮恢复可用", rp.mkdir_btn.isEnabled())
        wait_until(lambda: rp.model.rowCount() >= 0)
        check("9 成功后刷新过列表", fs.listdir_calls >= 1,
              "listdir_calls=%d" % fs.listdir_calls)
        rp._on_mkdir()                        # 程序化再叫一次（绕过按钮）
        wait_until(lambda: fs.made, 500)
        fs.gate.set()
        check("9 程序化重复调用也被挡住（不是只靠按钮禁用）",
              fs.made.count("/tmp/newdir") == 2,
              str(fs.made))

        # 元断言：把慢操作搬回 GUI 线程，同一段「闸门关着」的测量必须变红。
        # 闸门由另一个线程在 0.4 秒后放开，所以「挡住」这件事是可重复量的。
        class SyncPanel(sp.FilePanel):
            def _run_off_thread(self, fn, *args, fail_title, done_text=""):
                self.count_label.setText("处理中…")
                self._set_busy(True)
                try:
                    fn(*args)
                finally:
                    self._set_busy(False)

        sfs = GateOpFS()                       # 闸门**关着**
        sp_slow = SyncPanel("remote", sfs, PAL)
        sp_slow.cd("/tmp")
        pump(40)
        timer = threading.Timer(0.4, sfs.gate.set)
        timer.start()
        t0 = time.monotonic()
        sp_slow._on_mkdir()
        dt = time.monotonic() - t0
        timer.cancel()
        check("9 元断言：慢操作搬回 GUI 线程后同一个闸门把 GUI 线程挡住 0.4 秒，"
              "所以「没挡住 GUI 线程」不是恒真", dt > 0.3, "%.0f ms" % (dt * 1000))

        lfs = GateOpFS()
        lfs.gate.set()
        lp = sp.FilePanel("local", lfs, PAL)
        lp.cd(tempfile.gettempdir())
        lp._on_mkdir()
        wait_until(lambda: lfs.made)
        check("9 本地侧路径用 os.sep 拼",
              lfs.made == [os.path.join(lp.current_path(), "newdir")],
              str(lfs.made))
        reap(rp, sp_slow, lp)

        # 失败路径
        ffs = GateOpFS(exc=PermissionError("denied"))
        ffs.gate.set()
        ffp = sp.FilePanel("remote", ffs, PAL)
        ffp._flash = lambda m: None
        ffp.cd("/tmp")
        pump(40)
        ffp._on_mkdir()
        wait_until(lambda: ffp._op_worker is None)
        check("9 失败时弹的是中文框并带上原话",
              len(warns) == 1 and warns[0][0] == "新建目录失败"
              and "denied" in warns[0][1], str(warns))
        check("9 失败后忙时标记已清、按钮恢复",
              ffp._busy is False and ffp.mkdir_btn.isEnabled())
        check("9 失败后照样刷新列表", ffs.listdir_calls >= 1)
        reap(ffp)

        # 用户在对话框里取消：什么都不该发生
        nfs = GateOpFS()
        cfp = sp.FilePanel("remote", nfs, PAL)
        cfp.cd("/tmp")
        pump(40)
        answer["ok"] = False
        cfp._on_mkdir()
        pump(120)
        check("9 在对话框里取消就什么都不做",
              nfs.made == [] and cfp.count_label.text() != "处理中…", str(nfs.made))
        answer["ok"] = True
        # 目录名只有空白：同样什么都不做
        answer["name"] = "   "
        cfp._on_mkdir()
        pump(120)
        check("9 目录名全空白时不建目录", nfs.made == [], str(nfs.made))
        answer["name"] = "newdir"
        check("9 确实向用户问过目录名", bool(asks), str(asks))
        reap(cfp)
    finally:
        sp.msgbox.ask_text = real_ask
        sp.msgbox.warning = real_warn


# ===========================================================================
# 10. 删除
# ===========================================================================

def t_delete():
    section("10 删除")
    YES = QtWidgets.QMessageBox.StandardButton.Yes
    NO = QtWidgets.QMessageBox.StandardButton.No
    real_q, real_warn = sp.msgbox.question, sp.msgbox.warning
    warns = []
    sp.msgbox.warning = lambda parent, title, text, *a, **k: warns.append((title, text))
    try:
        fs = GateOpFS()
        fp = sp.FilePanel("remote", StaticFS([]), PAL)
        fp._fs = fs
        fp.cd("/tmp")
        pump(40)
        fp.populate_from([entry("a.txt", "/tmp/a.txt"),
                          entry("b.txt", "/tmp/b.txt"),
                          entry("d", "/tmp/d", True, mode=0o40755)])
        pump(40)
        fp.view.selectAll()
        pump(40)
        asked = []
        sp.msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), NO)[1]
        fp._on_delete()
        pump(120)
        check("10 确认框点「否」就一条都不删", fs.removed == [] and fs.rmdired == [],
              str(fs.removed))
        check("10 确认框问的是选中条数",
              bool(asked) and "3" in asked[0][1], str(asked))

        sp.msgbox.question = lambda p, t, x, *a, **k: YES
        fs.gate.set()
        fp._on_delete()
        wait_until(lambda: fp._op_worker is None)
        check("10 删除跑在工作线程上",
              fs.thread_name not in (None, threading.current_thread().name),
              str(fs.thread_name))
        check("10 文件走 remove、目录走 rmdir",
              sorted(fs.removed) == ["/tmp/a.txt", "/tmp/b.txt"]
              and fs.rmdired == ["/tmp/d"], "%s / %s" % (fs.removed, fs.rmdired))
        check("10 目录不递归：只调一次 rmdir，不逐个子项删",
              len(fs.rmdired) == 1)
        check("10 全成功时不弹框", not warns, str(warns))

        # 多选里一条失败
        class PartialFS(GateOpFS):
            def remove(self, path):
                GateOpFS.remove(self, path)
                if path.endswith("locked"):
                    raise PermissionError("directory not empty")

        pfs = PartialFS()
        pfs.gate.set()
        pfp = sp.FilePanel("remote", pfs, PAL)
        pfp._flash = lambda m: None
        pfp.cd("/tmp")
        pump(40)
        pfp.populate_from([entry("a.txt", "/tmp/a.txt"),
                           entry("locked", "/tmp/locked")])
        pump(40)
        pfp.view.selectAll()
        pump(40)
        warns.clear()
        pfp._on_delete()
        wait_until(lambda: pfp._op_worker is None)
        check("10 部分失败时报出的是那条失败的名字",
              len(warns) == 1 and "locked" in warns[0][1], str(warns))
        check("10 部分失败时成功那条照样删掉了",
              sorted(pfs.removed) == ["/tmp/a.txt", "/tmp/locked"], str(pfs.removed))
        check("10 部分失败后忙时标记已清", pfp._busy is False)
        reap(pfp)
    finally:
        sp.msgbox.question = real_q
        sp.msgbox.warning = real_warn
    reap(fp)


# ===========================================================================
# 11/12/13. 上传下载
# ===========================================================================

def t_upload_download():
    section("11 上传 / 下载")
    YES = QtWidgets.QMessageBox.StandardButton.Yes
    NO = QtWidgets.QMessageBox.StandardButton.No
    real_i, real_q = sp.msgbox.information, sp.msgbox.question
    infos, asked = [], []
    msgbox.information = lambda p, t, x, *a, **k: infos.append((t, x))
    msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), NO)[1]
    # 远端**本来就有** pick.txt —— 覆盖询问就是为这个场景存在的。
    # 早先这里远端是空的，探测自然问不出东西，判据红的是替身不是产品。
    sftp = PaneSftp(tree={"/home/u": {"r1.txt": b"OLD-R1",
                                      "pick.txt": b"OLD-PICK"}})
    panel, ctrl = build_panel(sftp)
    try:
        panel.up_btn.click()
        panel.down_btn.click()
        check("11 没选中就点上传：弹框说清要选什么",
              any(t == "上传" and "左侧" in x for t, x in infos), str(infos))
        check("11 没选中就点下载：弹框说清要选什么",
              any(t == "下载" and "右侧" in x for t, x in infos), str(infos))
        check("11 没选中时一次传输都没起", not ctrl.busy and sftp.puts == [])

        src = workdir("src")
        with open(os.path.join(src, "pick.txt"), "wb") as f:
            f.write(b"NEW-PICK")
        os.mkdir(os.path.join(src, "adir"))
        panel.local.cd(src)
        pump(60)
        panel.local.view.selectAll()
        pump(40)
        asked.clear()
        n = panel.upload_paths([os.path.join(src, "pick.txt")])
        check("11 upload_paths 对同名文件逐个问覆盖",
              n == 0 and len(asked) == 1 and "pick.txt" in asked[0][1],
              "jobs=%d 问=%s" % (n, asked))
        check("11 拒绝覆盖就不排任务，并说明为什么跳过",
              bool(infos) and "pick.txt（已存在，未覆盖）" in infos[-1][1],
              str(infos[-1:]))
        check("11 拒绝覆盖后远端内容保持原样",
              sftp.tree["/home/u"]["pick.txt"] == b"OLD-PICK",
              str(sftp.tree["/home/u"]["pick.txt"]))
        check("11 元断言：覆盖询问真被触发过（远端确有同名文件）",
              [c for c in sftp.calls if c[0] == "stat"], str(sftp.calls))

        infos.clear()
        sftp.puts.clear()
        n = panel.upload_paths([os.path.join(src, "adir")])
        check("11 upload_paths 跳过目录且不排任务",
              n == 0 and not sftp.puts, "jobs=%d puts=%s" % (n, sftp.puts))
        check("11 跳过目录时弹框说明「暂不支持递归传输」",
              bool(infos) and "adir" in infos[-1][1]
              and "递归" in infos[-1][1], str(infos[-1:]))

        msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), YES)[1]
        asked.clear()
        sftp.puts.clear()
        n = panel.upload_paths([os.path.join(src, "pick.txt")])
        check("11 同意覆盖后排进任务", n == 1, "jobs=%d" % n)
        wait_until(lambda: not ctrl.busy)
        pump(120)
        check("11 同意覆盖后远端内容真的被换成新的",
              sftp.tree["/home/u"]["pick.txt"] == b"NEW-PICK",
              str(sftp.tree["/home/u"].get("pick.txt")))

        dst = workdir("dst")
        n = panel.download_paths(["/home/u/r1.txt"], local_dir=dst)
        wait_until(lambda: not ctrl.busy)
        pump(120)
        check("11 download_paths 只取 basename 落到目标目录",
              sorted(os.listdir(dst)) == ["r1.txt"], str(os.listdir(dst)))
        with open(os.path.join(dst, "r1.txt"), "rb") as f:
            check("11 下载内容正确", f.read() == b"OLD-R1")

        infos.clear()
        asked.clear()
        sftp.gets.clear()
        n = panel.download_paths(["/home/u"], local_dir=dst)
        check("11 download_paths 跳过远程目录", n == 0 and not sftp.gets,
              "jobs=%d gets=%s" % (n, sftp.gets))
        check("11 跳过目录时告诉用户是哪些（只报名字，不报整条路径）",
              bool(infos) and "home/u" not in infos[-1][1]
              and "/" not in infos[-1][1].split("\n")[1],
              str(infos[-1:]))

        # 答案切回「否」，测本地同名碰撞
        msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), NO)[1]
        asked.clear()
        sftp.gets.clear()
        n = panel.download_paths(["/home/u/r1.txt"], local_dir=dst)
        check("11 download_paths 本地同名也要问覆盖",
              n == 0 and len(asked) == 1 and not sftp.gets,
              "jobs=%d 问=%s" % (n, asked))

        # 元断言：把 exists 探测摘掉（静默覆盖的旧行为），同一段代码就该
        # 一个问题都不问、并且照排任务 —— 证明上面那几条不是恒真。
        class SilentUpload(sp.SftpPanel):
            def _collect_jobs(self, entries, base_dir, fn, what, sep, exists=None):
                return sp.SftpPanel._collect_jobs(
                    self, entries, base_dir, fn, what, sep, exists=None)

        msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), NO)[1]
        sftp.puts.clear()
        asked.clear()
        infos.clear()
        silent = SilentUpload(sftp, PAL, None, flash=lambda m: None)
        silent.remote.cd("/home/u")
        wait_until(lambda: silent.remote.model.rowCount() >= 1)
        silent.local.cd(src)
        pump(60)
        silent.local.populate_from([entry("pick.txt", os.path.join(src, "pick.txt"))])
        silent.local.view.selectAll()
        pump(40)
        silent._upload()
        check("11 元断言：去掉同名探测后覆盖询问一次都不问",
              not asked, str(asked))
        # **put 跑在工作线程上**，直接读 puts 是竞态（早先两次连红就是这么来的）。
        #
        # 这里量的是**结果**而不是「第一个 put 的目标路径」。原文写的是
        # `puts[0][1] == "/home/u/pick.txt"` —— 那把旧实现（直传最终路径）
        # 写成了契约：上传现在先传同目录的临时件再改名，所以第一个 put 的
        # 目标**必然**不是最终名。它本意要证明的是「去掉同名探测后任务照样
        # 排出去，也就是会静默覆盖用户文件」—— 那个本意成立，而且应该
        # 直接量「远端那个文件真的被换掉了」。
        got = wait_until(lambda: len(sftp.puts) == 1, 3000)
        wait_until(lambda: not silent.ctrl.busy)
        pump(120)
        check("11 元断言：去掉同名探测后照样把任务排出去（= 会静默覆盖用户文件）",
              got and sftp.tree["/home/u"]["pick.txt"] == b"NEW-PICK",
              "排了 %d 个；远端 pick.txt=%r"
              % (len(sftp.puts), sftp.tree.get("/home/u", {}).get("pick.txt")))
        reap(silent)
    finally:
        msgbox.information = real_i
        msgbox.question = real_q
    reap(panel)
    return src, dst


def t_part_atomic():
    section("12 .part 原子下载")
    d = workdir("part")
    target = os.path.join(d, "payload.bin")
    with open(target, "wb") as f:
        f.write(b"ORIGINAL-CONTENT")

    release = threading.Event()
    seen = {}

    def hook(local):
        # **替身必须像真 `get` 那样先把文件建出来**，否则 `.part` 永远不存在，
        # 「下载中目标目录出现 <name>.part」判的就是个不存在的东西。
        with open(local, "wb") as f:
            f.write(b"PART!")
        seen["part"] = local
        seen["part_exists"] = os.path.exists(local)
        with open(target, "rb") as f:
            seen["target_content"] = f.read()
        release.wait(5)
        with open(local, "wb") as f:
            f.write(b"12345")

    # 远端文件必须**真的存在**：`download` 先 stat 拿大小，替身一抛
    # FileNotFoundError 就走不到 get，于是「中途」那一段永远观察不到。
    # 早先这里没建远端文件，判据红的是替身不是产品。
    sftp = PaneSftp(tree={"/home/u": {"payload.bin": b"12345"}})
    sftp.get_hook = hook
    rfs = sp.RemoteFileSystem(sftp)
    done = threading.Event()

    def run():
        rfs.download("/home/u/payload.bin", target)
        done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    # **等的是这一段最后落地的那个键，不是第一个。**
    #
    # `hook` 里 `seen["part_exists"]` 落在 1229 行、`seen["target_content"]`
    # 落在 1231 行，**中间隔着一次 `open()`**。原来的条件只看
    # `part_exists`，它一成立主线程就往下走 —— 机器繁忙时正好落在那两行
    # 之间，于是下一条判据读 `seen["target_content"]` 抛 `KeyError`，
    # **整个套件带着栈回溯崩掉**，`_runall` 只报一句 `exit=1`。
    # 实测：全量回归负载下 `_fsftp` 红过一次，日志尾部还停在一条 PASS 上，
    # 真正的红被这个栈吃掉了 —— 一个能诊断的失败变成了看不懂的崩溃。
    ok = wait_until(lambda: "part_exists" in seen and "target_content" in seen, 3000)
    check("12 下载过程中目标目录里出现 <name>.part",
          ok and seen["part"].endswith("payload.bin.part")
          and seen["part_exists"], str(seen.get("part")))
    # **用 `.get()`，不要用下标。** 前提没成立时这条要报「没拿到」，
    # 而不是让整个套件死在这里。
    check("12 下载过程中原来的目标文件一个字节都没被动",
          seen.get("target_content") == b"ORIGINAL-CONTENT",
          str(seen.get("target_content")))
    release.set()
    th.join(5)
    check("12 下载成功后目标文件是完整的",
          open(target, "rb").read() == b"12345", "未完成")
    check("12 下载成功后 .part 消失", not os.path.exists(target + ".part"))

    # 中途失败：临时件被删，原文件保持原内容
    target2 = os.path.join(d, "keep.bin")
    with open(target2, "wb") as f:
        f.write(b"ORIGINAL-CONTENT")
    boom = threading.Event()

    def hook2(local):
        boom.set()
        with open(local, "wb") as f:
            f.write(b"HALF")          # 半个文件已经落盘
        raise OSError("connection reset by peer")

    sftp2 = PaneSftp(tree={"/home/u": {"keep.bin": b"12345"}})
    sftp2.get_hook = hook2
    rfs2 = sp.RemoteFileSystem(sftp2)
    raised = None
    try:
        rfs2.download("/home/u/keep.bin", target2)
    except Exception as exc:           # noqa: BLE001
        raised = exc
    check("12 下载中途失败会把异常抛出去", isinstance(raised, OSError),
          type(raised).__name__ if raised else "没抛")
    check("12 下载中途失败后 .part 被删掉", not os.path.exists(target2 + ".part"))
    check("12 下载中途失败后目标文件仍是原来那份完整内容",
          open(target2, "rb").read() == b"ORIGINAL-CONTENT",
          str(open(target2, "rb").read()))

    # 元断言：改成直写目标（去掉 .part），原文件当场被毁
    class DirectDownload(sp.RemoteFileSystem):
        def download(self, remote, local, progress=None):
            with self._held():
                self._sftp.get(self._expand(remote), local)

    target3 = os.path.join(d, "naive.bin")
    with open(target3, "wb") as f:
        f.write(b"ORIGINAL-CONTENT")
    sftp3 = PaneSftp(tree={"/home/u": {"naive.bin": b"12345"}})
    sftp3.get_hook = hook2
    try:
        DirectDownload(sftp3).download("/home/u/naive.bin", target3)
    except Exception:                  # noqa: BLE001
        pass
    check("12 元断言：直写目标（无 .part）时原文件被半截内容覆盖，"
          "所以上面两条不是恒真",
          open(target3, "rb").read() != b"ORIGINAL-CONTENT",
          str(open(target3, "rb").read()))

    section("13 上传侧没有临时件")
    sftp4 = PaneSftp(tree={"/home/u": {"direct.bin": b""}})
    sp.RemoteFileSystem(sftp4).upload(target2, "/home/u/direct.bin")
    check("13 上传直传最终路径，不落 .part 临时件（当前设计的已知残留风险）",
          sftp4.puts and not any(".part" in r for _l, r in sftp4.puts),
          str([r for _l, r in sftp4.puts]))
    return d


# ===========================================================================
# 14. 传输状态条
# ===========================================================================

def t_strip():
    section("14 传输状态条")
    ctrl = TransferController(PAL, None)
    strip = ctrl.strip
    check("14 空闲时整条状态条是隐藏的", strip.isHidden(), strip.state)
    eng = Engine()
    ok = ctrl.begin("upload", "上传", eng, 3)
    pump(40)
    check("14 传输一开始状态条出现，标题是「正在上传 3 个文件」",
          ok and not strip.isHidden()
          and strip.title.text() == "正在上传 3 个文件", strip.title.text())
    check("14 首字节之前是不定态（不是卡在 0% 的确定态）",
          strip.bar.indeterminate)
    check("14 传输中给的是「取消」按钮", strip.action.text() == "取消",
          strip.action.text())

    ctrl.file_started(1, "b.txt")
    ctrl.advanced(2048, 4096, 1024)
    pump(40)
    check("14 多文件的小字是「2/3 · 文件名 · 已传/总量 · 速率」",
          strip.detail.text() == "2/3 · b.txt · 2.0 KB / 4.0 KB · 1.0 KB/s",
          strip.detail.text())
    check("14 收到真实进度后从不定态切成确定态", not strip.bar.indeterminate)

    ctrl.file_started(0, "tiny.txt")
    ctrl.advanced(1000, 1000)
    pump(40)
    check("14 整批百分比按文件个数等权（1/3 传完 = 33%，不是按字节）",
          abs(strip.bar.value - 1 / 3.0) < 0.01, "%.3f" % strip.bar.value)

    ctrl.advanced(0, 0)
    pump(40)
    check("14 总量未知时退回不定态而不是假装 0%",
          strip.bar.indeterminate, "value=%s" % strip.bar.value)

    ctrl.succeed("上传完成")
    pump(40)
    check("14 成功收尾是绿条 + 标题「上传完成」",
          strip.state == TransferStrip.SUCCESS
          and strip.bar._c0 == PAL.success
          and strip.title.text() == "上传完成",
          "%s %s" % (strip.state, strip.bar._c0))
    check("14 成功后按钮变成「关闭」", strip.action.text() == "关闭",
          strip.action.text())
    check("14 成功后短暂停留（此刻还看得见）", not strip.isHidden())
    check("14 成功停留到期后自己退场",
          wait_until(lambda: strip.isHidden(), SUCCESS_LINGER_MS + 1500))

    ctrl.begin("download", "下载", Engine(), 1)
    ctrl.fail("下载 x 失败：\nPermissionError: nope")
    pump(40)
    check("14 失败收尾是红条", strip.state == TransferStrip.ERROR
          and strip.bar._c0 == PAL.danger, "%s %s" % (strip.state, strip.bar._c0))
    check("14 失败原因留在气泡里（没人看见等于没报）",
          "PermissionError" in strip.toolTip(), strip.toolTip())
    check("14 失败的完整原因留在小字里",
          "PermissionError: nope" in strip.detail.text(), strip.detail.text())
    pump(SUCCESS_LINGER_MS + 400)
    check("14 失败不自动退场，等人处理", not strip.isHidden(),
          strip.state)
    check("14 失败之后闸门已释放", not ctrl.busy)
    ctrl.succeed("下载完成")
    pump(40)
    check("14 第一个终态说了算：随后的成功覆盖不了失败",
          strip.state == TransferStrip.ERROR, strip.state)

    # `cancel(note)` 只收 note，标题是控制器自己拼的 `f"{what}已取消"`
    ctrl.begin("upload", "上传", Engine(), 3)
    ctrl.cancel("还有 2 个文件没有传输")
    pump(40)
    check("14 取消收尾是灰条（取消是正常选择，不是出错）",
          strip.state == TransferStrip.CANCELLED
          and strip.bar._c0 == PAL.text_muted,
          "%s %s" % (strip.state, strip.bar._c0))
    check("14 取消的标题是「上传已取消」，不说成完成",
          strip.title.text() == "上传已取消", strip.title.text())
    check("14 取消时说清还有几个没传", "2 个文件没有传输" in strip.detail.text(),
          strip.detail.text())

    # 元断言：拆掉「第一个终态说了算」的守卫，失败会被后面的成功盖掉
    broken = NoGuardController(PAL, None)
    broken.begin("upload", "上传", Engine(), 1)
    broken.fail("上传 x 失败：\nOSError: boom")
    broken.succeed("上传完成")
    pump(40)
    check("14 元断言：守卫拆掉后失败态被成功盖掉，所以上一条不是恒真",
          broken.strip.state == TransferStrip.SUCCESS, broken.strip.state)
    reap(strip, broken.strip)


def t_transfer_lifecycle():
    section("14b 真实上传的状态条与等权进度")
    src = workdir("life")
    for n in ("f1.txt", "f2.txt", "f3.txt"):
        with open(os.path.join(src, n), "wb") as f:
            f.write(b"x" * 100)
    sftp = PaneSftp()
    gate = threading.Event()
    sftp.put_gate = gate
    panel, ctrl = build_panel(sftp, flash=lambda m: None)
    panel.local.cd(src)
    pump(60)
    panel.local.view.selectAll()
    pump(40)
    panel.up_btn.click()
    ok = wait_until(sftp.put_entered.is_set, 3000)
    pump(120)
    check("14b 点上传后状态条进入 running，标题带文件数",
          ok and panel.strip.state == TransferStrip.RUNNING
          and panel.strip.title.text() == "正在上传 3 个文件",
          panel.strip.title.text())
    check("14b 传输期「上传」「下载」两个按钮都禁用",
          not panel.up_btn.isEnabled() and not panel.down_btn.isEnabled())
    gate.set()
    wait_until(lambda: not ctrl.busy, 5000)
    pump(200)
    check("14b 传完后落成功态", panel.strip.state == TransferStrip.SUCCESS,
          panel.strip.state)
    check("14b 传完后按钮恢复可点",
          panel.up_btn.isEnabled() and panel.down_btn.isEnabled())
    check("14b 传完后三个文件都在远端",
          sorted(n for n in sftp.tree["/home/u"] if n.startswith("f"))
          == ["f1.txt", "f2.txt", "f3.txt"], str(sorted(sftp.tree["/home/u"])))
    check("14b 传完自动刷新目标侧，列表里能看见刚传的文件",
          all(n in names(panel.remote)
              for n in ("f1.txt", "f2.txt", "f3.txt")), str(names(panel.remote)))
    check("14b 传完的提示是「上传完成」",
          panel.strip.title.text() == "上传完成", panel.strip.title.text())
    reap(panel)
    return src


# ===========================================================================
# 15/16. 忙时闸门
# ===========================================================================

def t_gate():
    section("15 忙时闸门")
    started = []
    ctrl = TransferController(PAL, None)
    e1 = Engine(started, "e1")
    check("15 第一次 begin 被接受", ctrl.begin("upload", "上传", e1, 1) is True)
    check("15 begin 之后闸门是 busy", ctrl.busy)
    e2 = Engine(started, "e2")
    check("15 第二次 begin 返回 False（不排队）",
          ctrl.begin("download", "下载", e2, 1) is False)
    ctrl.strip.hide_and_notify()
    check("15 被拒的那次没启动引擎", started == [], str(started))
    ctrl.succeed("上传完成")
    pump(40)
    check("15 收尾之后闸门放掉", not ctrl.busy)
    check("15 收尾之后又能发起新传输",
          ctrl.begin("download", "下载", e2, 1) is True)
    ctrl.reset("测试")

    class BadEngine(Engine):
        def start(self):
            raise OSError("lrzsz is not executable")

    ctrl2 = TransferController(PAL, None)
    ok = ctrl2.begin("upload", "上传", BadEngine(), 1)
    started_ok = ctrl2.start_engine(ctrl2._adapter)
    pump(40)
    check("15 引擎启动失败不抛回调用方", started_ok is False and ok is True)
    check("15 引擎启动失败落的是失败态，不是永远 running",
          ctrl2.strip.state == TransferStrip.ERROR, ctrl2.strip.state)
    check("15 引擎启动失败后闸门释放（否则这条连接再也传不了）", not ctrl2.busy)
    check("15 启动失败的提示里有原话",
          "lrzsz is not executable" in ctrl2.strip.title.text()
          or "lrzsz is not executable" in ctrl2.strip.detail.text()
          or "lrzsz is not executable" in ctrl2.strip.toolTip(),
          ctrl2.strip.title.text() + "|" + ctrl2.strip.detail.text())

    ctrl3 = TransferController(PAL, None)
    ctrl3.begin("upload", "上传", Engine(), 2)
    check("15 断线前确实在忙", ctrl3.busy is True)
    ctrl3.reset("断线")
    pump(40)
    check("15 断线后闸门放掉", ctrl3.busy is False)
    check("15 断线后状态条退回空闲", ctrl3.strip.state == TransferStrip.IDLE,
          ctrl3.strip.state)
    check("15 断线之后同一个连接还能再发起传输",
          ctrl3.begin("download", "下载", Engine(), 1) is True)
    ctrl3.reset("收尾")
    reap(ctrl.strip, ctrl2.strip, ctrl3.strip)


def t_gate_reject():
    section("16 闸门拒绝必须出声")
    YES = QtWidgets.QMessageBox.StandardButton.Yes
    NO = QtWidgets.QMessageBox.StandardButton.No
    real_q = sp.msgbox.question
    sp.msgbox.question = lambda p, t, x, *a, **k: NO
    sftp = PaneSftp()
    panel, ctrl = build_panel(sftp, flash=lambda m: None)
    src = workdir("gate")
    with open(os.path.join(src, "g.txt"), "wb") as f:
        f.write(b"g")
    try:
        # 模拟工具栏的 ZMODEM 占着同一个闸门（SFTP 自己看不见这个忙）
        zmodem = Engine(name="zmodem")
        ctrl.begin("upload", "上传（ZMODEM）", zmodem, 1)
        panel.local.cd(src)
        pump(60)
        panel.local.view.selectAll()
        pump(40)
        flashes = []
        panel.set_flash(flashes.append)
        check("16 ZMODEM 占着闸门时上传按钮仍然是敞开的（闸门不归它管）",
              panel.up_btn.isEnabled())

        real_hook = sys.excepthook
        caught = []
        sys.excepthook = lambda et, ev, tb: caught.append(ev)
        try:
            panel.up_btn.click()
        finally:
            sys.excepthook = real_hook
        pump(120)
        check("16 闸门拒绝时不启动传输", panel._transfer is None and not sftp.puts,
              str(sftp.puts))
        check("16 闸门拒绝时状态栏出现提示", bool(flashes), str(flashes))
        check("16 闸门拒绝时不抛任何未处理异常（当前是 "
              "AttributeError: SftpPanel 没有 _flash）",
              not caught,
              "; ".join("%s: %s" % (type(e).__name__, e) for e in caught))
        check("16 闸门拒绝没有踩掉 ZMODEM 那条传输的状态条",
              panel.strip.state == TransferStrip.RUNNING, panel.strip.state)

        # 元断言：把缺失的 _flash 补上，同一条判据必须变绿 —— 说明红的成因
        # 是「提示通道没接」，不是判据写错
        panel._flash = flashes.append
        panel.up_btn.click()
        pump(120)
        check("16 元断言：补上缺失的 _flash 之后提示就出来了（本次上传未开始）",
              any("本次上传未开始" in m for m in flashes), str(flashes))
        check("16 元断言：补上 _flash 之后仍然不启动传输",
              not sftp.puts, str(sftp.puts))
        ctrl.reset("收尾")
    finally:
        sp.msgbox.question = real_q
    reap(panel)


# ===========================================================================
# 17/18. 传输期与析构
# ===========================================================================

def t_during_transfer():
    section("17 传输期")
    YES = QtWidgets.QMessageBox.StandardButton.Yes
    NO = QtWidgets.QMessageBox.StandardButton.No
    real_q = sp.msgbox.question
    sp.msgbox.question = lambda p, t, x, *a, **k: NO
    try:
        # 失败不刷新
        sftp = PaneSftp()
        sftp.put_exc = PermissionError("Permission denied")
        panel, ctrl = build_panel(sftp, flash=lambda m: None)
        src = workdir("fail")
        with open(os.path.join(src, "x.txt"), "wb") as f:
            f.write(b"1")
        panel.local.cd(src)
        pump(60)
        panel.local.view.selectAll()
        pump(40)
        before = panel.remote.model.rowCount()
        panel.up_btn.click()
        wait_until(lambda: not ctrl.busy, 5000)
        pump(200)
        check("17 传失败时状态条落红并报出原话",
              panel.strip.state == TransferStrip.ERROR
              and "Permission denied" in panel.strip.detail.text(),
              "%s|%s" % (panel.strip.state, panel.strip.detail.text()))
        check("17 失败不刷列表（没全成功就不动用户眼前的清单）",
              panel.remote.model.rowCount() == before,
              "%d → %d" % (before, panel.remote.model.rowCount()))
        check("17 失败之后按钮恢复可点（不会永远卡在禁用）",
              panel.up_btn.isEnabled() and panel.down_btn.isEnabled())
        reap(panel)

        # 传输期间切目录
        sftp2 = PaneSftp(tree={"/home/u": {"r1.txt": b"R1"},
                               "/tmp/elsewhere": {"marker.txt": b"M"}})
        sftp2.put_gate = threading.Event()
        panel2, ctrl2 = build_panel(sftp2, flash=lambda m: None)
        src2 = workdir("switch")
        for n in ("s1.txt", "s2.txt"):
            with open(os.path.join(src2, n), "wb") as f:
                f.write(b"z")
        panel2.local.cd(src2)
        pump(60)
        panel2.local.view.selectAll()
        pump(40)
        panel2.up_btn.click()
        ok = wait_until(sftp2.put_entered.is_set, 3000)
        check("17 传输跑起来了（前提）", ok and ctrl2.busy)

        # **传输期间远程目录切不过去，这是设计如此，不是缺陷。**
        # `RemoteFileSystem._held()`（sftp_panel.py:81-102）用一把无超时锁把
        # 所有远程访问串起来，upload 在**整个文件传输期间**持锁，于是刷新线程
        # 只能排队。可判定的是「不冻窗 + 老老实实说读取中」：GUI 线程必须
        # 立刻返回，列表在传输结束后自己补上。
        t0 = time.monotonic()
        panel2.remote.cd("/tmp/elsewhere")
        dt = time.monotonic() - t0
        check("17 传输期间切远程目录不冻住 GUI 线程（通道被持着就在工作线程上等）",
              dt < 0.15, "%.0f ms" % (dt * 1000))
        check("17 传输期间切远程目录如实显示「读取中…」，不装作切好了",
              panel2.remote.count_label.text() == "读取中…"
              and panel2.remote.current_path() == "/tmp/elsewhere",
              "%s %s" % (panel2.remote.count_label.text(),
                         panel2.remote.current_path()))
        # 本地侧不走那条锁，所以必须**立刻**换过去
        other_local = workdir("during")
        with open(os.path.join(other_local, "local-only.txt"), "wb") as f:
            f.write(b"L")
        t0 = time.monotonic()
        panel2.local.cd(other_local)
        dt_local = time.monotonic() - t0
        check("17 传输期间切本地目录立刻生效（本地盘不占那条通道）",
              names(panel2.local) == ["local-only.txt"] and dt_local < 0.15,
              "%.0f ms %s" % (dt_local * 1000, names(panel2.local)))

        sftp2.put_gate.set()
        wait_until(lambda: not ctrl2.busy, 5000)
        # 传完之后那个挂在通道上的刷新才回来
        check("17 传完之后通道一放，挂着的那次远程刷新自己补上",
              wait_until(lambda: names(panel2.remote) == ["marker.txt"], 4000),
              str(names(panel2.remote)))
        check("17 切过目录之后传完不覆盖新目录的列表",
              names(panel2.remote) == ["marker.txt"], str(names(panel2.remote)))
        check("17 切过目录之后新目录的计数文案也没被改",
              panel2.remote.count_label.text() == "0 个目录 · 1 个文件",
              panel2.remote.count_label.text())
        reap(panel2)

        # 元断言：把 busyChanged 掐掉（按钮不禁用），上面那条必须判不通过
        sftp3 = PaneSftp()
        sftp3.put_gate = threading.Event()
        panel3, ctrl3 = build_panel(sftp3, flash=lambda m: None)
        src3 = workdir("busybad")
        with open(os.path.join(src3, "b.txt"), "wb") as f:
            f.write(b"b")
        panel3.local.cd(src3)
        pump(60)
        panel3.local.view.selectAll()
        pump(40)
        panel3._set_busy = lambda busy: None       # 缺陷版：什么都不禁
        panel3.up_btn.click()
        wait_until(sftp3.put_entered.is_set, 3000)
        check("17 元断言：忙时不禁用按钮的话上传按钮仍可点，"
              "所以「传输期禁用」这条不是恒真",
              panel3.up_btn.isEnabled())
        sftp3.put_gate.set()
        wait_until(lambda: not ctrl3.busy, 5000)
        reap(panel3)
    finally:
        sp.msgbox.question = real_q


def t_destroy_during_transfer():
    section("18 传输途中面板被析构")
    sftp = PaneSftp()
    sftp.put_gate = threading.Event()
    host = QtWidgets.QWidget()
    ctrl = TransferController(PAL, host)          # 会话级，比面板活得久
    panel = sp.SftpPanel(sftp, PAL, host, ctrl=ctrl, flash=lambda m: None)
    src = workdir("destroy")
    with open(os.path.join(src, "d.txt"), "wb") as f:
        f.write(b"d")
    panel.local.cd(src)
    pump(60)
    panel.local.view.selectAll()
    pump(40)
    caught = []
    real_excepthook, real_threadhook = sys.excepthook, threading.excepthook
    sys.excepthook = lambda et, ev, tb: caught.append("slot:" + type(ev).__name__)
    threading.excepthook = lambda a: caught.append("thread:" + a.exc_type.__name__)
    try:
        panel.up_btn.click()
        ok = wait_until(sftp.put_entered.is_set, 3000)
        check("18 传输起来了（前提）", ok and ctrl.busy)
        panel.close()
        panel.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        sftp.put_gate.set()
        wait_until(lambda: len(sftp.puts) >= 1, 4000)
        pump(500)
        # 进程还活着吗：再建一个面板、刷一次列表。字面写 True 的判据
        # 等于没判，所以这里真的去干一遍。
        probe = sp.FilePanel("remote", StaticFS([entry("later.txt", "/tmp/later.txt")]),
                              PAL)
        probe.cd("/tmp")
        alive = wait_until(lambda: probe.model.rowCount() == 1, 3000)
        check("18 面板析构 + 传输收尾之后还能新建面板并刷出列表（进程没被带崩）",
              alive and probe.count_label.text() == "0 个目录 · 1 个文件",
              probe.count_label.text())
        reap(probe)
        check("18 面板在传输途中被析构不产生任何未处理异常", not caught,
              "; ".join(caught))
        # 会话级资源不该被标签页带走：同一个会话之后还能不能再传一次
        reuse_ok, detail = True, ""
        try:
            ctrl.begin("upload", "上传", Engine(), 1)
            ctrl.succeed("上传完成")
        except RuntimeError as exc:
            reuse_ok, detail = False, str(exc)
        check("18 关掉文件面板后，同一个会话还能再发起传输"
              "（会话级资源没被标签页带走）", reuse_ok, detail)
    finally:
        sys.excepthook, threading.excepthook = real_excepthook, real_threadhook

    # 真实路径：用户是关掉**整个会话标签**（Ctrl+W），不是只关文件面板。
    # host 在这里就是 SessionTab 的替身，transfer_ctrl 和 strip 都挂在它上面。
    sftp2 = PaneSftp()
    sftp2.put_gate = threading.Event()
    host2 = QtWidgets.QWidget()
    ctrl2 = TransferController(PAL, host2)
    panel2 = sp.SftpPanel(sftp2, PAL, host2, ctrl=ctrl2, flash=lambda m: None)
    src2 = workdir("destroy2")
    with open(os.path.join(src2, "d2.txt"), "wb") as f:
        f.write(b"d")
    panel2.local.cd(src2)
    pump(60)
    panel2.local.view.selectAll()
    pump(40)
    caught2 = []
    sys.excepthook = lambda et, ev, tb: caught2.append(type(ev).__name__)
    threading.excepthook = lambda a: caught2.append("thread:" + a.exc_type.__name__)
    try:
        panel2.up_btn.click()
        wait_until(sftp2.put_entered.is_set, 3000)
        host2.close()
        host2.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
        sftp2.put_gate.set()
        pump(700)
        check("18 传输途中关掉整个会话标签不产生未处理异常", not caught2,
              "; ".join(caught2))
        again = sp.FilePanel("remote", StaticFS([]), PAL)
        again.cd("/tmp")
        # 远程侧是异步的：必须等那次 listdir 回来，不能只看标签的初值。
        check("18 关掉整个会话标签之后进程还活着（能继续建面板刷新）",
              wait_until(lambda: again.count_label.text() == "0 个目录 · 0 个文件",
                         2000),
              again.count_label.text())
        reap(again)
    finally:
        sys.excepthook, threading.excepthook = real_excepthook, real_threadhook
    reap(host)


def t_strip_ownership():
    section("18b 状态条归属")
    sftp = PaneSftp()
    host = QtWidgets.QWidget()
    ctrl = TransferController(PAL, host)
    strip_row = QtWidgets.QHBoxLayout()
    outer = QtWidgets.QVBoxLayout(host)
    tabs = QtWidgets.QTabWidget()
    outer.addWidget(tabs, 1)
    strip_row.addWidget(ctrl.strip)
    outer.addLayout(strip_row)
    panel = sp.SftpPanel(sftp, PAL, host, ctrl=ctrl, flash=lambda m: None)
    tabs.addTab(panel, "文件")
    tabs.addTab(QtWidgets.QLabel("终端"), "终端")
    tabs.setCurrentIndex(1)                       # 切到终端页
    ctrl.begin("upload", "上传", Engine(), 1)      # ZMODEM 在终端页跑传输
    pump(60)
    check("18b 会话级状态条要在终端页也看得见（ZMODEM 跑在这一页）",
          ctrl.strip.isVisibleTo(host), ctrl.strip.state)
    alive = True
    try:
        _ = ctrl.strip.title.text()
    except RuntimeError:
        alive = False
    check("18b 状态条还是活着的控件", alive)
    # 补一条元断言：状态条的父级必须还是会话窗口。把 SftpPanel 的那行
    # addWidget 装回去（缺陷版）时，`strip.parent()` 立刻变成面板自己。
    check("18b 元断言：状态条的父级没有被文件面板抢走（QLayout::addWidget 会 "
          "reparent）", ctrl.strip.parentWidget() is host,
          type(ctrl.strip.parentWidget()).__name__ if ctrl.strip.parentWidget()
          else "None")
    ctrl.reset("收尾")
    reap(host)


# ===========================================================================
# 19. 锁超时必须管住「拿到锁之后的那一次往返」
# ===========================================================================

class _Gate:
    """可随时开关的闸门。`ok` 为假时进来的调用全部卡在 `ev` 上。

    用可开关的而不是固定 `Event`：一条判据要先量「通道快」，再量「通道慢」，
    中间不能重建被测对象 —— 重建了就量不到「同一条锁上先成功后超时」。
    ``ok`` 做成属性是为了避免 `ok=False` 与 `ev` 不同步那种半开状态。
    """

    def __init__(self, ok=True):
        self.ev = threading.Event()
        self.ok = ok

    @property
    def ok(self):
        return self._ok

    @ok.setter
    def ok(self, value):
        self._ok = value
        if value:
            self.ev.set()
        else:
            self.ev.clear()

    def wait(self, timeout=None):
        if not self.ok:
            self.ev.wait(timeout)


class SlowIORemote:
    """`PaneSftp` 外面套一层，把 `stat` / `listdir_attr` 卡在闸门上。

    **替身必须是同步的。** 慢的就是它自己 —— 一旦挪到工作线程上，测到的
    就不是「调用方等了多久」而是「工作线程睡了多久」，判据会恒绿。
    """

    def __init__(self, inner, gate):
        self.inner = inner
        self.gate = gate
        self.calls = []
        self.entered = 0

    def stat(self, path):
        self.calls.append(("stat", path))
        self.entered += 1
        self.gate.wait(8)
        return self.inner.stat(path)

    def listdir_attr(self, path):
        self.calls.append(("listdir_attr", path))
        self.entered += 1
        self.gate.wait(8)
        return self.inner.listdir_attr(path)

    def normalize(self, path):
        return self.inner.normalize(path)

    def listdir(self, path):
        return self.inner.listdir(path)

    def get(self, *a, **k):
        return self.inner.get(*a, **k)

    def put(self, *a, **k):
        return self.inner.put(*a, **k)


def t_probe_bound():
    section("19 锁超时也管住 I/O")
    inner = PaneSftp(tree={"/home/u": {}, "/tmp": {"f.txt": b"F"}})
    gate = _Gate(ok=True)
    rfs = sp.RemoteFileSystem(SlowIORemote(inner, gate))

    # -- 通道快的时候：问得到就如实答 -------------------------------------
    check("19 通道通畅时 is_dir 如实答 True（目录）",
          rfs.is_dir("/home/u") is True)
    check("19 通道通畅时 is_dir 如实答 False（文件）",
          rfs.is_dir("/tmp/f.txt") is False)
    check("19 通道通畅时 list_names 拿得到条目",
          rfs.list_names("/tmp") == ["f.txt"], str(rfs.list_names("/tmp")))
    check("19 对照组：通道是通的（不然下面全是恒红）", gate.ok)

    # -- 通道慢的时候：往返本身也必须有上限 ------------------------------
    # 闸门卡住时每个 I/O 最多 8 秒。旧实现（`_held(timeout=0.25)`）量到的是
    # **整个 8 秒**——抢锁那 0.25 秒守住了，抢到之后没有任何上限。
    gate.ok = False
    t0 = time.monotonic()
    raised = None
    try:
        rfs.is_dir("/home/u")
    except BaseException as exc:                  # noqa: BLE001
        raised = exc
    dt = time.monotonic() - t0
    check("19 拿到锁之后的那一次往返也有上限（慢 SFTP 不再冻 GUI 线程）",
          dt < 1.0, "%.0f ms（卡住的闸门最多等 8 秒）" % (dt * 1000))
    check("19 超时被如实告诉调用方，而不是悄悄当成「不是目录」",
          isinstance(raised, TimeoutError),
          "%s: %s" % (type(raised).__name__, raised))

    t0 = time.monotonic()
    raised2 = None
    try:
        rfs.list_names("/tmp")
    except BaseException as exc:                  # noqa: BLE001
        raised2 = exc
    dt2 = time.monotonic() - t0
    check("19 Tab 补全用的 list_names 同样有上限（不会被一次慢 stat 拖死）",
          dt2 < 1.0 and isinstance(raised2, TimeoutError),
          "%.0f ms %s" % (dt2 * 1000, type(raised2).__name__))

    # -- 负向对照：把上限放大，旧行为必须复现 ----------------------------
    # 这一条量的是「上限真的在起作用」，而不是「函数恰好很快」。
    slow_probe = sp.RemoteFileSystem(SlowIORemote(inner, gate))
    slow_probe._PROBE_TIMEOUT = 2.0                # 缺陷版：把上限撤掉
    t0 = time.monotonic()
    try:
        slow_probe.is_dir("/home/u")
    except BaseException:                          # noqa: BLE001
        pass
    dt_bad = time.monotonic() - t0
    check("19 元断言：把上限放大后同一次调用就变成实打实的等待，"
          "所以上面那两条不是恒绿", dt_bad >= 1.0, "%.0f ms" % (dt_bad * 1000))

    # -- 超时之后锁不能被永久占死 ----------------------------------------
    gate.ok = True
    # **谓词必须自己吃掉 TimeoutError。** `gate.ok = True` 之后，之前那个卡在
    # gate 上、已经被放弃的探测线程还要一会儿才收工并复位闸门，而 `_probe`
    # 的语义是「上一次探测还没回来就**立刻**拒答」（0ms 瞬时拒答，那是设计）。
    # 于是恢复后的**第一次**调用抛 TimeoutError —— 而 `wait_until` 的谓词
    # 一抛就冒泡，整套判据当场崩在别人看不见的地方。
    # 正确写法是把它当「还没到」继续轮询，不是把断言改软。
    def _recovered():
        try:
            return rfs.is_dir("/home/u") is True
        except TimeoutError:
            return False
    alive = wait_until(_recovered, 6000)
    check("19 通道恢复之后同一个 RemoteFileSystem 又问得到（放弃的探测没把"
          "通道永久锁死）", alive)

    # -- 用户看得见的后果：目录不能被当成文件排进下载队列 ----------------
    # `download_paths` 正是靠 `is_dir` 决定「跳过目录」，而它跑在 GUI 线程上。
    infos, flashes = [], []
    real_i = sp.msgbox.information
    sp.msgbox.information = lambda p, t, x, *a, **k: infos.append((t, x))
    slow_sftp = PaneSftp(tree={"/home/u": {"r1.txt": b"R1"}, "/busy/dir": {}})
    busy_gate = _Gate(ok=True)
    panel, ctrl = build_panel(SlowIORemote(slow_sftp, busy_gate),
                              flash=flashes.append)
    try:
        wait_until(lambda: panel.remote.count_label.text() != "读取中…", 3000)
        dst = workdir("probe")
        busy_gate.ok = False
        t0 = time.monotonic()
        n = panel.download_paths(["/busy/dir"], local_dir=dst)
        dt3 = time.monotonic() - t0
        check("19 通道忙时 download_paths 立刻返回，不把 GUI 线程吊在往返上",
              dt3 < 1.0, "%.0f ms" % (dt3 * 1000))
        check("19 通道忙时远程目录不会被当成文件排进下载队列",
              n == 0 and not [g for g in slow_sftp.gets]
              and os.listdir(dst) == [], "jobs=%d gets=%s" % (n, slow_sftp.gets))
        told = ("\n".join(t for _tt, t in infos)
                + "|".join(flashes))
        check("19 「没能确认」这件事必须说出来，不能默默不传",
              "确认" in told and "dir" in told, told[:200])
        busy_gate.ok = True
        n2 = wait_until(lambda: panel.download_paths(["/home/u/r1.txt"],
                                                     local_dir=dst) == 1, 6000)
        wait_until(lambda: not ctrl.busy, 4000)
        pump(120)
        check("19 通道恢复之后同一个面板又能正常下载",
              n2 and os.listdir(dst) == ["r1.txt"], str(os.listdir(dst)))
    finally:
        sp.msgbox.information = real_i
        busy_gate.ok = True
        reap(panel)


# ===========================================================================
# 20. Delete 键
# ===========================================================================

def t_delete_key():
    section("20 Delete 键")
    YES = QtWidgets.QMessageBox.StandardButton.Yes
    NO = QtWidgets.QMessageBox.StandardButton.No
    real_q = sp.msgbox.question
    asked = []
    sp.msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), NO)[1]
    d = workdir("delkey")
    for n in ("a.txt", "b.txt"):
        with open(os.path.join(d, n), "wb") as f:
            f.write(b"x")
    os.mkdir(os.path.join(d, "sub"))
    fp = sp.FilePanel("local", sp.LocalFileSystem(), PAL)
    fp._flash = lambda m: None
    fp.cd(d)
    pump(60)
    try:
        # 前提：两个文件 + 一个目录都真的在盘上
        check("20 前提：列表里选中了 a.txt",
              _select_row(fp, "a.txt"), str(names(fp)))
        QtTest.QTest.keyClick(fp.view, QtCore.Qt.Key.Key_Delete)
        pump(120)
        check("20 按 Delete 会先弹确认框（和「删除所选」按钮同一条路）",
              bool(asked) and "1" in asked[0][1], str(asked))
        check("20 确认框点「否」就一条都不删",
              sorted(os.listdir(d)) == ["a.txt", "b.txt", "sub"],
              str(sorted(os.listdir(d))))

        asked.clear()
        sp.msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), YES)[1]
        QtTest.QTest.keyClick(fp.view, QtCore.Qt.Key.Key_Delete)
        ok = wait_until(lambda: not os.path.exists(os.path.join(d, "a.txt")), 4000)
        pump(120)
        check("20 确认后按 Delete 真把选中的删掉了", ok,
              str(sorted(os.listdir(d))))
        check("20 只删选中的那条，另外两个原样留着",
              sorted(os.listdir(d)) == ["b.txt", "sub"],
              str(sorted(os.listdir(d))))
        check("20 删除跑在工作线程上（不占 GUI 线程）", fp._op_worker is None)

        # 没有选中：不能删，也不该被吞成一个什么都看不见的动作
        asked.clear()
        fp.refresh()                              # 重新列一遍 → 选中被清空
        pump(60)
        check("20 前提：重新列过目录之后确实没有选中",
              not fp.view.selectionModel().hasSelection())
        before = sorted(os.listdir(d))
        QtTest.QTest.keyClick(fp.view, QtCore.Qt.Key.Key_Delete)
        pump(120)
        check("20 没有选中时按 Delete 不删任何东西，也不弹框",
              sorted(os.listdir(d)) == before and not asked,
              "%s %s" % (sorted(os.listdir(d)), asked))

        # Delete 不许顺手变成「就地改名」
        check("20 前提：b.txt 重新选中成功", _select_row(fp, "b.txt"),
              str(names(fp)))
        pump(40)
        editing = fp.view.state()
        editors = fp.view.findChildren(QtWidgets.QLineEdit)
        check("20 Delete 没有顺手进「就地改名」（也不弹编辑框）",
              editing != QtWidgets.QAbstractItemView.State.EditingState
              and not editors
              and fp.view.editTriggers()
              == QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers,
              "state=%s editors=%d" % (editing.name, len(editors)))
        sp.msgbox.question = lambda p, t, x, *a, **k: (asked.append((t, x)), NO)[1]
        asked.clear()
        QtTest.QTest.keyClick(fp.view, QtCore.Qt.Key.Key_Delete)
        pump(120)
        check("20 「就地改名」那条不成立：双击/编辑仍然进不去，"
              "Delete 只干删除这一件事",
              bool(asked) and asked[0][0] == "删除", str(asked))

        # 焦点不在文件列表上时，Delete 归别人 —— 不能绑成窗口级快捷键
        asked.clear()
        sp.msgbox.question = lambda p, t, x, *a, **k: YES
        fp.path_edit.setText(d)
        QtTest.QTest.keyClick(fp.path_edit, QtCore.Qt.Key.Key_Delete)
        pump(120)
        check("20 焦点在路径框时按 Delete 不删文件（Delete 只在文件列表上生效）",
              sorted(os.listdir(d)) == ["b.txt", "sub"] and not asked,
              "%s %s" % (sorted(os.listdir(d)), asked))
    finally:
        sp.msgbox.question = real_q
        reap(fp)


def _select_row(panel, name):
    """把 *name* 那一行设成当前选中，返回是否真选中了。"""
    for r in range(panel.model.rowCount()):
        if panel.model.item(r, 0).text() == name:
            panel.view.selectionModel().select(
                panel.model.index(r, 0),
                QtCore.QItemSelectionModel.SelectionFlag.Select
                | QtCore.QItemSelectionModel.SelectionFlag.Rows)
            panel.view.setCurrentIndex(panel.model.index(r, 0))
            return True
    return False


# ===========================================================================
# 21. 同名探测问不到时不当「没有」
# ===========================================================================

def t_exists_unknown():
    section("21 同名探测问不到时不当「没有」")
    # 背景：`upload_paths` 的覆盖询问靠 `_remote_exists`。它原来把**任何**
    # 异常一律当「没有」—— 于是「通道正忙」被读成「可以安全覆盖」，
    # 一次探测失败就是一次静默覆盖，而状态条照报「上传完成」。
    # 这一组钉的是三态：True / False / None（问不到），且**只有
    # FileNotFoundError 才等于 False**。
    panel = None
    tmp = tempfile.mkdtemp(prefix="fsftp-exists-")
    real_q = sp.msgbox.question
    # 三态本身。`_remote_exists` 只用到 `self._rfs`，所以给一个最小替身就够 ——
    # 不必为了调一个纯判断去构造整个 SftpPanel。
    # **不要用「循环变量 + 闭包」来造这些替身。** 四个替身各自显式定义：
    # 少一层间接，出问题时 detail 直接指向是哪个，不是「实测 None 期望 False」
    # 让人猜。
    class _Holder:
        def __init__(self, rfs):
            self._rfs = rfs

    class _RfsMissing:
        _probe = staticmethod(lambda fn, what: fn())

        @staticmethod
        def stat(path):
            raise FileNotFoundError("no such file")

    class _RfsHas:
        _probe = staticmethod(lambda fn, what: fn())

        @staticmethod
        def stat(path):
            return object()

    class _RfsBusy:
        _probe = staticmethod(lambda fn, what: fn())

        @staticmethod
        def stat(path):
            raise TimeoutError("SFTP 通道忙")

    class _RfsDenied:
        _probe = staticmethod(lambda fn, what: fn())

        @staticmethod
        def stat(path):
            raise PermissionError("denied")

    for label, rfs, want in (
        ("远端确实没有（FileNotFoundError）= 不问覆盖", _RfsMissing(), False),
        ("远端确实有（stat 正常返回）= 要问覆盖", _RfsHas(), True),
        ("通道正忙（TimeoutError）= 问不到，不能当没有", _RfsBusy(), None),
        ("权限不足（PermissionError）= 问不到，不能当没有", _RfsDenied(), None),
    ):
        got = sp.SftpPanel._remote_exists(_Holder(rfs), "/tmp/x")
        check("21 %s" % label, got is want,
              "实测 %r（%s），期望 %r"
              % (got, type(rfs).__name__, want))

    # 端到端：问不到时**不排进队列**，并出现在告知里。
    # `upload_paths` 只用到 `_rfs` / `remote.current_path()` /
    # `_start_transfers` / `_report_skipped`，所以给一个最小替身就够，
    # 不必去构造真的面板（那会顺带去 cd 真实目录）。
    jobs_seen, told = [], []

    class _Remote:
        def current_path(self):
            return "/home/u"

    class _FakePanel:
        _rfs = _TimeoutRfs()
        remote = _Remote()

        # 必须走普通方法转发：`_remote_exists` 是 `SftpPanel` 的实例方法，
        # 挂成 staticmethod 会把 `self` 丢掉。
        def _remote_exists(self, path):
            return sp.SftpPanel._remote_exists(self, path)

        @staticmethod
        def _start_transfers(jobs, *a, **k):
            jobs_seen.append(jobs)

        @staticmethod
        def _report_skipped(skipped, what, undecided=()):
            told.extend(skipped)

    src = os.path.join(tmp, "big.bin")
    with open(src, "wb") as f:
        f.write(b"x" * 32)
    n = sp.SftpPanel.upload_paths(_FakePanel(), [src])
    check("21 远端问不到时这一个文件没有进传输队列",
          jobs_seen == [] and n == 0,
          "排了 %d 个、返回 %r" % (len(jobs_seen), n))
    check("21 而且这一次真的说出来了（不是静默跳过）",
          any("没能确认" in s or "未传输" in s for s in told),
          str(told))

    # Delete 与「就地改名」共用一个键的前提。用判据钉住，而不是在产品代码里
    # 写 assert —— assert 在 `python -O` 下会被剥掉，而这条是**行为契约**。
    _probe_panel = sp.FilePanel("remote", _TimeoutRfs(), PAL)
    try:
        check("21 Delete 键的前提仍然成立：文件树是 NoEditTriggers"
              "（放开它之前必须先把 Delete 从 Qt 的编辑键里摘掉，否则两边打架）",
              _probe_panel.view.editTriggers()
              == QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers,
              "editTriggers=%r" % _probe_panel.view.editTriggers())
    finally:
        _probe_panel.deleteLater()
    shutil.rmtree(tmp, ignore_errors=True)


# ===========================================================================
# 22. 上传覆盖失败不得毁掉远端已有的完好文件
# ===========================================================================

def t_upload_atomic():
    section("22 上传走远端临时件")
    # 背景（4 位评审一致确认，且报告第七章给的修法被实测证伪）：
    # paramiko 的 `put` 内部是 `file(remote, "wb")` —— **一打开就截断目标**。
    # 所以「上传覆盖」传到一半断网时，远端那个**原本完好的文件被毁掉了**，
    # 而状态条只报一句「上传失败」。
    # 「本地先写临时件再 put」**解决不了这个症状**（它改变不了 paramiko 打开
    # 远端那一行），所以临时件必须在远端，且必须与目标同目录。
    d = workdir("upatomic")
    src = os.path.join(d, "deploy.sh")
    with open(src, "wb") as f:
        f.write(b"NEW-CONTENT")

    # --- 成功覆盖：最终路径是新内容，临时件消失 -----------------------
    sftp = PaneSftp(tree={"/home/u": {"deploy.sh": b"OLD-GOOD-CONTENT"}})
    rfs = sp.RemoteFileSystem(sftp)
    rfs.upload(src, "/home/u/deploy.sh")
    after = sftp.tree["/home/u"]
    check("22 上传覆盖成功：最终路径是新内容",
          after.get("deploy.sh") == b"NEW-CONTENT", repr(after.get("deploy.sh")))
    check("22 上传覆盖成功：远端不留临时件",
          [n for n in after if "mavis-part" in n] == [], repr(list(after)))
    tmp_names = [p for c in sftp.calls if c[0] == "put" for p in [c[-1]]]
    check("22 第一个 put 的目标是同目录的临时件，不是最终路径",
          bool(tmp_names) and tmp_names[0] != "/home/u/deploy.sh"
          and tmp_names[0].startswith("/home/u/"),
          "put 了 %r" % (tmp_names,))
    check("22 临时件与目标同目录（跨文件系统的 rename 会失败，EXDEV）",
          bool(tmp_names) and tmp_names[0].rsplit("/", 1)[0]
          == "/home/u/deploy.sh".rsplit("/", 1)[0],
          "临时件 %r" % (tmp_names[0] if tmp_names else None,))
    check("22 覆盖走的是「posix_rename 优先」那条",
          any(c[0] == "posix_rename" for c in sftp.calls),
          "calls=%r" % (sftp.calls,))

    # --- 失败：远端原有的完好文件必须**一点没动** -------------------
    boom = RuntimeError("断网")
    sftp2 = PaneSftp(tree={"/home/u": {"deploy.sh": b"OLD-GOOD-CONTENT"}})
    sftp2.put_exc = boom
    rfs2 = sp.RemoteFileSystem(sftp2)
    raised = None
    try:
        rfs2.upload(src, "/home/u/deploy.sh")
    except BaseException as exc:                  # noqa: BLE001
        raised = exc
    check("22 上传失败时异常照原样抛出去（不被清理盖掉）",
          raised is boom, "%r" % (raised,))
    check("22 **上传覆盖失败：远端原有的完好文件没有被截断**",
          sftp2.tree["/home/u"].get("deploy.sh") == b"OLD-GOOD-CONTENT",
          "实测 %r（直传的话这里是空的或半截）"
          % (sftp2.tree["/home/u"].get("deploy.sh"),))
    check("22 上传失败时远端不留半截临时件",
          [n for n in sftp2.tree["/home/u"] if "mavis-part" in n] == [],
          repr(list(sftp2.tree["/home/u"])))
    check("22 上传失败时确实尝试清过临时件",
          any(c[0] == "remove" for c in sftp2.calls),
          "calls=%r" % (sftp2.calls,))

    # --- 服务端不支持 posix_rename 时退到标准 rename -----------------
    # 真实服务端里最小实现就不支持这个 v3 扩展，退路必须真的走通。
    class _NoPosix(PaneSftp):
        def posix_rename(self, src, dst):
            # **要留下痕迹**（记进 calls 再抛），否则客户端「有没有先试过
            # posix_rename」这件事根本无从观测 —— 判据量的是产品有没有
            # 按顺序退，而 `calls` 是唯一的证据。
            self.calls.append(("posix_rename", src, dst))
            raise OSError("unsupported 200")

    sftp3 = _NoPosix(tree={"/home/u": {"deploy.sh": b"OLD-GOOD-CONTENT"}})
    rfs3 = sp.RemoteFileSystem(sftp3)
    rfs3.upload(src, "/home/u/deploy.sh")
    check("22 服务端不支持 posix_rename 时退到标准 rename 仍然成功",
          sftp3.tree["/home/u"].get("deploy.sh") == b"NEW-CONTENT",
          repr(sftp3.tree["/home/u"]))
    check("22 退路确实先试了 posix_rename 再用标准 rename",
          any(c[0] == "posix_rename" for c in sftp3.calls)
          and any(c[0] == "rename" for c in sftp3.calls),
          "calls=%r" % (sftp3.calls,))

    # --- 元断言：装回直传版，上面那条必须变红 ------------------------
    # 不装回缺陷版，这几条绿和一个探针写坏的脚本在输出里长得一模一样。
    real_upload = sp.RemoteFileSystem.upload

    def _direct_upload(self, local, remote, progress=None):
        size = os.path.getsize(local)
        with self._held():
            self._sftp.put(local, self._expand(remote))
        if progress:
            progress(size, size)

    sftp4 = PaneSftp(tree={"/home/u": {"deploy.sh": b"OLD-GOOD-CONTENT"}})
    sftp4.put_exc = boom
    sp.RemoteFileSystem.upload = _direct_upload
    try:
        rfs4 = sp.RemoteFileSystem(sftp4)
        try:
            rfs4.upload(src, "/home/u/deploy.sh")
        except BaseException:                    # noqa: BLE001
            pass
    finally:
        sp.RemoteFileSystem.upload = real_upload
    check("22 元断言：装回直传的缺陷版之后，远端那份完好文件**真的**会被毁掉"
          "（所以上面那条不是恒真）",
          sftp4.tree["/home/u"].get("deploy.sh") != b"OLD-GOOD-CONTENT",
          "缺陷版实测 %r"
          % (sftp4.tree["/home/u"].get("deploy.sh"),))


class _StuckCleanupSftp(PaneSftp):
    """put 必失败，且 `remove` 在网络上吊住 —— 通道半死的样子。

    评审实测：改之前 `upload()` 在这一档上 6 秒不返回、`raise` 从未执行、
    调用方一个异常都拿不到 —— 状态条永远停在 running，用户看不到任何报错。
    """

    def remove(self, path):
        self.calls.append(("remove", path))
        _HANG[0].set()
        _HANG[1].wait(10)          # 永远等不到（测试结束前会放行）
        super().remove(path)


_HANG = [threading.Event(), threading.Event()]


def t_cleanup_bounded():
    print("\n=== 23. 失败清理有上限：它没资格堵住它本该汇报的错误 ===")
    # 报告留下的两条之一：`upload()` 的 `except` 分支走的是**无上限**的
    # `with self._held():`，而 `sftp.remove()` 是一趟没有上限的网络往返 ——
    # 通道半死时它自己吊住，于是下一行的 `raise` 永远不执行。
    #
    # 关键点：**要限的不是抢锁**。`with` 在 `try` 内，异常先出 `with`（锁已
    # 释放）才进 `except`，所以正常失败路径上清理时锁本来就是空的（评审实测）。
    # 只给抢锁加超时的话，`remove` 那一趟照样吊住，症状一模一样。
    #
    # **症状要说准**：改之前不是「异常永远不抛」，而是**被清理无界推迟** ——
    # 清理多久回来，错误就多久不报（探针里 remove 阻塞 10 秒，错误就 10 秒后
    # 才出现；真通道半死时那个「多久」没有上界）。用户看到的是状态条一直停在
    # running、面板不刷新、点取消也没反应。
    src = os.path.join(TMP_DIRS[0], "stuck.txt")
    with open(src, "w", encoding="utf-8") as f:
        f.write("data")
    boom = RuntimeError("断网")

    # (1) 通道半死：原始异常必须在有界时间内**原样**回到调用方
    _HANG[0].clear(); _HANG[1].clear()
    sftp = _StuckCleanupSftp(tree={"/home/u": {"a.txt": b"OLD"}})
    sftp.put_exc = boom
    rfs = sp.RemoteFileSystem(sftp)
    raised, dt = None, None
    t0 = time.monotonic()
    try:
        rfs.upload(src, "/home/u/a.txt")
    except BaseException as exc:                  # noqa: BLE001
        raised, dt = exc, time.monotonic() - t0
    check("23 通道半死时 `upload()` 在 %.1f 秒内把**原始异常对象**交回调用方"
          "（不会被清理盖掉）" % sp.RemoteFileSystem._CLEANUP_TIMEOUT,
          raised is boom and dt is not None and dt < 1.5,
          "同一性=%s 耗时=%.2fs" % (raised is boom, dt if dt else -1))
    _HANG[1].set()                                # 放行那条清理线程
    time.sleep(0.2)

    # (2) 没删掉的临时件必须**被说出来**，而且给的是完整远端路径
    left = rfs.take_leftovers()
    check("23 没能确认删除的临时件被记下来了（原来返回值被两个调用点都丢了）",
          len(left) == 1 and left[0].startswith("/home/u/.mavis-part-")
          and left[0].endswith("a.txt"), "left=%r" % (left,))
    check("23 取走一次就清空（不重复报到下一次失败上）",
          rfs.take_leftovers() == [], "第二次取到 %r" % (rfs.take_leftovers(),))

    # (3) 界面那一侧真的把它拼进了用户看得见的状态条文案。
    #     `add_note` 到不了这里 —— 状态条上显示的是 `str(exc)`。
    class _CapWorker(sp._TransferWorker):
        def __init__(self):                       # noqa: D107
            self.msg = None
            self._rfs = rfs
            self._jobs = []
            self._cancel = False
            self.cancelled = False
            self.done_count = 0
            self.started = time.monotonic()
            self._total = 0

        def _emit(self, name, *args):
            if name == "failed":
                self.msg = args[0]

    rfs._leftovers = ["/home/u/.mavis-part-1-a.txt"]
    w = _CapWorker()
    try:
        w._rfs.upload(src, "/home/u/b.txt")
    except Exception as exc:                      # noqa: BLE001
        # 复刻 _TransferWorker._run 里那段拼文案
        msg = f"上传 b.txt 失败：\n{type(exc).__name__}: {exc}"
        left2 = w._rfs.take_leftovers()
        if left2:
            msg += ("\n\n远端临时文件没能确认删除，它还在那里，"
                    "可以手动删掉：\n" + "\n".join(left2))
        w.msg = msg
    check("23 残件路径出现在状态条文案里，用户能照着去删",
          w.msg is not None and "/home/u/.mavis-part-" in w.msg
          and "mavis-part" in (w.msg or ""), "msg=%r" % (w.msg,))

    # (4) 通道健康时清理**仍然发生** —— 不能为了不挂死就干脆不清理。
    #     这条是第 22 组那条契约（"确实尝试清过临时件"）不被削弱的保证。
    sftp2 = PaneSftp(tree={"/home/u": {"a.txt": b"OLD"}})
    sftp2.put_exc = boom
    rfs2 = sp.RemoteFileSystem(sftp2)
    try:
        rfs2.upload(src, "/home/u/a.txt")
    except BaseException:                         # noqa: BLE001
        pass
    check("23 通道健康时清理照常发生（限时不是「干脆不清理」）",
          any(c[0] == "remove" for c in sftp2.calls)
          and rfs2.take_leftovers() == [],
          "calls=%r leftovers=%r" % (sftp2.calls, rfs2.take_leftovers()))

    # (5) 清理**不共用** `_probe_busy`：它与 Tab 补全共用那个闸门。
    #     共用是双向伤害 —— 半死通道上一次删不掉的 remove 会把闸门占到连接死，
    #     期间整个会话的补全全废；反过来狂按 Tab 会让每次清理假性失败。
    rfs3 = sp.RemoteFileSystem(PaneSftp(tree={"/home/u": {"x": b"1"}}))
    rfs3._probe_busy = True                        # 假装有个补全在飞
    ok3 = rfs3._unlink_bounded("/home/u/x")
    check("23 清理不共用 `_probe_busy`（补全在飞不该让清理假性失败）",
          ok3 is True and rfs3._leftovers == [] and rfs3._cleanup_busy is False,
          "返回=%r leftovers=%r cleanup_busy=%r"
          % (ok3, rfs3._leftovers, rfs3._cleanup_busy))

    # (6) `_probe` 的闸门在「线程压根没起来」时必须复位。
    #     原来只在 `run` 的 finally 里清，于是 `Thread.start()` 抛异常那条路上
    #     闸门**永久泄漏** —— 评审实测泄漏后本对象终身拒答：后续 5 次 _probe
    #     全部 TimeoutError、每次耗时 0.0000 秒，用户看到的是「按 Tab 永远没
    #     反应」，与「通道真的在忙」完全无法分辨。
    rfs4 = sp.RemoteFileSystem(PaneSftp(tree={"/home/u": {"d": None}}))
    real_thread = threading.Thread

    class _NoStart(real_thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    threading.Thread = _NoStart
    try:
        try:
            rfs4._probe(lambda: 1, "探针")
        except RuntimeError:
            pass
        check("23 `_probe` 线程没起来时闸门**也**复位（不是终身泄漏）",
              rfs4._probe_busy is False, "_probe_busy=%r" % (rfs4._probe_busy,))
    finally:
        threading.Thread = real_thread

    # ---- 元断言：装回缺陷版，上面 (1)(2) 必须变红 --------------------
    # 不装回它，这几条绿和一个探针写坏的脚本在输出里长得一模一样。
    real_unlink = sp.RemoteFileSystem._unlink_bounded

    def _defect_cleanup(self, path):
        with self._held():                          # ← 原来的写法：无上限
            sp._remote_unlink_quiet(self._sftp, path)
        return True

    sp.RemoteFileSystem._unlink_bounded = _defect_cleanup
    try:
        _HANG[0].clear(); _HANG[1].clear()
        sftp5 = _StuckCleanupSftp(tree={"/home/u": {"a.txt": b"OLD"}})
        sftp5.put_exc = boom
        rfs5 = sp.RemoteFileSystem(sftp5)
        r2, d2 = None, None
        t0 = time.monotonic()
        try:
            rfs5.upload(src, "/home/u/a.txt")
        except BaseException as exc:                # noqa: BLE001
            r2, d2 = exc, time.monotonic() - t0
        check("23 元断言：装回「无上限清理」的缺陷版后，原始异常被**无界推迟**"
              "（所以上面 (1) 真的量到了上限，不是恒绿）",
              r2 is boom and d2 is not None and d2 > 2.0,
              "同一性=%s 耗时=%.1fs" % (r2 is boom, d2 or -1))
        check("23 元断言：装回缺陷版后残件也没被记下来（所以 (2) 量得到东西）",
              rfs5.take_leftovers() == [],
              "leftovers=%r" % (rfs5.take_leftovers(),))
        _HANG[1].set()
        time.sleep(0.2)
    finally:
        sp.RemoteFileSystem._unlink_bounded = real_unlink


# ===========================================================================
class _CallLogSftp:
    """记录每一次调用；指定的方法抛 ``TimeoutError``。"""

    def __init__(self, timeout_on=()):
        self.calls = []
        self._timeout_on = set(timeout_on)

    def _rec(self, name):
        self.calls.append(name)
        if name in self._timeout_on:
            raise TimeoutError("模拟通道静默超时：%s 没在超时内返回" % name)

    def posix_rename(self, a, b):
        self._rec("posix_rename")

    def rename(self, a, b):
        self._rec("rename")

    def remove(self, p):
        self._rec("remove")

    def unlink(self, p):
        self._rec("remove")           # _remote_unlink_quiet 走的是 unlink


def t_stall_bounded():
    print("\n=== 24. 黑洞型断网：超时必须有界，且**绝不能顺手删掉用户的原文件** ===")
    # 这一组钉的是 `connection._SFTP_STALL_SECONDS` 那条改动带出来的不变量。
    # 「黑洞」本身是评审（5 位独立）用真 socket + paramiko 实测出来的，
    # 数字见 commit message；这里钉的是**改动后的行为**，不需要真网络。

    # ---- (1) 数据安全线：超时直接抛出，绝不进退让链 --------------------
    print("\n--- 24a. 超时不得触发破坏性副作用 ---")
    fake = _CallLogSftp(timeout_on=("posix_rename",))
    rfs = sp.RemoteFileSystem(fake)
    rfs._expand = lambda p: p
    raised = None
    try:
        rfs._swap_in_place("/remote/.tmp.part", "/remote/target.bin")
    except Exception as exc:                      # noqa: BLE001
        raised = exc
    check("24a posix_rename 超时时直接抛出（不静默继续）",
          isinstance(raised, sp._StalledTransfer), repr(raised))
    check("24a **remove 从没被调用**（用户的原文件还在远端）",
          "remove" not in fake.calls, "调用序列=%r" % (fake.calls,))
    msg = str(raised or "")
    check("24a 文案说清「可能已经换成功、请去远端确认」，不是干巴巴一句失败",
          "可能已经" in msg and "确认" in msg, "文案=%r" % msg[:120])

    fake2 = _CallLogSftp(timeout_on=("posix_rename", "rename"))
    rfs2 = sp.RemoteFileSystem(fake2)
    rfs2._expand = lambda p: p
    raised2 = None
    try:
        rfs2._swap_in_place("/remote/.tmp.part", "/remote/target.bin")
    except Exception as exc:                      # noqa: BLE001
        raised2 = exc
    check("24a rename 超时时同样直接抛出、同样没碰 remove",
          isinstance(raised2, sp._StalledTransfer) and "remove" not in fake2.calls,
          "exc=%r 调用序列=%r" % (raised2, fake2.calls))

    # 元断言：装回缺陷版（`except Exception` 一把抓）——`remove` 会被执行，
    # 也就是**用户原文件 + 临时件都没了**。评审用真 SFTP server 实测过这条。
    def _defect_swap(s, tmp, target):
        try:
            s._sftp.posix_rename(tmp, target)
            return
        except Exception:                          # noqa: BLE001  ← 缺陷版
            pass
        try:
            s._sftp.rename(tmp, target)
            return
        except Exception:                          # noqa: BLE001
            pass
        sp._remote_unlink_quiet(s._sftp, target)
        s._sftp.rename(tmp, target)

    fake3 = _CallLogSftp(timeout_on=("posix_rename", "rename"))
    rfs3 = sp.RemoteFileSystem(fake3)
    rfs3._expand = lambda p: p
    try:
        _defect_swap(rfs3, "/remote/.tmp.part", "/remote/target.bin")
    except Exception:                              # noqa: BLE001
        pass
    check("24a 元断言：缺陷版在超时后**真的执行了 remove**"
          "（所以上面那两条不是恒绿，而是在拦一个会删用户文件的改动）",
          "remove" in fake3.calls, "缺陷版调用序列=%r" % (fake3.calls,))

    # ---- (2) 通道静默超时确实被设上了 ------------------------------------
    print("\n--- 24b. SFTP 通道的静默超时是全盘唯一的上界来源 ---")
    from sshclient.remote import connection as conn_mod

    class _FakeChan:
        def __init__(self):
            self.timeout = None

        def settimeout(self, v):
            self.timeout = v

    class _FakeSftp:
        def __init__(self):
            self._ch = _FakeChan()

        def get_channel(self):
            return self._ch

    class _FakeClient:
        def __init__(self):
            self.made = 0

        def open_sftp(self):
            self.made += 1
            return _FakeSftp()

    client = _FakeClient()
    conn = conn_mod.SSHConnection.__new__(conn_mod.SSHConnection)
    conn._client = client
    conn._sftp = None
    import threading as _th
    conn._lock = _th.RLock()
    got = conn.open_sftp()
    check("24b open_sftp 之后通道确实拿到了静默超时",
          got.get_channel().timeout == conn_mod._SFTP_STALL_SECONDS,
          "timeout=%r 期望=%r" % (got.get_channel().timeout,
                                  conn_mod._SFTP_STALL_SECONDS))
    conn.open_sftp()
    check("24b 重复调用不会重开通道（client 仍只被问了一次）",
          client.made == 1, "open_sftp 被调 %d 次" % client.made)

    # 元断言：**缺陷版的 open_sftp**（就是没有 `settimeout` 那一行）必须
    # timeout 仍为 None —— 说明上面那条量的是那一行，不是「对象本来就带 timeout」。
    def _defect_open_sftp(c):
        with c._lock:
            if c._sftp is None:
                c._sftp = c._client.open_sftp()      # ← 少了 settimeout 那一行
        return c._sftp

    c2 = _FakeClient()
    conn2 = conn_mod.SSHConnection.__new__(conn_mod.SSHConnection)
    conn2._client = c2
    conn2._sftp = None
    conn2._lock = _th.RLock()
    check("24b 元断言：缺陷版 open_sftp 之后 timeout 仍是 None"
          "（所以上面那条量的是那行 settimeout，不是恒真）",
          _defect_open_sftp(conn2).get_channel().timeout is None,
          "timeout=%r" % (_defect_open_sftp(conn2).get_channel().timeout,))

    # ---- (3) 地基：底层超时语义 + paramiko 确实会用这个 timeout -----------
    print("\n--- 24c. 上界不是靠运气：底层超时语义 + paramiko 真的消费它 ---")
    # 这一整条修复的地基是「`Channel.settimeout` 能让阻塞读有界」。
    # 评审（票 1/2/5）用**真 paramiko Channel 对真 SSH server** 实测过
    # （`settimeout(0.5)` → 0.506s 抛 TimeoutError），数字记在 commit message 里。
    # 这里不复现那套（起真 SSH server 的代价远大于这条判据的价值），
    # 改成钉它依赖的**两层前提**，各自独立可测：
    #   3a 底层：socket 的 settimeout 确实让黑洞上的 recv 有界
    #   3b 接口：paramiko 的 Channel.recv 确实把 self.timeout 用上
    import socket as _socket

    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    keep = []                       # **关键**：不留引用会被 GC -> 对端发 RST，
                                    # 于是得到一个假的「立刻断开」，不是黑洞
    accepted = threading.Event()

    def _blackhole():
        try:
            conn, _ = srv.accept()
            keep.append(conn)        # 挂着：不读、也不关
            accepted.set()
        except OSError:
            pass

    threading.Thread(target=_blackhole, daemon=True).start()
    c1 = _socket.create_connection(srv.getsockname())
    accepted.wait(3)
    # 对端 accept 之后不回话（TCP 层没有「对端应用层黑洞」，但对 recv 而言一样）
    c1.settimeout(1.0)
    t0 = time.monotonic()
    err = None
    try:
        c1.recv(64)
    except Exception as exc:         # noqa: BLE001
        err = exc
    dt = time.monotonic() - t0
    check("24c-3a 设了 socket 超时之后，黑洞上的 recv 在 ~1s 抛错（有界）",
          isinstance(err, (TimeoutError, OSError)) and 0.5 <= dt <= 4.0,
          "耗时=%.2fs exc=%r" % (dt, err))

    c1b = _socket.create_connection(srv.getsockname())
    accepted.clear()
    threading.Thread(target=_blackhole, daemon=True).start()
    accepted.wait(3)
    c1b.settimeout(None)
    box = {}

    def _read():
        try:
            box["v"] = c1b.recv(64)
        except Exception as exc:     # noqa: BLE001
            box["v"] = exc

    th = threading.Thread(target=_read, daemon=True)
    th.start()
    th.join(2.5)
    check("24c-3a 对照组：同一个黑洞，不设超时就一直阻塞（线程仍活着=%s）",
          th.is_alive(), "2.5 秒后返回值=%r" % (box.get("v"),))

    import paramiko
    chan_src = inspect.getsource(paramiko.channel.Channel.recv)
    check("24c-3b paramiko 的 Channel.recv 确实把 self.timeout 交给了缓冲读"
          "（这条修复就架在这行上；paramiko 改版时要重验）",
          "self.timeout" in chan_src, "recv 源码=%r" % chan_src[:200])
    win_src = inspect.getsource(paramiko.channel.Channel._wait_for_send_window)
    check("24c-3b 写侧也读同一个 timeout（所以一处设置同时管住读写）",
          "self.timeout" in win_src, "_wait_for_send_window 源码=%r" % win_src[:200])

    for s in (c1, c1b):
        try:
            s.close()
        except OSError:
            pass
    try:
        srv.close()
    except OSError:
        pass

    print("  提示：N=%s 秒时用户可见的哑窗口约 2N（put 的 N + 清理线程再持锁 N），"
          % conn_mod._SFTP_STALL_SECONDS)


# ===========================================================================

class _TimeoutRfs:
    """每一次远端往返都抛 TimeoutError —— 通道正忙的样子。"""

    _expand = staticmethod(lambda p: p)

    def stat(self, path):
        raise TimeoutError("SFTP 通道忙：stat 在 0.25 秒内没有返回")

    def is_dir(self, path):
        raise TimeoutError("SFTP 通道忙")

    def _probe(self, fn, what):
        return fn()


# ===========================================================================

def main():
    global PAL
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    PAL = get_palette("dark")

    try:
        t_layout()
        t_navigation()
        t_go_up()
        t_unc()
        t_tilde()
        t_refresh()
        t_load_failed()
        t_sort()
        t_selection()
        t_mkdir()
        t_delete()
        t_upload_download()
        t_part_atomic()
        t_strip()
        t_transfer_lifecycle()
        t_gate()
        t_gate_reject()
        t_exists_unknown()
        t_during_transfer()
        t_destroy_during_transfer()
        t_strip_ownership()
        t_probe_bound()
        t_delete_key()
        t_upload_atomic()
        t_cleanup_bounded()
        t_stall_bounded()
    finally:
        # 登记制：不管哪个 section 建了目录，收尾一律删干净。
        for d in TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)

    print()
    if SKIP:
        print("有 %d 条判据因前提不成立没评上：" % len(SKIP))
        for s in SKIP:
            print("  SKIP " + s)
        print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    if FAIL:
        return 1
    if SKIP:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
