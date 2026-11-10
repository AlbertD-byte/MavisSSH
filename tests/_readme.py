"""让 README.md 不会自信地写错。

README 是新读者唯一会打开的文档，所以真正要防的失败不是"写得含糊"，而是
"写得很确定但其实是错的"：范围变了、快捷键改了、模块没了、功能落地了却没人
记下来。这些在截图里全都看不出来。

所以能机械核对的断言就在这里逐条对着源码核对，每次回归都跑一遍。核对不了的
——行文、理由——由 `docs/ARCHITECTURE.md` 承担。

刻意**不**断言的：任何本机状态里会被正当改动推动的东西。已装字体数量只打印、
绝不让整轮失败 —— 装个字体不该算文档回归。

核对是**双向**的：既查源码确实绑定了某个键，也查 README 里写的就是那个键。
只查源码的话，README 怎么漂移都会一直通过。
"""
import ast
import os
import pathlib
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PyQt6 import QtGui, QtWidgets

from sshclient.core import fonts
from tests._tracked import tracked_files

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)


# 本脚本在 tests/ 下，被它核对的东西全在项目根，所以往上两级。
ROOT = pathlib.Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DIALOG = (ROOT / "sshclient/ui/settings_dialog.py").read_text(encoding="utf-8")
MAIN = (ROOT / "sshclient/ui/main_window.py").read_text(encoding="utf-8")
TERM = (ROOT / "sshclient/ui/terminal_widget.py").read_text(encoding="utf-8")
TUNNEL = (ROOT / "sshclient/ui/tunnel_panel.py").read_text(encoding="utf-8")
RUNALL_PATH = ROOT / "_runall.py"
RUNALL = RUNALL_PATH.read_text(encoding="utf-8")
# 用 **AST** 读 _runall.py 里的 TESTS 元组，不要用正则扫文本。扫文本的话
# 模块 docstring 里出现的 '_readme' 这类举例也会被当成"已登记的测试"，
# 门禁就会以为某套跑过了——而它其实根本不在列表里。
_runall_mod = ast.parse(RUNALL)
TESTS: list[str] = []
for _node in _runall_mod.body:
    if (isinstance(_node, ast.Assign)
            and any(getattr(t, "id", None) == "TESTS" for t in _node.targets)):
        TESTS = list(ast.literal_eval(_node.value))
if not TESTS:
    raise SystemExit("_runall.py 里读不出 TESTS 元组，测试清单没法核对")

bad: list[str] = []
note: list[str] = []


def check(name, ok, detail=""):
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}"
          + ("" if ok else f"   <- {detail}"))
    if not ok:
        bad.append(name if not detail else f"{name}  <- {detail}")


def regex(name, pat, haystack):
    ok = re.search(pat, haystack) is not None
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}")
    if not ok:
        bad.append(f"{name}（模式 {pat!r} 不再匹配）")


if not README.exists():
    print("README.md 不存在")
    sys.exit(1)
readme = README.read_text(encoding="utf-8")

# ------------------------------------------------------------- 模块表
print("=== 每个模块都被写到了 ===")
# rglob 而不是 glob：包已经分成 core/ remote/ ui/ 三层，只扫一层会漏掉
# 下面所有的模块，于是 README 里留着旧路径也照样"全绿"。
named = set(re.findall(r"`(sshclient/[\w/]+\.py)`", readme))
check("README 至少写到 12 个模块", len(named) >= 12)
for path in sorted(named):
    check(f"{path} 确实存在", (ROOT / path).exists())

actual = {p.relative_to(ROOT).as_posix()
          for p in (ROOT / "sshclient").rglob("*.py")
          # __init__ 只是包标记，不是组件
          if p.name != "__init__.py"}
missing = sorted(actual - named)
check("没有模块漏写", missing == [])
if missing:
    print(f"       未记录：{missing}")

# --------------------------------------------------------- 运行方式与安装
print("\n=== 文档里的运行命令都存在 ===")
check("README 指向 main.py", "main.py" in readme)
check("main.py 存在", (ROOT / "main.py").exists())
regex("文档里的解释器是 venv 里那个",
       r"\.\\\.venv\\Scripts\\python\.exe", readme)
regex("安装步骤用 `python -m venv .venv`", r"python -m venv \.venv", readme)
check("requirements.txt 被引用且存在",
      "requirements.txt" in readme and (ROOT / "requirements.txt").exists())
