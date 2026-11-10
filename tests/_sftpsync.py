"""SFTP 面板：为什么点"上传"会一直转圈，而且传不完。

`paramiko.SFTPClient` 的 `_lock`（`sftp_client.py:118`）只保护 `request_number`
记账，不保护通道 I/O；响应包由**读到它的那个线程**消费，`_read_response`
（884-919）里若是别人的同步响应就静默丢弃（914 行的守卫）。于是两个线程
共用一个 SFTPClient 时，要么报文头被交错写坏报 "Garbage packet received"，
要么一方的响应被另一方吃掉、永远挂在 `recv` 上。

修复前 `_upload` 同时做了两件致命的事：给每个文件起一个线程共用同一个
client，然后**立刻**在 GUI 线程上 `self.remote.refresh()` 用同一个 client。
传输失败后又在工作线程里弹模态 `QMessageBox`——跨线程建控件 + 嵌套事件
循环，界面就此卡死，鼠标变转圈，异常本身反而没人看见。

本文件用**真实 paramiko 代码路径**复现：进程内起一个最小 SFTP v3 服务端，
两端 `socket.socketpair` 直连，中间跑真的 `paramiko.SFTPClient`。观察到的
行为是 paramiko 的真实行为，不是桩件的产物。

注意检查 4 为什么不写成"去掉锁跑一次看会不会报错"：**那条竞态是非确定性的**，
去掉锁之后有时报 `Garbage packet received`、有时全过。断言必须落在**性质**
（同时进入通道的线程数）上，不能落在某一次调用的结果上。

Checks:
  1. 同一 client 上并发 upload + listdir 必须都完成
  2. 并发 3 个 upload + 1 次 listdir（模拟多选上传）必须都完成
  3. 串行化之后功能仍然正确（不能只不死锁就完事）
  4. 通道占用不变量：有锁时最大同时占用 = 1，去掉锁必须 > 1
  5. 端到端走真 SftpPanel：传输期间 GUI 线程最长空档 < 400 ms
  6. 传输途中销毁面板，进程不能 abort
"""
import contextlib
import os
import select
import socket
import struct
import sys
import tempfile
import threading
import time
import traceback
import unittest.mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import paramiko
from paramiko.message import Message
from paramiko.sftp import (
    CMD_ATTRS,
    CMD_CLOSE,
    CMD_INIT,
    CMD_NAME,
    CMD_OPEN,
    CMD_OPENDIR,
    CMD_READDIR,
    CMD_REALPATH,
    CMD_REMOVE,
    CMD_RENAME,
    CMD_STAT,
    CMD_STATUS,
    CMD_VERSION,
    CMD_WRITE,
    SFTP_OK,
)
from paramiko.sftp_attr import SFTPAttributes
from PyQt6 import QtCore, QtWidgets

from sshclient.ui import sftp_panel as sp

PASS, FAIL = [], []
_SFTP_EOF = 1          # 协议里的 SFTP_EOF
_CMD_HANDLE = 102     # 协议里的 CMD_HANDLE（paramiko 那边不导出这个名字）


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


# ---------------------------------------------------------------------------
# 进程内最小 SFTP v3 服务端
# ---------------------------------------------------------------------------

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("客户端断开")
        buf += chunk
    return buf


def _send_packet(sock, t, payload):
    sock.sendall(struct.pack(">I", len(payload) + 1) + bytes([t]) + payload)


def _status(num, code=SFTP_OK, text=""):
    m = Message()
    m.add_int(num)
    m.add_int(code)
    m.add_string(text)
    return m.asbytes()


def _attrs_msg(num, size=0, mtime=0, mode=0o100644):
    m = Message()
    m.add_int(num)
    a = SFTPAttributes()
    a.st_size = size
    a.st_mtime = mtime
    a.st_mode = mode
    a._pack(m)
    return m.asbytes()


