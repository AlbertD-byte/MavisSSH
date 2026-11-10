"""图标：每个名字都得画得出来，而且名字要能替按钮说话。

**为什么要专门测「语义绑定」。** 图标贴错了不会报错、不会崩，只会让用户点错。
这一版就贴错了五处，其中「清屏」用的是 `close`（一个 ✕）——而同一个工具栏旁边
就是关标签的 ✕，用户会把「清屏」读成「关窗口」。这类回归靠肉眼看 diff 是守不住
的：改名、重排、复制粘贴都会让它悄悄回来，所以断言直接钉在**按钮和图标的对应
关系**上。

第 2 类断言（画出来必须有墨迹）也是踩过的：`icon_pixmap` 对不存在的名字**返回空
pixmap 而不抛异常**，于是「拼错了一个名字」会变成「那个按钮的图标不见了」，
测试全绿。名字写错和画得空必须分开测。

Checks:
  1. 源码里引用的每个图标名都在 ICONS 表里（拼错名字 -> 空图标）
  2. 每个图标在 15 / 16 / 18 / 20 px 下都真的画出了墨迹
  3. 每个图标至少有一条路径（不是空路径）
  4. 语义绑定：清屏不是 ✕、新建目录不是光秃秃的 +、上一级不是光秃秃的 ↑
  5. 语义绑定：重新连接是插头、已取消是圆圈斜杠、折叠侧边栏是面板
  6. 菜单项都带图标（纯文字菜单项看不出是什么命令）
  7. _icon_button 只有一份实现（重复定义迟早只改一处）
  8. 图标在不同颜色下确实被染上色（漏染色就是"全灰"）
"""
import os
import pathlib
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.ui import icons as I
from sshclient.ui.design.v1.palette import get_palette

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name +
          ("" if ok else "   <- %s" % detail))


def has_ink(pm) -> bool:
    """这张 pixmap 上真的画了东西吗。

    判据用"非透明像素数"，而不是"和纯色相比变过"——后者在 1px 抗锯齿边缘上会
    漏判，而图标在 15px 下恰恰只剩边缘像素是有可能的。
    """
    img = pm.toImage()
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() > 8:
                n += 1
    return n


def source_files():
    out = []
    ui = os.path.join(ROOT, "sshclient", "ui")
    for f in os.listdir(ui):
        if f.endswith(".py"):
            out.append(os.path.join(ui, f))
    return out


def referenced_names():
    """从源码里把 icons.icon("x" / icon_pixmap("x" / icon_name 参数里用到的名字抠出来。"""
    names = set()
    pat = re.compile(r'icons\.(?:icon|icon_pixmap)\(\s*["\']([a-z0-9\-]+)["\']')
    for path in source_files():
        txt = open(path, encoding="utf-8").read()
        names.update(pat.findall(txt))
    return names


def menu_actions_have_icons():
    """菜单项是不是都带图标：addAction(icons.icon(...), "文字") 这种形式。"""
    missing = []
    txt = open(os.path.join(ROOT, "sshclient", "ui", "main_window.py"),
               encoding="utf-8").read()
    for m in re.finditer(r"addAction\(\s*([^,\n]{0,60})", txt):
        head = m.group(1)
        if "icons.icon" not in head and head.strip().startswith('"'):
            missing.append(head.strip()[:40])
    return missing


