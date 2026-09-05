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


# ---- gurt/types.h scatter-gather types -----------------------------------
# Needed only for the async path: dfs_read() takes a d_sg_list_t whose storage
# must outlive the operation, so the caller has to own it (see submit_read).
class _Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _Stat(ctypes.Structure):
    """glibc x86_64 ``struct stat`` (144 bytes). dfs_sys_stat fills st_mode,
    st_size, st_nlink, timestamps; only st_size / st_mode are read here."""
    _fields_ = [
        ("st_dev", ctypes.c_ulong), ("st_ino", ctypes.c_ulong),
        ("st_nlink", ctypes.c_ulong), ("st_mode", ctypes.c_uint),
        ("st_uid", ctypes.c_uint), ("st_gid", ctypes.c_uint),
        ("_pad0", ctypes.c_int), ("st_rdev", ctypes.c_ulong),
        ("st_size", ctypes.c_long), ("st_blksize", ctypes.c_long),
        ("st_blocks", ctypes.c_long), ("st_atim", _Timespec),
        ("st_mtim", _Timespec), ("st_ctim", _Timespec),
        ("_reserved", ctypes.c_long * 3),
    ]


class DIov(ctypes.Structure):
    """``d_iov_t`` — {void *iov_buf; size_t iov_buf_len; size_t iov_len;}"""

    _fields_ = [
        ("iov_buf", ctypes.c_void_p),
        ("iov_buf_len", ctypes.c_size_t),
        ("iov_len", ctypes.c_size_t),
    ]


class DSgList(ctypes.Structure):
    """``d_sg_list_t`` — {uint32 sg_nr; uint32 sg_nr_out; d_iov_t *sg_iovs;}"""

    _fields_ = [
        ("sg_nr", ctypes.c_uint32),
        ("sg_nr_out", ctypes.c_uint32),
        ("sg_iovs", ctypes.POINTER(DIov)),
    ]


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

    # int dfs_sys_stat(dfs_sys_t *, const char *path, int flags, struct stat *buf);
    _dfs.dfs_sys_stat.restype = ctypes.c_int
    _dfs.dfs_sys_stat.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(_Stat),
    ]
    # int dfs_sys_mkdir_p(dfs_sys_t *, const char *dir_path, mode_t mode,
    #                     daos_oclass_id_t cid);
    _dfs.dfs_sys_mkdir_p.restype = ctypes.c_int
    _dfs.dfs_sys_mkdir_p.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint,
    ]

    # -- async path: the base dfs API, not the dfs_sys wrapper ---------------
    # int dfs_sys2base(dfs_sys_t *dfs_sys, dfs_t **dfs);
    _dfs.dfs_sys2base.restype = ctypes.c_int
    _dfs.dfs_sys2base.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
    ]

    # int dfs_read(dfs_t *dfs, dfs_obj_t *obj, d_sg_list_t *sgl,
    #              daos_off_t off, daos_size_t *read_size, daos_event_t *ev);
    _dfs.dfs_read.restype = ctypes.c_int
    _dfs.dfs_read.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(DSgList),
        ctypes.c_ulonglong, ctypes.POINTER(ctypes.c_ulonglong),
        ctypes.c_void_p,
    ]


def libdaos() -> ctypes.CDLL:
    """Return the loaded ``libdaos`` handle (loading it if needed).

    Exposed for :mod:`lmcache_daos.daos_event`, which declares the event/event
    queue prototypes on the same handle instead of dlopen'ing a second copy.
    """
    _load()
    return _daos


