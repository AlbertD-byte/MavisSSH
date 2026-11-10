"""判据：标签页右键菜单（关闭当前 / 关闭其他 / 关闭右侧）。

**这个判据量的是「菜单能不能算对」，不是「菜单长什么样」** —— 界面截图
由 `tests/_design_shots.py` 负责，这里管行为。

要点：
  · 关闭动作走**与关闭按钮同一条路**（`tabCloseRequested`）——
    所以欢迎页那个不可关闭的标签在菜单里也是灰的
  · 「关闭其他」「关闭右侧」必须**从大到小**关 —— 关掉一个会让后面的
    下标全部前移，从小到大关会关错
  · 右键「按下与释放」必须在**同一个标签**上，否则会在别的标签上弹菜单
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "windows")

from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: E402

from sshclient.core import settings as S  # noqa: E402
from sshclient.core.snippets import SnippetStore  # noqa: E402
from sshclient.ui import main_window as MW  # noqa: E402
from sshclient.ui.tab_close import NO_CLOSE  # noqa: E402
from tests._teardown import teardown  # noqa: E402

FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print("  [ok] %s" % name)
    else:
        FAIL.append(name)
        print("  [!!] %s  %s" % (name, detail))


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def paint(ms: int = 40) -> None:
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


tmp = pathlib.Path(tempfile.mkdtemp())
st = S.AppSettings()
# **指走落盘位置。** `AppSettings()` 默认指向用户真实的
# `%APPDATA%\MavisSSH\settings.json`，而 MainWindow 里有多处 `save()`
# （切主题、折叠侧栏、断开连接…）—— 建一个 MainWindow 就可能把用户的配置改一遍。
# 判据 `tests/_readme.py` 守着这条，这里是真的漏了。
st._path = tmp / "settings.json"
store = S.SessionStore(tmp / "sessions.json")
mw = MW.MainWindow(settings=st, store=store,
                    snippet_store=SnippetStore(tmp / "snippets.json"))
mw.resize(1200, 700)
mw.show()
paint()

tabs = mw.tabs
ctx = mw._tab_context
bar = tabs.tabBar()

# ---- 造 4 个可关闭的 tab（不连服务器）----
made = []
for i in range(4):
    tab = MW.SessionTab(mw)
    idx = tabs.addTab(tab, "tab-%d" % (i + 1))
    made.append(idx)
paint()

print("=== 基本接线 ===")
check("MainWindow 挂了 TabContextMenu",
      isinstance(getattr(mw, "_tab_context", None), type(ctx)))
check("菜单挂在会话标签栏上（不是子标签栏）",
      ctx._bar is bar)
check("欢迎页不可关闭（NO_CLOSE）",
      tabs.widget(0).property(NO_CLOSE) is True)

print("\n=== 下标判定 ===")
# 第 2 个 tab（索引 2，因为 0 是欢迎页）
rect = bar.tabRect(2)
check("能按坐标找到标签下标",
      ctx._tab_at(rect.center()) == 2,
      "实际 %r" % ctx._tab_at(rect.center()))
check("空白处返回 -1",
      ctx._tab_at(QtCore.QPoint(5, bar.height() + 50)) == -1)

print("\n=== 可关闭性 ===")
check("普通标签可关", ctx._closable(2) is True)
check("欢迎页不可关", ctx._closable(0) is False)

print("\n=== 关闭动作走同一条路 ===")
closed: list[int] = []
tabs.tabCloseRequested.connect(lambda i: closed.append(i))
ctx._close(2)
check("_close 发出的是 tabCloseRequested（与关闭按钮同一条路）",
      closed == [2], str(closed))

print("\n=== 「关闭其他」的下标集合 ===")
# **别写死标签个数。** 这一版先写死「5 个」错、下一版写死「9 个」也错 ——
# 因为上面已经真的关掉过几个标签，个数取决于前面的动作。
# 一律**现算**：先记下当前个数，再加，再按加完之后的数算期望。
_n_before = tabs.count()
for _ in range(4):
    t = MW.SessionTab(mw)
    tabs.addTab(t, "x")
paint()
total = tabs.count()
check("加了 4 个之后标签数 +4",
      total == _n_before + 4,
      "before=%d after=%d" % (_n_before, total))

# 「关闭其他」= 除当前之外全部 —— 但欢迎页不可关，要被排除
others = [i for i in range(total) if i != 2]
has_other = any(ctx._closable(i) for i in others)
check("「关闭其他」可用（有别的可关标签）", has_other is True)

# 「关闭右侧」
right = [i for i in range(3, total) if ctx._closable(i)]
check("「关闭右侧」取的是当前标签右边的",
      right == list(range(3, total)),
      "实际 %r 期望 %r" % (right, list(range(3, total))))

print("\n=== 从大到小关（否则下标会错位）===")
# 用真实路径验证：直接调 _close 会走 tabCloseRequested -> _on_tab_close，
# 它会真的移除标签。这里只验证「下标前移」这件事本身。
to_close = [1, 3, 4]
before = tabs.count()
for i in sorted(to_close, reverse=True):
    ctx._close(i)
    paint()
check("关掉 %d 个之后标签数少了 %d" % (len(to_close), len(to_close)),
      tabs.count() == before - len(to_close),
      "before=%d after=%d" % (before, tabs.count()))

print("\n=== 右键按下与释放必须同一个标签 ===")
# 模拟：按在 tab2，释放在 tab3 —— 不应弹菜单（这里只看下标判定）
r2 = bar.tabRect(2).center()
r3 = bar.tabRect(3).center() if tabs.count() > 3 else r2
check("按下与释放在不同标签时判定为不同下标",
      ctx._tab_at(r2) != ctx._tab_at(r3) or tabs.count() <= 3,
      "r2=%r r3=%r" % (ctx._tab_at(r2), ctx._tab_at(r3)))

# ---------------------------------------------------- 收尾
# 这个脚本是**模块级**跑的：建了 `mw`（MainWindow）和两个 `SessionTab`，
# 然后一路走到文件末尾 `sys.exit()`。这三个对象**从不销毁**，于是它们的
# 寿命比 `QApplication` 还长，进程退出期间只要有一个待触发的回调摸到
# 正在销毁的 C++ 对象，整个进程就是 `0xC0000005`。
#
# **症状极有迷惑性**：判据输出照打，`ALL CHECKS PASSED` 也打印了，
# 只是进程在最后一步死了 —— 门禁只看退出码，于是被记成一条 FAIL，
# 看起来像回归，其实是收尾问题。`tests/_uibtns.py` 踩的是同一个坑。
#
# 自己按正确顺序拆干净：先关再 deleteLater，最后**显式投递 DeferredDelete**。
#
# 注意：**`processEvents()` 不会投递 DeferredDelete** —— Qt 只在「回到与
# 调用 `deleteLater()` 相同的事件循环层级」时才投递它，模块级
# `processEvents()` 不会把排队的事件发出去，销毁根本不会发生。
#
# 收尾范式与「为什么不能用 `processEvents()` 投递 DeferredDelete」记在
# `tests/_teardown.py` 里 —— 那里是这套样板唯一的出处。
teardown(mw, tab, t)

print()
if FAIL:
    print("FAILED %d:" % len(FAIL))
    for f in FAIL:
        print("  - %s" % f)
    sys.exit(1)
print("ALL CHECKS PASSED")
