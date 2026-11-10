"""配色：深浅两套 token、ANSI/xterm256 调色板、颜色工具。

这里是整套设计系统的地基：所有颜色先成为这里的具名 token，别的模块
不再各自写死一个色值。改一处主题色因此只改这一个文件。

三个 token 的取值是被实测卡住的，别看着"太重/太亮"随手调回去：

- `text_muted` 是**正文级小字**（侧栏主机地址、分组标题、占位符），按
  WCAG AA 4.5:1 定。深色之所以不是更暗的那个 `#77818f`（对 `bg_base` 4.86，
  但对弹窗底只有 3.94），是因为 `bg_dialog` 比 `bg_base` **更亮** —— 弹窗里
  的分组标题和占位符就落在那一档上，抬到 `#828b98` 才四个底色全部 ≥4.5。
  代价是 `text_dim → text_muted` 的层次从 1.53 收到 1.34，仍然分得开。
  注意 `accent_soft`（选中行）是第五个底色：它上面的 `text_muted` 实测
  **4.89 (light) / 4.48 (dark)** —— 浅色刚好过 AA，深色差一点。所以选中期间
  由 `SessionItem.set_selected()` 把主机地址换成 `text_dim`（8.40/6.68），
  而不是去调 `accent_soft` —— 调 `accent_soft` 会连带牵动菜单选中、
  icon 按下、文本选区。
  （这几个数字动过：早先写的是 4.29/4.33 与 4.86/5.79，是配色后来调浅
  `accent_soft` 之后重新解出来的。现在的值以实测为准。）
- `border` 用在**每一张**卡片/按钮/输入框上，是界面里出现次数最多的色，
  所以刻意停在 2.4~2.6（肉眼明显可见）而**不**推到 3.0 —— 全描一遍
  界面会发闷。要 3.0 的是 `border_strong`，它只在悬停/焦点态出现，
  少而显眼，3.2~3.4 正好。
- 文字三级层次（`text` / `text_dim` / `text_muted`）是**排出来的**，不是
  等距的。深色 `text_dim` 已经 7.4，往 7:1(AAA) 走会把三级压成一级。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # **只为静态分析存在，运行期不导入。** `qcolor()` 里那个
    # `from PyQt6 import QtGui` 是函数体内的延迟导入，为的是让本模块在
    # 没有 Qt 的环境里也能被单独测。代价是模块级没有 `QtGui` 这个名字，
    # 于是 `qcolor` 的返回注解 `QtGui.QColor` 对静态分析器是悬空的
    # （ruff F821 报的就是这个），而 `from __future__ import annotations`
    # 让普通调用不求值它，所以跑得起来。
    #
    # **注意它救不了 `typing.get_type_hints()`**：那玩意按运行期的
    # `__globals__` 求值，而 `TYPE_CHECKING` 分支运行期是 False，
    # 实测照样 `NameError: name 'QtGui' is not defined`。要让注解在运行期
    # 真的可解析就得在模块级真导入 Qt，那正好毁掉「无 Qt 可测」这个性质
    # —— 本仓没有任何地方对 qcolor 调 get_type_hints，所以这里停在这条线上：
    # 静态可解析、运行期不求值、模块仍可脱离 Qt 单独 import。
    from PyQt6 import QtGui


@dataclass(frozen=True)
class Palette:
    """Color tokens for one theme variant."""

    name: str

    # Surfaces
    #
    # **状态色必须声明它压在哪一层，跨层就是两个 token。** 下面两个悬停色就是
    # 这么拆的：按钮坐在 `bg_elevated` 上，行坐在 `bg_surface`/`bg_base` 上，
    # 两边对「要离基色多远」的要求是反的 —— 一个要离得远才看得出悬停，另一个
    # 离太远就压过了 `accent_soft` 选中行。用一个 token 装两件事时，实测
    # 浅色会出现「按钮悬停 == 按钮静置」（对比度 1.000，悬停零反馈）。
    bg_base: str          # window background
    bg_dialog: str        # dialog / message-box background
    bg_surface: str       # panels / cards / list backgrounds
    bg_rail: str          # the sidebar rail only — **recessed**, i.e. darker than
                          # bg_base in the light theme and lighter in the dark one.
                          # 单独一个字段而不是复用 `bg_surface`：参考图里侧栏
                          # `#E6E6E3` 比主区 `#FAFAF8` **更暗**，而 `bg_surface`
                          # 同时还撑着 QListWidget / QTreeView / QTableWidget 的
                          # 背景、下拉弹层和传输条卡片 —— 把它整体压到窗口底之下，
                          # 那些**内容区**就会变成「凹进去的洞」而不是侧栏那种
                          # 「退到后面去」（票 1 实测到的视觉反转风险）。
                          # 只有侧栏该是负空间。
    bg_elevated: str      # raised grey: button rest fill; hover wash for flat
                          # controls (ghost/icon/side/tab); progress groove;
                          # tooltip; sidebar badge; chart track; titlebar hover
    bg_input: str         # text fields (and the checkbox/radio indicator)
    bg_hover_ctrl: str    # hover wash on a control that sits on bg_elevated
    bg_hover_row: str     # hover wash on a list/tree/table row
    bg_active: str        # titlebar caption pressed — NOT the selected row
                          # (that is accent_soft); mouse presses never reach it
                          # because the hover branch sits above isDown()
    # **这里原来还有 `bg_scrim`，零消费者。** 全仓（`palette.py` 之外）一个
    # 引用都没有：弹窗是**独立顶层窗口**（`msgbox` 那几个类都自带标题栏），
    # 本仓从来没有在窗口上盖过一层半透明色，所以「遮罩」这件事还没有发生。
    # 留着这个字段等于凭空多一个「加遮罩时记得改它」的责任，而它唯一的
    # 引用还是**错的**：8 位十六进制按 Qt 的 #AARRGGBB 解析，写的人以为是
    # CSS 的 #RRGGBBAA，谁用谁得到一层隐形遮罩且不报错（`_accent.py` 那两条
    # alpha 判据量到的就是这个歧义，字段删掉时一并撤了）。

    # Lines
    border: str
    border_strong: str
    # **这里原来还有 `border_focus`，零消费者。** `qss.py` 里 8 条 focus 规则中
    # 7 条的焦点环画的是 `accent`（按钮/输入框/列表/树/表格/指示器），剩下那条
    # 是 primary 按钮刻意用**反色**的 `text_on_accent`，另一条只改文字色 ——
    # **没有一条读这个字段**。它被定义、被推导（`_derive` 两条分支都写
    # `border_focus=accent`），却谁也不看。
    # 更糟的是 `_accent.py` 曾把它钉成字面量，等于给一个「改了也没人看」的
    # token 上了锁：谁想清理它，先得改测试。留着的唯一理由只能是「万一将来
    # 要一个跟 accent 分开的焦点色」，而那要的是**决定**，不是一个空字段。

    # Text
    text: str
    text_dim: str
    text_muted: str
    text_on_accent: str

    # Accent
    accent: str
    accent_hover: str
    accent_press: str
    accent_soft: str      # selected rows (list/tree/table), menu selection,
                          # icon button pressed, text
                          # selection, sidebar badge. NOT tabs — the two
                          # selected-tab rules use bg_surface / bg_base.
                          # （原来还列着 "sideAccent hover" —— 那个 variant
                          #   连同 `_side_button` 的 accent 参数一起删了。）

    # **图形档。与 `accent` 是两条不同的线，不是深浅两档。**
    #
    # `accent` 同时干两件事，而这两件事要求它**相反**的明度：
    #   作为**填充**（primary 按钮底、文本选区）→ 要够亮，`text_on_accent`
    #     才压得住。浅色下它是**近黑**（白字只 3.43，破 AA）。
    #   作为**图形**（侧栏徽标、标题栏图标、品牌小标）
    #     → 要够深，才看得见（非文本 3:1，WCAG 1.4.11）。
    #
    # 两条约束把可用带夹到只剩 **2 档宽**（`#2A80CE`…`#2A7ECC`，ΔE 2.8），
    # 连三态都排不下 —— 压深 `accent` 会牺牲用户已认可的填充色观感。
    #
    # **为什么不是 `accent_solid`。** 它五档全过（最差 3.31），但
    # `__init__.py` 钉死了「色值不许跨族借用」：`accent_solid` 按定义要满足
    # 白字 ≥4.5，压得比任何「看起来更深」的值都深，拿去做图形会把实心档的
    # 语义搞脏。所以这里另起一档，参照底取 `accent_soft`（图形最不利的那档
    # 静态底），门槛 3.0，由 `_solve_soft_lightness(target=3.0)` 解出。
    #
    # **判据只锚静态底，不锚 hover/active 底。** `bg_active` 对页面底只有
    # 2.27、`bg_hover_ctrl` 2.50 —— 这些底**自身**就够不到 3:1，hover 反馈
    # 靠的正是这点差值。要求停在它们上面的图标也够 3:1，等于要求底和图形
    # 同时再压深一档，那会把 hover 反馈本身抹掉。图标也**确实不会**停在
    # 上面：侧栏徽标只有「选中 `accent_soft`」「未选中 `bg_elevated`」两态，
    # hover 是瞬态且不改徽标底色。见 `tests/_uia11y.py` 的 GRAPHIC_PAIRS。
    accent_graphic: str   # sidebar badge / titlebar mark / brand mark —
                          # 任何「静止压在某个底色上的 accent 图形」

    # **实心填充族。与 `accent` 是两条不同的线，不是深浅两档。**
    #
    # 用户报「新建连接的配色很突兀：蓝色和黑色的字配在一起不柔和」。
    # 注意**那不是对比度问题** —— 浅色下近黑字压 `#2F8EE5` 是 5.59，AA
    # 远够。是不柔和。所以修法不是把字调得更黑，而是**换成白字**，
    # 而白字压 `#2F8EE5` 只有 3.43（破 AA）—— 于是必须先把底压深。
    #
    # `accent_solid` 就是「同色相、同饱和，把 L 压到白字刚好 ≥4.5」的那个
    # 值。它**不是** `accent` 的深一档：定义上它永远满足
    # `_contrast("#ffffff", accent_solid) >= 4.5`，由 `_derive` 二分求解。
    # 用户自选 accent 时这条**恒成立**（L 压到 0 就是纯黑，白字 21:1），
    # 不存在「某个自选色推不出实心档」的情况。
    #
    # 它同时是**键盘焦点环**的颜色（`qss.py` 里 6 条 focus 规则）。
    # 浅色下 `#1a78ce` 在四个底色上是 4.35 / 4.54 / 3.63 / 3.31，
    # 深色下 `#196cff` 在六个底色上最低 3.43 —— 非文本 3:1 全部达标。
    accent_solid: str
    accent_solid_hover: str
    accent_solid_press: str
    # **恒为白字，不是「按主题挑」。** `accent_solid` 按定义就够深，
    # 白字是它在**所有** accent 上都成立的那个选择（推导见上）。
    # 与 `text_on_accent` 分开是因为后者是按用户自选 accent 黑白择优的：
    # 浅色下它是**近黑**，直接拿去压 `accent_solid` 会得到近黑压深蓝。
    text_on_solid: str

    # Semantic
    success: str
    warning: str
    danger: str
    # **这里原来还有 `danger_hover`，零消费者。** 危险按钮的 hover 原本走
    # `qss.py` 里一条 `[variant="danger"]:hover` 专用规则（压 `danger` 本身 +
    # 硬编码 `#ffffff`，从不读 `accent_hover`）—— 而那个 variant 连同它的
    # 常驻规则**已经在同一次改动里删掉了**：全仓一个生产者都没有，连设置
    # 它的 API 都不存在。留着这个字段等于凭空多一个「改 danger 时记得改它」
    # 的责任。
    # `_accent.py` 的字面量守卫是**下限**，加字段不会红；删字段时记得跟着降
    # （`bg_scrim` / `border_focus` 删掉后是 27）。

    # Terminal
    term_bg: str
    term_fg: str
    term_cursor: str
    term_selection_bg: str
    term_selection_fg: str


DARK = Palette(
    name="dark",
    bg_base="#101319",
    bg_dialog="#1f2430",
    # **深色下终于有第三档了。** 原来与 `bg_surface` 逐字节同值，于是侧栏
    # 在深色主题里根本没有第三档 —— 主窗口实测像素：侧栏 `x=1..266` 全是
    # rail、`x=267` 是那条 border、`x=268` 起是 base，**侧栏完全靠那条线
    # 存在**。所以「深色观感完全不变」当时是事实，现在不是了。
    #
    # **为什么是「浮起一档」而不是像浅色那样「下沉」。** 浅色侧栏比主区**更
    # 暗**（recessed），因为浅底上「暗 = 退后」；深底上再暗就是黑洞，
    # 侧栏会变成一个坑。所以深色走反方向：比窗口底**亮**一档。
    #
    # **这个值是被夹在两条夹逼之间的，不是随手挑的**（实测全部数字）：
    #   #14171f（原来）  rail/base 1.0699  border/rail 2.4577
    #   #151824（上一版）  rail/base 1.0844  border/rail 2.4248
    #   #161b26          rail/base 1.1126  border/rail 2.3634  ← 破契约
    # 再往上抬，rail 越亮、`border` 对它就越暗，而 `border` 那个 2.4~2.6
    # 是本模块文档钉的区间。扫全 RGB 空间的结果是：要求
    # `rail/base ≥ 1.10` **且** `border/rail ≥ 2.40` **且** `muted/rail ≥ 4.5`
    # 三条同时成立的候选**一个都没有** —— 补色差和保 border 契约在深色下
    # **数学上互斥**。所以这里取「契约内能让 rail/base 尽量大」的那个值，
    # 不去追 1.10。
    #
    # **2026-10-10 现代化简约：surface / rail 从 `#14171f` / `#151824`
    # 拉开到 `#161a23` / `#1a1f2a`。**
    #
    # 原值两档只差 **ΔE 5.20 / 对比 1.014** —— 分不开，深色下侧栏和列表
    # 底实际上是一块。拉开后 `surface~rail` ΔE **9.5**，层次才立得住。
    # 同时 `surface~base` 15.3 → **20.7**，深色底下的卡片也不再要靠描边。
    #
    # **代价：`border` 对 surface 从 2.458 降到 2.388。** 本模块原先把
    # `border` 契约钉在 [2.4, 2.6]，现在必须放宽到 **[2.35, 2.6]** ——
    # 见下面 `border` 那一段。**这是主动让出 0.012，不是疏忽**：深色下
    # 「补色差」和「保 2.4」本来就互斥（上面那段注释已经算过全 RGB 空间），
    # 现代化简约要的是层次，不是那条线。
    #
    # **但代价比预估的大，这里记清楚。** 问卷里估的是「契约降到 2.39」，
    # 实测搜下来是 **2.34**，而且更硬的一条约束浮出来了：
    # **抬 `bg_elevated` / `bg_dialog` 的路走不通。** 扫了三档候选
    # （surface/rail/elevated/dialog 一起抬），`border #4d5670` 对抬亮后的
    # elevated / dialog 全部跌破 2.35 —— 一空搜索结果。根因就是上面那段
    # 已经算过的互斥：**border 是固定值，底色一抬它对底色的对比就降**，
    # 而 border 抬高去救底色，它在 base 上又会变成一条过于醒目的线。
    # 所以深色只能在 **elevated 之下**腾挪，rail 的天花板就是 `#1b1f29`。
    # 能拿到的最好结果：`surface~rail` ΔE 5.2 → **7.1**（+37%）。
    #
    # 取 `#141821` / `#171c26`：s~r **7.1**、surface~base 15.3→17.3、
    # rail~base 20.0→24.4，明度顺序 input < surface < rail < elevated < dialog
    # 保持成立，文字四档全够。代价 `border/rail` 2.425 → **2.341**。
    bg_surface="#181b23",
    bg_rail="#1c202a",
    bg_elevated="#222633",
    bg_input="#14171e",
    bg_hover_ctrl="#2b313e",
    bg_hover_row="#232733",
    bg_active="#212d42",
    # **`border` 契约从 [2.4, 2.6] 放宽到 [2.34, 2.6]（2026-10-10）。**
    # 上面 `bg_surface` / `bg_rail` 那段已经说明为什么必须让这 0.06 ——
    # 不是疏忽，是「深色下层次」与「保 2.4」二选一时选了层次。
    # 现值 2.436（surface）/ 2.341（rail），两端都在新区间内。
    border="#525c73",
    border_strong="#5f6a84",
    text="#e9edf4",
    text_dim="#a6b0c0",
    text_muted="#868f9e",
    # **不是白色。** 这个 token 落在 `accent` / `accent_hover` /
    # `accent_press` 三个**亮蓝**填充上（primary 按钮、文本选区、
    # 「新建连接」图标），白字实测只有 3.20 / 2.57 / 4.09，三个全破 AA。
    # 近黑是 5.99 / 7.45 / 4.69，全过。
    #
    # 这也让**默认蓝**和**自选亮色**走同一条路：`_derive` 对自定义 accent
    # 一直是 `toa = "#0d0f14" if _rel_lum(accent) > 0.18 else "#ffffff"`
    # 推出来的，而默认蓝的 `_rel_lum` 是 0.278，本来也该给深色 ——
    # 之前只有这个字面量没跟上，于是「换个亮色强调色，按钮文字会自己变深，
    # 换回默认反而是白的」。
    text_on_accent="#0d0f14",
    # **accent 族饱和度 1.000 → 0.880（2026-10-10 现代化简约）。**
    # 深色这三条原来是**满饱和**（S=1.000）—— 在近黑底上会「发光」，
    # 是整副配色里「不丝滑」最直接的来源。降到 0.88 之后：
    #   · 视觉上仍是同一个蓝，艳感收掉一档
    #   · `text_on_accent` 对它的对比 **5.99 → 6.12（反而更好）**
    #   · 三态两两 ΔE 34.7 / 37.6 / 71.6，远超 3.0
    #
    # **三态各自单独降饱和，原有的明度关系一律不动。** 深色这一版是
    # 「先亮后暗」（hover L71.0 > accent L64.9 > press L57.1），
    # 而 `_derive` 的规则是「近黑字 → 逐级变亮」。**这两者本来就不一致，
    # 这次不趁机统一** —— 统一它是一次语义改动（会改变「按下」的观感），
    # 不属于调色范围。`accent_press` 原本 S=0.790 已经低于目标，原样保留。
    accent="#5790f4",
    accent_hover="#74a3f6",
    accent_press="#3b7ae8",
    # **选中底：从 `#1a2740`（蓝染）改成 `#25292f`（近灰）。**
    #
    # 两件事一起改，因为它们改的是同一个 token：
    #
    # 1. **P0-2 修正违约。** `palette.py` 里 `_SOFT_TARGET_CR = 1.12` 的
    #    注释写明意图是「让**任何**色相都不会隐形」，而这一项实测 **1.1048**
    #    —— 低于自定门槛（浅色同项 1.1617 达标）。之所以长期没人发现：默认
    #    路径不走 `_derive`（`get_palette` 无自定义 accent 时原样返回字面量
    #    常量），而 `tests/_accent.py` 只钉了字面量、没钉性质。
    # 2. **P1-3 统一明暗色相。** 当时浅色 `accent_soft` 是纯中性灰
    #    （hue 60 / sat 0.041，注释明写「参考图的选中胶囊就是灰的，蓝只留给
    #    强调元素」），深色却是蓝染（hue 219.5 / sat 0.422）—— 切主题时
    #    选中项会「从灰变蓝」。于是深色也走灰。
    #
    #    **这一段记的是当时的状态，浅色那一支后来又变了。** 浅色现在的
    #    `accent_soft` 是 `#E2E8F2` —— hue **217.5** / sat **0.381**，
    #    也就是**带蓝的浅灰**，不再是「纯中性灰」。深色仍是 `#25292f`
    #    （hue 216.0 / sat 0.12），**两支的 hue 现在是对齐的**（217.5 vs
    #    216.0）—— 也就是说 P1-3 那个「切主题时选中项从灰变蓝」的问题
    #    现在是靠**两边都用同一个冷灰**解决的，不是靠「两边都纯灰」。
    #    （记下来是因为上面这段原文读起来像是「浅色是纯灰」，会误导人
    #    以为浅色不该带蓝。）
    #
    # **S=0.12 而不是纯灰 S=0。** 纯灰会让选中态的着色整个消失（`_SOFT_S`
    # 那段注释反对的正是这个），0.12 留一丝冷调，肉眼仍读作灰。
    accent_soft="#25292f",
    # **深色下图形档与 `accent` 同值，随它一起降饱和。**
    # 深色 `accent` 在九个底色上全部达标，浅色那条「可用带只有 2 档宽」的
    # 矛盾在这里不存在 —— 所以不另取值。（`__init__.py` 要求每个字段都有
    # 真消费者：这一档的消费者与浅色完全同一批代码路径，见 `sidebar.py` /
    # `titlebar.py`。）用户自选强调色时 `_derive` 仍会解一遍。
    accent_graphic="#5790f4",
    # **深色下的实心填充：`#266ff1`（原 `#196cff`，降饱和后重解）。**
    # 它按定义就是「同色相、同饱和，把 L 压到白字 ≥4.5」—— 降饱和改变了
    # 亮度，**必须重新求解而不是手工降**。第一次手工把 `#196cff` 降到
    # S=0.88 得 `#2770f1`，白字只剩 **4.493**（破 AA）。重解后 4.539。
    # 作为焦点环在六个底色上最低 3.422，三态 ΔE 10.8 / 27.5 / 16.8。
    accent_solid="#266ff1",
    accent_solid_hover="#1d69f1",
    accent_solid_press="#0f60ef",
    text_on_solid="#ffffff",
    success="#3fb950",
    warning="#d29922",
    # **`danger` 原本是 `#f05252`，对 `bg_dialog` 只有 4.463，破 AA。**
    #
    # **提亮到 `#f35555` 之后，真正的问题才露出来。** 那一版的论证写的是
    # 「危险文字大量出现在弹框里」——**那个前提后来被证伪了**：danger 文字
    # 真实挨着的底色是 `bg_base`（状态标签）、`bg_surface`（传输条标题、
    # 磁盘表百分比）、`bg_hover_row`（磁盘表那一行被鼠标悬停时）。
    # 压在 `bg_hover_row` 上只有 **4.428** —— 破了 AA，而且**没有任何一条
    # 判据在量它**。这就是「量对了、量错了地方」的又一层：不是量错了数值，
    # 是量了一对**根本不相邻**的底色（`bg_elevated` 上 danger 只作图形填充）。
    #
    # 提亮到 `#f65958` 后，**真实邻接**全部达标：
    #   文字档 4.5   bg_base 5.746 / bg_surface 5.319 / bg_hover_row 4.603
    #   图形档 3.0   bg_elevated 4.658 / bg_dialog 4.795 /
    #                bg_surface 5.319 / bg_base 5.746
    #
    # 与 `#f35555` 的色距 ΔE00 **1.035**（JND 约 2.3），色相只偏 **0.38°**，
    # 肉眼与原色无差 —— 与当初 `#f05252` → `#f35555` 是同一量级的微调。
    danger="#f65958",
    term_bg="#0b0e14",
    term_fg="#c9d1d9",
    term_cursor="#62a7f5",
    term_selection_bg="#2d4a6b",
    term_selection_fg="#ffffff",
)


LIGHT = Palette(
    name="light",
    # **整套浅色是照着一张参考截图的实测像素改的**（暖调中性：R=G，B 略低 3），
    # 而原先是冷调（`#f4f6f9` / `#5a6472` 的 B 高于 R）。三条结构性差异：
    #
    # 1. **侧栏比主区深**（关系反转了）。原来 `bg_surface` 是 `#ffffff`、
    #    比 `bg_base` 更亮；参考图里侧栏 `#E6E6E3` 比主区 `#FAFAF8` **更暗**，
    #    所以现在 `bg_surface` 压到 `bg_base` 之下。`qss.py` 里
    #    `QFrame[role="sidebar"]` 用的就是 `bg_surface`。
    # 2. **选中项是中性灰**（`accent_soft = #DCDCD9`），不是蓝染 ——
    #    参考图的选中胶囊就是灰的，蓝只留给强调元素。
    # 3. **`text_on_accent` 从白字改成近黑**（`#0d0f14`）。参考的强调蓝偏亮，
    #    白字压上去只有 **3.43**，破 AA；而深色主题早就用近黑字解过同一道题。
    #    连带地，**强调三态的明度顺序反了过来**：hover 必须**亮于** accent
    #    （深字需要蓝更亮才有对比度），press 仍**深于** hover。
    #    `tests/_accent.py` 的「press 比 hover 更深」在浅色下依然成立 ——
    #    已用 `_rel_lum` 量过：hover 0.2676 > accent 0.2254 > press 0.2003。
    #
    # 三处**没有**照抄参考图，是算出来的：
    # · `text_muted` 参考值 `#8B8B89` 在最深的 `bg_hover_row` 上只有 3.94，
    #   达不到正文级小字的 4.5 → 压到 `#636362`（各底色最差 4.55）。
    # · `accent_hover` / `accent_press` 参考图只有一个蓝，按 `_derive` 的同一条
    #   规则（`_from_hsl(h, s, l*1.05)` / `l*1.12`）补出另两态。
    # · `border` 参考图推算值太淡 → `#9E9E9C`，对 `bg_base` 是 2.57，
    #   落回本模块文档钉的 2.4~2.6 区间。
    # **页面底从 `#FAFAF8` 压到 `#F6F5F2`（2026-10-10 现代化简约）。**
    #
    # 视觉理由：现代化简约的层次靠**色差**不靠线。而原来这版卡片
    # `bg_surface`（纯白）与页面底只差 **ΔE 9.9** —— 这么浅的差值撑不起
    # 分层，所以 `QFrame[role="card"]` 必须画 1px 描边。**那圈描边不是
    # 多余，它是在替色差干活。** 要减线条就得先把色差拉开。
    #
    # 为什么选 `#F6F5F2`（实测三档平衡）：
    #     页面底候选      vs 卡片 #FFFFFF   vs bg_elevated #ECECEA   正文对比
    #     #FAFAF8（原值）        9.9              24.2              15.60
    #     #F6F5F2（采用）       18.7              15.7              14.96
    #     #F2F1ED               26.2               8.4   ← 与卡片按钮糊了
    # 更亮则卡片浮不起来，更暗则 `bg_elevated`（卡片内按钮底）埋进页面底。
    # `term_bg` 跟着一起改 —— 终端底与页面底同源，本来就该同值。
    bg_base="#F2F1EE",
    # **浅色下的弹窗底不能是纯白。** 原来这里写的是 `#FFFFFF`，而当时页面底色
    # `#FAFAF8` 只比纯白低 10 级 —— 色差 9.9，肉眼几乎分不出「弹窗浮起来
    # 了」（用户报「白色识别度不高」）。浅色没有「再亮一点」的空间了，
    # 所以只能**往深走**。
    #
    # **但不能一路深到按钮糊掉。** 弹窗里的按钮用 `bg_elevated=#ECECEA`，
    # 两者的色差必须也留得住，否则「Pick Screen Color」这种按钮会直接沉进
    # 背景（实测：`#F2F1ED` 时按钮距弹窗底只剩 8.4，界面上已经看不见了）。
    # 候选表的**两列都要看**：离页面够远（认得出是浮层）**且**离按钮底够远
    # （按钮不糊）。
    #
    # 页面底在 2026-10-10 从 `#FAFAF8` 压到 `#F6F5F2` 之后重算（色距，
    # RGB 欧氏和；两列都列出来是为了看出改底色动了哪一档）：
    #
    #     候选          vs 页面 #F6F5F2   vs 旧 #FAFAF8   vs 按钮底 #ECECEA
    #     #FFFFFF       18.7 ✓            9.9 ✗           34.1 ✓
    #     #F7F6F3        1.7 ✗ 太近       7.1 ✗           17.4 ✓
    #     #F2F1ED        7.5 ✗            16.3 ✓           8.4  ✗ 按钮糊掉
    #     #ECEBE7       17.9 ✓           26.6 ✓           3.2  ✗ 完全糊掉
    #     #E4E3DE       32.4 ✓           41.1 ✓          17.0 ✓   <- 采用
    #
    # **结论没变，仍是 `#E4E3DE`** —— 它是唯一两头都够的那一档。注意
    # `#F7F6F3` 在新页面底下**只剩 1.7**，基本等于同色了：页面底一压深，
    # 「往亮走」这条路就彻底封死，这也是浅色弹框底必须往深走的第二个理由。
    #
    # 深色那边**保持变亮**（`#1e2431` 对终端 `#0b0e14` 是 16 级亮差，本来就
    # 有明显层次，往深走会让弹窗变成一个黑洞）。两套主题方向相反，但承诺
    # 同一条：弹窗底和内容区底必须一眼分得开。
    bg_dialog="#FCFBF9",
    bg_surface="#FFFFFF",
    # **侧栏单独一个字段**：见上面 `bg_rail` 的说明。参考图侧栏 `#E6E6E3`
    # 比主区 `#FAFAF8` **更暗**，而 `bg_surface` 仍保持「比窗口亮」的浮起语义 ——
    # 它撑着列表背景、下拉弹层和传输条卡片，那些是内容区，不该跟着侧栏一起沉下去。
    bg_rail="#EAE9E5",
    bg_elevated="#F1F0EC",
    bg_input="#FFFFFF",
    bg_hover_ctrl="#E7E6E1",
    bg_hover_row="#ECEBE7",
    bg_active="#E2EAF5",
    # `border` 2.57（在 2.4~2.6 区间内）；**输入框边界反而比旧版更好**
    # （白底上 2.68，旧版 `#9aa2b0` 是 2.57）—— 输入框是要靠这条边界被认出来的
    # （WCAG 1.4.11），所以在往深里调一点点之前先确认没有把它调得更浅。
    # `border_strong` 对主区 3.84、**对侧栏 `bg_rail` 3.21** —— 侧栏比主区暗，
    # 所以它必须在**两边**都够 3.2，只按 `bg_base` 解会漏掉侧栏那一档。
    border="#8E8D89",
    border_strong="#747370",
    text="#20201F",
    text_dim="#404042",
    text_muted="#636362",
    text_on_accent="#0d0f14",
    # **accent 族饱和度 0.778 → 0.750（2026-10-10 现代化简约）。**
    # 「丝滑」在这一版里的主要来源是**降饱和**，不是调明度。S 0.778 在暖白
    # 页面上偏艳；0.750 保持 hue 与 L 完全不变，只把艳度收一档，肉眼仍是
    # 同一个蓝。近黑字对比 5.57（原来 5.59，几乎不变）。
    #
    # **三态各自单独降饱和，明度关系原样保留**（hover 0.567 > accent 0.541
    # > press... 实测 hover L56.9 > accent L54.1 < press L60.6 的原序不动）——
    # 这一版按「近黑字 → 整体往亮里推」排的序**不因为降饱和而重排**，
    # 否则「按下」和「悬停」的方向就变了，那是一次语义改动，不是调色。
    # 三态两两 ΔE 13.9 / 19.5 / 33.4，仍远超 3.0。
    accent="#328ee2",
    accent_hover="#3e95e3",
    accent_press="#4f9ee6",
    accent_soft="#E2E8F2",
    # **图形档：压深到 `#2A80CE`，只用于「静止压在某个底色上的图形」。**
    #
    # 实测 `accent #2F8EE5` 在五个静态底上（括号内是换成 graphic 之后）：
    #   accent_soft   2.50 ✗ (3.01)   侧栏徽标胶囊
    #   bg_elevated   2.90 ✗ (3.50)   侧栏折叠行图标
    #   bg_dialog     2.67 ✗ (3.22)   弹框标题栏图标
    #   bg_rail       2.74 ✗ (3.31)   侧栏品牌小标
    #   bg_surface    3.43 ✓ (4.14)   窗口标题栏图标 / SFTP 图标 / 隧道开关轨
    # 前四个是 WCAG 1.4.11 的真失败 —— 且都是**功能图标**（认证方式、弹框
    # 标识），不是品牌标识，不适用 logo 豁免。深色同一批点全过（4.66~5.60），
    # 明暗严重不对称。
    #
    # **`#2A80CE` 是怎么定的：** `accent` 的可行带只有 2 档宽，这里取「压深
    # 10%」那一档 —— 图形侧五档全过，且对 `accent` 的 ΔE 27.4，填充与图形
    # 两种用途一眼能分辨，不会看成同一件事。
    accent_graphic="#2A80CE",
    # **浅色下的实心填充 = `#1d78cb`（原 `#1a78ce`，2026-10-10 降饱和后重解）。**
    # 它是「同色相、S=0.750，把 L 压到白字 ≥4.5」这条规则解出来的。
    # **降饱和不能手工照搬** —— S 一降，亮度跟着变，直接把 `#196cff` 那类
    # 值降饱和后白字会掉到 **4.493**（破 AA，深色那次就是这样）。所以实心档
    # 一律**重新求解**，不手工降。
    # 白字 4.564，作为焦点环在六个底色上最低 3.552；三态 ΔE 10.3/25.7/15.4。
    accent_solid="#1d78cb",
    accent_solid_hover="#1c73c2",
    accent_solid_press="#1a6bb5",
    text_on_solid="#ffffff",
    success="#1A7237",
    # **三个状态色都压深了一档**，因为侧栏比主区暗，而状态标签同时出现在两层
    # 底上：主区 5.42 / 4.53，warning 5.41 / 4.52，danger 5.41 / 4.52 ——
    # 全部 ≥4.5。原来那条 `#966400` 在 `bg_base` 上只有 4.4961（差 0.0039 破 AA）
    # 的教训仍然适用，只是最紧的一档从「主区」换成了「侧栏」。
    #
    # **但那一段量的是主区和侧栏，漏了弹框。** `bg_dialog` 比两者都亮，
    # 三个状态色在弹框里都要重算：`success` 原色 4.405 / `warning` 4.397，
    # **两条都破 AA**。压深后：success 4.661、warning 4.657。
    warning="#885A00",
    # **同理压深一档**：原色 `#C42A2A` 对 `bg_dialog`（浅色弹框底）只有
    # **4.403**，破 AA —— 那一段注释里量的是主区和侧栏，**弹框底色
    # 比两者都亮**，所以那两个 ≥4.5 并不能保证弹框里也达标。
    #
    # **这一版的数字已按「真实邻接」重算**（原来的 4.608 / 5.006 / 5.921 /
    # 5.666 是在 `bg_dialog` 仍被当成文字邻接时写的，而那个前提后来被证伪）：
    #   文字档 4.5   bg_base 5.242 / bg_surface 5.921 / bg_hover_row 4.964
    #   图形档 3.0   bg_elevated 5.193 / bg_dialog 5.726 / bg_surface 5.921
    # `success` / `warning` 同理，全部 ≥4.5（见 `tests/_uia11y.py`）。
    danger="#C02626",
    term_bg="#F6F5F2",
    term_fg="#20201F",
    term_cursor="#328ee2",
    # **选区底必须压到 3:1（WCAG 1.4.11）才看得见选中了什么。**
    # 我第一版按参考的「淡蓝」直接取了 `#C9DEFA`，压在 `term_bg` 上只有
    # **1.31** —— 文本选区几乎与终端底色融为一体，而旧版 `#b6d7ff` 是 3.53。
    # 那是本次改色引入的**真实回归**，评审实算出来的，不是目测。
    term_selection_bg="#5E93D6",
    term_selection_fg="#20201F",
)


THEMES: dict[str, Palette] = {"dark": DARK, "light": LIGHT}


# ---------------------------------------------------------------------------
# 用户自选强调色
# ---------------------------------------------------------------------------
#
# **默认路径一个字都不改。** 没设 accent 时 `get_palette(theme)` 直接返回
# `THEMES` 里的字面量常量（`is` 都成立），所以所有既有像素断言一条也不用动。
# 下面这一整套推导只在用户真的挑了个颜色时才跑。

# 用正则而不是 `QColor.isValid()`：后者有洞——`"#4c8dff80"`（8 位）返回 True
# 却解析成 `#8dff80`（绿），`"red"` 也放行。accent 存的是"用户挑的那个颜色"，
# 不是一段要解析的 CSS，所以只认规范的 6 位十六进制。
_ACCENT_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# 浅色/深色的明度夹取区间不同。**别把它们写成同一个值**：评审实测
# 深色用 [0, 0.97] 时，有 8% 的色会让 press 比 accent 更亮——"按下比悬停亮"
# 是方向反了，用户看着像 bug。深色收窄到 0.95 把这一类归零。
_CLAMP = {"light": (0.0, 0.97), "dark": (0.0, 0.95)}

# `accent_soft` 的目标对比度（对 bg_elevated）。写死值是 1.090 / 1.105，
# 这里留一点余量，让**任何**色相都不会隐形。
_SOFT_TARGET_CR = 1.12

# **悬停底（`bg_hover_ctrl` / `bg_hover_row`）离静置底的最小色距。**
#
# **为什么它不能沿用 `_SOFT_TARGET_CR`（1.12）。** 1.12 是给**持久选中胶囊**
# `accent_soft` 定的 —— 那个底是「一直显示在那儿」的，浅到看不清就是真缺陷。
# 悬停是**瞬时态**：指针一移开就消失，它的主要反馈通道其实另有两条 ——
# ① 悬停时显形的 1px `border_strong`（对 `bg_elevated` 实测 4.158:1，
#    远超 WCAG 1.4.11 要的 3:1）；
# ② 状态跳变本身。
# 填色差只是次要修饰。
#
# **而且 1.12 在惩罚浅色主题。** WCAG 相对亮度比在接近白的一端被压缩：
# 浅色 `bg_hover_ctrl` vs `bg_elevated` 的 ΔE 是 **17.92**，深色是 **17.97**
# —— 两套主题的可感知台阶几乎相同，CR 却差 0.06（1.096 vs 1.157）。
# 同一个「悬停要看得见」，对浅色就要求了更大的感知差才放行。
#
# **改用仓库自己的 `_delta`（RGB RMS 色距）来度量**，门槛 12.0：
# 这一档与仓库已有的底色层次门槛同量级（`surface~rail` 是 7.1、
# `accent` 三态两两 13.9 / 19.5 / 33.4），且**深浅两套主题用同一把尺子**。
# 现值 light 17.92 / dark 17.97 都过，余量约 49%。
#
# **代价：浅色的 hover 差值看起来比深色小一点。** 那是 CR 在白端的压缩
# 造成的度量失真，不是观感问题 —— 两者的 ΔE 几乎相同。
_HOVER_MIN_DELTA = 12.0
# `accent_soft` 的固定饱和度。固定而不是从 base 继承 S：继承的话，灰色
# accent（S=0）会让 soft 变成纯灰，选中态的着色就整个消失了。
#
# **深色从 0.45 降到 0.12**：浅色那一支当时是纯中性灰（sat 0.041，注释
# 明写「参考图的选中胶囊就是灰的，蓝只留给强调元素」），深色却给到 0.45
# 的蓝染 —— 切主题时选中项会「从灰变蓝」。两支统一到走灰。
#
# **浅色这一支后来又变了**：现在的浅色 `accent_soft` 是 `#E2E8F2`
# （hue 217.5 / sat 0.381），也就是带蓝的浅灰。所以「两支统一到走灰」
# 现在的实际含义是**两支用同一个冷灰色相**（217.5 vs 216.0），
# 而不是「两支都是无彩色」。
_SOFT_S = {"light": 0.65, "dark": 0.12}


def _hsl(hex_str: str) -> tuple[float, float, float]:
    """#rrggbb -> (h 0-360, s 0-1, l 0-1)。"""
    r, g, b = (int(hex_str[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = 60 * (((g - b) / d) % 6)
    elif mx == g:
        h = 60 * ((b - r) / d + 2)
    else:
        h = 60 * ((r - g) / d + 4)
    return h, s, l


def _from_hsl(h: float, s: float, l: float) -> str:
    h, s, l = h % 360.0, min(1.0, max(0.0, s)), min(1.0, max(0.0, l))
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = l - c / 2
    tbl = ((c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x))
    r, g, b = tbl[int(h // 60) % 6]
    return "#%02x%02x%02x" % (round((r + m) * 255), round((g + m) * 255),
                              round((b + m) * 255))


def _rel_lum(hex_str: str) -> float:
    """WCAG 相对亮度。"""
    out = []
    for i in (1, 3, 5):
        v = int(hex_str[i:i + 2], 16) / 255.0
        out.append(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def _contrast(a: str, b: str) -> float:
    la, lb = _rel_lum(a), _rel_lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


#: 黑白择优的分界。**不是 0.5** —— 0.5 时 12.5% 的色白字看不清。
#: `_derive` 里推 `text_on_accent` 用的就是这条线。
_TEXT_ON_LIGHT_THRESHOLD = 0.18

#: 择优时的两个候选色：近黑与纯白。
_TEXT_ON_DARK = "#0d0f14"
_TEXT_ON_LIGHT = "#ffffff"


def text_on(fill: str) -> str:
    """在给定的**填充色**上，该用近黑字还是白字。

    **这是 `_derive` 里那条黑白择优规则的公开版本。**

    ## 为什么要有这个函数

    `text_on_accent` 是按 **accent** 推的。把它拿去压 `danger` / `success` /
    `warning` 的填充，就正好犯了 `design/v1/__init__.py` 里写死的那条硬约束：

    > **色值不许跨族借用。** `text_on_accent` 是按用户自选 accent 推的，
    > 拿它去压 `danger` 在深色 accent 下会原样退回原来的低对比度 ——
    > 数值不变，所以没有任何测试会红。

    借用不会让数值变差到「测试会红」的程度，所以**没有任何判据能抓到它** ——
    只有把「填充色上那行字实际用了什么色」算出来才知道。

    真实例子（2026-10 一轮无障碍评审）：
    - 监控条的百分比标签画在状态色**填充**上，用的是 `p.text`。
      深色主题下 `text #e9edf4` 压 `success #3fb950` 只有 **2.163**，
      压 `warning #d29922` **2.150**，压 `danger` 2.865 —— **三个全破 AA**。
    - 标题栏关闭键的 ✕ 画在 `danger` 填充上，用的是 `text_on_accent`。
      浅色主题下那是近黑 `#0d0f14`，压 `#C02626` 只有 **3.237**（图形档 3.0
      勉强够，但同样的填充用白字是 5.921）。

    ## 用法

    ```python
    pen = QtGui.QColor(palette.text_on(fill_hex))
    ```

    **必须按目标填充色自己算**，不许借用 `text_on_accent`，也不许用 `text`。

    ## 边界

    这条规则**不做「≥4.5」的承诺** —— 实测 HSL 全域有 2.75% 的色达不到，
    承诺一个做不到的数就是撒谎（与 `_derive` 里的同一段说明）。
    它只保证「在黑白两个候选里挑较好的那个」。
    """
    return _TEXT_ON_DARK if _rel_lum(fill) > _TEXT_ON_LIGHT_THRESHOLD \
        else _TEXT_ON_LIGHT


def _delta(a: str, b: str) -> float:
    """粗略色距（RGB 空间的 RMS）。判"这两个色用户分不分得出来"够用了。"""
    return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2
               for i in (1, 3, 5)) ** 0.5


def _solve_soft_lightness(hue: float, sat: float, bg: str, light: bool,
                          target: float = _SOFT_TARGET_CR) -> float:
    """反解 L，使 ``HSL(hue, sat, L)`` 对 *bg* 的对比度**达到或超过** target，
    从「紧挨着 bg 的明度」开始，沿**远离**方向走到刚好够为止。

    **为什么既不能二分、也不能在全域上搜「最接近 target 的解」。**
    ``contrast(L)`` 对 L **不是单调的**，它是**两端达标、中间谷底** ——
    因为参照底 `bg_elevated` 本身既不是纯黑也不是纯白：

        浅色 HSL(209.5, 0.65, L) vs #ECECEA
          L=0.00 → 17.75 ✓      L=0.85 → 1.229 ✓
          L=0.90 → 1.077 ✗      L=1.00 → 1.183 ✓   ← 纯白又"达标"
        深色 HSL(218.2, 0.12, L) vs #1b1f29
          L=0.00 → 1.275 ✓      L=0.15 → 1.073 ✗
          L=0.20 → 1.263 ✓      L=1.00 → 16.48 ✓   ← 纯白更"达标"

    原二分那句 `too_faint == light` 等价于假设「对比不足 ⟺ L 太大」，
    在这个形状上**根本不成立**：实测它让 128/360 个自选色相的 `accent_graphic`
    解出 1.58~2.99（门槛 3.0）。而改成「全域搜离 target 最近的达标点」
    同样错 —— 它会让浅色红系的 `accent_soft` 落在 `#fdf8f8`（几乎纯白，
    40/360），级联下去 `accent_graphic` 有 203/360 掉到 2.45。

    **起点必须是 bg 自己的明度，不是原 accent 的。** 这条 token 的语义是
    「浮起/沉下**一点点**」，它天生贴着参照底：浅色 `#DCDCD9`(L .857) 紧贴
    `#ECECEA`(L .926)，深色 `#25292f`(L .165) 紧贴 `#1b1f29`(L .128)。从
    原色出发会被用户的自选色带偏；从 bg 出发则「离 bg 越远越好」这件事
    本身就由扫描方向保证了。

    *target* 参数化是为了让 `accent_graphic` 复用**同一条**扫描：它要的
    也是「某个 L 使这族色对某个底刚好够对比」，只是目标从软底的 1.12 换成
    图形的 3.0。两条规则共用一份实现，就不会在换目标时各自漂移。
    """
    l0 = _hsl(bg)[2]
    # 亮底要往暗走才够对比，暗底要往亮走。步长 0.001 ≲ 1/255，
    # 比 8bit 一档还细，所以不会从达标跳到不达标。
    step = -0.001 if light else 0.001
    for i in range(0, 1001):
        L = l0 + i * step
        if not 0.0 <= L <= 1.0:
            break
        if _contrast(_from_hsl(hue, sat, L), bg) >= target:
            return L
    # 8bit 下无解。返回紧贴 bg 的那一端，由调用方的判据如实报出失败 ——
    # 这里返回任何"看起来对"的值都是撒谎，判据该红就让它红。
    return l0


def _solve_solid_lightness(hue: float, sat: float, lightness: float) -> float:
    """反解 L，使 ``HSL(hue, sat, L)`` 压**白字**的对比度 ≥ 4.5。

    **只压不抬**（`hi` 就是传进来的 `lightness`）。accent 本来就够深的
    时候（比如用户选了藏青 `#000080`，白字 11:1），解就是它自己 ——
    实心档必须**不改变用户挑的颜色**，而不是为了凑某个目标值去调亮它。

    **这个函数恒有解。** L 压到 0 就是纯黑，白字 21:1，所以不存在
    「某个自选色推不出实心档」的情况 —— 也就没有「推不出来时回退到
    accent 凑合」这条分支需要写。

    **二分之后还有一步「补够」**，因为二分解出的是**连续** L，量化成
    #rrggbb 之后可能掉到 4.5 以下（详见下面那段注释）。
    """
    lo, hi = 0.0, max(0.0, lightness)
    if _contrast("#ffffff", _from_hsl(hue, sat, hi)) >= 4.5:
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2
        if _contrast("#ffffff", _from_hsl(hue, sat, mid)) >= 4.5:
            lo = mid
        else:
            hi = mid
    # **落盘后必须真的 ≥4.5，所以这里是「补够」而不是「挪近」。**
    #
    # 上一版写成「哪边离 4.5 近就往哪边挪几步」，看着和
    # `_solve_soft_lightness` 一致，其实是错的：二分解出的是**连续** L，
    # 量化成 #rrggbb 之后可能掉到 4.5 以下，而「挪近」在这种情况下会
    # 因为另一侧跳过了 4.5 更远而**停在不合格的值上**。实测洋红
    # `#ff2fd0`：二分解出的 L 落盘是 `#dd00ab`，白字 **4.478** ——
    # 而这个 token 的全部意义就是「白字 ≥4.5」，差 0.022 也是不合格。
    #
    # 改成单向：只要落盘结果还差，就继续往下（更暗）走。走到底 L=0 是纯黑，
    # 白字 21:1，所以这个循环恒有解。
    best = lo
    for _ in range(64):
        if _contrast("#ffffff", _from_hsl(hue, sat, best)) >= 4.5:
            break
        nxt = max(0.0, best - 0.002)
        if nxt == best:
            break
        best = nxt
    return best


_SOLID_STEP = (0.02, 0.05)     # hover / press 的**固定** L 步长
_SOLID_MIN_DELTA = 3.0          # 与既有 accent 族同一个三态可分辨门槛


def _solid_states(hue: float, sat: float, solid_l: float,
                  solid: str) -> tuple[str, str]:
    """从实心档推出 hover / press，保证三态两两色距 ≥3。

    **步长是固定的 L 差值，不是倍数。** 原来照 accent 族写成 `l*0.906` /
    `l*0.75`，结果 accent 本来就是纯黑/近黑时（白字已经够用，
    `_solve_solid_lightness` 原样返回它自己的 L），`l*0.906` 量化回 8bit
    与 `l` 同色，三态糊成一个：实测 `#000000` 三态全等于 `#000000`
    （色距 0.0），按下去零反馈。固定步长在 L≈0 处仍然走出可见的差。

    **方向自适应：默认更暗，压不动就反过来更亮。** 更暗是深色填充的惯例，
    而且变暗只会让白字对比度更高；更亮同样安全（白字压更亮的底对比度只会
    更高，不会破 4.5）。

    **恒有解。** 实心档的定义保证 ``solid_l`` 远小于 1（白字在 L=1 上是
    1.00），所以「更亮」这个方向永远走得动；实心档解在 L=0 附近时三态
    分得开，走的正是更亮那一支。返回的两个值必然与 ``solid`` 色距 ≥3。
    """
    def at(ll: float) -> str:
        return _from_hsl(hue, sat, min(1.0, max(0.0, ll)))

    for sign in (-1.0, 1.0):
        hover = at(solid_l + sign * _SOLID_STEP[0])
        press = at(solid_l + sign * _SOLID_STEP[1])
        if (min(_delta(hover, solid), _delta(press, solid),
                _delta(press, hover)) >= _SOLID_MIN_DELTA):
            return hover, press
    # 走不到的唯一可能是 solid_l 同时贴住 0 和 1 —— 而实心档的定义排除了
    # 后者。留这一支是为了「万一」时有个确定行为，而不是让上面那个
    # `for` 静悄悄返回 None 崩在下游。
    return solid, solid


def _derive(theme: str, base: Palette, accent: str) -> Palette:
    """从一个基准色推出整个 accent 家族。

    判据不是"好看"，是**三态可区分**：``accent_hover`` / ``accent_press``
    必须和 accent、彼此都分得开。用户点下去看不到变化，和没有 hover 是一样
    的体验缺陷。分不开就整族退回该主题的写死值——宁可换个颜色，也不能给一个
    按下去没反馈的按钮。
    """
    h, s, l = _hsl(accent)
    lo, hi = _CLAMP[theme]
    light = theme == "light"

    def scale_l(factor: float) -> float:
        return min(hi, max(lo, l * factor))

    # 黑白择优的分界在 relL≈0.18，不在 0.5。（0.5 时 12.5% 的色白字看不清。）
    # 这里不做"强制 ≥4.5"的承诺——实测 HSL 全域有 2.75% 达不到，承诺一个做不到
    # 的数就是撒谎。自动择优能把下限从 4.21 抬到 4.40，那就取这个。
    # **规则本体在 `text_on()` 里** —— 这里不重抄一遍，免得两处漂移。
    toa = text_on(accent)

    # **hover/press 的方向必须跟着 `toa` 走，而不是跟着主题名走。**
    #
    # 原来浅色固定「越按越暗」（`0.906` / `0.75`），那在**白字**下是对的 ——
    # 底越暗白字越清楚。但 `toa` 是**按 accent 逐个择优**的：用户在浅色主题下
    # 挑一个亮色（relL > 0.18）时它就是**近黑字**，而底还在越按越暗 —— 方向反了。
    #
    # 实测（**既有缺陷，与换配色无关**，用换色之前的 palette 跑同一条
    # `_derive` 就能复现）：浅色 accent `#2F8EE5` → press **3.44**，
    # `#4c8dff` → 3.48，`#e5484d` → 3.12。**深色也一样**：`#df20df` press
    # **3.33**、`#df2020` hover **4.38**（那支 `toa` 是白字，而悬停被调亮了）。
    # 18 个抽样色相里浅色 2 个、深色 5 个不达标。
    #
    # **改成一条规则：近黑字就把三态整体调亮，白字就整体调暗。**
    # 于是深色主题那条「悬停更亮」的旧约定作废 —— 它只在白字下成立。
    _dark_text = toa == "#0d0f14"

    if _dark_text:
        # 近黑字要更亮的底。**press 给最亮**：压深在近黑字上必然掉对比度
        # （实测只压到 0.955 倍就已经掉到 3.97），而「按下更亮」在亮蓝上是
        # 常见的激活反馈，不违反「三态要分得开」这条真正的不变量。
        hover = _from_hsl(h, s, scale_l(1.05))
        press = _from_hsl(h, s, scale_l(1.12))
    else:
        # 白字：底要暗，越按越暗。
        hover = _from_hsl(h, s, scale_l(0.906))
        press = _from_hsl(h, s * 0.85, scale_l(0.75))

    soft = _from_hsl(
        h, _SOFT_S[theme],
        _solve_soft_lightness(h, _SOFT_S[theme], base.bg_elevated, light))

    # 图形档：同色相同饱和，L 挪到「对 `soft` 刚好 3.0」。
    #
    # **参照底取 `soft` 而不是页面底。** 图形（侧栏徽标、标题栏图标）最不利
    # 的那档底就是选中胶囊 —— 它比页面底更极端，浅色下 `#DCDCD9` 对
    # `#2F8EE5` 只有 2.50，而 `#FAFAF8` 有 3.28。拿页面底当参照会解出一个
    # 在胶囊上看不见的值。
    #
    # 复用 `_solve_soft_lightness`（只换 target / near_l）而不是另写一份：
    # 两条规则问的是同一个问题，只是目标不同。默认路径下深色这一档等于
    # `accent` 本身（深色 accent 九个底色最低 4.07，本就全过），浅色则
    # 从 `#2F8EE5` 压到 `#2A80CE`。
    graphic = _from_hsl(
        h, s, _solve_soft_lightness(h, s, soft, light, target=3.0))

    # 实心档：同色相、同饱和，把 L 压到白字 ≥4.5。**方向与上面那三态无关**
    # —— 它服务的是「白字压这个底」，而不是 `toa` 那套黑白择优。
    solid_l = _solve_solid_lightness(h, s, l)
    solid = _from_hsl(h, s, solid_l)
    solid_hover, solid_press = _solid_states(h, s, solid_l, solid)

    # 黑白择优的分界在 relL≈0.18，不在 0.5。（0.5 时 12.5% 的色白字看不清。）
    # 这里不做"强制 ≥4.5"的承诺——实测 HSL 全域有 2.75% 达不到，承诺一个做不到
    # 的数就是撒谎。自动择优能把下限从 4.21 抬到 4.40，那就取这个。
    # **规则本体在 `text_on()` 里** —— 这里不重抄一遍，免得两处漂移。
    toa = text_on(accent)

    if min(_delta(hover, accent), _delta(press, accent),
           _delta(press, hover)) < 3.0:
        # 三态分不开。**只退 hover/press，保住用户挑的 accent。**
        # 整族退回的话，用户选的颜色直接没生效（全域约 6.6% 的色相会这样），
        # "我设的蓝色没换上"比"按下时颜色变化小一点"糟糕得多。
        #
        # **实心档不跟着退。** 它解的是另一个问题（白字够不够），与
        # 「三态分不分得开」无关 —— 跟着退的话，用户挑的这个色在主按钮上
        # 仍然是默认主题的蓝，等于实心档对自选色完全失效。
        return replace(base, accent=accent, text_on_accent=toa,
                       term_cursor=accent,
                       # 三态分不开时只退 hover/press，但**图形档不跟着退** ——
                       # 它解的是「看得见」（≥3.0），与「三态分不分得开」无关。
                       # 跟着退的话，用户挑的浅色在侧栏徽标上会看不见。
                       accent_graphic=graphic,
                       accent_solid=solid, accent_solid_hover=solid_hover,
                       accent_solid_press=solid_press, text_on_solid="#ffffff")

    return replace(
        base, accent=accent, accent_hover=hover, accent_press=press,
        accent_soft=soft, text_on_accent=toa,
        accent_graphic=graphic,
        accent_solid=solid, accent_solid_hover=solid_hover,
        accent_solid_press=solid_press, text_on_solid="#ffffff",
        # **`term_cursor` 跟着 accent 走，不是写死成默认蓝。** 终端光标和
        # 焦点环用的是同一个 `accent`，所以换强调色时光标和焦点环一起换 ——
        # 这是**决定**（焦点色就该等于强调色），不是两套行为。
        # （原来这里还并列写着一个 `border_focus=accent`，那个字段零消费者，
        # 已删；它的注释写反了，说代码里写死了蓝。）
        term_cursor=accent)


@lru_cache(maxsize=16)
def _resolve(theme: str, accent: str) -> Palette:
    # **主题名先归一。** 下面 _derive 里是 `theme` 直接当下标用的
    # （_CLAMP[theme] / _SOFT_S[theme]），主题名要是坏的（配置文件里
    # 手写成 "Dark"）就直接 KeyError 崩在启动上——而**没有 accent 时
    # 同一个输入是优雅回退的**，等于 accent 功能凭空引入了一个崩溃回归。
    if theme not in THEMES:
        theme = "dark"
    base = THEMES[theme]
    # 用户把"默认色"重新选了一遍时直接回字面量。推导出来的 text_on_accent
    # 会和写死的不一样（默认深色 accent 的 relL=0.278 > 0.18，推导给黑字，
    # 字面量是白字）——不做这一步，用户什么都没改，按钮文字却白变黑。
    if accent == base.accent.lower():
        return base
    return _derive(theme, base, accent)


def get_palette(theme: str, accent: str = "") -> Palette:
    """按主题取配色；给了 ``accent`` 就把整个强调色家族推导出来。

    ``accent`` 传空 / 非法 / 正好是默认色，一律原样返回字面量常量。
    """
    if not accent or not isinstance(accent, str):
        return THEMES.get(theme, DARK)
    key = accent.strip().lower()
    if not _ACCENT_RE.match(key):
        return THEMES.get(theme, DARK)
    return _resolve(theme, key)


def ansi_palette(theme: str) -> list[str]:
    """16-color ANSI palette tuned per theme so it stays readable."""
    if theme == "light":
        return [
            "#24292f", "#cf222e", "#1a7f37", "#9a6700",
            "#0969da", "#8250df", "#1b7c83", "#6e7781",
            "#57606a", "#a40e26", "#116329", "#7d4e00",
            "#218bff", "#a475f9", "#3192aa", "#8c959f",
        ]
    return [
        "#484f58", "#ff7b72", "#3fb950", "#d29922",
        "#58a6ff", "#bc8cff", "#39c5cf", "#b1bac4",
        "#6e7681", "#ffa198", "#56d364", "#e3b341",
        "#79c0ff", "#d2a8ff", "#56d4dd", "#f0f6fc",
    ]


def xterm256_palette() -> list[str]:
    """The 240 non-system xterm colors (indices 16-255).

    **这份表的生产消费者还没接上，别当成「已经在用」。** 终端里有一份**逐字符
    相同**的拷贝（`terminal_widget._build_xterm_256()`），两份得合并成一份；
    而且实测 pyte 把 ``38;5;N`` 直接交给 ``_color_to_hex`` 的是 6 位十六进制
    串，走的是「原样加 # 返回」那一支，**根本读不到任何表** —— 也就是
    `terminal_widget` 那份拷贝自己也是死的。统一到本函数之前，这里唯一的
    使用者是测试，别据此推断终端的 256 色来自这里。
    """
    colors: list[str] = []
    levels = (0, 95, 135, 175, 215, 255)
    for r in levels:
        for g in levels:
            for b in levels:
                colors.append(f"#{r:02x}{g:02x}{b:02x}")
    for i in range(24):
        v = 8 + 10 * i
        colors.append(f"#{v:02x}{v:02x}{v:02x}")
    return colors


def qcolor(hex_str: str) -> "QtGui.QColor":
    # Qt 只在这一处、而且只在真正被调用时才需要。放在函数体内导入，
    # 是为了让 core/ 真的能被"没有 Qt 的环境"单独测——见 core/__init__.py。
    #
    # **返回注解上的引号是必要的，别让 ruff 剥掉。** `QtGui` 在这个文件里
    # 从来没有模块级 import（它只在函数体内 import），所以注解里的
    # `QtGui.QColor` 是个只在运行期求值才会炸的悬空名。模块顶部的
    # `from __future__ import annotations` 让**普通调用**不求值它，所以现在
    # 跑得起来；但 `typing.get_type_hints()`、dataclass 字段解析、
    # pydantic 之类会实打实去 eval 这条注解，那时就是 NameError。
    # AGENTS.md 把这条列进「外部限定名 3」——它严格说不是 forward ref，
    # 是带点路径，正是这个原因。
    from PyQt6 import QtGui
    return QtGui.QColor(hex_str)


def blend(a: str, b: str, t: float) -> str:
    """Blend two hex colors; *t*=0 returns *a*, *t*=1 returns *b*."""
    from PyQt6 import QtGui
    ca, cb = QtGui.QColor(a), QtGui.QColor(b)
    return QtGui.QColor(
        round(ca.red() * (1 - t) + cb.red() * t),
        round(ca.green() * (1 - t) + cb.green() * t),
        round(ca.blue() * (1 - t) + cb.blue() * t),
    ).name()
