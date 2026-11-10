"""同一模块 / 同一个类里的重复定义（= 没人能调到的死代码）。

**这个项目被它咬过两次**：`snippets.py` 里重复的 `audit_builtins`，和
`tests/_snippets.py` 里被新签名覆盖掉的旧版 `_writer_main`。两次都是
后一个静默覆盖前一个，前一个变成只有 grep 才找得到的幽灵函数。

**为什么不能用 grep**：按文本比同名会满屏噪音 —— `sftp_panel.py` 里有 5 个
`__init__`（5 个类，`panel` / `upload` / `download`…），那是正常的。所以必须
按 **AST** 走，而且要分清"同一模块的函数"和"同一个类的方法"。

**还要排除 property 的 getter/setter**：它们天然同名，
``_zmodem_pending_direction`` 就是一个合法的 getter+setter 对。不排除的话
每个 property 都报一次，真死函数直接被淹掉。

**顺便挡一类错**：``except`` 少写一个异常基类。Python 不会告诉你
``UnicodeEncodeError`` 抓不到 —— 那是运行时的事，见 ``_snippets.py`` 第 31 组。
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._tracked import tracked_files  # noqa: E402

TARGETS = ["sshclient", "tests", "main.py"]

# **只扫进版本库的文件。** 工作区里还有 `tests/_scratch/`（gitignore 的历史
# 归档区）里几十个一次性脚本 —— 用 `rglob` 扫目录会把它们一起算进来，
# 于是「幽灵函数」报的是**根本不在仓库里的文件**，而同一个仓库在两台
# 机器上（一个有 _scratch、一个没有）会得出不同结论。
# 详见 `tests/_tracked.py` 的说明。
files = tracked_files(*TARGETS)

problems = []
checked = 0


def _is_accessor(node) -> bool:
    """property 的 setter / deleter 装饰器。"""
    for d in (node.decorator_list or []):
        if isinstance(d, ast.Attribute) and d.attr in ("setter", "deleter"):
            return True
    return False


def _is_property(node) -> bool:
    for d in (node.decorator_list or []):
        if isinstance(d, ast.Name) and d.id == "property":
            return True
    return False


for f in files:
    rel = f.relative_to(ROOT)
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
    except SyntaxError as exc:
        problems.append("%s 语法错误: %s" % (rel, exc))
        continue
    checked += 1

    mod_fn = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            mod_fn.setdefault(node.name, []).append(node.lineno)
    for name, lns in mod_fn.items():
        if len(lns) > 1:
            problems.append("%s 模块级函数 %s 定义了 %d 次，行 %s"
                            % (rel, name, len(lns), lns))

    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        meths = {}
        for m in cls.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                meths.setdefault(m.name, []).append(m)
        for name, defs in meths.items():
            if len(defs) <= 1:
                continue
            # getter + setter/delegate 是合法的同名，排除掉
            if _is_property(defs[0]) and all(_is_accessor(d) for d in defs[1:]):
                continue
            problems.append("%s 类 %s 的方法 %s 定义了 %d 次，行 %s"
                            % (rel, cls.name, name, len(defs),
                               [d.lineno for d in defs]))

print("检查了 %d 个文件" % checked)
if problems:
    print("== 发现 %d 处重复定义 ==" % len(problems))
    for p in problems:
        print("  " + p)
    print("\n重复定义 = 后一个静默覆盖前一个，前一个成了只有 grep 才找得到的"
          "幽灵函数。")
    sys.exit(1)
print("没有同一模块 / 同一类内的重复定义")
