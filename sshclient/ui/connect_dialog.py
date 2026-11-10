"""Connection dialog -- gathers host, port, user, and password / key auth."""
from __future__ import annotations

import logging
import os

from PyQt6 import QtCore, QtWidgets

from sshclient.core.settings import DEFAULT_GROUP
from sshclient.ui import icons, msgbox, titlebar

from .design.v1 import qss
from .design.v1.palette import Palette, get_palette
from .design.v1.qss import build_qss
from .switch import Switch
from .window_frame import enable_rounded_corners

log = logging.getLogger(__name__)

#: SOCKS5 的默认本地端口。与 OpenSSH `ssh -D 1080` 的惯例一致，所以打开
#: switch 就有一个能直接抄进浏览器设置的地址。
_DEFAULT_SOCKS_PORT = 1080

# 监听地址存在规则行的第 5 位，但表里只有 4 列，所以它没有显示位置。
# 收窄到这一个值是有意的：它是用户真会手工写进配置、也真需要保留的那一个
# （写 `0.0.0.0` 就是明说要对外监听），而本表没有一列能承载别的监听地址，
# 凭空放行一个本文件无法校验的字符串，只会把「静默收窄」换成「连上时抛
# TunnelError」——那是拿可见的崩溃换不可见的收紧，不划算。
_LOOPBACK = "127.0.0.1"
_WIDE_LISTEN = "0.0.0.0"

#: 分组下拉里代表「这条会话不归任何分组」的那一项。
#:
#: **显示成「默认」，和数据层的空串不是一回事。** 数据层用空串表示未分组
#: （见 ``SavedSession.group``），而侧栏把空串那一段显示成
#: :data:`DEFAULT_GROUP`（见 ``SavedSession.group_name``）。下拉框跟着侧栏
#: 说话 —— 两处写不同的名字，用户在连接框里选的是「默认」，回头在侧栏
#: 找「（未分组）」，会以为程序没认出他刚才的设置。
#:
#: 它同时也是**退出分组**的那一项：不是空白的条目。空白的条目看不出是什么，
#: 它和「这一项被清空了」长得一模一样，用户既不知道点它会发生什么，也没法
#: 确认自己现在是不是已经选了它。有名字之后，「怎么退出分组」从「手动全选
#: 删除」变成点一下。
UNGROUPED = DEFAULT_GROUP


def _normalize_listen(value) -> str:
    """把一条规则里存着的监听地址收窄成**这一档能表达的**，并记住收窄过。

    收窄本身保留（`::` 那个 IPv6 通配在本仓**永远绑不上** ——
    `core/tunnel.py` 建的是写死的 `socket.AF_INET`，而 `Forward.start()`
    会拿它去 `bind`；原样存下来只是把一次必然的失败从「编辑时」推迟到
    「连上时」，不划算）。

    **但收窄必须被说出来。** 静默把用户的 `10.1.2.3` 变成 `127.0.0.1`，
    症状是「同事访问不通」而界面上毫无线索。这里把收窄过的事实挂在一个
    列表上，由 `accept()` 一并告知 —— 与「无效转发行当场移除 + 点名」
    是同一类处理：**能被静默改掉的东西，就不许静默。**
    """
    raw = str(value or "").strip()
    if not raw or raw == _LOOPBACK or raw == _WIDE_LISTEN:
        return raw or _LOOPBACK
    _NARROWED.append(raw)
    return _LOOPBACK


#: 本次打开对话框期间被收窄过的监听地址。
#:
#: **模块级但按次清空** —— 它由 `_normalize_listen` 这个模块函数写入，
#: 而那个函数是从表格的 `_append` 里调的，拿不到对话框实例。跨次累积会
#: 让上一个对话框的收窄被下一个当成自己的（而在测试里则是跨用例泄漏，
#: 一条判据红在另一条的输入上，根本查不出来）。
#: 所以构造时 `clear()`、`accept()` 时排空 —— 与 `_dropped`/`take_dropped()`
#: 同一套写法。
_NARROWED: list[str] = []

#: 转发规则的开关状态挂在「第 0 列 item」的这个 data role 上。
#: 与 `listen_host` 共用同一格 item 是有意的：item 跟着 `removeRow` 一起消失，
#: 所以不存在「另记一份和行号平行的列表」那种错位 bug。
_ROLE_ENABLED = QtCore.Qt.ItemDataRole.UserRole + 1


