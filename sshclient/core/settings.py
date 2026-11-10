"""Persistent app settings and saved SSH sessions.

Stored as JSON in the platform user config dir, e.g.

    Windows: %APPDATA%\\MavisSSH\\sessions.json
    Linux:   ~/.config/MavisSSH/sessions.json
    macOS:   ~/Library/Application Support/MavisSSH/sessions.json
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import crypto
from .logs import get_logger

log = get_logger("mavisssh.settings")


#: 没有分组的会话在侧栏里归到的那个分组的名字。
#:
#: **磁盘上仍然是空串，不是这个字符串。** 空串 = 未分组是数据层的契约
#: （见 `SavedSession.group`）；「默认」只是它在侧栏上显示的那一行。
#: 写进文件的话，老版本读回来会多出一个叫「默认」的真分组，而它和原本
#: 的未分组会话混在一起 —— 又一次只有显示层该做的事漏到了数据层。
#:
#: 顺带定了一个副作用：**用户自己建的分组正好叫「默认」时，会和这个默认
#: 分组合成同一个。** 这是故意的 —— 界面上只该有一个「默认」，两个同名
#: 的段并排出现只会让人以为程序认错了。
DEFAULT_GROUP = "默认"


def _safe_int(value, default: int = 0) -> int:
    """把任何东西收成一个 int，收不住就用 *default*。

    手改坏过的 settings.json / sessions.json 里出现过 `"abc"`、`["x"]`、
    `1.5`、`null`。这些值一路穿过 dataclass（**它不校验类型**）到 Qt 才炸，
    而那时候已经在启动路径上了 —— 一个 `ui_font_size: "abc"` 就是启动即崩。
    配置文件是用户能直接改的文件，所以每个读进来的标量都得先过这一关。
    """
    try:
        if isinstance(value, bool):      # bool 是 int 的子类，单独挑出来
            return int(value)
        if isinstance(value, str):
            value = value.strip()
        return int(value)
    except (TypeError, ValueError):
        return default


def _config_dir() -> Path:
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
SESSIONS_FILE = CONFIG_DIR / "sessions.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"


def _migrate_legacy_tunnels(data: dict) -> list:
    """Convert the pre-``tunnels`` shape into the canonical rule dicts.

    Earlier builds stored ``tunnel_local`` as rows of
    ``[local_port, remote_host, remote_port, description, listen_host]`` and a
    single ``tunnel_socks`` port. That shape could not express the on/off
    state, a description on the SOCKS rule, or more than one SOCKS rule, so
    those sessions are folded into the new form once, on load.
    """
    from .tunnel import TunnelSpec  # local import: avoids a cycle

    rules: list = []
    for row in data.get("tunnel_local") or []:
        spec = TunnelSpec.from_row(row)
        if spec is not None:
            rules.append(spec.to_dict(enabled=True))
    socks_port = _safe_int(data.get("tunnel_socks"), 0)
    if socks_port:
        rules.append(TunnelSpec(
            kind="socks", local_port=socks_port, description="SOCKS5",
        ).to_dict(enabled=True))
    return rules


@dataclass
class SavedSession:
    """A saved SSH connection profile."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    # 分组名。**空串 = 未分组**，不是 None，也不是 ``DEFAULT_GROUP``
    # 那个字符串 —— 用 None 当键会让「用户手工写了 group: null」和
    # 「没写 group」走出两条不同的路，而把显示名存进数据层则会在老版本
    # 里凭空多出一个叫「默认」的真分组。侧栏怎么显示由
    # ``SavedSession.group_name()`` 决定。
    group: str = ""
    host: str = ""
    port: int = 22
    username: str = ""
    auth_method: str = "password"  # "password" | "key"
    # When auth_method == "password"
    password: str = ""
    # When auth_method == "key"
    key_filename: str = ""
    key_passphrase: str = ""
    # Port forwarding rules, as written by TunnelSpec.to_dict(). Canonical
    # home for both -L and -D rules, including their on/off state.
    tunnels: list = field(default_factory=list)
    # UI
    last_dir: str = ""
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def base_name(self) -> str:
        """不带分组的显示名。

        **名称的回退只在这里发生一次。** 原来 ``session.name or
        f"{username}@{host}"`` 在侧栏、删除确认框等五处各写一遍；漏改一处
        的症状不是报错，而是同一份会话在两个界面显示成两个名字。
        """
        return self.name or f"{self.username}@{self.host}"

    def group_name(self) -> str:
        """侧栏上这条属于哪一段，**含默认分组那一档**。

        和 :meth:`display_name` 刻意分开：这里回答「它归在哪」，那里回答
        「标签上写什么」。未分组的会话在这里得到 ``默认``，在
        :meth:`display_name` 里仍然不加任何前缀 —— 否则每一个没分组的标签
        都会变成「默认-Web-01」，那是凭空多出来的字，不是用户填的名字。
        """
        return str(self.group or "").strip() or DEFAULT_GROUP

    def display_name(self) -> str:
        """标题栏 / 确认框该显示的**全名**：``分组名-会话名``。

        用户要的是 tab 上写「分组名-服务器命名」，而不是 ``用户名@ip:端口``。
        无分组时**不加那个连字符** —— 否则每个未分组的标签都变成 ``-Web-01``。
        未分组在侧栏里归到「默认」段，但**标签标题不受影响**（理由见
        :meth:`group_name`）。

        规则只有这一处。不要在别处再拼 ``f"{group}-{name}"``：第二份拼法
        一旦漏掉无分组那支，界面上就是一个凭空冒出来的前导连字符，而代码
        读起来完全正常。

        **必须 strip 后再判真假。** `group` 是空串时好办，但手改过的
        ``sessions.json`` 里可能写着 ``"   "`` —— 那是真值，于是
        ``f"{group}-{base}"`` 交出 ``"   -Web-01"``：标签上凭空多出一段
        空白和一个连字符，正是上面那段注释在防的那个症状。判据
        ``tests/_group.py`` 里「group_name 归一但 display_name 不变」
        实测抓到过这一条。
        """
        base = self.base_name()
        group = str(self.group or "").strip()
        return f"{group}-{base}" if group else base

    @classmethod
    def from_dict(cls, data: dict) -> SavedSession:
        # tolerate missing fields from older files
        allowed = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in allowed}
        # **端口必须夹紧。** 原来它原样进内存，于是手改 `sessions.json`
        # 写 `port: 99999` 或 `port: -1` 会一路带到 `AuthConfig.port`，
        # 交给 paramiko 之后才炸 —— 症状是「连接失败」而不是「配置坏了」，
        # 而真正的错在文件里，用户从界面上看不出来。
        # 连接对话框那一侧早就 `setRange(1, 65535)` 了，这里是数据层的同一道闸：
        # 界面不是唯一能写进这个字段的地方（老文件、手改、迁移都算）。
        if "port" in clean:
            try:
                clean["port"] = max(1, min(65535, int(clean["port"])))
            except (TypeError, ValueError):
                clean["port"] = 22
        # 同理：`group` 是空串=未分组，任何非字符串值（老文件里的 null、
        # 手改时写进去的数字）都归一到空串。`display_name()` 里直接 f-string
        # 一个 None，标题栏上就会出现字面量 "None-Web-01" —— 界面照常显示，
        # 没有任何报错。
        for key in ("group", "name", "host", "username"):
            if key in clean and not isinstance(clean[key], str):
                clean[key] = "" if clean[key] is None else str(clean[key])
        session = cls(**clean)
        if not session.tunnels:
            session.tunnels = _migrate_legacy_tunnels(data)
        return session