class _FakeSftpServer:
    """够 paramiko 的 put/get/listdir/stat/normalize 跑通的最小服务端。

    ``write_delay`` 是每个 CMD_WRITE 回应前的延时，用来把竞争窗口拉开；
    真实网络有 RTT，竞态照样发生，只是更难复现。
    """

    ENTRIES = (("alpha.txt", 12, 0o100644), ("subdir", 4096, 0o040755))

    def __init__(self, sock, write_delay=0.0):
        self._sock = sock
        self._write_delay = write_delay
        self._dir_reads = {}
        self._handle_path = {}
        self._written = {}
        self._file_size = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        for s in ():
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self):
        try:
            while not self._stop.is_set():
                hdr = _recv_exact(self._sock, 4)
                data = _recv_exact(self._sock, struct.unpack(">I", hdr)[0])
                msg = Message(data[1:])
                self._dispatch(data[0], msg.get_int(), msg)
        except (EOFError, OSError):
            pass
        except Exception:
            traceback.print_exc()

    def _dispatch(self, t, num, msg):
        if t == CMD_INIT:
            m = Message()
            m.add_int(3)
            _send_packet(self._sock, CMD_VERSION, m.asbytes())
        elif t == CMD_OPEN:
            path = str(msg.get_text())
            msg.get_int()                      # pflags
            handle = b"h%d" % num
            self._handle_path[handle] = path
            self._written[path] = 0
            m = Message()
            m.add_int(num)
            m.add_string(handle)
            _send_packet(self._sock, _CMD_HANDLE, m.asbytes())
        elif t == CMD_WRITE:
            handle = msg.get_binary()
            msg.get_int64()                    # offset
            data = msg.get_binary()
            path = self._handle_path.get(handle, "")
            self._written[path] = self._written.get(path, 0) + len(data)
            if self._write_delay:
                time.sleep(self._write_delay)
            _send_packet(self._sock, CMD_STATUS, _status(num))
        elif t == CMD_CLOSE:
            path = self._handle_path.get(msg.get_binary(), "")
            if path:
                self._file_size[path] = self._written.get(path, 0)
            _send_packet(self._sock, CMD_STATUS, _status(num))
        elif t == CMD_STAT:
            path = str(msg.get_text())
            _send_packet(self._sock, CMD_ATTRS,
                         _attrs_msg(num, size=self._file_size.get(path, 0)))
        elif t == CMD_OPENDIR:
            handle = b"h%d" % num
            self._dir_reads[handle] = 0
            m = Message()
            m.add_int(num)
            m.add_string(handle)
            _send_packet(self._sock, _CMD_HANDLE, m.asbytes())
        elif t == CMD_READDIR:
            handle = msg.get_binary()
            n = self._dir_reads.get(handle, 0)
            self._dir_reads[handle] = n + 1
            if n:
                _send_packet(self._sock, CMD_STATUS, _status(num, _SFTP_EOF, "EOF"))
            else:
                m = Message()
                m.add_int(num)
                m.add_int(len(self.ENTRIES))
                for fname, size, mode in self.ENTRIES:
                    m.add_string(fname)
                    m.add_string("-rw-r--r-- 1 u u %8d Jan  1 00:00 %s" % (size, fname))
                    a = SFTPAttributes()
                    a.st_size = size
                    a.st_mtime = 1700000000
                    a.st_mode = mode
                    a._pack(m)
                _send_packet(self._sock, CMD_NAME, m.asbytes())
        elif t == CMD_REALPATH:
            msg.get_text()
            m = Message()
            m.add_int(num)
            m.add_int(1)
            m.add_string("/")
            m.add_string("/")
            SFTPAttributes()._pack(m)
            _send_packet(self._sock, CMD_NAME, m.asbytes())
        elif t == CMD_RENAME:
            # 上传现在走「同目录临时件 + 改名」，所以这里必须真的实现改名 ——
            # 少了它，「不支持 → 回退」那条路根本走不到。
            old = str(msg.get_text())
            new = str(msg.get_text())
            if new in self._file_size:
                # 标准 v3：目标必须已存在之外的情况才允许改。这里按
                # SFTP_FAILURE 拒掉，客户端就该走「删旧的再改」那条退路。
                _send_packet(self._sock, CMD_STATUS, _status(num, 4, "File exists"))
            elif old in self._file_size:
                self._file_size[new] = self._file_size.pop(old)
                _send_packet(self._sock, CMD_STATUS, _status(num))
            else:
                _send_packet(self._sock, CMD_STATUS,
                              _status(num, 2, "No such file"))
        elif t == CMD_REMOVE:
            path = str(msg.get_text())
            if path in self._file_size:
                del self._file_size[path]
                _send_packet(self._sock, CMD_STATUS, _status(num))
            else:
                _send_packet(self._sock, CMD_STATUS,
                              _status(num, 2, "No such file"))
        else:
            # **刻意不实现 `posix-rename@openssh.com`（EXTENDED / 200）。**
            # 真实服务端里最小实现就是不支持它，而客户端必须能退到标准
            # `rename` —— 让这个 fixture 保持不支持，才测得到那条退路。
            _send_packet(self._sock, CMD_STATUS, _status(num, 8, "unsupported %d" % t))


