# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import time,json,urllib.request
def send(p):
    b=json.dumps({"model":"qwen3","prompt":p,"max_tokens":1,"temperature":0}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=180))["usage"]["prompt_tokens"]
p="tiny single chunk test "+" ".join(f"s{i}" for i in range(140))  # ~1 chunk (256 tok)
print("store=",send(p),flush=True); time.sleep(3); print("load=",send(p),flush=True)