check("测试入口被引用且存在",
      "_runall.py" in readme and RUNALL_PATH.exists())
check("Windows-only 的 .ps1 / .bat 入口已经清掉（跨平台）",
      not (ROOT / "_runall.ps1").exists() and not (ROOT / "run.bat").exists(),
      "还有：%s" % "、".join(
          n for n in ("_runall.ps1", "run.bat") if (ROOT / n).exists()))

# ---------------------------------------------------------------- 快捷键
print("\n=== README 列的每个快捷键都真的绑上了 ===")
readme_keys = {k.replace(" ", "") for k in
               re.findall(r"^\|\s*`([^`]+)`\s*\|[^|]*\|[^|]*\|\s*$", readme, re.MULTILINE)}

for label, pat, key in [
    ("Ctrl+N  新建连接",
     r'act_new\.setShortcut\(QtGui\.QKeySequence\.StandardKey\.New', "Ctrl+N"),
    # `StandardKey.Close` 在 Windows 上解析成 **Ctrl+F4**。README 原来写的是
    # `Ctrl+W`，下面那条「README 写的就是 Ctrl+W」于是判了绿 —— 一条虚假绿灯。
    ("Ctrl+F4  关闭标签页",
     r'act_close\.setShortcut\(QtGui\.QKeySequence\.StandardKey\.Close', "Ctrl+F4"),
    ("Ctrl+,  设置",
     r'act_settings\.setShortcut\(QtGui\.QKeySequence\("Ctrl\+,"\)', "Ctrl+,"),
    ("Ctrl+T  切换主题", r'act_theme\.setShortcut\("Ctrl\+T"\)', "Ctrl+T"),
    ("Ctrl+B  折叠侧边栏", r'act_fold\.setShortcut\("Ctrl\+B"\)', "Ctrl+B"),
]:
    regex(label + " 在源码里绑定了", pat, MAIN)
    check(f"README 写的就是 {key}", key in readme_keys)

# **标准键必须在运行时真的解析成 README 写的那个键。**
#
# 上面那条双向核对有个洞：它验证的是「源码里写了 `StandardKey.Close`」和
# 「README 里写着 Ctrl+W」两件事，**从没问过那个 API 在本平台解析成什么**。
# 而 `StandardKey.Close` 恰好不等于 Ctrl+W —— 于是这条守卫在为一个**错的**
# README 长期背书。这类「两段都对、中间那句没人验」的洞，截图里更看不出来。
print("\n=== 标准键真的解析成 README 写的那个键 ===")
for _label, _std, _key in (
    ("Ctrl+N  新建连接", QtGui.QKeySequence.StandardKey.New, "Ctrl+N"),
    ("Ctrl+F4  关闭标签页", QtGui.QKeySequence.StandardKey.Close, "Ctrl+F4"),
):
    _actual = QtGui.QKeySequence(_std).toString()
    check(f"{_label}：本平台解析成 {_key}", _actual == _key,
          f"实际是 {_actual!r}")
# 负向对照：把期望换成那个**看起来很合理**的键，这条必须判不通过。
# 少了它，上面那条判据有可能恒真（比如有人把 toString() 写成了硬编码常量）。
check("负向对照：标准键判据不是恒真的",
      QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Close).toString() != "Ctrl+W",
      "本平台上它居然真的等于 Ctrl+W —— 那么 README 原本是对的，"
      "是这条判据自己写错了")

regex("Ctrl+Shift+C 复制", r"key == QtCore\.Qt\.Key\.Key_C and shift", TERM)
regex("Ctrl+V 粘贴", r"key == QtCore\.Qt\.Key\.Key_V", TERM)
# Ctrl+C 是那个必须一物两用的键，所以不按模式匹配而是说明白：
# 有选区就复制，没有就发 ^C。
regex("Ctrl+C 无选区时落到 ^C", r"self\._send\(bytes\(\[key - 0x40\]\)\)", TERM)
for key in ("Ctrl+C", "Ctrl+Shift+C", "Ctrl+V"):
    check(f"README 列了终端快捷键 {key}", key in readme_keys)

