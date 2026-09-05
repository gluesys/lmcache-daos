import time, json, urllib.request, threading
KVPT=160*1024
def post(path, obj, timeout=600):
    b=json.dumps(obj).encode()
    r=urllib.request.Request("http://localhost:8001"+path,data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=timeout))
def gen_ids(ids, mx=1):
    b=json.dumps({"model":"qwen3","prompt":ids,"max_tokens":mx,"temperature":0,"stream":True}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    t0=time.time(); resp=urllib.request.urlopen(r,timeout=600)
    for raw in resp:
        s=raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip()!="[DONE]":
            try: o=json.loads(s[5:].strip())
            except: continue
            if o.get("choices",[{}])[0].get("text",""): return time.time()-t0
    return time.time()-t0
N=8; tag="tokscale%d"%int(time.time())
texts=[f"{tag}_t{i} "+" ".join(f"w{i}k{j}v{j%89}" for j in range(1500)) for i in range(N)]
ids=[post("/tokenize",{"model":"qwen3","prompt":t})["tokens"] for t in texts]
print("pre-tokenized: %d ids each"%len(ids[0]), flush=True)
for x in ids: gen_ids(x)          # store
time.sleep(6)
for conc in [1,2,4,8]:
    sel=ids[:conc]; tt=[None]*conc
    def run(i): tt[i]=gen_ids(sel[i])
    t0=time.time(); th=[threading.Thread(target=run,args=(i,)) for i in range(conc)]
    [x.start() for x in th]; [x.join() for x in th]; wall=time.time()-t0
    gb=conc*len(ids[0])*KVPT/1e9
    print(f"conc={conc}: wall={wall*1000:6.0f}ms agg={gb/wall:5.2f} GB/s TTFT avg={sum(tt)/conc*1000:5.0f}ms max={max(tt)*1000:5.0f}ms", flush=True)
    time.sleep(3)
