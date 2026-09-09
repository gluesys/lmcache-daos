# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.
import torch, time
N=1<<30
for pin in [False, True]:
    cpu=torch.empty(N,dtype=torch.uint8,pin_memory=pin); g=torch.empty(N,dtype=torch.uint8,device='cuda')
    for _ in range(3): g.copy_(cpu,non_blocking=pin)
    torch.cuda.synchronize(); t=time.time()
    for _ in range(5): g.copy_(cpu,non_blocking=pin)
    torch.cuda.synchronize(); dt=(time.time()-t)/5
    print(f"H2D {'pinned' if pin else 'pageable'}: {dt*1000:.1f}ms/GiB = {1/dt:.1f} GB/s",flush=True)