class _Field(QtWidgets.QWidget):
    """Icon + label + input, stacked. Used for every input in the dialog."""

    def __init__(self, icon_name: str, label: str, editor: QtWidgets.QWidget, palette: Palette, parent=None):
        super().__init__(parent)
        # 纯行内容器，不画自己的底（理由见 `qss.plain_container`）。
        qss.plain_container(self)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        ic = QtWidgets.QLabel()
        ic.setPixmap(icons.icon_pixmap(icon_name, palette.text_muted, 14, 1.7))
        ic.setFixedWidth(16)
        head.addWidget(ic, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        head.addWidget(QtWidgets.QLabel(label))
        head.addStretch(1)

        lay.addLayout(head)
        lay.addWidget(editor)


class _TunnelRulesEditor(QtWidgets.QWidget):
    """Editable list of ``-L`` rules: local port, remote host, remote port.

    Stores the same row shape as :attr:`SavedSession.tunnel_local` so the
    dialog and the live panel agree on the format.
    """

    COLUMNS = ["本地端口", "远程主机", "远程端口", "说明"]

    def __init__(self, initial_rows, palette: Palette, parent=None):
        super().__init__(parent)
        # 卡片内容器：它待在 `role="card"`（白）里面，所以**不能**刷自己的底
        # ——刷了就是白卡片里嵌一块灰，正是用户报的那条（理由与量法见
        # `qss.plain_container`，判据在 `tests/_socksbg.py`）。
        qss.plain_container(self)
        self._palette = palette
        self._rows: list[list] = [list(r) for r in initial_rows]
        # 本次打开对话框期间被移出行号（已经拼成给人看的一句话）。
        # 存下来是为了让 `accept()` 能**告诉用户移走了哪几行**：静默移除和
        # 静默 `continue` 的区别只剩这一句话，缺了它就是把同一个缺陷换个地方。
        self._dropped: list[str] = []

        self.table = QtWidgets.QTableWidget(0, len(self.COLUMNS), self)
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setFixedHeight(132)

        add_btn = QtWidgets.QPushButton("添加规则")
        add_btn.clicked.connect(lambda: self._append(13306, "127.0.0.1", 3306, ""))
        del_btn = QtWidgets.QPushButton("删除")
        del_btn.clicked.connect(self._remove_selected)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(add_btn)
        bar.addWidget(del_btn)
        bar.addStretch(1)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(self.table)
        lay.addLayout(bar)

        for row in self._rows:
            self._append(*row[:4],
                         listen_host=row[4] if len(row) > 4 else "",
                         enabled=bool(row[5]) if len(row) > 5 else True)
        # **不要预填。** 原来这里在空表时塞一条 `13306 -> 127.0.0.1:3306`，
        # 而 `rows()` 会把它当成用户填的正常规则吐出去，连上之后
        # `_arm_session_tunnels` 真的把它监听起来 —— 用户从头到尾没要求过，
        # 却已经在他机器上开了一个监听端口。
        # 想要规则就点「添加规则」，那一行的默认值（13306/127.0.0.1/3306）
        # 仍然作为**用户主动添加**时的起点。

    def _append(self, local_port, host, remote_port, desc, listen_host: str = "",
                enabled: bool = True) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(str(local_port)))
        self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(str(host)))
        self.table.setItem(r, 2, QtWidgets.QTableWidgetItem(str(remote_port)))
        self.table.setItem(r, 3, QtWidgets.QTableWidgetItem(str(desc or "")))
        # 监听地址挂在第 0 列 item 的 UserRole 上，而不是另开一列、也不是另记
        # 一份「和行号平行的列表」：item 会跟着 `removeRow` 一起消失，所以不存在
        # 行号对不上的那种错位 bug；它只是不进 `rows()` 的取值过程。
        self.table.item(r, 0).setData(
            QtCore.Qt.ItemDataRole.UserRole,
            _normalize_listen(listen_host))
        # **开关状态也挂在 item 上，理由同上。** 它原来是写死的 `True`：
        # 用户在转发面板里手动关掉一条隧道 → 打开这个对话框改个别的东西 →
        # 确定 → 那条隧道自己开了，全程零提示。面板那边把开关状态存回会话
        # （`main_window` 里的 `rulesChanged`），是这个洞的另一头。
        self.table.item(r, 0).setData(_ROLE_ENABLED, bool(enabled))

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def take_dropped(self) -> list[str]:
        """交出「本次移走了哪些行」并清空，由 ``accept()`` 拿去告知用户。"""
        out, self._dropped = list(self._dropped), []
        return out

    def rows(self) -> list[list]:
        """只吐合法规则；**填不对的行当场从表里移除**。

        原来这里是无条件 ``continue``：规则被悄悄丢掉，那几行却还留在屏幕上，
        用户点确定之前没有任何东西说它们不会被保存——屏幕上有 5 行、实际生效
        1 条，而界面从头到尾没提过另外 4 条。于是「我明明填了」和「它明明没
        生效」之间没有任何可见的连接。

        改成当场移除 + `accept()` 弹窗点名，是因为这两处都在同一个对话框里、
        不需要新的视觉语言：留在表里却不让保存的行，在用户眼里和有效行长
        得一模一样，而要让它们「看起来不一样」得往 ``design/v1`` 那套 QSS 里
        加无效态配色，那是另一个模块的事。
        """
        out: list[list] = []
        # 倒着删：升序删会让后面的行号整体前移，同一轮里就会拿错 item。
        for r in range(self.table.rowCount() - 1, -1, -1):
            try:
                port = int(self.table.item(r, 0).text().strip())
                host = self.table.item(r, 1).text().strip()
                rport = int(self.table.item(r, 2).text().strip())
            except (AttributeError, TypeError, ValueError):
                self._drop(r, "本地端口/远程端口不是数字")
                continue
            if not (1 <= port <= 65535 and 1 <= rport <= 65535) or not host:
                self._drop(r, self._why(port, host, rport))
                continue
            desc = self.table.item(r, 3).text().strip() if self.table.item(r, 3) else ""
            listen = self.table.item(r, 0).data(QtCore.Qt.ItemDataRole.UserRole)
            enabled = bool(self.table.item(r, 0).data(_ROLE_ENABLED))
            out.append([port, host, rport, desc, listen or _LOOPBACK, enabled])
        out.reverse()      # 倒序遍历的产物，交回去要还原成表格从上到下的顺序
        return out

    @staticmethod
    def _why(port: int, host: str, rport: int) -> str:
        """说清**是哪一栏**不对 —— 只说「这一行无效」等于让用户自己回去找。"""
        bad = []
        if not host:
            bad.append("远程主机为空")
        if not 1 <= port <= 65535:
            bad.append("本地端口 %d 超出 1–65535" % port)
        if not 1 <= rport <= 65535:
            bad.append("远程端口 %d 超出 1–65535" % rport)
        return "、".join(bad)

    def _drop(self, r: int, why: str) -> None:
        """记下这一行、连同它的原文一起记下，然后把行号从表里摘掉。

        原文（端口/主机/说明）要留在提示里：行被摘掉之后界面上就再也找不回来
        了，只报行号等于让用户凭记忆重填。
        """
        desc = self.table.item(r, 3).text().strip() if self.table.item(r, 3) else ""
        raw = " → ".join(
            (self.table.item(r, c).text().strip() if self.table.item(r, c) else "")
            for c in (0, 1, 2))
        label = "「%s」" % desc if desc else "无说明"
        self._dropped.append("第 %d 行（%s：%s）：%s" % (r + 1, label, raw, why))
        self.table.removeRow(r)


