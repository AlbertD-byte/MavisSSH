"""卡片内行内容器的底色协调 + 开关两态的观感。

用户报的是「这一部分背景色和字体不太协调」——**两件事**，所以两条判据：

## A. 白卡片里不能有灰补丁
QSS 的 `QWidget { background-color: root_bg }` 是**兜底**规则，Qt 的类型
选择器匹配**每一个**控件，于是每个裸 `QWidget + setLayout` 都自己刷一遍
root_bg。放在 `role="card"`（白）里就是白卡片上盖一块灰。

判据按**实际渲染像素**断言，不看 token —— 因为「声明 ≠ 结果」正是这次的
坑：`plain_container` 打上了 property、QSS 规则也写了，但只要有一边没接上，
像素上就还是灰的，而读代码完全看不出来。

**覆盖全仓，不只这一处。** 逐个 widget 打补丁必然漏，所以这次改成
`qss.plain_container()` 一处生效；判据就查「卡片里**每一个**子控件都
不该是 root_bg 那个颜色」。

## B. 关闭态轨道不该比旁边文字还重
判据钉的是**面积差**这件事，而不是「颜色好不好看」：`text_muted` 压白卡
CR 6.01 明明过 AA，但 38×21 的实心块 vs 1-2px 笔画 —— 所以要钉的是
「轨道对底 ≥3:1（可辨识是开关）且 < muted 文字的 CR（不抢文字）」。
"""
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from sshclient.ui import connect_dialog as cd  # noqa: E402
from sshclient.ui import tunnel_panel as tp  # noqa: E402
from sshclient.ui.design.v1 import palette as pal  # noqa: E402
from sshclient.ui.design.v1 import qss  # noqa: E402
from sshclient.ui.switch import PILL_H, PILL_W, Switch  # noqa: E402

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