def _icon_mismatches(widget, want, palette, colors=None):
    """列出**渲染结果**与期望不符的 ``属性名 -> 期望图标`` 对。

    判据是拿按钮当前装着的 QIcon 的位图，和 ``icon_pixmap(期望名)`` 逐位比。
    尺寸取按钮**自己的** ``iconSize()``，不要写死：这些按钮有 15/16/18/20
    几种尺寸，写死一个会让正确的图标也报不匹配（这个坑踩过一次）。
    颜色同理走 ``colors`` 查表（默认 ``palette.text``）——侧边栏那排按钮染色是
    ``text_dim`` 而不是 ``text``，比错颜色会报假失败。

    **不要退回读 ``btn._icon_name``。** 上一版就是这么写的，结果恒真：
    那个属性在**构造时**写入，而 ``apply_palette`` 从来不碰它——于是"手抄名字表
    漏了 up/mkdir"那个原缺陷存在时，这个断言照样通过。评审用 monkeypatch 把
    缺陷版本装回去实测过：属性判据 PASS，而位图判据能抓到。只有"控件上现在
    真的画着什么"才是用户看到的东西，也只有它能证伪。
    """
    colors = colors or {}
    bad = []
    for attr, name in want.items():
        btn = getattr(widget, attr, None)
        if btn is None:
            bad.append("%s: 控件不存在" % attr)
            continue
        px = btn.iconSize().width()
        color = colors.get(attr, palette.text)
        got = btn.icon().pixmap(px)
        exp = I.icon_pixmap(name, color, px)
        if got.isNull() or got.size() != exp.size() or got.toImage() != exp.toImage():
            bad.append("%s(%dpx): 期望画 %s，实际画的是 %s"
                       % (attr, px, name, getattr(btn, "_icon_name", "?")))
    return bad


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    dark = get_palette("dark")

    # ------------------------------------------------------------ 1
    print("\n[1] 源码里引用的图标名都存在")
    used = referenced_names()
    missing = sorted(n for n in used if n not in I.ICONS)
    check("引用的图标名全都注册了（%d 个）" % len(used), not missing, str(missing))
    check("确实抠到了引用（防正则失灵导致这一组恒真）", len(used) >= 15,
          "只抠到 %d 个: %s" % (len(used), sorted(used)))

    # ------------------------------------------------------------ 2
    print("\n[2] 每个图标在各个尺寸下都画出了墨迹")
    for name in sorted(I.ICONS):
        sizes = [15, 16, 18, 20, 26]
        blanks = [s for s in sizes if not has_ink(I.icon_pixmap(name, "#ffffff", s))]
        check("%-13s 各尺寸都有墨迹" % name, not blanks, "空的是 %s" % blanks)

    # ------------------------------------------------------------ 3
    print("\n[3] 每个图标至少有一条非空路径")
    empties = []
    for name in sorted(I.ICONS):
        stroked, filled = I.ICONS[name]()
        s_ok = stroked is not None and not stroked.isEmpty()
        f_ok = filled is not None and not filled.isEmpty()
        if not (s_ok or f_ok):
            empties.append(name)
    check("没有空图标（空路径 = 画出来是空白）", not empties, str(empties))

    # ------------------------------------------------------------ 4
    print("\n[4] 语义绑定：这几处曾经贴错")
    mw = open(os.path.join(ROOT, "sshclient", "ui", "main_window.py"),
              encoding="utf-8").read()
    sp = open(os.path.join(ROOT, "sshclient", "ui", "sftp_panel.py"),
              encoding="utf-8").read()
    ts = open(os.path.join(ROOT, "sshclient", "ui", "transfer_strip.py"),
              encoding="utf-8").read()

    check("清屏用橡皮，不是 ✕", '_icon_button("eraser", "清屏"' in mw)
    check("清屏没有再用 close（✕ 在这儿说的是关窗口）",
          '_icon_button("close", "清屏"' not in mw)
    check("新建目录用「目录 + 加号」，不是光秃秃的 +",
          '_icon_button("folder-plus", "新建目录"' in sp)
    check("上一级用「目录 + 上箭头」，不是光秃秃的 ↑",
          '_icon_button("folder-up", "上一级"' in sp)
    check("四个语义图标都注册了",
          all(n in I.ICONS for n in ("eraser", "folder-plus", "folder-up", "ban")),
          str([n for n in ("eraser", "folder-plus", "folder-up", "ban")
               if n not in I.ICONS]))

    # 橡皮和 ✕ 不能画成同一个东西——换了名字但没换形状等于没换
    a = I.icon_pixmap("eraser", "#ffffff", 18).toImage()
    b = I.icon_pixmap("close", "#ffffff", 18).toImage()
    diff = sum(1 for y in range(18) for x in range(18)
               if a.pixel(x, y) != b.pixel(x, y))
    check("eraser 和 close 画出来确实不同（换了名也要换了形）", diff > 30, "差 %d 像素" % diff)
    for pair in (("folder-plus", "plus"), ("folder-up", "up"),
                 ("ban", "close"), ("sidebar", "list")):
        ia = I.icon_pixmap(pair[0], "#ffffff", 18).toImage()
        ib = I.icon_pixmap(pair[1], "#ffffff", 18).toImage()
        d = sum(1 for y in range(18) for x in range(18)
                if ia.pixel(x, y) != ib.pixel(x, y))
        check("%s 和 %s 画出来确实不同" % pair, d > 30, "只差 %d 像素" % d)

    # ------------------------------------------------------------ 5
    print("\n[5] 语义绑定：重新连接 / 已取消 / 折叠侧边栏")
    check("重新连接用插头，不是环形箭头（环形箭头说的是刷新）",
          '_icon_button("plug", "重新连接"' in mw)
    check("已取消用圆圈斜杠，不是 ✕", 'name = "ban"' in ts)
    # 负向源码守卫：换过主题之后不许再出现手抄的图标名表。
    # 断言成立不代表按钮画对了（那要靠 5b 的位图判据），它的作用是**排除**
    # "构造对、换主题回退"这种只有位图能抓到的回归重新被引进来。
    stale = [("main_window", mw), ("sftp_panel", sp)]
    residue = {n: t.count("setIcon(icons.icon(") for n, t in stale}
    check("换主题的路径里不再手抄图标名表",
          all(v == 0 for v in residue.values()), str(residue))
    check("成功态仍然是 ✓（别把 check 也一起换了）", 'name = "check"' in ts)

    # ------------------------------------------------------------ 5b
    print("\n[5b] 切主题不能把图标打回旧名（这里真踩过）")
    # sftp_panel 的 apply_palette 里原先手抄了一份图标名表，漏了 mkdir/up 两处。
    # 于是按 Ctrl+T 切主题，「上一级」「新建目录」会悄悄变回旧图标——
    # 构造函数的断言照样全绿，因为构造那段是对的。
    # 所以这里断言**按钮上实际装着的图标名**，并且真的换一次调色板。
    from sshclient.ui.sftp_panel import SftpPanel

    class _FakeSftp:
        def getcwd(self):
            return "/"

        def listdir(self, path):
            return []

        def stat(self, path):
            raise OSError(path)

    pal_a = get_palette("dark")
    pal_b = get_palette("light")
    p5 = SftpPanel(_FakeSftp(), pal_a, None)
    # 注意 SftpPanel.up_btn 是中间那个「上传」大按钮；路径栏那排
    # （上一级/刷新/新建目录/删除）在 FilePanel 上，也就是 p5.local / p5.remote。
    want5 = {
        "up_btn": "folder-up",
        "refresh_btn": "refresh",
        "mkdir_btn": "folder-plus",
        "delete_btn": "trash",
    }
    check("路径栏按钮构造时画的就是对的图标",
          _icon_mismatches(p5.local, want5, pal_a) == [],
          str(_icon_mismatches(p5.local, want5, pal_a)))
    check("左右两栏一致",
          _icon_mismatches(p5.remote, want5, pal_a) == [],
          str(_icon_mismatches(p5.remote, want5, pal_a)))
    p5.apply_palette(pal_b)
    check("换过调色板之后画的对不对（这才是原来漏掉的那一步）",
          _icon_mismatches(p5.local, want5, pal_b) == []
          and _icon_mismatches(p5.remote, want5, pal_b) == [],
          str(_icon_mismatches(p5.local, want5, pal_b)
              + _icon_mismatches(p5.remote, want5, pal_b)))
    p5.apply_palette(pal_a)
    check("来回换两次也对", _icon_mismatches(p5.local, want5, pal_a) == [],
          str(_icon_mismatches(p5.local, want5, pal_a)))

    # 负向对照：把「上一级」按钮的图标换成别的，这一组必须红。
    # 不做这一步，上面那些断言有可能因为判据写错而恒真。
    I.icon_pixmap("folder-up", pal_a.text, 18)   # 确认这个图标本身画得出来
    p5.local.up_btn.setIcon(I.icon("close", pal_a, 18))
    check("负向对照：故意换成别的图标后必须被抓到",
          _icon_mismatches(p5.local, want5, pal_a) != [],
          "换成 close 居然没被抓到，说明判据恒真")
    p5.local.up_btn.setIcon(I.icon("folder-up", pal_a, 18))
    check("换回来之后不再报", _icon_mismatches(p5.local, want5, pal_a) == [],
          str(_icon_mismatches(p5.local, want5, pal_a)))

    # 上传/下载那两个大按钮（15px）同样过一遍。
    # 注意 up_btn 染的是 text_on_solid、down_btn 染的是 text，两个颜色不一样；
    # 而且要比**当前调色板**下的渲染结果——拿另一套调色板去比，两边都会误报。
    #
    # **`text_on_accent` → `text_on_solid`**：主按钮底色换成了实心档
    # `accent_solid`，压在它上面的字/图标必须一起换成白字。这里跟着改，
    # 不是「判据过时」—— 它量的正是「图标颜色跟不跟得上底色」。
    big = {"up_btn": "upload", "down_btn": "download"}
    big_col_a = {"up_btn": pal_a.text_on_solid, "down_btn": pal_a.text}
    big_col_b = {"up_btn": pal_b.text_on_solid, "down_btn": pal_b.text}
    check("传输大按钮当前调色板下画的对不对",
          _icon_mismatches(p5, big, pal_a, big_col_a) == [],
          str(_icon_mismatches(p5, big, pal_a, big_col_a)))
    p5.apply_palette(pal_b)
    check("传输大按钮换主题后画的对不对",
          _icon_mismatches(p5, big, pal_b, big_col_b) == [],
          str(_icon_mismatches(p5, big, pal_b, big_col_b)))
    p5.apply_palette(pal_a)
    check("传输大按钮换回来也对",
          _icon_mismatches(p5, big, pal_a, big_col_a) == [],
          str(_icon_mismatches(p5, big, pal_a, big_col_a)))

    # SessionTab 的工具栏同理
    from sshclient.ui.main_window import SessionTab

    tab5 = SessionTab()
    want_tab = {
        "reconnect_btn": "plug", "disconnect_btn": "power",
        "upload_btn": "upload", "download_btn": "download",
        "copy_btn": "copy", "paste_btn": "paste", "clear_btn": "eraser",
    }
    check("工具栏画的对不对", _icon_mismatches(tab5, want_tab, pal_a) == [],
          str(_icon_mismatches(tab5, want_tab, pal_a)))
    tab5.apply_palette(pal_b)
    check("工具栏换主题后画的对不对", _icon_mismatches(tab5, want_tab, pal_b) == [],
          str(_icon_mismatches(tab5, want_tab, pal_b)))

    # 折叠侧边栏那个按钮在 MainWindow 上（SessionTab 拿不到）。
    # 这里同样走位图判据：源码里那句 _set_icon(..., "sidebar", ...) 抄对了
    # 不代表按钮上装的是它——上一版的恒真就是这么来的。
    from sshclient.core import settings as _settings
    from sshclient.ui import main_window as _mw_mod

    st5 = _settings.AppSettings()
    st5.save = lambda: None            # 绝不碰真实的 %APPDATA% 配置
    win5 = _mw_mod.MainWindow(
        st5, _settings.SessionStore(pathlib.Path(tempfile.mkdtemp()) / "s.json"))
    win5.apply_theme("dark")
    fold = {"_fold_btn": "sidebar"}
    fold_dim = {"_fold_btn": pal_a.text_dim}
    check("折叠侧边栏按钮画的是面板图标，不是一串圆点",
          _icon_mismatches(win5, fold, pal_a, fold_dim) == [],
          str(_icon_mismatches(win5, fold, pal_a, fold_dim)))
    win5.apply_theme("light")
    check("换主题之后折叠按钮还是面板图标",
          _icon_mismatches(win5, fold, pal_b, {"_fold_btn": pal_b.text_dim}) == [],
          str(_icon_mismatches(win5, fold, pal_b, {"_fold_btn": pal_b.text_dim})))
    # 负向对照：换成 list（旧的错图标）必须被抓到，否则上面两条是恒真
    win5._fold_btn.setIcon(I.icon("list", pal_b, 18, pal_b.text_dim))
    check("负向对照：折叠按钮换成 list 之后必须被抓到",
          _icon_mismatches(win5, fold, pal_b, {"_fold_btn": pal_b.text_dim}) != [],
          "换成 list 居然没被抓到，说明判据恒真")
    win5.apply_theme("dark")
    check("换回深色之后恢复正确",
          _icon_mismatches(win5, fold, pal_a, fold_dim) == [],
          str(_icon_mismatches(win5, fold, pal_a, fold_dim)))
    # 顺带把侧边栏另外三个按钮也过一遍（它们同样走过 _retheme_icon_button）
    side_want = {"_new_btn": "plus", "_side_edit_btn": "pencil",
                 "_side_del_btn": "trash"}
    side_col = {"_new_btn": pal_a.text_on_solid, "_side_edit_btn": pal_a.text,
                "_side_del_btn": pal_a.text}
    check("侧边栏其余三个按钮画的对不对",
          _icon_mismatches(win5, side_want, pal_a, side_col) == [],
          str(_icon_mismatches(win5, side_want, pal_a, side_col)))
    win5.apply_theme("light")
    side_col_b = {"_new_btn": pal_b.text_on_solid, "_side_edit_btn": pal_b.text,
                  "_side_del_btn": pal_b.text}
    check("侧边栏按钮换主题后画的对不对",
          _icon_mismatches(win5, side_want, pal_b, side_col_b) == [],
          str(_icon_mismatches(win5, side_want, pal_b, side_col_b)))
    win5.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)

    # ------------------------------------------------------------ 6
    print("\n[6] 菜单项都带图标")
    missing = menu_actions_have_icons()
    check("没有纯文字的菜单项", not missing, str(missing))
    for name in ("gear", "info", "power", "sun", "sidebar", "plug"):
        check("菜单用到的 %s 已注册" % name, name in I.ICONS)

    # ------------------------------------------------------------ 7
    print("\n[7] _icon_button 只有一份实现")
    defs = 0
    for path in source_files():
        txt = open(path, encoding="utf-8").read()
        defs += len(re.findall(r"^def _icon_button\(", txt, re.MULTILINE))
    check("全项目只有一处 _icon_button 定义", defs == 1, "找到 %d 处" % defs)
    check("sftp_panel 用的是 controls 那份",
          "from .controls import _icon_button" in sp)

    # ------------------------------------------------------------ 8
    print("\n[8] 染色确实生效（漏染色就是一片灰）")
    # 判据用**绿色通道**。踩过：用 red 通道判红色是不行的——QImage.pixelColor
    # 返回未预乘的颜色，纯红图标里每个不透明像素的 red 都是 255，
    # "min(red) < 60" 于是永远不成立，一条好图标被报成失败。
    # 绿色才是判别式：白 -> 255，红 -> 0。
    for name in ("eraser", "ban", "folder-plus", "gear", "sidebar", "info"):
        wi = I.icon_pixmap(name, "#ffffff", 18).toImage()
        ri = I.icon_pixmap(name, "#ff0000", 18).toImage()
        wg = [wi.pixelColor(x, y).green()
              for y in range(18) for x in range(18)
              if wi.pixelColor(x, y).alpha() > 200]
        rg = [ri.pixelColor(x, y).green()
              for y in range(18) for x in range(18)
              if ri.pixelColor(x, y).alpha() > 200]
        check("%-12s 白色渲染是白的" % name, bool(wg) and min(wg) > 200,
              "green 样本 %s" % (sorted(set(wg))[:4] or "没有不透明像素"))
        check("%-12s 红色渲染是红的" % name, bool(rg) and max(rg) < 40,
              "green 样本 %s" % (sorted(set(rg))[:4] or "没有不透明像素"))

    # ------------------------------------------------------------ 9
    print("\n[9] 每个图标和「最像它的那几个」画出来确实不同")
    # 评审实测：把 ICONS["snippet"] 整个换成 _list 之后，_icons.py 的 97 项
    # **全绿** —— 原来只比对了"引用到的名字"和"画得出墨迹"，没有任何一处
    # 约束图标**长什么样**。于是有人把一个图标悄悄换成另一个，这个测试
    # 完全看不见。补上两两像素差。
    #
    # 用像素差而不是"名字不同"：名字可以随便改，像素改不了。
    THRESH = 30

    def pixel_diff(a: str, b: str, size: int = 18) -> int:
        ia = I.icon_pixmap(a, "#ffffff", size).toImage()
        ib = I.icon_pixmap(b, "#ffffff", size).toImage()
        return sum(1 for y in range(size) for x in range(size)
                   if ia.pixel(x, y) != ib.pixel(x, y))

    # 语义上最容易被搞混的几组
    near = [("snippet", "list"), ("snippet", "chevron-right"),
            ("snippet", "terminal"), ("eraser", "close"),
            ("folder-plus", "plus"), ("folder-up", "up"),
            ("ban", "close"), ("sidebar", "list"),
            ("upload", "download"), ("plug", "refresh"),
            ("copy", "paste"), ("plus", "dots")]
    for a, b in near:
        d = pixel_diff(a, b)
        check("%-11s 和 %-13s 画出来确实不同" % (a, b), d > THRESH,
              "只差 %d 像素" % d)

    # 全表两两比对：任何一个图标被换成别的，得立刻抓到。
    #
    # 阈值比上面那组低得多（8 vs 30），而且这是**刻意的**：上面那组是
    # "语义上容易被搞混"的近邻，必须一眼能分辨；而全表里本来就有合法的
    # 近亲 —— `folder` 和 `folder-up` 只差一个箭头，18px 下实测算 24 像素
    # 差，拿 30 去卡它就成了假失败。这一组要抓的是"**整个图标被换掉**"
    # （评审实测把 ICONS["snippet"] 换成 _list 之后 97 项全绿），
    # 那种替换的像素差是几百，不存在贴边。
    SAME = 8
    names = sorted(I.ICONS)
    collide = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if pixel_diff(a, b, 18) <= SAME:
                collide.append((a, b))
    check("没有任何两个图标画出来是一样的（%d 个两两比对）"
          % (len(names) * (len(names) - 1) // 2), not collide,
          str(collide[:6]))
    check("对照组：folder 与 folder-up 确实是不同的图（不是判据瞎了）",
          pixel_diff("folder", "folder-up") > SAME,
          "只差 %d 像素" % pixel_diff("folder", "folder-up"))

    # 负向对照：真的把一个图标换成另一个，上面那条必须变红
    saved = I.ICONS["snippet"]
    try:
        I.ICONS["snippet"] = I.ICONS["list"]
        check("负向对照：snippet 换成 list 之后像素差必须塌到阈值以下",
              pixel_diff("snippet", "list") <= SAME,
              "换了图标像素差仍是 %d，判据没生效" % pixel_diff("snippet", "list"))
    finally:
        I.ICONS["snippet"] = saved
    check("换回来之后又不一样了（证明上面不是恒真）",
          pixel_diff("snippet", "list") > THRESH,
          "只差 %d 像素" % pixel_diff("snippet", "list"))

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
