"""判据：配色 token 的对比度必须达标，而且**每一对都要在它真正挨着的底色上量**。

## 为什么要有这条

2026-10 一轮 UI 审计里翻出过两件事，都属于「量对了、量错了地方」：

1. **对家配错了。** 有个脚本把 `text_on_solid`（实心按钮上的**白字**）
   拿去和 `bg_base`（页面底色）比，得出「对比度 1.05，惨不忍睹」。
   实心按钮的底是 `accent_solid`，真值 **4.54，完全达标**。这条假警报
   差点让人去改一个没坏的配色。
2. **只量了主区，漏了弹框。** `danger` 在 `bg_base` 上是 5.4，达标；
   而危险文字大量出现在**弹框**里，`bg_dialog` 比主区亮一档 —— 同一个
   `danger` 在那儿只有 **4.40（浅）/ 4.46（深）**，两条都破 AA。

第 2 条已经修掉（`danger` 压到 `#C02626` / 提到 `#f35555`），但**修完不钉
判据就等于没修**：下一个人调色时很容易只对着主区看。

所以这张表里**每个 token 都对每一个它会挨着的底色各量一遍**，而不是只量
一个代表值。

## 2026-10 第二轮：这张表自己被查出「量错了地方」

**上一轮那条「危险文字大量出现在弹框里」的前提是错的。** 全仓唯一给 label
设 color 属性的地方是 `main_window.py` 的状态文字，它挂在 SessionTab 工具栏
上（底色 `bg_base`）——**弹框里根本没有 danger 文字**，弹框的危险表现是
Qt 标准的 Critical 图标，走 style 自己的颜色，不是本项目的 token。

于是上一轮那行 `danger/bg_dialog`（4.5 文字档）**量了一对永不相邻的底色**，
而**真正会破线的那一对（`danger` 压在 `bg_hover_row` 上，4.428）根本没人量**。

这一轮把整张表按**真实使用点**重建：

| 档 | 门槛 | 真实邻接 |
|---|---|---|
| 文字 | 4.5 | `bg_base`（状态标签）/ `bg_surface`（传输条标题、磁盘表百分比）/ `bg_hover_row`（磁盘表那一行被悬停时） |
| 图形 | 3.0 | `bg_elevated`（监控条/传输条进度填充）/ `bg_dialog`（弹框标题栏关闭键 hover 填充）/ `bg_surface`（窗口标题栏关闭键、传输条 3px 色条）/ `bg_base`（状态圆点） |

**每加一行之前先问「界面上这两种颜色真的上下叠在一起吗」，答不上来就别加。**

## 元断言（为什么这不是恒绿）

**逐对构造。** 对每一对 `(fg, bg, need)`，**用该对自己的底色**构造一个
必然破线的对照值（朝底色方向推），断言「构造值在这一对上是破线的」且
「现值在这一对上是达标的」。

**为什么必须逐对。** 原来只有一个共用的构造值（锚在 `bg_dialog` 上），
用它去量所有底色 —— 结果**新增的那一行恰好抓不到**：那个构造值在
`bg_surface` 上仍然是达标的，于是「元断言存在」与「那一行真的被管着」
是两回事。**元断言自己必须能覆盖到它要守的那一对**，否则它是装饰。

**外加一条反向对照**：现值在每一对上都必须仍然达标 —— 少了它，这张表
可能整体恒红，看着"抓到了"其实量错了对家。

## 填充上的文字（`text_on` 档）

状态色**当填充用**时（监控条进度、标题栏关闭键、传输条），压在它上面的
那个字/图形必须**按填充色自己算**黑白 —— 这就是 `palette.text_on()`。

这一档单列，因为它是**绘制顺序问题**：token 对 token 的表装不下它，
而且原来的写法（借用 `text_on_accent`）**不会让任何数值变差到「有测试会红」**，
所以只能靠把「填充色上那行字实际用了什么色」算出来才知道。

跑法::

    .\\.venv\\Scripts\\python.exe tests\\_uia11y.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshclient.ui.design.v1 import palette
from sshclient.ui.design.v1.palette import THEMES, _contrast, _from_hsl, _hsl, text_on

bad = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}"
          + (f"  {detail}" if detail else ""))
    if not ok:
        bad.append(name + "  " + detail)


#: (说明, 前景 token 名, 背景 token 名, WCAG 需求)
#:
#: **每一条都必须是对真的会挨在一起的两个底色。** 加一行之前先问
#: 「界面上这两种颜色真的上下叠在一起吗」，答不上来就别加 ——
#: 上一轮那个 1.05 的假警报就是拿一对永不相邻的颜色算出来的。
#:
#: **上一轮这张表里写错了一条前提，本轮更正。** 当时写的是「`accent` 只
#: 出现在主窗口，`bg_dialog` 上永远不会出现 accent 图形」，并据此删掉了对应
#: 检查行。**两处都错**：
#: 1. 清单漏了 `sidebar.py:98` / `sidebar.py:264`（侧栏认证图标）——
#:    而它们才是浅色下真实不达标的那两处。
#: 2. `titlebar.py:740` 明写对话框标题栏底是 `bg_dialog`，`:748` 的图标
#:    就压在上面 —— 弹框里**确实有** accent 图形，实测 2.67。
#:
#: 教训和上一轮那条假警报是同一个：**写判据之前先 grep 核实 token 的真实
#: 用途，不要凭印象写「它只出现在……」。** 参见 `_accent.py` 的契约 6~9。
#:
#: **再下一轮又更正了一条**：当时写「危险文字大量出现在弹框里」，
#: 据此立了 `danger/bg_dialog`（文字档）。那个前提**是错的** —— 弹框里
#: 没有 danger 文字（Qt 标准 Critical 图标走 style 自己的颜色）。
#: 于是那一行量了一对永不相邻的底色，而真正会破线的
#: `danger` / `bg_hover_row`（4.428）**没人量**。见文件头的说明。
PAIRS = [
    # 实心主按钮：三档底色都要撑得住白字
    ("实心按钮文字/常规", "text_on_solid", "accent_solid", 4.5),
    ("实心按钮文字/悬停", "text_on_solid", "accent_solid_hover", 4.5),
    ("实心按钮文字/按下", "text_on_solid", "accent_solid_press", 4.5),
    ("强调块文字/accent", "text_on_accent", "accent", 4.5),
    # 正文 / 次要 / 弱化：主区、弹框、控件面三种底都要过
    ("正文/bg_base", "text", "bg_base", 4.5),
    ("正文/bg_dialog", "text", "bg_dialog", 4.5),
    ("正文/bg_elevated", "text", "bg_elevated", 4.5),
    ("次要/bg_base", "text_dim", "bg_base", 4.5),
    ("次要/bg_dialog", "text_dim", "bg_dialog", 4.5),
    ("次要/bg_elevated", "text_dim", "bg_elevated", 4.5),
    # text_muted 是弱化文字（占位符、辅助说明），走大字号 AA 的 3:1
    ("弱化/bg_base", "text_muted", "bg_base", 3.0),
    ("弱化/bg_dialog", "text_muted", "bg_dialog", 3.0),
    # 焦点环画在 `accent_solid` 上（不是 `accent`）—— 那四档见 qss.py 注释
    ("焦点环accent_solid/bg_base", "accent_solid", "bg_base", 3.0),
    ("焦点环accent_solid/bg_dialog", "accent_solid", "bg_dialog", 3.0),
    # **状态色当「文字」用时的真实邻接。**
    #
    #   bg_base       状态标签（`qss.py` 的 stateText，主区/侧栏两处）
    #   bg_surface    传输条标题（transferTitle）、磁盘表百分比（静置）
    #   bg_hover_row  磁盘表那一行被鼠标悬停时（`qss.py` 的 item:hover）
    #
    # **不含 `bg_dialog`** —— 弹框里没有状态色**文字**（Critical 走 Qt 标准
    # 图标，用 style 自己的颜色）。状态色在弹框里只作**图形填充**，
    # 见下面的图形档。
    # **不含 `bg_elevated`** —— 状态色在那儿只作**图形填充**（进度条），
    # 见图形档。
    #
    # **`success` 只有 `bg_base` 一行**：它没被用在传输条标题上
    # （`qss.py` 只给 `tone="error"` 的 transferTitle 上色），也没被用在
    # 磁盘表前景上（那里的 else 分支是 `p.text`）。多写一行不违反
    # 「只量真邻接」，但会让这张表假装它比实际覆盖得全。
    ("success/bg_base", "success", "bg_base", 4.5),
    ("warning/bg_base", "warning", "bg_base", 4.5),
    ("warning/bg_surface", "warning", "bg_surface", 4.5),
    ("warning/bg_hover_row", "warning", "bg_hover_row", 4.5),
    ("danger/bg_base", "danger", "bg_base", 4.5),
    ("danger/bg_surface", "danger", "bg_surface", 4.5),
    ("danger/bg_hover_row", "danger", "bg_hover_row", 4.5),
]

#: **图形档。** WCAG 1.4.11 非文本对比度 3:1。
#:
#: 背景这一列**只放"静止压在某个底色上的图形"**，每个都对应真实使用点：
#:
#:   accent_soft   sidebar.py:98  侧栏徽标胶囊（key/user 认证图标）
#:   bg_elevated   sidebar.py:264 侧栏折叠行图标
#:   bg_dialog     titlebar.py:748 对话框标题栏图标
#:   bg_surface    titlebar.py:686 窗口标题栏图标 / sftp_panel.py:922 目录图标
#:                 / tunnel_panel.py:64 隧道开关轨
#:   bg_rail       main_window.py:1929 侧栏品牌小标
#:
#: **故意不含 `bg_hover_row` / `bg_active` / `bg_hover_ctrl`。** 这三个是
#: **状态底**，它们对页面底**自己**就只有 2.27 / 2.27 / 2.50，达不到 3:1 ——
#: hover 反馈靠的正是这点差值。要求停在它们上面的图形也够 3:0，等于要求底和
#: 图形同时再压深一档，那会把 hover 反馈本身抹掉。
#: 而且图形**确实不会**停在上面：侧栏徽标只有「选中 `accent_soft`」「未选中
#: `bg_elevated`」两态，hover 是瞬态且不改徽标底色（`sidebar.py:284`）。
GRAPHIC_PAIRS = [
    ("侧栏徽标胶囊", "accent_graphic", "accent_soft", 3.0),
    ("侧栏折叠行图标", "accent_graphic", "bg_elevated", 3.0),
    ("对话框标题栏图标", "accent_graphic", "bg_dialog", 3.0),
    ("窗口标题栏图标/SFTP/开关轨", "accent_graphic", "bg_surface", 3.0),
    ("侧栏品牌小标", "accent_graphic", "bg_rail", 3.0),
]

#: **状态色当「图形/填充」用时的真实邻接。** 同样走 1.4.11 的 3:1。
#:
#:   bg_elevated   监控条进度填充（`monitor_panel.py` 的条，轨底也是它）、
#:                 传输条 SlimBar 的填充（轨底同样是它）
#:   bg_dialog     **弹框标题栏关闭键 hover 时的填充**
#:                 （`titlebar.py` 给弹框装的 TitleBar 底色是 `bg_dialog`，
#:                 关闭键 hover 时整块变 `danger`）
#:   bg_surface    主窗口标题栏关闭键 hover 的填充、传输条左侧 3px 色条、
#:                 标签页关闭按钮的填充
#:   bg_base       状态圆点（8×8）
#:
#: **这些是图形，不是文字** —— 所以门槛是 3.0 而不是 4.5。
#: 上一轮把它们当文字量（`danger/bg_elevated` 4.5），既量错了档位，
#: 又量错了对家（`danger` 从不作为文字压在那里）。
STATUS_GRAPHIC_PAIRS = [
    ("监控条/传输条填充", "danger", "bg_elevated", 3.0),
    ("监控条/传输条填充", "success", "bg_elevated", 3.0),
    ("监控条/传输条填充", "warning", "bg_elevated", 3.0),
    ("弹框标题栏关闭键hover", "danger", "bg_dialog", 3.0),
    ("窗口标题栏关闭键hover/传输条色条", "danger", "bg_surface", 3.0),
    ("传输条色条", "success", "bg_surface", 3.0),
    ("状态圆点", "danger", "bg_base", 3.0),
]

#: **填充色上的文字/图形。** 状态色当填充用时，压在它上面的那个 ✕ 或
#: 百分比必须**按填充色自己算**黑白（`palette.text_on()`）。
#:
#: 这一档单列，因为它不是「token 对 token」而是「绘制顺序」：
#: 借用 `text_on_accent`（按 accent 推的）**不会让数值变差到「有测试会红」**，
#: 所以没有别的地方能抓到它 —— 见 `design/v1/__init__.py` 的硬约束 1。
FILL_TEXT_PAIRS = [
    ("监控条标签/danger", "danger"),
    ("监控条标签/success", "success"),
    ("监控条标签/warning", "warning"),
    ("标题栏✕/danger", "danger"),
]


def _make_bad(fg: str, bg: str, need: float) -> str:
    """构造一个在 ``bg`` 上必然破线的对照值。

    **每一对 (fg, bg) 各构造各的。** 用一个共用的构造值去量所有底色，
    会出现「它恰好在某一对上不破线」—— 那时元断言还在、那一行却没人管，
    正是「量错了对家」的又一种形态。

    方向按「朝底色那边走」定，而不是拍一个方向：深色底上把浅色调亮，
    对比度反而**涨**（离底色越远对比度越高）。
    """
    h, s, l = _hsl(fg)
    bg_l = _hsl(bg)[2]
    d = +1 if bg_l > l else -1
    cand = fg
    for i in range(0, 120):
        cand = _from_hsl(h, s, max(0.0, min(0.97, l + d * 0.01 * i)))
        if _contrast(cand, bg) < need:
            return cand
    # 第一条路走不通（对比度没随明度单调下降）就换方向再试一遍。
    for i in range(0, 120):
        cand = _from_hsl(h, s, max(0.0, min(0.97, l - d * 0.01 * i)))
        if _contrast(cand, bg) < need:
            return cand
    return cand


print("=" * 68)
print("配色 token 对比度（WCAG 2.1 相对亮度公式）")
for theme in ("light", "dark"):
    p = THEMES[theme]
    print(f"\n--- {theme} ---")
    for desc, fg_name, bg_name, need in PAIRS:
        fg = getattr(p, fg_name)
        bg = getattr(p, bg_name)
        c = _contrast(fg, bg)
        check(f"{theme} {desc}", c >= need,
              f"{fg} on {bg} = {c:.3f} (需 {need})")
    for desc, fg_name, bg_name, need in GRAPHIC_PAIRS:
        fg = getattr(p, fg_name)
        bg = getattr(p, bg_name)
        c = _contrast(fg, bg)
        check(f"{theme} 图形·{desc}", c >= need,
              f"{fg} on {bg} = {c:.3f} (需 {need})")
    for desc, fg_name, bg_name, need in STATUS_GRAPHIC_PAIRS:
        fg = getattr(p, fg_name)
        bg = getattr(p, bg_name)
        c = _contrast(fg, bg)
        check(f"{theme} 状态图形·{desc}", c >= need,
              f"{fg} on {bg} = {c:.3f} (需 {need})")

print("\n" + "=" * 68)
print("填充色上的文字：必须按填充色自己算黑白")
#: `text_on()` 是 `palette` 里公开的那条黑白择优规则的入口。
#: 这一档验的是**填充色上那个字实际用了什么色**。
for theme in ("light", "dark"):
    p = THEMES[theme]
    for desc, fill_name in FILL_TEXT_PAIRS:
        fill = getattr(p, fill_name)
        fg = text_on(fill)
        c = _contrast(fg, fill)
        check(f"{theme} {desc}：填充上的文字达标", c >= 4.5,
              f"{fg} on {fill} = {c:.3f} (需 4.5)")

print("\n" + "=" * 68)
print("元断言 1：每一对都必须有区分度（逐对构造，不是共用一个对照值）")
#: 判据必须真的会因为**这一对**的前景色变差而红掉，否则那几行是恒绿的装饰。
#:
#: **逐对构造**：对每一对 `(fg, bg, need)` 单独造一个必然破线的值。
#: 原来只有一个共用的构造值（锚在 `bg_dialog` 上），用它去量所有底色时，
#: **新增的那一行恰好抓不到** —— 那个值在 `bg_surface` 上仍然达标。
for theme in ("light", "dark"):
    p = THEMES[theme]
    for table_name, table in (("文字", PAIRS),
                               ("图形", GRAPHIC_PAIRS),
                               ("状态图形", STATUS_GRAPHIC_PAIRS)):
        for desc, fg_name, bg_name, need in table:
            fg = getattr(p, fg_name)
            bg = getattr(p, bg_name)
            bad_fg = _make_bad(fg, bg, need)
            c_bad = _contrast(bad_fg, bg)
            check(f"{theme} {table_name}·{desc}：构造值在这一对上是破线的",
                  c_bad < need,
                  f"{bad_fg} on {bg} = {c_bad:.3f} vs {need}")
            # 反向：现值在这一对上必须达标 —— 少了它，这张表可能整体恒红，
            # 看着"抓到了"其实量错了对家。
            c_now = _contrast(fg, bg)
            check(f"{theme} {table_name}·{desc}：现值在这一对上是达标的",
                  c_now >= need,
                  f"{fg} on {bg} = {c_now:.3f}")

print("\n" + "=" * 68)
print("元断言 2：填充上的文字必须真的靠择优才达标（不是恒定白字或恒定黑字）")
#: 把 `text_on()` 换成一个**常量**（恒白或恒近黑），这条判据必须会红。
#:
#: 这一条比「构造一个违规值」更直接：它验的是**黑白择优这个动作本身**
#: 在起作用。六个状态色上，「选错那一侧」全部破线 ——
#:   深色 success/warning/danger 压近黑 7.5 / 7.6 / 5.9，压白字只有
#:     2.5 / 2.5 / 3.2；浅色反过来，压白字 6.0 / 6.0 / 5.9，
#:     压近黑只有 3.2 / 3.2 / 3.2。
for theme in ("light", "dark"):
    p = THEMES[theme]
    for desc, fill_name in FILL_TEXT_PAIRS:
        fill = getattr(p, fill_name)
        chosen = text_on(fill)
        other = "#ffffff" if chosen == "#0d0f14" else "#0d0f14"
        c_other = _contrast(other, fill)
        check(f"{theme} {desc}：选错黑白必须破线（证明择优在起作用）",
              c_other < 4.5,
              f"{other} on {fill} = {c_other:.3f} < 4.5")
        check(f"{theme} {desc}：择优选中的这一侧达标",
              _contrast(chosen, fill) >= 4.5,
              f"{chosen} on {fill} = {_contrast(chosen, fill):.3f}")

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("contrast: all checks passed")
sys.exit(1 if bad else 0)
