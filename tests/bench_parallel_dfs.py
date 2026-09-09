# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import os, sys, time, threading, ctypes
sys.path.insert(0, "/lmd")
from lmcache_daos.dfs_binding import DfsSys
POOL=os.environ["DAOS_TEST_POOL"]; CONT=os.environ["DAOS_TEST_CONT"]
BLOB=64<<20   # 64 MiB per blob
NBLOB=16      # 1 GiB working set
shared=DfsSys(pool=POOL, cont=CONT)
data=os.urandom(BLOB)
paths=[f"/pbench_{i}" for i in range(NBLOB)]
for p in paths: shared.write(p, data)
def read_shared(p):
    obj=shared.open_rdonly(p)
    try:
        dst=ctypes.create_string_buffer(BLOB)
        shared.read_obj_into(obj, 0, BLOB, dst)
    finally: shared.close_obj(obj)
# per-thread handle variant
tls=threading.local()
def read_ownhandle(p):
    h=getattr(tls,"h",None)
    if h is None: h=tls.h=DfsSys(pool=POOL, cont=CONT)
    obj=h.open_rdonly(p)
    try:
        dst=ctypes.create_string_buffer(BLOB)
        h.read_obj_into(obj,0,BLOB,dst)
    finally: h.close_obj(obj)
def run(fn, conc):
    import concurrent.futures as cf
    work=paths*2  # 32 reads
    t=time.time()
    with cf.ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(fn, work))
    dt=time.time()-t
    gb=len(work)*BLOB/1e9
    print(f"{'shared' if fn is read_shared else 'ownhdl'} conc={conc:2d}: {gb:.2f} GB in {dt*1000:6.0f} ms = {gb/dt:5.2f} GB/s", flush=True)
for c in [1,2,4,8,16]: run(read_shared, c)
for c in [1,2,4,8,16]: run(read_ownhandle, c)
