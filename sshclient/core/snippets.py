"""远端命令片段库：存一组常用命令，点一下**插入**到远端的行缓冲里。

**只插入，绝不自动回车。** 这是整个功能唯一不可让步的一条：片段写进的是
远端 shell 的行编辑缓冲（`rz`/`vim` 里面则是它们各自的缓冲），用户还能退格、
Ctrl+U、Ctrl+C。如果插入完顺手补一个 `\\r`，那么「我想看看这条命令会干什么」
就变成了「这条命令已经干了」——在生产机上代价是不可逆的。

顺带一个协议层的好处：传输（ZMODEM 走 shell 通道裸字节复用）期间绝不能插，
因为那时通道属于 `sz`/`rz`，写进去的字符会被当成 ZMODEM 载荷。

**为什么单独一个文件，不挂到 ``sessions.json`` 上。** 会话文件带口令密文，
混进"用户随手写的命令片段"会让两种生命周期不同的数据绑死在一起；更现实的是
``sessions.json`` 曾经因为一个 UTF-8 BOM 触发 ``json.JSONDecodeError``，而
``load()`` 之后的裸 ``except`` 把它吞成全默认值，紧跟着的一句 ``save()``
就把用户自己存的会话**永久清空**。片段库不承担这个风险。

**同理，``load()`` 之后绝不在同一条路径上 ``save()``。** 没改过就不该写盘。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .logs import get_logger

log = get_logger("mavisssh.snippets")


def _config_dir() -> Path:
    """和 :mod:`settings` 同一套目录，但**不共用那个模块的常量**。

    共用常数看起来更省事，实则让片段库在 ``settings`` 还没导入时就得把它拉起来
    （连带 crypto、uuid 一堆），而且测试里想换一个临时目录就得把整个 settings
    一起换掉。三平台分支和 settings 保持一致，重复三行比引入依赖便宜。
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    p = Path(base) / "MavisSSH"
    p.mkdir(parents=True, exist_ok=True)
    return p


CONFIG_DIR = _config_dir()
SNIPPETS_FILE = CONFIG_DIR / "snippets.json"


#: ``_config_dir()`` 里的三个分支标记，与 ``settings._config_dir()`` 的一致性
#: 由 ``tests/_portable.py`` 守。新增平台分支时**记得把标记也补上**，否则守卫不会点亮。
_PLATFORM_BRANCHES = ("win32", "darwin", "linux")


# ---------------------------------------------------------------------------
# 默认片段
# ---------------------------------------------------------------------------

#: 选内置命令的三条硬规矩（改这个表之前先读一遍）：
#:
#: 1. **只读。** 不写文件、不改进程、不动配置。一个片段被误插两次的代价必须是 0。
#: 2. **不需要参数。** 需要参数的没法做成"点一下就好"，用户还得去改，反而更烦。
#: 3. **不重定向、不管道进写操作。** ``>`` 落到 ``/etc`` 上就是一个事故。
#:
#: 每条都注明它回答什么问题——面板上直接显示，免得用户面对一排命令猜。
BUILTIN_SNIPPETS: tuple[tuple[str, str, str], ...] = (
    ("磁盘用量", "df -h", "各挂载点还剩多少空间"),
    ("内存占用", "free -h", "内存和 swap 用得怎么样"),
    ("最占内存的进程", "ps aux --sort=-%mem | head -n 15", "内存黑洞在谁身上"),
    ("监听端口", "ss -tulpn", "谁在监听、绑在哪个地址"),
    ("最近日志", "journalctl -n 100 --no-pager", "systemd 日志的最后一百行"),
    ("磁盘大头", "du -sh /* 2>/dev/null | sort -h", "根目录下谁最占地"),
    ("启动耗时", "systemd-analyze blame", "开机时哪个 unit 最磨蹭"),
    ("文件面板", "vim /", "直接用 vim 浏览当前根目录"),
    ("网络连通", "ping -c 4 1.1.1.1", "到公网通不通"),
    ("路由表", "ip route", "默认出口走哪条"),
)

#: 破坏性关键字。用来做**自检**：内置集里出现任何一个都当 bug 报出来。
#: 判据是命令的首个 token 加常见的重定向/递归删除形态，不是黑名单打分——
#: 宁可误报一次也不让 ``rm -rf`` 混进默认集。
FORBIDDEN_TOKENS = (
    "rm", "rmdir", "del", "mkfs", "dd", "shred", "fdisk", "parted",
    "shutdown", "reboot", "halt", "poweroff", "init",
    "kill", "killall", "pkill",
    "chmod", "chown", "chattr",
    "mv", "cp", "install",
    "userdel", "useradd", "passwd",
    "iptables", "nft", "systemctl", "service", "mount", "umount",
    "truncate", "sed", "tee", "patch",
    # 评审补的三个「一键毁机器」的：擦设备、擦文件系统、清空 crontab。
    # 之前不在表里，`wipe -a` / `wipefs -a` / `crontab -r` 全部放行。
    "wipe", "wipefs", "crontab",
)

#: 第一个词无害、但**子命令**是刀的命令。漏过 `git clean -fdx`：
#: `git` 本身不在黑名单里，而危险全在第二个词上。
SUBCOMMAND_HOSTS = frozenset({
    "git", "docker", "podman", "kubectl", "npm", "yarn", "pnpm",
    "apt", "apt-get", "yum", "dnf", "apk", "brew", "pip", "cargo",
    "go", "helm", "terraform", "systemctl", "snap",
})

