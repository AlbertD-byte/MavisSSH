"""SOCKS 开关与监听地址的判据。

**判据钉的是行为，不是长什么样。** 「开关存在」测不出来「开关改了配置」，
「有监听下拉」也测不出它真的进了 `spec()`。所以每条都对着最终产物断言。

判据清单：
  1a switch 是真的两态控件，且默认端口是 1080
  1b 关掉 switch → 端口框禁用（可见的「这条不生效」）
  1c **开关状态真的进了保存结果**（元断言：把 _tunnel_rules 换回旧实现必红）
  1d 老配置回填：tunnel_socks=0 → 关；>0 且 enabled → 开；enabled=False → 关
  2a 两个入口都有监听地址，且值真的进了 spec/to_dict
  2b 监听地址会被 core.tunnel 的校验接受（不是死值）
  2c 关掉但原来有规则 → 保留为 enabled=False，不静默删除（元断言）
  3  QMessageBox 正文比普通 QLabel 大（用户报「弹框内容太小」）
  3b 大字号随 ui_font_size 缩放（元断言：装回旧值必红）
"""
import inspect
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from PyQt6 import QtWidgets  # noqa: E402

from sshclient.core.tunnel import validate  # noqa: E402
from sshclient.ui import msgbox  # noqa: E402
from sshclient.ui.connect_dialog import _DEFAULT_SOCKS_PORT, ConnectDialog  # noqa: E402
from sshclient.ui.design.v1 import palette as pal  # noqa: E402
from sshclient.ui.design.v1 import qss  # noqa: E402
from sshclient.ui.switch import PILL_H, PILL_W, Switch  # noqa: E402
from sshclient.ui.tunnel_panel import _AddDialog, _SwitchDelegate  # noqa: E402

app = QtWidgets.QApplication(sys.argv)
FAILS: list[str] = []
CHECKS = [0]


