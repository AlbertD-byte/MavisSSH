"""片段面板：挑一条常用命令，插进远端当前的输入行。

**对远端只做一件事，剩下的一件都不做。**

1. 把片段的文本**写进远端的行编辑缓冲**。
2. 让用户自己看清楚、自己按回车。

（面板本地还管增删改存盘，但那些一个字节都不往远端发。）

**绝不自动回车。** 片段写进去之后远端还没执行，用户可以退格、可以 Ctrl+U、
可以 Ctrl+C。加一个 `\\r` 进去，"我想看看这条命令会干什么"就变成了"它已经干了"。
同理**不清行**——本地并不知道远端行缓冲的真实内容（readline 补全过的、
带多字节宽字符的），硬猜着发退格键只会删错字符。想换命令请自己 Ctrl+U，
面板底部一直写着这句话。

**不抢焦点。** 面板是 ``Qt.Tool`` 窗口并且带 ``WA_ShowWithoutActivating``：
点工具栏按钮把它叫出来时，键盘焦点**留在终端上**，用户可以一边看着它一边
继续敲命令。真去点搜索框或列表时焦点才过去，而那时按 Enter 走的是面板自己的
槽，不会漏到远端去——终端那侧压根没有焦点。

**增删改。** 选中一条按「编辑」，标题和命令装进底部输入框，「添加」变成
「保存」。改完 id 不变、条数不变，所以选中项不会跳走，内置条目也能改。
编辑期间**列表是锁着的**：不然「保存」改的到底是哪条要看当时选中了什么，
而那是个用户没法从界面上看出来的状态。**退出编辑的每一条路都会解锁** ——
「保存」、「取消」，以及关窗和隐藏（``closeEvent`` / ``hideEvent``）都要
最后一条最容易被漏掉：``main_window`` 复用同一个面板实例，只堵按钮的话，
收起再叫出时状态还停在编辑态，而那时在命令框按回车会**静默覆盖**原来那条。

**传输期间整块禁用。** ZMODEM 跑的时候通道被 `sz`/`rz` 占着，这时候往里写
字符会被当成 ZMODEM 载荷，插进去的东西会变成传输流的一部分。靠
``TransferController.busyChanged`` 跟着会话状态走，不自己判断。
"""
from __future__ import annotations

from collections.abc import Callable

from PyQt6 import QtCore, QtWidgets

from ..core.fonts import mono_font
from ..core.snippets import Snippet, SnippetStore
from .controls import _label
from .design.v1.palette import Palette
from .transfer_controller import TransferController


