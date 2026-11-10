"""主机密钥确认对话框。

**为什么单独一个类。** 需求是「让用户能拿指纹去跟管理员核对」——
所以框里要能（1）看清指纹、（2）一键复制、（3）**手输对方给的指纹**做比对。

用标准 ``QMessageBox`` 做不到第 3 条（它只有几个按钮，没有输入框），
所以这里自绘一个。

**默认焦点落在「取消」上**：安全默认，防「一路回车」把一台陌生主机
点了信任。焦点顺序是 取消 → 仅此一次 → 信任并记住 —— 最安全的在最前。
"""
from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from ..remote.hostkeys import HostKeyRequest
from .design.v1.palette import Palette

#: 三个决策
TRUST = "trust"
TRUST_ONCE = "trust_once"
REJECT = "reject"


def _fingerprint_matches(given: str, expected: str) -> bool:
    """用户手输的指纹和实际的是否一致。

    **比对要宽容一点**：用户多半是**从别处复制**过来的，可能带冒号、
    带空格、大小写不一样、或者用了 OpenSSH 老式的 MD5。

    做法：两边都归一化成「纯小写十六进制串」再比。
    - ``SHA256:hXQ2...`` → 去掉前缀与 padding，剩下的 base64 解成 hex
    - ``aa:bb:cc:...`` → 去掉冒号

    两种编码方式不同（base64 vs hex），所以**只在**编码方式一致时可比
    —— SHA256 和 MD5 之间不互认（那是两个不同的算法）。
    """
    g = (given or "").strip()
    e = (expected or "").strip()
    if not g or not e:
        return False

    def norm(s: str) -> tuple[str, str]:
        """→ (算法, 归一化后的指纹)。"""
        s = s.strip()
        if s.upper().startswith("SHA256:"):
            import base64
            body = s[7:].rstrip("=")
            try:
                return "sha256", base64.b64decode(body).hex().lower()
            except Exception:      # noqa: BLE001
                return "sha256", body.lower()
        # MD5：16 字节。**带冒号**（PuTTY/WinSCP 格式）或**不带**。
        # 注意判据是「32 位 hex」而不是「冒号分隔」—— 冒号分隔的短串
        # （比如 `aa:bb:cc`）长度不够 32，不是 MD5，别误认。
        flat = s.replace(":", "").replace(" ", "")
        if len(flat) == 32 and all(c in "0123456789abcdefABCDEF" for c in flat):
            return "md5", flat.lower()
        return "unknown", s.lower()

    ga, gv = norm(g)
    ea, ev = norm(e)
    if ga != ea or ga == "unknown":
        return False
    return gv == ev


