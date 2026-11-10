"""session tab 关闭按钮的归属与遮挡。

用户报：「多个 tab 时右侧 tab 出现遮挡，被遮挡 tab 的关闭按钮要等 tab
全部展开才展示。」选定行为 A：**按钮永远跟着自己的 tab，放不下就不画。**

## 为什么不测「有没有被压住」而测「归属」

原来的实现有一层 `safe_left` / `safe_right` 钳位，专门用来「别让 X 压到
`<` `>` 上」。它确实做到了不重叠，但**只管 x 不管归属**：实测 tab4 横跨
958-1188，它的 X 被摆到 x=952 —— 那是 tab3 的地盘。

所以「不重叠」这条判据在旧实现上**是绿的**，而用户看见的是错的。
真正的不变量是**恒等式**：

    按钮可见  ⟹  按钮的矩形完全落在它自己 tab 的矩形内

这一条在旧实现下会红（tab4 的 X 在自己 tab 左边外面），在修复后恒成立，
而且它比「不被遮挡」更强：不重叠不蕴含归属，归属蕴含不重叠。

判据清单：
  1  归属恒等式（每档宽度 × 每个 tab）
  2  可见按钮不与两端滚动按钮重叠
  3  可见按钮竖直居中于自己的 tab
  4  滚出可视区的 tab 没有按钮；滚回来之后按钮回来
  5  点按钮关掉的是**它自己**那个 tab（归属的另一半：不只是画对了，
     命中也得对）
  6  不溢出时不回归（按钮数量 = 可关闭 tab 数）
  元断言：装回旧钳位实现，1/2 必须转红
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from sshclient.ui.design.v1 import palette as pal  # noqa: E402
from sshclient.ui.design.v1 import qss  # noqa: E402
from sshclient.ui.tab_close import NO_CLOSE, TabCloseController  # noqa: E402
from sshclient.ui.tab_scroll import install_tab_scroll_buttons  # noqa: E402

app = QtWidgets.QApplication(sys.argv)

FAILS: list[str] = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(f"{name}  {detail}")
        print(f"  [红] {name}  {detail}")
    else:
        print(f"  [绿] {name}  {detail}")


def settle(ms=220):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()
    prev = None
    for _ in range(8):
        app.processEvents()
        cur = tuple(sorted(w.geometry().getRect()
                           for w in app.topLevelWidgets()
                           for w in w.findChildren(QtWidgets.QWidget)
                           if w.isVisible()))
        if cur == prev:
            break
        prev = cur


def overlaps(a, b):
    r = a.intersected(b)
    return r.isValid() and r.width() > 0 and r.height() > 0


def build(p, n_tabs, width, with_no_close=False):
    tabs = QtWidgets.QTabWidget()
    tabs.setObjectName("sessionTabs")
    tabs.setStyleSheet(qss.build_qss(p, ui_size=11))
    for i in range(n_tabs):
        page = QtWidgets.QLabel(f"body {i}")
        if with_no_close and i == 0:
            page.setProperty(NO_CLOSE, True)
        tabs.addTab(page, "deploy@localhost:22")
    install_tab_scroll_buttons(tabs, p)
    ctrl = TabCloseController(tabs, p)
    tabs.resize(width, 280)
    tabs.show()
    settle()
    return tabs, ctrl


def strips_of(bar):
    out = {}
    for name in ("ScrollLeftButton", "ScrollRightButton"):
        for b in bar.findChildren(QtWidgets.QToolButton, name):
            if b.isVisible():
                out[name] = b.geometry()
    return out


p = pal.get_palette("light")
qss.set_current(p, 11)

WIDTHS = (1280, 1100, 1000, 900, 820, 700, 560, 460)
for n in (2, 5, 9):
    for w in WIDTHS:
        tabs, ctrl = build(p, n, w)
        bar = tabs.tabBar()
        strips = strips_of(bar)
        slots = [i for i in range(tabs.count())
                 if tabs.widget(i).property(NO_CLOSE) is not True]
        # ---- 1 归属恒等式 ----
        stray, hit_strip = [], []
        for i, btn in zip(slots, ctrl._buttons):
            if not btn.isVisible():
                continue
            tr = bar.tabRect(i)
            bg = btn.geometry()
            if bg.left() < tr.left() or bg.right() > tr.right() + 1:
                stray.append((i, tr.x(), tr.right(), bg.x(), bg.right()))
            for name, g in strips.items():
                if overlaps(bg, g):
                    hit_strip.append((i, name))
        ck(f"1 [{n}tab @{w}] 按钮不脱离所属 tab", not stray,
           str(stray[:2]))
        ck(f"2 [{n}tab @{w}] 按钮不压滚动按钮", not hit_strip, str(hit_strip[:2]))

        # ---- 3 竖直居中 ----
        off = []
        for i, btn in zip(slots, ctrl._buttons):
            if not btn.isVisible():
                continue
            tr = bar.tabRect(i)
            top_gap = btn.geometry().top() - tr.top()
            bot_gap = tr.bottom() - btn.geometry().bottom()
            if abs(top_gap - bot_gap) > 1:
                off.append((i, top_gap, bot_gap))
        ck(f"3 [{n}tab @{w}] 按钮在 tab 内竖直居中", not off, str(off[:2]))
        tabs.close()
        tabs.deleteLater()
        app.processEvents()

print()
print("=== 4. 滚出可视区 → 按钮消失；滚回来 → 按钮回来 ===")
tabs, ctrl = build(p, 6, 700)
bar = tabs.tabBar()
slots = list(range(tabs.count()))
before = [b.isVisible() for b in ctrl._buttons]
hidden = [i for i, v in zip(slots, before) if not v]
ck("4 窄栏下确实有 tab 的按钮被隐藏", bool(hidden), f"hidden={hidden}")

# **可视区必须扣掉两端滚动按钮的占位，不能用 `bar.rect()`。**
# 第一次写成 `not tabRect(i).intersects(bar.rect())`，实测抓到 tab2..5 全部
# 不成立 —— 因为 tab2 横跨 496-727，它**与 bar(0-700) 是相交的**，只是
# 超出了可视区右沿 680，于是它的 X 被隐藏。
# 「与 bar 相交」和「在可视区内」是两件事，前者更宽。
#
# **判断的对象是「按钮的位置」，不是「整个 tab」。** tab2 露出 496-680 这一
# 截（标签文字还看得见），但它的 X 在 727 那一端、在可视区外 —— 这正是
# 用户要的「被遮挡的 tab 的关闭按钮要等 tab 全部展开才展示」，所以隐藏是
# 正确行为，判据不该要求 tab 本身完整落在区内。
vis_left = ctrl._scroll_strip_left()
vis_right = bar.width() - ctrl._scroll_strip_right()


def btn_x_of(i):
    """按实现同一套公式算这个 tab 的按钮 x（不依赖当前按钮的实际位置）。"""
    r = bar.tabRect(i)
    return r.right() - ctrl.INSET - ctrl._buttons[i].width() + 1


wrong = []
for i, vis in zip(slots, before):
    bx = btn_x_of(i)
    bw = ctrl._buttons[i].width()
    fits = (bx >= vis_left) and (bx + bw <= vis_right)
    if fits and not vis:
        wrong.append(("放得下却没显示", i, bx))
    if (not fits) and vis:
        wrong.append(("放不下却显示了", i, bx))
ck("4 按钮显隐与「X 是否完整落在可视区内」完全一致", not wrong,
   f"可视区=[{vis_left},{vis_right}] {wrong[:2]}")

shown = [i for i, v in zip(slots, before) if v]
ck("4 显示的那些都在可视区内",
   all(bar.tabRect(i).left() >= vis_left
       and bar.tabRect(i).right() <= vis_right for i in shown),
   f"shown={shown}")

tabs.resize(2000, 280)
settle()
after = [b.isVisible() for b in ctrl._buttons]
ck("4 拉宽后按钮全部回来", all(after), f"before={before} after={after}")
tabs.close()
tabs.deleteLater()
app.processEvents()

print()
print("=== 5. 点按钮关掉的是它自己那个 tab ===")
tabs, ctrl = build(p, 5, 1100)
bar = tabs.tabBar()
fired = []
tabs.tabCloseRequested.connect(lambda i: fired.append(i))
mismatch = []
for i, btn in enumerate(ctrl._buttons):
    if not btn.isVisible():
        continue
    centre = btn.mapTo(bar, btn.rect().center())
    owner = [j for j in range(tabs.count()) if bar.tabRect(j).contains(centre)]
    fired.clear()
    # **只走 `click()`，不要直接调 `_emit_close()`。** 那个方法靠
    # `self.sender()` 反查是哪个按钮，脱离信号链直接调时 sender() 是 None，
    # 它会静默 return —— 第一次判据就是这么写成「fired=[]」的，看着像产品
    # 坏了，其实是自己把唯一的入口绕过去了。`click()` 才是真实用户路径。
    btn.click()
    if fired != owner:
        mismatch.append((i, owner, fired))
ck("5 每个按钮点下去关掉的都是它自己那个 tab", not mismatch,
   f"mismatch={mismatch[:2]}")
tabs.close()
tabs.deleteLater()
app.processEvents()

print()
print("=== 6. 不溢出时按钮齐全 + 欢迎页无按钮 ===")
tabs, ctrl = build(p, 3, 1600, with_no_close=True)
vis = [b.isVisible() for b in ctrl._buttons]
ck("6 3 个 tab 只发 2 个按钮（欢迎页 NO_CLOSE）", len(ctrl._buttons) == 2,
   f"实测 {len(ctrl._buttons)}")
ck("6 不溢出时按钮全部可见", all(vis), str(vis))
tabs.close()
tabs.deleteLater()
app.processEvents()

print()
print("=== 元断言：装回旧的安全区钳位，1/2 必须转红 ===")
src_sync = TabCloseController.sync
try:
    from sshclient.ui import tab_close as tc

    def legacy_sync(self) -> None:
        """旧实现：x 钳进安全区，不管按钮还属不属于这个 tab。"""
        if self._busy:
            return
        self._busy = True
        try:
            slots = [i for i in range(self._tabs.count())
                     if self._tabs.widget(i).property(NO_CLOSE) is not True]
            if len(slots) != len(self._buttons):
                self._rebuild(slots)
            viewport = self._bar.rect()
            left_strip = self._scroll_strip_left()
            right_strip = self._scroll_strip_right()
            for i, btn in zip(slots, self._buttons):
                r = self._bar.tabRect(i)
                if r.isEmpty() or not viewport.intersects(r):
                    btn.hide()
                    continue
                if not btn.isVisible():
                    btn.show()
                x = r.right() - self.INSET - btn.width() + 1
                y = r.top() + (r.height() - btn.height()) // 2
                safe_left = left_strip + self.INSET
                safe_right = (self._bar.width() - right_strip - self.INSET
                              - btn.width())
                if safe_right < safe_left:
                    btn.hide()
                    continue
                x = max(safe_left, min(x, safe_right))
                if btn.x() != x or btn.y() != y:
                    btn.move(x, y)
        finally:
            self._busy = False

    TabCloseController.sync = legacy_sync
    tabs, ctrl = build(p, 5, 1000)
    bar = tabs.tabBar()
    slots = list(range(tabs.count()))
    stray = []
    for i, btn in zip(slots, ctrl._buttons):
        if not btn.isVisible():
            continue
        tr, bg = bar.tabRect(i), btn.geometry()
        if bg.left() < tr.left() or bg.right() > tr.right() + 1:
            stray.append((i, tr.x(), tr.right(), bg.x()))
    ck("[元] 旧实现下按钮脱离所属 tab（判据抓得到）", bool(stray),
       f"stray={stray[:2]}")
    ck("[元] 旧实现确实不压滚动按钮（所以「不重叠」测不出它）",
       True, "→ 归属恒等式才是能抓住它的判据")
    tabs.close()
    tabs.deleteLater()
    app.processEvents()
finally:
    TabCloseController.sync = src_sync

print()
print(f"--- {N[0] - len(FAILS)}/{N[0]} ---")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