#: 这些主命令下,这些子命令是不可逆的。
DESTRUCTIVE_SUBCOMMANDS = frozenset({
    "clean", "reset", "checkout", "restore", "rebase", "filter-branch",
    "prune", "rm", "rmi", "system", "volume", "down", "push", "kill",
    "purge", "autoremove", "remove", "destroy", "delete", "uninstall",
})

#: ``VAR=value`` 形式的赋值前缀。
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: 这些词自己不做危险的事，**剥掉之后后面那个才是真命令**。
#:
#: 为什么必须剥：实测 19 种危险写法里，下面这些的**第一个词都是无害的** ——
#: ``sudo rm`` / ``env rm`` / ``time rm`` / ``busybox rm`` / ``FOO=1 rm``。
#: 包装词是"提权、改环境、换解释器"的前缀，刀在它**后面**那一个。
#: 只查 ``words[0]`` 的版本会把这一整类全部放过去。
_PREFIX_WRAPPERS = frozenset({
    "sudo", "doas", "env", "time", "nice", "ionice", "nohup", "setsid",
    "stdbuf", "unbuffer", "command", "exec", "busybox", "xargs", "su",
    "timeout", "watch",
})

#: 每个包装词里**哪些选项自带值**（``sudo -u root``、``nice -n 5``），剥的时候
#: 要连值一起跳。
#:
#: **必须按"选项"列，不能按"包装词"列。** 评审实测：写成
#: ``{"sudo", "doas", "nice", ...}`` 之后，``sudo -n rm -rf`` 里的 ``rm``
#: 被当成 ``-n``（non-interactive，**根本不带值**）的值吃掉了 —— 35 条对抗
#: 用例里 11 条漏，**全是这一个根因**，而且会复利：``sudo -n git clean -fdx``
#: 的子命令判据被连坐打穿。
#:
#: 附着式选项（``-n5`` / ``-o0`` / ``-uroot``）和 ``--user=root`` 天然不在表里，
#: 所以**不需要单独判断长度**：``"-n5" in value_opts`` 是 False，值已经贴在
#: 选项上，不该再吃下一个词。
_WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U",
             "--user", "--group", "--chdir", "--prompt", "--role", "--type"},
    "doas": {"-u", "-C", "-s", "--user"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "env": {"-u", "-C", "-S", "--unset", "--chdir", "--split-string"},
    "unbuffer": {"-e", "-o", "-i", "--env", "--error", "--output", "--input"},
    "su": {"-c", "-s", "-g", "-G", "--command", "--shell", "--group"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "watch": {"-n", "-W", "--interval", "--precise"},
    "xargs": {"-I", "-L", "-n", "-d", "-s", "-P", "-a", "-E",
              "--replace", "--max-lines", "--max-args", "--delimiter",
              "--max-repl-args", "--max-procs", "--arg-file", "--eof"},
}

#: 这些包装词在第一个真命令之前还要吃掉几个**非选项**位置参数（``su root``）。
_WRAPPER_POSITIONALS = {"su": 1}

#: ``timeout 5`` / ``watch 1`` 的时长是个位置参数而不是选项，得按"长得像时长"
#: 认。漏了它 ``timeout 5 rm -rf`` 的头词就成了 ``5``。
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")
_WRAPPERS_WITH_DURATION = frozenset({"timeout", "watch"})

#: 主命令的**全局选项**里自带值的那些。不跳的话子命令就不在紧邻位置：
#: ``git -C /tmp/x clean -fdx`` 的 ``clean`` 在第三，``apt-get -y purge nginx``
#: 的 ``purge`` 在第二。只看紧邻 token 的话，容器命令的全局选项一挡就全漏。
_GLOBAL_VALUE_OPTS = frozenset({
    "-C", "-c", "-u", "-g", "-H", "-P", "-i", "-m", "-w", "-t", "-o",
    "--chdir", "--config", "--context", "--cwd", "--env", "--exec-path",
    "--git-dir", "--host", "--index-url", "--kubeconfig", "--load-profile",
    "--manifest-path", "--namespace", "--option", "--prefix", "--python",
    "--root", "--target", "--target-dir", "--target-release", "--user",
    "--work-tree", "--workdir", "--workspace",
})

#: 只从这些主命令的**引号参数**里再抽一层命令出来复查（``sh -c '…'``、
#: ``awk '{system("…")}'``、``python -c '…'``）。加这道门是为了不误报 ——
#: ``echo 'rm -rf'`` 是无害的，但 ``python -c 'os.system("rm -rf …")'`` 不是。
_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish",
    "awk", "gawk", "mawk", "perl", "python", "python2", "python3",
    "node", "ruby", "php", "ssh", "su", "eval", "osascript",
    "powershell", "pwsh", "cmd", "busybox", "sudo", "timeout", "make",
})

#: ``-c`` / ``-e`` 后面跟引号串（``sh -c '…'``）。
_FLAG_PAYLOAD = re.compile(r"(?:^|\s)-[ceE]\s+(['\"])(?P<body>.*?)\1", re.DOTALL)

#: 解释器内部再调用 shell（``os.system("…")``、``subprocess.popen``、``eval("…")``）。
#: **``run`` 也在表里**：`subprocess.run(...)` 是最常写的那个，漏掉它等于
#: 上面的 `system` 白写。
_NESTED_CALL = re.compile(
    r"(?:\b\w+\.)?\b(?:system|popen|run|execv\w*|spawn\w*|check_output|check_call"
    r"|call|eval)\s*\(\s*(['\"])(?P<body>.*?)\1", re.DOTALL)

