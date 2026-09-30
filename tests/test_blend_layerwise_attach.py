#!/usr/bin/env python3
"""SPDX-License-Identifier: Apache-2.0
Copyright 2026 Gluesys Co., Ltd.

CacheBlend/layerwise 에서 커넥터가 붙는지 — 가짜가 아니라 **실제 LMCache 클래스**로.

tests/test_layer_folding.py 는 LMCache 없이 돌도록 키 클래스를 흉내 낸다. 흉내가
맞는지는 흉내로 확인할 수 없으므로, LMCache 가 있는 곳에서는 진짜 타입으로 같은
성질을 확인한다 -- 특히 청크 문자열을 MRO 로 뽑는 방식이 실제
LayerCacheEngineKey 에 대해 성립하는지.

CacheBlend 는 layerwise 안에서만 자기 KV 포맷(KV_2TD)을 고르므로
(cache_engine.py: `if self.use_layerwise: ... elif config.enable_blending`),
"블렌딩에서 붙는가"는 곧 "layerwise 키가 접히는가 + KV_2TD 가 헤더를 통과하는가"다.

LMCache 가 없으면 건너뛴다.

    python3 tests/test_blend_layerwise_attach.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import torch
    import lmcache                                    # noqa: F401
    from lmcache.utils import CacheEngineKey
    from lmcache.v1.memory_management import MemoryFormat
    from lmcache.v1.protocol import RemoteMetadata, init_remote_metadata_info
except Exception as e:                                # pragma: no cover
    print(f"SKIP: LMCache/torch 없음 ({e})")
    sys.exit(0)

import lmcache_daos.connector as C                     # noqa: E402
from lmcache_daos import serde_v3 as v3                # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print(f"  {what:56s} {'PASS' if ok else 'FAIL'} {extra}")


print(f"\nlmcache {getattr(lmcache, '__version__', '?')}  torch {torch.__version__}")

print("\n=== 블렌딩이 쓰는 포맷이 있는가 ===")
for n in ("KV_2TD", "KV_T2D", "KV_MLA_FMT"):
    ck(f"MemoryFormat.{n}", hasattr(MemoryFormat, n))

print("\n=== 실제 LayerCacheEngineKey 가 한 dkey 로 접히는가 ===")
k = CacheEngineKey(model_name="m", world_size=1, worker_id=0,
                   chunk_hash=0x1234, dtype=torch.bfloat16)
layers = k.split_layers(40)
ck("split_layers(40)", len(layers) == 40)
ck("layer_id 가 노출된다", C._layer_of(layers[7]) == 7)
ck("40개 레이어 -> dkey 1개",
   len({C._raw_addr(lk)[0] for lk in layers}) == 1)
ck("청크 키와 같은 dkey", C._raw_addr(k)[0] == C._raw_addr(layers[0])[0])
ck("청크 문자열이 레이어 필드를 뺀 것 (MRO 방식 검증)",
   C._chunk_str(layers[7]) == k.to_string(),
   f"{layers[7].to_string()} -> {C._chunk_str(layers[7])}")
ck("akey 는 레이어마다 다르다",
   len({C._akeys_for(C._layer_of(x)) for x in layers}) == 40)
g = C.DaosConnector._group_layers([C._raw_addr(x) for x in layers])
ck("배치가 1개 그룹으로 접힌다",
   g is not None and len(g) == 1 and len(next(iter(g.values()))) == 40)

print("\n=== KV_2TD 가 헤더를 통과하는가 ===")
# RemoteMetadata 직렬화는 프로세스 전역 포맷이 초기화된 뒤에만 유효하다(protocol.py:24).
# 엔진이 기동 중에 부르는 것을 여기서는 직접 부른다 -- 그룹 1개 = 레이어 하나.
init_remote_metadata_info(1)
shape, dt = torch.Size([2, 16, 8, 128]), torch.bfloat16
for name in ("KV_2TD", "KV_T2D"):
    fmt = getattr(MemoryFormat, name)
    md = RemoteMetadata(length=4096, shapes=[shape], dtypes=[dt], fmt=fmt)
    back = RemoteMetadata.deserialize(
        v3.parse_meta(v3.pack_meta(bytes(md.serialize()), 4096)).meta)
    ck(f"{name} 왕복 (fmt/length/shape/dtype)",
       back.fmt == fmt and back.length == 4096
       and list(back.shapes) == [shape] and list(back.dtypes) == [dt],
       f"fmt={back.fmt}")

print(f"\n  === {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'} "
      f"({fails} failures) ===\n")
sys.exit(1 if fails else 0)