def _raw_is_encrypted(text: str) -> bool:
    """磁盘上这份文本里的敏感字段是不是密文。

    **必须在 `crypto.load()` 之前判。** `load()` 返回的 rows 已经解密过了，
    对明文再判一次 `is_encrypted()` 恒为 False —— 于是 `encrypted` 这个标志
    恒 False，还会拿它去判那句「会话文件仍是明文」，对着一份全密文的文件
    照报不误（缺陷 B25）。
    文本本身读不出来时返回 False：那一条由调用方自己的异常分支处理，
    这里**不重复判断、也不吞异常**，免得两套判断给出两个矛盾的结论。
    代价只是同一份文本 `json.loads` 了两遍（会话文件很小），换来的是
    `crypto.load()` 仍是**解析+解密**的唯一权威，它的签名和语义一个字没动 ——
    判标志这件事不该塞进加解密模块里。
    """
    try:
        rows = json.loads(text)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(rows, list):
        return False
    return any(
        crypto.is_encrypted(r.get(f, ""))
        for r in rows if isinstance(r, dict) for f in crypto.SENSITIVE
    )


class SessionStore:
    """JSON-backed CRUD for :class:`SavedSession`.

    落盘时敏感字段（主机、端口、账号、口令、私钥路径与口令）会被逐字段
    加密，见 :mod:`sshclient.core.crypto`。`SavedSession` 本身保持纯数据，
    加解密只发生在这两个 I/O 关口上。
    """

    def __init__(self, path: Path = SESSIONS_FILE, config_dir: Path = CONFIG_DIR):
        self.path = path
        # 密钥是**全局一份**，不跟着这个 path 走：测试会把 store 建在临时目录
        # 里，密钥若跟着走，每次都会现生成一把新密钥。
        self._config_dir = config_dir
        self._items: list[SavedSession] = []
        self.encrypted = False        # 落盘时实际有没有成功加密
        self.load_error: str = ""     # 非空表示这份文件没能读出来
        self._writable = True
        self.reload()

    # ------------------------------------------------------------------ I/O
    def reload(self) -> None:
        self._items = []
        self.encrypted = False
        self.load_error = ""
        self._writable = True
        if not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8-sig")
        except Exception as exc:
            self._quarantine(f"读不出来（{exc}）")
            return
        # 加密标志从**原始文本**上取，在解密之前（见 _raw_is_encrypted）。
        self.encrypted = _raw_is_encrypted(text)
        try:
            rows = crypto.load(text, self._config_dir)
        except crypto.CryptoUnavailable as exc:
            # 密钥和密文对不上。**原文件一个字都不动，也不允许再写它**——
            # 这不是文件损坏，是密钥没了。把文件改名或者干脆当作空列表，
            # 下一次保存都会把用户所有会话连同密码一起抹掉。
            # 留着原样，用户把密钥文件找回来、重启一次就全好了。
            self.load_error = f"密钥与数据不匹配：{exc}"
            self._writable = False
            log.error("%s（文件 %s 保持原样，未做任何改动）",
                      self.load_error, self.path)
            return
        except Exception as exc:
            self._quarantine(f"内容损坏（{exc}）")
            return
        try:
            self._items = [SavedSession.from_dict(d) for d in rows]
        except Exception as exc:
            # **这一行原本在 try 外面**，所以一个坏字段（`tunnel_socks`
            # 写成了 "abc"）会在 `reload()` 里直接抛出去，逃过 `_quarantine`、
            # 逃过 `__init__`、一路抛到 `main.py` —— **启动即崩**，而且
            # 崩溃发生在隔离改名**之前**，用户的文件原样留着却再也打不开。
            self._quarantine(f"内容损坏（{exc}）")
            return
        if not self.encrypted and any(
                d.get(f) for d in rows for f in crypto.SENSITIVE):
            log.info("会话文件仍是明文，下次保存时自动转成密文")

    def _quarantine(self, why: str) -> None:
        """把真的读不出来的文件改名保留，不直接删——删了就没法手工抢救。"""
        self.load_error = why
        log.error("会话文件 %s（%s），已改名保留", self.path, why)
        try:
            self.path.rename(self.path.with_suffix(".json.bak"))
        except Exception:
            pass
        self._items = []

    def save(self) -> None:
        if not self._writable:
            # 宁可这次修改不落盘，也不能拿一份解不开的密文覆盖掉用户唯一
            # 的那一份。参见 reload() 里 CryptoUnavailable 那段。
            log.error("拒绝写入 %s：%s", self.path, self.load_error)
            return
        try:
            payload = crypto.dump([s.to_dict() for s in self._items],
                                  self._config_dir)
            self.encrypted = True
        except crypto.CryptoUnavailable as exc:
            # **退回明文这一条已经删掉。** 拿不到密钥就写明文，等于把用户
            # 所有口令一次性摊在磁盘上，而且当时 `encrypted=False` /
            # `load_error` 都还是空的，`upsert()` 照常返回 —— 界面照样报
            # 「已添加」，全程没有任何一处能读到这次降级（缺陷 A2）。
            # 这里改成**拒写**：内存里的改动留着（当场的工作不丢），
            # 下次保存还会再试一次，密钥恢复正常就能落盘。
            # 不置 `_writable`：那扇门是给「文件解不开」的，这一把钥匙
            # 只是**暂时**拿不到，锁死就等于让用户必须重启程序。
            self.encrypted = False
            self.load_error = f"会话没有落盘：加密不可用，本次改动未保存（{exc}）"
            log.error("%s（文件 %s 保持原样）", self.load_error, self.path)
            return
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ CRUD
    @property
    def all(self) -> list[SavedSession]:
        return list(self._items)

    def get(self, sid: str) -> SavedSession | None:
        for s in self._items:
            if s.id == sid:
                return s
        return None

    def upsert(self, session: SavedSession) -> None:
        for i, s in enumerate(self._items):
            if s.id == session.id:
                self._items[i] = session
                self.save()
                return
        self._items.append(session)
        self.save()

    def delete(self, sid: str) -> None:
        self._items = [s for s in self._items if s.id != sid]
        self.save()


