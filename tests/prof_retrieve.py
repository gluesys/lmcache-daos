"""Profile connector-side retrieve (batched_get) in isolation, over UCX.

Stores N large chunks then times conn.batched_get(keys) — the exact path vLLM's
LMCache calls — WITHOUT LMCache's post-retrieve GPU H2D staging. Compares to the
vLLM-observed ~2.2 GB/s to localize the ceiling (connector vs LMCache-internal).
Also sweeps thread-pool size to see parallel scaling.
"""
import os, sys, time, asyncio, threading
sys.path.insert(0, "/lmd")
import torch
from lmcache.v1.config import LMCacheEngineConfig
from lmcache.v1.metadata import LMCacheMetadata
from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
from lmcache.v1.memory_management import MemoryFormat
from lmcache.utils import CacheEngineKey
import lmcache_daos.connector as C
from lmcache_daos.connector import DaosConnector

POOL = os.environ["DAOS_TEST_POOL"]; CONT = os.environ["DAOS_TEST_CONT"]
N = int(os.environ.get("PROF_N", "24"))
cfg = LMCacheEngineConfig.from_defaults()
cfg.remote_url = f"plugin://daos/{POOL}/{CONT}"
meta = LMCacheMetadata(model_name="prof", world_size=1, local_world_size=1,
                       worker_id=0, local_worker_id=0, kv_dtype=torch.bfloat16,
                       kv_shape=(28, 2, 256, 8, 128))
lcb = LocalCPUBackend(cfg, meta)
loop = asyncio.new_event_loop()

shape = torch.Size([2, 28, 256, 1024]); dt = torch.bfloat16
chunk_bytes = 2 * 28 * 256 * 1024 * 2
gb = N * chunk_bytes / 1e9

def make_conn(workers):
    conn = DaosConnector(loop=loop, local_cpu_backend=lcb, config=cfg)
    import concurrent.futures as cf
    conn._pool.shutdown(wait=True)
    conn._pool = cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="prof")
    return conn

conn = make_conn(16)
# store N distinct chunks
keys = []
for i in range(N):
    mo = lcb.allocate(shape, dt, MemoryFormat.KV_2LTD)
    v = mo.byte_array
    mv = v if isinstance(v, memoryview) else memoryview(v)
    mv = mv.cast("B"); mv[0] = i & 0xFF; mv[chunk_bytes - 1] = (i * 3) & 0xFF
    k = CacheEngineKey("prof", 1, 0, 100000 + i, dt)
    loop.run_until_complete(conn.put(k, mo)); keys.append(k)
print(f"stored {N} x {chunk_bytes//(1<<20)}MB = {gb:.2f} GB", flush=True)

for w in [1, 4, 8, 16]:
    conn = make_conn(w)
    best = 9e9
    for _ in range(3):
        t = time.time()
        objs = loop.run_until_complete(conn.batched_get(keys))
        el = time.time() - t; best = min(best, el)
        ok = sum(1 for o in objs if o is not None)
    print(f"workers={w:2d}: batched_get {gb:.2f}GB in {best*1000:6.0f}ms = {gb/best:5.2f} GB/s (ok={ok}/{N})", flush=True)