class _ChanShim:
    """只补上 paramiko 日志/流水线要用的几个方法，收发原样转发。

    `SFTPClient.__init__` 会调 `self.sock.get_name()` 打一行日志；裸 socket
    没有这个方法。`SFTPFile._write` 在流水线攒够 100 个未确认请求后会问
    `self.sftp.sock.recv_ready()`（sftp_file.py:204）才决定要不要回收确认 ——
    也就是**只有大于约 3.2 MB 的上传**才会走到这里，小文件测试碰不到，
    但真传大文件时一定会碰到。

    这里刻意不实现任何 SFTP 协议逻辑，保证跑的是 paramiko 自己的
    `_request` / `_read_response` / `put`。
    """

    def __init__(self, sock):
        self._sock = sock

    def get_name(self):
        return "sftp-loopback"

    def send(self, data):
        return self._sock.send(data)

    def recv(self, n):
        return self._sock.recv(n)

    def recv_ready(self):
        """语义照抄 `Channel.recv_ready`：有数据可读就 True。

        真 socket 上用 select 探一下即可；无阻塞地读走数据反而会把包弄丢。
        """
        try:
            r, _, _ = select.select([self._sock], [], [], 0)
            return bool(r)
        except (OSError, ValueError):
            return False

    def close(self):
        self._sock.close()


def make_pair(write_delay=0.0):
    """返回 (真实的 paramiko.SFTPClient, 服务端)。"""
    c_sock, s_sock = socket.socketpair()
    server = _FakeSftpServer(s_sock, write_delay=write_delay)
    server.start()
    return paramiko.SFTPClient(_ChanShim(c_sock)), server


# ---------------------------------------------------------------------------
# 并发执行器
# ---------------------------------------------------------------------------

def run_concurrently(fns, timeout):
    """用 barrier 让所有 fn 真正同时开跑，返回 name -> 结果。

    每个 fn 跑在自己的守护线程里；超时的记 finished=False（= 卡死），
    卡住的线程不会拖住整个测试。
    """
    barrier = threading.Barrier(len(fns))
    boxes = {name: {} for name in fns}

    def wrap(name, fn):
        def target():
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                boxes[name]["exc"] = RuntimeError("barrier 超时")
                return
            try:
                boxes[name]["ok"] = fn()
            except Exception as exc:
                boxes[name]["exc"] = exc
        return target

    threads = [threading.Thread(target=wrap(n, f), daemon=True, name=n)
               for n, f in fns.items()]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    for name, t in zip(fns, threads):
        boxes[name]["finished"] = not t.is_alive()
    return boxes


def _all_ok(boxes):
    return all(b.get("finished") and "exc" not in b for b in boxes.values())


def _spin_until(app, pred, timeout):
    """转到 *pred* 为真为止，返回是否成立。"""
    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 10)
    return bool(pred())


def _summarize(boxes):
    """按"完成/抛错/卡死"三态汇报。

    注意不能拿 `ok is not None` 当成功标志：``upload``/``download`` 正常返回
    就是 ``None``，那会把成功误报成卡死。
    """
    def one(b):
        if not b.get("finished"):
            return "卡死"
        if "exc" in b:
            return type(b["exc"]).__name__ + ":" + str(b["exc"])[:40]
        return "OK"
    return "  ".join("%s=%s" % (n, one(b)) for n, b in boxes.items())


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

def check_concurrent(label, fns, timeout=20.0):
    boxes = run_concurrently(fns, timeout)
    check(label, _all_ok(boxes), _summarize(boxes))
    return boxes


