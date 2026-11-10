"""SFTP file manager panel.

A split view with the *local* filesystem on one side and the *remote* one on
the other. The two halves are independent widgets; drag-and-drop and toolbar
buttons drive cross-side transfers.
"""
from __future__ import annotations

import contextlib
import os
import socket
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅用于类型标注，运行时不需要 paramiko
    import paramiko
from PyQt6 import QtCore, QtGui, QtWidgets, sip

from ..core.settings import UI_SIZE_REF
from . import icons, msgbox
from .controls import _icon_button, _retheme_icon_button, _set_icon
from .design.v1.palette import get_palette
from .transfer_controller import TransferController

# _icon_button 原先在这里另有一份，和 controls.py 的完全重复。两份同名同实现的
# 函数迟早会只改一处，于是两个面板的图标按钮长得不一样。现在只留 controls 那份。


# ---------------------------------------------------------------------------
# File model -- one model for both sides; "side" controls which root to use.


@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    mode: int

    @property
    def is_link(self) -> bool:
        return stat.S_ISLNK(self.mode)

    @property
    def mtime_str(self) -> str:
        try:
            return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""


class _StalledTransfer(Exception):
    """远端在通道静默超时内没有响应。

    **单独一个类型，是为了让「超时」在退让链里与「服务端不支持这个扩展」
    分得开。** 两者在 `except Exception` 眼里是一回事，但后果差得极远 ——
    见 :meth:`RemoteFileSystem._swap_in_place` 的 docstring 与那里的实测。

    给用户看的那句话必须说清「服务端可能已经换成功了，请去远端确认」，
    因为这时候**远端的状态是未知的**，说「失败」会让人以为原文件还在。
    """


#: 文案里报的秒数。与 `connection._SFTP_STALL_SECONDS` 同值，但**不 import 它**
#: —— 面板不该为了报错文案依赖连接层。真要改一个记得改另一个。
_STALL_REPORT_SECONDS = 8


