"""标题栏的命中测试：双击/拖动必须处处可用，缩放必须还在。

用户报「标题栏，双击和放大缩小的按钮，时灵时不灵」。

## 根因不是"有时候坏"，是标题栏顶部一整条死区

`hit_test` 的判定顺序是「极角 → 按钮 → 缩放边框 → 标题栏 → 客户区」。
而标题栏是 `setGeometry(0, 0, W, 38)` **满宽铺在窗口最上面**——所以窗口顶边
100% 落在标题栏内。缩放边框里那条 `y <= RESIZE_MARGIN(6)` → `HTTOP` 的判定
又排在标题栏**前面**，于是标题栏内 y=0..6 这 7 行被"拖顶边改高度"吃掉。

逐像素实测（旧）：标题栏 48640px 里有 **7945px 判成 HTTOP**（16.3%）。
双击那 7 行不最大化，按住拖也不是移动窗口。用户看到的"时灵时不灵"就是它。

## 取舍：顶边缩放 vs 标题栏拖动，物理上互斥

同一批像素，只能选一个。选标题栏，理由写死在 `hit_test` 里：

1. 双击最大化、拖动窗口是高频交互，顶边缩放是低频的；
2. 无边框自绘标题栏**没有任何视觉提示**说"最上面这几像素能拖"，
   原生窗口有边框线；
3. 缩放能力没消失——左右、下边、四个角全在，四角是 e2c1832 才补齐的。

改后实测：死区 7945px → **266px（0.55%）**，且剩下的全在左端 7 列
（`HTLEFT`，那里没有 logo 以外的控件，原生 Windows 那儿也是缩放带）。

## 第二个改动：`WM_NCHITTEST` 用 lParam，不用 QCursor.pos()

Windows 发 `WM_NCHITTEST` 时 lParam 里带的就是"正在问的那个点"；
`QCursor.pos()` 回答的是"鼠标**此刻**在哪"。鼠标在动时两者不是一回事，
而 Windows 会**缓存**返回的命中码去处理随后的点击——"快速移进最大化按钮
再点"就落空。`GET_X_LPARAM` 那个有符号 16 位解法正是 `DefWindowProc` 自己
的解法；多显示器截断的担忧用「解出来的点离窗口太远就回退」解决。

## 判据钉什么

1. 标题栏里不在按钮上的像素，判成 HTCAPTION 的比例（**钉死区不许回来**）
2. 元断言：把顶边判定加回去，比例要掉下去 —— 证明它量的是死区不是恒绿
3. 四角 + 左/右/下三边仍可缩放（别把缩放改坏了）
4. 顶边现在判 HTCAPTION（明确钉住这个取舍）
5. 三个 caption 按钮命中正确（max 走 HTMAXBUTTON 那条手动路径）
6. 关闭按钮右上角的角死区有界（CORNER_GRAB 是有意取舍，但要有上限）
7. `_hittest_pos` 的 lParam 解包：正常坐标 / 副屏负坐标 / 超范围回退
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import settings
from sshclient.ui import main_window
from sshclient.ui import titlebar as tb_mod

CODE = {1: "HTCLIENT", 2: "HTCAPTION", 9: "HTMAXBUTTON", 10: "HTLEFT",
        11: "HTRIGHT", 12: "HTTOP", 15: "HTBOTTOM", 13: "HTTOPLEFT",
        14: "HTTOPRIGHT", 16: "HTBOTTOMLEFT", 17: "HTBOTTOMRIGHT"}

bad = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}"
          + (f"  {detail}" if detail else ""))
    if not ok:
        bad.append(f"{name}  {detail}")


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


def settle(ms=250):
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


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
    host.resize(1280, 800)
    host.show()
    settle(450)
    return mw, host


def caption_share(mw):
    """标题栏里「不在按钮上」的像素中，判成 HTCAPTION 的比例。"""
    tb = mw.titlebar
    w, bh = mw.width(), tb_mod.TITLEBAR_H
    cap = tot = 0
    for y in range(bh):
        for x in range(w):
            if any(b.geometry().contains(QtCore.QPoint(x, y))
                   for b in tb.buttons):
                continue
            tot += 1
            if tb_mod.hit_test(mw, tb, QtCore.QPoint(x, y)) == 2:
                cap += 1
    return cap, tot


for theme in ("light", "dark"):
    print(f"\n================ {theme} ================")
    mw, host = build(theme)
    tb = mw.titlebar
    w, h, bh = mw.width(), mw.height(), tb_mod.TITLEBAR_H
    print(f"  win {w}x{h}  titlebar h={bh}  "
          + "  ".join(f"{b.kind}={b.geometry().getRect()}" for b in tb.buttons))

    # ---------------------------------------------------------- 1. 死区
    cap, tot = caption_share(mw)
    share = cap * 100.0 / tot
    check(f"标题栏可双击面积 {cap}/{tot} = {share:.2f}% >= 99%",
          share >= 99.0, f"{share:.2f}%")

    # 元断言：把顶边判定加回去，死区必须回来
    _orig = tb_mod.hit_test

    def with_top(win, t, pos, _o=_orig):
        """旧版逻辑：多判一条 `y <= RESIZE_MARGIN` -> HTTOP。"""
        if (pos.y() <= tb_mod.RESIZE_MARGIN
                and not (pos.x() <= tb_mod.CORNER_GRAB)
                and not (pos.x() >= win.width() - tb_mod.CORNER_GRAB)):
            return 12
        return _o(win, t, pos)

    tb_mod.hit_test = with_top
    try:
        cap_old, tot_old = caption_share(mw)
    finally:
        tb_mod.hit_test = _orig
    share_old = cap_old * 100.0 / tot_old
    print(f"  元断言对照：旧逻辑 {cap_old}/{tot_old} = {share_old:.2f}%")
    check(f"元断言：旧逻辑死区明显更大（{share_old:.2f}% < {share:.2f}%）",
          share_old < share - 5.0, f"{share_old:.2f} vs {share:.2f}")
    cap, tot = caption_share(mw)
    check("还原后判据仍然是绿的（没把 hit_test 留在坏版本）",
          cap * 100.0 / tot >= 99.0)

    # ---------------------------------------------------------- 3/4. 缩放
    corners = {"左上": (1, 1), "右上": (w - 2, 1),
               "左下": (1, h - 2), "右下": (w - 2, h - 2)}
    for name, (x, y) in corners.items():
        c = tb_mod.hit_test(mw, tb, QtCore.QPoint(x, y))
        check(f"{name}角仍可缩放",
              CODE.get(c) in ("HTTOPLEFT", "HTTOPRIGHT",
                              "HTBOTTOMLEFT", "HTBOTTOMRIGHT"),
              CODE.get(c, c))
    edges = {"左边": (2, h // 2), "右边": (w - 3, h // 2),
             "下边": (w // 2, h - 3)}
    for name, (x, y) in edges.items():
        c = tb_mod.hit_test(mw, tb, QtCore.QPoint(x, y))
        check(f"{name}仍可缩放", CODE.get(c) in ("HTLEFT", "HTRIGHT", "HTBOTTOM"),
              CODE.get(c, c))
    # 顶边：**有意**改成 HTCAPTION（取舍写在 hit_test 里）
    c = tb_mod.hit_test(mw, tb, QtCore.QPoint(w // 2, 2))
    check("顶边现在判 HTCAPTION（双击最大化优先于顶边缩放）",
          c == 2, CODE.get(c, c))

    # ---------------------------------------------------------- 5. 按钮
    for b in tb.buttons:
        g = b.geometry()
        want = 9 if b.kind == "max" else 1
        bad_px = sum(
            1 for lx in range(g.left(), g.right() + 1)
            for ly in range(g.top(), g.bottom() + 1)
            if tb_mod.hit_test(mw, tb, tb.mapTo(mw, QtCore.QPoint(lx, ly)))
            != want)
        area = g.width() * g.height()
        if b.kind == "close":
            # 右上角 CORNER_GRAB 见方是有意让给缩放的，取舍要有上限
            check(f"close 按钮角死区有界（{bad_px}/{area} = "
                  f"{bad_px * 100.0 / area:.2f}% <= 2%）",
                  bad_px * 100.0 / area <= 2.0,
                  f"{bad_px}px -> {CODE.get(tb_mod.hit_test(mw, tb, tb.mapTo(mw, QtCore.QPoint(g.right(), 0))), '?')}")
        else:
            check(f"{b.kind} 按钮命中正确", bad_px == 0, f"{bad_px}px")

    # ------------------------------------------------- 7. lParam 解包
    def encode(gp: QtCore.QPoint) -> int:
        """把屏幕坐标编成 Windows 那样的 lParam（两个有符号 16 位）。"""
        return ((gp.y() & 0xFFFF) << 16) | (gp.x() & 0xFFFF)

    ok_pts = [(mw.width() // 3, 19), (mw.width() - 30, 20), (40, 30)]
    for lx, ly in ok_pts:
        gp = mw.mapToGlobal(QtCore.QPoint(lx, ly))
        got = tb_mod._hittest_pos(mw, encode(gp))
        check(f"lParam 解出窗口内坐标 ({lx},{ly})",
              got == gp, f"期望 {gp.x()},{gp.y()} 得到 {got.x()},{got.y()}")

    # **mw 被嵌在 host 里，不是顶层窗口。** 校验曾经用
    # `win.frameGeometry()`，而对子控件它返回的是**相对父窗口**的矩形，
    # 于是窗口内每个点都被误判成「被截断了」，整个函数退化成永远回退 ——
    # 而回退正是这条改动要修掉的那条路，等于白改。判据必须盖住它，
    # 判据本身也**只能在非顶层窗口上跑**，否则测不到。
    check("被测窗口确实不是顶层（这条判据才盖得到那个坑）",
          mw.window() is host, f"window()={type(mw.window()).__name__}")
    _fg = mw.frameGeometry()
    _screen_rect = QtCore.QRect(mw.mapToGlobal(QtCore.QPoint(0, 0)), mw.size())
    check("被测窗口 frameGeometry 不是屏幕坐标（踩坑的前提成立）",
          _fg != _screen_rect,
          f"frameGeometry={_fg.getRect()} 真实屏幕矩形={_screen_rect.getRect()}")

    # 副屏在左侧时坐标是负的 —— 有符号 16 位解包要还原成负数
    def decode(lp):
        import ctypes
        return (ctypes.c_short(lp & 0xFFFF).value,
                ctypes.c_short((lp >> 16) & 0xFFFF).value)
    for pt in [(100, 200), (-1340, 200), (1920, -5), (-1, -1)]:
        gx, gy = decode(encode(QtCore.QPoint(*pt)))
        check(f"lParam 往返 {pt}", (gx, gy) == pt, f"解出 {gx},{gy}")

    # 超范围（超大分辨率多屏）必须回退到 QCursor.pos()，而不是拿截断值
    huge = ((0x7FFF) << 16) | 0x7FFF          # (32767, 32767)，不落在任何屏上
    check("构造的越界点确实不落在任何显示器上（回退前提成立）",
          QtGui.QGuiApplication.screenAt(QtCore.QPoint(0x7FFF, 0x7FFF)) is None)
    got = tb_mod._hittest_pos(mw, huge)
    cur = QtGui.QCursor.pos()
    check("lParam 被截断（点不落在任何显示器上）时回退到 QCursor.pos()",
          got == cur, f"得到 {got.x()},{got.y()} 期望 {cur.x()},{cur.y()}")

    mw.close()
    host.close()

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("titlebar hit test: all checks passed")
sys.exit(1 if bad else 0)
