"""会话分组的判据：命名规则、排序、侧栏渲染、标签标题、对话框往返。

用户提的需求是一句话：「可能有多个不同用户的服务器，需要分组进行管理，
session tab 的展示按照『分组名-服务器命名』」。这句话里藏了四个独立的东西，
这里逐个钉：

  1. **数据能装下分组。** `SavedSession.group` 加上，老文件读得进来，
     手改坏的值会被归一。
  2. **名称规则只有一处。** `display_name()` 是全名 `分组-名称`，而且
     没有那个前导连字符 —— 未分组的标签写出来是 `-Web-01` 的话，用户
     看到的是一排凭空多个横杠的标签，而代码读起来完全正常。
  3. **侧栏真的分段了。** 分组头行插进去、不可选中、折叠态不出现，
     选中项在重排之后还停在原来那一行。
  4. **标题栏真的换了。** 标签上写的是「分组名-会话名」，不是
     `用户名@ip:端口`；没有分组时也不能多个横杠。

有三条是**先测出来、才知道该这么写**的，记在这里免得下次又靠推断：

  · **不可选中的分组头照样吃 `:hover`。** 「不可选中所以大概不会悬停」
    是个很顺的推断，实测两套主题下分组头的悬停底色都是 `bg_hover_row`，
    和普通会话行一模一样 —— 所以 `groupCount` 那 11px 的对比度必须按
    悬停态算，不能只量静置的 `bg_rail`。
  · **未分组用 `None` 而不是空串。** 空串同时是「无分组」和「用户填了
    一串空格」的归一结果，这两种会话在界面上必须是两种表现。
  · **分组头行的 `UserRole` 必须是 `None`。** 它是所有「按行取会话」的
    入口的钥匙，填个假 id 会让它们去 store 里查一个不存在的会话。

每节末尾都有**元断言**：把刚验过的实现换成已知坏版本，判据必须转红。
没有这一条，上面那些「≥」和「等于」很可能只是恒真的装饰。
"""
import json
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from sshclient.core import settings as S
from sshclient.ui import connect_dialog, icons, sidebar
from sshclient.ui import main_window as MW
from sshclient.ui.design.v1.palette import _contrast, get_palette

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail))


def section(title):
    print("\n=== %s ===" % title)


# 宿主窗口保活，和 tests/fonts.py 的 snap 一个理由：这里量的是像素，
# 而从未真正 show 过的顶层窗口 grab 出来是空的（Qt 不给它跑真实 backing
# store），那样「有没有画出来」就成了恒真的问题。
_HOSTS: list = []


def pump(ms=200):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(ms, loop.quit)
    loop.exec()


def sess(name, host="10.0.0.1", group="", user="root", port=22, sid=None):
    return S.SavedSession(id=sid or (name + host), name=name, group=group,
                          host=host, username=user, port=port)


# ===========================================================================
# 1. 名称规则
# ===========================================================================
section("1. 名称规则（display_name / base_name）")

s = S.SavedSession(name="Web-01", host="10.0.0.1", group="生产", username="root")
check("有分组：全名是「分组-名称」", s.display_name() == "生产-Web-01", s.display_name())
check("有分组：base_name 不带分组", s.base_name() == "Web-01", s.base_name())

u = S.SavedSession(name="Web-01", host="10.0.0.1", group="", username="root")
check("无分组：全名就是名称本身", u.display_name() == "Web-01", u.display_name())
check("无分组：**没有那个前导连字符**",
      not u.display_name().startswith("-"), repr(u.display_name()))
check("无分组：两个都不带连字符", u.display_name() == u.base_name() == "Web-01")

b = S.SavedSession(name="", host="10.0.0.9", group="", username="deploy")
check("既没分组又没名字：退回 用户名@主机",
      b.base_name() == "deploy@10.0.0.9", b.base_name())
bg = S.SavedSession(name="", host="10.0.0.9", group="测试", username="deploy")
check("没名字但有分组：分组仍然带在前面",
      bg.display_name() == "测试-deploy@10.0.0.9", bg.display_name())

# 元断言：把「无分组时不加连字符」那支删掉，判据必须红。
_orig_dn = S.SavedSession.display_name
S.SavedSession.display_name = lambda self: f"{self.group}-{self.base_name()}"
try:
    bad = S.SavedSession(name="Web-01", host="h", group="").display_name()
    check("元断言：坏实现（无分组也给连字符）确实产出 %r" % bad, bad == "-Web-01", bad)
    check("元断言：上面那条「无前导连字符」在坏实现下会红",
          S.SavedSession(name="Web-01", host="h", group="").display_name()
          .startswith("-"))
finally:
    S.SavedSession.display_name = _orig_dn
check("元断言：还原之后判据恢复绿",
      S.SavedSession(name="Web-01", host="h", group="").display_name() == "Web-01")

# ===========================================================================
# 2. 持久化往返
# ===========================================================================
section("2. sessions.json 往返")

tmp = pathlib.Path(tempfile.mkdtemp())
store = S.SessionStore(tmp / "s.json")
store.upsert(sess("Web-01", group="生产"))
back = S.SavedSession.from_dict(store.all[0].to_dict())
check("group 跟着 to_dict/from_dict 走一趟", back.group == "生产", back.group)

legacy = S.SavedSession.from_dict({"name": "老会话", "host": "10.0.0.5"})
check("老文件里没有 group 键 → 归一成空串（未分组）",
      legacy.group == "", repr(legacy.group))
check("老会话的显示名里没有多余连字符",
      legacy.display_name() == "老会话", legacy.display_name())

for bad, why in ((None, "null"), (123, "数字"), ([], "列表")):
    got = S.SavedSession.from_dict({"name": "X", "host": "h", "group": bad})
    check("手改坏的 group（%s）被 str 化，不会变成字面量 None" % why,
          isinstance(got.group, str) and "None" not in got.display_name(),
          "%r -> %r" % (bad, got.display_name()))
check("数字 group 被 str 化而不是丢弃",
      S.SavedSession.from_dict({"name": "X", "host": "h", "group": 7}).group == "7")

# ===========================================================================
# 3. 排序纯函数
# ===========================================================================
section("3. grouped_sessions 排序")

