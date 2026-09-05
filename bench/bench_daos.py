import time, json, urllib.request, urllib.error
PORT=8001
def build(n): return "Passage. " + " ".join(f"tok{i}" for i in range(n))
def send(prompt):
    body=json.dumps({"model":"qwen3","prompt":prompt,"max_tokens":1,"temperature":0}).encode()
    req=urllib.request.Request(f"http://localhost:{PORT}/v1/completions",data=body,headers={"Content-Type":"application/json"})
    t=time.time()
    try:
        r=json.load(urllib.request.urlopen(req,timeout=180)); dt=time.time()-t
        return r["usage"]["prompt_tokens"], dt, None
    except urllib.error.HTTPError as e:
        return None, time.time()-t, f"HTTP{e.code}:{e.read()[:120]}"
# probe sizes → aim ~3500 and ~7000 tokens
for nw in [1500, 3200]:
    p=build(nw)
    tok,dt,err=send(p)
    if err: print(f"[probe nw={nw}] ERR {err}",flush=True); continue
    print(f"[STORE] nw={nw} prompt_tokens={tok} t={dt*1000:.0f}ms",flush=True)
    time.sleep(3)
    tok,dt,err=send(p)
    print(f"[LOAD ] nw={nw} prompt_tokens={tok} t={dt*1000:.0f}ms err={err}",flush=True)
    time.sleep(2)
