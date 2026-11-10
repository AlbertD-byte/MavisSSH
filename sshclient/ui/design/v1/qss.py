from __future__ import annotations

import re

from ....core.settings import UI_SIZE_REF
from .palette import Palette

#: 交互控件的统一高度（逻辑像素）。
#:
#: **为什么需要这个常量。** 改之前同一屏里有**五档**控件高度：按钮 33px
#: （`padding: 7px 14px` + 13px 字）、输入框 37px、`QSpinBox` 41px、
#: `QTabBar` 35px、弹框底部按钮 38px（`setFixedHeight(38)`）。连接框里
#: 「添加规则/删除」（33）和「取消/连接」（38）**并排**，那是肉眼第一眼
#: 就能看见的不齐 —— 比任何配色问题都扎眼。
#:
#: **用 min-height 而不是写死 height。** 高度要跟着界面字号缩放（`min-height`
#: 在 `_SCALE_PROPS` 里），而 `height` 是固定值、不缩放；写死的话
#: 18pt 时按钮还是 33px 而输入框已经涨到 60px，不齐会更严重。
#:
#: **为什么是 34。** 取「13px 正文 + 上下各 10px 内边距 + 2px 描边」量出来的
#: 值，正好落在 Windows 11 Fluent 的控件高度（32-36）和 macOS 的按钮
#: （28-32）之间偏舒适的一档；再高一档在信息密集的表单里会显得笨重
#: （连接框一屏要放 6 个输入 + 4 个按钮）。
CONTROL_H = 36

# 跟着字号一起缩放的属性。**只缩这些**：`border: 1px`（发丝线）、
# 图标 16px、指示器 16px 这些是**物理尺寸**，跟着字号放大只会让界面发虚。
#
# **长名在前**只是让人读起来不用去推 alternation 的回溯顺序（正则本身两种
# 顺序都对：`margin` 撞上 `margin-top` 的 `:` 会失败并自动试下一个分支）。
_SCALE_PROPS = (
    # 圆角：短写和四个方向写法在 CSS 里是**各自独立**的属性，短写既不带它们
    # 也不被它们覆盖。漏一个方向就是「同一个圆角概念两套行为」——`7px` 跟着
    # 字号变、旁边那个 `9px` 不变。
    "border-top-left-radius", "border-top-right-radius",
    "border-bottom-left-radius", "border-bottom-right-radius",
    "border-radius",
    # 外边距同理：`padding` 缩放而 `margin-right` 不缩放，是同一个
    # 「跟着字号走的间距」概念的两套行为。
    #
    # **四个方向都留着，哪怕当下没有一处用到。** 这张表是**机制**不是清单：
    # `tests/_fconn.py` 里有一条覆盖面判据（「表里出现过的每一个方向性
    # margin 都缩放了」），将来谁写了 `margin-top: 8px` 却忘了走这条路，
    # 它会红。把用不到的方向从表里删掉，那条判据就永远抓不到它了。
    # （`margin-top` 之前唯一的用户是 `QGroupBox`，那段已随零消费者一起删。）
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "margin",
    "font-size", "letter-spacing", "padding",
    "min-width", "min-height",
)
# **每个值要么带 `px`，要么是 CSS 允许的裸 `0`。**
# 原来只认 `\d+(\.\d+)?px`，于是 `padding: 0 6px` 这种**合法**写法整条
# 匹配不上 —— 不是「0 不缩放」，是**这一条声明完全不缩放**，连带把那个
# 该跟着字号走的 `6px` 也漏掉了。
# 交替项的顺序不能反：`0` 必须排在 `0px` 之后，否则输入 `0px` 时会先吃掉
# `0`、把 `px` 留在匹配之外。
_SCALE_RE = re.compile(
    r"\b(%s):(\s*)((?:\d+(?:\.\d+)?px|0)(?:[ ]+(?:\d+(?:\.\d+)?px|0))*)"
    % "|".join(_SCALE_PROPS))

#: px → pt 的换算系数（1pt = 96/24 px = 4/3 px，故 1px = 0.75pt）。
#:
#: **生成出来的 `font-size` 一律是 pt，本表源文件里继续写 px。**
#:
#: *为什么必须是 pt。* `font-size: 15px` 让 Qt 走 `QFont::setPixelSize()`，
#: 于是 `font().pointSize()` / `pointSizeF()` 返回 **-1**。本仓多处从
#: pointSize 派生尺寸（`sftp_panel` 的表头字号、`tab_scroll` 的步进字号），
#: 拿到 -1 只能各写各的补丁绕过去；更直接的后果是 **Qt 自己在展开
#: QComboBox 弹层时会 `setPointSize(-1)` 并打出警告** ——
#: 实测 `13px` 触发 1 条，`9.75pt` 触发 0 条。
#:
#: *为什么数字不能照抄。* 实测（96dpi、Microsoft YaHei UI）：
#:
#: | 声明 | pointSizeF() | 渲染高 | advance("中文样例") |
#: |------|--------------|--------|--------------------|
#: | `13px`  | **-1**   | 16 | 52 |
#: | `9.75pt` | **9.75** | **16** | **52** |
#: | `13pt`  | 13.0      | **21** | **68** |
#:
#: 照抄会大 31%，而 `×0.75` 之后**逐像素一致** —— 这正是「换了单位但界面
#: 一像素没动」的前提。
#:
#: *为什么不在缩放里顺手换。* `_SCALE_RE` 只认 `px`：一旦把 `pt` 写进源表，
#: 缩放会**静悄悄失效**（用户把界面字号调到 18pt，QSS 一动不动，且没有一处
#: 报错）。所以顺序必须是「先按 px 缩放，再整体换算」，见 `build_qss` 末尾。
_FONT_PX_TO_PT = 0.75

#: 只认单值 —— `font-size` 本来就只吃一个值（`padding: 0 6px` 那种列表
#: 形式是 `_SCALE_RE` 的事）。写成单值可以让「漏网」的形状很窄：
#: 多值写法匹配不上，会**原样留在表里**，被「全表 font-size 都是 pt」抓住。
_FONT_SIZE_RE = re.compile(r"\bfont-size:(\s*)(\d+(?:\.\d+)?px)\b")


def _font_size_to_pt(sheet: str) -> str:
    """把 *sheet* 里 `font-size` 的值由 px 换成 pt（数值 ×0.75）。

    **在 `_scale_metrics` 之后调用**，不是之前 —— 理由见
    :data:`_FONT_PX_TO_PT` 那段「为什么不在缩放里顺手换」。

    `pt` 只在这一步出现，所以 `_SCALE_RE` 全程只面对 px，
    「缩放会不会漏掉一个 `pt`」这件事根本不成立。

    换算结果去掉多余的零：`9.75` 不写成 `9.7500000001`，整数也不带小数点。
    """
    def repl(m: re.Match) -> str:
        pt = _FONT_PX_TO_PT * float(m.group(2)[:-2])
        # 两位小数足够：`0.75 × 整数` 只可能出现 .00/.25/.50/.75 四种小数。
        text = ("%.2f" % pt).rstrip("0").rstrip(".") or "0"
        return "font-size:%s%spt" % (m.group(1), text)

    return _FONT_SIZE_RE.sub(repl, sheet)


_CHECK_URLS: dict = {}


