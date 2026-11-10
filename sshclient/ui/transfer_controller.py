"""传输编排层：一条状态条、一个忙时闸门、一套收尾语义。

**为什么协议层不合并、编排层要合并。** SFTP 走 paramiko 的子系统通道，ZMODEM
走 shell 通道上的裸字节复用——这两件事在物理上没法合成一个实现（ZMODEM 传输
期间终端还是不可用的，因为它占着那同一条通道）。但它们**编排**的部分一模一样：
挑一批文件、报一个 (已传, 总量, 速度)、在成功/失败/取消里挑一种收尾、留一行
日志。

原来这两条路各写一套，代价是工具栏那条路没有进度、没有取消、没有忙时守卫——
于是可以在同一次会话里先点文件 tab 的上传、再点工具栏的上传，两个传输抢同一条
shell 通道，谁都传不完。现在两条路都从这里走。

**进度在这里归一成一个三元组** ``(done, total, bps)``。底下喂进来的是
paramiko 的回调还是 lrzsz 写在 stderr 上的进度行，状态条不关心——它是纯 UI，
不该知道协议。

三处刻意的选择：

**收尾只有三种，不多不少。** 成功 / 失败 / 取消。没传完就是没传完，被取消的
那一批报"上传完成"是撒谎；但取消是用户的正常选择，不该做成红色的失败。

**取消是"不再开始下一个"，不是"回滚当前这个"。** SFTP 没有中途放弃一个 put
的办法，ZMODEM 强行杀掉进程只会在远端留半个文件。所以取消只影响还没轮到的
文件，已经发出去的那个照常传完——真传完了就该报完成。

**忙时直接拒，不排队也不弹框。** 状态条就在那儿，弹一个框把它盖住等于
"我不知道刚才那一下点没点上"。
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from PyQt6 import QtCore, QtWidgets

from ..core.logs import get_logger
from .transfer_strip import TransferStrip, format_bytes, format_speed

log = get_logger("mavisssh.transfer")


# ---------------------------------------------------------------------------
# 进度折算
# ---------------------------------------------------------------------------


class TransferReport:
    """把一个文件的进度折算成整批的百分比，并拼出状态条那行小字。

    整批按**文件个数**等权，而不是按字节加权：一个 1KB 的文件和 1GB 的文件
    各占 1/n，这样"传到了第 3 个"和"传到了 37%"不会互相矛盾。字节数在 detail
    那行里照给。
    """

    def __init__(self, total_files: int) -> None:
        self.total_files = max(1, int(total_files))
        self.index = 0
        self.name = ""

    def set_file(self, index: int, name: str) -> None:
        self.index = max(0, int(index))
        self.name = name or ""

    def render(self, done: int, total: int, bps: float | None = None
               ) -> tuple[int | None, str]:
        """返回 ``(百分比, detail 文案)``。

        百分比为 ``None`` 表示**总量未知**——调用方应当显示不定态，而不是
        假装知道一个假百分比。lrzsz 在还没拿到文件大小时就是这样。
        """
        prefix = (f"{self.index + 1}/{self.total_files} · {self.name}"
                  if self.total_files > 1 else self.name)
        if total is None or total <= 0:
            detail = f"{prefix} · {format_bytes(done)}" if prefix else format_bytes(done)
            if bps:
                detail += f" · {format_speed(bps)}"
            return None, detail

        parts = [prefix] if prefix else []
        parts.append(f"{format_bytes(done)} / {format_bytes(total)}")
        if bps:
            parts.append(format_speed(bps))
        pct = int((self.index + min(1.0, done / total)) * 100 / self.total_files)
        return max(0, min(100, pct)), " · ".join(parts)


# ---------------------------------------------------------------------------
# 引擎协议
# ---------------------------------------------------------------------------


@runtime_checkable
class TransferAdapter(Protocol):
    """任何"能传一批文件"的东西都要会这两招，外加把结果报回来。

    实现方只需要保证：``start()`` 之后**恰好调用一次** `succeed` / `fail` /
    `cancel`（见 :class:`TransferController`）。
    """

    def start(self) -> None: ...

    def request_cancel(self) -> None: ...


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------


class TransferController(QtCore.QObject):
    """整个会话唯一的传输状态条与忙时闸门。"""

    # 引擎跑在自己的线程里（paramiko 回调、lrzsz 的 stderr 读线程），而控件
    # 只能在 GUI 线程碰。发一个信号给自己、槽落在 GUI 线程，是唯一干净的
    # 跨线程改法——直接调 strip 的方法会当场 fast-fail。
    # _file 同样走信号：lrzsz 那个读线程也会报"换文件了"，直接改 report
    # 就变成和 GUI 线程抢同一份数据。
    _file = QtCore.pyqtSignal(int, str)
    # 第三个参数是"百分比是否有效"：pct 为 None 时代表总量未知。
    _progress = QtCore.pyqtSignal(object, str)
    #: 忙时状态翻转了。片段面板靠它决定能不能往通道里写东西。
    #:
    #: **只在真的翻转时发。** ``begin()`` 被闸门拒掉时不发、``_finish()`` 撞上
    #: "没有在传的批次"提前 return 时不发、``reset()`` 在本来就空闲时也不发 ——
    #: 否则面板会被无意义地禁用又启用，而调用方还得自己去比对上一个值。
    busyChanged = QtCore.pyqtSignal(bool)

    def __init__(self, palette=None, parent=None,
                 strip: TransferStrip | None = None) -> None:
        super().__init__(parent)
        # parent 可能只是个 QObject（SessionTab 自己是 QWidget，但独立测试里
        # 常常传 None），而 QFrame 只认 QWidget —— 传错类型是 TypeError。
        strip_parent = parent if isinstance(parent, QtWidgets.QWidget) else None
        self.strip = strip if strip is not None else TransferStrip(palette, strip_parent)
        self.strip.cancelRequested.connect(self.request_cancel)

        self._adapter: TransferAdapter | None = None
        self._report: TransferReport | None = None
        self._what = "传输"
        self._direction = "upload"
        self._t0 = time.monotonic()

        self._file.connect(self._on_file)
        self._progress.connect(self._on_progress)

    # ------------------------------------------------------------------ 状态

    @property
    def busy(self) -> bool:
        """现在有传输在跑吗。"""
        return self._adapter is not None

    def apply_palette(self, p) -> None:
        self.strip.apply_palette(p)

    # ------------------------------------------------------------------ 发起

    def begin(self, direction: str, what: str, adapter: TransferAdapter,
              total_files: int, note: str = "") -> bool:
        """登记一次新传输。**已经在传就返回 False**，调用方应当什么都不做。

        返回 False 而不是排队：状态条就明晃晃停在"正在上传"，用户看得见。
        """
        if not isinstance(adapter, TransferAdapter):
            # 真的校验一次：Protocol 写成装饰性文档，引擎接错方法名时要在这里
            # 就报出来，而不是等到传输结束才发现状态条永远不动。
            missing = [m for m in ("start", "request_cancel")
                       if not callable(getattr(adapter, m, None))]
            raise TypeError("传输引擎 %s 缺少方法：%s"
                            % (type(adapter).__name__, missing))
        if self._adapter is not None:
            log.info("忽略一次%s请求：上一批还在传", what)
            return False
        self._adapter = adapter
        self._what = what
        self._direction = direction
        self._t0 = time.monotonic()
        self._report = TransferReport(total_files)
        if not total_files:
            self._report.set_file(0, "")

        title = f"正在{what}"
        if total_files > 1:
            title += f" {total_files} 个文件"
        self.strip.begin(direction, title, note)
        log.info("开始%s：%d 个文件  引擎=%s", what, total_files,
                 type(adapter).__name__)
        self.busyChanged.emit(True)
        return True

    # ------------------------------------------------------------------ 进度

    def file_started(self, index: int, name: str) -> None:
        """换文件了。**可以从任意线程调。**"""
        # 同 advanced：取局部引用，别把 self._report 读两次。
        if self._report is not None:
            self._report.set_file(index, name)
        self._file.emit(index, name)

    def advanced(self, done: int, total: int, bps: float | None = None) -> None:
        """报一次进度。**可以从任意线程调。**"""
        # 取局部引用再取属性：self._report 随时可能被 GUI 线程的 _finish()
        # 置成 None，分成两次读就夹在中间了。评审压测 3000 轮没能自然复现，
        # 但这是一次读换一次写的事，代价为零。
        rep = self._report
        if rep is None:
            return
        pct, detail = rep.render(done, total, bps)
        self._progress.emit(pct, detail)

    # ------------------------------------------------------------------ 收尾

    def succeed(self, note: str = "") -> None:
        self._finish(self.strip.succeed, f"{self._what}完成", note)

    def fail(self, message: str) -> None:
        head, _, rest = message.partition("\n")
        self._finish(self.strip.fail, head, rest, tip=message)

    def start_engine(self, adapter: TransferAdapter) -> bool:
        """在 ``begin()`` 之后启动引擎，并把启动失败收成一次正常的失败态。

        **不要直接 ``adapter.start()``。** ``resolve_binary`` 只查 ``is_file()``，
        一个损坏或没有执行权限的二进制能过检，于是 ``subprocess.Popen`` 抛
        ``OSError``；而 ``begin()`` 已经把 ``_adapter`` 登记上了，直接抛出去
        的话闸门永远不释放、状态条永远停在 running。而且这通常发生在
        ``QSocketNotifier.activated`` 驱动的 GUI 线程槽里 —— PyQt6 里槽抛
        异常是整个进程 ``0xC0000409``，不是打一行日志。
        """
        try:
            adapter.start()
            return True
        except Exception as exc:
            log.warning("传输引擎启动失败：%s: %s", type(exc).__name__, exc)
            self.fail(f"传输启动失败：{type(exc).__name__}: {exc}")
            return False

    def cancel(self, note: str = "") -> None:
        self._finish(self.strip.cancel, f"{self._what}已取消", note)

    def report_problem(self, title: str, detail: str = "") -> None:
        """连传输都没能开始就没的问题（找不到本地 lrzsz 二进制之类的）。

        同样落在状态条上、不弹模态框：理由和 :meth:`fail` 一样——模态框会抢
        焦点，而一次没人看见的报错等于没报。

        **已经在传的时候不许踩。** 本方法绕开 :meth:`_finish`、也不会清
        ``_adapter``，所以若无脑盖上去，那条正在传的 SFTP/ZMODEM 结束时会被
        "第一个终态说了算" 的守卫吞掉它自己的收尾——用户的进度条停在别人的
        错误上，而且永远等不到自己那次的成功。这种窄窗口真会出现（比如连接
        在传输途中断掉，同时另一条路报找不到二进制）。
        """
        log.warning("%s：%s", title, detail.replace("\n", " | ") if detail else "")
        if self._adapter is not None:
            log.warning("已有传输在进行，这条问题只记日志，不覆盖状态条：%s", title)
            return
        self.strip.fail(title, detail)
        self.strip.setToolTip(detail)

    def _finish(self, apply, title: str, detail: str, tip: str = "") -> None:
        # **第一个终态说了算。** 引擎失败时会先发 failed、再发 done；而 done
        # 那个槽里有"没出错就报完成"的分支，不挡一下就会把红色失败态覆盖成
        # 绿色成功态 —— 那正好是这条路径最初要防的那件事。
        if self._adapter is None:
            log.warning("忽略一次多余的收尾：%s（已经没有在传的批次了）", title)
            return
        log.info("%s：用时 %.0f ms", title, (time.monotonic() - self._t0) * 1000)
        if tip:
            self.strip.setToolTip(tip)
        apply(title, detail)
        self._adapter = None
        self._report = None
        self.busyChanged.emit(False)

    # ------------------------------------------------------------------ 取消

    def request_cancel(self) -> None:
        """状态条上的"取消"。

        只通知引擎"别再开始下一个"，**不**去打断已经发出去的那一个：
        SFTP 没有中途放弃一个 put 的办法，强杀 lrzsz 只会在远端留半个文件。
        真到不了下一个文件时，引擎报上来的就是"取消"而不是"完成"。
        """
        adapter = self._adapter
        if adapter is None:
            return
        log.info("收到取消请求")
        try:
            adapter.request_cancel()
        except Exception as exc:
            # 引擎已经没了之类。这里吞掉是安全的：取消失败不该再抛给用户
            # 制造第二次错误，那条状态条马上会被真正的收尾覆盖掉。
            log.warning("引擎处理取消请求时出错：%s: %s", type(exc).__name__, exc)

    def reset(self, why: str = "") -> None:
        """连接断了：把闸门放掉，状态条退回空闲。

        不放的后果是"同一条连接里之后再也不能传了"：`_adapter` 悬着，文件 tab
        的上传和工具栏的上传都只走个空判断就返回，而状态条还停在 running 上，
        看上去像还在传。
        """
        if self._adapter is not None:
            log.warning("连接断开时还有传输在飞（%s），强制释放闸门", why or "?")
            self._adapter = None
            self.busyChanged.emit(False)
        self._report = None
        self.strip.hide_and_notify()

    # ------------------------------------------------------------------ 内部

    def _on_file(self, index: int, name: str) -> None:
        if self._report is not None:
            self._report.set_file(index, name)

    def _on_progress(self, pct: int | None, detail: str) -> None:
        self.strip.advance(pct, detail)
