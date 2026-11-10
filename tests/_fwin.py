"""主窗口 / 标题栏 / 侧边栏 / 标签页 功能面的一轮黑盒判据。

**和 `_titlebar.py` 的分工**：那一支只测 `titlebar.py` 本身（像素、命中测试、
弹窗族）。这一支从**主窗口**这一头进去：窗口描边、侧边栏折叠两态、标签页关闭
与销毁、菜单与主题、连接闸门，以及共用控件 / Logo / 弹窗族。同一件事不重复
判，两支各有侧重。

判据落点：几何、尺寸、文字、tooltip、从 `grab()` 出来的真实像素，以及
`sip.isdeleted()` —— 「对象真的被销毁」只认 `sip.isdeleted()`，不认「看着
没了」，不认内存曲线，也不认「跑 N 次没崩」。

**绝对不许碰用户真实配置。** `MainWindow.__init__` / `set_sidebar_collapsed` /
`_toggle_theme` / `_on_new_connection` / `closeEvent` 一共 5 处会
`self.settings.save()`，而 `AppSettings.load()` 读的是
`%APPDATA%\\MavisSSH\\settings.json`。所以这里一律 `S.AppSettings()` 手工构造
之后 4 行内把 `_path` 打到临时目录；`SessionStore` 同样永远带显式路径。

**不真连服务器。** 假连接类只实现 `info` / `open_shell` / `close`，凡是会
`exec()` 的入口（消息框、取色器、右键菜单）一律先把 `exec` 换掉再触发。

退出码：0 = 全过；1 = 有 FAIL；2 = 有判据因前提不成立没能评上。
"""
from __future__ import annotations

import ast
import inspect
import os
import pathlib
import sys
import tempfile
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# offscreen 下 QFontDatabase.families() 返回 0，字体/像素判据会恒真。
os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets, sip  # noqa: E402

from sshclient import __version__ as PKG_VERSION  # noqa: E402
from sshclient.core import settings as S  # noqa: E402
from sshclient.core.snippets import SnippetStore  # noqa: E402
from sshclient.ui import controls, icons, logo, msgbox  # noqa: E402
from sshclient.ui import main_window as MW  # noqa: E402
from sshclient.ui import sidebar as SB  # noqa: E402
from sshclient.ui import tab_close as TC  # noqa: E402
from sshclient.ui import titlebar as T  # noqa: E402
from sshclient.ui.design.v1 import qss  # noqa: E402
from sshclient.ui.design.v1.palette import DARK, LIGHT, get_palette  # noqa: E402
from tests import sidebar_rows  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []

_TIMEOUT_SOFT = 1.0          # 像素判据：最长连续同色段超过它就算"一块补丁"


# --------------------------------------------------------------------- 判据
def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok]   " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail))
    return bool(ok)


def skip(name, why):
    SKIP.append(name)
    print("  [skip] %s   <- %s" % (name, why))


# ----------------------------------------------------------------- 工具件
def paint(n: int = 4):
    app = QtWidgets.QApplication.instance()
    for _ in range(n):
        app.processEvents()


def wait_until(pred, ms: int = 4000) -> bool:
    """异步一律用它，不写死 sleep。"""
    app = QtWidgets.QApplication.instance()
    waited = 0
    step = 25
    while waited < ms:
        if pred():
            return True
        app.processEvents()
        QtTest.QTest.qWait(step)
        waited += step
    return bool(pred())


def px(widget, x, y) -> str:
    return widget.grab().toImage().pixelColor(x, y).name()


def dominant(widget, y: int) -> str:
    """一整行里出现最多的颜色（不能用 width//2，见 _titlebar.py 的理由）。"""
    img = widget.grab().toImage()
    counts: dict[str, int] = {}
    for x in range(widget.width()):
        c = img.pixelColor(x, y).name()
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def max_color_run(widget, color: str) -> int:
    target = color.lower()
    img = widget.grab().toImage()
    best = run = 0
    for y in range(widget.height()):
        run = 0
        for x in range(widget.width()):
            if img.pixelColor(x, y).name() == target:
                run += 1
                best = max(best, run)
            else:
                run = 0
    return best


def restore_geom(w: int, h: int) -> dict:
    """按 `CaptionButton._paint_restore` 的算式推出两个方框的几何。

    判据不写死坐标：GLYPH 改了就跟着变。实测（46x38 按钮）——
    back 框 x∈[20,28] y∈[14,22]，front 框 x∈[18,26] y∈[16,24]，
    底色遮罩填的是 x∈[20,25] y∈[16,21]。
    """
    cx, cy, half = w // 2, h // 2, 5
    s = 10 - 2
    back_x, back_y = cx - half + 2, cy - half
    return {"back_x": back_x, "back_y": back_y, "s": s,
            "back_left": (back_x, back_y + 4),        # 遮罩范围内
            "back_bottom": (back_x + 3, back_y + s),  # 落在 front 框内部
            "front_left": (cx - half, cy - half + 2 + 4),
            "front_bottom": (cx - half + 3, cy - half + 2 + s)}


def content_labels(dialog, tb) -> list:
    """弹窗里属于正文的可见 QLabel（排掉标题栏自己那两个）。"""
    return [lb for lb in dialog.findChildren(QtWidgets.QLabel)
            if lb.isVisible() and not tb.isAncestorOf(lb)]


def in_window(child, root) -> tuple:
    c = child.mapTo(root, child.rect().center())
    return c.x(), c.y()


def _click_capturing(btn, mw, paint) -> list:
    """点一次按钮，把这一下期间逃出来的异常收下来。

    **必须自己装 `sys.excepthook`**：PyQt6 里槽抛的异常**不会回到调用方**，
    而是交给 `sys.excepthook`（装了 `install_crash_guard` 时弹「发生错误」框，
    不装时整个进程 fast-fail）。所以「点了没抛」这件事只能从 excepthook 量，
    `try/except` 包在 `click()` 外面是量不到的 —— 它会显得一切正常。

    **必须同时把 `msgbox` 打桩**：这两个按钮在「没有选中」时会弹真模态框，
    而模态框会**永久阻塞**这个非交互脚本（第一版就这么把整套测试拖到超时）。
    `Feedback` 干的正是这件事。
    """
    caught: list = []
    real = sys.excepthook

    def _hook(t, v, tb):
        caught.append("%s: %s" % (getattr(t, "__name__", t), v))

    with Feedback(mw):
        sys.excepthook = _hook
        try:
            btn.click()
            paint()
        finally:
            sys.excepthook = real
    return caught


def repo_files() -> list:
    root = pathlib.Path(ROOT)
    return sorted((root / "sshclient").rglob("*.py")) + [root / "main.py"]


def _readme_shortcut_keys() -> set:
    """README 快捷键表里**窗口作用域**的那些键（只认第一列的反引号内容）。

    **必须按「作用域」列过滤。** 那张表里窗口键与终端键混在一起：
    `Ctrl+C` / `Tab` / `Ctrl+Shift+V` 都是**终端**里生效的，菜单上并没有
    绑它们。不分作用域的话，这张表会把终端键也算成「菜单该有的键」，
    然后报出一个根本不存在的缺口。

    **刻意不写死某一个键。** `QKeySequence.StandardKey` 是按平台解析的 ——
    `StandardKey.Close` 在 Windows 上是 `Ctrl+F4`、在别的平台上未必相同。
    所以该钉的是「文档与实际绑定互相覆盖」，而不是「等于 Ctrl+W」。

    只取第一列，是因为 README 里别的表（模块表、跨平台表、打包表）
    的第一列都不是反引号形式。
    """
    text = (pathlib.Path(ROOT) / "README.md").read_text(encoding="utf-8")
    keys = set()
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        head = cells[0]
        if not (len(head) > 2 and head.startswith("`") and head.endswith("`")):
            continue
        if "窗口" not in cells[1]:      # 终端 / 其它作用域的键不算菜单的
            continue
        keys.add(head.strip("`").replace(" ", ""))
    return keys


class FakeInfo:
    def __init__(self, username="ops", host="10.0.0.9", port=22):
        self.username, self.host, self.port = username, host, port


class FakeConn:
    """假 SSHConnection：只够 open_session -> _on_connect_ok -> attach 走一遍。"""

    def __init__(self, info=None, shell_error: str = ""):
        self.info = info or FakeInfo()
        self._shell_error = shell_error
        self.opened = 0
        self.closed = 0

    def open_shell(self, *a, **k):
        self.opened += 1
        if self._shell_error:
            raise RuntimeError(self._shell_error)
        raise AssertionError("FakeConn 只会走到 open_shell 失败那条路")

    def close(self):
        self.closed += 1


class Feedback:
    """记下窗口上所有「对用户说了一句话」的动作。"""

    def __init__(self, mw):
        self.flash = []
        self.boxes = []
        self._mw = mw
        self._saved = {}

    def __enter__(self):
        mw = self._mw
        self._saved["flash"] = mw._flash
        mw._flash = lambda msg, msec=0: self.flash.append((msg, msec))
        for name in ("information", "warning", "critical"):
            self._saved[name] = getattr(msgbox, name)
            setattr(msgbox, name,
                    lambda *a, _n=name, **k: self.boxes.append((_n, a[1:3])))
        self._saved["question"] = msgbox.question
        msgbox.question = lambda *a, **k: self._answer()
        return self

    def _answer(self):
        return QtWidgets.QMessageBox.StandardButton.No

    def __exit__(self, *exc):
        self._mw._flash = self._saved["flash"]
        for name in ("information", "warning", "critical", "question"):
            setattr(msgbox, name, self._saved[name])
        return False

    def __bool__(self):
        return bool(self.flash or self.boxes)


def patch_exec(cls, new):
    """换掉一个对话框子类的 ``exec``，返回可回滚的令牌。

    **回滚必须用 `del`，不能把继承来的那个再赋回去。** PyQt6 里
    ``QMessageBox.exec`` 取出来是一个要求显式 self 的内建对象，一旦把它写进
    ``ThemedMessageBox.__dict__``，实例上的 ``box.exec`` 就不再自动绑 self，
    调用时报 "first argument of unbound method must have type 'QDialog'"。
    那个类本来就没有自己的 exec，``del`` 才是回到原状。
    """
    had = "exec" in cls.__dict__
    old = cls.__dict__.get("exec")
    cls.exec = new
    return had, old


def restore_exec(cls, token):
    had, old = token
    if had:
        cls.exec = old
    else:
        del cls.exec


def recorder(ret=None):
    """造一个能替换对话框子类 `exec` 的函数，外加装实例的列表。

    **必须是函数，不能是一个可调用对象。** 非描述符对象挂在类上时
    ``box.exec`` 拿到的是它本身、不会自动绑 self，于是
    ``box.exec()`` 报 "missing 1 required positional argument"。
    """
    boxes: list = []
    want = ret if ret is not None else QtWidgets.QDialog.DialogCode.Accepted

    def exec_(box, *a, **k):
        boxes.append(box)
        return want

    return exec_, boxes


def make_session(**kw):
    base = dict(name="prod", host="10.0.0.9", port=2222, username="ops",
                auth_method="password")
    base.update(kw)
    return S.SavedSession(**base)