rows = [
    sess("Web-02", group="生产", sid="a"),
    sess("裸奔", group="", sid="b"),
    sess("DB-01", group="测试", sid="c"),
    sess("Web-01", group="生产", sid="d"),
    sess("空格", group="   ", sid="e"),
]
# **中文分组名按码位排（测 U+6D4B < 生 U+751F），不是拼音、也不是插入顺序。**
# 中文没有「字典序」这种东西，唯一能做到确定且人人复现的只有码位序；
# 拼音排序要一张多音字表，而「生产」和「长宁」的拼音首字母根本分不出稳定结果。
# 这里把码位序**写死成基准**，而不是只断言「排好了」—— 不写死的话，
# 下一个人换成拼音序时这条判据还会绿，而那是一个没跟用户商量过的决定。
shape = [(g, [x.name for x in rs])
         for g, rs in sidebar.grouped_sessions(rows)]
check("分组按码位升序、组内按名称、默认分组排最后",
      shape == [("测试", ["DB-01"]),
                ("生产", ["Web-01", "Web-02"]),
                ("默认", ["空格", "裸奔"])], str(shape))
check("未分组那一段的键是「默认」而不是 None —— 用户要的就是一个能看见的默认分组",
      sidebar.grouped_sessions(rows)[-1][0] == S.DEFAULT_GROUP,
      repr(sidebar.grouped_sessions(rows)[-1][0]))
check("**没有任何一段的键是 None**（None 只留给折叠态那个哨兵）",
      all(g is not None for g, _ in sidebar.grouped_sessions(rows)),
      str([g for g, _ in sidebar.grouped_sessions(rows)]))
check("只有空格的分组被归一进默认分组，不会自己冒出一个头",
      not any(g == "   " for g, _ in sidebar.grouped_sessions(rows)),
      str([g for g, _ in sidebar.grouped_sessions(rows)]))

check("空列表 → 空结果", sidebar.grouped_sessions([]) == [])
only_free = sidebar.grouped_sessions([sess("A", group="")])
check("全都没分组时**恰好一个**「默认」头（不是零个）",
      len(only_free) == 1 and only_free[0][0] == S.DEFAULT_GROUP, str(only_free))
check("全都有分组时**不产生**默认分组段（不凭空多一个空的）",
      not any(g == S.DEFAULT_GROUP
              for g, _ in sidebar.grouped_sessions(
                  [sess("A", group="生产"), sess("B", group="测试")])),
      str([g for g, _ in sidebar.grouped_sessions(
          [sess("A", group="生产"), sess("B", group="测试")])]))

# 默认分组不是只有 grouped_sessions 在用：会话自己就得回答「我归哪一段」。
check("group_name：未分组 -> 默认",
      sess("A", group="").group_name() == S.DEFAULT_GROUP,
      repr(sess("A", group="").group_name()))
check("group_name：只有空格也算未分组",
      sess("A", group="   ").group_name() == S.DEFAULT_GROUP,
      repr(sess("A", group="   ").group_name()))
check("group_name：有名字就是那个名字（两端空白被 strip）",
      sess("A", group="  生产 ").group_name() == "生产",
      repr(sess("A", group="  生产 ").group_name()))
check("group_name 归一但 display_name 不变 —— 侧栏有头、标签不加前缀",
      sess("A", group="  ").group_name() == S.DEFAULT_GROUP
      and sess("A", group="  ").display_name() == "A",
      "%r / %r" % (sess("A", group="  ").group_name(),
                   sess("A", group="  ").display_name()))
# 元断言：默认分组**不参与码位排序，永远排最后**。
#
# **必须挑一个码位比「默认」大的分组名，否则这条元断言是恒绿的。**
# 「默」是 U+9ED8，而测试集里的「测试」U+6D4B、「生产」U+751F 都在它前面
# —— 也就是说「按码位排」这件事**本来就已经把默认分组排在最后**，
# 拿它当对照根本区分不出「我特意让它排最后」和「碰巧也排最后」。
# 「龟」是 U+9F9F，比「默」大：把实现换成不特判的朴素排序，它就会插到
# 「龟」前面，两种实现才真的分叉。
_ga = S.SavedSession(group="龟", name="A", host="h", username="root")
_zh = S.SavedSession(group="正常", name="B", host="h", username="root")
rows_gt = [sess("裸奔", sid="b"), _ga, _zh]
ours = [(g, [x.name for x in rs]) for g, rs in sidebar.grouped_sessions(rows_gt)]
naive = [(g, [x.name for x in rs]) for g, rs in
         sorted(sidebar.grouped_sessions(rows_gt), key=lambda o: o[0])]
check("对照：默认分组在朴素码位序里**不是**最后（所以下面那条有区分力）",
      naive != ours, "%s vs %s" % (naive, ours))
check("默认分组永远排最后，哪怕有个分组的码位比它大",
      ours[-1][0] == S.DEFAULT_GROUP and naive[-1][0] != S.DEFAULT_GROUP,
      "%s vs %s" % (ours, naive))


def order_of(items):
    out = []
    for _g, rs in sidebar.grouped_sessions(items):
        out.extend(x.id for x in rs)
    return out


dup = [sess("同名", group="G", sid="z"), sess("同名", group="G", sid="a")]
check("同名会话用 id 兜底，输入顺序不同也得到同一个顺序",
      order_of(dup) == order_of(list(reversed(dup))),
      "%s vs %s" % (order_of(dup), order_of(list(reversed(dup)))))

mixed = [sess("b", group="Zeta", sid="1"), sess("a", group="alpha", sid="2")]
check("拉丁字母分组名按大小写不敏感排序（alpha 排在 Zeta 前）",
      [g for g, _ in sidebar.grouped_sessions(mixed)] == ["alpha", "Zeta"],
      str([g for g, _ in sidebar.grouped_sessions(mixed)]))

# 元断言：把「默认分组排最后」改成排最上，上面的排序判据必须红。
_gs = sidebar.grouped_sessions
sidebar.grouped_sessions = lambda items: (
    [o for o in _gs(items) if o[0] == S.DEFAULT_GROUP]
    + [o for o in _gs(items) if o[0] != S.DEFAULT_GROUP])