class DfsSys:
    """Thin object wrapper over a single mounted dfs_sys namespace.

    NOTE: libdaos client handles are process-global; call daos_init() once.
    This class does that on first connect and daos_fini() on process exit is
    left to the caller / interpreter teardown (DAOS tolerates skipping fini).
    """

    _daos_inited = False

    # sflags defaults to 0 -- dfs_sys caching AND locking on.
    #
    # This used to pass DFS_SYS_NO_LOCK, which daos_fs_sys.h documents as
    # "Turn off locking. Useful for single-threaded applications." The connector
    # is emphatically not single-threaded: one DfsSys handle is shared across a
    # 16-thread pool, and the lock is what protects dfs_sys's internal directory
    # cache. Tests passing under that flag was absence of observed failure, not
    # safety. Caching is left on because every path here is "/<sha256>", so the
    # root directory entry is looked up on literally every operation.
    def __init__(self, pool: str, cont: str, sys: Optional[str] = None,
                 mflags: int = DFS_RDWR,
                 sflags: int = 0):
        # sflags=0 -> dfs_sys caching AND locking on, which is what
        # daos_fs_sys.h calls the default ("DFS_SYS_NO_LOCK ... useful for
        # single-threaded applications"). This used to default to
        # DFS_SYS_NO_CACHE | DFS_SYS_NO_LOCK.
        #
        # Measured before changing it (tests/bench_sflags_scaling.py, 28 MiB
        # objects — the real chunk size — 16 threads, best of 4):
        #
        #   sflags               handles      preopen   open+read
        #   NO_CACHE|NO_LOCK     perthread      32.54      34.78
        #   0 (cache+lock on)    perthread      33.64      33.13
        #   0 (cache+lock on)    shared         33.73      32.90
        #   NO_CACHE (lock on)   perthread      33.63      33.39
        #
        # All equal within noise, so the lock is free for bulk reads just as it
        # was for the 64 KiB metadata ops. Caching stays on because every path
        # is "/<sha256>", so the root directory entry is looked up on every
        # operation.
        #
        # Consequence for callers: the per-thread handle pool in connector.py
        # was introduced *only* to make NO_LOCK safe. With locking on it is no
        # longer required — it is retained for now (harmless, and gives each
        # thread its own directory cache) but is a candidate for removal.
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
        self._base = None  # lazily resolved dfs_t* (async path); see base()
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
    def _open(self, path: str, flags: int, create: bool,
              cid: int = 0) -> ctypes.c_void_p:
        """``cid`` is the DAOS object class for a file created here (0 = the
        container default); it is ignored when the file already exists."""
        mode = _S_IFREG | 0o644 if create else 0
        obj = ctypes.c_void_p()
        rc = _dfs.dfs_sys_open(self._sys, path.encode(), mode, flags,
                               cid, 0, None, ctypes.byref(obj))
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

    def open_rdwr_create(self, path: str, oclass: int = 0) -> ctypes.c_void_p:
        return self._open(path, DFS_RDWR | os.O_CREAT, create=True, cid=oclass)

    def write_obj_from(self, obj: ctypes.c_void_p, offset: int, length: int,
                       src) -> int:
        """Write ``length`` bytes from a caller-provided buffer at ``offset``.

        ``src`` is a ctypes array/pointer (e.g. ``(c_char*n).from_buffer(mv)``
        aliasing a MemoryObj) so no copy is made. Mirrors read_obj_into: the
        ctypes call releases the GIL for the duration of the write.
        """
        size = ctypes.c_ulonglong(length)
        rc = _dfs.dfs_sys_write(self._sys, obj, src, offset,
                                ctypes.byref(size), None)
        if rc != 0:
            raise DaosError("dfs_sys_write", rc)
        return size.value

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

    # -- low-level single-open API (avoids re-open per read; enables reading a
    #    blob's prefix/meta/payload with ONE open, and reading the large payload
    #    straight into the caller's destination buffer with no intermediate copy) -
    def open_rdonly(self, path: str) -> ctypes.c_void_p:
        return self._open(path, DFS_RDONLY, create=False)

    def close_obj(self, obj: ctypes.c_void_p) -> None:
        _dfs.dfs_sys_close(obj)

    def read_obj(self, obj: ctypes.c_void_p, offset: int, length: int) -> bytes:
        """Read ``length`` bytes from an already-open object into fresh bytes."""
        buf = ctypes.create_string_buffer(length)
        size = ctypes.c_ulonglong(length)
        rc = _dfs.dfs_sys_read(self._sys, obj, buf, offset,
                               ctypes.byref(size), None)
        if rc != 0:
            raise DaosError("dfs_sys_read", rc)
        return buf.raw[:size.value]

    def read_obj_into(self, obj: ctypes.c_void_p, offset: int, length: int,
                      dest) -> int:
        """Read ``length`` bytes into a caller-provided writable buffer.

        ``dest`` is a ctypes array/pointer whose capacity is >= ``length``
        (typically ``(c_char * n).from_buffer(memoryview)`` aliasing the target
        MemoryObj buffer -- no copy). ``dfs_sys_read`` is a ctypes C call, so it
        releases the GIL for its duration; concurrent ``read_obj_into`` calls on
        distinct objects/buffers therefore run truly in parallel instead of
        serializing on the interpreter lock. Returns bytes actually read.
        """
        size = ctypes.c_ulonglong(length)
        rc = _dfs.dfs_sys_read(self._sys, obj, dest, offset,
                               ctypes.byref(size), None)
        if rc != 0:
            raise DaosError("dfs_sys_read", rc)
        return size.value

    # -- async read ---------------------------------------------------------
    def base(self) -> ctypes.c_void_p:
        """The underlying ``dfs_t *`` for this ``dfs_sys_t`` (cached).

        ``daos_fs_sys.h`` explicitly sanctions this: *"the DFS API can be used
        directly by getting the DFS Object with dfs_sys2base()"*.
        """
        if self._base is None:
            h = ctypes.c_void_p()
            rc = _dfs.dfs_sys2base(self._sys, ctypes.byref(h))
            if rc != 0:
                raise DaosError("dfs_sys2base", rc)
            self._base = h
        return self._base

    def submit_read(self, obj: ctypes.c_void_p, offset: int, sgl: DSgList,
                    size, ev_addr: int) -> None:
        """Submit an asynchronous read; completion arrives via the event queue.

        Uses ``dfs_read()`` directly rather than ``dfs_sys_read()``. That is not
        a style preference — **``dfs_sys_read()`` cannot be used asynchronously
        at all.** Its implementation (DAOS 2.8 ``src/client/dfs/dfs_sys.c``)
        builds the scatter-gather list on its own stack::

            d_iov_t     iov;
            d_sg_list_t sgl;
            d_iov_set(&iov, buf, *size);
            sgl.sg_nr = 1; sgl.sg_iovs = &iov; sgl.sg_nr_out = 1;
            return dfs_read(dfs_sys->dfs, obj, &sgl, off, size, ev);

        With ``ev != NULL`` that call returns immediately and the frame dies,
        but DAOS's completion callback still dereferences the caller's sgl
        (``dc_array.c:check_short_read_cb`` does ``D_ASSERT(args->sgl->sg_nr ==
        1)``). Passing an event to ``dfs_sys_read`` therefore aborts the process
        on a dangling stack read — observed as::

            check_short_read_cb() Assertion 'args->sgl->sg_nr == 1' failed

        Calling ``dfs_read`` with a caller-owned ``sgl`` is the fix.

        **Lifetime contract — the caller must keep ALL of these alive until the
        event is harvested:** ``sgl``, the ``d_iov_t`` it points at, ``size``
        (the ``daos_size_t *`` out-param), the destination buffer, the
        ``dfs_obj_t`` handle, and the event itself.
        :class:`~lmcache_daos.daos_event.AsyncRead` exists to own exactly that
        set; dropping any member early is a use-after-free DAOS writes into.

        A non-zero return means *submission* failed. An operation that was
        submitted and then failed reports through the event's ``ev_error``.
        """
        rc = _dfs.dfs_read(self.base(), obj, ctypes.byref(sgl), offset,
                           ctypes.byref(size), ctypes.c_void_p(ev_addr))
        if rc != 0:
            raise DaosError("dfs_read(async submit)", rc)

    def exists(self, path: str) -> bool:
        try:
            obj = self._open(path, DFS_RDONLY, create=False)
        except DaosError as e:
            if e.rc == 2:  # ENOENT
                return False
            raise
        _dfs.dfs_sys_close(obj)
        return True

    def stat_size(self, path: str) -> Optional[int]:
        """``st_size`` of ``path``, or ``None`` if it does not exist.

        One metadata RPC instead of the open/close pair ``exists()`` costs, and
        it gives the length, which is what the MP L2 adapter needs to decide
        whether a stored object is complete before advertising it as a hit.
        """
        st = _Stat()
        rc = _dfs.dfs_sys_stat(self._sys, path.encode(), 0, ctypes.byref(st))
        if rc == 2:  # ENOENT
            return None
        if rc != 0:
            raise DaosError(f"dfs_sys_stat({path})", rc)
        return int(st.st_size)

    def mkdir_p(self, path: str) -> None:
        """Create ``path`` and any missing parents (EEXIST is not an error)."""
        rc = _dfs.dfs_sys_mkdir_p(self._sys, path.encode(), 0o755, 0)
        if rc not in (0, 17):  # EEXIST
            raise DaosError(f"dfs_sys_mkdir_p({path})", rc)

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
