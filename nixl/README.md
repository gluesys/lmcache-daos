<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# NIXL DAOS backend

A storage backend plugin for [NIXL](https://github.com/ai-dynamo/nixl). NIXL
ships 16 backends; none of them speaks DAOS, which is what this adds.

Status: **registration and transfer work, and it has been measured.** On the
400G verbs testbed it reaches **34.17 GB/s** reading 4.69 GiB; see
`doc/NIXL-DAOS-MEASUREMENT.md`. All three programs in `tests/` pass, including
one that drives the backend through a real `nixlAgent`.

## Why the object API and not DFS

`doc/LAYERWISE-MEASUREMENT.md` measured the same 4800 reads of 1 MiB two ways:

| path | fixed cost per object | marginal |
|---|---|---|
| DFS (`dfs_sys_read` on a flat namespace) | 0.63 ms | 0.067 ms/MiB |
| raw object API (dkey/akey) | **0.0137 ms** | 0.0385 ms/MiB |

At 1 MiB objects DFS spends about 90% of the time on overhead that dkey/akey
does not pay. The object API also folds a whole descriptor list into one
`daos_obj_fetch()` through its iod array, which is the shape `prepXfer()` hands
us and which a file-per-object model cannot express.

## Descriptor mapping

`nixlBasicDesc` carries only `addr`, `len` and `devId`; `nixlBlobDesc` adds
`metaInfo`. The backend reads them as:

| field | meaning |
|---|---|
| `metaInfo` | `"pool/container"`, or `"pool/container/<hi>.<lo>"` for an explicit object id |
| `devId` | the caller's key for the object; the object id is derived from it when `metaInfo` has two fields |
| `addr` | offset within the object |
| `len` | bytes |

Deriving the object id from `devId` is deliberate: the same `devId` must name
the same object across process restarts, or a cache cannot find what it stored.
A test verifies that after the container handle has been closed and reopened.

## dkey/akey split

```
dkey = addr / dkeySpan          akey = addr % dkeySpan          (dkeySpan = 64 MiB)
```

dkey decides placement. Descriptors inside one span share a dkey, land on one
target, and fold into a single RPC; separate spans spread across targets. Both
halves matter, and the benchmark says why -- same 4.69 GiB, same bytes:

| shape | time |
|---|---|
| 40 akeys under one dkey, one RPC per chunk | **204 ms** |
| one akey per RPC | 245 ms |
| all 40 MiB as a single akey extent | 571 ms |

Folding wins, but only while the data still arrives as separate akeys rather
than one long extent. `dkeySpan` should become a plugin parameter as soon as
there is a second workload to tune it against.

## What it does not do yet

- **`VRAM_SEG` is off by default, and should stay off for now.** It works --
  device-to-device round trips are bit-identical -- but it is 3.4x *slower*
  than staging through host memory, because CaRT re-registers the GPU buffer
  on every transfer at about 0.43 ms per sgl entry. The escape hatch for that,
  `daos_mem_attr_t::ma_rkey`, is only reachable from cuFile's plugin callback,
  which a backend that calls DAOS directly never sees. See
  `doc/NIXL-DAOS-VRAM.md`.
- **Per-request overhead in the NIXL path**, roughly 0.076 ms, from the
  `dynamic_cast`, the `std::map` in `prepXfer()`, five vector allocations per
  group and the pool's mutex. Folding hides it -- 5% of a folded transfer,
  60% of an unfolded one -- but it is the next thing to attack. The figure
  comes from comparing two different harnesses, so it is an estimate.

## GPU memory

The plugin advertises `VRAM_SEG` only when the client it was built against
actually exports `daos_obj_fetch_gpu()`, which lives in the unmerged
`theodore/b_cufile` branch. meson checks for the symbol; `DAOS_PREFIX` in the
environment picks which client to build against, because a host can carry both
a stock client and the draft and choosing between them is a deployment
decision rather than something to guess from a search order.

```bash
DAOS_PREFIX=/opt/daos-gds-gpu meson setup build-gpu ...
#  -> "DAOS GPU-direct: available (VRAM_SEG enabled)"
```

Running it also needs the GDS transport stack: `D_MEM_DEVICE=1` and a
CUDA-built libfabric ahead of the stock one on `LD_LIBRARY_PATH`. Without
those Mercury refuses the bulk handle with `HG_OPNOTSUPPORTED` rather than
falling back, so a GPU run either exercises the GPU path or fails outright --
it cannot quietly measure a host bounce.

Read `doc/NIXL-DAOS-VRAM.md` before building anything on this. It works, and
it is slower than not using it.

## Threads

Work runs on a fixed pool, sized by `NIXL_DAOS_THREADS` (default 64).

64 is where the ladder flattens: unfolded, the backend reaches 8.3 GB/s at 16
threads, 11.3 at 32, 14.4 at 64 and 14.3 at 128. Effective concurrency is
`min(threads, requests in flight)`, so a caller that keeps fewer requests
outstanding is capped by its own depth, not by this setting.

## Bounding a dead engine (`NIXL_DAOS_EQ_TIMEOUT`)

**On by default, 60 s.** The backend submits to a DAOS event queue and polls
with that deadline. Set it to `0` to go back to a blocking call.

It exists because a blocking DAOS call whose engine has died **does not return**
(`doc/FAILURE-MODES.md`). Through this plugin, with `daos_server` killed
mid-read:

| | outcome |
|---|---|
| blocking | still running at **120 s**, 65 threads stuck |
| deadline 8 s | exited in **8 s**, every thread reclaimed |

### What it costs: nothing measurable

Measured on client-5 against cell1/cell2 -- 400G verbs, `ofi+verbs;ofi_rxm`,
2 ranks x 8 targets -- which is the regime the event queue was originally
rejected in. 120 objects x 40 layers, fold=1, four runs each:

| | runs | mean |
|---|---|---|
| blocking | 31.29 · 31.00 · 30.70 · 31.15 | **31.04 GB/s** |
| event queue | 30.44 · 31.27 · 30.23 · 30.61 | **30.64 GB/s** |

The ranges overlap and the event queue won one run outright. Unfolded, where
there are 4800 requests instead of 120, it is the same: 7.55 against 7.50.

This retires what this file used to say -- that an event queue "caps around
7-12 GB/s however the queues are arranged". That came from a sweep over the DFS
async path through Python with 28 MiB reads; it does not transfer to folded
object-API requests.

### Why the queues are borrowed

One EQ per thread was the first implementation and it was 35% SLOWER than
blocking on cxl2, 2.08 against 3.12 GB/s. The thread pool is deliberately
oversized: idle threads are free, but an idle event queue holds a network
context, so 64 threads paid for 64 contexts to run 16 concurrent requests.

| | 64 threads | 16 threads |
|---|---|---|
| blocking | 3.16 GB/s | 3.12 GB/s |
| event queue, one per thread | 2.08 | 3.24 |
| event queue, borrowed | **3.24** | **3.26** |

Borrowing for the duration of one request grows the set to the actual
concurrency and no further, so thread count stops mattering. No two threads
hold the same queue, so a poll cannot harvest another thread's completion.

60 s is a backstop, not a latency target: a request measured 1.34 ms on verbs,
so the default carries four orders of magnitude of headroom.

## Building

```bash
git clone https://github.com/ai-dynamo/nixl.git && cd nixl
cp -r <this>/plugin src/plugins/daos          # then apply integration.patch
pip install --user 'meson>=1.4' pybind11      # distro meson may be < 0.64
meson setup build -Dcudapath_inc=/usr/local/cuda/include \
                  -Dcudapath_lib=/usr/local/cuda/lib64
ninja -C build
```

NIXL requires C++20 and meson >= 0.64. GCC 11.5 is enough. On an older UCX or
libfabric the transport plugins fail to compile (`UCS_BIT_GET`,
`fi_mr_attr::rocr`); add `-Ddisable_plugins=UCX,LIBFABRIC` when only the
storage backends are wanted. UCX 1.21 builds them fine.

The plugin finds DAOS itself. `find_library('daos')` answers only "is it
somewhere the linker already looks" and returns no path, which produces a
configure that succeeds and a link that fails with `cannot find -ldaos`. So
`meson.build` searches `/var/daos-stockfull`, `/opt/daos-gds-gpu`, `/opt/daos`
and `/usr/local` for a prefix that actually carries `daos.h`, and takes both
the header and the library from it. A packaged DAOS under `/usr` and a source
build under `/var` both work with no arguments.

On a host without meson packaged (Rocky 10 has none), `python3 -m ensurepip`
then `pip install meson ninja` works. `nvcc` must be on `PATH` or meson's CUDA
probe fails.

## Tests

Both need a reachable pool and a container; neither needs a NIXL agent.

```bash
./test_reg   <pool> <container>   # register/deregister, oid stability, refcount
./test_xfer  <pool> <container>   # write/read round trip, integrity, miss detection
NIXL_PLUGIN_DIR=<build>/src/plugins/daos \
./test_agent <pool> <container>   # the same through a real nixlAgent
```

`test_agent` is the one that catches contract violations the other two cannot.
A backend driven directly never has `loadLocalMD()` called, so omitting it --
which the base class answers with an error, not a default -- passed every
direct test and every benchmark while failing *every* `registerMem()` the agent
made. It also confirms what the other tests only assume: `createXferReq()`
hands the backend `nixlBasicDesc`, with no metadata pointer, so the agent must
match a transfer descriptor back to a registered object by `devId` alone.

`test_xfer` writes a self-describing payload -- every 8-byte word encodes its
own (descriptor, offset) -- so a region that comes back wrong names where it
actually came from. A plain pattern would hide cross-chunk corruption, which
this project has already been bitten by once.

## An upstream bug found on the way

`nixlBackendEngine`'s constructor dereferences `init_params->customParams`
without a null check, while the field defaults to `nullptr`. The agent always
fills it, so it never fires in normal use, but it segfaults any attempt to
unit-test a backend directly. Worth reporting.
