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

- **No VRAM_SEG.** `daos_obj_fetch_gpu()` / `daos_obj_update_gpu()` exist in the
  `theodore/b_cufile` client and are the reason to add it, but the development
  host has no `nvidia_fs` loaded, so claiming the capability would advertise a
  path that cannot be exercised.
- **Per-request overhead in the NIXL path**, roughly 0.076 ms, from the
  `dynamic_cast`, the `std::map` in `prepXfer()`, five vector allocations per
  group and the pool's mutex. Folding hides it -- 5% of a folded transfer,
  60% of an unfolded one -- but it is the next thing to attack. The figure
  comes from comparing two different harnesses, so it is an estimate.

## Threads

Work runs on a fixed pool, sized by `NIXL_DAOS_THREADS` (default 64). A DAOS
event queue was rejected on purpose: it serialises on the per-EQ `eqx_lock` and
caps around 7-12 GB/s however the queues are arranged.

64 is where the ladder flattens: unfolded, the backend reaches 8.3 GB/s at 16
threads, 11.3 at 32, 14.4 at 64 and 14.3 at 128. Effective concurrency is
`min(threads, requests in flight)`, so a caller that keeps fewer requests
outstanding is capped by its own depth, not by this setting.

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
