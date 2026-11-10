"""用仓库自带的虚拟环境启动 MavisSSH。Windows / macOS / Linux 通用。

替代原来的 `run.bat`。那份只能在 Windows 上双击运行，而它做的事本来就只有
两件：切到脚本所在目录、用 `.venv` 里的解释器跑 `main.py`。

用法：

    python run.py              # 等价于 .venv 里的 python main.py
    python run.py --debug      # 参数原样透传给 main.py

想省掉这一层就直接跑解释器也一样：

    .venv\\Scripts\\python.exe main.py      # Windows
    .venv/bin/python main.py               # macOS / Linux
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

# venv 里的解释器在两个平台下目录名不同。
CANDIDATES = (ROOT / ".venv" / "Scripts" / "python.exe",
              ROOT / ".venv" / "bin" / "python")


def venv_python() -> pathlib.Path | None:
    for path in CANDIDATES:
        if path.exists():
            return path
    return None


def main() -> int:
    interpreter = venv_python()
    if interpreter is None:
        # 没找到就用当前解释器，但**说清楚**——安静地降级会让人以为跑的是
        # 配好的环境，缺包时排查起来很绕。
        print("没找到 .venv（找过 %s）。用当前解释器 %s 启动。\n"
              "需要先建环境的话：\n"
              "    python -m venv .venv\n"
              "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
              % ("、".join(str(p.relative_to(ROOT)) for p in CANDIDATES),
                 sys.executable),
              file=sys.stderr)
        interpreter = pathlib.Path(sys.executable)

    # 已经在目标解释器里就别再套一层，否则进程树多一层、Ctrl+C 收得别扭。
    if os.path.samefile(interpreter, sys.executable):
        os.chdir(ROOT)
        import runpy
        sys.argv = [str(ROOT / "main.py")] + sys.argv[1:]
        runpy.run_path(str(ROOT / "main.py"), run_name="__main__")
        return 0

    import subprocess
    return subprocess.call([str(interpreter), str(ROOT / "main.py")]
                           + sys.argv[1:], cwd=str(ROOT))


if __name__ == "__main__":
    sys.exit(main())
