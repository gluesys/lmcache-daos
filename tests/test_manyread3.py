# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import os, sys, ctypes
sys.path.insert(0, "/lmd")
from lmcache_daos.dfs_binding import DfsSys
POOL=os.environ["DAOS_TEST_POOL"]; CONT=os.environ["DAOS_TEST_CONT"]
SZ=29360128; N=16
d=DfsSys(pool=POOL, cont=CONT)
def fill(i): return (i*13+7)&0xFF
for i in range(N):
    d.write(f"/mr_{i}", bytes([fill(i)])*SZ)
print(f"wrote {N}; fills={[fill(i) for i in range(N)]}", flush=True)
for i in range(N):
    obj=d.open_rdonly(f"/mr_{i}"); got=d.read_obj(obj,0,SZ); d.close_obj(obj)
    exp=fill(i)
    # sample bytes at start/mid/end
    b0,bm,be=got[0],got[SZ//2],got[SZ-1]
    # count how many bytes match expected; find dominant byte
    if b0==exp and bm==exp and be==exp:
        print(f"#{i:2d} exp={exp:3d} OK", flush=True)
    else:
        # which blob does the dominant byte correspond to?
        who=[j for j in range(N) if fill(j)==b0]
        print(f"#{i:2d} exp={exp:3d} got[0]={b0:3d} mid={bm:3d} end={be:3d} -> looks like blob {who}", flush=True)
for i in range(N): d.remove(f"/mr_{i}")
d.close()
