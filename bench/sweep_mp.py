# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
"""Part-A style context sweep for MP mode, with a cold-L1 (DAOS) leg.

sweep2.py measures miss -> hit in one process. In MP mode that hit is served
from the cache server's L1, so this variant splits the sweep in two runs that
share a deterministic token sequence (tokenize is deterministic for a fixed
tag, and the tag has no timestamp):

    ARM=mp CTXS=8192,16384,31744 TAG=sweepA MODE=store python3 sweep_mp.py
      -> CSV,store,<arm>,<ctx>,<kvGB>,<miss ms>,<warm hit ms>,<x>
    <restart the MP server: L1 empty, DAOS keeps the objects>
    ARM=mp CTXS=8192,16384,31744 TAG=sweepA MODE=hit   python3 sweep_mp.py
      -> CSV,hit,<arm>,<ctx>,<kvGB>,<cold hit ms>,<warm hit ms>,

Same fixed-token method and KV-proportional settle as sweep2.py, so the
numbers line up with the Hub document's Part A table.
"""

import json
import os
import time
import urllib.request

ARM = os.environ.get("ARM", "mp")
CTXS = [int(x) for x in os.environ.get("CTXS", "8192,16384,31744").split(",")]
TAG = os.environ.get("TAG", "sweepA")
MODE = os.environ.get("MODE", "store")
URL = os.environ.get("URL", "http://localhost:8001")
KVPT = 160 * 1024


def post(p, o, t=1800):
    b = json.dumps(o).encode()
    r = urllib.request.Request(URL + p, data=b, headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r, timeout=t))


def ttft(ids):
    b = json.dumps({"model": "qwen3", "prompt": ids, "max_tokens": 1,
                    "temperature": 0, "stream": True}).encode()
    r = urllib.request.Request(URL + "/v1/completions", data=b,
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    resp = urllib.request.urlopen(r, timeout=1800)
    for raw in resp:
        s = raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip() != "[DONE]":
            try:
                o = json.loads(s[5:].strip())
            except Exception:
                continue
            if o.get("choices", [{}])[0].get("text", ""):
                return time.time() - t0
    return time.time() - t0


def make_ids(target, tag):
    ids = []
    k = 0
    while len(ids) < target:
        txt = f"{tag} seg{k} " + " ".join(f"w{k}_{i}z{i % 89}" for i in range(4000))
        ids += post("/tokenize", {"model": "qwen3", "prompt": txt})["tokens"]
        k += 1
    return ids[:target]


for ctx in CTXS:
    ids = make_ids(ctx, f"{TAG}_{ctx}")
    assert len(ids) == ctx
    kv = ctx * KVPT / 1e9
    if MODE == "store":
        miss = ttft(ids)
        time.sleep(max(10, int(kv * 1.5)))
        warm = ttft(ids)
        print(f"CSV,store,{ARM},{ctx},{kv:.2f},{miss*1000:.0f},{warm*1000:.0f},{miss/warm:.2f}", flush=True)
    else:
        cold = ttft(ids)
        time.sleep(3)
        warm = ttft(ids)
        print(f"CSV,hit,{ARM},{ctx},{kv:.2f},{cold*1000:.0f},{warm*1000:.0f},", flush=True)