# ---------------------------------------------------------------- PTY 尺寸
print("\n=== README 说的每件终端行为都得真接上了 ===")
# 这一条是补上的：`set_resize_handler` 从写下来到被接上，中间一直没有调用者，
# 而 README 早就在宣称"窗口尺寸变化会同步到远端 PTY"。截图里完全看不出来，
# 所以在这里双向核对：README 敢这么写，源码就必须真接了。
regex("README 写了尺寸同步到远端 PTY", r"同步到远端 PTY", readme)
regex("TerminalWidget 真的把 resize 回调接了出去", r"set_resize_handler\(", MAIN)
regex("回调最终调到了通道的 resize_pty", r"resize_pty\(", MAIN)
check("_ptyresize 进了回归", "_ptyresize" in TESTS)

# ---------------------------------------------------------------- 会话分组
print("\n=== README 写的那套分组行为，源码必须真接上 ===")
# README 新增了「会话分组」一节。分组的规矩不少（回退到哪个字段、
# 未分组排不排最后、pill 显示什么），每一条都可能在实现里悄悄走样，
# 而走样之后 README 仍然写得斩钉截铁。所以逐条双向核对。
SETTINGS_SRC = (pathlib.Path(ROOT) / "sshclient/core/settings.py").read_text(
    encoding="utf-8")
SIDEBAR_SRC = (pathlib.Path(ROOT) / "sshclient/ui/sidebar.py").read_text(
    encoding="utf-8")
DLG_SRC = (pathlib.Path(ROOT) / "sshclient/ui/connect_dialog.py").read_text(
    encoding="utf-8")

regex("README 写了分组这一节", r"##\s*会话分组", readme)
check("README 说分组名保持明文（不是加密字段）",
      "`group`" in readme.split("## 会话分组")[0]
      or "`group`" in readme)
regex("SavedSession 真的有 group 字段", r"group:\s*str\s*=", SETTINGS_SRC)
regex("标签标题走的是 display_name", r"label\s*=\s*saved\.display_name\(\)", MAIN)
# **pill 走的是另一个字段。** 这条不是形式主义：标签改成「分组名-会话名」之后，
# 如果 pill 还跟着读标签，状态栏上就只剩一个会话名，而 README 在同一段里
# 承诺它显示 `用户名@主机:端口`。两边共用一个字段时，README 的这句话会变成假话，
# 而界面看起来还挺正常。
regex("pill 读的是 _last_endpoint，不是 _last_label",
      r"live\s*=\s*w\._last_endpoint", MAIN)
check("pill 那一行没有退回读标签",
      "live = w._last_label" not in MAIN)
regex("连接框真的有分组输入框", r'self\.group_edit\s*=', DLG_SRC)
# README 写的是「可下拉选，也可以自己输入」——这两半各自对应一行代码，
# 少一半就会出现「用户根本不知道自己有哪些分组可选」。
check("分组框是可编辑下拉（列出来 + 打字都行）",
      "setEditable(True)" in DLG_SRC and "UNGROUPED" in DLG_SRC)
check("下拉不自己长条目（分组由保存动作创建）",
      "InsertPolicy.NoInsert" in DLG_SRC)
regex("分组被 result_dict 回吐了", r'"group":\s*_group_value\(', DLG_SRC)
regex("侧栏真的有分组标题行", r"class\s+GroupHeaderItem", SIDEBAR_SRC)
regex("分组头确实不可选中",
      r"setFlags\(QtCore\.Qt\.ItemFlag\.ItemIsEnabled\)", MAIN)
# README 现在承诺的是「默认分组」和「可折叠」，这两条都要在源码里有着落。
# 「默认」这个名字必须**只有一份**：下拉框里那一项、侧栏那一段、
# 数据层那个常量三处各写一个字面量的话，改一处就会出现「连接框叫未分组、
# 侧栏叫默认」，而界面看上去还挺正常。
check("「默认」这个显示名只有一处定义（core.settings.DEFAULT_GROUP）",
      'DEFAULT_GROUP = "默认"' in SETTINGS_SRC
      and "UNGROUPED = DEFAULT_GROUP" in DLG_SRC)
check("数据层存的是空串而不是「默认」两个字（老版本才不会多出一个真分组）",
      "DEFAULT_GROUP" not in SETTINGS_SRC.split("class SessionStore")[1].split("def ")[0])
check("连接框那一项直接用默认分组常量，不是自己抄的字面量",
      "addItem(UNGROUPED" in DLG_SRC and 'addItem("（未分组）"' not in DLG_SRC)
