"""进程退出前的收尾样板。

## 为什么需要它

PyQt6 的顶层 widget 如果活到解释器退出，就会在**进程退出期**被 Qt 析构 ——
而那个析构顺序我们控制不了。只要有一个待触发的回调（定时器、信号、绘制）
摸到正在销毁的 C++ 对象，整个进程就是 `0xC0000005`（access violation）。

**症状极有迷惑性**：判据输出照打，`RESULT: ALL CHECKS PASSED` 也打印了，
只是进程在最后一步死了。门禁只看退出码，于是它被记成一条 FAIL ——
**看起来像回归，其实是收尾问题**。

实测踩过的（都是本仓库自己的套件）：

- `tests/_uibtns.py` —— 一次建 7 个顶层 widget（MainWindow / host / tab /
  三个面板 / 两个对话框），从不销毁。修之前 3 次跑崩 2 次。
- `tests/_ftabcontext.py` —— 模块级建 MainWindow + 两个 SessionTab。

`docs/ARCHITECTURE.md` 的「测试脚本不收尾 = 退出时偶发 0xC0000005」一节
记的是同一件事的更早版本（`_tabwire.py` / `_tabclose.py` 各 30 次崩 1 次）。

## 正确写法

```python
from tests._teardown import teardown
...
teardown(mw, tab, panel)
```

`teardown()` 做三件事，**缺一不可**：

1. `close()` —— 只发 `closeEvent`，**不回收**；
2. `deleteLater()` —— 排队销毁；
3. `sendPostedEvents(None, QEvent.Type.DeferredDelete)` —— **真正投递**
   排队中的销毁。

**第 3 步不能用 `processEvents()` 代替。** Qt 只在「回到与调用
`deleteLater()` 相同的事件循环层级」时才投递 DeferredDelete；顶层
`processEvents()` 不会把排队的事件发出去，销毁根本不会发生。
（`tests/_accent.py` 结尾有同一套样板，那是它的历史教训。）

**另一条纪律**：收尾之后要**验证对象真的没了**（`sip.isdeleted()`），
而不是写「跑了 N 次没崩」——后者只在这一轮成立，而且 0/30 对 1/30 在
统计上根本不显著。
"""
from __future__ import annotations

from typing import Any


def teardown(*widgets: Any) -> int:
    """按正确顺序拆掉给定的顶层 widget。

    返回成功投递销毁的 widget 数量。已经在别处被销毁的（`RuntimeError`）
    会被跳过 —— 判据跑到最后时，有的 widget 可能已被产品的
    `deleteLater()` 干掉，再 `deleteLater()` 一次会抛。

    顺序：**先全部 close，再全部 deleteLater，最后统一投递。**
    分两轮是有原因的 —— 如果边 close 边 deleteLater，后一个 widget 的
    close 可能摸到前一个已经排队销毁的对象。
    """
    from PyQt6 import QtCore

    alive = []
    for w in widgets:
        if w is None:
            continue
        try:
            w.close()
        except RuntimeError:
            continue          # 已经没了
        alive.append(w)

    for w in alive:
        try:
            w.deleteLater()
        except RuntimeError:
            pass               # 已经没了

    # **不是 `processEvents()`** —— 它不投递 DeferredDelete。
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    return len(alive)


def is_destroyed(obj: Any) -> bool:
    """对象是不是真的被销毁了。

    **不能用 `obj is None` 判** —— PyQt6 里被 `deleteLater()` 掉的 widget
    仍然是个 Python 对象，只是它的 C++ 侧没了。判据必须用
    `sip.isdeleted()`，否则「销毁了」这条根本测不到。
    """
    try:
        import sip
    except ImportError:          # pragma: no cover - PySide6 兼容位
        import shiboken6 as sip
    try:
        return bool(sip.isdeleted(obj))
    except RuntimeError:
        return True
