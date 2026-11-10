"""弹框自适应：超高时封高度上限 + 正文滚动，标题栏和按钮不滚。

用户报「所有的弹框，需要根据操作系统的分辨率情况做一下自适应（可以出现滚动条），
避免出现展示不全的情况」。

## 根因：弹框高度跟着界面字号缩放，所以这不是小屏特例

QSS 的 padding / min-height / border-radius 全在 `_SCALE_PROPS` 里，
`ui_font_size`（6-48pt）一调，整份 QSS 等比放大。实测（工作区 1032px、上限 992px）：

| 字号 | ConnectDialog | SettingsDialog | _AddDialog |
|------|---------------|----------------|------------|
| 11pt（默认） | **1029px 超出** | 663px | 495px |
| 14pt | 1170px 超出 | 808px | 577px |
| 18pt | 1339px 超出 | 877px | 696px |
| 32pt | 1956px 超出 | 1388px 超出 | 1086px 超出 |
| 48pt（上限） | 2667px 超出 | 1893px 超出 | 1522px 超出 |

**ConnectDialog 在默认字号下就已经超了**，加「分组」之前它是 955px
（离上限还剩 37px），多出来的那一行正好把它吃掉。所以这不是少数配置才遇到。

屏幕一矮更糟：1366x768 的工作区 720px，默认字号下 ConnectDialog 超出约 350px。

**表里这列是「自然高度」，只能在装滚动区之前量。** `showEvent` 里
`fit_to_screen` 会封顶并把正文塞进 QScrollArea，之后 `sizeHint()` 报的是
滚动区自己的尺寸（实测 11pt 的 ConnectDialog 从 1029 变成 476）——
拿那个数填表会把「已经处理好了」写成「本来就不高」。

## 钉什么

1. **弹框高度不超过所在屏的可用高度**（回归不变量）
2. **超高时正文真的有滚动条**（不能只是裁掉 —— 那等于换个地方展示不全）
3. **标题栏和按钮行不跟着滚**：「连接」按钮滚出去比展示不全更糟
4. **不高时不装滚动条**：多一条 10px 槽子是视觉噪声，这个设计语言里没有描边分层
5. 元断言：字号调大 → 出现滚动条；调回默认 → 不出现。证明判据不是恒绿
6. `screen_height_for` 取的是**窗口所在屏**，不是主屏（竖屏副屏工作区 1872）

## 宽度：QMessageBox 照内容算，于是短消息得到一个窄框

上面六条都是高度。宽度是用户另外报的一条，症状是崩溃兜底框：

    正文折成两行、说明文字也折行，右下角「显示详情…」被挤到只剩半个。

**根因不是某一处忘了设宽度，而是 `QMessageBox` 自己按内容算 sizeHint**。
「关于 MavisSSH」框之所以看起来正常，是因为它正文长（实测 372px）——
它从来没有被谁设过宽度。短消息就没有这个运气：

| 弹框 | 修之前实测 | 修之后 |
|------|-----------|--------|
| 关于 | 372 | **400** |
| 崩溃兜底（`KeyboardInterrupt:`） | **242** | **400** |
| 崩溃兜底展开「显示详情…」 | **242** | **400** |
| 输入框（「目录名：」） | **200** | **400** |

下限取 400 = 「关于」框 372 取整，和 `ThemedCheckListDialog` 同一个数。

**「在构造里 `setMinimumWidth` 就完事」是错的，这一点量三次才发现。**
`QMessageBox` 和 `QInputDialog` 内部各有一个 `updateSizeHint()`，它会
`setMinimumSize(0, 0)` 再按内容重算。实测 show 时的 `resizeEvent` 序列：

    showEvent 进入前  400
    super().showEvent → 242   ← Qt 重算
    补钉              400
    …  …  400 -> 242          ← showEvent 返回之后**还有一轮级联**

所以判据一律量 **show 之后、并且跑过一轮事件循环** 的宽度。量构造后的那个
数，把 `_pin_min_width` 整个删掉判据照样绿。

## 刻意不做的（YAGNI）

- **`SnippetPanel`**：420px，48pt 约 780px，仍在上限内，不改。
- **横向超屏**：弹框可缩放，横向封顶会把「主机与端口」那种并排控件压扁。
- **按内容算宽度下限**（而不是常数）：正文标签的 sizeHint 随界面字号缩放，
  按内容算出来的下限也跟着缩放，而那几个字号下真正需要宽度的是正文自己，
  `sizeHint()` 已经把它撑开了。本仓其它弹框也是常数下限（连接框 480、
  设置框 460、转发框 430），这里跟着同一套写法。
- **msgbox 的高度自适应**：内部是 QGridLayout，PyQt6 没暴露 `insertRow`，
  插不进 QScrollArea（`titlebar.pad()` 正是因为这个才改 margins）。
  长文本消息框确实会超高，但那是另一个问题，不在这次范围。
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.core import settings
from sshclient.core.settings import UI_SIZE_REF
from sshclient.ui import connect_dialog, msgbox, settings_dialog, titlebar, tunnel_panel
from sshclient.ui.design.v1 import qss
from sshclient.ui.design.v1.palette import get_palette

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


prim = QtGui.QGuiApplication.primaryScreen()
avail = prim.availableGeometry()
CAP = max(240, avail.height() - titlebar.SCREEN_MARGIN)
print(f"主屏可用区高={avail.height()}  弹框上限={CAP}  默认字号={UI_SIZE_REF}pt")

p = get_palette("light")
tmp = pathlib.Path(tempfile.mkdtemp())
store = settings.SessionStore(tmp / "s.json")
store._items = []


def make(ui, which):
    """按生产路径构造弹框（字号经 qss 模块级状态，和 MainWindow.apply_theme
    做的同一件事）。"""
    st = settings.AppSettings()
    st.theme = "light"
    st.ui_font_size = ui
    st.save = lambda: None
    qss.set_current(p, ui)
    if which == "connect":
        return connect_dialog.ConnectDialog(palette=p)
    if which == "settings":
        return settings_dialog.SettingsDialog(st, p)
    return tunnel_panel._AddDialog(p)


def scroll_area(dlg):
    for a in dlg.findChildren(QtWidgets.QScrollArea):
        if a.objectName() == "dialogScroll":
            return a
    return None


def action_row(dlg):
    """按钮行：正文的兄弟里、标题栏之后的那块容器。"""
    lay = dlg.layout()
    widgets = [lay.itemAt(i).widget() for i in range(lay.count())]
    widgets = [w for w in widgets if w is not None]
    return widgets[-1] if widgets else None


for which, label in (("connect", "新建连接 ConnectDialog"),
                     ("settings", "设置 SettingsDialog"),
                     ("tunnel", "添加转发 _AddDialog")):
    print(f"\n================ {label} ================")

    # ---------------------------------------------------- 低字号：不该滚
    d = make(UI_SIZE_REF, which)
    d.show()
    settle(350)
    print(f"  字号 {UI_SIZE_REF}pt: 实际 {d.height()}px  上限 {CAP}px  "
          f"sizeHint {d.sizeHint().height()}px")
    check("默认字号下高度不超过上限", d.height() <= CAP,
          f"{d.height()} vs {CAP}")
    # **这条原来写死了「默认字号下**没有**滚动条」**，那等于把
    # 「默认字号下装得下」当成了前提。加「分组」之后本机的工作区只剩 37px
    # 余量，多出来的那一行正好吃掉，于是它真的装不下了。
    #
    # 而且**不能拿 `d.height()` 来分支**：封顶之后它永远 ≤ CAP，两支都会
    # 走「装得下」那一支。判据要问的是「装滚动区这件事和内容对不对得上」：
    # 装了就必须真有东西要滚（否则是白装的视觉噪声），没装就必须真的装得下
    # （否则就是漏装、底部被切）。
    # 「一律不装」被这两支夹住（不装且 h == cap → 第一支红）；
    # 「一律装」由下面的 6pt 元断言夹住。
    _a0 = scroll_area(d)
    if _a0 is None:
        check("没装滚动区时，内容确实装得下（不是漏装了）",
              d.height() < CAP, f"h={d.height()} cap={CAP}")
    else:
        check("装了滚动区时，内容确实超出可视区（不是白装的）",
              _a0.widget().sizeHint().height() > _a0.viewport().height(),
              "正文 %d / 可视 %d"
              % (_a0.widget().sizeHint().height(), _a0.viewport().height()))
    # 上限本身要生效，哪怕当前没触发
    check("setMaximumHeight 已生效（模拟矮屏也封得住）",
          d.maximumHeight() == CAP, f"max={d.maximumHeight()} want={CAP}")
    d.close()
    d.deleteLater()
    app.processEvents()

    # ---------------------------------------------------- 高字号：必须滚
    try:
        d = make(48, which)
        d.show()
        settle(400)
    except Exception:
        import traceback
        print("  [48pt 构造/显示抛异常]")
        traceback.print_exc()
        bad.append(f"{label} 48pt 抛异常")
        continue
    a = scroll_area(d)
    print(f"  字号 48pt: 实际 {d.height()}px  上限 {CAP}px  "
          f"sizeHint {d.sizeHint().height()}px  滚动区={'有' if a else '无'}")
    check("48pt 下高度被封在上限内", d.height() <= CAP,
          f"{d.height()} vs {CAP}")
    check("48pt 下正文装进了滚动区（不是裁掉）", a is not None)
    if a is not None:
        # 正文必须**真的能滚**：内容高度大于可视高度
        inner_h = a.widget().sizeHint().height()
        view_h = a.viewport().height()
        print(f"     正文 {inner_h}px / 可视 {view_h}px  "
              f"滚动条可见={a.verticalScrollBar().isVisible()} "
              f"range={a.verticalScrollBar().maximum()}")
        check("正文内容确实超出可视区（滚动条有用武之地）",
              inner_h > view_h, f"{inner_h} vs {view_h}")
        check("垂直滚动条可见", a.verticalScrollBar().isVisible())
        check("水平滚动条**不**出现（正文不能横向乱拖）",
              not a.horizontalScrollBar().isVisible())
        check("横向滚动策略是 AlwaysOff",
              a.horizontalScrollBarPolicy() ==
              QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    # 标题栏和按钮不滚
    tb = d.titlebar
    check("标题栏仍在弹框最顶部，没被正文盖住",
          tb.geometry().top() == 0 and tb.height() == titlebar.TITLEBAR_H,
          f"{tb.geometry().getRect()}")
    check("标题栏可见", tb.isVisible())
    row = action_row(d)
    check("按钮行仍是根布局的最后一个兄弟（不在滚动区里）",
          row is not None and a is not None and row.parentWidget() is d
          and row is not a.widget(),
          f"parent={type(row.parentWidget()).__name__ if row else '-'}")
    # 按钮必须**落在弹框的可视范围内** —— 这是用户主诉的直接目标。
    # 三个弹框的按钮文字不同，统一按「取消/关闭」这一类找。
    # **必须 mapTo(dialog)**：`geometry()` 是相对父控件的，holder 里按钮的
    # y 恒为 0，直接拿它跟标题栏的弹框坐标比会得出「按钮在标题栏上面」这种
    # 看着像 bug 的结论（判据自己踩过一次）。
    acts = [b for b in d.findChildren(QtWidgets.QPushButton)
            if b.text() in ("取消", "关闭")]
    check("弹框的取消/关闭按钮存在", len(acts) >= 1, f"{len(acts)} 个")
    for b in acts:
        ob = b.geometry()
        top = b.mapTo(d, QtCore.QPoint(0, 0)).y()
        bot = top + ob.height()
        check(f"「{b.text()}」完整落在弹框可视范围内（用户点得到）",
              top >= 0 and bot <= d.height() and b.isVisible(),
              f"btn y={top}..{bot} dlg h={d.height()} visible={b.isVisible()}")
        check(f"「{b.text()}」在标题栏下方（标题栏没被压掉）",
              d.titlebar.geometry().bottom() < top,
              f"tb.bottom={d.titlebar.geometry().bottom()} btn.top={top}")
    # 主按钮（连接/添加/确定）同样要点得到
    mains = [b for b in d.findChildren(QtWidgets.QPushButton)
             if b.text() in ("连接", "添加", "确定") and b.isVisible()]
    for b in mains:
        top = b.mapTo(d, QtCore.QPoint(0, 0)).y()
        check(f"主按钮「{b.text()}」在弹框可视范围内",
              0 <= top and top + b.height() <= d.height(),
              f"y={top}..{top + b.height()} dlg h={d.height()}")

    if a is not None:
        check("滚动区装的是原正文控件", a.widget() is d._scroll_body,
              f"{type(a.widget()).__name__}")
    d.close()
    d.deleteLater()
    app.processEvents()

# ------------------------------------------------- 元断言
print("\n================ 元断言 ================")
d = make(6, "connect")
d.show()
settle(300)
check("字号调到 6pt（最小）不装滚动区 —— 证明「有/无滚动条」跟着高度走",
      scroll_area(d) is None, f"h={d.height()}")
d.close()
d.deleteLater()
app.processEvents()

# 元断言：把上限彻底摘掉（连 resize 一起），弹框应该重新涨到远超屏幕
d = make(48, "connect")
d.show()
settle(400)                      # 先让 _clamp_height 排的 singleShot(0) 落地
d.setMaximumHeight(16777215)      # QWIDGETSIZE_MAX
d.resize(d.width(), d.sizeHint().height())
settle(400)
print(f"  摘掉上限并撑到 sizeHint 后：实际 {d.height()}px  "
      f"（上限应为 {CAP}px，sizeHint {d.sizeHint().height()}px）")
check("元断言：摘掉上限后弹框确实超出（判据量的是上限不是别的）",
      d.height() > CAP, f"{d.height()} vs {CAP}")
d.close()
d.deleteLater()
app.processEvents()

# screen_height_for 在真实窗口上
d = make(UI_SIZE_REF, "connect")
d.show()
settle(300)
print(f"\n  screen_height_for(真实弹框) = "
      f"{titlebar.screen_height_for(d)}   上限常量 CAP = {CAP}")
check("screen_height_for 对真实弹框返回的就是工作区高度减余量",
      titlebar.screen_height_for(d) == CAP,
      f"{titlebar.screen_height_for(d)} vs {CAP}")
d.close()
d.deleteLater()
app.processEvents()

# 未映射窗口回退到主屏，不能返回 0 或负数
w = QtWidgets.QWidget()
print(f"  screen_height_for(未映射窗口) = {titlebar.screen_height_for(w)}")
check("screen_height_for 对未映射窗口回退到主屏，不返回 0/负数",
      titlebar.screen_height_for(w) == CAP,
      f"{titlebar.screen_height_for(w)} vs {CAP}")

# =========================================================================
# 宽度
# =========================================================================
print("\n================ 宽度下限：消息框别窄到折行 ================")
FLOOR = msgbox._DIALOG_MIN_W


def width_of(dlg, rounds=2):
    """show 之后、跑过一轮事件循环的**实际宽度**。

    **不能量构造后的 `minimumWidth()`。** Qt 的 `updateSizeHint()` 会在
    show 时把它抹掉（见文件头那段 resizeEvent 序列），量那个数的话，
    把 `_pin_min_width` 整个删掉这一整节照样全绿。
    """
    dlg.show()
    app.processEvents()
    for _ in range(rounds):
        settle(160)
    return dlg.width()


def build_msg(icon, title, text, informative="", detailed=""):
    return msgbox._build(None, icon, title, text, palette=p,
                         informative=informative, detailed=detailed)


# 元断言用的坏实现：假装 Qt 不会覆盖下限（也就是「只在构造里设一次」的写法）
_BAD_STASH = {}


def _break_pin(cls):
    """把 `_pin_min_width` 摘掉，返回还原函数。"""
    _BAD_STASH["pin"] = msgbox._pin_min_width
    msgbox._pin_min_width = lambda d: None
    orig_re = cls.resizeEvent

    def _no_pin_resize(self, event):
        super(cls, self).resizeEvent(event)
    cls.resizeEvent = _no_pin_resize
    return orig_re


def _restore_pin(cls, orig_re):
    msgbox._pin_min_width = _BAD_STASH["pin"]
    cls.resizeEvent = orig_re


def not_wrapped(lab):
    """这个标签有没有被挤到折行；**找不到标签返回 None**。

    **比高度不如比宽度。** 实测同一个 informative 标签：框 400px 时高 36、
    框 242px 时高 52，而 `fm.height()` 是 19 —— 拿高度卡阈值（我先写的是
    `<= height * 2`，即 38）会把**没折行**的那一档也放过去，等于恒绿。

    折行的定义本身就是「可用宽度小于文本自然宽」，直接量这个。
    1px 容差给 Qt 的四舍五入；实测两边差着几十到上百像素，容差取错也
    翻不了盘。

    **找不到就返回 None 而不是 True。** 返回 True 的话，调用方那句
    `r is not False` 会把「标签压根没找到」判成通过 —— 我第一版就写成
    `objectName == "qlabel-text"`，实测那个 objectName 是**空的**，
    于是这条判据一直是空绿。调用方一律用 `is True` 判。
    """
    if lab is None:
        return None
    fm = lab.fontMetrics()
    need = max(fm.horizontalAdvance(ln) for ln in lab.text().split("\n"))
    return lab.width() >= need - 1


# ---- 用户报的那一个：崩溃兜底框
crash = build_msg(QtWidgets.QMessageBox.Icon.Critical, "发生错误",
                  "KeyboardInterrupt: ",
                  informative="完整信息已写入 logs/mavisssh.log",
                  detailed="AttributeError: boom\n" * 4)
w = width_of(crash)
print(f"  崩溃兜底折叠态：{w}px（下限 {FLOOR}）")
check("崩溃兜底框不窄于下限（用户报的就是它）", w >= FLOOR, f"{w} vs {FLOOR}")
for name in ("qt_msgbox_label", "qt_msgbox_informativelabel"):
    lab = crash.findChild(QtWidgets.QLabel, name)
    check(f"「{name}」没有被挤到折行", not_wrapped(lab) is True,
          f"标签{'没找到' if lab is None else '可用宽=%d' % lab.width()}")
# **展开「显示详情…」是最关键的一格**：那一轮 Qt 会重排并再收窄一次，
# 用户截图里被裁掉半个的就是这个按钮。
for b in crash.buttons():
    if "Details" in b.text() or "详细" in b.text():
        b.click()
        break
app.processEvents()
settle(220)
w2 = crash.width()
print(f"  展开「显示详情…」后：{w2}px")
check("展开详情之后仍然不窄于下限（按钮被裁就是这一步）",
      w2 >= FLOOR, f"{w2} vs {FLOOR}")
for b in crash.findChildren(QtWidgets.QPushButton):
    if b.isVisible():
        right = b.mapTo(crash, QtCore.QPoint(0, 0)).x() + b.width()
        check(f"按钮「{b.text()}」完整落在弹框内（右缘 {right} < {crash.width()}）",
              right <= crash.width(), f"right={right} dlg={crash.width()}")
        check(f"按钮「{b.text()}」没有被挤窄到省略文字",
              b.width() >= b.sizeHint().width(),
              f"{b.width()} vs sizeHint {b.sizeHint().width()}")
crash.close()
crash.deleteLater()
app.processEvents()

# ---- 其余消息框
for tag, box in (
    ("短信息", build_msg(QtWidgets.QMessageBox.Icon.Information, "导入连接",
                         "没有导入任何连接。")),
    ("提问", build_msg(QtWidgets.QMessageBox.Icon.Question, "删除",
                       "确定删除「生产-Web-01」吗？")),
    ("关于", build_msg(QtWidgets.QMessageBox.Icon.Information, "关于",
                       "MavisSSH 1.4.1",
                       informative="功能：终端 · 文件传输 · 资源监控")),
):
    w = width_of(box)
    print(f"  {tag}：{w}px")
    check(f"{tag}框不窄于下限", w >= FLOOR, f"{w} vs {FLOOR}")
    box.close()
    box.deleteLater()
    app.processEvents()

# ---- 输入框（用户报的第二处：22 个汉字的标签在 200px 框里放不下）
inp = msgbox.ThemedInputDialog(None, "从远端下载")
inp.setLabelText("远端路径（可空格分隔多个，带空格的用引号）：")
w = width_of(inp)
print(f"  输入框：{w}px")
check("输入框不窄于下限（22 个汉字的标签在原生 200px 里放不下）",
      w >= FLOOR, f"{w} vs {FLOOR}")
# **QInputDialog 的标签没有 objectName**（实测是空串），只能按文字找。
# 找它是因为「有没有折行」这件事判据得挂在那个标签上；按序号取第一个
# QLabel 也行，但那样标题栏文字一改就取错对象 —— 按内容找更耐用。
_long_label = "远端路径（可空格分隔多个，带空格的用引号）："
lab = next((c for c in inp.findChildren(QtWidgets.QLabel)
            if c.text() == _long_label), None)
check("输入框里那个标签找得到（找不到就说明取件器错了，判据要红）",
      lab is not None, "按文字 %r 找" % _long_label)
check("输入框的标签没有被挤到折行", not_wrapped(lab) is True,
      f"可用宽={lab.width() if lab else '-'} 自然宽=286")
# **拉到比下限还窄，会被按回去。** 这是「下限真的是地板」的直接验证：
# 窗口可缩放，用户（或者某个布局错误）就是能把它拽窄的。
inp.resize(FLOOR - 150, inp.height())
app.processEvents()
settle(160)
check("把它拽到比下限还窄，会被按回下限（地板不是摆设）",
      inp.width() >= FLOOR, f"{inp.width()} vs {FLOOR}")
inp.close()
inp.deleteLater()
app.processEvents()

# ---- 别的弹框：确认没漏
print("\n  ---- 顺手确认其余弹框也有下限 ----")


def _settings():
    st = settings.AppSettings()
    st.theme = "light"
    st.save = lambda: None      # 不许判据写用户真实配置
    return st


for tag, mk, want in (
    ("连接框", lambda: connect_dialog.ConnectDialog(palette=p), 480),
    ("设置框", lambda: settings_dialog.SettingsDialog(_settings(), p), 460),
    ("转发框", lambda: tunnel_panel._AddDialog(p), 430),
    ("勾选框", lambda: msgbox.ThemedCheckListDialog(
        None, "导入连接", "选择来源：", ["A（1 条）"], palette=p), FLOOR),
):
    d = mk()
    w = width_of(d)
    print(f"  {tag}：{w}px（它自己的下限 {want}）")
    check(f"{tag}没有被消息框的下限改动（仍按自己的 {want} 走）",
          d.minimumWidth() == want or w >= want,
          f"minW={d.minimumWidth()} 实宽={w} want={want}")
    d.close()
    d.deleteLater()
    app.processEvents()

# ---- 元断言：把补钉摘掉，上面必须红
print("\n  ---- 元断言 ----")
# **别把循环变量也叫 `bad`。** 这个文件顶层的 `bad` 是失败列表，
# 一旦被弹框对象遮蔽，最后那句 `len(bad)` 抛的是
# `TypeError: object of type 'ThemedMessageBox' has no len()` ——
# 整套判据在「打印结论」那一步崩掉，看起来像环境坏了。
_orig_re = _break_pin(msgbox.ThemedMessageBox)
try:
    unpinned = build_msg(QtWidgets.QMessageBox.Icon.Critical, "发生错误",
                         "KeyboardInterrupt: ",
                         informative="完整信息已写入 logs/mavisssh.log",
                         detailed="boom\n" * 4)
    w = width_of(unpinned)
    print(f"  摘掉补钉后：{w}px")
    check("元断言：摘掉补钉之后「崩溃兜底不窄于下限」会红",
          w < FLOOR, f"{w} vs {FLOOR}")
    for b in unpinned.buttons():
        if "Details" in b.text() or "详细" in b.text():
            b.click()
            break
    app.processEvents()
    settle(200)
    check("元断言：摘掉补钉之后「展开详情后仍不窄于下限」也会红",
          unpinned.width() < FLOOR, f"{unpinned.width()} vs {FLOOR}")
    # 「不折行」那条也要证明自己不是恒绿：摘掉下限之后**必须**折行，
    # 否则 `not_wrapped()` 就是个装饰。
    lab = unpinned.findChild(QtWidgets.QLabel, "qt_msgbox_informativelabel")
    check("元断言：摘掉补钉之后 informative 确实折行了（所以上面那条不是恒绿）",
          not_wrapped(lab) is False,
          f"可用宽={lab.width() if lab else '-'}")
    unpinned.close()
    unpinned.deleteLater()
finally:
    _restore_pin(msgbox.ThemedMessageBox, _orig_re)
app.processEvents()

# 对照：还原之后必须回绿，否则上面那些红是环境问题不是被测问题
ok = build_msg(QtWidgets.QMessageBox.Icon.Critical, "发生错误",
               "KeyboardInterrupt: ",
               informative="完整信息已写入 logs/mavisssh.log",
               detailed="boom\n" * 4)
w = width_of(ok)
check("元断言：还原之后回到下限（证明上面那两条红是被测代码造成的）",
      w >= FLOOR, f"{w} vs {FLOOR}")
ok.close()
ok.deleteLater()
app.processEvents()

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b_ in bad:
        print("  -", b_)
else:
    print("dialog fit to screen: all checks passed")
sys.exit(1 if bad else 0)
