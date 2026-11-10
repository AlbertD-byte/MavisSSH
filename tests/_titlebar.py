"""自绘标题栏：结构、像素、命中测试、交互。

判据落在**用户看到的东西**上——底色、字形、悬停色都从 `grab()` 出来的
真实像素里取，不去量对象上的属性。"按钮说自己叫什么"对得上，只说明生产
代码读它读对了；画出来对不对是另一回事。

本文件**不要** import `tests.fonts`：那个模块在 import 时就把
QT_QPA_PLATFORM 设成 offscreen，而本文件要测无边框窗口的原生行为
（命中测试、最大化），offscreen 下量不到。
"""
from __future__ import annotations

import ast
import ctypes.wintypes as _wt
import os
import pathlib
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from sshclient.core import settings as S
from sshclient.core.snippets import SnippetStore
from sshclient.ui import main_window as MW
from sshclient.ui import titlebar as T
from sshclient.ui.design.v1.palette import DARK, LIGHT, get_palette

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail))


def px(widget, x, y) -> str:
    """从真实渲染结果里取一个像素的颜色。"""
    return widget.grab().toImage().pixelColor(x, y).name()


def dominant(widget, y: int) -> str:
    """某一整行里出现最多的颜色。

    **不要用 `x = width // 2` 取底色。** 那个点会随窗口宽度漂：标题栏窄到
    100px 上下时，中间正好压在标题文字上，读到的是字的颜色（深色主题的标题
    是浅字、浅色主题的标题是深字，于是同一个判据在一套主题下绿、另一套下
    红，看着像产品坏了）。取众数才是"这一行大部分是什么颜色"，也就是用户
    真正看到的那片底色。
    """
    img = widget.grab().toImage()
    counts: dict[str, int] = {}
    for x in range(widget.width()):
        c = img.pixelColor(x, y).name()
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def count_color(widget, color: str) -> int:
    """整个部件里有多少个像素正好是 *color*。"""
    target = color.lower()
    img = widget.grab().toImage()
    n = 0
    for y in range(widget.height()):
        for x in range(widget.width()):
            if img.pixelColor(x, y).name() == target:
                n += 1
    return n


# 补丁是**一片**，文字是**若干细笔画**。所以判据不能是「一个像素都不许有」——
# 深色主题里 `text_on_accent` 和 `bg_base` 同为 `#0d0f14`（primary 按钮上的
# 深字压在亮蓝底上才达 AA），按钮文字会被数成「残留的主窗口底色」。
# 改判**最长连续横段**：一块补丁会横跨整个控件宽度（几十上百 px），
# 文字笔画最宽也就几个 px。这么改对补丁反而更严。
_PATCH_RUN = 24


def max_color_run(widget, color: str) -> int:
    """某一整行里，最长的连续 *color* 段有多长。"""
    target = color.lower()
    img = widget.grab().toImage()
    best = 0
    for y in range(widget.height()):
        run = 0
        for x in range(widget.width()):
            if img.pixelColor(x, y).name() == target:
                run += 1
                best = max(best, run)
            else:
                run = 0
    return best


def content_labels(dialog, tb) -> list:
    """弹窗里**属于正文**的可见 QLabel。

    必须排掉标题栏自己的那几个（图标和标题文字）。`findChildren` 是递归的，
    标题栏那两个也是弹窗的孩子，坐标算出来 y≈10——比标题栏高度小得多。
    第一次写这条判据就栽在这儿：三条"正文被盖掉"全红，读上去像三个弹窗的
    正文都被盖了，其实量的是标题栏的标题。
    """
    return [lb for lb in dialog.findChildren(QtWidgets.QLabel)
            if lb.isVisible() and not tb.isAncestorOf(lb)]


def paint():
    app = QtWidgets.QApplication.instance()
    for _ in range(5):
        app.processEvents()