def _icon_url(name: str, color: str, size: int = 12, width: float = 2.0) -> str:
    """给 QSS 的 ``image:`` 画一张图标 PNG，返回可塞进 url() 的片段。

    **Qt 的 QSS 画不出这些图形。** 纯 CSS 能做的只有「方块变色」——
    勾选状态那样就仍然是**只靠颜色**传达（WCAG 1.4.1 明确点名的失败模式）；
    下拉 / 步进箭头更糟：CSS 那套「``width:0`` + 四边 border 拼三角」的写法
    **在 Qt 里不成立**，实测画出来是一个纯黑方块（下拉框那一颗从上线起就
    是这个毛病）。

    **落在临时目录，不落在配置目录。** 用户的数据目录里放的是
    settings.json / sessions.json / 密钥；往那儿塞生成物既不可读又
    只增不减，更要命的是**任何构建样式的测试或探针都会往真实配置
    目录落文件** —— 这个项目早就栽过「跑一次全量把配置目录弄脏」，
    不能再重演。`url()` 只要一个路径，临时目录完全够用。
    """
    key = (name, color, size, width)
    if key in _CHECK_URLS:
        return _CHECK_URLS[key]
    url = "none"
    try:
        import pathlib
        import tempfile

        from ... import icons
        pm = icons.icon_pixmap(name, color, size, width)
        cache = pathlib.Path(tempfile.gettempdir()) / "mavisssh-qss"
        cache.mkdir(parents=True, exist_ok=True)
        # **文件名必须带 size 和 width。** 只按「图标名 + 颜色」命名的话，
        # 两个不同尺寸 / 笔宽的同名图标会互相覆盖，而 QSS 读的是**磁盘上
        # 最后写的那张** —— 界面上的箭头和当前代码画的那张不是同一张，
        # 且没有任何症状提示你。
        path = cache / ("%s-%s-%d-%g.png" % (name, color.lstrip("#"), size, width))
        # **每次都重画，不看文件在不在。** 画法（笔宽、栅格化、`icons` 里的
        # 路径）改过一次之后，旧 PNG 会被**无声**继续用上。取舍：代价是每个
        # （图标, 颜色, 尺寸, 笔宽）组合多写一次几十字节到临时目录，而
        # `_CHECK_URLS` 已经把同一进程内的重复调用挡掉了，所以这「多一次」
        # 每个进程最多发生「组合数」次。
        pm.save(str(path), "PNG")
        # **路径必须加引号。** Windows 用户目录里有空格是常态
        # （`C:/Users/<First Last>/AppData/...`），而 QSS 的 `url()` 走的是 CSS
        # 语法：未加引号的 token 在第一个空白处就截断，于是这条 `image:` 失效
        # 而复选框**静默**退化成「只有颜色」，界面上看不出任何异常。
        # **实测要老实说**：本机 Qt 6.11.0 两种写法都画得出对勾（像素 8 vs 8，
        # 指向不存在文件是 0），所以「一定静默退化」是按语法推的、不是实测；
        # 仍然照规范加引号，因为宽容度是 Qt 版本的事，不是本仓能保证的事。
        url = 'url("%s")' % str(path).replace("\\", "/")
    except Exception:                 # 画不出来就退回「只有颜色」
        url = "none"
    _CHECK_URLS[key] = url
    return url


def _check_mark_url(color: str) -> str:
    return _icon_url("check", color, 12, 2.0)


#: 箭头 PNG 的边长与笔宽（**物理像素，不跟界面字号缩放**）。
#: 箭头是「符号」不是「排版」：跟着 6-48pt 一起放大，18pt 时一颗箭头会比
#: 整行文字还高，而 QSS 的 ``width`` / ``height`` 本来也不在
#: :data:`_SCALE_PROPS` 里，不刻意加进去正是这个原因。
_ARROW_PX = 14
_ARROW_W = 2.0


def _chevron_down_url(color: str) -> str:
    return _icon_url("chevron_down", color, _ARROW_PX, _ARROW_W)


def _chevron_up_url(color: str) -> str:
    return _icon_url("chevron_up", color, _ARROW_PX, _ARROW_W)


def scaled_control_h(ui_size: int | None = None) -> int:
    """当前界面字号下 :data:`CONTROL_H` 换算出来的**像素**高度。

    **为什么 QSS 的 ``min-height`` 不够、非得从 Python 压一次。**
    ``QSpinBox`` 里塞着一个真·``QLineEdit`` 子控件（objectName 固定
    ``qt_spinbox_lineedit``），它会吃到 inputs 那条规则的 ``min-height``；
    而**任何后代 / id / 属性选择器都匹配不到它** —— 实测
    ``QSpinBox QLineEdit``、``QSpinBox > *``、``QLineEdit#qt_spinbox_lineedit``、
    ``[objectName=...]`` 四种写法全部无效，只有设在控件自己身上的表
    （无选择器那种）才生效。所以唯一可靠的办法是从外面把整个控件定高，
    而定高必须跟着字号缩放 —— 这就是本函数存在的理由。
    """
    size = current_ui_size() if ui_size is None else ui_size
    return round(CONTROL_H * max(1, int(size)) / UI_SIZE_REF)


def compact_spinbox(sb):
    """把 ``QSpinBox`` 压到 :data:`CONTROL_H` 并原样返回，方便链式赋值。

    **为什么必须从 Python 压。** 见 :func:`scaled_control_h` 的说明：
    QSS 里没有任何一种选择器够得着它内部那个 ``QLineEdit``。
    """
    sb.setFixedHeight(scaled_control_h())
    return sb


#: 纯布局容器用的 property。配套规则在 `build_qss` 的 ``text`` 段里
def compact_combobox(box):
    """把**可编辑** ``QComboBox`` 压到 :data:`CONTROL_H` 并原样返回。

    理由与 :func:`compact_spinbox` **完全一样**：可编辑的下拉框里也塞着一个
    真·``QLineEdit``（objectName 固定 `qt_combobox_lineedit`），QSS 的
    `min-height` 量的是外面那个盒子，于是内容盒被里面那层的 `min-height`
    顶大。非可编辑的下拉框不需要这一步 —— 它没有内层输入框。
    """
    box.setFixedHeight(scaled_control_h())
    return box


#: （`QWidget[role="plain"] { background: transparent }`）。
PLAIN_ROLE = "plain"


def plain_container(widget):
    """标成「不画自己的底」，让父容器/卡片的底色透上来，返回原对象。

    **为什么本仓到处需要它。** 本文件里那条
    ``QWidget { background-color: root_bg }`` 是**兜底**规则，Qt 的类型
    选择器会匹配到**每一个**控件。于是每一个「裸 ``QWidget`` +
    ``setLayout``」的行内容器都会自己刷一遍 root_bg —— 放在
    ``role="card"``（``bg_surface``）的卡片里时，用户看到的就是
    **白卡片里嵌了一块灰条**。

    实测（连接框「端口转发」卡片内的 SOCKS 行）：沿横向扫到
    **x=77…441 全是 ``#E4E3DE``**，而卡片本身是 ``#FFFFFF``。

    **为什么不逐个 widget 写 ``setStyleSheet``。** 本仓到处是裸
    QWidget 行内容器，逐个打补丁就是「漏一个就有一块灰补丁」，而且症状
    是随机的一小块、极难反推到 QSS 的兜底规则。

    **不要反过来删掉兜底规则。** 删了之后 QSS 不再覆盖 Qt 自己那些没被
    点名的控件，原生灰就会露出来 —— 那是更大的问题。
    """
    widget.setProperty(PLAIN_ROLE, True)
    return widget