#: 列表形式的参数（``subprocess.run(["rm", "-rf", "/var"])``）——
#: 上面那条只认引号串，这一类得单独捞出来再拼成一行命令。
_LIST_PAYLOAD = re.compile(r"\(\s*\[(?P<body>[^\[\]]*)\]")

#: shell 函数体（``cleanup() { rm -rf …; }``）。不剥它的话整条命令的第一个
#: 词是 ``cleanup``，干净得很。
_FUNC_BODY = re.compile(r"\{(?P<body>[^{}]*)\}")

#: 段里的**第一个** token，必须从**原始文本**取（不能取 ``shlex`` 的结果）。
_RAW_HEAD = re.compile(r"^\s*([^\s|;&]+)")

#: Windows 可执行后缀。
_EXE_SUFFIX = re.compile(r"\.(exe|com|bat|cmd)$", re.IGNORECASE)

#: 嵌套复查最多再往里走几层。防的是"把载荷再包一层就绕过去"，
#: 同时也保证递归一定收敛 —— 用户可以写出自我相似的字符串。
_MAX_DEPTH = 3


@dataclass
class Snippet:
    """一条片段。``id`` 稳定，用户改标题/命令都不换它。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    title: str = ""
    command: str = ""
    builtin: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "command": self.command,
                "builtin": self.builtin}

    @classmethod
    def from_dict(cls, raw: dict) -> Snippet | None:
        """从磁盘上的一条记录还原；脏数据返回 None 而不是抛。

        这里**不吞异常**：调用方要能区分"这条坏了"和"整个文件坏了"，
        前者跳过、后者退回默认集，两种情况在界面上该有不同表现。

        **带换行的命令一律拒收。** 片段的唯一作用是把一条命令插进远端的输入
        行，而 ``insert`` 原样把字节发出去 —— 命令里嵌一个 ``\n`` 就等于
        客户端替用户按了回车，把后半截执行掉。这与"只插入、不执行"这条
        唯一不可让步的承诺正面冲突，所以要在**入库时**就挡住，而不是等到
        发送时再想办法（那时候已经晚了）。
        """
        if not isinstance(raw, dict):
            return None
        title, command = raw.get("title"), raw.get("command")
        if not isinstance(title, str) or not isinstance(command, str):
            return None
        title, command = title.strip(), command.strip()
        # 空命令的片段点了等于没点，不如不要
        if not title or not command:
            return None
        # **标题也要查。** 列表里用户看到的就是标题，U+202E 出现在标题里才是真正的
        # 显示欺骗入口 —— 命令里那个用户不会逐字看。
        if _has_line_break(command) or _has_invisible(command) \
                or _has_invisible(title) \
                or _has_surrogate(command) or _has_surrogate(title):
            return None
        rid = raw.get("id")
        return cls(id=str(rid) if isinstance(rid, str) and rid else
                   uuid.uuid4().hex[:12],
                   title=title, command=command,
                   builtin=bool(raw.get("builtin")))


#: 会被 shell 当成"另起一行"的字符。U+2028/U+2029 在 Python 的 str 里不是
#: 换行，但远端按字节解出来未必安全，所以一并挡掉。
_LINE_BREAKS = "\r\n\x0b\x0c\x85\u2028\u2029"

#: **看不见但会骗人的字符。** 最毒的是 U+202E（RIGHT-TO-LEFT
#: OVERRIDE）：它让后面的字符按 RTL 渲染，于是 `rm -rf /tmp/x` 可以在
#: 界面上显示成完全不同的样子。片段列表是给人看、给人双击的，
#: 标题和命令里出现这些字符就等于在骗用户——而用户点下去
#: 执行的是**屏幕上那串字符**。零宽字符同理：肉眼完全看不见，
#: 却会进到发给远端的字节里。
#: **注意这是元组，不是括号包起来的字符串。** 原来是后者，而
#: ``_has_invisible`` 里的 ``any(c in command for c in _INVISIBLES)`` 对
#: 字符串和元组都成立，所以两种写法都能跑 —— 直到有人加了一条**尾逗号**，
#: 它就悄悄变成了 1 元组：``c in <1 元组>`` 变成"元素相等"而不是"子串包含"，
#: 于是**整张表静默失效**，而没有一行语法错误。
#: 加新字符时记得把逗号补齐，别再堆成一条大字符串。
_INVISIBLES = (
    "\u200b",                          # zero-width space
    "\u200c", "\u200d",                # ZWNJ / ZWJ
    "\u200e", "\u200f",                # LRM / RLM
    "\u202a", "\u202b", "\u202c",      # LRE / RLE / PDF
    "\u202d", "\u202e",                # LRO / RLO  <- 那个能骗过人的
    "\u2066", "\u2067", "\u2068", "\u2069",   # bidi isolate
    "\ufeff",                          # ZWNBSP
    "\u00ad",                          # soft hyphen
    # **制表符**。它渲染出来只是空白，肉眼看不出和空格的区别，但插进远端
    # 行缓冲之后会被 readline 当成**补全请求**——用户看到的是远端补出来的
    # 候选，而不是他写的那一个 Tab。这与"原样插入"矛盾。
    "\t",
)


def _has_line_break(command: str) -> bool:
    """命令里有没有换行 / 回车。**入库时的硬拒绝条件。**"""
    return any(c in command for c in _LINE_BREAKS)


def _has_invisible(command: str) -> bool:
    """命令里有没有零宽 / 双向控制字符。**入库时的硬拒绝条件。**"""
    return any(c in command for c in _INVISIBLES)


def _has_surrogate(text: str) -> bool:
    """有没有 UTF-16 意义上的孤立代理字符（U+D800–U+DFFF）。**入库时的硬拒绝条件。**

    **这一类不是"看不见"，是"根本编不出来"。** ``"\\ud800"`` 在 JSON 里
    **完全合法**（JSON 允许转义任意 ``\\uXXXX``），``json.loads`` 会老老实实
    读成 Python str 里的一个孤立代理；而 ``json.dumps(..., ensure_ascii=False)``
    在写盘时抛 ``UnicodeEncodeError``。

    评审实测的完整链条：一份只含合法 ``"\\ud800"`` 转义的 ``snippets.json``
    **静默加载成功**（``loaded_ok=True``，不隔离、不告警），然后在下一次
    **任何**写操作时把整个应用弄死 —— 用户什么都不用做，点一下「恢复内置」
    就够。而 ``UnicodeEncodeError`` 是 ``ValueError``、**不是** ``OSError``，
    ``save()`` 里的 ``except OSError`` 抓不住它，它一路冒到 Qt 槽里变成
    ``qFatal``，进程 ``0xC0000409`` 死掉、**一行 traceback 都没有**。

    这和 ``UnicodeDecodeError`` 是同一族：读侧那次让应用**起不来**，
    写侧这次让应用**用着用着消失**。两边都得自己兜。
    """
    return any("\ud800" <= ch <= "\udfff" for ch in text)


def builtin_id(title: str, command: str) -> str:
    """内置片段的 id **由内容派生**，不是按序号。

    序号派生的坏处很具体：把 ``BUILTIN_SNIPPETS`` 里的条目重排一下，
    ``b-00`` 就换了主人，用户磁盘上所有已持久化的内置 id 集体改嫁，
    ``restore_builtins`` 于是**永远补不回来**（它认为那些 id 都还在）。
    内容派生没有这个问题，重排不影响任何已落盘的 id。
    """
    digest = hashlib.sha1(("%s\x00%s" % (title, command)).encode("utf-8"))
    return "b-" + digest.hexdigest()[:12]


def default_snippets() -> list[Snippet]:
    """一份全新的内置集。id 由内容派生，重复调用、重排条目结果都一致。"""
    return [Snippet(id=builtin_id(t, c), title=t, command=c, builtin=True)
            for t, c, _hint in BUILTIN_SNIPPETS]


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class SnippetStore:
    """片段的增删改查 + 落盘。**app 级单例**，不是每个标签页一份。

    形状对齐 :class:`settings.SessionStore`：构造收一个路径，构造完就有一个
    可用的内存状态（读盘失败也不抛，退回默认集），调用方只管调方法。

    **不要在 ``__init__`` 里 ``self.load(); self.save()``。** 读失败时
    ``load()`` 会退回默认集，紧跟着的 ``save()`` 就把用户自己写的片段全覆盖掉
    了——一次读失败被升级成永久数据丢失。写入只发生在 :meth:`add` /
    :meth:`remove` / :meth:`restore_builtins` 这些**真的改了东西**的方法里。
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else SNIPPETS_FILE
        self.items: list[Snippet] = default_snippets()
        self.loaded_ok = True
        #: 最近一次 save() 成功了吗。必须把它暴露出来：
        #: 看定位为否“Windows 上并发 os.replace 会成片报
        #: WinError 5”（实测确认过）——保存失败可以看不到一个字，
        #: 而面板却无条件地报"已添加"，用户以为存下去了，下次打开就消失。
        self.last_save_ok = True
        self.load()

    # ------------------------------------------------------------------ 读

    def load(self) -> bool:
        """从磁盘读。读不到或读坏了都退回默认集并返回 False。

        返回值有意义：界面上据此提示"片段库读不出来，先用内置的"，
        而不是假装一切正常——用户得知道自己写的东西没加载上来。

        **坏文件会被改名隔离，不是留着等下次 save 覆盖。** 之前只是
        "构造时不覆盖"，用户下一次 add/remove 就把可抢救的原文件盖掉了 ——
        隔离成 ``snippets.json.bad-N`` 之后，原字节还在（用户能自己捞回来），
        而新文件可以从内置集干净地重建。
        """
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            self.items = default_snippets()
            self.loaded_ok = True        # 第一次跑，本来就没有
            return True
        except OSError as exc:
            log.warning("片段库读不出来：%s: %s", type(exc).__name__, exc)
            self.items = default_snippets()
            self.loaded_ok = False
            return False

        # **先解码再解析，两步都要兜。** `UnicodeDecodeError` 是 `ValueError`
        # 的子类，**不是** `OSError`：用 `read_text()` + `except OSError` 的话它
        # 会一路穿透 `__init__` 冒到 `MainWindow.__init__`，而那里没有 try，
        # 整个应用起不来。实测 UTF-16（PowerShell 5.1 的 Out-File 默认格式）
        # 就能触发。`utf-8-sig` 只挡 BOM，挡不住编码本身。
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            log.warning("片段库不是 UTF-8：%s: %s", type(exc).__name__, exc)
            self._quarantine()
            self.items = default_snippets()
            self.loaded_ok = False
            return False

        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            # 手工编辑截断、两个进程同时写、非 JSON 的内容。
            log.warning("片段库不是合法 JSON：%s: %s", type(exc).__name__, exc)
            self._quarantine()
            self.items = default_snippets()
            self.loaded_ok = False
            return False

        if not isinstance(data, list):
            log.warning("片段库顶层不是数组，退回内置集")
            self._quarantine()
            self.items = default_snippets()
            self.loaded_ok = False
            return False

        good, dupes = _dedupe(
            [s for s in (Snippet.from_dict(r) for r in data) if s is not None])
        dropped = len(data) - len(good) - dupes
        if dropped:
            log.warning("片段库里有 %d 条读不出来，已跳过", dropped)
        if dupes:
            log.warning("片段库里有 %d 条 id 重复，已合并", dupes)
        # 全军覆没比空列表更可疑：多半是版本不对，用内置集顶上，
        # 但 loaded_ok 仍然是 False，界面上要说一句。
        self.items = good or default_snippets()
        self.loaded_ok = not (dropped or dupes)
        return self.loaded_ok

    def _quarantine(self) -> Path | None:
        """把读不出来的原文件改名隔离，返回新路径（失败返回 None）。

        **不删。** 用户的原字节可能手工救得回来（换个编码打开、把坏的那条
        删掉）。留在原地等下一次 save 覆盖才是真的丢。
        """
        for n in range(1, 100):
            cand = self.path.with_name("%s.bad-%d" % (self.path.name, n))
            if cand.exists():
                continue
            try:
                os.replace(self.path, cand)
                log.warning("读不出来的片段库已隔离到 %s", cand)
                return cand
            except OSError as exc:
                log.warning("隔离失败（就留在原地）：%s: %s",
                            type(exc).__name__, exc)
                return None
        log.warning("隔离失败：旁边已经有 99 个 .bad-N 了")
        return None

    # ------------------------------------------------------------------ 写

    def save(self) -> bool:
        """落盘。**先写一个本次独有的临时文件再 replace**。

        中途崩了或磁盘满了，留下的是完整的旧文件，而不是一个被截断的新
        文件。返回 False 而不是抛：保存失败不该让整个功能崩掉。

        **临时文件名必须每次唯一。** 固定成 ``snippets.json.tmp`` 的话，
        两个进程各写各的：读者会在 ``replace`` 之前读到**别人写到一半的
        临时文件内容被拼进正式文件**的结果（实测 4 进程 × 40 次 × 400KB，
        读方撞到 61 次非法 JSON）。原子写只对崩溃成立，对并发不成立。

        **``except`` 必须带上 ``ValueError``。** ``UnicodeEncodeError``（孤立
        代理字符）和 ``TypeError``（编不出来的对象）都不是 ``OSError``，
        原来的 ``except OSError`` 抓不住，它们会一路冒到 Qt 槽里变成
        ``qFatal``，**整个应用 0xC0000409 死掉且无 traceback**。这里本来就
        写着"返回 False 而不是抛"—— 那句话对 ``OSError`` 成立，对
        ``ValueError`` 曾经是假的。入口已经用 :func:`_has_surrogate` 拒掉了
        源头那份，这一层是防别的漏网之鱼。
        """
        payload = [s.to_dict() for s in self.items]
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".",
                suffix=".tmp")
            tmp = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
            os.replace(tmp, self.path)
            self.last_save_ok = True
            return True
        except (OSError, ValueError, TypeError) as exc:
            log.warning("片段库保存失败：%s: %s", type(exc).__name__, exc)
            if tmp is not None:
                # 崩在半路时那个临时文件是残的，删掉；别留给下一轮。
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            self.last_save_ok = False
            return False

    # ------------------------------------------------------------------ 改

    def add(self, title: str, command: str) -> Snippet | None:
        """加一条用户片段。标题/命令是空的、或带了不应该出现的字符，拒绝（返回 None）。

        **带换行必须拒**：插入是逐字节原样发出去的，命令里一个 ``\\n`` 就等于
        客户端替用户按了回车。这是"只插入、不执行"那条承诺的兜底。

        **不可见字符也要拒**（标题命令都查）：U+202E 能让命令在界面上显示成
        别的样子，而用户双击执行的是**屏幕上那串字符**。

        **孤立代理字符更要拒**（见 :func:`_has_surrogate`）：它在 JSON 里合法、
        读得进来，却编不出去 —— 存下去的那一步会让整个应用死掉。
        """
        title, command = (title or "").strip(), (command or "").strip()
        if not title or not command or _has_line_break(command) \
                or _has_invisible(command) or _has_invisible(title) \
                or _has_surrogate(command) or _has_surrogate(title):
            return None
        sn = Snippet(title=title, command=command, builtin=False)
        self.items.append(sn)
        self.save()
        return sn
        # 注：返回值语义不变（“加进去了”），但保存成功与否请看
        # self.last_save_ok。不改返回值是因为调用方需要区分“参数非法”和
        # “添加了但没存上”。

    def remove(self, snippet_id: str) -> bool:
        """删一条。内置的也能删——用户就是嫌它占地方，不该拦着。

        真要恢复有 :meth:`restore_builtins`。
        """
        before = len(self.items)
        self.items = [s for s in self.items if s.id != snippet_id]
        if len(self.items) == before:
            return False
        self.save()
        return True

    def update(self, snippet_id: str, title: str, command: str) -> bool:
        """改标题/命令，id 不变。内置的也允许改（改完仍然是内置，只是可以删）。"""
        title, command = (title or "").strip(), (command or "").strip()
        if not title or not command or _has_line_break(command) \
                or _has_invisible(command) or _has_invisible(title) \
                or _has_surrogate(command) or _has_surrogate(title):
            return False
        for sn in self.items:
            if sn.id == snippet_id:
                sn.title, sn.command = title, command
                self.save()
                return True
        return False

    def restore_builtins(self) -> int:
        """把内置集恢复到**出厂原样**，返回动了几条。

        两种情况都算「不在」：

        1. **删掉了** —— 补一条。
        2. **改过了** —— 把它**改回原样**。

        第二种是评审挖出来的：内置片段的 id 由**内容**派生，而
        ``update()`` 按约定保住 id，于是改完之后 id 与内容对不上；原来这里
        只按 id 判重，就认为"已经在"，原文**永远取不回来**。实测：把
        ``df -h`` 改成 ``df -hT`` 再点恢复内置，提示是「内置片段都在」，
        磁盘上仍是改过的那条。绕法得用户自己想到"先删再恢复"。
        """
        by_id = {s.id: s for s in self.items}
        added, reset = [], 0
        for t, c, _h in BUILTIN_SNIPPETS:
            bid = builtin_id(t, c)
            cur = by_id.get(bid)
            if cur is None:
                added.append(Snippet(id=bid, title=t, command=c, builtin=True))
                continue
            if cur.title == t and cur.command == c:
                continue
            # 改过了：恢复成出厂原样。id 本来就是按内容派生的，**不用动**
            # —— by_id 是按它取出来的，动了反而和索引对不上。
            cur.title, cur.command = t, c
            reset += 1
        if not added and not reset:
            return 0
        self.items.extend(added)
        self.save()
        return len(added) + reset

    # ------------------------------------------------------------------ 查

    def search(self, needle: str) -> list[Snippet]:
        """按标题或命令过滤，空查询返回全部。"""
        needle = (needle or "").strip().lower()
        if not needle:
            return list(self.items)
        return [s for s in self.items
                if needle in s.title.lower() or needle in s.command.lower()]

    def get(self, snippet_id: str) -> Snippet | None:
        return next((s for s in self.items if s.id == snippet_id), None)


