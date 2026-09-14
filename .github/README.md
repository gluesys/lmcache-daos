<!-- SPDX-License-Identifier: Apache-2.0 -->
<!-- Copyright 2026 Gluesys Co., Ltd. -->

GitHub (`gluesys/lmcache-daos`) is a push mirror of the GitLab upstream; CI runs
from `.gitlab-ci.yml` there. The GitHub Actions copy of the pipeline was removed
on 2026-09-14: the mirror's token has only the `public_repo` scope, and GitHub
refuses any push that creates or updates `.github/workflows/*` without the
`workflow` scope, which had stalled the mirror since 2026-09-09.
