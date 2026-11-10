"""自绘窗口标题栏。

Windows 的原生标题栏是整个界面里唯一不受控的一块：它的底色由系统主题决定，
深色应用配上去就是截图里那条突兀的亮条。本模块把它换掉——窗口改成无边框，
顶上放一条 38px 的自绘条。

**为什么只对 Windows 生效。** macOS 的红绿灯按钮由系统绘制且有原生行为
（全屏、窗口列表），去掉它比留着原生标题栏更糟；Linux 各家 WM 差异更大。
所以 `frameless_enabled()` 只在 win32 上返回 True，其他平台保持原生边框。

**为什么字形是画的不是图标。** 右上角这几个字形要求像素级锐利（1px 实线、
不开抗锯齿），走 `icons.py` 的 QPainterPath 缩放会糊；而且"最大化"和"还原"
要在窗口状态变化时当场换，走图标缓存反而多一层间接。

**拖动和缩放交给系统。** 这里不自己实现"按住标题栏移动窗口"——那种手写
版本在多显示器、不同 DPI、拖到屏幕边缘触发贴边分屏时全都会出错。标题栏向
Windows 报告 `HTCAPTION`（见 `MainWindow.nativeEvent`），拖动、双击最大化、
Aero Snap 全部走系统那条已经打磨了二十年的路径。
"""
from __future__ import annotations

import sys

from PyQt6 import QtCore, QtGui, QtWidgets

from . import icons, logo
from .design.v1.palette import Palette, text_on

TITLEBAR_H = 38          # 与 Windows 11 原生标题栏同量级，比经典 30px 宽松
CAPTION_W = 46           # 右上角按钮宽度，和原生 caption button 一致
GLYPH = 10               # 最大化方框边长；关闭是 8，最小化是 10 的横线

MIN, MAX, CLOSE = "min", "max", "close"

_TIPS = {MIN: "最小化", MAX: "最大化", CLOSE: "关闭"}
_TIPS_RESTORE = "还原"


def frameless_enabled() -> bool:
    """这一版自绘标题栏只在 Windows 上启用。"""
    return sys.platform == "win32"


def attach_chrome(window: QtWidgets.QWidget, palette: Palette, title: str,
                  kinds=(MIN, MAX, CLOSE), icon_name: str = "terminal"):
    """把窗口改成无边框并装上自绘标题栏，返回 ``TitleBar``。

    标志必须在**第一次 show 之前**设：show 之后再改会让窗口重建原生句柄，
    肉眼看得见一次闪烁。
    """
    if frameless_enabled():
        window.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
    return TitleBar(window, palette, window, title=title, kinds=kinds,
                    icon_name=icon_name, surface=palette.bg_dialog)


#: 弹框高度上限相对工作区留的余量（逻辑像素）。留这一条是为了「弹框不贴屏幕
#: 边」——贴边时标题栏的拖动区域只剩一半，双击也更容易误触到最大化。
SCREEN_MARGIN = 40


def screen_height_for(window: QtWidgets.QWidget) -> int:
    """*window* 所在那块屏的可用高度上限（已扣掉 :data:`SCREEN_MARGIN`）。

    **必须按窗口实际所在的那块屏算，不能取主屏。** 弹框可以从第二块屏打开，
    主屏矮、副屏高（竖屏 1080x1920 的工作区是 1872）时按主屏算就白加了滚动条；
    反过来主屏高、副屏矮时按主屏算就直接超屏。

    **窗口还没 show 时 `screen()` 会回退到主屏** —— Qt 对未映射的窗口没有
    真实位置。所以调用点必须在 show 之后（见 :func:`fit_to_screen`）。

    ``availableGeometry()`` 排除了任务栏，返回的是**逻辑像素**，与
    ``QWidget.height()`` 同单位，可以直接比。
    """
    screen = window.screen() if window.isVisible() else None
    if screen is None:
        screen = QtGui.QGuiApplication.primaryScreen()
    if screen is None:                     # pragma: no cover - 无头环境
        return 0
    return max(240, screen.availableGeometry().height() - SCREEN_MARGIN)


#: 按钮行容器（``holder``）的 objectName。
#:
#: **样式表必须按这个名字限定，不能写裸 `QWidget`。**
#:
#: Qt 的类型选择器会命中**所有后代**，于是「给容器刷一层弹框底色」这一句，
#: 会连它下面每一个控件的 `background-color` 一起刷。而离得更近的祖先规则
#: 在 `background-color` 这一项上压过了弹框表里的 `QPushButton[variant="primary"]`
#: —— 主按钮于是**丢掉实心填充**，只剩「白字 + 强调色描边」，渲染成描边按钮，
#: 白字压在浅色底上几乎看不见。
#:
#: 实测（浅色，`accent_solid=#1d78cb`、`bg_dialog=#E4E3DE`）：
#:
#: | 祖先容器带的样式表 | primary 按钮主色 |
#: |-------------------|------------------|
#: | （没有）          | `#1a6bb5` 有填充 |
#: | `QWidget { background: … }` | `#e4e3de` 丢填充 |
#: | `QWidget#mavisActionHolder { background: … }` | `#1a6bb5` 有填充 |
#:
#: 底色本身还是要的（根布局 margins 是 0，容器得自己把底铺满，否则圆角处透出
#: 后面那层），所以修法是**限定范围**，不是删掉。
_HOLDER_OBJ = "mavisActionHolder"


def _holder_style(dialog: QtWidgets.QWidget) -> str:
    """按钮行容器的样式表：**只**给自己刷底色，见 :data:`_HOLDER_OBJ`。"""
    return "QWidget#%s { background: %s; }" % (_HOLDER_OBJ, _dialog_bg(dialog))


#: 按钮行上方那条发丝线的 objectName。同样**必须限定**，理由同 :data:`_HOLDER_OBJ`
#: —— 一条没限定的 `QFrame { background: … }` 会刷到容器里所有后代上。
_SEP_OBJ = "mavisActionSep"


def _action_sep(dialog: QtWidgets.QWidget) -> QtWidgets.QFrame:
    """按钮行与正文之间的一条 1px 分隔线。

    **为什么是线而不是换底色。** 「让按钮行这条带子换个底色」听起来更符合本项目
    「分层靠色差、不靠描边线」的做法，实测量下来不成立 —— 现有 token 里没有一个
    能和 `bg_dialog` 拉开足够的距离（本项目自己的经验：纯色差要 ΔE 18 左右才
    撑得起分层，10 出头只是「看起来差不多」）：

    | 候选 | 浅色 vs bg_dialog | 深色 vs bg_dialog |
    |------|-------------------|-------------------|
    | `bg_surface` | 10.2 | 6.5 |
    | `bg_base`    | 6.4  | 11.8 |
    | `bg_elevated`| 3.5  | 3.2 |

    两个主题里没有一个能同时站住，而「深色够、浅色不够」正好是最容易漏的一档
    （深色本来就更容易看出分层，浅色才是这个项目栽过跟头的地方）。
    所以这里老老实实画线。

    **线本身是这个弹框已有的语言**：正文里各段之间本来就用 `palette.border`
    的 1px 分隔线（见 `connect_dialog._sep_line`），底部这条是同一条规则用到
    页脚上，不是新发明的一层。
    """
    line = QtWidgets.QFrame(dialog)
    line.setObjectName(_SEP_OBJ)
    line.setFixedHeight(1)
    tb = getattr(dialog, "titlebar", None)
    p = getattr(tb, "_p", None)
    line.setStyleSheet("QFrame#%s { background: %s; }"
                       % (_SEP_OBJ, p.border if p is not None else "transparent"))
    return line


def _is_action_row(item) -> bool:
    """这个布局项是不是弹框的按钮行。

    **按内容认，不按位置。** 三个弹框的按钮行位置都不一样 ——
    settings_dialog.py:104 的按钮行后面还跟着别的东西；按位置认又会被
    ``addStretch(1)`` 之类骗过去。

    两种形态都要认（实测三种弹框三种写法，漏一种就有一个弹框的按钮滚走）：

    - **一个 layout，里面全是 QPushButton** —— connect_dialog.py:457
      （取消/连接）、tunnel_panel.py:194（取消/添加）；
    - **一个 widget，装着 QDialogButtonBox** —— settings_dialog.py:95
      （保存/取消）。

      第二个形态一开始被漏掉了，因为 docstring 里写着「本仓三个弹框都是
      手排的 QPushButton」——**那个前提是错的**。现场是设置框的「取消」
      落在 y=1774 而弹框只有 992 高，用户点不到。

    判据统一成**「这一项里能找到 QPushButton，而且没有别的非 spacer 控件」**：
    ``_Field`` / ``QTabWidget`` / ``QTableWidget`` / ``QLabel`` 都不是
    QPushButton，正文项过不了这一关。
    """
    w = item.widget()
    if w is not None:
        if isinstance(w, QtWidgets.QDialogButtonBox):
            return True
        # 一个普通 widget 里面**只有**按钮，也算按钮行
        return bool(w.findChildren(QtWidgets.QPushButton)) and not any(
            isinstance(c, (QtWidgets.QLabel, QtWidgets.QCheckBox,
                           QtWidgets.QRadioButton, QtWidgets.QComboBox,
                           QtWidgets.QSpinBox, QtWidgets.QLineEdit,
                           QtWidgets.QTabWidget, QtWidgets.QTableWidget))
            for c in w.findChildren(QtWidgets.QWidget))

    sub = item.layout()
    if sub is None:
        return False
    found = False
    for i in range(sub.count()):
        child = sub.itemAt(i).widget()
        if child is None:
            if sub.itemAt(i).spacerItem() is None:
                return False        # 嵌套布局，不是按钮行
            continue
        if not isinstance(child, QtWidgets.QPushButton):
            return False
        found = True
    return found


