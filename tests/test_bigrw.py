# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import os, sys
sys.path.insert(0, "/lmd")
from lmcache_daos.dfs_binding import DfsSys
POOL=os.environ["DAOS_TEST_POOL"]; CONT=os.environ["DAOS_TEST_CONT"]
d=DfsSys(pool=POOL, cont=CONT)
for sz in [1<<20, 8<<20, 29360128]:
    data=os.urandom(sz)
    p=f"/bigtest_{sz}"
    d.write(p, data)
    obj=d.open_rdonly(p)
    hdr=d.read_obj(obj, 0, 520)
    full=d.read_obj(obj, 0, sz)
    d.close_obj(obj)
    print(f"sz={sz}: wrote={sz} hdr_read={len(hdr)} full_read={len(full)} match={full==data}", flush=True)
    d.remove(p)
d.close()