def ck(name, cond, detail=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(f"{name}  {detail}")
        print(f"  [红] {name}  {detail}")
    else:
        print(f"  [绿] {name}")


def socks_of(rules):
    return [r for r in rules if r.get("kind") == "socks"]


p = pal.get_palette("light")
qss.set_current(p, 11)

print("=== 1. SOCKS 启用方式 ===")

# 1a 两态控件 + 默认端口
d = ConnectDialog(palette=p)
ck("1a switch 是两态控件", isinstance(d.socks_switch, Switch)
   and d.socks_switch.isCheckable())
ck("1a 默认端口 = 1080", d.tunnel_socks.value() == _DEFAULT_SOCKS_PORT,
   f"实际 {d.tunnel_socks.value()}")
ck("1a 端口范围不含 0（0 不再兼表「不启用」）",
   d.tunnel_socks.minimum() >= 1, f"min={d.tunnel_socks.minimum()}")
ck("1a 旧的 specialValueText 已移除",
   d.tunnel_socks.specialValueText() == "",
   repr(d.tunnel_socks.specialValueText()))
d.close()

# 1b 关掉 → 端口禁用
d = ConnectDialog(palette=p)
d.socks_switch.setChecked(True)
app.processEvents()
ck("1b 开时端口可编辑", d.tunnel_socks.isEnabled())
d.socks_switch.setChecked(False)
app.processEvents()
ck("1b 关时端口禁用（可见「不生效」）", not d.tunnel_socks.isEnabled())
ck("1b 关时监听地址同样禁用", not d.socks_listen.isEnabled())
# 关掉再打开，端口值不该被改
before = d.tunnel_socks.value()
d.socks_switch.setChecked(True)
app.processEvents()
ck("1b 开关切换不改端口值", d.tunnel_socks.value() == before,
   f"{before} → {d.tunnel_socks.value()}")
d.close()

# 1c 开关状态真的进了保存结果
d = ConnectDialog(palette=p)
d.socks_switch.setChecked(True)
d.tunnel_socks.setValue(2080)
on_rules = socks_of(d._tunnel_rules())
ck("1c 开 → 产出一条 socks 且 enabled", len(on_rules) == 1
   and on_rules[0]["enabled"] is True, str(on_rules))
ck("1c 端口写进去了", on_rules and on_rules[0]["local_port"] == 2080,
   str(on_rules))
d.socks_switch.setChecked(False)
off_rules = socks_of(d._tunnel_rules())
ck("1c 关（原本无规则）→ 不产出 socks", off_rules == [], str(off_rules))
d.close()


# --- 元断言 1c：把保存逻辑换回「端口即开关」的旧实现，判据必须转红 ---
class _LegacyDialog(ConnectDialog):
    def _tunnel_rules(self):
        rules = super()._tunnel_rules()
        # 旧实现：只看端口是不是 0，完全不知道 switch。
        port = int(self.tunnel_socks.value())
        if port:
            from sshclient.core.tunnel import TunnelSpec
            rules.append(TunnelSpec(kind="socks", local_port=port,
                                    description="SOCKS5").to_dict(enabled=True))
        return rules


d = _LegacyDialog(palette=p)
d.socks_switch.setChecked(False)
legacy = socks_of(d._tunnel_rules())
ck("[元] 旧实现下「关」会产出一条 socks（判据抓得到）",
   len(legacy) == 1, f"实际 {legacy}")
d.close()

print()
print("=== 1d. 老配置回填 ===")


def dialog_for(tunnels, socks=0):
    return ConnectDialog(palette=p, initial={
        "tunnels": tunnels or [], "tunnel_socks": socks})


# 旧格式：tunnel_socks=0 → 关
d = dialog_for(None, 0)
ck("1d tunnel_socks=0 → 开关关", not d.socks_switch.isChecked())
d.close()

# 旧格式：tunnel_socks=1080 → 开
d = dialog_for(None, 1080)
ck("1d tunnel_socks=1080 → 开关开且端口回填", d.socks_switch.isChecked()
   and d.tunnel_socks.value() == 1080,
   f"checked={d.socks_switch.isChecked()} port={d.tunnel_socks.value()}")
ck("1d 保存后仍是一条启用的 socks",
   len(socks_of(d._tunnel_rules())) == 1
   and socks_of(d._tunnel_rules())[0]["enabled"] is True)
d.close()

# 规范格式：enabled=False（用户手动关过）→ 开关关
d = dialog_for([{"kind": "socks", "local_port": 1080, "description": "SOCKS5",
                 "listen_host": "127.0.0.1", "enabled": False}])
ck("1d enabled=False → 开关关", not d.socks_switch.isChecked())
kept = socks_of(d._tunnel_rules())
ck("1d 但规则没被静默删除（保留为未启用）",
   len(kept) == 1 and kept[0]["enabled"] is False, str(kept))
d.close()

# 多条 socks：第一条进控件，其余原样保留
d = dialog_for([
    {"kind": "socks", "local_port": 1080, "listen_host": "127.0.0.1",
     "enabled": True},
    {"kind": "socks", "local_port": 1081, "listen_host": "0.0.0.0",
     "enabled": True}])
got = socks_of(d._tunnel_rules())
ck("1d 多条 socks 都不丢", len(got) == 2, str(got))
d.close()

print()
print("=== 2. 监听地址（原本 UI 缺的那个参数）===")

# 2a 连接框
d = ConnectDialog(palette=p)
d.socks_switch.setChecked(True)
d.socks_listen.setCurrentIndex(d.socks_listen.findData("0.0.0.0"))
app.processEvents()
r = socks_of(d._tunnel_rules())
ck("2a 连接框：0.0.0.0 进了保存结果",
   r and r[0]["listen_host"] == "0.0.0.0", str(r))
d.close()

# 2a 添加框
a = _AddDialog(p, None, used_ports=())
ck("2a 添加框有监听地址下拉", a.listen_box.count() == 2,
   f"count={a.listen_box.count()}")
a.listen_box.setCurrentIndex(a.listen_box.findData("0.0.0.0"))
ck("2a 添加框：spec() 带上 0.0.0.0", a.spec().listen_host == "0.0.0.0",
   a.spec().listen_host)
a.close()

# 2b core 的校验要接受这两个值（不是死值）
for host in ("127.0.0.1", "0.0.0.0"):
    try:
        from sshclient.core.tunnel import TunnelSpec
        spec = validate(TunnelSpec(kind="socks", local_port=1080,
                                   listen_host=host))
        ck(f"2b core 校验接受 {host}", spec.listen_host == host)
    except Exception as exc:                                # noqa: BLE001
        ck(f"2b core 校验接受 {host}", False, repr(exc))

# 2c 两条入口都能被 core 校验通过（端到端）。
# **必须切到 socks 类型**：添加框默认停在「本地端口转发」，那种规则按定义
# 就得有远程目标，`validate` 报的是另一件事（第一次跑就红在这里）。
a = _AddDialog(p, None, used_ports=())
a.kind_box.setCurrentIndex(1)          # 动态 SOCKS5 代理 (-D)
ck("2c 添加框已切到 socks 类型", a.spec().kind == "socks", a.spec().kind)
a.listen_box.setCurrentIndex(a.listen_box.findData("0.0.0.0"))
try:
    validate(a.spec())
    ck("2c 添加框产出的 socks spec 能过 core 校验", True)
except Exception as exc:                                    # noqa: BLE001
    ck("2c 添加框产出的 socks spec 能过 core 校验", False, repr(exc))
a.close()

print()
print("=== 3. 帮助弹框字号 ===")


def msgbox_font_sizes(theme="light", ui_size=11):
    pal_ = pal.get_palette(theme)
    qss.set_current(pal_, ui_size)
    box = msgbox.ThemedMessageBox(None, "关于", pal_, icon_name="info")
    box.setStyleSheet(qss.build_qss(pal_, dialog=True, ui_size=ui_size))
    box.setText("MavisSSH 0.2.0")
    box.setInformativeText("功能：终端")
    box.show()
    app.processEvents()
    out = {}
    for lab in box.findChildren(QtWidgets.QLabel):
        if lab.objectName() in ("qt_msgbox_label", "qt_msgbox_informativelabel"):
            out[lab.objectName()] = lab.font().pixelSize()
    w, h = box.width(), box.height()
    box.close()
    return out, (w, h)


sizes, dims = msgbox_font_sizes()
plain = QtWidgets.QLabel("x")
plain.setStyleSheet(qss.build_qss(p, dialog=True, ui_size=11))
ck("3 正文比普通 QLabel 大（用户报内容太小）",
   sizes["qt_msgbox_label"] > 13,
   f"msgbox={sizes['qt_msgbox_label']} vs QLabel=13")
ck("3b 副文本也比普通 QLabel 大",
   sizes["qt_msgbox_informativelabel"] > 13,
   f"msgbox={sizes['qt_msgbox_informativelabel']}")
ck("3 主文本 > 副文本（有层级）",
   sizes["qt_msgbox_label"] > sizes["qt_msgbox_informativelabel"])

# 深色同样生效（不能只对浅色调）
dark_sizes, _ = msgbox_font_sizes("dark")
ck("3 深色主题同样放大", dark_sizes["qt_msgbox_label"] > 13,
   str(dark_sizes))

# 3b 大字号随 ui_font_size 缩放
big, big_dims = msgbox_font_sizes("light", 18)
ck("3b 18pt 下主文本继续变大", big["qt_msgbox_label"] > sizes["qt_msgbox_label"],
   f"11pt={sizes['qt_msgbox_label']} 18pt={big['qt_msgbox_label']}")
ck("3b 18pt 下弹框跟着长高（不是字挤在框里）", big_dims[1] > dims[1],
   f"{dims} → {big_dims}")


# --- 元断言 3：把 QSS 的 msgbox 块摘掉，字号必须掉回 13（判据确实在测它） ---
#
# **怎么「摘掉」这件事本身也踩过一次。** 最早用
# `re.sub(r"/\* QMessageBox 正文层级 \*/.*?\}\}", sheet, flags=re.S)`，
# 结果字号纹丝不动 —— `qss.py` 里那些 `}}` 是 **f-string 的转义**，落到
# 运行时的样式表里是**单个 `}`**，所以 `\}\}` 匹配不到，块根本没被摘掉，
# 元断言于是「绿着通过了它本该抓住的坏版本」。
# 判据自己恒绿比没有判据更糟，所以这里改成按行摘：找注释行，往下摘到
# 花括号配平为止，并用「摘完之后文本确实变短了」自证摘到了东西。
def strip_msgbox_block(sheet: str) -> str:
    lines = sheet.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if "QMessageBox 正文层级" in ln), None)
    if start is None:
        raise AssertionError("样式表里找不到 msgbox 块，元断言失效")
    depth = 0
    end = start
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if i > start and depth <= 0:
            end = i
            break
    else:
        raise AssertionError("msgbox 块没有配平的结尾，元断言失效")
    return "\n".join(lines[:start] + lines[end + 1:])


