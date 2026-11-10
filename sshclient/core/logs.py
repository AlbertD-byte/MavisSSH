"""应用日志：写到**当前工作目录**下的 `logs/`。

放在 CWD 而不是 `%APPDATA%`，是因为这个 app 是在开发机上直接
`python main.py` 跑的，日志跟着项目目录走最好找。代价是从别的目录启动
就会写到别处——所以启动时会把实际路径打进日志第一行，别到时候找错地方。

轮转而不是无限追加：一个跑飞的连接循环能一天写出几个 G，不轮转迟早把盘撑满。
"""
from __future__ import annotations

import logging
import sys
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIRNAME = "logs"
LOG_FILENAME = "mavisssh.log"
MAX_BYTES = 2 * 1024 * 1024      # 单个文件 2 MB
BACKUP_COUNT = 5                 # 留 5 个历史，合计约 12 MB
FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def log_dir(base: Path | None = None) -> Path:
    """日志目录：``base``/logs，``base`` 默认取当前工作目录。"""
    root = Path.cwd() if base is None else Path(base)
    return root / LOG_DIRNAME


def _writable_dir(base: Path | None = None) -> Path:
    """挑一个**真的写得进去**的日志目录。

    直接用 CWD 看着最自然，但 macOS 从 `.app` 双击启动时 CWD 是 `/`，
    于是 `mkdir /logs` 权限拒绝——而这行一旦抛异常，异常发生在 `main()`
    还没建完 QApplication 的时候，整个应用起不来。所以这里逐个试，
    失败就退到下一档，实在全都不行才用临时目录。
    """
    candidates = []
    if base is not None:
        candidates.append(base / LOG_DIRNAME)
    else:
        candidates.append(Path.cwd() / LOG_DIRNAME)
        # 配置目录是必然存在且可写的（_config_dir 已经 mkdir 过）。
        from .settings import CONFIG_DIR
        candidates.append(CONFIG_DIR / LOG_DIRNAME)
        candidates.append(Path(tempfile.gettempdir()) / "MavisSSH" / "logs")

    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return c
        except Exception:  # noqa: BLE001
            continue
    # 走到这里说明连临时目录都写不进去，那是真的没救了——让调用方决定。
    return candidates[-1]


def setup(level: int = logging.INFO, base: Path | None = None,
          console: bool = True) -> Path:
    """配置根 logger，返回日志目录。重复调用只生效一次。

    `delay=True` 是有意的：脚本如果只是 import 了这个模块却从没记录任何东西，
    就不该在项目里凭空多出一个空的 `logs/` 目录。
    """
    global _configured
    directory = _writable_dir(base)
    if _configured:
        return directory
    root = logging.getLogger()
    root.setLevel(level)
    # 让重复 import 或测试反复调用 setup() 不会堆出 N 个重复 handler——
    # 那会让每条日志在文件里出现 N 次。
    fmt = logging.Formatter(FORMAT, datefmt=DATEFMT)

    fileh = RotatingFileHandler(
        directory / LOG_FILENAME,
        maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
        encoding="utf-8", delay=True)
    fileh.setFormatter(fmt)
    root.addHandler(fileh)

    if console:
        # GUI 程序没有 stdout，但开发时从 PowerShell 启动，WARNING 以上
        # 打到控制台仍然有用；INFO 级别不吵控制台。
        sh = logging.StreamHandler(sys.stderr)
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    _configured = True
    return directory


def get_logger(name: str) -> logging.Logger:
    """取一个具名 logger。名字建议用模块名，格式里会显示。"""
    return logging.getLogger(name)


def reset_for_tests() -> None:
    """只给测试用：卸掉所有 handler 并允许重新 setup()。

    没有它，一个测试进程里换目录再 setup() 是没有效果的——`_configured`
    已经是 True，第二次调用直接返回，handler 还指着上一个目录。
    """
    global _configured
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass
    _configured = False


def redact(value: str, keep: int = 0) -> str:
    """把敏感值变成可安全写进日志的样子。

    密码、口令、私钥路径**根本不该进日志**——这个函数是给那些确实需要
    辨认「是哪一条会话/哪个端口」的场合用的，比如
    `redact("10.0.0.7", keep=3)` -> `10.0.0.7`，`redact("hunter2")` -> `***`。
    """
    if not value:
        return ""
    if keep and len(value) > keep:
        return value[:keep] + "***"
    return "***"