def _hoist_actions(dialog: QtWidgets.QDialog, body: QtWidgets.QWidget) -> None:
    """把 *body* 布局里的按钮行搬到 *dialog* 的根布局末尾。

    搬完根布局是「标题栏 / 滚动区 / 按钮行」三层：标题栏钉顶、按钮行钉底，
    中间滚动。

    ## 搬的是**控件**，不是布局项

    原来试的是「摘掉那个 QLayoutItem、把它里面的 QHBoxLayout 挂到新的
    holder 上」。两步都错：

    1. **顺序错**：先 ``addLayout(sub)`` 再 ``removeItem`` 会报
       ``layout already has a parent`` —— ``removeItem`` 只解掉 item 与布局的
       索引关系，**不重置 sub 的父**，而 addLayout 查的正是父。
    2. **删 item 会连带销毁按钮**：改成先 remove 后 add 就不再报错了，
       但 holder 变成空的（``layout() is None``、count 0），按钮仍留在
       y=2139 —— 因为那个 QLayoutItem **持有** sub，``item.deleteLater()``
       把 sub 连带里面的 QPushButton 全销毁了。

    所以**逐个搬 QPushButton**：``takeAt`` 摘、``insertWidget`` 挂，
    整个过程不碰任何 QLayoutItem 的生命周期。这是唯一不会误删的做法。
    """
    body_lay = body.layout()
    root_lay = dialog.layout()
    if body_lay is None or root_lay is None:
        return

    for i in range(body_lay.count() - 1, -1, -1):
        it = body_lay.itemAt(i)
        if not _is_action_row(it):
            continue
        m = body_lay.contentsMargins()

        # 形态二：按钮行本身就是一个 widget（QDialogButtonBox）——
        # 整体 reparent 就行，它内部的按钮跟着走。
        wdg = it.widget()
        if wdg is not None:
            body_lay.removeItem(it)
            holder = QtWidgets.QWidget(dialog)
            holder.setObjectName(_HOLDER_OBJ)
            # **线要满宽，按钮要留白，所以分两层。** 线放在零边距的 outer 里，
            # 按钮仍归 hl 管边距；写成一层的话线会跟着缩进 22px，
            # 变成一小段而不是一条分隔线。
            outer = QtWidgets.QVBoxLayout(holder)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)
            outer.addWidget(_action_sep(dialog))
            hl = QtWidgets.QVBoxLayout()
            outer.addLayout(hl)
            # 上边距同样跟着 body 的 top 走，理由见下面第二种形态那段注释
            # （两个弹框走这两条路，必须一致）。
            hl.setContentsMargins(m.left(), m.top(), m.right(), m.bottom())
            hl.setSpacing(0)
            wdg.setParent(None)
            hl.addWidget(wdg)
            holder.setStyleSheet(_holder_style(dialog))
            root_lay.addWidget(holder)
            root_lay.setContentsMargins(0, 0, 0, 0)
            break

        sub = it.layout()
        buttons = []
        for j in range(sub.count()):
            w = sub.itemAt(j).widget()
            if isinstance(w, QtWidgets.QPushButton):
                buttons.append(w)
        # **倒着摘。** takeAt 会让后面的 item 下标位移，正着摘会漏项。
        for w in reversed(buttons):
            sub.takeAt(sub.indexOf(w))
        holder = QtWidgets.QWidget(dialog)
        holder.setObjectName(_HOLDER_OBJ)
        # 同上：线满宽、按钮留白，分两层。理由见上面第一种形态那段。
        outer = QtWidgets.QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(_action_sep(dialog))
        hl = QtWidgets.QHBoxLayout()
        outer.addLayout(hl)
        hl.setSpacing(10)
        for b in buttons:
            b.setParent(None)
            hl.addWidget(b)
        body_lay.removeItem(it)
        # 两侧留白的 addStretch 靠这一个补：按钮整体右对齐，和原来一致。
        hl.insertStretch(0, 1)
        # 按钮行原本靠 body 布局的 margins 拿到内边距（connect 是 24,16,24,20，
        # settings 是 22,18,22,18，tunnel 是 20,16,20,16），搬到根布局后那层
        # 没了。**从 body 自己的 margins 抄一份**，写死就会有一个对不上。
        #
        # **上边距原来写死 0，现在跟着 body 的 top 走。** 按钮行被搬出滚动区、
        # 直接贴在正文区下面，实测两者之间**一点缝都没有**（滚动区 y=38 高 900
        # → 底边 938，按钮行顶边也是 938），按钮和上面的表/输入框挤成一片。
        # 用 body 自己的 top 而不是写死数值：三个弹框的 top 本来就分别是
        # 16/18/16，跟着它走等于各用各的纵向节奏，也不会有一处对不上。
        m = body_lay.contentsMargins()
        hl.setContentsMargins(m.left(), m.top(), m.right(), m.bottom())
        holder.setObjectName(_HOLDER_OBJ)
        holder.setStyleSheet(_holder_style(dialog))
        root_lay.addWidget(holder)
        root_lay.setContentsMargins(0, 0, 0, 0)
        # item 的所有权已经交给 body_lay，这里不能 deleteLater ——
        # 见本函数 docstring 第 2 条，它持有 sub 和里面的控件。
        break
    body_lay.invalidate()
    root_lay.invalidate()


def _clamp_height(dialog: QtWidgets.QDialog, cap: int) -> None:
    """把超过 *cap* 的弹框高度压回去，并排队再压一次。

    **为什么要在 showEvent 之后再压一次。** showEvent 里窗口还没定尺寸，
    此刻的 ``resize()`` 会被 Qt 紧接着的那次 ``adjustSize()`` 覆盖回去
    （48pt 下实测弹框停在 2235px 不动）。推迟一拍的 ``singleShot(0)``
    跑在布局算完之后，这时 resize 才留得住。

    推迟的那次**装滚动区之后也得压**：正文进了 QScrollArea 之后，它的
    sizeHint 不再驱动外框尺寸，那时才真的拉得住。两次都无害 ——
    没超过上限就什么都不做。
    """

    def _clamp() -> None:
        if dialog.isVisible() and dialog.height() > cap:
            dialog.resize(dialog.width(), cap)

    _clamp()
    QtCore.QTimer.singleShot(0, _clamp)


