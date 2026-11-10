"""测试专用：按「第几个会话」去侧栏里取行，而不是按行号。

**为什么需要它。** 侧栏列表里现在有两种行：**分组标题行**和**会话行**。
标题行的 `UserRole` 是 `None`（它是所有「按行取会话」的入口要挡住的东西），
会话行才有 id。

分组功能上线之前列表第 0 行必然是会话，所以判据里到处写
``session_list.item(0)`` 一直是对的。默认分组上线之后，**哪怕一条会话都没
分组，列表最上面也先有一行「默认」标题** —— 于是所有「拿第 0 行当会话」
的判据一起踩空，症状是「会话行是 GroupHeaderItem」「会话行有两行」这种
看着和分组毫无关系的红（一次就红 6 套）。

这些地方要的是「第 N 个会话」，不是「第 N 行」。行号是布局的产物，会话才是
被断言的对象。
"""
from __future__ import annotations

from PyQt6 import QtCore

ROLE = QtCore.Qt.ItemDataRole.UserRole


def session_rows(lst) -> list[int]:
    """所有**会话**行的行号，按列表顺序，跳过分组标题行。"""
    return [i for i in range(lst.count()) if lst.item(i).data(ROLE) is not None]


def session_row(lst, n: int = 0) -> int:
    """第 *n* 个会话行的行号；越界返回 -1。"""
    rows = session_rows(lst)
    return rows[n] if 0 <= n < len(rows) else -1


def session_item(lst, n: int = 0):
    """第 *n* 个会话行的 ``QListWidgetItem``；越界返回 ``None``。"""
    i = session_row(lst, n)
    return lst.item(i) if i >= 0 else None
