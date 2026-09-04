"""TTFT for a prompt whose KV was stored by a *previous* server lifetime.

bench_value.py measures miss -> hit inside one process; in MP mode that hit is
served from the cache server's L1 (pinned CPU), so it says nothing about the
L2 (DAOS) path. This script separates the two:

    TAG=persistA WORDS=1500 MODE=store python3 bench_persist.py   # miss + hit (L1)
    <restart the MP server / container: L1 is empty, DAOS keeps the objects>
    TAG=persistA WORDS=1500 MODE=hit   python3 bench_persist.py   # L2 -> L1 -> GPU

MODE=store prints the recompute TTFT and the warm (L1) hit; MODE=hit prints the
cold-L1 hit, which is the DAOS-served number. Same tokenizer input both times:
the prompt is a deterministic function of TAG and WORDS.
"""

import json
import os
import time
import urllib.request

URL = os.environ.get("URL", "http://localhost:8001/v1/completions")
TAG = os.environ.get("TAG", "persist0")
WORDS = int(os.environ.get("WORDS", "1500"))
MODE = os.environ.get("MODE", "store")


def ttft(prompt, mx=1):
    body = json.dumps({"model": "qwen3", "prompt": prompt, "max_tokens": mx,
                       "temperature": 0, "stream": True}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    r = urllib.request.urlopen(req, timeout=300)
    for raw in r:
        s = raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip() != "[DONE]":
            try:
                o = json.loads(s[5:].strip())
            except Exception:
                continue
            if o.get("choices", [{}])[0].get("text", ""):
                return time.time() - t0
    return time.time() - t0


p = TAG + " " + " ".join(f"w{i}x{i % 97}" for i in range(WORDS))
if MODE == "store":
    miss = ttft(p); time.sleep(2)
    warm = ttft(p)
    print(f"[PERSIST store] tag={TAG} words={WORDS} miss(recompute)={miss*1000:.0f}ms "
          f"warm_hit(L1)={warm*1000:.0f}ms", flush=True)
else:
    cold = ttft(p); time.sleep(2)
    warm = ttft(p)
    print(f"[PERSIST hit] tag={TAG} words={WORDS} cold_hit(L2/DAOS)={cold*1000:.0f}ms "
          f"warm_hit(L1)={warm*1000:.0f}ms", flush=True)