class RemoteFileSystem:
    """paramiko ``SFTPClient`` 的线程安全包装。

    同一个 ``SFTPClient``（``SSHConnection.open_sftp`` 缓存的那一个）会同时被
    三处访问：文件面板两侧的 GUI 线程、上传/下载的工作线程、终端的 Tab 补全。

    而 ``SFTPClient`` **不是线程安全的**：它内部的 ``_lock``（``sftp_client.py:118``）
    只保护 ``request_number`` 记账，不保护通道 I/O。两个线程各自 ``send()`` 会把
    报文头交错写坏，报 ``Garbage packet received``；而响应被对方读走时，
    ``_read_response``（884-919）会把它静默丢弃（914 行的守卫），等的那一方
    就永远挂在 ``recv`` 上。两种都表现为"上传不完成"。

    所以这里用一把 ``RLock`` 把所有远程访问串起来。用 ``RLock`` 而不是 ``Lock``
    是为了容忍重入：``download`` 内部还要 ``stat``，将来若某个方法转调同类方法
    也不会自己把自己锁死。
    """

    def __init__(self, sftp: paramiko.SFTPClient):
        self._sftp = sftp
        self._lock = threading.RLock()
        # 远端家目录缓存，供 _expand 展开 \~\ 用（见那里的超时说明）
        self._home = ""
        # `_probe` 的「同时只允许一个探测在飞」闸门。它**不**参与通道竞争
        # （那是 `_lock` 的事），只用来给探测线程的条数封顶。
        self._probe_gate = threading.Lock()
        self._probe_busy = False
        #: 清理线程**自己的**单飞闸门。**必须和 `_probe_busy` 分开**：
        #: `_probe_busy` 与 Tab 补全共用，而补全是延迟敏感的读探测；半死通道上
        #: 一次删不掉的 `remove` 会把那个闸门占到连接死，期间整个会话的补全全废，
        #: 反过来用户狂按 Tab 又会让每次清理假性失败。清理是低频后台操作，
        #: 不该和它共享单飞。
        self._cleanup_gate = threading.Lock()
        self._cleanup_busy = False
        #: **没能确认删除的远端临时件**（完整远端路径）。见 `_unlink_bounded`。
        self._leftovers: list[str] = []

    @contextlib.contextmanager
    def _held(self, timeout: float | None = None):
        """占住远程通道。

        ``timeout=None`` 表示必须拿到（列表刷新、传输等，保证正确性）。
        给一个秒数则表示"尽力而为"：抢不到就抛 ``TimeoutError``，由调用方
        安静放弃。

        **注意这里约束的只是「等锁」，不是「等这条通道做完事」。** 拿到锁
        之后接下来那次 `stat` / `listdir_attr` / `remove` 是一段没有上限的网络
        等待，而拿到锁的那一刻恰恰是最糟的时候（传输全程持锁）。需要连 I/O
        一起限时的地方走 :meth:`_probe` 或 :meth:`_unlink_bounded`，别用这里。

        **这个 timeout 参数目前生产代码零使用者** —— 12 处调用点全是裸调
        （`tests/_sftpsync.py` 的并发守卫探针还在用它，那不是产品路径）。
        它留着的理由是 `_held` 是个通用闸门；**但别把它当成「已经限过时了」**
        去写新代码。
        """
        if timeout is None:
            self._lock.acquire()
            try:
                yield
            finally:
                self._lock.release()
            return
        if not self._lock.acquire(timeout=timeout):
            raise TimeoutError("SFTP 通道正忙")
        try:
            yield
        finally:
            self._lock.release()

    # 「抢锁 + 随后的远端往返」的整体上限。与 `_held(timeout=0.25)` 同档，
    # 因为它接替的正是那 0.25 秒 —— 调大就等于允许 Tab 补全冻窗更久。
    _PROBE_TIMEOUT = 0.25

    def _probe(self, fn, what: str):
        """把「抢锁 + 这一次远端往返」整体放进 :attr:`_PROBE_TIMEOUT` 里。

        **为什么不能只给抢锁设上限。** `RLock` 只保证同一时刻只有一个人动
        那条通道，它不保证**那一次往返**多久回来。原来的 `is_dir` /
        `list_names` 用 `_held(timeout=0.25)` 只约束了前半段：锁一到手，
        随后的 `stat` 就是裸的网络等待。实测假 SFTP 每次往返 2.5 秒时
        `is_dir` 实耗 2.50 秒 —— 冻的是 GUI 线程。Tab 补全（`main_window`
        的 `_complete_remote`）、删目录前的 `is_dir` 判断、`_remote_exists`
        都走这里，所以 "传大文件时按 Tab" 就是界面整段卡死。

        **为什么丢出去的线程不回收。** paramiko 的 `SFTPClient` 没有"取消
        一次正在等的 stat"的办法，硬掐只会把报文写坏（见类说明）。放弃的
        探测线程让它自己跑完自己放锁。

        **但「一直挂到连接关闭」这个说法不准确，评测实测的上界是 1。**
        `_probe_busy` 这个单飞闸门保证**同一时刻只有一个探测在飞**，所以
        用户狂按 Tab 也只会多出**一条**放弃线程、也只会多发**一次**往返 ——
        实测连打 300 次 `list_names`：300 个 `TimeoutError`、真正发出去的
        往返 1 次、存活的 `sftp-probe` 线程 1 条，通道恢复后归 0。
        所以「线程累积到影响进程」这个担心不成立。

        **真正要紧的不是线程数，是它一直占着通道锁。** 被放弃的那条线程
        持着 `_lock` 不放，期间所有走无超时 `_held()` 的路径
        （`listdir`、远程面板刷新、`upload`/`download`）都会**无限期**阻塞。
        症状是双重的，而且第二个更隐蔽：
          1. 那些路径彻底不刷新，且没有任何错误框；
          2. **本函数变成 0 毫秒瞬时拒答**（实测 300 次共耗时 0.000 秒）——
             不耗时、不卡窗，看起来像「程序没反应」。

        **`set_keepalive(20)` 救不了，注释里那句「分钟级自愈」是错的。**
        paramiko 的 keepalive 是 `global_request("keepalive@lag.net", wait=False)`，
        **只写包、从不读包、不等响应**（`transport.py` 里的调用点在
        `packet.py` 读超时的分支上）。它对「丢包不回 RST」的黑洞型半死完全无效
        —— 实测真黑洞 70.0 秒零异常、仍卡在 `recv`，而对照组对端主动关闭
        0.00 秒就 FIN。**上界由 TCP 重传策略与对端是否发 FIN 决定，
        与 `set_keepalive` 无关**，应用层给不出有限秒。

        **超时必须让调用方知道**：这里抛 ``TimeoutError``，绝不当成"问过了"。
        """
        with self._probe_gate:
            if self._probe_busy:
                raise TimeoutError("SFTP 通道正忙：上一次探测还没回来")
            self._probe_busy = True
        box: list = []
        done = threading.Event()

        def run() -> None:
            try:
                with self._held():
                    box.append((True, fn()))
            except BaseException as exc:          # noqa: BLE001
                # 原样传出去：下游要打 type(exc).__name__ 才认得出是
                # SFTPError 还是 FileNotFoundError，包一层 RuntimeError 等于
                # 把唯一的诊断线索毁掉。
                box.append((False, exc))
            finally:
                with self._probe_gate:
                    self._probe_busy = False
                done.set()

        try:
            threading.Thread(target=run, daemon=True, name="sftp-probe").start()
        except BaseException:                  # noqa: BLE001
            # **`_probe_busy` 必须在这里也复位。** 它原本只在 `run` 的 finally 里
            # 清，于是「线程压根没起来」（`Thread.start()` 抛 RuntimeError：
            # 解释器关停中、或线程数超限）这条路上闸门**永久泄漏** ——
            # 实测泄漏后本对象终身拒答：后续 5 次 `_probe` 全部
            # `TimeoutError`、每次耗时 0.0000 秒，而用户看到的是「按 Tab
            # 永远没反应」。这和「通道真的在忙」长得一模一样，没法分辨。
            with self._probe_gate:
                self._probe_busy = False
            raise
        if not done.wait(self._PROBE_TIMEOUT):
            raise TimeoutError("SFTP 通道忙：%s 在 %.2f 秒内没有返回"
                               % (what, self._PROBE_TIMEOUT))
        ok, value = box[0]
        if not ok:
            raise value
        return value

    def listdir(self, path: str) -> list[FileEntry]:
        with self._held():
            attrs = self._sftp.listdir_attr(self._expand(path))
        items: list[FileEntry] = []
        for attr in attrs:
            full = (path.rstrip("/") + "/" + attr.filename) if path not in ("", "/") else "/" + attr.filename
            items.append(
                FileEntry(
                    name=attr.filename,
                    path=full,
                    is_dir=stat.S_ISDIR(attr.st_mode) if attr.st_mode else False,
                    size=int(attr.st_size or 0),
                    mtime=float(attr.st_mtime or 0),
                    mode=int(attr.st_mode or 0),
                )
            )
        items.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return items

    def stat(self, path: str):
        with self._held():
            return self._sftp.stat(self._expand(path))

    def list_names(self, path: str) -> list[str]:
        """Bare entry names, used by the terminal's completion fallback.

        尽力而为：**抢锁和这一次往返都算在** :attr:`_PROBE_TIMEOUT` 里
        （见 `_probe`），超时就抛 `TimeoutError`，由调用方安静放弃补全。
        """
        return self._probe(
            lambda: [a.filename
                     for a in self._sftp.listdir_attr(self._expand(path))],
            "列目录 " + path)

    def is_dir(self, path: str) -> bool:
        """这个远端路径是不是目录。

        **只把「问到了、确实不是目录」说成 False。** 问不到（超时）抛
        `TimeoutError`：原来这里 `except: return False`，于是「没问到」和
        「不是目录」成了同一件事 —— 而 `download_paths` 正是靠它决定
        「跳过目录」，通道一忙就把远程**目录**当文件排进队列，然后
        `get()` 在服务端报错。假答案比问不到更糟。
        """
        try:
            mode = self._probe(
                lambda: self._sftp.stat(self._expand(path)).st_mode,
                "stat " + path)
        except TimeoutError:
            raise
        except Exception:
            return False
        return stat.S_ISDIR(mode)

    def mkdir(self, path: str) -> None:
        with self._held():
            self._sftp.mkdir(self._expand(path))

    def rmdir(self, path: str) -> None:
        with self._held():
            self._sftp.rmdir(self._expand(path))

    def remove(self, path: str) -> None:
        with self._held():
            self._sftp.remove(self._expand(path))

    def rename(self, src: str, dst: str) -> None:
        with self._held():
            self._sftp.rename(self._expand(src), self._expand(dst))

    def getcwd(self) -> str:
        try:
            with self._held():
                cwd = self._sftp.normalize(".")
        except Exception:
            return "/"
        if cwd:
            self._home = cwd
        return cwd

    def _home_dir(self) -> str:
        """远端家目录，缓存优先。取不到时返回空串 —— **不是** `getcwd()`
        那个 `"/"` 兜底：把「没问到」当成「家目录是根目录」会把
        `~/workspaces/x` 拼成 `//workspaces/x`，看着像展开了其实不是。"""
        if self._home:
            return self._home
        with self._held():
            try:
                self._home = self._sftp.normalize(".")
            except Exception:
                return ""
        return self._home

    # ------------------------------------------------------------------ ~ 展开
    #
    # **SFTP 不认 `~`，它只认绝对路径。** 实测（对着真服务器）：
    #   listdir_attr("~/workspaces/qwen3.5-4b") -> FileNotFoundError [Errno 2]
    #   normalize("~/workspaces/qwen3.5-4b")     -> 同样失败（paramiko 也不展开）
    #   normalize(".")                            -> "/home/deploy"
    #   家目录 + "/workspaces/qwen3.5-4b"        -> OK，8 项
    #
    # 而 `~` 是 **shell 的概念**，恰恰是用户在路径栏里最想敲的东西
    # （`~/workspaces/xxx`）。原来每个方法都把路径原样交给 SFTP，于是这一个
    # `~` 同时造成了两个症状：远程面板「读取失败」，以及上传时的
    # `FileNotFoundError [Errno 2]` —— 两者走的是同一条路。
    #
    # **必须在这里统一展开，而不是让每个调用点自己处理** —— 漏一个方法就
    # 有一半入口还是坏的，而且症状是「有时行有时不行」。
    def _expand(self, path: str) -> str:
        """把开头的 `~` 换成远端家目录。`~user` 形式不处理（少见，
        且 SFTP 侧也拿不到家目录列表去解析用户名）。

        **家目录是缓存的。** `is_dir` / `list_names` 走的是 0.25 秒超时的
        `_held`，为的是「传输期间没有补全可以接受，但为了补全把界面卡住不行」；
        要是这里每次都去 `getcwd()` 问一次网络（它走的是**无超时**的
        `_held`），通道一忙就永久阻塞，那条设计就白费了。所以：
        缓存命中时纯本地计算；没命中才问一次，问不到就**原样返回**，
        让 SFTP 去报错 —— 早失败比卡住好。
        """
        if not path.startswith("~"):
            return path
        if path == "~":
            return self._home_dir() or path
        if not (path.startswith("~/") or path.startswith("~\\")):
            return path                    # ~user/...，原样交给 SFTP 报错
        home = self._home_dir()
        if not home:
            return path                    # 问不到家目录就原样送出去，早失败
        rest = path[2:]
        if not rest:
            return home
        return home.rstrip("/") + "/" + rest

    def download(self, remote: str, local: str, progress: Callable[[int, int], None] | None = None) -> None:
        # **先写 `<目标>.part`，成功才替换。**
        # paramiko 的 `get(remote, local)` 内部是 `file(local, "wb")` —— **一打开
        # 就把目标文件截断**。所以下载大文件时断网 / 远端磁盘满，原来那个完好的
        # 文件会变成 60% 的半截文件，**而且它是唯一副本**。先落临时件，失败就把
        # 临时件删掉，原来那份一点没动。
        tmp = local + ".part"
        try:
            with self._held():
                size = self._sftp.stat(self._expand(remote)).st_size
                def cb(done: int, total: int) -> None:
                    if progress:
                        progress(done, total)
                self._sftp.get(self._expand(remote), tmp, callback=cb if progress else None)
            # 临时件和目标**同目录**，所以跨盘/跨设备那套 copy 兜底在这里
            # 永远走不到 —— 留着只会把「临时件不存在」这种真错误伪装成
            # 一个看不懂的 copyfile 报错。os.replace 本身在同卷上就是原子的。
            os.replace(tmp, local)
        except BaseException:
            _silent_unlink(tmp)
            raise
        if progress:
            progress(size, size)

    def upload(self, local: str, remote: str, progress: Callable[[int, int], None] | None = None) -> None:
        size = os.path.getsize(local)   # 本地操作，不必持锁
        target = self._expand(remote)
        # **先传临时件，成功后才改名。**
        #
        # paramiko 的 `put(local, remote)` 内部是 `file(remote, "wb")` ——
        # **一打开就把目标截断**。所以「上传覆盖」传到一半断网时，远端那个
        # **原本完好的文件被毁掉了**，而状态条只报一句「上传失败」。
        # 评审实测：现状下远端原文件确实被截断，且「本地先写临时件再 put」
        # （本文件原来注释里写的方向）**完全无效** —— 它改变不了 paramiko
        # 打开远端那一行，于是症状一模一样。临时件必须在**远端**。
        #
        # 为什么不用 `posix_rename` 一把梭：它是 SFTP **v3 扩展**，最小实现
        # 的服务端会报 `unsupported`（仓库自带的最小服务端就是），而 v3 的
        # 标准 `rename` 要求「目标必须不存在」，所以覆盖场景需要一条退路：
        #   1. 先试 `posix_rename`（原子覆盖，服务端支持就用这条）
        #   2. 不支持 → 标准 `rename`（目标不存在时可用）
        #   3. 目标已存在导致 2 失败 → 删掉旧的再 `rename`
        # 3 的危险窗口只有「一次 rename」那么长，而不是「整个上传」。
        tmp = _sibling_temp(target)
        try:
            with self._held():
                def cb(done: int, total: int) -> None:
                    if progress:
                        progress(done, total)
                self._sftp.put(local, tmp, callback=cb if progress else None)
                self._swap_in_place(tmp, target)
        except BaseException:
            # 失败清理走远端 remove —— 那是**无上限的网络调用**：`_held()` 只限
            # 「等锁」，而通道半死时真正吊住的是 `sftp.remove()` 本身那一趟。
            #
            # **这里绝不能用裸的 `with self._held():`。** 那是 `acquire()` 无上限，
            # 吊住时**下面那行 `raise` 永远不会执行** —— 原始异常连同它本来
            # 该报的失败一起消失，用户只看到进度条永远转、状态条永远不报错
            # （评审实测：`remove` 阻塞 30 秒时 6 秒内不返回、调用方一个异常
            # 都拿不到）。清理是尽力而为的工作，它**没有资格**堵住它本该
            # 汇报的那个错误。
            #
            # 顺带纠正一个常见误解：正常失败路径上这里的锁**本来就是空的**
            # （`with` 在 `try` 内，异常先出 `with`、锁已释放，才进 `except`）。
            # 所以要限的不是抢锁，是 remove 那一趟 —— 只给抢锁加超时会漏掉
            # 真正会吊住的那一半。
            self._unlink_bounded(tmp)
            raise
        if progress:
            progress(size, size)

    #: 清理（删远端临时件）的总预算。**只用来兜住「清理卡住」，不是等锁时间。**
    #: 健康通道上这一次 remove 是毫秒级，1 秒有十几倍余量；而它同时是
    #: 「原始异常多久能回到调用方」的上界 —— 所以取小的那一头。
    _CLEANUP_TIMEOUT = 1.0

    def _unlink_bounded(self, path: str) -> bool:
        """限时删远端文件；返回**是否确认删掉了**。**永不抛。**

        **为什么需要它**：`_remote_unlink_quiet` 自己不抛，但它**会吊住** ——
        `sftp.remove()` 是一趟没有上限的网络往返，而它跑在正拿着原始异常
        等着上报的工作线程上。吊住 = 那个错误永远不报。

        **为什么不用 `_probe`**（它也是「线程 + 限时」）：

        - `_probe_busy` 是与 Tab 补全**共用**的全局单飞闸门。共用有双向伤害：
          半死通道上一次删不掉的 `remove` 会把闸门占到连接死，期间整个会话的
          补全全废；反过来用户狂按 Tab 时每次清理都假性失败，把「补全在飞」
          误报成「通道忙」。
        - 0.25 秒对删除太紧：它原来是给「补全该不该有」定的预算，不是给
          「一次远端写操作」定的。
        这里用**自己的**闸门（`_cleanup_gate`），两条路互不影响。

        **说「没能确认」而不是「没删掉」**：超时放弃之后，那条清理线程可能
        事后真的把文件删了。说「没删掉」就是撒谎，而用户会照着这句话去找。
        """
        with self._cleanup_gate:
            if self._cleanup_busy:
                # 上一条清理还在飞。再起一条只是让它们一起堵在同一把锁后面，
                # 而结果仍然是「这一趟清理不了」—— 直接记账走人。
                self._leftovers.append(path)
                return False
            self._cleanup_busy = True
        done = threading.Event()
        box: list[bool] = []

        def run() -> None:
            try:
                with self._held():
                    box.append(_remote_unlink_quiet(self._sftp, path))
            except BaseException:              # noqa: BLE001
                box.append(False)
            finally:
                with self._cleanup_gate:
                    self._cleanup_busy = False
                done.set()

        try:
            threading.Thread(target=run, daemon=True,
                             name="sftp-cleanup").start()
        except BaseException:                  # noqa: BLE001
            with self._cleanup_gate:
                self._cleanup_busy = False
            self._leftovers.append(path)
            return False
        if not done.wait(self._CLEANUP_TIMEOUT):
            # 放弃等待，但**不清 _cleanup_busy**：那条线程还活着，它自己会清。
            # 故意留着那个标记，是为了让后面几次清理立刻返回 False 而不是
            # 往同一条死路上再堆线程。
            self._leftovers.append(path)
            return False
        if not (box and box[0]):
            self._leftovers.append(path)
            return False
        return True

    def take_leftovers(self) -> list[str]:
        """取走并清空「没能确认删除的远端临时件」。

        **必须由界面那一侧消费掉**（见 :meth:`_TransferWorker._run`）：留在
        `RemoteFileSystem` 上只有代码能看见，用户看不见 —— 那就等于没报。
        临时件在远端列表里是**看得见、认得出、也能手动删**的（面板不过滤点号
        文件），所以报出来就够用户自己收拾，不需要程序替它收尸。
        """
        out, self._leftovers = self._leftovers, []
        return out

    def _swap_in_place(self, tmp: str, target: str) -> None:
        """把 `tmp` 落到 `target`，覆盖场景按三条路依次退。调用方已持锁。

        **超时必须直接抛出，绝不能进退让链 —— 这条是数据安全线。**

        「超时」和「服务端不支持这个扩展」在 `except Exception` 眼里是同一件事，
        但它们的后果差得极远：服务端**可能已经把 `posix_rename` 执行完了**，
        只是响应没回来。评审实测（`logs/verify/`，真 SFTP server）：

            [server] posix_rename(...)  -> EXECUTING   ← 已经换成功了
            [server] rename(...)        -> EXECUTING
            [server] remove(/rem/target) -> EXECUTING  ← 把用户原文件删了
            upload -> raised TimeoutError
            after: target exists? False      ← 原文件和临时件都没了

        没有超时的时候，第一步会永久阻塞、文件反而是好的。**所以这条守卫是
        给「设了超时」这个改动配套的，不是顺手加的保险**：撤掉它，
        上面那段实测会原样复现。

        判据在 `tests/_sftpsstall.py`（元断言专门钉这一条）。
        """
        try:
            self._sftp.posix_rename(tmp, target)      # 原子覆盖，服务端支持就有
            return
        except (TimeoutError, socket.timeout) as exc:
            # 服务端可能已经执行完了，只是响应没回来。**再做任何一步都是
            # 在一个未知状态上叠加破坏**（上面那段实测：最后连原文件都没了）。
            # 残件由 `_unlink_bounded` / `take_leftovers` 那条路去报。
            raise _StalledTransfer(
                "远端在 %d 秒内没有响应，放弃这次覆盖（服务端可能已经换成功，"
                "请到远端确认一下 %s）" % (_STALL_REPORT_SECONDS, target)
            ) from exc
        except Exception:                            # noqa: BLE001
            pass                                     # v3 没这个扩展，走下面
        try:
            self._sftp.rename(tmp, target)           # 目标不存在时标准 v3 就够
            return
        except (TimeoutError, socket.timeout) as exc:
            raise _StalledTransfer(
                "远端在 %d 秒内没有响应，放弃这次覆盖（服务端可能已经换成功，"
                "请到远端确认一下 %s）" % (_STALL_REPORT_SECONDS, target)
            ) from exc
        except Exception:                            # noqa: BLE001
            pass
        # 目标已存在：删掉旧的再改名。窗口只有这一次 rename。
        _remote_unlink_quiet(self._sftp, target)
        try:
            self._sftp.rename(tmp, target)
        except (TimeoutError, socket.timeout) as exc:
            raise _StalledTransfer(
                "远端在 %d 秒内没有响应，替换没做完（临时件已按同名规则留在 "
                "同目录，可认得出、可手删）" % _STALL_REPORT_SECONDS) from exc


