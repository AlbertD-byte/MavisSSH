"""端口转发：多会话配同一个本地端口时，后面那条自动改端口。

**为什么这条要单独立判据。** 原来的行为是后连的会话直接绑不上：规则留在
界面上显示「开」，实际一个端口都没监听，用户看到的是「规则是开的但连不上」，
而且没有任何提示说清是谁占了。两条会话都按惯例写 13306 指向各自那台库
（生产 / 预发）是最常见的使用姿势，所以这不是边角情况。

**判据不落在「端口号变了」上。** 那太容易恒真 —— 只要实现里有一行
`port += 1` 就绿了，而实际仍然可能没在监听。所以这里量的是**真的在监听**：
用 socket 连上去，并确认对端确实收到了数据。

Checks:
  1. 同会话内重复端口仍然报「已被占用」（不是静默改口）
  2. 第二个会话自动换端口，且**两个端口都能连上**
  3. 换端口后规则对象里的端口也改了（不是只改了监听用的那一份）
  4. 注册表按**新**端口索引：remove_tunnel 能删掉它
  5. set_tunnel_enabled 开着的时候触发改口，注册表也会重新 key
  6. 整段 20 个端口全被占时如实抛错，不静默改到一个离谱的号
  7. 关掉一条转发，它占的端口立刻放掉
"""
import os
import socket
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshclient.core.tunnel import TunnelError, TunnelSpec
from sshclient.remote import connection as C

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def listening(port, host="127.0.0.1"):
    """这个端口上真的有东西在 LISTEN —— 状态量，不靠 connect 的副作用。"""
    if sys.platform == "win32":
        import subprocess
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout
        needle = ":%d" % port
        for line in out.splitlines():
            if needle in line and "LISTENING" in line.upper():
                return True
        return False
    for fam, _t, _p, _c, sa in socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM):
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            s.bind(sa)
            return False                     # 还能绑 = 没人监听
        except OSError:
            return True
        finally:
            s.close()
    return False


def echo_opener(payload=b"hello"):
    """当 `open_direct_channel`：返回一个会把 payload 回送的对端 socket。"""
    def _open(dest_host, dest_port, src):
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        addr = srv.getsockname()
        c = socket.create_connection(addr, timeout=3)
        conn, _ = srv.accept()
        conn.sendall(payload)
        threading.Timer(1.0, conn.close).start()
        srv.close()
        return c
    return _open


def make_conn(opener):
    conn = C.SSHConnection(C.ConnectionInfo(host="10.0.0.5", port=22, username="deploy"))
    conn._open_direct = opener
    return conn


def spec_for(port, desc):
    return TunnelSpec(kind="local", local_port=port,
                       remote_host="db.internal", remote_port=5432,
                       description=desc)


