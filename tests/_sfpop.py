"""SFTP 面板的四条判据：F1 / F2 / F4 / F5。

这四条**全部是我自己在前一版改动里引入或漏掉的**，不是历史遗留问题，
所以这一套同时也是给上一版验收的：判据落在**用户看得见的行为**上，
不落在「代码里有没有那个 if」上。

  F1  `populate_from` 不推进请求号 → 传输完成后，还在飞的那次 refresh
      结果回来会把刚传完的文件从列表里抹掉（实测 `['uploaded.txt']` 变
      `['T']`，因为 `removeRows` 之后模型空了，剩一行残留）。
  F2  `mkdir` / `delete` 在 GUI 线程上抢无超时的 SFTP 锁 → 大文件上传
      全程持锁，此时点删除 = 界面冻结整个上传时长。
  F4  `Thread.start()` 抛了没人接 → 异常冒到事件循环，而 `count_label`
      已经停在「读取中…」，面板既不报错也刷不出来。
  F5  失败信号传 `str(exc)` 再包一层 `RuntimeError` → 真实的
      `SFTPError` / `PermissionError` 被抹成 RuntimeError，日志里唯一的
      诊断线索没了。

**判据为什么不写成「跑一次看看崩不崩」**：F2 的冻窗和 F1 的抹除都要看
时间/时序，一次跑不出来。这里全部用可控的事件来驱动：假 fs 的 `mkdir`
卡在一个 `threading.Event` 上，断言 GUI 线程在这段时间里还能处理事件、
按钮是禁用的，然后把事件放掉再收结果。

Checks:
  1. F5  listdir 失败时收到的是**原异常对象**，类型没被抹掉
  2. F4  起线程抛异常不外泄，标签不留在「读取中…」
  3. F1  传输填表推进请求号，旧请求号的结果被丢弃
  4. F2  mkdir 跑在工作线程上，GUI 线程不被它挡住
  5. F2  慢操作在飞时两个按钮都是禁用的，放行后恢复
  6. F2  成功时走注入的状态条提示
  7. F2  失败时弹框 + 按钮恢复 + 列表照样刷新
  8. F2  连点两次只会起一个操作
  9. F2  删除走同一条线程，失败项攒起来一起报
"""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtWidgets

from sshclient.ui import sftp_panel as sp

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


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


def entry(name, path=None):
    return sp.FileEntry(name=name, path=path or ("/tmp/" + name),
                        is_dir=False, size=1, mtime=0.0, mode=0o644)


# --------------------------------------------------------------------------
# 假文件系统。**只碰「会被测到」的方法**，其余不存在 —— 面板真去调一个
# 没实现的方法会 AttributeError，而那会伪装成「产品坏了」。
# --------------------------------------------------------------------------

class ListFailFS:
    def __init__(self, exc):
        self.exc = exc

    def listdir(self, cwd):
        raise self.exc


class OpFS:
    """`mkdir` 卡在 `gate` 上，用来量「GUI 线程有没有被挡住」。"""

    def __init__(self, exc=None):
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.thread_name = None
        self.exc = exc
        self.listdir_calls = 0
        self.made = []
        self.removed = []

    def listdir(self, cwd):
        self.listdir_calls += 1
        return [entry("keep.txt")]

    def mkdir(self, path):
        self.thread_name = threading.current_thread().name
        self.entered.set()
        self.gate.wait(5)
        self.made.append(path)

    def remove(self, path):
        self.thread_name = threading.current_thread().name
        if self.exc is not None:
            raise self.exc
        self.removed.append(path)

    def rmdir(self, path):
        self.remove(path)


# --------------------------------------------------------------------------