class LocalFileSystem:
    def listdir(self, path: str) -> list[FileEntry]:
        """列目录。**读不到目录本身时把异常抛出去**（原样交给调用方的
        `_load_failed`），只有「单个条目 lstat 失败」才静默跳过。

        原来 `os.listdir` 的 OSError 被 `pass` 吞掉、返回空列表，于是
        `FilePanel.refresh()` 里给本地侧准备的 `try/except → _load_failed`
        **永远走不到**：目录被别的进程删掉、或 `Z:\\` 网络盘掉线时，面板只显示
        「0 个目录 · 0 个文件」，和「这文件夹本来就是空的」完全一样，用户
        以为文件没了。**远程侧同样的情况会显示「读取失败」** —— 两侧不一致。
        """
        items: list[FileEntry] = []
        # 这里不吞：调用方负责把它变成用户看得懂的提示
        names = os.listdir(path)
        for name in names:
            full = os.path.join(path, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue                # 断链的符号链接 / 竞态删掉的条目，跳过无妨
            items.append(
                FileEntry(
                    name=name,
                    path=full,
                    is_dir=stat.S_ISDIR(st.st_mode),
                    size=int(st.st_size),
                    mtime=float(st.st_mtime),
                    mode=int(st.st_mode),
                )
            )
        items.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return items

    def stat(self, path: str):
        return os.lstat(path)

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def remove(self, path: str) -> None:
        os.remove(path)

    def rename(self, src: str, dst: str) -> None:
        os.rename(src, dst)


# ---------------------------------------------------------------------------
# 表头：只做一件事 —— 把排序箭头放回标签旁边
# ---------------------------------------------------------------------------

# 和 `qss.py` 里 `QHeaderView::section` 的取值一一对应。覆写 paintSection
# 之后那条 QSS 只管**几何**（padding -> 表头高度），颜色/字号全归这里画，
# 所以两边必须同步，改一处就要改另一处。
_SECTION_PAD = 8
_ARROW_GAP = 6
_ARROW_W = 9
# 字号原点（对应比例尺 `ui_size=11`），实际值一律再乘 `_ui_scale()`。
_HDR_PT = 9.0
_ARROW_PT = 8.0


def _silent_unlink(path: str) -> None:
    """删文件，失败就算了 —— 这是清理路径，不该盖掉原始异常。"""
    try:
        os.unlink(path)
    except OSError:
        pass


def _remote_unlink_quiet(sftp, path: str) -> bool:
    """删**远端**文件，失败就算了；返回是否真的删掉了。

    与 `_silent_unlink` 分开是因为两者性质完全不同：本地 unlink 是瞬间的，
    远端 remove 是一次**网络往返** —— 通道半死时它自己会吊住。所以：
      · 绝不抛（它是清理路径，不该盖掉原始异常）；
      · **绝不抛 ≠ 不会吊住。** 真正兜住「吊住」那一条的是
        `RemoteFileSystem._unlink_bounded`（限时 + 它自己的闸门），本函数
        自己没有时间上限；
      · 返回值让调用方能区分「删掉了」与「没删掉」—— 现在**真的有人消费
        它了**：`upload()` 的失败清理把它交给 `_unlink_bounded`，后者把没
        删掉的记进 `_leftovers`，由界面那一侧拼进状态条文案。
    """
    try:
        sftp.remove(path)
        return True
    except Exception:                   # noqa: BLE001
        return False


def _sibling_temp(target: str) -> str:
    """`target` 同目录下的一个临时名。

    **必须同目录**：跨文件系统的 `rename` 会失败（`EXDEV`），而跨盘那条
    copy 兜底在远端做不了 —— 那正是「写一半、断网、目标被截断」这条
    症状的来源。临时名里**不含 `.part` 字面**，免得和下载侧那个 `.part`
    在测试与用户的直觉里混成同一类东西（下载那份是本地件、随目标目录走）。
    """
    folder, _slash, name = target.rpartition("/")
    return "%s/.mavis-part-%s-%s" % (folder or "/", os.getpid(), name or "x")


def _ui_scale() -> float:
    """用户选的界面字号 ÷ 比例尺原点。

    取自 `QApplication` 的字体 —— 那是 `MainWindow.apply_theme` 里
    `ui_font(settings.ui_font_size)` 真正设上去的地方。控件自己的
    `font()` 因为被 QSS 的 `font-size: Npx` 改成了像素字号，
    `pointSizeF()` 会是 -1，拿它算比例会得到 0。
    """
    app = QtWidgets.QApplication.instance()
    pt = app.font().pointSizeF() if app is not None else -1.0
    if pt <= 0:
        return 1.0
    return max(0.5, pt / UI_SIZE_REF)


class _SortHeader(QtWidgets.QHeaderView):
    """自绘表头，唯一职责是让排序箭头待在标签旁边。

    **为什么非要自绘。** Qt 默认把排序标记画在 section 的**右端**，而这套
    QSS 给 `::section` 加了 `padding: 8px`，标记被顶到内容区之外，于是实测
    它浮在表头顶端、和标签行完全脱节（真机 `FilePanel` 渲染可复现）。
    试过三条纯 QSS 的路，都不行：
      - 改 `padding` 8/4/2/0：表头高 41->33/29/25，箭头纹丝不动
      - `QHeaderView::down-arrow` 用 `QComboBox` 那套透明边框三角形：
        **表头不吃这套**，画出来是个灰方块，比原来更糟
    所以只剩重写 `paintSection` 这一条路。

    **刻意不做的事。** 不改列宽、不画列间分割线、不接管表头几何 ——
    那些是另一回事，混进来会让这一处的回归面变得没必要地大。

    升序 ▲ / 降序 ▼ 靠**形状**区分而不是颜色（色盲安全）。当前排序列的箭头
    用 `text`，比表头标签的 `text_muted` 明显一档。
    """

    def __init__(self, palette, parent=None):
        super().__init__(QtCore.Qt.Orientation.Horizontal, parent)
        self._palette = palette
        self.setSectionsClickable(True)
        # **新建一个 QHeaderView 会把这些默认全部丢一遍。** 其中
        # `stretchLastSection` 默认 True，丢了它末列就不再拉伸（实测列宽
        # 从 118 缩回 100），这属于本次改动之外的**行为变化**，得显式补回来。
        # `sectionsMovable` 则必须保持 False —— 拖动表头换列序会让下面的
        # `sortIndicatorSection` 和列宽对不上。
        self.setStretchLastSection(True)
        self.setSectionsMovable(False)

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.viewport().update()

    def paintSection(self, painter: QtGui.QPainter, rect: QtCore.QRect,
                     index: int) -> None:
        p = self._palette
        painter.save()
        # `QHeaderView` 不裁剪，section 贴边时会画到隔壁上去。
        painter.setClipRect(rect)

        text = self.model().headerData(index, QtCore.Qt.Orientation.Horizontal,
                                       QtCore.Qt.ItemDataRole.DisplayRole)
        text = "" if text is None else str(text)

        # **字号一律从应用字体派生，不写死。** `QWidget { font-size: 13px }`
        # 是像素字号，所以 `self.font().pointSizeF()` 是 -1；真正的用户设置
        # 落在 `QApplication` 的字体上（`ui_font(settings.ui_font_size)`），
        # 从那儿取比例。写死 9.0/8.0 的话，用户在设置里把字号调到 18pt，
        # 满屏都变大了而表头小字纹丝不动 —— 一眼就能看出是哪块没跟上。
        scale = _ui_scale()
        f = QtGui.QFont(self.font())
        f.setPointSizeF(_HDR_PT * scale)
        f.setPixelSize(0)
        f.setBold(True)               # font-weight: 600
        f.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing,
                           0.5 * scale)
        painter.setFont(f)
        painter.setPen(QtGui.QColor(p.text_muted))

        fm = QtGui.QFontMetrics(f)
        inner = rect.adjusted(_SECTION_PAD, 0, -_SECTION_PAD, 0)
        pad_x = _SECTION_PAD

        sorted_here = (index == self.sortIndicatorSection())
        order = self.sortIndicatorOrder()

        tw = fm.horizontalAdvance(text)
        # 条件只看「是不是当前排序列」。**不要**再判 order —— 升序
        # (`AscendingOrder`) 同样要画 ▲，早先写成 `order != Ascending`
        # 结果升序一列箭头都不出现。什么都没排时 `sortIndicatorSection()`
        # 是 -1，`sorted_here` 自然为假。
        if sorted_here:
            # 箭头在文字右边 6px 处，整组仍然**左对齐**：数据行也是左对齐，
            # 箭头跟着标签走才不会读成属于下一列。
            #
            # 用**字符**画而不是 `drawPolygon`：`QStylePainter` 上
            # `drawPolygon(QPolygonF)` 实测静默不画（同一段代码在裸 QPainter
            # 上正常），而 `drawText` 两条路都成立，边缘还有抗锯齿。
            arrow_f = QtGui.QFont(self.font())
            arrow_f.setPointSizeF(_ARROW_PT * scale)
            arrow_f.setPixelSize(0)
            painter.setFont(arrow_f)
            painter.setPen(QtGui.QColor(p.text))
            glyph = "▼" if order == QtCore.Qt.SortOrder.DescendingOrder else "▲"
            # **坐标是表头级的，不是 section 局部的。** 上面刚
            # `setClipRect(rect)` 把绘制锁在 `[rect.x(), rect.x()+w)` 里，
            # 按局部坐标算出来的 x=64 会被整块裁掉（实测：箭头永远不出现，
            # 而同一段里用 `rect.adjusted()` 画的标签正常）。所以这里必须
            # 加回 `rect.x()`。
            painter.drawText(
                QtCore.QRect(rect.x() + pad_x + tw + _ARROW_GAP, rect.top(),
                             _ARROW_W * 2, rect.height()),
                int(QtCore.Qt.AlignmentFlag.AlignLeft
                    | QtCore.Qt.AlignmentFlag.AlignVCenter), glyph)
            painter.setFont(f)
            painter.setPen(QtGui.QColor(p.text_muted))

        painter.drawText(inner, int(QtCore.Qt.AlignmentFlag.AlignLeft
                                    | QtCore.Qt.AlignmentFlag.AlignVCenter), text)

        # 底边：QSS 那条 `border-bottom` 在自绘下不会自动画，得自己补。
        painter.setPen(QtGui.QColor(p.border))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()


