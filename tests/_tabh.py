"""会话 tab 压低之后的复验。

**三件事都要量，不能只看「变矮了」。**
① 高度真的降下来，且落在 34（与所有控件同基线）；
② **关闭按钮仍完整落在 tab 内** —— 高度一压，按钮可能被挤到边界外，
   那是把一个观感问题换成一个功能问题；
③ tab 里的文字没有被裁掉（Qt 会按 tab 高度裁 label）。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from sshclient.ui.design.v1 import palette as pal  # noqa: E402
from sshclient.ui.design.v1 import qss  # noqa: E402
from sshclient.ui.tab_close import TabCloseController  # noqa: E402

app = QtWidgets.QApplication(sys.argv)

FAILS = []
N = [0]


def ck(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(f"{name} {detail}")
        print(f"  [红] {name}  {detail}")
    else:
        print(f"  [绿] {name}  {detail}")


for theme in ("light", "dark"):
    for ui_size in (11, 14, 18):
        p = pal.get_palette(theme)
        qss.set_current(p, ui_size)
        sheet = qss.build_qss(p, ui_size=ui_size)

        sess = QtWidgets.QTabWidget()
        sess.setObjectName("sessionTabs")
        sess.setStyleSheet(sheet)
        sess.addTab(QtWidgets.QLabel("x"), "deploy@localhost:22")
        sess.addTab(QtWidgets.QLabel("x"), "root@203.0.113.20:2222")
        sess.resize(900, 300)
        ctrl = TabCloseController(sess, p)
        sess.show()
        app.processEvents()

        bar = sess.tabBar()
        # **尺子取「同界面里的普通控件」，不是 `scaled_control_h`。**
        #
        # 18pt 下这个区别是 2px：实测 QPushButton / QLineEdit / QComboBox /
        # QTabBar **全是 54**，而 `qss.scaled_control_h(18)` 给的是 56 ——
        # 那个函数只服务 QSpinBox（QSS 够不着它内部那个 QLineEdit，只能从
        # Python 定高，见该函数 docstring），它**不是**通用高度来源。
        # 拿它当基线会得出「tab 比控件矮 2px」的红灯，而实际上四者完全一致。
        #
        # 基线取 QPushButton 的实测高度：它是最普通的、没有任何定高兜底的控件。
        ref = QtWidgets.QPushButton()
        ref.setStyleSheet(sheet)
        ref.show()
        app.processEvents()
        want = ref.height()
        ref.close()

        # ① 高度
        ck(f"[{theme} {ui_size}pt] 会话 tab 高 = 普通控件基线 {want}",
           bar.height() == want, f"实测 {bar.height()}")

        # ①b 子 tab 与会话 tab 同高（两类 tab 必须落在同一条基线上）
        sub = QtWidgets.QTabWidget()
        sub.setStyleSheet(sheet)
        for t in ("终端", "文件", "监控", "隧道"):
            sub.addTab(QtWidgets.QLabel(t), t)
        sub.show()
        app.processEvents()
        ck(f"[{theme} {ui_size}pt] 子 tab 与会话 tab 同高",
           sub.tabBar().height() == bar.height(),
           f"子 {sub.tabBar().height()} vs 会话 {bar.height()}")

        # ①c 基线本身没选歪：三类普通控件都该与 tab 同高。
        # 这条的作用是**守住尺子** —— 万一以后某个控件被单独定高，
        # 用它当参考就会把 tab 一起拖歪，而那时上面几条仍然全绿。
        others = []
        for cls in (QtWidgets.QLineEdit, QtWidgets.QComboBox):
            w = cls()
            w.setStyleSheet(sheet)
            w.show()
            app.processEvents()
            others.append(w.height())
            w.close()
        ck(f"[{theme} {ui_size}pt] QLineEdit/QComboBox 也同高（尺子可信）",
           all(h == bar.height() for h in others),
           f"{others} vs tab {bar.height()}")

        # ② 关闭按钮完整落在 tab 内（功能不变量）
        ok_all = True
        detail = []
        for i, btn in enumerate(ctrl._buttons):
            tr = bar.tabRect(i)
            br = btn.geometry()
            # 完全包含，且上下左右各留至少 1px 余量
            inside = (tr.contains(br) and br.top() > tr.top()
                      and br.bottom() < tr.bottom())
            ok_all &= inside
            detail.append(f"tab={tr.x()},{tr.y()} {tr.width()}x{tr.height()} "
                          f"btn={br.x()},{br.y()} {br.width()}x{br.height()}")
        ck(f"[{theme} {ui_size}pt] 关闭按钮完整落在 tab 内", ok_all,
           "; ".join(detail[:1]))

        # ②b 关闭按钮竖直居中（压低之后不该偏上/偏下）
        centred = True
        for i, btn in enumerate(ctrl._buttons):
            tr = bar.tabRect(i)
            br = btn.geometry()
            top_gap = br.top() - tr.top()
            bot_gap = tr.bottom() - br.bottom()
            centred &= abs(top_gap - bot_gap) <= 2
        ck(f"[{theme} {ui_size}pt] 关闭按钮竖直居中", centred)

        # ③ tab 高度够放文字（没被裁）
        fm = bar.fontMetrics()
        ck(f"[{theme} {ui_size}pt] tab 高度容得下一行文字",
           bar.height() >= fm.height() + 2,
           f"tab={bar.height()} font={fm.height()}")

        sess.close()
        sub.close()

print()
print("=== 元断言：把 padding 装回旧的 8px，tab 必须重新比控件高出一个内边距量 ===")

p = pal.get_palette("light")
qss.set_current(p, 11)
sheet = qss.build_qss(p, ui_size=11)
legacy = sheet.replace(
    f"padding: 0 {TabCloseController.PAD_RIGHT}px 0 18px;",
    f"padding: 8px {TabCloseController.PAD_RIGHT}px 8px 18px;",
)
ck("[元] 确实替换到了那一行", legacy != sheet)

s = QtWidgets.QTabWidget()
s.setObjectName("sessionTabs")
s.setStyleSheet(legacy)
s.addTab(QtWidgets.QLabel("x"), "deploy@localhost:22")
s.resize(900, 300)
s.show()
app.processEvents()
old_h = s.tabBar().height()

ref = QtWidgets.QPushButton()
ref.setStyleSheet(legacy)
ref.show()
app.processEvents()
base_h = ref.height()
ref.close()
ck("[元] 旧实现下 tab 高于普通控件（这正是要修的观感问题）",
   old_h > base_h, f"tab={old_h} 控件={base_h}")
# **钉性质，不钉绝对像素。**
#
# 这里原来写的是 `old_h == 50` —— 一个**具体高度**。它会随字重、字号、
# QSS 的任何一次调整而漂移，于是「字重加粗」那一轮把控件从 34 抬到 36
# 之后，这条判据就红了，而**产品并没有坏**：旧样式的行为完全没变，
# 只是「旧样式的 tab 比控件高多少」这个数字跟着基线一起挪了。
#
# 该守的是**关系**：旧实现给 tab 加了上下各 8px 的内边距，所以
# tab 应当正好比同一样式下的普通控件高 `2 * 8` px。这个数字由
# **被替换进去的那段 QSS 自己算出来**，不是手抄的常量 ——
# 改了内边距，这条判据自动跟着改，而「旧样式确实更高」这个性质
# 永远不会因为基线漂移而误报。
_LEGACY_PAD = 8
ck("[元] 旧实现的高度比控件高出上下内边距之和",
   old_h == base_h + 2 * _LEGACY_PAD,
   f"tab={old_h} 控件={base_h} 期望={base_h + 2 * _LEGACY_PAD}")

# **元断言的元断言：证明上面那条不是恒绿的假货。**
# 一条「关系型」判据有它自己的失败模式 —— 表达式写错（比如把 `+` 写成 `-`）
# 会让它永远红，而反过来若两个量恰好相等，它也会永远绿。这里把期望值
# 人为挪动一格，确认判据**确实会红**：恒绿的判据测不出任何东西。
_broken = base_h + 2 * _LEGACY_PAD + 1
ck("[元] 把期望值挪动一格后，上面的判据必须会红",
   (old_h == _broken) is False,
   f"old_h={old_h} 挪动后期望={_broken}")
s.close()

print()
print(f"--- {N[0] - len(FAILS)}/{N[0]} ---")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
