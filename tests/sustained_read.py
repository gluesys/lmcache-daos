import os, sys, time, ctypes, threading
import concurrent.futures as cf
sys.path.insert(0, "/root/lmcache-daos")
from lmcache_daos.dfs_binding import DfsSys
POOL=os.environ["DAOS_TEST_POOL"]; CONT=os.environ["DAOS_TEST_CONT"]
SZ=28<<20; N=32; DUR=float(os.environ.get("DUR","45")); CONC=int(os.environ.get("CONC","16"))
main=DfsSys(pool=POOL,cont=CONT)
paths=[f"/sr_{i}" for i in range(N)]; data=b"\x5a"*SZ
for p in paths: main.write(p,data)
tls=threading.local()
def _h():
    x=getattr(tls,"h",None)
    if x is None: x=tls.h=DfsSys(pool=POOL,cont=CONT)
    return x
def _b():
    x=getattr(tls,"b",None)
    if x is None: x=tls.b=ctypes.create_string_buffer(SZ)
    return x
def rd(p):
    h=_h(); b=_b(); o=h.open_rdonly(p)
    try: h.read_obj_into(o,0,SZ,b)
    finally: h.close_obj(o)
stop=time.time()+DUR; cnt=0; t0=time.time()
with cf.ThreadPoolExecutor(max_workers=CONC) as ex:
    while time.time()<stop:
        list(ex.map(rd,paths)); cnt+=N
el=time.time()-t0
print(f"sustained conc={CONC}: {cnt*SZ/1e9:.1f} GB in {el:.1f}s = {cnt*SZ/1e9/el:.2f} GB/s", flush=True)
for p in paths: main.remove(p)
