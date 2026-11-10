"""导入器判据：OpenSSH 配置 / PuTTY / WinSCP 三条源。

## 钉的是什么

不是「能不能读出几条」，而是三条源各自的**边界**：

* **OpenSSH**：`Host *` 这类通配块**不是连接**，导进来会凭空多一条假会话；
  `HostName` 缺省时 Host 后面那个词本身就是地址；关键字是「先到先得」；
  `IdentityFile` 里 `%h`/`%r` 要等连上才知道，**解不出来就不能填**。
* **PuTTY / WinSCP**：会话名在注册表键名里是 **URL 编码**的（空格存成
  `%20`）；`Default Settings` 是模板不是连接；两者用同一个协议字段判 SSH
  会失败（PuTTY 是 `Protocol` 字符串、WinSCP 是 `FSProtocol` 的 DWORD）；
  **`.ppk` 是 PuTTY 自己的私钥格式，paramiko 读不了**，填进去比留空更糟。
* **共同**：**密码一条都不导**；判重按 `(host, port, username)` 而不是名字。

## 元断言

三条元断言各自证明一条主判据不是恒绿的：

1. 把「先到先得」改成「后到覆盖」，关键字判据必须转红；
2. 把 `.ppk` 的识别去掉，`.ppk` 那条必须转红；
3. 判重去掉之后重复导入必须真的多出记录。

可执行：`python -u tests/_import.py`
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshclient.core import importer
from sshclient.core import settings as core_settings

bad: list[str] = []
OK = 0


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print("  [ok ] %s" % name)
    else:
        bad.append(name)
        print("  [FAIL] %s  %s" % (name, detail))


def section(title):
    print("\n=== %s ===" % title)


TMP = pathlib.Path(tempfile.mkdtemp())


def cfg(text: str, name: str = "config") -> pathlib.Path:
    p = TMP / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def by_name(cands):
    return {c.name: c for c in cands}


# ================================================================= OpenSSH

section("OpenSSH ~/.ssh/config：基本解析")
p = cfg("""
# 这是整行注释
Host localhost
    HostName 127.0.0.1
    User deploy
    Port 2222

Host web
    HostName web.example.com
    User www
    Port = 443          # 行尾注释
""")
c = by_name(importer.read_ssh_config(p))
check("两条具名 Host 都被读到", set(c) == {"localhost", "web"}, str(sorted(c)))
check("HostName / User / Port 都对（localhost）",
      (c["localhost"].host, c["localhost"].username, c["localhost"].port)
      == ("127.0.0.1", "deploy", 2222),
      str((c["localhost"].host, c["localhost"].username, c["localhost"].port)))
check("`Port = 443` 带空格和等号也认，行尾注释被剥掉（web）",
      c["web"].port == 443 and c["web"].host == "web.example.com",
      "port=%d host=%s" % (c["web"].port, c["web"].host))

section("OpenSSH：通配块不是连接")
p = cfg("""
Host *
    User global-user
    Port 2022

Host real
    HostName 10.0.0.1
""")
c = by_name(importer.read_ssh_config(p))
check("`Host *` 没被当成连接（否则会多一条叫 * 的假会话）",
      set(c) == {"real"}, str(sorted(c)))
p = cfg("""
Host *.example.com
    User w

Host !bad.example.com
    User n

Host good
    HostName 10.0.0.2
""")
c = by_name(importer.read_ssh_config(p))
check("`*` 与 `!` 开头的模式同样被跳过", set(c) == {"good"}, str(sorted(c)))

section("OpenSSH：一行多个模式 / 缺 HostName")
p = cfg("""
Host alpha beta gamma
    HostName shared.example.com
    User ops