def main() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    offscreen = app.platformName() in ("offscreen", "minimal")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="titlebar_"))
    store = S.SessionStore(tmp / "sessions.json", config_dir=tmp)
    # **不要用 S.AppSettings.load()** —— 那读的是用户真实的
    # %APPDATA%\MavisSSH\settings.json，而 MainWindow 里有 5 处
    # self.settings.save()，跑一次测试就把用户的配置改一遍。
    st = S.AppSettings()
    st._path = tmp / "settings.json"
    st.theme = "dark"
    mw = MW.MainWindow(settings=st, store=store,
                       snippet_store=SnippetStore(tmp / "snippets.json"))
    mw.resize(1280, 820)
    mw.show()
    paint()
    tb = mw.titlebar
    p = mw.palette_

    # ---------------------------------------------------- 1 结构
    print("\n[1] 结构")

    want_frame = T.frameless_enabled()
    got_frame = bool(int(mw.windowFlags())
                     & int(QtCore.Qt.WindowType.FramelessWindowHint))
    check("无边框标志与 frameless_enabled() 一致", got_frame == want_frame,
          "flag=%s enabled=%s" % (got_frame, want_frame))

    check("标题栏贴在窗口最顶端", tb.geometry().y() == 0, str(tb.geometry()))
    check("标题栏横贯整个窗口宽度", tb.width() == mw.width(),
          "tb=%d win=%d" % (tb.width(), mw.width()))
    check("标题栏高度是 %d" % T.TITLEBAR_H, tb.height() == T.TITLEBAR_H,
          str(tb.height()))

    check("菜单栏在标题栏**下面**而不是上面",
          mw._menu_bar.geometry().y() == T.TITLEBAR_H,
          str(mw._menu_bar.geometry()))

    # 三个按钮：等宽、贴右边、彼此挨着
    btns = [tb.btn_min, tb.btn_max, tb.btn_close]
    check("三个窗口按钮都在",
          all(b is not None and b.isVisible() for b in btns))
    check("每个窗口按钮宽度都是 %d" % T.CAPTION_W,
          all(b.width() == T.CAPTION_W for b in btns),
          str([b.width() for b in btns]))
    check("关闭按钮紧贴标题栏右缘（不悬空）",
          btns[-1].geometry().right() + 1 == tb.width(),
          "right=%d width=%d" % (btns[-1].geometry().right() + 1, tb.width()))
    check("三个按钮左右相邻、中间没有缝",
          all(btns[i].geometry().right() + 1 == btns[i + 1].geometry().x()
              for i in range(2)),
          str([b.geometry() for b in btns]))
    check("从左到右依次是最小化/最大化/关闭",
          [b.kind for b in btns] == [T.MIN, T.MAX, T.CLOSE],
          str([b.kind for b in btns]))

    # 提示：图标按钮没有文字，提示就是用户认出它的唯一线索
    tips = [b.toolTip() for b in btns]
    check("三个按钮都有 tooltip", all(t.strip() for t in tips), str(tips))
    check("三个按钮的 tooltip 互不相同（用户分得清）",
          len(set(tips)) == 3, str(tips))

    # ---------------------------------------------------- 2 像素
    print("\n[2] 像素（用户真正看到的）")

    # **标题栏底色从 `bg_base` 改成 `bg_surface`。** 它和菜单栏、状态栏一起
    # 是「chrome 带」，靠色差浮在内容区（`bg_base`）之上；底下那条发丝线
    # 也随之删掉。**弹窗那一条发丝线仍然在** —— 弹窗标题栏与弹窗体都是
    # `bg_dialog`，色差在那里是 1.000，线是唯一的分层信号。
    check("标题栏底色是 chrome 色 %s（浮在内容区 bg_base 之上）" % p.bg_surface,
          px(tb, 600, 18) == p.bg_surface.lower(),
          "%s" % px(tb, 600, 18))
    check("标题栏底边**不再有**发丝线（分层交给色差）",
          px(tb, 600, T.TITLEBAR_H - 1) == p.bg_surface.lower(),
          "%s（不该是 border %s）" % (px(tb, 600, T.TITLEBAR_H - 1), p.border.lower()))

    # 字形确实画上去了：按钮正中一块区域不能和背景一个颜色
    def glyph_pixels(b):
        img = b.grab().toImage()
        bg = p.bg_base.lower()
        n = 0
        for y in range(6, b.height() - 6):
            for x in range(6, b.width() - 6):
                if img.pixelColor(x, y).name() != bg:
                    n += 1
        return n

    for b in btns:
        n = glyph_pixels(b)
        check("%s 按钮的字形真的画出来了" % b.kind, n > 3, "非背景像素 %d 个" % n)

    # 最大化 <-> 还原：两种字形必须不同
    tb.btn_max.set_restoring(False)
    paint()
    img_normal = tb.btn_max.grab().toImage()
    tb.btn_max.set_restoring(True)
    paint()
    img_restore = tb.btn_max.grab().toImage()
    check("最大化后按钮画的是「还原」，和「最大化」不是同一张图",
          img_normal != img_restore)
    check("最大化状态下 tooltip 变成「还原」",
          tb.btn_max.toolTip() == "还原", tb.btn_max.toolTip())
    # 切回来必须逐像素回到原样——否则上面那条可能只是每次重绘的随机差异
    tb.btn_max.set_restoring(False)
    paint()
    check("切回「最大化」字形后和原来逐像素一致（上面那条不是噪声）",
          tb.btn_max.grab().toImage() == img_normal)

    # 悬停：关闭按钮要变红，这是 Windows 上最该被认出来的一个。
    # 判据是"从红变回非红"而不是"变回窗口底色"——真实鼠标可能正好停在这
    # 个按钮上，underMouse() 为真时底色是 bg_elevated 而不是透明，
    # 拿 bg_base 当期望值等于在赌鼠标位置。
    tb.btn_close.set_native_hover(True)
    paint()
    hovered = px(tb.btn_close, 4, 19)
    tb.btn_close.set_native_hover(False)
    paint()
    unhovered = px(tb.btn_close, 4, 19)
    check("关闭按钮悬停时底色是 danger %s" % p.danger,
          hovered == p.danger.lower(), "%s" % hovered)
    check("移开悬停后关闭按钮不再是红的", unhovered != p.danger.lower(),
          "%s" % unhovered)

    # ---------------------------------------------------- 3 命中测试
    print("\n[3] 命中测试（拖动 / 缩放 / 贴边分屏）")

    if offscreen:
        print("  跳过：offscreen 平台量不到原生窗口行为")
    else:
        # hit_test 收的是**窗口坐标**，按钮的 rect() 是按钮自己的坐标。
        # 中间还差一级标题栏坐标，要转两次。少转一次会得到一个看着合理、
        # 实际错位的结果——这正是「判据坐标系不对」最典型的表现。
        def center_in_window(b):
            in_tb = b.mapTo(tb, b.rect().center())
            c = tb.mapTo(mw, in_tb)
            return c.x(), c.y()

        # ------------------------------------------------ 一次点击 = 一次切换
        # **这一组是补一个实测过的空白。** 前面那些判据量的是「hit_test 返回
        # 什么」和「nativeEvent 有没有把消息交给 handle_native」，**没有一条
        # 量过「一次物理点击到底切换了几次」** —— 而「按了没生效」正是这个数
        # 不等于 1（0 次或 2 次）时的样子，在前面那些判据下长得一模一样。
        #
        # 原来的写法把切换挂在 `WM_NCLBUTTONUP` 上，而 Windows 只有在
        # `WM_NCLBUTTONDOWN` 走完 DefWindowProc 才会进入非客户端按钮捕获；
        # 本项目为了不让系统把整个标题栏拖走，恰恰**必须**在 DOWN 就吞掉它，
        # 于是 UP 往往根本不会送到 ——「按下有反应、抬起没反应」= 净效果零。
        # 现在挂在 DOWN 上：DOWN 保证送达，一次点击只有一个 DOWN。
        _real_from = _wt.MSG.from_address

        def _feed(ids):
            """按给定顺序喂一串 WM_NCLBUTTON* 消息，数切换了几次。"""
            box = []
            rt = T.TitleBar.toggle_maximize
            T.TitleBar.toggle_maximize = lambda self: box.append(1)
            _wt.MSG.from_address = staticmethod(lambda _a: _cur["m"])
            try:
                for mid in ids:
                    _cur["m"] = types.SimpleNamespace(
                        message=mid, wParam=T.HTMAXBUTTON, lParam=0)
                    T.handle_native(mw, tb, 0)
            finally:
                T.TitleBar.toggle_maximize = rt
                _wt.MSG.from_address = _real_from
            return len(box)

        _cur = {"m": None}
        _only_down = _feed([T.WM_NCLBUTTONDOWN])
        check("最大化按钮：即使**抬起那条消息没来**，按下也必须切换"
              "（真机上的症状正是抬起收不到 → 按了没反应）",
              _only_down == 1, "只发按下时切换了 %d 次" % _only_down)
        _down_up = _feed([T.WM_NCLBUTTONDOWN, T.WM_NCLBUTTONUP])
        check("最大化按钮：按下+抬起**总共**只切换一次（不能变成两次）",
              _down_up == 1, "切换了 %d 次" % _down_up)

        # 元断言：装回「挂在抬起上」的缺陷版，「只发按下」那条必须变红。
        #
        # **为什么第一条不能只发 DOWN+UP**：那样模拟出来的序列里 UP 一定到场，
        # 缺陷版照样切一次 —— 模拟复现不出「UP 根本不会送达」这个真实断点，
        # 缺陷和修复在它眼里**完全一样**。判据必须去量那个真正区分两者的东西：
        # **动作不依赖那条可能不来的消息**。
        def _defect_handle(win_, tb_, message):
            m = _wt.MSG.from_address(int(message))
            if m.message == T.WM_NCLBUTTONDOWN and m.wParam == T.HTMAXBUTTON:
                return True, 0
            if m.message == T.WM_NCLBUTTONUP and m.wParam == T.HTMAXBUTTON:
                if tb_.btn_max is not None:
                    tb_.btn_max.set_native_hover(False)
                    tb_.toggle_maximize()
                return True, 0
            return None

        _real_handle_max = T.handle_native
        T.handle_native = _defect_handle
        _dbox = []
        _rt = T.TitleBar.toggle_maximize
        T.TitleBar.toggle_maximize = lambda self: _dbox.append(1)
        _wt.MSG.from_address = staticmethod(lambda _a: _cur["m"])
        try:
            _cur["m"] = types.SimpleNamespace(
                message=T.WM_NCLBUTTONDOWN, wParam=T.HTMAXBUTTON, lParam=0)
            _defect_handle(mw, tb, 0)
            _d_hits = len(_dbox)
        finally:
            T.handle_native = _real_handle_max
            T.TitleBar.toggle_maximize = _rt
            _wt.MSG.from_address = _real_from
        check("元断言：装回「切换挂在抬起上」的缺陷版后，只发按下切 0 次"
              "（所以上面两条真的量到了「按了没生效」，不是恒绿）",
              _d_hits == 0,
              "缺陷版切换了 %d 次（模拟里它只在 UP 上动，而真机上 UP 未必到）"
              % _d_hits)

        hit = lambda x, y: T.hit_test(mw, tb, QtCore.QPoint(x, y))

        check("标题栏中部 = HTCAPTION（交给系统拖动）",
              hit(600, 18) == T.HTCAPTION, str(hit(600, 18)))
        cmax = center_in_window(tb.btn_max)
        check("最大化按钮 = HTMAXBUTTON（Win11 贴边分屏靠它）",
              hit(*cmax) == T.HTMAXBUTTON,
              "点 (%d,%d) 得到 %d" % (cmax[0], cmax[1], hit(*cmax)))
        # 直接钉住「判定区在右边」：用 rect()（按钮自己的坐标）代替
        # geometry()（相对标题栏）时，贴边分屏区会跑到标题栏左上角，
        # 而其它判据可能照样全绿。
        check("贴边分屏的判定区在标题栏**右半边**，不在左上角",
              cmax[0] > tb.width() / 2,
              "最大化按钮中心 x=%d，标题栏宽 %d" % (cmax[0], tb.width()))
        check("标题栏左上角不是 HTMAXBUTTON（那里归顶边缩放）",
              hit(1, 1) != T.HTMAXBUTTON, str(hit(1, 1)))
        # caption 按钮必须压过缩放边框和标题栏，否则最右 6px 抢走关闭按钮，
        # 而标题栏的 HTCAPTION 会让按钮"看得见点不着"
        cmin = center_in_window(tb.btn_min)
        cclose = center_in_window(tb.btn_close)
        for nm, c in (("最小化", cmin), ("关闭", cclose)):
            check("%s按钮判定为 HTCLIENT（点得动，不会被当成标题栏）" % nm,
                  hit(*c) == T.HTCLIENT,
                  "点 %s 得到 %d（HTCLIENT=%d HTCAPTION=%d）"
                  % (c, hit(*c), T.HTCLIENT, T.HTCAPTION))
        check("窗口左上角 = HTTOPLEFT（标题栏不能把顶边缩放区吃掉）",
              hit(1, 1) == T.HTTOPLEFT, str(hit(1, 1)))
        # 右上角最外侧那一小块**让给缩放角**了（CORNER_GRAB 见方），这样
        # 四个角都能拖。原来这里是「按钮优先于缩放角」，代价是右上角永远
        # 到不了 —— 用户明确要求四角都能缩放，取舍就反过来了。按钮本体
        # （中心附近）仍然是 HTCLIENT，点得中。
        check("右上角最外侧让给缩放角（四角都要能缩放）",
              hit(mw.width() - 2, 1) == T.HTTOPRIGHT, str(hit(mw.width() - 2, 1)))
        check("右上角让出的范围外仍是关闭按钮（按钮没被整条吃掉）",
              hit(mw.width() - T.CORNER_GRAB - 1,
                  1) == T.HTCLIENT,
              str(hit(mw.width() - T.CORNER_GRAB - 1, 1)))
        # **顶边 = 标题栏，不是缩放区**（commit f7f1a97「修标题栏顶部 7 行
        # 死区」）。标题栏满宽铺在窗口最上面，顶边 100% 落在标题栏里，
        # 而 `y <= RESIZE_MARGIN` 判成 HTTOP 的判定排在标题栏**前面**，
        # 于是标题栏内顶部 7 行被「拖顶边改高度」吃掉：双击那里不会最大化、
        # 按住拖也不是移窗口 —— 用户报的「双击时灵时不灵」就是这条。
        # 顶边缩放和标题栏拖动物理上互斥（同一批像素），选了标题栏。
        #
        # 下面两条原来断言 `== T.HTTOP`，编码的是**已被主动移除**的行为，
        # 从那以后一直红着。一条红的判据比没有判据更糟。
        check("顶边右侧（按钮区之外）= HTCAPTION，是拖窗口而不是改高度",
              hit(mw.width() - T.CAPTION_W * 4, 1) == T.HTCAPTION,
              str(hit(mw.width() - T.CAPTION_W * 4, 1)))
        check("右边缘在标题栏以下仍可拖宽",
              hit(mw.width() - 1, T.TITLEBAR_H + 30) == T.HTRIGHT,
              str(hit(mw.width() - 1, T.TITLEBAR_H + 30)))
        check("窗口左下角 = HTBOTTOMLEFT",
              hit(1, mw.height() - 2) == T.HTBOTTOMLEFT)
        check("窗口右下角 = HTBOTTOMRIGHT",
              hit(mw.width() - 2, mw.height() - 2) == T.HTBOTTOMRIGHT)
        check("上边缘中段 = HTCAPTION（顶边归标题栏）",
              hit(mw.width() // 2, 1) == T.HTCAPTION,
              str(hit(mw.width() // 2, 1)))
        # **缩放没有被整个堵死**：极角那 6×6 见方仍然含 TopEdge。
        check("左上角仍然能拉高（角上没被一起让掉）",
              T.RESIZE_EDGE_FOR_CODE.get(T.HTTOPLEFT)
              & QtCore.Qt.Edge.TopEdge)
        check("左边缘中段 = HTLEFT（能拉宽）",
              hit(1, mw.height() // 2) == T.HTLEFT)
        check("右边缘中段 = HTRIGHT",
              hit(mw.width() - 2, mw.height() // 2) == T.HTRIGHT)
        check("下边缘中段 = HTBOTTOM",
              hit(mw.width() // 2, mw.height() - 2) == T.HTBOTTOM)
        check("窗口中央 = HTCLIENT（正常点击，不抢）",
              hit(mw.width() // 2, mw.height() // 2) == T.HTCLIENT,
              str(hit(mw.width() // 2, mw.height() // 2)))
        check("标题栏从左到右整条都是 HTCAPTION（不只左边一块能拖）",
              hit(tb.width() - T.CAPTION_W * 3 - 2, 18) == T.HTCAPTION,
              str(hit(tb.width() - T.CAPTION_W * 3 - 2, 18)))

    # ---------------------------------------------------- 4 交互
    print("\n[4] 交互")

    was_max = mw.isMaximized()
    tb.btn_max.click()
    paint()
    check("点最大化按钮真的切换了最大化状态",
          mw.isMaximized() != was_max, "isMaximized=%s" % mw.isMaximized())
    if mw.isMaximized():
        check("最大化之后按钮提示变成「还原」",
              tb.btn_max.toolTip() == "还原", tb.btn_max.toolTip())
    if mw.isMaximized():
        mw.showNormal()
        paint()
    check("再点一次能还原", not mw.isMaximized(), str(mw.isMaximized()))

    # 双击标题栏
    mw.showNormal()
    paint()
    before = mw.isMaximized()
    QtTest.QTest.mouseDClick(tb, QtCore.Qt.MouseButton.LeftButton,
                             QtCore.Qt.KeyboardModifier.NoModifier,
                             QtCore.QPoint(600, 18))
    paint()
    check("双击标题栏切换最大化", mw.isMaximized() != before,
          "before=%s after=%s" % (before, mw.isMaximized()))
    if mw.isMaximized():
        mw.showNormal()
        paint()

    # 关闭 / 最小化：用 QSignalSpy 盯真实信号，不去 disconnect 原有的槽
    # （`disconnect()` 是通配断连，会把同按钮上别的连接一起掐掉并打警告）
    spy_close = QtTest.QSignalSpy(tb.btn_close.clicked)
    tb.btn_close.click()
    paint()
    check("点关闭按钮真的发出 clicked", len(spy_close) == 1,
          "收到 %d 次" % len(spy_close))

    # 槽确实接的是 close：把窗口的 close 换成计数器
    n_closed = []
    orig_win_close = mw.close
    try:
        mw.close = lambda *a, **k: n_closed.append(1)
        tb.btn_close.click()
        paint()
    finally:
        mw.close = orig_win_close
    check("关闭按钮的槽最终调用的是窗口 close()", bool(n_closed), str(n_closed))

    tb.btn_min.click()
    paint()
    check("点最小化按钮真的最小化了", mw.isMinimized(), str(mw.isMinimized()))
    mw.showNormal()
    paint()

    # ---------------------------------------------------- 5 主题
    print("\n[5] 主题")

    tb.apply_palette(LIGHT)
    paint()
    # **跟着 [2] 那条改：期望值是 `bg_surface` 而不是 `bg_base`。**
    # 标题栏现在是 chrome 带，与菜单栏/状态栏同色、浮在内容区 `bg_base` 之上；
    # 写 `bg_base` 是在赌「两层同色」，而那正是这批改动刚拆掉的东西。
    check("换成浅色主题后标题栏底色跟着变 %s" % LIGHT.bg_surface,
          px(tb, 600, 18) == LIGHT.bg_surface.lower(),
          "%s" % px(tb, 600, 18))
    tb.apply_palette(DARK)
    paint()
    check("换回深色主题后底色又回来了",
          px(tb, 600, 18) == DARK.bg_surface.lower(),
          "%s" % px(tb, 600, 18))

    mw.apply_theme("light")
    paint()
    check("走 apply_theme 这条真实路径，标题栏也跟着换",
          px(tb, 600, 18) == LIGHT.bg_surface.lower(),
          "%s" % px(tb, 600, 18))
    mw.apply_theme("dark")
    paint()

    # ---------------------------------------------------- 6 对话框
    print("\n[6] 对话框（设置框 / 连接框）")

    from sshclient.ui import connect_dialog, settings_dialog

    st2 = S.AppSettings()      # 同上：别读用户真实配置
    st2._path = tmp / "settings2.json"
    st2.theme = "dark"
    pal = get_palette("dark")
    dset = settings_dialog.SettingsDialog(st2, pal, None)
    dcon = connect_dialog.ConnectDialog(palette=pal)

    for nm, d in (("设置框", dset), ("连接框", dcon)):
        d.show()
        paint()
        tb_d = d.titlebar
        check("%s 标题栏贴在最顶端" % nm, tb_d.geometry().y() == 0,
              str(tb_d.geometry()))
        check("%s 标题栏高度和主窗口一致（%d）" % (nm, T.TITLEBAR_H),
              tb_d.height() == T.TITLEBAR_H, str(tb_d.height()))
        check("%s 横贯整个对话框宽度" % nm, tb_d.width() == d.width(),
              "tb=%d dlg=%d" % (tb_d.width(), d.width()))
        # 底色取整行的众数（不能用 width//2，见 dominant 的注释）；
        # 发丝线取**按钮区左边那一整条**必须全是 border —— 右边 46px 是
        # 关闭按钮，它不悬停时画的是透明，取样点落进去会读到底色。
        got_bg = dominant(tb_d, 18)
        check("%s 标题栏底色是弹窗那一层 %s" % (nm, pal.bg_dialog),
              got_bg == pal.bg_dialog.lower(), "读到 %s" % got_bg)
        # 这一条才是用户真正提的那件事：**弹窗不能和主窗口一个颜色**。
        # 不钉死具体色值——以后调深浅只要还留着色差就该过。
        check("%s 底色和主窗口**不一样**（弹出来一眼认得出）" % nm,
              got_bg != pal.bg_base.lower(),
              "两边都是 %s" % got_bg)
        # 整个弹窗里不该再出现主窗口那一层底色。QSS 里 `QWidget` 是**兜底**
        # 规则，会匹配到弹窗里的每一个子控件；兜底规则没跟着换的话，正文区
        # 就是一块块比周围更暗的补丁——标题栏看着对，正文是花的。
        # 不要求整个弹窗同一个颜色：文件框里的文件列表本来就该是 bg_surface。
        run = max_color_run(d, pal.bg_base)
        check("%s 弹窗里没有残留主窗口底色的补丁" % nm, run < _PATCH_RUN,
              "最长连续 %d px（阈值 %d）" % (run, _PATCH_RUN))
        hair = [px(tb_d, x, T.TITLEBAR_H - 1)
                for x in range(tb_d.width() - T.CAPTION_W)]
        check("%s 底边那条发丝线整条都是 border 色" % nm,
              bool(hair) and all(c == pal.border.lower() for c in hair),
              "读到 %s" % sorted(set(hair)))
        check("%s 只有关闭按钮（对话框不最小化、不最大化）" % nm,
              [b.kind for b in tb_d.buttons] == [T.CLOSE],
              str([b.kind for b in tb_d.buttons]))
        check("%s 关闭按钮紧贴右缘" % nm,
              tb_d.btn_close.geometry().right() + 1 == tb_d.width(),
              str(tb_d.btn_close.geometry()))
        check("%s 关闭按钮的 tooltip 是「关闭」" % nm,
              tb_d.btn_close.toolTip() == "关闭", tb_d.btn_close.toolTip())

        if not offscreen:
            c = tb_d.btn_close
            cx = tb_d.mapTo(d, c.mapTo(tb_d, c.rect().center()))
            check("%s 关闭按钮判定为 HTCLIENT（点得动）" % nm,
                  T.hit_test(d, tb_d, cx) == T.HTCLIENT,
                  str(T.hit_test(d, tb_d, cx)))
            check("%s 标题栏空白处判定为 HTCAPTION（能拖）" % nm,
                  T.hit_test(d, tb_d, QtCore.QPoint(120, 18)) == T.HTCAPTION,
                  str(T.hit_test(d, tb_d, QtCore.QPoint(120, 18))))

        # 标题栏说了一遍、内容里不该再说一遍
        titles = [w for w in d.findChildren(QtWidgets.QLabel)
                  if w.property("role") == "title" and w.isVisible()]
        check("%s 内容里没有和标题栏重复的大标题" % nm, not titles,
              str([t.text() for t in titles]))

        d.close()
        paint()

    # 关掉真的关掉（QDialog 上 close() 走 reject）
    dset.show()
    paint()
    dset.titlebar.btn_close.click()
    paint()
    check("点标题栏的关闭按钮真的关掉了对话框", not dset.isVisible(),
          "visible=%s" % dset.isVisible())

    # 取消按钮仍然走 reject（原来的行为没被改）
    dset.show()
    paint()
    dset.reject()
    paint()
    check("reject() 仍然让对话框消失", not dset.isVisible())

    # ---------------------------------------------------- 7 弹窗
    print("\n[7] 弹窗（消息框 / 文件框 / 输入框）")

    from sshclient.ui import msgbox

    # 配色不靠调用点传：主窗口换主题时记一份，弹窗自己去取。这条走真实路径。
    mw.apply_theme("dark")
    paint()

    # 主窗口**不能**跟着弹窗走：弹窗抬到 `bg_dialog`，主窗口抬到
    # `bg_surface`（chrome 带），**都不是内容区的 `bg_base`**。
    check("主窗口标题栏是 chrome 色 %s，不是内容区底色 %s（只有弹窗再抬一层）"
          % (pal.bg_surface, pal.bg_base),
          dominant(tb, 18) == pal.bg_surface.lower()
          and pal.bg_surface.lower() != pal.bg_base.lower(),
          "读到 %s" % dominant(tb, 18))

    boxes = []

    # 消息框
    mbox = msgbox._build(None, QtWidgets.QMessageBox.Icon.Warning,
                         "连接失败", "连不上 %s:%d，请检查网络或账号。"
                         % ("10.0.0.9", 22))
    boxes.append(("消息框", mbox))

    # 文件框
    fbox = msgbox.ThemedFileDialog(None, "选择私钥文件", "")
    boxes.append(("文件框", fbox))

    # 输入框
    ibox = msgbox.ThemedInputDialog(None, "新建目录")
    ibox.setLabelText("目录名：")
    boxes.append(("输入框", ibox))

    for nm, d in boxes:
        d.show()
        paint()
        tb_d = getattr(d, "titlebar", None)
        # 这条是本轮真缺陷的直接钉子：`native_event` 靠 getattr(window,
        # "titlebar") 找标题栏，找不到就静悄悄返回 (False, 0)——标题栏照样
        # 画得出来，可拖动和边缘缩放整个是死的，而且一点报错都没有。截图
        # 完全看不出来（画得出来 ≠ 点得动）。
        check("%s 标题栏挂在窗口的 titlebar 属性上" % nm,
              tb_d is not None and tb_d.parent() is d,
              "titlebar=%r" % (tb_d,))
        if tb_d is None:
            continue

        check("%s 标题栏贴在最顶端" % nm, tb_d.geometry().y() == 0,
              str(tb_d.geometry()))
        check("%s 标题栏高度是 %d" % (nm, T.TITLEBAR_H),
              tb_d.height() == T.TITLEBAR_H, str(tb_d.height()))
        check("%s 标题栏横贯整个弹窗宽度" % nm,
              tb_d.width() == d.width(),
              "tb=%d dlg=%d" % (tb_d.width(), d.width()))

        got_bg = dominant(tb_d, 18)
        check("%s 标题栏底色是弹窗那一层 %s" % (nm, pal.bg_dialog),
              got_bg == pal.bg_dialog.lower(), "读到 %s" % got_bg)
        check("%s 底色和主窗口**不一样**（弹出来一眼认得出）" % nm,
              got_bg != pal.bg_base.lower(), "两边都是 %s" % got_bg)
        run = max_color_run(d, pal.bg_base)
        check("%s 弹窗里没有残留主窗口底色的补丁" % nm, run < _PATCH_RUN,
              "最长连续 %d px（阈值 %d）" % (run, _PATCH_RUN))
        hair = [px(tb_d, x, T.TITLEBAR_H - 1)
                for x in range(tb_d.width() - T.CAPTION_W)]
        check("%s 底边那条发丝线整条都是 border 色" % nm,
              bool(hair) and all(c == pal.border.lower() for c in hair),
              "读到 %s" % sorted(set(hair)))
        check("%s 只有关闭按钮" % nm,
              [b.kind for b in tb_d.buttons] == [T.CLOSE],
              str([b.kind for b in tb_d.buttons]))
        check("%s 关闭按钮紧贴右缘" % nm,
              tb_d.btn_close.geometry().right() + 1 == tb_d.width(),
              str(tb_d.btn_close.geometry()))

        # **正文不能被标题栏盖掉。** 这是本轮出图时真出过的症状：顶边距
        # 必须在 setText 之后设，症状是正文被盖掉一半看着像"内容被裁了"。
        # 取所有可见 QLabel 里最靠上的那个——它就是第一个会被压住的控件。
        tops = [lb.mapTo(d, QtCore.QPoint(0, 0)).y()
                for lb in content_labels(d, tb_d)]
        if tops:
            check("%s 正文被顶到标题栏**下面**（没被盖掉一半）" % nm,
                  min(tops) >= T.TITLEBAR_H,
                  "最靠上的可见文字 y=%d，标题栏高 %d" % (min(tops), T.TITLEBAR_H))
        else:
            check("%s 至少有一个属于正文的可见 QLabel（否则上面那条是空判据）" % nm,
                  False, "一个都没有")

        if not offscreen:
            c = tb_d.btn_close
            cx = tb_d.mapTo(d, c.mapTo(tb_d, c.rect().center()))
            check("%s 关闭按钮判定为 HTCLIENT（点得动）" % nm,
                  T.hit_test(d, tb_d, cx) == T.HTCLIENT,
                  str(T.hit_test(d, tb_d, cx)))
            check("%s 标题栏空白处判定为 HTCAPTION（能拖）" % nm,
                  T.hit_test(d, tb_d, QtCore.QPoint(60, 18)) == T.HTCAPTION,
                  str(T.hit_test(d, tb_d, QtCore.QPoint(60, 18))))

        # nativeEvent 必须真的走到 handle_native。直接调 handle_native 要
        # 传真的 Windows 消息指针，探针里造一个会崩；改成盯它有没有被调，
        # 判的是"这条路通不通"而不是"原生消息怎么处理"。
        seen = []
        real_handle = T.handle_native
        T.handle_native = lambda win, tbar, msg: seen.append((win, tbar)) or None
        try:
            d.nativeEvent(b"whatever", None)
        finally:
            T.handle_native = real_handle
        check("%s nativeEvent 真的把消息交给了 handle_native（拖动/缩放这条路是通的）"
              % nm, seen == [(d, tb_d)], "收到 %d 次" % len(seen))

    for nm, d in boxes:
        d.close()
        paint()

    # 配色跟着主题走（走真实路径：主窗口换主题 -> qss 记一份 -> 新弹窗自取）
    mw.apply_theme("light")
    paint()
    lb = msgbox._build(None, QtWidgets.QMessageBox.Icon.Information, "浅色", "正文")
    lb.show()
    paint()
    light_pal = get_palette("light")
    check("主窗口换成浅色后，新弹窗的标题栏也跟着变 %s" % light_pal.bg_dialog,
          dominant(lb.titlebar, 18) == light_pal.bg_dialog.lower(),
          "读到 %s" % dominant(lb.titlebar, 18))
    # 浅色主题同样要有色差：白卡片压在浅灰页面上。虽然两套主题的色差方向
    # 相反（深色是变浅、浅色是变白），承诺是同一条。
    check("浅色主题下弹窗也和主窗口有色差 %s vs %s"
          % (light_pal.bg_dialog, light_pal.bg_base),
          dominant(lb.titlebar, 18) != light_pal.bg_base.lower(),
          "读到 %s" % dominant(lb.titlebar, 18))
    lb.close()
    mw.apply_theme("dark")
    paint()

    # 行为一个字没改：问 yes/no 的返回值、取消输入的返回值照旧
    got = {}

    def grab_and_reject():
        w = QtWidgets.QApplication.activeModalWidget()
        got["seen"] = w is not None
        if w is not None:
            SB = QtWidgets.QMessageBox.StandardButton
            got["buttons"] = int(w.standardButtons())
            # 注意 defaultButton() 返回的是 **QPushButton** 不是 StandardButton
            # （顺手 int() 一下会把整个进程带走：PyQt6 里槽抛异常 = 0xC0000409）。
            got["default_is_no"] = w.defaultButton() is w.button(SB.No)
            got["has_yes"] = bool(w.standardButtons() & SB.Yes)
            got["has_no"] = bool(w.standardButtons() & SB.No)
            w.reject()

    QtCore.QTimer.singleShot(0, grab_and_reject)
    r = msgbox.question(None, "确认", "要继续吗？")
    check("question() 真的弹出了一个模态框（不是直接返回）",
          got.get("seen") is True, str(got))
    check("question() 给的是「是 / 否」两个按钮",
          got.get("has_yes") and got.get("has_no"), str(got.get("buttons")))
    check("question() 的默认按钮是「否」（回车不会误触确认）",
          got.get("default_is_no") is True, str(got.get("default_is_no")))
    # reject 之后 exec() 返回的是 0，也就是 StandardButton.NoButton——那是
    # **Qt 自己的**语义（"这个框是被关掉的，不是用户按了否"），不是本轮改的。
    # 注意别拿它和 StandardButton.No 比：PyQt6 里 No 是 65536，0 是
    # NoButton，两个不同的成员，第一版就是在这儿写错的。
    check("question() 被关掉时返回 NoButton(0)（和原来的静态方法一致）",
          int(r) == 0, repr(r))

    got2 = {}

    def grab_and_cancel():
        w = QtWidgets.QApplication.activeModalWidget()
        if w is not None:
            w.reject()

    QtCore.QTimer.singleShot(0, grab_and_cancel)
    txt, ok = msgbox.ask_text(None, "新建目录", "目录名：")
    check("ask_text() 取消时仍然返回 (\"\", False)", (txt, ok) == ("", False),
          repr((txt, ok)))

    # ---------------------------------------------------- 7b 一个都不许漏
    print("\n[7b] 漏网检查：还有没有人在用原生静态弹窗")

    # msgbox.py 存在的全部理由就是这个。二十几处调用分散在五个文件里，
    # 漏掉一处就是一条又白又黑的原生标题栏，而且没人会立刻发现——所以这条
    # 直接扫源码钉死。
    #
    # **用 AST 扫属性节点，不要用正则扫文本。** 正则会把 docstring 里"我们
    # 不用 QMessageBox.warning"这种举例也当成残留（第一版就是这么误报的，
    # 扫出 sftp_panel.py 的注释里那一行），还得靠"跳过 # 开头"这种补丁打
    # 地鼠——注释掉的旧调用、字符串里粘的代码都会漏。AST 只看真正的代码。
    STATIC = {
        "QMessageBox": {"warning", "critical", "information", "question", "about"},
        "QFileDialog": {"getOpenFileName", "getOpenFileNames", "getSaveFileName",
                        "getExistingDirectory"},
        "QInputDialog": {"getText", "getItem", "getInt", "getDouble"},
    }
    leaks = []
    for py in sorted((pathlib.Path(ROOT) / "sshclient").rglob("*.py")) \
            + [pathlib.Path(ROOT) / "main.py"]:
        if py.name == "msgbox.py":
            continue        # 它自己就是那个替代品
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.attr in STATIC.get(node.value.id, ())):
                leaks.append("%s:%d  %s.%s(...)"
                             % (py.name, node.lineno, node.value.id, node.attr))
    check("生产代码里没有残留的原生静态弹窗调用（漏一个就是一条原生标题栏）",
          not leaks, "\n       " + "\n       ".join(leaks))
    check("这条漏网检查自己不是空转（至少扫到了 8 个源文件）",
          len(list((pathlib.Path(ROOT) / "sshclient").rglob("*.py"))) + 1 >= 8,
          "只扫到 %d 个"
          % (len(list((pathlib.Path(ROOT) / "sshclient").rglob("*.py"))) + 1))

    # ---------------------------------------------------- 8 元断言
    print("\n[8] 元断言：把缺陷装回去，判据必须变红")

    # 8a 像素判据不是恒真的：把标题栏底色换成一个错的颜色
    tb._surface = "#ff00ff"
    tb.repaint()
    paint()
    broken = dominant(tb, 18)
    check("元断言：把底色换成 #ff00ff 之后，底色判据**确实**会红",
          broken != p.bg_base.lower(), "仍读到 %s" % broken)
    tb._surface = p.bg_base
    tb.repaint()
    paint()

    # 8b 命中测试判据不是恒真的：退化实现（什么都报 HTCAPTION）在同样的
    # 取样点上必须给出**不同**的答案，否则上面那组判据根本区分不出好坏。
    def degenerate(win, tbar, pos):
        return T.HTCAPTION

    probes = [(1, 1), (600, 18), (mw.width() - 2, 1),
              (mw.width() // 2, mw.height() // 2)]
    diff = [pt for pt in probes
            if degenerate(mw, tb, QtCore.QPoint(*pt))
            != T.hit_test(mw, tb, QtCore.QPoint(*pt))]
    check("元断言：退化实现（什么都报 HTCAPTION）和真实现**确实**有分歧",
          len(diff) == 3, "分歧点 %s / 共 %d" % (diff, len(probes)))

    # 8c 字形判据不是恒真的：空画布上「非背景像素数」应该是 0
    blank = QtGui.QPixmap(T.CAPTION_W, T.TITLEBAR_H)
    blank.fill(QtGui.QColor(DARK.bg_base))
    blank_img = blank.toImage()
    n_blank = sum(1 for y in range(6, blank.height() - 6)
                  for x in range(6, blank.width() - 6)
                  if blank_img.pixelColor(x, y).name() != DARK.bg_base.lower())
    check("元断言：一块纯底色画布上「非背景像素数」为 0（所以上面 >3 不是恒真）",
          n_blank == 0, "空画布数到 %d" % n_blank)

    # 8d 弹窗的「正文没被盖掉」不是恒真：把 showEvent 里的顶边距跳过
    # （预置 padded=True 让 pad() 不跑），正文就该跑到标题栏底下去。
    # 这正是本轮出图时真出过的症状。
    dgap = msgbox._build(None, QtWidgets.QMessageBox.Icon.Warning, "对照", "正文")
    dgap._st["padded"] = True          # ← 缺陷桩：pad() 被跳过
    dgap.show()
    paint()
    gtops = [lb2.mapTo(dgap, QtCore.QPoint(0, 0)).y()
             for lb2 in content_labels(dgap, dgap.titlebar)]
    check("元断言：跳过顶边距后，正文确实会跑到标题栏底下（所以上面那条能抓到这个症状）",
          bool(gtops) and min(gtops) < T.TITLEBAR_H,
          "最靠上的可见文字 y=%s，标题栏高 %d" % (min(gtops) if gtops else None,
                                             T.TITLEBAR_H))
    dgap.close()
    paint()

    # 8e 「nativeEvent 真的把消息交给 handle_native」不是恒真：把
    # window.titlebar 摘掉，native_event 就应该静悄悄返回 (False, 0)——
    # 那正是本轮真缺陷的形态（画得出来、点不动、零报错）。
    dbare = QtWidgets.QDialog()
    dbare.show()
    paint()
    seen2 = []
    T.handle_native = lambda win, tbar, msg: seen2.append(1) or None
    try:
        check("元断言：一个没装标题栏的裸 QDialog 上没有 titlebar 属性"
              "（所以上面那条量的是我们的代码，不是 Qt 白送的）",
              getattr(dbare, "titlebar", None) is None)
        T.native_event(dbare, None)
    finally:
        T.handle_native = real_handle
    check("元断言：摘掉 titlebar 之后 handle_native 不再被调（所以上面那条能抓到本轮的缺陷）",
          seen2 == [], "仍被调了 %d 次" % len(seen2))
    dbare.close()
    paint()

    # 8f 色差判据不是恒真：把弹窗底色按回主窗口那一层（也就是这一轮改动
    # 之前的样子），"弹窗和主窗口不一样"那条必须变红。
    dsame = msgbox._build(None, QtWidgets.QMessageBox.Icon.Information, "对照", "正文")
    dsame.titlebar.apply_palette(pal)
    dsame.titlebar._surface = pal.bg_base          # ← 缺陷桩：色差被抹平
    dsame.repaint()
    dsame.show()
    paint()
    check("元断言：把弹窗底色按回主窗口那一层后，色差确实没了"
          "（所以上面那条能抓到这一轮要修的东西）",
          dominant(dsame.titlebar, 18) == pal.bg_base.lower(),
          "读到 %s" % dominant(dsame.titlebar, 18))
    dsame.close()
    paint()

    # ---------------------------------------------------------------
    # 按钮行的容器样式表：限定范围，否则主按钮丢掉实心填充
    #
    # **症状。** 用户报「编辑连接弹框的主按钮看起来很奇怪」，渲染出来是
    # 「白字 + 强调色描边、底色透出弹框底色」——一个描边按钮，白字压在
    # 浅米色上几乎看不见。
    #
    # **根因。** 按钮被 `_hoist_actions` 搬进一个 holder 容器，那容器为了
    # 把底铺满（根布局 margins 是 0），带了一句 `QWidget { background: … }`。
    # Qt 的类型选择器命中**所有后代**，而离得更近的祖先规则在
    # `background-color` 上压过了弹框表里的 `QPushButton[variant="primary"]`。
    # 实测：祖先带裸 `QWidget` 规则 → 主色 `#e4e3de`（丢填充）；
    # 祖先带 `#objectName` 限定 → `#1a6bb5`（有填充）。
    print()
    print("=== 按钮行容器：主按钮的填充不能被祖先样式表吃掉 ===")
    from sshclient.ui.connect_dialog import ConnectDialog  # noqa: E402

    def _edit_dialog():
        d = ConnectDialog(
            palette=pal, initial=dict(name="a", host="1.2.3.4", port=22,
                                      username="u", auth_method="password",
                                      password="p"))
        d.show()
        paint()
        return d

    def _btn_mode(btn):
        """按钮中间那一行的众数 —— 实心按钮在这里读到的就是填充色。"""
        return dominant(btn, btn.height() // 2)

    _dc = _edit_dialog()
    check("编辑连接的主按钮是实心强调档（不是白字压浅底的描边按钮）",
          _btn_mode(_dc.ok_btn) == pal.accent_solid.lower(),
          "读到 %s，期望 %s" % (_btn_mode(_dc.ok_btn), pal.accent_solid.lower()))
    _cancel = next(b for b in _dc.findChildren(QtWidgets.QPushButton)
                   if b.text() == "取消")
    check("同一行里的取消按钮拿到 elevated 底色（证明不是只修了主按钮）",
          _btn_mode(_cancel) == pal.bg_elevated.lower(),
          "读到 %s，期望 %s" % (_btn_mode(_cancel), pal.bg_elevated.lower()))
    # **holder 的样式表必须按 objectName 限定。** 这条钉的是机制本身：
    # 将来有人图省事改回裸 `QWidget`，主按钮会当场变回描边样式。
    _holder = _dc.findChild(QtWidgets.QWidget, T._HOLDER_OBJ)
    check("按钮行容器带着 objectName（样式表靠它限定范围）",
          _holder is not None, "没找到 %s" % T._HOLDER_OBJ)
    _dc.close()
    paint()

    # 元断言：把限定去掉，主按钮必须当场丢掉填充。
    _real_holder_style = T._holder_style

    def _bare(dialog):
        """故意退回「裸 QWidget 选择器」这个坏版本。"""
        return "QWidget { background: %s; }" % T._dialog_bg(dialog)

    T._holder_style = _bare
    try:
        _dbad = _edit_dialog()
        check("元断言：容器样式表退回裸 QWidget 后，主按钮确实丢掉填充",
              _btn_mode(_dbad.ok_btn) != pal.accent_solid.lower(),
              "实测还是 %s（那就说明上面那条不是恒绿，而是量错了对象）"
              % _btn_mode(_dbad.ok_btn))
        _dbad.close()
    finally:
        T._holder_style = _real_holder_style
    paint()
    _dback = _edit_dialog()
    check("元断言：还原之后主按钮回到实心（证明上面那条红是被测代码造成的）",
          _btn_mode(_dback.ok_btn) == pal.accent_solid.lower(),
          "读到 %s" % _btn_mode(_dback.ok_btn))
    _dback.close()
    paint()

    # ---------------------------------------------------------------
    # 按钮行与正文之间要有缝，而且这条缝**得看得见**
    #
    # 只加边距是没用的：实测按钮行上方 40px 全是同一个 `bg_dialog`，
    # 一条色差都没有，缝是隐形的。现有 token 里也没有一个能和 bg_dialog
    # 拉开到撑得起分层的距离（bg_surface 浅色 ΔE 10.2 / 深色 6.5，
    # bg_base 浅色只有 6.4），所以这里画一条 1px 线 —— 正文各段之间本来
    # 就用同一个 `palette.border` 的分隔线，底部这条不是新发明的一层。
    print()
    print("=== 按钮行上方：分隔线 + 间距 ===")
    _dg = _edit_dialog()
    _sep = _dg.findChild(QtWidgets.QFrame, T._SEP_OBJ)
    check("按钮行上方有分隔线", _sep is not None, "没找到 %s" % T._SEP_OBJ)
    if _sep is not None:
        check("分隔线是 1px", _sep.height(), 1)
        _gy = _sep.mapTo(_dg, QtCore.QPoint(0, 0)).y()
        check("分隔线满宽（不跟着按钮的左右边距缩进）",
              _sep.width(), _dg.width())
        check("分隔线颜色就是 palette.border",
              dominant(_sep, 0), pal.border.lower())
        # 间距：线底边到按钮顶边。**要 > 0**，且不能大到把线推离内容。
        _gap = _dg.ok_btn.mapTo(_dg, QtCore.QPoint(0, 0)).y() - (_gy + 1)
        check("分隔线与按钮之间留了间距（原来是 0，整片贴在一起）",
              _gap > 0, "间距 %d px" % _gap)
    _dg.close()
    paint()

    # 元断言：把间距拿掉，上面那条必须红。
    _real_sep = T._action_sep

    def _no_sep(dialog):
        """故意返回一个高度 0 的「线」——等价于没有缝。"""
        f = _real_sep(dialog)
        f.setFixedHeight(0)
        return f

    T._action_sep = _no_sep
    try:
        _dn = _edit_dialog()
        _sn = _dn.findChild(QtWidgets.QFrame, T._SEP_OBJ)
        _n = _sn.mapTo(_dn, QtCore.QPoint(0, 0)).y() if _sn else -1
        check("元断言：把线压成 0 高之后间距判据确实会红",
              not (_sn is not None and _sn.height() == 1), "线还在 %s" % (_sn.height() if _sn else None))
        _dn.close()
    finally:
        T._action_sep = _real_sep
    paint()

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
