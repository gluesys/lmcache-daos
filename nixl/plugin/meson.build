# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.

# libdaos is now a hard requirement: registerMem() opens a pool. The gate in
# the parent src/plugins/meson.build skips this subdir when the library is
# absent, so reaching here without it means the build was asked for the plugin
# explicitly and should fail loudly rather than produce a stub .so.
if not daos_dep.found()
    error('DAOS plugin requires libdaos (install daos-devel)')
endif

plugin_deps = [nixl_infra, nixl_common_dep, daos_dep]

daos_sources = ['daos_backend.cpp', 'daos_backend.h', 'daos_plugin.cpp']

if 'DAOS' in static_plugins
    daos_backend_lib = static_library('DAOS',
        daos_sources,
        dependencies: plugin_deps,
        cpp_args: compile_flags,
        include_directories: [nixl_inc_dirs, utils_inc_dirs],
        install: false,
        name_prefix: 'libplugin_')
else
    daos_backend_lib = shared_library('DAOS',
        daos_sources,
        dependencies: plugin_deps,
        cpp_args: ['-fPIC'],
        include_directories: [nixl_inc_dirs, utils_inc_dirs],
        install: true,
        name_prefix: 'libplugin_',
        install_dir: plugin_install_dir,
        install_rpath: '$ORIGIN/..')
    if get_option('buildtype') == 'debug'
        run_command('sh', '-c',
            'echo "DAOS=' + daos_backend_lib.full_path() + '" >> ' + plugin_build_dir + '/pluginlist',
            check: true)
    endif
endif

daos_backend_interface = declare_dependency(link_with: daos_backend_lib)