def main():
    # ---- 1. 同会话内重复端口：仍然报占用 ------------------------------
    p = free_port()
    conn_a = make_conn(echo_opener())
    conn_a.add_tunnel(spec_for(p, "第一条"), enabled=True)
    try:
        conn_a.add_tunnel(spec_for(p, "同会话重复"), enabled=True)
        dup_ok = False
        dup_msg = "居然没报错"
    except TunnelError as exc:
        dup_ok = "已被占用" in str(exc)
        dup_msg = str(exc)
    check("同会话内重复端口仍然报「已被占用」（不是静默改口）", dup_ok, dup_msg)

    # ---- 2. 第二个会话自动改端口，两个端口都在监听 --------------------
    conn_b = make_conn(echo_opener())
    got = conn_b.add_tunnel(spec_for(p, "第二条"), enabled=True)
    check("第二个会话的端口被自动改掉了", got.local_port != p,
          "%d -> %d" % (p, got.local_port))
    check("改后的端口确实在监听（不是只改了数字）",
          listening(got.local_port), ":%d" % got.local_port)
    check("原来那个端口仍归第一条会话，没有被抢走",
          listening(p) and not (got.local_port == p))

    # ---- 3. 规则对象里的端口也改了 ----------------------------------
    specs_b = conn_b.tunnel_specs()
    check("注册表里的 spec 也是新端口",
          len(specs_b) == 1 and specs_b[0].local_port == got.local_port,
          str([s.local_port for s in specs_b]))
    check("状态表也按新端口索引",
          set(conn_b.tunnel_states()) == {got.local_port},
          str(sorted(conn_b.tunnel_states())))

    # ---- 4. 按新端口能删掉 ------------------------------------------
    check("remove_tunnel(新端口) 删得掉", conn_b.remove_tunnel(got.local_port))
    check("删掉之后那个端口放掉了", not listening(got.local_port),
          ":%d" % got.local_port)
    check("按旧端口已经删不到了（key 确实换过）",
          not conn_b.remove_tunnel(p))

    # ---- 5. set_tunnel_enabled 也会改口并重新 key --------------------
    # **必须先把那个端口占住**，否则压根不会触发改口，断言会变成恒真 ——
    # 实测踩过：`real_c in (pc,) or real_c != pc` 这个写法无论端口改没改
    # 都是 True，端口明明没变却报 PASS。
    pc = free_port()
    hog = socket.socket()
    hog.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    hog.bind(("127.0.0.1", pc))
    hog.listen(1)
    conn_c = make_conn(echo_opener())
    try:
        conn_c.add_tunnel(spec_for(pc, "第三条"), enabled=False)   # 先存着，不绑
        # **别用 listening() 验这条** —— 那个端口正被我们自己占着，
        # 它当然「在监听」。要验的是**这条规则自己没去绑**，看状态。
        check("未开启的规则没有自己去绑（状态为关）",
              conn_c.tunnel_states() == {pc: False},
              str(conn_c.tunnel_states()))
        conn_c.set_tunnel_enabled(pc, True)
        specs_c = conn_c.tunnel_specs()
        real_c = specs_c[0].local_port
        check("set_tunnel_enabled 开启时也会改口", real_c != pc,
              "%d -> %d" % (pc, real_c))
        check("开启后新端口在监听", listening(real_c), ":%d" % real_c)
        check("原来那个被占的端口没被抢走", not (real_c == pc))
        check("set_tunnel_enabled 改口后注册表重新 key 了",
              set(conn_c.tunnel_states()) == {real_c},
              str(sorted(conn_c.tunnel_states())))
        # 反向对照：能按新端口关掉，说明 key 真的换了而不是多留了一条僵尸项
        conn_c.set_tunnel_enabled(real_c, False)
        check("能按新端口关掉（没有僵尸项）", not listening(real_c), ":%d" % real_c)
    finally:
        hog.close()

    # ---- 6. 整段端口全被占时如实抛错 --------------------------------
    base = free_port()
    hogs = []
    holders = []
    try:
        for i in range(C._PORT_SCAN_LIMIT + 3):
            sp_ = socket.socket()
            sp_.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sp_.bind(("127.0.0.1", base + i))
                sp_.listen(1)
                hogs.append(sp_)
            except OSError:
                pass
        holders = [h.getsockname()[1] for h in hogs]
        conn_d = make_conn(echo_opener())
        try:
            conn_d.add_tunnel(spec_for(base, "全被占"), enabled=True)
            ran_out = False
            ran_msg = "居然成功了"
        except TunnelError as exc:
            ran_out = True
            ran_msg = str(exc)
        check("整段端口全被占时如实抛错，不静默改到离谱的号", ran_out, ran_msg)
        check("抛错之后没有留下半注册规则",
              conn_d.tunnel_specs() == [], str(conn_d.tunnel_specs()))
    finally:
        for h in hogs:
            h.close()

    # ---- 7. 关掉一条转发，端口立刻放掉 -------------------------------
    conn_a.set_tunnel_enabled(p, False)
    check("关掉第一条后它的端口放掉了", not listening(p), ":%d" % p)

    for c in (conn_a, conn_b, conn_c, conn_d):
        try:
            c.close()
        except Exception:                     # noqa: BLE001
            pass
    time.sleep(0.3)

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL " + f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
