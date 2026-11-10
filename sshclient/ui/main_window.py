"""主窗口：左侧会话边栏 + 右侧标签工作区。

`SessionTab` 和 `MainWindow` 都留在这个文件里——它们互相持有（窗口建标签、
标签回调窗口），拆到两个文件只会把同一份耦合搬成一条 import 边，读代码时
反而要来回跳。真正独立的侧边栏在 `sidebar.py`，共用控件工厂在 `controls.py`。
"""
from __future__ import annotations

import os
import posixpath
import re
import threading
from typing import TYPE_CHECKING

from PyQt6 import QtCore, QtGui, QtWidgets

if TYPE_CHECKING:  # 只用于类型标注，运行时不需要 paramiko
    import paramiko

from .. import __version__  # 版本号的唯一真相在包上，别再写第二份
from ..core import importer, lrzsz
from ..core.fonts import mono_font, ui_font
from ..core.logs import get_logger
from ..core.settings import (
    SETTINGS_FILE,
    AppSettings,
    SavedSession,
    SessionStore,
    accent_for,
)
from ..core.snippets import SnippetStore
from ..core.tunnel import TunnelError, TunnelSpec
from ..remote import hostkeys
from ..remote.connection import AuthConfig, SSHConnection
from . import hostkey_dialog, icons, logo, msgbox, thread_post
from .connect_dialog import ConnectDialog
from .controls import (
    _icon_button,
    _label,
    _retheme_icon_action,
    _retheme_icon_button,
    _set_icon,
)
from .design.v1 import qss
from .design.v1.palette import Palette, get_palette
from .design.v1.qss import build_qss
from .monitor_panel import MonitorPanel
from .settings_dialog import open_settings
from .sftp_panel import SftpPanel
from .sidebar import (
    GROUP_H,
    ROW_H,
    ROW_H_COMPACT,
    SIDEBAR_W,
    SIDEBAR_W_COLLAPSED,
    CompactSessionItem,
    GroupHeaderItem,
    SessionItem,
    WelcomePage,
    _pin,
    _release,
    _side_button,
    _unpin_width,
    grouped_sessions,
)
from .snippet_panel import SnippetPanel
from .tab_close import NO_CLOSE, TabCloseController
from .tab_context import TabContextMenu
from .tab_scroll import install_tab_scroll_buttons
from .terminal_widget import TerminalWidget
from .titlebar import TitleBar, frameless_enabled, native_event
from .transfer_controller import TransferController
from .tunnel_panel import TunnelPanel
from .window_frame import enable_rounded_corners

log = get_logger("mavisssh.session")

#: 顶层窗口用这个 Qt 属性挂着自己的 `Palette`，`msgbox` 优先从父窗口读它，
#: 而不是读 `qss` 那个模块级全局 —— 同进程里两个主题不同的窗口会互相覆盖。
_PALETTE_PROP = "mavisPalette"

#: 「首次连接这台主机，要不要信？」等用户回答的上限（秒）。
#:
#: **必须有上限。** 用户不点就一直挂着的话，工作线程永久阻塞 ——
#: 连接卡死 + 线程泄漏，而且界面上什么提示都没有（因为它就等着那个弹窗）。
#: 超时按「拒绝」处理：宁可连不上，也不要在没人应答的情况下替他信任。
_HOST_KEY_ASK_TIMEOUT = 120.0


class _HostKeyAsk:
    """一次「主机密钥确认」的跨线程传话板。

    工作线程创建它、把它投到主线程、然后 ``event.wait()``；
    主线程弹框、填 ``accept``、``event.set()``。
    """

    __slots__ = ("req", "event", "accept", "once")

    def __init__(self, req) -> None:
        self.req = req
        self.event = threading.Event()
        self.accept = False
        #: True = 「仅此一次」（这次信任，但不写进 known_hosts）
        self.once = False

_LRZSZ_HINT = (
    "未能定位本地 lrzsz 二进制。\n\n"
    "Windows：仓库的 bin/ 里已附带；\n"
    "macOS / Linux：请先安装 lrzsz（brew install lrzsz / "
    "apt install lrzsz），它会落在 PATH 上。\n"
    "详见 README 的 lrzsz 配置。"
)

# download_dir() 三档全不可写时给回 None（不再兜底一条已知写不进去的路径），
# 而"下载还没开始"和"下载失败"在用户眼里是两件事，提示得说清楚是哪一件。
_DL_DIR_HINT = (
    "lrzsz 找不到能落盘的本地目录，三档候选都不可写。\n"
    "磁盘满、目录只读、或启动目录就是只读挂载都会这样；\n"
    "腾出空间或换个目录后重试，也可以先清空下载路径再试。"
)

# 认「用户刚在终端里敲的是 rz 还是 sz」时用的三样常量。
# lrzsz 会同时装 lrz/lsz 这两个名字（指向同一个程序的软链），一起认掉，
# 否则用户敲了 lsz 却掉进「认不出 → 弹框问」，那正是这次要消掉的东西。
_ZMODEM_RZ_NAMES = ("rz", "lrz")
_ZMODEM_SZ_NAMES = ("sz", "lsz")

#: 从光标往上最多看多少行去找那条命令。lrzsz 的等待提示和帧头最多占两三行，
#: 8 行足够跨过去，又不至于把更早的、已经被顶出上下文的命令捞回来。
_ZMODEM_HINT_LOOKBACK = 8


# ---------------------------------------------------------------------------
# ZMODEM 引擎适配器
# ---------------------------------------------------------------------------


_APP_SNIPPET_STORE: SnippetStore | None = None


def app_snippet_store() -> SnippetStore:
    """整个进程一份的片段库。

    **不是每个 MainWindow 一份。** 同一时刻开两个窗口时，w1 刚加的片段会被
    w2 的下一次 ``add()`` 静默覆盖（磁盘上只剩 w2 那份，而 w1 的内存里还
    以为自己有）。用户看到的就是"我明明加了，怎么又没了"。单窗口是常态，
    但这条不该只在常态下成立。
    """
    global _APP_SNIPPET_STORE
    if _APP_SNIPPET_STORE is None:
        _APP_SNIPPET_STORE = SnippetStore()
        if not _APP_SNIPPET_STORE.loaded_ok:
            log.warning("片段库没能从磁盘读出来，先用内置的：%s",
                        _APP_SNIPPET_STORE.path)
    return _APP_SNIPPET_STORE


class ZmodemTransfer(QtCore.QObject):
    """把 :class:`lrzsz.ZmodemPump` 包装成传输适配器。

    进度不用另写一套：lrzsz 把人读的输出写在 **stderr**（ZMODEM 协议走
    stdout），而那些行本来就是 ``已传/总量/速度``，喂给同一个控制器即可。

    **取消在这里是真的会打断当前文件的**，和 SFTP 那条路不一样，原因在协议：
    一批文件是同一个 ``sz`` 进程连着送的，没有"只停下一个"的开关。所以
    ``request_cancel`` 只能终止进程，代价是远端可能留半个文件——因此收尾的
    文案必须说清楚，不能报"完成"。

    **必须继承 QObject。** 收尾是从 pump 的等待线程回来的（daemon
    ``threading.Thread``，不是 QThread），而控件只能在 GUI 线程碰。第一版这里
    是普通类，用 ``QMetaObject.invokeMethod(self, ...)`` 想跳回去 —— PyQt6 只
    接受 ``QObject | None``，于是**每次传输结束时都抛 TypeError**：状态条永远
    停在 running、`_adapter` 不清空、忙时闸门整场锁死、之后所有上传下载静默
    空转。所以这里用信号：跨线程自动排队。
    """

    #: pump 的等待线程在跑，用它把收尾搬回 GUI 线程。
    _settle = QtCore.pyqtSignal(int)

    def __init__(self, ctrl: TransferController, channel, direction: str,
                 binary: str, files: list[str], cwd: str,
                 on_log=None, note: str = "", targets: list[str] | None = None,
                 on_finished=None, parent=None):
        super().__init__(parent)
        self._ctrl = ctrl
        self._targets = list(targets or files)
        self._note = note
        self._on_finished = on_finished
        self._cancelled = False
        self._settled = False
        self.pump = lrzsz.ZmodemPump(
            channel=channel,
            direction=direction,
            binary=binary,
            files=files,
            cwd=cwd,
            on_log=on_log,
            on_finished=self._on_proc_exit,
            on_file=ctrl.file_started,
            on_progress=ctrl.advanced,
        )
        self._settle.connect(self._do_settle)

    # -- 适配器接口 -------------------------------------------------------
    def start(self) -> None:
        self.pump.start()

    def request_cancel(self) -> None:
        self._cancelled = True
        self.pump.stop()

    # -- 内部 -------------------------------------------------------------
    def _on_proc_exit(self, code: int) -> None:
        """跑在 pump 的等待线程（普通 daemon 线程）里。"""
        self._settle.emit(int(code))

    @QtCore.pyqtSlot(int)
    def _do_settle(self, code: int) -> None:
        """落回 GUI 线程的真正收尾。"""
        if self._settled:
            return
        self._settled = True
        if self._on_finished is not None:
            self._on_finished(code)
        if self._cancelled:
            self._ctrl.cancel(self._note or "已停止，当前文件可能不完整")
        elif code == 0:
            names = "、".join(os.path.basename(t) for t in self._targets[:3])
            if len(self._targets) > 3:
                names += "…"
            self._ctrl.succeed(self._note or (f"已接收 {names}" if names else ""))
        else:
            what = "上传" if self.pump.direction == "upload" else "下载"
            self._ctrl.fail(f"{what}失败（lrzsz 退出码 {code}）")


# ---------------------------------------------------------------------------
# One connected session: terminal + sftp + monitor
# ---------------------------------------------------------------------------


