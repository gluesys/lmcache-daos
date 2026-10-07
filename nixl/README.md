<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

# NIXL DAOS backend — moved

The NIXL DAOS backend plugin that lived here (`plugin/`, `tests/`,
`integration.patch`) moved on 2026-10-08 to **exastor/nixl-daos**:

| here (until 2026-10-08) | now in exastor/nixl-daos |
|---|---|
| `nixl/plugin/` | `src/plugins/daos/` (with this README as `src/plugins/daos/README.md`) |
| `nixl/tests/` | `test/unit/plugins/daos/` |
| `nixl/integration.patch` | `upstream/integration.patch`, applied by `upstream/apply-integration.sh` |

Its history up to the move stays in this repository (`git log -- nixl`). The
measurement and design documents it relies on remain here:
`doc/NIXL-DAOS-MEASUREMENT.md`, `doc/NIXL-DAOS-VRAM.md`,
`doc/LAYERWISE-MEASUREMENT.md`, `doc/FAILURE-MODES.md`.