regex("会话自己回答「我归哪一段」", r"def\s+group_name\(self\)", SETTINGS_SRC)
regex("侧栏按 group_name 分桶", r"buckets\.setdefault\(s\.group_name\(\)",
      SIDEBAR_SRC)
check("默认分组排最后这件事在实现里有着落（不参与码位排序）",
      "DEFAULT_GROUP" in SIDEBAR_SRC)
# 折叠
regex("分组头可点：它发 toggled 信号", r"toggled\s*=\s*QtCore\.pyqtSignal\(str\)",
      SIDEBAR_SRC)
check("MainWindow 真的接了这个信号",
      "head.toggled.connect(self._toggle_group)" in MAIN)
check("折叠态会写回配置（不是只在内存里）",
      "self.settings.collapsed_groups = sorted(self._collapsed_groups)"
      in MAIN)
regex("折叠态存进 AppSettings", r"collapsed_groups:\s*list\s*=", SETTINGS_SRC)
check("README 说了点分组标题能折叠/展开",
      "点一下分组标题就能折叠/展开整段" in readme)
check("README 说了折叠态会存下来",
      "折叠状态会记进" in readme and "配置" in readme)
check("README 说了箭头指示折叠状态", "▾" in readme and "▸" in readme)
check("_group 进了回归", "_group" in TESTS)

# ---------------------------------------------------------------- 设置页
print("\n=== 设置表格与对话框一致 ===")
# **范围只有一份真相：`AppSettings._INT_RANGE`。**
#
# 这一段原来在设置框的源码里找 `ui_size.setRange(8, 18)` 这种**字面量**，
# 再要求 README 说出同一个数 —— 也就是在核对「两份手抄的常量要一致」。
# 但两份手抄本身就是问题的根源：它们一度**并不一致**（设置框只让填
# 8–18 pt，而数据层接受 6–48 pt），于是用户手填不出一个程序本身支持的
# 字号，而「两份一致」这件事没人检查。
# 现在设置框从 `_INT_RANGE` 派生，所以该守的是**只有一份**：
#   · 设置框里不许再出现手抄的范围字面量；
#   · README 写的就是 `_INT_RANGE` 里的那个数。
from sshclient.core.settings import AppSettings as _AS  # noqa: E402

_RANGES = [
    ("界面字号", "ui_size", "ui_font_size"),
    ("终端字号", "term_size", "term_font_size"),
    ("回滚行数", "scrollback", "scrollback"),
    ("刷新间隔", "monitor_ms", "monitor_interval_ms"),
]


def readme_has_range(text, lo, hi):
    """README 的表格里有没有一格写着 ``lo–hi``。

    **抽成函数是为了能反着试一次。** 这一段原来只有一句
    ``check(f"{label}范围仍是 {lo}–{hi}", True)`` —— 第二个参数是**字面量
    True**，不管范围是什么都通过，而输出里那行绿字看着像真的在核对什么。
    真正的核对在它下面，可它没法单独被证伪：``[^|]*`` 写成 ``.*`` 也是绿的。
    下面那段负向对照就是拿这个函数去反着试的。
    """
    cell = re.search(rf"\|[^|]*{lo}\s*[–-]\s*{hi}[^|]*\|", text)
    if cell:
        return True
    # README 的上界可能带千位分隔符。不能用 str.replace：
    # "200000".replace("000", " 000") 会替换所有不重叠匹配，得到 "2 00000"。
    spaced = f"{int(hi):,}".replace(",", " ")
    return re.search(rf"\|[^|]*{lo}\s*[–-]\s*{spaced}[^|]*\|", text) is not None


for label, widget, field in _RANGES:
    lo, hi = _AS._INT_RANGE[field]
    # (1) 设置框不许自己抄一份范围
    literal = re.search(rf"{widget}\.setRange\(\s*(\d+)\s*,\s*(\d+)\s*\)", DIALOG)
    check(f"{label}：设置框的范围从 _INT_RANGE 派生，不手抄字面量",
          literal is None and f'_INT_RANGE["{field}"]' in DIALOG,
          "又出现了手抄的 setRange(%s)" % (literal.groups() if literal else ""))
    # (2) README 写的就是数据层那个数
    check(f"README 写的{label}范围就是 _INT_RANGE 的 {lo}–{hi}",
          readme_has_range(readme, lo, hi),
          f"README 里没有 {lo}–{hi}")


