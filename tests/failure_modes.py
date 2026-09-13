#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""What LMCache actually sees when DAOS goes wrong.

Every number in this repo so far was measured on a healthy pool. That says
nothing about the case that decides whether this backend is shippable: a
serving process does not get to stop when storage misbehaves, so the only
question that matters is whether a fault arrives as a clean error, as a hang,
or as wrong bytes.

Three outcomes, in descending order of how much they matter:

  WRONG    a get() returned data that is not what put() wrote. Unacceptable at
           any rate -- vLLM will serve it. The v1 object format carries no
           payload checksum, so nothing below this harness would notice.
  HANG     the call never came back inside the budget. Nearly as bad: the
           request thread is gone, and because the blocking DAOS call sits in
           a run_in_executor() thread, asyncio cancellation does not free it.
           A pool of DAOS_WORKERS threads can be consumed one hang at a time.
  raise    a clean failure. Fine, provided LMCache treats it as a miss.

`miss` (get returns None) is the correct answer for an object that was never
completely written -- tests/test_torn_object.py covers the truncation shapes
directly; here it is checked as an outcome of a real crash rather than of a
constructed short file.

One scenario per invocation, because a fault that wedges the executor pool
would poison every scenario after it in the same process. tests/failure_modes.sh
is the driver that injects the faults and collects the matrix.

    failure_modes.py <pool> <cont> --scenario NAME [--keys N] [--mib M]

Scenarios that need no privilege (the driver has already broken something, or
nothing is broken):

    baseline      healthy put/get roundtrip -- proves the harness itself
    put           put() against whatever state the driver left
    get           get() of keys written before the fault
    recover       both, on a connector BUILT BEFORE the fault, after repair

Scenarios that break things themselves, because they need the fault to land at
a specific instant:

    kill-mid-put  SIGKILL daos_server partway through a batched_put
    agent-cycle   kill daos_agent under a live connector, restart it, reuse it
    cont-destroy  destroy the container under an open handle
    enospc        fill the pool, then check that reads still work

Exit code is the number of findings that are WRONG or HANG. A clean raise is
not a failure of the backend; it is the backend working.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The budget after which a call is declared hung. Deliberately much larger than
# any healthy latency here (a 4 MiB put lands in single-digit ms) so that
# "HANG" means hung, not "slow because the pool is busy".
BUDGET = float(os.environ.get("FM_BUDGET", "30"))

findings: list[tuple[str, str, str]] = []   # (severity, what, detail)


def note(sev: str, what: str, detail: str = "") -> None:
    findings.append((sev, what, detail))
    print("  %-7s %-44s %s" % (sev, what, detail), flush=True)


def timed(loop, coro, budget=BUDGET):
    """Run one connector call. Returns (outcome, value_or_exc, seconds).

    outcome: "ok" | "raise" | "hang"

    wait_for() only abandons the await; the DAOS call keeps running in its
    executor thread. That is precisely the damage a hang does in production, so
    it is reported rather than papered over.
    """
    t0 = time.perf_counter()
    try:
        return ("ok", loop.run_until_complete(asyncio.wait_for(coro, budget)),
                time.perf_counter() - t0)
    except asyncio.TimeoutError:
        return ("hang", None, time.perf_counter() - t0)
    except BaseException as e:      # noqa: BLE001 - classifying, not handling
        return ("raise", e, time.perf_counter() - t0)


def describe(exc: BaseException) -> str:
    rc = getattr(exc, "rc", None)
    return "%s(%s)%s" % (type(exc).__name__, str(exc)[:70],
                         "" if rc is None else " rc=%s" % rc)


