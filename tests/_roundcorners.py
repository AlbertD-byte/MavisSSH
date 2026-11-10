"""四角 5px 圆角：主窗口 + 全部弹框。

**QSS 的 `border-radius` 在这里是被实测否掉的**，不是"没试"：

    只加 QSS border-radius          四角 alpha=255（没圆）
    translucent + QSS radius       四角 alpha=255（还是没圆）
    translucent + 浮层挖角          四角 alpha=0，边缘/中心 alpha=255

所以判据钉在**四角像素的 alpha**：真圆角 = 0。

同时钉住三件容易被"改对了但改坏了"的事：
  - 窗口中心/边缘仍不透明（否则等于把窗口整个挖空，界面上是一块空白）
  - 最大化时**不**挖（Windows/macOS 惯例，且最大化挖角只会露出壁纸）
  - 浮层不吃鼠标（否则窗口最外一圈的缩放带和贴边分屏手势全废）
"""
import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "windows")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from pathlib import Path  # noqa: E402

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from sshclient.core import settings as S  # noqa: E402
from sshclient.core.snippets import SnippetStore  # noqa: E402
from sshclient.ui import main_window as MW  # noqa: E402
from sshclient.ui import msgbox  # noqa: E402
from sshclient.ui.design.v1.palette import get_palette  # noqa: E402
from sshclient.ui.window_frame import CORNER_RADIUS  # noqa: E402

app = QtWidgets.QApplication(sys.argv)

TMP = Path(tempfile.mkdtemp(prefix="mavis-round-"))
FAIL = []
OK = 0


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


def paint(n=4):
    for _ in range(n):
        app.processEvents()