def _scale_metrics(sheet: str, scale: float) -> str:
    """把样式表里所有**跟着文字走**的像素值按 *scale* 缩放。

    **为什么不直接在 f-string 里逐处乘。** 下面那张表有 20 多处
    `font-size: Npx`，手改漏一处就是一个「这块永远不跟着字号走」的控件，
    而且症状是"看起来差不多"，测试照样绿。**统一在生成之后缩放**，新增规则
    自动被覆盖，不存在漏项。

    缩放依据是 `ui_font_size`（pt）相对默认值 `UI_SIZE_REF` 的比值。字号是
    用户在设置里选的，这张表里所有字的大小都必须由它派生 —— 否则用户在设置
    里把字号调到 18pt，SFTP 列表还是纹丝不动（`QWidget { font-size: 15px }`
    是全局基准，它一钉死下面所有控件就都不听 `ui_font_size` 的了）。
    """
    if abs(scale - 1.0) < 1e-9:
        return sheet

    def _num(tok: str) -> str:
        if tok == "0":
            return "0px"           # CSS 允许裸 0；见 _SCALE_RE 那段注释
        n = float(tok[:-2])
        # **0 必须还是 0。** 下面 `max(1, …)` 那道下限是为「1px 发丝线」
        # 设的，可它一并把「0px = 不要」抬成了「1px = 要一点点」：
        # 标签 pane 那两个 `border-top-left-radius: 0px`（故意留方角，让选中的
        # 标签盖住接缝）在 200% 字号下会悄悄变回微圆，缝里透出底色。同一条
        # 理由适用于 `padding: 0px` / `margin: 0px`——「不要」不该被缩放。
        if n == 0:
            return "0px"
        return "%dpx" % max(1, round(n * scale))

    def repl(m: re.Match) -> str:
        value = " ".join(_num(t) for t in m.group(3).split())
        return "%s:%s%s" % (m.group(1), m.group(2), value)

    return _SCALE_RE.sub(repl, sheet)


