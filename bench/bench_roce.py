# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import time,json,urllib.request
def send(p):
    b=json.dumps({"model":"qwen3","prompt":p,"max_tokens":1,"temperature":0}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    o=json.load(urllib.request.urlopen(r,timeout=180)); return o["usage"]["prompt_tokens"]
p="RoCEv1 fresh "+" ".join(f"r7q{i}k{i%61}" for i in range(1500))
print("store tokens=",send(p),flush=True); time.sleep(3); print("load tokens=",send(p),flush=True)
