"""结构守卫：界面上**每个**按钮 / 输入框都得接上、都得说得清自己是谁。

`tests/_live.py` 会在真服务器上把按钮一个一个点过去，那是**行为**；
这份脚本管的是**结构**，而且必须进 `_runall.py` —— 行为测试依赖外部服务，
网络一抖就没法跑，结构回归不能跟着一起消失。

它钉住三条：

  1. **没有死按钮。** 任何可点控件都必须在它自己的主信号上挂了至少一个槽。
     没挂的那个在界面上和别的按钮长得一模一样，用户点了毫无反应，而
     没有一条日志会提到它。
  2. **图标按钮必须带 tooltip。** 只有图标的按钮，用户能认出它的唯一线索
     就是悬停提示。没提示 = 一排猜谜。
  3. **同父级下不许有两个图标按钮共用同一个 tooltip。** 那说明有俩按钮
     用户根本分不清，而它们大概率干着不同的事。

覆盖面：MainWindow、SessionTab（离线能建出来的那些面板）、SnippetPanel、
TunnelPanel、两个对话框。**SFTP 面板的文件列表按钮不在这里** —— 它要一个真的
`SFTPClient` 才建得起来，那部分由 `tests/_live.py` 覆盖，本文件在结尾把
实际清点到的控件数打出来，让人知道漏了什么。
"""
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtWidgets

from sshclient.core import settings as S
from sshclient.core.snippets import SnippetStore
from sshclient.ui import connect_dialog, settings_dialog, tunnel_panel
from sshclient.ui import main_window as MW
from sshclient.ui.design.v1.palette import get_palette
from sshclient.ui.snippet_panel import SnippetPanel
from tests._teardown import teardown

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

BUTTONS = (QtWidgets.QPushButton, QtWidgets.QToolButton)
TOGGLES = (QtWidgets.QCheckBox, QtWidgets.QRadioButton)
COMBOS = (QtWidgets.QComboBox, QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)
INPUTS = (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit)

# Qt 自己造的部件不算：标签页的左右滚动钮（父级是 QTabBar）、
# QLineEdit 的内置清除钮（父级就是那行编辑框）。它们没有文字也没有提示，
# 但那是 Qt 的内部实现，不是我们漏写的。
def _is_qt_internal(w) -> bool:
    if (w.objectName() or "").startswith("qt_"):
        return True
    return isinstance(w.parent(),
                      (QtWidgets.QTabBar, QtWidgets.QLineEdit))


