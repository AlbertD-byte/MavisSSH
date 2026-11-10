"""判据专用的共享辅助：**哪些文件属于这个仓库**。

## 为什么需要它

判据要扫源码，而工作区里除了仓库的文件，还躺着三类**不在版本库里**的东西：

- `tests/_scratch/`、`tests/_design_*.py`、`tests/_probe_*.py` —— 一次性探针与
  设计稿工具，gitignore 掉，跑完即弃
- `docs/_shots/`、`docs/_design/`、`docs/commits/` —— 本地留档
- `__pycache__/`、`.venv/` 等

**注意 `docs/img/` 不在其中** —— 那里放的是 README 引用的**发布截图**，
是仓库资产，要提交。

判据如果用 `rglob("*.py")` 扫目录，就会把这些一起扫进来。后果有两个：

1. **结论因「谁本地有什么」而不同。** 同一个仓库在两台机器上跑出不一样的结果，
   这让判据失去意义 —— CI 上是绿的，本地是红的，反之亦然。
2. **报告的是不存在的问题。** `_deadfn.py` 曾把 `tests/_scratch/` 里 30 个历史
   脚本的重复定义报成「幽灵函数」，而那些文件根本不在仓库里。

所以判据要问的不是「目录里有哪些文件」，而是「**哪些文件是仓库的一部分**」。
这个问题只有 git 能回答。

## 用法

```python
from tests._tracked import tracked_files

for p in tracked_files("tests", suffix=".py"):
    ...
```

## 为什么失败时退回扫目录

git 不可用时（源码包解压、没有 .git、CI 里用了奇怪的方式）宁可多扫几个文件，
也不要让守卫**静默失效** —— 守卫失效比误报更坏。退回时会打印一行提示，
这样「这条判据现在在宽松模式下跑」这件事是可见的，而不是悄悄发生。
"""
from __future__ import annotations

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent


def tracked_files(*prefixes: str, suffix: str = ".py") -> list[pathlib.Path]:
    """返回**进版本库**的、位于 *prefixes* 下的文件。

    *prefixes* 相对于仓库根，例如 ``"tests"``、``"sshclient"``、``"main.py"``。
    前缀既可以是目录，也可以是具体文件。

    git 不可用或没有匹配结果时，退回扫目录（并打印提示）。
    """
    args = ["git", "ls-files", "--cached"]
    if prefixes:
        args += list(prefixes)
    try:
        out = subprocess.run(
            args, cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            rels = [r for r in out.stdout.split("\n") if r.strip()]
            files = [ROOT / r for r in rels]
            if files:
                if suffix:
                    files = [p for p in files if p.name.endswith(suffix)]
                return sorted(files)
    except (OSError, subprocess.SubprocessError):
        pass

    print("  （git ls-files 不可用，退回扫目录 —— 守卫进入宽松模式）")
    files: list[pathlib.Path] = []
    for pre in prefixes:
        p = ROOT / pre
        if p.is_dir():
            files.extend(sorted(p.rglob("*" + suffix)) if suffix
                         else sorted(p.rglob("*")))
        elif p.exists():
            files.append(p)
    return sorted(files)
