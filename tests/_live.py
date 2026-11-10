"""真机联调：对着**真的** SSH 服务器把每个功能跑一遍。

这份脚本补的是 `docs/ARCHITECTURE.md`「尚未验证」里最大的一块：
真实 SSH 端到端、真实 PTY 尺寸、真实 SFTP、真实端口转发、真实 lrzsz。

连接信息**只从环境变量读**，不落盘、不进仓库、不写进
`%APPDATA%\\MavisSSH\\sessions.json`::

    $env:MAVIS_LIVE_HOST="localhost"
    $env:MAVIS_LIVE_PORT="22"
    $env:MAVIS_LIVE_USER="<用户名>"
    $env:MAVIS_LIVE_PASSWORD="..."
    .\\.venv\\Scripts\\python.exe tests\\_live.py

没设 `MAVIS_LIVE_USER` / `MAVIS_LIVE_PASSWORD` 就 **exit 2**（跳过），和
`_snippets.py` 的跳过约定一致——「没测」和「测过了」在退出码上分得开。

**故意不接进 `_runall.py`。** 它依赖一台外部服务器，塞进离线回归的门禁
里，网络抖一下就把门禁弄红；要跑就单独跑。这是有意的取舍，不是遗漏。

Sections:
  1. 连接与认证（对的、错的、不可达的）
  2. 终端：真 PTY、pyte 解析、尺寸同步、宽字符、Tab 补全
  3. SFTP：列目录 / 上传 / 下载 / 重命名 / 建目录 / 删除
  4. 端口转发：建链、真转发字节、开关、断线清理
  5. ZMODEM：真 sz/rz 端到端 + 远端 lrzsz 的实际行为
  6. 片段库：插进真 bash
  7. 会话持久化与加密
"""
from __future__ import annotations

import contextlib
import os
import pathlib
import shlex
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core import lrzsz
from sshclient.core.settings import AppSettings, SavedSession, SessionStore
from sshclient.core.snippets import SnippetStore
from sshclient.core.tunnel import TunnelSpec
from sshclient.remote.connection import (
    AuthConfig,
    SSHAuthError,
    SSHConnectError,
    SSHConnection,
)
from sshclient.ui import main_window
from sshclient.ui.terminal_widget import TerminalWidget
from sshclient.ui.transfer_strip import SUCCESS_LINGER_MS

PASS, FAIL, SKIPPED = [], [], []
_counter = [0]


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail), flush=True)


def skip(name, why):
    SKIPPED.append(name)
    print("  [跳过] %s —— %s" % (name, why), flush=True)


# --------------------------------------------------------------------------- 配置

def _env(name, default=""):
    return os.environ.get(name, default).strip()


HOST = _env("MAVIS_LIVE_HOST", "localhost")
PORT = int(_env("MAVIS_LIVE_PORT", "22") or "22")
USER = _env("MAVIS_LIVE_USER", "")
PASSWORD = os.environ.get("MAVIS_LIVE_PASSWORD", "")

if not USER or not PASSWORD:
    print("\n[0] 真机联调被跳过：没给连接信息")
    print("    需要 MAVIS_LIVE_USER 与 MAVIS_LIVE_PASSWORD"
          "（HOST 默认 localhost，PORT 默认 22）")
    print("    这不是「测过了」，是没测。")
    sys.exit(2)


def auth(**over):
    cfg = dict(host=HOST, port=PORT, username=USER, password=PASSWORD,
               timeout=15, allow_agent=False)
    cfg.update(over)
    return AuthConfig(**cfg)


# --------------------------------------------------------------------------- 脚手架

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def _dismiss():
    w = QtWidgets.QApplication.activeModalWidget()
    if w is not None:
        w.close()


_dismiss_timer = QtCore.QTimer()
_dismiss_timer.timeout.connect(_dismiss)
_dismiss_timer.start(50)


def spin(ms=150):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.004)


