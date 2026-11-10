"""Regression test: the close button on a session tab must not overlap the
tab bar's end-of-row scroll buttons (``<`` / ``>``).

## 这个 bug 怎么来的

给标签栏两端画上 ``<`` / ``>`` 之后，用户反馈「最右边那个标签的 X 被 ``>``
压了」。旧版 ``sync()`` 用 ``safe_left`` / ``safe_right`` 把按钮的 x 钳进
两端滚动按钮让出来的那条带子里。

## 上一轮改完之后，这条判据的两块地基塌了

改成「位置永远由自己的 tab 决定 + 放不下就隐藏」之后，实测发现：

* **「不重叠」从一个要维护的性质变成了结构上的必然。** 按钮 x 只由
  ``tabRect(i).right()`` 决定，而 ``QTabBar`` 的 ``tabRect`` 从不产出「只
  探出一点」的 tab —— 排在可视区右边的 tab 拿到的是**整个**矩形（实测
  bar.w=980 / 8 个等宽 tab：tab5 右沿 940，tab6 左沿 941 直接跑到 1108）。
  滚到底时也一样：点着 ``<`` ``>`` 扫遍所有滚动态，每个状态下重叠数都是 0。
* **所以那条元断言是死的。** 它靠把 ``_scroll_strip_*`` 打桩成 0 来模拟
  「关掉 clamp」，可新实现里这两个值**只影响显隐、不影响位置**，打桩后
  可见按钮集一个不多一个不少（实测 1280 宽打桩前后都是同样 5 个）。

顺带把 tab 宽度打散（真实会话的主机名长短不一）重扫了一遍——14 个不等宽
tab、56 个滚动态，重叠合计 0。也就是说两端那条扣减在这个应用里目前
**够不着**，但它是防御性的：字号一变 tab 宽度就变（标签文字是按可用宽度
省略的），我没把字体档位全枚举一遍，所以留着。

## 那这条判据还钉什么

两条，替换掉塌掉的那两块：

* **几何不重叠**：可见关闭按钮的 x-range 与任一可见滚动按钮的 x-range
  不相交（gap >= 0）。不下到 ``INSET`` 是因为那是 UX 间距不是回归不变量，
  UI 改一下就无故红。
* **尺子量得准（元断言）**：把一个关闭按钮直接摆到右滚动按钮上，判据必须
  报出重叠。旧元断言想证明的是「这条判据能红」，做法已经失效；直接验
  尺子本身，是同一个目的唯一还成立的做法。

外加一条**不许无声消失**：极端窄栏下一个可见关闭按钮都没有是合理的
（唯一完整可见的欢迎页本来就 ``NO_CLOSE``），但那必须能被几何解释——
凡是「本该看得见却没画」的 tab 都要判红，否则「按钮集体不见了」会被
当成通过。

## 还有一个更重要的性质在别处

不重叠**不蕴含**按钮属于自己的 tab：旧的 clamp 版本就是不重叠却把最右
tab 的 X 贴到了左邻居身上。那条归属恒等式钉在 _tabclosefit.py。
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core import settings
from sshclient.ui import main_window
from sshclient.ui.tab_close import NO_CLOSE, TabCloseButton, TabCloseController

bad = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}"
          + (f"  {detail}" if detail else ""))
    if not ok:
        bad.append(f"{name}  {detail}")


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def settle(ms=200):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


def scroll_buttons(bar):
    """返回 (left, right)；找不到就是 (None, None)。"""
    left = bar.findChild(QtWidgets.QToolButton, "ScrollLeftButton")
    right = bar.findChild(QtWidgets.QToolButton, "ScrollRightButton")
    return left, right


def visible_closes(bar):
    return [w for w in bar.findChildren(TabCloseButton) if w.isVisible()]


def closable_slots(tabs):
    """有资格带关闭按钮的 tab 下标（欢迎页被排除）。"""
    return [i for i in range(tabs.count())
            if tabs.widget(i).property(NO_CLOSE) is not True]


def build(theme, bar_w, names):
    """造一个标签栏宽度确定、标签名给定的 MainWindow。

    MainWindow 自己有 1020×660 的最小尺寸，用 ``setMinimumSize(0, 0)``
    解除，然后再把宿主窗口 resize 到给定宽度。
    """
    tmp = pathlib.Path(tempfile.mkdtemp())
    store = settings.SessionStore(tmp / "s.json")
    store._items = []
    st = settings.AppSettings()
    st.theme = theme
    st.save = lambda: None
    mw = main_window.MainWindow(st, store)
    mw.setMinimumSize(0, 0)
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(mw)
    host.resize(bar_w, 600)
    host.show()
    for n in names:
        mw.tabs.addTab(QtWidgets.QWidget(), n)
    settle(400)
    return mw, host


def _dismiss_modals():
    w = QtWidgets.QApplication.activeModalWidget()
    if w is not None:
        w.close()


_dismiss_timer = QtCore.QTimer()
_dismiss_timer.timeout.connect(_dismiss_modals)
_dismiss_timer.start(50)


# ============================================================ 主断言
INSET = TabCloseController.INSET


def assert_no_overlap(mw, host):
    bar = mw.tabs.tabBar()
    left, right = scroll_buttons(bar)
    closes = visible_closes(bar)
    check("找得到两端滚动按钮",
          left is not None and right is not None,
          f"left={left is not None} right={right is not None} "
          f"closes={len(closes)} bar.w={bar.width()}")

    overflow_right = right is not None and right.isVisible()
    overflow_left = left is not None and left.isVisible()

    for c in closes:
        cr = c.geometry()
        if overflow_right:
            gap = right.geometry().x() - cr.right()
            check(f"close x={cr.x()}.right={cr.right()} "
                  f"不撞右滚动按钮 x={right.geometry().x()} "
                  f"（gap={gap} ≥ 0）",
                  gap >= 0, f"gap={gap}")
        if overflow_left:
            gap = cr.x() - left.geometry().right()
            check(f"close x={cr.x()} 不撞左滚动按钮 right={left.geometry().right()} "
                  f"（gap={gap} ≥ 0）",
                  gap >= 0, f"gap={gap}")

    if not closes:
        _assert_empty_is_explained(mw, bar)


def _assert_empty_is_explained(mw, bar):
    """一个可见关闭按钮都没有时，核对确实是几何决定的隐藏，不是集体消失。

    227px 窄栏就是这种情形：唯一完整可见的 tab 是欢迎页，``NO_CLOSE``，
    本来就没有按钮；其余 tab 的按钮全在可视区右边之外。
    """
    ctrl = mw._tab_close
    slots = closable_slots(mw.tabs)
    bw = ctrl._buttons[0].width() if ctrl._buttons else 0
    vis_l = ctrl._scroll_strip_left()
    vis_r = bar.width() - ctrl._scroll_strip_right()

    unexplained = []
    for i in slots:
        r = bar.tabRect(i)
        x = r.right() - INSET - bw + 1
        if r.isEmpty() or r.right() < vis_l or r.left() > vis_r \
                or x < vis_l or x + bw > vis_r:
            continue                      # 几何上就该被隐藏
        unexplained.append(i)            # 本该看得见却没画 → 真 bug
    check(f"0 个可见 close 是几何决定的（{len(slots)} 个可关闭 tab，"
          f"vis=[{vis_l},{vis_r}]）",
          not unexplained,
          f"本该可见却没画的 tab={unexplained}")


# ============================================================ 滚动态扫描
def sweep_scroll(mw, host, label):
    """点着 ``<`` / ``>`` 扫过所有滚动态，每一步都验一遍不重叠。

    等宽 tab 时 ``QTabBar`` 按整 tab 宽步进滚动，tab 边界始终对齐，扫不出
    「tab 只探出一半」的姿态；真实会话的主机名长短不一，tab 宽度不等，
    滚动态就不再对齐 —— 那才是两端扣减真正承重的地方，所以这轮用的是
    不等宽的 tab 名。
    """
    bar = mw.tabs.tabBar()
    ctrl = mw._tab_close
    total = 0
    for direction in ("right", "right", "left", "left"):
        for _ in range(14):
            left, right = scroll_buttons(bar)
            btn = right if direction == "right" else left
            if btn is None or not btn.isVisible():
                break
            before = [bar.tabRect(i).getRect() for i in range(bar.count())]
            btn.click()
            settle(100)
            ctrl.sync()
            settle(60)
            after = [bar.tabRect(i).getRect() for i in range(bar.count())]
            # 滚到头之后那个按钮还"可见"但点了不动。不看 tab 布局变没变就
            # 继续量，同一个状态会被量 7 遍——那只是把条数撑大，不是多保护了
            # 什么，所以到头就停。
            if after == before:
                break
            total += 1
            right = bar.findChild(QtWidgets.QToolButton, "ScrollRightButton")
            left = bar.findChild(QtWidgets.QToolButton, "ScrollLeftButton")
            for c in visible_closes(bar):
                cr = c.geometry()
                if right is not None and right.isVisible():
                    gap = right.geometry().x() - cr.right()
                    check(f"{label} 滚动态 不撞右（gap={gap} ≥ 0）", gap >= 0)
                if left is not None and left.isVisible():
                    gap = cr.x() - left.geometry().right()
                    check(f"{label} 滚动态 不撞左（gap={gap} ≥ 0）", gap >= 0)
    print(f"  [{'ok ' if total else 'FAIL'}] {label} 扫过 {total} 个滚动态")


# ============================================================ 元断言
def assert_detector_can_go_red(mw, host):
    """把一个关闭按钮直接摆到右滚动按钮上，判据必须报出重叠。

    旧元断言靠打桩 ``_scroll_strip_*`` 来制造重叠，新实现下那条路已经走不通
    （见文件头）。这里改成验尺子本身：重叠真存在时这把尺子量得出来吗。
    摆完立刻量、**不 settle**——settle 会触发 paint 里的 ``sync()`` 把按钮
    摆回原位，元断言就自己把自己抹平了。
    """
    bar = mw.tabs.tabBar()
    right = bar.findChild(QtWidgets.QToolButton, "ScrollRightButton")
    closes = visible_closes(bar)
    if right is None or not right.isVisible() or not closes:
        check("元断言前置条件：右滚动按钮可见且有可见 close",
              False, f"right_vis={right is not None and right.isVisible()} "
                     f"closes={len(closes)}")
        return
    victim = closes[-1]
    keep = victim.geometry()
    try:
        victim.move(right.geometry().x() - 2, victim.y())
        gap = right.geometry().x() - victim.geometry().right()
        check(f"元断言：把 close 摆到右滚动按钮上 gap 必须为负（gap={gap}）",
              gap < 0)
        hits = sum(1 for c in visible_closes(bar)
                   if c.geometry().right() > right.geometry().x())
        check(f"元断言：主判据的重叠统计此时应 > 0（hits={hits}）", hits > 0)
    finally:
        victim.move(keep.x(), keep.y())


# ============================================================ suite
EQUAL = [f"root@10.0.0.{i}:22" for i in range(7)]
#: 长度差异大到 tab 宽度从 67px 到 383px，逼出不对齐的滚动态
VARIED = [
    "a", "ab", "abc", "abcd", "root@10.0.0.1", "root@10.0.0.22",
    "user@very-long-production-hostname.example.com",
    "root@10.0.0.100", "x", "deploy@staging-eu-west-1.internal",
    "root@10.0.0.2", "ops@jump-host", "n",
]

for theme in ("light", "dark"):
    print(f"\n================ {theme} ================")

    # 1280 宽度的普通窗口：溢出右侧
    mw, host = build(theme, 1280, EQUAL)
    bar = mw.tabs.tabBar()
    print(f"  bar.w={bar.width()}  tabs={bar.count()}")
    assert_no_overlap(mw, host)
    assert_detector_can_go_red(mw, host)
    mw.close()
    host.close()

    # 更窄的窗口：标签更挤，确保不是只在某宽度下不撞
    mw, host = build(theme, 900, EQUAL)
    bar = mw.tabs.tabBar()
    print(f"  bar.w={bar.width()}  tabs={bar.count()}")
    assert_no_overlap(mw, host)
    mw.close()
    host.close()

    # 极端窄度：唯一完整可见的是欢迎页，没有 close 按钮
    mw, host = build(theme, 500, EQUAL)
    bar = mw.tabs.tabBar()
    print(f"  bar.w={bar.width()}  tabs={bar.count()}")
    check("极端宽度下 sync() 不抛", True)
    assert_no_overlap(mw, host)
    mw.close()
    host.close()

    # 不等宽 tab + 滚动态扫描
    mw, host = build(theme, 1280, VARIED)
    bar = mw.tabs.tabBar()
    print(f"  bar.w={bar.width()}  tabs={bar.count()}  "
          f"宽度={[bar.tabRect(i).width() for i in range(bar.count())]}")
    assert_no_overlap(mw, host)
    sweep_scroll(mw, host, "不等宽")
    mw.close()
    host.close()

_dismiss_timer.stop()
QtCore.QCoreApplication.sendPostedEvents(
    None, QtCore.QEvent.Type.DeferredDelete)

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("close vs scroll button overlap: all checks passed")
sys.exit(1 if bad else 0)