# 负向对照：拿一份范围对不上的 README 喂进去，上面那个判据必须判它不通过。
# 少这一段的话，readme_has_range 就算整个写反了也照样每轮全绿。
check("负向对照的前提：范围写得对时，判据确实通过",
      readme_has_range("| 终端 | 终端字号（8–28 pt）、回滚行数（0–200 000 行） |",
                       "8", "28"),
      "连写对的都判不通过，说明判据本身坏了")
check("负向对照：范围漂移之后判据必须判它不通过",
      not readme_has_range("| 终端 | 终端字号（9–99 pt）、回滚行数（0–200 000 行） |",
                           "8", "28"),
      "换了范围还判通过 —— 这条判据是恒真的")
check("负向对照：表里压根没有这一项时也要判不通过",
      not readme_has_range("| 终端 | 终端字体 |", "8", "28"),
      "表里没有也判通过，判据没在量东西")

for page in ("外观", "终端", "监控"):
    regex(f"存在「{page}」页", rf'"{page}"', DIALOG)
regex("存在「显示全部字体」开关", r"显示全部字体", DIALOG)
regex("「自动选择」是'不指定'那一项", r'AUTO_FONT = "自动选择"', DIALOG)

# 那两个"这个字体用不了"的标注是契约不是装饰：全靠它们，已保存但失效的值
# 才不会被静默丢掉。
regex("卸载的字体标注为「当前不可用」", r"当前不可用", DIALOG)
regex("被过滤的字体标注为「已过滤」", r"已过滤", DIALOG)

# ---------------------------------------------------------------- 端口转发
print("\n=== 端口转发 ===")
regex("本地转发（-L）", r'"本地端口转发 \(-L\)"', TUNNEL)
regex("SOCKS5 转发（-D）", r'"动态 SOCKS5 代理 \(-D\)"', TUNNEL)
regex("面板统计实时连接数", r"counts\.get\(port, 0\)", TUNNEL)
check("转发规则随会话持久化",
      "tunnels" in (ROOT / "sshclient/core/settings.py").read_text(encoding="utf-8"))

# -------------------------------------------------------------------- 字体
print("\n=== README 描述的字体行为 ===")
check("指名的 CJK 兜底字体存在", bool(fonts.cjk_fallback_families()))
check("装了就优先用黑体",
      not fonts.cjk_fallback_families() or fonts.cjk_fallback_families()[0] == "黑体")
check("终端字体带着兜底链",
      len(fonts.mono_font(11, "Monaco").families()) > 1)
check("界面字体保持比例字宽", not fonts._grid_even(fonts.ui_font(11, "")))
for face in ("SimSun-ExtB", "SimSun-ExtG"):
    if face in set(QtGui.QFontDatabase.families()):
        check(f"{face} 已从选择器里滤掉",
              face not in fonts.mono_font_families())
        check(f"勾「显示全部字体」后 {face} 回来",
              face in fonts.mono_font_families(include_all=True))

# -------------------------------------------------------------------- 上下文
print("\n=== 上下文（永远不让整轮失败）===")
fams = QtGui.QFontDatabase.families()
prop = sum(1 for f in fams if not fonts._grid_even(QtGui.QFont(f, 11)))
note.append(f"本机有 {len(fams)} 个字体族，其中 {prop} 个是比例字宽")
# 这个数字是机器状态而非文档事实：写这个脚本的两次运行之间它就变过。所以
# README 只许写量级，不许写确切值 —— 「400 多个」可以，「410 个」不行。
#
# 匹配**必须带上「字体」这个上下文**。原来只查 `\d+\s*个`，靠「400 多个」
# 里那个「多」来放行——于是任何「留 5 个历史」这种正常句子都会误报。
# 误报比漏报更糟：它会逼着人把中文写歪，而不是把文档写对。
HARD_CODED_FONT_COUNT = re.compile(
    r"\d+\s*个[^\n]{0,12}?字体"      # 「410 个字体族」
    r"|字体[^\n]{0,20}?\d+\s*个"      # 「字体……410 个」
)
hits = HARD_CODED_FONT_COUNT.findall(readme)
if hits:
    bad.append(f"README 写死了已装字体的确切数量（会漂移），"
               f"请写成「400 多个」这种量级：{hits[:3]}")
# 负向自查：把「400 多个」这种合法写法喂进去，绝不能报。
if HARD_CODED_FONT_COUNT.search("本机报告有 400 多个字体族"):
    bad.append("守卫自身有 bug：把「400 多个字体族」误判成写死了数量")