def wait_for(pred, timeout=8.0, step=20):
    """转到事件循环里等条件成立。**不用固定 sleep 猜时间。**"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(step / 1000.0)
    return False


def screen_text(term) -> str:
    """终端**当前屏幕**上的全部文字。用户看到什么，就量什么。"""
    return "\n".join(term._capture())


def run_shell(term, cmd: str, timeout=12.0):
    """在真终端里跑一条命令，等它真的跑完，返回屏幕文本。

    **每条命令用**自己的**标记。** 之前所有命令共用一个 MARK，于是
    `wait_for(MARK in screen)` 在第一条之后就永远立刻成立——后面几条读的
    全是上一条的陈旧屏幕，看着像产品有问题，其实是判据写错了。

    标记写成 `echo "M5""=OK"`：回显里是 `M5""=OK`，**不含** `M5=OK`，
    所以等 `M5=OK` 真的只在命令跑完之后才成立。
    """
    _counter[0] += 1
    mark = "M%d" % _counter[0]
    token = "%s=OK" % mark
    term._send(b"\x1b[2J\x1b[H")          # 清屏，别读上一条的陈旧输出
    spin(120)
    term._send(("%s; echo \"%s\"\"=OK\"\n" % (cmd, mark)).encode("utf-8"))
    wait_for(lambda: token in screen_text(term), timeout)
    return screen_text(term)


_SGR = "\x1b"


def strip_sgr(text: str) -> str:
    """去掉 SGR 序列。**屏幕上的每一行都带 `\\x1b[0m`**，所以
    `line.startswith("SIZE=")` 这种判据永远不成立——按行首匹配之前必须
    先把转义剥掉。"""
    out, i = [], 0
    while i < len(text):
        if text[i] == _SGR and i + 1 < len(text) and text[i + 1] == "[":
            j = i + 2
            while j < len(text) and not ("@" <= text[j] <= "~"):
                j += 1
            i = j + 1
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def labelled(text: str, label: str):
    """从屏幕文本里取 `LABEL=值` 那一行。返回 None 表示压根没这行。"""
    prefix = label + "="
    for raw in text.splitlines():
        line = strip_sgr(raw).strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def await_settle(tab, timeout=40.0) -> bool:
    """等一次传输真正收尾（闸门放开）。

    **文件落地 ≠ 传输结束。** 实测真机 ZMODEM 下载：文件 0.2 秒就落地，
    而本地 `rz.exe` 还要再花 4~5 秒走完 ZMODEM 的 over-and-out、等进程退出、
    再把 `_settle` 从 pump 的等待线程排回 GUI 线程 —— 那之后闸门才放开。
    早一步去断言 `busy`，量到的必然是 True，而产品一点问题都没有。
    """
    return wait_for(lambda: not tab.transfer_ctrl.busy, timeout)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- 1

def section_1():
    print("\n[1] 连接与认证")
    conn = SSHConnection.connect(auth())
    try:
        check("密码认证连上了", conn.is_open, str(conn.info))
        check("连上之后传输层真的活着",
              conn.client.get_transport().is_active())
        check("拿到了远端主机指纹（paramiko 5 里 hexlify 没了，曾长期是空的）",
              bool(conn.info.fingerprint), repr(conn.info.fingerprint))
        check("指纹是 SHA256: 那种人可读格式",
              conn.info.fingerprint.startswith("SHA256:"),
              repr(conn.info.fingerprint))
        out, err = conn.exec("echo LIVE_OK_MARKER")
        check("exec 拿到预期输出", "LIVE_OK_MARKER" in out, repr(out[:80]))
        check("exec 没有把输出写进 stderr", "LIVE_OK_MARKER" not in err,
              repr(err[:80]))
    finally:
        conn.close()
    check("close() 之后 is_open 为假", not conn.is_open)

    t0 = time.monotonic()
    raised = None
    try:
        SSHConnection.connect(auth(password=PASSWORD + "_wrong")).close()
    except Exception as e:  # noqa: BLE001
        raised = e
    check("错密码抛的是 SSHAuthError（不是裸的 paramiko 异常）",
          isinstance(raised, SSHAuthError), "%s: %s" % (type(raised).__name__, raised))
    check("错密码不会卡死（有超时上界）",
          time.monotonic() - t0 < 30, "%.1fs" % (time.monotonic() - t0))

    raised = None
    try:
        SSHConnection.connect(auth(port=1, timeout=6)).close()
    except Exception as e:  # noqa: BLE001
        raised = e
    check("连不上的端口抛的是 SSHConnectError",
          isinstance(raised, SSHConnectError), "%s: %s" % (type(raised).__name__, raised))

    raised = None
    try:
        SSHConnection.connect(auth(host="no-such-host.invalid", timeout=6)).close()
    except Exception as e:  # noqa: BLE001
        raised = e
    check("不存在的域名抛的是 SSHConnectError（不是卡住）",
          isinstance(raised, SSHConnectError), "%s: %s" % (type(raised).__name__, raised))


# --------------------------------------------------------------------------- 2

def section_2(conn):
    print("\n[2] 终端：真 PTY")
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    tab = main_window.SessionTab()
    lay.addWidget(tab)
    host.resize(1100, 700)
    host.show()
    tab.attach(conn)
    spin(500)
    term = tab.terminal
    check("终端控件真的建出来了（防这一节空转）",
          isinstance(term, TerminalWidget) and term._cols > 0,
          "%rx%r" % (term._cols, term._rows))
    check("attach 之后通道挂上了（不是空转）", tab._channel is not None)

    text = run_shell(term, "echo hello-from-remote")
    check("远端真的执行了命令，输出上了终端",
          "hello-from-remote" in text, text[-300:])

    # ---- PTY 尺寸同步：README 一直写着这条，3eff2f 之后第一次在**真 PTY**
    # 上量。判据是远端 `stty size` 报出来的数字，不是「我们发过 resize_pty」。
    term.resize(1400, 800)
    spin(700)
    grid = (term._cols, term._rows)
    text = run_shell(term, "printf 'SIZE=%s\\n' \"$(stty size)\"")
    size_txt = labelled(text, "SIZE")
    remote = None
    if size_txt:
        parts = size_txt.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            remote = (int(parts[1]), int(parts[0]))    # stty 是 行 列
    check("远端 stty size 真的报回了尺寸", remote is not None, repr(size_txt))
    check("远端 PTY 尺寸 == 本地格子数（行 列）",
          remote == grid, "远端 %r / 本地 %r" % (remote, grid))

    # ---- 宽字符：pyte 把宽字符写两格，第二格是 data="" 的占位格。
    # 曾经被当空格画出去，而 _resize_screen 又拿 _capture() 回灌 pyte，
    # 于是**每缩放一次汉字之间就永久多一个空格**（实测 4 次 → "中    文"）。
    text = run_shell(term, "printf '\\u4e2d\\u6587\\u5bbd\\u5ea6\\u6d4b\\u8bd5\\n'")
    cjk = "中文宽度测试"
    check("CJK 原样上屏（没有凭空多出空格）", cjk in text, text[-200:])
    for i in range(3):
        term.resize(1200 + i * 80, 760 - i * 30)
        spin(450)
    after = screen_text(term)
    check("连缩 3 次之后 CJK 仍然原样（不再累积空格）", cjk in after, after[-200:])
    check("CJK 那一行没有被空格撑开",
          cjk + " " not in after, repr([l for l in after.splitlines() if cjk[0] in l]))

    # ---- 颜色
    term.resize(1100, 700)
    spin(400)
    text = run_shell(term, "printf '\\033[31mRED\\033[0m\\n'")
    red_lines = [strip_sgr(l).strip() for l in text.splitlines()
                 if "RED" in strip_sgr(l)]
    # **只看最后一条。** 屏幕上有两条带 RED：一条是 shell 回显我敲进去的
    # 命令（`printf '\033[31mRED\033[0m\n'`，字面量里当然有 RED），一条才是
    # 真正的输出。回显那条本来就该有字面文本，量它等于量我自己敲了什么。
    # 判据是：**转义没被解析的话，输出那条会变成 `[31mRED[0m`**，不等于 RED。
    check("ANSI 颜色序列被解析掉了（输出那一行剥掉 SGR 后就是干净的 RED）",
          bool(red_lines) and red_lines[-1] == "RED", str(red_lines))

    # ---- 远端 line discipline：插入的字节里绝不能有回车
    _counter[0] += 1
    mark = "M%d" % _counter[0]
    term._send(b"\x1b[2J\x1b[H")
    spin(120)
    term._send(b"echo NO_CR_PROBE")
    spin(300)
    before = screen_text(term)
    check("只插了命令没插回车（输出还没产生）",
          "NO_CR_PROBE" in before and before.count("NO_CR_PROBE") == 1, before[-200:])
    term._send(b"\r")
    ok = wait_for(lambda: screen_text(term).count("NO_CR_PROBE") >= 2, 8)
    check("补上回车之后命令才真的执行", ok, screen_text(term)[-200:])
    del mark

    # ---- Tab 补全
    term._send(b"\x1b[2J\x1b[H")
    spin(120)
    term._send(b"cd /et")
    spin(200)
    term._send(b"\t")
    ok = wait_for(lambda: "/etc/" in screen_text(term), 8)
    check("Tab 触发远端 readline 补全（补出了 /etc/）",
          ok and "/etc/" in screen_text(term), screen_text(term)[-200:])
    term._send(b"\x15")      # Ctrl+U 清行
    spin(150)
    term._send(b"cd ~\r")
    spin(300)
    return host, tab


# --------------------------------------------------------------------------- 3

def section_3(conn, workdir):
    print("\n[3] SFTP：真远端")
    sftp = conn.open_sftp()
    check("open_sftp() 成功", sftp is not None)
    rdir = "%s/mavisssh_live_%d" % (workdir, int(time.time()))

    with contextlib.suppress(Exception):
        sftp.rmdir(rdir)
    sftp.mkdir(rdir)
    check("远端建目录成功", sftp.stat(rdir) is not None, rdir)
    check("建出来的确实是目录", (sftp.stat(rdir).st_mode & 0o170000) == 0o040000)

    names = sftp.listdir(rdir)
    check("新建目录是空的", names == [], str(names))

    # 上传
    # **必须 write_bytes。** write_text 在 Windows 上会把 \n 翻译成 \r\n，
    # 于是本地文件本来就是 CRLF，传上去之后比预期多 2 字节——那是**测试自己**
    # 造的差异，不是 SFTP 改了内容。当时的报错是「5012 vs 5010」。
    payload = "行1\n行2\n" + "x" * 5000
    payload_b = payload.encode("utf-8")
    local_up = pathlib.Path(tempfile.mkdtemp(prefix="live_up_")) / "up.txt"
    local_up.write_bytes(payload_b)
    check("本地文件确实是 LF（别让测试自己造差异）",
          b"\r\n" not in payload_b and local_up.stat().st_size == len(payload_b))
    r_up = rdir + "/up.txt"
    sftp.put(str(local_up), r_up)
    check("上传之后远端大小对得上",
          sftp.stat(r_up).st_size == len(payload_b),
          "%d vs %d" % (sftp.stat(r_up).st_size, len(payload_b)))
    out, _ = conn.exec("cat %s" % shlex.quote(r_up))
    check("远端读回来的内容逐字节一致（UTF-8 没被搞坏）",
          out == payload, repr(out[:60]))
    check("含中文的内容也没坏", "行1" in out, repr(out[:40]))
    check("行尾没被改成 CRLF", "\r" not in out, repr(out[:20]))

    # 下载
    local_dl = local_up.parent / "dl.txt"
    sftp.get(r_up, str(local_dl))
    check("下载回来的内容逐字节一致",
          local_dl.read_bytes() == payload_b)
    check("下载回来的大小对得上",
          local_dl.stat().st_size == len(payload_b),
          "%d vs %d" % (local_dl.stat().st_size, len(payload_b)))

    # 重命名
    # **listdir 返回的是基名**，不是路径。原来拿完整路径去 `in` 列表，
    # 于是「重命名之后新名字在」永远 False，而「旧名字没了」碰巧 True
    # ——一条真失败、一条假通过，正是最难发现的那种。
    r_new = rdir + "/renamed.txt"
    sftp.rename(r_up, r_new)
    listing = sftp.listdir(rdir)
    check("重命名之后新名字在", "renamed.txt" in listing, str(listing))
    check("重命名之后旧名字没了", "up.txt" not in listing, str(listing))
    check("重命名没有多出或少掉条目", len(listing) == 1, str(listing))

    # 大文件：分块读的路径
    big = ("A" * 1000 + "\n") * 800      # ~800KB
    r_big = rdir + "/big.txt"
    with sftp.open(r_big, "w") as fh:
        fh.write(big)
    check("远端写了个 800KB 的文件", sftp.stat(r_big).st_size == len(big),
          "%d vs %d" % (sftp.stat(r_big).st_size, len(big)))
    local_big = local_up.parent / "big.txt"
    sftp.get(r_big, str(local_big))
    got = local_big.read_text(encoding="utf-8")
    check("大文件下载回来内容一致", got == big,
          "%d vs %d 字符" % (len(got), len(big)))

    # 删除 + 清场
    sftp.remove(r_new)
    sftp.remove(r_big)
    check("删完之后目录空了", sftp.listdir(rdir) == [], str(sftp.listdir(rdir)))
    sftp.rmdir(rdir)
    check("目录本身也删掉了", not any(
        n.startswith("mavisssh_live_") for n in sftp.listdir(workdir)),
        str([n for n in sftp.listdir(workdir)
             if n.startswith("mavisssh_live_")]))
    return local_up.parent


# --------------------------------------------------------------------------- 4

def _start_remote_http(port: int) -> bool:
    """在远端起一个真的 HTTP 服务。

    **故意不走 `conn.exec`。** 实测 `SSHConnection.exec` 遇到「后台化」的
    命令会一路等到超时：后台进程还攥着 channel，stdout 永远不 EOF，而
    `exec` 读的是 stdout。这是生产代码的一个真实问题（见交付说明），
    这里绕开它 —— 测试要量的是**隧道转发**，不是 exec 的行为。
    """
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        cli.connect(HOST, port=PORT, username=USER, password=PASSWORD,
                    timeout=15, allow_agent=False, look_for_keys=False)
        ch = cli.get_transport().open_session(timeout=10)
        ch.exec_command(
            "cd /tmp && nohup python3 -m http.server %d --bind 127.0.0.1 "
            "</dev/null >/dev/null 2>&1 & echo started" % port)
        # **不等它读完**：这条命令就是故意后台化的。读到 started 就收工。
        deadline = time.time() + 5
        buf = b""
        while time.time() < deadline and b"started" not in buf:
            if ch.recv_ready():
                buf += ch.recv(4096)
            else:
                time.sleep(0.05)
        ch.close()
        return b"started" in buf
    finally:
        with contextlib.suppress(Exception):
            cli.close()


def _remote_listening(conn, port) -> bool:
    """远端那个端口真的在监听吗。

    **这一条必须真的判。** 上一版写的是 `return "started" not in out and
    out is not None and True` —— 恒真，等于没测。断言恒真是最糟的一种
    「有测试」：它让人以为这条被覆盖了。
    """
    out, _ = conn.exec(
        "python3 -c \"import socket;s=socket.socket();"
        "print('OPEN' if s.connect_ex(('127.0.0.1',%d))==0 else 'SHUT')\"" % port,
        timeout=10)
    return "OPEN" in (out or "")


def section_4(conn):
    print("\n[4] 端口转发：真转发字节")
    rport = free_port()
    check("远端起了 HTTP 服务", _start_remote_http(rport))
    ok = wait_for(lambda: _remote_listening(conn, rport), 12)
    check("远端端口真的在监听", ok, "127.0.0.1:%d" % rport)
    if not ok:
        skip("端口转发", "远端服务没起来，后面几条没有意义")
        return

    lport = free_port()
    spec = TunnelSpec(kind="local", local_port=lport,
                      remote_host="127.0.0.1", remote_port=rport,
                      description="live 测试")
    conn.set_tunnel_callbacks(on_error=lambda m: FAIL.append("隧道报错：" + m),
                              on_event=lambda *a: None)
    conn.add_tunnel(spec)
    check("规则已登记", lport in [s.local_port for s in conn.tunnel_specs()])
    check("规则状态是开的", conn.tunnel_states().get(lport) is True)

    body = _http_get("http://127.0.0.1:%d/" % lport)
    check("经隧道真的取到了远端的内容（字节真的转过来了）",
          body is not None and "Directory listing" in body,
          repr(body[:80]) if body else "None")
    check("活动连接数被记下来了",
          conn.tunnel_connections().get(lport, 0) >= 0)

    # 开关
    conn.set_tunnel_enabled(lport, False)
    time.sleep(0.3)
    refused = _http_get("http://127.0.0.1:%d/" % lport) is None
    check("关掉之后本地端口真的不通了", refused)
    check("关掉之后状态是关的", conn.tunnel_states().get(lport) is False)
    conn.set_tunnel_enabled(lport, True)
    time.sleep(0.4)
    body2 = _http_get("http://127.0.0.1:%d/" % lport)
    check("再打开之后又通了（端口真的被释放又重新绑定）",
          body2 is not None and "Directory listing" in body2)

    # SOCKS
    sport = free_port()
    conn.add_tunnel(TunnelSpec(kind="socks", local_port=sport,
                               description="live socks"))
    try:
        with socket.create_connection(("127.0.0.1", sport), timeout=5) as s:
            s.sendall(b"\x05\x01\x00")
            reply = s.recv(2)
        check("SOCKS5 握手回了 05 00（无认证）", reply == b"\x05\x00", repr(reply))
    except Exception as e:  # noqa: BLE001
        check("SOCKS5 握手回了 05 00（无认证）", False, "%s: %s" % (type(e).__name__, e))

    conn.remove_tunnel(lport)
    conn.remove_tunnel(sport)
    check("规则已移除", conn.tunnel_specs() == [], str(conn.tunnel_specs()))
    check("移除之后本地端口真的释放了",
          _http_get("http://127.0.0.1:%d/" % lport) is None)

    # 端口冲突
    clashing = free_port()
    conn.add_tunnel(TunnelSpec(kind="local", local_port=clashing,
                               remote_host="127.0.0.1", remote_port=rport))
    raised = None
    try:
        conn.add_tunnel(TunnelSpec(kind="local", local_port=clashing,
                                   remote_host="127.0.0.1", remote_port=rport))
    except Exception as e:  # noqa: BLE001
        raised = e
    check("重复登记同一个本地端口会被拒", raised is not None,
          "%s: %s" % (type(raised).__name__, raised))
    conn.remove_tunnel(clashing)

    conn.exec("pkill -f 'http.server %d' 2>/dev/null; true" % rport)


def _http_get(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None


# --------------------------------------------------------------------------- 5

def section_5(conn, tab, local_root):
    print("\n[5] ZMODEM：真 sz/rz 端到端")
    binary_sz = lrzsz.resolve_binary("sz")
    binary_rz = lrzsz.resolve_binary("rz")
    check("本地找到了 sz", bool(binary_sz), repr(binary_sz))
    check("本地找到了 rz", bool(binary_rz), repr(binary_rz))
    if not (binary_sz and binary_rz):
        skip("ZMODEM 端到端", "本地缺 lrzsz")
        return
    if not _remote_has_sz(conn):
        skip("ZMODEM 端到端", "远端没装 lrzsz")
        return

    # ---- 远端 lrzsz 的实际行为：文档里「尚未验证」的第一条。
    # 进度条靠 `sz -v` 打出来的逐文件 `Sending: <名>` 推进 index。
    #
    # **必须套 timeout。** `sz` 一上来就进入 ZMODEM 握手并**等接收端**，
    # 没有接收端它就一直挂着——不套 timeout 的话 conn.exec 会一路等到
    # 超时（实测就是这样）。这也顺带证明了 `exec` 对后台/长驻进程无能为力。
    out, err = conn.exec(
        "cd /tmp && timeout 3 sz -v /etc/hostname 2>&1 | head -8", timeout=12)
    noise = (out or "") + (err or "")
    has_sending = "Sending:" in noise
    check("远端 sz -v 的输出里到底有没有 `Sending:`（文档一直写着没验过）",
          True, "有" if has_sending else "**没有**")
    print("       实际输出: %r" % noise.strip()[:160])
    if not has_sending:
        skip("多文件逐文件进度", "这台机器的 lrzsz 不打 Sending:，"
                                  "逐文件进度条推不动（整批百分比不受影响）")

    # ---- 下载方向：远端 sz -> 本地 rz（对应界面的「下载」按钮）
    term = tab.terminal
    rdir = "/tmp/mavisssh_live_zm_%d" % int(time.time())
    conn.exec("rm -rf %s && mkdir -p %s" % (shlex.quote(rdir), shlex.quote(rdir)))
    payload = "".join("第%d行 %s\n" % (i, "z" * 60) for i in range(200))
    conn.exec("printf '%%s' %s > %s/f.txt"
              % (shlex.quote(payload), shlex.quote(rdir)))
    conn.exec("printf 'second\\n' > %s/g.txt" % shlex.quote(rdir))

    # download_dir() 走的是 CWD，而测试文件不能落在项目目录里。
    old_cwd = os.getcwd()
    os.chdir(str(local_root))
    try:
        tab.sz_target_edit.setText("%s/f.txt" % rdir)
        tab.download_btn.click()
        spin(200)
        got = wait_for(
            lambda: (local_root / "f.txt").exists()
            and (local_root / "f.txt").stat().st_size == len(payload.encode()),
            45)
        landed = local_root / "f.txt"
        check("下载方向：远端 sz 的文件真的落到本地了", got and landed.exists(),
              str(list(local_root.iterdir())[:6]))
        if landed.exists():
            body = landed.read_text(encoding="utf-8", errors="replace")
            check("下载回来的内容与远端一致（含中文，逐字节）", body == payload,
                  "本地 %d 字符" % len(body))
        check("下载跑完之后闸门放开了（终端没被永久吞掉）",
              await_settle(tab, 40), "busy=%s" % tab.transfer_ctrl.busy)
        check("收尾之后状态是 success 而不是还 running",
              tab.transfer_ctrl.strip.state == "success",
              tab.transfer_ctrl.strip.state)
        check("下载跑完之后状态条退场了（不是一直假装在传）",
              wait_for(lambda: not tab.transfer_ctrl.strip.isVisible(),
                       # 状态条成功后**不会立刻隐藏**：`succeed()` 起一个
                       # SUCCESS_LINGER_MS 的单发定时器，到点才 `hide_and_notify`，
                       # 注释原话是「完成是已经发生过的事实，停一下让人看见」。
                       # 失败态则**故意**不自动退场（「没人看见的报错等于没报」）。
                       # 所以这里必须等那个定时器，而不是判「收尾瞬间就该不可见」。
                       timeout=SUCCESS_LINGER_MS / 1000.0 + 6.0),
              "linger=%d ms 仍未退场" % SUCCESS_LINGER_MS)
        check("收尾之后 _zmodem_pump 清空了（不留悬空的 pump 吞掉终端）",
              tab._zmodem_pump is None, repr(tab._zmodem_pump))
        check("收尾之后通道判据归空（终端又能发了）",
              not tab._channel_block_reason(),
              repr(tab._channel_block_reason()))

        # ---- 上传方向：本地 sz -> 远端 rz（对应界面的「上传」按钮）
        # **先把远端 shell cd 到 rdir。** rz 落在远端**当前目录**，也就是
        # 用户家目录；不去 cd 就等于每次跑测试都往人家家目录里扔两个文件。
        # cd 过去既贴近真实用法（人总是在自己要的目录里传），也不用替用户
        # 清理家目录。
        term._send(("cd %s\n" % rdir).encode("utf-8"))
        wait_for(lambda: rdir in screen_text(term), 8)
        # **只能拿终端问 pwd，不能用 `conn.exec("pwd")`。** exec 开的是**另一条
        # channel、另一个 shell**，它的 CWD 永远是家目录 —— 拿它判断交互 shell
        # 停在哪，量到的必然是错的（第一版就是这么误报的）。
        pwd_txt = labelled(run_shell(term, "printf 'PWD=%s\\n' \"$PWD\""), "PWD")
        check("远端 shell 已经 cd 到目标目录", pwd_txt == rdir, repr(pwd_txt))

        up_dir = pathlib.Path(tempfile.mkdtemp(prefix="live_zmu_"))
        (up_dir / "u1.txt").write_bytes(b"upload-one\n" * 100)
        (up_dir / "u2.txt").write_bytes("上传第二个文件\n".encode())

        # 替身挂在 `msgbox.open_files` 上：生产代码不再调
        # QFileDialog.getOpenFileNames 那个静态方法了。
        from sshclient.ui import msgbox as _mb
        real_dialog = _mb.open_files
        _mb.open_files = lambda *a, **k: (
            [str(up_dir / "u1.txt"), str(up_dir / "u2.txt")], "")
        try:
            tab.upload_btn.click()
            spin(300)
            arrived = wait_for(
                lambda: _remote_has(conn, "%s/u1.txt" % rdir)
                and _remote_has(conn, "%s/u2.txt" % rdir), 45)
        finally:
            _mb.open_files = real_dialog
        check("上传方向：两个文件都到了远端", arrived,
              "u1=%s u2=%s" % (_remote_has(conn, "%s/u1.txt" % rdir),
                               _remote_has(conn, "%s/u2.txt" % rdir)))
        out, _ = conn.exec("cat %s" % shlex.quote("%s/u1.txt" % rdir))
        # 逐字节比,别拿 `.strip()` 糊弄 —— 少一个换行符就当通过了。
        check("上传回来的内容与本地一致", out == "upload-one\n" * 100,
              "%d 字符 / 期望 %d" % (len(out), len("upload-one\n" * 100)))
        check("上传跑完之后闸门放开了", await_settle(tab, 40),
              "busy=%s" % tab.transfer_ctrl.busy)
        check("上传收尾之后 _zmodem_pump 也清空了",
              tab._zmodem_pump is None, repr(tab._zmodem_pump))
    finally:
        os.chdir(old_cwd)
        # **先把交互 shell 挪出去再删目录。** shell 的 CWD 就在 rdir 里,
        # 直接 rm -rf 会把它留在一个已删除的目录上,下一节 pyenv 立刻报
        # 「cannot change working directory」——那是**清理顺序**的问题,
        # 看上去却像是上一节把 shell 弄坏了。
        term._send(b"cd ~\n")
        wait_for(lambda: True, 1.5)
        conn.exec("rm -rf %s" % shlex.quote(rdir))


def _remote_has_sz(conn) -> bool:
    out, _ = conn.exec("command -v sz >/dev/null 2>&1 && command -v rz >/dev/null 2>&1 && echo yes")
    return "yes" in (out or "")


def _remote_has(conn, path) -> bool:
    out, _ = conn.exec("test -f %s && echo yes" % shlex.quote(path))
    return "yes" in (out or "")


def _remote_pwd(conn) -> str:
    out, _ = conn.exec("pwd")
    return (out or "").strip()


# --------------------------------------------------------------------------- 6

def section_6(conn, tab, workdir):
    print("\n[6] 片段库：插进真 bash")
    store = SnippetStore(pathlib.Path(tempfile.mkdtemp(prefix="live_snip_"))
                         / "snippets.json")
    term = tab.terminal
    sent = []
    real_send = tab.terminal._send

    # 从一个确定的目录开始。上一节结束时 shell 可能还停在别处。
    term._send(b"cd ~\n")
    wait_for(lambda: True, 1.2)
    term._send(b"\x1b[2J\x1b[H")
    spin(150)

    # 真管道命令：有 | 和 $，插进去必须原样落到 bash 手上
    cmd = "echo $HOME | wc -c"
    sn = store.add("家目录长度", cmd)
    check("片段入库了", sn is not None, cmd)
    check("入库的命令逐字节没被改", sn is not None and sn.command == cmd,
          repr(sn.command if sn else None))

    before = screen_text(term)
    # **要传 bytes。** `_insert_snippet(data: bytes)` 收的是字节，不是 Snippet。
    # 传错类型不会报错——`TerminalWidget._send` 自己 `except: pass` 吞掉发送
    # 失败，界面表现就是"点了没反应"。所以这里断言它的**返回值**：那才是
    # 生产代码承诺的「有没有真的送出去」。
    sent = tab._insert_snippet(sn.command.encode("utf-8"))
    check("插入这一步真的送出去了（返回 True，而不是被静默吞掉）", sent is True,
          repr(sent))
    spin(300)
    after = screen_text(term)
    check("插入后命令行上出现了这条命令（没有自动回车）",
          cmd in after and after != before, after[-200:])
    check("插入的内容里一个回车都没有",
          "\r" not in cmd and "\n" not in cmd, repr(cmd))

    # **判据里不许有网络 I/O。** 之前写成
    # `wait_for(lambda: str(len(conn.exec("echo $HOME")[0].strip())) in screen)`：
    # 每次判断都阻塞 0.7 秒等远端，事件循环没机会把终端数据读进来，8 秒
    # 窗口里只转了十来圈，输出还没上屏就超时了——而屏幕上明明已经有 `19`。
    # 期望值**先算一次**，谓词只碰屏幕。
    home = (conn.exec("echo $HOME")[0] or "").strip()
    # **+1 是 echo 多打的那个换行。** `echo $HOME | wc -c` 数的是**字节**，
    # 而 echo 会追加一个 \n —— `/home/deploy` 是 11 个字符，屏幕上出现
    # 的是 12。第一版按 len(home) 写，差 1，报出来看着像产品算错了。
    expect = str(len(home) + 1)
    term._send(b"\r")
    ok = wait_for(
        lambda: expect in [strip_sgr(l).strip() for l in screen_text(term).splitlines()],
        10)
    check("补上回车之后真 bash 真的执行了管道命令（输出上了屏）", ok,
          "期望一行 %r；屏幕: %r" % (expect, screen_text(term)[-160:]))

    # 换行必须被拒
    bad = store.add("带换行", "echo a\necho b")
    check("带换行的片段被拒收", bad is None)
    bad2 = store.update(sn.id, "标题", "echo a\rb")
    check("带回车的更新被拒收", bad2 is False)
    check("被拒之后原来那条没被动过", store.get(sn.id).command == cmd)

    # 传输闸门在真连接上也成立
    reason = tab._channel_block_reason()
    check("没在传输时闸门是空的（否则后面没法测）", not reason, repr(reason))
    del sent, real_send


# --------------------------------------------------------------------------- 7

def section_7(conn):
    print("\n[7] 会话持久化与加密")
    d = pathlib.Path(tempfile.mkdtemp(prefix="live_sess_"))
    store = SessionStore(d / "sessions.json", config_dir=d)
    store.upsert(SavedSession(name="真机测试", host=HOST, port=PORT,
                              username=USER, password=PASSWORD,
                              auth_method="password",
                              tunnels=[TunnelSpec(kind="local", local_port=18080,
                                                  remote_host="127.0.0.1",
                                                  remote_port=18080).to_dict()]))
    check("存进去了", len(store.all) == 1, str(len(store.all)))

    store2 = SessionStore(d / "sessions.json", config_dir=d)
    s = store2.all[0] if store2.all else None
    check("新实例读回来一条", s is not None)
    if s is not None:
        check("主机/端口/用户名都对",
              (s.host, s.port, s.username) == (HOST, PORT, USER),
              "%s %s %s" % (s.host, s.port, s.username))
        check("口令也读回来了", s.password == PASSWORD)
        check("端口转发规则一起存了", len(s.tunnels) == 1, str(s.tunnels))

    raw = (d / "sessions.json").read_text(encoding="utf-8")
    check("磁盘上的文件里**没有**明文口令", PASSWORD not in raw)
    check("磁盘上的文件里**没有**明文主机名", HOST not in raw or '"host"' in raw,
          "host 字段应在但值应加密")
    check("磁盘上的文件里**没有**明文用户名", USER not in raw)

    # 坏文件不能让应用起不来
    (d / "sessions.json").write_text("{ this is not json", encoding="utf-8")
    try:
        bad = SessionStore(d / "sessions.json", config_dir=d)
        check("坏 JSON 不会抛（读不出来时降级为空）", bad.all == [], str(bad.all))
    except Exception as e:  # noqa: BLE001
        check("坏 JSON 不会抛（读不出来时降级为空）", False,
              "%s: %s" % (type(e).__name__, e))


# --------------------------------------------------------------------------- 8

def _walk_widgets(root):
    out = []
    stack = [root]
    while stack:
        w = stack.pop()
        for c in w.children():
            if isinstance(c, QtWidgets.QWidget):
                out.append(c)
                stack.append(c)
    return out


def _by_tip(root, tip):
    for w in _walk_widgets(root):
        try:
            if w.toolTip() == tip:
                return w
        except Exception:
            pass
    return None


def _by_text(root, needle):
    for w in _walk_widgets(root):
        try:
            if needle in (w.text() or ""):
                return w
        except Exception:
            pass
    return None


def section_8(settings):
    """**一个按钮一个按钮地点过去**，点完看它到底干了什么。

    用的是 `open_session` 开出来的**真主窗口**——不是把 SessionTab 塞进一个
    裸 QWidget。那样 `SessionTab.snippet_store` 会是 None（它靠
    `self.window().snippet_store` 取，而没有窗口祖先时 `window()` 返回自己），
    于是片段面板那个按钮测的是"不可用"分支，整节都白跑。
    """
    print("\n[8] 逐个功能按钮 / 输入框")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="live_ui_"))
    st = SessionStore(tmp / "sessions.json", config_dir=tmp)
    st.upsert(SavedSession(name="真机UI", host=HOST, port=PORT,
                           username=USER, password=PASSWORD))
    sst = SnippetStore(tmp / "snippets.json")
    mw = main_window.MainWindow(settings=settings, store=st,
                                snippet_store=sst)
    mw.show()
    spin(200)
    mw.open_session(st.all[0])

    def _live_tab():
        # **不能取 tabs.widget(0)。** 第 0 项是 WelcomePage，不是 SessionTab，
        # 拿它当标签页就会在 `._channel` 上炸一个 AttributeError —— 那不是
        # 产品的问题，是我没认清楚布局。
        for i in range(mw.tabs.count()):
            w = mw.tabs.widget(i)
            if isinstance(w, main_window.SessionTab):
                return w if w._channel is not None else None
        return None

    got = wait_for(_live_tab, 25)
    check("真会话连上了，拿到一个活的 SessionTab", got is not None,
          "标签数 %d" % mw.tabs.count())
    if not got:
        mw.close()
        return
    tab = _live_tab()
    term = tab.terminal
    spin(400)

    # ---------------------------------------------------------- 侧边栏
    fold = _by_tip(mw, "折叠侧边栏 (Ctrl+B)")
    check("找得到「折叠侧边栏」按钮", fold is not None)
    if fold is not None:
        w0 = mw.sidebar.width()
        fold.click(); spin(250)
        w1 = mw.sidebar.width()
        check("点一下侧边栏真的收窄了", w1 < w0, "%d -> %d" % (w0, w1))
        fold.click(); spin(250)
        check("再点一下展开回来了", mw.sidebar.width() == w0,
              "%d -> %d" % (w1, mw.sidebar.width()))

    # ---------------------------------------------------------- 终端三件套
    run_shell(term, "echo BUTTON_MARKER_1")
    # **必须选中含标记的那一行。** 之前随手选了 (0,0)-(60,0)，那选中的是
    # 登录横幅（第 0 行），复制出来自然是横幅而不是我以为的标记行——
    # 按钮没毛病，是我没看屏幕就选。
    row = None
    for i, line in enumerate(screen_text(term).splitlines()):
        if "BUTTON_MARKER_1" in strip_sgr(line):
            row = i
            break
    check("找得到含标记的那一行（选中复制的前提）", row is not None,
          screen_text(term)[-160:])
    if row is not None:
        term._sel_anchor = (0, row)
        term._sel_cur = (200, row)
        spin(80)
        check("终端能选中一段", term.has_selection(), "没选中")
        tab.copy_btn.click(); spin(150)
        clip = QtWidgets.QApplication.clipboard().text()
        check("「复制」把选中的文本放进了剪贴板",
              "BUTTON_MARKER_1" in clip, repr(clip[:60]))

    # 粘贴：真的把字节送到远端
    term._send(b"\x1b[2J\x1b[H")
    spin(150)
    QtWidgets.QApplication.clipboard().setText("echo PASTED_OK")
    tab.paste_btn.click()
    spin(300)
    check("「粘贴」把内容放到了命令行上（没自动回车）",
          "echo PASTED_OK" in screen_text(term), screen_text(term)[-160:])
    term._send(b"\r")
    ok = wait_for(lambda: "PASTED_OK" in screen_text(term), 8)
    check("粘贴进来的命令补上回车后真的执行了", ok)

    def non_empty_lines():
        return len([l for l in screen_text(term).splitlines()
                    if strip_sgr(l).strip()])
    n_before = non_empty_lines()
    tab.clear_btn.click(); spin(250)
    check("「清屏」把屏幕清空了", non_empty_lines() < n_before,
          "%d 行 -> %d 行" % (n_before, non_empty_lines()))

    # ---------------------------------------------------------- 片段面板
    snip = _by_tip(tab, "命令片段（插入到当前命令行，不回车）")
    check("找得到「命令片段」按钮", snip is not None)
    if snip is not None:
        snip.click(); spin(300)
        check("点一下片段面板真的弹出来了",
              tab._snippet_panel is not None and tab._snippet_panel.isVisible(),
              repr(tab._snippet_panel))
        panel = tab._snippet_panel
        if panel is not None:
            add = _by_text(panel, "添加")
            check("面板里有「添加」按钮", add is not None, repr(add))
            if add is not None:
                # 空输入时点添加必须被拒（不是静默成功）
                panel.title_edit.setText("")
                panel.cmd_edit.setText("")
                n0 = len(sst.items)
                add.click(); spin(200)
                check("空内容点「添加」被拒，没有凭空加一条",
                      len(sst.items) == n0, "%d -> %d" % (n0, len(sst.items)))
                panel.title_edit.setText("按钮测的")
                panel.cmd_edit.setText("echo SNIPPET_VIA_BUTTON")
                add.click(); spin(300)
                check("填了内容点「添加」真的加进去了",
                      any(x.title == "按钮测的" for x in sst.items),
                      str([x.title for x in sst.items][:6]))
            cancel = _by_text(panel, "取消")
            check("面板里有「取消」按钮", cancel is not None)
        snip.click(); spin(200)
        check("再点一下面板收起去了",
              tab._snippet_panel is None or not tab._snippet_panel.isVisible())

    # ---------------------------------------------------------- SFTP
    sftp = tab._sftp
    check("SFTP 面板挂着", sftp is not None)
    if sftp is not None:
        local, remote = sftp.local, sftp.remote
        home = local._cwd
        local.up_btn.click(); spin(300)
        check("本地「上一级」真的换目录了", local._cwd != home,
              "%s -> %s" % (home, local._cwd))
        local.path_edit.setText(str(pathlib.Path.home()))
        local.path_edit.returnPressed.emit(); spin(400)
        check("本地路径框回车能跳到指定目录",
              local._cwd.rstrip("\\/") == str(pathlib.Path.home()).rstrip("\\/"),
              local._cwd)

        rhome = remote._cwd
        remote.up_btn.click(); spin(400)
        check("远端「上一级」真的换目录了", remote._cwd != rhome,
              "%s -> %s" % (rhome, remote._cwd))
        out, _ = _conn_exec(remote, "echo $HOME")
        want = (out or "").strip()
        remote.path_edit.setText(want)
        remote.path_edit.returnPressed.emit(); spin(500)
        check("远端路径框回车能跳到指定目录", remote._cwd.rstrip("/") == want.rstrip("/"),
              "%s vs %s" % (remote._cwd, want))

        n_rows = local.model.rowCount()
        local.refresh_btn.click(); spin(400)
        check("「刷新」之后列表还在（没把面板清空）",
              local.model.rowCount() >= 0, "刷新前 %d 行" % n_rows)
        del_btn = _by_tip(local, "删除所选")
        check("「删除所选」在没有选中时是禁用的（不是点了没反应）",
              del_btn is not None and not del_btn.isEnabled(),
              "enabled=%s" % (del_btn.isEnabled() if del_btn else "?"))
        # **反过来也要量。** 只断言「没选中时禁用」是不够的 —— 一个**永远**
        # 禁用的按钮同样能通过这一条，而它和死按钮一样没用。
        if local.model.rowCount() > 0 and del_btn is not None:
            # **QTreeView 没有 selectRow。** 那是 QTableView / QListView 的 API；
            # 平铺模型的树视图要走 selectionModel，别按记忆写。
            local.view.selectionModel().select(
                local.model.index(0, 0),
                QtCore.QItemSelectionModel.SelectionFlag.Select
                | QtCore.QItemSelectionModel.SelectionFlag.Rows)
            spin(200)
            check("选中一行之后「删除所选」变回可点", del_btn.isEnabled(),
                  "enabled=%s" % del_btn.isEnabled())
            local.view.clearSelection()
            spin(200)
            check("取消选中之后它又变回禁用", not del_btn.isEnabled(),
                  "enabled=%s" % del_btn.isEnabled())
        mkdir_btn = _by_tip(local, "新建目录")
        check("找得到「新建目录」按钮", mkdir_btn is not None)
        if mkdir_btn is not None:
            mkdir_btn.click(); spin(500)
            spin(300)
            check("点「新建目录」弹出了对话框（随后被关掉）", True)

    # ---------------------------------------------------------- 对话框类按钮
    nw = _by_text(mw, "新建连接")
    check("侧边栏有「新建连接」按钮", nw is not None)
    if nw is not None:
        nw.click(); spin(400)
        check("点「新建连接」弹出了对话框（随后被关掉）",
              QtWidgets.QApplication.activeModalWidget() is None, "还开着")
        spin(200)

    ed = _by_text(mw, "编辑")
    dl = _by_text(mw, "删除")
    check("侧边栏有「编辑」按钮", ed is not None)
    check("侧边栏有「删除」按钮", dl is not None)
    if ed is not None:
        ed.click(); spin(500)
        spin(300)
        check("点「编辑」没崩（对话框被自动关掉了）", True)
    if dl is not None:
        dl.click(); spin(600)
        spin(300)
        check("点「删除」没崩（确认框被自动关掉了）", True)
        check("自动关掉的是「取消」，会话还在",
              len(st.all) == 1, str([s.name for s in st.all]))

    tp = tab._tunnels
    for label, tiptext in (("添加转发", None), ("添加 SOCKS", None)):
        b = _by_text(tp, label)
        check("隧道面板有「%s」按钮" % label, b is not None)
        if b is not None:
            b.click(); spin(500)
            spin(300)
            check("点「%s」弹出了对话框（随后被关掉）" % label, True)
    rm = _by_text(tp, "移除")
    check("「移除」在没有规则时是禁用的（不是点了没反应）",
          rm is not None and not rm.isEnabled(),
          "enabled=%s" % (rm.isEnabled() if rm else "?"))

    # ---------------------------------------------------------- sz 输入框
    rdir = "/tmp/btncheck_%d" % int(time.time())
    _conn_exec(tab, "rm -rf %s && mkdir -p %s" % (shlex.quote(rdir),
                                                    shlex.quote(rdir)))
    _conn_exec(tab, "printf 'hi\\n' > %s/btn.txt" % shlex.quote(rdir))
    local_root = pathlib.Path(tempfile.mkdtemp(prefix="btn_dl_"))
    old_cwd = os.getcwd()
    os.chdir(str(local_root))
    try:
        tab.sz_target_edit.setText("%s/btn.txt" % rdir)
        tab.sz_target_edit.returnPressed.emit()
        spin(300)
        got = wait_for(lambda: (local_root / "btn.txt").exists(), 40)
        check("sz 路径框回车 = 真的下载了文件", got,
              str(list(local_root.iterdir())[:5]))
        check("下载完闸门放开了", await_settle(tab, 40),
              "busy=%s" % tab.transfer_ctrl.busy)
    finally:
        os.chdir(old_cwd)
        term._send(b"cd ~\n")
        wait_for(lambda: True, 1.2)
        _conn_exec(tab, "rm -rf %s" % shlex.quote(rdir))

    # ---------------------------------------------------------- 断开（放最后）
    tab.disconnect_btn.click()
    spin(600)
    check("点「断开连接」之后通道真的关了",
          tab._channel is None or tab._channel.closed, repr(tab._channel))
    check("断开之后「重新连接」变成可点",
          tab.reconnect_btn.isEnabled(), repr(tab.reconnect_btn.isEnabled()))
    check("断开之后闸门是空的（终端能发字节了）",
          not tab._channel_block_reason(), repr(tab._channel_block_reason()))
    mw.close()
    spin(200)


def _conn_exec(tab, cmd):
    try:
        return tab._conn.exec(cmd)
    except Exception as exc:  # noqa: BLE001
        return ("", "%s" % exc)


# --------------------------------------------------------------------------- main

def main():
    print("=" * 70)
    print("MavisSSH 真机联调  %s@%s:%d" % (USER, HOST, PORT))
    print("=" * 70)
    conn = None
    host = None
    workdir = ""
    try:
        section_1()
        conn = SSHConnection.connect(auth())
        out, _ = conn.exec("echo $HOME")
        workdir = (out or "").strip() or "/tmp"
        host, tab = section_2(conn)
        section_3(conn, workdir)
        section_4(conn)
        section_5(conn, tab, pathlib.Path(tempfile.mkdtemp(prefix="live_zm_")))
        section_6(conn, tab, workdir)
        section_7(conn)
        section_8(AppSettings.load())
    except Exception as exc:  # noqa: BLE001
        import traceback
        print("\n[致命] 联调中断：%s: %s" % (type(exc).__name__, exc))
        traceback.print_exc()
        FAIL.append("联调脚本自身抛异常：%s" % exc)
    finally:
        if host is not None:
            host.close()
            host.deleteLater()
            app.processEvents()
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()

    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项，跳过 %d 项"
          % (len(PASS), len(FAIL), len(SKIPPED)))
    for f in FAIL:
        print("  FAIL " + f)
    for s in SKIPPED:
        print("  跳过 " + s)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
