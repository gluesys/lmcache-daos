# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import time, json, urllib.request
PORT=8001
def build(n): return "Alpha. " + " ".join(f"w{i}a{i%97}" for i in range(n))
def send(prompt):
    body=json.dumps({"model":"qwen3","prompt":prompt,"max_tokens":1,"temperature":0}).encode()
    req=urllib.request.Request(f"http://localhost:{PORT}/v1/completions",data=body,headers={"Content-Type":"application/json"})
    r=json.load(urllib.request.urlopen(req,timeout=180)); return r["usage"]["prompt_tokens"]
# ~3400 tok point (distinct content from run1 so it's a real store)
p=build(850)
print("tokens=",send(p),flush=True); time.sleep(3); print("tokens2=",send(p),flush=True)
