"""Application settings dialog.

Everything configurable that was previously scattered -- the theme toggle in
the View menu, the font the terminal ignored, the scrollback cap the wheel
event hardcoded, the monitor interval nobody could reach -- lands here, and
:class:`AppSettings` is the single record of it.

The dialog is a thin editor: it edits a copy, and only writes it back on
accept, so Cancel really cancels.
"""
from __future__ import annotations

import copy
import pathlib

import paramiko
from PyQt6 import QtCore, QtWidgets

from sshclient.core import fonts
from sshclient.remote import hostkeys
from sshclient.ui import msgbox, titlebar
from sshclient.ui.design.v1 import qss

from ..core.settings import UI_SIZE_REF, AppSettings, accent_for
from .design.v1.palette import Palette, get_palette
from .design.v1.qss import build_qss
from .window_frame import enable_rounded_corners

#: 四个数值控件的范围**一律从这张表派生**，不要在手抄第二份。
#: 原来它们各自写死（8–18 / 8–28 / 0–200000 / 500–30000），而数据层那张表
#: 是 6–48 / 6–48 / 0–2,000,000 / 100–600,000 —— 两道闸范围各不相同，于是
#: 用户在设置框里手填不出程序本身支持的值，而改那份手抄常量时忘了同步就
#: 又是一处「静默收窄」。提到模块级是因为三个页面是三个独立方法。
_INT_RANGE = AppSettings._INT_RANGE

#: the "no explicit choice" entry; its value is AppSettings.term_font_family
AUTO_FONT = "自动选择"

#: 主题下拉框的可选项。**曾经和 `palette.THEMES` 同名**——上面那行 import
#: 把调色板的 THEMES 字典引进来，紧接着这里用显示列表把它整个盖掉，于是
#: 这个模块里任何想读调色板 THEMES 的代码都会拿到一个完全不同的东西，
#: 而且不报错。ruff 的 F811 报的就是这个。现在 import 侧不再引它。
THEMES_UI = [("深色", "dark"), ("浅色", "light")]