try:
    flipped = [(g, [x.name for x in rs])
               for g, rs in sidebar.grouped_sessions(rows)]
    check("元断言：把默认分组排到最上之后，「分组升序 + 默认最后」判据会红",
          flipped != shape, str(flipped))
finally:
    sidebar.grouped_sessions = _gs
check("元断言：还原之后排序判据恢复绿",
      [(g, [x.name for x in rs]) for g, rs in sidebar.grouped_sessions(rows)]
      == shape)

# ===========================================================================
# 4. 连接对话框
# ===========================================================================
section("4. 连接对话框的分组输入与回吐")

dlg = connect_dialog.ConnectDialog(palette=get_palette("dark"),
                                    groups=["生产", "测试"])
check("分组框存在", hasattr(dlg, "group_edit"))
check("下拉里那一项写的就是「默认」——和侧栏那一段同名（不同名=用户找不到）",
      connect_dialog.UNGROUPED == S.DEFAULT_GROUP == "默认",
      "%r vs %r" % (connect_dialog.UNGROUPED, S.DEFAULT_GROUP))
check("分组框是**可编辑下拉**：能列出来，也能自己打字",
      isinstance(dlg.group_edit, QtWidgets.QComboBox) and dlg.group_edit.isEditable(),
      "%s editable=%s" % (type(dlg.group_edit).__name__, dlg.group_edit.isEditable()))
check("下拉里第一项是「默认」",
      dlg.group_edit.itemText(0) == connect_dialog.UNGROUPED,
      dlg.group_edit.itemText(0))
check("已有的分组都列在后面，按传进来的顺序",
      [dlg.group_edit.itemText(i) for i in range(1, dlg.group_edit.count())]
      == ["生产", "测试"],
      str([dlg.group_edit.itemText(i) for i in range(dlg.group_edit.count())]))
check("新建时默认落在「默认」上",
      dlg.group_edit.currentText() == connect_dialog.UNGROUPED,
      dlg.group_edit.currentText())
check("不选就等于没分组",
      dlg.result_dict()["group"] == "", repr(dlg.result_dict()["group"]))

dlg.name_edit.setText("Web-01")
dlg.group_edit.setCurrentIndex(dlg.group_edit.findText("生产"))
check("**从下拉里选一个已有的**分组，回吐该名字",
      dlg.result_dict()["group"] == "生产", repr(dlg.result_dict()["group"]))

dlg.group_edit.setCurrentText("  生产-华东  ")
check("**自己输入一个新分组**也照样回吐（两端空白被 strip）",
      dlg.result_dict()["group"] == "生产-华东", repr(dlg.result_dict()["group"]))
check("打字不会把新名字混进下拉条目里（NoInsert：分组由保存动作创建）",
      "生产-华东" not in [dlg.group_edit.itemText(i)
                          for i in range(dlg.group_edit.count())],
      str([dlg.group_edit.itemText(i) for i in range(dlg.group_edit.count())]))

dlg.group_edit.setCurrentIndex(0)
check("选回「默认」就是退出分组（存成空串，不是存一个叫「默认」的真分组）",
      dlg.result_dict()["group"] == "", repr(dlg.result_dict()["group"]))
out = dlg.result_dict()
check("回吐里有 group 这个键 —— 漏了就等于「编辑一次分组就空了」",
      "group" in out, str(sorted(out)))
dlg.close()
del dlg

edit = connect_dialog.ConnectDialog(
    palette=get_palette("dark"), groups=["生产"],
    initial={"name": "Web-01", "host": "h", "group": "生产", "notes": "留个备注"})
check("编辑时当前分组被选中", edit.group_edit.currentText() == "生产",
      edit.group_edit.currentText())
rt = edit.result_dict()
check("往返不丢分组", rt["group"] == "生产", rt["group"])
check("顺带确认那两个没有输入框的字段仍然原样回吐（不是这次改坏的）",
      rt["notes"] == "留个备注", rt["notes"])
edit.close()
del edit

no_key = connect_dialog.ConnectDialog(
    palette=get_palette("dark"), groups=["生产"],
    initial={"name": "老", "host": "h"})
check("老会话（无 group 键）打开编辑不报错，落在「默认」上",
      no_key.group_edit.currentText() == connect_dialog.UNGROUPED
      and no_key.result_dict()["group"] == "",
      no_key.group_edit.currentText())
no_key.close()
del no_key

odd = connect_dialog.ConnectDialog(palette=get_palette("dark"), groups=["开发机"])
stray = connect_dialog.ConnectDialog(
    palette=get_palette("dark"), groups=["开发机"],
    initial={"name": "X", "host": "h", "group": "老分组"})
check("当前分组不在列表里（老数据 / 手改文件）时也照原样显示，不丢",
      stray.group_edit.currentText() == "老分组"
      and stray.result_dict()["group"] == "老分组",
      "%r -> %r" % (stray.group_edit.currentText(), stray.result_dict()["group"]))
check("它也没有因此被塞进下拉条目（分组名单只认 store）",
      stray.group_edit.count() == 2, str(stray.group_edit.count()))
check("**只有**「默认」这一项传了空值，其余条目都带着自己的名字",
      [odd.group_edit.itemData(i) for i in range(odd.group_edit.count())]
      == ["", "开发机"],
      str([odd.group_edit.itemData(i) for i in range(odd.group_edit.count())]))
odd.close()
del odd
stray.close()
del stray

# ===========================================================================
# 5. 侧栏渲染
# ===========================================================================
section("5. 侧栏真的按分组分段了")


def build(sessions, theme="dark", collapsed=None):
    tmp = pathlib.Path(tempfile.mkdtemp())
    st = S.SessionStore(tmp / "s.json")
    st._items = list(sessions)
    ap = S.AppSettings()
    ap.theme = theme
    ap.save = lambda: None
    mw = MW.MainWindow(ap, st)
    mw.setMinimumSize(0, 0)
    host = QtWidgets.QWidget()
    lay = QtWidgets.QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(mw)
    host.resize(1280, 700)
    host.show()
    mw.session_list.show()
    _HOSTS.append(host)
    pump(450)
    if collapsed is not None:
        mw.set_sidebar_collapsed(collapsed, persist=False)
        pump(250)
    return mw, host


