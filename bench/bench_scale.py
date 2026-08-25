import time, json, urllib.request, threading, sys
KVPT = 160*1024   # 14B: 160 KiB/token
def req(prompt, mx=1):
    b=json.dumps({"model":"qwen3","prompt":prompt,"max_tokens":mx,"temperature":0,"stream":True}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    t0=time.time()
    resp=urllib.request.urlopen(r,timeout=600)
    for raw in resp:
        s=raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip()!="[DONE]":
            try: o=json.loads(s[5:].strip())
            except: continue
            if o.get("choices",[{}])[0].get("text",""): return time.time()-t0
    return time.time()-t0
N=8
tag="scale%d"%int(time.time())
prompts=[f"{tag}_s{i} "+" ".join(f"w{i}k{j}v{j%89}" for j in range(1500)) for i in range(N)]
print("storing %d prompts..."%N, flush=True)
for p in prompts: req(p)
time.sleep(6)
print("tokens/req ~6100, KV/req ~0.93GB", flush=True)
for conc in [1,2,4,8]:
    sel=prompts[:conc]; ttfts=[None]*conc
    def run(i):
        ttfts[i]=req(sel[i])
    t0=time.time()
    th=[threading.Thread(target=run,args=(i,)) for i in range(conc)]
    [x.start() for x in th]; [x.join() for x in th]
    wall=time.time()-t0
    gb=conc*6100*KVPT/1e9
    print(f"conc={conc}: wall={wall*1000:6.0f}ms  agg={gb/wall:5.2f} GB/s  TTFT avg={sum(ttfts)/conc*1000:5.0f}ms max={max(ttfts)*1000:5.0f}ms", flush=True)
    time.sleep(3)
