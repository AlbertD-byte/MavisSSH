"""Tunnel panel — manage local port forwards and a SOCKS5 proxy per session.

Lives as a sub-tab next to 终端 / 文件 / 监控 and drives
:class:`~sshclient.connection.SSHConnection`'s tunnel registry. Every call
that touches the connection hops through :meth:`_call`, which re-enters the Qt
thread: the accept loops and relay pumps run on their own threads and must
never mutate widgets directly.
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.ui import icons, msgbox, thread_post, titlebar
from sshclient.ui.design.v1 import qss

from ..core.tunnel import TunnelError, TunnelSpec
from ..remote.connection import SSHConnection
from .design.v1.palette import Palette
from .switch import PILL_H, PILL_W, paint_switch
from .window_frame import enable_rounded_corners

_HEADERS = ["", "类型", "本地监听", "远程目标", "连接数", "说明"]
_SWITCH_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1
_PORT_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2


class _SwitchDelegate(QtWidgets.QStyledItemDelegate):
    """A pill-and-knob toggle drawn in the first column.

    Click handling lives in :meth:`editorEvent` rather than a persistent editor:
    a boolean needs no edit session, and this keeps the tree's selection and
    keyboard behaviour untouched.

    The delegate does **not** write the new value to the model. It calls
    *on_toggle* and lets the panel ask the connection first, because a switch
    that claims to be on while the listener failed to bind is worse than one
    that visibly stays off. The panel then rebuilds from the real state.

    颜色**由面板把本仓的 `Palette` token 递进来**（:meth:`set_palette`），不在
    绘制时去读 `option.widget.palette()`。原因：全仓没有一处 `setPalette`，
    界面完全靠 QSS 上色，而 QSS 的 `background-color` 只写 `Base` / `Window`
    这两个角色，**从不写 `Accent` / `Mid`** —— 那两个角色永远是系统色。实测
    设了 `background-color:#ff0000` 之后 `Accent` 仍是系统蓝 `#0067c0`、`Mid`
    仍是 `#a0a0a0`：开启的轨道在三种配色下都是那一片系统蓝，关闭的轨道都是
    `#575858`（`Mid` 叠半透明），切主题、换强调色都纹丝不动。
    不用 `setPalette()` 修是因为那会给整棵控件树写一套 QPalette 角色，和本仓
    「全靠 QSS」这条约定正面冲突（同一个界面会出现两套上色来源）。

    **画法本身已经搬去 `ui/switch.py`，这里只调 `paint_switch`。**
    本类早年是开关字形的唯一实现，于是连接框想用同一个开关时只能重画一遍 ——
    两条实现迟早会分叉（一处 36px 一处 38px、只有一处跟着改主题）。
    """

    _W, _H = PILL_W, PILL_H

    def __init__(self, on_toggle, palette: Palette, parent=None):
        super().__init__(parent)
        self._on_toggle = on_toggle
        self._palette = palette

    def set_palette(self, palette: Palette) -> None:
        self._palette = palette

    def paint(self, painter: QtGui.QPainter, option, index) -> None:
        paint_switch(painter, option.rect,
                     bool(index.data(_SWITCH_ROLE)), self._palette,
                     1.0 if bool(index.data(_SWITCH_ROLE)) else 0.55)

    def editorEvent(self, event, model, option, index) -> bool:
        if event.type() != QtCore.QEvent.Type.MouseButtonRelease:
            return False
        if not option.rect.adjusted(2, 2, -2, -2).contains(event.pos()):
            return False
        port = index.data(_PORT_ROLE)
        if port is not None:
            want = not bool(index.data(_SWITCH_ROLE))
            # Defer to the next event-loop turn. Toggling rebuilds every row,
            # and doing that while the view is still inside this mouse event
            # destroys the items the view is mid-way through using. The port
            # and the intent are captured by value because `index` will not
            # survive the rebuild.
            QtCore.QTimer.singleShot(
                0, lambda p=int(port), w=want: self._on_toggle(p, w))
        return True

    def sizeHint(self, option, index) -> QtCore.QSize:
        return QtCore.QSize(self._W + 12, self._H + 8)


class _AddDialog(titlebar.FitToScreenMixin, QtWidgets.QDialog):
    """One form for both rule kinds; the remote column is hidden for SOCKS."""

    def __init__(self, palette: Palette, parent=None, used_ports: tuple = ()):
        super().__init__(parent)
        self.setWindowTitle("添加端口转发")
        self.setModal(True)
        self.setMinimumWidth(430)
        self._used = set(used_ports)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 自己套一份弹窗配色：这一层要比主窗口亮一档，否则弹出来和窗口糊成
        # 一片。Qt 不会把主窗口的样式表按"弹窗该有的样子"重新解释一遍。
        #
        # **第三参必须给。** 漏了它退回 `UI_SIZE_REF`，这个弹窗就永远按
        # 11pt 渲染：主窗口跟用户放大到 18pt，从转发面板点「添加转发」弹出
        # 的却还是 13px 满屏小字。弹窗是独立顶层窗口，拿不到主窗口那份已经
        # 缩放好的样式表，漏传就等于「颜色跟着主题走、字号停在默认值」——
        # 字号不一致比配色不一致更刺眼。同一条路：msgbox.py:46、
        # connect_dialog.py:228。这个对话框不持有 AppSettings，所以字号
        # 从 qss 的模块级 current 取（由 MainWindow.apply_theme 记下）。
        qss.apply(self, palette, dialog=True, ui_size=qss.current_ui_size())
        # 四角 5px 圆角，见 window_frame.py（QSS 的 border-radius 在 frameless
        # 弹窗上是 0 px：标题栏浮层满宽铺角，必须浮层挖角）。
        enable_rounded_corners(self, palette, border=False)

        # 和设置框、连接框同一条标题栏；内容里那个"新建转发规则"大标题
        # 删掉，它和标题栏说的是同一件事。
        self.titlebar = titlebar.attach_chrome(
            self, palette, title="添加端口转发", kinds=(titlebar.CLOSE,),
            icon_name="plug")
        root.addWidget(self.titlebar)

        body = QtWidgets.QWidget(self)
        root.addWidget(body, 1)
        # 超高时 mixin 把这块装进滚动区（titlebar.fit_to_screen）。要在
        # 下面那行 `root = bl` 重绑定**之前**赋值，绑了就没法再用外层的名字。
        self._scroll_body = body
        bl = QtWidgets.QVBoxLayout(body)
        bl.setContentsMargins(20, 16, 20, 16)
        bl.setSpacing(14)
        root = bl        # 后面照旧往 root 里加

        self.kind_box = QtWidgets.QComboBox()
        self.kind_box.addItem("本地端口转发 (-L)", "local")
        self.kind_box.addItem("动态 SOCKS5 代理 (-D)", "socks")
        self.kind_box.currentIndexChanged.connect(self._sync_kind)
        root.addWidget(self._row("类型", self.kind_box))

        self.local_spin = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.local_spin.setRange(1, 65535)
        self.local_spin.setValue(1080)
        self.local_edit = _port_edit(self.local_spin)
        root.addWidget(self._row("本地端口", self.local_edit))

        # **监听地址是这条规则真正缺的参数。**
        #
        # `TunnelSpec.listen_host` 在 `core/tunnel.py:184` 是**必校验**字段，
        # `Forward.start()` 直接拿它去 `bind()`；而这个表单原来压根没有它的
        # 输入口，`spec()` 只能吃默认值 —— 于是「让局域网内另一台机器连到我的
        # 代理」在纯 UI 操作下做不到，只能手改 sessions.json，而纯 UI 操作恰恰
        # 是这个面板自己的主路径（点一次「添加 SOCKS」就写回会话）。
        #
        # 只给两个取值而不是自由文本：`_valid_listen_host` 放行的形状很宽
        # （任何主机名都算合法），而自由文本框里一个手滑的地址要到 `bind()`
        # 才炸；下拉把「仅本机 / 对外监听」这两个**真正常用的意图**摆出来，
        # 顺带把「0.0.0.0 有暴露面」这件事写进提示里。
        self.listen_box = QtWidgets.QComboBox()
        self.listen_box.addItem("127.0.0.1（仅本机）", "127.0.0.1")
        self.listen_box.addItem("0.0.0.0（局域网可连）", "0.0.0.0")
        self.listen_box.setToolTip(
            "仅本机：只有这台电脑能连上（默认）\n"
            "局域网可连：同一网络内其它机器也能连，请确认网络环境可信")
        root.addWidget(self._row("监听地址", self.listen_box))

        self.host_edit = QtWidgets.QLineEdit()
        self.host_edit.setPlaceholderText("127.0.0.1 或 db.internal")
        self.remote_spin = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.remote_spin.setRange(1, 65535)
        self.remote_spin.setValue(3306)
        self.remote_edit = _port_edit(self.remote_spin)
        target = QtWidgets.QHBoxLayout()
        target.setContentsMargins(0, 0, 0, 0)
        target.setSpacing(6)
        target.addWidget(self.host_edit, 1)
        target.addWidget(QtWidgets.QLabel(":"))
        target.addWidget(self.remote_edit)
        self._target_row = self._row("远程目标", _wrap(target))
        root.addWidget(self._target_row)

        self.desc_edit = QtWidgets.QLineEdit()
        self.desc_edit.setPlaceholderText("可选，例如 MySQL")
        root.addWidget(self._row("说明", self.desc_edit))

        self.hint = QtWidgets.QLabel()
        self.hint.setProperty("role", "muted")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        root.addStretch(1)

        ok = QtWidgets.QPushButton("添加")
        ok.setProperty("variant", "primary")
        ok.setDefault(True)
        # 高度统一交给 QSS 的 `CONTROL_H`（见 connect_dialog.py 同一条说明：
        # 写死的 setFixedHeight 不跟界面字号缩放）。这里原本是 34、
        # 连接框是 38、QSS 的按钮是 33，三个高度并排。
        ok.setMinimumHeight(qss.CONTROL_H)
        ok.clicked.connect(self.accept)
        cancel = QtWidgets.QPushButton("取消")
        cancel.setMinimumHeight(qss.CONTROL_H)
        cancel.clicked.connect(self.reject)
        actions = QtWidgets.QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(ok)
        root.addLayout(actions)

        self._sync_kind()

    def nativeEvent(self, event_type, message):
        # 无边框后拖动窗口要靠标题栏向系统报 HTCAPTION，逻辑在 titlebar 里。
        return titlebar.native_event(self, message)

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _row(label: str, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        # `plain_container`：裸 QWidget 会吃 QSS 的兜底底色，在弹框里就是
        # 一块与周围不同色的补丁。理由见 qss.plain_container。
        box = qss.plain_container(QtWidgets.QWidget())
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        text = QtWidgets.QLabel(label)
        text.setProperty("role", "muted")
        lay.addWidget(text)
        lay.addWidget(widget)
        return box

    def _sync_kind(self) -> None:
        is_local = self.kind_box.currentData() == "local"
        self._target_row.setVisible(is_local)
        if is_local:
            self.hint.setText(
                "本地端口的连接会经 SSH 转发到远程目标。远程主机名由服务端解析。")
        else:
            self.hint.setText(
                "在本地开一个 SOCKS5 代理，把浏览器或工具的代理指向它即可访问内网。"
                "目标地址由客户端在每次连接时指定。")

    def spec(self) -> TunnelSpec:
        kind = self.kind_box.currentData()
        return TunnelSpec(
            kind=kind,
            local_port=self.local_spin.value(),
            remote_host=self.host_edit.text().strip() if kind == "local" else "",
            remote_port=self.remote_spin.value() if kind == "local" else 0,
            listen_host=str(self.listen_box.currentData() or "127.0.0.1"),
            description=self.desc_edit.text().strip(),
        )

    def accept(self) -> None:
        spec = self.spec()
        if spec.local_port in self._used:
            msgbox.warning(
                self, "端口已占用",
                f"本地端口 {spec.local_port} 已经有一条转发规则，请换一个。",
            )
            return
        if spec.kind == "local" and not spec.remote_host:
            self.host_edit.setFocus()
            msgbox.warning(self, "信息不完整", "请填写远程目标地址。")
            return
        super().accept()


def _port_edit(spin: QtWidgets.QSpinBox) -> QtWidgets.QWidget:
    wrap = qss.plain_container(QtWidgets.QWidget())
    lay = QtWidgets.QHBoxLayout(wrap)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(spin)
    lay.addStretch(1)
    return wrap


def _wrap(layout: QtWidgets.QLayout) -> QtWidgets.QWidget:
    w = qss.plain_container(QtWidgets.QWidget())
    w.setLayout(layout)
    return w


class TunnelPanel(QtWidgets.QWidget):
    """Live view of a session's forwarding rules.

    Emits :attr:`rulesChanged` after every add / remove / switch so the owner
    can persist the set; the panel itself never writes to disk.
    """

    rulesChanged = QtCore.pyqtSignal()

    def __init__(self, conn: SSHConnection, palette: Palette,
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._conn = conn
        self._palette = palette
        self._flash = None                 # set by SessionTab

        conn.set_tunnel_callbacks(on_error=self._on_tunnel_error,
                                  on_event=self._on_tunnel_event)

        # ---- header ----
        title = QtWidgets.QLabel("端口转发")
        tf = title.font()
        tf.setBold(True)
        # 原来还有一句 `setPointSize(11)`，实测同样没生效：QSS 的
        # `QWidget { font-size: 13px }` 优先级更高，会把它顶掉。字号由 QSS
        # 统一按 `ui_font_size` 缩放，这里只管粗细。
        title.setFont(tf)
        self.count_label = QtWidgets.QLabel("")
        self.count_label.setProperty("role", "muted")

        self.add_local_btn = QtWidgets.QPushButton("添加转发")
        self.add_local_btn.setProperty("variant", "primary")
        self.add_local_btn.clicked.connect(lambda: self._on_add("local"))
        self.add_socks_btn = QtWidgets.QPushButton("添加 SOCKS")
        self.add_socks_btn.clicked.connect(lambda: self._on_add("socks"))
        self.remove_btn = QtWidgets.QPushButton("移除")
        self.remove_btn.clicked.connect(self._on_remove)

        badge = QtWidgets.QLabel()
        badge.setPixmap(icons.icon_pixmap("globe", palette.accent, 15))
        badge.setFixedWidth(18)

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(2, 0, 2, 0)
        head.setSpacing(8)
        head.addWidget(badge, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        head.addWidget(title, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.count_label, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        head.addStretch(1)
        head.addWidget(self.add_local_btn)
        head.addWidget(self.add_socks_btn)
        head.addWidget(self.remove_btn)

        # ---- rule list ----
        self.model = QtGui.QStandardItemModel(0, len(_HEADERS), self)
        self.model.setHorizontalHeaderLabels(_HEADERS)
        self.view = QtWidgets.QTreeView(self)
        self.view.setModel(self.model)
        self.view.setRootIsDecorated(False)
        self.view.setUniformRowHeights(True)
        self.view.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setAlternatingRowColors(True)
        # 轨道色是**绘制时**从 token 取的，所以 token 得先递给委托，委托才有
        # 东西可读；换主题时 apply_palette 再递一次新的。
        self._switch = _SwitchDelegate(self._on_switch, palette, self.view)
        self.view.setItemDelegateForColumn(0, self._switch)
        self.view.setColumnWidth(0, 46)

        self.empty_label = QtWidgets.QLabel(
            "还没有转发规则。\n\n"
            "本地端口转发把本机端口接到远程服务，例如 127.0.0.1:13306 → 127.0.0.1:3306，"
            "这样本地工具就能直连内网数据库。\n\n"
            "SOCKS5 代理则把本机变成一个代理端口，浏览器指向它即可访问内网。"
        )
        self.empty_label.setProperty("role", "muted")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setVisible(False)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addLayout(head)
        root.addWidget(self.view, 1)
        root.addWidget(self.empty_label, 1)

        self._refresh_timer = QtCore.QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self._refresh_counts)
        self._refresh_timer.start()

        self.refresh()

    # ------------------------------------------------------------------ theme

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        # 开关轨道是委托在 paint 里用 token 现画的：不把新 token 递过去，
        # 它就一直画上一套配色那一档（切主题后轨道纹丝不动）。而 muted 前景
        # 是烘进 item 里的，所以两边都要重刷。
        self._switch.set_palette(palette)
        if self.view.viewport() is not None:
            self.view.viewport().update()
        self.refresh()

    # ------------------------------------------------------------------ add/remove

    def _on_add(self, kind: str) -> None:
        used = tuple(s.local_port for s in self._conn.tunnel_specs())
        dlg = _AddDialog(self._palette, self, used)
        dlg.kind_box.setCurrentIndex(0 if kind == "local" else 1)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self._call(self._conn.add_tunnel, dlg.spec())

    def _on_switch(self, local_port: int, enabled: bool) -> None:
        """Switch a rule on or off from the panel's toggle column."""
        self._call(self._conn.set_tunnel_enabled, local_port, enabled)

    def _on_remove(self) -> None:
        port = self._selected_port()
        if port is None:
            self._flash("请先选中一条规则")
            return
        self._call(self._conn.remove_tunnel, port)

    def _selected_port(self) -> int | None:
        sel = self.view.selectionModel()
        rows = sel.selectedRows() if sel is not None else []
        if not rows:
            return None
        item = self.model.item(rows[0].row(), 0)
        if item is None:
            return None
        port = item.data(_PORT_ROLE)
        return int(port) if port is not None else None

    def _select_port(self, port: int) -> None:
        """Re-select a row after the table was rebuilt.

        QTreeView has no selectRow() -- that is a QListView/QTableView method.
        Going through the selection model is the supported way, and the wrong
        call raised inside a mouse event, which Qt turns into a hard process
        abort rather than a catchable Python exception.
        """
        sm = self.view.selectionModel()
        if sm is None:
            return
        for row in range(self.model.rowCount()):
            item = self.model.item(row, 0)
            if item is not None and item.data(_PORT_ROLE) == port:
                sm.select(
                    self.model.index(row, 0),
                    QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                    | QtCore.QItemSelectionModel.SelectionFlag.Rows,
                )
                return

    # ------------------------------------------------------------------ refresh

    @QtCore.pyqtSlot()
    def refresh(self) -> None:
        """Rebuild the table from the connection's real state.

        Always rebuilding (rather than patching the clicked cell) is what keeps
        the switch honest: if enabling failed, the row comes back showing the
        state the connection actually holds.
        """
        specs = self._conn.tunnel_specs()
        states = self._conn.tunnel_states()
        counts = self._conn.tunnel_connections()
        keep_port = self._selected_port()
        muted = QtGui.QColor(self._palette.text_muted)
        self.model.setRowCount(0)
        for spec in specs:
            on = states.get(spec.local_port, False)
            row = self.model.rowCount()
            self.model.insertRow(row)
            kind = "本地 -L" if spec.kind == "local" else "SOCKS -D"
            cells = [
                kind,
                f"{spec.listen_host}:{spec.local_port}",
                f"{spec.remote_host}:{spec.remote_port}" if spec.kind == "local" else "由客户端指定",
                str(counts.get(spec.local_port, 0)) if on else "—",
                spec.description or "—",
            ]
            for col, text in enumerate(cells):
                item = QtGui.QStandardItem(text)
                if col == 0:
                    # cells[0] is 类型, which lives in view column 1
                    item.setData(spec.local_port, _PORT_ROLE)
                if not on:
                    # Mute the whole row so "off" is readable at a glance.
                    item.setForeground(QtGui.QBrush(muted))
                self.model.setItem(row, col + 1, item)
            sw = QtGui.QStandardItem("")
            sw.setData(on, _SWITCH_ROLE)
            sw.setData(spec.local_port, _PORT_ROLE)
            if not on:
                sw.setForeground(QtGui.QBrush(muted))
            self.model.setItem(row, 0, sw)

        self.view.setVisible(bool(specs))
        self.empty_label.setVisible(not specs)
        self.remove_btn.setEnabled(bool(specs))
        if keep_port is not None:
            self._select_port(keep_port)      # keep the selection across a rebuild
        self._update_summary(specs, states, counts)

    def _update_summary(self, specs, states, counts) -> None:
        on_count = sum(1 for v in states.values() if v)
        total = sum(counts.values())
        self.count_label.setText(
            f"{len(specs)} 条规则 · {on_count} 条开启 · {total} 个活动连接")

    def _refresh_counts(self) -> None:
        """Cheap per-second tick so the 连接数 column tracks live traffic.

        Also updates the summary label: the two must agree, otherwise the
        column ticks up while the total sits stale.
        """
        specs = self._conn.tunnel_specs()
        states = self._conn.tunnel_states()
        counts = self._conn.tunnel_connections()
        for row in range(self.model.rowCount()):
            item = self.model.item(row, 0)
            if item is None:
                continue
            port = item.data(_PORT_ROLE)
            on = states.get(port, False)
            cell = self.model.item(row, 4)      # 0 switch, 1 kind, 2 local,
            if cell is not None:                # 3 remote, 4 connections
                cell.setText(str(counts.get(port, 0)) if on else "—")
        self._update_summary(specs, states, counts)

    # ------------------------------------------------------------------ plumbing

    def _call(self, fn, *args) -> bool:
        """Run a registry mutation, report failures inline, then persist.

        ``add_tunnel`` binds a listener and spawns a thread — about a
        millisecond — so calling it straight from the GUI thread is fine and
        keeps the error reportable at the call site. Only the *callbacks*
        coming back from tunnel worker threads need to be marshalled, which
        :meth:`_on_tunnel_error` and :meth:`_on_tunnel_event` handle.

        ``rulesChanged`` fires on both the success and the failure path: a
        rejected change still leaves the stored rule set in whatever state
        the connection actually holds, and that is what must be written back.
        """
        try:
            fn(*args)
        except TunnelError as exc:
            self._flash(str(exc))
            self.refresh()
            self.rulesChanged.emit()
            return False
        except Exception as exc:  # noqa: BLE001
            self._flash(f"操作失败: {exc}")
            self.refresh()
            self.rulesChanged.emit()
            return False
        self.refresh()
        self.rulesChanged.emit()
        return True

    def current_rules(self) -> list[dict]:
        """The full rule set, in :meth:`TunnelSpec.to_dict` form.

        Includes rules that are switched off: the configuration has to
        survive a reconnect even while nothing is listening.
        """
        states = self._conn.tunnel_states()
        return [spec.to_dict(enabled=states.get(spec.local_port, False))
                for spec in self._conn.tunnel_specs()]

    def _on_tunnel_error(self, message: str) -> None:
        """Called from a tunnel worker thread."""
        # **投之前先看面板还在不在** —— 与 monitor_panel 同一个坑：
        # 面板是 SessionTab 的子控件，关标签时被连带销毁，而隧道 worker
        # 还活着。详见 thread_post.py。
        thread_post.post(self, "_report", QtCore.Q_ARG(str, message))

    @QtCore.pyqtSlot(str)
    def _report(self, message: str) -> None:
        if self._flash is not None:
            self._flash(message)

    def _on_tunnel_event(self, forward, kind: str) -> None:
        if kind in ("started", "stopped"):
            thread_post.post(self, "refresh")

    def shutdown(self) -> None:
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