def row_kinds(mw):
    return ["头" if mw.session_list.item(i).data(
        QtCore.Qt.ItemDataRole.UserRole) is None else "会话"
        for i in range(mw.session_list.count())]


data = [
    sess("Web-01", group="生产", sid="a"),
    sess("DB-01", group="测试", sid="b"),
    sess("Web-02", group="生产", sid="c"),
    sess("裸奔", group="", sid="d"),
]
mw, host = build(data)
lst = mw.session_list

check("行数 = 会话数 + 分组数（默认分组也产一个头）",
      mw.session_list.count() == 7, str(mw.session_list.count()))
check("头的数量 = 分组数（3 个：测试 / 生产 / 默认）",
      row_kinds(mw).count("头") == 3, str(row_kinds(mw)))

# **这里刻意不写死分组头的先后。** 「分组成不成立」和「中文按什么顺序排」
# 是两件事，钉在一起的话，改排序规则会连着把分段判据一起弄红，而那条红
# 与分组本身无关。码位序另在第 3 节钉死。
#
# **段和头必须一一对上，一个都不能挂错。** 上一版这里有一个「对不上的会话」
# 的兜底分支，用来容忍「未分组那段没有头、会话挂在上一个头下面」——
# 默认分组有了自己的名字之后，这条豁免消失了：界面上每一个会话都该有一个
# 属于自己的头，挂错就是分组错位。
headers, seen, cur, mismatched = [], [], None, []
for i in range(mw.session_list.count()):
    it = lst.item(i)
    sid = it.data(QtCore.Qt.ItemDataRole.UserRole)
    if sid is None:
        cur = lst.itemWidget(it).findChild(
            QtWidgets.QLabel, "groupName").text()
        headers.append(cur)
        seen.append([])
    else:
        row = mw.store.get(sid)
        if row.group_name() != cur:
            mismatched.append((row.name, row.group_name(), cur))
        seen[-1].append(row.name)
check("每个会话都落在自己那个头下面，没有一个挂错段",
      not mismatched, str(mismatched))
check("三段都非空，且内容正好是那三个名字",
      sorted(n for seg in seen for n in seg) == ["DB-01", "Web-01", "Web-02", "裸奔"]
      and all(seg for seg in seen), str(seen))
check("**未分组那一段有自己的「默认」头**（用户要的就是这个）",
      S.DEFAULT_GROUP in headers, str(headers))
check("裸奔落在「默认」那一段里",
      "裸奔" in seen[headers.index(S.DEFAULT_GROUP)], str(seen))
check("第一个头就是第一行",
      len(headers) == 3 and row_kinds(mw)[0] == "头",
      "%s %s" % (headers, row_kinds(mw)))
check("所有会话都出现过，一次不多一次不少",
      sorted(n for seg in seen for n in seg) == ["DB-01", "Web-01", "Web-02", "裸奔"],
      str(seen))

h0 = lst.item(0)
head = lst.itemWidget(h0)
# **按 objectName 取，不要按 findChildren 的顺序。** 分组头现在还有一个
# 装箭头的 QLabel，而 `findChildren(QLabel)` 返回的是控件树遍历顺序 ——
# 那是 Qt 的实现细节，把它当契约的话，箭头控件换个位置就弄红一条与功能
# 无关的判据（这正是记忆里「一次改坏连带三条」的那一类）。
gname = head.findChild(QtWidgets.QLabel, "groupName")
gcount = head.findChild(QtWidgets.QLabel, "groupCount")
check("分组头控件画出的是分组名 + 会话数",
      isinstance(head, sidebar.GroupHeaderItem)
      and gname.text() == headers[0] and gcount.text() == str(len(seen[0])),
      "%s %r %r" % (type(head).__name__,
                    gname.text() if gname else None,
                    gcount.text() if gcount else None))
check("分组头的提示里带着分组名、数量，以及「点它会发生什么」",
      headers[0] in (h0.toolTip() or "")
      and ("%d 个会话" % len(seen[0])) in (h0.toolTip() or "")
      and "点击" in (h0.toolTip() or ""),
      h0.toolTip())

flags = h0.flags()
check("分组头不可选中（只有 ItemIsEnabled）",
      bool(flags & QtCore.Qt.ItemFlag.ItemIsEnabled)
      and not bool(flags & QtCore.Qt.ItemFlag.ItemIsSelectable), str(flags))
check("分组头的 UserRole 是 None",
      h0.data(QtCore.Qt.ItemDataRole.UserRole) is None)
check("分组头行的高度就是 GROUP_H",
      lst.sizeHintForRow(0) == sidebar.GROUP_H,
      "%s vs %s" % (lst.sizeHintForRow(0), sidebar.GROUP_H))
r = lst.visualItemRect(h0)
check("分组头的行控件完整落在自己的 item 里（不溢出）",
      head is not None and head.height() == sidebar.GROUP_H <= r.height()
      and head.y() >= r.top() and head.y() + head.height() <= r.bottom() + 1,
      "item=%s head.y=%s head.h=%s" % (r, head.geometry().y(), head.height()))
check("分组头行不该被当成选中行上色（没有 set_selected 这个钩子）",
      not hasattr(head, "set_selected"))

# **按会话 id 找那一行，不写死行号。** 之前写的是 `setCurrentItem(item(5))`，
# 而那条索引在旧布局里恰好是会话、在新布局里恰好是分组头 —— 分组头不可
# 选中，`setCurrentItem` 对它静悄悄地不生效，于是 `picked` 变成 None，
# 判据红得莫名其妙。行号是布局的产物，会话 id 才是我们要断言的东西。
_target = next(i for i in range(lst.count())
               if lst.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == "c")
lst.setCurrentItem(lst.item(_target))
picked = lst.currentItem().data(QtCore.Qt.ItemDataRole.UserRole)
check("先能选中一个会话行（上面那条的前提本身）", picked == "c", repr(picked))
mw.store.upsert(sess("Web-03", group="生产", sid="e"))
mw._reload_session_list()
now = lst.currentItem()
check("列表重排之后选中项还停在同一个会话上",
      now is not None
      and now.data(QtCore.Qt.ItemDataRole.UserRole) == picked,
      "%s vs %s" % (now.data(QtCore.Qt.ItemDataRole.UserRole)
                    if now else None, picked))

