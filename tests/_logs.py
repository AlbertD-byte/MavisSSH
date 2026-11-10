"""日志：写到当前工作目录的 logs/ 下，并且不泄漏口令。

最容易出问题的不是「日志没写出来」，而是两件相反的事：
该记的没记（出问题查不到），以及**不该记的记了**——密码一旦进了日志文件，
那加密存储就白做了，因为日志通常比配置文件更容易被翻到、被同步、被贴进
issue。所以这里对「口令不出现」用的是硬断言。
"""
import logging
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshclient.core import logs

FAIL: list[str] = []


def check(name, cond, extra=""):
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f"  ({extra})" if extra else ""))
    if not cond:
        FAIL.append(name)


work = pathlib.Path(tempfile.mkdtemp(prefix="mavisssh_logs_"))
logs.reset_for_tests()

print("=" * 70)
print("A. 落到当前工作目录的 logs/ 下")
print("=" * 70)

base = work / "proj"
base.mkdir()
log_dir = logs.setup(base=base)
log = logs.get_logger("demo")

check("返回的目录是 <base>/logs", log_dir == base / "logs", str(log_dir))
check("目录已建出来", log_dir.is_dir())
check("还没有文件——delay=True，没记录就不该凭空造一个",
      not (log_dir / logs.LOG_FILENAME).exists())

log.info("第一条 INFO")
log.warning("第一条 WARNING")
log.error("第一条 ERROR")

f = log_dir / logs.LOG_FILENAME
check("记录之后文件出现了", f.exists(), str(log_dir.name))

text = f.read_text(encoding="utf-8")
check("INFO 落盘了", "第一条 INFO" in text)
check("WARNING 落盘了", "第一条 WARNING" in text)
check("ERROR 落盘了", "第一条 ERROR" in text)
check("带时间戳", text.startswith("20"), text[:24])
check("带 logger 名", "[demo]" in text)
check("是 UTF-8，中文没变乱码", "第一条" in text)

print()
print("=" * 70)
print("B. 重复 setup 不会堆出重复 handler")
print("=" * 70)

before = len(logging.getLogger().handlers)
logs.setup(base=base)
logs.setup(base=base)
after = len(logging.getLogger().handlers)
check("handler 数量没变", before == after, f"{before} -> {after}")

# 故意**不删文件**再写：RotatingFileHandler 在 Windows 上会把文件占住，
# 删不掉。handler 有几个，就会有几行同内容——数行数才是要断言的东西。
log.info("重复 setup 标记")
lines = [l for l in f.read_text(encoding="utf-8").splitlines()
         if "重复 setup 标记" in l]
check("同一条日志只写了一行（没有 handler 堆叠）", len(lines) == 1, str(len(lines)))

print()
print("=" * 70)
print("C. 轮转真的会发生")
print("=" * 70)

rot = work / "rot"
rot.mkdir()
logs.reset_for_tests()
rot_dir = logs.setup(base=rot, console=False)
big = logging.getLogger("mavisssh.rotate")
big.info("x" * 4096)
for i in range(700):                       # 约 2.8 MB，超过 MAX_BYTES
    big.info("填充 %d %s", i, "y" * 4096)

backups = sorted(rot_dir.glob(f"{logs.LOG_FILENAME}.*"))
check("产生了历史文件", len(backups) > 0, f"{len(backups)} 个")
check("历史文件不超过 BACKUP_COUNT", len(backups) <= logs.BACKUP_COUNT,
      f"{len(backups)} <= {logs.BACKUP_COUNT}")
check("单个文件没超过上限太多",
      all(p.stat().st_size <= logs.MAX_BYTES * 1.2 for p in backups),
      str([p.stat().st_size for p in backups[:3]]))

print()
print("=" * 70)
print("D. 脱敏：口令不能原样出现在日志里")
print("=" * 70)

check("空串", logs.redact("") == "")
check("短值整体打码", logs.redact("abc") == "***", logs.redact("abc"))
check("长值保留前缀", logs.redact("abcdefgh", keep=3) == "abc***", logs.redact("abcdefgh", keep=3))
check("不超过 keep 长度时整体打码", logs.redact("ab", keep=5) == "***", logs.redact("ab", keep=5))
check("None 也不炸", logs.redact(None) == "")

print()
print("=" * 70)
print("E. 连接层记日志时不会带出口令")
print("=" * 70)

# 真连一次会失败的，验证「认证失败」这条 warning 的内容
from sshclient.remote.connection import AuthConfig, SSHConnection

logs.reset_for_tests()
cap = work / "conn"
cap.mkdir()
cap_dir = logs.setup(base=cap, console=False)
SECRET = "sup3r-s3cret-passphrase"

cfg = AuthConfig(host="127.0.0.1", port=1, username="nobody",
                 password=SECRET, timeout=2)
try:
    SSHConnection.connect(cfg)
    check("应当连不上（127.0.0.1:1）", False, "居然连上了")
except Exception:
    pass

body = "\n".join(p.read_text(encoding="utf-8")
                 for p in cap_dir.glob("*.log"))
check("确实记了连接失败", "连接失败" in body or "认证失败" in body,
      body.strip()[-80:] if body else "(空)")
check("日志里没有口令", SECRET not in body)
check("日志里记了是谁在哪", "nobody@127.0.0.1:1" in body)

print()
print("=" * 70)
shutil.rmtree(work, ignore_errors=True)
if FAIL:
    print(f"FAILURES ({len(FAIL)}):")
    for n in FAIL:
        print("  -", n)
    sys.exit(1)
print("RESULT: PASS -- 日志落到 logs/，会轮转，且不带出口令")