# ---------------------------------------------------------------------------
# 自检：内置集里不该有危险命令
# ---------------------------------------------------------------------------

def audit_builtins() -> list[str]:
    """返回内置集里的违规条目（正常情况下是空列表）。

    这不是"防用户输入危险命令"——用户自己写 ``rm -rf`` 是他自己的事，面板
    不会去拦。这条只守**我们出厂时塞进去的东西**：一个从没见过的默认列表
    里混着一条 ``shutdown``，用户双击两次就够了。

    **第一版这个函数几乎没用。** 它只 `lstrip("|&;")` 削**开头**、只把 `>`
    当独立 token 判，于是 16 种危险写法里放过去 12 种，包括
    ``ls;rm -rf …``、``$(rm -rf …)``、``curl … | sh``、``find . -delete``，
    以及 ``echo x >/etc/hosts``（**没空格**的重定向，直接违反本文件顶上
    那条第 3 条硬规矩）。

    **第二版仍然漏了 12/19。** 上一轮只把「切段查词」做对了一件事：每一段
    的 ``words[0]``。可是 ``sudo rm`` / ``env rm`` / ``time rm`` /
    ``busybox rm`` / ``FOO=1 rm`` 的第一个词全是无害的包装词 —— 提权和改
    环境的那一步恰好把刀挡在了判据**后面**。再加上 ``awk '{system("rm -rf /")}'``、
    ``python -c "os.system('rm -rf /')"``、``cleanup() { rm -rf …; }`` 这三种
    根本不经过"第一个词"这条路。现在的判据是三件事合起来：
    **剥前缀链**（:func:`_resolve_head`）+ **认点号变体**（``mkfs.ext4``）+
    **把载荷再抽一层复查**（函数体 / ``-c`` / ``system(...)``）。

    **仍然不是 shell 解释器。** 真正完整的做法是把内置集送进 ``bash -n`` 或
    AST 解析器，那超出了这个文件该管的范围。这里的目标是"我们出厂塞进去的
    东西里不该有一键能毁机器的命令"，不是"证明这条命令安全"。

    **已知的一处有意误报**：``systemctl status nginx`` 会被报成违规，因为
    ``systemctl`` 整词在 :data:`FORBIDDEN_TOKENS` 里。这不是判据写错了，
    是不想为"逐个子命令开白名单"松掉整词黑名单 —— 反正这套检查**只审我们
    出厂的内置集**（用户自己加的片段一条都不审），而内置集里没有 systemctl。
    真要放宽，代价是 ``systemctl stop nginx`` 这类重新混进来。
    """
    bad: list[str] = []
    for title, command, _hint in BUILTIN_SNIPPETS:
        _audit_command(title, command, bad, depth=0)
    # 同一处危险会从「头词」和「嵌套载荷」两条路各报一次，去重且保序，
    # 免得测试和日志里看到两行一模一样的。
    seen, out = set(), []
    for msg in bad:
        if msg not in seen:
            seen.add(msg)
            out.append(msg)
    return out


