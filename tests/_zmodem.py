"""ZMODEM（lrzsz）这条路：多文件、进度、以及一个静默失效的坑。

**最重要的一条是 [1]。** 现有代码把 `getOpenFileName` 返回的**绝对路径**直接
喂给 `sz`。实测（本仓库自带的 `bin/sz.exe`，Windows）：传绝对路径时 **sz 和
rz 的退出码都是 0，却一个字节都没落地**——界面显示"传输完成"、远端什么都没有。

同时钉住的还有用户报的那条"一次只能传一个文件"：传 basename 时 `sz` 一次能
送完整批（见 [2]）。所以那不是协议限制，是 UI 只给了一个路径。

**平台守卫。** 仓库自带的 lrzsz 是 Windows PE 可执行文件。别的平台上真二进制
对灌那几组（[1][2][6][7][14]）跳过，但**后面那些纯逻辑组照跑**——它们在任何
机器上都该被验证。这一点是被评审指出来的：原来 `main()` 只有一个
`try: ... finally:`，macOS 上第一组就抛 Exec format error 直接退出，
`plan_local_upload` / `split_remote_paths` / `TransferReport` / 忙时闸门
这些与平台无关的断言**一段都跑不到**，可移植逻辑的覆盖率静默归零。

**[10] 那组曾经给了假信心。** 它直接调 `tr._settle(130)`，绕开了唯一坏掉的
那一跳——`ZmodemTransfer` 当时没继承 QObject，用 `QMetaObject.invokeMethod`
跳回 GUI 线程会 TypeError，于是**每次传输结束都收不了尾、闸门整场锁死**，
而测试因为绕开了它一直全绿。现在走真实入口 `_on_proc_exit`。

Checks:
  1. sz 传绝对路径 -> 双方退出码 0 但零落地（真 sz -> 真 rz 对灌）
  2. sz 传 basename 一次能送完整批（多文件本来就支持）
  3. plan_local_upload 永远给 basename，跨目录如实跳过
  4. split_remote_paths 的引号 / 空格规则
  5. parse_progress 认得两种格式串 + 无总量简写 + Countem
  6. ZmodemPump 靠自己数字节给出总量准确的进度
  7. 管道里 lrzsz 根本不吐 Bytes Sent 行（[6] 那条实现的前提）
  8. 忙时闸门：第二次 begin 必须被拒，且引擎压根没被 start
  9. 两个引擎共用同一条状态条
 10. 取消是第三种收尾；ZmodemTransfer 必须是 QObject
 10b. 真实跨线程收尾：成功路径真的落到 success、闸门真的释放
 11. TransferReport 的整批百分比是按文件个数等权算的
 12. ZmodemPump 拒绝绝对路径，不静默
 13. 第一个终态说了算：失败不会被随后的 done 覆盖成成功
 14. 下载方向：字节计数、total=0、内容逐字节正确
 15. 引号往返：剥了就得加回来
 16. report_problem 不许踩掉正在跑的传输
 17. 断线时闸门必须放掉
 18. 引擎起不来时 _zmodem_pump 不能悬空（悬空 = 终端被永久吞掉）
 19. 通道自己关掉（远端 EOF / 抖动）也要放掉闸门
 20. 下载落盘目录：挑第一个真写得进去的，别想当然用 CWD（macOS .app 启动 CWD=/）
"""
import os
import subprocess
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core import lrzsz
from sshclient.ui import main_window as main_window_mod
from sshclient.ui.design.v1.palette import get_palette
from sshclient.ui.transfer_controller import TransferController, TransferReport
from sshclient.ui.transfer_strip import TransferStrip

BIN = os.path.join(ROOT, "bin")
PASS, FAIL = [], []

#: 真二进制对灌只在 Windows 上做得了（仓库自带的是 Windows PE）。
HAVE_LRZSZ = sys.platform == "win32" and os.path.isfile(os.path.join(BIN, "sz.exe"))


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name +
          ("" if ok else "   <- %s" % detail))


def skip(groups, why):
    print("\n[跳过 %s] %s" % (groups, why))


class _FeedFn:
    """把普通函数包成有 .write/.flush 的样子，好让 _drain 统一搬运。"""

    def __init__(self, fn):
        self._fn = fn

    def write(self, b):
        self._fn(b)

    def flush(self):
        pass


def _drain(src, dst, done):
    """把 src 的字节搬到 dst，直到 EOF。**不**主动关 dst 的 stdin。

    搬运出错必须打出来：曾经这里一个 bare ``except: break`` 把
    "dst 是个方法不是文件对象" 这个错误吞成了"什么都没发生"，
    整套进度断言于是读到空列表，还要另找半天才看出真凶。
    """
    while True:
        try:
            b = src.read1(4096)
        except Exception as exc:
            print("     [drain] 读失败：%r" % (exc,))
            break
        if not b:
            break
        try:
            dst.write(b)
            dst.flush()
        except Exception as exc:
            print("     [drain] 写失败：%r" % (exc,))
            break
    done.set()


class FakeChannel:
    """ZmodemPump 眼里的"SSH 通道"，这里换成本机管道。"""

    def __init__(self):
        self.closed = False
        self.sent = 0
        self._to_local = None

    def sendall(self, data):
        self.sent += len(data)
        if self._to_local is not None:
            self._to_local(data)

    def close(self):
        self.closed = True


class _RecvChannel(FakeChannel):
    """还会往外吐字节的通道：`_on_channel_ready` 走的是 `ch.recv`。"""

    def __init__(self, payload=b"hello-from-remote"):
        super().__init__()
        self._payload = payload

    def recv(self, size):
        data, self._payload = self._payload[:size], self._payload[size:]
        return data


# ---------------------------------------------------------------------------
# 真二进制对灌（仅 Windows）
# ---------------------------------------------------------------------------