def build_qss(p: Palette, dialog: bool = False, ui_size: int = UI_SIZE_REF) -> str:
    """Build the full application stylesheet for *p*.

    ``dialog=True`` 换掉**根底色**：主窗口用 ``bg_base``，弹窗用 ``bg_dialog``。

    为什么要整张表都换，而不只是给 ``QDialog`` 加一条规则：上面那条
    ``QWidget { background-color: … }`` 是**兜底**规则，Qt 的类型选择器会匹配到
    每一个控件。于是弹窗底色一旦改了，弹窗里每一个 QLabel / QGroupBox 底下
    仍然是旧的 ``bg_base``，深色主题下就是一块块比周围更暗的补丁。
    显式配过色的那些（输入框、列表、按钮、卡片）不受影响，它们各有各的规则。

    ``ui_size`` 是用户在设置里选的界面字号（pt）。**必须传进来** ——
    漏传一个调用点，那个弹窗就永远停在默认字号，而它在界面上看不出任何异常。
    """
    # 延迟导入：下面只用到 TabCloseController.PAD_RIGHT 这一个常量，而它挂在
    # 一个 QObject 上。build_qss 本身是纯字符串生成，不该因为一个数字就把 Qt
    # 拖进导入期——这样 qss.py 才能在没有 Qt 的环境里被单独读、被单独测。
    from ...tab_close import TabCloseController
    root_bg = p.bg_dialog if dialog else p.bg_base
    # 弹窗按钮必须和弹窗底色有足够对比度。QMessageBox 内部用 QDialogButtonBox，
    # 它的按钮不是 `variant="primary"`（那是主窗口工具栏用的），走的是通用
    # QPushButton 规则——通用规则在深色主题下按钮底色与弹窗底色几乎无色差。
    # 这里在 dialog=True 时给弹窗内的 QPushButton 补一条明确的 elevated 样式。
    dialog_btn_block = f"""

/* dialog=True: 弹窗内按钮与弹窗背景必须有区分度 */
QDialog QPushButton, QMessageBox QPushButton {{
    background-color: {p.bg_elevated};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 7px;
    /* **和主规则同一个 `CONTROL_H`。** 这条原本是 `padding: 7px 14px`
       （33px），与 buttons 那条一样但各写各的 —— 改一处漏一处的典型。
       现在两处都从常量来。 */
    min-height: {CONTROL_H - 2}px;
    padding: 0 14px;
}}
QDialog QPushButton:hover, QMessageBox QPushButton:hover {{
    background-color: {p.bg_hover_ctrl};
    border-color: {p.accent_solid};
}}
QDialog QPushButton:pressed, QMessageBox QPushButton:pressed {{
    background-color: {p.accent_press};
    border-color: {p.accent_press};
    color: {p.text_on_accent};
}}
QDialog QPushButton:focus, QMessageBox QPushButton:focus {{
    border: 2px solid {p.accent_solid};
}}
QDialog QPushButton:default, QMessageBox QPushButton:default {{
    /* 默认按钮（回车触发）用 accent_solid 实心档，与普通按钮拉开视觉层次 */
    background-color: {p.accent_solid};
    color: {p.text_on_solid};
    border-color: {p.accent_solid};
    font-weight: 600;
}}""" if dialog else ""
    # **弹框浮层边界（仅浅色）。**
    #
    # 浅色 `bg_dialog #E4E3DE` 与侧栏底 `bg_rail #E6E6E3` 的色差只有 ΔE 6.16
    # （对比 1.028）—— 弹框「浮起来了」这件事看不出来，而用户当初报的正是
    # 「白色识别度不高」。
    #
    # **换色无解，扫过 5 个候选全都更差**：往亮走退回原问题（`#F7F6F3` 对
    # 页面底 ΔE 仅 7.07）；往深走会让弹框里的按钮糊掉（`#ECEBE7` 时按钮距底
    # 色只剩 ΔE 3.2）。现值已经是测试集里最优。
    #
    # 所以不动底色，改用一条描边表达边界：`border_strong` 压 `bg_dialog` 是
    # **3.12**，过 WCAG 1.4.11 的 3:1。
    #
    # **深色不加。** `bg_dialog #1e2431` 比周围亮 15 级，浮起本来就一眼可见；
    # 而且深色 `border_strong` 压 `bg_dialog` 只有 2.75，加了反而是条看得见
    # 的「框」，与「分层靠色差、不要描边线」那条原则相反。
    dialog_frame_block = f"""
/* dialog=True 且浅色：弹框浮层边界 */
QDialog {{
    border: 1px solid {p.border_strong};
}}""" if (dialog and p.name == "light") else ""
    # **QMessageBox 正文加大。** 用户报「点帮助菜单，弹框展示内容太小」——
    #
    # 实测（`_uimsgbox.py`）：改动前 `qt_msgbox_label` 与
    # `qt_msgbox_informativelabel` 的 `pixelSize` **都是 13**，与页面上任何一个
    # QLabel 完全一样。QMessageBox 内部是 Qt 自己的 QGridLayout + QLabel，而
    # **全表没有一条规则提到这两个 objectName**，于是它们只吃兜底的
    # `QWidget { font-size: 15px }` —— 「弹框里的字比界面上的小」不是错觉，
    # 它就是一个没有规则的控件。
    #
    # objectName 是 Qt 固定写死的（`qt_msgbox_label` /
    # `qt_msgbox_informativelabel`），且这两个 QLabel 是 QMessageBox 的直接
    # 子控件，**祖先链完整**，所以后代选择器能命中 —— 实测确认
    # pixelSize 13 → 17 / 15，且弹框跟着从 236x195 长到 260x207。
    # （对照记忆里那条「`QSpinBox` 内部 `QLineEdit` 够不到」：那里是
    # 祖先链断了，这里不是，**不能拿那条结论套到这里**。）
    msgbox_text_block = f"""
/* QMessageBox 正文层级 */
QMessageBox QLabel#qt_msgbox_label {{
    font-size: 17px;
    font-weight: 600;
    color: {p.text};
}}
QMessageBox QLabel#qt_msgbox_informativelabel {{
    font-size: 15px;
    color: {p.text_muted};
}}
QMessageBox QLabel#qt_msgbox_detailedLabel {{
    font-size: 15px;
}}
QLabel#msgText {{
    font-size: 17px;
    font-weight: 600;
    color: {p.text};
}}
QLabel#msgInfo {{
    font-size: 15px;
    color: {p.text_muted};
}}""" if dialog else ""
    sheet = f"""
/* ---------- base ---------- */
QWidget {{
    background-color: {root_bg};
    color: {p.text};
    font-size: 15px;
}}
QMainWindow, QDialog, QMessageBox {{
    background-color: {root_bg};
}}
QWidget:disabled {{
    color: {p.text_muted};
}}

/* ---------- menus ---------- */
QMenuBar {{
    /* 菜单栏与标题栏、状态栏一起是「chrome 带」，用 `bg_surface` 浮在内容区
       （`bg_base`）之上。**原来这里是一条 `border-bottom` 发丝线**，
       已删 —— 分层现在由色差承担。 */
    background-color: {p.bg_surface};
    color: {p.text_dim};
    border-bottom: none;
    padding: 2px 6px;
}}
QMenuBar::item {{
    background: transparent;
    padding: 6px 12px;
    border-radius: 7px;
}}
QMenuBar::item:selected {{
    background-color: {p.bg_elevated};
    color: {p.text};
}}
QMenu {{
    background-color: {p.bg_surface};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 28px 7px 14px;
    border-radius: 7px;
}}
QMenu::item:selected {{
    background-color: {p.accent_soft};
    color: {p.text};
}}
QMenu::separator {{
    height: 1px;
    background: {p.border};
    margin: 5px 8px;
}}

/* ---------- text ---------- */
/* **纯布局用的裸 QWidget 一律透明。**
 *
 * 上面那条 `QWidget {{ background-color: root_bg }}` 是**兜底**规则：Qt 的
 * 类型选择器会匹配到**每一个**控件。于是任何拿来装子控件的裸 QWidget
 * 都会自己刷一遍 root_bg —— 卡片是 `bg_surface`（白），而它把 root_bg
 * （灰）盖在上面，用户看到的就是「白卡片里嵌了一块灰条」。
 *
 * 实测（连接框「端口转发」卡片内的 SOCKS 行）：沿横向扫到
 * **x=77…441 全是 `#E4E3DE`**，而卡片本身是 `#FFFFFF`。
 *
 * **为什么不逐个 widget 打补丁。** 本仓到处是「裸 QWidget + setLayout」
 * 的行内容器（`_row()`、`_port_edit()`、`_wrap()`、host_row、key_row…），
 * 逐个 `setStyleSheet("background: transparent")` 就是「漏一个就有一块
 * 灰补丁」，而且症状是随机的一小块、极难联想到 QSS 的兜底规则。
 *
 * **两侧必须同时成立**：Python 侧 `qss.plain_container()` 打上
 * `PLAIN_ROLE`，这里的选择器再认这个值。少任何一边都静默失效。
 *
 * **也不要把兜底规则删掉。** 删了 QSS 就不再覆盖 Qt 自己那些没被点名的
 * 控件，原生灰会露出来 —— 那是更大的问题。
 */
QWidget[{PLAIN_ROLE}] {{
    background: transparent;
}}
QLabel {{
    background: transparent;
    color: {p.text};
}}
QLabel[role="dim"]    {{ color: {p.text_dim}; }}
QLabel[role="muted"]  {{ color: {p.text_muted}; font-size: 14px; }}
QLabel[role="section"] {{
    font-size: 14px;
    font-weight: 600;
    color: {p.text_muted};
    letter-spacing: 1px;
}}
QLabel[role="metric"] {{ font-size: 14px; color: {p.text_dim}; }}
QLabel[role="host"]   {{ font-size: 23px; font-weight: 700; color: {p.text}; }}

/* app chrome */
QLabel[role="brandTitle"] {{ font-size: 18px; font-weight: 700; color: {p.text}; }}
QLabel[role="stateText"]  {{ font-size: 14px; color: {p.text_dim}; }}
QLabel[role="stateText"][color="{p.success}"] {{ color: {p.success}; }}
QLabel[role="stateText"][color="{p.warning}"] {{ color: {p.warning}; }}
QLabel[role="stateText"][color="{p.danger}"]  {{ color: {p.danger}; }}

/* sidebar session rows */
QLabel[role="sessionName"] {{ font-size: 15px; font-weight: 600; color: {p.text}; }}
QLabel[role="sessionHost"] {{ font-size: 14px; color: {p.text_muted}; }}
/* sidebar group headers. 比会话名弱一档 —— 分组是结构，会话才是内容。
   底色是侧栏的 bg_rail（sessionList 自己 transparent），悬停时换 bg_hover_row。
   **这两条都实测过**，不是照着 QSS 推的：分组头虽然不可选中，但照样吃
   `:hover`（tests/_group.py 里有一条专门钉它，因为「不可选中所以大概不悬停」
   这个推断看起来很合理、而且是错的）。实测 groupName 静置 8.27/6.63、
   悬停 7.82/6.19；groupCount 静置 4.81/4.95、**悬停 4.55/4.63** ——
   浅色那一档贴着 4.5，是本组最紧的一处，改 bg_hover_row 时先看它。 */
QLabel[role="groupName"] {{ font-size: 14px; font-weight: 600; color: {p.text_dim}; }}
QLabel[role="groupCount"] {{ font-size: 13px; color: {p.text_muted}; }}

/* welcome */
QLabel[role="welcomeTitle"] {{ font-size: 32px; font-weight: 700; color: {p.text}; }}
QLabel[role="welcomeSub"]   {{ font-size: 18px; color: {p.text_muted}; }}
QLabel[role="welcomeHint"]  {{ font-size: 14px; color: {p.text_muted}; }}

/* ---------- buttons ----------
   **下面这两条必须拆开，不能继续合成一条**（2026-10-10 现代化简约）。

   带文字的 `QPushButton` 去掉了描边 —— 它有 `bg_elevated` 底色撑着，
   边界本来就看得出来，hover 时再出现 1px `border_strong`。这是当下简约
   控件的通行做法：线是**反馈**，不是**轮廓**。

   `QToolButton` 的描边**必须留**。本仓每个 `QToolButton` 都是纯图标按钮
   （`controls._icon_button` 30×30、`_fold_btn` 34×34、`TabCloseButton`
   18×18、`CaptionButton`），去掉描边它就是一排空白方块 —— 渲染对照图里
   真出现过这一幕。**图标按钮的描边是它唯一的可见内容，不是装饰。**
   同样必须留的还有 `variant="ghost"`（透明底，去描边=完全不可见）与
   输入框（白底，去描边=找不到输入区）。 */
QPushButton {{
    background-color: {p.bg_elevated};
    color: {p.text};
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 0 14px;
    font-size: 15px;
}}
/* **图标按钮：描边照旧，且高度不给 `min-height`**（见下面那段说明）。 */
QToolButton {{
    background-color: {p.bg_elevated};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 7px;
}}
/* **高度只给 `QPushButton`，不给 `QToolButton`。**
   本仓每一个 `QToolButton` 都是**定尺寸图标按钮**：`controls._icon_button`
   30×30、`main_window._fold_btn` 34×34、`tab_close.TabCloseButton` 18×18、
   `titlebar.CaptionButton` CAPTION_W×TITLEBAR_H。它们各自有一套尺寸规格，
   和表单密度无关。

   **QSS 的 `min-height` 会顶掉 `setFixedSize`。** 实测：18×18 的
   `TabCloseButton` 加上 `min-height: 32px` 之后实测 **18×34** ——
   标签上那个「X」被拉成了一条竖窄条。机制是 Qt 的样式表在 polish 时
   改写了控件的 minimumSize，而 `QToolButton` 的纵向 size policy 是 Fixed，
   布局直接采用样式表算出来的 sizeHint，**固定尺寸根本没机会生效**。
   `tests/_fwin.py` 的「关闭按钮是 18x18」当场抓到了这条。

   （同样的机制下 `setFixedHeight` 也不保险：`QPushButton` 的纵向 policy
   同样是 Fixed，所以欢迎页那个写死 38px 的 CTA 也会被 34 顶掉 ——
   它是刻意比表单高一档的 hero 按钮，用 QSS 的 `role="cta"` 单独给，
   见下方那条规则。） */
QPushButton {{
    /* **高度由 `CONTROL_H` 统一**（见 inputs 那段的同一条说明）。
       原来是 `padding: 7px 14px` 算出的 33px，跟输入框的 37px 差一档，
       而弹框底部按钮另有 `setFixedHeight(38)` —— 同一个弹框里三种高度。 */
    min-height: {CONTROL_H - 2}px;
}}
/* **欢迎页那个 hero CTA 比表单高一档**（38 vs 34）。
   它整页只有这一个可点的东西，做成和侧栏里那些小按钮一样大会把主次
   抹平；高 4px 配 150px 最小宽度，视觉重量刚好压住标题。
   **必须走 QSS，不能走 `setFixedHeight`** —— 见上面那段：样式表的
   `min-height` 会顶掉固定尺寸。 */
QPushButton[role="cta"] {{
    min-height: 40px;
}}
QPushButton:hover, QToolButton:hover {{
    background-color: {p.bg_hover_ctrl};
    border-color: {p.border_strong};
}}
QPushButton:pressed, QToolButton:pressed {{
    background-color: {p.accent_press};
    border-color: {p.accent_press};
    color: {p.text_on_accent};
}}
QPushButton:disabled, QToolButton:disabled {{
    background-color: {p.bg_surface};
    color: {p.text_muted};
    border-color: {p.border};
}}
QPushButton:focus, QToolButton:focus {{
    border-color: {p.accent_solid};
}}

QPushButton[variant="primary"] {{
    /* **实心档：底压深到白字 ≥4.5，文字用白。**
       原来这里是 `accent` + `text_on_accent`，浅色下就是「亮蓝底 + 近黑字」
       —— 对比度 5.59 够 AA，但用户报「配在一起不柔和」，所以换白字；
       而白字压 `#2F8EE5` 只有 3.43，必须先把底压到 `#1a78ce`。 */
    background-color: {p.accent_solid};
    color: {p.text_on_solid};
    border: 1px solid {p.accent_solid};
    font-weight: 600;
}}
QPushButton[variant="primary"]:hover   {{ background-color: {p.accent_solid_hover}; border-color: {p.accent_solid_hover}; }}
QPushButton[variant="primary"]:pressed {{ background-color: {p.accent_solid_press}; }}

QPushButton[variant="ghost"] {{
    background: transparent;
    border: 1px solid transparent;
    color: {p.text_dim};
}}
QPushButton[variant="ghost"]:hover {{
    background-color: {p.bg_elevated};
    color: {p.text};
}}

/* square icon-only tool buttons */
QToolButton[variant="icon"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 5px;
}}
QToolButton[variant="icon"]:hover   {{ background-color: {p.bg_elevated}; }}
QToolButton[variant="icon"]:pressed {{ background-color: {p.accent_soft}; }}
QToolButton[variant="icon"]:disabled {{ background: transparent; }}

/* sidebar action buttons */
QPushButton[variant="side"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 8px 10px;
    text-align: left;
    color: {p.text_dim};
}}
QPushButton[variant="side"]:hover {{
    background-color: {p.bg_elevated};
    color: {p.text};
}}

/* ---------- 键盘焦点 ----------
   **这一段必须排在所有 variant 之后。** 上面那条
   `QPushButton:focus, QToolButton:focus {{ border-color }}` 单靠自己是不够的：
   各 variant 规则用的是 `border:` **简写**，简写会把 `border-color` 一起
   重置；而 `QPushButton[variant="primary"]` 和 `QPushButton:focus` 特异度
   **相同**（属性选择器 / 伪类都是 0-1-1），于是**位置更靠后的 variant 赢**。
   结果是 primary / ghost / side / icon 聚焦时一个像素都不变 ——
   实测 11 个可聚焦控件里只有普通按钮、输入框、下拉框有可见焦点环。

   **焦点环只改 `border-color`，绝不改 `border-width`。**
   原来这里是 `border: 2px solid accent_solid`（简写写法），看着只是"更醒目"，
   实际把每个可聚焦按钮变成了一张会呼吸的纸：``QPushButton`` 的纵向 size
   policy 是 **Fixed**，布局直接采用 sizeHint，于是 border 从 1px 变 2px
   就是一次真实的 **2px 高度跳变** —— 同排按钮随焦点来回长高/缩回。
   实测隧道面板三连按钮：聚焦「添加转发」= [34,32,32]、聚焦「添加 SOCKS」
   = [32,34,32]、聚焦「移除」= [32,32,34]；五种 variant 里有四种会跳。

   **为什么不用 padding 补偿**（border 加宽、padding 减 1px，总高不变）：
   按钮 padding 在本表里有四种（`7px 14px` / `5px` / `8px 10px` / `9px 10px`），
   通用焦点规则一刀切地减 1px 会把 `variant="side"`、`variant="ghost"`
   反过来弄矮 2px —— 把一个 bug 换成另一个 bug，而且更难看出。
   只改颜色则几何**天然恒定**，没有任何需要跟人脑同步的算式。

   **焦点环用 `accent_solid` 而不是 `accent`。** 浅色下 accent 在各底色上只有
   2.50~3.43 —— 过了非文本 3:1，但满饱和、偏艳，和选中态撞脸；压到
   `#1a78ce` 之后是 3.31~4.54，沉得住。深色下压深会低一点（5.99 → 4.23，
   六个底色最低 3.43），仍全部达标。 */
QPushButton:focus, QToolButton:focus,
QPushButton[variant="ghost"]:focus, QPushButton[variant="side"]:focus,
QToolButton[variant="icon"]:focus {{
    border-color: {p.accent_solid};
}}
/* primary 的填充本身就是 `accent_solid`，同色描边等于同色压同色，糊在里面
   看不见（实测 2px 也一样）。所以这一支的焦点反馈走**底色**：
   `accent_solid_press` 与常态 `accent_solid` 色距 26.6(浅)/29.7(深)，
   肉眼可辨，而白字压它仍有 5.49/5.28（都 ≥4.5）。
   `accent` 色距更大（38~61）但白字只有 3.20/3.43，`accent_press` 浅色下
   只有 2.85 —— 两个都不能用，白字不可读。

   **原来这里是 `border: 2px solid text_on_solid`（纯白整圈）。**
   暗主题下白框压在深蓝实心档上像贴了张白边（用户截图逐像素可查：
   白框 `y=53..90` 高 38，另外两个按钮 `y=54..89` 高 36）；
   而 border 1px→2px 那一下，正是上面说的 2px 跳变。
   改成只改底色后，三个按钮在任意聚焦组合下都是 32，且再无白框。 */
QPushButton[variant="primary"]:focus {{
    border-color: {p.accent_solid};
    background-color: {p.accent_solid_press};
}}
/* 列表 / 树 / 表格的底规则带 `outline: none`，聚焦时没有任何回退指示，
   一片空白。改成 `accent_solid` 描边（保持 1px，不让选中行随 Tab 抖动）。 */
QListWidget:focus, QTreeView:focus, QTreeWidget:focus,
QTableWidget:focus, QTableView:focus, QTabWidget::tab-bar:focus {{
    border: 1px solid {p.accent_solid};
}}

/* ---------- inputs ---------- */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background-color: {p.bg_input};
    color: {p.text};
    border: 1px solid {p.border};
    border-radius: 7px;
    /* **高度由 `CONTROL_H` 统一，原来的 `padding: 8px 11px` 算出 37px，
       按钮那边是 33px** —— 输入框和按钮并排时差 4px，一眼能看出来。
       改成 min-height 之后两者都落到 :data:`CONTROL_H`，且跟着字号缩放。 */
    min-height: {CONTROL_H - 2}px;
    padding: 0 11px;
    /* **选中态跟着主按钮一起换成实心档 + 白字。**
       原来这里是 `accent` + `text_on_accent`，浅色下就是「亮蓝底 + 近黑字」——
       和用户点名要改的「新建连接」是同一种视觉毛病。对比度 5.59 → 4.54，
       两边都过 AA，换的是观感。 */
    selection-background-color: {p.accent_solid};
    selection-color: {p.text_on_solid};
}}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover {{
    border-color: {p.border_strong};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {p.accent_solid};
    background-color: {p.bg_input};
}}
QLineEdit::placeholder {{
    color: {p.text_muted};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background-color: {p.bg_surface};
    color: {p.text_muted};
}}
/* **箭头换成真图，浮雕去掉。** 原来这里有两种画法，两种都不对：
   1. 不写规则 → 用 Windows 原生那套立体箭头，三角是对的，但自带
      底边 / 右边两道 3D 浮雕线，和本仓「靠色差分层、不描边」的规矩
      直接打架；
   2. CSS 那套「``width:0;height:0`` + 四边 border 拼三角」→ **Qt 不认**，
      实测画出来是一个**纯黑方块**（放大 4 倍截图确认）。
   两条路都堵死之后只剩给图，而 :func:`_icon_url` 已经在给复选框画对勾了，
   同一套机制直接复用。 */
QSpinBox::up-button, QSpinBox::down-button {{
    background: transparent;
    border: none;
    width: 20px;
    subcontrol-origin: border;
}}
QSpinBox::up-button   {{ subcontrol-position: top right; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; }}
QSpinBox::up-arrow {{
    image: {_chevron_up_url(p.text_dim)};
    width: {_ARROW_PX}px;
    height: {_ARROW_PX}px;
}}
QSpinBox::down-arrow {{
    image: {_chevron_down_url(p.text_dim)};
    width: {_ARROW_PX}px;
    height: {_ARROW_PX}px;
}}
QSpinBox::up-arrow:disabled, QSpinBox::down-arrow:disabled {{
    image: {_chevron_up_url(p.text_muted)};
}}
QSpinBox::down-arrow:disabled {{
    image: {_chevron_down_url(p.text_muted)};
}}
QComboBox::drop-down {{
    border: none;
    width: 24px;
}}
/* **可编辑下拉框里那层输入框必须剥掉自己的框。**
   上面 inputs 那条是按**类型**匹配的，够不到父级 —— 所以可编辑
   `QComboBox` 内部的 `QLineEdit` 照样吃到 1px 描边和 7px 圆角，
   于是一个下拉框画出了**两层框**，还比旁边的输入框高一截。
   底下这四条把内层的底色、描边、圆角、留白和最小高度全部清掉，
   外面下拉框那层就成了唯一的框，高度也回到 CONTROL_H。

   **注释里一个字都不能出现「属性名 + 冒号 + 值」。**
   两个原因叠在一起：这张表是 f-string（所以花括号也不能出现），
   而缩放器是对整张表的原文跑正则的，它**不跳过注释**，并且那条正则的
   字符类是「除分号和花括号以外的一切」，**能跨行** —— 于是注释里随手写的
   示例会被一起缩放（1px solid 变成 2px、0 变成 0px），
   `tests/_fconn.py` 的「只有白名单里的属性变了」就会报出一条根本不存在的
   属性变化。所以上面刻意写成「1px 描边和 7px 圆角」这种说法。
   另外下面这四条一律写 `0px` 不写裸 `0` —— `_num` 会把裸 0 变成 `0px`，
   而 11pt 那一档 `scale == 1` 是**原样返回**的，于是同一个「不要」
   在 11pt 的表里叫 `0`、在 18pt 的表里叫 `0px`，比对的人会以为它变了。 */
QComboBox QLineEdit {{
    background: transparent;
    border: none;
    border-radius: 0px;
    padding: 0px;
    min-height: 0px;
}}
/* **同一个毛病**：原来也是那套 border 拼三角，实测是黑方块。
   这颗箭头从上线起就没画对过 —— 不是这一轮改坏的。 */
QComboBox::down-arrow {{
    image: {_chevron_down_url(p.text_dim)};
    width: {_ARROW_PX}px;
    height: {_ARROW_PX}px;
    margin-right: 8px;
}}
QComboBox::down-arrow:disabled {{
    image: {_chevron_down_url(p.text_muted)};
}}
QComboBox QAbstractItemView {{
    background-color: {p.bg_surface};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: {p.accent_soft};
}}

/* ---------- lists / views / tables ---------- */
QListWidget, QTreeView, QTableWidget, QTableView {{
    background-color: {p.bg_surface};
    alternate-background-color: {p.bg_surface};
    /* **去描边**（2026-10-10）：列表整块铺 `bg_surface`，底色差已够分层，
       与卡片同理。`outline: none` 那行本来就压掉了焦点虚线框。 */
    border: 1px solid transparent;
    border-radius: 10px;
    outline: none;
    gridline-color: transparent;
    color: {p.text};
}}
QListWidget::item {{
    border-radius: 8px;
    margin: 2px 6px;
    padding: 9px 10px;
    color: {p.text};
}}
QListWidget::item:hover {{
    background-color: {p.bg_hover_row};
}}
QListWidget::item:selected {{
    background-color: {p.accent_soft};
    color: {p.text};
}}
QListWidget#sessionList {{
    background-color: transparent;
    border: none;
}}
/* 上面这条 `border: none` 是 **ID 选择器**，特异度比
   `QListWidget:focus` 高，位置再靠后也压不过它 —— 所以会话列表必须
   单独再写一条，否则键盘 Tab 进去焦点框是一片空白。 */
/* sessionList:focus border 已删：原来键盘聚焦时会在侧栏内缘画一条 1px 蓝线
   （accent_solid），与侧栏右边界重叠，用户看到多余的蓝色竖线。
   `border: none` 兜底规则已在 sessionList{{}} 那条里写了，这里不需要再重复声明。 */
/* Session rows are setItemWidget widgets that carry their own contents
   margins (10/8/10/8). The generic `::item` padding and the vertical part of
   its margin would both stack on top of that: 9 + 2 (margin) + 8 = 19px of
   inset at each end of a row pinned to setFixedHeight(58), so every row
   overflowed its own item and the second text line was sliced by the
   selection highlight. Hand both the padding and the vertical margin back to
   the row -- only the horizontal inset is kept, so the highlight still has
   its 6px gutter. The gap between rows now comes from QListWidget.setSpacing
   alone, which can grow the gap without ever making a row overflow. */
QListWidget#sessionList::item {{
    padding: 0px;
    margin: 0px 6px;
}}
/* The 56px rail cannot afford a 6px gutter on each side: 55px of viewport
   minus 12px of margin leaves 43px... minus the 4px the list itself insets
   is 35px, and the 40px compact badge overflows that by 5. Tightening the
   gutter in the rail state is what lets the badge keep its size. */
QListWidget#sessionList[rail="1"]::item {{
    margin: 0px 2px;
}}
/* The rail's item box is 4px wider than the 40px badge the row puts in it,
   so an item background shows through as two vertical bars hugging the
   badge's left and right edges -- and nothing else, because vertically the
   row and the item are both exactly 40px. Those bars are the user-visible
   "edges" flagged next to the SSH column: `accent_soft` bleeding out from
   behind an opaque badge, so it read as a stripe that belonged to nothing.

   `:selected` and `:hover` are cleared together on purpose. They are the
   same 2px gutter showing the same fill, so clearing only the selected one
   would swap the stripe to the other branch rather than remove it. What
   replaces them is on the badge itself -- `CompactSessionItem.set_selected`
   steps it to `accent_soft`, the same fill the expanded row's badge uses.
   The list's own background is already `transparent`, so with no item fill
   the gutter shows the sidebar's own `bg_rail` and disappears into it. */
QListWidget#sessionList[rail="1"]::item:selected,
QListWidget#sessionList[rail="1"]::item:hover {{
    background-color: transparent;
}}

QTreeView::item, QTableWidget::item {{
    padding: 6px 8px;
    border: none;
    /* No `color` here: a rule that sets it would override per-item foreground
       brushes, which the monitor uses to flag hot disks. */
}}
QTreeView::item:hover, QTableWidget::item:hover {{
    background-color: {p.bg_hover_row};
}}
QTreeView::item:selected, QTableWidget::item:selected {{
    background-color: {p.accent_soft};
    color: {p.text};
}}
QTreeView::branch {{ background: transparent; }}

QHeaderView::section {{
    background-color: transparent;
    color: {p.text_muted};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 8px;
    font-size: 14px;
    font-weight: 600;
    letter-spacing: 0.5px;
}}
QHeaderView {{ background-color: transparent; }}
QTableCornerButton::section {{ background: transparent; border: none; }}

/* ---------- tabs ----------
   Tabs sit *on* the pane: only the top corners are rounded and the selected
   tab paints a bottom border in the pane's own colour so the seam between
   them is covered. Rounding all four corners leaves the pane background
   visible through the bottom corners, which reads as a rough edge.        */
QTabWidget::pane {{
    background-color: {p.bg_surface};
    /* **去描边**（2026-10-10）：与卡片、列表同理，页签容器整块铺
       `bg_surface`，靠底色差分层。`top: -1px` 那行留着 —— 它是给页签
       「压住」pane 上沿用的，与描边是两回事。 */
    border: 1px solid transparent;
    border-radius: 10px;
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
    top: -1px;
}}
QTabBar {{ qproperty-drawBase: 0; background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {p.text_dim};
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 0px;
    /* 页签和输入框/按钮对齐到同一条基线。原来 `padding: 8px 18px` 算出
       35px，比输入框矮 2px、比按钮高 2px，正好卡在中间谁都不齐。
       `- 2` 是**描边那两条边** —— QSS 的 `min-height` 量内容盒，
       上面每条规则都减 2，页签也不例外（这里原来漏减，实测 36px）。 */
    min-height: {CONTROL_H - 2}px;
    padding: 0 18px;
    margin-right: 6px;
    font-size: 15px;
}}
QTabBar::tab:hover {{
    background-color: {p.bg_elevated};
    color: {p.text};
}}
QTabBar::tab:selected {{
    background-color: {p.bg_surface};
    color: {p.text};
    border-color: {p.border};
    border-bottom: 1px solid {p.bg_surface};
    font-weight: 600;
}}
/* Spacing only, for tab bars that still use the *native* close button. Do NOT
   add box properties (border-radius, background-color) here: Qt stops drawing
   the native glyph as soon as the subcontrol is styled, and the tab loses its
   close button entirely. Session tabs carry a real tab_close.TabCloseButton
   instead, positioned by tab_close.TabCloseController -- these margins have no
   effect on such a widget. */
QTabBar::close-button {{
    subcontrol-position: right;
    margin-left: 8px;
    margin-right: 8px;
}}

/* ---------- session tabs ---------- */
QTabWidget#sessionTabs {{
    background: transparent;
}}
QTabWidget#sessionTabs::pane {{
    background-color: {p.bg_base};
    border: 1px solid {p.border};
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
    top: -1px;
}}
QTabWidget#sessionTabs > QTabBar {{ background: transparent; }}
/* padding-right reserves the strip the close widget is painted into, so Qt
   elides the label short of it. It does NOT move the widget -- TabClose-
   Controller places that itself, keyed off the same constant. */
QTabWidget#sessionTabs QTabBar::tab {{
    background: transparent;
    color: {p.text_muted};
    border: 1px solid transparent;
    border-bottom: none;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    border-bottom-left-radius: 0px;
    border-bottom-right-radius: 0px;
    /* **高度改走 `min-height`，不再靠上下 padding 撑。**
       原来这里是 `padding: 8px 34px 8px 18px`（右值是
       `TabCloseController.PAD_RIGHT`，写在注释里以免本 f-string 再吃一层
       花括号）—— 上下各 8px，而这条规则**没有 `min-height`**，
       于是高度完全由 padding 决定：实测 34（内容盒）+ 16（上下 padding）
       + 2（描边）= **50px**。
       同一个界面里子 tab（终端/文件/监控/隧道）是 **34px**，
       两类 tab 差 16px，就是「tab 有点高」的全部来源 —— 而
       `QPushButton` / `QLineEdit` / `QComboBox` 也都是 34。
       50 那一条不但比同类高一截，还比所有控件高一截。
       现在与上面 `QTabBar::tab` 用**同一个** `CONTROL_H - 2`（同样减掉
       描边那两条边），两类 tab 因此落在同一条基线上。 */
    min-height: {CONTROL_H - 2}px;
    /* padding 只留横向：左边给图标前的留白，右边给关闭按钮那条带子
       （`TabCloseController.PAD_RIGHT`）。纵向必须写 0 —— QSS 的
       `min-height` 量的是内容盒，纵向 padding 会在它**之外**再加，
       那正是刚才 50px 的成因。 */
    padding: 0 {TabCloseController.PAD_RIGHT}px 0 18px;
    margin-right: 6px;
}}
QTabWidget#sessionTabs QTabBar::tab:hover {{
    background-color: {p.bg_elevated};
    color: {p.text_dim};
}}
QTabWidget#sessionTabs QTabBar::tab:selected {{
    background-color: {p.bg_base};
    color: {p.text};
    border-color: {p.border};
    border-bottom: 1px solid {p.bg_base};
    font-weight: 600;
}}
/* No close-button rule for the session tabs: a widget installed with
   setTabButton ignores these margins, and adding a box property here would
   suppress the native glyph on every other tab bar as a side effect. */

/* ---------- cards ---------- */
/* **卡片不再画描边**（2026-10-10 现代化简约）。
   现代化简约的层次靠**色差**，不靠线。而原来这版页面底 `#FAFAF8` 与
   卡片白只差 ΔE 9.9 —— 这么浅的差值撑不起分层，所以必须画 1px 描边。
   **那圈线不是多余，它是在替色差干活。** 所以顺序是「先拉开色差，再去线」：
   页面底压到 `#F6F5F2` 后 ΔE 变 **18.7**，卡片靠底色自己就浮起来了。
   深色那侧同理（`bg_surface` 与 `bg_base` 的 ΔE 15.3 → 17.3）。
   圆角 12px 保留 —— 现代化简约减的是线，不是曲率。 */
QFrame[role="card"] {{
    background-color: {p.bg_surface};
    border: 1px solid transparent;
    border-radius: 12px;
}}
/* 设置页的强调色预览色板。**度量必须待在这张表里**，因为只有这里的
   声明才会经过 `_scale_metrics` 跟着 `ui_font_size` 缩放；写在
   `settings_dialog` 的 `setStyleSheet` 内联串里就完全不缩放（内联表
   既不经缩放、优先级又压过本表）。这四项都列在 `_SCALE_PROPS` 里。
   颜色不在这里 —— accent 是用户选的、运行期才知道，只能由那边内联。 */
QPushButton[role="accentSwatch"] {{
    border-radius: 7px;
    /* **padding 必须写 0。** QSS 的 `min-height` 量的是**内容盒**，
       上下 padding 和描边是**额外加上去的** —— 原来 `min-height: 32px`
       配 `padding: 4px 10px`，注释里写「比按钮矮一档」，实测却是
       30 + 8 + 2 = **40px**，比同行的 QLineEdit 和「默认」按钮（34px）
       还高 6px。**声明和结果差 10px，而结果才是用户看见的。**
       横向 padding 不影响高度，保留 10px 只为色块文字不贴边。 */
    padding: 0 10px;
    font-size: 14px;
    /* 和普通控件同高：它和 hex 输入框、「默认」按钮是同一行，
       不同高就是一眼可见的参差。 */
    min-height: {CONTROL_H - 2}px;
}}
QFrame[role="sidebar"] {{
    /* 侧栏是「负空间」，靠 `bg_rail` 与内容区的色差站住。
       **原来这里还有一条 `border-right` 发丝线，已删** —— 深色下 `bg_rail`
       曾与 `bg_surface` 逐字节同值，侧栏是**完全靠那条线**存在的（实测像素：
       x=1..266 全 rail、x=267 是线、x=268 起 base）。现在 `bg_rail` 有了
       自己的第三档（深色 #151824，对 bg_base 1.0844），线是重复。 */
    background-color: {p.bg_rail};
    border: none;
}}

/* ---------- transfer strip ---------- */
/* 贴在 SFTP 面板底部的传输状态卡。左侧一道 3px 的色条按状态换色，
   靠 [tone] 区分 running / success / error —— 不用三套 QSS，只改属性。 */
QFrame[role="transferStrip"] {{
    background-color: {p.bg_surface};
    /* **四边的 1px 去掉，左侧那 3px 色条留着** —— 它是状态的唯一载体
       （running / success / error / cancelled），不是轮廓，是内容。 */
    border: 1px solid transparent;
    border-left: 3px solid {p.border};
    border-radius: 12px;
}}
QFrame[role="transferStrip"][tone="running"] {{ border-left-color: {p.accent}; }}
QFrame[role="transferStrip"][tone="success"] {{ border-left-color: {p.success}; }}
QFrame[role="transferStrip"][tone="error"]   {{ border-left-color: {p.danger}; }}
/* 取消不是错误：灰色,别用红色吓人 */
QFrame[role="transferStrip"][tone="cancelled"] {{ border-left-color: {p.text_muted}; }}

QLabel[role="transferTitle"] {{
    font-size: 15px;
    font-weight: 600;
    color: {p.text};
}}
QLabel[role="transferDetail"] {{
    font-size: 14px;
    color: {p.text_muted};
}}
QFrame[role="transferStrip"][tone="error"] QLabel[role="transferTitle"] {{
    color: {p.danger};
}}

/* **原 `QGroupBox` 整段已删。** `sshclient/` 里**零实例**（grep 确认）——
   monitor 面板用的是 `QFrame[role="card"]`。留着它就是一条永远不画任何
   像素的规则，而它偏偏带 `border: 1px solid {p.border}`，让「这个主题
   到底有几条描边规则」这个问题答不出来。 */

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {p.border_strong};
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_muted}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {p.border_strong};
    border-radius: 4px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.text_muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- status bar ---------- */
QStatusBar {{
    /* 同 QMenuBar：chrome 带用 `bg_surface`，`border-top` 发丝线已删，
       分层交给色差。 */
    background-color: {p.bg_surface};
    color: {p.text_muted};
    border-top: none;
    font-size: 14px;
}}
QStatusBar::item {{ border: none; }}

/* **原 `QProgressBar` 整段已删。** `sshclient/` 里零实例 ——
   传输进度条是 `transfer_strip.py` 自绘的，QSS 给它上不了色。 */

/* ---------- tooltip ---------- */
QToolTip {{
    background-color: {p.bg_elevated};
    color: {p.text};
    border: 1px solid {p.border_strong};
    border-radius: 7px;
    padding: 6px 9px;
}}

/* **原 `QSplitter` 整段已删。** `sshclient/` 里零实例。留着它更糟的是：
   删掉这段之后 Qt 会把分栏条**掉回原生绘制**（Windows 那种立体凹槽），
   也就是「从一种样子变成另一种样子」而不是「变得不明显」。零实例时两者
   都看不见，但留着规则会让人以为这条路径是有保障的。 */

/* ---------- checkbox / radio ---------- */
QCheckBox, QRadioButton {{ spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.border_strong};
    background: {p.bg_input};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    /* **实心档 + 白勾**，理由同上面那条笔触的注释：这一组原来是
       `accent` + `text_on_accent`（浅色下就是「亮蓝方块 + 近黑勾」），
       和用户点名要改的主按钮是同一种视觉毛病，所以一起换。 */
    background-color: {p.accent_solid};
    border-color: {p.accent_solid};
}}
/* 复选框要**真的有勾**。`::indicator:checked` 原来只有「方块变实心」，
   那是纯颜色变化 —— WCAG 1.4.1 明确点名的失败模式。Qt 的 QSS 画不出对勾，
   只能给它一张图（见 `_check_mark_url`）。
   单选框**不动**：它的空心/实心本身就是通用做法，不是「只靠颜色」。 */
QCheckBox::indicator:checked {{
    /* 勾的笔触跟着底色一起换成实心档 + 白字，和主按钮同一套。
       **对比度是降的**：accent(#2F8EE5) + 近黑字是 5.59，accent_solid
       (#1a78ce) + 白字是 4.54 —— 都过 AA，换的是观感不是可读性。
       16px 方块里一根白勾比一根近黑勾干净得多。 */
    image: {_check_mark_url(p.text_on_solid)};
}}
/* ---------- 复选框 / 单选框的键盘焦点 ----------
   **同样必须排在所有 `:checked` 之后**，理由与上面按钮那一段一字不差：
   `::indicator:focus` 与 `::indicator:checked` **特异度相同**（都是 0-1-2），
   谁在后面谁赢。今天 `:checked` 只写 `border-color`、focus 那 2px 宽度还留着，
   所以看不出破坏；**只要有人给 `:checked` 换个颜色，焦点环就被静默吃掉**，
   而复选框恰好是最需要焦点可见的地方（勾一下就走，键盘用户靠那一圈确认
   焦点还在哪个上）。按钮那侧早就用「整段后置」修过同一个坑，这里跟上，
   不改特异度写法（改成 `!important` 会与按钮那侧的既有解法不一致）。 */
QCheckBox:focus, QRadioButton:focus {{
    color: {p.text};
}}
QCheckBox::indicator:focus, QRadioButton::indicator:focus {{
    border: 2px solid {p.accent_solid};
}}{msgbox_text_block}{dialog_frame_block}{dialog_btn_block}"""

    # **先按 px 缩放，再把 font-size 换成 pt。** 顺序反了 `_SCALE_RE` 就再也
    # 匹配不到 `font-size`，界面字号会静悄悄失效 —— 理由见 `_FONT_PX_TO_PT`。
    return _font_size_to_pt(
        _scale_metrics(sheet, max(1, int(ui_size)) / UI_SIZE_REF))


