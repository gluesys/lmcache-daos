import time,json,urllib.request
def send(p):
    b=json.dumps({"model":"qwen3","prompt":p,"max_tokens":1,"temperature":0}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=180))["usage"]["prompt_tokens"]
# fresh multi-chunk (~12 chunks). unique prefix so not previously stored
p="freshmulti_sess9 "+" ".join(f"u{i}v{i%71}" for i in range(700))
print("store=",send(p),flush=True); time.sleep(3); print("load=",send(p),flush=True)
