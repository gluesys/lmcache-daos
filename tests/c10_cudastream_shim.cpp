/* Two-symbol ABI shim that lets LMCache's prebuilt c_ops load against a torch
 * it was not built for. MEASUREMENT ONLY -- see the warning at the bottom.
 *
 * Why this is needed. The kvsup:052 image pairs vLLM/torch 2.10.0+cu128 with
 * the official LMCache 0.5.2 wheel, whose c_ops extension is built against
 * CUDA 13 and a torch that exports two CUDAStream methods out-of-line:
 *
 *     _ZNK3c104cuda10CUDAStream5queryEv         c10::cuda::CUDAStream::query()
 *     _ZNK3c104cuda10CUDAStream11synchronizeEv  ...::synchronize()
 *
 * In the deployed torch both are header-inline, so libc10_cuda.so does not
 * export them and dlopen of c_ops fails with an undefined symbol. LMCache
 * catches that failure and silently substitutes python_ops_fallback.py, so the
 * fused CUDA gather/scatter kernels never run and nothing in the logs says the
 * slow path is in use beyond one WARNING at startup. Every KV transfer number
 * measured on this image before this shim existed was a Python fallback
 * number.
 *
 * Checked before writing this: of the 47 c10 symbols c_ops imports, these are
 * the only two the deployed torch lacks, and every LMCache 0.5.x wheel (0.5.0,
 * 0.5.1, 0.5.2, 0.5.3) needs libcudart.so.13 and the same two symbols -- so no
 * published wheel matches this image, and downgrading does not help.
 *
 * Why redefining them here is legitimate: a C++ member function's mangled name
 * is determined by namespace, class name, method name and cv-qualification --
 * not by the class's member layout. Declaring a minimal CUDAStream with the
 * same names therefore emits exactly the two symbols above, and the call to
 * stream() binds to the real _ZNK3c104cuda10CUDAStream6streamEv that
 * libc10_cuda.so does export. No member of the real class is touched.
 *
 * build (inside the container, so libstdc++ matches):
 *   g++ -shared -fPIC -O2 -o libc10cuda_shim.so c10_cudastream_shim.cpp \
 *       -I/usr/local/cuda/include -L/usr/local/cuda/lib64 -lcudart
 * use:
 *   -e LD_PRELOAD=/shim/libc10cuda_shim.so
 *
 * WARNING -- do not ship this. It leaves two CUDA runtimes live in one process
 * (cudart 13 for c_ops, cudart 12 for torch and for this shim), and this
 * synchronize() omits the device-guard and error-check work the real one does.
 * It exists to measure what the fused kernels are worth, so that aligning the
 * image's torch and LMCache builds -- the actual fix -- can be justified or
 * dropped on evidence. Any run using it MUST be gated by
 * kv_correctness_gate.sh, because a subtly wrong KV tensor keeps generation
 * fluent and logs nothing.
 */
#include <cuda_runtime.h>

namespace c10 {
namespace cuda {

class CUDAStream {
public:
	/* Defined in libc10_cuda.so; declared here only so the definitions
	 * below can call it. */
	cudaStream_t stream() const;

	bool query() const;
	void synchronize() const;
};

bool CUDAStream::query() const
{
	return cudaStreamQuery(stream()) == cudaSuccess;
}

void CUDAStream::synchronize() const
{
	cudaStreamSynchronize(stream());
}

} /* namespace cuda */
} /* namespace c10 */
