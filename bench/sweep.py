import time, json, os, urllib.request
ARM=os.environ.get("ARM","?"); CTXS=[int(x) for x in os.environ.get("CTXS","8192,16384,32000").split(",")]
CACHED=os.environ.get("CACHED","1")=="1"
def post(p,o,t=1200):
    b=json.dumps(o).encode()
    r=urllib.request.Request("http://localhost:8001"+p,data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=t))
def ttft(ids):
    b=json.dumps({"model":"qwen3","prompt":ids,"max_tokens":1,"temperature":0,"stream":True}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    t0=time.time(); resp=urllib.request.urlopen(r,timeout=1200)
    for raw in resp:
        s=raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip()!="[DONE]":
            try: o=json.loads(s[5:].strip())
            except: continue
            if o.get("choices",[{}])[0].get("text",""): return time.time()-t0
    return time.time()-t0
KVPT=160*1024
for ctx in CTXS:
    tag=f"{ARM}_{ctx}_{int(time.time())}"
    words=int(ctx/1.1)+200
    txt=tag+" "+" ".join(f"w{i}x{i%97}" for i in range(words))
    ids=post("/tokenize",{"model":"qwen3","prompt":txt})["tokens"][:ctx]
    miss=ttft(ids)                       # recompute (+store if cached)
    hit=None
    if CACHED:
        time.sleep(8)                    # store settle
        hit=ttft(ids)
        if hit>miss*0.5:                 # 의심시 재시도(warm)
            time.sleep(4); hit=min(hit,ttft(ids))
    kv=len(ids)*KVPT/1e9
    print(f"CSV,{ARM},{len(ids)},{kv:.2f},{miss*1000:.0f},{'' if hit is None else f'{hit*1000:.0f}'},{'' if hit is None else f'{miss/hit:.2f}'}",flush=True)
