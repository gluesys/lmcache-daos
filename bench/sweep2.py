# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import time, json, os, urllib.request
ARM=os.environ.get("ARM","?"); CTXS=[int(x) for x in os.environ.get("CTXS","65536").split(",")]
CACHED=os.environ.get("CACHED","1")=="1"; KVPT=160*1024
def post(p,o,t=1800):
    b=json.dumps(o).encode()
    r=urllib.request.Request("http://localhost:8001"+p,data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=t))
def ttft(ids):
    b=json.dumps({"model":"qwen3","prompt":ids,"max_tokens":1,"temperature":0,"stream":True}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    t0=time.time(); resp=urllib.request.urlopen(r,timeout=1800)
    for raw in resp:
        s=raw.decode(errors="ignore").strip()
        if s.startswith("data:") and s[5:].strip()!="[DONE]":
            try: o=json.loads(s[5:].strip())
            except: continue
            if o.get("choices",[{}])[0].get("text",""): return time.time()-t0
    return time.time()-t0
def make_ids(target, tag):
    """목표 토큰 수까지 증분 토크나이즈 후 정확히 슬라이스. 1회만 생성해 재사용."""
    ids=[]; k=0
    while len(ids) < target:
        txt=f"{tag} seg{k} "+" ".join(f"w{k}_{i}z{i%89}" for i in range(4000))
        ids += post("/tokenize",{"model":"qwen3","prompt":txt})["tokens"]; k+=1
    return ids[:target]
for ctx in CTXS:
    tag=f"{ARM}_{ctx}_{int(time.time())}"
    ids=make_ids(ctx,tag)                      # ★ 고정 토큰열
    assert len(ids)==ctx, f"len={len(ids)} != {ctx}"
    kv=ctx*KVPT/1e9
    miss=ttft(ids)
    hit=None; valid=""
    if CACHED:
        time.sleep(max(10,int(kv*1.5)))         # KV 크기에 비례한 settle
        hit=ttft(ids)
        if hit>miss*0.5:                        # 재시도 1회
            time.sleep(6); h2=ttft(ids)
            if h2<hit: hit=h2
        valid = "OK" if hit<miss*0.5 else "INVALID(cache-miss?)"
    print(f"CSV,{ARM},{ctx},{kv:.2f},{miss*1000:.0f},{'' if hit is None else f'{hit*1000:.0f}'},"
          f"{'' if hit is None else f'{miss/hit:.2f}'},{valid}",flush=True)
