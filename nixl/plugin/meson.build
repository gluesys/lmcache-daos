# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Gluesys Co., Ltd.

# DAOS is frequently installed outside /usr: a source build lands in
# /var/daos-<name>, the GPU-direct client in /opt/daos-gds-gpu. The parent's
# find_library('daos') only answers "does it exist somewhere the linker
# already looks", which is not the same question, and its result carries no
# path -- relying on it produced a configure that succeeded and a link that
# failed with "cannot find -ldaos". So resolve the prefix here, and take both
# the header and the library from it.
fs = import('fs')
cxx = meson.get_compiler('cpp')

daos_incdirs = [nixl_inc_dirs, utils_inc_dirs]
daos_lib = daos_dep

if not cxx.has_header('daos.h')
    daos_prefixes = ['/var/daos-stockfull', '/opt/daos-gds-gpu', '/opt/daos', '/usr/local']
    found_prefix = ''
    foreach pfx : daos_prefixes
        if found_prefix == '' and fs.exists(pfx / 'include' / 'daos.h')
            found_prefix = pfx
        endif
    endforeach
    if found_prefix == ''
        error('DAOS plugin: daos.h not found. Searched the compiler default path and ' + ', '.join(daos_prefixes) + '. Set CPATH to the DAOS include directory.')
    endif

    libdirs = []
    foreach sub : ['lib64', 'lib']
        if fs.is_dir(found_prefix / sub)
            libdirs += [found_prefix / sub]
        endif
    endforeach

    daos_lib = cxx.find_library('daos', dirs: libdirs, required: true)
    message('DAOS prefix: ' + found_prefix)
    daos_incdirs += [include_directories(found_prefix / 'include', is_system: true)]
endif

if not daos_lib.found()
    error('DAOS plugin requires libdaos (install daos-devel, or put its lib dir on LIBRARY_PATH)')
endif

plugin_deps = [nixl_infra, nixl_common_dep, daos_lib]

daos_sources = ['daos_backend.cpp', 'daos_backend.h', 'daos_plugin.cpp']

if 'DAOS' in static_plugins
    daos_backend_lib = static_library('DAOS',
        daos_sources,
        dependencies: plugin_deps,
        cpp_args: compile_flags,
        include_directories: daos_incdirs,
        install: false,
        name_prefix: 'libplugin_')
else
    daos_backend_lib = shared_library('DAOS',
        daos_sources,
        dependencies: plugin_deps,
        cpp_args: ['-fPIC'],
        include_directories: daos_incdirs,
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