class SessionTab(QtWidgets.QWidget):
    """A single SSH connection presented as a tab with three sub-views."""

    statusChanged = QtCore.pyqtSignal(str, bool)  # message, is_busy
    # Emitted on every idle/busy/live/error transition, so the sidebar rail
    # can re-mark which squares are actually connected.
    stateChanged = QtCore.pyqtSignal()

    def __init__(self, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._conn: SSHConnection | None = None
        self._settings: AppSettings | None = None
        self._channel: paramiko.Channel | None = None
        self._notifier = None
        self._zmodem_pump: lrzsz.ZmodemPump | None = None
        # 工具栏「下载目录」按钮：用户选中则优先用，未选则 _start_zmodem_download
        # 走 lrzsz.download_dir() 的系统默认三档。
        self._dl_target_dir: str | None = None
        # 走 property：待办方向一变就刷新片段面板的可用状态。挨个赋值点
        # 补一句通知是典型的"漏一处就静默失效"——上面有 11 处赋值。
        self._zmodem_pending: str | None = None
        self._snippet_panel: SnippetPanel | None = None
        self._zmodem_detector = lrzsz.ZmodemDetector()
        # 远端 lrzsz 探测的缓存，None = 还没问过。挂在标签上、重连时清空：
        # 换了台机器就得重问，沿用上一次的结论就正好把"没装"说成"装了"。
        self._lrzsz_probe: lrzsz.RemoteProbe | None = None
        self._completion_timer: QtCore.QTimer | None = None
        self._pty_resize_timer: QtCore.QTimer | None = None
        self._pending_pty_size: tuple[int, int] = (80, 24)
        # None = 还没发过。不在 __init__ 里定死成某个尺寸：重连后拿到的是
        # **新**通道（PTY 开在 100x30），若沿用上一次的记录，尺寸恰好相同的
        # 那一次就会被当成"没变"而跳过，新会话的远端就停在错误尺寸上。
        self._sent_pty_size: tuple[int, int] | None = None
        self._pending_zmodem_files: list[str] = []
        # sz 只吃 basename + cwd，绝对路径是静默失效（见 lrzsz.plan_local_upload）
        self._pending_zmodem_cwd: str = ""
        self._pending_zmodem_skipped: list[str] = []
        self._sz_targets: list[str] = []
        self._last_saved: SavedSession | None = None
        # **标签标题**：分组名-会话名（`_on_connect_ok` 写）。
        self._last_label: str = ""
        # **连到了哪台机器**：`用户名@主机:端口`（同上）。
        #
        # **为什么和 `_last_label` 分成两个。** 标签改成「分组名-会话名」之后，
        # 两者回答的是两个不同的问题：标签回答「哪一个」，pill 回答「在哪里」。
        # 共用一个字段的话，状态栏那个 pill 也会跟着只剩一个会话名 —— 它上面
        # 有 `_host_pill` 这个名字、有「断开时必须清掉」的整套逻辑，职责一直是
        # 显示主机；两组同名会话在 pill 上还会撞在一起。
        self._last_endpoint: str = ""
        self._busy = False
        # 远端 locale 只问一次（_remote_is_utf8）
        self._remote_utf8: bool | None = None
        self._palette = get_palette("dark")

        # ---- sub-tabs (created first so button handlers can bind) ----
        self.sub_tabs = QtWidgets.QTabWidget()
        self.terminal = TerminalWidget()
        self.sub_tabs.addTab(self.terminal, "终端")
        self._sftp: SftpPanel | None = None
        self._monitor: MonitorPanel | None = None
        self._tunnels: TunnelPanel | None = None

        # ---- connection state pill ----
        self.state_dot = QtWidgets.QLabel()
        self.state_dot.setFixedSize(8, 8)
        self.state_label = _label("未连接", "stateText")

        # ---- toolbar ----
        # 「重新连接」用插头而不是环形箭头：环形箭头说的是"刷新数据"，
        # 插头说的是"把连接插回去"，而它旁边的 power 正好是反义。
        self.reconnect_btn = _icon_button("plug", "重新连接", self._palette)
        self.reconnect_btn.clicked.connect(self._on_reconnect)
        self.disconnect_btn = _icon_button("power", "断开连接", self._palette, 20)
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        self.upload_btn = _icon_button("upload", "上传文件到远程 (rz)", self._palette)
        self.upload_btn.clicked.connect(self._on_upload)
        self.download_btn = _icon_button("download", "从远程下载文件 (sz)", self._palette)
        self.download_btn.clicked.connect(self._on_download)
        self.sz_target_edit = QtWidgets.QLineEdit()
        self.sz_target_edit.setPlaceholderText("远程文件路径，回车后按 ↓ 下载")
        self.sz_target_edit.setFixedWidth(230)
        self.sz_target_edit.returnPressed.connect(self._on_download)
        # The shortcuts are handled in TerminalWidget.keyPressEvent rather than
        # as QAction shortcuts: those would fire whatever has focus, and
        # Ctrl+C/Ctrl+V have to keep working normally in the toolbar's own
        # inputs. The tooltips carry them so they stay discoverable.
        self.copy_btn = _icon_button("copy", "复制所选文本 (Ctrl+Shift+C)",
                                     self._palette)
        self.copy_btn.clicked.connect(self.terminal.copy_selection)
        self.paste_btn = _icon_button("paste", "粘贴 (Ctrl+V)", self._palette)
        self.paste_btn.clicked.connect(self.terminal.paste_clipboard)
        self.clear_btn = _icon_button("eraser", "清屏", self._palette)
        self.clear_btn.clicked.connect(self.terminal.reset_terminal)
        # 提示符 + 光标：一条还没执行的命令。这个按钮**只**把片段写进远端
        # 的行缓冲，不回车。
        self.snippet_btn = _icon_button(
            "snippet", "命令片段（插入到当前命令行，不回车）", self._palette)
        self.snippet_btn.clicked.connect(self._toggle_snippets)

        toolbar = QtWidgets.QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 8)
        toolbar.setSpacing(6)
        toolbar.addWidget(self.state_dot)
        toolbar.addWidget(self.state_label)
        toolbar.addSpacing(10)
        # **原来这里是 `toolbar.addWidget(_sep())`（一条竖线），已改成纯间距。**
        # 那条线不吃 border token，被 `QWidget{color}` 兜成 text 色、
        # 实测 4.52:1，是全界面最亮的一条线，而两侧同色、零信息量。
        # 分组靠留白读，不靠画线。
        toolbar.addSpacing(10)
        toolbar.addWidget(self.reconnect_btn)
        toolbar.addWidget(self.disconnect_btn)
        toolbar.addSpacing(14)
        toolbar.addWidget(self.upload_btn)
        toolbar.addWidget(self.download_btn)
        toolbar.addWidget(self.sz_target_edit)
        toolbar.addStretch(1)
        toolbar.addWidget(self.copy_btn)
        toolbar.addWidget(self.paste_btn)
        toolbar.addWidget(self.clear_btn)
        toolbar.addSpacing(14)
        toolbar.addWidget(self.snippet_btn)

        # 整个会话只有这一条传输状态条、这一个忙时闸门：SFTP 上传下载、ZMODEM
        # 的 rz/sz 都往它上面报。放在 SessionTab 而不是各自面板里，是因为
        # ZMODEM 跑在「终端」这一页——条子要是关在文件面板里，用户根本看不到。
        # 共用同一个控制器还顺带堵掉一个洞：不然能在同一次会话里先点文件 tab
        # 的上传再点工具栏的上传，两个传输抢同一条 shell 通道，谁都传不完。
        self.transfer_ctrl = TransferController(self._palette, self)
        self.transfer_strip = self.transfer_ctrl.strip
        # 传输一开始就锁终端输入，结束就开。**必须接信号**：闸门有
        # begin/_finish/reset 三条收尾路径，手工在每处补一句开锁，漏一处
        # 就是「这次会话的终端从此哑掉」。
        self.transfer_ctrl.busyChanged.connect(
            lambda _b: self._sync_snippet_gate())

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(toolbar)
        outer.addWidget(self.sub_tabs, 1)

        strip_row = QtWidgets.QHBoxLayout()
        strip_row.setContentsMargins(12, 0, 12, 10)
        strip_row.addWidget(self.transfer_strip)
        outer.addLayout(strip_row)

        self._toolbar_icon_buttons = (
            self.reconnect_btn, self.disconnect_btn, self.upload_btn,
            self.download_btn, self.copy_btn, self.paste_btn, self.clear_btn,
            self.snippet_btn)

        self._set_connected(False)
        self._set_state("idle")

    # ------------------------------------------------------------------ theme

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        # 图标名由按钮自己记住（见 controls._icon_button），这里只重画。
        # 原先这里是手抄的一份名字表，抄漏一处，切主题（Ctrl+T）就会把那个
        # 按钮悄悄打回旧图标。
        for btn in self._toolbar_icon_buttons:
            _retheme_icon_button(btn, palette)
        self.terminal.apply_palette(palette)
        if self._sftp is not None:
            self._sftp.apply_palette(palette)
        if self._monitor is not None:
            self._monitor.apply_palette(palette)
        if self._tunnels is not None:
            self._tunnels.apply_palette(palette)
        if self._snippet_panel is not None:
            self._snippet_panel.apply_palette(palette)
        self._refresh_state_style()

    def apply_settings(self, settings: AppSettings | None) -> None:
        """Push the terminal-side settings onto this tab.

        Separate from :meth:`apply_palette` because a palette change must not
        re-run the font metrics on every open tab. *settings* may be None
        while the tab is being built before the window has one.
        """
        if settings is None:
            return
        self.terminal.set_font(
            mono_font(settings.term_font_size, settings.term_font_family))
        self.terminal.set_scrollback(settings.scrollback)
        # 片段面板是**缓存复用**的（`if self._snippet_panel is None` 才新建），
        # 所以改完设置要主动再喂一次预览字体，否则复用旧面板时字族不更新。
        if self._snippet_panel is not None:
            self._snippet_panel.set_preview_font(
                settings.term_font_family, settings.ui_font_size)
        if self._monitor is not None:
            self._monitor.set_interval_ms(settings.monitor_interval_ms)

    # ------------------------------------------------------------- 命令片段

    @property
    def _zmodem_pending_direction(self) -> str | None:
        """远端 rz/sz 已在等、本地引擎还没起。**这期间通道已经归远端了。**"""
        return self._zmodem_pending

    @_zmodem_pending_direction.setter
    def _zmodem_pending_direction(self, value: str | None) -> None:
        if value == self._zmodem_pending:
            return
        self._zmodem_pending = value
        self._sync_snippet_gate()

    @property
    def snippet_store(self):
        """本标签用的片段库，**可能为 None**。

        从窗口上取，和 :meth:`attach` 里 ``self._settings`` 同一个路子。**不要
        在这里兜一个 ``SnippetStore()``** ：那是 app 级的单例，在无 parent 的
        ``SessionTab()``（测试里到处都是）上兜底会造出第二份，两边各写各的
        文件，用户的片段看起来"存了又没了"。取不到就是取不到。

        ``window() is self`` 那个判断不能省：没有窗口祖先时 Qt 的
        ``window()`` 返回自己，于是 ``getattr(self, "snippet_store")`` 又回到
        这个 property，**无限递归直接 RecursionError**。测试里到处是
        ``SessionTab()`` 这种无 parent 的构造，踩到是迟早的事。
        """
        win = self.window()
        if win is self:
            return None
        return getattr(win, "snippet_store", None)

    def _toggle_snippets(self) -> None:
        """叫出/收起片段面板。"""
        if self._snippet_panel is not None and self._snippet_panel.isVisible():
            self._snippet_panel.close()
            return
        store = self.snippet_store
        if store is None:
            self._flash_status("片段库不可用")
            return
        if self._snippet_panel is None:
            self._snippet_panel = SnippetPanel(
                store, self._insert_snippet, ctrl=self.transfer_ctrl,
                block_reason=self._channel_block_reason, parent=self)
            self._snippet_panel.apply_palette(self._palette)
            self._snippet_panel.destroyed.connect(self._on_snippet_panel_gone)
        # show() 而非 exec()：面板带 WA_ShowWithoutActivating，焦点**留在终端
        # 上**——用户正要敲命令，别把它夺走。
        self._snippet_panel.show()
        self._snippet_panel.raise_()

    def _on_snippet_panel_gone(self, *_a) -> None:
        self._snippet_panel = None

    def _channel_block_reason(self) -> str:
        """这条 shell 通道现在归谁所有。**返回空串 = 归用户，可写可插。**

        一个判据**三处共用**：片段面板能不能插、终端能不能收按键、传输能不能
        开始。它们是同一个物理约束——通道同时只能属于一边。

        判据比 :attr:`TransferController.busy` **宽**，这是刻意的。
        忙时闸门是在 ``begin()`` 那一刻才锁的，而下载方向上通道更早就归了
        远端：按下下载之后 ``sz`` 命令已经发出，本地 ``rz`` 还要等 500ms
        才起（见 :meth:`_begin_zmodem_download`）。那半秒里 ``busy`` 还是
        False，按钮是使能的、按键照收，插进去的字节被 ``sz``/``rz`` 当成
        传输载荷吃掉。实测过：设上 ``_zmodem_pending_direction`` 就能复现。
        上传方向更糟 —— 等 ZRQINIT 的窗口**没有上限**，远端 rz 可以一直等。

        ``_zmodem_pending_direction`` 做成 property 就是为了这个判据只有
        一份真相：它有 11 处赋值点，挨个补一句刷新通知是典型的"漏一处就
        静默失效"。
        """
        if self.transfer_ctrl.busy:
            return "传输进行中：通道被占着，插进去会被当成传输数据"
        if self._zmodem_pump is not None:
            return "ZMODEM 传输进行中：通道被占着"
        if self._zmodem_pending_direction is not None:
            return "远端 rz/sz 正在等待：通道已经被占用了"
        return ""

    def _sync_snippet_gate(self) -> None:
        """通道归属变了：刷新面板可用状态**和终端输入闸门**。

        两件事共用同一个判据，是因为它们是同一个物理约束的两面 —— 通道
        归传输所有时，面板不能插命令、终端也不能收按键。
        """
        blocked = bool(self._channel_block_reason())
        if self._snippet_panel is not None:
            self._snippet_panel._sync_enabled()
        if getattr(self, "terminal", None) is not None:
            self.terminal.set_input_blocked(blocked)

    def _insert_snippet(self, data: bytes) -> bool:
        """把片段送进远端行缓冲。**不补回车。**

        返回 False 表示没送出去（通道被占 / 通道没了），面板据此给一句提示
        —— :meth:`TerminalWidget._send` 自己 ``except: pass`` 静默吞掉发送
        失败，界面上的表现就是"点了没反应"，那比报错更难查。
        """
        if self._channel_block_reason():
            return False
        ch = self._channel
        if ch is None or ch.closed:
            return False
        return bool(self.terminal._send(data))

    # ------------------------------------------------------------------ connect

    def attach(self, conn: SSHConnection) -> None:
        self._conn = conn
        # 新通道 = 新的传输上下文。上一条连接留下的待办必须清干净，否则新会话
        # 的第一批字节就会被当成一次传输的开头（见 _reset_zmodem_state）。
        self._reset_zmodem_state()
        self._set_state("busy")
        win = self.window()
        self._settings = getattr(win, "settings", None)
        try:
            ch = conn.open_shell(term="xterm-256color", cols=100, rows=30)
        except Exception as exc:
            self._set_state("error", str(exc))
            msgbox.critical(self, "打开终端失败", str(exc))
            # **必须把这条连接关掉。** TCP 已经建好，不关就是一个谁也不管的
            # SSHConnection 挂在那儿：远端以为有人在登录、本地占着一个 fd，
            # 而界面上只剩一个点不动的标签，用户没有别的办法把它收掉。
            try:
                conn.close()
            except Exception:
                log.warning("open_shell 失败后关连接又抛了", exc_info=True)
            self._conn = None
            return
        self._channel = ch
        # 新通道的 PTY 开在 open_shell 的写死尺寸上，之前的"已发送"记录对它无效；
        # "已经告诉过用户同步失败"同理 —— 新会话的第一次失败必须自己说一遍。
        self._sent_pty_size = None
        self._pty_sync_told = False

        try:
            notifier = QtCore.QSocketNotifier(
                ch.fileno(), QtCore.QSocketNotifier.Type.Read, self
            )
            notifier.activated.connect(self._on_channel_ready)
            notifier.setEnabled(True)
            self._notifier = notifier
        except Exception:
            self._notifier = None

        # This tab owns the QSocketNotifier for *ch* (it has to sniff the stream
        # for zmodem before the terminal sees it), so the terminal must not
        # register a second one on the same fd.
        self.terminal.bind_channel(ch, on_close=self._on_channel_closed,
                                   manage_notifier=False)
        self.terminal.set_tab_handler(self._on_tab)
        self.terminal.set_resize_handler(self._on_terminal_resize)
        self.terminal.apply_palette(self._palette)
        self.terminal.setFocus()

        try:
            # 注入会话级控制器：文件 tab 的 SFTP 和工具栏的 ZMODEM 共用同一条
            # 状态条、同一个忙时闸门。
            self._sftp = SftpPanel(conn.open_sftp(), self._palette, self,
                                   ctrl=self.transfer_ctrl,
                                   flash=self._flash_status)
            self._sftp.apply_palette(self._palette)
            self.sub_tabs.addTab(self._sftp, "文件")
        except Exception as exc:
            self._sftp = None
            msgbox.warning(self, "SFTP 不可用", str(exc))

        try:
            self._monitor = MonitorPanel(
                conn,
                interval_ms=(self._settings.monitor_interval_ms
                             if self._settings else 2000),
                parent=self)
            self._monitor.apply_palette(self._palette)
            self.sub_tabs.addTab(self._monitor, "监控")
        except Exception as exc:
            self._monitor = None
            msgbox.warning(self, "监控不可用", str(exc))

        try:
            self._tunnels = TunnelPanel(conn, self._palette, self)
            self._tunnels._flash = self._flash_status
            self._tunnels.apply_palette(self._palette)
            self._tunnels.rulesChanged.connect(self._persist_tunnels)
            self.sub_tabs.addTab(self._tunnels, "隧道")
        except Exception as exc:
            self._tunnels = None
            msgbox.warning(self, "端口转发不可用", str(exc))

        self._arm_session_tunnels()

        self._set_connected(True)
        self._set_state("live")
        # **显式补一次尺寸同步。** open_shell 把远端 PTY 开在写死的 100x30 上，
        # 而 `TerminalWidget._recompute_grid` 在网格没变时会提前 return ——
        # 万一首次布局算出的网格恰好也是 100x30，一次 resize_pty 都不会发，
        # 远端就一直停在这个写死的尺寸上（less/vim 会按错的宽度折行）。
        # 直接读当前网格走 `_on_terminal_resize`（而不是 `_send_pty_resize`）：
        # 那就是"格子数变了"唯一的通路，同样的节流、同样的去重。在这里另起一条
        # 直发路径，等于多出一份"尺寸同步"的定义，迟早两边不一致。
        self._on_terminal_resize(self.terminal._cols, self.terminal._rows)

    def _persist_tunnels(self) -> None:
        """Write the live rule set back onto the saved session and to disk.

        This is what makes a rule added at runtime survive closing the tab:
        without it the configuration only ever existed in the connection
        object, which dies with the session.
        """
        if self._tunnels is None or self._last_saved is None:
            return
        self._last_saved.tunnels = self._tunnels.current_rules()
        win = self.window()
        if isinstance(win, MainWindow):
            win.persist_session(self._last_saved)

    def _arm_session_tunnels(self) -> None:
        """Start the forwards stored on the saved session, if any.

        A rule that cannot start (no port left in the scan range, usually) is
        reported and left off — it must not take down the session that just
        connected, and the stored state is written back so the switch shows
        the truth.

        **端口被自动改过时要回写并告诉用户。** 两条会话都按惯例写 13306 时，
        后连的那条会顺延到 13307 —— 这个号必须存回磁盘，否则用户下次打开
        会话，看到的还是 13306，而实际监听着的是 13307，规则形同虚设。
        """
        if self._conn is None or self._last_saved is None:
            return
        started = 0
        remapped: list[tuple[int, int]] = []
        for entry in self._last_saved.tunnels or []:
            parsed = TunnelSpec.from_dict(entry)
            if parsed is None:
                continue
            spec, enabled = parsed
            try:
                # **用返回值**：add_tunnel 返回的是实际绑上的那条，
                # 端口可能已经不是传进去的那个了。
                actual = self._conn.add_tunnel(spec, enabled=enabled)
                if actual.local_port != spec.local_port:
                    remapped.append((spec.local_port, actual.local_port))
                if enabled:
                    started += 1
            except TunnelError as exc:
                self._flash_status(f"隧道未启动 · {exc}")
        for old, new in remapped:
            self._flash_status(f"本地端口 {old} 已被占用，已自动改到 {new}")
        if self._tunnels is not None:
            self._tunnels.refresh()
        if remapped and self._tunnels is not None:
            # **必须真的落盘。** 只改内存里那份的话，用户下次打开会话看到的
            # 还是老端口，而实际监听着的是新端口 —— 规则看着开着却连不上。
            # 从面板回读而不是手拼：面板拿的是 conn 里的真实 spec，
            # 手拼一份迟早和面板显示的不一致。
            self._persist_tunnels()
        if started:
            self._flash_status(f"已启动 {started} 条端口转发")

    def _set_title(self, title: str) -> None:
        w = self
        while w is not None and not isinstance(w, QtWidgets.QTabWidget):
            w = w.parent()
        if w is not None:
            idx = w.indexOf(self)
            if idx >= 0:
                w.setTabText(idx, title)
                w.setTabToolTip(idx, title)

    def _set_connected(self, on: bool) -> None:
        self.reconnect_btn.setEnabled(not on)
        self.disconnect_btn.setEnabled(on)
        self.upload_btn.setEnabled(on)
        self.download_btn.setEnabled(on)
        self.sz_target_edit.setEnabled(on)

    def _set_state(self, kind: str, detail: str = "") -> None:
        """kind: idle | busy | live | error"""
        # 颜色不在这里定：同一个 kind 在深浅两套下是不同的色，而 QSS 是按
        # `setProperty("color", …)` 的实参去匹配的，颜色归 `_refresh_state_style`
        # 按**当前** palette 算（见那里的注释）。这里只管文字。
        text = {"idle": "未连接", "busy": "连接中…", "live": "已连接",
                "error": detail[:60] or "连接失败"}.get(kind, "")
        self._state_kind = kind
        self.state_label.setText(text)
        self._refresh_state_style()
        self.stateChanged.emit()

    def state_kind(self) -> str:
        """idle | busy | live | error -- the sidebar rail keys off "live"."""
        return getattr(self, "_state_kind", "idle")

    def _state_color(self) -> str:
        """当前状态该用哪个语义色。**读的是 ``self._palette``，不是旧主题。**"""
        p = self._palette
        return {"idle": p.text_muted, "busy": p.warning,
                "live": p.success, "error": p.danger}.get(
                    getattr(self, "_state_kind", "idle"), p.text_muted)

    def _refresh_state_style(self) -> None:
        # **绿点和状态文字必须一起重写。** 状态文字的颜色来自
        # `setProperty("color", …)`，而 QSS 靠 `QLabel[role="stateText"]
        # [color="#…"]` 匹配那个实参 —— 切主题后新 QSS 里根本没有旧实参那条
        # 规则，于是文字落回 `text_dim`（绿点却因为这里被重画还是新色的，
        # 所以用户不会把它读成"断线"，只会觉得颜色脏了）。所以这里不能只动
        # state_dot，得把 label 的 property 按**新**配色重写一遍再 repolish。
        color = self._state_color()
        self.state_dot.setStyleSheet(f"background-color: {color}; border-radius: 4px;")
        self.state_label.setProperty("color", color)
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)

    # ------------------------------------------------------------------ channel

    def _on_channel_ready(self, _fd: int = 0) -> None:
        ch = self._channel
        if ch is None:
            return
        if ch.closed:
            self._on_channel_closed()
            return
        # 全部走 getattr：这个槽在测试里也会拿到只实现了 recv/closed 的假
        # channel，直接摸属性会 AttributeError；而 PyQt6 里槽抛异常是直接
        # 整个进程 fast-fail（0xC0000409），不是只打一行日志。
        def st(name):
            return getattr(ch, name, "?")
        try:
            data = ch.recv(65536)
        except Exception as exc:
            log.info("通道读取异常  %r  closed=%s eof=%s active=%s",
                     exc, st("closed"), st("eof_received"), st("active"))
            data = b""
        if not data:
            log.info("通道读到空字节 -> 判定断开  closed=%s eof_sent=%s "
                     "eof_recv=%s active=%s exit_status=%r",
                     st("closed"), st("eof_sent"), st("eof_received"),
                     st("active"), st("exit_status"))
            self._on_channel_closed()
            return

        # Anything coming back means the remote reacted -- if that was a Tab,
        # its readline already completed and our fallback must stay out of it.
        self._cancel_completion()

        # A running pump owns the byte stream: the notifier is the *only*
        # reader of the channel and simply routes the bytes.
        if self._zmodem_pump is not None:
            self._zmodem_pump.feed(data)
            return

        # No transfer in progress -- look for one starting. lrzsz's `sz` also
        # prints a ZRQINIT-shaped "rz waiting to receive" hint, so the header
        # alone cannot say which way the data flows; the user's intent (set by
        # the toolbar buttons) is what decides.
        if self._zmodem_pending_direction == "download":
            # Remote `sz` waits for our ZRINIT, so the local receiver has to go
            # first -- there is no inbound header to wait for.
            #
            # **必须判 None。** _start_zmodem_download() 有三条早退路径（本地找不到
            # rz 二进制、通道已关、忙时闸门拒绝），它们都让 pump 留空。下一行直接
            # .feed() 就是 AttributeError，而这是在 PyQt6 的槽里 —— 槽抛异常是整个
            # 进程 0xC0000409，不是打一行日志。macOS/Linux 没装 lrzsz 正是 README
            # 主推的路径，这条在那儿稳定触发。
            self._start_zmodem_download()
            if self._zmodem_pump is not None:
                self._zmodem_pump.feed(data)
            else:
                # 传输没起来，这些字节还给终端，别把人吞了。
                self.terminal.write(data)
            return

        # **工具栏「上传」这条路以前完全没有分支接。**
        #
        # 症状：点上传 -> 远端 rz 起来了、`** waiting to receive.` 也上了屏，
        # 然后**什么都不发生** —— 文件没到，而状态条和闸门都干干净净
        # （因为 pump 从来没起过，没有任何东西处于 busy）。真机联调实测：
        # `u1=False u2=False`、`0 字符 / 期望 1100`。
        #
        # 机制：`_on_upload` 发完 `rz` 之后设 `_zmodem_pending_direction="upload"`，
        # 于是这里 `== "download"` 不匹配、`"upload"` 又没人接，一路掉到最下面：
        # `QtCore.QTimer.singleShot(0, self._ask_zmodem_direction)` 把这一批
        # **含 ZRQINIT 的字节**交给终端，`self.terminal.write(data)` 收尾；
        # 而 singleShot 那一拍里 `_ask_zmodem_direction` 第一句就是
        # `if self._zmodem_pending_direction is not None: return`
        # —— 方向是工具栏自己设的，于是它**什么都不做**直接返回。
        #
        # **所以含 ZRQINIT 的那一批永远到不了本地 `sz.exe` 的 stdin。**
        # sz 是被动方：启动后等的就是这个 ZRQINIT，收到才回 ZRINIT；
        # 收不到就一直等，远端 rz 也就一直等 —— 两边互等，零异常零报错。
        # 下载方向没这个毛病，正是因为上面那条分支把 `data` 喂给了 pump。
        #
        # **顺序必须与 download 那条完全一致：先起 pump，再 feed 这一批。**
        # 反过来（先 feed 再起 pump）会因为 `_proc` 还是 None 而被 `feed()`
        # 的早退吞掉，结果一模一样地不通。
        if self._zmodem_pending_direction == "upload":
            self._start_zmodem_upload()
            if self._zmodem_pump is not None:
                self._zmodem_pump.feed(data)
            else:
                self.terminal.write(data)
            return

        # 不要用 `or` 短路。** 原来写成
        # `looks_like_zmodem_header(data) or self._zmodem_detector.feed(data)`：
        # 一旦启发式命中，`feed()` 就**不会被调用**，检测器那 3 字节 tail
        # 不前进 —— 于是下一批数据会拿一个**过期的尾巴**去拼，可能拼出假头。
        # 两边都算，代价是几百次字符串匹配，换来 tail 始终是真的。
        _saw_header = lrzsz.looks_like_zmodem_header(data)
        _detector_hit = self._zmodem_detector.feed(data)
        if _saw_header or _detector_hit:
            # **只说明「远端在等一次传输」，不含方向。** ZRQINIT（`B0`）是
            # rz 和 sz **共用**的头，方向只能从用户敲的命令认
            # （`_zmodem_command_hint`），认不出才在这里问。
            #
            # **推迟一拍再问。** 模态框不能在 `QSocketNotifier` 的槽里开：
            # 它会进嵌套事件循环，通道里那批还没读完的字节**再次**触发同一个
            # 槽，于是叠出第二层对话框、同一批 ZRQINIT 也往终端里画两遍
            # （评审实测：叠 2 层、`terminal.write` 调 2 次）。
            QtCore.QTimer.singleShot(0, self._ask_zmodem_direction)

        self.terminal.write(data)

    def _on_terminal_resize(self, cols: int, rows: int) -> None:
        """终端格子数变了 -> 同步远端 PTY。

        节流而不是防抖：拖动窗口边时 resizeEvent 每秒能来几十次，逐次发
        window-change 会把通道塞满；而纯防抖（等到停下来才发）又会让整个
        拖动过程中远端一直停在旧尺寸，less/vim 会按错的宽度重排。
        所以是"最多每 80 ms 一次，每次发最新的那个尺寸"。
        """
        self._pending_pty_size = (cols, rows)
        if self._pty_resize_timer is None:
            self._pty_resize_timer = QtCore.QTimer(self)
            self._pty_resize_timer.setSingleShot(True)
            self._pty_resize_timer.timeout.connect(self._send_pty_resize)
        if not self._pty_resize_timer.isActive():
            self._pty_resize_timer.start(80)

    def _send_pty_resize(self) -> None:
        ch = self._channel
        if ch is None or getattr(ch, "closed", True):
            return
        cols, rows = self._pending_pty_size
        if (cols, rows) == getattr(self, "_sent_pty_size", None):
            return
        try:
            ch.resize_pty(width=cols, height=rows)
        except Exception as exc:
            # @open_only：通道刚断时仍可能抛；PTY 同步失败不该影响会话本身，
            # 但**不能一声不吭**。远端停在旧尺寸上，用户看到的现象是"窗口拉大了、
            # 远端没跟着变"，而原因在界面上无处可查 —— 这正是"完全不吭声"那一类。
            #
            # 状态栏**只说连续失败的第一句**：这条路径最多每 80ms 触发一次，
            # 逐次刷新等于把提示变成刷屏，那又是一种打扰。日志每次都记。
            log.warning("PTY 尺寸同步失败  %dx%d  %r", cols, rows, exc)
            if not getattr(self, "_pty_sync_told", False):
                self._pty_sync_told = True
                self._flash_status(f"远端窗口尺寸没能同步（{cols}x{rows}）：{exc}")
            return
        self._sent_pty_size = (cols, rows)
        self._pty_sync_told = False

    def _on_channel_closed(self) -> None:
        log.info("通道关闭  closed=%s eof_recv=%s active=%s",
                 getattr(self._channel, "closed", None),
                 getattr(self._channel, "eof_received", None),
                 getattr(self._channel, "active", None))
        if self._pty_resize_timer is not None:
            self._pty_resize_timer.stop()
        # 摘掉 resize 回调：通道没了还去 resize_pty 只会抛，而 PyQt6 里
        # 槽抛异常是整个进程 fast-fail，不是只打一行日志。
        self.terminal.set_resize_handler(None)
        if self._notifier is not None:
            try:
                self._notifier.setEnabled(False)
            except Exception:
                pass
            self._notifier = None
        if self._channel is not None and not self._channel.closed:
            try:
                self._channel.close()
            except Exception:
                pass
        self._channel = None
        self._set_connected(False)
        self._set_state("idle")
        self._set_title("已断开")
        # 通道没了，传输的字节流也就没人喂了。**必须在这里也清一次**：这条路径
        # 既可能是用户点的断开（_on_disconnect 已经清过），也可能是远端 EOF /
        # 网络抖动自己关的。后者以前不清，于是 `_zmodem_pump` 悬着、闸门还锁着，
        # 状态条停在 running —— 明明什么都没在传。
        if self._zmodem_pump is not None:
            self._zmodem_pump.stop()
            self._zmodem_pump = None
        self._reset_zmodem_state()
        self.transfer_ctrl.reset("通道关闭")

    def _on_disconnect(self) -> None:
        if self._snippet_panel is not None:
            # 断了之后面板里点什么都插不进去，留着只是个能点不动的窗
            self._snippet_panel.close()
        if self._zmodem_pump is not None:
            self._zmodem_pump.stop()
            self._zmodem_pump = None
        self._reset_zmodem_state()
        self.transfer_ctrl.reset("断线")
        if self._monitor is not None:
            self._monitor.stop()
        # Stop the panel's poll timer first, then the listeners themselves;
        # conn.close() also drains the registry, but doing it here keeps the
        # ports free before the transport goes away.
        if self._tunnels is not None:
            self._tunnels.shutdown()
        self._on_channel_closed()
        if self._conn is not None:
            self._conn.close()
        self._conn = None

    def _reset_zmodem_state(self) -> None:
        """把所有「上一次传输留下的待办」清空。

        不清会串台：`_zmodem_pending_direction` 留着 ``"download"`` 时，**重连后
        新会话收到的第一批字节**（很可能只是 shell banner）就会命中
        ``_on_channel_ready`` 里那条分支，凭空拉起一个本地 ``rz`` 去灌它。
        detector 的 3 字节 tail 同理——它可能和新字节拼出一个假头。

        attach() / _on_disconnect() 两处都要调：前者防"重连"，后者防"断线后
        同一标签再连"。
        """
        self._zmodem_pending_direction = None
        self._sz_targets = []
        self._pending_zmodem_files = []
        self._pending_zmodem_cwd = ""
        self._pending_zmodem_skipped = []
        self._zmodem_detector.reset()
        # 换了通道，上一次那份"远端有没有 lrzsz"的结论就不作数了。
        self._lrzsz_probe = None

    def _on_reconnect(self) -> None:
        if self._last_saved is not None:
            win = self.window()
            if isinstance(win, MainWindow):
                win.reopen_session(self._last_saved)
                return
        msgbox.information(
            self, "重新连接", "这是一个临时会话，没有保存的配置。请从左侧会话列表重新打开。"
        )

    # ------------------------------------------------------------------ zmodem

    def _remote_is_utf8(self) -> bool:
        """远端 shell 是不是在 UTF-8 locale 里。**只问一次**，结果缓存。

        问的是 `charmap` 而不是 `LANG`：有的机器 `LANG` 留着 `en_US.UTF-8`
        而实际 `LC_CTYPE` 已经是别的了，`locale charmap` 报的是**真正生效**
        的那一个。问不到就按 UTF-8 算 —— 那是最常见的情形，而
        「多退几格」比「退不够」安全（readline 到行首后 DEL 无副作用，
        多退只会把光标停在行首，不会吃掉提示符）。

        **判据得写清楚这个不对称。** 退够了但补全不干净 = 用户看到半个词；
        退过头 = 光标跑到行首、再补全，结果是路径前面多了空格。两者都坏，
        但后者不会**吃掉命令**，所以非 UTF-8 时宁可按字符数。
        """
        if self._remote_utf8 is not None:
            return self._remote_utf8
        self._remote_utf8 = True
        if self._conn is None:
            return True
        try:
            out, _err = self._conn.exec("locale charmap 2>/dev/null", timeout=5)
        except Exception:                          # noqa: BLE001
            return True
        text = (out or "").strip().lower()
        # C.UTF-8 / en_US.UTF-8 / utf8 都算；查不到输出（老 locale 无此命令）
        # 也按 UTF-8 算，理由同上。
        self._remote_utf8 = (text == "" or "utf" in text)
        return self._remote_utf8

    def _erase_width(self, word: str) -> int:
        """删掉 *word* 要发几个 DEL（0x7f）。

        **UTF-8 locale 下 readline 按字符退，非 UTF-8 下按字节退。**
        原来无条件用 `len(word)`（字符数），在 `LANG=C` / `POSIX` 的远端上
        一个汉字要退 3 次却只发了 1 次 —— 补全退不干净，后面的字留在原地。
        实测本机 `locale` 是 `C.UTF-8`，所以这条在多数机器上**看不出问题**，
        它是隐患不是现网故障；但改起来只有一行，不值得留着。

        注意**不能无脑改成字节数**：那在 UTF-8（也就是绝大多数）上会退过头。
        """
        if self._remote_is_utf8():
            return len(word)
        return len(word.encode("utf-8", errors="replace"))

    def _shell_cwd(self) -> str:
        """交互 shell 当前所在的目录；拿不到就返回 ``""``。

        **只能从提示符里拿。** `conn.exec("pwd")` 开的是**另一个** shell
        （paramiko 的 exec_command 走登录 shell），它的 CWD 永远是家目录——
        拿它判断交互 shell 停在哪，实测会把文件传到 `~/` 而不是用户当时所在的
        那一层。终端补全已经靠 `_guess_cwd()` 从提示符解析了同一件事，这里复用
        它，不另开一条路。

        ``_guess_cwd`` 解析不出来时返回 ``~``，而 SFTP 不是 shell、不会展开它，
        所以换成远端真实的家目录。
        """
        if self.terminal is None:
            return ""
        try:
            line, _cursor = self.terminal.input_line()
        except Exception:                          # noqa: BLE001
            return ""
        prompt, _typed = self._split_prompt(line)
        # **`$` 后面可能一个空格都还没有**——用户刚 cd 完、光标就贴在 `$` 上，
        # 实测真机的提示符就是 `deploy@host:/tmp/x$`。而 `_split_prompt`
        # 的 `[$#]\s` 要求后面有空白，这时它会把这行当成"没有提示符"，返回空
        # 提示符 + 整行内容，于是 `_guess_cwd("")` 给出 "~"，文件就掉回家目录了
        # （真机端到端验出来的）。改用 `_guess_cwd` 自己的 `\s*$`，零个空格也收。
        cwd = self._guess_cwd(prompt or line)
        if cwd in ("", "~"):
            try:
                return self._sftp._rfs.getcwd() if self._sftp is not None else ""
            except Exception:                      # noqa: BLE001
                return ""
        return cwd

    def _ensure_lrzsz(self, direction: str) -> str:
        """远端能不能用 ZMODEM。返回 ``"zmodem"`` / ``"sftp"`` / ``"cancel"``。

        问一次、记一份，重连时清掉（见 ``_reset_zmodem_state``）。

        **探测失败（超时、通道半死）不拦着用户**，当可用处理——把一次问不出来
        的结果说成"没装"，会白拦一次本来好好的传输。真的没装才提示。
        """
        if self._lrzsz_probe is None:
            self._lrzsz_probe = lrzsz.probe_remote(self._conn) \
                if self._conn is not None else lrzsz.RemoteProbe(ok=False)
        if self._lrzsz_probe.usable:
            return "zmodem"

        what = "ZMODEM 上传" if direction == "upload" else "ZMODEM 下载"
        box = msgbox._build(
            self, QtWidgets.QMessageBox.Icon.Warning, "远端没有 lrzsz",
            "远端没有检测到 sz / rz，%s 用不了。\n\n"
            "安装方式（按远端发行版选一条）：\n"
            "    Debian / Ubuntu:  apt-get install -y lrzsz\n"
            "    RHEL / CentOS:     yum install -y lrzsz" % what,
            palette=self._palette)
        to_sftp = box.addButton("改用 SFTP 传输",
                                QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("先不传，我去装 lrzsz",
                      QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(to_sftp)
        box.exec()
        # 点"先不传"就**不要**接着去发 sz/rz：远端根本没这个命令，发过去只会
        # 换来终端里一句 command not found，按钮这边毫无反馈。
        return "sftp" if box.clickedButton() is to_sftp else "cancel"

    def _fallback_upload(self, paths: list) -> None:
        """ZMODEM 不可用时，用 SFTP 把 *paths* 传到 shell 所在的目录。"""
        if self._sftp is None:
            msgbox.warning(self, "无法改用 SFTP", "这个会话没有打开 SFTP 面板。")
            return
        cwd = self._shell_cwd() or self._sftp.remote.current_path()
        # 顺带把远端面板也指过去：用户被切到「文件」页时看到的应该就是文件
        # 真正落地的那个目录，而不是面板上一次停留的地方。
        if self._sftp.remote.current_path() != cwd:
            try:
                self._sftp.remote.cd(cwd)
            except Exception:                      # noqa: BLE001
                pass
        n = self._sftp.upload_paths(paths, cwd)
        if n:
            self._flash_status(f"远端没有 lrzsz，已改用 SFTP · {n} 个文件 → {cwd}")
        self._set_sub_tab("文件")

    def _fallback_download(self, paths: list) -> None:
        """ZMODEM 不可用时，用 SFTP 把 *paths* 下载到本地。"""
        if self._sftp is None:
            msgbox.warning(self, "无法改用 SFTP", "这个会话没有打开 SFTP 面板。")
            return
        target = lrzsz.download_dir()
        if target is None:
            msgbox.warning(self, "无法改用 SFTP",
                           "本地下载目录不可用，本次下载未开始。\n\n" + _DL_DIR_HINT)
            return
        n = self._sftp.download_paths(paths, target)
        if n:
            self._flash_status(f"远端没有 lrzsz，已改用 SFTP · {n} 个文件 → {target}")
        self._set_sub_tab("文件")

    def _set_sub_tab(self, name: str) -> None:
        for i in range(self.sub_tabs.count()):
            if self.sub_tabs.tabText(i) == name:
                self.sub_tabs.setCurrentIndex(i)
                return

    def _on_upload(self) -> None:
        if not self._channel or self._channel.closed:
            msgbox.warning(self, "未连接", "请先建立连接。")
            return
        if self._channel_block_reason():
            return          # 状态条就在那儿，不用再弹一个框
        # 允许多选：sz 一次能送一批（实测传 basename 时确实会一个不落地收齐）。
        paths, _ = msgbox.open_files(
            self, "选择要上传的文件 (ZMODEM，可多选)", "", "All files (*)"
        )
        if not paths:
            return
        # **探测放在选完文件之后**：没有文件就没必要问远端有没有 lrzsz，
        # 白白多等一次往返。反过来（先探测再弹框）会让"其实用不上"的场景
        # 也付这次等待。
        action = self._ensure_lrzsz("upload")
        if action != "zmodem":
            if action == "sftp":
                self._fallback_upload(list(paths))
            return
        cwd, names, skipped = lrzsz.plan_local_upload(list(paths))
        if not names:
            self.transfer_ctrl.report_problem("没有可上传的文件", "\n".join(skipped))
            return
        try:
            self._channel.sendall(b"rz\n")
        except Exception as exc:
            self.transfer_ctrl.report_problem("rz 失败", str(exc))
            return
        # Remote `rz` answers with a ZRQINIT right away; the local `sz` is only
        # spawned once that header actually shows up (see _on_channel_ready).
        self._zmodem_pending_direction = "upload"
        self._pending_zmodem_files = names
        self._pending_zmodem_cwd = cwd
        self._pending_zmodem_skipped = skipped
        who = names[0] if len(names) == 1 else f"{len(names)} 个文件"
        self._flash_status(f"等待远程 rz 响应 · {who}")

    def _on_download(self) -> None:
        if not self._channel or self._channel.closed:
            msgbox.warning(self, "未连接", "请先建立连接。")
            return
        # **闸门要在往通道写东西之前就问。** 原来只查 `busy`，于是那半秒
        # 窗口里 `sz` 已经发出去了，之后只能靠一个 Ctrl+C 去打断远端 ——
        # 而那条 Ctrl+C 本身就是往载荷流里塞字节。
        block = self._channel_block_reason()
        if block:
            # 下载期间终端命令通道被 ZMODEM 占用，用户敲什么都是空输入。
            # 弹窗告知，而不是让用户以为界面卡死了。
            msgbox.warning(
                self, "下载期间终端不可用",
                f"下载已开始，稍后请在终端窗口查看进度。\n\n{block}",
            )
            return
        # 输入框可以写多个路径，按 shell 规矩拆：空格分隔，引号包住带空格的。
        targets = lrzsz.split_remote_paths(self.sz_target_edit.text())
        if not targets:
            msgbox.warning(
                self, "请输入远程下载路径",
                "在输入框里填入要下载的远程文件路径，例如：\n"
                "    /var/log/syslog\n"
                "    ~/mavisprobe/t.md\n\n"
                "多个文件用空格分开，带空格的路径用引号包住。",
            )
            return
        # **下载前先问远端这些路径在不在。** 原来直接发 `sz`，路径打错时远端
        # 只会回一句 `sz: no such file or directory` 混在终端输出里，而按钮这边
        # 什么反馈都没有 —— 用户看着「什么都没发生」。这里提前问一次，把结果
        # 变成一句说人话的提示。
        exists = lrzsz.check_remote_paths(self._conn, targets) \
            if self._conn is not None else None
        if exists is not None:
            # `is False` 而不是 `.get(t, False)`：**只有远端明确回了 0 才算
            # 不存在**。问过了但这一行没解析出来（路径带控制字符等），那是
            # 我们这边的解析问题，不是远端说文件没了 —— 按 `check_remote_paths`
            # 自己的理由，同样该放行而不是拦下来。默认 False 会把「问不到」
            # 悄悄算成「不存在」，一次解析抖动就变成一句劝退的假报错。
            missing = [t for t in targets if exists.get(t) is False]
            if missing:
                # **「~ 展开失败」和「文件不存在」要分开说。**
                #
                # 新的路径拼接把 `~` 交给远端用 `${HOME}` 展开，所以
                # `~nosuchuser/x` 这种**用户不存在**的情况，远端 shell 会回
                # 一句 `bash: ~nosuchuser: No such file or directory` ——
                # 而在探测协议里它表现为「这个路径不存在」。
                #
                # 两种情况的**下一步动作不同**：后者是文件名打错了，前者是
                # 用户名写错了（或者是别人的家目录）。混成一句「文件不存在」
                # 的话，用户会去检查路径里的文件名，而真正错的那一段
                # 根本不在那儿。
                bad_user = [m for m in missing
                            if m.startswith("~") and
                            len(m) > 1 and m[1] not in ("/", "\\")]
                if bad_user:
                    msgbox.warning(
                        self, "远程路径展开失败",
                        "以下路径里的 `~用户名` 在远程服务器上不存在：\n\n%s\n\n"
                        "`~` 后面是**用户名**，不是路径 —— 请确认这个用户名在"
                        "远程机器上确实存在。"
                        % "\n".join("    " + m for m in bad_user),
                    )
                    return
                msgbox.warning(
                    self, "远程文件不存在",
                    "以下路径在远程服务器上不存在：\n\n%s\n\n"
                    "请检查下载的文件路径后重试。"
                    % "\n".join("    " + m for m in missing),
                )
                return
        # **确认存在之后才问保存目录。** 顺序是刻意的：路径都没让用户先挑目录，
        # 白挑一趟还得回来改路径。用户取消 = 什么都不做。
        chosen = msgbox.ask_dir(
            self, "选择下载保存目录",
            self._dl_target_dir or lrzsz.download_dir() or "",
        )
        if not chosen:
            self._flash_status("已取消下载")
            return
        self._dl_target_dir = chosen
        # **探测要在往通道写 `sz` 之前**。写出去就晚了：远端回一句
        # `sz: command not found`，用户看到的是终端里一行看不懂的报错，
        # 而按钮这边什么反馈都没有。
        action = self._ensure_lrzsz("download")
        if action != "zmodem":
            if action == "sftp":
                self._fallback_download(list(targets))
            return
        self._zmodem_pending_direction = "download"
        self._sz_targets = targets
        # **每个路径在远端必须恰好是一个 word，且不能带任何可被远端 shell
        # 解释的语法。** 这条命令是打进**交互式登录 shell** 的 —— 注入面比
        # `check_remote_paths` 那条 `conn.exec` 更直接（那条也是远端 shell
        # 执行，但至少不经过用户的交互会话）。
        #
        # 规则收在 `lrzsz.remote_words` 一处，别在这里另写一份：
        # 老写法是「`~` 开头原样拼、其余 shlex.quote」，那条规则让 `~` 路径
        # 里的 `;` `|` `>` 被远端 bash 二次解释（详见 lrzsz.remote_word 的
        # docstring 里的实测表）。现在 `~` 走远端 `${HOME}` 插值展开。
        cmdline = "sz " + lrzsz.remote_words(targets) + "\n"
        try:
            self._channel.sendall(cmdline.encode("utf-8"))
        except Exception as exc:
            self._zmodem_pending_direction = None
            msgbox.warning(self, "sz 失败", str(exc))
            return
        self._flash_status(f"等待远程 sz · {'、'.join(targets)}")
        # The remote sender sits waiting for our ZRINIT, so the local receiver
        # has to speak first. Give the shell a beat to start `sz`, otherwise our
        # ZRINIT is typed into a prompt that isn't listening yet and is lost.
        QtCore.QTimer.singleShot(500, self._begin_zmodem_download)

    def _begin_zmodem_download(self) -> None:
        if self._zmodem_pending_direction != "download" or self._zmodem_pump:
            return
        self._start_zmodem_download()

    def _zmodem_command_hint(self) -> tuple[str | None, str]:
        """从终端屏幕上认出用户刚敲的是 ``rz`` 还是 ``sz``，连同它的参数。

        返回 ``(direction, args)``。``direction`` 是 ``"upload"`` /
        ``"download"`` / ``None``（**认不出来**，交回调用方去问）。

        **方向一直都知道，只是没人去读它。** ``rz``（远端收）与 ``sz``
        （远端发）发的是同一个 ZRQINIT 头，字节上分不出方向 —— 但用户刚敲的
        那条命令**就印在终端上**，而且是离这个等待头最近的一条提示符行。

        三道防误认的关：

        1. **``**`` 之前才是命令。** lrzsz 每一帧头都是 ``**\\x18B0…``
           （``\\x18`` 被 pyte 画成空白，屏幕上就是 ``**B0100…``）。真实命令行
           不会有 ``**``，所以从它起把这一行截断 —— 这一刀同时干掉三种东西：
           lrzsz 自己的提示行、帧头行、以及「命令和帧头被回显挤在同一行」那种。
        2. **只往上看** ``_ZMODEM_HINT_LOOKBACK`` 行，**离光标最近的那条赢**。
        3. **带提示符的赢裸行。** 认不出 PS1 时只能把整行当命令，那种情况下
           才有资格用裸行；两者都命中时先返回带提示符那条。lrzsz 在 ``sz``
           等不到回应时会打印 ``rz\\r**\\x18B0100…`` 提醒「该起 rz 了」——
           它第一个词就是 ``rz``，光看裸行会把一次**下载**说成上传。

        认不出就返回 ``None`` 去问用户，**不硬猜**：猜错的方向比多问一次的
        代价大得多（两个发送方对着干，传输必然失败）。
        """
        terminal = getattr(self, "terminal", None)
        if terminal is None:
            return None, ""
        try:
            rows, cursor_y = terminal.screen_rows()
        except Exception:                          # noqa: BLE001
            return None, ""
        if not rows:
            return None, ""
        top = max(0, cursor_y - _ZMODEM_HINT_LOOKBACK)
        bare: tuple[str, str] | None = None
        for y in range(min(cursor_y, len(rows) - 1), top - 1, -1):
            # 帧头那一刀必须在切提示符**之前**：帧头和命令挤在一行时（远端不回显
            # 或两批字节被并成一批），命令行的后面会粘着 `**B0…`。
            cut = rows[y].find("**")
            row = rows[y] if cut < 0 else rows[y][:cut]
            if not row.strip():
                continue
            prompt, typed = self._split_prompt(row)
            parts = typed.split(None, 1)
            if not parts:
                continue
            args = parts[1].strip() if len(parts) > 1 else ""
            # basename：`/usr/bin/sz`、`./sz` 认的是同一个东西
            name = posixpath.basename(parts[0]).lower()
            if name in _ZMODEM_RZ_NAMES:
                hit = ("upload", args)
            elif name in _ZMODEM_SZ_NAMES:
                hit = ("download", args)
            else:
                continue
            if prompt:
                return hit
            if bare is None:
                bare = hit
        return bare if bare is not None else (None, "")

    def _ask_zmodem_direction(self) -> None:
        """远端自己敲了 ``rz`` / ``sz``：**先自己认，认不出才问**。

        **为什么必须问。** ``rz``（远端收）与 ``sz``（远端发）发的是
        **同一个** ZRQINIT 头（``**\\x18B0``），字节上完全一样 ——
        光看字节只知道「远端在等一次传输」，不知道谁往谁发。

        **为什么不再每次都问。** 用户已经在终端里把命令敲下去了：
        ``rz`` 就是要把本地文件发过去，``sz <文件>`` 就是要把远端文件拉下来。
        方向是**白纸黑字印在屏幕上**的，让用户对着一个自己刚敲过的命令再点
        一次「上传还是下载」，是白问。`_zmodem_command_hint()` 去认，认得出
        就直接往下走；**认不出才弹框** —— 提示符被自定义成认不出的样子、
        或者远端是被脚本/别名触发的，那仍然只有用户知道。

        **原来这里直接 ``_start_zmodem_upload()``，两个后果都是静默的**：

        - 敲 ``sz <文件>``：远端在等**收**，我们却去起一个本地 ``sz``
          **发** —— 两个发送方对着干，传输必然失败，用户看到的是
          「什么都没发生」。
        - 敲 ``rz``：本该弹「选择要上传的文件」，但 ``_ask_zmodem_files``
          的第一行是 ``if self._zmodem_pending_direction != "upload": return``，
          而**这条路上没人设过它**（只有工具栏按钮那条路设），
          于是它当场静默返回 —— 终端里敲 ``rz`` 什么都不会发生。
          README 里「在终端里直接敲 ``rz`` 会触发上传流程」就是这么不成立的。

        已经证明方向的那两条路（工具栏按钮）**不走这里** —— 它们在
        ``_zmodem_pending_direction`` 上写着方向，由 ``_on_channel_ready``
        上面的分支直接接走。
        （**这条注释以前是假的**：上面原本只有 ``"download"`` 那一条分支，
        ``"upload"`` 没人接，于是工具栏上传的 ZRQINIT 被
        ``terminal.write`` 吃掉，而这里又因为 ``_zmodem_pending_direction``
        非 None 直接 return —— pump 永远不起来。真机联调抓到过，
        见上面 ``== "upload"`` 那一支的注释。）
        """
        if self._zmodem_pending_direction is not None or self._zmodem_pump is not None:
            # 方向已经定了（工具栏那条路），或已经有传输在跑。别插手。
            return
        if self._channel is None or self._channel.closed:
            return

        direction, args = self._zmodem_command_hint()
        if direction == "upload":
            # **还是得问文件**：本地有哪些要发，只有用户知道，而远端已经在等。
            # 省掉的只是「方向」那一次问。
            self._zmodem_pending_direction = "upload"
            self._flash_status("终端里敲的是 rz · 请选择要上传的文件")
            QtCore.QTimer.singleShot(0, self._ask_zmodem_files)
            return
        if direction == "download":
            # **参数也从屏幕上直接取。** 用户敲的 `sz a.log "my file.txt"`
            # 原样就能用，一个框都不用弹。
            targets = lrzsz.split_remote_paths(args) if args else []
            if targets:
                self._zmodem_pending_direction = "download"
                self._sz_targets = targets
                self._flash_status(
                    f"终端里敲的是 sz · 下载 {'、'.join(targets)}")
                self._start_zmodem_download()
                return
            # **光敲了 `sz` 没给参数**：远端那侧在交互地问文件名，而传输期间
            # 终端输入是被闸门封着的，用户没法在远端那边回答。所以只能问一次
            # 「要下哪些」，不能装作知道。
            self._flash_status("终端里敲的是 sz（无参数）· 请填要下载的远端路径")
            self._ask_zmodem_download_target()
            return

        box = msgbox._build(
            self, QtWidgets.QMessageBox.Icon.Question, "远端在等待 ZMODEM 传输",
            "没认出你在终端里敲的是 `rz` 还是 `sz`，这一边该做哪件事？",
            palette=self._palette)
        up = box.addButton("上传：把本地文件发给远端 (rz)",
                           QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        down = box.addButton("下载：从远端拉取文件 (sz)",
                             QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消（给远端发 Ctrl-C）",
                      QtWidgets.QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(up)
        box.exec()
        picked = box.clickedButton()

        if picked is up:
            # **必须先把方向设上。** `_ask_zmodem_files` 的第一行就是拿它当
            # 守门人 —— 不设就等于「问了个对谁都没用的框」，这正是原来
            # 终端里敲 `rz` 毫无反应的原因。
            self._zmodem_pending_direction = "upload"
            self._flash_status("等待选择要上传的文件")
            QtCore.QTimer.singleShot(0, self._ask_zmodem_files)
            return

        if picked is down:
            self._ask_zmodem_download_target()
            return

        self._zmodem_pending_direction = None
        self._break_remote_wait()

    def _ask_zmodem_download_target(self) -> None:
        """问「要从远端拉哪几个文件」，然后直接起下载。

        单独拆出来是因为有两条路会走到这儿：弹框问出来的「下载」，以及
        终端里敲了**光秃秃的 `sz`**（没有参数，认不出要下什么）。
        """
        text, ok = msgbox.ask_text(
            self, "从远端下载", "远端路径（可空格分隔多个，带空格的用引号）：",
            "")
        targets = lrzsz.split_remote_paths(text) if ok else []
        if not targets:
            self._zmodem_pending_direction = None
            self._break_remote_wait()
            return
        self._zmodem_pending_direction = "download"
        self._sz_targets = targets
        self._start_zmodem_download()

    def _ask_zmodem_files(self) -> None:
        """在 socket 槽**之外**问用户要哪些文件。

        推迟这一拍期间连接可能已经断了、或待办方向被别的事改掉了，
        所以第一件事是确认这件事还成立 —— 否则会弹出一个对谁都没用的框。
        """
        if self._zmodem_pending_direction != "upload" or self._channel is None \
                or self._channel.closed:
            self._zmodem_pending_direction = None
            return
        paths, _ = msgbox.open_files(
            self, "选择要上传的文件 (ZMODEM，可多选)", "", "All files (*)"
        )
        if not paths:
            self._zmodem_pending_direction = None
            return
        cwd, files, skipped = lrzsz.plan_local_upload(list(paths))
        if not files:
            self._zmodem_pending_direction = None
            self.transfer_ctrl.report_problem(
                "没有可上传的文件", "\n".join(skipped))
            return
        self._pending_zmodem_files = files
        self._pending_zmodem_cwd = cwd
        self._pending_zmodem_skipped = skipped
        self._start_zmodem_upload()

    def _start_zmodem_upload(self) -> None:
        # Remote `rz` is the *receiver*, so the local side must be `sz` (sender).
        binary = lrzsz.resolve_binary("sz")
        if not binary:
            self._zmodem_pending_direction = None
            self.transfer_ctrl.report_problem("找不到 lsz", _LRZSZ_HINT)
            return
        files = self._pending_zmodem_files
        if not files:
            # 用户是在终端里自己敲了 `rz`，没走过工具栏的按钮。
            #
            # **这个模态框必须挪出 socket 槽。** 本函数是被
            # ``QSocketNotifier.activated`` 驱动的，而 notifier 是**电平触发**的：
            # 模态框一开就进入嵌套事件循环，通道里那批还没读完的字节会**再次**
            # 触发同一个槽，于是叠出第二层文件对话框，同一批 ZRQINIT 也往终端
            # 里画两遍（评审实测：叠 2 层、terminal.write 调 2 次）。
            # 推迟一个事件循环，让 notifier 先把这一批读完。
            QtCore.QTimer.singleShot(0, self._ask_zmodem_files)
            return
        else:
            cwd = self._pending_zmodem_cwd
            skipped = self._pending_zmodem_skipped
        self._pending_zmodem_files = []
        self._pending_zmodem_cwd = ""
        self._pending_zmodem_skipped = []

        note = ""
        if skipped:
            note = "已跳过（不在同一目录）：" + "、".join(
                os.path.basename(p) for p in skipped)
        transfer = self._make_zmodem_transfer("upload", binary, files, cwd, note)
        if transfer is None:
            return
        if not self.transfer_ctrl.begin("upload", "上传", transfer, len(files), note):
            # 已经有传输在跑（比如文件 tab 那边正传着）。远端 rz 已经在等了，
            # 得把它打断，否则终端会一直卡在 rz 模式里出不来。
            self._break_remote_wait()
            self._flash_status("已有传输在进行，本次上传未开始")
            self._zmodem_pending_direction = None
            return
        self._set_state("busy")
        # 走 start_engine：Popen 抛异常时收成一次失败态，否则闸门永远不释放
        if not self.transfer_ctrl.start_engine(transfer):
            # 同 _start_zmodem_download：引擎没起来时 on_finished 永不触发，
            # 远端 rz 的 raw 模式没人救回来，必须由这一句打断。
            self._break_remote_wait()
            self._set_state("live")
        else:
            self._arm_zmodem_pump(transfer)
        self._zmodem_pending_direction = None

    def _abort_local_download(self, why: str, detail: str) -> None:
        """本地无处落盘 → 本次传输没起来，**但远端 sz 已经在等了**。

        不发 Ctrl-C 的话远端 tty 会一直停在 sz 的 raw 模式里出不来，而恢复
        提示符的那句 ``sendall(b"\\r\\n")`` 只在 ``_on_zmodem_finished`` 里，
        引擎压根没起来时它永不执行 —— 这正是 B18：闸门会放、终端不哑，
        可远端卡住没人救。
        """
        self.transfer_ctrl.report_problem(why, detail)
        self._break_remote_wait()
        self._zmodem_pending_direction = None

    def _start_zmodem_download(self) -> None:
        # Remote `sz` is the *sender*, so the local side must be `rz` (receiver).
        binary = lrzsz.resolve_binary("rz")
        targets = list(self._sz_targets)
        self._sz_targets = []
        if not binary:
            self._zmodem_pending_direction = None
            self.transfer_ctrl.report_problem("找不到 lrz", _LRZSZ_HINT)
            return
        # 落盘目录不能想当然用 CWD：macOS 从 .app 双击启动时它是 /，
        # rz 往 / 写必然权限拒绝，而失败点在子进程里，报出来的话没法看。
        # 全都不行时它给回 None（不再兜底一条已知写不进去的路径）。
        # 用户通过工具栏按钮选了目录 → 优先用它；未选则走系统默认三档。
        dl_dir = self._dl_target_dir or lrzsz.download_dir()
        if dl_dir is None:
            self._abort_local_download("本地下载目录不可用", _DL_DIR_HINT)
            return
        transfer = self._make_zmodem_transfer("download", binary, [],
                                              dl_dir, "", targets)
        if transfer is None:
            return
        if not self.transfer_ctrl.begin("download", "下载", transfer, len(targets)):
            self._break_remote_wait()
            self._flash_status("已有传输在进行，本次下载未开始")
            self._zmodem_pending_direction = None
            return
        self._set_state("busy")
        # 走 start_engine：Popen 抛异常时收成一次失败态，否则闸门永远不释放
        if not self.transfer_ctrl.start_engine(transfer):
            # 引擎没起来 → on_finished 永不触发 → _on_zmodem_finished 里那句
            # 把远端 tty 从 raw 模式救回来的 \r\n 也永远不发，而远端 sz 还在
            # 等着。和 begin() 被拒是同一种残留，必须同样打断它。
            self._break_remote_wait()
            self._set_state("live")
        else:
            self._arm_zmodem_pump(transfer)
        self._zmodem_pending_direction = None

    def _break_remote_wait(self) -> None:
        """给远端还在等的 rz / sz 送一个 Ctrl-C，把它从等待里拽出来。

        **但绝不能打断一个已经在传的 ZMODEM。** 这条通道此刻正被 sz/rz 的
        载荷流占着，插进去的 ``\\x03\\r\\n`` 会被当成传输数据，远端那半个
        文件就此报废。评审实跑确认过这条路径确实会写进通道。
        """
        if self._zmodem_pump is not None:
            log.info("有 ZMODEM 在传，不打断远端等待（会污染载荷流）")
            return
        ch = self._channel
        if ch is None or ch.closed:
            return
        try:
            ch.sendall(b"\x03\r\n")
        except Exception:
            pass

    def _make_zmodem_transfer(self, direction: str, binary: str, files: list[str],
                              cwd: str, note: str = "",
                              targets: list[str] | None = None
                              ) -> ZmodemTransfer | None:
        """建 ZmodemTransfer（它自己持有 ZmodemPump 并挂到本标签上）。

        **这里绝不碰 ``self._zmodem_pump``。** 那一行以前写在函数末尾，于是
        ``_zmodem_pump`` 在 ``begin()`` / ``start_engine()`` 还没跑之前就非空了：
        - ``begin()`` 被忙时闸门拒掉 -> pump 悬着没人用；
        - ``start_engine()`` 失败（Popen 抛 OSError）-> pump 悬着，而 ``_proc``
          是 None。

        两种情况下 ``_on_channel_ready`` 都会命中 ``if self._zmodem_pump is not
        None: pump.feed(data); return``，``feed`` 因为 ``_proc is None`` 直接
        return —— **远端来的每一个字节都被吞掉，终端从此完全哑掉**，而
        ``_reset_zmodem_state()`` 也不管这个字段，所以重连也救不回来，只能
        关标签页。用户看到的只是状态条上一句"传输启动失败"，之后终端再无输出。

        正确做法是「引擎真的起来了才挂上」，见 :meth:`_arm_zmodem_pump`。
        """
        if self._channel is None or self._channel.closed:
            self.transfer_ctrl.report_problem("连接已断开", "")
            return None
        transfer = ZmodemTransfer(
            self.transfer_ctrl, self._channel, direction, binary, files, cwd,
            on_log=self._flash_status, note=note,
            targets=list(targets or files),
            on_finished=self._on_zmodem_finished,
            parent=self,
        )
        return transfer

    def _arm_zmodem_pump(self, transfer: ZmodemTransfer) -> None:
        """引擎确认启动之后，才让这个 pump 接管字节流。

        放在 ``start_engine`` 之后调用：``_on_channel_ready`` 靠 ``_zmodem_pump``
        是否为 None 来决定「这批字节归终端还是归传输」，所以它必须**恰好**等价于
        「有一个真的在跑的 pump」。
        """
        self._zmodem_pump = transfer.pump
        self._sync_snippet_gate()

    def _on_zmodem_finished(self, code: int) -> None:
        self._zmodem_pump = None
        self._zmodem_detector.reset()
        # 通道交还给用户之前**必须**先把输入闸门打开，否则这次会话的终端
        # 从此再敲什么都没有 —— 而且界面上看不出任何异常。
        self._sync_snippet_gate()
        if self._channel is not None and not self._channel.closed:
            self._set_state("live")
            # lrzsz leaves the remote tty in raw mode and swallows the prompt;
            # a bare newline brings it back so the session is usable again.
            try:
                self._channel.sendall(b"\r\n")
            except Exception:
                pass
        QtCore.QTimer.singleShot(200, self.terminal.update)

    def _flash_status(self, msg: str) -> None:
        self.statusChanged.emit(msg, True)

    # ------------------------------------------------------------ completion
    #
    # The remote shell owns completion, and we must not fight it: Tab is
    # forwarded verbatim so bash/zsh readline behave exactly as they would in
    # any other terminal. Only when the remote stays silent (busybox sh, a
    # command that just ignores Tab, a broken prompt) do we fall back to
    # completing the word ourselves from an SFTP directory listing.
    #
    # The fallback *types* the completion -- backspaces plus the finished word
    # -- rather than repainting the screen, so the remote's own line editor
    # stays authoritative and the result looks the same on any shell.

    _COMPLETION_WAIT_MS = 300
    _WORD_CHARS = re.compile(r"[A-Za-z0-9_./~:@%+=,-]")

    def _on_tab(self) -> None:
        # 传输期间 Tab 也要被吞掉。原来这里有个 `if self._zmodem_pump is not
        # None: send(b"\t"); return` 的分支 —— 它和下一行**完全一样**，所以
        # 什么也没挡住，评审实测传输期按 Tab 会把 b"\t" 送进 ZMODEM 载荷流。
        # 现在由 TerminalWidget.set_input_blocked 统一收口（见 _sync_snippet_gate）。
        self.terminal._send(b"\t")           # let readline try first
        # One reusable timer: stop() leaves the object alive, so re-arming it
        # here is what makes a second Tab work at all.
        if self._completion_timer is None:
            self._completion_timer = QtCore.QTimer(self)
            self._completion_timer.setSingleShot(True)
            self._completion_timer.timeout.connect(self._local_complete)
        self._completion_timer.start(self._COMPLETION_WAIT_MS)

    def _cancel_completion(self) -> None:
        if self._completion_timer is not None:
            self._completion_timer.stop()

    @staticmethod
    def _split_prompt(line: str) -> tuple[str, str]:
        """Split a prompt line into ``(prompt, typed)``.

        Recognises the common ``user@host:~/path$ `` shape; anything else is
        treated as prompt-less, in which case the whole line is the word.
        """
        m = re.search(r"^(\S*?[:>]\S*?[$#]\s)(.*)$", line)
        if m:
            return m.group(1), m.group(2)
        return "", line

    @staticmethod
    def _guess_cwd(prompt: str) -> str:
        """Best-effort cwd from the prompt's ``:path$`` segment.

        Returns a POSIX path (or ``~``) — never a Windows path — so that the
        local SFTP fallback can pass it directly to the remote POSIX server.
        """
        m = re.search(r":([^$#]*)[$#]\s*$", prompt)
        if not m:
            return "~"
        path = m.group(1) or "~"
        # Do NOT expand ~ with os.path.expanduser: on Windows that resolves
        # to a local Windows path (C:\\Users\\...) and the SFTP fallback then
        # lists the local filesystem instead of the remote home directory.
        # Leave ~ as-is so posixpath.join/cwd below handles it correctly.
        return path

    def _local_complete(self) -> None:
        if self._channel is None or self._channel.closed or self._sftp is None:
            return
        line, cursor = self.terminal.input_line()
        prompt, typed = self._split_prompt(line[:cursor])

        # the token immediately before the cursor
        i = len(typed)
        while i > 0 and self._WORD_CHARS.match(typed[i - 1]):
            i -= 1
        word = typed[i:]
        if not word:
            return

        head, _, prefix = word.rpartition("/")
        cwd = self._guess_cwd(prompt)
        # posixpath, not os.path: the remote is always a POSIX host, and on
        # Windows os.path.join("C:/Users/me", "/etc") yields "C:/etc", which
        # would break completion of any absolute remote path.
        target = posixpath.join(cwd, head) if head else cwd
        if not target.startswith("/"):
            return                      # relative, and we have no base: skip

        try:
            entries = self._sftp._rfs.list_names(target)
        except TimeoutError:
            # **通道正忙要说出来。** 这里原来是 `except Exception: return`，
            # 于是「远端没这个目录」（正常）和「SFTP 通道忙」被压成同一个
            # 沉默：用户狂按 Tab、屏幕一动不动，也没有任何字告诉他为什么。
            # 两者都不是错，但只有后者值得说一句 —— 补全没做，状态栏说。
            self._flash_status("远程通道正忙，这一次补全没做", 3000)
            return
        except Exception:
            return

        matches = sorted(n for n in entries if n.startswith(prefix))
        if not matches:
            self._flash_status("没有匹配项")
            return

        if len(matches) == 1:
            done = matches[0] + ("/" if self._is_dir(target, matches[0]) else "")
            payload = ("\x7f" * self._erase_width(word)) + done
            self.terminal._send(payload.encode("utf-8"))
            return

        # several candidates: show the common prefix, then the list in the
        # status bar rather than spamming the terminal
        common = os.path.commonprefix(matches)
        if common and len(common) > len(prefix):
            payload = ("\x7f" * self._erase_width(word)) + common
            self.terminal._send(payload.encode("utf-8"))
        shown = matches[:12]
        more = f"  (+{len(matches) - len(shown)} 更多)" if len(matches) > len(shown) else ""
        self._flash_status("  ".join(shown) + more)

    def _is_dir(self, parent: str, name: str) -> bool:
        try:
            return self._sftp._rfs.is_dir(posixpath.join(parent, name))
        except Exception:
            return False

    def closeEvent(self, event):
        self._on_disconnect()
        super().closeEvent(event)
# ---------------------------------------------------------------------------
# Main window


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings: AppSettings, store: SessionStore,
                 snippet_store=None):
        super().__init__()
        self.settings = settings
        self.store = store
        # **app 级单例，不是每个标签页一份。** SessionTab 靠
        # `self.window().snippet_store` 取它（见 SessionTab.snippet_store）。
        # 片段是用户的偏好，不随连接走；做成每标签一份的话，两个标签各改各的，
        # 界面上看着像丢数据。
        #
        # **可以注入**（参数缺默时才建默认那个）：默认那个指向真实的
        # %APPDATA%：测试直接 `MainWindow(st, store)` 再 `.add()` 一条，
        # 每跑一次就往用户自己的配置里塞一条垃圾。踩过，用户的文件里一度
        # 积了 15 条同名测试片段。
        self.snippet_store = snippet_store if snippet_store is not None \
            else app_snippet_store()
        if not self.snippet_store.loaded_ok:
            log.warning("片段库没能从磁盘读出来，先用内置的：%s", self.snippet_store.path)
        # 第一帧就得带上 accent：菜单那 7 个 QAction 的图标在 _build_menu()
        # 里按 self.palette_ 烘死，而 apply_theme 的重染色循环不覆盖 QAction，
        # 这里不传，后面就再也刷不回来了。
        self._accent_applied = accent_for(settings, settings.theme)
        self.palette_ = get_palette(settings.theme, self._accent_applied)
        self._connect_inflight = False
        #: **正在等用户回答「要不要信任这台主机」的请求。**
        #: 主窗口被关时要把它们全部唤醒 —— 否则连接工作线程会在这里
        #: 干等满 120 秒（连接线程挂着不退，进程也退不干净）。
        self._host_key_asks: set = set()

        self.setWindowTitle("MavisSSH")
        self.setMinimumSize(1020, 660)
        self.resize(1280, 820)
        # 必须在 show() 之前设：show() 之后再改窗口标志会重建原生窗口，
        # 肉眼看得见一次闪烁。非 Windows 平台保持原生边框（红绿灯不能丢）。
        if frameless_enabled():
            self.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)

        # ---------------- sidebar ----------------
        # None, not False: the first set_sidebar_collapsed call has to run
        # even when the stored preference is already False.
        self._collapsed = None
        # **折叠起来的分组名，存名字不存行号。** 存集合是因为「这个分组现在
        # 折着吗」每次重建列表都要问一遍，行号会随会话增删漂移；写回配置时
        # 排序成列表，保证同一份状态存两次得到同一个文件（不然用户在版本
        # 控制里会看到一堆无意义 diff）。
        #
        # 存进 `set(...)` 时顺手丢掉空串：折叠态没有名字可记，留在里面会
        # 让「折着几个分组」这个数永远多一个。
        self._collapsed_groups = {g for g in (settings.collapsed_groups or [])
                                  if g}
        self.sidebar = QtWidgets.QFrame()
        self.sidebar.setProperty("role", "sidebar")
        self.sidebar.setFixedWidth(SIDEBAR_W)
        self._build_sidebar()

        # ---------------- workspace ----------------
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setObjectName("sessionTabs")
        # Left off deliberately: Qt would reserve width for a native close
        # glyph that is never drawn, on top of the padding the close widget
        # actually needs. tabCloseRequested is emitted by TabCloseController.
        self.tabs.setTabsClosable(False)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close)
        # Paints and places the close glyph. A stylesheet cannot: styling the
        # close-button subcontrol makes Qt drop the native icon, and a widget
        # installed with setTabButton ignores both the subcontrol margins and
        # the tab padding. See TabCloseController.
        self._tab_close = TabCloseController(self.tabs, self.palette_, self)
        # 右键菜单：关闭当前 / 关闭其他 / 关闭右侧。
        # **走的是与关闭按钮同一条路**（`tabCloseRequested`）——
        # 见 tab_context.py 的说明：两条路共用 `_on_tab_close`，
        # 所以欢迎页那个不可关闭的标签在菜单里也是灰的。
        self._tab_context = TabContextMenu(self.tabs, self.palette_, self)
        # 标签一多时两端会各留出一条原生滚动按钮，而 QSS 一旦样式化 QTabBar
        # 走的就是 QStyleSheetStyle，它不画那两根箭头 —— 原位留下两个空方块。
        # 换成自绘的 `<` / `>`。
        #
        # **每个标签栏各装一次**：`sub_tabs`（终端/文件/监控/隧道）在每个
        # `SessionTab` 里，不在 `MainWindow` 上 —— 所以 `SessionTab.__init__`
        # 那里也装一份。同一个 QSS 规则适用于所有标签栏，单修一个等于没修。
        self._tab_scroll = install_tab_scroll_buttons(self.tabs, self.palette_)

        self.welcome = WelcomePage(self.palette_)
        self.welcome.newRequested.connect(self._on_new_connection)
        self.welcome.sessionRequested.connect(self._on_welcome_session)
        self.welcome.setProperty(NO_CLOSE, True)
        self.tabs.addTab(self.welcome, "欢迎")
        self._tab_close.sync()
        # 欢迎页的「快速连接」卡片。
        self._refresh_welcome_quick()

        central = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 自绘标题栏顶到最上面，下面才是菜单栏和内容。
        # icon_name="logo"：主窗口用应用 Logo 当标记。其它地方（对话框）继续用
        # 各自的语义图标——对话框图标说的是"这个窗口干什么"。
        self.titlebar = TitleBar(self, self.palette_, central,
                                 icon_name="logo")
        outer.addWidget(self.titlebar)

        # **不用 QMainWindow 自带的 menuBar()。** 它固定画在菜单区，而菜单区
        # 在客户区之上、标题栏得压在它上面——两者没法共存。自己建一个塞进
        # 布局，顺序才是可控的。QAction 的快捷键靠 WindowShortcut 上下文
        # 生效，和菜单栏挂在哪儿无关。
        self._menu_bar = QtWidgets.QMenuBar(central)
        self._menu_bar.setObjectName("menuBar")
        outer.addWidget(self._menu_bar)

        body = QtWidgets.QWidget(central)
        lay = QtWidgets.QHBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.sidebar)

        # The tab bar is inside the QTabWidget, so the only way to keep the
        # first and last tab off the window edge is to inset the whole widget.
        # Without this the rightmost close button touches the frame.
        holder = QtWidgets.QWidget()
        holder_lay = QtWidgets.QVBoxLayout(holder)
        holder_lay.setContentsMargins(16, 12, 16, 0)
        holder_lay.setSpacing(0)
        holder_lay.addWidget(self.tabs, 1)
        lay.addWidget(holder, 1)
        outer.addWidget(body, 1)
        self.setCentralWidget(central)
        # 窗口外框 + 四角圆角。**QSS 的 border / border-radius 在这里都画不出来**
        # （central 满铺不透明，把描边和圆角一起盖光，实测 0 px 可见，见
        # window_frame.py 顶部），所以是自绘的浮层。
        # 挂在**窗口**上而不是 central：状态栏在 central 之外，挂在 central
        # 上够不到窗口底边（实测 8 个采样点只中 5 个，下边和下两角全缺）。
        # 圆角靠 `WA_TranslucentBackground` + 浮层挖角，所以**必须 show() 之前**装。
        self._frame = enable_rounded_corners(self, self.palette_)

        self._build_menu()
        # accent 必须一起传。这行在 __init__ 靠后，**不带 accent 会把上面
        # 1314 行算好的那一份抹掉**，用户存的颜色在首帧就丢了——而且
        # 不报错、不写日志，界面上只是一片默认蓝。
        self.apply_theme(settings.theme, self._accent_applied)
        # Restored after the tabs exist: set_sidebar_collapsed rebuilds the
        # session list, and that reads the live set off self.tabs.
        self.set_sidebar_collapsed(bool(settings.sidebar_collapsed),
                                   persist=False)
        self._reload_session_list()

        self.status_label = QtWidgets.QLabel("就绪")
        self.statusBar().addWidget(self.status_label)
        # 读起来别扭是有原因的：pill 必须**以属性的身份**进状态栏，
        # 建它的那次调用是唯一一次有人碰它，之后只有更新。
        self._host_pill = self._host_pill()
        self.statusBar().addPermanentWidget(self._host_pill)
        # 状态栏也说一句，且**不带时限**：会话文件的问题不会自己好起来，
        # 4 秒后自动回退成「就绪」等于把唯一的解释吞掉了。
        if getattr(self.store, "load_error", ""):
            self._flash(f"会话读不出来：{self.store.load_error}")

    def _host_pill(self) -> QtWidgets.QLabel:
        pill = _label("", "muted")
        pill.setContentsMargins(8, 0, 0, 0)
        return pill

    def _sync_host_pill(self, *_a) -> None:
        """状态栏右侧那个 pill：显示此刻**真的连着**的那台主机。

        读的是 ``_last_endpoint``（``用户名@主机:端口``），不是 ``_last_label``
        （标签标题，现在是分组名-会话名）。这两个回答的是不同的问题：标签回答
        「哪一个」，pill 回答「在哪里」。标签改成会话名之后，pill 也跟着只剩
        一个名字 —— 而它的职责从头到尾都是显示主机，两组同名会话在 pill 上
        还会撞在一起。

        **断开时必须一起清掉。** 只在连接成功那一刻写一次的话，会话掉线之后
        pill 还挂着一台早已不存在的远端，用户会以为连接还在 —— 一个永远显示
        主机名却从不更新的控件，比没有这个控件更坏。

        它同时也是 ``stateChanged`` 的接收端：那条信号在 idle/busy/live/error
        每次转换上都发，所以用户手动断开、远端 EOF 自己掉线都走同一条路，
        不用在每个断开点上补一句（补漏一处就是"pill 撒谎"的那一处）。
        """
        live = ""
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab) and w.state_kind() == "live":
                live = w._last_endpoint
                break
        self._host_pill.setText(live)

    # ------------------------------------------------------------------ sidebar

    def _build_sidebar(self) -> None:
        p = self.palette_

        # Collapse toggle. It is first in the brand row and stays at that x in
        # both states -- the one thing the 56px rail cannot afford to move,
        # because a toggle that jumps to a different corner is a toggle nobody
        # finds the second time. The logo and title follow it and go away when
        # there is no room for them.
        self._fold_btn = QtWidgets.QToolButton()
        self._fold_btn.setProperty("variant", "icon")
        _set_icon(self._fold_btn, "sidebar", p, 18, p.text_dim)
        self._fold_btn.setIconSize(QtCore.QSize(18, 18))
        self._fold_btn.setFixedSize(34, 34)
        self._fold_btn.setToolTip("折叠侧边栏 (Ctrl+B)")
        self._fold_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._fold_btn.clicked.connect(self.toggle_sidebar)

        brand = QtWidgets.QHBoxLayout()
        brand.setContentsMargins(11, 8, 11, 10)
        brand.setSpacing(9)
        self._brand_logo = QtWidgets.QLabel()
        self._brand_logo.setPixmap(
            icons.icon_pixmap("terminal", p.accent_graphic, 20, 1.9))
        self._brand_title = _label("MavisSSH", "brandTitle")
        brand.addWidget(self._fold_btn)
        brand.addWidget(self._brand_logo)
        brand.addWidget(self._brand_title)
        brand.addStretch(1)

        self._new_btn = QtWidgets.QPushButton("新建连接")
        self._new_btn.setProperty("variant", "primary")
        _set_icon(self._new_btn, "plus", p, 15, p.text_on_solid)
        self._new_btn.setIconSize(QtCore.QSize(15, 15))
        self._new_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self._new_btn.setFixedHeight(36)
        self._new_btn.clicked.connect(self._on_new_connection)

        self.session_list = QtWidgets.QListWidget()
        self.session_list.setObjectName("sessionList")
        # 4px between rows, the same gap the item's old 2px vertical margin
        # used to produce together with a 2px spacing. The margin is gone now
        # so a row pinned to setFixedHeight(58) fills its item exactly; all
        # the gap lives here, where it can never overflow a row.
        self.session_list.setSpacing(4)
        self.session_list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.session_list.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.session_list.setTextElideMode(QtCore.Qt.TextElideMode.ElideRight)
        self.session_list.itemDoubleClicked.connect(self._on_session_activated)
        self.session_list.currentRowChanged.connect(self._sync_session_row_tints)
        self.session_list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.session_list.customContextMenuRequested.connect(self._on_session_menu)
        # **回车是纯键盘用户唯一的入口。** 不能用 itemActivated：它在双击时
        # 也会发，而 itemDoubleClicked 已经接着 _on_session_activated，两条都接
        # 的话双击会开两个会话。QShortcut 只认这一颗键，没有这个副作用。
        self._list_open = QtGui.QShortcut(
            QtGui.QKeySequence(QtCore.Qt.Key.Key_Return), self.session_list)
        self._list_open.setContext(QtCore.Qt.ShortcutContext.WidgetShortcut)
        self._list_open.activated.connect(self._open_selected_session)

        self._side_edit_btn = _side_button("编辑", "pencil", "编辑选中的会话", p)
        # **必须包一层 lambda 把 `checked` 吃掉。** `QPushButton.clicked` 的签名
        # 是 `clicked(bool checked = false)`，PyQt6 恒传 `False`；而槽的签名是
        # `_on_edit_session(item=None)`，里面靠 `item is None` 分流 ——
        # `False is not None` 判**真**，于是走进 `_session_of(False)` 去调
        # `False.data(...)`，`AttributeError` 当场抛出去。
        # 后果是这两个按钮 100% 不可用（右键菜单里的同名项走 lambda 捕获
        # `item`，所以只有这两个入口坏，测试里也只测得到这两个）。
        self._side_edit_btn.clicked.connect(
            lambda _checked=False: self._on_edit_session())
        self._side_del_btn = _side_button("删除", "trash", "删除选中的会话", p)
        self._side_del_btn.clicked.connect(
            lambda _checked=False: self._on_delete_session())

        # One QBoxLayout for both states rather than two layouts holding the
        # same buttons: swapping layouts would mean re-parenting them, and
        # re-parenting drops the hover and focus state mid-click. Changing the
        # direction keeps the widgets exactly where they are.
        # 56 - 2*6 gutter = 44px of usable width in the rail, which cannot
        # hold two side-by-side buttons, so the rail stacks them instead.
        self._foot = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.Direction.LeftToRight)
        self._foot.setContentsMargins(8, 0, 8, 10)
        self._foot.setSpacing(4)
        self._foot.addWidget(self._side_edit_btn, 1)
        self._foot.addWidget(self._side_del_btn, 1)

        self._empty_hint = _label("还没有保存的会话", "muted")
        self._empty_hint.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self._empty_hint.setContentsMargins(10, 24, 10, 24)

        # 会话文件读不出来时的那句话。**必须常驻侧栏、而不是只在关窗时弹一次**：
        # 密钥不匹配的表现是「侧栏空了 + 改任何会话都不落盘」，而"真的一个
        # 会话都没有"在界面上和它长得一模一样，用户无从判断是被挡了还是文件没了。
        self._store_problem = _label("", "dim")
        self._store_problem.setWordWrap(True)
        self._store_problem.setContentsMargins(10, 0, 10, 4)
        self._store_problem.setVisible(False)

        lay = QtWidgets.QVBoxLayout(self.sidebar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addLayout(brand)
        lay.addWidget(self._new_btn, 0, QtCore.Qt.AlignmentFlag.AlignHCenter)
        # **这里原来有个「会话  N」计数标签，已按用户要求删掉。**
        # 它的信息量本来就是零：下面每一个方块就是一条会话，数一下就知道，
        # 而「会话  1」这种标签在侧栏顶端只占一行高度、不指向任何可点的东西。
        # 删除时把 `count_lab` / `_count_label` / 两处写入**连同测试里钉着它的
        # 8 条判据和 1 条元断言一起删干净** —— 留下能设但没人读的标签，
        # 等于把静态死物留成引信。
        lay.addWidget(self.session_list, 1)
        lay.addWidget(self._empty_hint)
        lay.addWidget(self._store_problem)
        lay.addLayout(self._foot)

    # ------------------------------------------------------------------ collapse

    def sidebar_collapsed(self) -> bool:
        return self._collapsed

    def toggle_sidebar(self) -> None:
        self.set_sidebar_collapsed(not self._collapsed)

    def set_sidebar_collapsed(self, collapsed: bool,
                              persist: bool = True) -> None:
        """Switch between the 268px sidebar and the 56px icon rail.

        The switch is instant on purpose. Animating the width would re-lay-out
        every session row on every frame, and QListWidget does not make that
        cheap, so the "smooth" version is the one that stutters.
        """
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed

        self.sidebar.setFixedWidth(SIDEBAR_W_COLLAPSED if collapsed else SIDEBAR_W)

        # A dynamic property, not a second stylesheet: QSS keys the tighter
        # rail gutter off it, and repolish is what makes Qt re-evaluate the
        # ::item box for this list only.
        self.session_list.setProperty("rail", "1" if collapsed else "")
        self.session_list.style().unpolish(self.session_list)
        self.session_list.style().polish(self.session_list)

        # The toggle keeps its place; only its meaning and its tooltip change.
        self._fold_btn.setToolTip("展开侧边栏 (Ctrl+B)" if collapsed
                                  else "折叠侧边栏 (Ctrl+B)")
        self._brand_logo.setVisible(not collapsed)
        self._brand_title.setVisible(not collapsed)

        # A 56px rail has room for the glyph and nothing else.
        self._new_btn.setText("" if collapsed else "新建连接")
        if collapsed:
            _pin(self._new_btn, 36, 36)
            self._new_btn.setToolTip("新建连接 (Ctrl+N)")
        else:
            _unpin_width(self._new_btn)
            self._new_btn.setFixedHeight(36)
            self._new_btn.setToolTip("")

        self._empty_hint.setVisible(not collapsed and len(self.store.all) == 0)

        self._foot.setDirection(
            QtWidgets.QBoxLayout.Direction.TopToBottom if collapsed
            else QtWidgets.QBoxLayout.Direction.LeftToRight)
        self._foot.setSpacing(8 if collapsed else 4)
        self._foot.setContentsMargins(0 if collapsed else 8, 0,
                                      0 if collapsed else 8, 10)
        for btn, text in ((self._side_edit_btn, "编辑"),
                          (self._side_del_btn, "删除")):
            # 40px cannot fit the label, so the rail shows the glyph and the
            # tooltip carries the word.
            btn.setText("" if collapsed else text)
            if collapsed:
                _pin(btn, 40, 28)
            else:
                _release(btn)          # the expanded buttons size themselves
            # A stretch factor only buys height once the box goes vertical, so
            # a 40px-wide button is laid out flush left unless it is told to
            # centre itself -- which left the rail's footer hanging off the
            # edge by 8px on one side.
            self._foot.setAlignment(
                btn, QtCore.Qt.AlignmentFlag.AlignHCenter if collapsed
                else QtCore.Qt.AlignmentFlag.AlignVCenter)

        self._sync_fold_action()
        self._reload_session_list()
        if persist:
            self.settings.sidebar_collapsed = collapsed
            self.settings.save()

    def _sync_fold_action(self) -> None:
        act = getattr(self, "_fold_action", None)
        if act is not None:
            act.setText("展开侧边栏" if self._collapsed else "折叠侧边栏")

    # ------------------------------------------------------------------ menu

    def _build_menu(self) -> None:
        bar = self._menu_bar
        # 记下这批 QAction，好让 _retint_menu_icons 之后能按新配色重画。
        # QAction 不在 QWidget 树里，findChildren 够不到，只能自己收着。
        self._menu_actions: list[QtGui.QAction] = []

        m_file = bar.addMenu("文件")
        act_new = m_file.addAction(icons.icon("plug", self.palette_, 16), "新建连接…")
        act_new.setShortcut(QtGui.QKeySequence.StandardKey.New)
        act_new.triggered.connect(self._on_new_connection)
        act_import = m_file.addAction(icons.icon("download", self.palette_, 16),
                                      "从其他终端导入…")
        act_import.triggered.connect(self._on_import_sessions)
        m_file.addSeparator()
        act_close = m_file.addAction(icons.icon("close", self.palette_, 16),
                                     "关闭当前标签")
        act_close.setShortcut(QtGui.QKeySequence.StandardKey.Close)
        act_close.triggered.connect(lambda: self._on_tab_close(self.tabs.currentIndex()))
        m_file.addSeparator()
        act_settings = m_file.addAction(icons.icon("gear", self.palette_, 16), "设置…")
        act_settings.setShortcut(QtGui.QKeySequence("Ctrl+,"))
        act_settings.triggered.connect(self._on_settings)
        m_file.addSeparator()
        act_quit = m_file.addAction(icons.icon("power", self.palette_, 16), "退出")
        act_quit.setShortcut(QtGui.QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)

        m_view = bar.addMenu("视图")
        act_theme = m_view.addAction(icons.icon("sun", self.palette_, 16), "切换主题")
        act_theme.setShortcut("Ctrl+T")
        act_theme.triggered.connect(self._toggle_theme)
        act_fold = m_view.addAction(icons.icon("sidebar", self.palette_, 16),
                                    "折叠侧边栏")
        act_fold.setShortcut("Ctrl+B")
        # The label has to follow the state, or the menu lies about what the
        # shortcut is about to do.
        act_fold.triggered.connect(self.toggle_sidebar)
        self._fold_action = act_fold
        self._sync_fold_action()

        m_help = bar.addMenu("帮助")
        act_about = m_help.addAction(icons.icon("info", self.palette_, 16),
                                     "关于 MavisSSH")
        act_about.triggered.connect(self._on_about)

        # 记下这批 QAction 及其图标名，好让 _retint_menu_icons 之后能按新
        # 配色重画。QAction 不在 QWidget 树里，findChildren 够不到。
        for _act, _ico in ((act_new, "plug"), (act_import, "download"),
                           (act_close, "close"),
                           (act_settings, "gear"), (act_quit, "power"),
                           (act_theme, "sun"), (act_fold, "sidebar"),
                           (act_about, "info")):
            _act._icon_name = _ico
        self._menu_actions = [act_new, act_import, act_close, act_settings,
                              act_quit, act_theme, act_fold, act_about]

    # ------------------------------------------------------------------ theme

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        # 外框是浮层，不跟布局。窗口一变它就得重新量一次尺寸**并抬到最上层**
        # ——否则子控件盖上来这条环就消失了，而且不报任何错。
        frame = getattr(self, "_frame", None)
        if frame is not None:
            frame.setGeometry(0, 0, self.width(), self.height())
            frame.raise_over()

    def apply_theme(self, theme: str, accent: str = "") -> None:
        self.palette_ = get_palette(theme, accent)
        # 记下来给 apply_settings 的门槛比。只比主题名的话，只改强调色的
        # 时候这里根本不会被调用，界面纹丝不动。
        self._accent_applied = accent
        p = self.palette_
        icons.clear_cache()
        # 字号也走这里：`QWidget { font-size: 13px }` 是全局基准，它一钉死，
        # 下面所有控件就都不听 `ui_font_size` 的了（实测：设置里把字号调到
        # 18pt，SFTP 列表纹丝不动）。漏传的表现是"主窗口跟着变了、某个弹窗
        # 没变"，界面上很难一眼看出是漏了哪个。
        # 属性名是 `self.settings`（1301 行就赋好了，早于这里）—— 别写成
        # `self._settings`，那是 SessionTab 上从窗口借读的别名，主窗口自己
        # 没有这个属性，而它在 `__init__` 里就被调用，缺属性会直接崩在启动。
        ui_size = self.settings.ui_font_size
        self.setStyleSheet(build_qss(p, ui_size=ui_size))
        # 外框是自绘的浮层，**不跟 QSS**（那条路实测 0 px 可见），
        # 所以换主题要自己重刷一次颜色。
        if getattr(self, "_frame", None) is not None:
            self._frame.apply_palette(p)
            self._frame.raise_over()
        # 底板是自绘的，**不跟 QSS**（半透明窗口的根底色那条规则管不到它，
        # 实测把底板 hide 掉和留着，弹框透明行数一模一样），所以换主题要自己换。
        backer = getattr(self, "_mavisBacker", None)
        if backer is not None:
            backer.apply_palette(p.bg_base)
        # 记一份"当前配色"给弹窗用。QMessageBox / QFileDialog / QInputDialog
        # 都是在各自调用点临时 new 出来的，没有构造参数能塞 palette——不给
        # 它们记一份，就只能逐个调用点手传，那正是会漏的地方。
        qss.set_current(p, ui_size)
        # **但模块级全局只是兜底，不是首选。** 它只有一个槽位：同进程里开
        # 两个主题不同的主窗口时，后 `apply_theme` 的那个会覆盖先前的，
        # 于是**先开的那个窗口之后弹出的任何对话框都会用另一个窗口的配色**。
        # 所以同时把配色挂在这个窗口自己身上，`msgbox` 优先从父窗口取。
        # （崩溃兜底那种没有父窗口的框才真的只能靠全局。）
        self.setProperty(_PALETTE_PROP, p)
        # 任务栏 / Alt-Tab 的图标跟**主题**走，但**不跟自选强调色**走。
        # 理由：窗口内的品牌 logo 用的是 p.accent，任务栏图标如果用带 accent
        # 的那一份，用户会同时看到两个同款 logo 一个新一个旧，比都跟着更割裂。
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setWindowIcon(logo.window_icon(get_palette(theme)))
        # The close glyph is painted with palette colours, so the buttons have
        # to be re-pointed at the new palette and re-placed against the new
        # metrics.
        self._tab_close.set_palette(p)
        # 标签栏两端的 `<` / `>` 同样是画上去的，不跟上就会留一条旧主题的
        # 颜色（深浅两套 text_dim 差得很远，肉眼一眼能看出来）。
        self._tab_scroll.set_palette(p)
        # 右键菜单的图标也是按主题画的（菜单每次弹出才建，所以只要换掉
        # 存着的那份 palette 就行）。
        self._tab_context.set_palette(p)
        # 否则深浅切换之后顶上留一条深色的窄条。
        if getattr(self, "titlebar", None) is not None:
            self.titlebar.apply_palette(p)

        # re-tint everything that carries baked-in colors
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab):
                w.apply_palette(p)
            elif isinstance(w, WelcomePage):
                w.repaint_theme(p)
        # Sidebar chrome. Re-pointed through the real references rather than
        # findChild(): findChild(QPushButton) returns whichever button the
        # object tree happens to walk first, and the side buttons used to keep
        # their old-palette icons entirely -- the loop that was supposed to
        # re-tint them had an empty body.
        # 走 _retheme_icon_button：图标名由按钮自己记住，不在这里手抄一份。
        # 手抄过一次，漏改的按钮在 Ctrl+T 时会悄悄变回旧图标。
        _retheme_icon_button(self._fold_btn, p, p.text_dim)
        _retheme_icon_button(self._new_btn, p, p.text_on_solid)
        _retheme_icon_button(self._side_edit_btn, p)
        _retheme_icon_button(self._side_del_btn, p)
        self._brand_logo.setPixmap(
            icons.icon_pixmap("terminal", p.accent_graphic, 20, 1.9))
        # **菜单图标之前没被重染色过。** 它们的 QAction 图标在
        # _build_menu() 里按当时的 self.palette_ 烘死，而上面那个循环只走
        # 按钮和 QLabel，够不到 QAction —— 换强调色之后菜单里会留一排默认蓝。
        self._retint_menu_icons()
        self._retint_tab_icons()
        self._reload_session_list()

    def _retint_menu_icons(self) -> None:
        """菜单 QAction 的图标按当前配色重画。

        QAction 的图标是构造时定下的，:func:`_retheme_icon_button` 够不到它
        （它只处理 QAbstractButton）。不在这儿补一遍，换强调色之后菜单会留
        着一排旧颜色。
        """
        p = self.palette_
        for act in self._menu_actions:
            _retheme_icon_action(act, p)

    def _retint_tab_icons(self) -> None:
        """标签 tab 图标按当前 accent 重画。

        和菜单 QAction 是同一个坑的另一半：``setTabIcon`` 只在建标签那一刻调
        过一次，图标被按当时的 palette_ 烘进 ``QTabBar``；而上面那个换主题的
        循环只对 QWidget 调 ``apply_palette``，够不到 QTabBar。菜单那边有
        ``_retint_menu_icons`` 补救，标签这边原来没有，于是切一次深浅，
        标签上留的是旧强调色的图标（自选强调色时尤其明显）。
        """
        p = self.palette_
        icon = icons.icon("server", p, 15, color=p.accent)
        for i in range(self.tabs.count()):
            if isinstance(self.tabs.widget(i), SessionTab):
                self.tabs.setTabIcon(i, icon)

    def _toggle_theme(self) -> None:
        self.settings.theme = "light" if self.settings.theme == "dark" else "dark"
        self.settings.save()
        # accent 必须一起传。漏掉的话 Ctrl+T 会把用户挑的强调色换回默认色，
        # 而界面上完全看不出是谁干的。
        self.apply_theme(self.settings.theme,
                          accent_for(self.settings, self.settings.theme))

    # ----------------------------------------------------------------- settings

    def _on_settings(self) -> None:
        edited = open_settings(self, self.settings, self.palette_)
        if edited is None:
            return          # cancelled -- leave everything as it was
        # apply_settings adopts, so it has to run before the save; the other
        # way round persists the *old* settings.
        self.apply_settings(edited)
        self.settings.save()

    def apply_settings(self, settings: AppSettings) -> None:
        """Adopt *settings* and push them at everything already on screen."""
        # The docstring said "adopt" and the body did not, so the only thing
        # keeping self.settings in sync was the caller happening to assign it
        # first. Adopt it here and _on_settings gets simpler.
        self.settings = settings
        accent = accent_for(settings, settings.theme)
        # **两个都要比。** 原来只有主题名，于是只改强调色时 QSS 根本不重建：
        # 设置写了、界面纹丝不动，用户看着像设置没生效。
        if (settings.theme != self.palette_.name
                or accent != self._accent_applied):
            self.apply_theme(settings.theme, accent)
        # The UI font is the application font, so it re-flows every widget
        # including the ones that are not session tabs.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setFont(ui_font(settings.ui_font_size, settings.ui_font_family))
        # The close glyph is placed against the tab's font metrics, and those
        # just moved -- without this the buttons keep the old tab's geometry.
        self._tab_close.set_palette(self.palette_)
        # `<` / `>` 是自绘的字形，字号跟着应用字体走，字体换了必须重画。
        self._tab_scroll.set_palette(self.palette_)
        self._tab_context.set_palette(self.palette_)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab):
                w.apply_settings(settings)
        # **先看有没有真存下去，再报喜。** `save()` 原来返回 None，而它在
        # `load_error` 非空时会静默 return —— 于是这句「设置已保存」无条件
        # 先亮，用户以为存了，下次启动全部回退，要到关窗口时才被解释一句。
        if self.settings.save():
            self._flash("设置已保存", 2000)
        else:
            self._flash("设置没存上：读不出来的配置不会被覆盖（详情见日志）", 6000)

    def _on_about(self) -> None:
        # 版本号从包上读，不要在这里再写一份字面量（原来这里和 `main.py` 各写
        # 一份、与 `__init__.py` 又不一致，用户在「关于」里看到的版本和实际
        # 构建出来的包对不上）。
        msgbox.about(self, __version__, self.palette_)

    # ------------------------------------------------------------------ sessions

    def _sync_store_problem(self) -> None:
        """把 ``SessionStore.load_error`` 那句话摆在侧栏上（为空就收起）。

        **不在关窗时再弹一次模态框。** ``closeEvent`` 已经为
        ``AppSettings.load_error`` 弹过一次；同一类故障弹两遍就是打扰，
        而用户真正缺的那句解释只有这里说得到 —— 关窗时他看不到侧栏。
        """
        why = getattr(self.store, "load_error", "") or ""
        self._store_problem.setText(why)
        self._store_problem.setVisible(bool(why) and not self._collapsed)

    def _reload_session_list(self) -> None:
        current = self.session_list.currentItem()
        keep = current.data(QtCore.Qt.ItemDataRole.UserRole) if current else None
        self.session_list.clear()
        p = self.palette_
        collapsed = self._collapsed
        height = ROW_H_COMPACT if collapsed else ROW_H
        live = self._live_session_ids()
        # **折叠态不画分组头。** 56px 只放得下一个 40px 方块（见
        # CompactSessionItem），塞一行分组名进去必然把方块挤出自己的 item ——
        # 那正是本文件处理过两次的行溢出。而窄条本来就只剩头像+状态点，
        # 分组这件事在展开态看一眼就够了。
        groups = ([(None, self.store.all)] if collapsed
                  else grouped_sessions(self.store.all))
        for group, rows in groups:
            if group is not None:
                folded = group in self._collapsed_groups
                self._add_group_header(group, len(rows), expanded=not folded)
                if folded:
                    # **整段跳过，只留那个头。** 会话行不建 —— 建了再 hide 是
                    # 另一种做法，但那样 `setItemWidget` 里的控件仍然占着
                    # item，方向键和 Ctrl+T 会在看不见的行之间跳。
                    continue
            for s in rows:
                item = QtWidgets.QListWidgetItem()
                item.setData(QtCore.Qt.ItemDataRole.UserRole, s.id)
                # Height only. The width used to be baked in from
                # viewport().width(), which on the first build runs before the
                # window is laid out and therefore stores a stale value --
                # and visualItemRect then reports that stale width forever after.
                # setItemWidget sizes the row to the viewport regardless.
                item.setSizeHint(QtCore.QSize(0, height))
                item.setToolTip(f"{s.display_name()}  ·  {s.host}:{s.port}")
                self.session_list.addItem(item)
                row = (CompactSessionItem(s, p) if collapsed
                       else SessionItem(s, p))
                if collapsed:
                    row.set_live(s.id in live)
                # The ::item rule has no vertical padding or margin any more, so
                # the row fills its item exactly and this height is the whole
                # story -- bump one without the other and the row clips.
                row.setFixedHeight(height)
                self.session_list.setItemWidget(item, row)
        n = len(self.store.all)
        self._empty_hint.setVisible(n == 0 and not collapsed)
        self._sync_store_problem()
        if keep:
            for i in range(self.session_list.count()):
                it = self.session_list.item(i)
                if it.data(QtCore.Qt.ItemDataRole.UserRole) == keep:
                    self.session_list.setCurrentItem(it)
                    break
        self._sync_session_row_tints()
        # 侧栏变了，欢迎页的「快速连接」卡片也得跟着变 —— 两者是同一份数据的
        # 两个出口，不同步的话会看到「刚删掉的会话还在欢迎页上」。
        self._refresh_welcome_quick()

    def _refresh_welcome_quick(self) -> None:
        """刷新欢迎页的「快速连接」卡片。"""
        welcome = getattr(self, "welcome", None)
        if welcome is None:
            return
        try:
            sessions = list(self.store.all)
        except Exception:      # noqa: BLE001 —— 卡片刷不出来不该拖垮整个界面
            sessions = []
        welcome.set_sessions(sessions)

    def _on_welcome_session(self, sid: str) -> None:
        """欢迎页「快速连接」里点了某条会话。"""
        s = self.store.get(sid)
        if s is None:
            return
        self.open_session(s)

    def _add_group_header(self, group: str, count: int,
                          expanded: bool = True) -> None:
        """在列表里插一行分组标题。

        **这一行必须不可选中，而且 `UserRole` 必须是 `None`。** 两者各挡一类
        故障：`UserRole` 是所有「按行取会话」的入口（激活、右键、删除确认）
        的钥匙，填一个假 id 会让它们去 store 里查一个不存在的会话；flags 只
        留 `ItemIsEnabled` 则让 Qt 根本不把它当成选择目标，于是方向键、回车、
        Ctrl+T 全都跨不过去。少了任何一个，症状都是「按回车打开了一个叫
        生产 的会话」或者「选中分组头之后侧栏底部的编辑/删除按钮全废」。

        现在这一行**还能点**（切换折叠），所以 tooltip 必须把「能点什么」
        说清楚 —— 它不可选中，用户点它时选中态不会变，很容易以为没点上。
        """
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.ItemDataRole.UserRole, None)
        item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
        item.setSizeHint(QtCore.QSize(0, GROUP_H))
        item.setToolTip("分组「%s」· %d 个会话 · %s"
                        % (group, count, "点击折叠" if expanded else "点击展开"))
        self.session_list.addItem(item)
        head = GroupHeaderItem(group, count, self.palette_, expanded=expanded)
        head.setFixedHeight(GROUP_H)
        head.toggled.connect(self._toggle_group)
        self.session_list.setItemWidget(item, head)

    def _toggle_group(self, group: str) -> None:
        """折叠 / 展开一个分组。

        **整段重建列表，而不是让控件自己藏起来。** 这一行是 `setItemWidget`
        装进去的，列表一 `clear()` 它连同里面所有控件一起没了；与其在两个
        地方各留一份「哪些行该显示」的判断（这里算一次、那边算一次），不如
        只留 `_reload_session_list` 这一处。代价是选中态会按
        `UserRole` 回填 —— 而分组头不可选中，回填不到就是没有选中，不会选到
        别的行上去。

        **写回配置时排序**：集合的迭代顺序跟着哈希走，同一份折叠状态存两次
        会写出两种排列，用户在版本控制里看到的是一堆无意义 diff。
        """
        if group in self._collapsed_groups:
            self._collapsed_groups.discard(group)
        else:
            self._collapsed_groups.add(group)
        self.settings.collapsed_groups = sorted(self._collapsed_groups)
        self.settings.save()
        self._reload_session_list()

    def _sync_session_row_tints(self, *_a) -> None:
        """Tell every row whether it is the current one.

        The endpoint label has to step from ``text_muted`` up to ``text_dim``
        while its row is selected -- ``text_muted`` on the selected fill is
        4.29/4.33, under the 4.5 that 11px body text needs. See
        ``SessionItem.set_selected`` for why this cannot be done in QSS.
        """
        row = self.session_list.currentRow()
        for i in range(self.session_list.count()):
            w = self.session_list.itemWidget(self.session_list.item(i))
            setter = getattr(w, "set_selected", None)
            if setter is not None:
                setter(i == row)

    def _live_session_ids(self) -> set:
        """Ids of the sessions that have a live tab right now.

        Only the 56px rail draws this -- with 268px there is room for the
        name, and the tab bar already shows what is open.
        """
        out = set()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab) and w.state_kind() == "live":
                saved = getattr(w, "_last_saved", None)
                if saved is not None:
                    out.add(saved.id)
        return out

    def _refresh_rail_state(self) -> None:
        """Re-mark the live squares after a tab connects or drops."""
        if not self._collapsed:
            return
        live = self._live_session_ids()
        for i in range(self.session_list.count()):
            it = self.session_list.item(i)
            row = self.session_list.itemWidget(it)
            if isinstance(row, CompactSessionItem):
                row.set_live(it.data(QtCore.Qt.ItemDataRole.UserRole) in live)

    def _selected_session(self) -> SavedSession | None:
        item = self.session_list.currentItem()
        if not item:
            return None
        return self.store.get(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def persist_session(self, saved: SavedSession) -> None:
        """Write a mutated session back to the store and refresh the list.

        Called by panels that own per-session state (currently the tunnel
        rules) so changes made while connected outlive the tab. The session
        keeps its id, so this updates in place rather than creating a copy.
        """
        if saved is None or not saved.id:
            return
        try:
            self.store.upsert(saved)
        except Exception:  # noqa: BLE001
            # Persistence is best-effort: losing it must not take down a
            # working session, and the rule set stays live in the tab.
            self._flash("会话配置保存失败", 4000)
            return
        self._reload_session_list()

    def _on_session_activated(self, item: QtWidgets.QListWidgetItem) -> None:
        s = self.store.get(item.data(QtCore.Qt.ItemDataRole.UserRole))
        if s is not None:
            self.open_session(s)

    def _open_selected_session(self) -> None:
        """回车打开当前选中行。

        走 :meth:`_selected_session` 而不是把 ``currentItem()`` 递下去：
        没有选中行时它是 None，那条路第一行就 AttributeError —— 而这正是
        「按了回车整个窗口报错」最容易复现的场景。
        """
        s = self._selected_session()
        if s is not None:
            self.open_session(s)

    def _session_of(self, item) -> SavedSession | None:
        """按**指定的行**取会话，而不是"当前选中行"。

        这两件事在右键菜单里不是一回事：右键**不会**改选中行。原来菜单上的
        「编辑…」「删除」直接连到不带参数的方法，走 `_selected_session()`，
        于是「选中 A、右键 B、点删除」**删掉的是 A** —— 而且是静默的、
        没有撤销。菜单里只有「连接」用 lambda 把 `item` 捕获了下来，
        另外两项漏了，于是正好只有这两项出错。
        """
        if item is None:
            return None
        return self.store.get(item.data(QtCore.Qt.ItemDataRole.UserRole))

    def _on_session_menu(self, pos: QtCore.QPoint) -> None:
        item = self.session_list.itemAt(pos)
        if not item:
            return
        # **分组头行没有会话，右键它就该什么都不弹。**
        # 原来这里直接 `self.store.get(None)` 拿不到会话，菜单却照弹三项，
        # 而菜单里的「编辑…」「删除」不带参数、走的是「当前选中行」。
        # 右键**不会**改选中行 —— 所以「选中 A、右键分组头、点删除」
        # 删掉的是 A，还弹着一句「确定删除「A」？」，用户完全看不出
        # 自己点的是哪一行。「连接」那一项反而无事发生，因为它捕获了 item
        # 且判了 `is not None` —— 三项里唯一不出错的，也是唯一不会提醒
        # 用户「你点错了」的那一项。
        if item.data(QtCore.Qt.ItemDataRole.UserRole) is None:
            return
        menu = QtWidgets.QMenu(self)
        act = menu.addAction(icons.icon("plug", self.palette_, 16), "连接")
        act.triggered.connect(lambda: self._on_session_activated(item))
        menu.addAction(icons.icon("pencil", self.palette_, 16), "编辑…").triggered.connect(
            lambda: self._on_edit_session(item))
        menu.addAction(icons.icon("trash", self.palette_, 16), "删除").triggered.connect(
            lambda: self._on_delete_session(item))
        menu.exec(self.session_list.mapToGlobal(pos))

    def _known_groups(self) -> list[str]:
        """已有的分组名，给连接对话框做补全用。

        **现算，不缓存。** 缓存要多一处失效点（编辑分组、删除分组、导入），
        而这里一次调用就是一个补全列表，一行排序而已。
        """
        return sorted({str(s.group).strip() for s in self.store.all
                       if str(s.group).strip()})

    def _on_new_connection(self) -> None:
        dlg = ConnectDialog(self, palette=self.palette_,
                            groups=self._known_groups())
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        data = dlg.result_dict()
        s = SavedSession(**{k: v for k, v in data.items() if k in SavedSession.__dataclass_fields__})
        if not s.name:
            s.name = f"{s.username}@{s.host}"
        self.store.upsert(s)
        self.settings.last_session_id = s.id
        self.settings.save()
        self._reload_session_list()
        self.open_session(s)

    def _on_import_sessions(self) -> None:
        """把别的 SSH 客户端里保存的连接导进来。**密码一条都不导。**

        这不是省事，是搬不动：PuTTY/WinSCP 压根不存密码，Xshell 用 DPAPI，
        MobaXterm 是 AES + 本机 Windows 凭据，FinalShell 是 DES/AES —— 没有
        一个能跨机解密。而去逆向别家客户端的密码加密在合规上也不干净。
        导进来的会话一律是「密码登录」，用户第一次连的时候填一次。

        找不到来源时**说清楚查了哪儿**，而不是静悄悄地导入 0 条：这些客户端
        几乎都允许改配置路径，猜错一个位置就什么都读不到。
        """
        srcs = [s for s in importer.sources() if s.candidates]
        if not srcs:
            msgbox.information(
                self, "导入连接",
                "在常见位置没有找到可以导入的连接。\n\n已查找：\n"
                "· %s\n· PuTTY 注册表 HKCU\\Software\\SimonTatham\\PuTTY\\Sessions\n"
                "· WinSCP 注册表 HKCU\\Software\\Martin Prikryl\\WinSCP 2\\Sessions\n\n"
                "如果你用的是别的客户端，或者把配置挪过位置，"
                "请手动新建连接。" % importer.default_ssh_config_path(),
                palette=self.palette_)
            return
        picked = msgbox.multi_choice(
            self, "导入连接", "选择要导入的来源：",
            ["%s（%d 条）" % (s.label, len(s.candidates)) for s in srcs],
            palette=self.palette_,
            informative="只导入主机、端口、用户名和私钥路径；**密码不会导入**，"
                        "导入后需要你重新填写一次。")
        if not picked:
            return
        cands: list = []
        for i in picked:
            cands.extend(srcs[i].candidates)
        added, skipped = importer.merge(self.store, cands)
        self._reload_session_list()
        if not added:
            msgbox.information(
                self, "导入连接",
                "没有导入任何连接 —— 选中的 %d 条都已经在列表里了。"
                % skipped, palette=self.palette_)
            return
        lines = ["已导入 %d 条连接。" % len(added)]
        if skipped:
            lines.append("跳过 %d 条：地址和用户名已经存在。" % skipped)
        lines.append("\n导入的连接都是**密码登录**（各家客户端的密码都用了"
                     "本机凭据加密，搬不过来），首次连接时填一次即可。")
        msgbox.information(self, "导入连接", "\n".join(lines),
                           palette=self.palette_)

    def _on_edit_session(self, item: QtWidgets.QListWidgetItem = None) -> None:
        s = self._session_of(item) if item is not None else self._selected_session()
        if s is None:
            msgbox.information(self, "编辑", "请先在左侧选择一个会话。")
            return
        dlg = ConnectDialog(self, initial=s.to_dict(), palette=self.palette_,
                            groups=self._known_groups())
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        fields = SavedSession.__dataclass_fields__
        new = SavedSession(id=s.id, **{k: v for k, v in dlg.result_dict().items() if k in fields and k != "id"})
        self.store.upsert(new)
        self._reload_session_list()

    def _on_delete_session(self, item: QtWidgets.QListWidgetItem = None) -> None:
        s = self._session_of(item) if item is not None else self._selected_session()
        if s is None:
            # **和「编辑」那一句对称。** 原来这里是光秃秃一个 return：同样
            # 「没选中」的状态，编辑会告诉你去选一个，删除什么都不说 ——
            # 用户只会以为按钮坏了。
            msgbox.information(self, "删除", "请先在左侧选择一个会话。")
            return
        # 用**全名**而不是 name：分组功能一上，两边各有一个叫「Web-01」
        # 就是常事，而这里恰恰是「不可撤销」的那一步。确认框里只写
        # 「确定删除「Web-01」？」时，用户没有任何办法确认自己删的是哪一组。
        name = s.display_name()
        if msgbox.question(self, "删除会话", f"确定删除「{name}」？") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.store.delete(s.id)
        self._reload_session_list()

    # ------------------------------------------------------------------ connect

    def open_session(self, s: SavedSession) -> None:
        if self._connect_inflight:
            self._flash("正在连接中，请稍候…")
            return
        cfg = AuthConfig(
            host=s.host,
            port=s.port,
            username=s.username,
            password=s.password or None,
            key_filename=s.key_filename or None,
            key_passphrase=s.key_passphrase or None,
        )
        self._connect_inflight = True
        self._flash(f"正在连接 {s.username}@{s.host}:{s.port} …")

        def worker() -> None:
            try:
                # **把主机密钥确认回调传进去。** 未知主机时 paramiko 会在
                # **这个工作线程上**同步调它（`missing_host_key`），所以它
                # 自己不能弹控件 —— 只能把问题送到主线程去问。
                conn = SSHConnection.connect(cfg, host_key_prompt=self._ask_host_key)
            except Exception as exc:
                # **投之前先看主窗口还在不在** —— 连接尝试最长 15 秒
                # （cfg.timeout），这期间用户可能关掉了整个窗口。
                # 详见 thread_post.py。
                thread_post.post(
                    self, "_on_connect_failed",
                    QtCore.Q_ARG(str, str(exc)),
                    QtCore.Q_ARG(str, type(exc).__name__),
                    QtCore.Q_ARG(object, s),
                )
                return
            thread_post.post(
                self, "_on_connect_ok",
                QtCore.Q_ARG(object, conn), QtCore.Q_ARG(object, s),
            )

        threading.Thread(target=worker, daemon=True).start()

    @QtCore.pyqtSlot(object, object)
    def _on_connect_ok(self, conn: SSHConnection, saved: SavedSession) -> None:
        self._connect_inflight = False
        # **标签写「分组名-会话名」，不写 `用户名@主机:端口`。**
        # 标签栏宽度有限，而每个标签都挂着一段地址，用户要靠对照才分得清
        # 「哪个标签是哪台机器」；会话名是他自己起的、对应的是那台机器在
        # 业务里的名字。地址没丢，它还在侧栏那一行里。
        label = saved.display_name()
        endpoint = f"{conn.info.username}@{conn.info.host}:{conn.info.port}"
        tab = SessionTab(self)
        tab._last_saved = saved
        tab._last_label = label
        tab._last_endpoint = endpoint
        tab.apply_palette(self.palette_)
        tab.apply_settings(self.settings)
        tab.statusChanged.connect(lambda m, _b: self._flash(m, 4000))
        tab.stateChanged.connect(self._refresh_rail_state)
        tab.stateChanged.connect(self._sync_host_pill)

        idx = self.tabs.addTab(tab, label)
        self.tabs.setTabIcon(idx, icons.icon("server", self.palette_, 15,
                                             color=self.palette_.accent))
        self._tab_close.sync()
        self.tabs.setCurrentIndex(idx)
        tab.attach(conn)
        if tab.state_kind() == "error":
            # **开终端失败就把标签撤掉，别留一个永远打不开的残骸。**
            # 留着的话用户点「重新连接」会**再开一个新标签**（`_on_reconnect`
            # 走的是 `reopen_session`），几次之后标签栏里全是连不上的死标签。
            # 回收走用户点关闭那同一条路径，`_on_tab_close` 里的 deleteLater
            # 也才是同一条。conn 由 `attach` 自己关掉，这里不重复。
            self._on_tab_close(idx)
            self._sync_host_pill()
            return
        self._sync_host_pill()
        # **连接成功时把主机指纹也报出来。** 原来 `ConnectionInfo.fingerprint`
        # 算完了只进日志（`connection.py` 的 `log.info`），界面上从来看不见 ——
        # 于是「这台主机是��一台」在用户眼里根本不存在，密钥变更更是无从
        # 发现。指纹进状态栏一眼可见，密钥变了用户才有东西可对照。
        fp = getattr(conn.info, "fingerprint", "") or ""
        if fp:
            self._flash(f"已连接到 {label} · 指纹 {fp[:19]}…", 6000)
        else:
            self._flash(f"已连接到 {label}", 4000)

    # **签名必须写在一行。** `tests/_fwin.py` 有一条判据量的是
    # 「`_on_connect_ok` / `_on_connect_failed` 的**函数第一行**都有
    # `_connect_inflight = False`」—— 它的取法是「跳过 def 行、跳过 docstring
    # 后的第一条语句」。签名一旦换行，`getsource` 里 `def` 那行后面紧跟的是
    # **续行**（`saved=None) -> None:`），它既不是 docstring 也不是注释，
    # 于是被当成第一条语句 → 判据判负。
    # 这不是判据写得脆，是「把复位放在第一行」这条性质要求第一行真的能
    # 被认出来 —— 续行会插在中间。
    @QtCore.pyqtSlot(str, str, object)
    def _on_connect_failed(self, msg: str, exc_type: str = "", saved=None) -> None:
        self._connect_inflight = False
        self._flash("连接失败", 4000)

        # **四类分开说，因为下一步动作完全不同。**
        # 混成一句「无法建立 SSH 连接」的话，用户只能靠猜 —— 而这四类
        # 分别要他去查网络、改口令、问管理员、或核对指纹。
        #
        #   主机密钥变了  → 核对指纹（可能是中间人）
        #   认证失败      → 改口令
        #   服务器拒绝    → 问管理员
        #   网络不可达    → 查网络/防火墙
        if exc_type == "SSHHostKeyChangedError":
            box = msgbox._build(self, QtWidgets.QMessageBox.Icon.Warning,
                                "主机密钥已变更", "这台主机的密钥与之前记录的不一致",
                                self.palette_, informative=msg)
            box.exec()
            return

        if exc_type == "SSHServerRefusedError":
            box = msgbox._build(self, QtWidgets.QMessageBox.Icon.Critical,
                                "服务器拒绝连接", "服务器有响应，但拒绝了这次连接",
                                self.palette_, informative=msg)
            box.exec()
            return

        # **用户拒绝接受主机密钥** —— 不是「网络不可达」。主机是通的、
        # 密钥也对得上，只是用户点了拒绝。这个现象**恰恰是中间人攻击
        # 想让用户看到的**，写成「请检查网络和防火墙」会把人带偏。
        if exc_type == "SSHHostKeyRejectedError":
            box = msgbox._build(self, QtWidgets.QMessageBox.Icon.Warning,
                                "已取消连接", "您没有接受这台主机的主机密钥",
                                self.palette_, informative=msg)
            box.exec()
            return

        if exc_type == "SSHNetworkError":
            box = msgbox._build(self, QtWidgets.QMessageBox.Icon.Critical,
                                "连不上", "无法连接到这台主机",
                                self.palette_, informative=msg)
            box.exec()
            return

        # **认证失败要能当场重输口令。**
        # 原来这里只弹一个「连接失败」，而 ``open_session`` 直接复用
        # ``s.password`` —— 用户点「重新连接」走的是同一个错口令，于是陷入
        # 「点重连 → 再失败」的循环，**没有任何地方告诉他「口令不对，请去
        # 编辑会话改口令」**。
        if exc_type == "SSHAuthError":
            if saved is not None and self._retry_auth(saved, msg):
                return
            box = msgbox._build(self, QtWidgets.QMessageBox.Icon.Critical,
                                "认证失败", "用户名或口令不对",
                                self.palette_, informative=msg)
            box.exec()
            return

        box = msgbox._build(self, QtWidgets.QMessageBox.Icon.Critical,
                            "连接失败", "无法建立 SSH 连接", self.palette_,
                            informative=msg)
        box.exec()

    def _retry_auth(self, saved, msg: str) -> bool:
        """认证失败后弹编辑框让用户重输口令，存回去并重连。

        返回 True 表示「已经重连了」，调用方不用再弹那个「连接失败」。
        """
        dlg = ConnectDialog(self, initial=saved.to_dict(), palette=self.palette_,
                            groups=self._known_groups())
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return False
        fields = SavedSession.__dataclass_fields__
        new = SavedSession(id=saved.id,
                           **{k: v for k, v in dlg.result_dict().items()
                              if k in fields and k != "id"})
        self.store.upsert(new)
        # 口令变了才值得自动重连；没改的话再试一次是白试。
        if new.password == saved.password and not new.key_passphrase:
            return False
        self.open_session(new)
        return True

    # ------------------------------------------------------- 主机密钥确认

    def _ask_host_key(self, req) -> str:
        """「这台主机我��认识，要不要信？」—— **在连接工作线程上被调用**。

        paramiko 的 ``MissingHostKeyPolicy.missing_host_key()`` 是**同步**回调，
        它跑在 :meth:`open_session` 起的工作线程里。而 Qt 控件只能在 GUI
        线程操作，**在工作线程里弹 QMessageBox 会拉起一个脱离主线程的嵌套
        事件循环** —— 那种弹窗的绘制、模态行为、焦点全都不对。

        所以这里是**两阶段**：

        1. 工作线程把请求 ``invokeMethod`` 到主线程，然后自己
           ``Event.wait()`` 阻塞；
        2. 主线程弹框，用户选完把决定写回并 ``Event.set()``，
           工作线程醒来继续握手。

        **用 ``QueuedConnection`` 而不是 ``BlockingQueuedConnection``** ——
        后者要求目标线程等当前线程处理完队列，而当前线程正是**在等**
        这个调用完成，直接死锁。

        **这里只许调 ``invokeMethod`` 和 ``log``，不许碰任何控件。**

        返回 :data:`hostkeys.TRUST` / :data:`hostkeys.TRUST_ONCE` /
        :data:`hostkeys.REJECT`。
        """
        ask = _HostKeyAsk(req)
        # **投之前先看主窗口还在不在。** 这一步跑在**连接工作线程**上，
        # 而主窗口可能已经被关掉 —— 那时 `invokeMethod(self, ...)` 会抛
        # 「wrapped C/C++ object of type MainWindow has been deleted」。
        # 详见 thread_post.py。
        #
        # 投不出去就直接回「拒绝」：没人能回答「要不要信」时，
        # 绝不能替他信任。
        if not thread_post.post(self, "_on_host_key_ask",
                                QtCore.Q_ARG(object, ask)):
            return hostkeys.REJECT
        # 记下这一个是**在等的** —— 主窗口被关时要把它们全部唤醒，
        # 否则 worker 会在这里干等满 120 秒（连接线程挂着不退）。
        self._host_key_asks.add(ask)
        try:
            if not ask.event.wait(timeout=_HOST_KEY_ASK_TIMEOUT):
                # 用户一直没点 —— 不能把工作线程永久挂在这儿（连接卡死 + 线程泄漏）。
                # 按「拒绝」处理：宁可连不上，也不要在没人应答的情况下替他信任。
                log.warning("主机密钥确认超时，按拒绝处理  %s", req.hostname)
                return hostkeys.REJECT
        finally:
            self._host_key_asks.discard(ask)
        if not ask.accept:
            return hostkeys.REJECT
        if ask.once:
            return hostkeys.TRUST_ONCE
        return hostkeys.TRUST

    @QtCore.pyqtSlot(object)
    def _on_host_key_ask(self, ask) -> None:
        """主线程侧：弹框问用户。"""
        req = ask.req
        try:
            # **用 HostKeyDialog**（不是标准 QMessageBox）—— 它多两样东西：
            # 「复制指纹」按钮，和「手输管理员给的指纹自动比对」的输入框。
            # 用户拿到的指纹通常来自工单/IM，**能贴进去比对**才谈得上
            # 「核对」；只有一个只读文本框的话，用户得靠眼睛一行行对。
            dlg = hostkey_dialog.HostKeyDialog(req, self.palette_, self)
            dlg.exec()
            decision = dlg.decision()
            ask.accept = decision in (hostkey_dialog.TRUST,
                                      hostkey_dialog.TRUST_ONCE)
            ask.once = (decision == hostkey_dialog.TRUST_ONCE)
        except BaseException as exc:      # noqa: BLE001 —— 绝不能让 UI 线程的异常把 worker 永久挂住
            log.error("主机密钥确认框出错：%s: %s", type(exc).__name__, exc)
            ask.accept = False
        finally:
            # ⚠ **必须在 finally 里 set。** UI 线程上任何异常（包括上面
            #   那个 except 之外的）都会让工作线程永远等下去 —— 连接挂死
            #   且线程泄漏。
            ask.event.set()

    def reopen_session(self, saved: SavedSession) -> None:
        self.open_session(saved)

    def _on_tab_close(self, idx: int) -> None:
        w = self.tabs.widget(idx)
        if w is None or w is self.welcome:
            return
        # **close() 只是发 closeEvent，QWidget 不保证对象被销毁。** 评审实测：
        # 关掉 20 个标签之后 SessionTab=20、片段面板=20、顶层窗口=20，
        # 原样存活 —— 光 close 面板等于只是把它藏起来。
        # removeTab 之后必须 deleteLater，让 Qt 真正回收。
        panel = getattr(w, "_snippet_panel", None)
        if panel is not None:
            panel.close()
        w.close()
        self.tabs.removeTab(idx)
        w.deleteLater()
        self._tab_close.sync()

    # ------------------------------------------------------------------ status

    def _flash(self, msg: str, msec: int = 0) -> None:
        self.status_label.setText(msg)
        if msec:
            QtCore.QTimer.singleShot(msec, lambda: self.status_label.setText("就绪"))

    def nativeEvent(self, event_type, message):
        """无边框窗口的原生管道：边缘缩放、标题栏拖动、最大化不盖任务栏。

        逻辑全在 `titlebar.native_event` 里——PyQt6 有三个坑（不能调
        `super().nativeEvent()`、不能对所有消息都返回 `True`、不能靠混类
        提供这个虚方法），踩中任何一个都是窗口一 `show()` 就
        0xC000041D，连 traceback 都打不出来。细节见 `docs/ARCHITECTURE.md`。

        这个方法必须是**这个类自己**的：`nativeEvent` 是 Qt 的虚方法，
        放进纯 Python mixin 里 sip 算不对 vtable 槽位。
        """
        return native_event(self, message)

    def changeEvent(self, event):
        """窗口最大化/还原时，把右上角那个按钮的字形跟着换掉。

        没有这条，窗口最大化之后按钮还画着「最大化」方框——按下去是从最大化
        变回正常，图标却提示"再放大一次"，用户会以为按钮失灵了。
        """
        if event.type() == QtCore.QEvent.Type.WindowStateChange:
            tb = getattr(self, "titlebar", None)
            if tb is not None:
                tb.sync_state()
        super().changeEvent(event)

    def closeEvent(self, event):
        # **先把在等「要不要信任这台主机」的连接线程叫醒。**
        # 它们正卡在 `ask.event.wait(timeout=120)` 上，而这个 wait 只有
        # 主线程跑 `_on_host_key_ask` 才会被 set。主窗口一关，那个槽
        # 永远不会跑了 —— worker 就干等满 120 秒，连接线程挂着不退。
        for ask in list(self._host_key_asks):
            ask.accept = False          # 没人回答 = 拒绝
            ask.event.set()
        self._host_key_asks.clear()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab):
                w._on_disconnect()
        # 读不出来的那份**保留原样**，并且告诉用户一次。静默保留最糟：
        # 用户改了设置发现「关掉再打开又变回去了」，只会以为是程序有毛病，
        # 而真正的原因（配置文件坏了、我们不敢写）没人告诉他。
        if self.settings.load_error:
            msgbox.warning(
                self, "配置没有被保存",
                "%s\n\n原文件保持原样，没有被覆盖。"
                "修好 %s 之后，下次修改设置就能正常保存了。"
                % (self.settings.load_error, self.settings._path or SETTINGS_FILE))
        else:
            self.settings.save()
        super().closeEvent(event)