note.append(f"_runall.py 覆盖 {len(TESTS)} 套脚本")
for needed in ("_glyphfb", "_readme", "_zmodem", "_sftpsync", "_icons"):
    if needed not in TESTS:
        bad.append(f"{needed} 没有接进 _runall.py")

# ------------------------------------------------- 测试不许读用户真实配置
# AppSettings.load() 读的是 %APPDATA%\MavisSSH\settings.json，而 MainWindow
# 里有 5 处 self.settings.save()（切主题、折叠侧栏、保存设置、断开连接…）。
# 测试里建一个 MainWindow 就可能把用户的配置改一遍——踩过：settings.json 的
# mtime 正好落在跑全量那一刻。
# 用 AST 扫而不是 grep：注释里提到 AppSettings.load() 不算，docstring 里提到
# 更不算，只有真的调用了才算。
print("\n=== 测试脚本不许碰用户真实的配置 ===")
import ast as _ast


def _tracked_test_scripts() -> list[pathlib.Path]:
    """**进版本库的**测试脚本，不是目录里现存的全部。

    仓库里有一批一次性工具（`_design_*.py`、`_probe_*.py`、`_shots*.py`）
    是 gitignore 掉的本地脚本 —— 它们不进版本库，也就不该由门禁来管：
    一来判据的结果会因「谁本地有什么工具」而不同（同一个仓库在两台机器上
    结论不一样），二来它们是一次性评估工具，本来就不该受长期约束。

    **用 git 问「哪些文件是仓库的一部分」，而不是问「目录里有哪些文件」。**
    这样钉的是「仓库里的测试脚本不碰用户配置」这个性质。

    实现见 `tests/_tracked.py`（`_deadfn.py` 也用同一份）。
    """
    return tracked_files("tests", suffix=".py")


_TEST_SCRIPTS = _tracked_test_scripts()