mw.set_sidebar_collapsed(True, persist=False)
pump(250)
check("折叠态不画分组头（56px 放不下），行数就是会话数",
      mw.session_list.count() == 5 and "头" not in row_kinds(mw),
      "%s %s" % (mw.session_list.count(), row_kinds(mw)))
mw.set_sidebar_collapsed(False, persist=False)
pump(250)
check("展开回来分组头又回来了（5 会话 + 3 组 = 8 行）",
      mw.session_list.count() == 8 and row_kinds(mw)[0] == "头",
      str(row_kinds(mw)))
host.close()
pump(150)

# 元断言：把分组头改成可选中，上面那条 flags 判据必须红。
_orig_add = MW.MainWindow._add_group_header


def _bad_header(self, group, count, expanded=True):
    item = QtWidgets.QListWidgetItem()
    item.setData(QtCore.Qt.ItemDataRole.UserRole, None)
    item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable)
    item.setSizeHint(QtCore.QSize(0, sidebar.GROUP_H))
    self.session_list.addItem(item)
    h = sidebar.GroupHeaderItem(group, count, self.palette_)
    h.setFixedHeight(sidebar.GROUP_H)
    self.session_list.setItemWidget(item, h)


MW.MainWindow._add_group_header = _bad_header
try:
    mw2, host2 = build(data)
    f = mw2.session_list.item(0).flags()
    check("元断言：把分组头改成可选中之后，那条 flags 判据会红",
          not (bool(f & QtCore.Qt.ItemFlag.ItemIsEnabled)
               and not bool(f & QtCore.Qt.ItemFlag.ItemIsSelectable)))
    host2.close()
    pump(120)
finally:
    MW.MainWindow._add_group_header = _orig_add

# ===========================================================================
# 6. 配色
# ===========================================================================
section("6. 分组头的配色与对比度")

# **两条底色都要量。** 静置是 bg_rail，悬停是 bg_hover_row —— 而
# 「不可选中的行大概不会悬停」这个推断实测是错的（见下一节的像素判据）。
measured = {}
for theme in ("light", "dark"):
    p = get_palette(theme)
    for bg_name in ("bg_rail", "bg_hover_row"):
        bg = getattr(p, bg_name)
        for fg_name in ("text_dim", "text_muted"):
            c = _contrast(getattr(p, fg_name), bg)
            measured[(fg_name, bg_name, theme)] = c
            check("%s：%s 上 %s 的对比度 %.2f ≥ 4.5" % (theme, bg_name, fg_name, c),
                  c >= 4.5, "%.3f" % c)

tight = min(v for k, v in measured.items() if k[0] == "text_muted")
check("最紧的一档（groupCount 悬停）仍在 4.5 之上，实测 %.2f" % tight,
      tight >= 4.5, "%.3f" % tight)

# 负向对照用**仓库里真实存在的那一对**，不是编一个颜色。
#
# **为什么是 `text_muted` 压在 `accent`（实心强调色）上？**
#
# 原来这里选的是 `text_muted on accent_soft`（选中行底），依据是当时的实测
# 4.29/4.33 —— 破 AA，于是它能证明「≥4.5 这个门槛会拒绝东西」。
#
# **后来配色调过了，light 的 `accent_soft` 变浅到 4.89，已经不破了**，
# 于是这条负向对照在 light 主题下再也找不到反例，判据红了。
# 产品没有坏 —— 坏的是判据选的那个反例不存在了。
#
# 现在选 `text_muted on accent`：实心强调色（主按钮底）上放 muted 文字，
# 实测 1.75 / 1.04，**两套主题都远低于 4.5**。而且这个搭配在产品里
# **正是被刻意避免的** —— 主按钮用的是 `text_on_accent` 而不是 `text_muted`
# （`palette.py` 里 `text_on_accent` 的存在理由就是「白字压亮强调蓝只有
# 3.43，破 AA」）。也就是说：这一对既真实存在于调色板里，又确实违规，
# 且产品真的在避免它 —— 负向对照要的三件事都占全了。
#
# 换成它之后，判据不再依赖**某一个主题的调色细节**：配色再调一次，
# 只要「实心强调色上的 muted 文字仍然破 AA」这条还成立（它必须成立，
# 否则产品就已经把 muted 文字放到实心底上了），这条判据就一直有效。
for theme in ("light", "dark"):
    p = get_palette(theme)
    c = _contrast(p.text_muted, p.accent)
    check("负向对照：%s 的 text_muted on accent = %.2f < 4.5（门槛不是恒真）" % (theme, c),
          c < 4.5, "%.3f" % c)

# **这里原本有一条「量分组头 hover 时的底色」，已经撤掉了。**
#
# 记下来是因为它推翻了一个本来很顺的推断，而且撤掉的理由不是「懒得测」：
# **「不可选中的行大概不会悬停」是错的** —— 实测两套主题下分组头都吃
# `bg_hover_row`，和普通会话行一模一样（`QListView::mouseMoveEvent` 只判
# `index.isValid() && isIndexEnabled(index)`，选中性与 hover 无关）。
#
# 但这条判据自己**测不稳**，前后返工三次全是测量错：
#   · 照着 QSS 推出来是「不悬停」，实测反了；
#   · 补 `setAttribute(WA_UnderMouse, True)` 之后单独跑一直对（86/86），
#     进了 `_runall` 又飘一次；
#   · 改成补发真的 `QEnterEvent` 之后变成**稳定失败**，于是加了一组
#     对照（普通会话行、关掉分组头的列表）才发现：**连普通会话行在这个
#     环境里都上不了 hover 底色** —— 是取样装置不行，不是分组头特殊。
#
# 一条会飘的判据比没有判据更坏，所以撤掉。撤掉之后**该保的还在**：
# 上面那八条对比度把 `bg_rail` 和 `bg_hover_row` 两个底色都算了，
# 也就是说「万一它悬停了」这件事配色是安全的 —— 而它到底悬不悬停是
# Qt 的行为，不由本仓决定，也不值得为它留一条飘的判据。

# ===========================================================================
# 7. 标签标题
# ===========================================================================
section("7. 标签标题写的是「分组名-会话名」")