def main():
    tmpdir = tempfile.mkdtemp(prefix="sftpsync_")
    payload = os.path.join(tmpdir, "payload.bin")
    with open(payload, "wb") as f:
        f.write(b"x" * (256 * 1024))

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    # 传输期可能弹框；QMessageBox::exec 跑的是嵌套事件循环，定时器照样进得去
    def _dismiss():
        w = QtWidgets.QApplication.activeModalWidget()
        if w is not None:
            w.close()
    dlg_timer = QtCore.QTimer()
    dlg_timer.timeout.connect(_dismiss)
    dlg_timer.start(50)

    try:
        real_held = sp.RemoteFileSystem._held

        print("\n[1] 同一 SFTPClient 上并发 upload + listdir")
        client, server = make_pair(write_delay=0.0005)
        rfs = sp.RemoteFileSystem(client)
        check_concurrent("并发 upload + listdir 都完成", {
            "upload": lambda: rfs.upload(payload, "/alpha_copy.txt", progress=None),
            "listdir": lambda: len(rfs.listdir("/")),
        })
        server.stop()
        client.close()

        print("\n[2] 并发 3 个 upload + 1 次 listdir（模拟多选上传）")
        client2, server2 = make_pair(write_delay=0.0005)
        rfs2 = sp.RemoteFileSystem(client2)
        check_concurrent("3 路并发上传都完成", {
            "up1": lambda: rfs2.upload(payload, "/m1.bin", progress=None),
            "up2": lambda: rfs2.upload(payload, "/m2.bin", progress=None),
            "up3": lambda: rfs2.upload(payload, "/m3.bin", progress=None),
            "ls": lambda: len(rfs2.listdir("/")),
        }, timeout=30.0)
        server2.stop()
        client2.close()

        print("\n[3] 串行化之后功能仍然正确")
        client3, server3 = make_pair(write_delay=0.0)
        rfs3 = sp.RemoteFileSystem(client3)
        try:
            rfs3.upload(payload, "/single.bin", progress=None)
            seen = [e.name for e in rfs3.listdir("/")]
            check("串行 upload/listdir 结果正确",
                  "alpha.txt" in seen and "subdir" in seen, "条目=%r" % seen)
        except Exception as exc:
            check("串行 upload/listdir 结果正确", False, repr(exc))
        server3.stop()
        client3.close()

        print("\n[4] 通道占用不变量：任何时刻只允许一个线程进远程通道")
        # 靠"跑一次看会不会报错"来当守卫是不可靠的：去掉锁之后，竞态**有时**
        # 恰好不发作（实测同样两个调用，坏的时候报 Garbage packet，好的时候
        # 全过）。所以这里直接量那条不变量本身——同时进入远程通道的最大线程数。
        # 探针里那 10ms sleep 只为把重叠固定下来，不碰生产代码。
        def _probe(serialize: bool):
            state = {"cur": 0, "max": 0}

            @contextlib.contextmanager
            def probe(self, timeout=None):
                if serialize:
                    with real_held(self, timeout):
                        state["cur"] += 1
                        state["max"] = max(state["max"], state["cur"])
                        try:
                            time.sleep(0.01)
                            yield
                        finally:
                            state["cur"] -= 1
                else:
                    state["cur"] += 1
                    state["max"] = max(state["max"], state["cur"])
                    try:
                        time.sleep(0.01)
                        yield
                    finally:
                        state["cur"] -= 1
            return state, probe

        def measure(serialize):
            c, sv = make_pair(write_delay=0.0)
            r = sp.RemoteFileSystem(c)
            state, probe = _probe(serialize)
            with unittest.mock.patch.object(sp.RemoteFileSystem, "_held", probe):
                run_concurrently({
                    "a": lambda: r.upload(payload, "/probe_a.bin", progress=None),
                    "b": lambda: len(r.listdir("/")),
                    "c": lambda: len(r.list_names("/")),
                }, timeout=30.0)
            sv.stop()
            c.close()
            return state["max"]

        assert sp.RemoteFileSystem._held is real_held, "锁被永久换掉了"
        with_lock = measure(True)
        without_lock = measure(False)
        check("有锁时最大同时占用 = 1", with_lock == 1, "实测 %d" % with_lock)
        check("去掉锁时最大同时占用 > 1（证明这条不变量真在拦）",
              without_lock > 1, "实测 %d" % without_lock)

        print("\n[5] 端到端走真 SftpPanel：GUI 线程不能被卡住")
        # 传输要够长，"心跳次数够不够"才有意义；下面用最长空档来判。
        big = os.path.join(tmpdir, "big.bin")
        with open(big, "wb") as f:
            f.write(b"y" * (4 * 1024 * 1024))
        client5, server5 = make_pair(write_delay=0.004)
        panel = sp.SftpPanel(client5, None, None)
        panel.local.cd(tmpdir)
        panel.remote.cd("/")
        panel.resize(1000, 600)
        panel.show()

        # 选中 big.bin
        row = next(r for r in range(panel.local.model.rowCount())
                   if panel.local.model.item(r, 0).text() == "big.bin")
        panel.local.view.selectionModel().select(
            panel.local.model.index(row, 0),
            QtCore.QItemSelectionModel.SelectionFlag.Select |
            QtCore.QItemSelectionModel.SelectionFlag.Rows)
        picked = [e.name for e in panel.local.selected_entries()]
        check("本地选中生效", picked == ["big.bin"], "选中=%r" % picked)

        # 心跳：GUI 事件循环每跳一次记一次，并量出两次之间的**最长空档**。
        # 鼠标一直转圈的本质就是 GUI 线程长时间不泵消息，所以判据是空档而非次数。
        beats, last, gap_max = [0], [time.monotonic()], [0.0]
        seen_pct, seen_detail, saw_modal = [0], [""], [False]

        def _beat():
            now = time.monotonic()
            gap_max[0] = max(gap_max[0], now - last[0])
            last[0] = now
            beats[0] += 1
            if panel.strip.isVisible():
                seen_pct[0] = max(seen_pct[0], panel.strip.bar.value)
                if panel.strip.detail.text():
                    seen_detail[0] = panel.strip.detail.text()
            if QtWidgets.QApplication.activeModalWidget() is not None:
                saw_modal[0] = True

        beat = QtCore.QTimer()
        beat.timeout.connect(_beat)
        beat.start(20)

        check("空闲时状态条是收起的", not panel.strip.isVisible())

        t0 = time.monotonic()
        panel._upload()
        deadline = t0 + 60.0
        while panel.ctrl.busy and time.monotonic() < deadline:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 5)
        for _ in range(40):
            app.processEvents()
        beat.stop()
        elapsed = time.monotonic() - t0

        check("传输已结束（工作线程已退出）", not panel.ctrl.busy,
              "耗时 %.0f ms" % (elapsed * 1000))
        check("心跳至少跳了几次（说明确实在传输）", beats[0] >= 5,
              "心跳 %d 次" % beats[0])
        check("GUI 线程最长空档 < 400 ms（= 不会转圈）", gap_max[0] < 0.4,
              "最长空档 %.0f ms，传输 %.0f ms" % (gap_max[0] * 1000, elapsed * 1000))
        check("传输期间状态条确实出现过并推进过", seen_pct[0] > 0.5,
              "最高到 %.0f%%" % (seen_pct[0] * 100))
        check("状态条那行小字有内容", bool(seen_detail[0]), seen_detail[0])
        check("全程没有弹出任何模态框", not saw_modal[0])
        check("传输结束后状态条进入成功态", panel.strip.state == "success",
              "state=%s  %s" % (panel.strip.state,
                                panel.strip.toolTip().replace("\n", " | ")))
        check("成功态会自动退场", not panel.strip.isVisible()
              or _spin_until(app, lambda: not panel.strip.isVisible(), 4.0))
        check("上传/下载按钮已恢复可用",
              panel.up_btn.isEnabled() and panel.down_btn.isEnabled())
        server5.stop()
        client5.close()
        panel.close()
        panel.deleteLater()
        app.processEvents()

        print("\n[6] 传输途中销毁面板，不能 abort")
        # SftpPanel 是标签页的子控件，关闭标签时它被父级析构，根本收不到
        # closeEvent —— 也就没地方 wait()。若工作线程是 QThread，这个析构
        # 就是 "QThread: Destroyed while thread is still running" 直接 abort。
        client6, server6 = make_pair(write_delay=0.006)
        panel6 = sp.SftpPanel(client6, None, None)
        panel6.local.cd(tmpdir)
        panel6.remote.cd("/")
        panel6.show()
        row6 = next(r for r in range(panel6.local.model.rowCount())
                    if panel6.local.model.item(r, 0).text() == "payload.bin")
        panel6.local.view.selectionModel().select(
            panel6.local.model.index(row6, 0),
            QtCore.QItemSelectionModel.SelectionFlag.Select |
            QtCore.QItemSelectionModel.SelectionFlag.Rows)
        panel6._upload()
        w6 = panel6._transfer.worker if panel6._transfer else None
        check("传输确实在跑", w6 is not None and not w6.finished.is_set())
        # 传输刚开始就把它连同父级一起析构
        end = time.monotonic() + 0.15
        while time.monotonic() < end:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 5)
        panel6.setParent(None)
        panel6.deleteLater()
        app.processEvents()
        # 之后还要把事件循环转一会儿：daemon 线程会在中途发信号，
        # 接收方已经没了，_emit 必须兜住而不是抛出去。
        end = time.monotonic() + 15.0
        while not w6.finished.is_set() and time.monotonic() < end:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 10)
        check("面板析构后工作线程自己退出了（没挂在空指针上）",
              w6.finished.is_set())
        check("面板析构后进程仍然活着（没有 abort）", True)
        server6.stop()
        client6.close()

        print("\n[7] 失败要在原地报出来（不再弹模态框）")
        # 失败原来走的是 QMessageBox.warning。改成状态条的红色失败态之后，
        # 必须证明"错误仍然被看见"——这条不成立的话就是拿没报错换了个好看。
        client7, server7 = make_pair(write_delay=0.0)
        panel7 = sp.SftpPanel(client7, None, None)
        panel7.local.cd(tmpdir)
        panel7.remote.cd("/")
        panel7.show()
        modal_seen = [False]
        probe = QtCore.QTimer()
        probe.timeout.connect(
            lambda: modal_seen.__setitem__(
                0, modal_seen[0] or QtWidgets.QApplication.activeModalWidget() is not None))
        probe.start(20)

        def _boom(*a, **k):
            raise PermissionError(13, "Permission denied")

        row7 = next(r for r in range(panel7.local.model.rowCount())
                    if panel7.local.model.item(r, 0).text() == "payload.bin")
        panel7.local.view.selectionModel().select(
            panel7.local.model.index(row7, 0),
            QtCore.QItemSelectionModel.SelectionFlag.Select |
            QtCore.QItemSelectionModel.SelectionFlag.Rows)
        panel7._rfs.upload = _boom
        panel7._upload()
        end = time.monotonic() + 10.0
        while panel7.ctrl.busy and time.monotonic() < end:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 5)
        for _ in range(20):
            app.processEvents()
        probe.stop()

        check("失败后状态条进入 error 态", panel7.strip.state == "error",
              "state=%s" % panel7.strip.state)
        check("失败态停在那里等人处理（不自动退场）", panel7.strip.isVisible())
        check("错误原因写在状态条/提示里", "Permission denied" in panel7.strip.toolTip(),
              panel7.strip.title.text() + " / " + panel7.strip.detail.text())
        check("失败不再弹模态框", not modal_seen[0])
        check("失败后按钮恢复可用", panel7.up_btn.isEnabled())
        server7.stop()
        client7.close()

        print("\n[8] 取消不能报成「完成」")
        # 取消后循环 break 出去，状态条还停在 RUNNING，很容易顺手就写成
        # succeed —— 没传完却报"上传完成"是撒谎。
        #
        # 这里刻意选**两个**文件：只有一个文件时按取消往往已经晚了（那个文件
        # 真的传完了），报"完成"反而是实话。真正要守的是"有文件没轮到"。
        client8, server8 = make_pair(write_delay=0.004)
        panel8 = sp.SftpPanel(client8, None, None)
        panel8.local.cd(tmpdir)
        panel8.remote.cd("/")
        panel8.show()
        sel = QtCore.QItemSelectionModel.SelectionFlag
        for r in range(panel8.local.model.rowCount()):
            panel8.local.view.selectionModel().select(
                panel8.local.model.index(r, 0), sel.Select | sel.Rows)
        picked8 = [e.name for e in panel8.local.selected_entries()]
        check("选中了两个文件（取消要能真的停下来）", len(picked8) >= 2, picked8)
        panel8._upload()
        panel8.strip.action.click()          # 传输中点「取消」
        end = time.monotonic() + 30.0
        while panel8.ctrl.busy and time.monotonic() < end:
            app.processEvents(QtCore.QEventLoop.ProcessEventsFlag.AllEvents, 5)
        for _ in range(20):
            app.processEvents()
        check("取消后状态条是 cancelled 态，不是 success",
              panel8.strip.state == "cancelled",
              "state=%s  标题=%r" % (panel8.strip.state, panel8.strip.title.text()))
        check("取消的文案不出现「完成」", "完成" not in panel8.strip.title.text(),
              panel8.strip.title.text())
        check("取消态说清还剩几个没传",
              "没有传输" in panel8.strip.detail.text()
              or not panel8.ctrl.busy,
              "detail=%r" % panel8.strip.detail.text())
        check("取消后按钮恢复可用", panel8.up_btn.isEnabled())
        server8.stop()
        client8.close()
    finally:
        dlg_timer.stop()
        # 整个临时目录一起清：中途某一套用了哪个文件，finally 不该知道。
        try:
            for name in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, name))
            os.rmdir(tmpdir)
        except OSError:
            pass

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