def apply(widget, palette: Palette, dialog: bool = False,
          ui_size: int = UI_SIZE_REF):
    """Apply the theme stylesheet to *widget* and all its descendants.

    Qt only cascades a stylesheet set on a QMainWindow down its own object
    tree -- a QDialog that merely *has* the window as its parent renders
    unthemed. Every top-level window must therefore be styled explicitly.

    It lives next to build_qss, not next to the palette tokens: it needs the
    generated stylesheet, and putting it in `core` would have made the core
    layer depend on the UI layer.

    ``dialog=True`` 让它换用弹窗那套根底色，见 :func:`build_qss`。
    """
    widget.setStyleSheet(build_qss(palette, dialog=dialog, ui_size=ui_size))
    return widget


_CURRENT: dict = {"palette": None, "ui_size": UI_SIZE_REF}


def set_current(palette: Palette, ui_size: int = UI_SIZE_REF) -> None:
    """记下"当前生效的配色和界面字号"。

    弹窗（QMessageBox / QFileDialog / QInputDialog）是在各自的调用点临时
    造出来的，没有构造参数能塞进 palette。逐个给调用点传参数既啰嗦又
    必然会漏——漏掉的那个就是一条又黑又白的原生标题栏。所以由
    ``MainWindow.apply_theme`` 在这里记一份，弹窗自己去取。

    字号和配色**一起记**，是同一个道理：记了配色不记字号，弹窗就会
    「颜色跟着主题走、字号停在默认值」—— 而字号不一致比配色不一致更刺眼，
    因为满屏都是字。
    """
    _CURRENT["palette"] = palette
    _CURRENT["ui_size"] = max(1, int(ui_size))


def current() -> Palette:
    """当前配色；还没设过（比如崩溃兜底框）就回退到深色。"""
    if _CURRENT["palette"] is None:
        from .palette import DARK
        return DARK
    return _CURRENT["palette"]


def current_ui_size() -> int:
    """当前界面字号（pt）；还没设过就回退到比例尺原点。"""
    return _CURRENT["ui_size"]
