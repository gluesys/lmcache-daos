"""Stage 0: isolate the raw dfs read ceiling over UCX.

Separates three costs that the older bench conflated:
  (1) buffer allocation (create_string_buffer = malloc + zero-fill)
  (2) dfs_sys_read itself (GIL-releasing C call)
  (3) per-thread handle vs shared handle

Uses a PRE-ALLOCATED per-thread buffer pool so repeat runs measure read only.
This number is the payoff ceiling for every later GDS/pinned-staging stage.
"""
import os, sys, time, ctypes, threading
import concurrent.futures as cf
sys.path.insert(0, "/lmd")
from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]; CONT = os.environ["DAOS_TEST_CONT"]
SZ = int(os.environ.get("CEIL_SZ", str(28 * (1 << 20))))   # match LMCache chunk
NBLOB = int(os.environ.get("CEIL_N", "32"))

main = DfsSys(pool=POOL, cont=CONT)
paths = [f"/ceil_{i}" for i in range(NBLOB)]
data = b"\xa5" * SZ
for p in paths:
    main.write(p, data)
gb = NBLOB * SZ / 1e9
print(f"wrote {NBLOB} x {SZ//(1<<20)}MB = {gb:.2f} GB", flush=True)

tls = threading.local()

def _h():
    h = getattr(tls, "h", None)
    if h is None:
        h = tls.h = DfsSys(pool=POOL, cont=CONT)
    return h

def _buf():
    b = getattr(tls, "b", None)
    if b is None:
        b = tls.b = ctypes.create_string_buffer(SZ)   # allocated ONCE per thread
    return b

def read_pooled(p):
    h = _h(); buf = _buf()
    obj = h.open_rdonly(p)
    try:
        h.read_obj_into(obj, 0, SZ, buf)
    finally:
        h.close_obj(obj)

def read_fresh(p):
    h = _h()
    obj = h.open_rdonly(p)
    try:
        buf = ctypes.create_string_buffer(SZ)          # allocate EVERY read
        h.read_obj_into(obj, 0, SZ, buf)
    finally:
        h.close_obj(obj)

def run(fn, conc, label):
    best = 9e9
    for _ in range(3):
        t = time.time()
        with cf.ThreadPoolExecutor(max_workers=conc) as ex:
            list(ex.map(fn, paths))
        best = min(best, time.time() - t)
    print(f"{label:14s} conc={conc:2d}: {gb:.2f} GB in {best*1000:6.0f} ms = {gb/best:6.2f} GB/s", flush=True)

for c in [1, 4, 8, 16, 24]:
    run(read_pooled, c, "pooled-buf")
for c in [1, 8, 16]:
    run(read_fresh, c, "fresh-buf")

for p in paths:
    main.remove(p)
main.close()