def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    pal = sp.get_palette("dark")

    # ---- F5：失败信号必须传原异常对象 -------------------------------
    class MySFTPError(Exception):
        pass

    seen = []
    fp = sp.FilePanel("remote", ListFailFS(MySFTPError("no such file")), pal)
    fp._load_failed = lambda exc: seen.append(exc)
    fp.refresh()
    wait_until(lambda: seen, 2000)
    check("F5 收到的是原异常对象而不是包一层的 RuntimeError",
          len(seen) == 1 and type(seen[0]) is MySFTPError,
          type(seen[0]).__name__ if seen else "一次都没收到")
    check("F5 信号签名是 object 而不是 str",
          str(sp._ListWorker.failed) == "<unbound PYQT_SIGNAL failed(int,PyQt_PyObject)>",
          str(sp._ListWorker.failed))
    fp.deleteLater()

    # ---- F4：起线程抛了不能外泄，标签不能卡在「读取中…」 -------------
    fs2 = OpFS()
    fp2 = sp.FilePanel("remote", fs2, pal)
    real_start = threading.Thread.start

    def boom_start(self, *a, **k):
        raise RuntimeError("can't start new thread")

    threading.Thread.start = boom_start
    try:
        threw = None
        try:
            fp2.refresh()
        except BaseException as exc:          # noqa: BLE001
            threw = exc
    finally:
        threading.Thread.start = real_start
    check("F4 refresh() 没有把起线程的异常抛回调用方", threw is None,
          type(threw).__name__ if threw else "")
    check("F4 标签没有留在「读取中…」", fp2.count_label.text() != "读取中…",
          fp2.count_label.text())
    check("F4 仍然只有一次 listdir（没有把失败重复放大）",
          fs2.listdir_calls == 0, "listdir_calls=%d" % fs2.listdir_calls)

    # ---- F1：传输填表必须推进请求号 --------------------------------
    before = fp2._list_req
    fp2.populate_from([entry("uploaded.txt")])
    after = fp2._list_req
    check("F1 populate_from 推进了请求号", after > before,
          "%d → %d" % (before, after))
    # 之前那次还在飞的结果回来：请求号对不上，必须丢掉
    fp2._on_listed(before, [entry("stale.txt")])
    names = [fp2.model.item(r, 0).text() for r in range(fp2.model.rowCount())]
    check("F1 过期结果没有覆盖刚传完的列表", names == ["uploaded.txt"], str(names))
    # 反向对照：同号的结果**要**被接受，否则这条判据是恒真的
    fp2._on_listed(after, [entry("fresh.txt")])
    names2 = [fp2.model.item(r, 0).text() for r in range(fp2.model.rowCount())]
    check("F1 对照组：同号结果仍然会被填进去", names2 == ["fresh.txt"], str(names2))

    # ---- F2：mkdir 必须离开 GUI 线程 -------------------------------
    fs3 = OpFS()
    fp3 = sp.FilePanel("remote", fs3, pal)
    flashes = []
    fp3._flash = flashes.append
    main_thread = threading.current_thread()

    t0 = time.monotonic()
    fp3._run_off_thread(fs3.mkdir, "/tmp/newdir",
                        fail_title="新建目录失败", done_text="已新建「newdir」")
    # 门还没开，这一下必须立刻返回 —— 挡住了就是冻窗
    t1 = time.monotonic()
    check("F2 mkdir 没有挡住 GUI 线程", (t1 - t0) < 0.15,
          "%.0f ms" % ((t1 - t0) * 1000))
    wait_until(fs3.entered.is_set, 2000)
    check("F2 mkdir 真的跑在工作线程上",
          fs3.thread_name not in (None, main_thread.name), str(fs3.thread_name))

    # 门关着的时候：两个按钮都必须禁用（看得见的反馈），而不是弹窗骂人
    check("F2 在飞时「新建目录」是禁用的", not fp3.mkdir_btn.isEnabled())
    check("F2 在飞时「删除所选」是禁用的", not fp3.delete_btn.isEnabled())
    check("F2 在飞时标签显示「处理中…」", fp3.count_label.text() == "处理中…",
          fp3.count_label.text())

    # 连点第二次：不该再起一个
    fs3.made.clear()
    fp3._run_off_thread(fs3.mkdir, "/tmp/second", fail_title="新建目录失败")
    fs3.gate.set()
    wait_until(lambda: fp3._op_worker is None, 3000)
    check("F2 连点两次只起一个操作", fs3.made == ["/tmp/newdir"], str(fs3.made))
    check("F2 完成后忙时标记已清", fp3._busy is False)
    check("F2 完成后「新建目录」恢复可用", fp3.mkdir_btn.isEnabled())
    # **必须先等列表填好再 selectAll。** `_on_op_ok` 是先清 worker 再
    # `refresh()`，而远程的 `refresh()` 还要等 `_ListWorker` 回来才填模型。
    # 早一步 `selectAll` 选的是**空模型**，之后再来的行不会自己带上选中态
    # —— 判据会红，而产品其实好好的。
    wait_until(lambda: fp3.model.rowCount() > 0, 3000)
    fp3.view.selectAll()
    pump(60)
    check("F2 完成后选中一下「删除所选」就恢复", fp3.delete_btn.isEnabled(),
          "rows=%d" % fp3.model.rowCount())
    check("F2 成功时走了注入的状态条", flashes == ["已新建「newdir」"], str(flashes))
    check("F2 完成后列表刷新过", fs3.listdir_calls >= 1,
          "listdir_calls=%d" % fs3.listdir_calls)

    # ---- F2 失败路径 ------------------------------------------------
    warned = []
    real_warning = sp.msgbox.warning
    sp.msgbox.warning = lambda parent, title, text, *a, **k: warned.append((title, text))
    try:
        fs4 = OpFS(exc=PermissionError("denied"))
        fp4 = sp.FilePanel("remote", fs4, pal)
        fp4._flash = lambda m: None
        fs4.gate.set()
        fp4._run_off_thread(fs4.remove, "/tmp/secret", fail_title="删除失败")
        wait_until(lambda: fp4._op_worker is None, 3000)
    finally:
        sp.msgbox.warning = real_warning
    check("F2 失败时弹的是中文框且带上了原话",
          len(warned) == 1 and warned[0][0] == "删除失败"
          and "denied" in warned[0][1], str(warned))
    check("F2 失败后忙时标记已清", fp4._busy is False)
    check("F2 失败后「新建目录」恢复可用", fp4.mkdir_btn.isEnabled())
    wait_until(lambda: fp4.model.rowCount() > 0, 3000)
    fp4.view.selectAll()
    pump(60)
    check("F2 失败后选中一下「删除所选」就恢复", fp4.delete_btn.isEnabled(),
          "rows=%d" % fp4.model.rowCount())
    check("F2 失败后照样刷新了列表", fs4.listdir_calls >= 1,
          "listdir_calls=%d" % fs4.listdir_calls)
    fp4.deleteLater()

    # ---- F2 删除：多条一起删，失败攒起来一起报 ---------------------
    warned2 = []
    sp.msgbox.warning = lambda parent, title, text, *a, **k: warned2.append((title, text))

    class PartialFS(OpFS):
        def __init__(self):
            super().__init__()
            self.rmdir_err = None

        def remove(self, path):
            super().remove(path)
            if path.endswith("locked"):
                raise PermissionError("directory not empty")

    try:
        fs5 = PartialFS()
        fs5.gate.set()
        fp5 = sp.FilePanel("remote", fs5, pal)
        fp5._flash = lambda m: None
        entries = [entry("a.txt", "/tmp/a.txt"), entry("b.txt", "/tmp/b.txt")]
        fp5._run_off_thread(fp5._delete_entries, entries, fail_title="删除失败")
        wait_until(lambda: fp5._op_worker is None, 3000)
    finally:
        sp.msgbox.warning = real_warning
    check("F2 删除走工作线程",
          fs5.thread_name not in (None, threading.current_thread().name),
          str(fs5.thread_name))
    check("F2 删除成功时一条都不该弹框", not warned2, str(warned2))

    # 两条里一条失败：成功的照删，失败的要报出来
    warned2.clear()
    sp.msgbox.warning = lambda parent, title, text, *a, **k: warned2.append((title, text))
    try:
        fs6 = PartialFS()
        fs6.gate.set()
        fp6 = sp.FilePanel("remote", fs6, pal)
        fp6._flash = lambda m: None
        both = [entry("a.txt", "/tmp/a.txt"), entry("locked", "/tmp/locked")]
        fp6._run_off_thread(fp6._delete_entries, both, fail_title="删除失败")
        wait_until(lambda: fp6._op_worker is None, 3000)
    finally:
        sp.msgbox.warning = real_warning
    check("F2 部分失败时报的是那次失败", len(warned2) == 1
          and "locked" in warned2[0][1], str(warned2))
    check("F2 部分失败时好的那条照样删掉了", fs6.removed == ["/tmp/a.txt", "/tmp/locked"],
          str(fs6.removed))
    for p in (fp2, fp3, fp5, fp6):
        p.deleteLater()
    pump(50)

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