# ---- fixtures ------------------------------------------------------------
def build(pool, cont, mib):
    """A real LocalCPUBackend and a real loop, as LMCache's adapter builds them.

    A stub would not notice a change in what the connector reads out of the
    backend, and the staging pool has to be large enough for the objects below
    or allocate() returns None and the scenario silently tests nothing.
    """
    import torch
    from lmcache.v1.config import LMCacheEngineConfig
    from lmcache.v1.metadata import LMCacheMetadata
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.v1.storage_backend.local_cpu_backend import LocalCPUBackend
    from lmcache_daos.connector import DaosConnector

    cfg = LMCacheEngineConfig.from_defaults()
    cfg.remote_url = "plugin://daos/%s/%s" % (pool, cont)
    cfg.local_cpu = False
    meta = LMCacheMetadata(model_name="fm-test", world_size=1, local_world_size=1,
                           worker_id=0, local_worker_id=0, kv_dtype=torch.bfloat16,
                           kv_shape=(2, 256, 8, 1, 8))
    lcb = LocalCPUBackend(cfg, meta)
    loop = asyncio.new_event_loop()
    conn = DaosConnector("daos://%s/%s" % (pool, cont), loop, lcb)
    return conn, loop, lcb, torch, MemoryFormat


def mkobj(lcb, torch, MemoryFormat, mib, seed):
    """A MemoryObj filled with a seed-dependent pattern.

    Seed-dependent matters: with one shared pattern, a get() that returned some
    OTHER key's object would compare equal and the WRONG case -- the only one
    that is unacceptable -- would be invisible. That is the exact confusion the
    KV corruption hunt burned weeks on.
    """
    n = int(mib * (1 << 20)) // 2            # bf16 elements
    mo = lcb.allocate(torch.Size([2, n // 2]), torch.bfloat16, MemoryFormat.KV_2LTD)
    if mo is None:
        raise RuntimeError("LocalCPUBackend.allocate returned None -- staging pool too small")
    v = mo.byte_array
    if isinstance(v, memoryview):
        v = v.cast("B")
    pat = bytes(((i * 7 + seed * 31 + 3) & 0xFF) for i in range(256))
    nb = len(v)
    v[:nb] = (pat * (nb // 256 + 1))[:nb]
    return mo, pat[:64], nb                # 64-byte witness is enough to identify


def witness(seed):
    """The first 64 bytes mkobj() writes for this seed -- computed, not read.

    Readback-only scenarios need the expected bytes but not the object, and
    allocating one MemoryObj per key just to look at its pattern would hold a
    staging buffer per key for no reason.
    """
    return bytes(((i * 7 + seed * 31 + 3) & 0xFF) for i in range(64))


def keyfor(i):
    from lmcache.utils import CacheEngineKey
    import torch
    return CacheEngineKey("fm-test", 1, 0, 0xFA11_0000 + i, torch.bfloat16)


def verify(loop, conn, witnesses, label, budget=BUDGET, max_hang=3):
    """Read every key back and classify. This is the part that earns the file.

    Stops after max_hang hangs. Without that, a readback of 64 keys against a
    dead server costs 64 x budget -- half an hour of learning the same fact
    sixty-four times, and long enough that the driver's own timeout fires and
    the run never reaches its restore step.
    """
    exact = miss = wrong = hung = err = 0
    for i, (w, nb) in enumerate(witnesses):
        out, val, _ = timed(loop, conn.get(keyfor(i)), budget)
        if out == "hang":
            hung += 1
            if hung >= max_hang:
                note("HANG", "%s: stopped after %d hangs" % (label, hung),
                     "%d/%d keys probed" % (i + 1, len(witnesses)))
                return exact, miss, wrong
        elif out == "raise":
            err += 1
        elif val is None:
            miss += 1
        else:
            g = val.byte_array
            if isinstance(g, memoryview):
                g = g.cast("B")
            if bytes(g[:64]) == w:
                exact += 1
            else:
                wrong += 1
            try:
                val.ref_count_down()
            except Exception:
                pass
    detail = "exact=%d miss=%d wrong=%d hang=%d raise=%d" % (exact, miss, wrong, hung, err)
    if wrong:
        note("WRONG", "%s: readback returned data that was never written" % label, detail)
    elif hung:
        note("HANG", "%s: readback never returned" % label, detail)
    else:
        note("ok", "%s: no silent corruption" % label, detail)
    return exact, miss, wrong


# ---- scenarios -----------------------------------------------------------
def sc_baseline(conn, loop, mk, args):
    """Proves the harness can tell right from wrong before anything is broken."""
    ws = []
    for i in range(args.keys):
        mo, w, nb = mk(i)
        out, val, dt = timed(loop, conn.put(keyfor(i), mo))
        if out != "ok":
            note("HANG" if out == "hang" else "raise",
                 "healthy put #%d did not succeed" % i,
                 "%.2fs %s" % (dt, "" if val is None else describe(val)))
            return
        ws.append((w, nb))
    note("ok", "%d healthy puts landed" % args.keys, "")
    exact, _, _ = verify(loop, conn, ws, "healthy readback")
    if exact != args.keys:
        note("WRONG", "healthy roundtrip is not byte-exact",
             "%d/%d exact" % (exact, args.keys))


def sc_put(conn, loop, mk, args):
    """put() into a broken pool. Wanted: a raise, promptly."""
    mo, w, nb = mk(0)
    out, val, dt = timed(loop, conn.put(keyfor(0), mo))
    if out == "hang":
        note("HANG", "put() into a broken pool never returned", "%.1fs budget" % dt)
    elif out == "raise":
        note("ok", "put() into a broken pool raised", "%.2fs %s" % (dt, describe(val)))
    else:
        # Not automatically wrong -- DFS may buffer -- but it means the caller
        # was told the store succeeded, so the data had better be there later.
        note("SOFT", "put() into a broken pool reported success", "%.2fs" % dt)


def sc_get(conn, loop, mk, args):
    """get() of keys written before the fault."""
    ws = [(witness(i), 0) for i in range(args.keys)]
    verify(loop, conn, ws, "readback during fault")


def sc_recover(conn, loop, mk, args):
    """The connector that lived through the outage -- is it still usable?

    This is the scenario with teeth. A connector that raises forever after one
    transport error is a process-lifetime outage from one blip, and nothing in
    the health ping would fix it: ping() would report unhealthy correctly and
    the backend would still never come back.
    """
    w = witness(90)
    mo, _, nb = mk(90)
    out, val, dt = timed(loop, conn.put(keyfor(90), mo))
    if out != "ok":
        note("HANG" if out == "hang" else "STUCK",
             "put() on a pre-outage connector after repair failed",
             "%.2fs %s" % (dt, "" if val is None else describe(val)))
        return
    out, val, dt = timed(loop, conn.get(keyfor(90)))
    if out != "ok" or val is None:
        note("STUCK", "get() on a pre-outage connector after repair failed",
             "%.2fs %s" % (dt, "" if not isinstance(val, BaseException) else describe(val)))
        return
    g = val.byte_array
    if isinstance(g, memoryview):
        g = g.cast("B")
    if bytes(g[:64]) == w:
        note("ok", "pre-outage connector works again after repair", "%.2fs" % dt)
    else:
        note("WRONG", "pre-outage connector returned wrong bytes after repair", "")
    try:
        val.ref_count_down()
    except Exception:
        pass


def sc_kill_mid_put(conn, loop, mk, args):
    """SIGKILL the server partway through a batched_put.

    The write order in _put_sync is header-then-payload, so a crash between the
    two leaves an object whose 8-byte prefix promises a payload that is not
    there. Torn objects are supposed to read back as a miss; this checks that
    against a real crash rather than a file truncated on purpose, and it also
    checks the half that truncation tests cannot reach -- whether any object
    comes back COMPLETE and wrong.
    """
    objs, ws = [], []
    for i in range(args.keys):
        mo, w, nb = mk(i)
        objs.append(mo)
        ws.append((w, nb))
    keys = [keyfor(i) for i in range(args.keys)]

    fired = threading.Event()

    def killer():
        time.sleep(args.kill_after)
        subprocess.run(["pkill", "-9", "-x", "daos_server"], check=False)
        fired.set()

    threading.Thread(target=killer, daemon=True).start()
    out, val, dt = timed(loop, conn.batched_put(keys, objs), args.put_budget)
    if not fired.is_set():
        # The write finished before the kill, so nothing was interrupted and the
        # scenario proved nothing. Say so instead of reporting a pass: size the
        # payload up (--keys/--mib) or drop --kill-after.
        note("SOFT", "the write outran the kill -- scenario vacuous",
             "%.2fs write vs %.2fs kill" % (dt, args.kill_after))
        return
    note("ok" if out == "raise" else ("HANG" if out == "hang" else "SOFT"),
         "batched_put interrupted by a server SIGKILL -> %s" % out,
         "%.1fs (budget %.0fs) %s" % (dt, args.put_budget,
                                      "" if not isinstance(val, BaseException) else describe(val)))

    # Whatever the call said, reading back must not produce bytes that were
    # never written. Everything will fail here -- the server is dead -- and that
    # is the point: the check is for WRONG, not for success.
    verify(loop, conn, ws, "readback with the server dead", budget=20, max_hang=2)

    # What CANNOT be answered on this host: whether the objects that were
    # in flight survived on disk as torn (-> miss, correct) or as complete and
    # wrong. SCM is a ramdisk, so restoring the server means reformatting and
    # the evidence is destroyed with it. That needs MD-on-SSD.
    note("gap", "survivor inspection needs a pool that outlives a restart", "")


def _sh(*cmd):
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def sc_agent_cycle(conn, loop, mk, args):
    """Kill daos_agent under a live connector, bring it back, keep using it.

    The transient fault that a real deployment hits most often, and the only
    one this single-node host can inject and then genuinely undo: the server
    never stops, so the pool and every handle on it survive, and "does the
    connector recover" has an unambiguous answer. (Stopping the server here
    cannot answer it -- SCM is a ramdisk, so the pool that comes back is a
    different pool, and a connector that failed forever would be
    indistinguishable from one correctly rejecting a stale handle.)

    A DAOS client talks to the agent over a unix socket at startup to resolve
    the system and get its credentials, then goes straight to the engine over
    the fabric. So the interesting question is not whether I/O stops -- it may
    well not -- but whether anything silently degrades while the agent is gone.
    """
    ws = []
    for i in range(args.keys):
        mo, w, nb = mk(i)
        out, val, dt = timed(loop, conn.put(keyfor(i), mo))
        if out != "ok":
            note("STUCK", "pre-fault put #%d failed" % i, describe(val) if val else "")
            return
        ws.append((w, nb))
    note("ok", "%d puts before the fault" % args.keys, "")

    r = _sh("pkill", "-9", "-x", "daos_agent")
    time.sleep(2)
    alive = _sh("pgrep", "-x", "daos_agent").stdout.strip()
    if alive:
        note("SOFT", "daos_agent did not die -- scenario is vacuous", alive[:40])
        return
    note("ok", "daos_agent killed", "")

    # I/O on handles that were already open. Whatever happens, it must not be
    # wrong bytes.
    verify(loop, conn, ws, "readback with the agent gone")
    mo, w, nb = mk(50)
    out, val, dt = timed(loop, conn.put(keyfor(50), mo))
    note({"ok": "ok", "raise": "ok", "hang": "HANG"}[out],
         "put() with the agent gone -> %s" % out,
         "%.2fs %s" % (dt, describe(val) if isinstance(val, BaseException) else ""))
    wrote_during = (out == "ok")

    # Same invocation the host's restore script uses -- setsid so it survives
    # this process, and the explicit -o because a bare restart would silently
    # pick up a different config than the one the fault was injected against.
    # Checked here, before the restart is attempted: if that put really landed
    # it is readable now, and a restart that fails must not be able to hide
    # whether a store reported OK was a store that happened.
    if wrote_during:
        out, val, dt = timed(loop, conn.get(keyfor(50)))
        ok = out == "ok" and val is not None and bytes(
            (val.byte_array.cast("B") if isinstance(val.byte_array, memoryview)
             else val.byte_array)[:64]) == w
        note("ok" if ok else "WRONG",
             "the put that reported OK with no agent is readable and correct",
             "" if ok else "outcome=%s" % out)

    # systemd owns this directory -- the unit declares RuntimeDirectory=daos_agent
    # -- and after a SIGKILL it is gone, so a hand-rolled restart binds its unix
    # socket into a directory that does not exist and dies with ENOENT. Recreate
    # it, and prefer the unit, which does this itself.
    _sh("mkdir", "-p", "/var/run/daos_agent")
    _sh("systemctl", "start", "daos_agent")
    time.sleep(4)
    if not _sh("pgrep", "-x", "daos_agent").stdout.strip():
        _sh("sh", "-c", "setsid /usr/bin/daos_agent -o /etc/daos/daos_agent.yml"
                        " </dev/null >/var/log/daos_agent_fm.log 2>&1 &")
        time.sleep(6)
    if not _sh("pgrep", "-x", "daos_agent").stdout.strip():
        note("STUCK", "could not restart daos_agent -- recovery untested", "")
        return
    note("ok", "daos_agent back up", "")

    verify(loop, conn, ws, "readback after the agent returned")
    sc_recover(conn, loop, mk, args)


def sc_cont_destroy(conn, loop, mk, args):
    """Destroy the container under a connector that is holding it open.

    The operator-error case: someone reclaims space on the wrong container. The
    bar is the same as everywhere else -- fail, do not serve wrong bytes.
    """
    ws = []
    for i in range(args.keys):
        mo, w, nb = mk(i)
        out, val, dt = timed(loop, conn.put(keyfor(i), mo))
        if out != "ok":
            note("STUCK", "pre-fault put #%d failed" % i, describe(val) if val else "")
            return
        ws.append((w, nb))
    note("ok", "%d puts before the fault" % args.keys, "")

    r = _sh("daos", "cont", "destroy", "--force", args.pool, args.cont)
    if r.returncode != 0:
        note("SOFT", "container destroy did not run", (r.stderr or r.stdout).strip()[:70])
        return
    note("ok", "container destroyed underneath the open handle", "")

    verify(loop, conn, ws, "readback after the container was destroyed")
    mo, w, nb = mk(60)
    out, val, dt = timed(loop, conn.put(keyfor(60), mo))
    note({"ok": "SOFT", "raise": "ok", "hang": "HANG"}[out],
         "put() into a destroyed container -> %s" % out,
         "%.2fs %s" % (dt, describe(val) if isinstance(val, BaseException) else ""))


def sc_enospc(conn, loop, mk, args):
    """Write until the pool is full, then keep using the connector.

    The one failure here that arrives on its own schedule: nothing is broken,
    nobody did anything wrong, the cache simply did its job until there was no
    room. LMCache has no idea how big the pool is and will go on calling put()
    forever, so what matters is not that the write fails -- it must -- but that
    the failure is clean, cheap, and does not take the READ path down with it.
    A backend that stops serving hits once it stops accepting writes has turned
    a full cache into an outage.
    """
    ws, i, t0 = [], 0, time.perf_counter()
    while i < args.max_keys and time.perf_counter() - t0 < args.fill_budget:
        mo, w, nb = mk(i)
        out, val, dt = timed(loop, conn.put(keyfor(i), mo))
        if out == "hang":
            note("HANG", "put #%d never returned while filling" % i, "%.1fs" % dt)
            return
        if out == "raise":
            note("ok", "put failed after %d objects (%.1f GiB)" % (i, i * args.mib / 1024.0),
                 "%.2fs %s" % (dt, describe(val)))
            break
        ws.append((w, nb))
        i += 1
    else:
        note("SOFT", "the pool never filled -- scenario vacuous",
             "%d objects, %.1f GiB, %.0fs" % (i, i * args.mib / 1024.0,
                                              time.perf_counter() - t0))
        return

    # The point of the scenario. Reads of what fitted must still work.
    verify(loop, conn, ws[:8], "readback on a full pool")

    # And the failure must be repeatable rather than a one-off that leaves the
    # handle wedged: a second put has to fail the same cheap way.
    mo, w, nb = mk(args.max_keys + 1)
    out, val, dt = timed(loop, conn.put(keyfor(args.max_keys + 1), mo))
    note({"raise": "ok", "hang": "HANG", "ok": "SOFT"}[out],
         "a second put on a full pool -> %s" % out,
         "%.2fs %s" % (dt, describe(val) if isinstance(val, BaseException) else ""))


SCENARIOS = {"baseline": sc_baseline, "put": sc_put, "get": sc_get,
             "recover": sc_recover, "kill-mid-put": sc_kill_mid_put,
             "agent-cycle": sc_agent_cycle, "cont-destroy": sc_cont_destroy,
             "enospc": sc_enospc}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pool")
    ap.add_argument("cont")
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--keys", type=int, default=8)
    ap.add_argument("--mib", type=float, default=4.0)
    ap.add_argument("--kill-after", type=float, default=0.05)
    ap.add_argument("--max-keys", type=int, default=4000)
    ap.add_argument("--fill-budget", type=float, default=300.0)
    ap.add_argument("--put-budget", type=float, default=BUDGET,
                    help="budget for the interrupted write alone; make it large "
                         "enough to measure when DAOS actually gives up")
    args = ap.parse_args()

    print("== %s ==  pool=%s cont=%s keys=%d x %.1f MiB  budget=%.0fs"
          % (args.scenario, args.pool, args.cont, args.keys, args.mib, BUDGET), flush=True)

    # Building the connector is itself a DAOS operation and can be the thing
    # that fails; that is a legitimate outcome to report, not a crash.
    t0 = time.perf_counter()
    try:
        conn, loop, lcb, torch, MemoryFormat = build(args.pool, args.cont, args.mib)
    except BaseException as e:   # noqa: BLE001
        note("ok" if args.scenario in ("put", "get") else "STUCK",
             "connector construction failed", "%.2fs %s" % (time.perf_counter() - t0, describe(e)))
        return _exit()

    def mk(seed):
        return mkobj(lcb, torch, MemoryFormat, args.mib, seed)

    try:
        SCENARIOS[args.scenario](conn, loop, mk, args)
    finally:
        # close() itself can hang on a dead transport. Bound it, and say so --
        # a process that cannot exit is its own failure mode.
        out, _, dt = timed(loop, conn.close(), budget=10)
        if out == "hang":
            note("HANG", "close() did not return", "%.1fs" % dt)
    return _exit()


def _exit():
    bad = sum(1 for s, _, _ in findings if s in ("WRONG", "HANG", "STUCK"))
    # SOFT is not a backend defect, it is this harness failing to inject the
    # fault it claims to inject. Counted separately and printed either way,
    # because a vacuous scenario that prints no findings reads exactly like a
    # pass, and that is how a fault matrix ends up proving nothing.
    soft = sum(1 for s, _, _ in findings if s in ("SOFT", "gap"))
    print("  -> %d finding%s%s" % (bad, "" if bad == 1 else "s",
                                   "" if not soft else ", %d not proven" % soft), flush=True)
    return bad


if __name__ == "__main__":
    # os._exit, not sys.exit: concurrent.futures joins every worker thread at
    # interpreter shutdown (verified -- a process whose only stuck worker was a
    # 30 s sleep lived the full 30 s even after shutdown(wait=False)), so a
    # scenario that deliberately wedges the pool would never return to the
    # driver and the run would never reach its restore step. Skipping cleanup
    # is correct here and only here: this process exists to be wrecked.
    sys.stdout.flush()
    os._exit(main())