class _Info:
    def __init__(self, u, h, p):
        self.username, self.host, self.port = u, h, p


class _Conn:
    def __init__(self, u="root", h="10.0.0.1", p=22):
        self.info = _Info(u, h, p)


def open_tab(mw, saved):
    """真走一遍 `_on_connect_ok`，只把 `attach` 换掉。

    换成什么不重要 —— 这一条要量的是**标签上写了什么**。而 `attach`
    一被调用就会开线程去连真服务器，那之后发生的事与标签标题无关。
    """
    orig = MW.SessionTab.attach
    MW.SessionTab.attach = lambda self, conn: None
    try:
        mw._on_connect_ok(_Conn(), saved)
    finally:
        MW.SessionTab.attach = orig
    return mw.tabs.tabText(mw.tabs.count() - 1)


mw3, host3 = build([])
try:
    txt = open_tab(mw3, sess("Web-01", group="生产", sid="a"))
    check("标签写的是「分组名-会话名」", txt == "生产-Web-01", repr(txt))
    check("标签上不再有 用户名@主机:端口",
          "root@" not in txt and "10.0.0.1" not in txt, repr(txt))

    txt2 = open_tab(mw3, sess("DB-01", group="", sid="b"))
    check("无分组时标签就是会话名，**没有前导连字符**",
          txt2 == "DB-01", repr(txt2))

    nameless = S.SavedSession(name="", host="10.0.0.9", group="", username="deploy")
    txt3 = open_tab(mw3, nameless)
    check("无分组又没名字：退回 用户名@主机",
          txt3 == "deploy@10.0.0.9", repr(txt3))

    # 元断言：把标签名换回旧实现（用户名@主机:端口），上面三条必须一起红。
    _ok = MW.MainWindow._on_connect_ok

    def _bad(self, conn, saved):
        self._connect_inflight = False
        label = f"{conn.info.username}@{conn.info.host}:{conn.info.port}"
        self.tabs.addTab(QtWidgets.QWidget(), label)

    MW.MainWindow._on_connect_ok = _bad
    try:
        bad_txt = open_tab(mw3, sess("Web-01", group="生产", sid="a"))
        check("元断言：旧实现产出 %r，标题判据会红" % bad_txt,
              bad_txt != "生产-Web-01")
    finally:
        MW.MainWindow._on_connect_ok = _ok
    check("元断言：还原之后标签名恢复正确",
          open_tab(mw3, sess("Web-05", group="生产", sid="f")) == "生产-Web-05")
finally:
    host3.close()
    pump(120)

# ===========================================================================
# 8. 右键菜单对分组头的防御
# ===========================================================================
section("8. 右键分组头不该弹出菜单")

mw4, host4 = build([sess("Web-01", group="生产", sid="a"),
                    sess("裸奔", group="", sid="b")])
popped = []
_orig_exec = QtWidgets.QMenu.exec
QtWidgets.QMenu.exec = lambda menu, *a, **k: (popped.append(menu), None)[1]
try:
    lst4 = mw4.session_list
    head_pos = lst4.visualItemRect(lst4.item(0)).center()
    row_pos = lst4.visualItemRect(lst4.item(1)).center()

    mw4._on_session_menu(head_pos)
    check("右键分组头：一个菜单都不弹", len(popped) == 0, "弹了 %d 个" % len(popped))

    popped.clear()
    mw4._on_session_menu(row_pos)
    check("对照：右键普通会话行照常弹菜单（说明上面那条不是恒真）",
          len(popped) == 1, "弹了 %d 个" % len(popped))

    popped.clear()
    mw4._on_session_menu(QtCore.QPoint(5, 5))
    check("对照：点在空白处也不弹", len(popped) == 0, "弹了 %d 个" % len(popped))
finally:
    QtWidgets.QMenu.exec = _orig_exec
    host4.close()
    pump(120)

# ===========================================================================
# 9. 补全候选来自真实数据
# ===========================================================================
section("9. 分组补全候选来自 store")

mw5, host5 = build([sess("A", group="生产", sid="a"),
                    sess("B", group="测试", sid="b"),
                    sess("C", group="生产", sid="c"),
                    sess("D", group="   ", sid="d"),
                    sess("E", group="", sid="e")])
check("_known_groups 去重、排序、剔掉空白",
      mw5._known_groups() == ["测试", "生产"], str(mw5._known_groups()))
host5.close()
pump(120)

# ===========================================================================
# 10. 分组折叠
# ===========================================================================
section("10. 分组头可折叠展开")


def head_of(mw, name):
    """按分组名找到那一行，返回 ``(行号, 头控件)``；找不到返回 ``(None, None)``。"""
    for i in range(mw.session_list.count()):
        it = mw.session_list.item(i)
        if it.data(QtCore.Qt.ItemDataRole.UserRole) is not None:
            continue
        w = mw.session_list.itemWidget(it)
        if w.findChild(QtWidgets.QLabel, "groupName").text() == name:
            return i, w
    return None, None


def pix_sig(pixmap):
    """图标画成什么样的一小份指纹：非透明像素的外接框 + 数量。

    **比 `cacheKey()` 可靠**：缓存命中时同一个 key 会被复用，而我们要断的
    恰恰是「有没有换图标」。**也不靠采样几个点** —— 1.7px 的折线在 14px 上
    只有三十来个非空像素，随便挑 9 个点很可能一个都挑不中，于是两个不同的
    箭头指纹相同，那条「箭头换过来了」的判据就恒绿了。
    """
    img = pixmap.toImage()
    pts = [(x, y) for y in range(img.height()) for x in range(img.width())
           if img.pixelColor(x, y).alpha() > 0]
    if not pts:
        return (0, 0, 0, 0, 0)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys), len(pts))


ARROW_DOWN = pix_sig(icons.icon_pixmap("chevron_down", "#ffffff", 14, 1.7))
ARROW_RIGHT = pix_sig(icons.icon_pixmap("chevron-right", "#ffffff", 14, 1.7))
check("对照：展开用的箭头和折叠用的箭头**画出来不一样**（否则下面两条恒绿）",
      ARROW_DOWN != ARROW_RIGHT, "%s / %s" % (ARROW_DOWN, ARROW_RIGHT))


