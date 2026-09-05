/* Reproduce ucp_mem_map() on CUDA device memory outside DAOS, so UCX's own
 * logging is not swallowed by CaRT's log handler. This is the exact call
 * Mercury's na_ucx_mem_register() makes for a GPU bulk buffer.
 */
#include <stdio.h>
#include <stdlib.h>
#include <ucp/api/ucp.h>

typedef unsigned long long CUdeviceptr;
typedef int CUresult, CUdevice;
typedef void *CUcontext;
extern CUresult cuInit(unsigned int);
extern CUresult cuDeviceGet(CUdevice *, int);
extern CUresult cuCtxCreate_v2(CUcontext *, unsigned int, CUdevice);
extern CUresult cuMemAlloc_v2(CUdeviceptr *, size_t);

int main(int argc, char **argv)
{
	size_t             size = argc > 1 ? (size_t)atol(argv[1]) : (1UL << 20);
	ucp_config_t      *cfg;
	ucp_params_t       params;
	ucp_context_h      ctx;
	ucp_mem_map_params_t mp;
	ucp_mem_h          memh;
	ucs_status_t       st;
	CUdevice           dev;
	CUcontext          cuctx;
	CUdeviceptr        dptr = 0;
	int                rc;

	if ((rc = cuInit(0)) || (rc = cuDeviceGet(&dev, 0)) ||
	    (rc = cuCtxCreate_v2(&cuctx, 0, dev)) || (rc = cuMemAlloc_v2(&dptr, size))) {
		printf("CUDA setup failed rc=%d\n", rc);
		return 2;
	}
	printf("GPU buffer 0x%llx size %zu\n", dptr, size);

	st = ucp_config_read(NULL, NULL, &cfg);
	if (st != UCS_OK) { printf("config_read: %s\n", ucs_status_string(st)); return 3; }

	memset(&params, 0, sizeof(params));
	params.field_mask = UCP_PARAM_FIELD_FEATURES;
	params.features   = UCP_FEATURE_RMA | UCP_FEATURE_TAG;
	st = ucp_init(&params, cfg, &ctx);
	ucp_config_release(cfg);
	if (st != UCS_OK) { printf("ucp_init: %s\n", ucs_status_string(st)); return 3; }

	memset(&mp, 0, sizeof(mp));
	mp.field_mask = UCP_MEM_MAP_PARAM_FIELD_ADDRESS |
			UCP_MEM_MAP_PARAM_FIELD_LENGTH |
			UCP_MEM_MAP_PARAM_FIELD_MEMORY_TYPE;
	mp.address     = (void *)(uintptr_t)dptr;
	mp.length      = size;
	mp.memory_type = UCS_MEMORY_TYPE_CUDA;

	st = ucp_mem_map(ctx, &mp, &memh);
	printf("\nucp_mem_map(CUDA) = %s\n", ucs_status_string(st));
	if (st == UCS_OK) {
		ucp_mem_unmap(ctx, memh);
		printf("VERDICT: CUDA registration through UCX works.\n");
	} else {
		printf("VERDICT: CUDA registration through UCX fails.\n");
	}
	ucp_cleanup(ctx);
	return st == UCS_OK ? 0 : 1;
}