def sz_to_rz(sz_args, sz_cwd):
    """真 sz -> 真 rz 对灌。返回 ``(sz 退出码, rz 退出码, 收到的文件名列表)``。

    刻意**不**关 sz 的 stdin：关掉等于告诉它"对端没了"，它会直接 128 退出，
    于是所有用例都变成"什么都没传"——第一版探针就栽在这上面。
    """
    recv = tempfile.mkdtemp(prefix="zrecv_")
    szp = subprocess.Popen([os.path.join(BIN, "sz.exe"), "-y"] + sz_args,
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, cwd=sz_cwd)
    rzp = subprocess.Popen([os.path.join(BIN, "rz.exe"), "-y"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, cwd=recv)
    done = threading.Event()
    t1 = threading.Thread(target=_drain, args=(szp.stdout, rzp.stdin, done), daemon=True)
    t2 = threading.Thread(target=_drain, args=(rzp.stdout, szp.stdin, done), daemon=True)
    t1.start()
    t2.start()
    sc = szp.wait(timeout=60)
    rc = rzp.wait(timeout=60)
    t1.join(timeout=5)
    t2.join(timeout=5)
    got = sorted(os.path.relpath(os.path.join(r, f), recv)
                 for r, _d, fs in os.walk(recv) for f in fs)
    return sc, rc, got


def collect_stderr(sz_args, sz_cwd, extra=("-y", "-v")):
    """跑一次真 sz -> 真 rz，只把 sz 的 stderr 收回来。"""
    tmp = tempfile.mkdtemp(prefix="zerr_")
    szp = subprocess.Popen([os.path.join(BIN, "sz.exe")] + list(extra) + list(sz_args),
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, cwd=sz_cwd)
    rzp = subprocess.Popen([os.path.join(BIN, "rz.exe"), "-y"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, cwd=tmp)
    done = threading.Event()
    t1 = threading.Thread(target=_drain, args=(szp.stdout, rzp.stdin, done), daemon=True)
    t2 = threading.Thread(target=_drain, args=(rzp.stdout, szp.stdin, done), daemon=True)
    t1.start()
    t2.start()
    szp.wait(timeout=60)
    szerr = szp.stderr.read().decode("utf-8", "replace")
    try:
        rzp.wait(timeout=10)
    except Exception:
        rzp.kill()
    t1.join(timeout=5)
    t2.join(timeout=5)
    return szerr


def pump_upload(files, cwd):
    """用 ZmodemPump 走完一整趟真上传，收回调记录。

    接线：sz 的 stdout -> FakeChannel.sendall（= 远端在收）-> 本地 rz 的 stdin；
    rz 的 stdout -> pump.feed（= 远端发来的字节）。跑的是真的 ``ZmodemPump``、
    真的 ``bin/sz.exe``、真的 ``bin/rz.exe``。
    """
    ch = FakeChannel()
    recv = tempfile.mkdtemp(prefix="zrecv_")
    rzp = subprocess.Popen([os.path.join(BIN, "rz.exe"), "-y"],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, cwd=recv)
    ch._to_local = lambda b: (rzp.stdin.write(b), rzp.stdin.flush())

    events = []
    feed_done = threading.Event()
    exit_done = threading.Event()

    pump = lrzsz.ZmodemPump(
        channel=ch, direction="upload",
        binary=os.path.join(BIN, "sz.exe"), files=files, cwd=cwd,
        on_log=lambda s: events.append(("log", s)),
        on_finished=lambda c: (events.append(("exit", c)), exit_done.set()),
        on_file=lambda i, n: events.append(("file", i, n)),
        on_progress=lambda d, t_, b: events.append(("prog", d, t_, b)),
    )
    t = threading.Thread(target=_drain,
                         args=(rzp.stdout, _FeedFn(pump.feed), feed_done), daemon=True)
    t.start()
    pump.start()
    if not exit_done.wait(timeout=60):
        print("     （超时：lrzsz 没在 60 秒内退出）")
    t.join(timeout=5)
    try:
        rzp.wait(timeout=10)
    except Exception:
        rzp.kill()
    got = sorted(os.path.relpath(os.path.join(r, f), recv)
                 for r, _d, fs in os.walk(recv) for f in fs)
    return pump, events, got, recv


def pump_download(targets, recv):
    """下载方向：真 sz（远端）-> pump（本地 rz）。真二进制对灌。

    **一个** sz 进程按顺序送多个文件，不是多个 sz 并发灌一个 rz —— 后者是
    协议违规（两个发送端抢同一条 ZMODEM 会话），测出来的是假失败。
    """
    ch = FakeChannel()
    src = tempfile.mkdtemp(prefix="zsrc_")
    payloads = {}
    for n in targets:
        with open(os.path.join(src, n), "wb") as f:
            f.write((n[0] * 5000).encode("utf-8"))
        payloads[n] = (n[0] * 5000).encode("utf-8")
    sender = subprocess.Popen(
        [os.path.join(BIN, "sz.exe"), "-y"] + list(targets),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=src)
    # 回程：本地 rz 的响应要写回远端 sz 的 stdin。漏了这条，sender 收不到
    # ZRINIT 就一直等，零字节落地。
    ch._to_local = lambda b: (sender.stdin.write(b), sender.stdin.flush())
    done = threading.Event()
    exit_done = threading.Event()
    events = []

    pump = lrzsz.ZmodemPump(
        channel=ch, direction="download", binary=os.path.join(BIN, "rz.exe"),
        files=[], cwd=recv,
        on_log=lambda s: events.append(("log", s)),
        on_finished=lambda c: (events.append(("exit", c)), exit_done.set()),
        on_file=lambda i, n: events.append(("file", i, n)),
        on_progress=lambda d, t_, b: events.append(("prog", d, t_, b)),
    )
    t = threading.Thread(target=_drain,
                         args=(sender.stdout, _FeedFn(pump.feed), done), daemon=True)
    t.start()
    pump.start()
    if not exit_done.wait(timeout=60):
        print("     （超时：rz 没在 60 秒内退出）")
    t.join(timeout=5)
    got = sorted(os.path.relpath(os.path.join(r, f), recv)
                 for r, _d, fs in os.walk(recv) for f in fs)
    return pump, events, got, src, payloads


def _same_bytes(src_dir, recv_dir, names):
    """落地文件与源文件逐字节相同。

    调用方必须自己保证 ``names`` 非空再问这个 —— ``all([])`` 是 True，
    "什么都没收到"会因此被判成"内容完全正确"。这条已经坑过两轮。
    """
    if not names:
        return False
    for n in names:
        a = os.path.join(src_dir, n)
        b = os.path.join(recv_dir, n)
        if not os.path.isfile(b):
            return False
        if os.path.getsize(a) != os.path.getsize(b):
            return False
        if open(a, "rb").read() != open(b, "rb").read():
            return False
    return True


# ---------------------------------------------------------------------------
# 走真二进制的组
# ---------------------------------------------------------------------------


def binary_checks(src, other):
    print("\n[1] sz 传绝对路径 = 静默失效（真二进制对灌）")
    sc, rc, got = sz_to_rz([os.path.join(src, "a.txt")], src)
    check("绝对路径：退出码确实是 0（所以界面看不出问题）",
          sc == 0 and rc == 0, "sz=%s rz=%s" % (sc, rc))
    check("绝对路径：一个字节都没落地", got == [], "落地了 %s" % got)

    print("\n[2] 传 basename 一次能送完整批")
    sc, rc, got = sz_to_rz(["a.txt", "b.txt"], src)
    check("basename 一次收齐两个文件", got == ["a.txt", "b.txt"],
          "sz=%s rz=%s got=%s" % (sc, rc, got))
    sc, rc, got = sz_to_rz(["a.txt", "b.txt", "c.log"], src)
    check("三个文件也一次送完", got == ["a.txt", "b.txt", "c.log"], "got=%s" % got)

    print("\n[6] ZmodemPump 的进度是自己数字节，不是读 lrzsz 的人读输出")
    pump, events, got, recv = pump_upload(["a.txt", "b.txt"], src)
    progs = [e for e in events if e[0] == "prog"]
    files = [e for e in events if e[0] == "file"]
    exits = [e for e in events if e[0] == "exit"]
    want_total = (os.path.getsize(os.path.join(src, "a.txt"))
                  + os.path.getsize(os.path.join(src, "b.txt")))
    check("真跑一趟收到了进度回调", len(progs) > 0, "progs=%d" % len(progs))
    check("总量等于两个文件的大小之和",
          bool(progs) and progs[-1][2] == want_total,
          "got=%s want=%s" % (progs[-1] if progs else None, want_total))
    check("已传数单调不减",
          all(progs[i][1] <= progs[i + 1][1] for i in range(len(progs) - 1)),
          str(progs[:4]))
    # 运行期间封顶 99%：协议流含 ZMODEM 头与 ACK，字节发够了会早于
    # 文件真的传完，直接钳到 100% 会让条子提前满格然后停滞。
    # 这里只断言**性质**（已传 < 总量），不钉死具体比例——那个比例依文件
    # 大小、链路、块大小而变，写成数字就是编造。
    check("运行期间封顶 99%，不会因为协议开销提前钉在满格",
          all(e[1] < e[2] for e in progs if e[2] > 0 and e[1] > 0),
          str([e for e in progs if e[2] > 0][-3:]))
    check("给了速度", any(e[3] > 0 for e in progs), str(progs[:2]))
    check("sz -v 让 pump 数出文件名", len(files) >= 2, "files=%s" % files[:5])
    # 第一个 on_file 一定早于第一行 `Sending:`，那时名字还是空——那是"已经开始
    # 搬了但还不知道是哪个文件"，不是 bug。断言只看有名字的那些。
    named = [e[2] for e in files if e[2]]
    check("文件名和实际对得上", named == ["a.txt", "b.txt"], str(named))
    check("第二个文件的索引是 1", any(e[1] == 1 for e in files),
          str([e[1] for e in files][:6]))
    check("退出码 0", exits and exits[-1][1] == 0, str(exits))
    check("真收到了两个文件", got == ["a.txt", "b.txt"], str(got))
    # 内容也要对：只比文件名的话，半截文件照样 pass。**不能只写 all(...)** ——
    # 空列表时它恒真，上一版就是这么把"什么都没收到"测成绿的。
    check("落地文件与源逐字节相同（不只是文件名对）",
          _same_bytes(src, recv, got), "got=%s" % got)

    print("\n[7] 管道里 lrzsz 根本不吐 Bytes Sent 行（整条进度实现的前提）")
    # 整条"自己数字节"的实现建立在这个事实上，所以把它钉死：哪天换了
    # lrzsz 构建、管道里也开始打进度行了，这条会红，提示要重新评估
    # parse_progress 那层兜底还值不值得留。
    errs = collect_stderr(["a.txt", "b.txt"], src)
    check("sz 的 stderr 里确实没有 Bytes Sent 行",
          "Bytes Sent" not in errs, repr(errs[:200]))
    check("但 -v 的 Sending 行在（这才是逐文件信号的来源）",
          "Sending:" in errs, repr(errs[:200]))
    check("Sending 行确实带了文件名", "a.txt" in errs and "b.txt" in errs,
          repr(errs[:200]))

    print("\n[14] 下载方向")
    recv14 = tempfile.mkdtemp(prefix="zdl_")
    p14, ev14, got14, src_dir, payloads = pump_download(["a.txt", "b.log"], recv14)
    progs14 = [e for e in ev14 if e[0] == "prog"]
    check("下载也收到了进度回调", len(progs14) > 0, "progs=%d" % len(progs14))
    check("下载的已收字节数是递增的",
          all(progs14[i][1] <= progs14[i + 1][1]
              for i in range(len(progs14) - 1)), str(progs14[:4]))
    check("下载拿不到远端文件大小，total=0（退回不定态而不是编百分比）",
          all(e[2] == 0 for e in progs14), str(progs14[:3]))
    check("下载收到了两个文件", got14 == ["a.txt", "b.log"], str(got14))
    # **不能只写 all(...)**：空列表时 all() 恒真，于是"零文件落地"也能通过
    # "内容逐字节正确"——上一版就是这么把一个没接回程的空跑测成绿的。
    check("下载到的内容逐字节正确（不只是文件名对）",
          _same_bytes(src_dir, recv14, got14), "got=%s" % got14)
    check("下载方向跑的是 rz 而不是 sz",
          os.path.basename(p14.binary).lower().startswith("rz"), p14.binary)


# ---------------------------------------------------------------------------
# 与平台无关的组
# ---------------------------------------------------------------------------


class _Engine:
    """假引擎：只记录自己被调了哪些方法。"""

    def __init__(self, log, tag):
        self._log = log
        self._tag = tag

    def start(self):
        self._log.append((self._tag, "start"))

    def request_cancel(self):
        self._log.append((self._tag, "cancel"))


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    src = tempfile.mkdtemp(prefix="zsrc_")
    for name, n in (("a.txt", 3000), ("b.txt", 5000), ("c.log", 100)):
        with open(os.path.join(src, name), "w", encoding="utf-8") as f:
            f.write("x" * n)
    other = tempfile.mkdtemp(prefix="zoth_")
    with open(os.path.join(other, "d.txt"), "w", encoding="utf-8") as f:
        f.write("y" * 200)
    # [20] 自己一个根：底下要造**目录**，而 finally 里那个清理只会
    # os.remove 文件，碰上目录会 OSError 然后整块放弃清理。
    dlroot = tempfile.mkdtemp(prefix="zdl_")

    try:
        if HAVE_LRZSZ:
            binary_checks(src, other)
        else:
            skip("[1][2][6][7][14]",
                 "仓库自带的 lrzsz 是 Windows PE 可执行文件，本机 %s" % sys.platform)

        # ------------------------------------------------------------ 3
        print("\n[3] plan_local_upload 永远给 basename")
        cwd, names, skipped = lrzsz.plan_local_upload(
            [os.path.join(src, "a.txt"), os.path.join(src, "b.txt")])
        check("同目录：给 basename 且不跳过",
              names == ["a.txt", "b.txt"] and not skipped, "%s %s" % (names, skipped))
        cwd, names, skipped = lrzsz.plan_local_upload(
            [os.path.join(src, "a.txt"), os.path.join(other, "d.txt")])
        check("跨目录：只取第一个的目录，其余如实跳过",
              names == ["a.txt"] and len(skipped) == 1, "%s %s" % (names, skipped))
        check("cwd 是第一个文件所在目录，不是当前工作目录",
              os.path.normcase(cwd) == os.path.normcase(src), cwd)
        dup = lrzsz.plan_local_upload(
            [os.path.join(src, "a.txt"), os.path.join(src, "a.txt")])
        check("重复选同一个文件只算一次", dup[1] == ["a.txt"], str(dup[1]))
        allbad = True
        for p in (os.path.join(src, "a.txt"), os.path.join(other, "d.txt")):
            _c, ns, _s = lrzsz.plan_local_upload([p])
            allbad = allbad and all(os.path.basename(n) == n and not os.path.isabs(n)
                                    for n in ns)
        check("任何输入给出去的都是纯文件名", allbad)

        # ------------------------------------------------------------ 4
        print("\n[4] split_remote_paths")
        check("空格分隔", lrzsz.split_remote_paths("a.log b.log") == ["a.log", "b.log"])
        check("双引号包住带空格的路径",
              lrzsz.split_remote_paths('a.log "my file.txt"') == ["a.log", "my file.txt"])
        check("单引号也行",
              lrzsz.split_remote_paths("'/var/log/x y'") == ["/var/log/x y"])
        check("连续空格不会造出空项",
              lrzsz.split_remote_paths("  a   b  ") == ["a", "b"])
        check("空串给空表", lrzsz.split_remote_paths("   ") == [])

        # ------------------------------------------------------------ 5
        print("\n[5] parse_progress")
        p = lrzsz.parse_progress("Bytes Sent:  1024/  4096   BPS:  2048    ETA 00:01")
        check("认得 sz 的格式串", p is not None and p.transferred == 1024
              and p.total == 4096 and p.bps == 2048 and p.eta_secs == 1, str(p))
        p = lrzsz.parse_progress("Bytes received:  1024/  4096   BPS:  2048  ETA 00:01")
        check("认得 rz 的格式串", p is not None and p.transferred == 1024
              and p.total == 4096, str(p))
        p = lrzsz.parse_progress("Bytes Sent:  1024   BPS:  2048")
        check("无总量的简写形式 total=0、fraction=None",
              p is not None and p.total == 0 and p.fraction is None, str(p))
        p = lrzsz.parse_progress("Countem: 002 b.txt")
        check("Countem 给出序号和文件名",
              p is not None and p.counter == 2 and p.filename == "b.txt", str(p))
        check("普通行返回 None", lrzsz.parse_progress("hello world") is None)
        check("空行返回 None", lrzsz.parse_progress("   ") is None)

        # ------------------------------------------------------------ 8
        print("\n[8] 忙时闸门")
        ctrl = TransferController(get_palette("dark"), None)
        started = []
        e1, e2 = _Engine(started, "e1"), _Engine(started, "e2")
        ok1 = ctrl.begin("upload", "上传", e1, 2)
        ok2 = ctrl.begin("download", "下载", e2, 1)
        check("第一次 begin 被接受", ok1 is True)
        check("第二次 begin 被拒（同时点两次上传的那个洞）", ok2 is False, str(ok2))
        check("被拒的那次引擎压根没被 start 过（不是「没报错」）",
              started == [], "started=%s" % started)
        check("控制器处于 busy", ctrl.busy is True)
        ctrl.succeed("")
        check("收尾之后不再是 busy", ctrl.busy is False)
        check("收尾之后又能发起新传输", ctrl.begin("download", "下载", e2, 1) is True)

        # ------------------------------------------------------------ 9
        print("\n[9] 两个引擎共用同一条状态条")
        from sshclient.ui.sftp_panel import SftpPanel

        class _FakeSftp:
            def getcwd(self):
                return "/"

            def listdir(self, path):
                return []

            def stat(self, path):
                raise OSError(path)

        shared = TransferController(get_palette("dark"), None)
        panel = SftpPanel(_FakeSftp(), get_palette("dark"), None, ctrl=shared)
        check("注入的控制器被 SftpPanel 用上了", panel.ctrl is shared)
        check("文件 tab 和工具栏是同一条状态条", panel.strip is shared.strip)
        solo = SftpPanel(_FakeSftp(), get_palette("dark"), None)
        check("不注入时自建一套，不共享", solo.ctrl is not shared)

        # ----------------------------------------------------------- 10
        print("\n[10] 取消是第三种收尾")
        from sshclient.ui.main_window import ZmodemTransfer

        # **必须**是 QObject：收尾从 pump 的等待线程回来，而第一版这里是普通
        # 类，用 QMetaObject.invokeMethod 跳线程直接 TypeError，于是每次传输
        # 结束都收不了尾、闸门整场锁死。
        check("ZmodemTransfer 是 QObject（否则没法跨线程跳回来）",
              issubclass(ZmodemTransfer, QtCore.QObject),
              str([c.__name__ for c in ZmodemTransfer.__mro__[:3]]))

        ch = FakeChannel()
        ctrl2 = TransferController(get_palette("dark"), None)
        tr = ZmodemTransfer(ctrl2, ch, "upload",
                            os.path.join(BIN, "sz.exe") if HAVE_LRZSZ else "sz",
                            ["a.txt"], src, targets=["a.txt"])
        ctrl2.begin("upload", "上传", tr, 1)
        check("begin 之后确实进入忙时状态（下面才有意义）", ctrl2.busy is True)
        tr.request_cancel()
        # 走**真实入口** `_on_proc_exit`（pump 的等待线程回调它），再泵事件
        # 循环让信号落到 GUI 线程。直接调 `_settle` 正好绕开了唯一坏掉的那一跳。
        tr._on_proc_exit(130)
        app.processEvents()
        check("取消后是 cancelled 态，不是 success",
              ctrl2.strip.state == TransferStrip.CANCELLED,
              "state=%s" % ctrl2.strip.state)
        check("取消的文案不说「完成」", "完成" not in ctrl2.strip.title.text(),
              ctrl2.strip.title.text())
        check("取消后控制器不 busy", not ctrl2.busy)

        # ----------------------------------------------------------- 10b
        print("\n[10b] 真实跨线程收尾：成功路径")
        ch3 = FakeChannel()
        ctrl3 = TransferController(get_palette("dark"), None)
        tr3 = ZmodemTransfer(ctrl3, ch3, "upload",
                             os.path.join(BIN, "sz.exe") if HAVE_LRZSZ else "sz",
                             ["a.txt"], src, targets=["a.txt"])
        check("begin 被接受", ctrl3.begin("upload", "上传", tr3, 1) is True)
        # 这条必须真的锁上：忘了 begin 的话"闸门释放"恒真——一个从来没锁过的
        # 门当然会"释放"，它证明不了任何事。
        check("begin 之后确实进入忙时状态", ctrl3.busy is True)
        tr3._on_proc_exit(0)
        app.processEvents()
        check("成功路径真的落到 success 态",
              ctrl3.strip.state == TransferStrip.SUCCESS,
              "state=%s 标题=%r" % (ctrl3.strip.state, ctrl3.strip.title.text()))
        check("成功之后闸门释放（不释放就再也传不了）", ctrl3.busy is False)
        check("还能再发起新传输",
              ctrl3.begin("download", "下载", _Engine(started, "e3"), 1) is True)

        # ----------------------------------------------------------- 11
        print("\n[11] 整批百分比按文件个数等权")
        r = TransferReport(4)
        r.set_file(0, "a")
        pct, _d = r.render(50, 100)
        check("第一个文件传一半 = 12%", pct == 12, str(pct))
        r.set_file(3, "d")
        pct, _d = r.render(0, 100)
        check("第四个文件刚开始 = 75%", pct == 75, str(pct))
        r2 = TransferReport(1)
        r2.set_file(0, "a")
        pct, detail = r2.render(256, 1024)
        check("单文件就是它自己", pct == 25, str(pct))
        check("detail 带字节数和文件名",
              "256 B" in detail and "1.0 KB" in detail and "a" in detail, detail)
        pct, detail = r2.render(10, 0)
        check("总量未知时 pct 是 None（退回不定态）", pct is None, str(pct))
        check("总量未知时 detail 不编造分母", "/" not in detail, detail)

        # ----------------------------------------------------------- 12
        print("\n[12] ZmodemPump 拒绝绝对路径，不静默")
        ch2 = FakeChannel()
        got_exit = []
        bad = lrzsz.ZmodemPump(channel=ch2, direction="upload",
                               binary=os.path.join(BIN, "sz.exe") if HAVE_LRZSZ else "sz",
                               files=[os.path.join(src, "a.txt")], cwd=src,
                               on_log=lambda s: None,
                               on_finished=lambda c: got_exit.append(c))
        bad.start()
        check("传绝对路径时根本不起进程，直接报非零退出码",
              bool(got_exit) and got_exit[0] != 0, str(got_exit))
        check("没有往通道写任何东西", ch2.sent == 0, "sent=%d" % ch2.sent)

        # ----------------------------------------------------------- 13
        print("\n[13] 第一个终态说了算（失败不能被随后的 done 覆盖成成功）")
        c13 = TransferController(get_palette("dark"), None)
        c13.begin("upload", "上传", _Engine(started, "e13"), 1)
        c13.fail("上传 a.txt 失败：\nPermissionError: Permission denied")
        check("失败后是 error 态", c13.strip.state == TransferStrip.ERROR,
              c13.strip.state)
        c13.succeed("上传完成")
        check("随后的 succeed 被忽略", c13.strip.state == TransferStrip.ERROR,
              c13.strip.state)
        check("失败原因还在提示里",
              "Permission denied" in c13.strip.toolTip(), c13.strip.toolTip())
        c13.cancel("已取消")
        check("随后的 cancel 也被忽略", c13.strip.state == TransferStrip.ERROR,
              c13.strip.state)
        check("收尾之后不再 busy", not c13.busy)

        # ----------------------------------------------------------- 15
        print("\n[15] 引号往返：剥了就得加回来")
        import shlex as _sx
        raw = 'a.log "my file.txt" /var/log/syslog'
        targets = lrzsz.split_remote_paths(raw)
        # 这就是 _on_download 实际发给远端的那一行
        line = "sz " + _sx.join(targets)
        remote_argv = _sx.split(line)[1:]
        check("远端 shell 切出来的参数数和用户输入的一致",
              remote_argv == ["a.log", "my file.txt", "/var/log/syslog"],
              "发出去=%r 远端看到=%s" % (line, remote_argv))
        # 没有加回引号时是什么样（这条是防回归的对照）
        naive = "sz " + " ".join(targets)
        check("对照：直接 join 会把带空格的路径拆成两个参数",
              len(_sx.split(naive)) - 1 != len(remote_argv),
              "%r -> %s" % (naive, _sx.split(naive)[1:]))

        # ----------------------------------------------------------- 16
        print("\n[16] report_problem 不许踩掉正在跑的传输")
        c16 = TransferController(get_palette("dark"), None)
        c16.begin("upload", "上传", _Engine(started, "e16"), 1)
        c16.report_problem("找不到 lsz", "装一下 lrzsz")
        check("忙时 report_problem 只记日志，不改状态条",
              c16.strip.state == TransferStrip.RUNNING, c16.strip.state)
        c16.succeed("")
        check("那批传输自己的收尾仍然算数",
              c16.strip.state == TransferStrip.SUCCESS, c16.strip.state)
        c16.report_problem("找不到 lrz", "装一下 lrzsz")
        check("空闲时 report_problem 正常报出来",
              c16.strip.state == TransferStrip.ERROR, c16.strip.state)
        check("空闲时 report_problem 不该占着闸门（下次还能传）", not c16.busy)

        # ----------------------------------------------------------- 17
        print("\n[17] 断线时闸门必须放掉")
        c17 = TransferController(get_palette("dark"), None)
        c17.begin("upload", "上传", _Engine(started, "e17"), 2)
        check("先确认它真的在忙（下面才有意义）", c17.busy is True)
        c17.reset("断线")
        check("reset 之后不再 busy", c17.busy is False)
        check("reset 之后状态条退场", not c17.strip.isVisible())
        check("reset 之后还能发起新传输",
              c17.begin("upload", "上传", _Engine(started, "e18"), 1) is True)

        # ----------------------------------------------------------- 18
        print("\n[18] 引擎起不来时，终端不能被永久吞掉")
        # 这条是被架构复审实测挖出来的真缺陷：_make_zmodem_transfer 一建好就把
        # self._zmodem_pump 挂上，而 start_engine 失败（Popen 抛 OSError）时没人
        # 清它。此后 _on_channel_ready 命中 `if self._zmodem_pump is not None:
        # pump.feed(data); return`，feed 因为 _proc is None 直接 return ——
        # 远端来的每个字节都被吞掉，终端从此完全哑掉，而 _reset_zmodem_state()
        # 也不管这个字段，所以**重连也救不回来**，只能关标签页。
        from sshclient.ui.main_window import SessionTab

        tab18 = SessionTab()
        ch18 = _RecvChannel()
        tab18._channel = ch18

        seen = []
        real_write = tab18.terminal.write
        tab18.terminal.write = lambda data: seen.append(data)

        real_start = lrzsz.ZmodemPump.start
        real_resolve = lrzsz.resolve_binary
        starts = []

        def _boom(self):
            # 模拟"二进制过了 is_file() 检查但 Popen 失败"
            starts.append("start")
            raise OSError(216, "文件名、目录名或卷标语法不正确")

        lrzsz.ZmodemPump.start = _boom
        lrzsz.resolve_binary = lambda name: "sz" if name == "sz" else "rz"
        try:
            tab18._pending_zmodem_files = [os.path.join(src, "a.txt")]
            tab18._pending_zmodem_cwd = src
            tab18._start_zmodem_upload()
        finally:
            lrzsz.ZmodemPump.start = real_start
            lrzsz.resolve_binary = real_resolve

        # 这一条是防恒真的：没真的踩到 start_engine，下面全都不成立
        check("确实踩到了 start_engine（防这一组恒真）", starts == ["start"],
              str(starts))
        check("启动失败后 _zmodem_pump 是空的（不是悬着一个没 _proc 的 pump）",
              tab18._zmodem_pump is None, repr(tab18._zmodem_pump))
        check("启动失败后闸门已释放（不释放这条连接就再也传不了）",
              tab18.transfer_ctrl.busy is False)
        check("状态条落的是失败态，不是永远 running",
              tab18.transfer_ctrl.strip.state == TransferStrip.ERROR,
              tab18.transfer_ctrl.strip.state)

        # 关键后果：终端还能不能收到东西
        seen.clear()
        tab18._on_channel_ready(0)
        check("启动失败之后远端字节仍然进得了终端",
              seen == [b"hello-from-remote"],
              "终端收到 %r" % (seen,))

        # 忙时闸门拒掉的那条路径也不能留下悬空的 pump
        seen.clear()
        lrzsz.resolve_binary = lambda name: "sz"
        try:
            ctrl18 = tab18.transfer_ctrl
            ctrl18.begin("download", "下载", _Engine(started, "e18b"), 1)
            tab18._pending_zmodem_files = [os.path.join(src, "a.txt")]
            tab18._pending_zmodem_cwd = src
            tab18._start_zmodem_upload()
        finally:
            lrzsz.resolve_binary = real_resolve
        check("闸门拒掉的那次也没有留下悬空的 pump",
              tab18._zmodem_pump is None, repr(tab18._zmodem_pump))
        tab18.transfer_ctrl.reset("测试收尾")
        seen.clear()
        ch18._payload = b"hello-from-remote"     # 上一轮 recv 吃干净了，补回去
        tab18._on_channel_ready(0)
        check("闸门拒绝之后终端同样没被吞",
              seen == [b"hello-from-remote"], "终端收到 %r" % (seen,))
        tab18.terminal.write = real_write
        tab18.close()

        # ----------------------------------------------------------- 19
        print("\n[19] 通道自己关掉（远端 EOF / 抖动）也要放掉闸门")
        # _on_channel_closed 以前只处理通道和 notifier，不碰传输。于是断线后
        # 闸门还锁着、状态条停在 running，看上去像还在传。
        tab19 = SessionTab()
        ch19 = FakeChannel()
        tab19._channel = ch19
        c19 = tab19.transfer_ctrl
        c19.begin("upload", "上传", _Engine(started, "e19"), 2)
        check("先确认它真的在忙（下面才有意义）", c19.busy is True)
        tab19._zmodem_pump = lrzsz.ZmodemPump(
            channel=ch19, direction="upload", binary="sz", files=["a.txt"],
            cwd=src)
        tab19._on_channel_closed()
        check("通道关了之后闸门放掉", c19.busy is False)
        check("通道关了之后状态条退场（不再假装还在 running）",
              not c19.strip.isVisible())
        check("通道关了之后 _zmodem_pump 清空", tab19._zmodem_pump is None,
              repr(tab19._zmodem_pump))
        tab19.close()

        # ------------------------------------------------------------ 21
        print("\n[21] 工具栏上传：ZRQINIT 那一批字节必须到本地 sz 的 stdin")
        # **这条是被真机联调逮到的，离线一直没红。**
        #
        # `_on_upload` 发完 `rz` 之后设 `_zmodem_pending_direction="upload"`，
        # 于是 `_on_channel_ready` 里 `== "download"` 不匹配、`"upload"` 又没人接，
        # 一路掉到最下面：`singleShot(0, _ask_zmodem_direction)` 把那一批
        # **含 ZRQINIT 的字节**交给终端（`terminal.write(data)`），
        # 而 singleShot 那一拍里 `_ask_zmodem_direction` 第一句就是
        # `if self._zmodem_pending_direction is not None: return`
        # —— 方向是工具栏自己设的，于是它什么都不做直接返回。
        #
        # 净效果：**含 ZRQINIT 的那一批永远到不了本地 sz.exe 的 stdin。**
        # sz 是被动方，启动后就等这个 ZRQINIT，收不到就一直等；远端 rz 也一直等，
        # 两边互等，零异常零报错。真机症状：远端 `** waiting to receive.` 上了屏，
        # 文件没到，而状态条和闸门都干干净净（pump 从来没起过）。
        #
        # **下载方向没这个毛病**，正是靠 `:754` 那条分支把 `data` 喂给了 pump。
        # 所以下面钉的是「两个方向对称」这条不变量，不是某个字面量。
        from sshclient.ui.main_window import SessionTab as _Tab21

        ZRQINIT = b"**\x18B00000000000000\r"      # 远端 rz 发的那一下

        class _FakePump:
            """只记录 feed 收到什么，不 spawn 真进程。"""

            def __init__(self, *a, **k):
                self.fed = []
                self.started = False

            def start(self):
                self.started = True

            def feed(self, data):
                self.fed.append(data)

            def stop(self):
                pass

        _real_pump_cls = lrzsz.ZmodemPump

        def _run_uploads(pending_direction, files):
            tab = _Tab21()
            ch = _RecvChannel(payload=ZRQINIT + b"\r\n")
            tab._channel = ch
            to_term = []
            tab.terminal.write = lambda d: to_term.append(d)
            made = {}
            real_mk = tab._make_zmodem_transfer

            def _mk(direction, binary, flist, cwd, note, targets=None):
                made["direction"] = direction
                made["files"] = list(flist)
                return real_mk(direction, binary, flist, cwd, note, targets)

            tab._make_zmodem_transfer = _mk
            # 只换 pump，不换 ZmodemTransfer —— 后者的 __init__ 需要真的
            # QApplication，替身反而把这条判据变成在测替身。
            lrzsz.ZmodemPump = _FakePump
            try:
                if pending_direction is not None:
                    tab._zmodem_pending_direction = pending_direction
                tab._pending_zmodem_files = list(files)
                tab._pending_zmodem_cwd = src
                tab.transfer_ctrl.begin = lambda *a, **k: True
                tab.transfer_ctrl.start_engine = lambda *a, **k: True
                tab._on_channel_ready(0)
                pump = tab._zmodem_pump
                fed = list(getattr(pump, "fed", [])) if pump else []
                made_dir = made.get("direction")
            finally:
                lrzsz.ZmodemPump = _real_pump_cls
            tab.close()
            return fed, to_term, made_dir

        up_fed, up_term, up_dir = _run_uploads("upload", ["a.txt"])
        check("上传方向：ZRQINIT 那一批被喂给了 pump（不是终端）",
              any(ZRQINIT in d for d in up_fed),
              "pump 收到 %r / 终端收到 %r" % (up_fed, up_term))
        check("上传方向：pump 确实是 upload（方向没串）",
              up_dir == "upload", repr(up_dir))
        dn_fed, dn_term, dn_dir = _run_uploads("download", ["a.txt"])
        check("对照组：下载方向本来就能拿到这一批（两个方向必须对称）",
              any(ZRQINIT in d for d in dn_fed),
              "pump 收到 %r" % (dn_fed,))

        # 元断言：装回缺陷版 —— upload 方向不喂。
        def _defect_ready(tab, data):
            """缺陷版原样：`"upload"` 没人接，一路掉到终端。"""
            tab._zmodem_detector.feed(data)
            tab.terminal.write(data)

        _tab_d = _Tab21()
        _tab_d._channel = _RecvChannel(payload=ZRQINIT + b"\r\n")
        _d_term = []
        _tab_d.terminal.write = lambda d: _d_term.append(d)
        _defect_ready(_tab_d, ZRQINIT + b"\r\n")
        _d_fed = []
        check("元断言：缺陷版把 ZRQINIT 送进了终端、pump 一个字节都没拿到"
              "（所以上面那两条不是恒绿）",
              any(ZRQINIT in d for d in _d_term) and not _d_fed,
              "终端=%r pump=%r" % (_d_term, _d_fed))
        _tab_d.close()

        # ------------------------------------------------------------ 22
        print("\n[22] 下载落盘目录得挑一个真写得进去的")
        # 原来直接 os.getcwd()。macOS 从 .app 双击启动时 CWD 是 /，rz 往 /
        # 写必然权限拒绝 —— 而失败发生在子进程里，报出来的话用户看不懂。
        #
        # **"写不进去"用「拿一个普通文件当父目录」来造，不用 chmod。**
        # Windows 上 chmod 对目录只翻只读位、语义不可靠；而 mkdir 撞上一个
        # 已存在的文件，三平台行为一致：抛异常 → 退到下一档。跨平台守卫要
        # 是靠"三平台都成立"的那种理由，不能靠本机碰巧。
        blocker = os.path.join(dlroot, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        impossible = os.path.join(blocker, "sub")
        raised = False
        try:
            os.mkdir(impossible)
        except OSError:
            raised = True
        check("对照组：这个目录真的建不出来（不然下面全是空断言）", raised)

        good = os.path.join(dlroot, "dl")
        got = lrzsz.download_dir([impossible, good])
        check("写不进去的那档被跳过，落到第二个",
              os.path.normcase(got) == os.path.normcase(good), got)
        check("顺带把目录建出来了（rz 自己不会建目录，只往 cwd 里写）",
              os.path.isdir(good))
        left = os.listdir(good)
        check("探针文件用完就删了，不在用户目录里留垃圾", left == [], str(left))

        first = os.path.join(dlroot, "first")
        got1 = lrzsz.download_dir([first, good])
        check("第一档可写就直接用，不无谓地退到后面（用户期待落在启动目录）",
              os.path.normcase(got1) == os.path.normcase(first), got1)

        got2 = lrzsz.download_dir([impossible])
        check("全都不行时返回 None（不兜底一条已知写不进去的路径）",
              got2 is None, got2)

        # ---- 负向对照：装回缺陷版（不试写，直接拿第一档）
        def naive_download_dir(candidates=None):
            return candidates[0]

        check("元断言：缺陷版会选中那个根本建不出来的目录",
              os.path.normcase(naive_download_dir([impossible, good]))
              == os.path.normcase(impossible))
        check("元断言：缺陷版在第一档可写时**恰好蒙对**"
              "（所以上面那几条不是靠瞎报通过的）",
              os.path.normcase(naive_download_dir([first, good]))
              == os.path.normcase(first))
        check("元断言：缺陷版全都不行时给回一个不存在的路径",
              naive_download_dir([impossible]) == impossible)
        check("元断言：三条真判据里，缺陷版恰好只蒙对第一条",
              sum([os.path.normcase(naive_download_dir([first, good]))
                   == os.path.normcase(first),
                   os.path.normcase(naive_download_dir([impossible, good]))
                   == os.path.normcase(good),
                   naive_download_dir([impossible]) is None]) == 1)

        # ---- 守卫：别把这行改回 os.getcwd()
        mw_path = os.path.join(ROOT, "sshclient", "ui", "main_window.py")
        with open(mw_path, encoding="utf-8") as f:
            src_mw = f.read()
        i = src_mw.find('_make_zmodem_transfer("download"')
        window = src_mw[max(0, i - 220):i + 220] if i >= 0 else ""
        check("下载那处真的走 download_dir()，不是 os.getcwd()",
              i >= 0 and "download_dir()" in window, window)
    finally:
        # dlroot 底下有目录，os.walk 收干净（原来的清理只 os.remove 文件）。
        for root, dirs, files in os.walk(dlroot, topdown=False):
            for n in files:
                try:
                    os.remove(os.path.join(root, n))
                except OSError:
                    pass
            for n in dirs:
                try:
                    os.rmdir(os.path.join(root, n))
                except OSError:
                    pass
        try:
            os.rmdir(dlroot)
        except OSError:
            pass
        for d in (src, other):
            try:
                for n in os.listdir(d):
                    os.remove(os.path.join(d, n))
                os.rmdir(d)
            except OSError:
                pass

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
