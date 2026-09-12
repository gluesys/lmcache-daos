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

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
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

/*
 * A fixed pool of worker threads.
 *
 * The first implementation spawned a thread per posted request. Measured on
 * the testbed at 400G, that cost about 0.096 ms per layer -- roughly seven
 * times a DAOS RPC's own fixed cost of 0.0137 ms -- and it is why the
 * unfolded arm of the benchmark collapsed to 8.06 GB/s against 31.16 GB/s
 * folded. Folding hid the cost because it left only 120 requests; nothing
 * guarantees a real workload folds that well, so the threads had to go.
 *
 * Blocking DAOS calls on a pool, rather than a DAOS event queue: the event
 * path serialises on the per-EQ eqx_lock and has been measured to cap around
 * 7-12 GB/s however the queues are arranged.
 */
class nixlDaosThreadPool {
public:
    explicit nixlDaosThreadPool(unsigned n);
    ~nixlDaosThreadPool();

    nixlDaosThreadPool(const nixlDaosThreadPool &) = delete;
    void
    operator=(const nixlDaosThreadPool &) = delete;

    void
    submit(std::function<void()> job);

private:
    std::vector<std::thread> workers_;
    std::queue<std::function<void()>> q_;
    std::mutex m_;
    std::condition_variable cv_;
    bool stop_ = false;
};

class nixlDaosBackendReqH : public nixlBackendReqH {
public:
    nixlDaosBackendReqH() = default;
    ~nixlDaosBackendReqH() override = default;

    nixl_xfer_op_t op = NIXL_READ;
    std::vector<nixlDaosIoGroup> groups;

    /* One outstanding count per request; workers decrement as groups finish.
     * The first non-success wins, so a later group cannot mask an earlier
     * failure. */
    std::atomic<unsigned> pending{0};
    std::atomic<int> status{NIXL_SUCCESS};
    bool posted = false;

    void
    finishOne() {
        if (pending.fetch_sub(1) == 1) {
            /* Taking the lock before notifying is what makes waitDone() safe:
             * a waiter that has already evaluated the predicate is inside
             * wait() holding nothing, and this hands off cleanly. */
            std::lock_guard<std::mutex> g(doneMtx);
            doneCv.notify_all();
        }
    }

    void
    waitDone() {
        std::unique_lock<std::mutex> g(doneMtx);
        doneCv.wait(g, [this] { return pending.load() == 0; });
    }

    void
    recordError(nixl_status_t st) {
        int expected = NIXL_SUCCESS;
        status.compare_exchange_strong(expected, static_cast<int>(st));
    }

private:
    std::mutex doneMtx;
    std::condition_variable doneCv;
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

    /* Required whenever supportsLocal() is true, and the base class returns
     * an error rather than a default -- a backend that declares local support
     * and skips this fails every registerMem() the agent makes, which is what
     * happened here. For a loopback transfer the "remote" metadata is the same
     * object, so the handle passes straight through. POSIX and GDS do the
     * same. Nothing exercises it when the backend is driven directly, which is
     * why it survived until the first agent-level test. */
    nixl_status_t
    loadLocalMD(nixlBackendMD *input, nixlBackendMD *&output) override {
        output = input;
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

    /* Worker threads. Mutable because postXfer() is const: the interface
     * treats a transfer as not mutating the engine, which is true of every
     * field except this one. Sized from NIXL_DAOS_THREADS, default 64, which
     * is where throughput stops improving on the testbed -- see
     * doc/NIXL-DAOS-MEASUREMENT.md. */
    mutable std::unique_ptr<nixlDaosThreadPool> pool_;

    /* Offset span that maps to one dkey. 64 MiB by default: large enough that a
     * request's descriptors usually share a dkey and fold into one RPC, small
     * enough that a big object still spreads over targets. Not yet a plugin
     * parameter; it should become one as soon as there is a second workload to
     * tune it against. */
    uint64_t dkeySpan_ = 64ull << 20;
};

#endif // NIXL_SRC_PLUGINS_DAOS_DAOS_BACKEND_H