def _sep_line(palette: Palette) -> QtWidgets.QFrame:
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
    line.setStyleSheet(f"color: {palette.border}; background: {palette.border};")
    line.setFixedHeight(1)
    return line


def _group_value(box: QtWidgets.QComboBox) -> str:
    """分组下拉当前选中的东西，翻译成数据层那一档的写法。

    **``UNGROUPED``（「默认」）这一项要翻回空串。** 下拉框和侧栏显示的是
    给人看的名字，数据层认的是空串 = 未分组；两边的对应关系只在这一处，
    所以也只有这一处知道「默认」这两个字对应的是空。

    按**文字**判而不是按 `currentIndex()`：`currentIndex()` 在用户手动打字
    时不跟着变（`InsertPolicy.NoInsert` 下打字只写 lineEdit），于是
    「手打一个恰好叫默认的分组」会被当成选中第 0 项而丢掉。按文字判的
    代价是用户自己新建一个叫「默认」的分组会并进默认分组 —— 这是故意的，
    见 `DEFAULT_GROUP` 的注释。

    读 ``currentText()`` 而不是 ``currentData()``：这个框是可编辑的，
    用户可以直接打字，而打字的内容只存在于 lineEdit 里，没有对应的
    ``data`` —— 读 data 会把「新输入的分组名」静悄悄地变成空串，
    表现为「填了分组，保存之后又没了」。
    """
    text = box.currentText().strip()
    return "" if text == UNGROUPED else text


