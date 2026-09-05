import time, json, urllib.request, threading
PORT=8001
def build(tag,n): return f"{tag}. " + " ".join(f"{tag}x{i}y{i%89}" for i in range(n))
def send(prompt):
    body=json.dumps({"model":"qwen3","prompt":prompt,"max_tokens":1,"temperature":0}).encode()
    req=urllib.request.Request(f"http://localhost:{PORT}/v1/completions",data=body,headers={"Content-Type":"application/json"})
    return json.load(urllib.request.urlopen(req,timeout=180))["usage"]["prompt_tokens"]
prompts=[build(t,850) for t in ["Conc1","Conc2","Conc3"]]
for p in prompts: send(p)          # store all (distinct)
time.sleep(3)
t=time.time(); ths=[threading.Thread(target=send,args=(p,)) for p in prompts]
[x.start() for x in ths]; [x.join() for x in ths]
print(f"CONC 3-parallel load wall={ (time.time()-t)*1000:.0f}ms",flush=True)
