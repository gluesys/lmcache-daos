/* SPDX-License-Identifier: Apache-2.0 */
/* Copyright 2026 Gluesys Co., Ltd. */
/* GPU-direct round-trip gate for DAOS dfs_write_gpu()/dfs_read_gpu().
 *
 * Writes a pattern from CUDA device memory into a DFS file, reads it back into
 * a *different* device buffer, and compares. The comparison copy to the host is
 * only for verification -- the I/O itself never touches a host payload buffer,
 * which is the property being tested.
 *
 * CUDA driver API is declared inline so the test builds with plain gcc and does
 * not require the CUDA toolkit headers on the client.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <daos.h>
#include <daos_fs.h>

typedef unsigned long long CUdeviceptr;
typedef int  CUresult;
typedef int  CUdevice;
typedef void *CUcontext;
extern CUresult cuInit(unsigned int);
extern CUresult cuDeviceGet(CUdevice *, int);
extern CUresult cuCtxCreate_v2(CUcontext *, unsigned int, CUdevice);
extern CUresult cuMemAlloc_v2(CUdeviceptr *, size_t);
extern CUresult cuMemFree_v2(CUdeviceptr);
extern CUresult cuMemcpyHtoD_v2(CUdeviceptr, const void *, size_t);
extern CUresult cuMemcpyDtoH_v2(void *, CUdeviceptr, size_t);
extern CUresult cuMemsetD8_v2(CUdeviceptr, unsigned char, size_t);

#define CU(call) do { CUresult _r = (call); if (_r != 0) {                     \
	fprintf(stderr, "CUDA fail %s = %d\n", #call, _r); exit(2); } } while (0)
#define DC(call) do { int _r = (call); if (_r != 0) {                          \
	fprintf(stderr, "DAOS fail %s = %d (%s)\n", #call, _r, strerror(_r > 0 ? _r : -_r)); \
	exit(3); } } while (0)

static int one_size(dfs_t *dfs, const char *name, size_t size)
{
	dfs_obj_t      *obj = NULL;
	CUdeviceptr     src = 0, dst = 0;
	d_sg_list_t     sgl;
	d_iov_t         iov;
	daos_mem_attr_t ma;
	daos_size_t     got = 0;
	unsigned char  *h_in, *h_out;
	int             rc, bad = 0;

	h_in  = malloc(size);
	h_out = malloc(size);
	if (!h_in || !h_out) { fprintf(stderr, "host alloc\n"); exit(2); }
	for (size_t i = 0; i < size; i++)
		h_in[i] = (unsigned char)((i * 31u + size) & 0xff);

	CU(cuMemAlloc_v2(&src, size));
	CU(cuMemAlloc_v2(&dst, size));
	CU(cuMemcpyHtoD_v2(src, h_in, size));
	CU(cuMemsetD8_v2(dst, 0xAA, size));	/* poison so a no-op read is visible */

	rc = dfs_open(dfs, NULL, name, S_IFREG | 0644,
		      O_CREAT | O_RDWR | O_TRUNC, 0, 0, NULL, &obj);
	if (rc) { fprintf(stderr, "dfs_open(%s) = %d\n", name, rc); exit(3); }

	memset(&ma, 0, sizeof(ma));
	ma.ma_mem_type  = DAOS_MEM_TYPE_CUDA;
	ma.ma_device_id = 0;

	/* write straight out of device memory */
	sgl.sg_nr = 1; sgl.sg_nr_out = 0; sgl.sg_iovs = &iov;
	d_iov_set(&iov, (void *)(uintptr_t)src, size);
	rc = dfs_write_gpu(dfs, obj, &sgl, 0, &ma);
	if (rc) { fprintf(stderr, "dfs_write_gpu = %d\n", rc); exit(3); }

	/* read straight into a different device buffer */
	sgl.sg_nr = 1; sgl.sg_nr_out = 0; sgl.sg_iovs = &iov;
	d_iov_set(&iov, (void *)(uintptr_t)dst, size);
	rc = dfs_read_gpu(dfs, obj, &sgl, 0, &got, &ma);
	if (rc) { fprintf(stderr, "dfs_read_gpu = %d\n", rc); exit(3); }

	CU(cuMemcpyDtoH_v2(h_out, dst, size));
	if (got != size) { printf("  short read: %zu of %zu\n", (size_t)got, size); bad = 1; }
	for (size_t i = 0; i < size; i++)
		if (h_in[i] != h_out[i]) {
			printf("  mismatch at %zu: %02x != %02x\n", i, h_in[i], h_out[i]);
			bad = 1;
			break;
		}
	printf("  %-10zu bytes  read=%-10zu  %s\n", size, (size_t)got,
	       bad ? "FAIL" : "OK");

	dfs_release(obj);
	CU(cuMemFree_v2(src));
	CU(cuMemFree_v2(dst));
	free(h_in); free(h_out);
	return bad;
}

int main(int argc, char **argv)
{
	const char   *pool = argc > 1 ? argv[1] : "gdspool";
	const char   *cont = argc > 2 ? argv[2] : "kvgds";
	daos_handle_t poh, coh;
	dfs_t        *dfs = NULL;
	CUdevice      dev;
	CUcontext     ctx;
	int           fails = 0;

	CU(cuInit(0));
	CU(cuDeviceGet(&dev, 0));
	CU(cuCtxCreate_v2(&ctx, 0, dev));

	DC(daos_init());
	DC(daos_pool_connect(pool, NULL, DAOS_PC_RW, &poh, NULL, NULL));
	DC(daos_cont_open(poh, cont, DAOS_COO_RW, &coh, NULL, NULL));
	DC(dfs_mount(poh, coh, O_RDWR, &dfs));

	printf("GPU-direct DFS round-trip (pool=%s cont=%s)\n", pool, cont);
	size_t sizes[] = { 4096, 65536, 1048576, 33554432 };
	for (unsigned i = 0; i < sizeof(sizes) / sizeof(sizes[0]); i++) {
		char nm[64];
		snprintf(nm, sizeof(nm), "gpu_rt_%zu", sizes[i]);
		fails += one_size(dfs, nm, sizes[i]);
	}

	dfs_umount(dfs);
	daos_cont_close(coh, NULL);
	daos_pool_disconnect(poh, NULL);
	daos_fini();
	printf("%s\n", fails ? "RESULT: FAIL" : "RESULT: ALL OK");
	return fails ? 1 : 0;
}
