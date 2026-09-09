# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import time, json, urllib.request, threading, os
def post(p,o):
    b=json.dumps(o).encode()
    r=urllib.request.Request("http://localhost:8001"+p,data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=600))
def gen(ids):
    b=json.dumps({"model":"qwen3","prompt":ids,"max_tokens":1,"temperature":0}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    urllib.request.urlopen(r,timeout=600).read()
N=8; DUR=float(os.environ.get("DUR","40"))
tag="cpu%d"%int(time.time())
ids=[post("/tokenize",{"model":"qwen3","prompt":f"{tag}_c{i} "+" ".join(f"w{i}k{j}" for j in range(1500))})["tokens"] for i in range(N)]
for x in ids: gen(x)      # populate
time.sleep(3)
stop=time.time()+DUR; cnt=0
print("loadgen start", flush=True)
while time.time()<stop:
    th=[threading.Thread(target=gen,args=(x,)) for x in ids]
    [t.start() for t in th]; [t.join() for t in th]; cnt+=N
gb=cnt*len(ids[0])*160*1024/1e9
print(f"loadgen done: {cnt} reqs, {gb:.1f} GB retrieved in {DUR:.0f}s = {gb/DUR:.1f} GB/s", flush=True)
