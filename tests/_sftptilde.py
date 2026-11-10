"""远程路径里的 `~`：SFTP 不认它，而它是用户在路径栏最想敲的东西。

**实测（对着真服务器，不是推断）：**

    listdir_attr("~/workspaces/qwen3.5-4b")  ->  FileNotFoundError [Errno 2]
    normalize("~/workspaces/qwen3.5-4b")      ->  同样失败（paramiko 也不展开）
    normalize(".")                             ->  "/home/deploy"
    家目录 + "/workspaces/qwen3.5-4b"         ->  OK，8 项

`~` 是 **shell 的概念**，SFTP 只认绝对路径。而原来 `RemoteFileSystem`
每个方法都把路径原样交出去，于是这一个 `~` 同时造成了两个症状：
远程面板「读取失败」，以及上传时的 `FileNotFoundError [Errno 2]` ——
两者走的是同一条路。

**判据钉在「展开后是什么」而不是「有没有那个 if」**：一个写死的
`_expand` 就算返回 `path` 也能让大多数用例绿。元断言负责抓那种。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshclient.ui.sftp_panel import RemoteFileSystem

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def _dl_dst():
    """download 的目标路径：给系统临时目录一个固定名，**绝不碰仓库里的文件**。"""
    import tempfile
    return os.path.join(tempfile.gettempdir(), "_mavis_dl_probe.bin")


class FakeSFTP:
    """只实现展开用得到的那几个方法。"""

    def __init__(self, home="/home/deploy", home_fails=False):
        self.home = home
        self.home_fails = home_fails
        self.calls = []

    def normalize(self, path):
        self.calls.append(("normalize", path))
        if self.home_fails:
            raise OSError("channel busy")
        if path == ".":
            return self.home
        return path

    def listdir_attr(self, path):
        self.calls.append(("listdir_attr", path))
        A = type("A", (), {"st_mode": 0o100644,
                           "st_size": 1, "st_mtime": 0.0})
        return [type("A", (), {"filename": n, "st_mode": 0o100644,
                               "st_size": 1, "st_mtime": 0.0})()
                for n in ("a", "b")]

    def stat(self, path):
        self.calls.append(("stat", path))
        return type("S", (), {"st_size": 10, "st_mode": 0o40755})()

    def get(self, r, l, **k):
        self.calls.append(("get", r))
        # **真 SFTP 会在这个路径上建出文件**（download 现在是「先写 .part
        # 再 os.replace」），替身不照做，那一步必然失败，测的就不是它了。
        with open(l, "wb") as f:
            f.write(b"x")
    def put(self, l, r, **k):
        self.calls.append(("put", r))
    def remove(self, p):
        self.calls.append(("remove", p))
    def rmdir(self, p):
        self.calls.append(("rmdir", p))
    def mkdir(self, p):
        self.calls.append(("mkdir", p))
    def rename(self, a, b):
        self.calls.append(("rename", a, b))

    def sent(self, name):
        return [c for c in self.calls if c[0] == name]


print("=== ~ 展开 ===")
sf = FakeSFTP()
rfs = RemoteFileSystem(sf)

check("~ 展开成远端家目录", rfs._expand("~/x") == "/home/deploy/x", rfs._expand("~/x"))
check("~ 单独用就是家目录", rfs._expand("~") == "/home/deploy")
check("~/ 结尾就是家目录", rfs._expand("~/") == "/home/deploy")
check("~user/... 不乱猜，原样透传给 SFTP 报错",
      rfs._expand("~root/x") == "~root/x")
check("绝对路径原样不动", rfs._expand("/tmp") == "/tmp")
check("中间含 ~ 的路径不受影响", rfs._expand("/a/~/b") == "/a/~/b")
check("家目录尾部斜杠不会拼出双斜杠",
      RemoteFileSystem(FakeSFTP("/home/deploy/"))._expand("~/x") == "/home/deploy/x")

print()
print("=== 每一个把路径交给 SFTP 的入口都展开了（漏一个就有一半是坏的）===")
CASES = [
    ("listdir",     lambda r: r.listdir("~/d"),     "listdir_attr"),
    ("stat",        lambda r: r.stat("~/f"),        "stat"),
    ("list_names",  lambda r: r.list_names("~/d"),  "listdir_attr"),
    ("is_dir",      lambda r: r.is_dir("~/d"),      "stat"),
    ("upload",      lambda r: r.upload(__file__, "~/f"), "put"),
    # **目标绝不能用 `__file__`。** download 现在是「写 .part 再 os.replace」，
    # 拿测试源码当目标，跑一次就把这个文件本身覆盖成假 SFTP 写的内容 ——
    # 这个坑我自己踩过，文件被写成一个字节的 `x`。
    ("download",    lambda r: r.download("~/f", _dl_dst()), "get"),
    ("remove",      lambda r: r.remove("~/f"),      "remove"),
    ("rmdir",       lambda r: r.rmdir("~/d"),       "rmdir"),
    ("mkdir",       lambda r: r.mkdir("~/new"),     "mkdir"),
]
for label, call, sftp_name in CASES:
    fake = FakeSFTP()
    r = RemoteFileSystem(fake)
    call(r)
    sent = fake.sent(sftp_name)
    check("%s 展开后才交给 SFTP" % label,
          bool(sent) and all("~" not in str(p) for p in sent[0][1:]),
          str(sent[0][1:]) if sent else "没调用")

print()
print("=== 问不到家目录时早失败，而不是卡住 ===")
busy = FakeSFTP(home_fails=True)
r = RemoteFileSystem(busy)
check("家目录取不到时路径原样透传（让 SFTP 报错，不猜）",
      r._expand("~/x") == "~/x", r._expand("~/x"))
check("getcwd 仍然返回 '/'（对外契约没动）", r.getcwd() == "/")

print()
print("=== 家目录是缓存的：is_dir / list_names 靠 0.25s 超时 _held ===")
# 它们若每次都去问网络，通道忙时会永久阻塞，正好废掉
# 「传输期间没有补全可以接受，但为了补全把界面卡住不行」那条设计。
rfs2 = RemoteFileSystem(FakeSFTP())
rfs2._expand("~/x")
n_before = len(FakeSFTP and rfs2._sftp.calls)
rfs2._expand("~/y")
check("第二次展开不再打网络",
      len(rfs2._sftp.calls) == n_before,
      "normalize 调用次数=%d" % len([c for c in rfs2._sftp.calls if c[0] == "normalize"]))

print()
print("=== 元断言：把 _expand 打回原样透传，上面那些必须变红 ===")
class Broken(RemoteFileSystem):
    def _expand(self, path):        # noqa: D401
        return path
bfake = FakeSFTP()
b = Broken(bfake)
b.listdir("~/d")
check("元断言：_expand 失效时 listdir 把 ~ 原样送出（所以上面那组不是恒真）",
      any("~" in str(c[1]) for c in bfake.calls),
      str(bfake.calls))

print()
if FAIL:
    print("失败 %d 项：" % len(FAIL))
    for f in FAIL:
        print("  - " + f)
    sys.exit(1)
print("通过 %d 项，失败 0 项" % len(PASS))
sys.exit(0)
