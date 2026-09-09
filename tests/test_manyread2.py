# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import os, sys, ctypes, hashlib
sys.path.insert(0, "/lmd")
from lmcache_daos.dfs_binding import DfsSys
POOL=os.environ["DAOS_TEST_POOL"]; CONT=os.environ["DAOS_TEST_CONT"]
SZ=29360128; N=30
d=DfsSys(pool=POOL, cont=CONT)
digests=[]
for i in range(N):
    data=bytes([(i*13+7)&0xFF])*SZ
    d.write(f"/mr_{i}", data); digests.append(hashlib.md5(data).hexdigest())
print(f"wrote {N}", flush=True)
# ONE persistent buffer reused for every read (fixed address)
buf=ctypes.create_string_buffer(SZ)
bad=0
for i in range(N):
    obj=d.open_rdonly(f"/mr_{i}")
    n=d.read_obj_into(obj, 0, SZ, buf)
    d.close_obj(obj)
    md=hashlib.md5(buf.raw[:SZ]).hexdigest()
    if not (n==SZ and md==digests[i]):
        bad+=1
        if bad<=3: print(f"  CORRUPT #{i}: n={n} match={md==digests[i]}", flush=True)
print(f"DONE persistent-buf: {N-bad}/{N} OK", flush=True)
for i in range(N): d.remove(f"/mr_{i}")
d.close()