_offenders = []
for _py in _TEST_SCRIPTS:
    if _py.name == "_live.py":          # 对着真服务器跑，本来就拿真实设置
        continue
    try:
        _tree = _ast.parse(_py.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for _node in _ast.walk(_tree):
        if (isinstance(_node, _ast.Call)
                and isinstance(_node.func, _ast.Attribute)
                and _node.func.attr == "load"
                and isinstance(_node.func.value, _ast.Name)
                and _node.func.value.id.endswith("AppSettings")):
            _offenders.append("%s:%d" % (_py.name, _node.lineno))
check("没有测试去读用户真实的 AppSettings（MainWindow 会把它写回去）",
      not _offenders, "、".join(_offenders))

# SessionStore() 不带参数 = 真实 %APPDATA%
_store_offenders = []
for _py in _TEST_SCRIPTS:
    try:
        _tree = _ast.parse(_py.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for _node in _ast.walk(_tree):
        if (isinstance(_node, _ast.Call)
                and getattr(_node.func, "id", "") == "SessionStore"
                and not _node.args and not _node.keywords):
            _store_offenders.append("%s:%d" % (_py.name, _node.lineno))
# AppSettings.save() 以前无条件写模块级的 SETTINGS_FILE，也就是用户真实的
# %APPDATA%\\MavisSSH\\settings.json。用干净的 AppSettings() 造对象并**不能**
# 避免写用户配置——干净的是内容，不是落盘位置。
#
# **逐套实测过**：34 套里有 4 套（_comptest / _tabclose / _tabwire / _lrzprobe）
# 真的改过那个文件的时间戳。所以这条守卫不看"源码里有没有 MainWindow"这种
# 间接线索，只看 AppSettings 有没有被指走。
import ast as _ast2

_bad_save = []
for _py in _TEST_SCRIPTS:
    _src = _py.read_text(encoding="utf-8")
    try:
        _t = _ast2.parse(_src)
    except SyntaxError:
        continue
    # **按 AST 找「保护」，不按行号窗口找。**
    #
    # 原来这里取的是「构造那一行往后 4 行的文本」，找里面有没有
    # `._path =` 或 `.save =`。这个写法有两个问题：
    #   · 注释一多就滑出窗口 —— 给 `AppSettings()` 加 5 行说明性注释，
    #     保护语句就掉到第 6 行，判据误报「没指走」，而其实指走了；
    #   · 它判的是「附近有没有那个字符串」，而不是「这个对象有没有被指走」。
    #
    # 现在改成：**先找出所有 `X = AppSettings()` 的绑定名 X，再在整棵树里
    # 找对 X 的 `._path = ...` / `.save = ...` 赋值。** 与注释多少无关，
    # 也不受变量名影响。
    #
    # 认两种保护写法：`._path = ...` 指到临时目录，或者 `.save = ...`
    # 直接把写盘废掉。只认一种的话另一处立刻误报。
    _bound = {}          # 变量名 -> 构造所在的行号
    for _n in _ast2.walk(_t):
        if not (isinstance(_n, _ast2.Call)
                and isinstance(_n.func, _ast2.Attribute)
                and _n.func.attr == "AppSettings"):
            continue
        # 找到把它赋给变量的那条语句
        for _stmt in _ast2.walk(_t):
            if not isinstance(_stmt, _ast2.Assign):
                continue
            if _stmt.value is _n and isinstance(_stmt.targets[0], _ast2.Name):
                _bound[_stmt.targets[0].id] = _stmt.lineno

    _protected = set()
    for _n in _ast2.walk(_t):
        # `x._path = ...` / `x.save = ...`
        if isinstance(_n, _ast2.Assign):
            for _tg in _n.targets:
                if (isinstance(_tg, _ast2.Attribute)
                        and isinstance(_tg.value, _ast2.Name)
                        and _tg.attr in ("_path", "save")):
                    _protected.add(_tg.value.id)
        # `x = ...` 之后再 `x._path = ...`（同名不同对象时也算保守放过）

    for _name, _ln in _bound.items():
        if _name not in _protected:
            _bad_save.append("%s:%d" % (_py.name, _ln))
check("测试里的 AppSettings 都指走了（没指向用户真实配置）",
      not _bad_save, "；".join(_bad_save))

check("没有测试建默认 SessionStore（那指向真实的 sessions.json）",
      not _store_offenders, "、".join(_store_offenders))

# **元断言：确认「只扫进版本库的脚本」这条机制真的在工作。**
#
# 这条守卫如果某天退化成「扫全目录」，本地那些 gitignore 掉的一次性工具
# 就会重新混进来 —— 判据的结论会因「谁本地有什么工具」而不同，
# 同一个仓库在两台机器上给出不一样的结论。所以显式断言：
# 被扫的脚本集合必须是 git 跟踪的那一份，且不含任何被 ignore 的文件。
check("扫描范围确实是「进版本库的测试脚本」",
      _TEST_SCRIPTS == _tracked_test_scripts()
      and all(p.exists() for p in _TEST_SCRIPTS),
      "%d 个脚本" % len(_TEST_SCRIPTS))
_ignored_leak = [p.name for p in _TEST_SCRIPTS
                 if p.name.startswith("_design_")
                 or p.name.startswith("_probe_")
                 or p.name.startswith("_patch_")]
check("被 ignore 的一次性工具没混进扫描范围",
      not _ignored_leak, "、".join(_ignored_leak))

# ------------------------------------------------------- ZMODEM 新声明（双向）
#
# 这几条是 ZMODEM 那次改动写进 README 的能力，**双向**核对：README 写了就源码
# 必须真有，源码有而 README 没写也要报。只查源码的话 README 怎么漂移都会通过
# —— 那个漏洞我自己踩过一次。
MW = (ROOT / "sshclient/ui/main_window.py").read_text(encoding="utf-8")
LRZ = (ROOT / "sshclient/core/lrzsz.py").read_text(encoding="utf-8")
MSGBOX = (ROOT / "sshclient/ui/msgbox.py").read_text(encoding="utf-8")
CLAIMS = (
    # 「可多选」现在由 msgbox.open_files 承担（原来是 QFileDialog 的静态方法）。
    # 真正让"多选"成立的是 msgbox 里那一行 FileMode，调用点只是入口——
    # 少了它这个能力就没了，所以下面单独再钉一条。
    ("可以多选", MW, "main_window.py", "msgbox.open_files("),
    ("同一时间只允许一个传输", MW, "main_window.py", "transfer_ctrl.busy"),
    ("引号会重新加回", MW, "main_window.py", "lrzsz.remote_words("),
)
for claim, code, where, needle in CLAIMS:
    in_readme = claim in readme
    in_code = needle in code
    if in_readme and not in_code:
        bad.append(f"README 宣称「{claim}」，但 {where} 里没有 {needle}")
    if in_code and not in_readme:
        bad.append(f"{where} 里有 {needle}，README 却没有说明「{claim}」")

if "FileMode.ExistingFiles" not in MSGBOX:
    bad.append("README 宣称「可以多选」，但 msgbox.open_files 没有设 "
               "FileMode.ExistingFiles（单选框，宣称与实现不符）")

# ------------------------------------------------------------ 许可与协议文件
#
# 「许可」这一节最容易变成一句没人核过的漂亮话：README 里写着 GPL-3.0，
# 仓库里那份 LICENSE 其实是 MIT，或者 PyQt6 哪天换了许可而这里没跟上 ——
# 都不报错，装出来的包照样能跑。所以这一段核的是**文件真的在、内容真的是
# 那一份、以及分发路径上确实带着它们**。
print("\n=== 许可：文件、内容与分发路径 ===")
NOTICES = (ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
NSI = (ROOT / "installer/mavisssh.nsi").read_text(encoding="utf-8")
SPEC = (ROOT / "installer/mavisssh.spec").read_text(encoding="utf-8")
PKG_INIT = (ROOT / "sshclient/__init__.py").read_text(encoding="utf-8")

lic_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
check("LICENSE 是 GPL-3.0 全文（不是别的协议，也不是占位）",
      "GNU GENERAL PUBLIC LICENSE" in lic_text and "Version 3" in lic_text,
      "开头不是 GPLv3 的标准文本")
check("README 的「许可」一节声明 GPL-3.0（不是「内部项目」）",
      "GPL-3.0-only" in readme and "内部项目。" not in readme)
check("README 说清了为什么不是 MIT（PyQt6 的授权是硬约束）",
      "PyQt6" in readme and "MIT" in readme)
regex("包里的版权头与 README 一致", r"Copyright \(C\) 2026\s+Albert D", PKG_INIT)
check("README 写了版权归谁", "Copyright © 2026 Albert D" in readme)

# **双向**：README / 第三方声明里提到的每一份协议全文都得真在仓库里，
# 反过来 LICENSES/ 里也不该躺着没人引用的文件（那说明要么漏写文档，
# 要么是份多余文件）。
referenced = set()
for text in (readme, NOTICES):
    referenced |= set(re.findall(r"LICENSES/([\w.-]+\.txt)", text))
for name in sorted(referenced):
    check(f"引用的协议全文存在：LICENSES/{name}",
          (ROOT / "LICENSES" / name).is_file())
# 负向自查：这个判据得能判红，否则上面那圈「文件存在」可能恒真。
check("负向自查：不存在的协议文件判不通过",
      not (ROOT / "LICENSES" / "GPL-9.9.txt").is_file())
present = {p.name for p in (ROOT / "LICENSES").glob("*.txt")}
check("LICENSES/ 里没有没人引用的协议全文",
      present == referenced, f"多出来：{sorted(present - referenced)}")
check("bin/ 里的预编译二进制有归属声明",
      (ROOT / "bin/THIRD_PARTY_NOTICE.md").is_file()
      and "lrzsz-win32" in (ROOT / "bin/THIRD_PARTY_NOTICE.md").read_text(encoding="utf-8"))

# 分发路径：协议文件必须真的进到用户拿到手的东西里。
regex("NSIS 向导有一页展示 LICENSE", r'MUI_PAGE_LICENSE\s+"\.\.\\LICENSE"', NSI)
check("NSIS 把协议文件装进安装目录", 'File /nonfatal "..\\LICENSES\\*.txt"' in NSI)
check("打包产物里也带上协议全文", '"LICENSES"' in SPEC and "THIRD_PARTY_NOTICES" in SPEC)

# 负向自查：守卫自身也得能红。把每条 claim 从 README 里抹掉，对应的
# "源码有而 README 没写" 就必须触发——不然这组断言可能恒真。
for claim, _code, _where, needle in CLAIMS:
    if claim not in readme or needle not in _code:
        continue
    trimmed = readme.replace(claim, "")
    if not (needle in _code and claim not in trimmed):
        bad.append(f"守卫自身有 bug：把 README 里的「{claim}」删掉也不报错")

for n in note:
    print("  -", n)

print()
if bad:
    print(f"FAIL（{len(bad)} 项）：")
    for b in bad:
        print("  -", b)
    sys.exit(1)
print("README 与代码一致：全部检查通过")
sys.exit(0)