def font_without_msgbox_block(theme="light", ui_size=11):
    pal_ = pal.get_palette(theme)
    qss.set_current(pal_, ui_size)
    sheet = qss.build_qss(pal_, dialog=True, ui_size=ui_size)
    stripped = strip_msgbox_block(sheet)
    ck("[元] 确实摘掉了一块（文本变短）", len(stripped) < len(sheet),
       f"{len(sheet)} → {len(stripped)}")
    box = msgbox.ThemedMessageBox(None, "关于", pal_, icon_name="info")
    box.setStyleSheet(stripped)
    box.setText("MavisSSH 0.2.0")
    box.setInformativeText("功能：终端")
    box.show()
    app.processEvents()
    out = {lab.objectName(): lab.font().pixelSize()
           for lab in box.findChildren(QtWidgets.QLabel)
           if lab.objectName() in ("qt_msgbox_label",
                                   "qt_msgbox_informativelabel")}
    box.close()
    return out


stripped_sizes = font_without_msgbox_block()
ck("[元] 摘掉 QSS 块后字号掉回 13（判据不是恒绿）",
   stripped_sizes.get("qt_msgbox_label") == 13, str(stripped_sizes))

print()
print("=== 4. Switch 画法唯一来源 ===")

ck("4 委托与控件共用同一组尺寸常量",
   (_SwitchDelegate._W, _SwitchDelegate._H) == (PILL_W, PILL_H),
   f"{_SwitchDelegate._W}x{_SwitchDelegate._H} vs {PILL_W}x{PILL_H}")

src = inspect.getsource(_SwitchDelegate.paint)
ck("4 委托不再自绘（只调 paint_switch）", "paint_switch(" in src
   and "drawRoundedRect" not in src)

print()
print(f"--- {CHECKS[0] - len(FAILS)}/{CHECKS[0]} ---")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