class SnippetPanel(QtWidgets.QDialog):
    """一个非模态的小窗，挂在某个 :class:`SessionTab` 旁边。

    *send* 是一个可调用对象，负责把字节送进远端。**由调用方注入**，本面板不碰
    通道：它不知道通道存不存在、关没关，也不想知道。
    """

    def __init__(self, store: SnippetStore, send: Callable[[bytes], bool],
                 ctrl: TransferController | None = None,
                 block_reason: Callable[[], str] | None = None,
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._store = store
        self._send = send
        self._ctrl = ctrl
        # **唯一判断能不能插的地方**。默认只看忙时闸门，但那只覆盖了"已经
        # begin 之后"：远端 sz 已经发出、本地 rz 还在等 500ms 的那半秒里，
        # 通道已经归远端了而闸门还没锁。实跑确认过：那一刻按钮是使能的，
        # 插进去的字节被 sz/rz 当载荷吃掉。SessionTab 传入自己的判据
        # （见 SessionTab._snippet_block_reason），插入与按钮状态共用同一个
        # 谓词，两边不会各说各话。
        self._block_reason = block_reason or self._default_block_reason

        self.setWindowTitle("命令片段")
        # Qt.Tool：不在任务栏/Alt+Tab 里出现，永远浮在主窗之上。
        self.setWindowFlags(QtCore.Qt.WindowType.Tool
                            | QtCore.Qt.WindowType.WindowStaysOnTopHint)
        # **不要**因为 show() 就把焦点从终端抢走：用户正在敲命令的时候
        # 弹个面板把键盘夺走，打一半的字就得到别处去了。
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.resize(440, 420)

        self._palette: Palette = None
        # 非空 = 正在编辑这一条（此时「添加」按钮实际是「保存」）。空字符串
        # 就是普通的新增模式。**不用按钮的 isChecked 当状态**：那和"用户点
        # 了一下又点回去"没法区分，而这里要的是"现在这条输入框改的是谁"。
        self._editing_id: str = ""
        self._build()
        self.reload()
        # 唯一按钮自己接上，别指望调用方记得调：**「记得在某处连一下」这种
        # 约定，漏一次的表现是传输期间按钮还能点**，而那条路插进去的字符
        # 会被 sz/rz 当载荷吃掉 —— 是个很难查的故障。
        if self._ctrl is not None:
            self._ctrl.busyChanged.connect(self.set_busy)
        self._sync_enabled()

    def _default_block_reason(self) -> str:
        if self._ctrl is not None and self._ctrl.busy:
            return "传输进行中：通道被占着，插进去会被当成传输数据"
        return ""

    # ------------------------------------------------------------------ 构建

    def _build(self) -> None:
        self.filter_edit = QtWidgets.QLineEdit(self)
        self.filter_edit.setPlaceholderText("搜索片段（标题或命令）")
        self.filter_edit.setClearButtonEnabled(True)
        # Esc 在 QLineEdit 里默认是**什么都不做**，本窗口的 Esc 又要关窗口，
        # 两个撞在一起会让用户按一次没反应。这里明确接管。
        # 拦下来还有个更重要的作用 —— 免得这一下 Esc 被当成按键漏给远端，
        # 用户的 vim 莫名其妙就退出了。
        self.filter_edit.installEventFilter(self)

        self.list = QtWidgets.QListWidget(self)
        self.list.setAlternatingRowColors(True)
        self.list.itemSelectionChanged.connect(self._on_selection)
        self.list.itemActivated.connect(self._on_activated)   # 双击 / 回车

        # **这里原来给这个标签设了 `role=mono`，那个 role 已删。**
        # 等宽不是靠 QSS 给的 —— `qss.py` 里从来没有 mono 那条 role 规则，
        # 留着它只会让下一个人以为「改 QSS 就能调预览字体」，而真正生效的
        # 是紧接着的 `_apply_preview_font()`。为什么不用 QSS 的
        # `font-family`，见那个方法的注释。
        self.preview = _label("")
        self.preview.setWordWrap(True)
        self.preview.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self._preview_family = ""
        self._preview_size = 11
        self._apply_preview_font()

        self.insert_btn = QtWidgets.QPushButton("插入到命令行")
        self.insert_btn.clicked.connect(self._on_insert)
        self.edit_btn = QtWidgets.QPushButton("编辑")
        self.edit_btn.setToolTip("把这条装进下面的输入框改掉（id 不变）")
        self.edit_btn.clicked.connect(self._on_edit)
        self.delete_btn = QtWidgets.QPushButton("删除")
        self.delete_btn.clicked.connect(self._on_delete)
        self.restore_btn = QtWidgets.QPushButton("恢复内置")
        self.restore_btn.setToolTip(
            "把删掉的内置片段补回来（你自己加的不受影响）")
        self.restore_btn.clicked.connect(self._on_restore)

        self.hint = _label("插入到远端当前的输入行，不会自动回车；"
                           "想换命令先按 Ctrl+U 清行。", "section")

        self.title_edit = QtWidgets.QLineEdit(self)
        self.title_edit.setPlaceholderText("标题，例如「重启 nginx」")
        self.cmd_edit = QtWidgets.QLineEdit(self)
        self.cmd_edit.setPlaceholderText("命令原文，会原样插入")
        self.cmd_edit.returnPressed.connect(self._on_add)
        # **标题框和命令框也要装 filter**，不只是搜索框：只有搜索框装了的时候，
        # 这两个框里的 Esc 只能靠 focusWidget() 反推"用户在哪个框"，而焦点在
        # Windows 上抢不到时就会走偏（清掉另一个框的残留，或者干脆把窗关了）。
        # 装上之后事件本身就带着"键发到了哪儿"，不用猜。
        self.title_edit.installEventFilter(self)
        self.cmd_edit.installEventFilter(self)
        self.add_btn = QtWidgets.QPushButton("添加")
        self.add_btn.clicked.connect(self._on_add)
        self.add_btn.setEnabled(False)     # 没连上就没有远端可插，禁掉
        # 编辑态的出口。平时藏起来：一个一直亮着、绝大多数时候没用的按钮
        # 只会让人以为非点不可。
        self.cancel_btn = QtWidgets.QPushButton("取消")
        self.cancel_btn.setToolTip("不改了，回到新增状态")
        self.cancel_btn.clicked.connect(self._on_cancel_edit)
        self.cancel_btn.hide()

        add_row = QtWidgets.QHBoxLayout()
        add_row.setSpacing(6)
        add_row.addWidget(self.title_edit, 1)
        add_row.addWidget(self.cmd_edit, 2)
        add_row.addWidget(self.add_btn)
        add_row.addWidget(self.cancel_btn)

        act_row = QtWidgets.QHBoxLayout()
        act_row.setSpacing(6)
        act_row.addWidget(self.insert_btn, 1)
        act_row.addWidget(self.edit_btn)
        act_row.addWidget(self.delete_btn)
        act_row.addWidget(self.restore_btn)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(6)
        lay.addWidget(self.filter_edit)
        lay.addWidget(self.list, 1)
        lay.addWidget(self.preview)
        lay.addWidget(self.hint)
        lay.addLayout(act_row)
        # **原来这里是 `lay.addWidget(_hline())`（一条横线），已改成纯间距。**
        # 它和工具条那个竖线是同一个毛病：原生 QFrame 线没设颜色，被应用级
        # `QWidget { color: text }` 兜住，实测对比度 4.52，是全界面最亮的
        # 线条而两侧同色。分组靠留白读。
        lay.addSpacing(12)
        lay.addLayout(add_row)

        # 搜索/过滤/列表选中/新增输入全在这一个入口上收口，别处不重复实现。
        self.filter_edit.textChanged.connect(self.reload)
        # 这两条**必须**接：不然「添加」按钮的状态只在下一次选中列表项时才
        # 刷新，用户把标题和命令都填满了它还是灰的，看上去像坏了。
        self.title_edit.textChanged.connect(lambda _t: self._sync_enabled())
        self.cmd_edit.textChanged.connect(lambda _t: self._sync_enabled())

    # ------------------------------------------------------------------ 数据

    def reload(self) -> None:
        """按当前搜索词重建列表。"""
        keep = self.current_id()
        self.list.blockSignals(True)
        self.list.clear()
        found = self._store.search(self.filter_edit.text())
        for sn in found:
            item = QtWidgets.QListWidgetItem(sn.title)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, sn.id)
            item.setToolTip(sn.command)
            # 标题和命令并排显示：只给标题的话，用户还得靠 tooltip 才知道
            # 这条到底干什么。
            item.setText("%-14s %s" % (sn.title, sn.command))
            self.list.addItem(item)
        self.list.blockSignals(False)
        if not found:
            self.preview.setText("没有匹配的片段")
        # 原来选中的那条没了就退到第一条。
        #
        # **必须看 select() 的返回值**，不能只判 `if keep:`。踩过一次：
        # keep 是非空就走了 if 分支，select 在过滤后的列表里找不到它、静默
        # 返回 False，于是 elif 那条"退到第一条"永远不跑 —— 列表里明明有
        # 东西，却一个都没选中，用户按回车毫无反应。
        if not (keep and self.select(keep)) and found:
            self.list.setCurrentRow(0)
        self._sync_enabled()

    def _on_selection(self) -> None:
        sn = self.current()
        self.preview.setText(sn.command if sn else "")
        self._sync_enabled()

    def current_id(self) -> str:
        item = self.list.currentItem()
        return item.data(QtCore.Qt.ItemDataRole.UserRole) if item else ""

    def current(self) -> Snippet | None:
        return self._store.get(self.current_id())

    def select(self, snippet_id: str) -> bool:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item.data(QtCore.Qt.ItemDataRole.UserRole) == snippet_id:
                self.list.setCurrentRow(row)
                return True
        return False

    # ------------------------------------------------------------------ 动作

    def _on_activated(self, _item) -> None:
        """双击 / 列表里按回车 = 插入。"""
        self._on_insert()

    def _on_insert(self) -> None:
        """把选中的片段写进远端行缓冲。**不发回车。**"""
        sn = self.current()
        if sn is None:
            return
        why = self._block_reason()
        if why:
            # 按钮已经禁了，但双击列表这条路不走按钮，所以这里还要再挡一道。
            self.hint.setText(why)
            return
        if not self._send(sn.command.encode("utf-8")):
            self._flash("没插进去：连接已断开")
            return
        self._flash("已插入，不回车——自己看着办")

    def _on_delete(self) -> None:
        sn = self.current()
        if sn is None:
            return
        # 不弹确认框：这个 app 里模态框一律不用（理由见 transfer_controller
        # 的 report_problem），删错了还有「恢复内置」，而自己加的删了就是
        # 真没了 —— 所以确认放在这里，但拦不住手滑。
        self._store.remove(sn.id)
        self.reload()
        self._report_save("已删除「%s」" % sn.title)

    def _on_edit(self) -> None:
        """把选中的那条装进输入框，把「添加」切成「保存」。

        **没有这条路就只能删了重打。** 打错一个字的命令，用户唯一的办法是
        删掉、重新敲一遍、还得记住原来那条是什么 —— 而 `SnippetStore.update()`
        早就写好了、也测了，却一个生产调用方都没有。
        """
        sn = self.current()
        if sn is None:
            return
        # 先落状态再 setText：setText 会触发 textChanged → _sync_enabled，
        # 那一刻 _editing_id 必须已经是编辑态，列表才会跟着锁上。
        self._editing_id = sn.id
        self.title_edit.setText(sn.title)
        self.cmd_edit.setText(sn.command)
        self._enter_edit_mode()

    def _enter_edit_mode(self) -> None:
        self.add_btn.setText("保存")
        self.cancel_btn.show()
        # **编辑时锁住列表**：否则用户点「保存」时改的到底是哪条要看运气。
        # 退出编辑的每一条路（保存/取消/关窗/隐藏）都会解锁。
        self.list.setEnabled(False)
        self._sync_enabled()

    def _leave_edit_mode(self) -> None:
        self._editing_id = ""
        self.add_btn.setText("添加")
        self.cancel_btn.hide()
        self.list.setEnabled(True)
        self._sync_enabled()

    def _on_cancel_edit(self) -> None:
        self.title_edit.clear()
        self.cmd_edit.clear()
        self._leave_edit_mode()
        self._flash("已退出编辑")
    def _on_restore(self) -> None:
        n = self._store.restore_builtins()
        self.reload()
        self._report_save("补回了 %d 条内置片段" % n if n else "内置片段都在")

    def _on_add(self) -> None:
        title, command = self.title_edit.text(), self.cmd_edit.text()
        if self._editing_id:
            self._on_save(title, command)
            return
        sn = self._store.add(title, command)
        if sn is None:
            # 说清是哪一条没过，别让用户对着一个不亮的按钮猜
            self._flash("加不了：标题和命令都得填，且不能含换行或看不见的字符")
            return
        self.title_edit.clear()
        self.cmd_edit.clear()
        self.reload()
        self.select(sn.id)
        self._report_save("已添加「%s」" % sn.title)

    def _on_save(self, title: str, command: str) -> None:
        """编辑态下的「保存」。id 不变，所以选中项不会跳走。"""
        target = self._editing_id        # 存下来：下面 _leave_edit_mode 会清空它
        if self._store.get(target) is None:
            # 编辑期间那条被别人删了（另一个窗口的 store 共享同一份文件）。
            # 报"参数不合法"会把用户引到完全错误的方向上去。
            #
            # **必须 reload**：这一行在列表里已经不存在了，不重建的话用户看到
            # 的是"我明明点的是它，它却说不存在"——界面自相矛盾，而残行还在
            # 列表里躺着，用户多半会再点它一次。成功路径下面有 reload()，这条
            # 早退路径原来漏了。
            self.title_edit.clear()
            self.cmd_edit.clear()
            self._leave_edit_mode()
            self.reload()
            self._flash("原来那条已经不在了，请重新挑一条")
            return
        if not self._store.update(target, title, command):
            self._flash("改不了：标题和命令都得填，且不能含换行或看不见的字符")
            return
        # 先退出编辑态再 reload：reload() 会调 _sync_enabled，编辑态下它
        # 会把列表锁着，先解锁才不会 reload 完列表还灰着。
        self._leave_edit_mode()
        self.title_edit.clear()
        self.cmd_edit.clear()
        self.reload()
        self.select(target)
        self._report_save("已保存「%s」" % title)

    def _report_save(self, ok_msg: str) -> None:
        """改完之后**如实**报结果。

        **保存失败必须让用户看见。** Windows 上并发写同一个文件时
        ``os.replace`` 会成片报 ``PermissionError: [WinError 5]``（实测），
        而内存里的改动照样生效 —— 于是界面报"已添加"、用户以为存下了、
        下次打开发现片段没了。存一次数据丢一次，而且是那种"关掉才发现"
        的丢法。
        """
        if self._store.last_save_ok:
            self._flash(ok_msg)
            return
        self._flash("⚠ 没能存到磁盘（%s）。这次改动在内存里，"
                    "关掉应用就没了 —— 请检查目录权限后重试。"
                    % self._store.path.name)

    def _flash(self, msg: str) -> None:
        self.hint.setText(msg)
        QtCore.QTimer.singleShot(4000, self._restore_hint)

    def _restore_hint(self) -> None:
        # **传输期间不许拿默认文案盖掉禁用原因。** 踩过一次：添加/编辑片段会
        # 排一个 4 秒的定时器，这 4 秒里点了下载，_sync_enabled 把提示换成
        # 「传输进行中：通道被占着…」，4 秒后这个定时器照样触发，把用户真正
        # 需要的解释洗成默认那句。按钮是禁的（协议上没风险），但用户看到的
        # 是"莫名其妙全灰了还没说为什么"。所以这里先问闸门。
        why = self._block_reason()
        if why:
            self.hint.setText(why)
            return
        self.hint.setText("插入到远端当前的输入行，不会自动回车；"
                          "想换命令先按 Ctrl+U 清行。")

    # ------------------------------------------------------------------ 字体

    def set_preview_font(self, family: str = "", size: int = 11) -> None:
        """命令预览用等宽字体。由持有 AppSettings 的一方调进来。

        **为什么不直接在 __init__ 里取设置。** 这个面板只收回调，不收
        settings —— 保持它能被单独 new 出来测。它在 main_window 里是
        **缓存复用**的（`if self._snippet_panel is None` 才新建），所以
        改设置后必须有人再调一次这个方法，否则复用旧面板时字族不更新。
        """
        self._preview_family = family
        self._preview_size = size
        self._apply_preview_font()

    def _apply_preview_font(self) -> None:
        """把预览区设成等宽。

        **走 `setFont` 而不是在 QSS 里写 `font-family`。** QSS 的
        `font-family` 会**整条替换** families 链，而 `mono_font()` 额外带了
        `StyleHint(Monospace)` 和 CJK 兜底链（仓库有整套 mono 守卫：
        `_fontcheck`/`_glyphfb`/`_portable` + `_MONO_EXCLUDE` 滤掉
        SimSun-ExtB），写死字族会把那一层整个绕过去。

        **`size` 传的是 `ui_font_size` 而不是终端字号。** 两者**渲染上没差**：
        QSS 那条全局 `QWidget {{ font-size: 13px }}` 会把 `setFont` 设的点值
        整个顶掉（实测 pointSize 变 -1、改走 pixelSize），最终尺寸由
        `_scale_metrics` 按 `ui_font_size` 缩放决定。传终端字号的话，
        这里读代码的人会以为预览跟着终端字号走 —— 而它其实不跟。
        字**族**才取 `term_font_family`：那是用户唯一能选等宽的地方
        （界面字体清单里全是比例字体）。
        """
        self.preview.setFont(
            mono_font(self._preview_size, self._preview_family))

    # ------------------------------------------------------------------ 状态

    def _sync_enabled(self) -> None:
        why = self._block_reason()
        blocked = bool(why)
        editing = bool(self._editing_id)
        has = self.current() is not None
        # **传输期间"整块"禁用，一个都不能漏。** 评审逐控件核对的结果是
        # ``restore_btn`` 和 ``filter_edit`` 漏了：它们不发字节，所以不会
        # 污染 ZMODEM 流，但"整块禁用"这句话就该字面成立。
        can = not blocked and not editing
        self.insert_btn.setEnabled(has and can)
        self.edit_btn.setEnabled(has and can)
        self.delete_btn.setEnabled(has and can)
        self.restore_btn.setEnabled(can)
        self.filter_edit.setEnabled(can)
        # 编辑态下列表锁着：不然「保存」改的到底是哪条要看当时选中了什么。
        self.list.setEnabled(can)
        # 「添加」在编辑态下是「保存」，**那条路必须留着** —— 它是仅有的出口。
        self.add_btn.setEnabled(not blocked and bool(
            self.title_edit.text().strip() and self.cmd_edit.text().strip()))
        self.cancel_btn.setEnabled(not blocked)
        self.title_edit.setEnabled(not blocked)
        self.cmd_edit.setEnabled(not blocked)
        if blocked:
            self.hint.setText(why)

    def set_busy(self, busy: bool) -> None:
        """接 ``TransferController.busyChanged``。**构造时就接上了**，不用外部调。

        判据不在这里判：``_block_reason`` 才是唯一的真相来源，它比忙时闸门
        宽（还包括"远端 sz/rz 已发出但本地引擎还没起"的那半秒）。
        """
        self._sync_enabled()

    # ------------------------------------------------------------------ 事件

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.Type.KeyPress:
            return super().eventFilter(obj, event)
        # **三个输入框都装了这个 filter**（见 _build）。Esc 无论发到哪个框，
        # 都能确定地知道"用户是在哪个框里按的"，不用去猜焦点在哪 ——
        # `hasFocus()`/`focusWidget()` 在 Windows 上是**抢来的**，窗口没真正
        # 激活时它们经常不是用户以为的那个。实测按同一个键，清掉的却是
        # 另一个框的残留，甚至直接把窗关了。
        if obj in (self.filter_edit, self.title_edit, self.cmd_edit) \
                and event.key() == QtCore.Qt.Key.Key_Escape:
            self._escape(obj)
            return True
        if obj is not self.filter_edit:
            return super().eventFilter(obj, event)
        if event.key() in (QtCore.Qt.Key.Key_Return,
                           QtCore.Qt.Key.Key_Enter):
            # 在搜索框里回车 = 插入当前选中的那条，而不是触发 QLineEdit 的
            # returnPressed（那会落到「添加」上）。必须吃掉这个键，否则
            # 它还会被当成输入继续往下传。
            self._on_insert()
            return True
        if event.key() == QtCore.Qt.Key.Key_Down:
            self.list.setCurrentRow(max(0, self.list.currentRow() + 1))
            self.list.setFocus()
            return True
        return super().eventFilter(obj, event)

    def _escape(self, where=None) -> None:
        """Esc：先清**你正在用的那个框**，再按才关窗。

        ``where`` 是 :meth:`eventFilter` 告诉我们的目标控件。

        **第一版有个真 bug：清错框。** 它按固定顺序扫
        ``(搜索框, 标题框, 命令框)``，于是搜索框里有残留、而你正在命令框里
        打字时，按一次 Esc 清掉的是搜索框——离手最近的那个反而没反应。

        **第二版是"用 hasFocus() 反推你在哪个框"。** 方向对了，但焦点是
        **抢来的**：``Qt.Tool`` 窗口在 Windows 上没真正激活时
        ``hasFocus()`` 经常不是用户以为的那个，表现为按一次 Esc 把窗关了
        （半截输入被丢）。判据要落在**确定的东西**上，所以改成由事件本身
        告诉我们键发到了哪儿；实在拿不到（比如键发在列表或按钮上）才退回
        ``focusWidget()``，再退回固定顺序。
        """
        for cand in (where, self.focusWidget()):
            if isinstance(cand, QtWidgets.QLineEdit) and cand.text():
                cand.clear()
                return
        for edit in (self.filter_edit, self.title_edit, self.cmd_edit):
            if edit.text():
                edit.clear()
                return
        self.close()

    def keyPressEvent(self, event):
        # Esc 落在面板的**其他**控件上（列表、按钮、预览）时也收口。
        # 不这么做的话 QDialog 默认走 reject()，用户敲了一半的标题就没了。
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self._escape()
            event.accept()
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------ 生命周期

    def closeEvent(self, event):
        self._abandon_edit()
        super().closeEvent(event)

    def hideEvent(self, event):
        self._abandon_edit()
        super().hideEvent(event)

    def _abandon_edit(self) -> None:
        """**关窗/隐藏也是退出编辑态的一条路。**

        评审从三条不同的路各找到一次这个洞，而它们是同一个：
        原来只堵了「保存」和「取消」两个按钮。于是
        ``main_window.py`` 收起再叫出面板时（**它复用同一个实例**），
        ``_editing_id`` 还在、按钮还写着「保存」、列表还锁着 —— 用户这时在
        命令框按回车会走 ``cmd_edit.returnPressed``，落到 ``_on_add``，
        而它因为 ``_editing_id`` 非空转到 ``_on_save``，
        **把原来那条静默改掉，并且报「已保存」**。

        那条回车路上没有任何按钮文案提示，用户完全没有察觉。
        """
        if not self._editing_id:
            return
        # **拆除期也会被调到。** 进程退出时 Qt 挨个析构控件，而
        # ``_leave_edit_mode`` → ``_sync_enabled`` 要挨个 ``setEnabled``；
        # 碰到已经没了的 C++ 对象是 ``RuntimeError``，**在析构期抛出来就是
        # 0xC0000005 访问冲突**。全量回归里 ``_e2e`` 偶发崩在打印完
        # "ALL CHECKS PASSED" 之后 —— 就是这里。
        from PyQt6 import sip
        if sip.isdeleted(self):
            return
        for name in ("insert_btn", "edit_btn", "delete_btn", "restore_btn",
                     "add_btn", "cancel_btn", "filter_edit", "list",
                     "title_edit", "cmd_edit"):
            w = getattr(self, name, None)
            if w is None or sip.isdeleted(w):
                return
        self._leave_edit_mode()
        self.title_edit.clear()
        self.cmd_edit.clear()

    def apply_palette(self, palette: Palette) -> None:
        self._palette = palette
        for btn in (self.insert_btn, self.edit_btn, self.delete_btn,
                    self.restore_btn, self.add_btn, self.cancel_btn):
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        self._sync_enabled()


# **原 `_hline()` 已删。** 它的 docstring 还指着 `controls._sep`（也已删）——
# 两个都删掉，注释里那条「两个方向各一处」的互相引用一并消失。
# 同样没有留一个「不再画线的 `_hline`」：同名函数不再干它名字说的那件事。