def fit_to_screen(dialog: QtWidgets.QDialog, body: QtWidgets.QWidget) -> None:
    """给弹框封高度上限，并在超高时把 *body* 装进滚动区。

    ## 为什么需要

    弹框的自然高度是**跟着界面字号缩放**的 —— QSS 里 padding / min-height /
    border-radius 全在 `_SCALE_PROPS` 里，`ui_font_size`（6-48pt）一调，
    整份 QSS 等比放大。实测（主屏工作区 1032px）：

    | 字号 | ConnectDialog | SettingsDialog | _AddDialog |
    |------|---------------|----------------|------------|
    | 11pt（默认） | 939px | 688px | 446px |
    | 18pt | 1189px **超出** | 894px | 596px |
    | 32pt | 1704px **超出** | 1411px **超出** | 888px |
    | 48pt（上限） | 2283px **超出** | 1932px **超出** | 1216px **超出** |

    ConnectDialog **14pt 就开始超**（默认 11pt 的 939px 离 1032 只剩 93px）。
    屏幕一矮更糟：1366x768 的工作区 720px，**默认字号下就已经超出 219px**。
    所以这不是"少数配置才遇到"，是必现。

    ## 只包正文，标题栏和按钮不滚

    标题栏要钉在顶上、按钮行（取消/连接）要留在底下**始终可见可点**。
    全滚的话「连接」按钮会滚出屏幕 —— 那比展示不全更糟。

    ## 必须在 show 之后调

    ``attach_chrome`` 是在弹框构造**过程中**被调的，那时正文还没填完、
    窗口还没映射，既拿不到自然高度、也不知道它在哪块屏上。所以调用点
    统一挂在 ``showEvent``。

    ## 只封高度

    横向超屏不管：弹框可缩放，用户自己能拖窄，而横向封顶会把「主机与端口」
    那种并排控件压到放不下。

    ## 已经装过就直接返回

    ``exec_()`` 会反复触发 showEvent（每次 ``show()`` 一次）。重复装滚动区
    会把 *body* 一层层套进 QScrollArea 里，所以这里认 ``_mavisScrolled``。
    """
    if dialog.property("_mavisScrolled") is True:
        return
    cap = screen_height_for(dialog)
    if cap <= 0:
        return
    dialog.setMaximumHeight(cap)
    # **上限要压两次，一次在 showEvent 里、一次推迟到下一轮事件循环。**
    #
    # `showEvent` 里窗口还没定尺寸：此刻 `resize()` 会被 Qt 紧接着的那次
    # `adjustSize()` 覆盖回去（实测 48pt 下弹框停在 2235px 不动）。
    # 而推迟一拍的 `singleShot(0)` 跑在布局算完之后，这时 resize 才留得住。
    #
    # **两次都无害**：超过上限时压到上限，没超过就什么都不做。
    _clamp_height(dialog, cap)

    # **内容装得下就不要滚动条 —— 多一条 10px 宽的槽子纯粹是视觉噪声，
    # 而这个设计语言里没有描边线分层，那条滚动条会显得脏。**
    #
    # **但「装得下」要比的是整个弹框，不是正文。** 标题栏排在 body 之外
    # （实测 38px），拿 `body.sizeHint()` 去比 `cap`（这是**弹框**的上限）
    # 等于漏掉了它。实测默认字号下 body 991 ≤ cap 992，于是提前返回了，
    # 而真正需要的是 38 + 991 = **1029，超 37px** —— 弹框底部被屏幕切掉。
    # 之前一直没踩到只是因为余量够：加「分组」之前 ConnectDialog 是 955px，
    # 离上限还剩 37px，正好被多出来的那一行吃掉。
    #
    # 按根布局里 **body 的兄弟**逐个累加，而不是写死「标题栏 38px」：
    # 以后再加一栏常驻条带，这条判断会自己跟上，不会又变成一个对不上的数。
    chrome = 0
    root = dialog.layout()
    for i in range(root.count()):
        w = root.itemAt(i).widget()
        if w is not None and w is not body:
            chrome += w.sizeHint().height()
    if body.sizeHint().height() + chrome <= cap:
        return

    # **先把按钮行从 body 里搬到根布局上，再装滚动区。**
    #
    # 三个弹框的按钮行**一开始都在 body 里面**：connect_dialog.py:280 把
    # `root` 重绑定成了 body 的布局，于是后面那句 `root.addLayout(actions)`
    # 加的是 **body 的子项**；tunnel_panel.py:149 同理；settings_dialog.py
    # 本来就直接 `bl.addWidget(buttons)`。所以直接把 body 塞进 QScrollArea，
    # 按钮会跟着滚走：实测 48pt 下「连接」按钮落在 y=2139 而弹框只有 992 高
    # —— 用户点不到，那比展示不全更糟。
    #
    # **必须在 setWidget 之前搬**：一旦 body 进了 viewport，从 body 的布局里
    # 摘 item 就要跨 parent 操作，Qt 会留下半摘的 item（实测 _probe8）。
    _hoist_actions(dialog, body)

    area = QtWidgets.QScrollArea(dialog)
    area.setObjectName("dialogScroll")
    area.setWidgetResizable(True)
    # **横向滚动条不要。** 正文里有 `setFixedWidth(88)` 的端口框、
    # 需要完整宽度的表格；一旦横向也滚，手指在输入框里横向滑动会连带
    # 拖动正文，Qt 的 QLineEdit 自己处理滚动所以正文不会动，但表格会。
    area.setHorizontalScrollBarPolicy(
        QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
    # viewport 默认透明，会漏出底下那块不透明底板的颜色差；
    # 弹框底色是 bg_dialog，viewport 跟着铺同色。
    area.setStyleSheet(
        "QScrollArea#dialogScroll { background: %s; }"
        "QScrollArea#dialogScroll > QWidget > QWidget "
        "{ background: %s; }" % (_dialog_bg(dialog), _dialog_bg(dialog)))

    # ---------------------------------------------------------------- 顺序
    # **`setWidget()` 必须放在最后。**
    #
    # `QScrollArea.setWidget(w)` 会**立刻**把 w reparent 到 area 的 viewport，
    # 于是 `lay.itemAt(i).widget()` 从此再也找不到它（实测 idx 恒为 -1），
    # 走进「不在根布局里」那条分支、`area.deleteLater()` 把 area 连同已经在
    # 它底下的 body 一起销毁 —— 弹框当场塌成 38px，只剩一条 TitleBar
    # （_probe7 的输出就是现场）。
    #
    # 所以顺序固定是：**先在根布局里定位 body -> replaceWidget 换成 area ->
    # 最后 setWidget 把 body 塞进 area**。replaceWidget 只是换占位，
    # body 的父子关系在那时还没动。
    lay = dialog.layout()
    idx = -1
    if lay is not None:
        for i in range(lay.count()):
            if lay.itemAt(i).widget() is body:
                idx = i
                break
    if idx < 0:
        # 正文不在根布局里（结构变了）：不装滚动区，只留上面的高度上限。
        # **不能猜一个 index 去 replace** —— 那会把标题栏或按钮换掉。
        area.deleteLater()
        return
    lay.replaceWidget(body, area)
    dialog.setProperty("_mavisScrolled", True)
    area.setWidget(body)
    # **原来的 addStretch 语义会变。** `setWidget()` 把 body reparent 到
    # viewport，而 viewport 拿的是整个可视区高度 —— 内容不足时底部不再
    # 由 stretch 撑开，而是整个正文区固定高。正文里那些 `addStretch(1)`
    # （connect_dialog.py:435、tunnel_panel.py:184）会跟着一起被压扁，
    # 所以这一层要 invalidate 让布局重算。
    lay.invalidate()
    # 装完再压一次：正文进了滚动区之后 sizeHint 不再驱动外框尺寸，
    # 这一拍才真的把外框拉回上限内。
    _clamp_height(dialog, cap)


def _dialog_bg(dialog: QtWidgets.QWidget) -> str:
    """弹框的根底色。``palette.bg_dialog``，拿不到就退回 ``bg_base``。"""
    tb = getattr(dialog, "titlebar", None)
    p = getattr(tb, "_p", None)
    if p is not None:
        return p.bg_dialog
    return "transparent"


class FitToScreenMixin:
    """给弹框在 ``showEvent`` 上挂一次 :func:`fit_to_screen`。

    **为什么是 mixin 而不是逐个写 showEvent。** 三个弹框的 showEvent 各不相同，
    挨个塞同一段是典型的「漏一个就有一个弹框没封顶」。

    **继承顺序要放在 ``QtWidgets.QDialog`` 之后** —— mixin 定义了
    ``showEvent``，而 ``super().showEvent(event)`` 走的就是 QDialog 的虚方法。
    """

    #: 正文容器。见 :func:`fit_to_screen`。子类赋成 ``self.body`` 的那个
    #: QWidget：它在根布局里紧跟标题栏、按钮行之前。
    _scroll_body: QtWidgets.QWidget | None = None

    def fit(self) -> None:
        if self._scroll_body is not None:
            fit_to_screen(self, self._scroll_body)

    def showEvent(self, event) -> None:
        self.fit()
        super().showEvent(event)


def pin(dialog: QtWidgets.QDialog, tb: TitleBar) -> None:
    """把标题栏钉在对话框最上面一条。窗口尺寸变了要重新调一次。"""
    tb.setGeometry(0, 0, dialog.width(), TITLEBAR_H)
    tb.raise_()
    _raise_corner_frame(dialog)


def _raise_corner_frame(dialog: QtWidgets.QDialog) -> None:
    """把四角挖角浮层抬回最上层。

    **谁最后 raise 谁在上面，而顺序正好是反的。** `msgbox._install` 先装圆角
    浮层、再注入标题栏，`inject_chrome` / `pin` 里的 `tb.raise_()` 于是把标题栏
    压在浮层**上面** —— 标题栏满宽铺角，四个角有两个（上面两个）被它盖死，
    症状是「弹框只有下面两个角是圆的」。这里在每次 `raise_` 之后补一次，
    盖住所有会抢层级的路径（`pin` / `pad` / `inject_chrome`）。
    """
    frame = getattr(dialog, "_mavisFrame", None)
    if frame is not None:
        frame.raise_over()


def pad(dialog: QtWidgets.QDialog, tb: TitleBar) -> None:
    """把布局的内容整体顶下一个标题栏的高度。**只能在内容设完之后调。**

    QMessageBox / QFileDialog / QInputDialog 内部的 QGridLayout 在 PyQt6 里
    一个 ``insert*`` 方法都没暴露（``QLayout.insertRow`` 是 protected，
    Python 调不到），所以只能贴一条标题栏在 (0,0)、再把内容顶下去。

    顺序有讲究：**必须等正文设完再设边距**。`setText` / `setIconText` 会让
    QMessageBox 重排布局，之前设好的边距会被冲掉——症状是图标和标题被标题栏
    盖掉一半（看着像"内容被裁了"，其实是没顶下去）。调完还得 `adjustSize()`。
    """
    lay = dialog.layout()
    if lay is not None:
        m = lay.contentsMargins()
        lay.setContentsMargins(m.left(), m.top() + TITLEBAR_H, m.right(),
                               m.bottom())
    pin(dialog, tb)
    dialog.adjustSize()


def inject_chrome(dialog: QtWidgets.QDialog, title: str, palette: Palette,
                  icon_name: str = "info"):
    """给**已经有布局**的对话框补一条标题栏，返回 ``TitleBar``。

    和 ``attach_chrome`` 的区别：那个类自己建布局、可以直接 ``addWidget``；
    这个是往现成的布局里插。

    - ``QBoxLayout``（设置框、连接框、转发框都是自己建的）——
      ``insertWidget(0, tb)``，最干净，完事。
    - 其它布局（QMessageBox 那种）——贴上去，**由调用方在 ``showEvent``
      里调 :func:`pad`**，因为得等正文设完才知道要顶多少。
    """
    if frameless_enabled():
        dialog.setWindowFlag(QtCore.Qt.WindowType.FramelessWindowHint, True)
    tb = TitleBar(dialog, palette, dialog, title=title, kinds=(CLOSE,),
                  icon_name=icon_name, surface=palette.bg_dialog)
    lay = dialog.layout()
    if isinstance(lay, QtWidgets.QBoxLayout):
        lay.insertWidget(0, tb)
        return tb
    tb.setGeometry(0, 0, dialog.width(), TITLEBAR_H)
    tb.raise_()
    _raise_corner_frame(dialog)
    return tb


def native_event(window: QtWidgets.QWidget, message):
    """窗口的 ``nativeEvent`` 里转发到这里，返回 ``(bool, int)``。

    调用方的 ``nativeEvent`` 必须是**这个类自己的方法**（它就是 QWidget 的
    虚方法），写成一行 ``return native_event(self, message)`` 即可。

    三个 PyQt6 的坑都集中在这一个函数里，详见本文件 Windows 段的注释：
    不能调 ``super().nativeEvent()``、不能对所有消息都返回 ``True``、
    不能靠混类来提供它。
    """
    if not frameless_enabled() or sys.platform != "win32":
        return False, 0
    tb = getattr(window, "titlebar", None)
    if tb is None:
        return False, 0
    handled = handle_native(window, tb, message)
    return handled if handled is not None else (False, 0)


class CaptionButton(QtWidgets.QToolButton):
    """最小化 / 最大化 / 关闭。

    刻意用 QToolButton 而不是 QAbstractButton：``tests/_uibtns.py`` 的结构
    守卫只收 QPushButton 和 QToolButton，继承自 QAbstractButton 的话，
    「按钮没接槽」「按钮没 tooltip」这类问题它一条都抓不到——正好是
    自绘控件最容易出的错。
    """

    def __init__(self, kind: str, palette: Palette,
                 parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self.kind = kind
        self._p = palette
        self._restoring = False      # 窗口已最大化 -> 画「还原」而不是「最大化」
        # 最大化按钮被报告成 HTMAXBUTTON（为了 Win11 贴边分屏），那条路上
        # Qt 收不到鼠标事件，underMouse() 永远是 False。悬停态由
        # WM_NCHITTEST 那边喂进来。
        self._native_hover = False
        # 外观全权接管：paintEvent 被整个覆盖、不调 super()，所以应用级
        # QSS 里那些 QToolButton 规则（边框、圆角、内边距）一律进不来。
        # 不需要 QToolButton.setFlat()——那是 QPushButton 才有的 API。
        self.setFixedSize(CAPTION_W, TITLEBAR_H)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.setCursor(QtCore.Qt.CursorShape.ArrowCursor)
        self.setToolTip(_TIPS[kind])
        # _uibtns 第 2 条：没有文字的按钮必须有 tooltip；三个按钮的提示还必须
        # 互不相同（第 3 条）。这里的 _TIPS 满足后者，切换到「还原」时不撞。
        #
        # **这里原来还有一行 `self.setProperty("caption", kind)`，已删。**
        # 它设了但**没有任何读者**：全仓没有 `[caption=...]` 选择器，也没有
        # 任何代码读 `property("caption")`；颜色判别走的是 Python 属性
        # `self.kind`（`_bg()` / `_fg()` 里 `kind == CLOSE`），两者是同一份
        # 信息的两个拷贝。
        # 而且**加 QSS 规则也救不回来**：本控件的外观全权接管 ——
        # `paintEvent` 整个覆盖、不调 `super()`，文件里 146-148 行自己写着
        # 「应用级 QSS 里那些 QToolButton 规则一律进不来」。所以它不是
        # 「预留的样式钩子」，是**结构上永远不可能被消费**的死物。
        # 关闭键 hover 变红早就实现了（`_bg()` 里 `kind == CLOSE` → `p.danger`）。

    # ------------------------------------------------------------ 状态

    def set_restoring(self, on: bool) -> None:
        if on == self._restoring:
            return
        self._restoring = on
        self.setToolTip(_TIPS_RESTORE if on and self.kind == MAX else _TIPS[self.kind])
        self.update()

    def set_native_hover(self, on: bool) -> None:
        if on == self._native_hover:
            return
        self._native_hover = on
        self.update()

    def apply_palette(self, palette: Palette) -> None:
        self._p = palette
        self.update()

    def _is_hover(self) -> bool:
        return self._native_hover or self.underMouse()

    # ------------------------------------------------------------ 配色

    def _bg(self) -> str:
        if not self.isEnabled():
            return "transparent"
        if self.kind == CLOSE and self._is_hover():
            return self._p.danger
        if self._is_hover():
            return self._p.bg_elevated
        if self.isDown():
            return self._p.bg_active
        return "transparent"

    def _fg(self) -> str:
        if not self.isEnabled():
            return self._p.text_muted
        if self.kind == CLOSE and self._is_hover():
            # **✕ 画在 `danger` 填充上，图形色必须按填充色自己算。**
            #
            # 原来用的是 `text_on_accent` —— 那是按 **accent** 推的，拿它压
            # `danger` 属于**跨族借用**（`design/v1/__init__.py` 的硬约束 1）：
            # 浅色主题下它是近黑 `#0d0f14`，压 `#C02626` 只有 **3.237**，
            # 而同一个填充用白字是 **5.921**。借用不会让数值变差到
            # 「有测试会红」，所以只能就地按目标填充色算。
            return text_on(self._p.danger)
        if self._is_hover() or self.isDown():
            return self._p.text
        return self._p.text_dim

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)

        bg = self._bg()
        if bg != "transparent":
            p.fillRect(self.rect(), QtGui.QColor(bg))

        # 1px 实线必须落在整数像素上，否则半亮半暗显得发虚。
        pen = QtGui.QPen(QtGui.QColor(self._fg()))
        pen.setWidth(1)
        p.setPen(pen)

        w, h = self.width(), self.height()
        cx, cy = w // 2, h // 2
        half = GLYPH // 2

        if self.kind == MIN:
            # 最小化：一条 10px 横线，不放在几何正中，落在视觉重心上。
            y = cy + half - 1
            p.drawLine(cx - half, y, cx + half, y)
        elif self.kind == MAX:
            if self._restoring:
                self._paint_restore(p, cx, cy)
            else:
                p.drawRect(cx - half, cy - half, GLYPH, GLYPH)
        else:
            d = GLYPH // 2 - 1          # 关闭是 8px，比方框小一圈
            p.drawLine(cx - d, cy - d, cx + d, cy + d)
            p.drawLine(cx + d, cy - d, cx - d, cy + d)
        p.end()

    def _paint_restore(self, p: QtGui.QPainter, cx: int, cy: int) -> None:
        """两个错开的方框。后面那个被前面那个挡住一部分。"""
        half = GLYPH // 2
        s = GLYPH - 2
        back_x, back_y = cx - half + 2, cy - half
        front_x, front_y = cx - half, cy - half + 2

        p.drawRect(back_x, back_y, s, s)
        # 前面那个方框要盖住后者的左下角：先用底色填平，再画自己的边。
        #
        # **高度是 `s - 1` 而不是 `s - 2`**：`fillRect` 填的是 y..y+h-1，
        # 原来的 `s - 2` 只盖到 `back_y + s - 1`，而后方框的**下边线**在
        # `back_y + s` —— 那一行整条露着，穿进前方框内部（实测像素 (23,22)）。
        # 前方框的纵向范围是 `back_y+2 .. back_y+s+2`，所以那一行本来就该被盖。
        p.fillRect(back_x, back_y + 2, s - 2, s - 1, QtGui.QColor(self._bg_effective()))
        p.drawRect(front_x, front_y, s, s)

    def _bg_effective(self) -> str:
        """画「还原」时要拿底色去遮后一个方框，透明时得换成窗口底色。"""
        bg = self._bg()
        return self._p.bg_base if bg == "transparent" else bg


class TitleBar(QtWidgets.QWidget):
    """窗口顶部那条：左边应用标识 + 名称，右边窗口按钮。

    **主窗口和对话框用同一个类。** 对话框传 ``kinds=(CLOSE,)`` 就是只有
    关闭按钮、没有最小化/最大化；标题栏同时承担"窗口叫什么"这件事，所以
    对话框内容里那个重复的大标题就该删掉——留着就是同一句话说两遍。
    """

    def __init__(self, window: QtWidgets.QWidget, palette: Palette,
                 parent: QtWidgets.QWidget | None = None,
                 title: str = "MavisSSH", kinds=(MIN, MAX, CLOSE),
                 icon_name: str = "terminal", surface: str | None = None):
        super().__init__(parent)
        self._window = window
        self._p = palette
        # 弹窗这一层要比主窗口**亮一档**，否则弹出来和窗口糊成一片、一眼
        # 分不出哪儿是哪儿。主窗口传 None 用 bg_base，对话框由 attach_chrome /
        # inject_chrome 传 bg_dialog。深浅两套的值都在 palette.bg_dialog 里，
        # 是对着渲染图挑的，不是随手取的。
        #
        # **只记"是不是弹窗"，不记具体色值。** 记色值的话，换主题时
        # apply_palette 换了 _p 而 _surface 还冻在构造时那一档，主窗口的
        # 标题栏就再也不跟着深浅切换了（判据当场抓到过）。
        self._is_dialog = surface is not None
        # **主窗口的标题栏用 `bg_surface`，不再是 `bg_base`。** 它与菜单栏、
        # 状态栏一起构成三条「chrome 带」，现在靠**色差**浮在内容区之上，
        # 底下那条发丝线已经删掉 —— 两侧同色时那条线是唯一的分层信号，
        # 而用户要的是色差分层。弹窗那条路径不变（`bg_dialog` 对弹窗体
        # 本来就有自己的层次）。
        self._surface = surface or palette.bg_surface
        # **必须在这里挂，不能让各个调用点自己挂。** :func:`native_event` 靠
        # `getattr(window, "titlebar", None)` 找标题栏，找不到就静悄悄返回
        # (False, 0)——标题栏照样画得出来，可拖动和边缘缩放整个是死的，而且
        # 一点报错都没有。放在唯一的收口上，漏不掉。
        window.titlebar = self
        self.setFixedHeight(TITLEBAR_H)
        self.setObjectName("titleBar")

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(14, 0, 0, 0)
        # **spacing 必须是 0**：QHBoxLayout 的 spacing 作用在**所有**相邻控件
        # 之间，图标和文字要留空的话用 addSpacing 单独插，不然右上角三个按钮
        # 之间也会被撑出缝来（实测按钮之间裂开 9px，和原生标题栏对不上）。
        lay.setSpacing(0)

        self.mark = QtWidgets.QLabel(self)
        self.mark.setFixedSize(16, 16)
        self._icon_name = icon_name
        if icon_name == "logo":
            # 主窗口用 Logo 字形（不带底板），标题栏本来就小，底板会糊成一坨。
            # 对话框继续用各自的语义图标（终端 / 文件夹 / 插头）——对话框图标
            # 说的是"这个窗口干什么"，应用 Logo 说的是"这是哪个程序"。
            # **渐变两档必须跟着 palette 走**：不传的话 `logo_pixmap` 会用它自己
            # 的默认冷蓝（`#6ba1ff` / `#2f6fe0`），而侧栏那一处（`sidebar.py`）
            # 与窗口图标（`logo.window_icon`）都传了 —— 于是标题栏这一枚是全仓
            # **唯一**用写死冷色的地方，落在暖中性浅色主题上会一眼看出色温跳变。
            #
            # **基色用 `accent_graphic` 而不是 `accent`。** 这一枚压在标题栏底上，
            # 而对话框的标题栏底是 `bg_dialog` —— 浅色下 `accent` 对它是 2.67，
            # 破 WCAG 1.4.11 的 3:0。`top` 保留 `accent_hover` 作渐变高光：
            # 渐变本来就该有亮部，不是缺陷。
            self.mark.setPixmap(logo.logo_pixmap(
                16, plate=False, glyph=palette.accent_graphic,
                top=palette.accent_hover, bottom=palette.accent_graphic,
                dpr=self.devicePixelRatioF()))
        else:
            self.mark.setPixmap(icons.icon_pixmap(icon_name, palette.accent_graphic, 16, 1.8))
        lay.addWidget(self.mark, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        lay.addSpacing(9)

        self.label = QtWidgets.QLabel(title, self)
        self.label.setObjectName("titleBarText")
        lay.addWidget(self.label, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)

        self.btn_min = self.btn_max = self.btn_close = None
        self.buttons = []
        for kind in kinds:
            b = CaptionButton(kind, palette, self)
            lay.addWidget(b, 0, QtCore.Qt.AlignmentFlag.AlignTop)
            setattr(self, "btn_" + kind, b)
            self.buttons.append(b)

        for kind, b in zip(kinds, self.buttons):
            b.clicked.connect({
                MIN: self._on_min,
                MAX: self.toggle_maximize,
                CLOSE: self._on_close,
            }[kind])

    # ------------------------------------------------------------ 行为

    def _on_min(self) -> None:
        self._window.showMinimized()

    def toggle_maximize(self) -> None:
        win = self._window
        if win.isMaximized():
            win.showNormal()
        else:
            win.showMaximized()

    def _on_close(self) -> None:
        self._window.close()

    def sync_state(self) -> None:
        """窗口最大化状态变了就把「最大化」翻成「还原」。"""
        if self.btn_max is not None:
            self.btn_max.set_restoring(self._window.isMaximized())

    # ------------------------------------------------------------ 主题

    def apply_palette(self, palette: Palette) -> None:
        self._p = palette
        # 底色跟着换主题重算，别冻在构造时那一档。
        self._surface = palette.bg_dialog if self._is_dialog else palette.bg_surface
        if self._icon_name == "logo":
            self.mark.setPixmap(logo.logo_pixmap(
                16, plate=False, glyph=palette.accent_graphic,
                top=palette.accent_hover, bottom=palette.accent_graphic,
                dpr=self.devicePixelRatioF()))
        else:
            self.mark.setPixmap(icons.icon_pixmap(
                self._icon_name, palette.accent_graphic, 16, 1.8))
        self.label.setStyleSheet("color: %s; background: transparent;" % palette.text_dim)
        for b in self.buttons:
            b.apply_palette(palette)

    # ------------------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor(self._surface))
        # **发丝线只给弹窗画，不给主窗口画。**
        #
        # 主窗口原来这里是**无条件**画一条 1px `border` 线，理由（注释原话）
        # 是「与下方 QMenuBar 两侧同色，糊在一起看不出分层」。现在主窗口的
        # 标题栏与菜单栏都是 `bg_surface`、内容区是 `bg_base`，分层由色差
        # 承担，这条线是重复 —— 用户要求去掉边框线、按色差区分。
        #
        # **弹窗那一条必须留着**：弹窗标题栏和弹窗体**都是 `bg_dialog`**，
        # 色差在那里等于 1.000，线是唯一的分层信号。我第一版无条件删掉它，
        # 判据当场抓到了三个弹窗（消息框/文件框/输入框）底边不再是 border 色
        # —— 那是真的丢了信息，不是判据过时。
        if self._is_dialog:
            p.fillRect(0, self.height() - 1, self.width(), 1,
                       QtGui.QColor(self._p.border))
        p.end()

    def mouseDoubleClickEvent(self, event) -> None:
        """双击空白处切换最大化。

        标题栏整体向系统报了 HTCAPTION，所以 Windows 自己就会处理双击；
        这里再处理一次是为了**离线测试**——测试里发的是 Qt 的双击事件，走的
        是这条路径。双击在真机上只会触发其中一条，不会连着切两次。

        **弹窗上双击不做任何事。** 这里原来无条件 `self.btn_max.click()`，
        而所有弹窗都只传 ``kinds=(CLOSE,)``（见 :func:`inject_chrome`），
        ``btn_max`` 恒为 None —— 非 Windows 上那一版不自绘边框、双击直接落到
        这个子控件上，一次双击就是
        ``AttributeError: 'NoneType' object has no attribute 'click'``，
        而紧邻的 `sync_state` 里是有 None 判断的。

        选「什么都不做」而不是「关掉弹窗」：双击一下标题不该把用户正在填的
        表单、没保存的设置一起带走，这也正是 Windows 原生对话框的行为
        （对话框不能最大化，双击标题栏是空操作）。
        """
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            # 窗口才切换最大化；弹窗没有这个按钮，双击就地作废（事件仍然吃掉，
            # 不让它继续往父控件上传）。
            if self.btn_max is not None:
                self.btn_max.click()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


# ====================================================================== Windows

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    WM_GETMINMAXINFO = 0x0024
    WM_NCHITTEST = 0x0084
    WM_NCLBUTTONDOWN = 0x00A1
    WM_NCLBUTTONUP = 0x00A2
    WM_NCLBUTTONDBLCLK = 0x00A3

    HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
    HTLEFT, HTRIGHT, HTTOP = 10, 11, 12
    HTTOPLEFT, HTTOPRIGHT, HTBOTTOM = 13, 14, 15
    HTBOTTOMLEFT, HTBOTTOMRIGHT = 16, 17

    RESIZE_MARGIN = 6       # 窗口四边的不可见缩放带，和原生边框宽度接近
    #: 极角的让出尺寸（见方）。**比 RESIZE_MARGIN 小**：只让最外侧这么一小块，
    #: 四个角就都能拖，而标题栏按钮只在自己角落上少一小块，关闭键照常点得中。
    CORNER_GRAB = 4

    WM_SETCURSOR = 0x00A0
    IDC_ARROW, IDC_SIZEALL = 32512, 32646
    IDC_SIZENWSE, IDC_SIZENESW = 32642, 32643
    IDC_SIZEWE, IDC_SIZENS = 32644, 32645

    #: 命中码 -> 该显示的系统光标。**光标必须自己设**，不能指望 Windows：
    #: 上面实测本窗口样式里没有 `WS_THICKFRAME`，而 DefWindowProc 只在带
    #: thickframe 的窗口上才给 `HTLEFT` 这类命中码套 resize 光标 —— 和
    #: 「系统不起缩放循环」是同一个根因。全仓搜不到一处 resize 光标设置，
    #: 结果就是八个边角拖得动、却全程一个普通箭头。
    CURSOR_FOR_CODE = {
        HTLEFT: IDC_SIZEWE, HTRIGHT: IDC_SIZEWE,
        HTBOTTOM: IDC_SIZENS,
        HTTOPLEFT: IDC_SIZENWSE, HTBOTTOMRIGHT: IDC_SIZENWSE,
        HTTOPRIGHT: IDC_SIZENESW, HTBOTTOMLEFT: IDC_SIZENESW,
    }

    _E = QtCore.Qt.Edge
    #: 非客户区命中码 -> 要缩放的边。**写成纯映射而不是散在 if 里**，
    #: 这样「每一条边都接上了」是可以穷举断言的 —— 漏一个角的后果是
    #: 「那个角拖不动」，而现场表现和「整条边都坏了」一模一样。
    #:
    #: **表里没有 `HTTOP`，这是有意的。** 顶边归标题栏拖动（见 `hit_test`
    #: 第 2 步那段），`hit_test` 已经不返回 `HTTOP` 了，留着这一条就是
    #: 一份**永远走不到的死映射** —— 而「表里有、代码不返回」正是最容易
    #: 骗过自己的那种不一致：读表的人会以为顶边还能改高度。
    #: 要真把顶边缩放加回来，得同时加回判定**和**这一条。
    RESIZE_EDGE_FOR_CODE = {
        HTLEFT: _E.LeftEdge,
        HTRIGHT: _E.RightEdge,
        HTBOTTOM: _E.BottomEdge,
        HTTOPLEFT: _E.TopEdge | _E.LeftEdge,
        HTTOPRIGHT: _E.TopEdge | _E.RightEdge,
        HTBOTTOMLEFT: _E.BottomEdge | _E.LeftEdge,
        HTBOTTOMRIGHT: _E.BottomEdge | _E.RightEdge,
    }

    def edge_resize_rect(start, start_pos, pos, edges):
        """纯几何：按*edges* 把 *start* 拉到当前光标位置。**不碰 Qt 窗口。**

        拆成纯函数是为了能穷举断言 —— 真正驱动缩放的是那个轮询光标的定时器，
        它在测试里没法真跑（按钮状态是异步的）。几何一旦算错，现场表现是
        「往右拖窗口反而变窄」，而这跟「没接线」长得几乎一样。
        """
        r = QtCore.QRect(start)
        dx = pos.x() - start_pos.x()
        dy = pos.y() - start_pos.y()
        if edges & _E.LeftEdge:
            r.setLeft(start.left() + dx)
        if edges & _E.RightEdge:
            r.setRight(start.right() + dx)
        if edges & _E.TopEdge:
            r.setTop(start.top() + dy)
        if edges & _E.BottomEdge:
            r.setBottom(start.bottom() + dy)
        return r

    def edge_resize_geometry(start, start_pos, pos, edges, min_w, min_h):
        """缩放后的**窗口几何**（含位置）。

        `edge_resize_rect` 只描述矩形的四条边怎么变，而缩左边/缩上边光改边
        是不够的 —— **还得把窗口挪过去**，这一层才做那件事，外加最小尺寸夹取。

        **顶到最小尺寸时被拖的那条边必须停住。** 宽度已经被 `resize()` 夹在
        最小值上，左边界若还跟着光标走，窗口就成了「宽度纹丝不动、位置一路
        平移」—— 看着像窗口在滑，而不是在缩放。

        所以左/上边界不取 `r.left()`/`r.top()`（那只是光标的位移），改用
        「对面那条边 - 缩完的尺寸」反推。没顶到最小尺寸时两者恒等：

            start.right() - w + 1 == start.left() + dx == r.left()

        """
        r = edge_resize_rect(start, start_pos, pos, edges)
        w = max(r.width(), min_w)
        h = max(r.height(), min_h)
        if edges & _E.LeftEdge:
            r.setLeft(start.right() - w + 1)
        if edges & _E.TopEdge:
            r.setTop(start.bottom() - h + 1)
        return r

    def _left_button_down() -> bool:
        """左键当前是否按着。

        **不能用 `QApplication.mouseButtons()`** —— 按下发生在非客户区，
        Qt 收不到那次事件，鼠标按钮状态里也就没有它，松手反而永远检测不到，
        定时器停不下来，窗口会一直跟着光标跑。`GetAsyncKeyState` 问的是
        物理按键状态，与消息落在哪一块无关。
        """
        return bool(_user32.GetAsyncKeyState(0x01) & 0x8000)

    def resize_tick(win, start_geom, start_pos, edges) -> bool:
        """推进一次缩放，返回是否**还要继续**（左键松开就返回 False）。

        抽成模块级函数是为了能单测：上一版这段逻辑埋在定时器的闭包里，
        测试只能验到「定时器起来了」就收工，于是 `resize(QRect)` 那个
        TypeError 一路漏到真机才炸 —— **启动 ≠ 生效**，中间隔着这一整段。
        """
        if not _left_button_down():
            return False
        pos = QtGui.QCursor.pos()
        if pos == start_pos and win.geometry() == start_geom:
            return True               # 还没动，别做无谓的 resize
        # **`setGeometry()` 吃 QRect，`resize()` 不吃。** 给 `resize()` 传
        # QRect 报的是 `arguments did not match any overloaded call`，而且
        # 这个 TypeError 冒在定时器槽里 —— 直接 crash 整个应用。
        #
        # **这里必须连位置一起给，不能只给尺寸。** 缩左边/缩上边时窗口的
        # 左上角要跟着光标挪，只改尺寸的话那条边原地不动 —— 拖左边反而是
        # 右边在长。实测 800x600 的窗口拖左边往左 70：`x` 纹丝不动，宽度
        # +70，缩的是右半边；拖上边往上 70 同理，高度从下边长出去。
        # 对右边/下边反而是对的：那边靠的正是「对面钉住不动」。
        r = edge_resize_geometry(start_geom, start_pos, pos, edges,
                                 win.minimumWidth(), win.minimumHeight())
        win.setGeometry(r)
        return True

    def begin_edge_resize(win, edges) -> None:
        """从非客户区按下起一次缩放循环，直到左键松开。

        **为什么不能交给系统。** 无边框窗口的样式实测是 ``WS_POPUP``，
        **没有 ``WS_THICKFRAME``**（style=0x960B0000，WS_CAPTION / WS_BORDER /
        WS_DLGFRAME 同样没有）。Windows 只在带 thickframe 的窗口上对
        ``HTLEFT`` 这类返回值起 ``SC_SIZE`` 缩放循环 —— 所以 `hit_test`
        明明返回了正确的边缘码（实测左边 10 / 右边 11 / 上边 12 / 下边 15 /
        四角 13·14·16·17），拖边缘**一点反应都没有**，连光标都不变。

        补 ``WS_THICKFRAME`` 是另一条路，但代价是系统会自己画一圈方角边框和
        投影，正好压在自绘的 5px 圆角描边上 —— 为了能拖动而毁掉圆角，不划算。
        这里改成自己轮询光标，改多少算多少，最小尺寸由 `edge_resize_geometry`
        夹取 —— 被拖的那条边顶到最小值就**停住**，不会变成窗口平移。
        """
        if getattr(win, "_edge_timer", None) is not None:
            win._edge_timer.stop()          # 上一次没停干净就先停掉，别叠两个循环
        start_geom = QtCore.QRect(win.geometry())
        start_pos = QtGui.QCursor.pos()

        def tick():
            if not resize_tick(win, start_geom, start_pos, edges):
                t.stop()
                t.deleteLater()
                win._edge_timer = None

        t = QtCore.QTimer(win)
        t.timeout.connect(tick)
        t.start(16)
        win._edge_timer = t

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    class _MINMAXINFO(ctypes.Structure):
        _fields_ = [("ptReserved", wintypes.POINT),
                    ("ptMaxSize", wintypes.POINT),
                    ("ptMaxPosition", wintypes.POINT),
                    ("ptMinTrackSize", wintypes.POINT),
                    ("ptMaxTrackSize", wintypes.POINT)]

    # **必须显式声明原型。** `ctypes.windll.user32.MonitorFromWindow` 的默认
    # restype 是 c_int，64 位下会把 HMONITOR 句柄截断成 32 位，
    # GetMonitorInfoW 拿着垃圾句柄写内存 —— 窗口一 show() 就 access
    # violation（实测 0xC000041D，连 traceback 都来不及打）。
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.MonitorFromWindow.restype = wintypes.HANDLE
    _user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(_MONITORINFO)]
    _user32.GetMonitorInfoW.restype = wintypes.BOOL
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    # 同理：`LoadCursorW` / `SetCursor` 传的都是 HANDLE，不声明原型就是
    # 默认的 c_int 返回值，64 位下句柄被截成 32 位 —— 传进去的是个野指针，
    # 光标要么不换，要么整个进程崩在 user32 里。
    _user32.LoadCursorW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _user32.LoadCursorW.restype = wintypes.HANDLE
    _user32.SetCursor.argtypes = [wintypes.HANDLE]
    _user32.SetCursor.restype = wintypes.HANDLE


    def _set_hover(tb, kind, on) -> None:
        b = getattr(tb, "btn_" + kind, None)
        if b is not None:
            b.set_native_hover(on)

    def hit_test(win, tb, pos: QtCore.QPoint) -> int:
        """窗口坐标 *pos* 落在哪一块，返回 WM_NCHITTEST 的结果码。

        判定顺序是 **极角 -> caption 按钮 -> 缩放边框 -> 标题栏 -> 客户区**，
        和原生窗口一致，顺序反过来会坏掉几处：

        - 标题栏占着最上面 38px，先判它的话 HTTOP/HTTOPLEFT/HTTOPRIGHT 永远
          轮不到，用户就没法从顶边拉窗口了。
        - 但右上角的按钮又必须压过缩放边框，否则最右 6px 变成"拉窗口"，
          关闭按钮点不中。

        **「极角」这一层是为四个角都能缩放而加的。** 那 6px 顶边整个落在
        标题栏里，而右上角被最小化/关闭按钮盖着，按钮优先的话右上角永远返回
        `HTCLIENT` —— 四角里就少一个（实测另外三角都正常）。让出最外侧
        `CORNER_GRAB` 见方即可：四个角全都能拖，而按钮只在自己的可点区里少
        一小块，关闭按钮照常点得中。

        写成**模块级函数**而不是混进 MainWindow 的基类：`nativeEvent` 是 Qt
        的虚方法，把一个定义了它的纯 Python mixin 塞进 QMainWindow 的基类里，
        sip 算不出正确的 vtable 槽位，窗口一 show() 就 access violation
        （实测 0xC000041D，traceback 都打不出来）。虚方法必须写在真正的
        Qt 子类上，逻辑本身才挪到函数里。
        """
        # 按钮的 geometry() 是相对**标题栏**的，pos 是相对窗口的；三层坐标
        # 差一层就会得到完全错误的答案。这里必须用 geometry()——rect() 是
        # 按钮**自己**的坐标 (0,0,w,h)，拿它去 contains() 标题栏坐标，等于
        # 把按钮的判定区安到了标题栏左上角（真踩过：贴边分屏浮窗跑到了
        # 窗口左上角，而「标题栏左侧返回 HTCAPTION」那条判据照样是绿的）。
        in_tb = tb.mapFrom(win, pos)

        w, h = win.width(), win.height()
        x, y = pos.x(), pos.y()
        m = RESIZE_MARGIN
        g = CORNER_GRAB

        # 0. 极角：两轴都在最外侧 `CORNER_GRAB` 见方内。**必须在按钮之前**，
        #    否则右上角那个角永远被按钮吃掉。它比整条缩放边框（6px）小，
        #    所以按钮只在自己的角落上少一小块，而不是整条让出去。
        if (x <= g and y <= g) or (x <= g and y >= h - g):
            _set_hover(tb, MAX, False)
            for other in tb.buttons:
                _set_hover(tb, other.kind, False)
            return HTTOPLEFT if y <= g else HTBOTTOMLEFT
        if (x >= w - g and y <= g) or (x >= w - g and y >= h - g):
            _set_hover(tb, MAX, False)
            for other in tb.buttons:
                _set_hover(tb, other.kind, False)
            return HTTOPRIGHT if y <= g else HTBOTTOMRIGHT

        # 1. 每一个存在的 caption 按钮都要在这里拦下来，顺序是「极角 -> 按钮 ->
        # 缩放边框 -> 标题栏」。少判一个，那个按钮就会落进 HTCAPTION，
        # 被系统当成标题栏：看上去还是那个位置，但点不动、也拖不动（实测
        # 最小化和关闭就是这样失灵的，而直接调 btn.click() 的测试是绿的
        # ——它绕过了命中测试）。对话框只有关闭按钮，遍历 tb.buttons 即可。
        for b in tb.buttons:
            if b.kind == MAX and b.geometry().contains(in_tb):
                _set_hover(tb, MAX, True)
                return HTMAXBUTTON
        for b in tb.buttons:
            if b.geometry().contains(in_tb):
                for other in tb.buttons:
                    _set_hover(tb, other.kind, False)
                return HTCLIENT
        for b in tb.buttons:
            _set_hover(tb, b.kind, False)

        if x <= m:
            left = True
        elif x >= w - m:
            left = False
        else:
            left = None
        if y >= h - m:
            bottom = True
        else:
            bottom = None

        # 2. 缩放边框：**左边、右边、下边、四个角，没有顶边。**
        #
        # **顶边这一条是「标题栏顶部 7 行全是死区」的根因。** 标题栏是
        # `setGeometry(0, 0, W, 38)` 满宽铺在窗口最上面，所以窗口顶边
        # 100% 落在标题栏里；而 `y <= RESIZE_MARGIN(6)` 判成 HTTOP 的判定
        # 又排在标题栏**前面** —— 于是在标题栏内部，y=0..6 这 7 行（共
        # 7945px，占标题栏 18%）被"拖顶边改高度"吃掉：双击那里不会最大化，
        # 按住拖也不是移动窗口。**用户报「双击时灵时不灵」就是这条。**
        #
        # 顶边缩放和标题栏拖动**物理上互斥**（同一批像素），只能选一个。
        # 选标题栏，理由三条：
        # 1. 双击最大化、拖动窗口是高频交互，缩放顶边是低频的；
        # 2. 无边框自绘标题栏**没有任何视觉提示**告诉用户"最上面这几像素
        #    能拖"，而原生窗口有边框线提示；
        # 3. 缩放能力没有消失 —— 左右、下边、四角全在，用户从四角一样
        #    能改大小（那 4 个角本来就是本次才补齐的，见 e2c1832）。
        #
        # 所以这里只判 bottom，**`top` 这个变量整个去掉**。
        if bottom is True and left is True:
            return HTBOTTOMLEFT
        if bottom is True and left is False:
            return HTBOTTOMRIGHT
        if bottom is True:
            return HTBOTTOM
        if left is True:
            return HTLEFT
        if left is False:
            return HTRIGHT

        # 3. 标题栏其余部分：拖动、双击最大化、拖到边缘贴边全交给系统
        if tb.rect().contains(in_tb):
            return HTCAPTION

        # 4. 客户区
        return HTCLIENT

    def keep_maximize_off_taskbar(hwnd: int, lparam: int) -> None:
        """无边框窗口的最大化尺寸按**工作区**算，否则会盖住任务栏。

        去掉边框之后 Qt 默认把客户区当整个屏幕来铺，窗口一直铺到屏幕底部，
        任务栏被压在下面——这是无边框窗口的经典坑。
        """
        monitor = _user32.MonitorFromWindow(hwnd, 2)   # MONITOR_DEFAULTTONEAREST
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(monitor, ctypes.byref(mi)):
            return

        mmi = ctypes.cast(lparam, ctypes.POINTER(_MINMAXINFO)).contents
        # ptMaxPosition 是相对**显示器左上角**的偏移，不是相对工作区。
        mmi.ptMaxPosition.x = mi.rcWork.left - mi.rcMonitor.left
        mmi.ptMaxPosition.y = mi.rcWork.top - mi.rcMonitor.top
        mmi.ptMaxSize.x = mi.rcWork.right - mi.rcWork.left
        mmi.ptMaxSize.y = mi.rcWork.bottom - mi.rcWork.top
        mmi.ptMaxTrackSize.x = max(mmi.ptMaxTrackSize.x, mmi.ptMaxSize.x)
        mmi.ptMaxTrackSize.y = max(mmi.ptMaxTrackSize.y, mmi.ptMaxSize.y)

    def _hittest_pos(win, lparam: int) -> QtCore.QPoint:
        """从 ``WM_NCHITTEST`` 的 ``lParam`` 解出**屏幕**坐标。

        **为什么要用 lParam，而不是 `QCursor.pos()`。** Windows 发这条消息时
        lParam 里带的就是"正在问的那个点"；`QCursor.pos()` 回答的是另一个
        问题——鼠标**此刻**在哪。鼠标在动的时候两者不是一回事，而 Windows
        会**缓存**我们返回的命中码、拿它去处理随后的点击：于是「快速把鼠标
        移进最大化按钮然后点下去」，我们回答的是滑过去之后那个位置的码，
        Windows 缓存下来，按这一次就落空。表现正是用户说的「时灵时不灵」。

        **lParam 确实塞在 16 位里，多显示器坐标会被截断**（原来那条注释
        说的就是这件事，它是对的）。`GET_X_LPARAM` 那个有符号 16 位解法
        正是 `DefWindowProc` 自己的解法，Windows 就是这么发、这么读的。
        真正要做的是**别无条件相信它**：解出来的点如果**不落在任何一个已
        知的显示器上**，就说明这个坐标被截断了（超大分辨率多屏），这时候
        回退到 `QCursor.pos()`。

        **校验用 `QGuiApplication.screenAt()` 而不是 `win.frameGeometry()`。**
        后者只在 ``win`` 是顶层窗口时才是屏幕坐标；一旦这个 widget 被嵌进
        别的容器（测试里套个 host 就属于这种），它返回的是相对父窗口的矩形，
        于是**窗口内每个点都被误判成「被截断了」**，整个函数退化成永远走
        回退分支——而回退分支正是上面要修掉的那条路，等于白改。
        ``screenAt`` 直接回答「这个屏幕坐标落在哪块屏上」，多显示器、负
        坐标、超大分辨率全都对，也完全不关心 ``win`` 是不是顶层。
        """
        lparam = int(lparam)
        x = ctypes.c_short(lparam & 0xFFFF).value
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        gp = QtCore.QPoint(x, y)
        if QtGui.QGuiApplication.screenAt(gp) is not None:
            return gp
        return QtGui.QCursor.pos()

    def handle_native(win, tb, message) -> tuple[bool, int] | None:
        """处理我们自己关心的那几条消息。

        返回 None 表示"不归我管"，调用方要转给 ``super().nativeEvent``。
        拖动、双击最大化、拖到边缘贴边都走 HTCAPTION，由系统负责，没有自己
        实现；只有最大化按钮为了 Win11 贴边分屏返回 HTMAXBUTTON，那条路上
        Qt 收不到鼠标事件，悬停和点击都在这里补——**两个分支必须成对存在**，
        少一个就是"按钮彻底没反应"。
        """
        msg = wintypes.MSG.from_address(int(message))

        if msg.message == WM_NCHITTEST:
            # 用 lParam 里的那个点，不是 QCursor.pos()——见 _hittest_pos。
            code = hit_test(win, tb, win.mapFromGlobal(_hittest_pos(win, msg.lParam)))
            # 记下来给 WM_SETCURSOR 用。Windows 只在**相邻的**这条消息里
            # 问「该显示什么光标」，不带上位置，所以必须自己记住上次判成什么。
            win._last_ht = code
            return True, code

        if msg.message == WM_SETCURSOR:
            # **只接管缩放光标，其余一律放行。** 放行是必须的：终端里是
            # IBeam、按钮上是手型、文本框里是 I 型，全靠 Qt 自己那套
            # `setCursor` —— 一律接管就把整个界面的光标都夺走了。
            code = int(getattr(win, "_last_ht", HTCLIENT))
            cur_id = CURSOR_FOR_CODE.get(code)
            if cur_id is None:
                return None
            hcur = _user32.LoadCursorW(None, ctypes.c_void_p(cur_id))
            if hcur:
                _user32.SetCursor(hcur)
                return True, 0
            return None

        if msg.message == WM_GETMINMAXINFO:
            keep_maximize_off_taskbar(int(win.winId()), int(msg.lParam))
            return True, 0

        if msg.message == WM_NCLBUTTONDOWN:
            # **边缘缩放：自己起循环，然后吃掉这条消息。**
            # 必须吃掉 —— 不吃的话 DefWindowProc 会照着 HTLEFT 去处理，
            # 而本窗口没有 WS_THICKFRAME，它除了把这次按下丢掉之外什么也做不了，
            # 表现仍然是「拖不动」。UP 也一起吃掉，免得抬起时系统再插一脚。
            edges = RESIZE_EDGE_FOR_CODE.get(int(msg.wParam))
            if edges is not None:
                begin_edge_resize(win, edges)
                return True, 0
        elif msg.message == WM_NCLBUTTONUP:
            if int(msg.wParam) in RESIZE_EDGE_FOR_CODE:
                return True, 0

        # **双击标题栏：自己切换，不再指望 DefWindowProc。**
        #
        # 原来这里只列了 DOWN / UP，`WM_NCLBUTTONDBLCLK` 是漏的 —— 双击完全
        # 靠 `handle_native` 返回 None 之后 **Qt 把它转发给 DefWindowProc**
        # 才生效。实测（SendMessage 构造序列，_probe4）那条路**是通的**：
        # DBLCLK+HTCAPTION -> SC_MAXIMIZE，窗口确实最大化。但它有两层间接：
        # ① 得靠 QWidgetWindow 在「未处理」时转发，而那是 Qt 的内部行为；
        # ② 第一次按下会先进系统的 SC_MOVE 拖动循环（DragDetect + 模态循环），
        #    双击成不成立取决于它有没有正常退出。
        # 用户报「双击标题栏不一定生效」就落在这两层上，而**间歇性**正是
        # 「多数时候靠转发能成、偶尔那条路没走通」的样子。
        #
        # 自己处理之后这条链是：DBLCLK 到达 -> toggle_maximize() -> 返回 True
        # **绝不会再转发给 DefWindowProc**，所以也绝不会双切换（转发那条路
        # 会发 SC_MAXIMIZE，我们自己不会再发一次）。代价是失去原生双击的
        # "在最大化/还原之间自动切换"，而 `toggle_maximize()` 判的就是
        # `isMaximized()`，效果一致。
        if msg.message == WM_NCLBUTTONDBLCLK and int(msg.wParam) == HTCAPTION:
            # 弹窗不能最大化（`kinds=(CLOSE,)` 时 btn_max 恒为 None），
            # 与 `TitleBar.mouseDoubleClickEvent` 里那条判断同一个理由。
            if tb.btn_max is not None:
                tb.toggle_maximize()
            return True, 0

        if msg.message in (WM_NCLBUTTONDOWN, WM_NCLBUTTONUP) \
                and msg.wParam == HTMAXBUTTON:
            # **切换挂在「按下」上，不挂在「抬起」上。**
            #
            # 原来这里 DOWN 吞掉、UP 才 `toggle_maximize()`。那条路是不通的：
            # Windows 只有在 `WM_NCLBUTTONDOWN` 走完 `DefWindowProc` 才会进入
            # 非客户端按钮的捕获，而我们为了让系统别把整个标题栏拖走，恰恰
            # **必须**在 DOWN 就返回 0 把它吃掉。代价就是抬起那条
            # `WM_NCLBUTTONUP` 往往**根本不会送到本窗口** —— 于是
            # 「按下有反应、抬起没反应」，净效果是零，用户看到的就是
            # 「最大化按钮按了没生效」。
            #
            # 挂在 DOWN 上有两点保证：**DOWN 一定送达**（Windows 一按下就发），
            # 而**一次物理点击只有一个 DOWN**，所以不可能双切换。
            # 代价是「按下即切换」，与原生 Windows 的「抬起才切换」略有差别，
            # 以及按下后拖走再松开也会触发 —— 对一个最大化键可以接受，
            # 而「点了没反应」不能接受。
            #
            # UP 只负责清 hover，绝不再触发动作：这是「按一次 = 切换一次」的
            # 另一半保证，写清楚是为了下一个人别把切换又搬回 UP。
            if tb.btn_max is not None:
                if msg.message == WM_NCLBUTTONDOWN:
                    tb.toggle_maximize()
                else:
                    tb.btn_max.set_native_hover(False)
            return True, 0

        return None