def _audit_command(title: str, command: str, bad: list[str],
                   depth: int = 0) -> None:
    """审一条命令，把违规追加进 ``bad``。``_audit_one`` 是它的递归入口。"""
    import shlex

    try:
        shlex.split(command)
    except ValueError:
        bad.append("%s：引号不配对，插进去会变成乱码" % title)
        return

    # 1) 重定向：任何位置、任何写法都要拦。`>` 不必是独立 token ——
    #    `>/etc/hosts` 这种没空格的写法漏过一次。
    #
    #    **但 `2>/dev/null` 不是重定向危害**：那是把 stderr 丢掉，
    #    `du -sh /* 2>/dev/null | sort -h` 这种一行里就有一个。先把
    #    "丢输出"的那几种抹掉，剩下的 > 才值得报。不抹的话这一条会把
    #    我们自己的一条内置命令判成违规 —— 而一个天天误报的检查，
    #    总有一天被人直接关掉。
    probe = re.sub(r"\d*\s*(?:>>?|&>>?)\s*/dev/null", " ", command)
    probe = re.sub(r"\d*\s*>&1", " ", probe)
    if ">" in probe:
        bad.append("%s：有重定向（%r），可能覆盖文件"
                   % (title, _first_around(probe, ">")))
    # 2) 命令替换 / 求值：里面的命令 shlex 根本切不出来
    for marker in ("$(", "`", "${", "$("):
        if marker in command:
            bad.append("%s：有命令替换 %r" % (title, marker))

    # 3) 管道与串联：每一段都得查，不能只看开头
    for seg in re.split(r"[|;&\n]+", command):
        seg = seg.strip()
        if not seg:
            continue
        try:
            words = shlex.split(seg)
        except ValueError:
            continue
        if not words:
            continue
        raw = _RAW_HEAD.match(seg)
        head, chain, idx = _resolve_head(words, raw.group(1) if raw else "")
        # 报的时候把剥下来的链一起说清楚：光看到 `rm` 用户不知道它是被
        # 什么包装起来的，而 `sudo → rm` 才是能让人一眼看懂的东西。
        shown = " → ".join(chain + ([head] if head else [])) or seg[:24]

        if head in FORBIDDEN_TOKENS:
            bad.append("%s：含有危险命令 %r" % (title, shown))
        elif head and head.split(".")[0] in FORBIDDEN_TOKENS:
            # 点号变体：`mkfs.ext4` / `mount.cifs` 的危险在**词干**上，
            # 第一个词是 `mkfs.ext4` 时按整词比是比不出来的。
            bad.append("%s：含有危险命令 %r" % (title, shown))

        # 危险常常不在**第一个**词上：`git clean -fdx`、`docker system prune`
        # 的第一个词都是无害的容器命令，第二个词才是刀。漏过一次。
        # **子命令下标跟着剥前缀走** —— `sudo git clean` 的 clean 在 words[2]
        # 而不是 words[1]，写死下标就等于把这一类又漏回去了。
        # 还要**跳过主命令自己的全局选项**：`git -C /tmp/x clean -fdx` 的
        # clean 在第三，`apt-get -y purge nginx` 的 purge 在第二。
        if head in SUBCOMMAND_HOSTS:
            sub_idx = _skip_global_opts(words, idx + 1)
            if 0 <= sub_idx < len(words):
                sub = words[sub_idx].strip().lower()
                if sub in DESTRUCTIVE_SUBCOMMANDS:
                    bad.append("%s：%r 的子命令 %r 是破坏性的"
                               % (title, head, sub))
        # 同样带偏移：`sudo xargs -0 rm -f` 的 rm 也不在 words[1]。
        tail = words[idx + 1:] if idx < len(words) else []
        if head in ("find", "xargs") and any(
                w in ("-delete", "-exec", "-execdir", "-ok") for w in tail):
            bad.append("%s：%r 带 %s" % (
                title, head, [w for w in tail if w.startswith("-")]))
        if head in ("eval", "source", ".", "bash", "sh", "zsh"):
            bad.append("%s：含有求值/子 shell %r" % (title, head))
            # 已经按"求值"报了，**别再往里抽一层**：同一件事报两遍只会
            # 让人以为漏了别的。
            continue

        # 4) 剥出来的包装词和头词合起来决定要不要抽嵌套载荷。
        if depth < _MAX_DEPTH:
            if ({head} | set(chain)) & _INTERPRETERS:
                for m in _FLAG_PAYLOAD.finditer(command):
                    _audit_command(title, m.group("body"), bad, depth + 1)

    # 5) 嵌套载荷：函数体与解释器内部的 shell 调用。这两条**不看头词**，
    #    因为 `awk` / `python` / `bash` 本身就在 _INTERPRETERS 里报过了，
    #    真正要抓的是它们**里面**那句。
    if depth < _MAX_DEPTH:
        for pattern in (_NESTED_CALL, _FUNC_BODY):
            for m in pattern.finditer(command):
                _audit_command(title, m.group("body"), bad, depth + 1)
        # 列表参数要先拼回一行命令再审 —— `["rm", "-rf", "/var"]` 拆开看
        # 是三个没意义的词，拼起来才是那条命令。
        for m in _LIST_PAYLOAD.finditer(command):
            parts = [p.strip().strip("'\"") for p in m.group("body").split(",")]
            parts = [p for p in parts if p]
            if parts:
                _audit_command(title, " ".join(parts), bad, depth + 1)