# ---------------------------------------------------------------------------
# Generic file panel widget; reused for local & remote.


class _FileTreeView(QtWidgets.QTreeView):
    """文件列表。**只接管 Delete 这一个键**，其余原样交给 ``QTreeView``。

    **为什么不用 QShortcut。** 「删除所选」在面板里已经有完整的一条路
    （`_on_delete`：确认框 → 工作线程 → 汇总失败），Delete 键要走的必须是
    **同一条**，否则两处各写一份、各自漂移。而 `QShortcut` 默认作用域是窗口，
    于是焦点在下面那个路径输入框时按 Delete 也会去删文件 —— 那是把焦点
    当摆设。按键处理放在树里，作用域天然就是「焦点在文件列表上」。

    **不碰「就地改名」。** `QAbstractItemView` 把 Delete 也当作编辑键
    （`EditKeyPressed` 策略下），但这个面板建树时就是
    `NoEditTriggers`，那条路根本走不到；这里只做「有选中就删」，所以双击
    文件既不会改名也不会删（判据：`8` 组里「双击文件不弹编辑框」）。

    **没有选中就放行。** 把 Delete 吃掉的话，用户在别的控件上按 Delete 的
    原意（清路径框、清搜索框）会被这边抢先，而这边又什么都不做 —— 那正是
    这个项目在反复修的「点了没反应」。
    """

    def __init__(self, panel: FilePanel, parent=None):
        super().__init__(parent)
        self._panel = panel
        # **Delete 能用的前提是 `NoEditTriggers`，而那是在 `FilePanel.__init__`
        # 里、树建好之后才设的 —— 所以这里不能断言它**（此刻还是 Qt 的默认值）。
        # 哪天有人为了「支持就地改名」把 `setEditTriggers` 换成
        # `AllEditTriggers` 或 `SelectedClicked`：
        #   · 不选中的行按 Delete → Qt 先开一个空编辑框，用户看着像卡住；
        #   · 选中的行按 Delete → 编辑框先起来，紧接着被下面的
        #     `keyPressEvent` 抢走删文件 —— **两边打架，用户看到的是
        #     闪一下就没了**。
        # 真要加改名，请把 Delete 从 Qt 的编辑键里摘掉（保持
        # `NoEditTriggers`，改名走显式入口），不要两边都留着。
        # `tests/_fsftp.py` 第 21 组有一条判据钉住这个前提，改了它会红。

    def keyPressEvent(self, event) -> None:
        if (event.key() == QtCore.Qt.Key.Key_Delete
                and self.selectionModel().hasSelection()):
            self._panel._on_delete()
            return
        super().keyPressEvent(event)


