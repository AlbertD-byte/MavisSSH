"""折叠态侧栏的三条：图标对齐、绿点钉在图标右上角、badge 两侧不再漏出高亮条。

上一版把绿点当成徽章的**兄弟**塞进了同一个 `QHBoxLayout`
（`lay.addWidget(dot, AlignRight|AlignTop)`）。这个写法有一个静默的副作用：
QHBoxLayout 按加入顺序分配宽度，而**可见**控件才占位、隐藏控件不占位。
所以徽章是否居中，取决于这个会话当时连没连：

    绿点隐藏   徽章中心 28.0px
    绿点显示   徽章中心 26.0px      ← 偏左 2px

也就是说**只有已连接的那一行图标是歪的**，一列 SSH 图标看上去参差不齐。
同一个布局还被用来把 `::item` 的高亮色从徽章两侧漏出来：item 盒比徽章宽
4px，左右各 2px 的缝，item 背景正好从那儿透出来，表现为夹在徽章左右的
两条竖条。

判据钉的是**消费者**（画出来的像素 + `geometry()`），不是「代码里写了什么」。
其中一条带元断言：把 QSS 里那条 `transparent` 覆盖摘掉，判据必须转红，
否则它证明不了任何事。
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import settings
from sshclient.core.settings import SavedSession
from sshclient.ui import main_window, sidebar
from sshclient.ui.design.v1 import qss as qss_mod

SESSIONS = [
    SavedSession(name="a", host="h1", port=22, username="r", auth_method="password"),
    SavedSession(name="b", host="h2", port=22, username="r", auth_method="key"),
    SavedSession(name="c", host="h3", port=22, username="r", auth_method="password"),
]

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
bad = []


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: {got!r} (want {want!r})")
    if not ok:
        bad.append(f"{name}: got {got!r}, want {want!r}")


def settle(ms=300):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


def rgb(hexs):
    h = hexs.lstrip("#")
    return QtGui.QColor(int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def near(a, b, tol=14):
    return (abs(a.red() - b.red()) + abs(a.green() - b.green())
            + abs(a.blue() - b.blue())) <= tol


# `host` must outlive `build()`: it is the only thing holding a Python
# reference to the window, and letting it fall out of scope at return time
# deletes the whole widget tree underneath the assertions.
_hosts = []


def build(theme):
    tmp = pathlib.Path(tempfile.mkdtemp())
    store = settings.SessionStore(tmp / "sessions.json")
    store._items = list(SESSIONS)
    st = settings.AppSettings()
    st.theme = theme
    st.save = lambda: None
    mw = main_window.MainWindow(st, store)
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(mw)
    host.resize(1280, 820)
    host.show()
    _hosts.append(host)
    settle(450)
    mw.set_sidebar_collapsed(True, persist=False)
    settle(300)
    return mw


def badge_centre(mw, index):
    """徽章中心在侧栏坐标系里的 x，浮点。"""
    row = mw.session_list.itemWidget(mw.session_list.item(index))
    b = row._badge
    top_left = b.mapTo(mw.sidebar, QtCore.QPoint(0, 0))
    return top_left.x() + b.width() / 2.0


def stray_accent_pixels(mw, index):
    """选中行里，`accent_soft` 却落在**徽章外面**的像素数。

    判据量的是「缝里漏色」这件事本身，不是某个控件的某个属性 ——
    换一种实现（行自带不透明底、或者把 margin 收成 0）都应该照样通过。

    只扫描**这一行**的纵向范围。曾经扫整条侧栏，于是「新建连接」那个
    实心按钮附近的同色像素也被算了进来，判据红在一个跟本条完全无关的
    控件上 —— 量错对象比量错阈值更难查。
    """
    lst = mw.session_list
    row = lst.itemWidget(lst.item(index))
    badge = row._badge
    b = badge.mapTo(mw.sidebar, QtCore.QPoint(0, 0))
    band = lst.visualItemRect(lst.item(index)).translated(
        lst.mapTo(mw.sidebar, QtCore.QPoint(0, 0)))
    soft = rgb(mw.palette_.accent_soft)
    img = mw.sidebar.grab().toImage()
    n = 0
    for y in range(band.top(), band.bottom() + 1):
        for x in range(img.width()):
            inside = (b.x() <= x < b.x() + badge.width()
                      and b.y() <= y < b.y() + badge.height())
            if not inside and near(img.pixelColor(x, y), soft):
                n += 1
    return n


for theme in ("dark", "light"):
    print(f"\n================ {theme} ================")
    mw = build(theme)
    lst = mw.session_list
    sb = mw.sidebar
    mid = sb.width() / 2.0
    print(f"  侧栏宽 {sb.width()}  中心线 {mid}")

    # ------------------------------------------------------------ 1. 对齐
    # 绿点全部隐藏时的徽章中心：必须逐行相同，且落在中心线上。
    for i in range(lst.count()):
        lst.itemWidget(lst.item(i)).set_live(False)
    settle(200)
    quiet = [badge_centre(mw, i) for i in range(lst.count())]
    print(f"  绿点隐藏：各行徽章中心 {quiet}")
    check("徽章中心逐行一致（绿点隐藏）", len(set(quiet)), 1)
    check("徽章落在侧栏中心线上", quiet[0], mid)

    # 决定性的那一半：把某一行点亮，徽章**不许动**。
    # 旧实现是 26.0 / 28.0 / 28.0 —— 这一条正是当初漏掉的那半。
    lst.itemWidget(lst.item(1)).set_live(True)
    settle(200)
    loud = [badge_centre(mw, i) for i in range(lst.count())]
    print(f"  1 号已连接：各行徽章中心 {loud}")
    check("徽章中心逐行一致（绿点显示）", len(set(loud)), 1)
    check("绿点没有挤动徽章", loud, quiet)

    # ------------------------------------------------- 2. 绿点钉在右上角
    victim = lst.itemWidget(lst.item(1))
    dot = victim._dot
    dg = dot.geometry()
    bg = victim._badge.geometry()
    print(f"  徽章(row 内)={bg.getRect()}  绿点(row 内)={dg.getRect()}")
    # 徽章局部坐标下的绿点位置：右边与徽章右缘齐平，顶边内缩 1px 免得压圆角
    check("绿点右边与徽章右缘齐平",
          dg.x() + dot.width(), bg.x() + bg.width())
    check("绿点顶边 = 徽章顶边 + 1", dg.y(), bg.y() + 1)
    check("绿点仍在行内", dg.right() <= victim.width()
          and dg.top() >= 0 and dg.left() >= 0, True)
    check("绿点可见", dot.isVisible(), True)

    # 徽章一旦挪动，绿点必须跟着走 —— 「保持不动」说的是它钉在图标上，
    # 不是它钉在行上。这一条不是摆设：只监听 row 的 resizeEvent 时它会红，
    # 因为「行没变宽、徽章在行内滑了一下」根本不触发 resizeEvent。
    victim.layout().addStretch(0)
    settle(150)
    moved_dot = dot.geometry().x() - dg.x()
    moved_badge = victim._badge.geometry().x() - bg.x()
    print(f"  [钉住] 徽章移动 {moved_badge}px -> 绿点移动 {moved_dot}px")
    check("这次徽章确实挪动了（否则上面是空判据）", moved_badge != 0, True)
    check("绿点跟着徽章一起动", moved_dot, moved_badge)
    victim.layout().takeAt(1)
    settle(150)
    check("拉伸撤掉后绿点回到原位", dot.geometry(), dg)

    # 元断言：把绿点塞回布局里，上面两条必须会红。
    # 用一个**独立**的行做，不碰被测的那几行 —— 上一版就地拆装控件，
    # `setParent(None)` 会顺手把可见的绿点隐藏掉，后面所有测量全被带偏。
    # 判据只量「塞回去会不会挤歪」，恒绿就等于没测。
    #
    # 独立行必须挂进一个容器，不能 `show()` 成顶层窗口：Windows 不允许顶层
    # 窗口比自己的边框还窄，`setFixedSize(44, 40)` 会被顶回 120x40，徽章
    # 自然就不在中间了 —— 那测的是窗口管理器，不是布局。
    holder = QtWidgets.QFrame()
    holder.setFixedSize(44, 40)
    hl = QtWidgets.QVBoxLayout(holder)
    hl.setContentsMargins(0, 0, 0, 0)
    solo = sidebar.CompactSessionItem(SESSIONS[0], mw.palette_, holder)
    hl.addWidget(solo)
    holder.show()
    solo._dot.setVisible(True)
    settle(200)
    print(f"  [独立行] 实际尺寸 {solo.width()}x{solo.height()}"
          f"  徽章 x={solo._badge.geometry().x()}")
    check("独立行本身是 44 宽（否则元断言量的是窗口管理器）", solo.width(), 44)
    check("独立行：绿点不进布局时徽章居中",
          solo._badge.geometry().x(), 2)
    solo.layout().addWidget(solo._dot, 0,
                            QtCore.Qt.AlignmentFlag.AlignRight
                            | QtCore.Qt.AlignmentFlag.AlignTop)
    settle(200)
    print(f"  [元断言] 绿点塞回布局后徽章 x = {solo._badge.geometry().x()}")
    check("元断言：塞回布局确实会挤歪", solo._badge.geometry().x(), 0)
    holder.hide()
    holder.deleteLater()
    settle(100)

    # ------------------------------------------- 3. 徽章两侧不再漏出高亮
    lst.setCurrentRow(1)
    settle(220)
    stray = stray_accent_pixels(mw, 1)
    print(f"  选中行徽章外的 accent_soft 像素：{stray}")
    check("徽章外没有漏出高亮色", stray, 0)

    # 选中状态不能顺手弄丢：徽章自己得看得出被选中。
    # 取徽章内**靠左**的一点，不是正中：正中是 18x18 图标字形所在处，
    # 量到的永远是图标的抗锯齿边而不是底色（上一版就栽在这里）。
    def badge_fill(index):
        row = lst.itemWidget(lst.item(index))
        top_left = row._badge.mapTo(sb, QtCore.QPoint(0, 0))
        return mw.sidebar.grab().toImage().pixelColor(
            top_left.x() + 4, top_left.y() + 20).name()

    lst.setCurrentRow(1)
    settle(220)
    sel_fill = badge_fill(1)
    lst.setCurrentRow(0)
    settle(220)
    # 采**第 1 行**：选中已经挪走了，第 0 行这时才是「选中」的那个。
    # 上一版采第 0 行，两边都读到 accent_soft —— 不是实现没换色，
    # 是判据在两个时刻都量了同一行。
    idle_fill = badge_fill(1)
    print(f"  选中徽章填充 {sel_fill}   取消选中后同一徽章 {idle_fill}")
    check("取消选中后徽章换了底色", sel_fill != idle_fill, True)
    check("选中徽章就是 accent_soft", sel_fill,
          rgb(mw.palette_.accent_soft).name())
    check("取消选中后回到 bg_elevated", idle_fill,
          rgb(mw.palette_.bg_elevated).name())
    lst.setCurrentRow(1)
    settle(220)

    # 元断言：把 qss.py 里那条 `transparent` 覆盖摘掉，判据必须转红。
    # 追加在末尾的同特异度规则会压过 build_qss 生成的那条，等价于回到旧行为。
    # 跑完**必须还原** —— 追加的是整份窗口样式表，留着会污染后面所有取色。
    base_sheet = mw.styleSheet()
    lst.setCurrentRow(1)
    settle(200)
    mw.setStyleSheet(base_sheet + (
        f'QListWidget#sessionList[rail="1"]::item:selected'
        f' {{ background-color: {mw.palette_.accent_soft}; }}'))
    lst.style().unpolish(lst)
    lst.style().polish(lst)
    settle(250)
    stray_back = stray_accent_pixels(mw, 1)
    print(f"  [元断言] 摘掉 transparent 后：{stray_back}")
    check("元断言：摘掉覆盖确实会漏色", stray_back > 0, True)
    mw.setStyleSheet(base_sheet)
    lst.style().unpolish(lst)
    lst.style().polish(lst)
    settle(250)
    check("还原后徽章外重新没有漏色", stray_accent_pixels(mw, 1), 0)

    mw.close()
    mw.deleteLater()
    settle(120)

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("rail alignment: all checks passed")
sys.exit(1 if bad else 0)
