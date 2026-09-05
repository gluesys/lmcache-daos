import time,json,urllib.request
def send(p):
    b=json.dumps({"model":"qwen3","prompt":p,"max_tokens":1,"temperature":0}).encode()
    r=urllib.request.Request("http://localhost:8001/v1/completions",data=b,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(r,timeout=180))["usage"]["prompt_tokens"]
p="Final. "+" ".join(f"fin{i}z{i%53}" for i in range(1500))
print("store tok",send(p)); time.sleep(3); print("load tok",send(p))
