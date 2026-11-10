"""标签栏两端的滚动按钮：画上 `<` / `>`，并且**不许弄坏它原本的滚动**。

用户报「连接的远程服务器较多时，标签栏两端出现的按钮要分别换成 `<` `>`」。

## 那些按钮本来就是空的

QTabBar 在标签放不下时，两端各留一条 `QToolButton`（objectName 是
``ScrollLeftButton`` / ``ScrollRightButton``）。QSS 里
``QTabBar { qproperty-drawBase: 0; background: transparent; }`` 让标签栏走
``QStyleSheetStyle``，而它**不画这两个子控件** —— 原位留下两个空方块，用户
截图里红框圈的正是它们。所以这不是换图标，是补上从没画过的那个东西。

## 为什么接管绘制、而不是换成自定义控件

PyQt6 里换控件那条路实测是死的，三条都记在 ``tab_scroll.py`` 顶部：

1. ``QTabBar.ButtonPosition`` 只暴露 ``LeftSide``/``RightSide``；用整数值构造
   无名枚举成员传进 ``setTabButton``，**静默不生效**（第一次探针里量到的 20px
   让位其实是原生按钮自己留的，不是新控件 —— 差点据此得出错误结论）。
2. ``QTabBar.scrollLeft()`` / ``scrollRight()`` 在 PyQt6 里**不存在**；名字叫
   ``scroll`` 的那个是 ``QWidget.scroll(dx, dy)``。
3. 换上自定义控件后，Qt 那根「``clicked()`` → ``scrollLeft()``」的连接随原生
   控件一起没了，而 1、2 正好堵死了补救 —— 按钮会变成能看不能点的摆设。

在 QTabBar 上挂 ``QProxyStyle`` 也没用：QSS 子控件是 ``QStyleSheetStyle`` 自己
画的，控件级代理只截得到 ``CE_PushButton*``。

所以**保留原生按钮、只接管 Paint**：Qt 继续负责定位、显隐和点击滚动，本判据
的头一条就是守住这条原生行为。

判据分两类：像素类（真的画了东西吗）和行为类（点了会滚吗）。**像素判据带
元断言** —— 拿一个没装 painter 的标签栏对照，证明"非底色像素 > 0"不是恒绿。
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
from sshclient.ui.design.v1.palette import get_palette
from sshclient.ui.design.v1.qss import build_qss

bad = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r} (want {want!r})")
    if not ok:
        bad.append(f"{name}: got {got!r}, want {want!r}")


def check_true(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    if not ok:
        bad.append(f"{name}  {detail}")


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def settle(ms=250):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


def ink(btn, base_hex):
    """按钮上**不属于底色**的像素数。"""
    img = btn.grab().toImage()
    bg = base_hex.lstrip("#")
    br, bg_, bb = (int(bg[i:i + 2], 16) for i in (0, 2, 4))
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if abs(c.red() - br) + abs(c.green() - bg_) + abs(c.blue() - bb) > 14:
                n += 1
    return n


def build(theme):
    tmp = pathlib.Path(tempfile.mkdtemp())
    store = settings.SessionStore(tmp / "s.json")
    store._items = []
    st = settings.AppSettings()
    st.theme = theme
    st.save = lambda: None
    mw = main_window.MainWindow(st, store)
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(mw)
    host.resize(1280, 760)
    settle(450)
    return mw, host


for theme in ("light", "dark"):
    print(f"\n================ {theme} ================")
    mw, host = build(theme)
    bar = mw.tabs.tabBar()
    left = bar.findChild(QtWidgets.QToolButton, "ScrollLeftButton")
    right = bar.findChild(QtWidgets.QToolButton, "ScrollRightButton")
    check_true("找到了两个原生滚动按钮", left is not None and right is not None)

    for i in range(14):
        mw.tabs.addTab(QtWidgets.QWidget(), "root@10.0.0.%d:22" % i)
    settle(350)
    check_true("标签多到右端溢出（左端先不管）",
               bar.tabRect(bar.count() - 1).x() > bar.width(),
               f"tabN.x={bar.tabRect(bar.count() - 1).x()} bar.w={bar.width()}")

    base = get_palette(theme).bg_base
    n_left, n_right = ink(left, base), ink(right, base)
    print(f"  左按钮非底色像素 {n_left}   右按钮非底色像素 {n_right}")
    # 阈值不取高：**按钮的底子不是 `bg_base`**（实测与 base 差 10~30）。
    # 字形是几个像素粗的尖括号，能有 5 个以上就足以判有东西。
    check_true("左按钮画上了字形", n_left > 5, str(n_left))
    check_true("右按钮画上了字形", n_right > 5, str(n_right))
    check("左按钮在最左端", left.geometry().x(), 0)
    check_true("右按钮在最右端",
               right.geometry().right() >= bar.width() - 2,
               f"{right.geometry().right()} vs {bar.width()}")

    # ---------------------------------------------------- 行为：点击仍能滚
    # QTabBar 的左右按钮是独立 disable 的：左在「最左」禁用，右在「最右」禁用。
    # 所以两条禁用态要分开测。
    print("\n  --- 点击行为（原生行为不许被接管绘制弄坏）---")
    # 先推到最右，验证右按钮禁用 + 点右不动 + 点左会滚
    for _ in range(20):
        right.click()
        settle(120)
        if not right.isEnabled():
            break
    check("滚到最右时右按钮是禁用的", right.isEnabled(), False)
    x_at_right_end = bar.tabRect(0).x()
    print(f"  在最右端 tab0.x = {x_at_right_end}")
    right.click()
    settle(250)
    check("禁用时点右按钮不会动", bar.tabRect(0).x(), x_at_right_end)
    left.click()
    settle(250)
    check_true("点 < 之后 tab0 拉近",
               bar.tabRect(0).x() > x_at_right_end,
               f"{x_at_right_end} -> {bar.tabRect(0).x()}")

    # 再推到最左，验证左按钮禁用
    for _ in range(20):
        left.click()
        settle(120)
        if not left.isEnabled():
            break
    check("滚到最左时左按钮是禁用的", left.isEnabled(), False)
    x_at_left_end = bar.tabRect(0).x()
    left.click()
    settle(250)
    check("禁用时点左按钮不会动", bar.tabRect(0).x(), x_at_left_end)

    # -------------------------------------------------------- 元断言
    print("\n  --- 元断言：painter 关掉时不会画字形 ---")
    _painter = mw._tab_scroll
    _painter.set_paint_enabled(False)
    settle(300)
    plain_img = left.grab().toImage()
    _painter.set_paint_enabled(True)
    settle(300)
    paint_img = left.grab().toImage()

    d = sum(1 for y in range(0, min(plain_img.height(), paint_img.height()), 2)
            for x in range(0, min(plain_img.width(), paint_img.width()), 2)
            if plain_img.pixel(x, y) != paint_img.pixel(x, y))
    print(f"  关掉 painter vs 装上 painter：抽样不同像素 = {d}")
    check_true("元断言：装上 painter 后那两格多画了字形", d > 20, f"{d}")

    # -------------------------------------------------------- 换主题跟着重画
    print("\n  --- 换主题 ---")
    light_px = right.grab().toImage()
    mw.apply_theme("dark" if theme == "light" else "light")
    settle(400)
    dark_px = right.grab().toImage()
    diff = sum(1 for y in range(0, min(light_px.height(), dark_px.height()), 2)
               for x in range(0, min(light_px.width(), dark_px.width()), 2)
               if light_px.pixel(x, y) != dark_px.pixel(x, y))
    print(f"  切主题后按钮上抽样不同的像素 = {diff}")
    check_true("换主题后 < > 跟着重画（不是留一条旧色）", diff > 20, f"{diff}")

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("tab scroll buttons: all checks passed")
sys.exit(1 if bad else 0)
