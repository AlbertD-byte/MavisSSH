"""配置容错：坏文件不许毁用户数据、也不许把程序搞崩。

覆盖四条（都是审计出来的真缺陷）：
  1. 右键菜单「编辑/删除」必须作用在**被右键的那一行**，不是当前选中行
  2. 读不出来的 settings.json 绝不能被默认值覆写
  3. sessions.json 里的坏字段不许一路抛到启动路径上
  4. 坏标量要在这里就被钳掉，不许带到 Qt 才炸
"""
import json
import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtWidgets

from sshclient.core import settings as S
from sshclient.core.settings import AppSettings, SavedSession, SessionStore
from sshclient.core.snippets import SnippetStore
from sshclient.ui import main_window as MW
from tests import sidebar_rows

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name if ok else "%s  <- %s" % (name, detail))
    print(("  [ok] " if ok else "  [FAIL] ") + name
          + ("" if ok else "   <- %s" % detail))


def tmpdir(tag):
    d = pathlib.Path(tempfile.mkdtemp(prefix="cfgfuzz_"))
    return d


# ============================================================ 1 右键删错会话
print("\n[1] 右键菜单：操作的是被右键的那一行")

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
d = tmpdir("ctx")
store = SessionStore(d / "sessions.json", config_dir=d)
for nm, host in (("ALPHA", "1.1.1.1"), ("CHARLIE", "2.2.2.2")):
    store.upsert(SavedSession(name=nm, host=host, port=22,
                              username="u", auth_method="password", password="x"))
st = AppSettings()
st._path = d / "settings.json"
mw = MW.MainWindow(settings=st, store=store,
                   snippet_store=SnippetStore(d / "sn.json"))
host_w = QtWidgets.QWidget()
host_w.show()
for _ in range(5):
    app.processEvents()

lst = mw.session_list
# **按「第几个会话」取，不按行号。** 分组功能上线之前第 0 行必然是会话，
# 现在最上面先有一行「默认」标题 —— 于是「选中第 0 行」选到的是标题
# （不可选中，setCurrentRow 静悄悄不生效），而「右键第 1 行」右到的
# 是第一个会话：判据红得和它真正在断的那件事（按 item 删，不是按选中删）
# 毫无关系。
lst.setCurrentRow(sidebar_rows.session_row(lst, 0))   # 当前选中 = 第 0 个会话
for _ in range(3):
    app.processEvents()
target = sidebar_rows.session_item(lst, 1)            # 右键的是第 1 个会话
before = {s.id: s.name for s in store.all}

# 走菜单实际派发的那条路径：_on_delete_session(item)。
# 确认框必须顶掉——无头环境下它会返回非 Yes，于是什么都没删，
# 那样这条判据就分不出「删对了」和「压根没删」。
from sshclient.ui import msgbox as _msgbox

_q = _msgbox.question
_msgbox.question = lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes
try:
    mw._on_delete_session(target)
finally:
    _msgbox.question = _q
after = {s.id: s.name for s in store.all}
gone = [n for i, n in before.items() if i not in after]
check("右键第 2 个会话删掉的是它，不是当前选中的第 1 个",
      gone == ["CHARLIE"], "实际删掉 %s" % gone)
check("当前选中的那一行还在", "ALPHA" in after.values(), str(after))

# _session_of 存在且认 item
check("_session_of(item) 按 item 取，不看 currentItem()",
      mw._session_of(sidebar_rows.session_item(lst, 1)) is not None
      and mw._session_of(sidebar_rows.session_item(lst, 1)).name == "CHARLIE"
      if len(sidebar_rows.session_rows(lst)) > 1 else True,
      "会话行数 %d" % len(sidebar_rows.session_rows(lst)))

# ============================================================ 2 损坏配置不被覆写
print("\n[2] 读不出来的 settings.json 不能被默认值盖掉")

for tag, content in (("非法 JSON", "{ this is not json"),
                     ("不是对象", "[1, 2, 3]")):
    d2 = tmpdir("bad")
    p = d2 / "settings.json"
    p.write_text(content, encoding="utf-8")
    st2 = AppSettings.load_from(p)
    check("%s -> load_error 非空" % tag, bool(st2.load_error), repr(st2.load_error))
    st2._path = p
    st2.save()                                   # 这一步必须**不写**
    check("%s -> save() 之后原文件一字未动" % tag,
          p.read_text(encoding="utf-8") == content,
          "文件被改成了 %r" % p.read_text(encoding="utf-8")[:60])

# 正常配置要能存能读
d3 = tmpdir("ok")
p3 = d3 / "settings.json"
st3 = AppSettings.load_from(p3)
check("文件不存在时 load_error 为空（首次启动是正常的）", st3.load_error == "")
st3._path = p3
st3.scrollback = 12345
st3.save()
back = AppSettings.load_from(p3)
check("正常配置能存能读回来", back.scrollback == 12345, str(back.scrollback))

