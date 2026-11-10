"""从工作线程把结果安全地投回主线程。

**为什么需要它。** ``QMetaObject.invokeMethod(obj, ...)`` 要求 ``obj`` 是个
**活着的** C++ 对象。工作线程在飞的时候（一次 SSH exec 最多 10 秒），
用户可能关掉标签、关掉窗口 —— 那个 widget 连带被 ``deleteLater()`` 销毁。
worker 醒来再投一次，就撞上：

    RuntimeError: wrapped C/C++ object of type MonitorPanel has been deleted

**这不是「偶尔」的事。** 关标签是常规操作，而监控面板的轮询间隔是 2 秒、
一次 exec 超时 10 秒 —— 关得早了就必撞。

**做法。** 投之前先看对象还在不在：

- ``sip.isdeleted(obj)`` —— C++ 对象是否已被析构；
- 再套一层 ``try/except RuntimeError`` —— 挡住「查完之后、投递之前」这段
  窗口里的析构（几微秒，但存在）。

**两处都用同一个函数**，免得各写一份、其中一份漏掉。

**为什么用 ``sip`` 而不是别的办法。** 本项目已经在
``sftp_panel.py`` / ``snippet_panel.py`` 里用 ``sip.isdeleted()`` 做同一件事
（防 use-after-free），沿用同一套心智。
"""
from __future__ import annotations

from PyQt6 import QtCore, sip


def is_alive(obj) -> bool:
    """这个 Qt 对象还在吗。

    True = Python 包装器还在 **且** C++ 对象没被析构。
    """
    try:
        return not sip.isdeleted(obj)
    except TypeError:
        # 不是 sip 包装的对象（纯 Python 对象）—— 它没有 C++ 生命周期问题。
        return True


def post(obj, method: str, *qargs) -> bool:
    """把一次方法调用排队到 *obj* 所在线程；对象没了就安静地放弃。

    *qargs* —— 传给 ``QMetaObject.invokeMethod`` 的 ``Q_ARG(...)`` 列表。

    返回 True = 投递成功；False = 对象已销毁，这次调用被丢弃。

    **返回 False 是正常路径，不是错误。** 调用方不该因为「面板被关了」
    而报错 —— 那正是用户想要的结果。
    """
    if not is_alive(obj):
        return False
    try:
        QtCore.QMetaObject.invokeMethod(
            obj, method, QtCore.Qt.ConnectionType.QueuedConnection, *qargs)
        return True
    except RuntimeError:
        # 「查完之后、投递之前」那一瞬间被析构了。
        return False
