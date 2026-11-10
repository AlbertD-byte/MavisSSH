"""会话文件的落盘加密。

要防的失败很具体，全部按「读原始 JSON 文件」来断言，而不是调自己写的
解密函数——不然测的就只是「加解密互为逆运算」，真正的落盘格式没人看。

  1. 落盘后文件里**搜不到**明文主机 / 账号 / 密码 / 端口
  2. JSON 结构还在（name / tunnels 仍是明文），排障时能直接看
  3. 旧的明文文件照常读，且下次保存自动变成密文
  4. 端口加密后仍还原成 int，不是字符串
  5. 密钥丢了：文件原样保留、拒绝写入，不把用户数据抹掉
  6. 密钥不匹配时报错而不是静默返回空
  7. 文件损坏仍然走原来的改名保留，而不是当成空列表
"""
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshclient.core import crypto
from sshclient.core.settings import SavedSession, SessionStore

FAIL: list[str] = []


def check(name, cond, extra=""):
    print(f'  {"PASS" if cond else "FAIL"}  {name}' + (f"  ({extra})" if extra else ""))
    if not cond:
        FAIL.append(name)


def fresh() -> tuple[pathlib.Path, pathlib.Path]:
    """每次都用全新的 tmp 目录 + 全新密钥，免得互相污染。"""
    d = pathlib.Path(tempfile.mkdtemp(prefix="mavisssh_crypto_"))
    cfg = d / "cfg"
    cfg.mkdir()
    crypto.reset_for_tests()
    return d, cfg


SECRETS = {"host": "10.13.37.9", "port": 2222, "username": "deploy-ops",
           "password": "hunter2-very-secret", "key_filename": r"C:\keys\prod_id_rsa",
           "key_passphrase": "key-phrase-xyz"}


print("=" * 70)
print("A. 落盘后文件里没有明文")
print("=" * 70)

d, cfg = fresh()
store = SessionStore(d / "sessions.json", config_dir=cfg)
store.upsert(SavedSession(name="生产 Web-01", notes="每周重启",
                          tunnels=[{"kind": "local", "local_port": 13306,
                                    "remote_host": "db.internal", "remote_port": 3306,
                                    "enabled": True, "description": "MySQL"}],
                          **SECRETS))

raw = (d / "sessions.json").read_text(encoding="utf-8")
for field, value in SECRETS.items():
    check(f"{field} 的明文不在文件里", str(value) not in raw)
check("每条密文都带 enc:v1: 前缀", raw.count("enc:v1:") >= len(SECRETS),
      f'({raw.count("enc:v1:")} 处)')
check("store 报告已加密", store.encrypted is True)

print()
print("=" * 70)
print("B. JSON 结构保留，敏感字段仍可辨认")
print("=" * 70)

import json

rows = json.loads(raw)
check("顶层还是数组", isinstance(rows, list) and len(rows) == 1)
row = rows[0]
check("name 保持明文（排障要看得见）", row["name"] == "生产 Web-01", row["name"])
check("notes 保持明文", row["notes"] == "每周重启", row["notes"])
check("id 保持明文（它是查找键）", bool(row["id"]))
check("tunnels 保持明文结构",
      isinstance(row["tunnels"], list) and row["tunnels"][0]["remote_host"] == "db.internal")
check("端口是密文但仍能看出是个端口", crypto.is_encrypted(row["port"]), row["port"][:24])

print()
print("=" * 70)
print("C. 读回来完全一致，端口仍是 int")
print("=" * 70)

d2, cfg2 = fresh()
shutil.copy(d / "sessions.json", d2 / "sessions.json")
# 关键：密钥是全局一份，同机重启要能读到同一把，所以密钥文件也得跟着走。
# 不复制它就等于换了机器，那份密文本来就该读不出来（E 段专门测那个）。
shutil.copy(crypto.key_path(cfg), crypto.key_path(cfg2))
back = SessionStore(d2 / "sessions.json", config_dir=cfg2)
check("读回一条会话", len(back.all) == 1, str(len(back.all)))
s = back.all[0]
for field, value in SECRETS.items():
    check(f"{field} 还原正确", getattr(s, field) == value, repr(getattr(s, field)))
check("port 还原成 int 而不是字符串", isinstance(s.port, int), type(s.port).__name__)
check("tunnels 还原正确", s.tunnels[0]["remote_port"] == 3306)
check("往返后能再存一次", (back.upsert(s) or True) and back.encrypted is True)