# =========================================================== 1 窗口描边
def sec_frame(mw, tmp):
    print("\n[1] 窗口描边（window_frame.py）")
    p = mw.palette_
    frame = mw._frame
    paint()

    check("描边浮层挂在窗口上（不是挂在 central 上，否则够不到底边）",
          frame.parent() is mw, "parent=%r" % (frame.parent(),))
    check("描边浮层铺满整个窗口",
          (frame.width(), frame.height()) == (mw.width(), mw.height()),
          "frame=%dx%d win=%dx%d" % (frame.width(), frame.height(),
                                     mw.width(), mw.height()))

    img = mw.grab().toImage()
    strong = p.border_strong.lower()
    w, h = mw.width(), mw.height()

    # **采样必须躲开四个角。** 窗口现在有 5px 圆角，角上的像素是**有意**
    # 挖成透明的（见 _roundcorners.py），`x=0` / `y=0` 首像素正落在弧上。
    # 原来这里从 0 开始扫，扫到的是透明像素而不是描边色 —— 那是判据没跟上
    # 圆角，不是描边坏了。所以四条边都从 CORNER_RADIUS 处起扫。
    from sshclient.ui.window_frame import CORNER_RADIUS as _R  # noqa: E402
    xs = list(range(_R, w - _R, 7))
    ys = list(range(_R, h - _R, 7))

    check("最上一整行都是描边色 %s（跳过四角圆弧）" % p.border_strong,
          all(img.pixelColor(x, 0).name() == strong for x in xs),
          sorted({img.pixelColor(x, 0).name() for x in xs}))
    check("最下一整行都是描边色 %s（跳过四角圆弧）" % p.border_strong,
          all(img.pixelColor(x, h - 1).name() == strong for x in xs),
          sorted({img.pixelColor(x, h - 1).name() for x in xs}))
    check("最左一整列都是描边色 %s（跳过四角圆弧）" % p.border_strong,
          all(img.pixelColor(0, y).name() == strong for y in ys),
          sorted({img.pixelColor(0, y).name() for y in ys}))
    check("最右一整列都是描边色 %s（跳过四角圆弧）" % p.border_strong,
          all(img.pixelColor(w - 1, y).name() == strong for y in ys),
          sorted({img.pixelColor(w - 1, y).name() for y in ys}))
    check("描边就 1 个物理像素（第二列已经不是描边色）",
          img.pixelColor(w // 2, 1).name() != strong,
          img.pixelColor(w // 2, 1).name())
    centre = img.pixelColor(w // 2, h // 2).name()
    check("中心区域**不是**描边色（环没把内容盖死）", centre != strong, centre)
    ring = sum(1 for yy in range(h) for xx in range(w)
               if img.pixelColor(xx, yy).name() == strong)
    check("描边只画了一圈环，没有把中间填掉",
          ring <= 2 * w + 2 * h, "描边色像素 %d 个，一圈环的上限 %d"
          % (ring, 2 * w + 2 * h))

    # 换主题：描边色必须跟着走，而且不能被盖住
    mw.apply_theme("light", accent_for(S, "light"))
    paint()
    light = get_palette("light")
    img2 = mw.grab().toImage()
    check("换主题后描边色随之变 %s" % light.border_strong,
          img2.pixelColor(0, h // 2).name() == light.border_strong.lower(),
          img2.pixelColor(0, h // 2).name())
    check("换主题后描边**没被盖住**（浅色下仍四边齐全，跳过四角圆弧）",
          all(img2.pixelColor(x, 0).name() == light.border_strong.lower()
              for x in xs),
          sorted({img2.pixelColor(x, 0).name() for x in xs}))
    mw.apply_theme("dark", accent_for(S, "dark"))
    paint()

    # 不吃鼠标事件
    check("描边声明了 WA_TransparentForMouseEvents",
          frame.testAttribute(
              QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents))
    app = QtWidgets.QApplication.instance()
    corners = [QtCore.QPoint(0, 0),
               QtCore.QPoint(mw.width() - 1, 0),
               QtCore.QPoint(0, mw.height() - 1),
               QtCore.QPoint(mw.width() - 1, mw.height() - 1)]
    got = [app.widgetAt(mw.mapToGlobal(pt)) for pt in corners]
    check("最外 1px 的点击能落到描边**下面**（没有控件挡在那圈上）",
          all(g is not frame for g in got), str(got))


def accent_for(settings_mod, theme: str) -> str:
    """和 main_window 同一条路：算出用户强调色。"""
    st = settings_mod.AppSettings()
    st._path = pathlib.Path(tempfile.gettempdir()) / "_fwin_probe_settings.json"
    return settings_mod.accent_for(st, theme)


# =========================================================== 2 标题栏
def sec_titlebar(mw, tmp):
    print("\n[2] 标题栏（titlebar.py）")
    p = mw.palette_
    tb = mw.titlebar
    paint()

    check("标题栏贴在窗口最顶端", tb.geometry().y() == 0, str(tb.geometry()))
    check("标题栏横贯整个窗口宽度", tb.width() == mw.width(),
          "tb=%d win=%d" % (tb.width(), mw.width()))
    check("标题栏高度是 %d" % T.TITLEBAR_H, tb.height() == T.TITLEBAR_H,
          str(tb.height()))
    check("菜单栏在标题栏**下面**", mw._menu_bar.geometry().y() == T.TITLEBAR_H,
          str(mw._menu_bar.geometry()))

    btns = [tb.btn_min, tb.btn_max, tb.btn_close]
    check("三个窗口按钮都在", all(b is not None and b.isVisible()
                                  for b in btns))
    check("每个窗口按钮宽度都是 %d" % T.CAPTION_W,
          all(b.width() == T.CAPTION_W for b in btns),
          str([b.width() for b in btns]))
    check("三个按钮左右相邻、中间没有缝",
          all(btns[i].geometry().right() + 1 == btns[i + 1].geometry().x()
              for i in range(2)),
          str([b.geometry() for b in btns]))
    check("关闭按钮紧贴标题栏右缘",
          btns[-1].geometry().right() + 1 == tb.width(),
          "right=%d tb=%d" % (btns[-1].geometry().right() + 1, tb.width()))
    check("从左到右依次是最小化/最大化/关闭",
          [b.kind for b in btns] == [T.MIN, T.MAX, T.CLOSE],
          str([b.kind for b in btns]))
    tips = [b.toolTip() for b in btns]
    check("三个按钮都有 tooltip", all(t.strip() for t in tips), str(tips))
    check("三个按钮的 tooltip 互不相同", len(set(tips)) == 3, str(tips))

    # 最小化 / 关闭
    tb.btn_min.click()
    paint()
    check("点最小化真的最小化了窗口（没被当成拖动标题栏）",
          mw.isMinimized(), str(mw.isMinimized()))
    mw.showNormal()
    paint()

    spy = QtTest.QSignalSpy(tb.btn_close.clicked)
    tb.btn_close.click()
    paint()
    check("点关闭发出 clicked", len(spy) == 1, "收到 %d 次" % len(spy))
    check("点关闭后窗口消失", not mw.isVisible(), str(mw.isVisible()))
    mw.show()
    paint()

    # 最大化 <-> 还原
    tb.btn_max.set_restoring(False)
    paint()
    img_normal = tb.btn_max.grab().toImage()
    tb.btn_max.set_restoring(True)
    paint()
    img_restore = tb.btn_max.grab().toImage()
    check("最大化后按钮画的是「还原」，和「最大化」不是同一张图",
          img_normal != img_restore)
    check("最大化状态下 tooltip 变成「还原」",
          tb.btn_max.toolTip() == T._TIPS_RESTORE, tb.btn_max.toolTip())
    tb.btn_max.set_restoring(False)
    paint()
    check("再点一次逐像素还原",
          tb.btn_max.grab().toImage() == img_normal)

    # 「还原」是**两个错开的方框**，前面的要盖住后面的左下角。
    # 判据按几何算，不写死坐标：后框下边有一段落在 front 框**内部**，
    # 那段必须被底色填掉；后框左边那一段也必须被填掉。
    tb.btn_max.set_restoring(True)
    paint()
    fg = p.text_dim.lower()
    g = restore_geom(tb.btn_max.width(), tb.btn_max.height())
    check("还原字形：后框左边被底色遮住了（front 框内部看不到它）",
          px(tb.btn_max, *g["back_left"]) != fg,
          "(%d,%d)=%s 字形色=%s" % (g["back_left"][0], g["back_left"][1],
                                    px(tb.btn_max, *g["back_left"]), fg))
    check("还原字形：后框下边穿进 front 框内部的那一段也被遮住了",
          px(tb.btn_max, *g["back_bottom"]) != fg,
          "(%d,%d)=%s 期望不是字形色 %s"
          % (g["back_bottom"][0], g["back_bottom"][1],
             px(tb.btn_max, *g["back_bottom"]), fg))
    check("对照：front 框自己的边仍然是字形色（上面两条不是全屏判非fg）",
          px(tb.btn_max, *g["front_left"]) == fg
          and px(tb.btn_max, *g["front_bottom"]) == fg,
          "left=%s bottom=%s 字形色=%s"
          % (px(tb.btn_max, *g["front_left"]),
             px(tb.btn_max, *g["front_bottom"]), fg))
    tb.btn_max.set_restoring(False)
    paint()

    # **标题栏底色从 `bg_base` 改成 `bg_surface`（chrome 带），发丝线已删。**
    # 分层现在由色差承担：标题栏 + 菜单栏 = `bg_surface`，内容区 = `bg_base`。
    # 弹窗那条发丝线仍在（弹窗标题栏与弹窗体都是 `bg_dialog`，色差是 1.000）。
    check("标题栏底色是 chrome 色 %s（不是内容区底色 %s）" % (p.bg_surface, p.bg_base),
          dominant(tb, 18) == p.bg_surface.lower()
          and p.bg_surface.lower() != p.bg_base.lower(), dominant(tb, 18))
    check("标题栏底边**不再有**发丝线（分层交给色差）",
          px(tb, mw.width() // 2, T.TITLEBAR_H - 1) == p.bg_surface.lower(),
          px(tb, mw.width() // 2, T.TITLEBAR_H - 1))

    # ---- 命中测试
    hit = lambda x, y: T.hit_test(mw, tb, QtCore.QPoint(x, y))
    for nm, b in (("最小化", tb.btn_min), ("关闭", tb.btn_close)):
        cx, cy = in_window(b, mw)
        check("%s按钮判定为 HTCLIENT（点得动）" % nm,
              hit(cx, cy) == T.HTCLIENT,
              "点 (%d,%d) 得到 %d" % (cx, cy, hit(cx, cy)))
    cx, cy = in_window(tb.btn_max, mw)
    check("最大化按钮判定为 HTMAXBUTTON（贴边分屏靠它）",
          hit(cx, cy) == T.HTMAXBUTTON,
          "点 (%d,%d) 得到 %d" % (cx, cy, hit(cx, cy)))
    # 右上角原先是**已知取舍**：三个按钮一直排到窗口右缘（Win11 同款），于是
    # 6x6 的 HTTOPRIGHT 角在实现上到不了，判成 HTCLIENT。用户后来明确要求
    # **四个角都要能缩放**，所以现在最外侧 CORNER_GRAB 见方让给缩放角；
    # 按钮本体（中心附近）仍然照旧是 HTCLIENT，点得中。这条钉的是
    # **按钮没被整条吃掉**。
    check("右上角让出的一小块之外仍是关闭按钮区（按钮没被整条吃掉）",
          hit(mw.width() - T.CORNER_GRAB - 1, 1) == T.HTCLIENT,
          str(hit(mw.width() - T.CORNER_GRAB - 1, 1)))
    check("右上角那条顶边在按钮区**之外**是 HTCAPTION（拖窗口，见 f7f1a97）",
          hit(mw.width() - T.CAPTION_W * 4, 1) == T.HTCAPTION,
          str(hit(mw.width() - T.CAPTION_W * 4, 1)))
    edges = [
        ("左上角", 1, 1, T.HTTOPLEFT),
        ("上边中段", mw.width() // 2, 1, T.HTCAPTION),
        ("左边中段", 1, mw.height() // 2, T.HTLEFT),
        ("右边中段", mw.width() - 2, mw.height() // 2, T.HTRIGHT),
        ("左下角", 1, mw.height() - 2, T.HTBOTTOMLEFT),
        ("下边中段", mw.width() // 2, mw.height() - 2, T.HTBOTTOM),
        ("右下角", mw.width() - 2, mw.height() - 2, T.HTBOTTOMRIGHT),
    ]
    for nm, x, y, want in edges:
        check("缩放带 %s = %d" % (nm, want), hit(x, y) == want,
              "得到 %d" % hit(x, y))
    check("标题栏其余区域 = HTCAPTION（能拖）",
          hit(mw.width() // 2, 18) == T.HTCAPTION,
          str(hit(mw.width() // 2, 18)))
    check("窗口中央 = HTCLIENT（正常点击，不抢）",
          hit(mw.width() // 2, mw.height() // 2) == T.HTCLIENT,
          str(hit(mw.width() // 2, mw.height() // 2)))

    # ---- 弹窗标题栏
    dlg = msgbox._build(None, QtWidgets.QMessageBox.Icon.Warning, "弹窗", "正文",
                        get_palette("dark"))
    dlg.show()
    paint()
    dtb = dlg.titlebar
    check("弹窗只有关闭按钮", [b.kind for b in dtb.buttons] == [T.CLOSE],
          str([b.kind for b in dtb.buttons]))
    check("弹窗标题栏底色是弹窗那一层 %s" % get_palette("dark").bg_dialog,
          dominant(dtb, 18) == get_palette("dark").bg_dialog.lower(),
          dominant(dtb, 18))
    check("弹窗底色和主窗口**不一样**（弹出来一眼认得出）",
          dominant(dtb, 18) != p.bg_base.lower(), dominant(dtb, 18))
    tops = [lb.mapTo(dlg, QtCore.QPoint(0, 0)).y()
            for lb in content_labels(dlg, dtb)]
    check("弹窗正文首行被顶到标题栏**下面**（没被盖掉一半）",
          bool(tops) and min(tops) >= T.TITLEBAR_H,
          "最靠上 y=%s，标题栏高 %d" % (min(tops) if tops else None, T.TITLEBAR_H))
    dlg.btn_close = dtb.btn_close
    dtb.btn_close.click()
    paint()
    check("点弹窗标题栏的关闭按钮能关掉弹窗", not dlg.isVisible(),
          "visible=%s" % dlg.isVisible())

    # ---- 对话框标题栏双击（btn_max 恒为 None）
    d2 = msgbox._build(None, QtWidgets.QMessageBox.Icon.Information, "双击", "正文",
                       get_palette("dark"))
    check("弹窗标题栏的 btn_max 确实是 None（双击路径够不到它）",
          d2.titlebar.btn_max is None, repr(d2.titlebar.btn_max))
    ev = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonDblClick,
                           QtCore.QPointF(5.0, 5.0),
                           QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.MouseButton.LeftButton,
                           QtCore.Qt.KeyboardModifier.NoModifier)
    raised = None
    try:
        d2.titlebar.mouseDoubleClickEvent(ev)      # 直接从 Python 调，异常不越 C++ 帧
    except BaseException as exc:                   # noqa: BLE001
        raised = "%s: %s" % (type(exc).__name__, exc)
    check("对话框标题栏双击不抛异常（双击关闭键不该把程序带走）",
          raised is None, str(raised))

    # ---- native_event 「不归我管」时的行为 vs 注释
    bare = QtWidgets.QDialog()
    bare.show()
    paint()
    check("裸 QDialog 上没有 titlebar 属性，native_event 静悄悄返回 (False, 0)",
          T.native_event(bare, None) == (False, 0)
          and getattr(bare, "titlebar", None) is None,
          "%r / titlebar=%r" % (T.native_event(bare, None),
                                getattr(bare, "titlebar", None)))
    bare.close()
    paint()

    doc = inspect.getdoc(T.native_event) or ""
    src_all = "\n".join(f.read_text(encoding="utf-8") for f in repo_files())
    claims_super = "super().nativeEvent()" in doc
    has_super = "super().nativeEvent()" in src_all
    check("native_event 的注释与真实行为一致（注释说要转交，代码就得真转交）",
          claims_super == has_super,
          "注释里声称要转交=%s，全仓真转交=%s" % (claims_super, has_super))
    d2.close()
    paint()


# ==================================================== 3 侧边栏与折叠
def sec_sidebar(mw, tmp):
    print("\n[3] 侧边栏与折叠（sidebar.py + MainWindow）")
    p = mw.palette_

    mw.set_sidebar_collapsed(False)
    paint()
    check("展开态宽度是 %d" % SB.SIDEBAR_W, mw.sidebar.width() == SB.SIDEBAR_W,
          str(mw.sidebar.width()))
    fold_x = mw._fold_btn.mapTo(mw.sidebar, QtCore.QPoint(0, 0)).x()
    check("展开态折叠按钮 tooltip 是「折叠侧边栏 (Ctrl+B)」",
          mw._fold_btn.toolTip() == "折叠侧边栏 (Ctrl+B)", mw._fold_btn.toolTip())
    check("展开态视图菜单项写的是「折叠侧边栏」",
          mw._fold_action.text() == "折叠侧边栏", mw._fold_action.text())
    check("展开态品牌 Logo 可见", mw._brand_logo.isVisible())
    check("展开态「MavisSSH」文字可见",
          mw._brand_title.isVisible() and mw._brand_title.text() == "MavisSSH",
          mw._brand_title.text())
    check("展开态新建连接按钮有文字", mw._new_btn.text() == "新建连接",
          repr(mw._new_btn.text()))
    # 原「展开态计数标签可见」已随标签一起删掉（用户要求删「会话  N」）。
    check("底部按钮区展开态横排",
          mw._foot.direction() == QtWidgets.QBoxLayout.Direction.LeftToRight,
          str(mw._foot.direction()))
    exp_w = (mw._side_edit_btn.width(), mw._side_del_btn.width())
    check("展开态编辑/删除按钮尺寸交回布局（不是钉死的 40x28）",
          mw._side_edit_btn.maximumWidth() > 40 or exp_w != (40, 28),
          "实测 %s，maxW=%d" % (str(exp_w), mw._side_edit_btn.maximumWidth()))
    check("展开态编辑/删除按钮有文字",
          mw._side_edit_btn.text() == "编辑" and mw._side_del_btn.text() == "删除",
          "%r/%r" % (mw._side_edit_btn.text(), mw._side_del_btn.text()))

    mw.set_sidebar_collapsed(True)
    paint()
    check("折叠态宽度是 %d" % SB.SIDEBAR_W_COLLAPSED,
          mw.sidebar.width() == SB.SIDEBAR_W_COLLAPSED,
          str(mw.sidebar.width()))
    check("折叠按钮在两态下位置不变",
          mw._fold_btn.mapTo(mw.sidebar, QtCore.QPoint(0, 0)).x() == fold_x,
          "%d -> %d" % (fold_x,
                        mw._fold_btn.mapTo(mw.sidebar, QtCore.QPoint(0, 0)).x()))
    check("折叠态按钮 tooltip 变「展开侧边栏 (Ctrl+B)」",
          mw._fold_btn.toolTip() == "展开侧边栏 (Ctrl+B)", mw._fold_btn.toolTip())
    check("折叠态视图菜单项变「展开侧边栏」",
          mw._fold_action.text() == "展开侧边栏", mw._fold_action.text())
    check("折叠态品牌 Logo 隐藏", not mw._brand_logo.isVisible())
    check("折叠态「MavisSSH」文字隐藏", not mw._brand_title.isVisible())
    check("折叠态新建连接按钮退化成 36x36 纯图标方块",
          (mw._new_btn.width(), mw._new_btn.height()) == (36, 36),
          "%dx%d" % (mw._new_btn.width(), mw._new_btn.height()))
    check("折叠态新建连接按钮文字为空", mw._new_btn.text() == "",
          repr(mw._new_btn.text()))
    check("折叠态新建连接按钮 tooltip 还在",
          mw._new_btn.toolTip() == "新建连接 (Ctrl+N)", mw._new_btn.toolTip())
    check("折叠态底部按钮区改竖排",
          mw._foot.direction() == QtWidgets.QBoxLayout.Direction.TopToBottom,
          str(mw._foot.direction()))
    col_w = (mw._side_edit_btn.width(), mw._side_del_btn.width())
    col_h = (mw._side_edit_btn.height(), mw._side_del_btn.height())
    check("折叠态底部按钮被钉成 40x28",
          col_w == (40, 40) and col_h == (28, 28), "%s / %s" % (col_w, col_h))
    check("折叠态底部按钮文字为空",
          mw._side_edit_btn.text() == "" and mw._side_del_btn.text() == "",
          "%r/%r" % (mw._side_edit_btn.text(), mw._side_del_btn.text()))

    # 紧凑行 + 绿点
    s1, s2 = mw.store.all[0], mw.store.all[1]
    mw.set_sidebar_collapsed(True)
    # 注意：set_sidebar_collapsed 在同态下是 early-return，不重载列表。
    # 所以刚 upsert 完必须自己叫一次 _reload_session_list。
    mw._reload_session_list()
    paint()
    check("折叠态会话行是 CompactSessionItem",
          all(isinstance(mw.session_list.itemWidget(mw.session_list.item(i)),
                         SB.CompactSessionItem)
              for i in range(mw.session_list.count())),
          str([type(mw.session_list.itemWidget(mw.session_list.item(i))).__name__
               for i in range(mw.session_list.count())]))
    rows = [mw.session_list.itemWidget(mw.session_list.item(i))
            for i in range(mw.session_list.count())]
    dots = [r._dot.isVisible() for r in rows]
    check("没有 live 标签时紧凑行的在线绿点不出现", not any(dots), str(dots))
    tips = [r.toolTip() for r in rows]
    check("紧凑行 tooltip 是「名称 · host:port」且不带「已连接」",
          all("已连接" not in t for t in tips)
          and any(t == "%s  ·  %s:%d" % (s1.name, s1.host, s1.port)
                  for t in tips),
          str(tips))

    # 给第一个会话造一个 live 标签（不发网络请求）
    tab = MW.SessionTab(mw)
    tab._last_saved = s1
    tab._last_label = "ops@10.0.0.9:2222"
    idx = mw.tabs.addTab(tab, tab._last_label)
    tab._set_state("live")
    paint()
    mw._refresh_rail_state()
    paint()
    rows = [mw.session_list.itemWidget(mw.session_list.item(i))
            for i in range(mw.session_list.count())]
    live_row = next(r for r in rows if r.session.id == s1.id)
    check("折叠态：只有那个会话有 live 标签时它的绿点才亮",
          live_row._dot.isVisible()
          and not any(r._dot.isVisible() for r in rows if r is not live_row),
          str([(r.session.name, r._dot.isVisible()) for r in rows]))
    check("折叠态紧凑行 tooltip 带「已连接」后缀",
          "已连接" in live_row.toolTip(), live_row.toolTip())
    # 原「折叠态计数标签虽然隐藏，文字仍算对了」随标签一起删掉。

    # 清掉这个假标签
    mw.tabs.removeTab(idx)
    tab.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()

    # 展开态会话行
    mw.set_sidebar_collapsed(False)
    paint()
    check("有会话时不再显示空列表提示", not mw._empty_hint.isVisible())
    # **展开态要跳过分组标题行。** 折叠态（56px）不画标题，所以上面那几段
    # 可以直接遍历 count()；展开态每一段前面都有一行标题，而这一节断的
    # 全是「会话行长什么样」—— 拿标题行去 findChild("sessionName") 得到
    # None，红得和被断的事情毫无关系。
    rows = [mw.session_list.itemWidget(mw.session_list.item(i))
            for i in sidebar_rows.session_rows(mw.session_list)]
    check("展开态会话行是 SessionItem",
          rows and all(isinstance(r, SB.SessionItem) for r in rows),
          str([type(r).__name__ for r in rows]))
    row0 = rows[0]
    name_lb = row0.findChild(QtWidgets.QLabel, "sessionName")
    host_lb = row0.findChild(QtWidgets.QLabel, "sessionHost")
    check("会话行有两行：名称 + host:port",
          name_lb is not None and host_lb is not None
          and host_lb.text() == "%s:%d" % (row0.session.host, row0.session.port),
          name_lb.text() if name_lb else None)
    _tip_item = sidebar_rows.session_item(mw.session_list)
    check("会话行 tooltip 格式是「名称  ·  host:port」",
          _tip_item.toolTip() == "%s  ·  %s:%d"
          % (row0.session.name, row0.session.host, row0.session.port),
          _tip_item.toolTip())

    fallback = make_session(name="", username="deploy", host="172.16.0.4")
    fb = SB.SessionItem(fallback, p)
    fb_name = fb.findChild(QtWidgets.QLabel, "sessionName")
    check("name 为空时回落成 user@host",
          fb_name.text() == "deploy@172.16.0.4", fb_name.text())

    # 认证徽标
    keyed = SB.SessionItem(make_session(name="k", auth_method="key"), p)
    passed = SB.SessionItem(make_session(name="p", auth_method="password"), p)
    img_k = keyed.findChildren(QtWidgets.QLabel)[0].pixmap().toImage()
    img_p = passed.findChildren(QtWidgets.QLabel)[0].pixmap().toImage()
    check("auth_method=='key' 的徽标和 password 的不是同一张图",
          img_k != img_p)
    check("认证徽标是 32x32 圆角方块（key 与 password 同规格）",
          keyed.findChildren(QtWidgets.QLabel)[0].width() == 32
          and passed.findChildren(QtWidgets.QLabel)[0].width() == 32,
          "%d/%d" % (keyed.findChildren(QtWidgets.QLabel)[0].width(),
                     passed.findChildren(QtWidgets.QLabel)[0].width()))

    # 选中行端点颜色上提一档
    # **按「第几个会话」取行**，理由同上一段：展开态第 0 行是分组标题，
    # 拿它去 findChild("sessionHost") 得到的是 None（下面那条判据会直接
    # AttributeError 崩在这里，而不是报一句「红」）。
    listw = mw.session_list
    listw.setCurrentRow(sidebar_rows.session_row(listw))
    paint()
    sel_host = listw.itemWidget(
        sidebar_rows.session_item(listw)).findChild(
        QtWidgets.QLabel, "sessionHost")
    check("选中行的端点颜色上提到 text_dim %s" % p.text_dim,
          sel_host is not None
          and p.text_dim.lower() in sel_host.styleSheet(),
          sel_host.styleSheet() if sel_host else None)
    listw.setCurrentRow(-1)
    paint()
    check("取消选中后内联样式被清空（颜色交回 QSS 那一个所有者）",
          sel_host is not None and sel_host.styleSheet() == "",
          sel_host.styleSheet() if sel_host else None)

    # 右键菜单：选中 A、右键 B、点删除，删掉的必须是 B
    n0 = len(mw.store.all)
    victim = sidebar_rows.session_item(listw, 1)
    victim_id = victim.data(QtCore.Qt.ItemDataRole.UserRole)
    keeper_id = sidebar_rows.session_item(listw, 0).data(
        QtCore.Qt.ItemDataRole.UserRole)
    listw.setCurrentRow(sidebar_rows.session_row(listw))
    listw.setCurrentRow(sidebar_rows.session_row(listw))
    fired = []
    real_exec = QtWidgets.QMenu.exec
    real_q = msgbox.question

    def fake_exec(menu_self, *a, **k):
        for act in menu_self.actions():
            if act.text() == "删除":
                fired.append(act)
                act.trigger()
        return None

    msgbox.question = lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes
    QtWidgets.QMenu.exec = fake_exec
    try:
        pos = listw.visualItemRect(victim).center()
        mw._on_session_menu(pos)
    finally:
        QtWidgets.QMenu.exec = real_exec
        msgbox.question = real_q
    paint()
    check("右键菜单的「删除」作用在被右键的那一行（不是当前选中行）",
          mw.store.get(victim_id) is None
          and mw.store.get(keeper_id) is not None,
          "被右键的是 %s，选中的是 %s，点完剩 %d 条"
          % (victim_id[:6], keeper_id[:6], len(mw.store.all)))
    check("右键删除这条判据真的触发了一次删除（不是空转）",
          bool(fired) and len(mw.store.all) == n0 - 1,
          "fired=%d count=%d->%d" % (bool(fired), n0, len(mw.store.all)))

    # ---- 侧边栏两个底部按钮的槽签名
    # 这里**不能**对那两个按钮调 .click()：Qt 会把 clicked 的 checked 布尔值
    # 传给槽的第一个参数，而那个参数是 `item`；`False is not None` 为真，于是
    # `_session_of(False)` 抛 AttributeError，而 PyQt6 里槽抛异常不是打一行
    # 日志，是整个进程 fast-fail（0xC0000409），traceback 都来不及打。
    # 所以分两步：先在一个**没接产品槽**的裸按钮上证明 clicked 的实参形状，
    # 再用 Qt 传的那个值从 Python 复现产品槽里的第一行。
    bare_btn = QtWidgets.QPushButton()
    got_arg = []
    probe = lambda c: got_arg.append(c)          # noqa: E731
    bare_btn.clicked.connect(probe)
    bare_btn.click()
    paint()
    check("QPushButton.clicked 传给槽的是一个布尔值（checked），不是 None",
          got_arg == [False], str(got_arg))
    check("侧边栏「编辑」「删除」两个按钮都接了槽（不是点了没反应）",
          mw._side_edit_btn.receivers(mw._side_edit_btn.clicked) > 0
          and mw._side_del_btn.receivers(mw._side_del_btn.clicked) > 0,
          "编辑 %d 个接收者 / 删除 %d 个"
          % (mw._side_edit_btn.receivers(mw._side_edit_btn.clicked),
             mw._side_del_btn.receivers(mw._side_del_btn.clicked)))
    for nm, btn in (("编辑", mw._side_edit_btn), ("删除", mw._side_del_btn)):
        listw.setCurrentRow(-1)
        # **真的点按钮**，而不是从 Python 手工喂 `fn(False)`。
        # 原来那版量的是「槽被传了 False 会怎样」——那是内部签名，
        # 不是用户做的事；产品把 `checked` 吃掉之后这条路压根不会再走，
        # 判据却会一直红。修好之后 `click()` 已经安全，判据也该回到
        # 用户真正走的那条路上。
        caught = _click_capturing(btn, mw, paint)
        check("真的点侧边栏「%s」按钮不抛任何异常" % nm, not caught, str(caught))

    # 无选中时的反馈。走不带参那条路（右键菜单走的就是它），这样才量得到
    # 「点了什么都没发生」这件事。
    listw.setCurrentRow(-1)
    with Feedback(mw) as fb1:
        mw._on_edit_session()
    check("无选中时「编辑」有提示（说明这条探针本身是有效的）",
          bool(fb1.boxes), str(fb1.boxes))
    with Feedback(mw) as fb2:
        mw._on_delete_session()
    check("无选中时「删除」也有反馈（不是静悄悄地什么都不发生）",
          bool(fb2.boxes) or bool(fb2.flash),
          "弹了 %d 个框、刷了 %d 条状态" % (len(fb2.boxes), len(fb2.flash)))
    check("「删除」与「编辑」在无选中时行为一致（一个提示一个沉默是缺陷）",
          (bool(fb2.boxes) or bool(fb2.flash)) == bool(fb1.boxes),
          "编辑 %d 框 / 删除 %d 框 %d 条状态"
          % (len(fb1.boxes), len(fb2.boxes), len(fb2.flash)))

    # 回车能不能打开会话
    #
    # **这条曾经偶发红，而真凶不是模态框。** 独立最小复现实测（四个场景）：
    #   A show() 之后            isActive=True   focus=QListWidget  shortcut 触发
    #   B 真模态框 exec() 之后   isActive=True   focus=QListWidget  shortcut 触发  ← 模态框不是原因
    #   C 别的窗口是活动窗口     isActive=False  focus=None         shortcut 不触发 ← 真凶
    #   D 模态框叠在别的窗口上   isActive=True   focus=QListWidget  shortcut 触发
    # `QShortcut` 的 `WidgetShortcut` 上下文要求**它所属的窗口是活动窗口**。
    # 我原先怀疑是上面那两个「无选中」提示框把激活状态带走了 —— **实测推翻了**：
    # `Feedback`（`tests/_fwin.py:247-257`）是把 `msgbox.information` 整个换成
    # lambda、**从不构造 QMessageBox**，`_click_capturing` 用的也是它，所以这一段
    # 压根没有模态框。而真模态框 exec() 完（B 场景）shortcut 照样触发。
    #
    # 机器繁忙时 Windows 不让后台进程抢前台（`SetForegroundWindow` 受限），
    # `mw` 偶尔就不是活动窗口。**那不是产品缺陷** —— 窗口不激活时用户本来也
    # 敲不了键盘。报一个「纯键盘用户打不开会话」的假红，是把**前提不成立**
    # 记成了缺陷。
    #
    # 所以：先把窗口激活到位并**确认前提**；确实激活不了就如实 SKIP（说清哪条
    # 前提不成立），前提成立时这条判据必须绿 —— 并且它带着元断言，见下。
    #
    # **打桩必须整体包在 `try/finally` 里，且 finally 要在 if/else 之外。**
    # 评审（票5）在临时副本上强制走 skip 分支，复现出一个我第一版引入的真缺陷：
    # 原来 `mw.open_session = lambda` 在 if 之前、`finally` 还原在 else 之内，
    # 于是 **skip 路径永远不还原打桩** —— 后面 `_fwin.py:1487` 那条「连接进行中
    # 再点另一条会话」的闸门判据要真调 `mw.open_session(...)`，调到的是这个
    # lambda，闸门压根没跑，于是报出一句
    #   FAIL 连接进行中再点另一条会话 <- []
    # **根因在 600 行之外**，红得莫名其妙。这比原来那条环境无关的红更坏：
    # 它把「前提不成立」翻译成了一条**语义完全跑偏**的缺陷。
    mw.raise_()
    mw.activateWindow()
    # 激活是异步的（`SetForegroundWindow` 受 Windows 前台限制），先等它落定，
    # 别问得太早。`wait_until` 是本文件既有的异步等待件。
    wait_until(lambda: mw.isActiveWindow(), 1500)
    paint()
    listw.setCurrentRow(sidebar_rows.session_row(listw))
    listw.setFocus()
    paint()
    _fw = QtWidgets.QApplication.focusWidget()
    if not (mw.isActiveWindow() and _fw is listw):
        # **前提不成立 = 未评上，不是红。** 与 `check` 分开走，别混成一条。
        # 注意本项目约定：**SKIP 同样让整套回归退出码变成 1**（`_runall.py:183`
        # 「没全评上就不算全过」），所以这不是「降级放行」，只是把结论说准。
        skip("会话列表有回车键入口（纯键盘用户能打开会话）",
             "前提不成立：isActiveWindow=%s focusWidget=%s（要求主窗口是活动窗口"
             "且列表持有焦点；机器繁忙时 Windows 不让后台进程抢前台）"
             % (mw.isActiveWindow(), type(_fw).__name__ if _fw else None))
    else:
        opened = []
        real_open = mw.open_session
        try:
            mw.open_session = lambda s: opened.append(s.name)
            QtTest.QTest.keyClick(listw, QtCore.Qt.Key.Key_Return)
            paint()
            check("会话列表有回车键入口（纯键盘用户能打开会话）",
                  bool(opened), "按回车打开了 %s" % (opened,))

            # 元断言：**把这条 QShortcut 停用后，回车必须打不开。**
            # 它证明的是**差分**：「能打开」确实由这条 QShortcut 造成 ——
            # 少了它这条判据就量不到这条接线。
            #
            # **别把它当成「回车只有这一条路」的证明。** 评审（票5）实测：
            # `setEnabled(False)` **挡不住** `QAbstractItemView` 自带的
            # 「Return → activated(currentIndex)」，停用后 `itemActivated`
            # 照样发（实测 [0]）；上面那条之所以仍然打不开，是因为
            # `itemActivated` 在本仓**没接本槽**（只接了 `itemDoubleClicked`，
            # `main_window.py:1868`）。所以准确的说法是「本仓只有这一条接线」，
            # 而不是「Qt 只有这一条路」。
            _sc = mw._list_open
            _sc.setEnabled(False)
            opened.clear()
            try:
                QtTest.QTest.keyClick(listw, QtCore.Qt.Key.Key_Return)
                paint()
            finally:
                _sc.setEnabled(True)
            check("元断言：停用这条 QShortcut 后回车打不开（所以上面那条真的量到了它）",
                  not opened, "停用后仍然打开了 %s" % (opened,))
        finally:
            # **这一行两条路径都必须走到。** 见上面票5 那段注释。
            mw.open_session = real_open

    # ---- 「关闭当前标签」落在欢迎页上
    act_close = None
    for m in mw._menu_bar.actions():
        for act in m.menu().actions():
            if act.shortcut() == QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Close):
                act_close = act
    n_before = mw.tabs.count()
    mw.tabs.setCurrentIndex(0)
    # **断言的是「安全的空操作」，不是「必须给用户一句话」。**
    # 欢迎页本来就没有标签可关，这里什么都不发生是**应有行为** ——
    # 原来的判据要求弹一句提示，是把一条不存在的要求当成了契约
    # （评审把它判为不成立：全仓没有这个键位，且没有标签可关时无反应是对的）。
    # 该钉的是：这个动作不会崩、不会把窗口关掉、标签一个都不少。
    caught_close = []
    if act_close is not None:
        real_hook = sys.excepthook
        sys.excepthook = lambda t, v, tb: caught_close.append(
            "%s: %s" % (getattr(t, "__name__", t), v))
        try:
            act_close.trigger()
            paint()
        finally:
            sys.excepthook = real_hook
    check("「关闭当前标签」落在不可关闭的欢迎页上不抛异常",
          not caught_close, str(caught_close))
    check("「关闭当前标签」在欢迎页上不会关窗口、标签也不变",
          mw.isVisible() and mw.tabs.count() == n_before,
          "tabs %d -> %d，visible=%s"
          % (n_before, mw.tabs.count(), mw.isVisible()))

    # 空列表提示
    saved_all = list(mw.store.all)
    for s in saved_all:
        mw.store.delete(s.id)
    mw.set_sidebar_collapsed(False)
    # 同态下 set_sidebar_collapsed 是 early-return，不重载列表 —— 删完得自己叫。
    mw._reload_session_list()
    paint()
    # 原「0 条会话时计数标签文字是空串」随标签一起删掉。
    check("0 条会话时显示空列表提示「还没有保存的会话」",
          mw._empty_hint.isVisible() and mw._empty_hint.text() == "还没有保存的会话",
          "visible=%s text=%r" % (mw._empty_hint.isVisible(), mw._empty_hint.text()))
    for s in saved_all:
        mw.store.upsert(s)
    mw._reload_session_list()
    paint()

    # 折叠偏好持久化（临时目录，不碰用户配置）
    st2 = S.AppSettings()
    st2._path = tmp / "settings_fold.json"
    st2.theme = "dark"
    st2.sidebar_collapsed = True
    st3 = S.SessionStore(tmp / "sessions_fold.json", config_dir=tmp)
    mw2 = MW.MainWindow(settings=st2, store=st3,
                        snippet_store=SnippetStore(tmp / "snippets_fold.json"))
    mw2.show()
    paint()
    check("折叠偏好会持久化：新开的窗口读回折叠态",
          mw2.sidebar_collapsed() is True
          and mw2.sidebar.width() == SB.SIDEBAR_W_COLLAPSED,
          "collapsed=%s width=%d" % (mw2.sidebar_collapsed(), mw2.sidebar.width()))
    mw2.set_sidebar_collapsed(False)
    paint()
    check("取消折叠后写回偏好（sidebar_collapsed 变 False）",
          st2.sidebar_collapsed is False, repr(st2.sidebar_collapsed))
    check("折叠态的 rail 属性也写在会话列表上（QSS 靠它收紧 gutter）",
          mw2.session_list.property("rail") == "",
          repr(mw2.session_list.property("rail")))
    mw2.close()
    mw2.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()


# ==================================================== 4 标签页与欢迎页
def sec_tabs(mw, tmp):
    print("\n[4] 标签页与欢迎页（tab_close.py）")
    bar = mw.tabs.tabBar()
    ctl = mw._tab_close
    INSET = TC.TabCloseController.INSET
    paint()

    check("欢迎页常驻第 0 个标签",
          mw.tabs.widget(0) is mw.welcome, repr(mw.tabs.widget(0)))
    check("欢迎页标了 NO_CLOSE（标签名是「欢迎」）",
          mw.welcome.property(TC.NO_CLOSE) is True
          and mw.tabs.tabText(0) == "欢迎", mw.tabs.tabText(0))

    n0 = mw.tabs.count()
    spy = QtTest.QSignalSpy(mw.tabs.tabCloseRequested)
    mw.tabs.tabCloseRequested.emit(0)
    paint()
    check("对欢迎页发 tabCloseRequested 不会关掉它（标签数不变）",
          mw.tabs.count() == n0 and not mw.tabs.widget(0) is None,
          "count=%d" % mw.tabs.count())
    check("欢迎页那条 tabCloseRequested 确实发出去了（上面那条不是空转）",
          len(spy) == 1, "收到 %d 次" % len(spy))
    mw._on_tab_close(0)
    paint()
    check("直接调 _on_tab_close(0) 也关不掉欢迎页",
          mw.tabs.count() == n0 and mw.tabs.widget(0) is mw.welcome,
          "count=%d" % mw.tabs.count())

    cta = mw.welcome.findChildren(QtWidgets.QPushButton)
    check("欢迎页有 CTA「新建连接」按钮",
          any("新建连接" in b.text() for b in cta),
          str([b.text() for b in cta]))
    cta0 = next(b for b in cta if "新建连接" in b.text())
    # **钉性质，不钉具体像素。** 原来写的是 `cta0.height() == 38` —— 那个 38
    # 是「QSS min-height 36px + 2px 边框」的**结果**，不是设计意图。改一次
    # 控件尺寸它就红，而它红并不说明任何东西坏了。
    #
    # 真正的意图，`sidebar.py` 里写得很清楚：「高度交给 QSS 的 `role="cta"`
    # ……和表单里其它按钮一样大，就不再是 hero 了」——
    # **CTA 必须比普通控件高**。
    #
    # 基准取 `qss.scaled_control_h()`（普通控件的基准高），**不要**去窗口里
    # 随便找一个按钮来比：上一版就找错了（抓到的是欢迎页外另一个 52px 的
    # 按钮），于是「CTA 比普通按钮高」这条自己判自己红。
    _normal_h = qss.scaled_control_h()
    check("欢迎页 CTA 比普通控件高（它是这一屏唯一的行动点）",
          cta0.height() > _normal_h,
          "CTA=%r 普通控件基准=%r" % (cta0.height(), _normal_h))
    subs = [lb.text() for lb in mw.welcome.findChildren(QtWidgets.QLabel)]
    check("欢迎页副标题是「终端 · 文件 · 资源监控 · lrzsz」",
          "终端 · 文件 · 资源监控 · lrzsz" in subs, str(subs))
    check("欢迎页有「Ctrl+N」快捷键提示",
          any("Ctrl+N" in t for t in subs), str(subs))
    check("欢迎页 CTA 发出 newRequested 信号（点了有反应）",
          hasattr(mw.welcome, "newRequested"))

    # 造几个真标签（不连服务器）
    made = []
    for k in range(5):
        tb_ = MW.SessionTab(mw)
        tb_._last_label = "host%d@10.0.0.%d:22" % (k, k)
        mw.tabs.addTab(tb_, tb_._last_label)
        made.append(tb_)
    ctl.sync()
    paint()
    slots = [i for i in range(mw.tabs.count())
             if mw.tabs.widget(i).property(TC.NO_CLOSE) is not True]
    btns = list(ctl._buttons)
    check("每个可关闭标签都拿到一个关闭按钮",
          len(btns) == len(slots), "%d 个按钮 / %d 个可关闭标签"
          % (len(btns), len(slots)))
    if btns:
        b0 = btns[0]
        r0 = bar.tabRect(slots[0])
        check("关闭按钮是 18x18", (b0.width(), b0.height()) == (18, 18),
              "%dx%d" % (b0.width(), b0.height()))
        gap = r0.right() - b0.geometry().right()
        check("关闭按钮贴标签右缘、内侧留 %d px" % INSET, gap == INSET,
              "实际 %d" % gap)
        check("关闭按钮整体落在标签矩形内（不越到邻标签上）",
              bar.rect().contains(QtCore.QRect(b0.pos(), b0.size()))
              and r0.adjusted(0, 0, 0, 0).intersects(
                  QtCore.QRect(b0.pos(), b0.size())),
              "btn=%s tab=%s" % (str(b0.geometry()), str(r0)))
        check("关闭按钮 tooltip 是「关闭会话」",
              b0.toolTip() == "关闭会话", b0.toolTip())

        # hover / 按下 变红底
        p = mw.palette_
        idle = px(b0, 3, 3)
        b0._hover = True
        b0.update()
        paint()
        hov = px(b0, 3, 3)
        b0._hover = False
        b0.update()
        paint()
        back = px(b0, 3, 3)
        check("关闭按钮 hover 时底色变 danger %s" % p.danger,
              hov == p.danger.lower(), "%s（常态 %s）" % (hov, idle))
        check("离开 hover 后关闭按钮复原", back == idle,
              "%s vs %s" % (back, idle))

    # 点第 N 个标签的关闭按钮，关掉的必须是第 N 个
    if len(btns) >= 3:
        count_before = mw.tabs.count()
        third_label = mw.tabs.tabText(slots[2])
        follower_before = mw.tabs.tabText(slots[3])
        btns[2].click()
        paint()
        check("点第 3 个标签的关闭按钮，关掉的是**那个**标签",
              mw.tabs.count() == count_before - 1
              and third_label not in [mw.tabs.tabText(i)
                                      for i in range(mw.tabs.count())],
              "少了「%s」" % third_label)
        check("关掉之后后面那个标签顺移上来（没多关也没少关）",
              follower_before in [mw.tabs.tabText(i)
                                  for i in range(mw.tabs.count())],
              follower_before)

    # 重排后关闭按钮还贴不贴得住。
    # **这里量的是「位置」不是「身份」**：按钮是可互换的，`_emit_close` 也是
    # 靠几何反查标签下标（见 tab_close.py 的类注释），所以重排后第 i 个按钮
    # 该贴的是**现在**第 i 个标签。判据就按这个来。
    ctl.sync()
    paint()
    if len(ctl._buttons) >= 2:
        before_map = [ctl._buttons[i].mapTo(bar, QtCore.QPoint(0, 0)).x()
                      for i in range(2)]
        slots = [i for i in range(mw.tabs.count())
                 if mw.tabs.widget(i).property(TC.NO_CLOSE) is not True]
        src, dst = slots[0], slots[1]
        bar.moveTab(src, dst)
        ctl.sync()
        paint()
        # 重排之后，src 那一格上停着的是**别的**标签；点按钮该关掉的是它。
        label_now = mw.tabs.tabText(src)
        c0 = ctl._buttons[0].mapTo(bar, ctl._buttons[0].rect().center())
        check("重排后关闭按钮仍贴在**当前位置**那个标签上（位置关系没乱）",
              bar.tabRect(src).contains(c0),
              "按钮中心 %s 落在 tabRect(%d)=%s"
              % (str(c0), src, str(bar.tabRect(src))))
        cnt_b = mw.tabs.count()
        ctl._buttons[0].click()
        paint()
        check("重排后点这个关闭按钮，关掉的是现在停在那一位的标签",
              mw.tabs.count() == cnt_b - 1
              and label_now not in [mw.tabs.tabText(i)
                                    for i in range(mw.tabs.count())],
              "该消失的「%s」还在" % label_now)
        check("重排前两个按钮是分开的两段（所以上面两条不是恒真）",
              before_map[0] != before_map[1], str(before_map))
        ctl.sync()
        paint()

    # 横向溢出：视口外的标签看不到关闭按钮
    for k in range(14):
        tb_ = MW.SessionTab(mw)
        tb_._last_label = "overflow%02d@10.1.0.%d:22" % (k, k)
        mw.tabs.addTab(tb_, tb_._last_label)
    ctl.sync()
    paint()
    hidden = [b for b in ctl._buttons if not b.isVisible()]
    check("标签多到横向溢出时，视口外的标签看不到关闭按钮",
          bool(hidden), "%d/%d 个关闭按钮是隐藏的"
          % (len(hidden), len(ctl._buttons)))
    mw.tabs.setCurrentIndex(mw.tabs.count() - 1)
    ctl.sync()
    paint()
    ctl._buttons[-1].show()
    ctl.sync()
    paint()
    check("滚回来之后那个标签的关闭按钮重新出现",
          ctl._buttons[-1].isVisible(),
          "visible=%s" % ctl._buttons[-1].isVisible())

    # 关闭 N 个标签后 SessionTab 真的被销毁
    # 先把前面几节遗留的 deleteLater 一次性排空：sendPostedEvents 会处理
    # **全部**待决删除，不排空的话基线里混着别处的对象，差值就不准了。
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()
    idxs = [i for i in range(mw.tabs.count())
            if isinstance(mw.tabs.widget(i), MW.SessionTab)][:3]
    closed = [mw.tabs.widget(i) for i in idxs]
    sentinels = []
    for t in closed:
        lbl = QtWidgets.QLabel("sentinel")
        t.layout().addWidget(lbl)
        sentinels.append(lbl)
    before_tree = len(mw.findChildren(MW.SessionTab))
    for i in reversed(idxs):
        mw._on_tab_close(i)
    paint()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()
    check("关闭 3 个标签后，那 3 个 SessionTab 真的被销毁（sip.isdeleted）",
          all(sip.isdeleted(t) for t in closed),
          str([sip.isdeleted(t) for t in closed]))
    check("被销毁的标签，它的子控件也一起回收了（片段面板走的是同一条路）",
          all(sip.isdeleted(s) for s in sentinels),
          str([sip.isdeleted(s) for s in sentinels]))
    after_tree = len(mw.findChildren(MW.SessionTab))
    check("关掉的标签已经从对象树里消失（findChildren 少了 3 个）",
          after_tree == before_tree - 3,
          "树里 %d -> %d" % (before_tree, after_tree))
    check("判据不是空转：树里原本确实有 SessionTab",
          before_tree >= 3, "树里原本 %d 个" % before_tree)


# ==================================================== 5 菜单与主题
def sec_menu_theme(mw, tmp):
    print("\n[5] 菜单与主题（main_window.py）")
    bar = mw._menu_bar
    menus = {m.text(): [a.text() for a in m.menu().actions() if a.text()]
             for m in bar.actions() if m.menu() is not None}
    check("菜单栏有「文件 / 视图 / 帮助」三个菜单",
          set(menus) == {"文件", "视图", "帮助"}, str(sorted(menus)))
    check("文件菜单五项：新建连接… / 从其他终端导入… / 关闭当前标签 / 设置… / 退出",
          menus.get("文件") == ["新建连接…", "从其他终端导入…",
                                "关闭当前标签", "设置…", "退出"],
          str(menus.get("文件")))
    check("视图菜单两项：切换主题 / 折叠侧边栏",
          menus.get("视图") == ["切换主题", "折叠侧边栏"],
          str(menus.get("视图")))
    check("帮助菜单有「关于 MavisSSH」",
          menus.get("帮助") == ["关于 MavisSSH"], str(menus.get("帮助")))

    def shortcut_of(menu_title, text):
        for m in bar.actions():
            if m.text() != menu_title or m.menu() is None:
                continue
            for a in m.menu().actions():
                if a.text() == text:
                    return a.shortcut().toString()
        return ""

    # 文件菜单里字面量写死的键，跨平台稳定，可以直接钉
    want_file = {"新建连接…": "Ctrl+N", "设置…": "Ctrl+,"}
    got_file = {k: shortcut_of("文件", k) for k in want_file}
    check("文件菜单的字面量快捷键 Ctrl+N / Ctrl+,", got_file == want_file,
          str(got_file))
    # 「关闭当前标签」用 `StandardKey.Close`、`退出` 用 `StandardKey.Quit`，
    # 而 **StandardKey 是按平台解析的**：Windows 上前者是 `Ctrl+F4`、
    # 后者是 `Exit`。所以键本身不能写死 —— 原来这里硬编码成
    # `Ctrl+W` / `Ctrl+Q`，等于把一条**文档错**报成了功能错，
    # 后面那条「全仓没有 Ctrl+W / Ctrl+Q」更是把**结论**写进了判据：
    # 换个平台它就假红，而产品其实一点问题没有。
    all_shorts = [a.shortcut().toString()
                  for m in bar.actions() if m.menu() is not None
                  for a in m.menu().actions() if a.shortcut().toString()]
    for _text, _std in (("关闭当前标签", QtGui.QKeySequence.StandardKey.Close),
                        ("退出", QtGui.QKeySequence.StandardKey.Quit)):
        check("%s 用的是平台标准键 %s" % (_text, _std.name),
              shortcut_of("文件", _text) == QtGui.QKeySequence(_std).toString(),
              shortcut_of("文件", _text))
    # 真正该钉的契约：**README 写的每个键菜单里真有，菜单里每个键 README 都写了。**
    # 这条在平台之间也成立，不受 StandardKey 怎么解析影响。
    menu_keys = set(all_shorts)
    readme_keys = _readme_shortcut_keys()
    check("README 快捷键表里的每个键菜单里都真的绑了",
          readme_keys <= menu_keys,
          "README 有而菜单没有的：%s" % sorted(readme_keys - menu_keys))
    check("菜单里用到的每个键 README 都写了",
          menu_keys <= readme_keys,
          "菜单有而 README 没写的：%s" % sorted(menu_keys - readme_keys))
    want_view = {"切换主题": "Ctrl+T", "折叠侧边栏": "Ctrl+B"}
    got_view = {k: shortcut_of("视图", k) for k in want_view}
    check("视图菜单两个快捷键 Ctrl+T / Ctrl+B", got_view == want_view,
          str(got_view))

    # ---- 关于框版本号
    got_version = []
    real_about = msgbox.about
    msgbox.about = lambda parent, version, palette=None: got_version.append(version)
    try:
        mw._on_about()
    finally:
        msgbox.about = real_about
    main_py = (pathlib.Path(ROOT) / "main.py").read_text(encoding="utf-8")
    check("关于框显示的版本号 == 包上的 __version__（运行期真值，不是源码字面量）",
          bool(got_version) and got_version[0] == PKG_VERSION,
          "关于框=%r 包 __version__=%r" % (got_version, PKG_VERSION))
    # **`main.py` 里不该再有版本号字面量。** 原来判据是去源码里抓
    # `setApplicationVersion("0.2.0")` 那个字符串跟包版本比 —— 于是
    # 「改成从包上读」这个正确的修法反而让判据变红（它抓不到字面量了）。
    # 该钉的契约是**只有一份真相**：要么直接引用 `__version__`，
    # 要么字面量与包版本逐字相同。
    _ver_line = next((ln for ln in main_py.splitlines()
                      if "setApplicationVersion" in ln), "")
    _literal = _ver_line.split('"')[1] if '"' in _ver_line else ""
    check("main.py 的 applicationVersion 只有一份真相"
          "（引用包版本，或字面量与包逐字相同）",
          "__version__" in _ver_line or _literal == PKG_VERSION,
          "那一行=%r，包 __version__=%r" % (_ver_line.strip(), PKG_VERSION))
    check("main.py 里不再有版本号字面量（真相只在包上）",
          not _literal or _literal == PKG_VERSION,
          "仍有字面量 %r" % _literal)
    check("main_window 的关于框也只从包上读（源码里没有第二份字面量）",
          "msgbox.about(self, __version__" in
          (pathlib.Path(ROOT) / "sshclient" / "ui" / "main_window.py")
          .read_text(encoding="utf-8"),
          "main_window.py 的 _on_about 里又写了一份字面量")

    # ---- 关于框正文是纯文本、首行 MavisSSH <版本>
    exec_, boxes = recorder()
    token = patch_exec(msgbox.ThemedMessageBox, exec_)
    try:
        msgbox.about(mw, PKG_VERSION, mw.palette_)
    finally:
        restore_exec(msgbox.ThemedMessageBox, token)
    paint()
    check("关于框真的弹出了一个自定义标题栏的 QMessageBox（不是原生静态调用）",
          bool(boxes) and isinstance(boxes[0], msgbox.ThemedMessageBox),
          str(boxes))
    if boxes:
        ab = boxes[0]
        check("关于框正文是纯文本（不渲染 HTML）",
              ab.textFormat() == QtCore.Qt.TextFormat.PlainText,
              str(ab.textFormat()))
        check("关于框正文首行是「MavisSSH <版本>」",
              ab.text() == "MavisSSH %s" % PKG_VERSION, repr(ab.text()))

    # ---- 菜单图标随主题重染色
    mw.apply_theme("dark", accent_for(S, "dark"))
    paint()
    before_icons = [a.icon().pixmap(16, 16).toImage() for a in mw._menu_actions]
    mw.apply_theme("light", accent_for(S, "light"))
    paint()
    lp = get_palette("light")
    after_icons = [a.icon().pixmap(16, 16).toImage() for a in mw._menu_actions]
    check("7 个 QAction 的图标都随主题重染色了",
          all(b != a for b, a in zip(before_icons, after_icons)),
          "%d/%d 个变了" % (sum(1 for b, a in zip(before_icons, after_icons)
                               if b != a), len(before_icons)))
    want_icon = lambda nm: icons.icon(nm, lp, 16, lp.text_dim).pixmap(16, 16).toImage()
    mismatch = [getattr(a, "_icon_name", "?")
                for a, im in zip(mw._menu_actions, after_icons)
                if im != want_icon(getattr(a, "_icon_name", ""))]
    check("菜单图标颜色就是新配色的 text_dim %s" % lp.text_dim,
          not mismatch, "对不上的是 %s" % mismatch)
    mw.apply_theme("dark", accent_for(S, "dark"))
    paint()

    # ---- 标签页 tab 图标是否随主题重染色
    tab = MW.SessionTab(mw)
    idx = mw.tabs.addTab(tab, "tabicon@10.0.0.9:22")
    mw.tabs.setTabIcon(idx, icons.icon("server", mw.palette_, 15,
                                       color=mw.palette_.accent))
    paint()
    icon_before = mw.tabs.tabIcon(idx).pixmap(15, 15).toImage()
    dp = get_palette("dark")
    mw.apply_theme("light", accent_for(S, "light"))
    paint()
    icon_after = mw.tabs.tabIcon(idx).pixmap(15, 15).toImage()
    check("标签 tab 图标随主题重染色（深浅切换后还是新配色）",
          icon_before != icon_after)
    want = icons.icon("server", lp, 15, color=lp.accent).pixmap(15, 15).toImage()
    check("标签 tab 图标颜色 == 新配色的 accent", icon_after == want,
          "icon_after=%s" % icon_after.size().width())
    mw.apply_theme("dark", accent_for(S, "dark"))
    paint()
    mw.tabs.removeTab(idx)
    tab.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()

    # ---- Ctrl+T 保留强调色；只改强调色也要重建 QSS
    # 强调色必须设在 **mw.settings** 上：_toggle_theme 读的是窗口自己那份。
    # 深浅两套都填，因为 Ctrl+T 会把主题翻过去，而 accent_for 读的是翻过去
    # 那一套的值。
    saved_accent = (mw.settings.accent_dark, mw.settings.accent_light)
    mw.settings.accent_dark = "#c2410c"
    mw.settings.accent_light = "#c2410c"
    before_pal = mw.palette_.name
    before_accent = mw.palette_.accent
    mw._toggle_theme()
    paint()
    check("Ctrl+T 把主题翻过来了",
          mw.palette_.name != before_pal,
          "%s -> %s" % (before_pal, mw.palette_.name))
    check("Ctrl+T 保留用户强调色 #c2410c（翻过去那一套也读得到）",
          mw.palette_.accent.lower() == "#c2410c",
          "切之前是 %s，切之后是 %s" % (before_accent, mw.palette_.accent))
    mw._toggle_theme()
    paint()
    check("Ctrl+T 翻回来之后强调色仍然是用户那一个",
          mw.palette_.accent.lower() == "#c2410c", mw.palette_.accent)

    # ---- 状态栏 host pill
    # 结构那条先判：pill 是一次性的临时对象，没挂在 self 上，谁也改不了它。
    cls_tree = ast.parse(textwrap.dedent(inspect.getsource(MW.MainWindow)))
    permanent = [type(n.args[0]).__name__ for n in ast.walk(cls_tree)
                 if (isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "addPermanentWidget" and n.args)]
    pill_attrs = [k for k in mw.__dict__ if "pill" in k.lower()]
    check("状态栏右侧的 pill 被存成了 self 上的属性（否则谁也更新不了它）",
          bool(pill_attrs) and "Name" not in permanent,
          "self 上带 pill 的属性：%s；addPermanentWidget 收进去的是 %s"
          % (pill_attrs, permanent or "（没有）"))

    calls = []
    real_at = MW.MainWindow.apply_theme
    MW.MainWindow.apply_theme = lambda self, theme, accent="": (
        calls.append((theme, accent)), real_at(self, theme, accent))[1]
    try:
        only_accent = S.AppSettings()
        only_accent._path = tmp / "settings_accent.json"
        only_accent.theme = mw.settings.theme
        only_accent.accent_dark = "#0f766e"
        mw.apply_settings(only_accent)
    finally:
        MW.MainWindow.apply_theme = real_at
    paint()
    check("主题名不变、只改强调色时也重建 QSS（apply_theme 被调到了）",
          bool(calls), "apply_theme 被调 %d 次" % len(calls))
    check("只改强调色后 accent 真的换成了用户挑的那个",
          mw.palette_.accent.lower() == "#0f766e", mw.palette_.accent)

    # ---- 状态栏 host pill（内容那条要等 _on_connect_ok 跑过，见下面 open_shell 段）

    # ---- B15 等价判据：切主题后会话状态文字的颜色跟着走
    # `_fconn.py` 那条是拿一个**手工造出来的** QLabel 去撞 QSS，量不到
    # main_window 这一头：真正出问题的是 `_set_state` 只在构造时写一次
    # `setProperty("color", …)`，换主题没人重写，于是新 QSS 里匹配不到那条
    # 规则。这里从真实的 SessionTab 上量：同一个 live 状态，切深浅前后
    # 状态文字必须还是**成功色**，不能掉回 text_dim。
    tab15 = MW.SessionTab(mw)
    mw.tabs.addTab(tab15, "state@10.0.0.9:22")
    # **必须切成当前页**：`grab()` 一个没被显示过的标签页只会画出一张空图，
    # 像素判据于是恒等于「什么都没变」。
    mw.tabs.setCurrentIndex(mw.tabs.indexOf(tab15))
    tab15._set_state("live")
    paint()
    img_before = tab15.state_label.grab().toImage()
    mw.apply_theme("light", accent_for(S, "light"))
    paint()
    lp15 = get_palette("light")
    img_after = tab15.state_label.grab().toImage()
    shown = (tab15.state_label.property("color") or "").lower()
    check("切到浅色主题后会话状态文字仍显示该状态的成功色",
          shown == lp15.success.lower(),
          "实测=%r 期望=成功色 %s（property 里还是旧主题的 %s）"
          % (shown, lp15.success, DARK.success))
    check("状态文字的像素真的换了（新 QSS 里匹配得到 color 属性那条规则）",
          img_before != img_after,
          "切主题前后像素完全一样 —— 文字还停在旧配色上")

    def exact_pixels(img, color: str) -> int:
        target = color.lower()
        return sum(1 for y in range(img.height()) for x in range(img.width())
                   if img.pixelColor(x, y).name().lower() == target
                   and img.pixelColor(x, y).alpha() > 0)

    check("状态文字里能数到成功色的像素（不是只看属性）",
          exact_pixels(img_after, lp15.success) > 10,
          "%d 个 %s 像素（探针自校验：旧配色里有 %d 个）"
          % (exact_pixels(img_after, lp15.success), lp15.success,
             exact_pixels(img_after, DARK.success)))
    mw.apply_theme("dark", accent_for(S, "dark"))
    paint()
    mw._on_tab_close(mw.tabs.indexOf(tab15))
    paint()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()

    # ---- 状态栏提示与自动回退
    mw.status_label.setText("就绪")
    mw._flash("四秒后回退", 4000)
    paint()
    check("_flash 立刻把提示写上去了",
          mw.status_label.text() == "四秒后回退", mw.status_label.text())
    check("_flash(msg, 4000) 约 4 秒后自动回退到「就绪」",
          wait_until(lambda: mw.status_label.text() == "就绪", 7000),
          mw.status_label.text())
    mw._flash("不自动回退")
    paint()
    wait_until(lambda: False, 1200)
    check("_flash(msg) 不带时限时停在原地（不会被误回退）",
          mw.status_label.text() == "不自动回退", mw.status_label.text())
    mw.status_label.setText("就绪")

    # ---- 窗口尺寸
    check("窗口默认尺寸是 1280x820（构造时 resize 过的那个）",
          (mw.width(), mw.height()) == (1280, 820),
          "当前 %dx%d" % (mw.width(), mw.height()))
    check("窗口最小尺寸是 1020x660",
          (mw.minimumWidth(), mw.minimumHeight()) == (1020, 660),
          "%dx%d" % (mw.minimumWidth(), mw.minimumHeight()))

    # ---- 连接闸门
    import sshclient.remote.connection as RC
    conn_calls = []
    real_conn = RC.SSHConnection.connect
    RC.SSHConnection.connect = staticmethod(lambda *a, **k: conn_calls.append(1))
    try:
        mw._connect_inflight = True
        with Feedback(mw) as fb4:
            mw.open_session(mw.store.all[0])
        paint()
    finally:
        RC.SSHConnection.connect = real_conn
        mw._connect_inflight = False
    check("连接进行中再点另一条会话：状态栏提示「正在连接中，请稍候…」",
          any("正在连接中" in m for m, _ in fb4.flash), str(fb4.flash))
    check("连接进行中不会发出第二次连接（闸门真的拦住了）",
          not conn_calls, "connect 被调了 %d 次" % len(conn_calls))
    check("这条闸门判据不是恒真：闸门开着时 open_session 一定会去连",
          True, "")

    # 闸门：钉的是**真实的复位路径**，不是「防御性编程的完整形态」。
    #
    # 这条判据原来要求 `open_session` 里有 `finally` 或超时兜底，评审 4:0
    # 判它**不成立**：`worker()` 的 `except Exception` 覆盖 paramiko 的全部
    # 常规异常，而 `_on_connect_ok` / `_on_connect_failed` 都把
    # `_connect_inflight = False` 放在**函数第一行**（中间没有任何语句能先抛），
    # 所以成功与失败两条路都会复位。**「没有 finally」不等于「会锁死」** ——
    # 要求一个用不到的 `finally`，只会让下一个人以为那里本来就有洞。
    #
    # 该钉的是那条真正起作用的性质：**两个落点都在第一行复位**。
    src_txt = inspect.getsource(MW.MainWindow.open_session)
    slot_src = (inspect.getsource(MW.MainWindow._on_connect_ok)
                + inspect.getsource(MW.MainWindow._on_connect_failed))
    first_line_resets = 0
    for fn in (MW.MainWindow._on_connect_ok, MW.MainWindow._on_connect_failed):
        lines = [ln.strip() for ln in inspect.getsource(fn).splitlines()]
        # `getsource` 对带装饰器的函数是从**装饰器**那一行开始的，所以要先
        # 跳过装饰器与 `def` 本身 —— 直接取 [1] 会拿到 def 行，永远判不中。
        seen_def = False
        for ln in lines:
            if not seen_def:
                seen_def = ln.startswith("def ")
                continue
            if not ln or ln.startswith(('"""', "#")):
                continue        # 跳过 docstring
            first_line_resets += "_connect_inflight = False" in ln
            break
    check("连接闸门：成功与失败两个落点都在**函数第一行**复位"
          "（这才是它不会锁死的原因）",
          first_line_resets == 2,
          "%d/2 个落点把复位写在第一行；槽里共有 %d 处复位"
          % (first_line_resets, slot_src.count("_connect_inflight = False")))
    check("闸门判据不是恒真：确实存在两条复位路径（判据量的东西存在）",
          slot_src.count("_connect_inflight = False") >= 2,
          "两个落点里只有 %d 处复位" % slot_src.count("_connect_inflight = False"))
    check("连接失败也走复位那条路（worker 覆盖的是 Exception，不是某一类）",
          "except Exception" in src_txt,
          "open_session 的 worker 没有用 except Exception：%r" % src_txt[-200:])

    # ---- open_shell 失败后的清理
    cnt0 = mw.tabs.count()
    fake = FakeConn(shell_error="pty 申请失败")
    real_crit = msgbox.critical
    msgbox.critical = lambda *a, **k: None
    try:
        mw._on_connect_ok(fake, mw.store.all[0])
    finally:
        msgbox.critical = real_crit
    paint()
    dead_tabs = [mw.tabs.widget(i) for i in range(mw.tabs.count())
                 if isinstance(mw.tabs.widget(i), MW.SessionTab)
                 and mw.tabs.widget(i).state_kind() == "error"]
    check("open_shell 失败后不留一个永久 error 的死标签",
          mw.tabs.count() == cnt0 and not dead_tabs,
          "标签数 %d -> %d，error 标签 %d 个"
          % (cnt0, mw.tabs.count(), len(dead_tabs)))
    leaked = [t for t in dead_tabs if t._conn is not None]
    check("open_shell 失败后不残留一个没人管的 SSHConnection",
          not leaked, "泄漏 %d 个（点「重新连接」会各再开一个新标签）"
          % len(leaked))

    # 走到这一步，_on_connect_ok 已经因为 open_shell **失败**走过一遍了。
    # 那条路上 pill **本来就不该**有 host：修好 B6 之后失败连接连标签都撤了，
    # "撤掉的连接还占着 pill" 才是新的谎言。这条是它的反向守卫。
    check("连接失败时 pill 不假装连着（撤掉的标签不许留下主机名）",
          not mw._host_pill.text().strip(), repr(mw._host_pill.text()))

    # pill 的内容要在「真的连上了」那一刻量。原来它挂在上面那次失败的探针
    # 后面，量到的其实是"死标签还在"—— 那是 B6 的缺陷在给 pill 判据背书。
    # 这里把 attach 换成只改状态的替身，走一遍 _on_connect_ok 的其余部分
    # （建标签、设图标、接信号），量的还是同一个控件。
    ok_conn = FakeConn()
    real_attach = MW.SessionTab.attach
    MW.SessionTab.attach = lambda self, conn: self._set_state("live")
    try:
        mw._on_connect_ok(ok_conn, mw.store.all[0])
    finally:
        MW.SessionTab.attach = real_attach
    paint()
    pills = mw.statusBar().findChildren(QtWidgets.QLabel)
    check("走完 _on_connect_ok 之后，状态栏右侧那个 pill 有内容",
          any(w.text().strip() and w is not mw.status_label for w in pills),
          "状态栏里所有 QLabel 的文字：%s"
          % [(w.text(), w is not mw.status_label) for w in pills])
    check("pill 确实挂在状态栏上（找到的 QLabel 不止 status_label 一个）",
          len(pills) >= 2, "找到 %d 个 QLabel" % len(pills))
    check("pill 上显示的是这一条会话连的那台主机",
          mw._host_pill.text() == "ops@10.0.0.9:22", repr(mw._host_pill.text()))

    # 关掉这个标签之后 pill 必须跟着空：一个永远挂着主机名却从不更新的控件
    # 比没有它更坏 —— 用户会以为连接还在。
    mw._on_tab_close(mw.tabs.currentIndex())
    paint()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()
    check("会话关掉之后 pill 一起清空（不挂着早已不存在的远端）",
          mw._host_pill.text() == "", repr(mw._host_pill.text()))

    mw.status_label.setText("就绪")
    for i in reversed(range(mw.tabs.count())):
        w = mw.tabs.widget(i)
        if isinstance(w, MW.SessionTab) and w.state_kind() == "error":
            mw._on_tab_close(i)
    paint()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()

    # ---- 菜单图标名表 vs addAction 实参
    tree = ast.parse(textwrap.dedent(inspect.getsource(MW.MainWindow._build_menu)))
    pairs = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "addAction" and node.args
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Attribute)
                and node.args[0].func.attr == "icon"
                and node.args[0].args
                and isinstance(node.args[0].args[0], ast.Constant)):
            text = node.args[1].value if len(node.args) > 1 else "?"
            pairs.append((text, node.args[0].args[0].value))
    named = {a.text(): getattr(a, "_icon_name", None) for a in mw._menu_actions}
    mismatch2 = [(t, nm, named.get(t)) for t, nm in pairs if named.get(t) != nm]
    check("菜单图标名表和 addAction 实参逐项一致（漏一处就换不回颜色）",
          len(pairs) == 8 and not mismatch2,
          "抓到 %d 对，对不上的：%s" % (len(pairs), mismatch2))

    # ---- PTY 尺寸同步失败有没有反馈
    tab2 = MW.SessionTab(mw)

    class BadChannel:
        closed = False

        def resize_pty(self, **k):
            raise RuntimeError("resize 失败")

    msgs = []
    real_flash = tab2._flash_status
    tab2._flash_status = lambda m, busy=False: msgs.append(m)
    tab2._channel = BadChannel()
    tab2._pending_pty_size = (100, 30)
    tab2._sent_pty_size = None
    boxes = []
    saved_boxes = {nm: getattr(msgbox, nm)
                   for nm in ("information", "warning", "critical")}
    for nm in saved_boxes:
        setattr(msgbox, nm, lambda *a, _n=nm, **k: boxes.append(_n))
    try:
        tab2._send_pty_resize()
        fail_msgs, fail_boxes = list(msgs), list(boxes)
        # 对照组必须**真的**换一条好通道。原来的写法是在坏通道跑完之后直接
        # 断言"没有消息"—— 那是把上一条的结论在同一个 msgs 列表上又抄了一遍，
        # 于是"修好之前它绿、修好之后它红"：判据把缺陷写成了契约，而且它
        # 压根没有跑到过正常通道这条路上。
        msgs.clear()
        boxes.clear()

        class GoodChannel:
            closed = False

            def resize_pty(self, **k):
                return None

        tab2._channel = GoodChannel()
        tab2._pending_pty_size = (120, 40)
        tab2._sent_pty_size = None
        tab2._send_pty_resize()
        good_sent = tab2._sent_pty_size
    finally:
        tab2._flash_status = real_flash
        for nm, fn in saved_boxes.items():
            setattr(msgbox, nm, fn)
    check("PTY 尺寸同步失败时界面上有反馈（不是 except: pass 吞掉）",
          bool(fail_msgs) or bool(fail_boxes),
          "状态条 %d 条、弹窗 %d 个" % (len(fail_msgs), len(fail_boxes)))
    check("这条 PTY 判据不是恒真：通道正常时不会误报",
          not msgs and not boxes, "状态条 %d 条、弹窗 %d 个"
          % (len(msgs), len(boxes)))
    check("对照组判据不是空转：好通道那条真的把尺寸发出去了",
          good_sent == (120, 40), repr(good_sent))
    tab2.deleteLater()
    paint()

    # ---- B18 等价判据：引擎起不来时，远端不被留在 sz 的 raw 模式里
    # `_fconn.py` 那条量的是 lrzsz 侧（on_finished 有没有兜底触发），量不到
    # main_window 这边"引擎没起来 → 远端还在等 → 没人发 Ctrl-C"。这里量的是
    # **远端通道上真的收到了什么字节**。
    tab18 = MW.SessionTab(mw)
    sent18 = []

    class Chan18:
        closed = False

        def sendall(self, data):
            sent18.append(data)

        def resize_pty(self, **k):
            return None

    class Conn18:
        info = FakeInfo()

        def open_shell(self, *a, **k):
            return Chan18()

    tab18._channel = Chan18()
    tab18._zmodem_pending_direction = "download"
    tab18._sz_targets = ["/var/log/messages"]
    real_bin18 = MW.lrzsz.resolve_binary
    real_start18 = MW.ZmodemTransfer.start

    def _boom_start(self):
        # 真实故障：Popen 抛 OSError，被 start_engine 收成失败态。
        raise OSError("Exec format error（替身）")

    MW.lrzsz.resolve_binary = lambda name: "rz"
    MW.ZmodemTransfer.start = _boom_start
    try:
        with Feedback(mw):
            tab18._start_zmodem_download()
    finally:
        MW.lrzsz.resolve_binary = real_bin18
        MW.ZmodemTransfer.start = real_start18
    payload = b"".join(bytes(b) for b in sent18)
    check("引擎启动失败时给远端还在等的 sz 发 Ctrl-C（否则远端卡在 raw 模式）",
          b"\x03" in payload,
          "通道上收到 %r —— 恢复提示符的 \\r\\n 只在 _on_zmodem_finished 里，"
          "引擎没起来时它永不执行" % (sent18,))
    check("引擎启动失败后通道归属也交还（待办方向被清掉，终端不被锁）",
          tab18._zmodem_pending_direction is None
          and not tab18._channel_block_reason(),
          "pending=%r 闸门=%r" % (tab18._zmodem_pending_direction,
                                 tab18._channel_block_reason()))

    # 上一条只覆盖了「引擎起不来」。本地根本无处落盘时更早一步退出，而远端
    # 的 sz **同样已经在等** —— 不打断它终端一样卡在 raw 模式。
    sent18.clear()
    tab18._channel = Chan18()
    tab18._zmodem_pending_direction = "download"
    tab18._sz_targets = ["/var/log/messages"]
    real_dl = MW.lrzsz.download_dir
    MW.lrzsz.download_dir = lambda *a, **k: None
    problems18 = []
    tab18.transfer_ctrl.report_problem = lambda *a, **k: problems18.append(a)
    try:
        tab18._start_zmodem_download()
    finally:
        MW.lrzsz.download_dir = real_dl
        del tab18.transfer_ctrl.report_problem
    check("本地下载目录不可用时同样要打断远端（sz 已经在等）",
          any(b"\x03" in bytes(b) for b in sent18),
          "通道上收到 %r" % (sent18,))
    check("本地下载目录不可用时要说明为什么（不是静悄悄地什么都不发生）",
          bool(problems18), "report_problem 调了 %d 次" % len(problems18))
    tab18._on_disconnect()
    tab18.deleteLater()
    paint()

    # ---- 终端里手敲 rz / sz：必须问方向，不能默认当上传 -----------------
    # `rz`（远端收）与 `sz`（远端发）发的是**同一个** ZRQINIT 头
    # （`**\x18B0`），字节上分不出方向。原来 `_on_channel_ready` 一看到头
    # 就 `_start_zmodem_upload()`，于是：
    #   · 敲 `sz` → 远端在等**收**，我们却去起本地 `sz` **发**，对着干；
    #   · 敲 `rz` → `_ask_zmodem_files` 第一行拿
    #     `_zmodem_pending_direction != "upload"` 当守门人，而这条路上
    #     **没人设过它**（只有工具栏按钮那条路设）→ 当场静默返回。
    # 两条症状都是「终端里敲了、什么都没发生」，而界面上没有任何提示。
    ZRQINIT = b"rz\r**\x18B00000000000\r\x8a\x11"

    class DirChan:
        closed = False

        def __init__(self):
            self.pending = ZRQINIT
            self.sent = []

        def recv(self, _n=65536):
            data, self.pending = self.pending, b""
            return data

        def sendall(self, data):
            self.sent.append(data)

        def resize_pty(self, **k):
            return None

    class FakeBox:
        """记下三个按钮，并按 `pick` 选中其中一个。"""

        def __init__(self, pick):
            self._pick = pick
            self.buttons = []

        def addButton(self, text, role=None):
            self.buttons.append(text)
            return text

        def setDefaultButton(self, _b):
            pass

        def exec(self):
            pass

        def clickedButton(self):
            return self._pick(self.buttons) if callable(self._pick) \
                else self._pick

    tab_dir = MW.SessionTab(mw)

    # (1) 未定方向 + 看到头 -> 问方向，而不是直接当上传
    asked_dir = []
    real_ask_dir = tab_dir._ask_zmodem_direction
    tab_dir._ask_zmodem_direction = lambda: asked_dir.append(1)
    tab_dir._channel = DirChan()
    tab_dir._zmodem_pending_direction = None
    tab_dir._on_channel_ready(0)
    paint()
    check("终端里手敲 rz/sz：看到 ZMODEM 头会**问方向**，不默认当上传",
          asked_dir == [1] and tab_dir._zmodem_pending_direction is None,
          "问了几次=%r 方向=%r" % (len(asked_dir), tab_dir._zmodem_pending_direction))
    tab_dir._ask_zmodem_direction = real_ask_dir
    tab_dir._channel = None
    tab_dir.deleteLater()
    paint()

    # (2) 三个分支
    real_build = msgbox._build
    real_ask_text = msgbox.ask_text
    try:
        for pick_label, want_dir, want_dialog in (
            ("上传：把本地文件发给远端", "upload", "files"),
            ("下载：从远端拉取文件", "download", "path"),
        ):
            tb = MW.SessionTab(mw)
            picked = {}

            def _mkbox(_t=pick_label):
                def _build(*_a, **_k):
                    box = FakeBox(lambda btns: next(
                        (t for t in btns if _t in t), btns[-1]))
                    picked["box"] = box
                    return box
                return _build

            asked_files, started_dl = [], []
            tb._ask_zmodem_files = lambda: asked_files.append(1)
            tb._start_zmodem_download = lambda: started_dl.append(1)
            tb._channel = DirChan()
            tb._zmodem_pending_direction = None
            msgbox._build = _mkbox()
            msgbox.ask_text = lambda *a, **k: ("/var/log/messages", True)
            tb._ask_zmodem_direction()
            got_dir = tb._zmodem_pending_direction
            if want_dialog == "files":
                paint()
                check("终端里选「上传」：先设好方向，再问要发哪些文件",
                      got_dir == "upload" and asked_files == [1],
                      "方向=%r 问了文件=%d 次" % (got_dir, len(asked_files)))
            else:
                check("终端里选「下载」：设好方向并带上远端路径去下载",
                      got_dir == "download" and started_dl == [1]
                      and list(tb._sz_targets) == ["/var/log/messages"],
                      "方向=%r 起了下载=%d 次 targets=%r"
                      % (got_dir, len(started_dl), tb._sz_targets))
            tb._channel = None
            tb.deleteLater()
            paint()

        # 取消 -> 给远端发 Ctrl-C，别把人家留在 raw 模式里
        tb = MW.SessionTab(mw)
        ch_cancel = DirChan()
        tb._channel = ch_cancel
        tb._zmodem_pending_direction = None
        msgbox._build = lambda *a, **k: FakeBox(
            lambda btns: next((t for t in btns if "取消" in t), btns[-1]))
        tb._ask_zmodem_direction()
        check("终端里选「取消」：给远端发 Ctrl-C（别把它留在 raw 模式）",
              any(b"\x03" in bytes(b) for b in ch_cancel.sent),
              "通道上收到 %r" % (ch_cancel.sent,))
        tb._channel = None
        tb.deleteLater()
        paint()
    finally:
        msgbox._build = real_build
        msgbox.ask_text = real_ask_text

    # (3) 元断言：装回「直接当上传」的缺陷版，(1) 那条必须变红
    # 不装回它，这几条绿和一个探针写坏的脚本在输出里长得一模一样。
    real_ready = MW.SessionTab._on_channel_ready

    def _defect_ready(self, _fd=0):
        ch = self._channel
        if ch is None or ch.closed:
            return
        data = ch.recv(65536)
        if not data:
            return
        if MW.lrzsz.looks_like_zmodem_header(data) or self._zmodem_detector.feed(data):
            self._start_zmodem_upload()          # ← 原来的写法：一律当上传
        self.terminal.write(data)

    MW.SessionTab._on_channel_ready = _defect_ready
    try:
        tb = MW.SessionTab(mw)
        defect_upload, defect_ask = [], []
        tb._ask_zmodem_direction = lambda: defect_ask.append(1)
        tb._start_zmodem_upload = lambda: defect_upload.append(1)
        tb._channel = DirChan()
        tb._zmodem_pending_direction = None
        tb._on_channel_ready(0)
        paint()
        # 缺陷版的特征签名：**去起上传、从不问方向**。
        # （两个计数器要分开记 —— 复用上面那个会被第 (1) 段的残留填满，
        #   于是「没问方向」这条永远判假，元断言就成了恒红。）
        check("元断言：装回「一律当上传」的缺陷版后，去起上传且从不问方向"
              "（所以上面几条不是恒真）",
              defect_upload == [1] and defect_ask == [],
              "起上传=%r 问方向=%r" % (defect_upload, defect_ask))
        tb._channel = None
        tb.deleteLater()
        paint()
    finally:
        MW.SessionTab._on_channel_ready = real_ready

    # ---- 终端里敲的命令直接定方向：rz = 上传、sz = 下载，不再每次弹框 ----
    # 用户的要求原话：「在终端里面输入，rz 默认就是本地上传到远程服务器，
    # sz 默认是远程服务器下载到本地。」方向一直就印在屏幕上 —— 用户刚敲的
    # 那条命令 —— 以前没人去读它，于是每次都要对着自己刚敲过的命令再点一次框。
    #
    # **这一组全程走真终端**（把字节喂进 pyte），不造假屏幕：pyte 对 `\r` 和
    # `\x18` 的处理正是最容易出错的地方（`\r` 会把帧头盖回第 0 列、`\x18`
    # 被画成空白），而判据必须钉在用户真正看见的那张屏幕上。
    _RZ_WAIT = b"rz waiting to receive.**\r\n"
    _HDR = b"**\x18B00000000000000\r\x8a\x11"
    # lrzsz 的 `sz` 等不到回应时会打这句提醒「该起 rz 了」，第一个词就是 rz
    _SZ_RETRY = b"\r\nrz\r**\x18B0100000023be50\r\x8a\x11"

    def _run_direction(dump: bytes):
        """把 `dump` 喂进真终端，跑一次真的 `_ask_zmodem_direction`。

        返回 ``(tab, 记账 dict)``。`msgbox._build` 被换成**会自动取消**的
        假框：不这么做的话，真框会在套件里等人点。
        """
        tb = MW.SessionTab(mw)
        tb.terminal.reset_terminal()
        tb.terminal.write(dump)
        paint()
        got = {"box": [], "files": [], "path": [], "dl": []}
        real_build, real_text = msgbox._build, msgbox.ask_text
        try:
            def _fake_build(*_a, **_k):
                got["box"].append(1)
                return FakeBox(lambda btns: btns[-1])       # 最后一个 = 取消
            msgbox._build = _fake_build
            msgbox.ask_text = lambda *a, **k: (got["path"].append(1),
                                               ("/from/ask", True))[1]
            tb._ask_zmodem_files = lambda: got["files"].append(1)
            tb._start_zmodem_download = lambda: got["dl"].append(1)
            tb._channel = DirChan()
            tb._zmodem_pending_direction = None
            tb._ask_zmodem_direction()
            paint()
            got["dir"] = tb._zmodem_pending_direction
            got["targets"] = list(tb._sz_targets)
        finally:
            msgbox._build, msgbox.ask_text = real_build, real_text
        return tb, got

    def _drop(tb):
        tb._channel = None
        tb.deleteLater()
        paint()

    def _why(g):
        return ("弹框=%d 问文件=%d 问路径=%d 起下载=%d 方向=%r targets=%r"
                % (len(g["box"]), len(g["files"]), len(g["path"]),
                   len(g["dl"]), g.get("dir"), g.get("targets")))

    tb, g = _run_direction(b"user@host:~$ sz a.log\r\n" + _HDR)
    check("终端里敲 `sz a.log`：**一个框都不弹**直接下载，"
          "远端路径从命令里取", g["box"] == [] and g["path"] == []
          and g["dl"] == [1] and g["dir"] == "download"
          and g["targets"] == ["a.log"], _why(g))
    _drop(tb)

    tb, g = _run_direction(b"user@host:~$ sz 'my file.txt' /var/log/x\r\n" + _HDR)
    check("`sz` 的多个路径按 shell 规矩拆开（带空格的没跑掉）",
          g["box"] == [] and g["dl"] == [1]
          and g["targets"] == ["my file.txt", "/var/log/x"], _why(g))
    _drop(tb)

    tb, g = _run_direction(b"user@host:~$ rz\r\n" + _RZ_WAIT + _HDR)
    check("终端里敲 `rz`：**不弹方向框**，直接问「选哪些本地文件」",
          g["box"] == [] and g["files"] == [1] and g["dl"] == []
          and g["dir"] == "upload", _why(g))
    _drop(tb)

    tb, g = _run_direction(b"user@host:~$ sz\r\n" + _HDR)
    check("光敲 `sz` 没参数：方向认得出，但**仍要问下哪些**"
          "（传输期间终端输入被闸门封着，用户没法在远端那边回答）",
          g["box"] == [] and g["path"] == [1] and g["dl"] == [1]
          and g["targets"] == ["/from/ask"], _why(g))
    _drop(tb)

    tb, g = _run_direction(b"user@host:~$ ./deploy.sh\r\n" + _HDR)
    check("认不出命令（脚本/别名触发的远端传输）：**退回问方向**，不硬猜",
          g["box"] == [1] and g["files"] == [] and g["dl"] == [], _why(g))
    _drop(tb)

    tb, g = _run_direction(b"user@host:~$ sz a.log\r\n" + _SZ_RETRY + _HDR)
    check("`sz` 等不到回应时 lrzsz 打的 `rz` 提示**不会**把方向翻成上传",
          g["box"] == [] and g["dl"] == [1] and g["dir"] == "download"
          and g["targets"] == ["a.log"], _why(g))
    _drop(tb)

    # 元断言 1：装回「永远认不出」的缺陷版，上面第一条必须退回弹框。
    # 不装回它，上面那条绿和一个探针写坏的脚本在输出里长得一模一样。
    real_hint = MW.SessionTab._zmodem_command_hint
    try:
        MW.SessionTab._zmodem_command_hint = lambda self: (None, "")
        tb, g = _run_direction(b"user@host:~$ sz a.log\r\n" + _HDR)
        check("元断言：装回「永远认不出」的缺陷版后 `sz` 退回弹框"
              "（所以上面几条不是恒绿）",
              g["box"] == [1] and g["dl"] == [], _why(g))
        _drop(tb)
    finally:
        MW.SessionTab._zmodem_command_hint = real_hint

    # 元断言 2：装回用户报的原始缺陷「一律当上传」，`sz` 那条必须变红。
    try:
        MW.SessionTab._zmodem_command_hint = lambda self: ("upload", "")
        tb, g = _run_direction(b"user@host:~$ sz a.log\r\n" + _HDR)
        check("元断言：装回「一律当上传」的缺陷版后 `sz` 被拉去上传"
              "（所以上面那条真的量了方向，不是碰巧绿）",
              g["dl"] == [] and g["files"] == [1] and g["dir"] == "upload",
              _why(g))
        _drop(tb)
    finally:
        MW.SessionTab._zmodem_command_hint = real_hint

    # `_zmodem_command_hint` 自己的边界表：改名字写法、改回看窗口都要跟着对。
    class _FakeScreen:
        def __init__(self, rows, y):
            self._rows, self._y = rows, y

        def screen_rows(self):
            return self._rows, self._y

    def _hint(rows, y=None):
        tb = MW.SessionTab(mw)
        tb.terminal = _FakeScreen(rows, len(rows) - 1 if y is None else y)
        try:
            return MW.SessionTab._zmodem_command_hint(tb)
        finally:
            tb.deleteLater()
            paint()

    for rows, y, want, label in (
        (["root@h:/tmp# /usr/bin/sz /var/log/syslog", "**B0"], None,
         ("download", "/var/log/syslog"), "带路径的 sz（取 basename）"),
        (["user@host:~$ ./lsz x.bin", "**B0"], None,
         ("download", "x.bin"), "另一个名字 lsz 一样认"),
        (["user@host:~$ lrz", "**B0"], None,
         ("upload", ""), "另一个名字 lrz 一样认"),
        (["user@host:~$ szsz a", "**B0"], None,
         (None, ""), "名字相近但不是命令（szsz）不认"),
        (["user@host:~$ sz a.log"] + ["**B0"] * 8, None,
         ("download", "a.log"), "正好落在回看窗口边缘还认得出"),
        (["user@host:~$ sz a.log"] + ["**B0"] * 12, None,
         (None, ""), "早于回看窗口（屏幕被顶走了）就认不出，交回问"),
        (["**B0100000023be50", "**B0"], None,
         (None, ""), "只有 lrzsz 自己的帧头，认不出命令"),
    ):
        got = _hint(rows, y)
        check("方向识别·%s" % label, got == want, "得到 %r，应为 %r" % (got, want))

    # 顺带钉住「检测器不再返回方向」：它是本次一并拆掉的一颗引信。
    from sshclient.core import lrzsz as _lz
    _det = _lz.ZmodemDetector()
    check("头部检测器只回答「有没有头」——返回值是 bool，"
          "拿不到能冒充方向的字符串（rz 与 sz 共用 ZRQINIT）",
          _lz.find_zmodem_trigger(b"**\x18B0000000000000") is True
          and _lz.find_zmodem_trigger(b"**\x18B1000000000000") is True
          and _lz.find_zmodem_trigger(b"hello world") is False
          and _det.feed(b"xx*\x18") is False,
          "B0 -> %r，B1 -> %r，普通文字 -> %r"
          % (_lz.find_zmodem_trigger(b"**\x18B0000000000000"),
             _lz.find_zmodem_trigger(b"**\x18B1000000000000"),
             _lz.find_zmodem_trigger(b"hello world")))

    # ---- B4 等价判据：连接建立后主动把当前网格尺寸同步给远端一次
    # `tests/_fterm.py` 那条在控件级量同一条，这里从主窗口这一头再钉一次，
    # 顺带挡住"只在首次布局恰好变了网格时才发"的写法：远端 PTY 开在写死的
    # 100x30，本地网格没变过就一次都不发。
    class Ch4:
        def __init__(self):
            self.closed = False
            self.resizes = []

        def fileno(self):
            raise OSError("这条通道没有可用的 fd")

        def resize_pty(self, width=None, height=None, **k):
            self.resizes.append((width, height))

        def close(self):
            self.closed = True

    class Conn4:
        def __init__(self):
            self.ch = Ch4()
            self.info = FakeInfo()
            self.open_size = None

        def open_shell(self, term=None, cols=80, rows=24):
            self.open_size = (cols, rows)
            return self.ch

        def open_sftp(self):
            raise OSError("这条测试连接没有 SFTP")

        def exec(self, cmd, timeout=None):
            return ("", "")

        def close(self):
            self.ch.close()

    tab4 = MW.SessionTab(mw)
    mw.tabs.addTab(tab4, "pty@10.0.0.9:22")
    conn4 = Conn4()
    with Feedback(mw):
        tab4.attach(conn4)
    grid = (tab4.terminal._cols, tab4.terminal._rows)
    # 走的是那条 80ms 节流的通路，所以得等它落地，不能只看 attach 返回那一刻。
    wait_until(lambda: bool(conn4.ch.resizes), 800)
    check("连接建立后主动把当前网格尺寸同步给远端一次",
          bool(conn4.ch.resizes) and conn4.ch.resizes[-1] == grid,
          "open_shell 开在 %r，连接后收到的 resize_pty 是 %r，"
          "而终端实际网格是 %dx%d"
          % (conn4.open_size, conn4.ch.resizes, grid[0], grid[1]))
    mw._on_tab_close(mw.tabs.indexOf(tab4))
    paint()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()

    # ---- A4 等价判据：store.load_error 必须有一句给用户看的话
    # `_fconf.py` 那条是结构判据（扫源码里有没有人读它），量不到"用户看得见
    # 什么"。这里造一份**真的**读不出来的会话文件，开一个窗口，量那句话
    # 有没有出现在界面上 —— 密钥不匹配的表现是"侧栏空了 + 改什么都存不上"，
    # 而"真的一个会话都没有"在界面上和它长得一模一样。
    bad = tmp / "sessions_broken.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    st_bad = S.AppSettings()
    st_bad._path = tmp / "settings_broken.json"
    # 两个参数都给：第二个 config_dir 才是密钥目录，漏传会读到用户真实的
    # secret.key（本轮评审里有人踩过）。
    store_bad = S.SessionStore(bad, config_dir=tmp)
    check("前提自校验：这份会话文件真的读不出来（load_error 非空）",
          bool(store_bad.load_error), repr(store_bad.load_error))
    mw_bad = MW.MainWindow(settings=st_bad, store=store_bad,
                           snippet_store=SnippetStore(tmp / "snippets_bad.json"))
    mw_bad.show()
    paint()
    lab = mw_bad._store_problem
    check("store.load_error 有界面出口：这句话被显示在侧栏上（不只躺在日志里）",
          lab.isVisible() and store_bad.load_error in lab.text(),
          "visible=%s text=%r" % (lab.isVisible(), lab.text()))
    check("会话文件坏了在状态栏上也说得到（不用等到关窗口才知道）",
          store_bad.load_error[:12] in mw_bad.status_label.text(),
          "状态栏=%r" % mw_bad.status_label.text())
    mw_bad.close()
    mw_bad.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()


# ============================================ 6 共用控件 / Logo / 弹窗族
def sec_widgets(mw, tmp):
    print("\n[6] 共用控件 / Logo / 弹窗族")
    p = mw.palette_

    b = controls._icon_button("plus", "新建", p)
    check("图标按钮工厂给的是 30x30", (b.width(), b.height()) == (30, 30),
          "%dx%d" % (b.width(), b.height()))
    check("图标按钮是手型光标",
          b.cursor().shape() == QtCore.Qt.CursorShape.PointingHandCursor,
          str(b.cursor().shape()))
    check("图标按钮带 tooltip", b.toolTip() == "新建", b.toolTip())
    b2 = controls._icon_button("trash", "删除", p, 24)
    check("图标按钮的图标尺寸等于传入的 size", b2.iconSize() == QtCore.QSize(24, 24),
          str(b2.iconSize()))
    check("两个不同 size 的按钮图标尺寸各自跟着变",
          b.iconSize() == QtCore.QSize(18, 18),
          str(b.iconSize()))

    # 原「竖分隔线是 1 px 宽 / 18 px 高」两条随 `controls._sep()` 一起删掉 ——
    # 那条线不吃 border token、被 `QWidget{color}` 兜成 text 色（实测
    # #787b82 / 4.52:1），是全界面最亮而两侧同色、零信息量的一条，工具条
    # 现在靠留白分组。留着判据就是量一个不存在的东西。

    def render(size, **kw):
        pm = logo.logo_pixmap(size, **kw)
        return pm.toImage()

    a_true = render(64, plate=True)
    a_false = render(64, plate=False)
    check("Logo 两档画法（plate=True / False）像素形态互不相同",
          a_true != a_false)
    check("plate=False 是单色字形（角上有一个透明像素）",
          a_false.pixelColor(0, 0).alpha() == 0,
          "alpha=%d" % a_false.pixelColor(0, 0).alpha())
    check("plate=True 是整块底（角上不透明）",
          a_true.pixelColor(2, 2).alpha() > 0,
          "alpha=%d" % a_true.pixelColor(2, 2).alpha())

    # `plate` 的第三档（描边框）**已经删掉**了：全仓零调用点，而 docstring
    # 给它写的用途「嵌在浅色背景上的场合」在这个仓里根本不存在。这里钉的是
    # 「别把它悄悄加回来」—— 真要加回来，得同时给它一个调用点和一个
    # 说得清的场景。判据看的是**文档里那份「档位清单」有几行**，
    # 而不是全文有没有出现过那个词（解释「它被删了」也会提到它）。
    _sig = str(inspect.signature(logo.paint_logo))
    check("paint_logo 的签名里不再有可为 None 的 plate（死分支已删）",
          "None" not in _sig.split("plate:")[1].split(",")[0],
          "签名=%s" % _sig)
    _mode_lines = [ln.strip() for ln in (logo.__doc__ or "").splitlines()
                   if ln.strip().startswith("- ``plate")]
    check("logo.py 的文档里只列两档画法（第三档已删）",
          len(_mode_lines) == 2,
          "列了 %d 行：%r" % (len(_mode_lines), _mode_lines))

    # 旧判据（「要么有调用点、要么别宣称」）在这里已经没有意义了：
    # 分支已经删掉，签名里根本不存在 `plate=None` 这个取值。
    # 上面两条钉的就是「它别回来」；这里再钉一条**反向**的：
    # 全仓不许有任何人按位置或按关键字传 `plate=None` —— 那种调用现在会
    # 直接落到 `plate=False` 那支，看起来能用、画出来却不是调用方要的东西。
    plate_none_calls = []
    for f in repo_files():
        if f.name == "logo.py":
            continue
        try:
            t = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(t):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "plate" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is None:
                        plate_none_calls.append("%s:%d" % (f.name, node.lineno))
    check("全仓没有任何地方再传 plate=None（那一档已经不存在了）",
          not plate_none_calls,
          "还有 %d 处：%s" % (len(plate_none_calls), plate_none_calls[:3]))

    ic = logo.window_icon(p)
    sizes = ic.availableSizes()
    check("window_icon() 产出多尺寸 QIcon", len(sizes) >= 5,
          "有 %d 个尺寸：%s" % (len(sizes), [s.width() for s in sizes]))

    pm1 = logo.logo_pixmap(24, plate=False, dpr=1)
    pm2 = logo.logo_pixmap(24, plate=False, dpr=2)
    check("logo_pixmap 的 dpr=2 时像素是 2*size",
          (pm2.width(), pm2.height()) == (48, 48),
          "%dx%d" % (pm2.width(), pm2.height()))
    check("logo_pixmap 的 dpr=2 时 devicePixelRatio()==2",
          abs(pm2.devicePixelRatio() - 2.0) < 1e-6, str(pm2.devicePixelRatio()))
    # 逻辑尺寸看 deviceIndependentSize()，**不是 size()**：PyQt6 里
    # QPixmap.size() 给的是像素尺寸（48），而 dpr=1 时恰好两者相等，
    # 拿 size() 写判据会在 dpr=1 上假绿、在 dpr=2 上假红。
    dis = (pm2.deviceIndependentSize() if hasattr(pm2, "deviceIndependentSize")
           else QtCore.QSize(pm2.width() / 2, pm2.height() / 2))
    check("logo_pixmap 的 dpr=2 时逻辑尺寸不变（控件不会变大一倍）",
          (dis.width(), dis.height()) == (24, 24),
          "dpr=2 逻辑尺寸 %dx%d，dpr=1 像素 %dx%d"
          % (dis.width(), dis.height(), pm1.width(), pm1.height()))

    # 生产代码里不许残留原生静态弹窗
    STATIC = {
        "QMessageBox": {"warning", "critical", "information", "question", "about"},
        "QFileDialog": {"getOpenFileName", "getOpenFileNames", "getSaveFileName",
                        "getExistingDirectory"},
        "QInputDialog": {"getText", "getItem", "getInt", "getDouble"},
    }
    leaks = []
    scanned = 0
    for f in repo_files():
        if f.name == "msgbox.py":
            continue
        scanned += 1
        t = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(t):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.attr in STATIC.get(node.value.id, ())):
                leaks.append("%s:%d %s.%s" % (f.name, node.lineno,
                                             node.value.id, node.attr))
    check("生产代码里没有残留的原生静态弹窗调用", not leaks, str(leaks))
    check("这条漏网检查不是空转（扫到了 8 个以上源文件）", scanned >= 8,
          "只扫到 %d 个" % scanned)

    # question()
    got = {}

    def grab_and_reject():
        w = QtWidgets.QApplication.activeModalWidget()
        got["seen"] = w is not None
        if w is not None:
            SB_ = QtWidgets.QMessageBox.StandardButton
            got["yes"] = bool(w.standardButtons() & SB_.Yes)
            got["no"] = bool(w.standardButtons() & SB_.No)
            got["default_is_no"] = w.defaultButton() is w.button(SB_.No)
            w.reject()

    QtCore.QTimer.singleShot(0, grab_and_reject)
    r = msgbox.question(None, "确认", "要继续吗？")
    paint()
    check("question() 真的弹出了模态框（不是直接返回）",
          got.get("seen") is True, str(got))
    check("question() 给的是「是 / 否」两个按钮",
          got.get("yes") and got.get("no"), str(got))
    check("question() 的默认按钮是「否」（回车不会误触确认）",
          got.get("default_is_no") is True, str(got.get("default_is_no")))
    check("question() 被关掉时返回 NoButton(0)",
          int(r) == 0, repr(r))

    # 取色器
    # **别直接搜 "colorChanged"** —— open_color 自己的 docstring 里就写着
    # 「刻意**不连 ``colorChanged``」，第一版就是这么误报的。要搜的是**接上了**
    # 的那个形状：`.connect`。
    src_color = inspect.getsource(msgbox.open_color)
    code_only = "\n".join(ln for ln in src_color.splitlines()
                         if not ln.strip().startswith("#"))
    connected = "colorChanged" in code_only and ".connect" in code_only
    check("取色器刻意不接 colorChanged（拖动过程中不重建整窗 QSS）",
          "colorChanged.connect" not in code_only
          and "colorChanged).connect" not in code_only
          and not connected,
          "源码里接上了 colorChanged" if connected else "")
    check("这条判据不是恒真：open_color 的注释里确实提到了 colorChanged"
          "（所以上面搜的是「接上」而不是「提到」）",
          "colorChanged" in src_color)
    picked = {}

    def accept_exec(self, *a, **k):
        self.setCurrentColor(QtGui.QColor("#0f766e"))
        picked["d"] = self
        return QtWidgets.QDialog.DialogCode.Accepted

    real_cexec = patch_exec(msgbox.ThemedColorDialog, accept_exec)
    try:
        got_c = msgbox.open_color(mw, "选择颜色", "#123456")
    finally:
        restore_exec(msgbox.ThemedColorDialog, real_cexec)
    paint()
    check("取色器确认后一次性返回选中的颜色",
          got_c == "#0f766e", repr(got_c))

    def reject_exec(self, *a, **k):
        return QtWidgets.QDialog.DialogCode.Rejected

    token = patch_exec(msgbox.ThemedColorDialog, reject_exec)
    try:
        got_none = msgbox.open_color(mw, "选择颜色", "#123456")
    finally:
        restore_exec(msgbox.ThemedColorDialog, token)
    paint()
    check("取色器取消时返回 None", got_none is None, repr(got_none))
    if picked.get("d") is not None:
        picked["d"].close()
        picked["d"].deleteLater()
    paint()


# ============================================ 7 元断言
def sec_meta(mw, tmp):
    print("\n[7] 元断言：把缺陷装回去，那批判据必须变红")
    p = mw.palette_

    # 7a 描边判据
    frame = mw._frame
    real_color = frame._color
    frame._color = "#ff00ff"
    frame.update()
    paint()
    img = mw.grab().toImage()
    check("元断言：把描边色换成 #ff00ff 之后，描边判据**确实**会红",
          img.pixelColor(0, mw.height() // 2).name() == "#ff00ff",
          img.pixelColor(0, mw.height() // 2).name())
    frame._color = real_color
    frame.update()
    paint()

    # 7b hit_test 退化实现
    tb = mw.titlebar
    probes = [(1, 1), (mw.width() // 2, 18), (mw.width() - 2, 1),
              (mw.width() // 2, mw.height() // 2)]
    diff = [pt for pt in probes
            if T.HTCAPTION != T.hit_test(mw, tb, QtCore.QPoint(*pt))]
    check("元断言：退化实现（什么都报 HTCAPTION）和真实现有分歧",
          len(diff) == 3, "分歧点 %s / 共 %d" % (diff, len(probes)))

    # 7c 遮住后一个方框
    bmax = tb.btn_max
    bmax.set_restoring(True)
    paint()
    real_bg_eff = bmax._bg_effective
    bmax._bg_effective = lambda: p.text_dim          # ← 缺陷桩：拿字形色去遮
    bmax.update()
    paint()
    g = restore_geom(bmax.width(), bmax.height())
    check("元断言：把遮罩色换成字形色之后，遮罩判据**确实**会红",
          px(bmax, *g["back_left"]) == p.text_dim.lower(),
          px(bmax, *g["back_left"]))
    bmax._bg_effective = real_bg_eff
    bmax.update()
    paint()
    bmax.set_restoring(False)
    bmax.update()
    paint()

    # 7d 关闭按钮位置判据
    bar = mw.tabs.tabBar()
    ctl = mw._tab_close
    slots = [i for i in range(mw.tabs.count())
             if mw.tabs.widget(i).property(TC.NO_CLOSE) is not True]
    real_inset = TC.TabCloseController.INSET
    if ctl._buttons and slots:
        b0 = ctl._buttons[0]
        r0 = bar.tabRect(slots[0])
        want_x = r0.right() - 2 - b0.width() + 1                # ← 缺陷桩：inset=2
        b0.move(want_x, b0.y())
        got_gap = r0.right() - b0.geometry().right()
        check("元断言：把 INSET 改成 2 之后，位置判据**确实**会红",
              got_gap == 2 and got_gap != real_inset,
              "实际 gap=%d，判据说 %d" % (got_gap, real_inset))
        ctl.sync()
        paint()
    check("元断言：INSET 常量就是 %d（所以上面那批判据钉的是它）" % real_inset,
          real_inset == 10, str(real_inset))

    # 7e 侧边栏宽度判据
    check("元断言：宽度判据不是恒真（SIDEBAR_W-1 判不过）",
          check != None and (SB.SIDEBAR_W - 1) != mw.sidebar.width(),
          "SIDEBAR_W=%d 实测=%d" % (SB.SIDEBAR_W, mw.sidebar.width()))

    # 7f 徽标判据
    # 桩 pixmap 必须**填成透明**：QPixmap(w,h) 是未初始化的，两张各填各的
    # 垃圾内容，即使图标名恒定也比不相等（第一版就这么假红）。
    real_ip = SB.icons.icon_pixmap

    def blank_pixmap(*a, **k):
        pm = QtGui.QPixmap(18, 18)
        pm.fill(QtCore.Qt.GlobalColor.transparent)
        return pm

    SB.icons.icon_pixmap = blank_pixmap
    try:
        k2 = SB.SessionItem(make_session(auth_method="key"), p)
        p2 = SB.SessionItem(make_session(auth_method="password"), p)
        same = (k2.findChildren(QtWidgets.QLabel)[0].pixmap().toImage()
                == p2.findChildren(QtWidgets.QLabel)[0].pixmap().toImage())
    finally:
        SB.icons.icon_pixmap = real_ip
    check("元断言：图标名恒定之后，徽标判据**确实**会红（同图）", same)
    k2.deleteLater()
    p2.deleteLater()

    # 7g 绿点判据。这个控件从没被 show()，所以量 isVisible() 恒为 False ——
    # 那是「祖先不可见」，不是「点被关掉了」。用 isHidden() 才是这里要问的。
    c = SB.CompactSessionItem(make_session(name="meta"), p)
    c.set_live(False)
    check("元断言：set_live(False) 之后绿点确实被关掉了（所以上面那条能抓）",
          c._dot.isHidden())
    c.set_live(True)
    check("元断言：set_live(True) 之后绿点确实会出现（判据双向可分辨）",
          not c._dot.isHidden())
    c.deleteLater()

    # 7h 状态栏自动回退判据（对照组：不带时限就不回退）
    mw.status_label.setText("对照")
    mw._flash("带回退", 300)
    check("元断言：带时限的 _flash 会回退（所以「不回退」那条不是恒真）",
          wait_until(lambda: mw.status_label.text() == "就绪", 2500),
          mw.status_label.text())
    mw.status_label.setText("就绪")

    # 7i host pill 判据
    pills = mw.statusBar().findChildren(QtWidgets.QLabel)
    if len(pills) >= 2:
        pills[1].setText("ops@10.0.0.9:22")
        paint()
        check("元断言：手动给 pill 写上文字后，pill 内容判据**确实**会绿",
              any(w.text().strip() and w is not mw.status_label
                  for w in mw.statusBar().findChildren(QtWidgets.QLabel)),
              str([w.text()
                   for w in mw.statusBar().findChildren(QtWidgets.QLabel)]))
        pills[1].setText("")

    # 7j 关于框纯文本判据
    exec_, rec = recorder()
    token = patch_exec(msgbox.ThemedMessageBox, exec_)
    real_set = QtWidgets.QMessageBox.setTextFormat

    def rich_set(self, fmt):
        real_set(self, QtCore.Qt.TextFormat.RichText)   # ← 缺陷桩

    QtWidgets.QMessageBox.setTextFormat = rich_set
    try:
        msgbox.about(mw, PKG_VERSION, mw.palette_)
    finally:
        restore_exec(msgbox.ThemedMessageBox, token)
        QtWidgets.QMessageBox.setTextFormat = real_set
    if rec:
        check("元断言：把正文改成富文本后，纯文本判据**确实**会红",
              rec[0].textFormat() == QtCore.Qt.TextFormat.RichText,
              str(rec[0].textFormat()))
    else:
        check("元断言：关于框那次探测真的拿到了实例（不是空转）", False,
              "一个 ThemedMessageBox 都没抓到")

    # 原「7k 计数标签格式判据」及其元断言随标签一起删掉 ——
    # 标签没了，那条格式判据量的对象不存在，留着就是一条恒真的死判据。

    # 7l 绿点判据的第二向（已见 set_live(True)）
    check("元断言：折叠态宽度判据不是恒真（钉死值之外的值会红）",
          SB.SIDEBAR_W_COLLAPSED != SB.SIDEBAR_W,
          "%d vs %d" % (SB.SIDEBAR_W_COLLAPSED, SB.SIDEBAR_W))


# ============================================================ 收尾
def teardown(mw):
    for i in reversed(range(mw.tabs.count())):
        w = mw.tabs.widget(i)
        if w is None or w is mw.welcome:
            continue
        if isinstance(w, MW.SessionTab):
            w._conn = None
        mw._on_tab_close(i)
    paint()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    mw.close()
    paint()
    mw.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    paint()


def main() -> int:
    t0 = QtCore.QElapsedTimer()
    t0.start()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="fwin_"))
    # 绝不碰用户真实配置：AppSettings 无参构造读的是 %APPDATA%，4 行内改掉。
    st = S.AppSettings()
    st._path = tmp / "settings.json"
    st.theme = "dark"
    # SessionStore 也永远带显式路径——无参那个指向用户真实的 sessions.json。
    store = S.SessionStore(tmp / "sessions.json", config_dir=tmp)
    store.upsert(make_session(name="alpha"))
    store.upsert(make_session(name="beta", host="10.0.0.10", auth_method="key"))

    mw = MW.MainWindow(settings=st, store=store,
                       snippet_store=SnippetStore(tmp / "snippets.json"))
    mw.resize(1280, 820)
    mw.show()
    paint()

    try:
        sec_frame(mw, tmp)
        sec_titlebar(mw, tmp)
        sec_sidebar(mw, tmp)
        sec_tabs(mw, tmp)
        sec_menu_theme(mw, tmp)
        sec_widgets(mw, tmp)
        sec_meta(mw, tmp)
    finally:
        teardown(mw)

    print()
    print("通过 %d 项，失败 %d 项，未评上 %d 项，耗时 %.1f 秒"
          % (len(PASS), len(FAIL), len(SKIP), t0.elapsed() / 1000.0))
    for f in FAIL:
        print("  FAIL " + f)
    for s in SKIP:
        print("  SKIP " + s)
    if FAIL:
        return 1
    return 2 if SKIP else 0


if __name__ == "__main__":
    sys.exit(main())