class FilePanel(QtWidgets.QWidget):
    pathChanged = QtCore.pyqtSignal(str)

    HEADERS = ["名称", "大小", "修改时间", "权限"]

    def __init__(self, side: str, fs, palette, parent: QtWidgets.QWidget | None = None):
        """side: 'local' or 'remote'."""
        super().__init__(parent)
        self._side = side
        self._fs = fs
        self._cwd: str = ""
        self._palette = palette
        # 远程刷新走工作线程，请求号是「这次结果还算不算数」的闸门。
        self._list_req = 0
        self._list_worker: _ListWorker | None = None
        # 建目录 / 删除走另一条线程：它们要抢 SFTP 通道，而通道在**整个
        # 传输期间**都被 upload/download 持着。
        self._op_worker: _OpWorker | None = None
        self._busy = False
        # 状态条提示函数，由 SftpPanel 注入（同 TunnelPanel 的做法）。
        # 不注入时就是 None，下面每处调用都判过 —— 这个面板在测试里
        # 被单独 new 出来，不存在宿主。
        self._flash = None

        # ---- header: title + count ----
        self.title_label = QtWidgets.QLabel("本地" if side == "local" else "远程")
        tf = self.title_label.font()
        tf.setBold(True)
        # **原来这里还有一句 `setPointSize(11)`，它一直没生效。** 实测
        # 设完回读 `pointSizeF()` 是 -1、`pixelSize=13` —— QSS 的
        # `QWidget { font-size: 13px }` 优先级高于 `setFont`，把它顶掉了。
        # 写死在这里既没用，又让人以为字号是这儿说了算。字号现在由 QSS
        # 统一按 `ui_font_size` 缩放，这里只管粗细。
        self.title_label.setFont(tf)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setProperty("role", "muted")

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        head.setSpacing(8)
        badge = QtWidgets.QLabel()
        badge.setPixmap(icons.icon_pixmap(
            "server" if side == "remote" else "disk", palette.accent, 15))
        badge.setFixedWidth(18)
        head.addWidget(badge, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.title_label)
        head.addStretch(1)
        head.addWidget(self.count_label, 0, QtCore.Qt.AlignmentFlag.AlignTop)

        # ---- file list ----
        self.model = QtGui.QStandardItemModel(0, 4, self)
        self.model.setHorizontalHeaderLabels(self.HEADERS)
        self.view = _FileTreeView(self, self)
        self.view.setModel(self.model)
        # **必须在这一批配置之后、控件第一次 show 之前装上。** 反过来
        # （先 show 再 setHeader）实测表头量到 `height=0`、paintSection
        # 一次都不会被调用，点击排序全废 —— 极易误判成「自绘破坏了排序」。
        self._header = _SortHeader(palette, self.view)
        self.view.setHeader(self._header)
        self.view.setRootIsDecorated(False)
        self.view.setUniformRowHeights(True)
        self.view.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setIconSize(QtCore.QSize(16, 16))
        self.view.setIndentation(0)
        self.view.setMouseTracking(True)
        self.view.doubleClicked.connect(self._on_double_clicked)
        # **Enter 也要能进目录。** 原来只连了 doubleClicked，`activated`
        # 信号全仓无人接收 —— 键盘用户选中目录按回车毫无反应，文件浏览器
        # 的主功能就此失效，只能手敲绝对路径。同仓 `snippet_panel.py` 用的
        # 就是 `itemActivated`（双击 / 回车一个信号覆盖两种），这里是漏了。
        self.view.activated.connect(self._on_double_clicked)
        self._dir_icon = icons.icon("folder", palette, 16, palette.accent)
        self._file_icon = icons.icon("file", palette, 16, palette.text_muted)

        # ---- path bar ----
        self.path_edit = QtWidgets.QLineEdit(self)
        self.path_edit.setPlaceholderText("输入路径后回车")
        self.path_edit.returnPressed.connect(self._on_path_entered)

        self.up_btn = _icon_button("folder-up", "上一级", palette)
        self.up_btn.clicked.connect(self.go_up)
        self.refresh_btn = _icon_button("refresh", "刷新", palette)
        self.refresh_btn.clicked.connect(lambda: self.refresh(self._cwd))
        self.mkdir_btn = _icon_button("folder-plus", "新建目录", palette)
        self.mkdir_btn.clicked.connect(self._on_mkdir)
        self.delete_btn = _icon_button("trash", "删除所选", palette)
        self.delete_btn.clicked.connect(self._on_delete)

        # 按钮自己记住自己用的是哪个图标。换主题时从这里重画，**不要**在
        # apply_palette 里再手抄一遍图标名 —— 抄漏一处，切主题（Ctrl+T）就会
        # 把那个按钮悄悄打回旧图标，界面上看不出任何异常。
        self._nav_buttons = (self.up_btn, self.refresh_btn,
                             self.mkdir_btn, self.delete_btn)

        # **「删除所选」在没选中任何东西时必须是禁用的。** `_on_delete` 拿到空
        # 选中是直接 `return`：用户点了什么也不会发生，而且**没有任何提示** ——
        # 那正是这个项目反复在修的「点了没反应」。禁用它比弹一句「请先选择」
        # 更早一步告诉用户缺什么，也和隧道面板的「移除」行为一致（那边在没
        # 规则时就是禁用的）。
        self.view.selectionModel().selectionChanged.connect(
            lambda *_: self._sync_delete_btn())
        self._sync_delete_btn()

        nav = QtWidgets.QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(4)
        nav.addWidget(self.up_btn)
        nav.addWidget(self.refresh_btn)
        nav.addStretch(1)
        nav.addWidget(self.path_edit, 1)

        ops = QtWidgets.QHBoxLayout()
        ops.setContentsMargins(0, 0, 0, 0)
        ops.setSpacing(4)
        ops.addWidget(self.mkdir_btn)
        ops.addWidget(self.delete_btn)
        ops.addStretch(1)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addLayout(head)
        layout.addWidget(self.view, 1)
        layout.addLayout(nav)
        layout.addLayout(ops)

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self._dir_icon = icons.icon("folder", palette, 16, palette.accent)
        self._file_icon = icons.icon("file", palette, 16, palette.text_muted)
        for btn in self._nav_buttons:
            _retheme_icon_button(btn, palette)
        # 表头是**自绘**的，颜色烤在画笔里而不是走 QSS —— 不显式推一次，
        # 浅色切深色之后箭头还留在浅色的 `p.text` 上，对深底几乎隐形
        # （实测 delta 1.9）。不崩、不报、也没有任何测试会红。
        self._header.apply_palette(palette)
        self.refresh()

    # ------------------------------------------------------------------ nav

    def cd(self, path: str) -> None:
        self._cwd = self._normalize(path)
        self.path_edit.setText(self._cwd)
        self.refresh(self._cwd)
        self.pathChanged.emit(self._cwd)

    def go_up(self) -> None:
        if self._side == "local":
            # **已经在盘符根就停住。** `ntpath.dirname("D:\\")` 返回 `"D:\\"`，
            # 而 `dirname("D:")` 返回 **`"D:"`（非空）** —— 所以原来那句
            # `or self._cwd` 的兜底是死的：`cd("D:")` 经 abspath 会变成
            # **进程工作目录**，点一下「上一级」就从盘符根跳进应用启动目录，
            # 还要再点三次才回得来，且全程无任何报错。
            cur = self._cwd.rstrip("\\/")
            if len(cur) <= 3:               # "C:" 或 "C:\"，到顶了
                return
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                return
        else:
            parent = self._cwd.rstrip("/")
            if not parent:
                parent = "/"
            elif "/" in parent:
                parent = parent.rsplit("/", 1)[0] or "/"
        self.cd(parent)

    def refresh(self, _path: str = "") -> None:
        """刷新列表。

        **远程侧不再在 GUI 线程上做 SFTP 往返** —— 原来这里是直接
        `self._fs.listdir(self._cwd)`，实测 0.5s RTT 就把界面整段卡死
        0.5s（网慢时是几秒），而且没有任何进行中指示。改走
        `_ListWorker`：网络调用在工作线程，填充回 GUI 线程。

        本地面板是本地磁盘，继续同步 —— 一次 `os.scandir` 快到不值得
        开线程，而开了反而多一次线程同步。
        """
        self._cwd = self._normalize(self._cwd)
        self.path_edit.setText(self._cwd)
        if self._side != "remote":
            self.model.removeRows(0, self.model.rowCount())
            try:
                entries = self._fs.listdir(self._cwd)
            except Exception as exc:
                self._load_failed(exc)
                return
            self._populate(entries)
            return

        self._list_req += 1
        req = self._list_req
        if self._list_worker is None:
            self._list_worker = _ListWorker(self._fs, self)
            self._list_worker.listed.connect(self._on_listed)
            self._list_worker.failed.connect(self._on_list_failed)
        # 拿一个「正在转」的提示：不给反馈的话用户会以为按钮没生效，
        # 于是连点好几下。
        self.count_label.setText("读取中…")
        # **起线程必须兜住。** 抛了（线程耗尽、系统拒绝建线程）如果放任它
        # 逃出去，`refresh()` 是从 `clicked` / `returnPressed` 槽进来的，
        # 异常会一路冒到事件循环 —— 而 `count_label` 已经停在「读取中…」，
        # 面板既不报错也刷不出来，看起来就是"点了没反应"。
        # 走 `failed` 这条通道，和真正的 SFTP 失败同一个出口。
        try:
            self._list_worker.start(req, self._cwd)
        except Exception as exc:                       # noqa: BLE001
            self._on_list_failed(req, exc)

    def _on_listed(self, req: int, entries) -> None:
        # 请求号对不上 = 用户在往返期间又切了目录/又点了一次刷新，
        # 这次结果是**过期的**，填进去会把新目录覆盖掉。
        if req != self._list_req:
            return
        self.model.removeRows(0, self.model.rowCount())
        self._populate(entries)

    def _on_list_failed(self, req: int, exc: object) -> None:
        if req != self._list_req:
            return
        self.model.removeRows(0, self.model.rowCount())
        # 传的是**原异常对象**，不是 `RuntimeError(str)` —— 后者会把真实的
        # `SFTPError` / `OSError` 抹成 RuntimeError，而 `_load_failed` 打
        # 的正是 `type(exc).__name__`，等于把唯一的诊断线索毁掉。
        self._load_failed(exc if isinstance(exc, BaseException)
                          else RuntimeError(str(exc)))

    def fetch_async(self) -> list[FileEntry]:
        """只取数据不碰控件 —— 可以在工作线程上跑。"""
        return self._fs.listdir(self._cwd)

    def populate_from(self, entries: list[FileEntry]) -> None:
        """把 ``fetch_async`` 的结果填进控件 —— 只能在 GUI 线程上跑。

        传输完成后刷新远程列表就走这条路：网络调用在工作线程，界面更新回
        GUI 线程，两边都不互相拖住。
        """
        # **必须推进请求号。** 这一条原来是漏的：传输完成把新列表填进去之后，
        # 之前那次还在飞的 refresh 结果回来时 `req` 仍然对得上（没人动过
        # `_list_req`），于是照样 `removeRows` + `_populate`，**把刚传完的
        # 文件从列表里抹掉**。实测 `['uploaded.txt']` 变成 `['T']`。
        self._list_req += 1
        self._cwd = self._normalize(self._cwd)
        self.path_edit.setText(self._cwd)
        self.model.removeRows(0, self.model.rowCount())
        self._populate(entries)

    def _load_failed(self, exc: Exception) -> None:
        self.count_label.setText("读取失败")
        QtWidgets.QToolTip.showText(
            QtGui.QCursor.pos(), f"{type(exc).__name__}: {exc}", self
        )
        if os.environ.get("MAVISSSH_DEBUG"):
            import traceback
            traceback.print_exc()

    def _populate(self, entries: list[FileEntry]) -> None:
        for e in entries:
            name_item = QtGui.QStandardItem(e.name)
            name_item.setData(e, QtCore.Qt.ItemDataRole.UserRole)
            name_item.setIcon(self._dir_icon if e.is_dir else self._file_icon)
            # **名称列装不下文件名，tooltip 是唯一的出路。** 这个面板是
            # 双栏（`local | 150px | remote`，见本文件底部），主窗最小 1020
            # 时每侧视口只有 269px，而名称列在 Qt 默认下恒为 100px ——
            # `docker-compose.yml` 要 122px、`access.log.2026-10-03.gz`
            # 要 150px，都会被省略号截掉。文件名长度没有上界，**调列宽在
            # 物理上解决不了这件事**，只有 tooltip 能回答「这文件叫什么」。
            # 完整路径另有下方的路径栏兜底，tooltip 只放 basename。
            name_item.setToolTip(e.name)
            if e.is_dir:
                name_item.setForeground(QtGui.QColor(self._palette.text))
            else:
                name_item.setForeground(QtGui.QColor(self._palette.text_dim))
            size_item = QtGui.QStandardItem(self._format_size(e.size) if not e.is_dir else "")
            size_item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
            mtime_item = QtGui.QStandardItem(e.mtime_str)
            mode_item = QtGui.QStandardItem(self._format_mode(e.mode))
            # 另外三列同样在窄列里会被截断，一并挂上。空值不用挂 ——
            # 目录的大小列本来就是空的，弹一个空气泡只会烦人。
            for item, text in ((size_item, size_item.text()),
                               (mtime_item, e.mtime_str),
                               (mode_item, mode_item.text())):
                if text:
                    item.setToolTip(text)
            mode_item.setForeground(QtGui.QColor(self._palette.text_muted))
            mtime_item.setForeground(QtGui.QColor(self._palette.text_muted))
            size_item.setForeground(QtGui.QColor(self._palette.text_muted))
            self.model.appendRow([name_item, size_item, mtime_item, mode_item])
        n_dir = sum(1 for e in entries if e.is_dir)
        n_file = len(entries) - n_dir
        self.count_label.setText(f"{n_dir} 个目录 · {n_file} 个文件")
        # 重新填过模型之后选中一定是空的，所以这里要**显式**再收一次：
        # 指望 selectionChanged 触发是赌它一定会在"从有到无"时发一次。
        self._sync_delete_btn()

    def _sync_delete_btn(self) -> None:
        """「删除所选」跟着选中走；有慢操作在飞时也必须保持禁用。"""
        self.delete_btn.setEnabled(
            not self._busy and bool(self.selected_entries()))

    def _set_busy(self, busy: bool) -> None:
        """慢操作在飞：把同样会抢这条通道的按钮一起禁掉。

        **不用模态框去挡第二次点击。** 禁用是**看得见**的反馈；弹窗只能
        在用户已经点下去之后告诉他「不行」，那正是这个项目在反复修的
        「点了没反应」。
        """
        self._busy = busy
        self.mkdir_btn.setEnabled(not busy)
        self._sync_delete_btn()

    def current_path(self) -> str:
        return self._cwd

    def selected_entries(self) -> list[FileEntry]:
        rows = sorted({i.row() for i in self.view.selectionModel().selectedRows()})
        out: list[FileEntry] = []
        for r in rows:
            item = self.model.item(r, 0)
            if item:
                out.append(item.data(QtCore.Qt.ItemDataRole.UserRole))
        return out

    # ------------------------------------------------------------------ ops

    def _on_double_clicked(self, idx: QtCore.QModelIndex) -> None:
        entry: FileEntry = self.model.item(idx.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        if entry.is_dir:
            self.cd(entry.path)

    def _on_path_entered(self) -> None:
        self.cd(self.path_edit.text())

    def _on_mkdir(self) -> None:
        name, ok = msgbox.ask_text(self, "新建目录", "目录名：")
        if not ok or not name.strip():
            return
        new = (self._cwd.rstrip("/") + "/" + name.strip()) if self._side == "remote" else os.path.join(self._cwd, name.strip())
        self._run_off_thread(
            self._fs.mkdir, new,
            fail_title="新建目录失败",
            done_text="已新建「%s」" % name.strip(),
        )

    def _on_delete(self) -> None:
        entries = self.selected_entries()
        if not entries:
            return
        if msgbox.question(
            self,
            "删除",
            f"确定删除选中的 {len(entries)} 个项目？\n\n"
            "此操作不可撤销。",
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self._run_off_thread(
            self._delete_entries, entries,
            fail_title="删除失败",
        )

    def _delete_entries(self, entries) -> None:
        """跑在工作线程里：一条一条删，失败的攒起来一起报。"""
        failed = []
        for e in entries:
            try:
                if e.is_dir:
                    self._fs.rmdir(e.path)
                else:
                    self._fs.remove(e.path)
            except Exception as exc:                # noqa: BLE001
                failed.append("%s\n%s" % (e.path, exc))
        if failed:
            raise RuntimeError("\n\n".join(failed))

    def _run_off_thread(self, fn, *args, fail_title: str,
                         done_text: str = "") -> None:
        """在 GUI 线程之外做一次会抢 SFTP 通道的慢操作。

        **为什么不能只给 `_held()` 加超时。** 那会把「冻窗」换成「假失败」：
        用户在网慢的时候点新建目录，看到的是「新建目录失败 —— 通道忙」，
        而其实只是慢。冻窗至少是诚实的，假失败会让人反复重试。

        **为什么工作线程里不能调 `self.refresh()`。** 那是跨线程动控件，
        正是 `_TransferWorker` 那段注释声称已经修掉的那类 bug。刷新必须
        回 GUI 线程，由这里统一的槽来做。
        """
        if self._op_worker is not None:
            return                        # 按钮已禁用，走到这里是程序化调用
        self.count_label.setText("处理中…")
        self._set_busy(True)
        self._op_worker = _OpWorker(fn, args, fail_title, done_text, self)
        self._op_worker.ok.connect(self._on_op_ok)
        self._op_worker.failed.connect(self._on_op_failed)
        try:
            self._op_worker.start()
        except Exception as exc:            # noqa: BLE001
            # 同 `refresh()`：起线程失败必须走同一条出口，否则按钮会
            # 永远停在禁用态，而界面既不报错也没有任何提示。
            self._on_op_failed(fail_title, exc)

    def _on_op_ok(self, done_text: str) -> None:
        self._clear_op_worker()
        if done_text and self._flash is not None:
            self._flash(done_text)
        self.refresh()

    def _on_op_failed(self, title: str, exc: object) -> None:
        self._clear_op_worker()
        self.refresh()
        msgbox.warning(self, title, str(exc))

    def _clear_op_worker(self) -> None:
        w = self._op_worker
        self._op_worker = None
        self._set_busy(False)
        if w is not None:
            w.deleteLater()

    # ------------------------------------------------------------------ utils

    def _normalize(self, path: str) -> str:
        if self._side == "local":
            return os.path.abspath(path) if path else os.getcwd()
        if not path:
            return "/"
        return path

    @staticmethod
    def _format_size(n: int) -> str:
        # 与 transfer_strip.format_bytes **故意**不是同一个格式：这里要的是
        # 表格里那种紧凑写法（"2.0M"），状态条那行小字要的是"2.0 MB"。
        # 合成一个函数就得加个 gap/units 参数去伺候两处，反而更难读。
        units = ["B", "K", "M", "G", "T"]
        size = float(n)
        for u in units:
            if size < 1024 or u == units[-1]:
                return f"{int(size)}{u}" if u == "B" else f"{size:.1f}{u}"
            size /= 1024
        return str(n)

    @staticmethod
    def _format_mode(mode: int) -> str:
        if not mode:
            return ""
        try:
            return stat.filemode(mode)
        except Exception:
            return ""


class _OpWorker(QtCore.QObject):
    """在 daemon 线程上跑一次慢的 SFTP 操作（建目录 / 删除），结果发回 GUI 线程。

    和 `_ListWorker` 同一个理由：`_fs` 那几把锁是**无超时**的（`_held`
    的 ``timeout=None`` 分支直接 ``acquire()``），而 `upload`/`download`
    在**整个文件传输期间**都持锁。原来的 `_on_mkdir` / `_on_delete` 在 GUI
    线程上抢它 —— 于是「大文件上传中点删除」= 界面冻结整个上传时长。
    用户等不下去强杀进程，远端就留下半截文件。
    """

    ok = QtCore.pyqtSignal(str)             # 完成提示（可为空串）
    failed = QtCore.pyqtSignal(str, object)  # 失败标题, **原异常对象**

    def __init__(self, fn, args, fail_title: str, done_text: str,
                 parent=None):
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._fail_title = fail_title
        self._done_text = done_text

    def start(self) -> None:
        def run() -> None:
            try:
                self._fn(*self._args)
            except Exception as exc:               # noqa: BLE001
                # 传**对象**不是 `str`：失败提示要打 `type(exc).__name__`
                # 才认得出是 SFTPError 还是 PermissionError。
                self._emit("failed", self._fail_title, exc)
            else:
                self._emit("ok", self._done_text)

        threading.Thread(target=run, daemon=True,
                         name="sftp-op").start()

    def _emit(self, name: str, *args) -> None:
        try:
            getattr(self, name).emit(*args)
        except RuntimeError:
            pass                            # 面板已随标签页析构


class _ListWorker(QtCore.QObject):
    """在 daemon 线程上做一次 ``listdir``，结果发回 GUI 线程。

    远程侧原来是在 GUI 线程上直接 ``listdir``，而那是一次**阻塞的 SFTP
    往返** —— 实测 0.5s RTT 就把调用线程整段卡死 0.5s。网再慢一点，
    开会话、刷新、建目录、删除全程冻窗，而且**没有任何进行中指示**。

    和 `_TransferWorker` 同一套理由用 ``QObject`` + daemon 线程而不是
    ``QThread``：面板是标签页的子控件，父级析构时它压根收不到
    ``closeEvent``，没有地方 ``wait()``；而仍在运行的 ``QThread`` 在
    析构时是直接 abort 的。

    **过期的结果必须丢掉**：用户在 listdir 往返期间又切了目录的话，
    早先那次的结果回来就会把新目录的内容覆盖掉。用一个单调递增的
    请求号当闸门，回来时对不上就丢。
    """

    listed = QtCore.pyqtSignal(int, object)   # 请求号, entries
    failed = QtCore.pyqtSignal(int, object)   # 请求号, **原异常对象**

    def __init__(self, fs, parent=None):
        super().__init__(parent)
        self._fs = fs

    def start(self, req: int, cwd: str) -> None:
        def run() -> None:
            try:
                entries = self._fs.listdir(cwd)
            except Exception as exc:              # noqa: BLE001
                # 传**对象**不是 `str`：下游要打 `type(exc).__name__`，
                # 转成字符串再包一层 RuntimeError 就等于把唯一的诊断线索毁掉。
                self._emit("failed", req, exc)
            else:
                self._emit("listed", req, entries)
        threading.Thread(target=run, daemon=True,
                         name="sftp-listdir").start()

    def _emit(self, name: str, *args) -> None:
        try:
            getattr(self, name).emit(*args)
        except RuntimeError:
            pass                            # 面板已随标签页析构


class _TransferWorker(QtCore.QObject):
    """在一批 (fn, src, dst, 标题) 上顺序跑传输。

    刻意不碰任何控件：进度、错误框、刷新后的列表统统以信号发回 GUI 线程。
    修复前这里是在 ``threading.Thread`` 里直接调 ``progress.setValue()`` 和
    ``QMessageBox.warning(self, ...)`` —— 跨线程操作控件，其中模态框会跑一个
    嵌套事件循环，界面就此卡死、鼠标变转圈，而异常本身反而没人看见。

    **为什么是 ``QObject`` + daemon 线程，而不是 ``QThread``**：``SftpPanel``
    是标签页的子控件，关闭标签时它是被父级析构掉的，压根不会收到
    ``closeEvent`` —— 也就没有地方去 ``wait()``。而一个仍在运行的 ``QThread``
    在析构时是直接 abort 的，那等于把"界面转圈"换成了"关标签就崩"。
    daemon 线程则会在进程退出时干净了事，剩下的问题只是"面板没了还在发信号"，
    由 ``_emit`` 兜住。
    """

    # 注意别叫 `started` —— 下面 `self.started` 已经是"这批什么时候开始的"
    # 那个计时戳了，同名会让实例属性把类属性盖掉，接线时拿到一个 float。
    file_started = QtCore.pyqtSignal(int, str)    # 索引, 文件名
    progressed = QtCore.pyqtSignal(int, int, int, int)   # 索引, 已传, 总量, 速度
    listed = QtCore.pyqtSignal(str, object)
    failed = QtCore.pyqtSignal(str)
    done = QtCore.pyqtSignal()

    def __init__(self, jobs, rfs, refresh_fs, refresh_dir, parent=None):
        super().__init__(parent)
        self._jobs = jobs
        self._rfs = rfs
        self._refresh_fs = refresh_fs
        self._refresh_dir = refresh_dir
        self._cancel = False
        self.cancel_requested = False   # 用户按过取消
        self.cancelled = False          # 真的停下来过（有文件没轮到）
        self.done_count = 0
        self.started = time.monotonic()
        self.finished = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="sftp-transfer").start()

    def request_cancel(self) -> None:
        """进度条上的"取消"。

        paramiko 没有中途放弃一个 put 的办法(回调返回 False 只是停止写,
        远端会留下半个文件),所以这里只让**后续**文件不再开始,已经发出去
        的那一个照常传完 —— 因此 ``cancel_requested`` 与 ``cancelled``
        是两回事:单文件时按取消往往已经晚了,那个文件真的传完了,报"完成"
        才是实话。
        """
        self.cancel_requested = True
        self._cancel = True

    def _emit(self, name: str, *args) -> None:
        """面板可能已随标签页一起析构，这时 C++ 对象没了，发信号会抛。

        **传信号的名字，不要传信号本身。** 面板被销毁后 `self.xxx` 这个
        取属性动作本身就会抛 `RuntimeError`（PyQt 要读已释放对象的 metaobject），
        发生在调用方那一行、`_emit` 进来之前 —— 只护 `.emit()` 是护不住的，
        那样整条 traceback 会从工作线程里冒出来。
        """
        try:
            getattr(self, name).emit(*args)
        except RuntimeError:
            pass

    def _run(self) -> None:  # 跑在 daemon 线程里
        for i, job in enumerate(self._jobs):
            fn, src, dst, label, name = job
            if self._cancel:
                self.cancelled = True
                break
            self._emit("file_started", i, name)
            try:
                fn(src, dst, progress=self._make_cb(i, name))
            except Exception as exc:
                msg = f"{label} 失败：\n{type(exc).__name__}: {exc}"
                # **远端临时件没能确认删除，要在这里说出来。**
                # 原来这条信息只存在于代码里：`_remote_unlink_quiet` 的返回值
                # 两个调用点都丢了，于是「远端留了个用户看不见的残件」这件事
                # 永远不会被用户知道。用 `add_note` 也不行 —— 上面那句 `exc`
                # 才是用户看到的全部，`note` 只进 traceback，界面上一个字都不
                # 多。所以必须由**拿到 RemoteFileSystem 的这一侧**取出来拼进
                # 文案：残件记在**传输用的那一个**上（`fn` 就是
                # `self._rfs.upload`，见 `_upload`），不是刷新用的那一个 ——
                # 两个是不同的对象，取错了就永远取不到东西。
                left = self._rfs.take_leftovers()
                if left:
                    msg += ("\n\n远端临时文件没能确认删除，它还在那里，"
                            "可以手动删掉：\n" + "\n".join(left))
                self._emit("failed", msg)
                break
            self.done_count += 1
        else:
            # 只有全部成功才刷新；刷新同样在工作线程上取数
            try:
                entries = self._refresh_fs.listdir(self._refresh_dir)
            except Exception:
                entries = None
            if entries is not None:
                self._emit("listed", self._refresh_dir, entries)
        self.finished.set()
        self._emit("done")

    def _make_cb(self, index: int, name: str):
        """把 paramiko 的 ``(done, size)`` 原样报上去，**不拼任何界面文案**。

        文案是 :class:`TransferReport` 的活：ZMODEM 那条路喂进来的是 lrzsz
        写在 stderr 上的进度行，两者必须落到同一个渲染器上，否则状态条就得
        认得两种格式。
        """
        t0 = time.monotonic()

        def cb(done: int, size: int) -> None:
            if self._cancel or size <= 0:
                return
            # 速度只对当前这个文件有意义：整批的均速在大小悬殊时会骗人
            # （一个 1KB 文件会把均速拉到离谱的快）。
            bps = done / max(1e-6, time.monotonic() - t0)
            self._emit("progressed", index, done, size, int(bps))
        return cb


class SftpTransfer:
    """把 :class:`_TransferWorker` 包装成 :class:`TransferAdapter`。

    **收尾的判断留在这里，不上移。** 只有引擎知道自己是"全传完了"还是"被
    取消后提前停了"；控制器拿到的只是一个已经定好的结果，不该去猜。
    """

    def __init__(self, jobs, rfs, ctrl: TransferController,
                 refresh_fs=None, refresh_dir: str = "", note: str = "",
                 on_done: Callable[[], None] | None = None):
        self._ctrl = ctrl
        self._note = note
        self._total = len(jobs)
        self._on_done_cb = on_done
        # **这里不传 QObject 父对象。** `SftpTransfer` 本身不是 QObject，
        # `QObject.__init__` 只收 QObject 参数，传面板会直接
        # `TypeError: argument 1 has unexpected type 'SftpTransfer'`。
        #
        # **但「拿不到面板的 QObject 引用」是错的**（这句原来就写在这儿）：
        # `_start_transfers` 是 `SftpPanel`（一个 `QWidget`）的方法，
        # `self` 就是面板那个 QObject —— 真要传父对象，加一个形参的事，
        # 不是结构改动。
        #
        # 那为什么还是不传？**因为传了没有收益，只有新风险**：生命周期现在
        # 已经有人管（`self._transfer` 强引用 + `TransferController._adapter`
        # 持有，收尾时置 None），而挂上父对象之后，面板一析构就会连带析构
        # 仍在跑的 worker 的 C++ 对象，线程里再读它只剩 RuntimeError。
        # 评审实测：不传时 `done` 正常送达；传了之后信号全部被吞、没人收。
        #
        # 全仓（生产 + tests）**只有一处构造** `SftpTransfer`，都没传 parent，
        # 所以那条 TypeError 至今没有被触发过 —— 它是「还没被踩到的坑」，
        # 不是现存缺陷。
        self.worker = _TransferWorker(jobs, rfs, refresh_fs, refresh_dir)
        self.worker.file_started.connect(self._on_started)
        self.worker.progressed.connect(self._on_progressed)
        self.worker.listed.connect(self._on_listed)
        self.worker.failed.connect(self._on_failed)
        self.worker.done.connect(self._on_done)
        self._listed_cb: Callable[[str, list], None] | None = None

    def start(self) -> None:
        self.worker.start()

    def request_cancel(self) -> None:
        self.worker.request_cancel()

    def set_listed_callback(self, cb: Callable[[str, list], None] | None) -> None:
        self._listed_cb = cb

    # -- 全部落在 GUI 线程 --------------------------------------------------
    def _ctrl_gone(self) -> bool:
        """会话（连同这条状态条）是不是已经被析构了。

        **用户关掉的是整个会话标签**，`TransferController` 跟着 `SessionTab`
        一起没，而工作线程还会在那之后把 `progressed` / `done` 送回来。原来
        下面这几个槽直接 `self._ctrl.advanced(...)`，动作落在已释放的 C++
        对象上 → `RuntimeError` 抛在信号槽里 → 弹一个用户看不懂的「发生
        错误」框，外观就是「关掉标签之后程序莫名其妙报了个错」。判据见
        `_fsftp.py:18`。

        **只挡这一种。** 别的异常照常冒出来 —— 这里不是给真 bug 兜底的。
        """
        return sip.isdeleted(self._ctrl)

    def _on_started(self, index: int, name: str) -> None:
        if self._ctrl_gone():
            return
        self._ctrl.file_started(index, name)

    def _on_progressed(self, index: int, done: int, size: int, bps: int) -> None:
        # 索引和文件名由 _on_started 设好了：两个信号是发给同一个接收线程的
        # 队列，顺序有保证，这里不用再查一遍文件名。
        if self._ctrl_gone():
            return
        self._ctrl.advanced(done, size, bps)

    def _on_listed(self, path: str, entries) -> None:
        if self._listed_cb is not None:
            self._listed_cb(path, entries)

    def _on_failed(self, message: str) -> None:
        if self._ctrl_gone():
            return
        self._ctrl.fail(message)

    def _on_done(self) -> None:
        w = self.worker
        if self._ctrl_gone():
            return
        if w.cancelled:
            left = self._total - w.done_count
            detail = self._note
            if left > 0:
                tail = f"还有 {left} 个文件没有传输"
                detail = f"{detail} · {tail}" if detail else tail
            self._ctrl.cancel(detail)
        else:
            self._ctrl.succeed(self._note)
        # 失败那条路也会走到这里（failed 之后 worker 照样 done），所以按钮
        # 一定会被重新放开。
        if self._on_done_cb is not None:
            self._on_done_cb()


# ---------------------------------------------------------------------------
# Combined SFTP panel (local + remote) with transfer buttons.


class SftpPanel(QtWidgets.QWidget):
    """Side-by-side local & remote panels with upload/download buttons."""

    def __init__(self, sftp: paramiko.SFTPClient, palette=None, parent=None,
                 ctrl: TransferController | None = None,
                 flash=None):
        super().__init__(parent)
        self._rfs = RemoteFileSystem(sftp)
        self._lfs = LocalFileSystem()
        self._transfer: SftpTransfer | None = None
        self._refresh_panel: FilePanel | None = None
        p = palette or get_palette("dark")
        self._palette = p

        self.local = FilePanel("local", self._lfs, p, self)
        self.remote = FilePanel("remote", self._rfs, p, self)
        # 状态条提示：建目录/删除跑完的那句「已新建 xxx」要有个地方显示。
        # 面板自己的 `count_label` 会被随后的 refresh 立刻覆盖掉。
        self.set_flash(flash)

        # ---- transfer buttons ----
        self.up_btn = QtWidgets.QPushButton("  上传")
        self.up_btn.setProperty("variant", "primary")
        _set_icon(self.up_btn, "upload", p, 15, p.text_on_solid)
        self.up_btn.setIconSize(QtCore.QSize(15, 15))
        self.up_btn.setToolTip("将选中的本地文件上传到远程当前目录")
        self.up_btn.setFixedHeight(36)
        self.up_btn.setMinimumWidth(120)
        self.up_btn.clicked.connect(self._upload)

        self.down_btn = QtWidgets.QPushButton("  下载")
        _set_icon(self.down_btn, "download", p, 15)
        self.down_btn.setIconSize(QtCore.QSize(15, 15))
        self.down_btn.setToolTip("将选中的远程文件下载到本地当前目录")
        self.down_btn.setFixedHeight(36)
        self.down_btn.setMinimumWidth(120)
        self.down_btn.clicked.connect(self._download)

        center = QtWidgets.QVBoxLayout()
        center.setContentsMargins(10, 0, 10, 0)
        center.addStretch(1)
        center.addWidget(self.up_btn)
        center.addSpacing(8)
        center.addWidget(self.down_btn)
        center.addStretch(1)
        center.addWidget(QtWidgets.QLabel("↑ 本地 → 远程\n↓ 远程 → 本地"))
        center.itemAt(center.count() - 1).widget().setProperty("role", "muted")
        center.itemAt(center.count() - 1).widget().setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        center_w = QtWidgets.QWidget()
        center_w.setLayout(center)
        center_w.setFixedWidth(150)

        split = QtWidgets.QHBoxLayout()
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)
        split.addWidget(self.local, 1)
        split.addWidget(center_w)
        split.addWidget(self.remote, 1)

        # 传输状态条贴在会话底部，而不是弹一个模态框抢焦点 —— 一次后台传输
        # 不该打断用户，详见 transfer_strip.py 的开头。
        # 正常情况下由 SessionTab 注入**控制器**——整个会话只有那一条状态条，
        # 文件 tab 的 SFTP 和工具栏的 ZMODEM 都往它上面报，也共用它那个
        # 忙时闸门（否则能在同一次会话里同时点两次上传，把同一条 shell
        # 通道搞坏）。独立构造（测试）时没有，就自己建一套。
        self.ctrl = ctrl if ctrl is not None else TransferController(p, self)
        self.strip = self.ctrl.strip

        # **绝不能把这条状态条 addWidget 进本面板的布局。**
        #
        # `QLayout::addWidget` 会把控件 **reparent** 到该布局所属的窗口。
        # 正常接线下这条状态条是 SessionTab 的（`main_window.py:321` 已经
        # 把它放进 SessionTab 的 `strip_row`，位于子标签栏下方），于是这一句
        # 等于把它从 SessionTab **抢**到 SftpPanel 里：实测 attach 之后
        # `strip.parent()` 变成 SftpPanel，`strip.isVisibleTo(会话窗口)` 为
        # False —— **ZMODEM 跑在「终端」这一页，而进度条被关进了「文件」
        # 页，用户什么都看不到**；而且关掉文件面板会把这条会话级状态条
        # 一起析构掉（连同它那两支 QTimer），之后连「再传一次」都会抛。
        # 这正是 `main_window.py:300-304` 注释里明写绝不能发生的事。
        #
        # **只有「没人给它安排位置」时才自己摆。** 独立构造时那条状态条是
        # 本面板建的（父级就是本面板，但不在任何布局里），按浮层贴在底部 ——
        # 浮层不进布局，所以面板尺寸变化不会把它顶来顶去，空闲时它整个隐藏、
        # 不占地方，这个行为和进布局时是一样的（见 `window_frame.py`：不占
        # 布局的东西，布局压根不认它）。
        self._strip_own = (self.strip.parent() is None
                           or self.strip.parent() is self)
        if self._strip_own:
            self.strip.setParent(self)
            self.strip.raise_()

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addLayout(split, 1)

        # Initial dirs
        self.local.cd(os.getcwd())
        try:
            self.remote.cd(self._rfs.getcwd())
        except Exception:
            self.remote.cd("/")
        self._place_strip()

    def _place_strip(self) -> None:
        """浮层模式下自己给状态条定位（布局不认它，见 `__init__` 那段）。"""
        if not self._strip_own:
            return
        h = self.strip.sizeHint().height()
        self.strip.setGeometry(0, max(0, self.height() - h), self.width(), h)
        self.strip.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_strip()

    def set_flash(self, flash) -> None:
        """把会话的状态条提示函数交给两侧面板（由 SessionTab 注入）。

        **`self._flash` 自己也要一份。** `FilePanel.__init__` 里有
        `self._flash = None`，而 `SftpPanel` 不是 `FilePanel` 的子类
        （它直接继承 `QWidget`），所以这一支**从来没有被赋值过** ——
        于是 `_start_transfers` 里那句「闸门拒绝时必须出声」的
        `self._flash(...)` 必然抛 `AttributeError`。
        专门用来消灭静默的那句提示自己会炸，这条修复等于没落地。
        """
        self._flash = flash
        self.local._flash = flash
        self.remote._flash = flash

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.local.apply_palette(palette)
        self.remote.apply_palette(palette)
        self.strip.apply_palette(palette)
        _retheme_icon_button(self.up_btn, palette, palette.text_on_solid)
        _retheme_icon_button(self.down_btn, palette)

    # ------------------------------------------------------------------ transfers

    # -- 显式路径入口（给 ZMODEM 的 SFTP 回退用）--------------------------
    #
    # 上面的 _upload/_download 走的是"用户在树里选了什么"，而回退那条路手上
    # 已经有一份明确的路径表了（用户点上传时在文件框里选的那几个，或者
    # 下载时在输入框里敲的那几个）。**让用户再点一遍树是把活儿甩回去**，
    # 所以这里给一条按路径来的入口，复用同一套任务表和状态条。

    def upload_paths(self, local_paths, remote_dir: str | None = None
                     ) -> int:
        """把 *local_paths* 上传到 *remote_dir*（默认取右侧当前目录）。

        返回真正排进队列的文件数。目录会被跳过并在结束后提示。
        """
        base = (remote_dir or self.remote.current_path()).rstrip("/\\")
        jobs, skipped = [], []
        for p in local_paths:
            p = str(p)
            name = os.path.basename(p.rstrip("/\\")) or p
            if not name:
                continue
            if os.path.isdir(p):
                skipped.append(name)
                continue
            dst = base + "/" + name
            # **这里原来完全没有覆盖询问。** 加「上传下载逐个问覆盖」那次
            # 只改了 _upload/_download 两个入口，ZMODEM 回退走的正是这两个
            # 按路径的入口 —— 同名文件被静默替换，状态条还照报「上传完成」。
            # 远端同名要发一次 stat；这条路本来每个文件也要一次往返，多这一次
            # 换掉「静默覆盖用户文件」是值的。
            # **问不到（None）不能当「没有」。** 那会让一次探测失败直接变成
            # 一次静默覆盖，而状态条照报「上传完成」。跳过这一项并说出来。
            exists = self._remote_exists(dst)
            if exists is None:
                skipped.append(name + "（没能确认远端有没有同名文件，未传输）")
                continue
            if exists:
                if msgbox.question(
                        self, "上传",
                        "「%s」在目标位置已存在。\n\n要覆盖它吗？" % name
                ) != QtWidgets.QMessageBox.StandardButton.Yes:
                    skipped.append(name + "（已存在，未覆盖）")
                    continue
            jobs.append((self._rfs.upload, p, dst, "上传 " + name, name))
        if not jobs:
            self._report_skipped(skipped, "上传")
            return 0
        self._start_transfers(jobs, "upload", "上传", self.remote, base, skipped)
        return len(jobs)

    def download_paths(self, remote_paths, local_dir: str | None = None
                       ) -> int:
        """把 *remote_paths* 下载到 *local_dir*（默认取左侧当前目录）。

        远端路径是绝对路径，所以逐个按 ``basename`` 落到目标目录下——回退这条
        路最容易出的错就是把整条路径当文件名拼出去，在本地建出一串子目录。
        """
        base = local_dir or self.local.current_path()
        jobs, skipped, undecided = [], [], []
        for p in remote_paths:
            p = str(p).rstrip("/")
            name = p.rsplit("/", 1)[-1]
            if not name:
                skipped.append(p or "(空路径)")
                continue
            # **问不到类型就不排进队列。** 原来 `is_dir` 超时会返回 False，
            # 于是通道一忙就把远程**目录**当成文件排进队列，`get()` 在服务端
            # 报一个用户看不懂的错，而状态条照报「下载失败」。宁可这一次
            # 不传，也不要假装问过了 —— 而且必须说出来（见 `_report_skipped`）。
            try:
                is_dir = self._rfs.is_dir(p)
            except TimeoutError:
                undecided.append(name)
                continue
            if is_dir:
                skipped.append(name)
                continue
            dst = os.path.join(base, name)
            # 同上：这条按路径的下载入口原来也没有覆盖询问。本地存在性
            # 检查是零成本的（不用发网络），没有理由不做。
            if os.path.exists(dst):
                if msgbox.question(
                        self, "下载",
                        "「%s」在目标位置已存在。\n\n要覆盖它吗？" % name
                ) != QtWidgets.QMessageBox.StandardButton.Yes:
                    skipped.append(name + "（已存在，未覆盖）")
                    continue
            jobs.append((self._rfs.download, p, dst, "下载 " + name, name))
        if not jobs:
            self._report_skipped(skipped, "下载", undecided)
            return 0
        self._start_transfers(jobs, "download", "下载", self.local, base, skipped)
        if undecided and self._flash is not None:
            # 有东西真的在传就别弹模态框抢焦点（状态条正占着底部），
            # 一行状态栏提示足够把「这几个没传」说出来。
            self._flash("有 %d 个项目没能确认是不是目录（远程通道忙），本次未传输：%s"
                        % (len(undecided), "、".join(undecided)))
        return len(jobs)

    def _remote_exists(self, path: str) -> bool | None:
        """远端这个路径有没有。**返回 True / False / None（问不到）。**

        原来这里是 `except Exception: return False` —— 它把「远端确实没有」、
        「通道忙」、「权限不足」、「路径其实是个目录」四种情况**压成同一个
        答案**，而这个答案会被上传路径直接读成「可以安全覆盖」。于是一次
        探测失败就等于**静默覆盖用户的文件**，而状态条还照报「上传完成」。
        这正是「本次要消灭的东西」在下一条路径上又长了一遍。

        **只有 `FileNotFoundError` 才等于「没有」** —— 那是远端明确告诉你
        「这条路径不存在」。其余一律当问不到：**宁可多问一次，也不能替
        用户决定覆盖。** 调用方拿到 `None` 必须跳过并说出来。

        **依赖的假设要说清**：SFTP v3 规范要求「不存在」用 status 2 /
        ENOENT 表达，paramiko 的 `_convert_status` 映射成
        `IOError(errno.ENOENT, ...)`，在 Python 3 上就是 `FileNotFoundError`。
        所以**遵守规范的服务端都走这一条**。反过来说，一个用
        `SFTP_FAILURE`(status 4) 报「不存在」的服务端会让本函数一律返回
        `None` —— 那时**上传会全部被跳过并提示「没能确认」**，而不是静默覆盖。
        这是刻意选的失败方向：宁可少传，不可毁文件。

        这里也把「抢锁 + 这一次往返」纳入 `_probe` 的 0.25 秒上限：
        原来走的是无超时的 `stat()` 且在 GUI 线程上，通道一忙点上传就冻窗。
        """
        try:
            found = self._rfs._probe(
                lambda: self._rfs.stat(path), "远端同名探测")
        except FileNotFoundError:
            return False
        except Exception:                   # noqa: BLE001
            # TimeoutError 也在内：通道正忙时**正是最不能覆盖的时候**。
            return None
        return found is not None

    def _upload(self) -> None:
        entries = self.local.selected_entries()
        if not entries:
            msgbox.information(self, "上传", "请先在左侧选择要上传的文件。")
            return
        remote_dir = self.remote.current_path().rstrip("/")
        # 远端有没有同名文件：**直接读对面那栏的 model**，零网络往返。
        # 不走 `fetch_async()` —— 那是真发一次 listdir，而这条路径每
        # 文件多一次往返，慢的时候它本身就是冻窗的原因之一。
        _remote_names = set()
        for _r in range(self.remote.model.rowCount()):
            _it = self.remote.model.item(_r, 0)
            if _it is not None:
                _remote_names.add(_it.text())
        jobs, skipped = self._collect_jobs(
            entries, remote_dir, self._rfs.upload, "上传", "/",
            exists=lambda name, _dst: name in _remote_names)
        if not jobs:
            self._report_skipped(skipped, "上传")
            return
        self._start_transfers(jobs, "upload", "上传", self.remote, remote_dir, skipped)

    def _download(self) -> None:
        entries = self.remote.selected_entries()
        if not entries:
            msgbox.information(self, "下载", "请先在右侧选择要下载的文件。")
            return
        local_dir = self.local.current_path()
        jobs, skipped = self._collect_jobs(
            entries, local_dir, self._rfs.download, "下载", os.sep,
            exists=lambda _name, dst: os.path.exists(dst))
        if not jobs:
            self._report_skipped(skipped, "下载")
            return
        self._start_transfers(jobs, "download", "下载", self.local, local_dir, skipped)

    def _collect_jobs(self, entries, base_dir, fn, what: str, sep: str,
                      exists=None):
        """把选中的条目拆成 (fn, src, dst, 标题, 文件名) 任务表与被跳过的目录。

        ``sep`` 由调用方显式给出：远端是 POSIX 的 ``/``，本地才用 ``os.sep``。

        ``exists`` 是「这个目的地已经有同名文件了吗」的探测函数。不给就不查
        —— 但**两个方向都应该给**：直接覆盖同名文件**零确认**是静默毁数据，
        没有任何撤销，也不像删除那样会问一句。
        """
        jobs, skipped = [], []
        base = base_dir.rstrip("/\\")
        for e in entries:
            if e.is_dir:
                skipped.append(e.name)
                continue
            dst = base + sep + e.name
            if exists is not None and exists(e.name, dst):
                # 覆盖前逐个问，而不是「一次全传」或「一次都不传」。
                if msgbox.question(
                        self, what,
                        "「%s」在目标位置已存在。\n\n要覆盖它吗？" % e.name
                ) != QtWidgets.QMessageBox.StandardButton.Yes:
                    skipped.append(e.name + "（已存在，未覆盖）")
                    continue
            jobs.append((fn, e.path, dst, f"{what} {e.name}", e.name))
        return jobs, skipped

    def _report_skipped(self, skipped, what: str, undecided=()) -> None:
        """把没传的条目一次说清。**「问不到」必须和「确实跳过」分开写** ——
        两者在用户眼里是同一件事（旧代码把它们合并成一个假 `False`），
        但只有后者是「本来就不该传」。"""
        if not skipped and not undecided:
            return
        blocks = []
        if skipped:
            blocks.append("已跳过目录：\n" + "\n".join(skipped)
                          + "\n（暂不支持递归传输）")
        if undecided:
            blocks.append("没能确认是不是目录（远程通道忙），本次没传：\n"
                          + "\n".join(undecided))
        msgbox.information(self, what, "\n\n".join(blocks))

    def _start_transfers(self, jobs, direction: str, what: str,
                         refresh_panel: FilePanel, refresh_dir: str,
                         skipped) -> None:
        """一批传输 = 一个状态条 + 一个工作线程。

        刻意不做"每个文件一个线程"：那些线程会共用同一个 SFTPClient，
        互相把报文写坏（见 ``RemoteFileSystem`` 的说明）。串行跑同时也让
        进度条有确定的总进度。

        忙时闸门在控制器上，不在这里 —— 工具栏那条 ZMODEM 的路要跟这里共用
        同一个闸门，否则能同时点两次传输抢同一条通道。
        """
        note = ""
        if skipped:
            note = "已跳过目录：" + "、".join(skipped) + "（不支持递归）"
        transfer = SftpTransfer(jobs, self._rfs, self.ctrl,
                                refresh_panel._fs, refresh_dir, note,
                                on_done=self._on_transfer_done)
        self._refresh_panel = refresh_panel
        transfer.set_listed_callback(self._on_transfer_listed)
        if not self.ctrl.begin(direction, what, transfer, len(jobs), note):
            # **闸门拒绝时必须出声。** 原来这里直接 return：无提示、无日志，
            # 按钮照样可点。更糟的是同名覆盖确认**已经逐个问完了**才静默退出。
            # SFTP 自己占着通道时按钮会被 `_set_busy` 禁用，但那是 SFTP 自己
            # 的忙；**工具栏那条 ZMODEM 的路占着同一个闸门时，这两个按钮
            # 是敞开的**（busyChanged 没接到它们身上），于是点上传 ->
            # 覆盖确认问一圈 -> 什么都不发生。
            # 照抄 main_window 里那条 ZMODEM 路径的写法。
            self._flash("已有传输在进行，本次%s未开始" % what)
            return
        self._transfer = transfer
        self._set_busy(True)
        transfer.start()

    # -- 传输期回调（全部在 GUI 线程）----------------------------------------

    def _dead(self) -> bool:
        """本面板（或它要碰的控件）是不是已经被析构了。

        **必须真的问 C++，不能只看 Python 属性还在。** 面板是标签页的子控件，
        关掉标签时它是被父级**析构**掉的（收不到 `closeEvent`，也没有地方
        `wait()`），而工作线程还会在这之后把 `listed` / `done` 送回来。那两个
        槽要去动 `QLineEdit` / `QPushButton`，一动就抛 `RuntimeError`，而它
        发生在信号槽里 → `sys.excepthook` → 弹一个用户看不懂的「发生错误」框，
        且外观是「关掉标签之后程序莫名其妙报了个错」。判据见 `_fsftp.py:18`。
        """
        return sip.isdeleted(self)

    def _on_transfer_listed(self, path: str, entries) -> None:
        """传输后的目录刷新：网络调用在工作线程做，这里只填控件。"""
        panel = self._refresh_panel
        if panel is None or self._dead() or sip.isdeleted(panel):
            return
        if panel.current_path() == path:
            panel.populate_from(entries)

    def _on_transfer_done(self) -> None:
        if self._dead():
            return          # 按钮都没了，没什么可放开的
        self._transfer = None
        self._refresh_panel = None
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        for b in (self.up_btn, self.down_btn):
            b.setEnabled(not busy)
