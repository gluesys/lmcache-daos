#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""TTFT of a vLLM + LMCache + DAOS server while a storage node reboots.

Warms ``--prompts`` long prompts once (each stored to DAOS), then for
``--secs`` sends them again in random order with ``max_tokens=1``, so the
request time is the TTFT. Every ``--fresh-every``-th request is a prompt never
seen before: that is the recompute baseline at that moment. One JSON line per
request on stdout:

    {"t": ..., "kind": "warm"|"repeat"|"fresh", "i": 17, "secs": 0.081, "ok": true}

The server should run without vLLM prefix caching and with a small LMCache
CPU tier, so a repeat can only be fast by reading DAOS. Power a storage node
off and on while this runs (daos-reboot-repro drives that). What passing
looks like: no failed request; repeats stay between the hit time and the
fresh time during the outage; repeats return to the hit time after.

    python3 tests/vllm_reboot_ttft.py http://192.168.35.41:8100 --secs 900
"""
import argparse
import json
import random
import sys
import time
import urllib.request

WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
         "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
         "xray yankee zulu storage cache vector tensor kernel engine rank pool").split()


def prompt(seed: int, words: int) -> str:
    r = random.Random(seed)
    return "Document %d. " % seed + " ".join(r.choice(WORDS) for _ in range(words)) \
        + "\nSummarize the document in one word:"


def ask(url: str, model: str, text: str, timeout: float):
    body = json.dumps({"model": model, "prompt": text, "max_tokens": 1,
                       "temperature": 0}).encode()
    req = urllib.request.Request(url + "/v1/completions", body,
                                 {"Content-Type": "application/json"})
    s = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, time.monotonic() - s, None
    except Exception as e:                 # a failed request is the result, not a crash
        return False, time.monotonic() - s, str(e)[:200]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--model", default="qwen")
    ap.add_argument("--prompts", type=int, default=48)
    ap.add_argument("--words", type=int, default=4000)
    ap.add_argument("--secs", type=float, default=900)
    ap.add_argument("--fresh-every", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=120)
    a = ap.parse_args()

    def emit(kind, i, ok, secs, err):
        d = {"t": round(time.time(), 2), "kind": kind, "i": i, "secs": round(secs, 4),
             "ok": ok}
        if err:
            d["err"] = err
        print(json.dumps(d), flush=True)

    for i in range(a.prompts):
        emit("warm", i, *ask(a.url, a.model, prompt(i, a.words), a.timeout))
    print(json.dumps({"event": "warmed", "t": round(time.time(), 2)}), flush=True)

    rnd = random.Random(7)
    fresh = 1_000_000
    n = 0
    end = time.monotonic() + a.secs
    while time.monotonic() < end:
        n += 1
        if n % a.fresh_every == 0:
            fresh += 1
            emit("fresh", fresh, *ask(a.url, a.model, prompt(fresh, a.words), a.timeout))
        else:
            i = rnd.randrange(a.prompts)
            emit("repeat", i, *ask(a.url, a.model, prompt(i, a.words), a.timeout))
    print(json.dumps({"event": "end", "t": round(time.time(), 2)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
