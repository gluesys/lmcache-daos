/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/*
 * daos_evshim -- keep sizeof(daos_event_t) on the C side.
 *
 * The Python binding needs to allocate daos_event_t objects, but the struct's
 * payload is marked "Internal use" in daos_event.h and its size is
 * architecture-dependent (the header itself notes the pthread_mutex_t padding
 * difference on __aarch64__). Hard-coding the layout in ctypes means a future
 * DAOS that grows the struct would write past our allocation -- silent heap
 * corruption.
 *
 * This shim removes the assumption entirely: the size comes from the same
 * header DAOS itself was compiled against. lmcache_daos/daos_event.py loads it
 * when available and falls back to a canary-verified ctypes layout otherwise,
 * so the shim is optional for a PoC and recommended for production.
 *
 * Build:
 *     gcc -O2 -fPIC -shared -o libdaos_evshim.so daos_evshim.c -ldaos
 * Use:
 *     export DAOS_EVSHIM_PATH=/path/to/libdaos_evshim.so
 * (or drop it anywhere on the loader path as libdaos_evshim.so)
 */

#include <stdlib.h>
#include <string.h>

#include <daos.h>
#include <daos_event.h>

/* Authoritative size -- this is the only function daos_event.py requires. */
size_t
daos_ev_size(void)
{
	return sizeof(daos_event_t);
}

/*
 * Optional allocation helpers. Python can allocate the bytes itself once it
 * knows the size, so these exist for callers that would rather not hold the
 * buffer on the Python side at all.
 */
daos_event_t *
daos_ev_alloc(void)
{
	return (daos_event_t *)calloc(1, sizeof(daos_event_t));
}

void
daos_ev_free(daos_event_t *ev)
{
	free(ev);
}

/* Read ev_error without the Python side knowing the field offset. */
int
daos_ev_error(const daos_event_t *ev)
{
	return ev == NULL ? -1 : ev->ev_error;
}
