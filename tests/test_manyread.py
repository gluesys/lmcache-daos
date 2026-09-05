import os, sys, hashlib
sys.path.insert(0, "/lmd")
from lmcache_daos.dfs_binding import DfsSys
POOL=os.environ["DAOS_TEST_POOL"]; CONT=os.environ["DAOS_TEST_CONT"]
SZ=29360128; N=30
d=DfsSys(pool=POOL, cont=CONT)
# write N distinct blobs
digests=[]
for i in range(N):
    data=bytes([(i*13+7)&0xFF])*SZ  # distinct fill per blob
    d.write(f"/mr_{i}", data); digests.append(hashlib.md5(data).hexdigest())
print(f"wrote {N} x {SZ//(1<<20)}MB", flush=True)
# read each back sequentially, verify
bad=0
for i in range(N):
    obj=d.open_rdonly(f"/mr_{i}")
    got=d.read_obj(obj, 0, SZ)
    d.close_obj(obj)
    ok = (len(got)==SZ and hashlib.md5(got).hexdigest()==digests[i])
    if not ok:
        bad+=1; print(f"  CORRUPT at read #{i}: len={len(got)} match={hashlib.md5(got).hexdigest()==digests[i] if len(got)==SZ else 'shortlen'}", flush=True)
print(f"DONE reads: {N-bad}/{N} OK, {bad} corrupt", flush=True)
for i in range(N): d.remove(f"/mr_{i}")
d.close()