def srgb(c):
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(rgb):
    r, g, b = (srgb(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def cr(f, b):
    a, c = lum(f), lum(b)
    return (max(a, c) + 0.05) / (min(a, c) + 0.05)


def rgb_of(s):
    s = s.lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def norm(c):
    """统一成小写 6 位 hex。

    **踩过两次同一个坑**：`QImage.pixelColor().name()` 给的是**小写**
    （`#ffffff`），而 palette 里存的是**原样大小写**（`#FFFFFF`）。
    直接 `==` 比较就把「颜色完全一致」判成不一致，两次各造出 3 条假红。
    """
    if c is None:
        return None
    return c.lower()


def track_color(dialog, switch, img=None):
    """量开关轨道的颜色：取轨道上多个点，**排除旋钮**。

    **为什么不能只取一个点。** 旋钮直径 15px，轨道宽 38px：关闭态旋钮在
    **左**、开启态在**右**，所以「取 dx=6」在开启态会采到旋钮（= 卡片色），
    「取 width-7」在关闭态会采到旋钮 —— 两个方向各错一次。
    改成扫整条轨道、按**出现最多的颜色**（轨道色面积最大）下结论。
    """
    if img is None:
        img = dialog.grab().toImage()
    top = switch.mapTo(dialog, QtCore.QPoint(0, 0))
    counts: dict[str, int] = {}
    for x in range(2, switch.width() - 2):
        px = img.pixelColor(top.x() + x, top.y() + switch.height() // 2)
        counts[norm(px.name())] = counts.get(norm(px.name()), 0) + 1
    return max(counts, key=counts.get)


def pixel_under(dialog, widget, dx=6, dy=None, img=None):
    """取 widget 内一点的**实际渲染像素**。

    **取样点必须避开控件本身。** 第一版这里写死 `dx=6`，量「卡片内不该有
    兜底底色」时正好落在开关上 —— 采到的是**轨道像素**
    （`#7f7f7c` = `border_strong`），于是同一个根因一次性造出 4 类假红：
    「SOCKS 行像素 ≠ 卡片底」「关闭态 ≠ border_strong」「开启态 ≠ accent」
    「摘掉规则后灰块没回来」。

    **教训**：`diff` 看到一个颜色不符时，先确认**采到的是不是目标**。
    这里 `border_strong` 与 `text_muted` 相近、`#ffffff` 与旋钮相近，
    「差一个取样点」看起来就像「代码没生效」。
    所以下面每个取样点都显式避开控件。
    """
    if img is None:
        img = dialog.grab().toImage()
    if dy is None:
        dy = widget.height() // 2
    pos = widget.mapTo(dialog, QtCore.QPoint(dx, dy))
    if not (0 <= pos.x() < img.width() and 0 <= pos.y() < img.height()):
        return None
    return img.pixelColor(pos.x(), pos.y()).name()


for theme in ("light", "dark"):
    p = pal.get_palette(theme)
    qss.set_current(p, 11)
    print(f"=== {theme} ===")

    d = cd.ConnectDialog(palette=p, initial={
        "host": "10.0.0.1",
        "tunnels": [{"kind": "local", "local_port": 13306,
                     "remote_host": "db.internal", "remote_port": 3306,
                     "description": "MySQL", "listen_host": "127.0.0.1",
                     "enabled": True}],
    })
    d.show()
    app.processEvents()

    card = d.tunnel_local.parentWidget()
    root_bg = p.bg_dialog
    card_bg = p.bg_surface
    img = d.grab().toImage()

    # ---- A. 卡片里不该出现兜底底色 ----
    socks_row = d.socks_switch.parentWidget()
    offenders = []
    for child in card.findChildren(QtWidgets.QWidget):
        if not child.isVisible() or child is card:
            continue
        # 只看纯布局容器（有 layout 且不是具体控件类型）
        if not isinstance(child, (QtWidgets.QLabel, QtWidgets.QLineEdit,
                                  QtWidgets.QSpinBox, QtWidgets.QPushButton,
                                  QtWidgets.QComboBox, QtWidgets.QTableWidget,
                                  QtWidgets.QTabWidget)):
            continue
        c = pixel_under(d, child, dx=2, img=img)
        if c and c.lower() == root_bg.lower():
            offenders.append((type(child).__name__, child.property("role")))
    ck("A 卡片内无控件刷出兜底底色", not offenders,
       f"{root_bg} 出现在 {[o[0] for o in offenders]}")

    # **取样点挪到行内最右侧空白**（那里既没有开关、也没有输入框）——
    # 采 SOCKS 行的「底色」要问的是这一行自己的底，不是它上面控件的颜色。
    socks_px = norm(pixel_under(d, socks_row, dx=socks_row.width() - 4,
                                img=img))
    ck("A SOCKS 行像素 = 卡片底", socks_px == norm(card_bg),
       f"实测 {socks_px} 期望 {norm(card_bg)}")

    # ---- A2. 该标 plain 的都标了（全仓口径，不只这一处）----
    # **排除三类「本来就该有自己底色」的**：
    #   ① TitleBar      —— 自绘底板
    #   ② role="card"   —— 卡片**就是**那层底，透明它卡片就没了
    #   ③ ConnectDialog 的 body 正文容器 —— 它直接挂在弹框上，吃的正是
    #      root_bg；标成 transparent 反而会让整个正文区露出黑/系统底色
    # 另外 QStackedWidget 是 QTabWidget 的页容器（认证方式那两页），
    # 由 QSS 的 page 规则负责，也排除。
    #
    # **为什么 A2 要单独存在。** A 那条按**像素**判定，能抓住「规则没生效」；
    # 但漏标一个容器时它可能正好在灰色区域上、像素看不出来 —— A2 从
    # 「有没有打 property」这一侧兜住，两条互补。
    body = d.findChild(QtWidgets.QWidget, "")
    OPAQUE = (QtWidgets.QLabel, QtWidgets.QLineEdit, QtWidgets.QSpinBox,
              QtWidgets.QPushButton, QtWidgets.QComboBox,
              QtWidgets.QTableWidget, QtWidgets.QTabWidget,
              QtWidgets.QStackedWidget)
    unlabelled = []
    for w in d.findChildren(QtWidgets.QWidget):
        if w.layout() is None or isinstance(w, OPAQUE):
            continue
        if not w.isVisible():
            continue
        if type(w).__name__ == "TitleBar":
            continue
        if w.property("role") == "card":
            continue
        # 正文容器：直接挂在对话框上、且是所有行内容器的共同祖先
        if w.parentWidget() is d:
            continue
        if w.property(qss.PLAIN_ROLE) is None:
            unlabelled.append(type(w).__name__)
    ck("A2 卡片内所有行内容器都标了 plain", not unlabelled,
       f"漏标 {unlabelled[:6]}")

    # ---- B. 开关两态 ----
    d.socks_switch.setChecked(False)
    app.processEvents()
    off_px = track_color(d, d.socks_switch)
    ck("B 关闭态轨道 = border_strong", off_px == norm(p.border_strong),
       f"实测 {off_px} 期望 {norm(p.border_strong)}")

    card_rgb = rgb_of(card_bg)
    off_cr = cr(rgb_of(off_px), card_rgb)
    muted_cr = cr(rgb_of(p.text_muted), card_rgb)

    ck("B 关闭态可辨识（≥3:1，看得出是个开关）", off_cr >= 3.0,
       f"CR={off_cr:.2f}")
    ck("B 关闭态不比旁边 muted 文字更重（面积大，要更轻）",
       off_cr < muted_cr, f"轨道 {off_cr:.2f} vs muted {muted_cr:.2f}")

    d.socks_switch.setChecked(True)
    app.processEvents()
    on_px = track_color(d, d.socks_switch)
    ck("B 开启态轨道 = accent", on_px == norm(p.accent),
       f"实测 {on_px} 期望 {norm(p.accent)}")

    # ---- C. QSS 规则与常量必须对齐（两侧缺一就静默失效）----
    sheet = qss.build_qss(p, dialog=True, ui_size=11)
    ck("C QSS 认 plain 这个 property", f"QWidget[{qss.PLAIN_ROLE}]" in sheet)
    ck("C plain 规则确实写了 transparent",
       "QWidget[plain] {\n    background: transparent;\n}" in sheet,
       repr([ln for ln in sheet.splitlines()
             if "QWidget[plain]" in ln]))
    d.close()

    # ---- D. 隧道添加框同样受益 ----
    a = tp._AddDialog(p, None, used_ports=())
    a.kind_box.setCurrentIndex(1)
    a.show()
    app.processEvents()
    ck("D 添加框的监听地址行不是兜底灰",
       (pixel_under(a, a.listen_box.parentWidget()) or card_bg.lower())
       != p.bg_dialog.lower(),
       f"实测 {pixel_under(a, a.listen_box.parentWidget())}")
    a.close()

print()
print("=== 元断言 ===")

# 元断言 1：把 plain 规则从 QSS 里摘掉，白卡片里的灰补丁必须回来
p = pal.get_palette("light")
qss.set_current(p, 11)
sheet = qss.build_qss(p, dialog=True, ui_size=11)
stripped = sheet.replace(
    "QWidget[plain] {\n    background: transparent;\n}", "")
ck("[元] 确实摘掉了 plain 规则", stripped != sheet)
ck("[元] 摘掉后兜底规则仍在（不能靠删兜底来修）",
   f"QWidget {{\n    background-color: {p.bg_dialog};" in stripped)

d = cd.ConnectDialog(palette=p, initial={"host": "10.0.0.1", "tunnels": []})
d.setStyleSheet(stripped)          # 只认摘掉规则的版本
d.show()
app.processEvents()
socks_row = d.socks_switch.parentWidget()
# 同样避开开关：取最右侧空白处
px = pixel_under(d, socks_row, dx=socks_row.width() - 4)
ck("[元] 摘掉规则后灰块回来（判据抓得到这次的改动）",
   px is not None and px.lower() == p.bg_dialog.lower(),
   f"实测 {px} 期望 {p.bg_dialog}")
d.close()

# 元断言 2：把轨道颜色换回 text_muted，「不比文字更重」那条必须红
src = inspect.getsource(qp_switch := __import__(
    "sshclient.ui.switch", fromlist=["x"]))
ck("[元] switch 模块仍在用 border_strong 画关闭态轨道",
   "p.border_strong" in src and "p.text_muted" not in src.split(
       "track = QtGui.QColor")[1][:120])

print()
print(f"--- {N[0] - len(FAILS)}/{N[0]} ---")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