def _plain(widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
    """行内容器统一透明。实现与理由见 `qss.plain_container`。"""
    return qss.plain_container(widget)


def _row(label: str, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
    box = _plain(QtWidgets.QWidget())
    lay = QtWidgets.QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(5)
    text = QtWidgets.QLabel(label)
    text.setProperty("role", "muted")
    lay.addWidget(text)
    lay.addWidget(widget)
    return box


class ConnectDialog(titlebar.FitToScreenMixin, QtWidgets.QDialog):
    """Modal dialog producing an :class:`~sshclient.settings.SavedSession`-shaped dict."""

    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        *,
        initial: dict | None = None,
        palette: Palette | None = None,
        groups: list[str] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("新建连接" if not initial else "编辑连接")
        self.setModal(True)
        self.setMinimumWidth(480)
        self._initial = initial or {}
        self._groups = list(groups or ())
        p = palette or get_palette("dark")
        self._palette = p
        # A top-level dialog does not inherit the QMainWindow's stylesheet,
        # so it has to be styled explicitly or it renders unthemed.
        # 字号同理：弹窗是独立顶层窗口，拿不到主窗口那份缩放后的样式表，
        # 漏传就等于「颜色跟着主题走、字号停在默认值」——满屏都是字的时候
        # 字号不一致比配色不一致更刺眼。
        self.setStyleSheet(build_qss(p, dialog=True,
                                     ui_size=qss.current_ui_size()))
        # 四角 5px 圆角，见 window_frame.py（QSS 的 border-radius 在 frameless
        # 弹窗上是 0 px：标题栏浮层满宽铺角，必须浮层挖角）。
        enable_rounded_corners(self, p, border=False)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 自绘标题栏，和主窗口 / 设置框同一条。原来那块"图标 + 连接配置 +
        # 副标题"的大头部整个删掉：标题栏已经说了"新建连接"，内容再说一遍
        # "连接配置"是同一件事说两遍，副标题留下就够了。
        self.titlebar = titlebar.attach_chrome(
            self, p, title=self.windowTitle(), kinds=(titlebar.CLOSE,),
            icon_name="server")
        root.addWidget(self.titlebar)

        body = QtWidgets.QWidget(self)
        root.addWidget(body, 1)
        # 正文容器交给 mixin：内容超高时它会把这块装进滚动区（见
        # titlebar.fit_to_screen）。默认字号下就有 939px，而 ConnectDialog
        # 在 14pt 就超 1032 的工作区，所以这条路径是常用路径而非兜底。
        self._scroll_body = body
        root = QtWidgets.QVBoxLayout(body)
        root.setContentsMargins(24, 16, 24, 20)
        root.setSpacing(18)

        t2 = QtWidgets.QLabel("填写目标主机与认证方式")
        t2.setProperty("role", "muted")
        root.addWidget(t2)

        # ---------- basic fields ----------
        self.name_edit = QtWidgets.QLineEdit(self._initial.get("name", ""))
        self.name_edit.setPlaceholderText("例如 生产环境 Web-01")

        # 分组。**放在会话名称之后**而不是之前：名称是必答项，分组是可选项，
        # 「先看到自己必须填的那个」比「先看到一个可以留空的」更不容易漏。
        #
        # **可编辑下拉** = 「从现有的里挑一个」和「自己起一个」两件事同时成立，
        # 所以控件必须同时是这两样。
        #
        # （先做过的是 `QLineEdit` + 行内补全，只满足了「打字」那一半：
        # 补全只在文字里悄悄补全一段，从不列出有哪些分组，于是用户根本
        # 不知道自己能选什么，只能靠回忆去拼。）
        self.group_edit = qss.compact_combobox(QtWidgets.QComboBox())
        self.group_edit.setEditable(True)
        # **NoInsert。** 这个框不该自己长出条目：分组是由「保存这条会话」
        # 这个动作创建的，不是在框里敲几个字就创建了。用 NoInsert 之后
        # 列表永远只有外面传进来的那一份，用户打了新名字要到保存之后
        # 才在别的会话里看见 —— 这与「新建了一条分组」的直觉一致。
        self.group_edit.setInsertPolicy(QtWidgets.QComboBox.InsertPolicy.NoInsert)
        self.group_edit.addItem(UNGROUPED, "")
        # **照单全收，不再自己去重排序。** 传进来的是 `MainWindow
        # ._known_groups()`，那才是分组名单的唯一权威；这里再排一次等于
        # 多一个可能和它不一致的地方。`data` 也一并带上 —— 不是为了回吐
        # （回吐读的是 currentText，理由见 _group_value），而是别让
        # 「只有第一项有 data、其余是 None」这种半吊子状态留在那儿，
        # 免得下一个读 data 的人以为那是有意义的。
        for _g in self._groups:
            self.group_edit.addItem(_g, _g)
        _cur = str(self._initial.get("group", "") or "")
        if not _cur:
            self.group_edit.setCurrentIndex(0)          # 「默认」
        else:
            _idx = self.group_edit.findText(_cur)
            if _idx >= 0:
                self.group_edit.setCurrentIndex(_idx)
            else:
                # **列表里没有这个名字时也照原样显示，绝不悄悄改成
                # 「默认」。** 用 `setCurrentIndex(max(0, -1))` 图省事，
                # 后果是这条会话原本的分组被**改掉**、并且立刻落盘 ——
                # 打开一次编辑框就丢一个分组，没有任何提示。
                # `setCurrentText` 只写 lineEdit，不往列表里加条目。
                self.group_edit.setCurrentText(_cur)
        self.group_edit.lineEdit().setPlaceholderText(
            "选一个，或者直接输入新分组名")
        self.group_edit.setToolTip(
            "分组决定标签标题的写法（分组名-会话名）。\n"
            "选「%s」表示这条会话不归任何分组。" % UNGROUPED)

        self.host_edit = QtWidgets.QLineEdit(self._initial.get("host", ""))
        self.host_edit.setPlaceholderText("10.0.0.1 或 web-01.internal")
        self.port_edit = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.port_edit.setRange(1, 65535)
        self.port_edit.setValue(int(self._initial.get("port", 22)))
        self.port_edit.setFixedWidth(88)

        host_row = QtWidgets.QHBoxLayout()
        host_row.setContentsMargins(0, 0, 0, 0)
        host_row.setSpacing(8)
        host_row.addWidget(self.host_edit, 1)
        host_row.addWidget(QtWidgets.QLabel(":"))
        host_row.addWidget(self.port_edit)
        host_w = _plain(QtWidgets.QWidget())
        host_w.setLayout(host_row)

        self.user_edit = QtWidgets.QLineEdit(self._initial.get("username", ""))
        self.user_edit.setPlaceholderText("root / deploy / …")

        root.addWidget(_Field("list", "会话名称", self.name_edit, p))
        root.addWidget(_Field("folder", "分组", self.group_edit, p))
        root.addWidget(_Field("globe", "主机与端口", host_w, p))
        root.addWidget(_Field("user", "用户名", self.user_edit, p))

        # ---------- auth ----------
        auth_title = QtWidgets.QLabel("认证方式")
        auth_title.setProperty("role", "section")
        root.addWidget(auth_title)

        self.auth_tabs = QtWidgets.QTabWidget()
        self.auth_tabs.setDocumentMode(True)

        # Password page
        self.password_edit = QtWidgets.QLineEdit(self._initial.get("password", ""))
        self.password_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("密码")
        pw_page = _plain(QtWidgets.QWidget())
        pw_lay = QtWidgets.QVBoxLayout(pw_page)
        pw_lay.setContentsMargins(12, 12, 12, 12)
        pw_lay.addWidget(_Field("lock", "密码", self.password_edit, p))
        pw_lay.addStretch(1)

        # Key page
        self.key_path_edit = QtWidgets.QLineEdit(self._initial.get("key_filename", ""))
        # 平台中立的写法：写死 C:\... 会让 macOS/Linux 用户以为自己装错了。
        self.key_path_edit.setPlaceholderText("~/.ssh/id_ed25519")
        self.key_browse = QtWidgets.QPushButton("浏览…")
        self.key_browse.clicked.connect(self._browse_key)
        key_row = QtWidgets.QHBoxLayout()
        key_row.setContentsMargins(0, 0, 0, 0)
        key_row.setSpacing(8)
        key_row.addWidget(self.key_path_edit, 1)
        key_row.addWidget(self.key_browse)
        key_w = _plain(QtWidgets.QWidget())
        key_w.setLayout(key_row)

        self.key_pass_edit = QtWidgets.QLineEdit(self._initial.get("key_passphrase", ""))
        self.key_pass_edit.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.key_pass_edit.setPlaceholderText("私钥口令（若已加密）")

        key_page = _plain(QtWidgets.QWidget())
        key_lay = QtWidgets.QVBoxLayout(key_page)
        key_lay.setContentsMargins(12, 12, 12, 12)
        key_lay.addWidget(_Field("key", "私钥文件", key_w, p))
        key_lay.addWidget(_Field("lock", "私钥口令", self.key_pass_edit, p))
        key_lay.addStretch(1)

        self.auth_tabs.addTab(pw_page, "密码")
        self.auth_tabs.addTab(key_page, "SSH 私钥")
        if self._initial.get("auth_method") == "key":
            self.auth_tabs.setCurrentIndex(1)
        root.addWidget(self.auth_tabs)

        # ---------- port forwarding ----------
        tunnel_title = QtWidgets.QLabel("端口转发（可选）")
        tunnel_title.setProperty("role", "section")
        root.addWidget(tunnel_title)

        # The table only renders -L rules; the SOCKS rule lives in the spin
        # box below, so seed each from the parts of `tunnels` it owns.
        saved_rules = (self._initial.get("tunnels")
                       or self._initial.get("tunnel_local") or [])
        local_rows: list[list] = []
        saved_socks = int(self._initial.get("tunnel_socks", 0) or 0)
        # 老格式没有 `enabled`（那时规则只有开/关两种存在方式），
        # 所以缺这一项时默认 True —— 与迁移路径 `from_row` 的做法一致。
        self._socks_enabled = True
        # SOCKS 那条的监听地址同样要保住 —— `0.0.0.0`（对外监听）是**能用的**，
        # 收窄成 `127.0.0.1` 是纯损失，没有任何好处（本框没有监听地址控件，
        # 但「原样存着」不等于「让用户能改」）。
        self._socks_listen: str = ""
        _NARROWED.clear()          # 见 _NARROWED 那段：按次清空，不跨对话框累积
        # **spinbox 之外的那些 SOCKS 原样留着。** 本对话框只有一个 SOCKS
        # 端口框，装不下第二条；原来的循环是覆盖式赋值，于是「有几条留几条」
        # 变成「只剩最后一条」——静默丢用户配置。见 `_tunnel_rules` 那段。
        self._extra_socks: list[dict] = []
        for entry in saved_rules:
            if isinstance(entry, dict):
                if entry.get("kind") == "socks":
                    if not saved_socks:
                        saved_socks = int(entry.get("local_port") or 0)
                        self._socks_enabled = bool(entry.get("enabled", True))
                        self._socks_listen = _normalize_listen(
                            entry.get("listen_host"))
                    else:
                        self._extra_socks.append(dict(entry))
                else:
                    local_rows.append([
                        int(entry.get("local_port") or 0),
                        str(entry.get("remote_host") or ""),
                        int(entry.get("remote_port") or 0),
                        str(entry.get("description") or ""),
                        # 规范格式里本来就有 `listen_host`，这里却只抄了前四栏
                        # —— 于是「对外监听」在**回填进表的那一步**就丢了，
                        # 比 `rows()` 里写死 `127.0.0.1` 更早，且同样零提示。
                        str(entry.get("listen_host") or ""),
                        bool(entry.get("enabled", True)),
                    ])
            else:
                local_rows.append(list(entry))
        self.tunnel_local = _TunnelRulesEditor(local_rows, p, self)

        # ---------- SOCKS5 ----------
        # **启用状态是一个 switch，不再是「端口 = 0 表示不启用」。**
        #
        # 旧写法把状态藏在数字里：`setSpecialValueText("不启用")` 让 spinbox
        # 在 0 处显示「不启用」三个字，靠一句 hint 解释「0 表示不启用」。代价是
        # ①状态不在控件上，用户得读完旁边那句话才知道现在是开是关；
        # ②用户把端口误删/清零 = 静默关掉一条配置好的规则，且没有任何提示；
        # ③0 与「还没配」两种含义混在同一个值里。
        #
        # 现在：switch 直接表达状态，spinbox 只管端口，关掉时整个端口输入
        # 一起禁用（看得见「这条现在不生效」），端口默认值 1080 也终于有了
        # 明确含义 —— 打开 switch 就有可用的代理端口，而不是要先知道去填 1080。
        #
        # 迁移：老配置 `tunnel_socks = 0` → switch 关；`> 0` → switch 开。
        # `_socks_enabled`（隧道面板里手动关掉过的那条）仍然尊重。
        self.socks_switch = Switch(
            checked=bool(saved_socks) and self._socks_enabled, palette=p,
            parent=self)
        # 存成实例属性：`_tunnel_rules()` 要靠它判断「开关关着，但原来是不是
        # 真的有一条规则」—— 只看开关会分不清「用户刚把它关掉」和「这条会话
        # 本来就没有 SOCKS」，后者不该凭空造一条出来。
        self._saved_socks = saved_socks
        self.socks_switch.setToolTip("启用 SOCKS5 代理")
        self.tunnel_socks = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.tunnel_socks.setRange(1, 65535)
        self.tunnel_socks.setValue(saved_socks or _DEFAULT_SOCKS_PORT)

        # **监听地址是缺的第 N 个参数，不是可选装饰。** `TunnelSpec.listen_host`
        # 在 `core/tunnel.py:184` 是**必校验**字段，而本对话框与隧道面板的添加
        # 框都从没给过它输入口 —— 两条路径都写死回环/默认，于是「想让同事从
        # 另一台机器连我的 SOCKS」（监听 0.0.0.0）这件事**在纯 UI 操作下做不到**，
        # 只能手改 sessions.json。下面这个下拉补上这个入口。
        self.socks_listen = QtWidgets.QComboBox()
        self.socks_listen.addItem(_LOOPBACK, _LOOPBACK)
        self.socks_listen.addItem(_WIDE_LISTEN, _WIDE_LISTEN)
        _idx = self.socks_listen.findData(self._socks_listen or _LOOPBACK)
        self.socks_listen.setCurrentIndex(max(0, _idx))
        self.socks_listen.setToolTip(
            "127.0.0.1：只有本机能用（默认）\n"
            "0.0.0.0：局域网内其它机器也能连到（请确认网络环境可信）")

        self.socks_switch.toggled.connect(self._sync_socks)
        self._sync_socks()

        # **排成两行，不是挤在一行。** 一行放「开关 + 标签 + 端口框 + 监听框」
        # 在 480px 的弹框里塞不下：实测文字之间被压到 0 间距（「端口」贴着
        # spinbox、「监听」贴着下拉），而每一项都是用户要读的字，不是装饰。
        # 两行之后开关独占一行、参数在下面一行并排，哪一项都不挤。
        socks_head = _plain(QtWidgets.QWidget())
        head_lay = QtWidgets.QHBoxLayout(socks_head)
        head_lay.setContentsMargins(0, 0, 0, 0)
        head_lay.setSpacing(10)
        socks_label = QtWidgets.QLabel("SOCKS5 代理")
        socks_hint = QtWidgets.QLabel("本地动态代理，浏览器指向它即可访问内网")
        socks_hint.setProperty("role", "muted")
        head_lay.addWidget(self.socks_switch)
        head_lay.addWidget(socks_label)
        head_lay.addWidget(socks_hint, 1)

        socks_params = _plain(QtWidgets.QWidget())
        params_lay = QtWidgets.QHBoxLayout(socks_params)
        params_lay.setContentsMargins(0, 0, 0, 0)
        params_lay.setSpacing(8)
        params_lay.addWidget(QtWidgets.QLabel("端口"))
        params_lay.addWidget(self.tunnel_socks)
        params_lay.addSpacing(12)
        params_lay.addWidget(QtWidgets.QLabel("监听"))
        params_lay.addWidget(self.socks_listen, 1)

        socks_row = _plain(QtWidgets.QWidget())
        socks_lay = QtWidgets.QVBoxLayout(socks_row)
        socks_lay.setContentsMargins(0, 0, 0, 0)
        socks_lay.setSpacing(8)
        socks_lay.addWidget(socks_head)
        socks_lay.addWidget(socks_params)

        tunnel_box = QtWidgets.QFrame()
        tunnel_box.setProperty("role", "card")
        tunnel_lay = QtWidgets.QVBoxLayout(tunnel_box)
        tunnel_lay.setContentsMargins(14, 12, 14, 13)
        tunnel_lay.setSpacing(10)
        tunnel_lay.addWidget(self.tunnel_local)
        tunnel_lay.addWidget(_sep_line(p))
        tunnel_lay.addWidget(socks_row)
        root.addWidget(tunnel_box)

        root.addStretch(1)

        # ---------- actions ----------
        # **主按钮按「这个框接下来会做什么」说话，不是按「它长什么样」说话。**
        #
        # 新建和编辑调的是同一个框，可两个调用点点下去之后做的事不一样：
        # `main_window._on_new_connection` 存完还会 `open_session(s)` **真的
        # 去连**；`_on_edit_session` 只 `store.upsert` 再刷新列表，**根本不连**。
        # 用户编辑一条已经能用的会话时，按钮写着「连接」——它既不连，也没说
        # 「只保存不连接」，除了让人猜没有别的作用。截图报的就是这一条。
        #
        # 判据用 `self._initial` 而不是 `initial`：标题（`setWindowTitle`）用的是
        # `not initial`，而 `initial or {}` 会把「传了空 dict」和「没传」归成
        # 同一档 —— 两处必须跟着同一份判断走，否则标题和按钮会分家。
        #
        # **图标跟着文字一起换。** 插头图标配「保存」是同一句自相矛盾的话，
        # 换个单元格里还留着「连接」的暗示。
        _saving = bool(self._initial)
        self.ok_btn = QtWidgets.QPushButton("保存" if _saving else "连接")
        self.ok_btn.setProperty("variant", "primary")
        self.ok_btn.setIcon(icons.icon("check" if _saving else "plug",
                                       p, 15, p.text_on_solid))
        self.ok_btn.setIconSize(QtCore.QSize(15, 15))
        self.ok_btn.setDefault(True)
        # **高度不再写死。** 原来是 `setFixedHeight(38)`，而同弹框里
        # 「添加规则/删除」走 QSS 的 33px、输入框 37px —— 三个高度并排，
        # 截图上一眼就能看出来（38 vs 33）。高度统一交给 QSS 的 `CONTROL_H`。
        # **用 setMinimumHeight 而不是 setFixedHeight**：后者写死的 38px
        # 不跟界面字号缩放，18pt 时输入框涨到 60px 而按钮还是 38px，不齐更严重。
        self.ok_btn.setMinimumHeight(qss.CONTROL_H)
        self.ok_btn.setMinimumWidth(120)
        self.ok_btn.clicked.connect(self.accept)

        cancel_btn = QtWidgets.QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setMinimumHeight(qss.CONTROL_H)
        cancel_btn.setMinimumWidth(90)

        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(10)
        actions.addStretch(1)
        actions.addWidget(cancel_btn)
        actions.addWidget(self.ok_btn)
        root.addLayout(actions)

        # Enter anywhere on the host field submits.
        self.host_edit.returnPressed.connect(self.accept)

    # ------------------------------------------------------------------

    def _browse_key(self) -> None:
        path, _ = msgbox.open_file(
            self, "选择 SSH 私钥", self.key_path_edit.text() or os.path.expanduser("~"), "All files (*)"
        )
        if path:
            self.key_path_edit.setText(path)

    def result_dict(self) -> dict:
        is_key = self.auth_tabs.currentIndex() == 1
        password = self.password_edit.text()
        # **两套凭据都要回吐，不要按当前页签把另一套写成空串。**
        # 三个输入框在构造时就已经从 `initial` 回填好了（上面 `password_edit` /
        # `key_path_edit` / `key_pass_edit` 那三行），所以「用户没打算改的那
        # 一套」一直好好地躺在控件里。原来的 `"" if is_key else …` 把它抹成
        # 空串，而 `main_window._on_edit_session` 拿整个 dict **重建**会话 ——
        # 于是「编辑一个密码会话、手滑点了一下私钥页签」就等于静默丢掉口令，
        # 界面上一个字的提示都没有。
        #
        # `auth_method` 只在**当前页签真的带着凭据**时才跟着翻：私钥页签
        # 走 `accept()` 的必填校验，空路径根本到不了这里；反过来，key 会话
        # 的密码框本来就是空的，点一下「密码」页签不该把它变成一个没有口令
        # 的密码会话。
        if is_key or not password:
            auth_method = "key" if is_key else self._initial.get(
                "auth_method", "password")
        else:
            auth_method = "password"
        return {
            "name": self.name_edit.text().strip(),
            "group": _group_value(self.group_edit),
            "host": self.host_edit.text().strip(),
            "port": int(self.port_edit.value()),
            "username": self.user_edit.text().strip(),
            "auth_method": auth_method,
            "password": password,
            "key_filename": self.key_path_edit.text().strip(),
            "key_passphrase": self.key_pass_edit.text(),
            "tunnels": self._tunnel_rules(),
            # **这两个字段本对话框没有输入框，但必须原样回吐。**
            #
            # `main_window._on_edit_session`（`main_window.py:2053`）拿
            # `result_dict()` 的键**整条重建** `SavedSession`，再 `upsert` 落盘 ——
            # 凡是没在这里回吐的字段，都会被 `SavedSession` 的 dataclass 默认值
            # （空串）顶掉。所以「回吐」在这个对话框里不是可选项：漏一个字段，
            # 就是「用户点一次编辑，那个字段就空了」，而且界面上不会有任何提示。
            #
            # **有输入框的字段同样要回吐，只是走的是另一支**（`name` / `group`
            # 直接从输入框读）。别把「加输入框」当成「这一条就覆盖了」——
            # 那个覆盖说的是「本对话框**没有**输入框的字段怎么办」，两支缺一不可。
            #
            # 今天全仓还没有 UI 写这两个字段，用户填不出来，所以还没有实际损失；
            # 但**任何人往这个对话框里加一个「备注」输入框，都一定会撞到这一行**。
            # 加新输入框的时候，请连同它在这里的回吐一起加，否则新字段同样会被
            # 整条重建吃掉。这条不能靠 `_on_edit_session` 那边兜底 —— 那边是
            # 拿整个 dict 重建的，**它没有机会知道哪些字段是「这次没打算改」的**。
            "notes": str(self._initial.get("notes") or ""),
            "last_dir": str(self._initial.get("last_dir") or ""),
        }

    def _sync_socks(self) -> None:
        """关掉 SOCKS 时把端口与监听地址一起禁用。

        **为什么用禁用而不是隐藏。** 隐藏会让这一行在开/关之间改变高度，
        对话框跟着跳一下；而且「这里原本有东西」这件事就看不见了，用户之后
        打开 switch 会看到一个凭空冒出来的输入框。禁用保留了原位，也让
        「这条现在不生效」这件事一眼可见 —— 而这正是旧写法（端口 0 = 不启用）
        最缺的信息。

        **关着的时候不要顺手改端口值。** 禁用的 spinbox 留着用户上次的端口，
        他打开 switch 就能接着用，而不是得重新输一遍。
        """
        on = self.socks_switch.isChecked()
        self.tunnel_socks.setEnabled(on)
        self.socks_listen.setEnabled(on)

    def _tunnel_rules(self) -> list[dict]:
        """The -L table plus the SOCKS port, in TunnelSpec.to_dict form."""
        from ..core.tunnel import TunnelSpec

        rules = [TunnelSpec(
            kind="local",
            local_port=row[0],
            remote_host=row[1],
            remote_port=row[2],
            description=row[3],
            listen_host=row[4],
        ).to_dict(enabled=bool(row[5]) if len(row) > 5 else True)
            for row in self.tunnel_local.rows()]
        socks_port = int(self.tunnel_socks.value())
        socks_listen = str(self.socks_listen.currentData() or _LOOPBACK)
        if self.socks_switch.isChecked():
            rules.append(TunnelSpec(
                kind="socks", local_port=socks_port, description="SOCKS5",
                listen_host=socks_listen,
            ).to_dict(enabled=True))
        elif self._saved_socks:
            # **开关关着但原来有规则 → 保留成「已配置、未启用」，而不是删掉。**
            #
            # 直接不吐这条是最省事的写法，也正是旧代码的做法（旧写法是
            # 「端口 0 就不吐」）。但那会让「在隧道面板里把某条 SOCKS 手动关掉
            # → 打开本对话框改个别的东西」**把这条规则从配置里抹掉** ——
            # `_socks_enabled` 这个字段就是为了防这件事才存在的。
            # `add_tunnel(enabled=False)` 本来就支持「记住但不监听」，所以
            # 无损的表达方式一直都在，丢掉它没有任何好处。
            rules.append(TunnelSpec(
                kind="socks", local_port=socks_port, description="SOCKS5",
                listen_host=socks_listen,
            ).to_dict(enabled=False))
        # **spinbox 装不下的那些 SOCKS 原样吐回去。**
        # 原来这个循环是 `saved_socks = ...` 直接覆盖，于是含多条 socks 的会话
        # 只剩最后一条、其余**静默丢弃**，而 `main_window._on_edit_session`
        # 又拿这个结果整条重建会话 —— 配置真的没了，落盘也没了。
        # 可达性比「手改 sessions.json」高得多：隧道面板的「添加 SOCKS」可以
        # 反复点，每加一条都会 `rulesChanged` 写回会话，所以**纯 UI 操作**
        # 就能造出多条 socks。
        rules.extend(self._extra_socks)
        return rules

    def accept(self) -> None:
        if not self.host_edit.text().strip():
            self._flag(self.host_edit, "请填写主机地址")
            return
        if not self.user_edit.text().strip():
            self._flag(self.user_edit, "请填写用户名")
            return
        if self.auth_tabs.currentIndex() == 1 and not self.key_path_edit.text().strip():
            self._flag(self.key_path_edit, "请选择私钥文件，或切换到密码认证")
            return
        # 上面的校验没过就 return，对话框还开着 —— 所以**走到这一步才动表**。
        # 提前把填错的行摘掉，等于用户改完主机名再点确定，发现规则表里少了几行
        # 而他已经不记得那几行长什么样了。
        #
        # `rows()` 的返回值这次用不上（`result_dict()` 马上会再读一遍，摘干净的
        # 表读出来就是同一批规则）；这次调用要的是它那个「把填错的行移走」的副作用。
        self.tunnel_local.rows()
        dropped = self.tunnel_local.take_dropped()
        if dropped:
            # 日志留痕的写法照 `core/snippets.py` 那边丢片段的同族位置：条数 +
            # 具体是哪几条，不记就等于没发生过。
            log.warning("连接对话框里有 %d 行端口转发填得不对，已移除：%s",
                        len(dropped), "；".join(dropped))
            msgbox.warning(self, "端口转发规则有 %d 行无效" % len(dropped),
                           "下面 %d 行填得不对，已经从表里移除，保存时不会生效：\n\n%s"
                           % (len(dropped), "\n".join(dropped)),
                           palette=self._palette)
        # **装不下的那些 SOCKS 说一句。** 它们已经原样保留（见
        # `_tunnel_rules`），所以不是「你要丢东西」的警告，而是「这个对话框
        # 只能编辑其中一条」的告知 —— 不说，用户会以为其它几条被这个对话框
        # 弄丢了，下次连上代理不通还要回头找。
        if self._extra_socks:
            log.warning("连接对话框只有一条 SOCKS 端口框，%d 条已原样保留未受编辑",
                        len(self._extra_socks))
            msgbox.information(
                self, "端口转发",
                "这个对话框只能编辑**第一条** SOCKS 规则，剩下 %d 条已原样保留"
                "（端口 %s）。\n\n要改它们请到会话的「端口转发」页里管理。"
                % (len(self._extra_socks),
                   "、".join(str(r.get("local_port", "?"))
                             for r in self._extra_socks)),
                palette=self._palette)
        # **监听地址被收窄也要说。** 收窄本身是对的（`::` 在本仓永远绑不上，
        # 见 `_normalize_listen`），但静默改掉用户写的值不行 —— 症状是
        # 「同事访问不通」而界面上没有任何线索。
        if _NARROWED:
            narrowed = sorted(set(_NARROWED))
            log.warning("连接对话框把 %d 条规则的监听地址收窄成 %s",
                        len(narrowed), _LOOPBACK)
            msgbox.information(
                self, "端口转发",
                "有 %d 条规则的「对外监听地址」是 %s，这个对话框只能填 "
                "%s 或 %s，已按 %s 保存。\n\n要对外监听请写 %s。"
                % (len(narrowed), "、".join(narrowed),
                   _LOOPBACK, _WIDE_LISTEN, _LOOPBACK, _WIDE_LISTEN),
                palette=self._palette)
        _NARROWED.clear()          # 排空，别让下一次对话框替这次背锅
        super().accept()

    def nativeEvent(self, event_type, message):
        # 无边框后拖动窗口要靠标题栏向系统报 HTCAPTION，逻辑在 titlebar 里。
        return titlebar.native_event(self, message)

    def _flag(self, widget: QtWidgets.QWidget, message: str) -> None:
        widget.setFocus()
        # 走 msgbox 而不是就地 new 一个 QMessageBox：就地 new 的那条还是
        # 原生标题栏，深色界面配一条浅灰亮条。
        box = msgbox._build(self, QtWidgets.QMessageBox.Icon.Warning,
                            "信息不完整", message, palette=self._palette)
        box.exec()
