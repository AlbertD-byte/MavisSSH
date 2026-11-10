"""判据：同一个界面里，**并排的交互控件必须等高**。

## 这个 bug 怎么来的

2026-10 一轮 UI 审计里，同一个连接框里有**六档**控件高度：

    QPushButton（QSS padding 7px 14px）      33
    QTabBar::tab（padding 8px 18px）          35
    QLineEdit（padding 8px 11px）            37
    QSpinBox                                38
    转发框底部按钮（setFixedHeight 34）        34
    连接框底部按钮（setFixedHeight 38）        38

连接框里「添加规则 / 删除」（33）和「取消 / 连接」（38）**并排**，这是
肉眼第一眼就能看见的不齐 —— 比任何配色问题都扎眼。

统一到 `qss.CONTROL_H = 34` 之后仍然剩两个暗坑，都不是「写个数字」能修的：

1. **`QSpinBox` 38px。** 它内部有个真·`QLineEdit` 子控件
   （objectName 固定 `qt_spinbox_lineedit`），会吃到 inputs 那条规则的
   `min-height`，于是 spinbox 得再套一层自己的描边。
   **QSS 里没有任何选择器够得着它** —— 实测 `QSpinBox QLineEdit`、
   `QSpinBox > *`、`QLineEdit#qt_spinbox_lineedit`、`[objectName=...]`
   四种写法全部无效（那个内部 LE 的 `parentWidget()` 是空的，
   后代匹配走不通）。所以只能从 Python 侧定高，见 `qss.compact_spinbox`。
2. **`accentSwatch` 40px。** QSS 的 `min-height` 量的是**内容盒**，
   上下 padding 和描边额外加上去 —— 声明写 30、实测 40，**差 10px，
   而结果才是用户看见的**。同类规则还有页签（漏减那 2px 描边，实测 36）。

## 钉什么

**跨度 ≤ 2px**（不是「全部严格相等」）：描边 1px vs 2px 这类差异会让
相邻控件差 1-2px，那是控件类型本身的差异，不是回归。定在 2 是因为
**bug 现场最小的一档差是 4px**（38 vs 34 的 spinbox），2 的门槛抓得住
每一次真正的回归，又不会因为将来加一像素描边就无故红。

## 元断言

临时把某个 `QSpinBox` 的 `compact_spinbox` 摘掉，跨度必须真的超过门槛。
不写这条的话，判据只能证明「现在齐」，改实现的人把 `compact_spinbox`
删了判据仍然绿 —— 而用户看到的就是这次的 bug。

跑法::

    .\\.venv\\Scripts\\python.exe tests\\_uidensity.py
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core import settings
from sshclient.ui import connect_dialog, settings_dialog, tunnel_panel
from sshclient.ui.design.v1 import qss
from sshclient.ui.design.v1.palette import get_palette

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

#: 会被判据盯着的「可交互控件」。刻意不含 QToolButton —— 图标按钮
#: 有自己的一套尺寸规格（16px 图标 + 固定 padding），不算进表单密度。
INTERACTIVE = (QtWidgets.QPushButton, QtWidgets.QLineEdit,
               QtWidgets.QComboBox, QtWidgets.QSpinBox)

#: 允许的跨度。理由见上面「钉什么」。
TOLERANCE = 2

#: 三档字号都要过：只有 11pt 下齐，18pt 一缩放就又不齐了 ——
#: 而缩放恰恰是 `min-height` / `compact_spinbox` 要解决的问题。
UI_SIZES = (11, 18, 24)

DIALOGS = (
    ("ConnectDialog",
     lambda: connect_dialog.ConnectDialog(palette=get_palette("light"))),
    ("SettingsDialog",
     lambda: settings_dialog.SettingsDialog(_ST, get_palette("light"))),
    ("_AddDialog",
     lambda: tunnel_panel._AddDialog(get_palette("light"))),
)

bad = []


def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}"
          + (f"  {detail}" if detail else ""))
    if not ok:
        bad.append(f"{name}  {detail}")


def settle(ms=350):
    """等布局落定。

    **为什么不能只靠固定时长。** 原来这里是「死等 350ms」，实测在回归
    批量跑（同一解释器里前面刚跑完 `_socks5` / `_tabh` 等也改
    `qss._CURRENT` 的判据）之后，会出现**一次** `18pt ConnectDialog`
    报 `52×1 54×11 56×1`（跨度 4）的偶发红 —— 而单独连跑 3 次、
    按回归顺序复跑都是全绿，且 ConnectDialog 里根本不存在 56px 的控件
    （逐个量过：两个 QSpinBox 是 54/52，`socks_listen` 是 54）。

    也就是说那次红是**布局还没算完就量了**：控件还带着上一档字号的
    geometry，`findChildren` 一次性扫到的自然不是一个自洽的快照。

    所以改成「等到布局不再变化为止」，并且**必须有真实控件动过**才收工
    —— 否则空转 350ms 就返回，等于没改。
    """
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()
    # 再等布局稳定：连续两轮 `activate` + 高度不变才算落定
    prev = None
    for _ in range(12):
        for w in app.topLevelWidgets():
            lay = w.layout()
            if lay is not None:
                lay.activate()
        app.processEvents()
        cur = tuple(sorted(
            c.height() for w in app.topLevelWidgets()
            for c in w.findChildren(QtWidgets.QWidget)
            if isinstance(c, INTERACTIVE) and c.isVisible()))
        if cur == prev:
            return
        prev = cur


def heights_of(dlg):
    """可见交互控件 -> {高度: [控件名]}。"""
    out = {}
    for c in dlg.findChildren(QtWidgets.QWidget):
        if not isinstance(c, INTERACTIVE) or not c.isVisible():
            continue
        if not c.geometry().height():
            continue
        if isinstance(c, QtWidgets.QPushButton):
            label = c.text()
            if not label:                 # 图标按钮有自己的规格，跳过
                continue
        else:
            label = type(c).__name__
        out.setdefault(c.height(), []).append(label)
    return out


def spread(hs):
    vals = sorted(hs)
    return (vals[-1] - vals[0]) if len(vals) > 1 else 0


def fmt(hs):
    return "  ".join(f"{h}px×{len(hs[h])}" for h in sorted(hs))


tmp = pathlib.Path(tempfile.mkdtemp())
store = settings.SessionStore(tmp / "s.json")
store._items = []
_ST = settings.AppSettings()
_ST.theme = "light"
_ST.save = lambda: None

print("=" * 70)
print(f"交互控件密度（判据：同一界面内高度跨度 ≤ {TOLERANCE}px，"
      f"CONTROL_H={qss.CONTROL_H}）")
for ui in UI_SIZES:
    _ST.ui_font_size = ui
    qss.set_current(get_palette("light"), ui)
    print(f"\n--- 界面字号 {ui}pt（期望高度 ≈ {qss.scaled_control_h(ui)}px）---")
    for name, mk in DIALOGS:
        d = mk()
        d.show()
        settle()
        hs = heights_of(d)
        if not hs:
            check(f"{ui}pt {name} 有可见交互控件", False, "一个都没测到")
            d.close()
            continue
        check(f"{ui}pt {name} 高度跨度 ≤ {TOLERANCE}px",
              spread(hs) <= TOLERANCE, f"{fmt(hs)}  跨度={spread(hs)}px")
        d.close()
        d.deleteLater()
        app.processEvents()

# ---------------------------------------------------------------- 元断言
print("\n" + "=" * 70)
print("元断言：摘掉 compact_spinbox，跨度必须真的超标")
_ST.ui_font_size = 11
qss.set_current(get_palette("light"), 11)
dlg = tunnel_panel._AddDialog(get_palette("light"))
dlg.show()
settle()
spins = [s for s in dlg.findChildren(QtWidgets.QSpinBox) if s.isVisible()]
check("元断言前提：转发框里至少有两个可见 QSpinBox", len(spins) >= 2,
      f"实测 {len(spins)} 个")
for s in spins:
    s.setMinimumHeight(0)
    s.setMaximumHeight(16777215)      # 解除 compact_spinbox 的定高
for s in spins:
    s.updateGeometry()
dlg.layout().activate()
settle()
hs = heights_of(dlg)
check("摘掉定高后跨度超标（证明判据抓得住回归）", spread(hs) > TOLERANCE,
      f"{fmt(hs)}  跨度={spread(hs)}px")
dlg.close()
dlg.deleteLater()
app.processEvents()

print()
if bad:
    print(f"FAIL ({len(bad)}):")
    for b in bad:
        print("  -", b)
else:
    print("control density: all checks passed")
sys.exit(1 if bad else 0)