def sessions_under(mw, name):
    """某个头下面现在有几行会话。"""
    start, _ = head_of(mw, name)
    n = 0
    for i in range(start + 1, mw.session_list.count()):
        if mw.session_list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) is None:
            break
        n += 1
    return n


mw6, host6 = build([sess("Web-01", group="生产", sid="a"),
                    sess("Web-02", group="生产", sid="b"),
                    sess("DB-01", group="测试", sid="c"),
                    sess("裸奔", group="", sid="d")])
lst6 = mw6.session_list
check("起点：三个头，生产那段下面有两行",
      row_kinds(mw6).count("头") == 3 and sessions_under(mw6, "生产") == 2,
      "%s / %s" % (row_kinds(mw6), sessions_under(mw6, "生产")))


def pick(lst, sid):
    """按会话 id 找到那一行的 item（判据按会话找，不按行号）。"""
    return next(lst.item(i) for i in range(lst.count())
                if lst.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == sid)


# ---- 对照组：折叠掉的**正是**选中行所在的那一段，选中会跟着消失
# 这不是 bug，是行被从列表里删掉了，`_reload_session_list` 按 UserRole
# 回填自然回填不到。写下来是因为它容易被读成「点一下分组头就丢选中」。
lst6 = mw6.session_list
lst6.setCurrentItem(pick(lst6, "a"))
check("对照组起点：选中的是生产里的那条",
      lst6.currentItem() is not None
      and lst6.currentItem().data(QtCore.Qt.ItemDataRole.UserRole) == "a",
      "%r" % (lst6.currentItem(),))
mw6._toggle_group("生产")
check("折叠掉选中行所在的那一段 -> 选中随之消失（行没了，选中无从回填）",
      lst6.currentItem() is None, "%r" % (lst6.currentItem(),))
mw6._toggle_group("生产")       # 放回去，后面的判据按展开态继续
pump(120)

