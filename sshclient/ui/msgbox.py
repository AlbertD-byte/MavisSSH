"""所有弹窗走这一套，保证顶上一条都是同一个自绘标题栏。

**为什么不能继续用 `QMessageBox.warning(...)` 这种静态便捷方法。** 它们在
内部自己 new 一个 `QMessageBox`，没有地方插标题栏，于是那条由系统主题决定
的原生标题栏又回来了——深色界面配一条浅灰亮条，正是这一轮要消掉的东西。
而这些静态调用散落在五个文件里二十多处，逐个改成实例写法既啰嗦又**必然
会漏**，漏掉的那个就是一条又白又黑的原生栏。

所以这里包一层：名字不变，行为不变（返回值、按钮角色、`exec()` 语义都照
旧），只是多带一条自绘标题栏。

配色不靠调用点传——那正是会漏的第二个地方。由 `MainWindow.apply_theme`
经 `qss.set_current()` 记一份，弹窗自己去取。

`QFileDialog` / `QInputDialog` 同理：它们是 Qt 自己的复杂对话框，行为一个字
都没改，只在最上面插一条标题栏。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from . import titlebar
from .design.v1 import qss
from .window_frame import enable_rounded_corners

_ICON_FOR = {
    QtWidgets.QMessageBox.Icon.Critical: "ban",
    QtWidgets.QMessageBox.Icon.Warning: "info",
    QtWidgets.QMessageBox.Icon.Information: "info",
    QtWidgets.QMessageBox.Icon.Question: "info",
}


# ------------------------------------------------------------------ 三个子类

# 这三个 Qt 自己的对话框内部布局是 QGridLayout，PyQt6 一个 insert 方法都
# 没暴露，所以标题栏是"贴在最上面 + 把内容顶下去"实现的。
#
# **`resizeEvent` / `nativeEvent` / `showEvent` 必须写在真正的 Qt 子类上。**
# 它们都是虚方法，从纯 Python mixin 继承的话 sip 算不对 vtable 槽位，一调
# 就跳野指针（`nativeEvent` 那条尤其致命：窗口一 show() 就 0xC000041D）。
# 所以这里是三个老老实实的子类，不是 mixin。

#: 顶层窗口用这个 Qt 属性挂着自己的 `Palette`（见 `main_window._PALETTE_PROP`）。
#: 约定放在这里是因为**读它的是本模块** —— 全仓只有 `_install` 需要知道。
_PALETTE_PROP = "mavisPalette"

#: 本模块所有弹框的**宽度下限**。
#:
#: **参照「关于 MavisSSH」框实测的 372px 取整到 400。** 那个框之所以「看起来
#: 正常」，不是因为有人给它设过宽度，而是因为它的正文长 —— QMessageBox 照着
#: 内容自己算，算出来 372。短消息就没有这个运气：
#:
#: | 弹框 | 实测宽度（修之前） |
#: |------|------------------|
#: | 关于 | 372 |
#: | 崩溃兜底（`KeyboardInterrupt:`） | **242** |
#: | 输入框（「目录名：」） | **200** |
#:
#: 242 那个是用户截图报的：正文折成两行、说明文字也折行，右下角「显示详情…」
#: 的按钮还被挤到只剩半个。**按钮被裁比字折行更糟** —— 那是唯一能看到堆栈的
#: 入口。
#:
#: **为什么是常数而不是按内容算。** 正文标签的 sizeHint 会随界面字号缩放
#: （QSS 的 `font-size` 在 `_SCALE_PROPS` 里），按内容算出来的下限也跟着缩放，
#: 于是 6pt 时下限塌到 200 出头、48pt 时又冲上 600 —— 而那几个字号下真正
#: 需要宽度的是正文自己，`sizeHint()` 已经把它撑开了，常数那一档根本轮不到
#: 插手。本仓其它弹框也是常数下限（连接框 480、设置框 460、转发框 430），
#: 这里跟着同一套写法。
_DIALOG_MIN_W = 400


def _palette_of(widget) -> object | None:
    """沿父链往上找第一个挂了配色的祖先，返回它的 `Palette`。

    **优先用它，而不是 `qss.current()`。** 模块级全局只有一个槽位：同进程里
    开两个主题不同的主窗口时，后 `apply_theme` 的会覆盖先前的，于是**先开的
    那个窗口之后弹出的任何对话框都用另一个窗口的配色**。
    只有「没有父窗口」的那种框（崩溃兜底，早于任何窗口）才真的只能靠全局。
    """
    w = widget
    while w is not None:
        p = w.property(_PALETTE_PROP)
        if p is not None:
            return p
        w = w.parentWidget()
    return None


def _install(dialog, title, palette, icon_name):
    """三个子类的共同部分：套主题 + 挂标题栏 + 圆角。"""
    p = palette or _palette_of(dialog.parentWidget()) or qss.current()
    dialog.setStyleSheet(qss.build_qss(p, dialog=True,
                                       ui_size=qss.current_ui_size()))
    # **圆角在这里统一装**，因为这四个子类共用这一个收口 —— 挨个在构造里写
    # 就是典型的「漏一个就有一个方角」。实测 QSS 的 `border-radius` 在 frameless
    # 弹窗上同样是 0 px（标题栏浮层满宽铺角），必须浮层挖角，见 window_frame。
    # `border=False`：弹窗靠 bg_dialog 比主窗口亮一档已经有层次，再加一圈描边
    # 就是两套实现分叉（这条是原 WindowFrame 的结论，保留）。
    enable_rounded_corners(dialog, p, border=False)
    if not titlebar.frameless_enabled():
        return None
    return titlebar.inject_chrome(dialog, title, p, icon_name=icon_name)


def _on_show(dialog, tb, state):
    """showEvent 共同部分。

    **顶边距必须在这里做**，不能在构造时做：`setText` / `setIcon` 会让
    QMessageBox 重排布局，之前设的边距被冲掉，症状是正文被标题栏盖掉一半。
    showEvent 在正文设完之后才跑，时机正好。
    """
    if tb is None:
        return
    if not state["padded"]:
        state["padded"] = True
        titlebar.pad(dialog, tb)
    else:
        titlebar.pin(dialog, tb)


def _pin_min_width(dialog: QtWidgets.QDialog) -> None:
    """把 :data:`_DIALOG_MIN_W` 这个宽度下限钉回去。

    **钉在 `resizeEvent` 里，而不是只在 `showEvent` 里。** Qt 的
    `QMessageBox` / `QInputDialog` 各自有一个 `updateSizeHint()`：它先
    `setMinimumSize(0, 0)`，再按当前内容重算 `setMinimumWidth` —— 而那个
    sizeHint 本身就是「按短内容算出来的窄值」。实测触发时机有**两处**：

    * **show 时**：``resizeEvent`` 序列 `400 → 242 → 400 → 242`，
      showEvent 返回之后还有一轮「重排 → 重算 → 收窄」的级联，把任何
      写在 showEvent 里的补钉盖掉；
    * **展开「显示详情…」时**：整个布局重排，又收窄一次。这正是用户截图
      报的那一步——按钮挤在窄框里。

    只在 `showEvent` 里补，第一处就已经钉不住，第二处完全碰不到。而
    `resizeEvent` 两处都覆盖得到：**每次 Qt 要把它收窄，必然先发一个
    resize**，我们就地按回去。

    **不会死循环**：第二次进来时 `minimumWidth()` 已经达标，函数第一行就
    返回；`adjustSize()` 引发的那次递归 resize 同理。

    **只在比下限小的时候补。** 下限是地板不是尺寸：调用方显式把它调宽过
    （比如将来某个框自带更宽的最小宽度），这里不该把它按回去。
    """
    if dialog.minimumWidth() >= _DIALOG_MIN_W:
        return
    dialog.setMinimumWidth(_DIALOG_MIN_W)
    # 不 adjustSize 的话下限要到下一次 Qt 自己排版时才生效，而用户已经
    # 看到那个窄框了。
    dialog.adjustSize()


class ThemedMessageBox(QtWidgets.QMessageBox):
    def __init__(self, parent=None, title: str = "", palette=None,
                 icon_name: str = "info"):
        super().__init__(parent)
        self.setWindowTitle(title)
        # **宽度下限在构造里先设一次，`showEvent` 里再补一次。**
        # 只在构造里设会被 Qt 的 `updateSizeHint()` 抹掉（见 `_pin_min_width`），
        # 只在 showEvent 里设则 `adjustSize()` 的第一次排版看不到它。
        # 放构造里还有个好处：`_build` 覆盖不到的路径（`about()`、崩溃兜底
        # 框、将来任何一处 `ThemedMessageBox(...)`）也一并算上。
        self.setMinimumWidth(_DIALOG_MIN_W)
        self._st = {"padded": False}
        self._tb = _install(self, title, palette, icon_name)

    def showEvent(self, event):
        super().showEvent(event)
        _on_show(self, self._tb, self._st)
        # 宽度下限不在这里补 —— 见 `resizeEvent`。show 那一轮级联会把它盖掉，
        # 补了也是白补（实测：showEvent 里补钉之后 resizeEvent 序列是
        # 400 -> 242 -> 400 -> 242）。

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._tb is not None:
            titlebar.pin(self, self._tb)
        # **放在最后**：Qt 每次要收窄这个框都先发 resize（见 `_pin_min_width`
        # 里实测的序列），就地按回去。这是唯一一处能同时盖住「show 时」和
        # 「展开详情时」两个触发点的地方 —— 实测只在 showEvent 里补的话，
        # show 的级联会盖掉，展开详情那一轮完全碰不到。
        _pin_min_width(self)

    def nativeEvent(self, event_type, message):
        return titlebar.native_event(self, message)


class ThemedFileDialog(QtWidgets.QFileDialog):
    def __init__(self, parent=None, caption: str = "", directory: str = "",
                 palette=None, icon_name: str = "folder"):
        super().__init__(parent, caption, directory)
        self._st = {"padded": False}
        self._tb = _install(self, caption, palette, icon_name)

    def showEvent(self, event):
        super().showEvent(event)
        _on_show(self, self._tb, self._st)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._tb is not None:
            titlebar.pin(self, self._tb)

    def nativeEvent(self, event_type, message):
        return titlebar.native_event(self, message)


class ThemedColorDialog(QtWidgets.QColorDialog):
    """带自绘标题栏的取色器。

    **为什么不能直接用静态的 ``QColorDialog.getColor()``。** 它在内部 new 一个
    框出来，调用方拿不到实例，也就无从往上面插标题栏——那 11px 留白里露出来的
    是 Qt 原生的浅色标题栏，在深色界面上就是一条白亮条（正是 titlebar.py 存
    在的理由）。所以只能照上面三个那样自己子类化。

    **构造时必须清掉上边距。** ``QColorDialog`` 的布局自带边距，标题栏会
    落在 QRect(11, 11, ...) 而不是 QRect(0, 0, ...)——顶端就漏出一条
    ``#efefef``。评审实测：清掉之后 y=0、横贯整个宽度、顶端与正文同色。

    **但左右下三条边不能一起清。** 原来写的是 ``setContentsMargins(0,0,0,0)``，
    结果整个内容区顶到窗口边：实测「Cancel」右缘 x=665 而弹窗宽 666 ——
    **只差 1px**，按钮边框和弹窗边缘撞在一起（用户截图报的正是这个）；
    左边一整列控件的 x 全是 0。现在只清上边、左右下各留 12px：

        现状(0,0,0,0)     Cancel 右缘 665 / 弹窗宽 666 -> 留白 1px
        改后(12,0,12,12)  Cancel 右缘 677 / 弹窗宽 690 -> 留白 13px

    **标题栏必须从布局里摘出来。** 它是被 `inject_chrome` 用
    ``lay.insertWidget(0, tb)`` 放进去的 —— 一旦布局有了左右边距，布局就会
    把它一起缩进去，实测 ``geo=(12, 0, 666, 38)``：左边空 12px、离右缘
    38px，而关闭按钮是按「紧贴右缘」画的，于是整个按钮串到中间去了。
    所以摘出来改由 ``pin()`` 贴顶满宽，内容靠**上边距**让位 ——
    这正是 QMessageBox / QFileDialog / QInputDialog 那三个走 ``pad()`` 的路子。
    """

    #: 构造期固定这个：左右下留白。
    _MARGIN = 12

    def __init__(self, parent=None, title: str = "选择颜色", palette=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self._tb = _install(self, title, palette, "pencil")
        lay = self.layout()
        if lay is not None:
            m = self._MARGIN
            lay.setContentsMargins(m, titlebar.TITLEBAR_H, m, m)
            if self._tb is not None and lay.indexOf(self._tb) >= 0:
                lay.removeWidget(self._tb)
                self._tb.setParent(self)

    def showEvent(self, event):
        super().showEvent(event)
        if self._tb is not None:
            titlebar.pin(self, self._tb)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._tb is not None:
            titlebar.pin(self, self._tb)

    def nativeEvent(self, event_type, message):
        return titlebar.native_event(self, message)


def open_color(parent, title: str = "选择颜色", current: str = "",
               palette=None) -> str | None:
    """弹取色器；用户取消返回 ``None``。

    刻意**不连 ``colorChanged``**：那是一次拖动触发几十次回调，接上就得跟着
    重建几十次整窗 QSS（实测单次重抛光 41ms）。只认 ``exec()`` 的返回值。
    """
    p = palette or qss.current()
    dlg = ThemedColorDialog(parent, title, p)
    if current:
        dlg.setCurrentColor(QtGui.QColor(current))
    if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        got = dlg.currentColor().name()
        return got if got else None
    return None


class ThemedInputDialog(QtWidgets.QInputDialog):
    def __init__(self, parent=None, title: str = "", palette=None,
                 icon_name: str = "pencil"):
        super().__init__(parent)
        self.setWindowTitle(title)
        # **QInputDialog 的原生默认是 200px，实测。** 短标签（「目录名：」
        # 52px）看着还行，但 `main_window` 那条「从远端下载」的标签有 22 个
        # 汉字、实测 286px —— 一个 200px 的框根本放不下，而那是用户每次下载
        # 都要看的一行说明。下限的理由见 :data:`_DIALOG_MIN_W`，
        # 「为什么要在 showEvent 里补第二次」见 :func:`_pin_min_width`。
        self.setMinimumWidth(_DIALOG_MIN_W)
        self._st = {"padded": False}
        self._tb = _install(self, title, palette, icon_name)

    def showEvent(self, event):
        super().showEvent(event)
        _on_show(self, self._tb, self._st)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._tb is not None:
            titlebar.pin(self, self._tb)
        _pin_min_width(self)

    def nativeEvent(self, event_type, message):
        return titlebar.native_event(self, message)


# ------------------------------------------------------------------ 勾选框

class ThemedCheckListDialog(QtWidgets.QDialog):
    """标题栏 + 说明 + 一串复选框 + 取消/确定。

    **为什么不是 QMessageBox。** 原来的 :func:`multi_choice` 调
    ``box.setCheckBoxTexts(items)`` —— 那是个**不存在的 API**。PyQt6 的
    ``QMessageBox`` 只剩 Qt5 时代那**一个** ``checkBox`` / ``setCheckBox``
    （实测 ``dir()`` 里带 check 的就这两个），于是「从其他终端导入」一点
    就 AttributeError 崩在弹框构造里，整条导入功能从来没跑通过。

    往 QMessageBox 的 QGridLayout 里插 QCheckBox 看着能省掉一个类，实际
    是去猜 Qt 内部把按钮行放在第几行 —— PyQt6 连 ``QLayout.insertRow``
    （protected）都不暴露，只能猜一个行号填进去，两项落进同一格就是重叠。
    所以老老实实自己搭。样式不新增一份：``QDialog QPushButton`` 与
    ``QCheckBox::indicator`` 的规则本来就全表都有（见 ``qss.build_qss``）。
    """

    def __init__(self, parent=None, title: str = "", text: str = "",
                 items=(), palette=None, informative: str = "",
                 ok_text: str = "确定"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        p = palette or _palette_of(parent) or qss.current()
        self._palette = p
        self.setStyleSheet(qss.build_qss(p, dialog=True,
                                         ui_size=qss.current_ui_size()))
        # 圆角和连接框同一条（`border=False`，理由见 `_install`）。
        enable_rounded_corners(self, p, border=False)

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # 用 `attach_chrome` 而不是 `inject_chrome`：这个框的布局是自己建的，
        # 标题栏直接 `addWidget` 进去最干净（见 titlebar.attach_chrome）。
        self.titlebar = titlebar.attach_chrome(
            self, p, title=title, kinds=(titlebar.CLOSE,), icon_name="info")
        root.addWidget(self.titlebar)

        body = QtWidgets.QWidget(self)
        root.addWidget(body, 1)
        lay = QtWidgets.QVBoxLayout(body)
        lay.setContentsMargins(24, 18, 24, 20)
        lay.setSpacing(12)

        head = QtWidgets.QLabel(text)
        # objectName 走 QSS 里的 `QLabel#msgText`，尺寸对齐 QMessageBox 那条
        # `qt_msgbox_label`，不然同一句话在两条弹框路径上长得不一样。
        head.setObjectName("msgText")
        head.setWordWrap(True)
        lay.addWidget(head)

        if informative:
            info = QtWidgets.QLabel(informative)
            info.setObjectName("msgInfo")
            info.setWordWrap(True)
            lay.addWidget(info)

        self.checks: list[QtWidgets.QCheckBox] = []
        for label in items:
            cb = QtWidgets.QCheckBox(label)
            # **默认全勾。** 「导入连接」最常见的意图就是把找到的来源全导进来，
            # 默认全不勾等于每次都要先勾一遍才走得下去；而导入本身是幂等的
            # （`importer.merge` 按 (host, port, username) 判重），误勾的代价
            # 是「导多了几条可以删」，不是「弄坏了什么」。
            cb.setChecked(True)
            self.checks.append(cb)
            lay.addWidget(cb)

        lay.addStretch(1)

        cancel = QtWidgets.QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QtWidgets.QPushButton(ok_text)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        # **一个都没勾时确定按钮是灰的。** 否则「确定」和「取消」给出完全
        # 相同的结果（空列表），用户点了却什么都没发生，只能再猜一次。
        ok.setEnabled(bool(self.checks))
        for cb in self.checks:
            cb.toggled.connect(
                lambda _on, b=ok: b.setEnabled(any(c.isChecked() for c in self.checks)))

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        bar.addStretch(1)
        bar.addWidget(cancel)
        bar.addWidget(ok)
        lay.addLayout(bar)

        self.setMinimumWidth(_DIALOG_MIN_W)

    def checked_indices(self) -> list[int]:
        """被勾中的条目下标，按 *items* 的原顺序。"""
        return [i for i, cb in enumerate(self.checks) if cb.isChecked()]

    def nativeEvent(self, event_type, message):
        # 无边框后拖动窗口要靠标题栏向系统报 HTCAPTION，逻辑在 titlebar 里。
        return titlebar.native_event(self, message)


# ------------------------------------------------------------------ QMessageBox

def _build(parent, icon, title: str, text: str, palette=None,
           informative: str = "", detailed: str = "") -> QtWidgets.QMessageBox:
    p = palette or qss.current()
    box = ThemedMessageBox(parent, title, p, _ICON_FOR.get(icon, "info"))
    box.setIcon(icon)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    if detailed:
        box.setDetailedText(detailed)
    return box


def information(parent, title: str, text: str, palette=None) -> None:
    _build(parent, QtWidgets.QMessageBox.Icon.Information, title, text,
           palette).exec()


def warning(parent, title: str, text: str, palette=None) -> None:
    _build(parent, QtWidgets.QMessageBox.Icon.Warning, title, text,
           palette).exec()


def critical(parent, title: str, text: str, palette=None) -> None:
    _build(parent, QtWidgets.QMessageBox.Icon.Critical, title, text,
           palette).exec()


def question(parent, title: str, text: str, palette=None
             ) -> QtWidgets.QMessageBox.StandardButton:
    box = _build(parent, QtWidgets.QMessageBox.Icon.Question, title, text,
                 palette)
    box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes
                           | QtWidgets.QMessageBox.StandardButton.No)
    box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
    return box.exec()


def multi_choice(parent, title: str, text: str, items: list[str],
                 palette=None, informative: str = "", ok_text: str = "确定"
                 ) -> list[int]:
    """勾选式确认框：返回被勾选的 *items* 下标。用户取消返回空列表。

    **不新造弹框类的那个理由已经作废。** 原注释写的是「ThemedMessageBox
    本身就是 QMessageBox 的子类，`setCheckBoxTexts` 直接可用」—— 那个方法
    PyQt6 根本没有（只有 Qt5 的单个 `setCheckBox`），点「从其他终端导入」
    必崩。实现换成了 :class:`ThemedCheckListDialog`，理由和实测见那里。

    一个都没勾时返回空列表，和取消一样：确定按钮本来就是灰的，正常路径走
    不到这里，但返回空列表这条兜底留着（调用方 `if not picked: return`
    天然处理）。
    """
    dlg = ThemedCheckListDialog(parent, title, text, items, palette,
                                informative, ok_text)
    if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
        return []
    return dlg.checked_indices()


def about(parent, version: str, palette=None) -> None:
    """「关于 MavisSSH」。原来在 main_window 里现搭一个 QMessageBox。

    **正文分两层，字号由 QSS 决定**（`QMessageBox QLabel#qt_msgbox_label`
    与 `#qt_msgbox_informativelabel`，见 `qss.build_qss`）。这里只管**内容**
    —— `setInformativeText` 是一整块等权文本，用户真正需要的信息（版本、
    技术栈）和装饰性说明混在一起，所以逐行加前缀让结构自己显出来。
    """
    p = palette or qss.current()
    box = ThemedMessageBox(parent, "关于 MavisSSH", p, icon_name="info")
    box.setIcon(QtWidgets.QMessageBox.Icon.Information)
    # 原来这里是 <h3>…</h3> 富文本。QMessageBox 的富文本渲染在不同平台上
    # 字距和行高都不一致，而标题栏已经承担了"这是关于框"这件事，正文用
    # 纯文本反而更稳。
    box.setTextFormat(QtCore.Qt.TextFormat.PlainText)
    box.setText("MavisSSH %s" % version)
    box.setInformativeText(
        "功能：终端 · 文件传输 · 资源监控 · 端口转发 · SOCKS5 · lrzsz\n"
        "技术栈：PyQt6 + paramiko + pyte")
    box.exec()


# ------------------------------------------------------------------ 文件 / 输入

def open_files(parent, caption: str, directory: str = "",
               name_filter: str = "") -> tuple[list[str], str]:
    dlg = ThemedFileDialog(parent, caption, directory)
    dlg.setFileMode(QtWidgets.QFileDialog.FileMode.ExistingFiles)
    if name_filter:
        dlg.setNameFilter(name_filter)
    if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        return dlg.selectedFiles(), dlg.selectedNameFilter()
    return [], ""


def open_file(parent, caption: str, directory: str = "",
              name_filter: str = "") -> tuple[str, str]:
    dlg = ThemedFileDialog(parent, caption, directory, icon_name="file")
    dlg.setFileMode(QtWidgets.QFileDialog.FileMode.ExistingFile)
    if name_filter:
        dlg.setNameFilter(name_filter)
    if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        return dlg.selectedFiles()[0] if dlg.selectedFiles() else "", ""
    return "", ""


def ask_text(parent, title: str, label: str, text: str = "") -> tuple[str, bool]:
    dlg = ThemedInputDialog(parent, title)
    dlg.setLabelText(label)
    dlg.setTextValue(text)
    dlg.setInputMode(QtWidgets.QInputDialog.InputMode.TextInput)
    if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        return dlg.textValue(), True
    return "", False


def ask_dir(parent, title: str, directory: str = "") -> str | None:
    """弹目录选择框；用户取消或空返回 ``None``。"""
    dlg = ThemedFileDialog(parent, title, directory, icon_name="folder")
    dlg.setFileMode(QtWidgets.QFileDialog.FileMode.Directory)
    dlg.setOption(QtWidgets.QFileDialog.Option.ShowDirsOnly, True)
    if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
        return dlg.selectedFiles()[0] if dlg.selectedFiles() else None
    return None
