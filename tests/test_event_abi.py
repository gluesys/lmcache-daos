"""P0 gate: prove the daos_event_t layout before any async code relies on it.

Pass criteria (all must hold):
  1. sizeof(daos_event_t) is either taken from the C shim, or our ctypes
     declaration survives a real init -> async read -> poll -> fini cycle with
     its tail canary untouched.
  2. daos_eq_poll hands back the *same* event address we submitted, so the
     address is usable as a pending-table key.
  3. A deliberately undersized allocation is DETECTED (negative control) --
     otherwise the canary proves nothing.

Run inside the serving container:
    DAOS_TEST_POOL=kvpool2 DAOS_TEST_CONT=kv2s16 python3 /lmd/tests/test_event_abi.py
"""

import ctypes
import os
import sys

sys.path.insert(0, "/lmd")

from lmcache_daos import daos_event as de
from lmcache_daos.dfs_binding import DfsSys

POOL = os.environ["DAOS_TEST_POOL"]
CONT = os.environ["DAOS_TEST_CONT"]
PATH = "/evabi_probe"
SZ = 1 << 20  # 1 MiB is plenty to make DAOS actually drive the event

fail = 0
d = DfsSys(pool=POOL, cont=CONT)
try:
    d.write(PATH, bytes(range(256)) * (SZ // 256))

    print(f"sizeof(daos_event_t) = {de.EVENT_SIZE} B "
          f"(from shim: {de.EVENT_SIZE_FROM_SHIM})", flush=True)
    if not de.EVENT_SIZE_FROM_SHIM:
        print("  note: shim absent -> relying on the canary check below. "
              "Build shim/daos_evshim.c for production.", flush=True)

    # -- 1 & 2: the real cycle -------------------------------------------
    try:
        info = de.verify_abi(d, PATH, length=SZ)
        print(f"PASS abi: {info}", flush=True)
        if info["bytes_read"] != SZ:
            fail += 1
            print(f"  FAIL short read: {info['bytes_read']} != {SZ}", flush=True)
    except de.DaosEventABIError as e:
        fail += 1
        print(f"FAIL abi: {e}", flush=True)

    # -- 3: negative control ---------------------------------------------
    # Pretend the struct is 64 B smaller than it is. DAOS must then scribble
    # into the canary and the check must notice. If this "passes" the canary is
    # not actually positioned where DAOS writes, and criterion 1 is worthless.
    real = de.EVENT_SIZE
    de.EVENT_SIZE = max(8, real - 64)
    try:
        de.verify_abi(d, PATH, length=SZ)
        fail += 1
        print(f"FAIL negative-control: undersized event ({de.EVENT_SIZE} B vs "
              f"real {real} B) was NOT detected -- the canary does not guard "
              f"the bytes DAOS writes", flush=True)
    except de.DaosEventABIError:
        print(f"PASS negative-control: undersized event ({de.EVENT_SIZE} B) "
              f"detected as expected", flush=True)
    except Exception as e:
        # A crash/DER here also means "detected", but note it: we would rather
        # fail on the canary than on DAOS internals.
        print(f"PASS(weak) negative-control: detected via {type(e).__name__}: "
              f"{e}", flush=True)
    finally:
        de.EVENT_SIZE = real

    print(f"DONE test_event_abi: {'OK' if fail == 0 else f'{fail} FAILURE(S)'}",
          flush=True)
finally:
    try:
        d.remove(PATH)
    except Exception:
        pass
    d.close()

sys.exit(1 if fail else 0)