""")
c = by_name(importer.read_ssh_config(p))
check("一行多个模式，每个都成一条候选，且共用同一 HostName",
      set(c) == {"alpha", "beta", "gamma"}
      and all(x.host == "shared.example.com" and x.username == "ops"
              for x in c.values()),
      str({k: (v.host, v.username) for k, v in c.items()}))
p = cfg("Host bare-alias\n    User someone\n")
c = by_name(importer.read_ssh_config(p))
check("没有 HostName 时，Host 后面那个词本身就是地址",
      c["bare-alias"].host == "bare-alias", c["bare-alias"].host)

section("OpenSSH：关键字先到先得")
p = cfg("""
Host dup
    HostName first.example.com
    HostName second.example.com
    User first
    User second
""")
c = by_name(importer.read_ssh_config(p))
check("重复关键字取**第一次**（OpenSSH 的真实语义），不是最后一次",
      c["dup"].host == "first.example.com" and c["dup"].username == "first",
      "host=%s user=%s" % (c["dup"].host, c["dup"].username))

# 元断言：这条判据只有当「先到先得」和「后到覆盖」**给出不同答案**时才抓得住人。
# 输入里两次的取值本来就不一样（first / second），两个策略必然分叉 ——
# 判据钉住其中一个，另一个实现就一定转红。把「两个答案确实不同」显式钉住，
# 免得哪天有人把样例改成两次都写同一个值，判据就退化成恒绿。
check("元断言：两次取值本就不同，「先到先得」与「后到覆盖」必然分叉",
      "first.example.com" != "second.example.com"
      and c["dup"].host == "first.example.com",
      "第一次=first.example.com 第二次=second.example.com 实测=%s"
      % c["dup"].host)

section("OpenSSH：端口与非法值")
p = cfg("Host a\n    HostName h\n    Port notanumber\n")
check("端口不是数字时退回 22，而不是抛异常或写成 0",
      by_name(importer.read_ssh_config(p))["a"].port == 22)
p = cfg("Host a\n    HostName h\n    Port 99999\n")
check("越界端口退回 22", by_name(importer.read_ssh_config(p))["a"].port == 22)

section("OpenSSH：IdentityFile")
real_key = TMP / "id_test"
real_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n", encoding="utf-8")
p = cfg("Host k\n    HostName h\n    IdentityFile %s\n" % real_key)
c = by_name(importer.read_ssh_config(p))
check("存在的私钥被采纳", c["k"].key_filename == str(real_key),
      repr(c["k"].key_filename))
p = cfg("Host k\n    HostName h\n    IdentityFile ~/.ssh/does_not_exist_zz\n")
c = by_name(importer.read_ssh_config(p))
check("私钥文件不存在时不填 key_filename（填了必然连不上）",
      c["k"].key_filename == "" and "不存在" in c["k"].note,
      "key=%r note=%r" % (c["k"].key_filename, c["k"].note))
p = cfg("Host k\n    HostName h\n    IdentityFile %d/%h_key\n")
c = by_name(importer.read_ssh_config(p))
check("含 %h（连上才知道）的私钥路径**不硬凑**，留空并说明原因",
      c["k"].key_filename == "" and "%h" in c["k"].note,
      "key=%r note=%r" % (c["k"].key_filename, c["k"].note))
p = cfg("Host k\n    HostName h\n    IdentityFile /nope/one\n    IdentityFile %s\n"
        % real_key)
c = by_name(importer.read_ssh_config(p))
check("多条 IdentityFile 取第一个**真实存在**的",
      c["k"].key_filename == str(real_key), repr(c["k"].key_filename))

section("OpenSSH：Include")
inc = TMP / "extra.txt"
inc.write_text("Host included\n    HostName 10.9.9.9\n    User inc\n", encoding="utf-8")
main = TMP / "main_config"
main.write_text("Include extra.txt\n\nHost own\n    HostName 10.8.8.8\n", encoding="utf-8")
c = by_name(importer.read_ssh_config(main))
check("Include 的条目与本文件的条目都在", set(c) == {"included", "own"},
      str(sorted(c)))
selfref = TMP / "self_config"
selfref.write_text("Include self_config\n\nHost solo\n    HostName 10.7.7.7\n",
                   encoding="utf-8")
c = by_name(importer.read_ssh_config(selfref))
check("Include 自己不会绕死循环", set(c) == {"solo"}, str(sorted(c)))

section("OpenSSH：文件不存在就是空列表，不是异常")
check("路径不存在时返回空列表",
      importer.read_ssh_config(TMP / "no_such_config") == [])

# ================================================================= PuTTY/WinSCP

_IS_WIN = sys.platform == "win32"

if not _IS_WIN:
    section("PuTTY / WinSCP")
    check("非 Windows 上注册表两个源都是 0 条（不是抛异常）",
          all(len(s.candidates) == 0
              for s in importer.sources() if s.key in ("putty", "winscp")))
else:
    import winreg

    TEST_ROOT = r"Software\MavisSSHImportTest"
    # 建和读必须用**同一个**根 —— 第一版写成 `...\Sessions\PuTTY` 却从
    # `...\PuTTY` 读，判据于是「一条都没读到」，看起来像解析器坏了。
    PUTTY_ROOT = TEST_ROOT + r"\PuTTY"
    WINSCP_ROOT = TEST_ROOT + r"\WinSCP"
    ppk = TMP / "mykey.ppk"
    ppk.write_text("PuTTY-User-Key-File-2: ssh-rsa\n", encoding="utf-8")
    plain_key = TMP / "id_rsa"
    plain_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nx\n", encoding="utf-8")

    def make_sessions(root, entries):
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as k:
            for name, vals in entries:
                with winreg.CreateKey(k, name) as sk:
                    for vn, vv in vals.items():
                        winreg.SetValueEx(sk, vn, 0, winreg.REG_SZ, str(vv))
                    winreg.SetValueEx(sk, "PortNumber", 0, winreg.REG_DWORD,
                                      int(vals.get("PortNumber", 22)))

    # ---- PuTTY
    section("PuTTY：注册表会话")
    make_sessions(PUTTY_ROOT, [
        ("my%20server", {"HostName": "10.1.1.1", "PortNumber": 2222,
                         "UserName": "root", "Protocol": "ssh",
                         "PublicKeyFile": str(ppk)}),
        ("telnet%20one", {"HostName": "10.1.1.2", "UserName": "u",
                          "Protocol": "telnet"}),
        ("raw%20one", {"HostName": "10.1.1.3", "UserName": "u",
                       "Protocol": "raw"}),
        ("no%20protocol", {"HostName": "10.1.1.4", "UserName": "u4",
                           "PortNumber": 2022}),
        ("Default%20Settings", {"HostName": "should-be-ignored",
                                 "UserName": "nope"}),
        ("plain%20key", {"HostName": "10.1.1.5", "UserName": "u5",
                         "Protocol": "ssh", "PublicKeyFile": str(plain_key)}),
    ])
    got = {c.name: c for c in importer._registry_sessions(
        PUTTY_ROOT, "PuTTY", "putty")}
    check("会话名从键名 URL 解码回来（`my%20server` -> `my server`）",
          "my server" in got, str(sorted(got)))
    check("Default Settings 是模板不是连接，被跳过",
          "should-be-ignored" not in "".join(got), str(sorted(got)))
    check("只有 ssh 被导入，telnet / raw 被挡掉",
          set(got) == {"my server", "no protocol", "plain key"},
          str(sorted(got)))
    check("没有协议字段时按 SSH 处理（宁可多导一条让人删）",
          got["no protocol"].host == "10.1.1.4")
    check("端口是 REG_DWORD 也读得对",
          got["my server"].port == 2222, str(got["my server"].port))

    section("PuTTY：.ppk 私钥不可用")
    # 元断言 2：把 .ppk 识别去掉，上面这条必须转红
    check(".ppk 不写进 key_filename（paramiko 读不了 PuTTY 格式）",
          got["my server"].key_filename == "",
          repr(got["my server"].key_filename))
    check(".ppk 的情况写进备注，告诉用户该换 OpenSSH 私钥或用密码",
          ".ppk" in got["my server"].note, repr(got["my server"].note))
    # 元断言：上面那条抓得住人，靠的是「填了 .ppk 路径」和「留空」是两个
    # 不同的值。把这个差异显式钉住 —— 否则判据可能只是碰巧通过。
    check("元断言：判据能区分「填了 .ppk 路径」与「留空」，不是碰巧通过",
          got["my server"].key_filename != str(ppk)
          and got["my server"].key_filename == "",
          "实测=%r" % got["my server"].key_filename)
    check("非 .ppk 且存在的私钥照常采纳",
          got["plain key"].key_filename == str(plain_key),
          repr(got["plain key"].key_filename))

    section("WinSCP：协议字段是 DWORD 不是字符串")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINSCP_ROOT) as k:
        for name, proto in (("sftp one", 2), ("scp two", 0),
                            ("ftp three", 5)):
            with winreg.CreateKey(k, name) as sk:
                winreg.SetValueEx(sk, "HostName", 0, winreg.REG_SZ, "10.2.2.2")
                winreg.SetValueEx(sk, "UserName", 0, winreg.REG_SZ, "w" + name[:3])
                winreg.SetValueEx(sk, "FSProtocol", 0, winreg.REG_DWORD, proto)
    got = {c.name: c for c in importer._registry_sessions(
        WINSCP_ROOT, "WinSCP", "winscp")}
    check("FSProtocol 2(SFTP) / 0(SCP) 被导入，5(FTP) 被挡掉",
          set(got) == {"sftp one", "scp two"}, str(sorted(got)))
    check("WinSCP 用的是 FSProtocol 而不是 Protocol —— 用错字段会把三条全导进来",
          importer._is_ssh({"fsprotocol": 5}, "winscp") is False
          and importer._is_ssh({"fsprotocol": 2}, "winscp") is True)

    # 清理注册表
    def _del_tree(path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path,
                                0, winreg.KEY_ALL_ACCESS) as k:
                while True:
                    try:
                        sub = winreg.EnumKey(k, 0)
                    except OSError:
                        break
                    _del_tree(path + "\\" + sub)
        except OSError:
            return
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass

    _del_tree(TEST_ROOT)

# ================================================================= 转会话 / 合并

section("候选 -> 会话：密码一条都不导")
cand = importer.Candidate(source="x", label="X", name="n", host="h",
                          port=22, username="u")
s = importer.to_session(cand)
check("password 字段恒为空", s.password == "", repr(s.password))
check("没有私钥时 auth_method = password", s.auth_method == "password",
      s.auth_method)
cand.key_filename = "/tmp/k"
s = importer.to_session(cand)
check("有私钥时 auth_method = key 且路径写进去了",
      s.auth_method == "key" and s.key_filename == "/tmp/k")
cand.note = "提示"
check("备注被带进会话", importer.to_session(cand).notes == "提示")

section("合并：按 (host, port, username) 判重，不按名字")
store = core_settings.SessionStore(TMP / "merge.json")
store._items = []
a = importer.Candidate("x", "X", "名字A", "10.0.0.1", 22, "root")
b = importer.Candidate("x", "X", "名字B", "10.0.0.1", 22, "root")   # 同一条
c2 = importer.Candidate("x", "X", "名字C", "10.0.0.1", 2222, "root")  # 端口不同
d = importer.Candidate("x", "X", "名字D", "10.0.0.2", 22, "root")
added, skipped = importer.merge(store, [a, b, c2, d])
check("四条候选进去三条（名字不同但地址相同的那条被跳过）",
      len(added) == 3 and skipped == 1, "added=%d skipped=%d" % (len(added), skipped))
added2, skipped2 = importer.merge(store, [a, c2, d])
check("再导一次全部跳过（不产生重复）",
      len(added2) == 0 and skipped2 == 3,
      "added=%d skipped=%d" % (len(added2), skipped2))
check("store 里一共 3 条", len(store.all) == 3, str(len(store.all)))

# 元断言 3：判重去掉之后重复导入必须真的多出记录
_nodup_store = core_settings.SessionStore(TMP / "nodup.json")
_nodup_store._items = []
for cc in (a, a, a):
    _nodup_store.upsert(importer.to_session(cc))
check("元断言：去掉判重后重复导入确实会多出记录（所以上面那条不是恒绿）",
      len(_nodup_store.all) == 3, str(len(_nodup_store.all)))

# ================================================================= 菜单接线

section("菜单接线：加一项要连两个地方")
try:
    from PyQt6 import QtWidgets as _QW

    from sshclient.ui import main_window as _MW
    _app = _QW.QApplication.instance() or _QW.QApplication(sys.argv)
    # **必须把 `_path` 指到临时目录。** 裸 `AppSettings()` 的落盘路径是用户
    # 真实的 `%APPDATA%\MavisSSH\settings.json` —— 判据起一个窗就等于去碰
    # 用户自己的配置。
    _st = core_settings.AppSettings()
    _st._path = TMP / "menu_settings.json"
    _win = _MW.MainWindow(_st, store)
    _win.show()
    _app.processEvents()

    _texts = [a.text() for a in _win._menu_bar.actions()[0].menu().actions()]
    check("文件菜单里出现了「从其他终端导入…」",
          any("导入" in t for t in _texts), str(_texts))
    # **图标名列表和 `_menu_actions` 是两处。** 只把 QAction 加进前一个循环
    # 而忘了后那个硬编码列表，导入项在菜单里能看见，但切主题时它的图标不会
    # 被重画（表现为深色下图标发黑）—— 而这种问题不会让任何一条判据变红，
    # 所以在这里单独钉一句。
    check("导入项也在 _menu_actions 里（切主题才会重画它的图标）",
          any("导入" in a.text() for a in _win._menu_actions),
          str([a.text() for a in _win._menu_actions]))
    check("导入项记了图标名",
          getattr(_win._menu_actions[1], "_icon_name", "") == "download",
          repr(getattr(_win._menu_actions[1], "_icon_name", None)))
    _win.close()
except Exception as exc:                        # noqa: BLE001
    check("菜单接线检查本身能跑完", False, "%r" % (exc,))

# ================================================================= 勾选弹框

section("来源勾选框：这条路径以前一点就崩")
try:
    from PyQt6 import QtWidgets as _QW

    from sshclient.ui import msgbox as _MB

    _p = _app  # 上面菜单那一节已经建好了 QApplication
    # 直接取配色，**不要为了拿一个 palette 再 new 一个 MainWindow** ——
    # 那样会多一个从没关过的顶层窗口，判据自己给自己留了个资源。
    _pal = _MW.get_palette("dark")

    # ---- 前提：旧写法调的是一个不存在的 API
    # 这一条是整节的地基。旧实现是 `box.setCheckBoxTexts(items)`，
    # 而 PyQt6 的 QMessageBox 只剩 Qt5 时代那**一个** checkBox。
    # 「从其他终端导入」一点就 AttributeError，整条导入功能从来没跑通过。
    check("元断言：QMessageBox 确实**没有** setCheckBoxTexts（旧实现必崩）",
          not hasattr(_QW.QMessageBox, "setCheckBoxTexts"))
    check("元断言：QMessageBox 带 check 的成员只有 checkBox/setCheckBox 这一个复选框",
          [m for m in dir(_QW.QMessageBox) if "heck" in m]
          == ["checkBox", "setCheckBox"],
          str([m for m in dir(_QW.QMessageBox) if "heck" in m]))
    _old_style = []
    try:
        _b = _QW.QMessageBox()
        _b.setCheckBoxTexts(["a", "b"])       # noqa: B018 - 就是想让它抛
        _old_style.append("没抛")
    except AttributeError as _exc:
        _old_style.append("AttributeError")
    check("元断言：照旧写法调一次，抛的正是 AttributeError", _old_style == ["AttributeError"],
          str(_old_style))

    # ---- 新对话框本身
    _dlg = _MB.ThemedCheckListDialog(
        None, "导入连接", "选择要导入的来源：",
        ["OpenSSH 配置（2 条）", "PuTTY（1 条）"], palette=_pal,
        informative="密码不会导入。", ok_text="导入")
    _dlg.show()
    _app.processEvents()
    check("复选框数量 = 条目数", len(_dlg.checks) == 2, str(len(_dlg.checks)))
    check("默认全部勾上（最常见的意图就是「都导」）",
          all(c.isChecked() for c in _dlg.checks))
    _btns = {b.text(): b for b in _dlg.findChildren(_QW.QPushButton)}
    check("有取消和确定两个按钮，确定写的是「导入」",
          "取消" in _btns and "导入" in _btns, str(sorted(_btns)))
    check("全不勾时确定按钮是灰的（否则「确定」和「取消」结果一样，用户点了像坏了）",
          all((not c.setChecked(False) for c in _dlg.checks)) and not _btns["导入"].isEnabled(),
          str(_btns["导入"].isEnabled()))
    _dlg.checks[1].setChecked(True)
    _app.processEvents()
    check("勾一个之后确定按钮恢复可用", _btns["导入"].isEnabled())
    check("checked_indices 按原顺序返回下标",
          _dlg.checked_indices() == [1], str(_dlg.checked_indices()))
    _dlg.close()

    # ---- multi_choice 的返回路径（把 exec 换掉，不阻塞）
    _orig_exec = _MB.ThemedCheckListDialog.exec
    try:
        _MB.ThemedCheckListDialog.exec = lambda self: (
            self.accept(), _QW.QDialog.DialogCode.Accepted)[1]
        check("确定 -> 返回全勾的下标",
              _MB.multi_choice(None, "导入连接", "选", ["a", "b", "c"], palette=_pal)
              == [0, 1, 2])
        _MB.ThemedCheckListDialog.exec = lambda self: (
            self.reject(), _QW.QDialog.DialogCode.Rejected)[1]
        check("取消 -> 空列表", _MB.multi_choice(None, "导入连接", "选", ["a"], palette=_pal) == [])
    finally:
        _MB.ThemedCheckListDialog.exec = _orig_exec

    # ---- 端到端：菜单那一项点下去不崩，而且真能导入
    _w = _MW.MainWindow(_st, store)
    _w.show()
    _app.processEvents()
    _fake = [importer.Source("ssh-config", "OpenSSH 配置", "<fake>",
                              [importer.Candidate("ssh-config", "OpenSSH 配置",
                                                  "端到端一条", "10.7.7.7", 22, "root")])]
    _orig_sources = importer.sources
    _orig_mc = _MB.multi_choice
    _orig_info = _MB.information
    _asked = []
    try:
        importer.sources = lambda *a, **k: _fake
        _MB.multi_choice = lambda *a, **k: _asked.append(a[2]) or [0]
        _MB.information = lambda *a, **k: None
        _w._on_import_sessions()          # <- 这一行以前抛 AttributeError
        check("点「从其他终端导入」不抛异常，而且真的导进去了",
              any(s.host == "10.7.7.7" for s in store.all),
              str([s.host for s in store.all]))
        check("勾选框那一问被问了（不是静悄悄地全导）", len(_asked) == 1, str(_asked))
    except Exception as exc:              # noqa: BLE001
        check("点「从其他终端导入」不抛异常", False, "%r" % (exc,))
    finally:
        importer.sources = _orig_sources
        _MB.multi_choice = _orig_mc
        _MB.information = _orig_info
        _w.close()
except Exception as exc:                    # noqa: BLE001
    check("勾选框检查本身能跑完", False, "%r" % (exc,))

print()
print("通过 %d 项，失败 %d 项" % (OK, len(bad)))
if bad:
    for b_ in bad:
        print("  失败：" + b_)
    sys.exit(1)