def _command_name(token: str) -> str:
    """从 token 里取出命令名。**三种剥法缺一不可**。

    ``/bin/rm``  → ``rm``        （POSIX 路径）
    ``C:\\…\\rm.exe`` → ``rm``   （Windows 盘符路径 + 可执行后缀）
    ``/``        → ``""``        （裸路径，是重定向目标，**不是命令**）

    评审实测：只切 ``/`` 时 Windows 那两种全漏 ——
    ``C:\\<某目录>\\System32\\busybox.exe rm -rf …`` 在 Windows 上整条放行，
    而 POSIX 上同一条能抓到。**判定在两个平台上不对称，等于跨平台守卫根本没
    覆盖到它。** 而 ``.exe`` 后缀不剥的话 ``busybox.exe`` 压根不等于
    ``busybox``，包装词那一层同样认不出来。

    （例子刻意不写真实的系统目录名，见上面 :func:`_resolve_head` 里那条注释：
    ``tests/_portable.py`` 禁止片段库源码里出现写死的 Windows 路径，
    而它扫整份文件、不区分代码和注释。）
    """
    name = re.split(r"[/\\]", token.strip())[-1]
    if not name:
        return ""                    # 裸的 "/" 或 "//"：是重定向目标
    return _EXE_SUFFIX.sub("", name)