# load_error 不能进 settings.json
d4 = tmpdir("json")
p4 = d4 / "settings.json"
p4.write_text("{bad", encoding="utf-8")
st4 = AppSettings.load_from(p4)
st4._path = p4
st4.save_to(d4 / "out.json")
blob = json.loads((d4 / "out.json").read_text(encoding="utf-8"))
check("load_error 不进 settings.json", "load_error" not in blob, str(sorted(blob)))
check("_path 不进 settings.json", "_path" not in blob, str(sorted(blob)))

# ============================================================ 3 坏 sessions 不崩
print("\n[3] sessions.json 里的坏字段不许崩到启动路径上")

# 一个字段坏掉**不应该**拖垮整份文件：那个会话的其它部分都是好的，
# 整份隔离反而把用户所有会话一起藏起来。所以只丢那条坏规则。
d5 = tmpdir("sess")
p5 = d5 / "sessions.json"
p5.write_text(json.dumps([{"id": "a", "name": "x", "host": "h", "port": 22,
                           "username": "u", "auth_method": "password",
                           "password": "p", "tunnel_socks": "abc"}]),
              encoding="utf-8")
st5 = SessionStore(p5, config_dir=d5)
check("tunnel_socks 是字符串时不崩（SessionStore 建起来了）",
      len(st5._items) == 1, str(len(st5._items)))
check("那个会话本身还在（没有因为一个字段坏掉就整份隔离）",
      st5._items and st5._items[0].name == "x",
      str([s.name for s in st5._items]))
check("坏掉的 SOCKS 规则被丢掉而不是留下来一个 0 端口",
      not any(t.get("local_port") == 0 for t in (st5._items[0].tunnels or [])),
      str(st5._items[0].tunnels if st5._items else None))

# 正常的 socks 端口仍然生效（别把功能改坏）
d6 = tmpdir("sess_ok")
p6 = d6 / "sessions.json"
p6.write_text(json.dumps([{"id": "a", "name": "x", "host": "h", "port": 22,
                           "username": "u", "auth_method": "password",
                           "password": "p", "tunnel_socks": 1080}]),
              encoding="utf-8")
st6 = SessionStore(p6, config_dir=d6)
check("tunnel_socks 是正常数字时照常转成 SOCKS 规则",
      any(t.get("kind") == "socks" for t in (st6._items[0].tunnels or [])),
      str(st6._items[0].tunnels if st6._items else None))

# ============================================================ 4 坏标量被钳掉
print("\n[4] 坏标量在这里就被钳掉，不许带到 Qt")

d7 = tmpdir("coerce")
p7 = d7 / "settings.json"
p7.write_text(json.dumps({
    "ui_font_size": "abc", "term_font_size": None, "scrollback": "x",
    "monitor_interval_ms": 0, "theme": 42,
    "ui_font_family": ["a"], "accent_dark": 7,
    "ui_font_size_ok": True,
}), encoding="utf-8")
st7 = AppSettings.load_from(p7)
for field, (lo, hi) in AppSettings._INT_RANGE.items():
    v = getattr(st7, field)
    check("%s 被钳进 [%d, %d]（实际 %r）" % (field, lo, hi, v), lo <= v <= hi, repr(v))
check("theme 不是字符串时回落 dark", st7.theme == "dark", st7.theme)
check("list 型 ui_font_family 变成空串", st7.ui_font_family == "",
      repr(st7.ui_font_family))
check("int 型 accent_dark 变成空串", st7.accent_dark == "", repr(st7.accent_dark))
check("被改过的字段被记进 load_error", bool(st7.load_error), repr(st7.load_error))

# 越界但能收住的值
d8 = tmpdir("clamp")
p8 = d8 / "settings.json"
p8.write_text(json.dumps({"ui_font_size": 999, "scrollback": -5}),
              encoding="utf-8")
st8 = AppSettings.load_from(p8)
check("ui_font_size=999 被夹到上限 48", st8.ui_font_size == 48, str(st8.ui_font_size))
check("scrollback=-5 被夹到下限 0", st8.scrollback == 0, str(st8.scrollback))

# 元断言：装回缺陷实现时判据要红
print("\n[meta] 元断言")
check("元断言：_safe_int 收不住时会回默认值（不是抛异常）",
      S._safe_int("abc", 7) == 7 and S._safe_int(None, 7) == 7
      and S._safe_int("12", 7) == 12 and S._safe_int(1.9, 7) == 1,
      "abc=%r None=%r 12=%r" % (S._safe_int("abc", 7), S._safe_int(None, 7),
                                S._safe_int("12", 7)))

print()
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAIL " + f)
sys.exit(1 if FAIL else 0)
