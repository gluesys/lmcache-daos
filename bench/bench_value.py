import time, json, urllib.request
def ttft(prompt, mx=1):
    body=json.dumps({"model":"qwen3","prompt":prompt,"max_tokens":mx,"temperature":0,"stream":True}).encode()
    req=urllib.request.Request("http://localhost:8001/v1/completions",data=body,headers={"Content-Type":"application/json"})
    t0=time.time(); r=urllib.request.urlopen(req,timeout=300)
    for raw in r:
        s=raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip()!="[DONE]":
            import json as j
            try: o=j.loads(s[5:].strip())
            except: continue
            if o.get("choices",[{}])[0].get("text",""): return time.time()-t0
    return time.time()-t0
import random
tag="uniqV%d"%int(time.time())
p=tag+" "+" ".join(f"w{i}x{i%97}" for i in range(1500))   # fresh, uncached ~6000tok
miss=ttft(p); time.sleep(2)          # MISS: recompute prefill + store
hit=ttft(p)                           # HIT: load from DAOS
print(f"[VALUE] miss(recompute)={miss*1000:.0f}ms  hit(DAOS-load)={hit*1000:.0f}ms  speedup={miss/hit:.1f}x",flush=True)
