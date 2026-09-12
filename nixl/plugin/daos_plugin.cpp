/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Gluesys Co., Ltd.
 */
#include <memory>

#include "backend/backend_plugin.h"
#include "daos_backend.h"

using daos_plugin_t = nixlBackendPluginCreator<nixlDaosEngine>;

namespace {
const nixl_b_params_t daos_plugin_params = nixlDaosEngine::getPluginParams();
} // namespace

#ifdef STATIC_PLUGIN_DAOS
nixlBackendPlugin *
createStaticDAOSPlugin() {
    return daos_plugin_t::create(
        NIXL_PLUGIN_API_VERSION, "DAOS", "0.1.0", daos_plugin_params, {DRAM_SEG, FILE_SEG});
}
#else
extern "C" NIXL_PLUGIN_EXPORT nixlBackendPlugin *
nixl_plugin_init() {
    return daos_plugin_t::create(
        NIXL_PLUGIN_API_VERSION, "DAOS", "0.1.0", daos_plugin_params, {DRAM_SEG, FILE_SEG});
}

extern "C" NIXL_PLUGIN_EXPORT void
nixl_plugin_fini() {}
#endif