i_prod, head_prod = head_of(mw6, "生产")
# 选中一条**不在「生产」里**的会话，才能验「点分组头不会把选中态弄丢」。
# （早先这版选中的是生产里的那条，于是断言必红 —— 原因见上面那段对照。）
lst6.setCurrentItem(pick(lst6, "c"))
_sel_before = lst6.currentItem().data(QtCore.Qt.ItemDataRole.UserRole)
QtTest.QTest.mouseClick(head_prod, QtCore.Qt.MouseButton.LeftButton,
                        pos=QtCore.QPoint(4, head_prod.height() // 2))
pump(150)
check("点分组头不动当前选中的会话（这一行只管折叠）",
      lst6.currentItem() is not None
      and lst6.currentItem().data(QtCore.Qt.ItemDataRole.UserRole) == _sel_before,
      "%r vs %r" % (lst6.currentItem().data(QtCore.Qt.ItemDataRole.UserRole)
                    if lst6.currentItem() else None, _sel_before))
check("点一下分组头：头还在，会话行没了",
      head_of(mw6, "生产")[0] is not None and sessions_under(mw6, "生产") == 0,
      "%s / %s" % (row_kinds(mw6), sessions_under(mw6, "生产")))
check("只收起被点的那一段（另外两段一条不少）",
      sessions_under(mw6, "测试") == 1 and sessions_under(mw6, "默认") == 1,
      "%s / %s" % (sessions_under(mw6, "测试"), sessions_under(mw6, "默认")))
check("整行少了两条会话，其余照旧",
      lst6.count() == 5 and sorted(
          x.name for x in mw6.store.all) == ["DB-01", "Web-01", "Web-02", "裸奔"],
      str(lst6.count()))
arrow = head_of(mw6, "生产")[1].findChild(QtWidgets.QLabel, "groupArrow")
check("箭头从 ▾ 变成 ▸（折起来之后要看得出来）",
      pix_sig(arrow.pixmap()) == ARROW_RIGHT, str(pix_sig(arrow.pixmap())))
tip_folded = lst6.item(head_of(mw6, "生产")[0]).toolTip()
check("折叠后的提示写着「点击展开」，不再是「点击折叠」",
      "点击展开" in tip_folded and "点击折叠" not in tip_folded, tip_folded)
check("折叠状态下会话数仍然写对（3 个，只是没显示）",
      "2 个会话" in tip_folded, tip_folded)
check("折叠态记进了 settings",
      mw6._collapsed_groups == {"生产"} and
      mw6.settings.collapsed_groups == ["生产"],
      "%s / %s" % (mw6._collapsed_groups, mw6.settings.collapsed_groups))

_i, head_prod2 = head_of(mw6, "生产")
QtTest.QTest.mouseClick(head_prod2, QtCore.Qt.MouseButton.LeftButton,
                        pos=QtCore.QPoint(4, head_prod2.height() // 2))
pump(150)
check("再点一下展开回来",
      sessions_under(mw6, "生产") == 2 and lst6.count() == 7,
      "%s / %s" % (sessions_under(mw6, "生产"), lst6.count()))
arrow2 = head_of(mw6, "生产")[1].findChild(QtWidgets.QLabel, "groupArrow")
check("箭头换回 ▾", pix_sig(arrow2.pixmap()) == ARROW_DOWN,
      str(pix_sig(arrow2.pixmap())))
check("展开之后折叠态从 settings 里清掉了",
      mw6._collapsed_groups == set() and mw6.settings.collapsed_groups == [],
      "%s / %s" % (mw6._collapsed_groups, mw6.settings.collapsed_groups))

# 折叠过的分组在侧栏收起成 56px 窄条之后**不能被忘掉**。
head_of(mw6, "生产")[1].mousePressEvent(
    QtGui.QMouseEvent(QtCore.QEvent.Type.MouseButtonPress,
                      QtCore.QPointF(4, 4), QtCore.QPointF(4, 4),
                      QtCore.QPointF(4, 4),
                      QtCore.Qt.MouseButton.LeftButton,
                      QtCore.Qt.MouseButton.LeftButton,
                      QtCore.Qt.KeyboardModifier.NoModifier))
pump(120)
mw6.set_sidebar_collapsed(True, persist=False)
pump(200)
check("侧栏收成 56px 时分组头不出现，但会话一个不少",
      "头" not in row_kinds(mw6) and lst6.count() == 4,
      "%s / %s" % (row_kinds(mw6), lst6.count()))
mw6.set_sidebar_collapsed(False, persist=False)
pump(200)
check("**展开侧栏后分组折叠态还在**（收起侧栏不该顺手清掉它）",
      sessions_under(mw6, "生产") == 0
      and mw6._collapsed_groups == {"生产"},
      "%s / %s" % (sessions_under(mw6, "生产"), mw6._collapsed_groups))
host6.close()
pump(120)

# 落盘 / 重启
tmp6 = pathlib.Path(tempfile.mkdtemp())
st6 = S.SessionStore(tmp6 / "s.json")
st6._items = [sess("A", group="生产", sid="a"), sess("B", group="测试", sid="b")]
ap6 = S.AppSettings()
ap6._path = tmp6 / "settings.json"      # 不指临时路径就会去写用户真实配置
ap6.save()
mw7 = MW.MainWindow(ap6, st6)
mw7._toggle_group("生产")
host7 = QtWidgets.QWidget()
_lay7 = QtWidgets.QVBoxLayout(host7)
_lay7.setContentsMargins(0, 0, 0, 0)
_lay7.addWidget(mw7)
host7.resize(1280, 700)
host7.show()
pump(250)
check("折叠立刻写进了 settings.json（不是只在内存里）",
      "生产" in (tmp6 / "settings.json").read_text(encoding="utf-8"),
      (tmp6 / "settings.json").read_text(encoding="utf-8"))
host7.close()
pump(150)

ap7 = S.AppSettings.load_from(tmp6 / "settings.json")
mw8 = MW.MainWindow(ap7, st6)
host8 = QtWidgets.QWidget()
_lay8 = QtWidgets.QVBoxLayout(host8)
_lay8.setContentsMargins(0, 0, 0, 0)
_lay8.addWidget(mw8)
host8.resize(1280, 700)
host8.show()
pump(250)
check("重开程序后那一段仍然是折着的",
      sessions_under(mw8, "生产") == 0 and sessions_under(mw8, "测试") == 1,
      "%s / %s" % (sessions_under(mw8, "生产"), sessions_under(mw8, "测试")))
# 写回时排序：同一份状态存两次必须得到同一个文件（集合的迭代顺序跟着哈希走）
mw8._toggle_group("测试")
mw8._toggle_group("测试")
_saved = json.loads((tmp6 / "settings.json").read_text(encoding="utf-8"))
check("反复切同一个分组之后落盘的仍是同一份状态（写回时排序，不被集合顺序带跑）",
      _saved.get("collapsed_groups") == ["生产"], str(_saved.get("collapsed_groups")))
check("落盘的是一个列表而不是集合的 whatever（JSON 里只有数组这一种）",
      isinstance(_saved.get("collapsed_groups"), list),
      repr(_saved.get("collapsed_groups")))
host8.close()
pump(120)

# 坏值
for bad_val, why in (("生产", "字符串"), (["生产", 3], "含数字"), (None, "null")):
    p = tmp6 / ("bad_%s.json" % why)
    p.write_text('{"collapsed_groups": %s}' % json.dumps(bad_val),
                 encoding="utf-8")
    got = S.AppSettings.load_from(p)
    check("手改坏的 collapsed_groups（%s）被收成空列表，不是崩" % why,
          got.collapsed_groups == [], repr(got.collapsed_groups))
    check("　　而且被标成「这份配置读不出来」，save() 不会拿默认值盖掉它（%s）" % why,
          bool(got.load_error), repr(got.load_error))
check("对照组：正常的列表读得进来，且不被标成错误",
      S.AppSettings.load_from(
          (lambda _p: (_p.write_text('{"collapsed_groups": ["a", "b"]}',
                                     encoding="utf-8"), _p)[1])(
              tmp6 / "good.json")).collapsed_groups == ["a", "b"])

# 元断言 A：把 mousePressEvent 换成不切换，上面「点一下就收起」必须红。
_gp = sidebar.GroupHeaderItem.mousePressEvent
sidebar.GroupHeaderItem.mousePressEvent = (
    lambda self, ev: QtWidgets.QWidget.mousePressEvent(self, ev))
try:
    mw9, host9 = build([sess("A", group="生产", sid="a"),
                        sess("B", group="测试", sid="b")])
    QtTest.QTest.mouseClick(head_of(mw9, "生产")[1],
                            QtCore.Qt.MouseButton.LeftButton,
                            pos=QtCore.QPoint(4, 5))
    pump(120)
    check("元断言：把点击处理摘掉之后，「点一下就收起」判据会红",
          sessions_under(mw9, "生产") != 0
          and mw9._collapsed_groups == set(),
          "%s / %s" % (sessions_under(mw9, "生产"), mw9._collapsed_groups))
    host9.close()
    pump(120)
finally:
    sidebar.GroupHeaderItem.mousePressEvent = _gp

# 元断言 B：把写回配置那一步摘掉，「折叠立刻写进文件」必须红。
_tg = MW.MainWindow._toggle_group


def _no_persist(self, group):
    if group in self._collapsed_groups:
        self._collapsed_groups.discard(group)
    else:
        self._collapsed_groups.add(group)
    self._reload_session_list()


MW.MainWindow._toggle_group = _no_persist
try:
    tmpB = pathlib.Path(tempfile.mkdtemp())
    stB = S.SessionStore(tmpB / "s.json")
    stB._items = [sess("A", group="生产", sid="a")]
    apB = S.AppSettings()
    apB._path = tmpB / "settings.json"
    mwB = MW.MainWindow(apB, stB)
    mwB._toggle_group("生产")
    check("元断言：不写回配置时，重开程序后折叠态确实没了",
          S.AppSettings.load_from(tmpB / "settings.json").collapsed_groups == [],
          repr(S.AppSettings.load_from(tmpB / "settings.json").collapsed_groups))
    mwB.close()
finally:
    MW.MainWindow._toggle_group = _tg

# ===========================================================================
print("\n" + "=" * 66)
print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
for f in FAIL:
    print("  FAIL " + f)
sys.exit(1 if FAIL else 0)