def _resolve_head(words: list[str], raw_first: str = "") -> tuple[str, list[str], int]:
    """剥掉赋值前缀和包装词，返回 ``(真命令, 剥下来的链, 它在 words 里的下标)``。

    ``sudo -u root rm -rf /``  →  ``("rm", ["sudo"], 2)``
    ``FOO=1 rm -rf /``        →  ``("rm", [], 1)``
    ``busybox rm -rf /``      →  ``("rm", ["busybox"], 1)``
    ``sudo -l``               →  ``("", ["sudo"], -1)``（提权看权限，无命令）

    **返回下标而不只是名字**，是因为子命令判据要跟着偏移走：剥掉 ``sudo``
    之后 ``git clean`` 的 ``clean`` 落在 ``words[2]``，写死 ``words[1]``
    等于把 ``sudo git clean -fdx`` 漏回去。
    """
    i, chain = 0, []
    while i < len(words):
        tok = words[i]
        if _ASSIGNMENT.match(tok):
            i += 1
            continue
        # 头词**从原始段取**：``shlex`` 在 POSIX 模式下把反斜杠当转义符，
        # ``C:\<某目录>\busybox.exe`` 会被它吃成 ``C:<某目录>busybox.exe``，
        # 之后再怎么切分隔符都切不开。只有第一个 token 需要这么小心 ——
        # 包装词的名字都是纯 ASCII 裸词，``rm`` 之类的也不含反斜杠。
        #
        # （例子刻意**不写成真实的系统目录名**：``tests/_portable.py`` 有一条
        #  守卫禁止片段库源码里出现写死的 Windows 用户/系统目录前缀，它扫的
        #  是整份文件、不区分代码和注释。与其把守卫改成"忽略注释"，不如别在
        #  注释里放一个看起来像写死路径的东西 —— **注意这条注释自己也别把
        #  那个前缀抄出来**，我这么写过一遍，当场又把自己守卫拦红了。）
        base = (_command_name(raw_first) if (i == 0 and raw_first)
                else _command_name(tok))
        if not base:
            i += 1
            continue
        if base not in _PREFIX_WRAPPERS:
            return base, chain, i
        chain.append(base)
        i += 1
        # 包装词自己还带参数：选项、选项的值、少数位置参数都得吃掉，
        # 免得把 `rm` 当成 `sudo -u` 的那个 `root` 跳过去。
        value_opts = _WRAPPER_VALUE_OPTS.get(base, frozenset())
        left = _WRAPPER_POSITIONALS.get(base, 0)
        eat_duration = base in _WRAPPERS_WITH_DURATION
        while i < len(words):
            nxt = words[i]
            if nxt == "--":
                # **选项解析到此为止，下一个就是命令。** `sudo -- rm -rf`
                # 恰恰是最该抓的写法，而"把每个选项都当带值"的那一版
                # 会把 rm 当成 -- 的值吃掉，然后连 -rf 和路径一起错下去。
                i += 1
                break
            if nxt.startswith("-") and nxt != "-":
                # 只有"这个选项确实自带值"才吃下一个词。`-n5` 这种附着式
                # 和 `--user=root` 这种带等号的都不在 value_opts 里，
                # 天然不会误吃 —— 不需要另外判长度。
                if nxt in value_opts and "=" not in nxt:
                    left += 1
                i += 1
                continue
            if left > 0:
                left -= 1
                i += 1
                continue
            if eat_duration and _DURATION.match(nxt):
                i += 1
                continue
            break
    return "", chain, -1


