/*
 * SPDX-License-Identifier: Apache-2.0
 * Copyright 2026 Gluesys Co., Ltd.
 */
/*
 * NIXL backend for DAOS.
 *
 * Storage model: the raw object API, not DFS. Measured on the testbed
 * (doc/LAYERWISE-MEASUREMENT.md), the per-object cost is
 *     DFS         0.63   ms + 0.067  ms/MiB
 *     object API  0.0137 ms + 0.0385 ms/MiB
 * so DFS spends ~90% of a 1 MiB read on overhead dkey/akey does not pay. The
 * object API also folds a whole descriptor list into one daos_obj_fetch() via
 * its iod array -- exactly the shape prepXfer() hands us, and something a
 * file-per-object model cannot express.
 *
 * Descriptor mapping. nixlBasicDesc carries only addr/len/devId, and
 * nixlBlobDesc adds metaInfo:
 *     metaInfo -> "pool/container[/oid_hi.oid_lo]"   what to open
 *     devId    -> caller's key for the handle we hand back
 *     addr     -> offset within the object; with the layer layout the akey
 *                 index is derived from it
 *     len      -> bytes
 *
 * dkey/akey split. A descriptor gives one offset, and DAOS wants two levels of
 * key, so the offset is cut at a span:
 *     dkey = addr / dkeySpan        akey = addr % dkeySpan
 * dkey decides placement, so descriptors inside one span land on one target and
 * fold into a single RPC, while separate spans spread across targets. Both
 * halves of that matter, and the benchmark says why: with 40 akeys under one
 * dkey a 4.69 GiB read took 204 ms, with one akey per RPC 245 ms, and with the
 * whole 40 MiB as a single akey extent 571 ms. Folding wins, but only while the
 * data still arrives as separate akeys rather than one long extent.
 */
#ifndef NIXL_SRC_PLUGINS_DAOS_DAOS_BACKEND_H
#define NIXL_SRC_PLUGINS_DAOS_DAOS_BACKEND_H

#include <cstdint>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <daos.h>

#include "backend/backend_engine.h"

/*
 * What registerMem() hands back. Owns nothing by itself: the pool and
 * container handles are shared between every object registered against the
 * same "pool/container", so they are refcounted by the engine and only closed
 * when the last object under them goes.
 */
class nixlDaosObjMD : public nixlBackendMD {
public:
    nixlDaosObjMD(const std::string &pool,
                  const std::string &cont,
                  daos_handle_t oh,
                  daos_obj_id_t oid,
                  uint64_t dev_id)
        : nixlBackendMD(true),
          pool_(pool),
          cont_(cont),
          oh_(oh),
          oid_(oid),
          devId_(dev_id) {}

    ~nixlDaosObjMD() override = default;

    std::string pool_;
    std::string cont_;
    daos_handle_t oh_;
    daos_obj_id_t oid_;
    uint64_t devId_;
};

/*
 * One group of descriptors that share an object and a dkey, and therefore
 * become one daos_obj_fetch()/daos_obj_update() call. The vectors own the
 * memory that the DAOS structures point into, so they are sized once in
 * prepXfer() and never grown afterwards -- a reallocation here would leave
 * iod_recxs and sg_iovs dangling.
 */
struct nixlDaosIoGroup {
    daos_handle_t oh{};
    uint64_t dkeyVal = 0;
    std::vector<uint64_t> akeyVals;
    std::vector<daos_iod_t> iods;
    std::vector<daos_recx_t> recxs;
    std::vector<d_sg_list_t> sgls;
    std::vector<d_iov_t> iovs;
};

class nixlDaosBackendReqH : public nixlBackendReqH {
public:
    nixlDaosBackendReqH() = default;
    ~nixlDaosBackendReqH() override = default;

    nixl_xfer_op_t op = NIXL_READ;
    std::vector<nixlDaosIoGroup> groups;