def alphas(w):
    """返回 (四角, 内部透明行, 中心, 上边中, 左边中)。

    **「内部透明行」这条是用户报的那个回归。** 半透明窗口会让「本来没被
    任何控件画过」的区域真的透出桌面（标题栏以下整片）。只看四角是量不
    出来的 —— 四角照样是 0，看着"成功"了，正文却是空的。
    """
    w.grab()
    paint()
    img = w.grab().toImage()
    W, H = img.width(), img.height()
    corners = [img.pixelColor(x, y).alpha() for x, y in
               ((0, 0), (W - 1, 0), (0, H - 1), (W - 1, H - 1))]
    # 从 2 开始：最外 2px 是圆弧区，本来就该有透明像素
    inner = [y for y in range(2, H - 2)
             if img.pixelColor(W // 2, y).alpha() != 255]
    return (corners, inner,
            img.pixelColor(W // 2, H // 2).alpha(),
            img.pixelColor(W // 2, 1).alpha(),
            img.pixelColor(1, H // 2).alpha())


def fmt_inner(inner):
    if not inner:
        return "无"
    return "%d..%d (%d 行)" % (inner[0], inner[-1], len(inner))


# --------------------------------------------------------------- 主窗口
print("\n=== 主窗口 ===")
st = S.AppSettings()
st._path = TMP / "settings.json"      # 不碰用户真实配置
st.theme = "dark"
store = S.SessionStore(TMP / "sessions.json", config_dir=TMP)
mw = MW.MainWindow(settings=st, store=store,
                   snippet_store=SnippetStore(TMP / "snippets.json"))
mw.resize(900, 620)
mw.show()
paint(8)

corners, inner, mid, top_mid, left_mid = alphas(mw)
print("  四角 alpha=%s  内部透明行=%s  中心=%d  上边中=%d  左边中=%d"
      % (corners, fmt_inner(inner), mid, top_mid, left_mid))
check("主窗口四角被挖成透明（真圆角）", all(a == 0 for a in corners),
      "alpha=%s" % corners)
check("主窗口没有一行是透明的（半透明把没画过的地方露出来了）",
      not inner, fmt_inner(inner))
check("主窗口内容仍在（中心不透明，没整窗挖空）", mid == 255, "中心 alpha=%d" % mid)
check("主窗口上边中不透明（不是整条边都挖掉）", top_mid == 255,
      "alpha=%d" % top_mid)
check("主窗口左边中不透明", left_mid == 255, "alpha=%d" % left_mid)
check("圆角半径就是 5px", CORNER_RADIUS == 5, "CORNER_RADIUS=%d" % CORNER_RADIUS)
check("窗口开了半透明背景（圆角的前提）",
      mw.testAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground))

frame = getattr(mw, "_frame", None)
check("浮层不吃鼠标事件（否则最外一圈缩放带/贴边分屏全废）",
      frame is not None and frame.testAttribute(
          QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents))

# 浮层必须始终盖满整窗，否则圆角错位
if frame is not None:
    check("浮层铺满整窗",
          frame.geometry() == QtCore.QRect(0, 0, mw.width(), mw.height()),
          "geo=%s win=%dx%d" % (frame.geometry(), mw.width(), mw.height()))

# 最大化：不该挖角（挖了只会露出壁纸）
mw.showMaximized()
paint(8)
mcorners, minner, mmid, _, _ = alphas(mw)
print("  [最大化] 四角 alpha=%s 内部透明行=%s 中心=%d"
      % (mcorners, fmt_inner(minner), mmid))
check("最大化时四角不挖（Windows/macOS 惯例）",
      all(a != 0 for a in mcorners), "alpha=%s" % mcorners)
check("最大化时也没有透明行（底板在最大化下同样要铺）",
      not minner, fmt_inner(minner))
check("最大化时内容仍在", mmid == 255, "中心 alpha=%d" % mmid)
mw.showNormal()
paint(6)

# 改大小之后浮层还得跟上（漏了就是「一改大小圆角就错位」）
mw.resize(760, 540)
paint(6)
check("改大小后浮层仍铺满整窗",
      frame is not None
      and frame.geometry() == QtCore.QRect(0, 0, mw.width(), mw.height()),
      "geo=%s win=%dx%d" % (frame.geometry(), mw.width(), mw.height()))

# --------------------------------------------------------------- 弹框
print("\n=== 弹框（msgbox 收口，四个子类共用）===")
p = get_palette("dark")

for label, make, setup in (
        ("消息框", lambda: msgbox.ThemedMessageBox(mw, "标题", p),
         lambda d: d.setText("正文")),
        ("取色器", lambda: msgbox.ThemedColorDialog(mw, "选择颜色", p), None),
        ("输入框", lambda: msgbox.ThemedInputDialog(mw, "标题", p),
         lambda d: d.setLabelText("标签")),
        ("文件框", lambda: msgbox.ThemedFileDialog(mw, "选择文件", ""), None)):
    dlg = make()
    if setup is not None:
        setup(dlg)
    dlg.resize(360, 200)
    dlg.show()
    paint(6)
    c, inner, m, t, l = alphas(dlg)
    print("  %-6s 四角 alpha=%s  内部透明行=%s  中心=%d"
          % (label, c, fmt_inner(inner), m))
    check("%s 四角被挖成透明" % label, all(a == 0 for a in c), "alpha=%s" % c)
    check("%s 没有一行是透明的（用户报的就是这个：整框变透明）" % label,
          not inner, fmt_inner(inner))
    check("%s 内容仍在" % label, m == 255, "中心 alpha=%d" % m)
    dlg.close()

# --------------------------------------------------------------- 取色器留白
# 用户报的是「按钮边框与弹窗的边框重叠了」。原来布局边距是 (0,0,0,0)，
# 实测 Cancel 右缘 x=665 而弹窗宽 666 —— 只差 1px。
print("\n=== 取色器：内容不得贴边，标题栏必须满宽 ===")
dc = msgbox.ThemedColorDialog(mw, "选择强调色", p)
dc.show()
paint(8)
bb = dc.findChild(QtWidgets.QDialogButtonBox)
g = bb.geometry()
print("  弹窗 %dx%d  按钮框 %s  -> 左右留白 %d / %d"
      % (dc.width(), dc.height(), g, g.left(), dc.width() - g.right()))
check("取色器按钮框左侧留白 >=10px", g.left() >= 10, "留白 %d" % g.left())
check("取色器按钮框右侧留白 >=10px", dc.width() - g.right() >= 10,
      "留白 %d" % (dc.width() - g.right()))
check("取色器标题栏满宽贴顶（关闭按钮要紧贴右缘）",
      dc._tb is not None and dc._tb.x() == 0
      and dc._tb.width() == dc.width() and dc._tb.y() == 0,
      "%s / 弹窗宽 %d" % (dc._tb.geometry() if dc._tb else None, dc.width()))
inner_x = sorted({c.x() for c in dc.findChildren(QtWidgets.QWidget)
                  if c.parent() is dc and c.isVisible()
                  and c is not dc._tb
                  # 底板和角浮层**本来就该**满宽 x=0，它们是圆角机制的一部分，
                  # 混进来这条判据就恒红。排除掉才量得到「正文内容有没有贴边」。
                  and c is not getattr(dc, "_mavisBacker", None)
                  and c is not getattr(dc, "_mavisFrame", None)})
check("取色器正文没有控件贴左边（除了满宽的标题栏/底板/浮层）",
      not inner_x or min(inner_x) >= 10, "子控件 x=%s" % (inner_x[:6],))
dc.close()

# --------------------------------------------------------------- 弹窗底色
print("\n=== 弹窗底色必须和终端底色分得开 ===")
from sshclient.ui.design.v1 import palette as _P  # noqa: E402

# 距终端底：两套主题同一条承诺（弹窗一眼认得出是浮层）
for theme in ("dark", "light"):
    pal = get_palette(theme)
    d_term = _P._delta(pal.bg_dialog, pal.term_bg)
    d_btn = _P._delta(pal.bg_dialog, pal.bg_elevated)
    print("  [%s] 终端底=%s 弹窗底=%s 按钮底=%s -> 距页面 %.1f 距按钮 %.1f"
          % (theme, pal.term_bg, pal.bg_dialog, pal.bg_elevated, d_term, d_btn))
    check("[%s] 弹窗底与终端底色差 >=16（浅色原来只有 9.9，等于没分）" % theme,
          d_term >= 16, "%.1f" % d_term)

# 距按钮底：**只对浅色卡死**。深色是既有设计（9.9）且按钮还带 1px 描边，
# 用户明确说「深色保持变亮」—— 拿新标准去卡没改过的东西没有意义。
_l = get_palette("light")
check("浅色弹窗底与按钮底色差 >=12（这一轮刚改的，必须给按钮留出余量）",
      _P._delta(_l.bg_dialog, _l.bg_elevated) >= 12,
      "%.1f" % _P._delta(_l.bg_dialog, _l.bg_elevated))
check("浅色弹窗底不再是纯白（原来 #FFFFFF 和页面只差 9.9）",
      _l.bg_dialog.upper() != "#FFFFFF", _l.bg_dialog)

# --------------------------------------------------------------- 元断言
print("\n=== 元断言：装回缺陷版，判据必须变红 ===")
sheet = mw.styleSheet()
# 把浮层摘掉（等价于「没装圆角」），四角应重新变不透明
saved_geo = frame.geometry() if frame is not None else None
if frame is not None:
    frame.hide()
    paint(6)
    c, i2, _, _, _ = alphas(mw)
    check("元断言：摘掉浮层后四角重新不透明（所以上面那组不是恒真）",
          all(a != 0 for a in c), "alpha=%s" % c)
    frame.show()
    frame.setGeometry(saved_geo)
    paint(4)

# 反向元断言：摘掉**弹框**的底板 -> 内部重新出现透明行。
# 不能在主窗口上做这条：主窗口的 central 自己就铺底，底板对它冗余，
# 摘掉当然没变化 —— 那会让这条判据恒绿，什么也没证明。
d_meta = msgbox.ThemedColorDialog(mw, "选择强调色", p)
d_meta.show()
paint(8)
_, _, _, _, _ = alphas(d_meta)
bk_meta = getattr(d_meta, "_mavisBacker", None)
check("元断言：弹框确实装了底板", bk_meta is not None)
if bk_meta is not None:
    bk_meta.hide()
    paint(6)
    _, i3, _, _, _ = alphas(d_meta)
    check("元断言：摘掉弹框底板后内部重新透明（所以「没有透明行」量的是底板）",
          bool(i3), "透明行=%s" % fmt_inner(i3))
    d_meta.close()

# 反向：不挖角时「四角全透明」必须不成立
check("元断言：判据不是恒真（窗口确实开了半透明才可能全 0）",
      mw.testAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)
      and all(a == 0 for a in corners))

mw.close()
paint(4)
mw.deleteLater()
paint(2)

print("\n通过 %d 项，失败 %d 项" % (OK, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  失败：%s" % f)
    sys.exit(1)
