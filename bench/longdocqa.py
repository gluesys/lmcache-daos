"""VAST long-doc-qa 조건 재현: 100GB KV working set, 12 inflight.
VAST 공개: 평균 TTFT 2.8x 개선, peak 9.79 GB/s (모델/기준선 미공개).
우리 조건 명시: Qwen3-14B, KV 160KiB/token → 149 docs x 4096 tok = 100GB.
"""
import time, json, os, random, threading, statistics as st
import urllib.request
NDOC=int(os.environ.get("NDOC","149")); DOCTOK=int(os.environ.get("DOCTOK","4096"))
INFLIGHT=int(os.environ.get("INFLIGHT","12")); NQ=int(os.environ.get("NQ","149"))
ARM=os.environ.get("ARM","?"); POPULATE=os.environ.get("POPULATE","1")=="1"
KVPT=160*1024
def post(p,o,t=3600):
    b=json.dumps(o).encode()
    r=urllib.request.Request("http://localhost:8001"+p,data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=t))
def ttft(ids):
    b=json.dumps({"model":"qwen3","prompt":ids,"max_tokens":1,"temperature":0,"stream":True}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    t0=time.time(); resp=urllib.request.urlopen(r,timeout=3600)
    for raw in resp:
        s=raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip()!="[DONE]":
            try: o=json.loads(s[5:].strip())
            except: continue
            if o.get("choices",[{}])[0].get("text",""): return time.time()-t0
    return time.time()-t0
# 문서 토큰열 생성(고정, 문서별 유일)
SEED="longdocqa_v1"   # 고정 시드 → arm 간 동일 코퍼스
docs=[]
for d in range(NDOC):
    ids=[]; k=0
    while len(ids)<DOCTOK:
        txt=f"{SEED} doc{d} seg{k} "+" ".join(f"d{d}w{k}_{i}z{i%89}" for i in range(1200))
        ids+=post("/tokenize",{"model":"qwen3","prompt":txt})["tokens"]; k+=1
    docs.append(ids[:DOCTOK])
ws=NDOC*DOCTOK*KVPT/1e9
print(f"corpus: {NDOC} docs x {DOCTOK} tok = {NDOC*DOCTOK/1000:.0f}K tokens, KV working set {ws:.1f} GB",flush=True)
if POPULATE:
    t0=time.time()
    for i,d in enumerate(docs): ttft(d)
    print(f"populate: {time.time()-t0:.0f}s ({ws/(time.time()-t0):.2f} GB/s incl. prefill)",flush=True)
    time.sleep(30)   # store settle
random.seed(42); order=[random.randrange(NDOC) for _ in range(NQ)]
lat=[]; lk=threading.Lock(); idx=[0]
def worker():
    while True:
        with lk:
            if idx[0]>=len(order): return
            i=order[idx[0]]; idx[0]+=1
        t=ttft(docs[i])
        with lk: lat.append(t)
t0=time.time()
th=[threading.Thread(target=worker) for _ in range(INFLIGHT)]
[x.start() for x in th]; [x.join() for x in th]
wall=time.time()-t0
lat_ms=sorted(t*1000 for t in lat)
gb=NQ*DOCTOK*KVPT/1e9
print(f"RESULT,{ARM},inflight={INFLIGHT},queries={NQ},"
      f"avg={st.mean(lat_ms):.0f}ms,p50={lat_ms[len(lat_ms)//2]:.0f}ms,"
      f"p95={lat_ms[int(len(lat_ms)*0.95)]:.0f}ms,wall={wall:.1f}s,agg={gb/wall:.2f}GB/s",flush=True)