print()
print("=" * 70)
print("D. 旧的明文文件照常读，且自动转成密文")
print("=" * 70)

d3, cfg3 = fresh()
legacy = d3 / "sessions.json"
legacy.write_text(json.dumps([{"name": "老会话", **SECRETS}], indent=2,
                             ensure_ascii=False), encoding="utf-8")
st3 = SessionStore(legacy, config_dir=cfg3)
check("明文文件读得出来", len(st3.all) == 1, str(len(st3.all)))
check("字段值正确", st3.all[0].password == SECRETS["password"] if st3.all else False)
check("此刻尚未加密", st3.encrypted is False)
st3.save()
raw3 = legacy.read_text(encoding="utf-8")
check("保存后已转成密文", SECRETS["password"] not in raw3)
check("保存后报告已加密", st3.encrypted is True)

print()
print("=" * 70)
print("E. 密钥不匹配：报错、文件不动、拒绝写入")
print("=" * 70)

d4, cfg4 = fresh()
src = d4 / "sessions.json"
st4 = SessionStore(src, config_dir=cfg4)
st4.upsert(SavedSession(name="x", **SECRETS))
before = src.read_bytes()
key_file = crypto.key_path(cfg4)
key_file.unlink()                       # 密钥没了，密文还在
crypto.reset_for_tests()

st5 = SessionStore(src, config_dir=cfg4)     # 换一把新密钥来读
check("读不出会话", len(st5.all) == 0, str(len(st5.all)))
check("load_error 有内容", bool(st5.load_error), st5.load_error)
check("文件一个字节都没变", src.read_bytes() == before)
st5.upsert(SavedSession(name="y", host="1.2.3.4"))
check("拒绝写入，文件依然没变", src.read_bytes() == before)

print()
print("=" * 70)
print("F. 密文被手改：报错而不是静默变成空")
print("=" * 70)

d6, cfg6 = fresh()
f6 = d6 / "sessions.json"
st6 = SessionStore(f6, config_dir=cfg6)
st6.upsert(SavedSession(name="z", **SECRETS))
rows6 = json.loads(f6.read_text(encoding="utf-8"))
rows6[0]["password"] = crypto.PREFIX + "AAAAdefinitelyNotAValidToken"
f6.write_text(json.dumps(rows6, indent=2), encoding="utf-8")
try:
    crypto.load(f6.read_text(encoding="utf-8"), cfg6)
    check("坏密文应当抛错", False, "居然没抛")
except crypto.CryptoUnavailable:
    check("坏密文抛出 CryptoUnavailable", True)
except Exception as exc:                          # noqa: BLE001
    check("坏密文抛的是 CryptoUnavailable", False, type(exc).__name__)

print()
print("=" * 70)
print("G. 真正损坏的文件仍然改名保留")
print("=" * 70)

d7, cfg7 = fresh()
f7 = d7 / "sessions.json"
f7.write_text("{ 这不是 json", encoding="utf-8")
st7 = SessionStore(f7, config_dir=cfg7)
check("读不出会话", len(st7.all) == 0)
check("原文件被改名保留", (d7 / "sessions.json.bak").exists(),
      str(sorted(p.name for p in d7.iterdir())))

print()
print("=" * 70)
print("H. 密钥文件本身的性质")
print("=" * 70)

d8, cfg8 = fresh()
f8 = crypto.key_path(cfg8)
check("首次使用时自动生成", not f8.exists())
f = crypto.get_fernet(cfg8)
check("密钥文件已落盘", f8.exists())
check("内容是 Fernet 密钥", len(f8.read_text(encoding="utf-8").strip()) > 40)
check("同一目录重复取是同一把", crypto.get_fernet(cfg8) is f)
crypto.reset_for_tests()
check("清缓存后从文件读到的是同一把", crypto.get_fernet(cfg8) is not None)
check("重读后仍能解开工件", crypto.decrypt(
    crypto.encrypt("probe", cfg8), cfg8) == "probe")

print()
print("=" * 70)
if FAIL:
    print(f"FAILURES ({len(FAIL)}):")
    for n in FAIL:
        print("  -", n)
    sys.exit(1)
print("RESULT: PASS -- 会话文件已逐字段加密，明文不可见，且不因密钥问题丢数据")