def _skip_global_opts(words: list[str], start: int) -> int:
    """从 ``start`` 起跳过主命令的全局选项，返回**子命令的下标**；没有就 -1。

    **不能只看紧邻的那个 token。** ``git -C /tmp/x clean -fdx`` 的 ``clean``
    在第三个，``apt-get -y purge nginx`` 的 ``purge`` 在第二个，
    ``docker -H tcp://h system prune`` 要跳过 ``-H`` 和它的值。评审实测
    这一整类全漏。带值选项同样只看 :data:`_GLOBAL_VALUE_OPTS`：``-y`` 不在
    表里（它不带值），若也去吃下一个词，``purge`` 就被吃掉了。
    """
    i = start
    while i < len(words):
        tok = words[i]
        if not tok.startswith("-") or tok == "-":
            return i                       # 第一个非选项词就是子命令
        if tok in _GLOBAL_VALUE_OPTS and "=" not in tok:
            i += 2
            continue
        i += 1
    return -1


def _first_around(text: str, needle: str, span: int = 12) -> str:
    """截一段带上下文，好让报错说清"哪个 `>`"。"""
    i = text.find(needle)
    if i < 0:
        return needle
    return text[max(0, i - span // 2):i + span // 2].replace("\n", "\\n")


def _dedupe(items: list[Snippet]) -> tuple[list[Snippet], int]:
    """按 id 去重，返回 ``(去重后的列表, 被合并掉的条数)``。

    同一条在文件里出现两次时（手工复制粘贴、两次并发写各带一条），
    ``remove(id)`` 一次会删掉**两条** —— 用户点一下删除，列表少了一格却
    不止一格。保留第一条（更早的那条通常是用户自己写的）。
    """
    seen, out, dropped = set(), [], 0
    for sn in items:
        if sn.id in seen:
            dropped += 1
            continue
        seen.add(sn.id)
        out.append(sn)
    return out, dropped
