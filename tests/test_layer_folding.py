#!/usr/bin/env python3
"""SPDX-License-Identifier: Apache-2.0
Copyright 2026 Gluesys Co., Ltd.

The layerwise key -> (dkey, akey) mapping and the batch grouping that folds a
chunk's layers into one RPC. No DAOS and no LMCache: these are pure functions,
and the whole point of deriving the chunk string through the MRO rather than by
importing CacheEngineKey is that this can be checked off the serving host.

The fakes below copy LMCache's shape exactly (lmcache/utils.py): a layer key
subclasses the chunk key, and its to_string() inserts the layer id as the sixth
@-field, BEFORE the optional tags -- which is why stripping a suffix would be
wrong and the parent's method is called instead.

    python3 tests/test_layer_folding.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lmcache_daos import connector as C       # noqa: E402
from lmcache_daos import serde_v3 as v3       # noqa: E402

fails = 0


def ck(what, ok, extra=""):
    global fails
    if not ok:
        fails += 1
    print(f"  {what:56s} {'PASS' if ok else 'FAIL'} {extra}")


class ChunkKey:
    def __init__(self, chunk="abc", tags=()):
        self.chunk, self.tags = chunk, tags

    def to_string(self):
        s = f"m@1@0@{self.chunk}@bf16"
        if self.tags:
            s += "@" + "@".join(self.tags)
        return s


class LayerKey(ChunkKey):
    def __init__(self, chunk="abc", layer_id=0, tags=()):
        super().__init__(chunk, tags)
        self.layer_id = layer_id

    def to_string(self):
        s = f"m@1@0@{self.chunk}@bf16@{self.layer_id}"
        if self.tags:
            s += "@" + "@".join(self.tags)
        return s


print("\n=== chunk string ===")
ck("whole-chunk key is unchanged",
   C._chunk_str(ChunkKey()) == "m@1@0@abc@bf16")
ck("layer key drops the layer field",
   C._chunk_str(LayerKey(layer_id=7)) == "m@1@0@abc@bf16",
   f"got {C._chunk_str(LayerKey(layer_id=7))!r}")
ck("tags survive (layer is not a suffix)",
   C._chunk_str(LayerKey(layer_id=7, tags=("a%1",))) == "m@1@0@abc@bf16@a%1",
   f"got {C._chunk_str(LayerKey(layer_id=7, tags=('a%1',)))!r}")
ck("every layer of a chunk maps to ONE dkey",
   len({C._raw_addr(LayerKey(layer_id=i))[0] for i in range(40)}) == 1)
ck("a different chunk maps elsewhere",
   C._raw_addr(LayerKey(chunk="zzz"))[0] != C._raw_addr(LayerKey())[0])
ck("layer key and whole-chunk key agree on the dkey",
   C._raw_addr(LayerKey(layer_id=3))[0] == C._raw_addr(ChunkKey())[0])

print("\n=== akeys ===")
ck("whole-chunk mode uses P/M",
   C._akeys_for(None) == (v3.AKEY_PAYLOAD, v3.AKEY_META))
ck("layer 7 uses L007/M007",
   C._akeys_for(7) == (b"L007", b"M007"))
ck("layers never collide",
   len({C._akeys_for(i) for i in range(40)}) == 40)
ck("payload and metadata akeys are distinct",
   len(set(v3.payload_akeys(40)) & set(v3.meta_akeys(40))) == 0)
ck("meta_akeys(0) is whole-chunk", v3.meta_akeys(0) == (v3.AKEY_META,))

print("\n=== grouping ===")
g = C.DaosConnector._group_layers(
    [C._raw_addr(LayerKey(chunk=c, layer_id=i))
     for c in ("aa", "bb") for i in range(4)])
ck("two chunks x 4 layers -> 2 groups", g is not None and len(g) == 2)
ck("each group keeps its 4 layers in order",
   g is not None and all([l for _, l in m] == [0, 1, 2, 3] for m in g.values()))
ck("positions are preserved for the scatter back",
   g is not None and sorted(p for m in g.values() for p, _ in m) == list(range(8)))
ck("whole-chunk batch is not foldable",
   C.DaosConnector._group_layers([C._raw_addr(ChunkKey())]) is None)
ck("a mixed batch is not foldable",
   C.DaosConnector._group_layers(
       [C._raw_addr(LayerKey()), C._raw_addr(ChunkKey())]) is None)

print(f"\n  === {'ALL PASS' if not fails else str(fails) + ' FAILURE(S)'} "
      f"({fails} failures) ===\n")
sys.exit(1 if fails else 0)