class SettingsDialog(titlebar.FitToScreenMixin, QtWidgets.QDialog):
    """Edits a copy of :class:`AppSettings`; see :meth:`result_settings`."""

    def __init__(self, settings: AppSettings, palette: Palette, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._draft = copy.deepcopy(settings)
        # 取消时回滚要用的原值。强调色预览会真的把主窗口改掉，
        # 不留一份原件就回不去了。
        self._original = copy.deepcopy(settings)
        self._palette = palette

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 自绘标题栏，和主窗口同一条。**内容里原本那个大标题「设置」删掉了**
        # ——它和标题栏说的是同一句话，留着就是同一行字出现两次。副标题留着，
        # 它说的是标题栏没说的事（这个对话框管什么）。
        self.titlebar = titlebar.attach_chrome(
            self, palette, title="设置", kinds=(titlebar.CLOSE,),
            icon_name="gear")
        root.addWidget(self.titlebar)

        body = QtWidgets.QWidget(self)
        root.addWidget(body, 1)
        # 超高时 mixin 把这块装进滚动区（titlebar.fit_to_screen）。
        # SettingsDialog 在 19pt 就超 1032 的工作区（默认 11pt 时 688px）。
        self._scroll_body = body
        bl = QtWidgets.QVBoxLayout(body)
        bl.setContentsMargins(22, 18, 22, 18)
        bl.setSpacing(14)

        sub = QtWidgets.QLabel("外观、终端与监控。修改立即生效并保存。")
        sub.setProperty("role", "muted")
        bl.addWidget(sub)

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_appearance(), "外观")
        tabs.addTab(self._build_terminal(), "终端")
        tabs.addTab(self._build_monitor(), "监控")
        tabs.addTab(self._build_hostkeys(), "主机密钥")
        bl.addWidget(tabs, 1)

        note = QtWidgets.QLabel(
            "终端快捷键：Ctrl+V 粘贴 · Ctrl+C 有选中时复制、没有选中时发送中断"
            "（SIGINT）· Ctrl+Shift+C 强制复制")
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        bl.addWidget(note)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok
                       ).setText("保存")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel
                       ).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bl.addWidget(buttons)

        # **必须把 `ui_size` 传进来。** 漏了它 `qss.apply` 会退回
        # `UI_SIZE_REF`，于是这个对话框**永远按 11pt 渲染**，哪怕用户把界面
        # 字号调到 18：主窗口变大、设置框纹丝不动（实测 11pt 与 18pt 下框内
        # metrics 完全相同）。而这个对话框**就是调字号的地方**，用户改完
        # 点保存，回头再打开设置还是 13px。
        # 同文件 402/419、msgbox.py:47、connect_dialog.py:147 都传了，
        # 全仓只有这一行漏。
        draft = getattr(self, "_draft", None)
        qss.apply(self, palette, dialog=True,
                  ui_size=draft.ui_font_size if draft is not None
                  else UI_SIZE_REF)
        # 四角 5px 圆角。**构造期就要装** —— `WA_TranslucentBackground` 是
        # 窗口创建期属性，show 之后再设原生窗口会重建、肉眼闪一下。
        # 见 window_frame.py：QSS 的 border-radius 在 frameless 弹窗上是 0 px。
        enable_rounded_corners(self, palette, border=False)

    def nativeEvent(self, event_type, message):
        # 无边框后拖动窗口要靠标题栏向系统报 HTCAPTION，逻辑在 titlebar 里。
        return titlebar.native_event(self, message)

    # ------------------------------------------------------------------ pages

    def _build_appearance(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 14, 4, 4)
        lay.setSpacing(12)

        self.theme_combo = QtWidgets.QComboBox()
        for label, value in THEMES_UI:
            self.theme_combo.addItem(label, value)
        idx = self.theme_combo.findData(self._draft.theme)
        self.theme_combo.setCurrentIndex(max(0, idx))
        lay.addWidget(self._row("主题", "深色与浅色配色", self.theme_combo))

        # 强调色，深浅各一行、**不跟主题联动**：跟着联动的话，用户切主题时
        # 正在编辑的那一行会被静默换掉，手上的活儿白干。
        self._accent_rows = {}
        self._accent_labels = {}
        for theme, label, hint in (("dark", "深色主题强调色", "按钮、选中态、焦点、Logo"),
                                   ("light", "浅色主题强调色", "同上")):
            self._accent_labels[theme] = label
            row = self._make_accent_row(theme, label, hint)
            lay.addWidget(self._row(label, hint, row))
        lay.addWidget(self._hint(
            "改成别的颜色只影响这一套主题；留空就是默认蓝。"))

        # **范围一律从 `AppSettings._INT_RANGE` 派生**（模块级 `_INT_RANGE`）。
        # 原来这里四个 spinbox 各自手抄了一份，加载时钳一次、界面范围也用它。
        self.ui_size = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.ui_size.setRange(*_INT_RANGE["ui_font_size"])
        self.ui_size.setSuffix(" pt")
        self.ui_size.setValue(
            self._clamp(self._draft.ui_font_size, *_INT_RANGE["ui_font_size"]))
        lay.addWidget(self._row("界面字号", "按钮、菜单、面板文字", self.ui_size))

        self.ui_font_combo = QtWidgets.QComboBox()
        self.ui_font_combo.addItem(AUTO_FONT, "")
        for name in fonts.ui_font_families():
            self.ui_font_combo.addItem(name, name)
        want = self._draft.ui_font_family or ""
        idx = self.ui_font_combo.findData(want)
        if idx < 0 and want:
            # saved face is gone (uninstalled / another machine): keep the
            # value visible instead of silently dropping the user's choice
            self.ui_font_combo.addItem(f"{want}（当前不可用）", want)
            idx = self.ui_font_combo.count() - 1
        self.ui_font_combo.setCurrentIndex(max(0, idx))
        lay.addWidget(self._row("界面字体", "标题、菜单、按钮、列表文字", self.ui_font_combo))

        # The app font *is* the interface font and no stylesheet rule pins a
        # family, so setting it re-flows the whole app -- the dialog included.
        # That makes this combo its own preview, and it also means the change
        # has to be undone on cancel; see open_settings.
        self.ui_font_combo.currentIndexChanged.connect(self._preview_ui_font)
        self.ui_size.valueChanged.connect(self._preview_ui_font)
        self._preview_ui_font()

        lay.addWidget(self._hint(
            "界面字体和字号会立即应用到整个窗口；取消则恢复原来的外观。"))
        lay.addStretch(1)
        return page

    def _preview_ui_font(self) -> None:
        app = QtWidgets.QApplication.instance()
        if app is None:
            return
        family = self.ui_font_combo.currentData() or ""
        app.setFont(fonts.ui_font(self.ui_size.value(), family))

    def _fill_font_combo(self, refresh: bool = True) -> None:
        """(Re)build the terminal font list, keeping the current selection.

        Rebuilding rather than pre-hiding: a combo has no per-item visibility,
        and the selection has to survive the toggle -- losing it because the
        user ticked a box would be its own small bug.

        *refresh* is off during the initial build because _build_terminal
        creates term_size *after* this runs, and the preview needs it.
        """
        # On the first fill the combo exists but is empty, so currentData()
        # is "" -- reading it there would throw away the saved face and land
        # on 自动选择. Only trust it once there is something in the list.
        want = (self.font_combo.currentData() if self.font_combo.count()
                else (self._draft.term_font_family or ""))
        include_all = self.show_all_fonts.isChecked() if hasattr(self, "show_all_fonts") else False

        blocked = self.font_combo.blockSignals(True)
        self.font_combo.clear()
        self.font_combo.addItem(AUTO_FONT, "")
        for name in fonts.mono_font_families(include_all=include_all):
            self.font_combo.addItem(name, name)
        idx = self.font_combo.findData(want)
        if idx < 0 and want:
            # either filtered out, or gone from the machine (uninstalled /
            # another machine). Either way the value stays visible and
            # selected rather than being silently dropped.
            label = f"{want}（当前不可用）" if not fonts._is_installed(want) \
                else f"{want}（已过滤）"
            self.font_combo.addItem(label, want)
            idx = self.font_combo.count() - 1
        self.font_combo.setCurrentIndex(max(0, idx))
        self.font_combo.blockSignals(blocked)
        if refresh:
            self._refresh_preview()

    def _on_show_all_fonts(self, checked: bool) -> None:
        self._fill_font_combo()

    def _build_terminal(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 14, 4, 4)
        lay.setSpacing(12)

        self.font_combo = QtWidgets.QComboBox()
        self._fill_font_combo(refresh=False)   # term_size does not exist yet
        lay.addWidget(self._row("字体", "自动 = 用系统里第一个已安装的等宽字体",
                                self.font_combo))

        # 楷体 / 隶书 / 幼圆 / 仿宋 and the DOS rasters are fixed pitch for the
        # wrong reason -- every Han glyph is the same em box, and Fixedsys is a
        # bitmap -- so they pass the one test the list applies. They are hidden
        # by default rather than removed, because the user asked not to have
        # installed fonts silently taken away.
        self.show_all_fonts = QtWidgets.QCheckBox("显示全部字体")
        self.show_all_fonts.setToolTip(
            "连同楷体、隶书、幼圆、仿宋等排版字体和 DOS 点阵字体一起列出")
        self.show_all_fonts.toggled.connect(self._on_show_all_fonts)
        holder = QtWidgets.QWidget()
        hlay = QtWidgets.QHBoxLayout(holder)
        hlay.setContentsMargins(16, 0, 16, 0)
        hlay.addWidget(self.show_all_fonts)
        hlay.addStretch(1)
        lay.addWidget(holder)

        self.term_size = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.term_size.setRange(*_INT_RANGE["term_font_size"])
        self.term_size.setSuffix(" pt")
        self.term_size.setValue(self._clamp(self._draft.term_font_size, *_INT_RANGE["term_font_size"]))
        lay.addWidget(self._row("字号", "终端字符大小", self.term_size))

        self.scrollback = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.scrollback.setRange(*_INT_RANGE["scrollback"])
        self.scrollback.setSingleStep(500)
        self.scrollback.setSuffix(" 行")
        self.scrollback.setValue(self._clamp(self._draft.scrollback, *_INT_RANGE["scrollback"]))
        lay.addWidget(self._row("回滚行数", "鼠标滚轮可上翻的历史行数",
                                self.scrollback))

        self.font_preview = QtWidgets.QLabel()
        self.font_preview.setProperty("role", "card")
        self.font_preview.setMinimumHeight(56)
        self.font_preview.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.font_preview.setText("root@prod-01:/srv# ls -la 0123456789")
        lay.addWidget(self.font_preview)

        self.font_combo.currentIndexChanged.connect(self._refresh_preview)
        self.term_size.valueChanged.connect(self._refresh_preview)
        self._refresh_preview()
        lay.addStretch(1)
        return page

    def _build_monitor(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 14, 4, 4)
        lay.setSpacing(12)

        self.monitor_ms = qss.compact_spinbox(QtWidgets.QSpinBox())
        self.monitor_ms.setRange(*_INT_RANGE["monitor_interval_ms"])
        self.monitor_ms.setSingleStep(500)
        self.monitor_ms.setSuffix(" ms")
        self.monitor_ms.setValue(
            self._clamp(self._draft.monitor_interval_ms,
                        *_INT_RANGE["monitor_interval_ms"]))
        lay.addWidget(self._row("刷新间隔", "CPU / 内存轮询周期",
                                self.monitor_ms))
        lay.addWidget(self._hint(
            "间隔越短越实时，但每个会话都会多发一次远程命令。"))
        lay.addStretch(1)
        return page

    def _build_hostkeys(self) -> QtWidgets.QWidget:
        """「已信任主机」管理页。

        **这个面板存在的理由不是「方便」，是「可撤销」。** 信任一旦写进
        known_hosts 就固化了 —— 之后这台机器被换钥我们**发现不了**。
        所以用户必须有一条路把某条信任删掉，让下次连接重新问一遍。
        """
        page = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(page)
        lay.setContentsMargins(4, 14, 4, 4)
        lay.setSpacing(10)

        head = QtWidgets.QLabel(
            "已信任的主机。删掉一条之后，下次连接会重新询问是否信任。")
        head.setProperty("role", "muted")
        head.setWordWrap(True)
        lay.addWidget(head)

        self._hostkey_list = QtWidgets.QListWidget()
        self._hostkey_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self._hostkey_list.setMinimumHeight(180)
        lay.addWidget(self._hostkey_list, 1)

        self._hostkey_info = QtWidgets.QLabel("")
        self._hostkey_info.setProperty("role", "muted")
        self._hostkey_info.setWordWrap(True)
        lay.addWidget(self._hostkey_info)

        btns = QtWidgets.QHBoxLayout()
        self._btn_hostkey_remove = QtWidgets.QPushButton("删除这条信任")
        self._btn_hostkey_remove.clicked.connect(self._remove_trusted_host)
        self._btn_hostkey_import = QtWidgets.QPushButton("导入 ~/.ssh/known_hosts")
        self._btn_hostkey_import.setToolTip(
            "把 OpenSSH 客户端已信任的主机导入进来。\n"
            "只导入你点选的，不会覆盖 MavisSSH 已有的记录。")
        self._btn_hostkey_import.clicked.connect(self._import_known_hosts)
        btns.addWidget(self._btn_hostkey_remove)
        btns.addWidget(self._btn_hostkey_import)
        btns.addStretch(1)
        lay.addLayout(btns)

        self._refresh_hostkey_list()
        return page

    def _refresh_hostkey_list(self) -> None:
        self._hostkey_list.clear()
        for t in hostkeys.list_trusted_hosts():
            item = QtWidgets.QListWidgetItem("%s   (%s)" % (t.hostname, t.key_type))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, (t.hostname, t.key_type))
            item.setToolTip(t.fingerprint)
            self._hostkey_list.addItem(item)
        self._hostkey_info.setText(
            "共 %d 条信任记录。" % self._hostkey_list.count() if self._hostkey_list.count()
            else "还没有任何信任记录 —— 第一次连接某台主机时会问你要不要信任。")

    def _remove_trusted_host(self) -> None:
        item = self._hostkey_list.currentItem()
        if item is None:
            return
        hostname, key_type = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if msgbox.question(
                self, "删除信任",
                "删除对 %s 的信任？\n\n"
                "下次连接这台主机时会重新询问是否信任。" % hostname
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        if hostkeys.remove_trusted_host(hostname, key_type):
            self._refresh_hostkey_list()

    def _import_known_hosts(self) -> None:
        src = pathlib.Path.home() / ".ssh" / "known_hosts"
        if not src.is_file():
            msgbox.warning(self, "没有找到 known_hosts",
                           "找不到：%s\n\n"
                           "OpenSSH 客户端一般把已信任的主机记在这里。" % src)
            return
        try:
            hk = paramiko.HostKeys(str(src))
        except Exception as exc:      # noqa: BLE001
            msgbox.warning(self, "读取失败", "读不了 %s：\n%s" % (src, exc))
            return
        names = sorted(hk.entries.keys())
        if not names:
            msgbox.warning(self, "没有可导入的条目", "%s 里没有主机记录。" % src)
            return
        picked = msgbox.multi_choice(
            self, "导入已知主机",
            "从 %s 导入。已存在的记录不会被覆盖。" % src,
            names)
        if not picked:
            return
        wanted = [names[i] for i in picked]
        imported = hostkeys.import_known_hosts(src, hostnames=wanted)
        self._refresh_hostkey_list()
        if imported:
            msgbox.information(
                self, "导入完成", "已导入 %d 台主机：\n\n%s"
                % (len(imported), "\n".join("    " + h for h in imported)))
        else:
            msgbox.information(
                self, "没有导入", "选中的主机都已经在信任库里了。")

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _clamp(value, lo, hi):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return lo

    def _make_accent_row(self, theme: str, label: str, hint: str):
        """一个「色块按钮 + 默认」的小控件。

        **用 QPushButton 而不是 QLabel/Frame 承载颜色。** 标签的
        ``focusPolicy`` 是 NoFocus，键盘用户 tab 不到，也读不出这是个可点的
        东西——可访问性上过不去。按钮天生 StrongFocus，而且把十六进制写进
        文本里，色觉障碍的用户也有一个不靠颜色的读法。

        控件（色块 + 默认）实测约 130px，``_row`` 给的 190px 够用，
        所以**不去动那个 190**——它被两页 7 处共用，一放宽主题下拉、字号、
        字体、回滚会连带一起拉宽。
        """
        holder = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        swatch = QtWidgets.QPushButton()
        swatch.setProperty("role", "accentSwatch")
        swatch.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        # **高度改由 QSS 的 min-height 承担**，不再 setFixedHeight。
        # 固定 28px 不缩放，而 QSS 那边的 padding / font-size 会跟着
        # ui_font_size 放大 —— 18pt 时固定高度就装不下了，文字被截。
        swatch.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        # 无文字的按钮必须带 tooltip——那是用户认出它的唯一线索。
        # （这里有文字，但空串状态下 tooltip 仍然兜底。）
        swatch.setToolTip("点击选择强调色")
        swatch.clicked.connect(lambda _, th=theme: self._pick_accent(th))
        lay.addWidget(swatch)

        reset = QtWidgets.QPushButton("默认")
        # **原来这里写死 setFixedHeight(28)。** 它不缩放，而同一行的色块
        # 已经按同样的理由搬进 QSS 跟着字号走（见 :309-311）—— 18pt 下色块
        # 62px、这个按钮仍 28px，文字被横切成半截。高度交给 QSS 的
        # `min-height`，与紧邻的注释自洽。
        #
        # **但光靠 QSS 的 `min-height` 不够**：它只是**下限**，Qt 仍按
        # font-size + padding + border 算 sizeHint，实测「默认」是 40px、
        # 同页的 QComboBox 是 34px —— 一行里差 6px。`min-height` 管不住
        # sizeHint，所以这里要显式给到 `CONTROL_H`，与 connect/tunnel
        # 的底部按钮同一个来源（qss.CONTROL_H = 34）。
        reset.setMinimumHeight(qss.CONTROL_H)
        reset.setToolTip("恢复内置的强调色")
        reset.clicked.connect(lambda _, th=theme: self._set_accent(th, ""))
        lay.addWidget(reset)

        holder._swatch = swatch          # type: ignore[attr-defined]
        holder._reset = reset            # type: ignore[attr-defined]
        # **先登记再刷新。** 顺序反了的话 _refresh_accent_row 在字典里查不到
        # 这一行直接 return，swatch 的文本就一直是空的——按钮没有文字，
        # 既看不出是什么颜色，_uibtns 那条"无文字按钮必须有 tooltip"也会红。
        self._accent_rows[theme] = holder
        self._refresh_accent_row(theme)
        return holder

    def _accent_value(self, theme: str) -> str:
        return accent_for(self._draft, theme)

    def _refresh_accent_row(self, theme: str) -> None:
        """把这一行的色块和文字刷成当前值。

        文字色取**该行自己的** 配色，不是当前主题的——两行是独立的。
        """
        row = self._accent_rows.get(theme)
        if row is None:
            return
        val = self._accent_value(theme)
        p = get_palette(theme, val)
        swatch = row._swatch
        swatch.setText(val.upper() if val else "默认")
        swatch.setAccessibleName("%s，当前 %s"
                                 % (self._accent_labels.get(theme, theme),
                                    val or "默认"))
        # **焦点环必须自带。** 这里的 setStyleSheet 优先级比 qss.py 里那条
        # `QPushButton:focus` 高，不自己写就一点变化都没有——评审实测焦点态
        # 0 像素差异，而同一排的「默认」按钮反而有 260 像素焦点环。键盘用户
        # 看不见自己在哪。
        # 描边色用**中性色**而不是 accent：accent 就是这块底色，用它描边
        # 等于没描。
        #
        # **度量（font-size / border-radius / padding / min-height）搬去 QSS
        # 了，这里只留颜色。** `setStyleSheet` 的串是字面量，**不经
        # `qss.py` 的 `_scale_metrics()`** —— 而那四项全在 `_SCALE_PROPS`
        # 里。留在内联表里等于用户调界面字号时它们纹丝不动：`font-size: 12px`
        # 钉死字号、`padding: 4px 10px` 钉死内边距，配合下面那个
        # `setFixedHeight(28)`，18pt 时文字直接被 28px 截掉。评审实测
        # 同一页的纯 QSS 按钮 15→31px 跟着放大，而这个色板恒定 12px。
        # 改由 `qss.py` 的 `QPushButton[role="accentSwatch"]` 承担。
        #
        # **这里仍然用 `accent` 而不是 `accent_solid`，是故意的。**
        # 主按钮的底色现在确实是 `accent_solid`（压深过一档），但这个色板
        # 的职责是**让用户看清自己挑的那个 RGB** —— 摆实心档的话，两个相近
        # 的候选色压深之后更难分辨，而取色器要的是「我选的那个色」。
        # 界面里主按钮是同色相的深一档这件事，由 `_accent.py` 的
        # 「实心档与用户选的色同色相」那条判据钉着，不会退回默认蓝。
        swatch.setStyleSheet(
            "QPushButton { background: %s; color: %s; border: 1px solid %s; }"
            "QPushButton:focus { border: 2px solid %s; }"
            "QPushButton:hover { border-color: %s; }"
            % (p.accent, p.text_on_accent, p.border, p.text, p.text))

    def _pick_accent(self, theme: str) -> None:
        cur = self._accent_value(theme)
        got = msgbox.open_color(self, "选择强调色", cur)
        if got is None:
            return              # 用户取消，别动
        self._set_accent(theme, got)

    def _set_accent(self, theme: str, value: str) -> None:
        if theme == "dark":
            self._draft.accent_dark = value
        else:
            self._draft.accent_light = value
        self._refresh_accent_row(theme)
        self._preview_accent()

    def _preview_accent(self) -> None:
        """把当前草案里的强调色推到主窗口上。

        **只调一次 ``apply_theme``**——它内部已经做了 icons.clear_cache、
        qss.set_current、_retint_menu_icons、重载会话列表这一整套。手工挑几个
        步骤重放一遍，漏掉 qss.set_current 的话，下一个弹窗会留着预览色。
        设置框**自己**的样式和标题栏不在主窗口那套里，得单独刷。
        """
        win = self.parent()
        # 鸭子类型，**不要 import MainWindow** —— 它 import 本模块，
        # 那样就成循环依赖了。这里只要它有 apply_theme 就行。
        if callable(getattr(win, "apply_theme", None)):
            win.apply_theme(self._draft.theme,
                            accent_for(self._draft, self._draft.theme))
        self.setStyleSheet(build_qss(
            get_palette(self._draft.theme,
                        accent_for(self._draft, self._draft.theme)),
            ui_size=self._draft.ui_font_size))
        if getattr(self, "titlebar", None) is not None:
            self.titlebar.apply_palette(
                get_palette(self._draft.theme,
                            accent_for(self._draft, self._draft.theme)))

    def _restore_accent(self) -> None:
        """取消时把主窗口拨回原值。"""
        win = self.parent()
        # 鸭子类型，**不要 import MainWindow** —— 它 import 本模块，
        # 那样就成循环依赖了。这里只要它有 apply_theme 就行。
        if callable(getattr(win, "apply_theme", None)):
            win.apply_theme(self._original.theme,
                            accent_for(self._original, self._original.theme))
        self.setStyleSheet(build_qss(
            get_palette(self._original.theme,
                        accent_for(self._original, self._original.theme)),
            ui_size=self._original.ui_font_size))
        if getattr(self, "titlebar", None) is not None:
            self.titlebar.apply_palette(
                get_palette(self._original.theme,
                            accent_for(self._original, self._original.theme)))

    def _row(self, label: str, hint: str, editor: QtWidgets.QWidget
             ) -> QtWidgets.QWidget:
        box = QtWidgets.QFrame()
        box.setProperty("role", "card")
        lay = QtWidgets.QHBoxLayout(box)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)

        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        name = QtWidgets.QLabel(label)
        name.setProperty("role", "dim")
        sub = QtWidgets.QLabel(hint)
        sub.setProperty("role", "muted")
        sub.setWordWrap(True)
        text.addWidget(name)
        text.addWidget(sub)

        # 读屏软件只念得出控件自己的**可访问名**，而这一页的输入框全是空的
        # （QLineEdit/QSpinBox/QComboBox 都没有文字），于是七个框全都被念成
        # 「编辑框」；键盘用户 Tab 过去也看不出它属于上面哪一行。标题就在
        # 眼前，缺的是那条连接。
        #
        # `setBuddy` 而不是各写各的 label：它顺带把标题的助记键接到控件上，
        # 而两份文案（可见标题 + 可访问名）迟早会漂，所以**可访问名就用可见
        # 标题本身**，界面上改了这里跟着改。
        name.setBuddy(editor)
        editor.setAccessibleName(label)

        editor.setFixedWidth(190)
        lay.addLayout(text, 1)
        lay.addWidget(editor, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        return box

    @staticmethod
    def _hint(text: str) -> QtWidgets.QLabel:
        lab = QtWidgets.QLabel(text)
        lab.setProperty("role", "muted")
        lab.setWordWrap(True)
        return lab

    def _refresh_preview(self) -> None:
        family = self.font_combo.currentData() or ""
        size = self.term_size.value()
        font = fonts.mono_font(size, family)
        self.font_preview.setFont(font)

    def result_settings(self) -> AppSettings:
        out = self._draft
        out.theme = self.theme_combo.currentData() or "dark"
        out.ui_font_size = self.ui_size.value()
        out.ui_font_family = self.ui_font_combo.currentData() or ""
        out.term_font_family = self.font_combo.currentData() or ""
        out.term_font_size = self.term_size.value()
        out.scrollback = self.scrollback.value()
        out.monitor_interval_ms = self.monitor_ms.value()
        # 主题下拉换了的话，accent 该跟着存到对应的那一份上
        out.accent_dark = self._draft.accent_dark
        out.accent_light = self._draft.accent_light
        return out


def open_settings(parent, settings: AppSettings, palette: Palette,
                  dlg: SettingsDialog | None = None) -> AppSettings | None:
    """Run the dialog. Returns the edited settings, or None if cancelled.

    *dlg* lets a caller supply an already-built dialog. Nothing in the app
    needs it; it exists so a test can reach the dialog this function opens,
    which it otherwise creates privately and then exec()s.
    """
    if dlg is None:
        dlg = SettingsDialog(settings, palette, parent)
    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        # The dialog previews by setting the *application* font, so backing out
        # has to put the original back or the window keeps a face the user
        # just rejected. On accept MainWindow.apply_settings sets it anyway,
        # so only the cancel path needs this.
        # 强调色预览是**真的**把主窗口改了，取消必须拨回去。
        dlg._restore_accent()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.setFont(fonts.ui_font(settings.ui_font_size,
                                      settings.ui_font_family))
        return None
    return dlg.result_settings()