class HostKeyDialog(QtWidgets.QDialog):
    """「首次连接到这台主机」—— 展示指纹、让用户核对、给出三态决策。"""

    def __init__(self, req: HostKeyRequest, palette: Palette, parent=None):
        super().__init__(parent)
        self.setWindowTitle("未知主机")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._req = req
        self._palette = palette
        self._decision = REJECT

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        head = QtWidgets.QLabel("您还没有信任过这台主机，无法确认它就是您要连的那台。")
        head.setWordWrap(True)
        root.addWidget(head)

        # ---- 指纹（等宽字体，便于目视比对）----
        fp_box = QtWidgets.QWidget()
        fp_lay = QtWidgets.QVBoxLayout(fp_box)
        fp_lay.setContentsMargins(0, 0, 0, 0)
        fp_lay.setSpacing(4)

        host_row = QtWidgets.QHBoxLayout()
        host_row.addWidget(QtWidgets.QLabel("主机："))
        host_val = QtWidgets.QLabel(req.hostname)
        host_val.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        host_row.addWidget(host_val)
        host_row.addStretch(1)
        fp_lay.addLayout(host_row)

        sha_row = QtWidgets.QHBoxLayout()
        sha_row.addWidget(QtWidgets.QLabel("SHA-256："))
        sha_val = QtWidgets.QLabel(req.fingerprint)
        sha_val.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        sha_val.setFont(QtGui.QFontDatabase.systemFont(
            QtGui.QFontDatabase.SystemFont.FixedFont))
        sha_row.addWidget(sha_val, 1)
        copy_btn = QtWidgets.QPushButton("复制")
        copy_btn.setToolTip("复制 SHA-256 指纹，粘给管理员核对")
        copy_btn.clicked.connect(
            lambda: QtWidgets.QApplication.clipboard().setText(req.fingerprint))
        sha_row.addWidget(copy_btn)
        fp_lay.addLayout(sha_row)

        md5 = req.md5_fingerprint
        if md5:
            md5_row = QtWidgets.QHBoxLayout()
            md5_lab = QtWidgets.QLabel("MD5（兼容旧资料）：")
            md5_lab.setProperty("role", "muted")
            md5_row.addWidget(md5_lab)
            md5_val = QtWidgets.QLabel(md5)
            md5_val.setTextInteractionFlags(
                QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            md5_val.setFont(QtGui.QFontDatabase.systemFont(
                QtGui.QFontDatabase.SystemFont.FixedFont))
            md5_row.addWidget(md5_val, 1)
            fp_lay.addLayout(md5_row)
        root.addWidget(fp_box)

        # ---- 手输指纹核对 ----
        check_lay = QtWidgets.QVBoxLayout()
        check_lay.setSpacing(4)
        check_lab = QtWidgets.QLabel(
            "如果管理员给了指纹，粘贴到这里自动比对：")
        check_lab.setProperty("role", "muted")
        check_lay.addWidget(check_lab)
        self._check_edit = QtWidgets.QLineEdit()
        self._check_edit.setPlaceholderText(
            "SHA256:… 或 aa:bb:cc:…（留空则不比对）")
        self._check_edit.textChanged.connect(self._on_check_changed)
        check_lay.addWidget(self._check_edit)
        self._check_result = QtWidgets.QLabel("")
        self._check_result.setProperty("role", "muted")
        self._check_result.setWordWrap(True)
        check_lay.addWidget(self._check_result)
        root.addLayout(check_lay)

        warn = QtWidgets.QLabel(
            "指纹对不上请点「取消」—— 这可能是中间人攻击。")
        warn.setWordWrap(True)
        root.addWidget(warn)

        # ---- 按钮 ----
        buttons = QtWidgets.QDialogButtonBox()
        self._btn_trust = buttons.addButton(
            "信任并记住", QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self._btn_once = buttons.addButton(
            "仅此一次", QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)
        self._btn_cancel = buttons.addButton(
            "取消", QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._on_trust)
        buttons.rejected.connect(self._on_cancel)
        self._btn_once.clicked.connect(self._on_once)
        # **安全默认：焦点落在「取消」上。** 防「一路回车」把陌生主机点了信任。
        self._btn_cancel.setDefault(True)
        self._btn_cancel.setFocus()
        root.addWidget(buttons)

    # ------------------------------------------------------------------ 逻辑

    def _on_check_changed(self, text: str) -> None:
        t = (text or "").strip()
        if not t:
            self._check_result.setText("")
            self._btn_trust.setEnabled(True)
            self._btn_once.setEnabled(True)
            return
        if _fingerprint_matches(t, self._req.fingerprint):
            self._check_result.setText("✓ 指纹一致。")
            self._btn_trust.setEnabled(True)
            self._btn_once.setEnabled(True)
        elif _fingerprint_matches(t, self._req.md5_fingerprint):
            self._check_result.setText("✓ 指纹一致（MD5 匹配）。")
            self._btn_trust.setEnabled(True)
            self._btn_once.setEnabled(True)
        else:
            self._check_result.setText(
                "✗ 与本次连接提供的指纹**不一致**。请与管理员核对；"
                "如果确认无误再点「信任并记住」。")
            # **不一致时不禁用「信任」** —— 有些场景管理员给的就是另一套
            # 编码格式（SHA256 与 MD5 都给），或者服务器刚换了密钥。
            # 但提示必须足够醒目。
            self._btn_trust.setEnabled(True)
            self._btn_once.setEnabled(True)

    def _on_trust(self) -> None:
        self._decision = TRUST
        self.accept()

    def _on_once(self) -> None:
        self._decision = TRUST_ONCE
        self.accept()

    def _on_cancel(self) -> None:
        self._decision = REJECT
        self.reject()

    def decision(self) -> str:
        """:data:`TRUST` / :data:`TRUST_ONCE` / :data:`REJECT`。"""
        return self._decision