# ---------------------------------------------------------------------------
# App-level settings (theme, font size, last session, ...)


def accent_for(settings: AppSettings, theme: str) -> str:
    """当前主题该用哪个强调色值。

    单独一个函数而不是到处写 `settings.accent_dark if ... else ...`：那样
    的三元表达式散在五六个地方，改漏一处就是"切了主题颜色不对"。
    """
    return settings.accent_light if theme == "light" else settings.accent_dark


# 样式表里那些像素值（`font-size: 13px` 之类）是按这个字号量出来的。
# 用户把界面字号调离它时，`qss._scale_metrics` 按 `ui_font_size / UI_SIZE_REF`
# 的比值整体缩放。**改这个数等于改整套界面的比例**，所以它不是「默认值」，
# 是「比例尺的原点」——两个数必须一起动。
UI_SIZE_REF = 11


@dataclass
class AppSettings:
    theme: str = "dark"                 # "dark" | "light"
    ui_font_size: int = UI_SIZE_REF     # application-wide, in points
    # "" means "first interface face the system actually has". Same contract
    # as term_font_family below, and for the same reason: pinning a default
    # here would freeze the face the user never chose.
    ui_font_family: str = ""
    # "" means "first monospaced family the system actually has". The old
    # default was a hardcoded "Consolas" that nothing ever read, so persisting
    # it would pin the terminal to a font the user never chose.
    term_font_family: str = ""
    term_font_size: int = 11
    scrollback: int = 5000              # lines kept above the live view
    # 用户自选的强调色，**深浅两套各记一个**。空串 = 用内置默认色，理由和
    # ui_font_family 一样：这里写死一个默认值，就等于替用户挑了他没挑的颜色。
    accent_dark: str = ""
    accent_light: str = ""
    # 这个实例该写哪个文件。**不进 dataclass_fields** —— 它是运行期的
    # 落盘位置，不是用户偏好，不该进 settings.json 也不该被 asdict 带出去。
    #
    # 为什么需要它：``save()`` 以前无条件写模块级的 SETTINGS_FILE，也就是
    # 用户真实的 %APPDATA%\\MavisSSH\\settings.json。于是**任何测试只要建一个
    # MainWindow 并触发一次 save**（切主题、折叠侧栏、保存设置、断开连接……
    # MainWindow 里有 5 处），就把用户的真实配置覆盖成默认值。
    # 自己踩过两次：_titlebar/_uibtns 直接 load 真实配置；_accent.py 用了
    # 干净的 AppSettings() 以为没事，结果 _toggle_theme() 照样写回真实文件。
    # 缺的不是某几个测试的自觉，是这个口子。
    #
    # **不能写成带注解的字段** —— 那样它会进 __dataclass_fields__，
    # asdict() 就把它一起写进 settings.json 了。普通类属性不进。
    _path = None
    # 同上：普通类属性，不进 `__dataclass_fields__`，`asdict()` 不会写进
    # settings.json。非空表示「这份配置读不出来」，此时 `save()` 拒绝覆写。
    load_error = ""
    monitor_interval_ms: int = 2000
    last_session_id: str = ""
    sidebar_collapsed: bool = False       # 56px icon rail instead of 268px
    # 侧栏里被折叠起来的分组名。**存名字，不存行号**，行号会随会话增删漂移。
    #
    # 存下来是因为不存等于半成品：分组折叠是用户整理列表时最顺手的一个
    # 动作，而每次重启都要重新点一遍，等于这个功能没做完。
    collapsed_groups: list = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> AppSettings:
        p = path or SETTINGS_FILE
        obj = cls.load_from(p)
        obj._path = p
        return obj

    @classmethod
    def load_from(cls, path: Path) -> AppSettings:
        """读一份配置。**读不出来和没有，是两件事。**

        「文件不存在」回默认值是对的 —— 那是首次启动。
        「文件在但读不出来」回默认值是**灾难**：它和「没有」长得一模一样，
        而 `closeEvent` 那边无条件 `save()`，于是用户手改坏一次配置、
        下一次关窗口就被默认值**永久覆盖**，且没有任何提示。

        所以第二种情况把实例标成不可写（`load_error` 非空），让 `save()`
        拒绝覆写、让 `main.py` 能弹一句为什么。参照 `SessionStore` 的
        `_writable` —— 同一类问题的同一类解法。
        """
        inst = cls()
        if not path.exists():
            return inst
        try:
            # utf-8-sig, not utf-8. A byte-order mark makes json.loads raise,
            # and the bare `except` below turns that into "all defaults" -- and
            # main.py saves right after loading, so a BOM is enough to overwrite
            # the user's real preferences with defaults on the next start. It
            # is not hypothetical: PowerShell's `Set-Content -Encoding UTF8`
            # writes one, and that is how it got here.
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            inst.load_error = f"配置读不出来（{exc}）"
            log.error("%s（文件 %s 保持原样，不会被覆盖）", inst.load_error, path)
            return inst
        if not isinstance(data, dict):
            inst.load_error = "配置内容不是一个对象"
            log.error("%s（文件 %s 保持原样）", inst.load_error, path)
            return inst
        data = cls._migrate(data)
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        bad, clamped = cls._coerce(known)
        if bad:
            inst.load_error = "配置里有无法识别的值，已按默认值处理：" + "、".join(bad)
            log.warning("%s", inst.load_error)
        if clamped:
            # 只记日志，**不置 load_error**：这些值已经收成合法的，配置读得懂，
            # 存盘闸门就不该拦（两拨的区别见 _coerce）。落盘后越界值随之消失，
            # 下次启动也不会再夹一次。
            log.warning("配置里有越界的值，已夹到合法区间：%s", "、".join(clamped))
        inst.__dict__.update(known)
        return inst

    # 每个字段的合法取值。**dataclass 本身不校验类型**，所以这张表就是
    # 「用户把配置改坏了」和「程序崩了」之间唯一的一道闸。
    _INT_RANGE = {
        "ui_font_size": (6, 48),      # pt；<6 是不可读，>48 撑爆布局
        "term_font_size": (6, 48),
        "scrollback": (0, 2_000_000),  # 行
        "monitor_interval_ms": (100, 600_000),   # 0 会让 QTimer 每个事件循环
                                             # 都触发，空转烧 CPU
    }
    _STR_FIELDS = ("theme", "ui_font_family", "term_font_family",
                   "accent_dark", "accent_light", "last_session_id")
    #: 该是「一串字符串」的字段。**必须收进列表** —— 侧栏折叠态直接遍历它，
    # 而手改过的配置里出现过 `"collapsed_groups": "生产"`（字符串而不是列表）：
    # 遍历字符串拿到的是一个个单字，于是「生产」这个分组被判成「产」「生」
    # 两个分组折叠，用户点哪个头都展不开。
    _LIST_FIELDS = ("collapsed_groups",)

    @classmethod
    def _coerce(cls, data: dict) -> tuple[list, list]:
        """把 *data* 就地钳制成合法值，返回 ``(读不出来的, 已夹好的)``。

        **两拨必须分开**，因为后果正相反：

        - 「读不出来的」：类型收不住（``"abc"`` / ``null`` / ``["x"]``）或
          枚举值不认识。**我们无从知道用户填的是什么**，替他挑一个默认值
          就等于改了他的配置 —— 置 ``load_error``，``save()`` 拒写。
        - 「已夹好的」：类型收得住、只是越界（``ui_font_size: 999``）。
          这份文件我们**读懂了**：999 就是 999，只是 48 以外没法用。

        以前两拨一起塞进一个 ``bad``，于是夹一下就置 ``load_error``，
        ``save()`` 从此一路拒写（A1）：越界值留在文件里，下次加载又夹一次，
        **重启也不能自愈**，用户改任何设置都存不下去。
        """
        fatal: list = []      # 读不出来 -> 拒写
        clamped: list = []    # 已处理   -> 只记日志
        for key, (lo, hi) in cls._INT_RANGE.items():
            if key not in data:
                continue
            raw = data[key]
            val = _safe_int(raw, -1)
            if val < lo or val > hi:
                if raw != val:          # 收不住
                    val = getattr(cls(), key)
                    fatal.append("%s=%r" % (key, raw))
                else:                   # 收住了但越界
                    val = max(lo, min(hi, val))
                    clamped.append("%s=%r→%d" % (key, raw, val))
            data[key] = val
        for key in cls._STR_FIELDS:
            if key in data and not isinstance(data[key], str):
                fatal.append("%s=%r" % (key, data[key]))
                data[key] = ""
        for key in cls._LIST_FIELDS:
            if key not in data:
                continue
            raw = data[key]
            # **必须逐项查类型，不能只查 isinstancelist。** `["生产", 3]` 同样
            # 是列表，但它会在排序和 set 比较那一步炸 —— 而那两个调用点都在
            # 建 MainWindow 的路径上，症状是「启动即崩，堆栈指向配置文件」。
            if not isinstance(raw, list) or any(not isinstance(x, str)
                                                 for x in raw):
                fatal.append("%s=%r" % (key, raw))
                data[key] = []
        theme = data.get("theme")
        if theme not in ("dark", "light"):
            if theme is not None:
                fatal.append("theme=%r" % (theme,))
            data["theme"] = "dark"
        return fatal, clamped

    @staticmethod
    def _migrate(data: dict) -> dict:
        """Carry the old field names across.

        ``font_size`` drove the *UI* font and ``font_family`` drove nothing at
        all, which is exactly why the terminal ignored it. Rename the first;
        keep the second only when it is not the stale default, so upgrading
        does not pin the terminal to Consolas over the auto-picked face.
        """
        data = dict(data)
        if "font_size" in data and "ui_font_size" not in data:
            data["ui_font_size"] = data.pop("font_size")
        if "font_family" in data and "term_font_family" not in data:
            fam = data.pop("font_family") or ""
            if fam and fam != "Consolas":
                data["term_font_family"] = fam
        return data

    def save(self) -> bool:
        # **返回是否真的落盘了。** 原来是无条件 `return`，于是调用方
        # （`main_window` 的设置保存路径）先无条件闪一句「设置已保存」，
        # 随后发现返回 None 什么也做不了 —— 用户以为存了，下次启动全部
        # 回退，而**要到关窗口时**才解释「配置没有被保存」。
        # **读不出来的那一份，绝不能被默认值盖掉。** 原来的路径是：
        # 解析失败 -> `load_from` 回 `cls()` -> 界面正常起来（用户看不出
        # 任何异常）-> 关窗口时 `closeEvent` 无条件 save() -> 用户手改的
        # 配置永久消失，且从头到尾没有一句提示。`SessionStore` 早就有
        # `_writable` 这道闸，这里是同一个问题的另一半。
        if self.load_error:
            log.error("配置读不出来，拒绝覆写：%s", self.load_error)
            return False
        # **没有绑定落盘路径就拒绝写。**
        #
        # `_path` 只有 `AppSettings.load()` 会设置（全仓只有它调 `load_from`）。
        # 而这里原来写的是 `self._path or SETTINGS_FILE` —— 也就是说，
        # **任何一个直接 `AppSettings()` 造出来、没设 `_path` 的对象，只要
        # 调一次 `save()`，就会把用户真实的 `%APPDATA%\MavisSSH\settings.json`
        # 覆盖成它的内容。** 触发它不需要多复杂的路径：任何临时探针、单元
        # 测试、将来任何一个新调用方，建个干净对象改两个字段存一下就中了。
        #
        # 这个口子不是理论风险 —— 它已经被踩了不止一次，实测把用户的主题
        # 从 dark 改成 light、界面字号改成 12、界面字体改成 Arial，
        # 而用户那边只看到「我的配置怎么变了」，没有任何报错。
        #
        # **靠「提醒每次都记得设 `_path`」不管用**：那是纪律，不是机制。
        # 这里把它变成机制 —— 没有路径就没有落点，物理上写不到。
        if not self._path:
            log.error("这份配置没有绑定落盘路径（_path 为空），拒绝写入：%s",
                      SETTINGS_FILE)
            return False
        self.save_to(self._path)
        return True

    def save_to(self, path: Path) -> None:
        # Write to a sibling then replace, the same way SessionStore.save does.
        # Writing straight to the target meant a crash or a full disk mid-write
        # left the user with a truncated -- and therefore unparseable -- config,
        # which loads() then swallows as "all defaults".
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
