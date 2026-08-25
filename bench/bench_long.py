import time, json, urllib.request
def gen(prompt, mx, stream=True):
    body=json.dumps({"model":"qwen3","prompt":prompt,"max_tokens":mx,"temperature":0,"stream":stream}).encode()
    req=urllib.request.Request("http://localhost:8001/v1/completions",data=body,headers={"Content-Type":"application/json"})
    t0=time.time(); ttft=None; n=0
    r=urllib.request.urlopen(req,timeout=600)
    for raw in r:
        s=raw.decode(errors="ignore").strip()
        if not s.startswith("data:"): continue
        d=s[5:].strip()
        if d=="[DONE]": break
        try: o=json.loads(d)
        except: continue
        txt=o.get("choices",[{}])[0].get("text","")
        if txt:
            if ttft is None: ttft=time.time()-t0
            n+=1
    tot=time.time()-t0
    return ttft, tot, n
p="Story. "+" ".join(f"tok{i}" for i in range(1500))
gen(p,1); time.sleep(2)                       # populate cache
ttft,tot,n=gen(p,2000)                          # cache-hit + long gen
print(f"[LONGGEN] TTFT={ttft*1000:.0f}ms total={tot*1000:.0f}ms gen={n}tok tok/s={n/tot:.1f}",flush=True)
