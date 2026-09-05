/* Minimal check: can the NIC register CUDA device memory via a dma-buf FD?
 * This is the exact operation UCX's ucp_mem_map() performs for GPU buffers,
 * isolated from DAOS, Mercury and UCX so the failure has one possible source.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <infiniband/verbs.h>

typedef unsigned long long CUdeviceptr;
typedef int CUresult, CUdevice;
typedef void *CUcontext;
extern CUresult cuInit(unsigned int);
extern CUresult cuDeviceGet(CUdevice *, int);
extern CUresult cuCtxCreate_v2(CUcontext *, unsigned int, CUdevice);
extern CUresult cuMemAlloc_v2(CUdeviceptr *, size_t);
extern CUresult cuMemGetHandleForAddressRange(void *, CUdeviceptr, size_t, int,
					      unsigned long long);
extern CUresult cuGetErrorString(CUresult, const char **);
#define CU_MEM_RANGE_HANDLE_TYPE_DMA_BUF_FD 1

int main(int argc, char **argv)
{
	const char *want = argc > 1 ? argv[1] : "mlx5_0";
	size_t      size = argc > 2 ? (size_t)atol(argv[2]) : (1UL << 20);
	CUdevice    dev;
	CUcontext   ctx;
	CUdeviceptr dptr = 0;
	int         fd = -1, rc;
	const char *es = NULL;

	if ((rc = cuInit(0)) != 0) { printf("cuInit = %d\n", rc); return 2; }
	if ((rc = cuDeviceGet(&dev, 0)) != 0) { printf("cuDeviceGet = %d\n", rc); return 2; }
	if ((rc = cuCtxCreate_v2(&ctx, 0, dev)) != 0) { printf("cuCtxCreate = %d\n", rc); return 2; }
	if ((rc = cuMemAlloc_v2(&dptr, size)) != 0) { printf("cuMemAlloc = %d\n", rc); return 2; }
	printf("GPU buffer: %zu bytes at 0x%llx\n", size, dptr);

	rc = cuMemGetHandleForAddressRange(&fd, dptr, size,
					   CU_MEM_RANGE_HANDLE_TYPE_DMA_BUF_FD, 0);
	if (rc != 0) {
		cuGetErrorString(rc, &es);
		printf("1) dma-buf export      : FAIL (CUresult %d: %s)\n", rc, es ? es : "?");
		return 3;
	}
	printf("1) dma-buf export      : OK (fd=%d)\n", fd);

	struct ibv_device **list = ibv_get_device_list(NULL);
	struct ibv_device  *pick = NULL;
	if (!list) { printf("ibv_get_device_list failed\n"); return 4; }
	for (int i = 0; list[i]; i++)
		if (!strcmp(ibv_get_device_name(list[i]), want)) pick = list[i];
	if (!pick) { printf("device %s not found\n", want); return 4; }

	struct ibv_context *ictx = ibv_open_device(pick);
	if (!ictx) { printf("ibv_open_device failed: %s\n", strerror(errno)); return 4; }
	struct ibv_pd *pd = ibv_alloc_pd(ictx);
	if (!pd) { printf("ibv_alloc_pd failed: %s\n", strerror(errno)); return 4; }
	printf("2) ibv device %-9s: opened\n", want);

	int acc = IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_READ |
		  IBV_ACCESS_REMOTE_WRITE;
	errno = 0;
	struct ibv_mr *mr = ibv_reg_dmabuf_mr(pd, 0, size, 0, fd, acc);
	if (!mr) {
		printf("3) ibv_reg_dmabuf_mr   : FAIL errno=%d (%s)\n",
		       errno, strerror(errno));
		printf("\nVERDICT: the NIC cannot register this GPU buffer -- "
		       "GPUDirect RDMA is unavailable on this path.\n");
		return 5;
	}
	printf("3) ibv_reg_dmabuf_mr   : OK (lkey=0x%x rkey=0x%x)\n",
	       mr->lkey, mr->rkey);
	printf("\nVERDICT: GPUDirect RDMA registration works.\n");
	ibv_dereg_mr(mr);
	return 0;
}
