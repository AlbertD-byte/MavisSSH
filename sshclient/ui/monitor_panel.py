"""Resource monitor panel.

Polls a single bash command over SSH at a configurable interval and renders
CPU, memory, disks, network, load, and top processes. The probe text uses
simple `##SECTION##` markers so we don't depend on the remote having
Python or any particular toolchain.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from PyQt6 import QtCore, QtGui, QtWidgets

from sshclient.ui import icons, thread_post

from ..core.settings import AppSettings
from ..remote.connection import SSHConnection
from .design.v1.palette import get_palette

# 合法区间只由设置层定义一次（load() 已按它钳过一遍）。面板原来自己又写了一道
# max(250, ms)：比设置更严，于是设置允许的 100ms 永远设不上（同一件事两个真相）。
_INTERVAL_MS = AppSettings._INT_RANGE["monitor_interval_ms"]

# 下拉档位。末两项刻意贴着设置上限：下拉覆盖不到上限，用户就只会看到「面板
# 不支持」，而设置里其实能填 —— 面板的档位必须能表示设置能表达的全部范围。
_INTERVAL_CHOICES = (0.1, 1, 2, 3, 5, 10, 15, 30, 60, 120, 300, 600)

# 卡片图标烘成位图时的边长。构造和换主题两处都要用同一个值，否则换完主题
# 图标大小会跟着悄悄变一次。
_CARD_ICON_PX = 15


def _interval_text(secs: float) -> str:
    """下拉项文案。数据统一存「秒」（float），文案只管人读得懂。"""
    return f"{secs:g} 秒"


def _card(icon_name: str, title: str, palette) -> QtWidgets.QFrame:
    """A titled card: icon + heading + content area."""
    frame = QtWidgets.QFrame()
    frame.setProperty("role", "card")
    lay = QtWidgets.QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 13)
    lay.setSpacing(9)

    head = QtWidgets.QHBoxLayout()
    head.setSpacing(7)
    ic = QtWidgets.QLabel()
    # pixmap 是构造那一刻的**像素**，不会跟着主题自己变。把图标名挂在动态属性
    # 上，apply_palette 才知道该重画哪一个（否则浅色主题下会留一排深色图标）。
    ic.setProperty("iconName", icon_name)
    ic.setPixmap(icons.icon_pixmap(icon_name, palette.text_muted, _CARD_ICON_PX))
    ic.setFixedWidth(17)
    head.addWidget(ic, 0, QtCore.Qt.AlignmentFlag.AlignTop)
    lab = QtWidgets.QLabel(title)
    lab.setProperty("role", "section")
    head.addWidget(lab)
    head.addStretch(1)
    lay.addLayout(head)
    return frame


PROBE_CMD = r"""
set +e
echo "##HOST##"; uname -n 2>/dev/null || hostname
echo "##OS##";   uname -srm
echo "##UPTIME##"; uptime
echo "##LOAD##"; cat /proc/loadavg 2>/dev/null || true
echo "##CPU##"; top -bn1 2>/dev/null | grep -E "^(%Cpu|Cpu)" | head -2
echo "##MEM##"; free -m
echo "##DISK##"; df -hP 2>/dev/null | head -n 20
echo "##NET##"; cat /proc/net/dev 2>/dev/null || ifconfig 2>/dev/null
echo "##PROC##"; ps -eo pid,user,%cpu,%mem,etime,comm --sort=-%cpu 2>/dev/null | head -n 16
echo "##END##"
""".strip()


@dataclass
class CpuSample:
    user: float = 0.0
    system: float = 0.0
    idle: float = 0.0
    iowait: float = 0.0

    @property
    def used(self) -> float:
        total = self.user + self.system + self.idle + self.iowait
        return max(0.0, min(100.0, 100.0 * (1 - self.idle / total) if total else 0.0))


@dataclass
class MemSample:
    total_mb: float
    used_mb: float
    free_mb: float
    avail_mb: float

    @property
    def percent(self) -> float:
        if not self.total_mb:
            return 0.0
        return 100.0 * self.used_mb / self.total_mb


@dataclass
class DiskSample:
    fs: str
    size: str
    used: str
    avail: str
    percent: float
    mount: str


@dataclass
class NetSample:
    rx_bytes: int
    tx_bytes: int


@dataclass
class ProcSample:
    pid: str
    user: str
    cpu: float
    mem: float
    etime: str
    comm: str


@dataclass
class HostInfo:
    name: str = ""
    os: str = ""
    uptime: str = ""
    load: str = ""


@dataclass
class MonitorSnapshot:
    host: HostInfo
    cpu: CpuSample | None = None
    mem: MemSample | None = None
    disks: list[DiskSample] = None
    net: NetSample | None = None
    procs: list[ProcSample] = None
    # error 非空 = 这一轮的输出**不可全信**。两种来源对界面的处理不同：
    # exec 抛错（什么都没拿到）与输出被截断（探针只跑了一半）。后者另带
    # incomplete 标记，用来区分「失败」和「不完整」。
    error: str = ""
    incomplete: bool = False

    def __post_init__(self):
        if self.disks is None:
            self.disks = []
        if self.procs is None:
            self.procs = []


# ---------------------------------------------------------------------------
# Parser


def _to_float(s: str) -> float:
    try:
        return float(s.replace(",", "."))
    except Exception:
        return 0.0


def parse_snapshot(text: str) -> MonitorSnapshot:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^##([A-Z]+)##\s*$", line.strip())
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            continue
        if current:
            sections[current].append(line)
    host = HostInfo(
        name="\n".join(sections.get("HOST", [])).strip(),
        os="\n".join(sections.get("OS", [])).strip(),
        uptime="\n".join(sections.get("UPTIME", [])).strip(),
        load="\n".join(sections.get("LOAD", [])).strip(),
    )

    snap = MonitorSnapshot(host=host)

    # ##END## 是探测命令最后一行（PROBE_CMD），所以「收到它」= 这次输出没被
    # 截断。超时/断连留下半截输出时，缺段本该让 CPU 条和底部时间戳露馅，
    # 原来却把残缺快照当完整数据照常显示 —— 一个没到货的更新，长得和一个
    # 真的更新一模一样。
    if "END" not in sections:
        snap.incomplete = True
        snap.error = "探测输出不完整（未收到 ##END##）"

    # CPU -- top -bn1 prints "%Cpu(s):  3.2 us, 1.0 sy, ... 95.0 id, ..."
    for line in sections.get("CPU", []):
        nums = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*([a-z]+)", line)
        cpu = CpuSample()
        for val, unit in nums:
            unit = unit.lower()
            v = _to_float(val)
            if unit.startswith("us"):
                cpu.user = v
            elif unit.startswith("sy"):
                cpu.system = v
            elif unit.startswith("id"):
                cpu.idle = v
            elif unit.startswith("wa") or unit.startswith("io"):
                cpu.iowait = v
        if cpu.idle or cpu.user or cpu.system:
            snap.cpu = cpu
            break

    # Memory
    for line in sections.get("MEM", []):
        parts = line.split()
        if parts and parts[0].lower().startswith("mem"):
            try:
                total, used, free, _shared, _buff, avail = (float(x) for x in parts[1:7])
                snap.mem = MemSample(total, used, free, avail)
            except Exception:
                pass
            break

    # Disks
    for line in sections.get("DISK", []):
        parts = line.split()
        if len(parts) < 6 or parts[0] in ("Filesystem",):
            continue
        try:
            pct = _to_float(parts[4].rstrip("%"))
        except Exception:
            continue
        snap.disks.append(DiskSample(
            fs=parts[0], size=parts[1], used=parts[2], avail=parts[3],
            percent=pct, mount=parts[5],
        ))

    # Network -- /proc/net/dev
    rx_total = tx_total = 0
    for line in sections.get("NET", []):
        if ":" not in line:
            continue
        try:
            name, rest = line.split(":", 1)
            fields = rest.split()
            # recv bytes is fields[0], send bytes is fields[8]
            rx_total += int(fields[0])
            tx_total += int(fields[8])
        except Exception:
            continue
    if rx_total or tx_total:
        snap.net = NetSample(rx_total, tx_total)

    # Processes -- skip the ps header line (PID is non-numeric)
    for line in sections.get("PROC", []):
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        if not parts[0].isdigit():
            continue
        try:
            snap.procs.append(ProcSample(
                pid=parts[0], user=parts[1], cpu=_to_float(parts[2]),
                mem=_to_float(parts[3]), etime=parts[4], comm=parts[5].strip(),
            ))
        except Exception:
            continue

    return snap


# ---------------------------------------------------------------------------
# Bar widget (a horizontal "used/total" bar with a percent label).


class Bar(QtWidgets.QWidget):
    """Rounded usage bar, themed from the shared palette."""

    def __init__(self, palette=None, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._palette = palette or get_palette("dark")
        self.setMinimumHeight(22)
        self._percent = 0.0
        self._label = ""
        self._color = QtGui.QColor(self._palette.success)

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.set_value(self._percent, self._label)

    def set_value(self, percent: float, label: str = "") -> None:
        self._percent = max(0.0, min(100.0, percent))
        self._label = label
        p = self._palette
        if percent >= 90:
            self._color = QtGui.QColor(p.danger)
        elif percent >= 75:
            self._color = QtGui.QColor(p.warning)
        else:
            self._color = QtGui.QColor(p.success)
        self.update()

    def _label_color(self) -> str:
        """标签（百分比/用量）的墨色。

        **标签压在自己垫的那块胶囊上**，胶囊是轨道底色 `bg_elevated`，
        所以墨色用 `p.text` 即可：`text` 压 `bg_elevated` 实测
        **12.838（深）/ 14.300（浅）**，两套主题都远超 AA。

        ## 为什么垫一块胶囊，而不是按填充色算黑白

        标签是**居中**画在整条上的，而底色随占用率从「全是轨道」变成
        「一半轨道一半填充」再变成「全是填充」——**标签会横跨两种底**。
        实测（几何结论，与字体无关）：占用率 ≤35% 时标签 **120/120 像素
        全落在轨道上**，50% 时一半一半，≥65% 时全落在填充上。

        于是「按填充色算一种墨色」在低占用率时是**反的**：深色主题算出的
        近黑压轨道只有 **1.272**，标签等于消失 —— 而空闲机器的常态恰恰是
        低占用率。（这正是第一版改法踩的坑。）

        反过来，老实现用固定 `p.text`：轨道上可读（12.8），压在填充上只有
        **2.163 / 2.150 / 2.865** —— 三档状态色**全破 AA**。

        **两块垫底 + 一种墨色，是唯一让两种底都达标的画法**：
        标签底下永远是一块 `bg_elevated`，墨色永远是 `p.text`，
        不管底下的填充走到哪儿。而条没填满时胶囊与轨道同色，**看不见**；
        填满了才在彩色段上显出一块同色底的徽标。

        写成方法而不是就地算，是为了让判据能**独立验这一条**：
        `tests/_filltext.py` 直接问「这个条会用哪个颜色画标签」，
        再拿它跟**标签实际压在的那块底色**算对比度。
        """
        return self._palette.text

    def _chip_color(self) -> str:
        """标签底下那块胶囊的颜色。

        **必须是轨道底色** —— 标签因此永远压在一块对比度已知的底上，
        不管底下的填充走到哪儿（见 `_label_color` 的说明）。

        单列一个方法，是为了让判据能问「标签压在什么颜色上」。
        """
        return self._palette.bg_elevated

    def _label_chip(self, label: str) -> QtCore.QRectF:
        """标签底下那块胶囊的范围。

        **用实际字体度量算**，不能拍一个固定宽度 —— 标签内容随快照变化
        （「45%」对「45%   1.2G / 8.0G」差得远），宽度对不上胶囊就露馅。
        """
        fm = QtGui.QFontMetrics(self.font())
        box = QtCore.QRectF(0.0, 0.0,
                            float(fm.horizontalAdvance(label)) + 12.0,
                            float(fm.height()) + 2.0)
        box.moveCenter(QtCore.QPointF(self.rect().center()))
        w = float(self.rect().width())
        if box.width() > w:          # 标签比整条还宽时不能让胶囊溢出
            box.setWidth(w)
        return box

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        r = self.rect().adjusted(0, 0, -1, -1)
        radius = r.height() / 2

        track = QtGui.QColor(self._palette.bg_elevated)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, radius, radius)

        w = int(r.width() * self._percent / 100)
        if w > 0:
            fill = QtCore.QRectF(r.left(), r.top(), max(w, r.height()), r.height())
            p.setBrush(self._color)
            p.drawRoundedRect(fill, radius, radius)

        label = self._label or f"{self._percent:.0f}%"
        # **标签下面垫一块与轨道同色的胶囊** —— 标签因此永远压在一块
        # 对比度已知的底上（见 `_label_color` 的说明）。
        chip = self._label_chip(label)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(self._chip_color()))
        p.drawRoundedRect(chip, chip.height() / 2, chip.height() / 2)

        p.setPen(QtGui.QColor(self._label_color()))
        p.drawText(r, QtCore.Qt.AlignmentFlag.AlignCenter, label)
        p.end()


# ---------------------------------------------------------------------------
# Main panel


class MonitorPanel(QtWidgets.QWidget):
    def __init__(self, conn: SSHConnection, interval_ms: int = 2000,
                 palette=None, parent: QtWidgets.QWidget | None = None):
        super().__init__(parent)
        self._conn = conn
        self._palette = palette or get_palette("dark")

        # ---- header ----
        self.host_label = QtWidgets.QLabel("—")
        self.host_label.setProperty("role", "host")
        self.os_label = QtWidgets.QLabel("")
        self.os_label.setProperty("role", "muted")
        self.uptime_label = QtWidgets.QLabel("")
        self.uptime_label.setProperty("role", "metric")
        self.load_label = QtWidgets.QLabel("")
        self.load_label.setProperty("role", "metric")

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self.host_label)
        header.addWidget(self.os_label)
        header.addStretch(1)
        header.addWidget(self.uptime_label)
        header.addWidget(self.load_label)

        # ---- CPU / Memory ----
        self.cpu_bar = Bar(self._palette)
        self.mem_bar = Bar(self._palette)

        cpu_box = _card("cpu", "CPU", self._palette)
        cpu_box.layout().addWidget(self.cpu_bar)
        cpu_box.layout().addStretch(1)
        mem_box = _card("memory", "内存", self._palette)
        mem_box.layout().addWidget(self.mem_bar)
        mem_box.layout().addStretch(1)

        cpu_row = QtWidgets.QHBoxLayout()
        cpu_row.setSpacing(12)
        cpu_row.addWidget(cpu_box, 1)
        cpu_row.addWidget(mem_box, 1)

        # ---- Disk ----
        self.disk_table = QtWidgets.QTableWidget(0, 5)
        self.disk_table.setHorizontalHeaderLabels(["挂载点", "文件系统", "总大小", "已用", "使用率"])
        self.disk_table.verticalHeader().setVisible(False)
        self.disk_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.disk_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.disk_table.setShowGrid(False)
        self.disk_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        disk_box = _card("disk", "磁盘", self._palette)
        disk_box.layout().addWidget(self.disk_table)

        # ---- Network ----
        self.rx_label = QtWidgets.QLabel("—")
        self.rx_label.setProperty("role", "metric")
        self.tx_label = QtWidgets.QLabel("—")
        self.tx_label.setProperty("role", "metric")
        net_box = _card("network", "网络累计流量", self._palette)
        nv = net_box.layout()
        net_row = QtWidgets.QHBoxLayout()
        net_row.setSpacing(24)
        net_row.addWidget(QtWidgets.QLabel("↓ 接收"))
        net_row.addWidget(self.rx_label)
        net_row.addSpacing(16)
        net_row.addWidget(QtWidgets.QLabel("↑ 发送"))
        net_row.addWidget(self.tx_label)
        net_row.addStretch(1)
        nv.addLayout(net_row)

        # ---- Processes ----
        self.proc_table = QtWidgets.QTableWidget(0, 5)
        self.proc_table.setHorizontalHeaderLabels(["PID", "用户", "CPU%", "内存%", "命令"])
        self.proc_table.verticalHeader().setVisible(False)
        self.proc_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.proc_table.setShowGrid(False)
        self.proc_table.horizontalHeader().setSectionResizeMode(4, QtWidgets.QHeaderView.ResizeMode.Stretch)
        proc_box = _card("list", "进程 TOP", self._palette)
        proc_box.layout().addWidget(self.proc_table)

        # ---- footer ----
        self.status_label = QtWidgets.QLabel("正在采集…")
        self.status_label.setProperty("role", "muted")
        self.interval_combo = QtWidgets.QComboBox()
        # 数据统一存 float 秒：设置允许 100ms（0.1 秒）这种非整秒值，存成 int
        # 会被悄悄截成 0 秒 —— 那正是 QTimer 空转烧 CPU 的那个值。
        for secs in _INTERVAL_CHOICES:
            self.interval_combo.addItem(_interval_text(secs), float(secs))
        # 当前项不在这里写死：_set_interval() 末尾按真正在跑的间隔回写，否则
        # 下拉显示的是一个跟实际轮询周期毫无关系的秒数（构造后立刻被覆盖，
        # 真正会骗到人的是运行期从设置/恢复会话改间隔那条路）。
        self._syncing_interval = False
        self.interval_combo.setFixedWidth(90)
        self.interval_combo.currentIndexChanged.connect(self._on_interval_changed)

        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.status_label)
        bottom.addStretch(1)
        bottom.addWidget(QtWidgets.QLabel("刷新间隔"))
        bottom.addWidget(self.interval_combo)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(12)
        outer.addLayout(header)
        outer.addLayout(cpu_row)
        outer.addWidget(disk_box, 2)
        outer.addWidget(net_box)
        outer.addWidget(proc_box, 3)
        outer.addLayout(bottom)

        # Timer
        self._polling = threading.Lock()
        #: **「别再开始了」。** 定时器停了不代表在飞的那一轮停了 ——
        #: worker 可能正卡在 `conn.exec(..., timeout=10)` 里。这个标志让它
        #: 醒来后直接放弃，不再往一个可能已被销毁的面板上投。
        self._stopped = False
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._set_interval(interval_ms)
        self._timer.start()

    def apply_palette(self, palette) -> None:
        self._palette = palette
        self.cpu_bar.apply_palette(palette)
        self.mem_bar.apply_palette(palette)
        # 卡片图标的 pixmap 是在构造那一刻烘进位图的深色像素，重指向 Bar 不等于
        # 重画图标 —— 不重画的话浅色主题下就是一片深色小图标。
        for lab in self.findChildren(QtWidgets.QLabel):
            name = lab.property("iconName")
            if name:
                lab.setPixmap(icons.icon_pixmap(str(name), palette.text_muted,
                                                _CARD_ICON_PX))

    def stop(self) -> None:
        """停掉轮询。**在飞的那一轮也会放弃**（醒来后不再投递）。

        定时器停了不代表 worker 停了 —— 它可能正卡在
        ``conn.exec(PROBE_CMD, timeout=10)`` 里。所以这里除了停定时器，
        还要置 ``_stopped``，让在飞的那一轮醒来后直接走人。
        """
        self._stopped = True
        self._timer.stop()

    def set_interval_ms(self, ms: int) -> None:
        self._set_interval(ms)
        # 换间隔是「继续跑」的意思，所以把停止标志清掉。
        self._stopped = False
        self._timer.start()

    def _set_interval(self, ms: int) -> None:
        lo, hi = _INTERVAL_MS
        # 只按设置那张表钳一次。原来这里另有一道更严的 max(250, ms)，于是设置
        # 允许的 100ms 到不了面板（用户改了设置、面板却按 250ms 跑，且无提示）。
        self._timer.setInterval(max(lo, min(hi, int(ms))))
        self._sync_interval_combo(self._timer.interval())

    def _sync_interval_combo(self, ms: int) -> None:
        """把下拉回写成当前真正在跑的间隔。

        下拉是面板间隔的镜像：间隔从设置/恢复会话那边改过来时它不跟着走，
        面板就等于对外宣称一个自己根本没在用的秒数。
        """
        secs = ms / 1000.0
        idx = self.interval_combo.findData(secs)
        if idx < 0:
            # 固定档位没覆盖到的合法值（如 7 秒）也要能显示出来，否则还是回到
            # 「下拉说的不是实际值」的老问题，只能新增一项。
            self.interval_combo.addItem(_interval_text(secs), secs)
            idx = self.interval_combo.count() - 1
        if self.interval_combo.currentIndex() == idx:
            return
        # 回写是我自己发起的，别再当成用户操作推回去一遍。
        self._syncing_interval = True
        try:
            self.interval_combo.setCurrentIndex(idx)
        finally:
            self._syncing_interval = False

    def _on_interval_changed(self, _idx: int) -> None:
        if self._syncing_interval:
            return
        self.set_interval_ms(
            int(round(float(self.interval_combo.currentData()) * 1000)))

    def _poll(self) -> None:
        # Run in worker thread to keep UI responsive
        def worker():
            # 上一轮还在飞就不开新一轮。每一轮都会在同一条 SSH 上开一个新
            # channel，堆积起来既拖慢连接，也正是「对端收到指向已不存在
            # channel 的报文」这类传输层错误的来源。
            if not self._polling.acquire(blocking=False):
                return
            try:
                try:
                    out, _err = self._conn.exec(PROBE_CMD, timeout=10)
                    snap = parse_snapshot(out)
                except Exception as exc:
                    snap = MonitorSnapshot(host=HostInfo())
                    snap.error = str(exc)
                # **投之前先看面板还在不在、还在跑没有。**
                # 这一行曾经直接 `invokeMethod(self, ...)`，而面板是
                # SessionTab 的子控件 —— 关标签时 `w.deleteLater()` 连带把它
                # 销毁了，而 worker 可能正卡在上面的 `exec` 里最多 10 秒。
                # 醒来再投就撞：
                #   RuntimeError: wrapped C/C++ object of type MonitorPanel
                #   has been deleted
                # 详见 thread_post.py 的说明。
                if self._stopped:
                    return
                thread_post.post(self, "_apply_snapshot",
                                 QtCore.Q_ARG(object, snap))
            finally:
                # 漏掉这一句，监控就永远停在"有轮次在飞"，再也不刷新了
                self._polling.release()
        threading.Thread(target=worker, daemon=True).start()

    @QtCore.pyqtSlot(object)
    def _apply_snapshot(self, snap: MonitorSnapshot) -> None:
        import datetime
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        if snap.error and not snap.incomplete:
            # 真失败（exec 抛错）：这一轮什么都没有，整块停更，保住上一轮的显示。
            self.status_label.setText(f"错误：{snap.error}")
            return
        if snap.incomplete:
            # 截断的一轮**不作为有效更新**：残段里哪些是真数据我们无从判断
            # （探针可能死在任何一段中间），拿它覆盖显示等于把一份不完整的
            # 观测当成本轮的结论。所以已知值全部留着，只做两件事：
            #   1) 把两条 Bar 清成「未知」——它们是唯一会持续重绘的读数，留着
            #      上一轮的数值就等于把旧读数顶着新时间戳当成现在的读数；
            #   2) 时间戳照跳但明写「不完整」，让这次刷新不再伪装成一次成功。
            self.cpu_bar.set_value(0.0, "未知")
            self.mem_bar.set_value(0.0, "未知")
            self.status_label.setText(f"更新于 {stamp} · "
                                       f"{snap.error or '本次输出不完整'}")
            return

        self.host_label.setText(snap.host.name or "未知主机")
        self.os_label.setText(snap.host.os)
        # uptime already contains "load average: ..." -- keep only the uptime
        # part here, the precise load is shown separately from /proc/loadavg.
        self.uptime_label.setText(snap.host.uptime.split("load average")[0].strip())
        self.load_label.setText(f"负载 {snap.host.load}")

        if snap.cpu:
            self.cpu_bar.set_value(
                snap.cpu.used,
                f"{snap.cpu.used:.0f}%   用户 {snap.cpu.user:.0f}% · 系统 {snap.cpu.system:.0f}% · 空闲 {snap.cpu.idle:.0f}%",
            )
        else:
            # 完整的一轮里缺 CPU 段：不是「没变化」，是「这一段没发过来」。
            # Bar 会保留上一次的数值继续 paint，旧读数就被当成本轮读数显示了。
            self.cpu_bar.set_value(0.0, "未知")
        if snap.mem:
            self.mem_bar.set_value(
                snap.mem.percent,
                f"{snap.mem.percent:.0f}%   {snap.mem.used_mb:.0f}M / {snap.mem.total_mb:.0f}M",
            )
        else:
            self.mem_bar.set_value(0.0, "未知")

        # Disks
        self.disk_table.setRowCount(len(snap.disks))
        for row, d in enumerate(snap.disks):
            vals = (d.mount, d.fs, d.size, d.used, f"{d.percent:.0f}%")
            for col, val in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(val)
                if col == 4:
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                    if d.percent >= 90:
                        item.setForeground(QtGui.QColor(self._palette.danger))
                    elif d.percent >= 75:
                        item.setForeground(QtGui.QColor(self._palette.warning))
                    else:
                        item.setForeground(QtGui.QColor(self._palette.text))
                elif col in (2, 3):
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                    item.setForeground(QtGui.QColor(self._palette.text_dim))
                self.disk_table.setItem(row, col, item)

        # Net
        if snap.net:
            self.rx_label.setText(_format_bytes(snap.net.rx_bytes))
            self.tx_label.setText(_format_bytes(snap.net.tx_bytes))

        # Processes
        self.proc_table.setRowCount(len(snap.procs))
        for row, p in enumerate(snap.procs):
            vals = [p.pid, p.user, f"{p.cpu:.1f}", f"{p.mem:.1f}", p.comm]
            for col, val in enumerate(vals):
                item = QtWidgets.QTableWidgetItem(val)
                if col in (2, 3):
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.proc_table.setItem(row, col, item)

        import datetime
        self.status_label.setText("更新于 " + stamp)


def _format_bytes(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(n)
    for i, u in enumerate(units):
        if size < 1024 or i == len(units) - 1:
            return f"{int(size)} {u}" if u == "B" else f"{size:.1f} {u}"
        size /= 1024
    return f"{n} B"
