"""强调色可配置：推导、接线、守卫。

评审反复指出的一条：**默认路径上的任何断言都是恒真的**——现有那 30 多套
像素测试符号化地读 `p.bg_base` 之类，把 accent 强改成 `#ff2fd0` 它们照样全过，
所以推导写错了没有任何东西会红。这个文件就是补那个洞的。

因此这里的判据分两类，缺一不可：

- **默认路径**：钉字面量（``== "#4c8dff"``，不是从函数读出来再和自己比）
  外加 ``get_palette("dark") is DARK`` 的身份守卫
- **派生路径**：钉**具体的推导结果字面量**。只钉默认路径的话，
  把推导换成什么错的东西它都不红——评审实测过「保 H 保 S 只平移 L」和
  真正的 hover 公式逐位相同，压根不变红。

两条元断言在最后：把缺陷装回去，判据必须变红。
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fonts import boot

app = boot([])

from PyQt6 import QtCore, QtGui, sip
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QMessageBox, QVBoxLayout, QWidget

from sshclient.core import settings as S
from sshclient.ui import main_window as MW
from sshclient.ui.design.v1 import palette as P
from sshclient.ui.design.v1.palette import DARK, LIGHT, THEMES, get_palette

PASS, FAIL = [], []
PROBE = "#ff2fd0"          # 洋红：和默认蓝差得足够远，认错了一定看得出来
_DEFAULT_DARK_ACCENT = DARK.accent


def _hue(hex_color: str) -> float:
    """色相 0~360。用它判「这个蓝到底是用户挑的蓝，还是退回默认蓝了」。"""
    return P._hsl(hex_color)[0]


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail))


_TMP = pathlib.Path(tempfile.mkdtemp(prefix="accent_"))


def _st(**kw):
    """新设置对象，**落盘位置指到临时目录**。

    AppSettings.save() 以前无条件写模块级的 SETTINGS_FILE，也就是
    用户真实的 %APPDATA%\\MavisSSH\\settings.json。所以"用
    AppSettings() 造一个干净的" 并**不能**避免写用户配置——
    干净的是内容，不是落盘位置。
    """
    s = S.AppSettings(**kw)
    s._path = _TMP / "settings.json"
    return s


def dominant(widget, y: int) -> str:
    """某一整行里出现最多的颜色。**不取单点**——单点会随窗口宽度落到不同
    的东西上（取样点压到标题文字上就变了）。"""
    img = widget.grab().toImage()
    counts: dict[str, int] = {}
    for x in range(widget.width()):
        c = img.pixelColor(x, y).name()
        counts[c] = counts.get(c, 0) + 1
    return max(counts, key=counts.get) if counts else ""


def near_count(widget, color: str, tol: int = 26) -> int:
    """控件内与 *color* 相距在 *tol* 以内的像素数。"""
    img = widget.grab().toImage()
    tgt = QtGui.QColor(color)
    n = 0
    for y in range(widget.height()):
        for x in range(widget.width()):
            c = img.pixelColor(x, y)
            if (abs(c.red() - tgt.red()) <= tol and abs(c.green() - tgt.green()) <= tol
                    and abs(c.blue() - tgt.blue()) <= tol):
                n += 1
    return n


# ---------------------------------------------------------------- 1 硬性约束

print("\n[1] 硬性约束：DARK / LIGHT 的字面量一个字都不能被改写")

src = pathlib.Path(ROOT, "sshclient", "ui", "design", "v1", "palette.py").read_text(encoding="utf-8")
tree = ast.parse(src)


def _assigned_literals(node) -> dict:
    """取一个赋值语句里所有 ``key=<字面量字符串>``。

    **别只认 ``ast.Assign``。** 原来只处理 Assign、且构造器名限定 (Palette,
    replace)，于是 ``DARK = _mk_palette()`` 和 ``DARK: Palette = Palette(...)``
    （AnnAssign）两种改法全都绕过守卫、判据照样全绿——节点一消失，收集到的
    字典变空，``not bad`` 就成了恒真。现在不认语句类型，只问**值是不是字面量**。
    """
    out = {}
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        value = node.value
        for kw in getattr(value, "keywords", ()):
            if kw.arg and isinstance(kw.value, ast.Constant) \
                    and isinstance(kw.value.value, str):
                out[kw.arg] = kw.value.value
    return out


names = {"DARK", "LIGHT", "THEMES"}
literals, seen = {}, set()
for node in ast.walk(tree):
    tgt = None
    if isinstance(node, ast.Assign):
        tgt = node.targets[0] if node.targets else None
    elif isinstance(node, ast.AnnAssign):
        tgt = node.target
    if isinstance(tgt, ast.Name) and tgt.id in names:
        seen.add(tgt.id)
        literals.update(_assigned_literals(node))

check("找得到 DARK / LIGHT / THEMES 的赋值节点（守卫自己没瞎）",
      names <= seen, "只找到 %s" % sorted(seen))
# 判据名原来写「一共 27 个字段」而断言是 `>= 27` —— 那是**下界**，所以
# 往后加字段（例如这次给侧栏单开的 `bg_rail`）它照样绿，可名字已经和现实
# 不符了。改成**从 dataclass 动态取字段数**：这样「多一个字段」既不会被
# 误报成红，名字也不会再烂掉。
_n_fields = len([f for f in P.Palette.__dataclass_fields__])
check("DARK / LIGHT 的字面量字段数与 Palette 的字段数对得上（%d 个）"
      % _n_fields, len(literals) >= _n_fields, "只收集到 %d 个" % len(literals))

offenders = []
for node in ast.walk(tree):
    tgt = None
    if isinstance(node, ast.Assign):
        tgt = node.targets[0] if node.targets else None
    elif isinstance(node, ast.AnnAssign):
        tgt = node.target
    if not (isinstance(tgt, ast.Name) and tgt.id in names):
        continue
    value = node.value
    for kw in getattr(value, "keywords", ()):
        if not kw.arg:
            continue
        if not (isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)):
            offenders.append("%s.%s = %s" % (tgt.id, kw.arg,
                                             type(kw.value).__name__))
check("这些字段的值全是字符串字面量（没有一个被换成函数调用）",
      not offenders, "；".join(offenders[:4]))

check("get_palette(\"dark\") 拿到的就是 DARK 那个对象（不是副本）",
      get_palette("dark") is DARK)
check("get_palette(\"light\") 拿到的就是 LIGHT 那个对象",
      get_palette("light") is LIGHT)
check("未知主题回退 DARK", get_palette("nope") is DARK)
check("accent 为空时也回退字面量", get_palette("dark", "") is DARK)

# ---------------------------------------------------------------- 2 默认值

print("\n[2] 默认路径的字面量（钉死的不是从函数读出来的）")

# **accent 族 2026-10-10 现代化简约：降饱和**（hue 与 L 都不动，只收艳度）。
# 深色原来是 S=1.000 满饱和，在近黑底上会「发光」，是整副配色里「不丝滑」
# 最直接的来源；降到 0.88 之后 `text_on_accent` 对它的对比 5.99 → **6.12
# （反而更好）**。浅色 0.778 → 0.750。实心档是**重新求解**的，不是手工降 ——
# 手工降过一次得 `#2770f1`，白字只剩 4.493 破 AA（见 palette.py 注释）。
check("深色 accent == #5790f4", DARK.accent == "#5790f4", DARK.accent)
check("深色 hover == #74a3f6", DARK.accent_hover == "#74a3f6", DARK.accent_hover)
check("深色 press == #3b7ae8", DARK.accent_press == "#3b7ae8", DARK.accent_press)
# **深色 soft 从 `#1a2740` 改成 `#25292f`（2026-10-10 配色评估）。**
# 两个理由，同一个 token：`palette.py:375-377` 自定的 `_SOFT_TARGET_CR=1.12`
# 明写意图是「任何色相都不会隐形」，而旧值只有 1.1048 —— 违约（浅色同项
# 1.1617 达标，只有深色违约）；同时深色原本是蓝染（hue 219 / sat 0.42），
# 而浅色照参考图是纯中性灰，切主题时选中项会「从灰变蓝」。
check("深色 soft == #25292f", DARK.accent_soft == "#25292f", DARK.accent_soft)
# **浅色这四条的字面量随参考图改了**（`palette.py` 的 LIGHT 段有完整推导）。
# 保留字面量守卫仍有价值 —— 它钉的是「默认值是**字面量**、不是从函数读出来的」
# 这条结构性质（见上面 116~138 行那一组）；把值同步过来即可，**不改成量性质**：
# 那几条性质判据在下面第 2/2b 节里已经独立存在，重复一遍只会让人误以为
# 这里在量对比度。
check("浅色 accent == #328ee2", LIGHT.accent == "#328ee2", LIGHT.accent)
check("浅色 hover == #3e95e3", LIGHT.accent_hover == "#3e95e3",
      LIGHT.accent_hover)
check("浅色 press == #4f9ee6", LIGHT.accent_press == "#4f9ee6",
      LIGHT.accent_press)
check("浅色 soft == #E2E8F2", LIGHT.accent_soft == "#E2E8F2", LIGHT.accent_soft)
# **浅色 `text_on_accent` 从白字改成近黑**：参考图的强调蓝偏亮，白字压上去只有
# **3.43**，破 AA —— 深色主题早就用近黑字解过同一道题，这里跟着走。
# 所以原来那条「浅色是白字」的字面量守卫**已经不成立**，改成钉住真正的理由：
# 「浅色也不能是白字」（不是因为旧值，是因为它在新蓝上不达标）。
check("浅色 text_on_accent **不是**白字（白在参考蓝上只有 3.43，破 AA）",
      LIGHT.text_on_accent != "#ffffff", LIGHT.text_on_accent)
check("深色 text_on_accent 不是白字（白在亮蓝上只有 3.20，破 AA）",
      DARK.text_on_accent != "#ffffff", DARK.text_on_accent)
for _thm, _pal in (("深", DARK), ("浅", LIGHT)):
    # **`danger` 这一档删掉了。** 它守的是 `text_on_accent` 压在 `danger` 上，
    # 而那条路要靠 `[variant="danger"]` 的按钮存在 —— 该 variant 连同它的
    # 常驻规则**已经在前面某一次改动里删掉了，全仓零生产者**（第 299 行那段
    # 记着同一件事：`sideAccent` 也是这么没的）。用户根本看不到那些像素。
    # 换浅色 `text_on_accent` 之后这一档会红（3.39），但那是**一个不存在的
    # 消费者**，不是缺陷 —— 留着它等于给一个「改了也没人看」的组合上锁。
    for _state, _bg in (("常态", "accent"), ("悬停", "accent_hover"),
                        ("按下", "accent_press")):
        _cr = P._contrast(_pal.text_on_accent, getattr(_pal, _bg))
        check("%s色 text_on_accent 在 accent %s 上 >= 4.5" % (_thm, _state),
              _cr >= 4.5, "%.2f" % _cr)

# ---------------------------------------------------------------- 2b 悬停态
#
# 这一段是补一个**实测过的空白**：改动面专家逐套跑完全量回归发现
# 「34/34 全绿」是**无覆盖**——全仓没有任何一条断言读按钮悬停的底色。
# 浅色下 `bg_hover` 和 `bg_elevated` 曾经是同一个 `#eef1f5`（对比度 1.000，
# 悬停零反馈），而这条缺陷能一路绿灯通过 34 套测试。下面把「悬停必须看得见」
# 和「悬停底上的文字必须可读」钉成契约。

print("\n[2b] 悬停态：看得见 + 底上的字读得清")

# **门槛从 `palette._HOVER_MIN_DELTA` 来，不在这里手抄一个数。**
# 手抄的门槛会和模块里的定义漂移 —— 那个漂移已经发生过一次：
# 这里原来写的是「≥ 1.12」，而 1.12 是 `palette._SOFT_TARGET_CR`，
# 那是**持久选中胶囊 `accent_soft`** 的目标，不是悬停底的。
#
# 悬停是**瞬时态**：指针一移开就没了。它真正的主反馈通道是悬停时显形的
# 1px `border_strong`（对 `bg_elevated` 实测 4.158:1，远超 WCAG 1.4.11
# 要的 3:1），填色差只是次要修饰。用持久态的 1.12 去卡瞬时态，
# 而且**卡的是浅色**（WCAG 相对亮度比在白端被压缩：浅色 ΔE 17.92、
# 深色 17.97 几乎相同，CR 却差 0.06）。
#
# 所以现在改用仓库自己的 `_delta`（RGB RMS 色距）与模块常量。
HOVER_MIN_DELTA = P._HOVER_MIN_DELTA

for theme in ("light", "dark"):
    p = THEMES[theme]

    # 契约 0：悬停底**不能等于**静置底 —— 那是那处 1.000 的真缺陷。
    check("%s 按钮悬停底 != 按钮静置底" % theme,
          p.bg_hover_ctrl != p.bg_elevated,
          "两者都是 %s" % p.bg_hover_ctrl)

    # 契约 1：悬停底离静置底要**有可感知的色距**（瞬时态的量级）。
    _d = P._delta(p.bg_hover_ctrl, p.bg_elevated)
    check("%s 按钮悬停离静置底 ΔE >= %.1f（不能等于，也不能弱到看不出）"
          % (theme, HOVER_MIN_DELTA),
          _d >= HOVER_MIN_DELTA, "实际 ΔE %.2f" % _d)

    # 契约 1b：行悬停底同理（它压在列表底上）。
    _dr = P._delta(p.bg_hover_row, p.bg_surface)
    check("%s 行悬停离列表底 ΔE >= %.1f" % (theme, HOVER_MIN_DELTA),
          _dr >= HOVER_MIN_DELTA, "实际 ΔE %.2f" % _dr)

    # 契约 2：按钮上印 13px text，悬停后仍要达 AA。
    check("%s 按钮悬停底上的 text >= 4.5" % theme,
          P._contrast(p.text, p.bg_hover_ctrl) >= 4.5,
          "实际 %.2f" % P._contrast(p.text, p.bg_hover_ctrl))

    # 契约 3：行悬停底上压着 11px 的主机地址（text_muted），必须 >= 4.5。
    # 这一条**同时**是行悬停值不能随便加深的理由：越深这行越小。
    m = P._contrast(p.text_muted, p.bg_hover_row)
    check("%s 行悬停底上的 text_muted(11px) >= 4.5" % theme, m >= 4.5,
          "实际 %.2f" % m)

    # 契约 4：行悬停不能压过选中行 —— 瞬时态不许盖过持久态。
    base = p.bg_surface
    check("%s 行悬停不比选中行更响（层级不倒挂）" % theme,
          P._contrast(p.bg_hover_row, base) < P._contrast(p.accent_soft, base),
          "悬停 %.3f / 选中 %.3f" % (P._contrast(p.bg_hover_row, base),
                                     P._contrast(p.accent_soft, base)))

# 契约 5：选中行上的 11px 主机地址。text_muted 在 accent_soft 上本来就
# 不达标（4.29 / 4.33），所以 SessionItem.set_selected 在选中期间把它
# 换成 text_dim。这一条钉的是**那个换色必须够用**，不是钉换色动作本身
# （动作是 UI 代码，由 _uibtns / _sideitem 那一侧覆盖）。
for theme in ("light", "dark"):
    p = THEMES[theme]
    check("%s 选中行底上的 text_dim(11px) >= 4.5（选中期间用它替 text_muted）"
          % theme, P._contrast(p.text_dim, p.accent_soft) >= 4.5,
          "实际 %.2f" % P._contrast(p.text_dim, p.accent_soft))
    # **这里原来钉的是「text_muted 在选中行上本来就不达标」——那是一条
    #   前提，不是性质。**
    #
    #   前提是「text_muted 压在 accent_soft 上破 AA，所以选中期间必须换成
    #   text_dim」。后来配色调过，**light 的 text_muted 在 accent_soft 上
    #   已经到 4.89（达标了）**，那条前提在 light 下就不成立了 ——
    #   而判据还在要求它不达标，于是红了。产品没坏，是前提没了。
    #
    # 现在钉**真正的性质**：**选中行上那个 11px 端点必须是可读的** ——
    # 不管它最后用的是 text_muted 还是 text_dim。
    #
    #   light: text_muted 4.89（本来就够，保留 text_dim 也不亏）
    #   dark:  text_muted 4.48（不够，靠 text_dim 6.68 兜住）
    #
    # 「换色」这个**动作**仍由 UI 侧覆盖（`sidebar.SessionItem.set_selected`），
    # 这里守的是**结果**：用户看到的那个端点在任何主题下都必须达 AA。
    _muted = P._contrast(p.text_muted, p.accent_soft)
    _dim = P._contrast(p.text_dim, p.accent_soft)
    check("%s 选中行上的 11px 端点可读（text_muted 或 text_dim 至少一个 >= 4.5）"
          % theme, max(_muted, _dim) >= 4.5,
          "text_muted %.2f / text_dim %.2f" % (_muted, _dim))

# 契约 6：QSS 真的用上了新 token，而且旧的 bg_hover 没人再引用。
from sshclient.ui.design.v1 import qss as _qss

# 看**源码**而不是生成结果：生成后的样式表里没有 token 名可比，只有填进去
# 的色值——而色值相同是允许的（浅色 bg_hover_row 就和别的色撞）。
_qss_source = pathlib.Path(ROOT, "sshclient", "ui", "design", "v1", "qss.py").read_text(encoding="utf-8")
for theme in ("light", "dark"):
    sheet = _qss.build_qss(THEMES[theme])
    check("%s 样式表用 bg_hover_ctrl（按钮）" % theme,
          "background-color: %s;" % THEMES[theme].bg_hover_ctrl in sheet)
    check("%s 样式表用 bg_hover_row（行）" % theme,
          sheet.count("background-color: %s;"
                      % THEMES[theme].bg_hover_row) >= 2,
          "出现 %d 次" % sheet.count("background-color: %s;"
                                    % THEMES[theme].bg_hover_row))
    check("%s 样式表里没有裸的 bg_hover（拆干净了）" % theme,
          "{p.bg_hover}" not in _qss_source)
    check("%s Palette 上已经没有叫 bg_hover 的字段了" % theme,
          not hasattr(THEMES[theme], "bg_hover"))

# 契约 7 整条撤掉了：它量的是 `bg_scrim` 的 8 位十六进制按 Qt 的 #AARRGGBB
# 解析（写的人以为是 CSS 的 #RRGGBBAA），而那个 token **零消费者**，已从
# `Palette` 删除（原因写在 palette.py 的字段注释里）。原来这条判据守着一个
# 谁也不看的字段：它量出的 alpha>0 与否，全仓没有任何像素依赖。
# #AARRGGBB / #RRGGBBAA 这个坑本身还在 —— 下次真加遮罩时照旧要在测试里
# 钉一次「alpha 不是 0」，但那时才有得量。
from PyQt6.QtGui import QColor  # noqa: F401

# 元断言：把 bg_hover_ctrl 装回那个有缺陷的值（悬停 == 静置），上面那几条
# 必须真的变红。抓不到就说明这几条是恒真的。
#
# **这里原来断言的是 `contrast < 1.12`。** 那条**已经失效了**：契约改成
# ΔE 之后，`contrast` 不再是判据的一部分，而浅色现值的 contrast 恰好
# 就是 **1.0959 < 1.12** —— 于是这个谓词**对正常配色也成立**，
# 等于恒真，元断言什么也证明不了。
#
# 现在改成验**当前契约真正用的那个量**：把悬停底换成静置底，
# ΔE 必然掉到 0，远低于门槛 `_HOVER_MIN_DELTA`。
_gap_before = P._delta(THEMES["light"].bg_hover_ctrl,
                       THEMES["light"].bg_elevated)
_broken = P.replace(THEMES["light"], bg_hover_ctrl=THEMES["light"].bg_elevated)
check("元断言：装回缺陷值（悬停==静置）后判据会红",
      (P._delta(_broken.bg_hover_ctrl, _broken.bg_elevated)
       < P._HOVER_MIN_DELTA
       and _broken.bg_hover_ctrl == _broken.bg_elevated),
      "原值 ΔE=%.2f" % _gap_before)
# 反向：现值必须**满足**门槛 —— 否则上面那条可能是恒红的假对照。
check("元断言：现值的悬停 ΔE 确实达标（对照不是恒红的）",
      _gap_before >= P._HOVER_MIN_DELTA,
      "现值 ΔE=%.2f 门槛=%.1f" % (_gap_before, P._HOVER_MIN_DELTA))

# 契约 8：传输条的**轨道**必须和它压着的那层底色分得开。
#
# 这条是实测出来的坑：`SlimBar.apply_palette` 原来写的是
# `p.bg_base if p.name == "dark" else p.bg_elevated`，而传输条底下压的
# 恰恰就是 `bg_base`（SftpPanel 是普通 QWidget，命中 qss.py 那条兜底
# `QWidget { background-color: root_bg }`）—— 深色下轨道和底色**完全同色**，
# 对比度 1.000，轨道根本没画出来，界面上只剩一条悬空的填充色。
# 而按主题名分叉还意味着第三套主题会静默掉回深色那一支。
#
# 判据钉在**消费者**上而不是 token 上：钉 token 的话，只要没人用就永远绿。
_app = QApplication.instance() or QApplication(sys.argv)
from sshclient.ui.transfer_strip import SlimBar

for theme in ("light", "dark"):
    _pal = THEMES[theme]
    _bar = SlimBar()
    _bar.apply_palette(_pal)
    # 传输条坐在 SftpPanel 里，那一层是 bg_base。
    check("%s 传输条轨道和它压着的底色分得开" % theme,
          _bar._track != _pal.bg_base
          and P._contrast(_bar._track, _pal.bg_base) > 1.0,
          "轨道 %s vs 底 %s = %.3f"
          % (_bar._track, _pal.bg_base, P._contrast(_bar._track, _pal.bg_base)))
    check("%s 传输条轨道和填充色分得开" % theme,
          P._contrast(_bar._track, _pal.accent) >= 1.5,
          "%.3f" % P._contrast(_bar._track, _pal.accent))
    check("%s 传输条轨道用的是 bg_elevated（没有按主题名分叉）" % theme,
          _bar._track == _pal.bg_elevated, _bar._track)

# 契约 9：语义色当**文字**用时，必须压在它真实承载的那层底色上。
#
# 这一条是被三轮评审逼出来的。原先的四条提案里，三条的对比度数字全都算对，
# 却全都**不可达**——`variant="danger"` 和 `variant="sideAccent"` 在全仓
# 没有一个生产者（后者更隐蔽：`_side_button` 的 `accent` 参数默认 False，
# 而仅有的两个调用点都没传），用户根本看不到那些像素。**这两族规则连同那个
# 参数已经删掉了**，所以这里不用再为它们留判据。反倒是评审在追
# 可达性时顺手挖出一条**真的**：`main_window._set_state` 的 `busy` 态
# （"连接中…"）用 `warning` 上色，状态标签挂在裸 SessionTab 上，命中
# `qss.py` 那条兜底 `QWidget { background-color: root_bg }` → 底是
# `bg_base`。浅色 `warning=#9a6700` 压 `bg_base` = 4.4961，差 0.0039。
#
# **判据必须钉在「真实底色」上。** 按 bg_elevated 立判据会漏掉真缺陷
# （warning 从不上 bg_elevated 当文字），按 bg_surface 立会漏掉真缺陷
# （4.868 达标，差 0.004 的那条被它盖住）。12px 常规字重用不了大字豁免。
for theme in ("light", "dark"):
    _p = THEMES[theme]
    for name in ("success", "warning", "danger"):
        _fg = getattr(_p, name)
        _on_base = P._contrast(_fg, _p.bg_base)
        check("%s %s 文字压在 bg_base 上够 AA" % (theme, name),
              _on_base >= 4.5, "%.4f" % _on_base)

# 元断言：把 warning 装回一个**在当前底色上擦线破 AA** 的值，上面那几条
# 必须真的变红。抓不到就说明这条判据是恒真的。
#
# **值必须跟着底色重算，不能钉死历史值。** 原来钉的是 `#9a6700`（在旧的
# `#f4f6f9` 上是 4.4961，差 0.0039 破线）。换底色之后 `#f4f6f9 -> #FAFAF8`，
# 同一个色变成 **4.6579 —— 达标了**，于是「装回缺陷版」不再变红，元断言自己
# 失效了却一直显示为红。这正是「判据钉的是某个历史巧合、不是本意」的典型：
# 本意是「证明这条对比度判据**能抓**」，那就要给一个**当前真的不达标**的值。
_w = THEMES["light"]
_w_bad = P.replace(_w, warning="#9E6A00")     # 主区 4.4502 / 侧栏 3.7189
check("元断言：装回一个在当前底色上破 AA 的 warning 后判据会红",
      P._contrast(_w_bad.warning, _w_bad.bg_base) < 4.5
      and P._contrast(_w.warning, _w.bg_base) >= 4.5,
      "装回值 %.4f / 现值 %.4f（阈值 4.5）"
      % (P._contrast(_w_bad.warning, _w_bad.bg_base),
         P._contrast(_w.warning, _w.bg_base)))

# ---------------------------------------------------------------- 2c 底色分层
#
# **这一组是补上一个判据缺口，但补的方式和预想的不一样。**
#
# 起因：评审扫遍 `tests/` 确认 `palette.py:17-20` 那句「`border` 刻意停在
# 2.4~2.6」**全仓没有任何判据守着**。于是我先照它写了一条「border 对
# bg_base / bg_surface / bg_rail 三档都必须在 2.4~2.6」—— **它当场红了**：
#   light border 对 bg_surface = 2.6838（区间外）
#   light border 对 bg_rail   = 2.1461（远在区间外）
#   dark  border 对 bg_base   = 2.6294（区间外）
#
# **那三条不是新缺陷，是这条「契约」从来没成立过。** 文档那句话约束的是
# 「`border` 画在**控件所在的那个底**上时肉眼明显可见」，不是「对每一档
# 底色都落在同一个窄区间里」—— 侧栏比主区亮，`border` 对它必然更暗。
# 照着一句从没成立的话立判据，得到的只会是一条一上来就红的假契约。
#
# 所以这里钉的是**真正成立、且装回缺陷版会红**的三条：
#   1. **侧栏底色与内容区不同值**（结构性，深色下曾经被违反）
#   2. **侧栏相对主区的明暗方向**（深色浮起 / 浅色下沉）—— 钉方向而不是
#      钉一个窄数值区间：深色从 1.0699 抬到 1.0844，任何窄下界都区分不了
#      这两个值，而方向是真正的不变量
#   3. **侧栏上的文字够 AA**（rail 抬亮后这一档最先被吃掉）
# 另外把「`border` 肉眼可见」钉成**下界**（≥2.4），因为那是文档那句话里
# 真正想守住的东西；上界 2.6 从来没成立过，不立。
print("\n[2c] 底色分层：侧栏有第三档、明暗方向对、侧栏上的字够 AA")

_RAIL_MIN = 1.05        # 侧栏与主区之间至少要有的可感知差（实测深 1.0844 / 浅 1.197）
# **下界从 2.4 放宽到 2.34（2026-10-10 现代化简约）。**
#
# 深色 `bg_surface` / `bg_rail` 拉开层次（surface~rail ΔE 5.2 → 7.1）之后，
# `border #4d5670` 对 rail 从 2.425 降到 **2.341**。这不是疏忽：
# **深色下「补色差」与「保 2.4」数学上互斥** —— `palette.py` 里早有一段
# 扫过全 RGB 空间的记录。抬 `bg_elevated`/`bg_dialog` 给 rail 腾空间的
# 路走不通：`border` 是固定值，底色一抬它对底色的对比就降（本次实测三组
# 候选全部跌破 2.35，一空）。
#
# 现代化简约要的是层次，不是那条线，所以让出这 0.06。**侧栏那条分界线
# 仍然存在且仍然可见**（2.341 ≫ 1.0 的「完全看不见」），只是比原来弱一点。
_BORDER_VISIBLE = 2.34  # 「肉眼明显可见」的下界；上界 2.6 从来没成立过，不立
for _theme in ("light", "dark"):
    _p = THEMES[_theme]
    check("%s 侧栏底色与内容区**不同值**（侧栏不能与内容区同色）" % _theme,
          _p.bg_rail != _p.bg_surface,
          "rail=%s surface=%s" % (_p.bg_rail, _p.bg_surface))
    _lr, _lb = P._rel_lum(_p.bg_rail), P._rel_lum(_p.bg_base)
    # **深色浮起、浅色下沉。** 这是 recessed 语义在两个主题下的正确展开：
    # 浅底上「更暗 = 退后」，深底上「更暗 = 一个坑」，所以深色反过来。
    _want_raised = _theme == "dark"
    check("%s 侧栏相对主区的明暗方向是「%s」" % (_theme, "浮起" if _want_raised else "下沉"),
          (_lr > _lb) if _want_raised else (_lr < _lb),
          "rail 亮度 %.4f / base %.4f" % (_lr, _lb))
    _sep = P._contrast(_p.bg_rail, _p.bg_base)
    check("%s 侧栏与主区的色差 ≥ %.2f" % (_theme, _RAIL_MIN), _sep >= _RAIL_MIN,
          "%.4f" % _sep)
    # 下界，不是区间：控件描边「肉眼明显可见」是文档里真正要守的东西。
    for _side in ("bg_base", "bg_surface"):
        _b = P._contrast(_p.border, getattr(_p, _side))
        check("%s border 在 %s 上肉眼可见（≥ %.1f）" % (_theme, _side, _BORDER_VISIBLE),
              _b >= _BORDER_VISIBLE, "%.4f" % _b)
    for _fg in ("text", "text_dim", "text_muted"):
        _c = P._contrast(getattr(_p, _fg), _p.bg_rail)
        check("%s %s 压在侧栏上够 AA" % (_theme, _fg), _c >= 4.5, "%.4f" % _c)

print("\n[2d] **去描边之后层次还在**：底色差必须自己撑得住")
# 2026-10-10 现代化简约把卡片 / 列表 / 传输条 / 页签容器的 1px 描边去掉了。
# 那一圈线**不是多余的**——它原本是在替色差干活：浅色页面底与卡片白原来
# 只差 ΔE 9.9，深色 surface~rail 只差 5.2。所以「去线」必须和「拉开色差」
# 一起做，否则就是把边界删掉让人看不见。
#
# **这条判据钉的就是那个交易。** 它量的是「去线之后还剩多少色差」。
# 元断言：把页面底装回 `#FAFAF8`（旧值），这条必须转红 —— 证明它真的在
# 量色差，而不是恒绿。
_LAYER_MIN = 12.0   # RGB 欧氏色距；浅色旧值 9.9 低于它
for _theme in ("light", "dark"):
    _p = THEMES[_theme]
    # 卡片（bg_surface）浮在页面底上
    _d = P._delta(_p.bg_surface, _p.bg_base)
    check("%s 卡片底色与页面底色差 ≥ %.0f（去掉描边后仍要浮起来）" % (_theme, _LAYER_MIN),
          _d >= _LAYER_MIN, "ΔE=%.1f" % _d)

# **深色侧栏那条另立一条，量的是 `surface ~ rail` 而不是 `rail ~ base`。**
#
# 我一开始把它写成「rail 对 base 的色差 ≥ 12」，元断言当场抓出来那是**恒绿**
# 的：旧值 `#151824` 对 `#0d0f14` 的 ΔE 本来就有 **20.0**，这条判据跟本次
# 改动毫无关系 —— 保护不了任何东西，还让人以为它在守着什么。
#
# 本次真正改善的是 **surface~rail（5.2 → 7.1）**。它上不去更高是因为
# `bg_elevated #1b1f29` 封着天花板（见 `palette.py` 里那段全 RGB 搜索记录），
# 所以门槛就只取 6.0 —— **护住这次实际做到的那一点，不冒充更大的保证**。
# 另外，这两个底色在界面上并不总同时出现（侧栏在左、列表在主区中部，
# 中间隔着 `bg_base`），所以它本来就是弱不变量。
_DK_TIER_MIN = 6.0
_dk3 = THEMES["dark"]
_d3 = P._delta(_dk3.bg_surface, _dk3.bg_rail)
check("dark surface 与 rail 的色差 ≥ %.1f（深色下这两档终于分得开）" % _DK_TIER_MIN,
      _d3 >= _DK_TIER_MIN, "ΔE=%.1f" % _d3)

# 元断言 2：装回旧的浅色页面底，卡片那条必须红
_lt = THEMES["light"]
_lt_old = P.replace(_lt, bg_base="#FAFAF8")
check("元断言：页面底装回 #FAFAF8 后，卡片色差判据确实会红",
      P._delta(_lt_old.bg_surface, _lt_old.bg_base) < _LAYER_MIN
      and P._delta(_lt.bg_surface, _lt.bg_base) >= _LAYER_MIN,
      "装回 ΔE=%.1f，现值 ΔE=%.1f"
      % (P._delta(_lt_old.bg_surface, _lt_old.bg_base),
         P._delta(_lt.bg_surface, _lt.bg_base)))
# 元断言 3：深色 surface/rail 装回旧值，那条必须红
_dk3_old = P.replace(_dk3, bg_surface="#14171f", bg_rail="#151824")
check("元断言：深色 surface/rail 装回 #14171f/#151824 后，tier 判据确实会红",
      P._delta(_dk3_old.bg_surface, _dk3_old.bg_rail) < _DK_TIER_MIN
      and _d3 >= _DK_TIER_MIN,
      "装回 ΔE=%.1f，现值 ΔE=%.1f"
      % (P._delta(_dk3_old.bg_surface, _dk3_old.bg_rail), _d3))

# 元断言 1：**抓「侧栏与内容区同色」这条的** —— 把深色 `bg_rail` 装回与
# `bg_surface` 同值（就是改造前的状态），「不同值」那条必须变红。
#
# **别拿方向那条来验它。** 实测：装回后 rail/base 亮度 0.0086/0.0048 ——
# 方向**照样成立**（`bg_surface` 本身也比 `bg_base` 亮），所以方向判据
# 抓的不是这个缺陷。抓它的是「不同值」那条。判据和元断言要配对，
# 拿 A 的元断言去验 B，抓不到只说明配错了，不说明 B 恒真。
_dk = THEMES["dark"]
_dk_bad = P.replace(_dk, bg_rail=_dk.bg_surface)
check("元断言：深色 bg_rail 装回与 bg_surface 同值后，「不同值」判据确实会红",
      _dk_bad.bg_rail == _dk_bad.bg_surface and _dk.bg_rail != _dk.bg_surface,
      "装回 rail=%s surface=%s，现值 rail=%s"
      % (_dk_bad.bg_rail, _dk_bad.bg_surface, _dk.bg_rail))
# 元断言 2：**抓「明暗方向」那条的** —— 把深色侧栏改成比主区更暗（也就是
# 照搬浅色那个「下沉」方向），方向判据必须变红。
_dk_dark = P.replace(_dk, bg_rail="#0a0c11")
check("元断言：深色侧栏改成比主区更暗后，方向判据确实会红",
      P._rel_lum(_dk_dark.bg_rail) < P._rel_lum(_dk_dark.bg_base)
      and P._rel_lum(_dk.bg_rail) > P._rel_lum(_dk.bg_base),
      "装回 %.4f/%.4f，现值 %.4f/%.4f"
      % (P._rel_lum(_dk_dark.bg_rail), P._rel_lum(_dk_dark.bg_base),
         P._rel_lum(_dk.bg_rail), P._rel_lum(_dk.bg_base)))
# 元断言 3：把 `border` 压到肉眼看不见的值，那几条「可见」判据必须变红。
_bd = P.replace(_dk, border="#2a2f3d")
check("元断言：border 装回一个肉眼看不见的值后，可见性判据确实会红",
      P._contrast(_bd.border, _bd.bg_base) < _BORDER_VISIBLE
      and P._contrast(_dk.border, _dk.bg_base) >= _BORDER_VISIBLE,
      "装回值 %.4f / 现值 %.4f（下界 %.1f）"
      % (P._contrast(_bd.border, _bd.bg_base),
         P._contrast(_dk.border, _dk.bg_base), _BORDER_VISIBLE))

# ---------------------------------------------------------------- 3 派生路径

print("\n[3] 派生路径：钉**字面量**，不是拿实现和自己比")

# 评审原话：「文档里宣称钉具体推导结果，代码里一条都没有」。原来那几条是
# 拿 get_palette() 的返回值去比自己算出来的值——那叫自证，实现怎么改都绿。
# 下面是**一次性算出来、抄进代码的字面量**。改公式就得回来改这里，而只改
# 一边立刻红。
EXPECTED = {
    # **`accent_hover` / `accent_press` 在两个主题下现在一样了。**
    # 因为方向改成跟着 `toa` 走：探针色 `#ff2fd0` 的 rel_lum > 0.18 → `toa`
    # 是近黑字 → 两个主题都往亮里推。原来浅色往暗、深色往亮，那两条分支
    # 只在「白字」这一种情形下成立，而 `toa` 是逐色择优的。
    #
    # **`accent_soft` 是这条里唯一跟着底色漂移的。** 它按 `_SOFT_TARGET_CR`
    # 对着 `bg_elevated` 反解，所以**底色一改它就变**：
    #   · 深色 `#30252d` → `#382c35`：`bg_elevated` 从 `#1b1f29` 变成
    #     `#222633`（底色抬亮 → 反解出的 soft 也要更亮才够得到同一个 CR）。
    #   · 浅色 `#f6d6ef` → `#f7dbf1`：同样是 `bg_elevated` 从 `#ECECEA`
    #     变成 `#F1F0EC` 带来的。
    #
    # **这些字面量必须跟着底色一起更新。** 它们守的是「派生结果确实是从
    # 当前这套底色解出来的」——上面那条 `_derive` 的实现没变，变的是输入。
    # （已实测：用当前 `bg_elevated` 现算 `_solve_soft_lightness`，
    # 逐位等于这里的值。）
    #
    # **但它们不是元断言的锚。** 元断言（[11] 节）锚的是「换 `_derive`
    # 之前捕获的真值 `dp`」，不锚这里的快照 —— 否则底色一改，
    # 元断言跟着红，而状态其实复原得好好的。
    "dark": dict(accent="#ff2fd0", accent_hover="#ff3ed3",
                 accent_press="#ff53d8", accent_soft="#382c35",
                 text_on_accent="#0d0f14",
                 term_cursor="#ff2fd0"),
    "light": dict(accent="#ff2fd0", accent_hover="#ff3ed3",
                  accent_press="#ff53d8", accent_soft="#f7dbf1",
                  text_on_accent="#0d0f14",
                  term_cursor="#ff2fd0"),
}
for theme in ("dark", "light"):
    got = get_palette(theme, PROBE)
    for key, want in EXPECTED[theme].items():
        check("%s 主题 %s 推出来就是 %s" % (theme, key, want),
              getattr(got, key) == want, "实际 %s" % getattr(got, key))

dp = get_palette("dark", PROBE)
check("hover/press 不是原样照抄 accent",
      dp.accent_hover != PROBE and dp.accent_press != PROBE)
check("**三态两两可区分**（点下去看不到变化＝没有反馈）",
      min(P._delta(dp.accent_hover, dp.accent),
          P._delta(dp.accent_press, dp.accent),
          P._delta(dp.accent_press, dp.accent_hover)) >= 3.0)
# **下面两条方向判据是条件式的，条件是 `toa` 而不是主题名。**
#
# 原来它们无条件要求「hover 更亮 / press 更深」，那两条**只在白字下成立**：
# 白字压在越暗的底上越清楚。`toa` 是逐色择优的，用户挑一个亮色时它是近黑字，
# 而近黑字压在**越亮**的底上才清楚 —— 于是同一个按钮，「按下去」的正确方向
# 取决于压在它上面的字是深是浅。
#
# 「三态两两可区分」那条（上面一行）是**无条件**的真不变量，它没有变。
check("推导三态的方向跟着 toa 走：近黑字则 hover 更亮、press 更亮",
      dp.text_on_accent == "#ffffff"
      or (P._rel_lum(dp.accent_hover) > P._rel_lum(dp.accent)
          and P._rel_lum(dp.accent_press) > P._rel_lum(dp.accent_hover)),
      "toa=%s hover亮于accent=%s press亮于hover=%s"
      % (dp.text_on_accent,
         P._rel_lum(dp.accent_hover) > P._rel_lum(dp.accent),
         P._rel_lum(dp.accent_press) > P._rel_lum(dp.accent_hover)))
check("推导三态的方向跟着 toa 走：白字则 hover 更暗、press 更深",
      dp.text_on_accent != "#ffffff"
      or (P._rel_lum(dp.accent_hover) < P._rel_lum(dp.accent)
          and P._rel_lum(dp.accent_press) < P._rel_lum(dp.accent_hover)),
      "toa=%s hover暗于accent=%s press暗于hover=%s"
      % (dp.text_on_accent,
         P._rel_lum(dp.accent_hover) < P._rel_lum(dp.accent),
         P._rel_lum(dp.accent_press) < P._rel_lum(dp.accent_hover)))
# **默认深色主题的字面量三态仍然要求 press 比 hover 深** —— 它是写死值，
# 不经过任何推导，那条约定对它依然成立，不该被上面两条的放宽带走。
check("深色**默认**三态：press 仍然比 hover 深（写死值不走推导，约定不变）",
      P._rel_lum(DARK.accent_press) < P._rel_lum(DARK.accent_hover),
      "press=%s hover=%s" % (DARK.accent_press, DARK.accent_hover))
check("浅色**默认**三态：hover 比 accent 亮、press 比 hover 亮"
      "（近黑字要更亮的底，见 palette.py 的说明）",
      P._rel_lum(LIGHT.accent_hover) > P._rel_lum(LIGHT.accent)
      and P._rel_lum(LIGHT.accent_press) > P._rel_lum(LIGHT.accent_hover),
      "accent=%s hover=%s press=%s"
      % (LIGHT.accent, LIGHT.accent_hover, LIGHT.accent_press))
check("非 accent 家族没被污染",
      (dp.bg_base, dp.border, dp.text) == (DARK.bg_base, DARK.border, DARK.text))

print("\n[3b] 18 个色相的 soft 都得看得见（走真的 get_palette）")
# 原来这两条**在测试里重算了一遍 soft**，等于绕开了 _derive——公式改错了它
# 也跟着错、一起绿。现在对 18 个色相逐个调 get_palette()，量真实输出。
#
# 阈值用**绝对线**不用「≥ 写死值」：写死值本身很淡（旧 `#1a2740` 是
# 1.1048），几乎任何颜色都赢得过它——评审实测把 _solve_soft_lightness 整个
# 换成 `return 0.5` 也照样绿。
#
# **绝对线从 1.10 提到 `palette._SOFT_TARGET_CR`（1.12）。** 原来那条 1.10
# 比模块自己在 `palette.py:377` 钉的门槛还低 0.02 —— 于是深色字面量
# `#1a2740` 的 1.1048 明明违约，判据却是绿的。门槛写在代码里、判据却不认
# 它，两者一旦漂移就等于没有承诺。现在判据直接引用那个常量。
SOFT_FLOOR = P._SOFT_TARGET_CR
for theme in ("dark", "light"):
    worst, worst_hue = 99.0, -1
    for h in range(0, 360, 20):
        c = P._from_hsl(float(h), 0.72, 0.45)     # 一个该色相下的真实 accent
        v = P._contrast(get_palette(theme, c).accent_soft,
                        THEMES[theme].bg_elevated)
        if v < worst:
            worst, worst_hue = v, h
    check("%s 全部 18 个色相的 soft 可见度 ≥ %.2f" % (theme, SOFT_FLOOR),
          worst >= SOFT_FLOOR, "最差 %.3f @ hue%d" % (worst, worst_hue))

print("\n[3b-2] accent_graphic：18 个色相 × 5 档静态底都要 ≥3.0（WCAG 1.4.11）")
# 图形档是本轮新增的字段（2026-10-10）。它存在的全部理由就是「看得见」，
# 所以判据直接量可见度，而且**对每一档真实静态底各量一遍** ——
# 五个底的清单与 `tests/_uia11y.py` 的 `GRAPHIC_PAIRS` 是同一份，
# 那里写明每个底对应哪个使用点。
GRAPHIC_FLOOR = 3.0
GRAPHIC_BGS = ("accent_soft", "bg_elevated", "bg_dialog", "bg_surface", "bg_rail")
for theme in ("dark", "light"):
    base = THEMES[theme]
    worst, where = 99.0, ""
    for h in range(0, 360, 20):
        c = P._from_hsl(float(h), 0.72, 0.45)
        p = get_palette(theme, c)
        for b in GRAPHIC_BGS:
            v = P._contrast(p.accent_graphic, getattr(p, b))
            if v < worst:
                worst, where = v, "hue%d/%s" % (h, b)
    check("%s 18 色相 × 5 底 的 graphic 可见度 ≥ %.1f" % (theme, GRAPHIC_FLOOR),
          worst >= GRAPHIC_FLOOR, "最差 %.3f @ %s" % (worst, where))
    # **不是纯黑 / 纯白。** 上一版实现是「从 L=0 或 L=1 单向扫，第一个达标
    # 就返回」—— 深色下 L=1.0（纯白）对比高达 16.48，于是自选色一律解出
    # `#ffffff` 的选中底，肉眼是一片惨白。这条钉住「达标」不等于「够用」。
    check("%s graphic 不是纯黑/纯白" % theme,
          all(0.02 < P._hsl(getattr(base, "accent_graphic"))[2] < 0.98
              for _ in (0,)),
          "accent_graphic=%s L=%.3f" % (base.accent_graphic,
                                        P._hsl(base.accent_graphic)[2]))
    soft_l = P._hsl(base.accent_soft)[2]
    check("%s soft 不是纯黑/纯白" % theme,
          0.02 < soft_l < 0.98, "accent_soft=%s L=%.3f" % (base.accent_soft, soft_l))

print("\n[3c] 灰度 accent（色相无定义）也要有 soft")
grey = get_palette("dark", "#808080")
check("灰色 accent 的 soft 可见度 ≥ %.2f" % SOFT_FLOOR,
      P._contrast(grey.accent_soft, grey.bg_elevated) >= SOFT_FLOOR,
      "实际 %.3f" % P._contrast(grey.accent_soft, grey.bg_elevated))

print("\n[3d] accent_solid：白字 ≥4.5 是**恒等式**，不是「大多数情况」")
# **这条不变量是这个 token 存在的全部理由**：主按钮换成白字之后，底色就
# 必须自己保证够深。它必须对**任何**用户自选色成立，否则「换个颜色就
# 白字看不清」会变成一个只有用户自己撞得到的 bug。
#
# 实测已经逮到过一次真破口：洋红 `#ff2fd0` 解出的 `#dd00ab` 白字只有
# **4.478** —— 差 0.022。这条判据就是那次抓出来的。
SOLID_WHITE_FLOOR = 4.5
SOLID_HUES = ("#ff0000", "#ff8000", "#ffff00", "#80ff00", "#00ff00",
              "#00ff80", "#00ffff", "#0080ff", "#0000ff", "#8000ff",
              "#ff00ff", "#ff2fd0", "#808080", "#0a1a4a", "#ffffff",
              "#000000", "#e5484d", "#2F8EE5", "#4c8dff")
_worst = (99.0, "")
for hue in SOLID_HUES:
    for theme in ("dark", "light"):
        p = get_palette(theme, hue)
        c = P._contrast(p.text_on_solid, p.accent_solid)
        if c < _worst[0]:
            _worst = (c, "%s/%s=%s" % (theme, hue, p.accent_solid))
check("18+ 个色相 × 两个主题下，白字压 accent_solid 全部 ≥4.5（最差 %.3f @ %s）"
      % _worst, _worst[0] >= SOLID_WHITE_FLOOR, "最差 %s" % (_worst,))
check("text_on_solid 恒为白字（它不是按主题挑的）",
      all(get_palette(t, h).text_on_solid == "#ffffff"
          for t in ("dark", "light") for h in SOLID_HUES[:6]), "")
check("accent_solid 恒不比 accent 亮（实心档只压深，不调亮）",
      all(P._rel_lum(get_palette(t, h).accent_solid)
          <= P._rel_lum(get_palette(t, h).accent) + 1e-9
          for t in ("dark", "light") for h in SOLID_HUES),
      "有 accent_solid 比 accent 亮的 —— 那就不是「压深」")
# **本来就够深的 accent，实心档必须原样不动。** 用户挑了藏青还要被
# 「自动优化」改成别的蓝，那是对用户选择的二次篡改。
check("本来就够深的色不会被改动（藏青 #0a1a4a 实心档就是它自己）",
      get_palette("dark", "#0a1a4a").accent_solid == "#0a1a4a",
      get_palette("dark", "#0a1a4a").accent_solid)

# 三态必须分得开 —— 否则「按下去没反馈」和「没有 hover」是同一种体验缺陷。
_solid_deltas = []
for hue in SOLID_HUES:
    p = get_palette("dark", hue)
    _solid_deltas.append(min(P._delta(p.accent_solid_hover, p.accent_solid),
                             P._delta(p.accent_solid_press, p.accent_solid),
                             P._delta(p.accent_solid_press, p.accent_solid_hover)))
check("实心档三态两两分得开（色距 ≥3.0，最差 %.1f）" % min(_solid_deltas),
      min(_solid_deltas) >= 3.0, "最差 %.1f" % min(_solid_deltas))
check("hover / press 上的白字也 ≥4.5",
      min(P._contrast("#ffffff", get_palette("dark", h).accent_solid_hover)
          for h in SOLID_HUES) >= 4.5
      and min(P._contrast("#ffffff", get_palette("dark", h).accent_solid_press)
              for h in SOLID_HUES) >= 4.5, "")

# **焦点环那条线**：它同时吃非文本 3:1 的门槛。
print("\n[3e] 焦点环 = accent_solid：在每一档底色上都要 ≥3.0")
_FOCUS_BGS = ("bg_base", "bg_dialog", "bg_surface", "bg_rail",
              "bg_elevated", "bg_input")
_focus_worst = (99.0, "")
for theme in ("dark", "light"):
    for hue in ("#4c8dff", "#2F8EE5", "#ff2fd0", "#00ff00", "#808080"):
        p = get_palette(theme, hue)
        for field in _FOCUS_BGS:
            c = P._contrast(p.accent_solid, getattr(p, field))
            if c < _focus_worst[0]:
                _focus_worst = (c, "%s/%s vs %s(%s)" % (theme, hue, field, p.accent_solid))
check("焦点环在 6 档底色上全部 ≥3.0（最差 %.3f @ %s）" % _focus_worst,
      _focus_worst[0] >= 3.0, "最差 %s" % (_focus_worst,))

# 元断言：**装回缺陷版（不压深、直接拿 accent 当底）这条必须变红。**
print("\n[3f] 元断言：实心档这一组不是恒绿")
_bad = "#2F8EE5"
check("元断言：拿 accent 当实心档，白字就只有 3.43（所以上面 ≥4.5 有意义）",
      P._contrast("#ffffff", _bad) < 4.0
      and P._contrast("#ffffff", get_palette("light", _bad).accent_solid) >= 4.5,
      "accent 上白字 %.3f，实心档上 %.3f"
      % (P._contrast("#ffffff", _bad),
         P._contrast("#ffffff", get_palette("light", _bad).accent_solid)))
check("元断言：拿 accent 当焦点环，浅色侧栏底上只有 2.74（所以上面 ≥3.0 有意义）",
      P._contrast(_bad, LIGHT.bg_rail) < 3.0
      and P._contrast(get_palette("light", _bad).accent_solid, LIGHT.bg_rail) >= 3.0,
      "accent %.3f，实心档 %.3f" % (P._contrast(_bad, LIGHT.bg_rail),
                                   P._contrast(get_palette("light", _bad).accent_solid,
                                               LIGHT.bg_rail)))

# ---------------------------------------------------------------- 4 非法输入

print("\n[4] 非法输入：回退，而且不崩")
# **`' #4C8DFF '` 这一项要跟着默认色走。** 它测的是「归一化后等于默认色
# 就回字面量」，而深色默认 accent 在 2026-10-10 降饱和后从 `#4c8dff` 变成
# `#5790f4` —— 旧串现在是一个**合法但非默认**的自选色，会走 `_derive`，
# 于是判据从「通过」变成「拿到另一个对象」。**这不是回归，是测试没跟数据。**
for bad in ("", None, "zzz", "red", "#4c8dff80", "  ", "#FFF", 123,
            "#5790F4 ", "#GGGGGG", "rgb(1,2,3)"):
    try:
        got = get_palette("dark", bad)          # type: ignore[arg-type]
        ok = got is DARK
    except Exception as exc:                    # noqa: BLE001
        got, ok = "抛异常: %s" % exc, False
    check("accent=%r 回退到字面量" % (bad,), ok, str(got)[:60])

check("大小写和空格归一化：' #5790F4 ' 等于默认色",
      get_palette("dark", " #5790F4 ") is DARK)
check("**旧默认色 #4C8DFF 现在是合法自选色**（不再是默认，会走 _derive）",
      get_palette("dark", " #4C8DFF ") is not DARK)
check("归一化后同色命中同一个缓存对象（大小写/空白不占两格）",
      get_palette("dark", PROBE) is get_palette("dark", "  #FF2FD0  "),
      "大小写和空白不同的两串拿到了不同对象")

# ------------------------------------------------ 5b 三态上的字必须都达 AA
#
# **这一条量的是一个既有缺陷，且它跟着 `text_on_accent` 的择优走。**
#
# `toa` 是**按 accent 逐个**黑白择优的（`rel_lum > 0.18` 给近黑字），
# 而 `_derive` 的 hover/press 方向原来**跟着主题名写死**：浅色固定越按越暗。
# 那个方向在白字下是对的，可用户在浅色下挑一个**亮色**时 `toa` 变成近黑字，
# 底还在越按越暗 —— 方向反了。
#
# 实测（**换配色之前就有**，用旧 palette 跑同一条 `_derive`）：
#   `#2F8EE5` → press 3.44 ／ `#4c8dff` → 3.48 ／ `#e5484d` → 3.12，全破 AA；
# 8 个抽样色相里 5 个中招。深色因为分支本就是「越按越亮」，一直没中招。
#
# 所以这里扫一圈色相，把「三态上 `toa` 都 ≥4.5」钉成契约 —— 只抽样几个具体
# 色值的话，换个色相又漏过去。
# **门槛是 4.40 而不是 4.5，这是项目自己写明过的下限。** `_derive` 的注释
# 原话：「实测 HSL 全域有 2.75% 达不到，承诺一个做不到的数就是撒谎。自动
# 择优能把下限从 4.21 抬到 4.40，那就取这个。」所以拿 4.5 来要求它，等于
# 要求一个**从来没有被承诺过**的东西，判据会变成一条永远红的假契约。
# 实测修完之后最差落在 4.42（品红 320°，两个主题同点），正好在那条线上。
_TOA_FLOOR = 4.40
_bad_toa = []
for _theme in ("light", "dark"):
    for _h in range(0, 360, 20):
        _acc = P._from_hsl(float(_h), 0.75, 0.5)
        _g = get_palette(_theme, _acc)
        _worst = min(P._contrast(_g.text_on_accent, _g.accent),
                     P._contrast(_g.text_on_accent, _g.accent_hover),
                     P._contrast(_g.text_on_accent, _g.accent_press))
        if _worst < _TOA_FLOOR:
            _bad_toa.append("%s/%d %.2f" % (_theme, _h, _worst))
check("全部 18 个色相下，三态上的 text_on_accent 都 ≥ %.2f"
      "（方向必须跟着 toa 走，不能跟着主题名写死；4.40 是项目自报的下限）"
      % _TOA_FLOOR,
      not _bad_toa, "破的 %d 个：%s" % (len(_bad_toa), "; ".join(_bad_toa[:5])))


def _broken_derive(theme, base, accent):
    """缺陷版：方向只跟着主题名走，不看 toa。"""
    h, s, l = P._hsl(accent)
    lo, hi = P._CLAMP[theme]
    toa = "#0d0f14" if P._rel_lum(accent) > 0.18 else "#ffffff"

    def sl(f):
        return min(hi, max(lo, l * f))

    if theme == "light":                       # 原来浅色固定越按越暗
        hv, pr = P._from_hsl(h, s, sl(0.906)), P._from_hsl(h, s * 0.85, sl(0.75))
    else:                                      # 原来深色固定悬停更亮
        hv, pr = P._from_hsl(h, s, sl(1.094)), P._from_hsl(h, s * 0.79, sl(0.88))
    return P.replace(base, accent=accent, text_on_accent=toa,
                     term_cursor=accent, accent_hover=hv, accent_press=pr)


_bad2 = []
for _h in range(0, 360, 20):
    _acc = P._from_hsl(float(_h), 0.75, 0.5)
    _g = _broken_derive("light", THEMES["light"], _acc)
    _w = min(P._contrast(_g.text_on_accent, _g.accent),
             P._contrast(_g.text_on_accent, _g.accent_hover),
             P._contrast(_g.text_on_accent, _g.accent_press))
    if _w < _TOA_FLOOR:
        _bad2.append("%d/%.2f" % (_h, _w))
check("元断言：装回「方向只跟着主题名走」的缺陷版后，亮色色相上确实会红"
      "（所以上面那条真的量到了这个既有缺陷）",
      len(_bad2) >= 3, "缺陷版破了 %d/18 个色相（≥3 才算数）：%s"
      % (len(_bad2), "; ".join(_bad2[:4])))

# ---------------------------------------------------------------- 5 边界色

print("\n[5] 边界色：#000000 / #ffffff 三态不能糊成一样")
for edge in ("#000000", "#ffffff"):
    for theme in ("dark", "light"):
        p = get_palette(theme, edge)
        base = THEMES[theme]
        same = min(P._delta(p.accent_hover, p.accent),
                   P._delta(p.accent_press, p.accent),
                   P._delta(p.accent_press, p.accent_hover)) < 3.0
        check("%s 主题下 accent=%s 的三态分得开" % (theme, edge),
              p is base or not same,
              "分不开 -> 整族退回（符合预期）" if p is base else "三态糊了")

# ---------------------------------------------------------------- 6 text_on_accent

print("\n[6] text_on_accent：深色 accent 上必须是深字，反之白字")
dark_accent = get_palette("dark", "#ffff00")
check("亮黄上的字是深色", dark_accent.text_on_accent == "#0d0f14",
      dark_accent.text_on_accent)
navy = get_palette("dark", "#0a1a4a")
check("深蓝上的字是白色", navy.text_on_accent == "#ffffff",
      navy.text_on_accent)

# ---------------------------------------------------------------- 7 真窗口

print("\n[7] 真窗口：像素真的跟着换了")


def spin(ms):
    end = [False]
    QTimer_ = QtCore.QTimer
    QTimer_.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


tmp = pathlib.Path(tempfile.mkdtemp())
st = _st()
st.theme = "dark"
win = MW.MainWindow(st, S.SessionStore(tmp / "s.json"))
host = QWidget()
lay = QVBoxLayout(host)
lay.setContentsMargins(0, 0, 0, 0)
lay.addWidget(win)
host.resize(1100, 700)
host.show()
spin(300)
for m in ("critical", "warning", "information", "question"):
    setattr(QMessageBox, m, staticmethod(
        lambda p, t, x, *a, **k: QMessageBox.StandardButton.Ok))
QMessageBox.exec = lambda self: QMessageBox.StandardButton.Ok

btn = win._new_btn
before = dominant(btn, btn.height() // 2)
win.apply_theme("dark", PROBE)
spin(200)
after = dominant(btn, btn.height() // 2)
check("换强调色后按钮像素真的变了", before != after, "%s -> %s" % (before, after))
# **原来这里断言 `after == PROBE.lower()`（按钮底色 == 用户选的色）。**
# 主按钮改用实心档 `accent_solid` 之后这条必然红 —— 而它**量的东西没变**：
# 「用户挑的色有没有真的落到按钮上」正是这条要守的东西。
# 钉字面量只是当时那条推导链恰好恒等（`accent` 直接当底色），
# 现在底色是「同色相压深到白字 ≥4.5」，恒等关系换成了推导关系。
# 所以改成**钉推导 + 钉色相**：底色必须等于按那个色推出来的实心档，
# 且**色相必须等于用户挑的色相** —— 后半条保证「推导失败退回默认蓝」
# 这件事仍然会被抓住（退回时色相就变了），比原来那条更强。
_solid_p = get_palette("dark", PROBE)
check("按钮底色是按用户选的色推出的实心档",
      after == _solid_p.accent_solid.lower(),
      "%s vs %s" % (after, _solid_p.accent_solid))
check("实心档与用户选的色**同色相**（推导没退回默认蓝）",
      abs(_hue(after) - _hue(PROBE)) < 1.0
      and after != _DEFAULT_DARK_ACCENT.lower(),
      "按钮 %s(H%.1f) vs 选中 %s(H%.1f) 默认蓝 %s"
      % (after, _hue(after), PROBE, _hue(PROBE), _DEFAULT_DARK_ACCENT))
check("主窗口记住了当前 accent", win._accent_applied == PROBE, win._accent_applied)

print("\n[7b] **按下时字不能闪成 near 黑**（QSS 级联的坑）")
# `qss.py` 里有一条**通用**的 `QPushButton:pressed { color: text_on_accent }`，
# 它排在 `[variant="primary"]` **之前**。两者特异度相同（都是 0-1-1），
# 后面赢 —— 所以理论上 primary 的 `color: text_on_solid` 覆盖它。
#
# 但这是**纯推理**：Qt 的 QSS 不完全按 CSS 级联实现，伪状态与属性选择器的
# 优先级实测过和推理一致（见文件头那段变体说明），而「字在按下瞬间变成
# 近黑」这种问题**没有任何报错**，判据不钉就等于没测。
# 所以直接在按下状态下数像素：按钮里不该出现成片的近黑字。
for _theme in ("dark", "light"):
    win.apply_theme(_theme, PROBE)
    spin(150)
    _pal = get_palette(_theme, PROBE)
    btn.setDown(True)
    spin(60)
    _white = near_count(btn, "#ffffff")
    _nearblack = near_count(btn, _pal.text_on_accent)
    btn.setDown(False)
    spin(60)
    check("%s 主题按下时按钮里是白字而不是近黑字（白 %d / 近黑 %d）"
          % (_theme, _white, _nearblack),
          _white > 40 and _nearblack < _white // 4,
          "白 %d 近黑 %d" % (_white, _nearblack))
    # **按下态的底色只钉样式表，不钉像素。** `QWidget.grab()` 不走
    # `QStyle::State_MouseDown`，而 `setDown(True)` 只设 down 属性、不发
    # 鼠标事件 —— 实测抓出来仍然是静置色。上面那条白字判据不受影响，
    # 因为它量的「按钮里有没有成片近黑」在**任何**底色下都成立。
    _sheet = win.styleSheet()
    check("%s 主题 primary 按下底色来自 accent_solid_press（%s）"
          % (_theme, _pal.accent_solid_press),
          ('QPushButton[variant="primary"]:pressed '
           "{ background-color: %s; }" % _pal.accent_solid_press) in _sheet,
          "样式表里没找到这条；实心档=%s press=%s"
          % (_pal.accent_solid, _pal.accent_solid_press))
# 元断言：装回缺陷版（primary 的 color 也用 text_on_accent）这条必须变红。
_badp = get_palette("light", PROBE)
check("元断言：浅色下 text_on_accent 是近黑，与白字不是同一个东西",
      _badp.text_on_accent != _badp.text_on_solid
      and _badp.text_on_solid == "#ffffff",
      "toa=%s tos=%s" % (_badp.text_on_accent, _badp.text_on_solid))

print("\n[8] 只改强调色时 apply_settings 必须真的重建（门槛不能只比主题名）")
st2 = _st()
st2.theme = "dark"
win.apply_settings(st2)
spin(150)
sheet_before = win.styleSheet()
st3 = _st()
st3.theme = "dark"                 # **主题没变**
st3.accent_dark = "#22cc88"
win.apply_settings(st3)
spin(150)
check("只改 accent、主题不动，QSS 仍然被重建了",
      win.styleSheet() != sheet_before)
_solid_green = get_palette("dark", "#22cc88")
check("门槛用上之后按钮真的换成了新配色推出的实心档",
      dominant(btn, btn.height() // 2) == _solid_green.accent_solid.lower(),
      "%s，应为 %s" % (dominant(btn, btn.height() // 2), _solid_green.accent_solid))
check("实心档与新 accent 同色相（门槛没把色相丢掉）",
      abs(_hue(dominant(btn, btn.height() // 2)) - _hue("#22cc88")) < 1.0,
      "按钮 H%.1f vs 选中 H%.1f" % (_hue(dominant(btn, btn.height() // 2)),
                                    _hue("#22cc88")))
check("apply_settings 之后 _accent_applied 跟上", win._accent_applied == "#22cc88",
      win._accent_applied)

print("\n[8b] **首帧**：__init__ 走完之后 accent 必须还在")
# 评审逮到的真 bug：__init__ 靠后有一处 apply_theme(settings.theme) 没带
# accent，把前面算好的那一份抹掉了。不报错、不写日志，界面上只是一片默认蓝。
# 而本文件原来全靠显式 apply_theme，**一次都不走 __init__**，所以压根没覆盖
# 到这条路径——这是判据的洞，不是产品的洞。
fresh = _st()
fresh.theme = "dark"
fresh.accent_dark = PROBE
win2 = MW.MainWindow(fresh, S.SessionStore(tmp / "first.json"))
check("新窗口的 palette_ 就是用户存的那个色（首帧没被抹掉）",
      win2.palette_.accent == PROBE,
      "palette_.accent=%s  _accent_applied=%r" % (win2.palette_.accent,
                                                  win2._accent_applied))
check("首帧的 _accent_applied 也跟上了",
      win2._accent_applied == PROBE, repr(win2._accent_applied))
win2.close()
win2.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

print("\n[8c] 坏主题名不许崩（配置文件被手改的场景）")
for bad_theme in ("Dark", "DARK", "", "none", "深色"):
    try:
        got = get_palette(bad_theme, PROBE)
        # 判据是「不崩，而且落到深色主题上」。**不是** `is DARK`——带了合法
        # accent 时返回的是"深色主题 + 用户的色"这套推导结果，本来就不是
        # 字面量。一开始写成 `is DARK`，把正确的行为判成了失败。
        ok = got.name == "dark" and got.accent == PROBE
    except Exception as exc:                    # noqa: BLE001
        got, ok = "抛异常: %r" % exc, False
    check("主题名=%r + 合法 accent：落到深色主题且保留用户的色" % (bad_theme,),
          ok, getattr(got, "name", got))

print("\n[9] Ctrl+T 换主题时不能把用户挑的强调色丢掉")
st4 = _st()
st4.theme = "dark"
st4.accent_dark = PROBE
st4.accent_light = PROBE
win.apply_settings(st4)
win._toggle_theme()
check("Ctrl+T 之后强调色还在（没退回默认）",
      get_palette(win.settings.theme, S.accent_for(win.settings, win.settings.theme)).accent
      == PROBE,
      get_palette(win.settings.theme,
                  S.accent_for(win.settings, win.settings.theme)).accent)
win._toggle_theme()

# ---------------------------------------------------------------- 10 持久化

print("\n[10] 持久化 round-trip")
path = tmp / "acc.json"
s1 = _st()
s1.theme = "dark"
s1.accent_dark = PROBE
s1.accent_light = "#22cc88"
s1.save_to(path)
s2 = S.AppSettings.load_from(path)
check("accent_dark 存进去读得回来",
      s2 is not None and s2.accent_dark == PROBE,
      getattr(s2, "accent_dark", "?"))
check("accent_light 存进去读得回来",
      s2 is not None and s2.accent_light == "#22cc88",
      getattr(s2, "accent_light", "?"))
check("老配置（没有这两个字段）也能加载，不炸",
      _st().accent_dark == "")

# ---------------------------------------------------------------- 11 元断言

print("\n[11] 元断言：把缺陷装回去，判据必须变红")

# 11a 把推导换成「保 H 保 S 只平移 L」——评审实测它会偏 26/255，而
#     原来那条只钉默认路径的判据对它**完全无感**。所以元断言必须作用在
#     派生路径上。
_orig_hsl = P._hsl
_orig_from = P._from_hsl
_orig_solve = P._solve_soft_lightness
_orig_del = P._delta


def _naive(theme, base, accent):
    """缺陷实现：只平移 L，其余一律不动。"""
    h, s, l = P._hsl(accent)
    lo, hi = P._CLAMP[theme]
    cl = lambda v: min(hi, max(lo, v))
    hover = P._from_hsl(h, s, cl(l * 1.094))
    press = P._from_hsl(h, s, cl(l * 0.88))
    soft = P._from_hsl(h, s, cl(l * 0.5))
    toa = "#0d0f14" if P._rel_lum(accent) > 0.18 else "#ffffff"
    return P.replace(base, accent=accent, accent_hover=hover, accent_press=press,
                     accent_soft=soft, text_on_accent=toa,
                     term_cursor=accent)


_real_derive = P._derive
P._derive = _naive
P._resolve.cache_clear()
try:
    broken = P.get_palette("dark", PROBE)
finally:
    P._derive = _real_derive
    P._resolve.cache_clear()
again = P.get_palette("dark", PROBE)

check("元断言：缺陷实现下的派生值和真实现**不同**"
      "（所以上面那组派生判据不是恒真）",
      (broken.accent_hover, broken.accent_press, broken.accent_soft)
      != (again.accent_hover, again.accent_press, again.accent_soft),
      "%s vs %s" % ((broken.accent_hover, broken.accent_press, broken.accent_soft),
                    (again.accent_hover, again.accent_press, again.accent_soft)))
# **锚点改成 `dp`，不锚 `EXPECTED` 里的字面量。**
#
# 原来这里断言「摘掉缺陷后派生值又回到 `EXPECTED["dark"][...]`」。
# 那个字面量是**一次性的快照**，它跟着 `bg_elevated` 一起漂 ——
# 底色一改它就过期，这条元断言就红，而**状态其实复原得好好的**。
# （今天已经发生过一次：`accent_soft` 的快照从 `#30252d` 变成 `#382c35`，
# 于是这条红了，红的是快照不是实现。）
#
# 现在锚 `dp` —— 也就是**换 `_derive` 之前**捕获的真实现值。
# 这样这条元断言验的是它真正想验的性质：**「装回缺陷 → 摘掉缺陷 →
# 状态复原」**，而与任何配色字面量彻底解耦。
check("元断言：把缺陷摘掉之后，派生值又回到真值（判据可复现、不是一次性）",
      (again.accent_hover, again.accent_press, again.accent_soft)
      == (dp.accent_hover, dp.accent_press, dp.accent_soft),
      "%s vs %s" % ((again.accent_hover, again.accent_press, again.accent_soft),
                    (dp.accent_hover, dp.accent_press, dp.accent_soft)))
# **反向对照**：`_derive` 没换回来时，上面那条必须会红。
# 少了它，「锚点改成 dp」本身就可能变成恒绿 —— 毕竟 `dp` 和 `again`
# 都来自 `get_palette`，万一缓存没清就永远是同一个对象。
_real_derive2 = P._derive
P._derive = _naive
P._resolve.cache_clear()
try:
    _not_restored = P.get_palette("dark", PROBE)
finally:
    P._derive = _real_derive2
    P._resolve.cache_clear()
check("元断言：_derive 没换回来时，上一条确实会红（不是恒绿）",
      (_not_restored.accent_hover, _not_restored.accent_press,
       _not_restored.accent_soft)
      != (dp.accent_hover, dp.accent_press, dp.accent_soft))

# ---------------------------------------------------------------- 收尾

win.apply_settings(_st())
spin(120)
win.close()
host.close()
win.deleteLater()
host.deleteLater()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def _isdeleted(o):
    try:
        return bool(sip.isdeleted(o))
    except RuntimeError:
        return True


check("收尾之后 MainWindow 真的被销毁了（不是只 close）", _isdeleted(win))
check("收尾之后 host 真的被销毁了", _isdeleted(host))

print()
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL " + f)
sys.exit(1 if FAIL else 0)