def _primary_signal(w):
    if isinstance(w, QtWidgets.QComboBox):
        return "currentIndexChanged"
    if isinstance(w, TOGGLES):
        return "toggled"
    if isinstance(w, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
        return "valueChanged"
    return "clicked"


def walk(root):
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        for c in w.children():
            if isinstance(c, QtWidgets.QWidget):
                out.append(c)
                stack.append(c)
    return out


def controls(root):
    hits = []
    for c in walk(root):
        if _is_qt_internal(c):
            continue
        if isinstance(c, BUTTONS + TOGGLES + COMBOS + INPUTS):
            hits.append(c)
    return hits


def text_of(w):
    try:
        return w.text() or ""
    except Exception:
        return ""


def tip_of(w):
    try:
        return w.toolTip() or ""
    except Exception:
        return ""


def collect(roots):
    btns, inputs = [], []
    for label, root in roots:
        if root is None:
            continue
        for c in controls(root):
            if isinstance(c, INPUTS):
                inputs.append((label, c))
            else:
                btns.append((label, c))
    return btns, inputs


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="uibtns_"))
    store = S.SessionStore(tmp / "sessions.json", config_dir=tmp)
    # 同 _titlebar.py：别读用户真实的 settings.json
    settings = S.AppSettings()
    settings._path = tmp / "settings.json"

    mw = MW.MainWindow(settings=settings, store=store,
                       snippet_store=SnippetStore(tmp / "snippets.json"))
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    tab = MW.SessionTab()
    lay.addWidget(tab)
    host.show()
    for _ in range(5):
        app.processEvents()

    pal = get_palette(settings.theme)
    sftp_store = SnippetStore(tmp / "snippets2.json")
    panel = SnippetPanel(sftp_store, lambda b: True, parent=None)
    panel.apply_palette(pal)
    class _FakeConn:
        """隧道面板只用到这几个方法，全是纯内存的。"""

        def set_tunnel_callbacks(self, on_error=None, on_event=None):
            pass

        def add_tunnel(self, spec, enabled=True):
            return spec

        def set_tunnel_enabled(self, local_port, enabled=True):
            return enabled

        def remove_tunnel(self, local_port):
            return True

        def tunnel_specs(self):
            return []

        def tunnel_states(self):
            return {}

        def tunnel_connections(self):
            return {}

    tunnels = tunnel_panel.TunnelPanel(_FakeConn(), pal)

    conn_dlg = connect_dialog.ConnectDialog(palette=pal)
    set_dlg = settings_dialog.SettingsDialog(settings, pal)

    btns, inputs = collect([
        ("MainWindow", mw), ("SessionTab", tab), ("SnippetPanel", panel),
        ("TunnelPanel", tunnels), ("ConnectDialog", conn_dlg),
        ("SettingsDialog", set_dlg),
    ])

    print("清点到：%d 个可点控件，%d 个输入框"
          % (len(btns), len(inputs)))
    print("  可点：" + "，".join(sorted({l for l, _ in btns})))
    print()

    # ---------------------------------------------------- 1 没有死按钮
    # **只管按钮。** SpinBox / ComboBox / CheckBox 的值是在对话框被接受时
    # 轮询读取的（见 ConnectDialog.result_dict），它们**本来就不该**接信号 ——
    # 逼着它们接 `valueChanged` 只会写出「有人改了值就顺手做点什么」这种
    # 没有调用方的槽。把没接线的 QComboBox 当缺陷，是把「值控件」和
    # 「事件源」搞混了。图标按钮另算：它们连文字都没有，提示就是全部。
    dead = []
    for label, c in btns:
        if not isinstance(c, BUTTONS):
            continue
        try:
            n = c.receivers(c.clicked)
        except Exception as exc:
            n = "?" + str(exc)
        if n == 0:
            dead.append("%s/%s %s" % (label, type(c).__name__, tip_of(c)))
    check("没有没接线的按钮（每个按钮的 clicked 上都挂了槽）", not dead,
          "; ".join(dead))

    # ---------------------------------------------------- 2 图标按钮要有提示
    icon_no_tip = ["%s/%s" % (label, type(c).__name__)
                   for label, c in btns
                   if isinstance(c, BUTTONS)
                   and not text_of(c).strip() and not tip_of(c).strip()]
    check("没有文字的按钮都带 tooltip（用户认出它的唯一线索）",
          not icon_no_tip, "; ".join(icon_no_tip))

    # ---------------------------------------------------- 3 不许撞提示
    by_parent = {}
    for label, c in btns:
        if text_of(c).strip():
            continue
        key = (label, id(c.parent()))
        by_parent.setdefault(key, []).append((c, tip_of(c)))
    dupes = []
    for (label, _pid), items in by_parent.items():
        seen = {}
        for c, tip in items:
            if not tip:
                continue
            if tip in seen:
                dupes.append("%s: %r 出现两次（%s / %s）"
                             % (label, tip, type(seen[tip]).__name__,
                                type(c).__name__))
            else:
                seen[tip] = c
    check("同父级下没有两个图标按钮共用同一个 tooltip", not dupes,
          "; ".join(dupes))

    # ---------------------------------------------------- 4 输入框可编辑且有交代
    ro = ["%s/%s" % (label, type(c).__name__) for label, c in inputs
          if c.isReadOnly()]
    check("没有只读的输入框（只读的那些是结果区，不是输入区）", not ro,
          "; ".join(ro))

    unlabelled = ["%s/%s" % (label, type(c).__name__) for label, c in inputs
                  if not (text_of(c).strip() or c.placeholderText().strip())]
    check("每个输入框都有文字或占位提示（不让人猜这是什么框）",
          not unlabelled, "; ".join(unlabelled))

    # ---------------------------------------------------- 5 元断言
    # 装一个**故意没接线**的按钮，上面的判据必须抓到它。抓不到就说明
    # 第 1 条是恒真的。
    trap = QtWidgets.QPushButton("没接线的诱饵")
    host.layout().addWidget(trap)
    btns2, _ = collect([("Trap", host)])
    still_dead = [c for _, c in btns2
                  if c.receivers(getattr(c, _primary_signal(c))) == 0]
    check("元断言：装一个没接线的按钮之后，第 1 条判据**确实**会红",
          any(type(c).__name__ == "QPushButton" for c in still_dead),
          str([type(c).__name__ for c in still_dead]))
    host.layout().removeWidget(trap)
    trap.deleteLater()

    # ---------------------------------------------------- 6 收尾
    # 这份脚本一次建了 7 个顶层 widget（MainWindow / host / tab / 三个面板 /
    # 两个对话框）。主函数一返回它们就被 Python 引用计数丢掉，随后由 Qt 在
    # **进程退出期**析构 —— 而析构顺序我们控制不了：MainWindow 与它内部的
    # 会话管理互相持有引用，随机顺序下就会踩到已销毁的 C++ 对象。
    #
    # 症状非常有迷惑性：**判据输出「通过 6 项，失败 0 项」，退出码却是
    # 0xC0000005（访问冲突）**，而且 stderr 一片空白 —— 崩在所有判据跑完
    # 之后、进程收尾那一刻。所以不能靠「判据全绿」当通过。
    #
    # 修法是自己按正确顺序拆干净：先关对话框和面板（它们是独立顶层窗），
    # 再关 host（连带它里面的 tab），最后关 MainWindow；然后显式投递
    # DeferredDelete 把排队中的 deleteLater 真正做完。
    #
    # 收尾范式与「为什么不能用 `processEvents()` 投递 DeferredDelete」记在
    # `tests/_teardown.py` 里 —— 那里是这套样板唯一的出处。
    teardown(mw, tab, host, panel, tunnels, conn_dlg, set_dlg)

    print()
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
