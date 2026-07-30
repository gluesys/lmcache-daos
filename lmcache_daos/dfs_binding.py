"""ctypes binding to the DAOS ``dfs_sys`` API (libdfs / libdaos).

Grounded in DAOS 2.9.100 headers:
  * src/include/daos_fs_sys.h : dfs_sys_connect / _disconnect / _open / _close /
                                _read / _write / _remove
  * src/include/daos_fs.h     : DFS_RDWR, DFS_RELAXED flag values

We deliberately use the ``dfs_sys`` wrappers (plain ``void *buf``) instead of the
scatter/gather ``dfs_read``/``dfs_write`` so no ``d_sg_list_t`` marshalling is
needed -- the same choice the Samba vfs_daos module made.

The library is dlopen'd lazily on first use, so importing this module on a host
without the DAOS client installed does not fail (useful for unit tests / CI that
only exercise pure-Python logic). Actual I/O requires a working DAOS client
(agent running, pool/container provisioned).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import stat
from typing import Optional

# ---- flag constants (from daos_fs.h / daos_fs_sys.h) ----------------------
DFS_RDONLY = os.O_RDONLY
DFS_RDWR = os.O_RDWR
DFS_RELAXED = 0
DFS_BALANCED = 4
DFS_SYS_NO_CACHE = 1
DFS_SYS_NO_LOCK = 2

_S_IFREG = stat.S_IFREG  # dfs_sys_open requires S_IFMT bits to match object type


class _Dirent(ctypes.Structure):
    """glibc x86_64 ``struct dirent`` -- dfs_sys_readdir hands back a pointer to
    one of these. Only ``d_name`` is used; the buffer may be reused between
    calls, so the name is copied out immediately."""

    _fields_ = [
        ("d_ino", ctypes.c_ulong),
        ("d_off", ctypes.c_long),
        ("d_reclen", ctypes.c_ushort),
        ("d_type", ctypes.c_ubyte),
        ("d_name", ctypes.c_char * 256),
    ]

_daos = None  # libdaos handle
_dfs = None   # libdfs handle


class DaosError(OSError):
    """Raised when a DAOS/DFS call returns a non-zero status."""

    def __init__(self, func: str, rc: int):
        self.rc = rc
        super().__init__(rc, f"{func} failed: rc={rc} ({os.strerror(rc) if 0 < rc < 200 else 'DER'})")


def _load() -> None:
    """dlopen libdaos + libdfs and declare prototypes (idempotent)."""
    global _daos, _dfs
    if _dfs is not None:
        return

    daos_path = ctypes.util.find_library("daos") or "libdaos.so"
    dfs_path = ctypes.util.find_library("dfs") or "libdfs.so"
    _daos = ctypes.CDLL(daos_path, mode=ctypes.RTLD_GLOBAL)
    _dfs = ctypes.CDLL(dfs_path, mode=ctypes.RTLD_GLOBAL)

    # int daos_init(void); int daos_fini(void);
    _daos.daos_init.restype = ctypes.c_int
    _daos.daos_init.argtypes = []
    _daos.daos_fini.restype = ctypes.c_int
    _daos.daos_fini.argtypes = []

    # int dfs_init(void); int dfs_fini(void);
    # Required before dfs_connect/dfs_sys_connect (sets up the DFS cache);
    # otherwise those calls return EACCES(13).
    _dfs.dfs_init.restype = ctypes.c_int
    _dfs.dfs_init.argtypes = []
    _dfs.dfs_fini.restype = ctypes.c_int
    _dfs.dfs_fini.argtypes = []

    # int dfs_sys_connect(const char *pool, const char *sys, const char *cont,
    #                     int mflags, int sflags, dfs_attr_t *attr, dfs_sys_t **);
    _dfs.dfs_sys_connect.restype = ctypes.c_int
    _dfs.dfs_sys_connect.argtypes = [
        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    # int dfs_sys_disconnect(dfs_sys_t *);
    _dfs.dfs_sys_disconnect.restype = ctypes.c_int
    _dfs.dfs_sys_disconnect.argtypes = [ctypes.c_void_p]

    # int dfs_sys_open(dfs_sys_t *, const char *path, mode_t mode, int flags,
    #                  daos_oclass_id_t cid, daos_size_t chunk_size,
    #                  const char *value, dfs_obj_t **obj);
    _dfs.dfs_sys_open.restype = ctypes.c_int
    _dfs.dfs_sys_open.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_int,
        ctypes.c_uint, ctypes.c_ulonglong, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]

    # int dfs_sys_close(dfs_obj_t *obj);
    _dfs.dfs_sys_close.restype = ctypes.c_int
    _dfs.dfs_sys_close.argtypes = [ctypes.c_void_p]

    # int dfs_sys_read(dfs_sys_t *, dfs_obj_t *, void *buf, daos_off_t off,
    #                  daos_size_t *size, daos_event_t *ev);
    _dfs.dfs_sys_read.restype = ctypes.c_int
    _dfs.dfs_sys_read.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong,
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_void_p,
    ]

    # int dfs_sys_write(dfs_sys_t *, dfs_obj_t *, const void *buf, daos_off_t off,
    #                   daos_size_t *size, daos_event_t *ev);
    _dfs.dfs_sys_write.restype = ctypes.c_int
    _dfs.dfs_sys_write.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulonglong,
        ctypes.POINTER(ctypes.c_ulonglong), ctypes.c_void_p,
    ]

    # int dfs_sys_remove_type(dfs_sys_t *, const char *path, bool force,
    #                         mode_t mode, daos_obj_id_t *oid);
    # NOTE: plain dfs_sys_remove() returns ENOTSUP(95) on the DAOS 2.8/2.9
    # build in use; dfs_sys_remove_type() with mode=0 (skip type check) is the
    # reliable path (same call the Samba vfs_daos module uses).
    _dfs.dfs_sys_remove_type.restype = ctypes.c_int
    _dfs.dfs_sys_remove_type.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_bool, ctypes.c_uint,
        ctypes.c_void_p,
    ]

    # int dfs_sys_opendir(dfs_sys_t *, const char *dir, int flags, DIR **dirp);
    # int dfs_sys_readdir(dfs_sys_t *, DIR *dirp, struct dirent **dirent);
    # int dfs_sys_closedir(DIR *dirp);
    # readdir returns 0 and sets *dirent to NULL once the directory is drained.
    _dfs.dfs_sys_opendir.restype = ctypes.c_int
    _dfs.dfs_sys_opendir.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _dfs.dfs_sys_readdir.restype = ctypes.c_int
    _dfs.dfs_sys_readdir.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_Dirent)),
    ]
    _dfs.dfs_sys_closedir.restype = ctypes.c_int
    _dfs.dfs_sys_closedir.argtypes = [ctypes.c_void_p]


class DfsSys:
    """Thin object wrapper over a single mounted dfs_sys namespace.

    NOTE: libdaos client handles are process-global; call daos_init() once.
    This class does that on first connect and daos_fini() on process exit is
    left to the caller / interpreter teardown (DAOS tolerates skipping fini).
    """

    _daos_inited = False

    def __init__(self, pool: str, cont: str, sys: Optional[str] = None,
                 mflags: int = DFS_RDWR, sflags: int = DFS_SYS_NO_LOCK):
        _load()
        if not DfsSys._daos_inited:
            rc = _daos.daos_init()
            if rc != 0:
                raise DaosError("daos_init", rc)
            rc = _dfs.dfs_init()  # required before dfs_sys_connect (else EACCES)
            if rc != 0:
                raise DaosError("dfs_init", rc)
            DfsSys._daos_inited = True

        self._sys = ctypes.c_void_p()
        rc = _dfs.dfs_sys_connect(
            pool.encode(), sys.encode() if sys else None, cont.encode(),
            mflags, sflags, None, ctypes.byref(self._sys),
        )
        if rc != 0:
            raise DaosError("dfs_sys_connect", rc)

    def close(self) -> None:
        if self._sys and self._sys.value:
            _dfs.dfs_sys_disconnect(self._sys)
            self._sys = ctypes.c_void_p()

    # -- object I/O ---------------------------------------------------------
    def _open(self, path: str, flags: int, create: bool) -> ctypes.c_void_p:
        mode = _S_IFREG | 0o644 if create else 0
        obj = ctypes.c_void_p()
        rc = _dfs.dfs_sys_open(self._sys, path.encode(), mode, flags,
                               0, 0, None, ctypes.byref(obj))
        if rc != 0:
            raise DaosError(f"dfs_sys_open({path})", rc)
        return obj

    def write(self, path: str, data: bytes) -> None:
        obj = self._open(path, DFS_RDWR | os.O_CREAT, create=True)
        try:
            buf = ctypes.create_string_buffer(data, len(data))
            size = ctypes.c_ulonglong(len(data))
            rc = _dfs.dfs_sys_write(self._sys, obj, buf, 0,
                                    ctypes.byref(size), None)
            if rc != 0:
                raise DaosError(f"dfs_sys_write({path})", rc)
        finally:
            _dfs.dfs_sys_close(obj)

    def read(self, path: str, offset: int, length: int) -> bytes:
        """Read ``length`` bytes at ``offset``. Returns the bytes actually read."""
        obj = self._open(path, DFS_RDONLY, create=False)
        try:
            buf = ctypes.create_string_buffer(length)
            size = ctypes.c_ulonglong(length)  # in: capacity, out: bytes read
            rc = _dfs.dfs_sys_read(self._sys, obj, buf, offset,
                                   ctypes.byref(size), None)
            if rc != 0:
                raise DaosError(f"dfs_sys_read({path})", rc)
            return buf.raw[:size.value]
        finally:
            _dfs.dfs_sys_close(obj)

    def exists(self, path: str) -> bool:
        try:
            obj = self._open(path, DFS_RDONLY, create=False)
        except DaosError as e:
            if e.rc == 2:  # ENOENT
                return False
            raise
        _dfs.dfs_sys_close(obj)
        return True

    # -- enumeration --------------------------------------------------------
    def iterdir(self, path: str = "/"):
        """Yield entry names under ``path``.

        A generator on purpose: a KV container can hold hundreds of thousands of
        objects and callers that only need a count or a running total should not
        have to materialise the whole list.
        """
        dirp = ctypes.c_void_p()
        rc = _dfs.dfs_sys_opendir(self._sys, path.encode(), 0, ctypes.byref(dirp))
        if rc != 0:
            raise DaosError(f"dfs_sys_opendir({path})", rc)
        try:
            while True:
                ent = ctypes.POINTER(_Dirent)()
                rc = _dfs.dfs_sys_readdir(self._sys, dirp, ctypes.byref(ent))
                if rc != 0:
                    raise DaosError(f"dfs_sys_readdir({path})", rc)
                if not ent:                      # NULL => end of directory
                    break
                name = ent.contents.d_name.decode("utf-8", "replace")
                if name in (".", ".."):
                    continue
                yield name
        finally:
            _dfs.dfs_sys_closedir(dirp)

    def listdir(self, path: str = "/", limit: Optional[int] = None) -> list:
        out = []
        for name in self.iterdir(path):
            out.append(name)
            if limit is not None and len(out) >= limit:
                break
        return out

    def remove(self, path: str) -> bool:
        # mode=0 skips the type check; force=False (force=True yields ENOTSUP on
        # this DAOS build, and force only matters for non-empty directories,
        # which a KV blob store never creates).
        rc = _dfs.dfs_sys_remove_type(self._sys, path.encode(), False, 0, None)
        if rc == 2:  # ENOENT
            return False
        if rc != 0:
            raise DaosError(f"dfs_sys_remove_type({path})", rc)
        return True
