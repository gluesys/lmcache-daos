#!/usr/bin/env python3
"""Expose LMCache's per-phase timers, which it measures but does not print.

LMCache 0.5.2 reports store as a single ``offload_time`` that is actually
``process_tokens_time + from_gpu_time``, and reports retrieve as one ``cost``
with no breakdown at all -- even though RetrieveRequestStats already carries
process_tokens_time, to_gpu_time and broadcast_time. There is a detailed
slow-retrieve log in observability.py, but it is gated behind
``retrieve_time_threshold`` which is initialised to 1e9 and never set from
config, so it is dead code.

Why patch files instead of profiling: py-spy cannot sample this process. vLLM
runs enough threads that ``py-spy record`` fell 128 s behind during a 5 s
window, and the offload being measured is only ~93 ms long. LMCache's own
timers are both cheaper and authoritative.

Usage -- run on the host, then bind-mount the outputs over the originals so
the patch survives container recreation (edits made inside a container are
lost when the launcher does ``podman rm``):

    python3 patch_lmcache_timers.py <src_dir> <out_dir>

      src_dir   directory holding pristine cache_engine.py and observability.py
                (copy them out of the image first)
      out_dir   where the patched copies are written

    podman run ... \\
      -v <out_dir>/cache_engine.py:<site>/lmcache/v1/cache_engine.py:ro \\
      -v <out_dir>/observability.py:<site>/lmcache/observability.py:ro

Every replacement is checked and the script exits non-zero if any anchor is
missing, because a half-patched file still imports and simply reports nothing
-- which would look like a measurement result rather than a failure.
"""
import pathlib
import sys

# (file, anchor, replacement, description)
PATCHES = [
    # ---- store: split offload into its two real components ----------------
    (
        "cache_engine.py",
        '            "offload_time: %.4f ms, put_time: %.4f ms",',
        '            "offload_time: %.4f ms '
        '(process_tokens %.4f + from_gpu %.4f), put_time: %.4f ms",',
        "store log format",
    ),
    (
        "cache_engine.py",
        "            (store_stats.process_tokens_time + store_stats.from_gpu_time) * 1000,\n"
        "            store_stats.put_time * 1000,",
        "            (store_stats.process_tokens_time + store_stats.from_gpu_time) * 1000,\n"
        "            store_stats.process_tokens_time * 1000,\n"
        "            store_stats.from_gpu_time * 1000,\n"
        "            store_stats.put_time * 1000,",
        "store log args",
    ),
    # ---- retrieve: it prints no breakdown at all --------------------------
    (
        "cache_engine.py",
        '                "cost %.4f ms, throughput: %.4f GB/s;",',
        '                "cost %.4f ms, throughput: %.4f GB/s; '
        'process_tokens %.4f ms, to_gpu %.4f ms, broadcast %.4f ms, '
        'detail %s",',
        "retrieve log format",
    ),
    (
        "cache_engine.py",
        "                onload_time * 1000,\n"
        "                tot_kv_size / onload_time / 1024**3 if onload_time > 0 else 0,\n"
        "            )",
        "                onload_time * 1000,\n"
        "                tot_kv_size / onload_time / 1024**3 if onload_time > 0 else 0,\n"
        "                retrieve_stats.process_tokens_time * 1000,\n"
        "                retrieve_stats.to_gpu_time * 1000,\n"
        "                retrieve_stats.broadcast_time * 1000,\n"
        "                retrieve_stats.detailed_metrics,\n"
        "            )",
        "retrieve log args",
    ),
    # ---- un-gate the detailed slow-retrieve log --------------------------
    (
        "observability.py",
        "        self.retrieve_time_threshold: float = 1e9",
        "        self.retrieve_time_threshold: float = 0.0",
        "retrieve_time_threshold gate",
    ),
]


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src = pathlib.Path(sys.argv[1])
    out = pathlib.Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)

    bodies: dict[str, str] = {}
    for name in {p[0] for p in PATCHES}:
        f = src / name
        if not f.is_file():
            print(f"missing: {f}", file=sys.stderr)
            return 1
        bodies[name] = f.read_text()

    failed = 0
    for name, anchor, repl, desc in PATCHES:
        body = bodies[name]
        n = body.count(anchor)
        if n != 1:
            print(f"FAIL {name}: {desc}: anchor found {n} times, need 1",
                  file=sys.stderr)
            failed += 1
            continue
        bodies[name] = body.replace(anchor, repl, 1)
        print(f"  ok  {name}: {desc}")

    if failed:
        print(f"{failed} anchor(s) missing -- LMCache version differs. "
              f"Not writing anything.", file=sys.stderr)
        return 1

    for name, body in bodies.items():
        (out / name).write_text(body)
        print(f"wrote {out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
