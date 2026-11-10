"""判据：压在**填充色**上的文字/图形，必须按那个填充色自己算黑白。

## 为什么要有这条

`tests/_uia11y.py` 里的「填充色上的文字」那一档，验的是
`palette.text_on()` 这个**函数**选得对不对。

**它验不到产品有没有真的用它。** 而原来的写法恰好就是「没有用它」：

- `monitor_panel.py` 的百分比标签用 `p.text` 画在状态色**填充**上。
  深色主题下 `#e9edf4` 压 `success #3fb950` 只有 **2.163** ——
  三档状态色**全破 AA**；浅色主题也全破（2.72 ~ 2.75）。
- `titlebar.py` 与 `tab_close.py` 的 ✕ 用 `text_on_accent` 画在 `danger` 填充上。
  `text_on_accent` 是按 **accent** 推的，拿它压 `danger` 属于**跨族借用**
  （`design/v1/__init__.py` 的硬约束 1）：浅色主题下它是近黑 `#0d0f14`，
  压 `#C02626` 只有 **3.237**，而同一个填充用白字是 **5.921**。

**跨族借用是最难抓的一类缺陷**：借来的数值不会变差到「有测试会红」，
所以 `palette` 那一层的判据全都绿着。只能**问产品真正会画哪个颜色**。

## 监控条那个标签：为什么它更麻烦

标签是**居中**画在整条上的，而底色随占用率变化 —— **标签会横跨两种底**。
实测（几何结论，与字体无关）：占用率 ≤35% 时标签 **120/120 像素全落在
轨道上**，50% 时一半一半，≥65% 时全落在填充上。

所以「按填充色算一种墨色」在低占用率时是**反的**：深色主题算出的近黑
压轨道只有 **1.272**，标签等于消失 —— 而空闲机器的常态恰恰是低占用率。

现在采用的画法是**标签底下垫一块与轨道同色的胶囊**：标签因此永远压在
一块对比度已知的底（`bg_elevated`）上，**两种占用率下都达标**。

## 这条判据量的是什么

**不是**「代码里有没有出现 `text_on` 这个名字」—— 那是钉字面量，
换一种正确实现就会红。

**是**「产品真正用来画那个字/图形的颜色，压在它实际背景上，对比度够不够」。
两个实现只要画出来一样可读，就都算过。

**而且要真的把像素取出来看** —— 只问「产品会用哪个颜色」不够：
把胶囊去掉、代码一个字不改，那条问答还是绿的，但标签在低占用率时又没了。
所以下面**既问方法、也查像素**。

## 元断言（为什么这不是恒绿）

用**实际踩过的两个错法**跑同一套量法，**必须破线**：

- 老实现：固定 `p.text` 直接画在填充上 → 低占用率没问题，**高占用率破线**；
- 第一版修法：按填充色算墨色 → **低占用率破线**（正是它把标签搞没了）。

两条都指名道姓。抓不到，说明这套量法根本没在量对比度。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6 import QtGui, QtWidgets  # noqa: E402

from sshclient.ui.design.v1.palette import (
    THEMES,
    _contrast,
    _from_hsl,
    _hsl,
    text_on,
)
from sshclient.ui.monitor_panel import Bar  # noqa: E402
from sshclient.ui.tab_close import TabCloseButton  # noqa: E402
from sshclient.ui.titlebar import CLOSE, CaptionButton  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


def ink_in_chip(bar: QtWidgets.QWidget, chip_rect, fill_hex: str):
    """把条渲染出来，取标签**实际画上去的墨色**及其对比度，
    并量**胶囊底下那块底色到底有没有被画出来**。

    **只在胶囊的内区取样**，并取**对比度极值**：

    - 只在胶囊内区：胶囊两端的圆角处会露出底下的填充色，取到那是噪声。
    - 取极值而不是众数：抗锯齿边缘产生一堆混色，每个只占十几个像素，
      众数会取到混色而不是实心笔色；而**混色永远在「底色」与「笔色」
      之间**，所以对比度最大的那个像素就是笔色本身。

    **底色占比这一项是「胶囊真的被画了」的证据。** 只验「墨色对胶囊色的
    对比度」是不够的：把胶囊的绘制删掉、其余代码一个字不改，那条对比度
    照样是绿的（因为它比的是「应该在那儿的底色」），可标签实际上直接
    压在填充上了 —— 那正是「量对了、量错了地方」。

    返回 (墨色, 墨色对胶囊底色的对比度, 胶囊底色在取样区里的占比)。
    """
    img = bar.grab().toImage()
    chip = bar._chip_color().lower()
    fill = fill_hex.lower()
    pad = chip_rect.height() / 2.0        # 避开胶囊两端的圆角
    x0 = int(chip_rect.left() + pad)
    x1 = int(chip_rect.right() - pad)
    y = img.height() // 2
    best, best_c = "", -1.0
    chip_hits = 0
    total = 0
    for x in range(max(0, x0), min(img.width(), x1)):
        name = QtGui.QColor(img.pixel(x, y)).name().lower()
        total += 1
        if name == chip:
            chip_hits += 1
            continue
        if name == fill:
            continue
        c = _contrast(name, chip)
        if c > best_c:
            best, best_c = name, c
    frac = (chip_hits / total) if total else 0.0
    return best, best_c, frac


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

#: **必须覆盖低占用率。** 低占用率是常态，也是第一版修法栽跟头的地方。
FILLS = (20, 50, 80, 100)

print("=" * 68)
print("监控条：标签压在它自己垫的底上")

for theme in ("light", "dark"):
    p = THEMES[theme]
    for name in ("success", "warning", "danger"):
        for pct in FILLS:
            bar = Bar(palette=p)
            bar.resize(220, 22)
            bar.set_value(float(pct), "%d%%   1.2G / 8.0G" % pct)
            app.processEvents()

            ink = bar._label_color()
            chip = bar._chip_color()
            c = _contrast(ink, chip)
            check("%s %s %d%% 监控条标签压在它的底上够 AA"
                  % (theme, name, pct), c >= 4.5,
                  "%s on %s = %.3f (需 4.5)" % (ink, chip, c))

            # **查真正画出来的像素** —— 方法问答绿了不代表画面对。
            fill_hex = QtGui.QColor(bar._color).name()
            chip_rect = bar._label_chip(bar._label or "%d%%" % pct)
            drawn, c_drawn, frac = ink_in_chip(bar, chip_rect, fill_hex)
            check("%s %s %d%% 监控条标签**画出来的墨色**压在它的底上够 AA"
                  % (theme, name, pct), c_drawn >= 4.5,
                  "画出来 %s on %s = %.3f (需 4.5)"
                  % (drawn, chip, c_drawn))
            # **胶囊必须真的被画在标签底下。** 没有这一条，把胶囊删掉
            # （其余代码一个字不改）判据照样是绿的 —— 那条对比度比的是
            # 「应该在那儿的底色」，可标签实际上直接压在填充上了。
            # 取样区里必须有相当比例的胶囊底色才算数。
            check("%s %s %d%% 监控条标签底下**真的垫了胶囊**"
                  % (theme, name, pct), frac >= 0.25,
                  "胶囊底色在取样区只占 %.0f%%（需 ≥25%%）—— 没垫胶囊，"
                  "标签直接压在底上了" % (frac * 100))

print()
print("=" * 68)
print("标题栏 / 标签页关闭键：✕ 压在 danger 填充上")

for theme in ("light", "dark"):
    p = THEMES[theme]

    tb = CaptionButton(kind=CLOSE, palette=p)
    tb._native_hover = True          # 强制走 hover 那一支（填充变 danger）
    fg = tb._fg()
    c = _contrast(fg, p.danger)
    check("%s 标题栏关闭键✕压在 danger 填充上够 AA" % theme, c >= 4.5,
          "%s on %s = %.3f (需 4.5)" % (fg, p.danger, c))
    check("%s 标题栏关闭键✕用的是按 danger 算出来的色" % theme,
          fg.lower() == text_on(p.danger).lower(),
          "用的是 %s，text_on 给的是 %s" % (fg, text_on(p.danger)))

    tc = TabCloseButton(palette=p)
    tc._hover = True
    fg = tc._glyph_color()
    c = _contrast(fg, p.danger)
    check("%s 标签页关闭键✕压在 danger 填充上够 AA" % theme, c >= 4.5,
          "%s on %s = %.3f (需 4.5)" % (fg, p.danger, c))
    check("%s 标签页关闭键✕用的是按 danger 算出来的色" % theme,
          fg.lower() == text_on(p.danger).lower(),
          "用的是 %s，text_on 给的是 %s" % (fg, text_on(p.danger)))

print()
print("=" * 68)
print("元断言：实际踩过的两个错法都必须被这套量法抓到")
#: **错法一（老实现）**：固定 `p.text` 直接画在填充上。
#:   高占用率时标签整段压在填充上 → 破线。
#:
#: **错法二（第一版修法）**：按填充色算墨色（`text_on(fill)`）。
#:   **低占用率时标签整段压在轨道上** → 近黑压轨道 1.272 → 破线。
#:
#: 两条都指名道姓 —— 元断言的形态是「老实现至少在**一批**输入里露馅」，
#: 不是「每条都红」。
for theme in ("light", "dark"):
    p = THEMES[theme]
    for name in ("success", "warning", "danger"):
        fill = getattr(p, name)
        # 错法一：固定 p.text 压在填充上
        c1 = _contrast(p.text, fill)
        check("%s 元断言·错法一：p.text 压在 %s 填充上必破线" % (theme, name),
              c1 < 4.5,
              "%s on %s = %.3f —— 没破线说明量法没在量对比度"
              % (p.text, fill, c1))
        # 错法二：按填充色算的墨色压在**轨道**上（低占用率时标签在那儿）
        c2 = _contrast(text_on(fill), p.bg_elevated)
        check("%s 元断言·错法二：按填充色算的墨色压在轨道上必破线"
              % theme, c2 < 4.5,
              "%s on %s = %.3f —— 没破线说明量法没在量对比度"
              % (text_on(fill), p.bg_elevated, c2))

print()
print("--- 量法本身：构造一个与底色亮度相同的色，它必须被抓出来 ---")
#: 上面那几条都是「拿具体的错法颜色」当对照。这一条与具体实现无关：
#: **构造一个和底色亮度几乎相同的色**（同一个色相，明度扫到对比度最低的
#: 那一档），它必然破线。它证明的是**量法本身**在量对比度，而不是碰巧对。
for theme in ("light", "dark"):
    p = THEMES[theme]
    for bg in ("bg_elevated", "bg_surface"):
        base = getattr(p, bg)
        h, s, _l = _hsl(base)
        worst, worst_c = base, 99.0
        for i in range(0, 101):
            cand = _from_hsl(h, s, i / 100.0)
            c = _contrast(cand, base)
            if c < worst_c:
                worst, worst_c = cand, c
        check("%s 量法自检：与 %s 亮度相同的色必须被抓出来（对比度破线）"
              % (theme, bg), worst_c < 4.5,
              "%s on %s = %.3f —— 没破线说明量法没在量对比度"
              % (worst, base, worst_c))

print()
if FAIL:
    print("FAILED %d:" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
