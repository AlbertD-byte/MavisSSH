"""Stand-in for a local lrzsz binary: replies in small, delayed chunks.

Used by _zmtest.py to prove whether ZmodemPump actually forwards the local
process's output while the process is still running, or only after it exits.
ZMODEM is a small-packet protocol, so a pump that waits for a full buffer
will never make progress.
"""
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "reply"

if mode == "reply":
    # wait for the remote's ZRQINIT, then answer with a short ZRINIT split
    # across two writes, well under any read() buffer size.
    data = sys.stdin.buffer.read(1)          # blocking read of 1 byte
    sys.stdout.buffer.write(b"**\x18B01000")
    sys.stdout.buffer.flush()
    time.sleep(0.4)                          # process stays alive
    sys.stdout.buffer.write(b"000023be50\r\x8a\x11")
    sys.stdout.buffer.flush()
    time.sleep(1.2)                          # still alive...
    sys.exit(0)                              # ...then exit
else:
    sys.exit(0)