    /* Set once postXfer() hands the work to a thread. Absent means prepared
     * but not posted, which checkXfer() reports as still in progress. */
    std::future<nixl_status_t> fut;
    bool posted = false;
};

class nixlDaosEngine : public nixlBackendEngine {
public:
    static nixl_b_params_t
    getPluginParams();

    explicit nixlDaosEngine(const nixlBackendInitParams *init_params);
    ~nixlDaosEngine() override;

    /* ---- capability declaration (final) ---------------------------------
     * DAOS is reached through its own client, so NIXL does not move bytes
     * between agents on our behalf: local only, no notification channel.
     * These are the same answers POSIX and GDS give.
     *
     * VRAM_SEG is deliberately absent. daos_obj_fetch_gpu() exists in the
     * theodore/b_cufile client and is the reason to add it later, but the
     * development host has no nvidia_fs loaded, so claiming it here would
     * advertise a path that cannot yet be exercised.
     */
    bool
    supportsRemote() const override {
        return false;
    }

    bool
    supportsLocal() const override {
        return true;
    }

    bool
    supportsNotif() const override {
        return false;
    }

    nixl_mem_list_t
    getSupportedMems() const override {
        return {FILE_SEG, DRAM_SEG};
    }

    /* ---- lifecycle no-ops (final) ---------------------------------------
     * There is no peer to connect to and no metadata to exchange: the DAOS
     * client library owns the connection to the pool.
     */
    nixl_status_t
    connect(const std::string &remote_agent) override {
        return NIXL_SUCCESS;
    }

    nixl_status_t
    disconnect(const std::string &remote_agent) override {
        return NIXL_SUCCESS;
    }

    nixl_status_t
    unloadMD(nixlBackendMD *input) override {
        return NIXL_SUCCESS;
    }

    /* ---- implemented ----------------------------------------------------- */
    nixl_status_t
    registerMem(const nixlBlobDesc &mem,
                const nixl_mem_t &nixl_mem,
                nixlBackendMD *&out) override;

    nixl_status_t
    deregisterMem(nixlBackendMD *meta) override;

    /* ---- transfer --------------------------------------------------------- */
    nixl_status_t
    prepXfer(const nixl_xfer_op_t &operation,
             const nixl_meta_dlist_t &local,
             const nixl_meta_dlist_t &remote,
             const std::string &remote_agent,
             nixlBackendReqH *&handle,
             const nixl_opt_b_args_t *opt_args = nullptr) const override;

    nixl_status_t
    postXfer(const nixl_xfer_op_t &operation,
             const nixl_meta_dlist_t &local,
             const nixl_meta_dlist_t &remote,
             const std::string &remote_agent,
             nixlBackendReqH *&handle,
             const nixl_opt_b_args_t *opt_args = nullptr) const override;

    nixl_status_t
    checkXfer(nixlBackendReqH *handle) const override;

    nixl_status_t
    releaseReqH(nixlBackendReqH *handle) const override;

private:
    /*
     * One entry per "pool/container" actually opened. Refcounted because a
     * deployment registers many objects against one container and closing the
     * container under a live object handle is a use-after-free on the DAOS
     * side, not a tidy error.
     */
    struct contHandles {
        daos_handle_t poh;
        daos_handle_t coh;
        int refs;
    };

    nixl_status_t
    getCont(const std::string &pool, const std::string &cont, contHandles *&out);
    void
    putCont(const std::string &pool, const std::string &cont);

    mutable std::mutex mtx_;
    std::map<std::string, contHandles> conts_;
    bool daosInited_ = false;

    /* Offset span that maps to one dkey. 64 MiB by default: large enough that a
     * request's descriptors usually share a dkey and fold into one RPC, small
     * enough that a big object still spreads over targets. Not yet a plugin
     * parameter; it should become one as soon as there is a second workload to
     * tune it against. */
    uint64_t dkeySpan_ = 64ull << 20;
};

#endif // NIXL_SRC_PLUGINS_DAOS_DAOS_BACKEND_H
