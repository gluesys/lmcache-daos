FROM kvsup-ucx:local
ENV CUDA_HOME=/usr/local/cuda-12.9
ENV PATH=/usr/local/cuda-12.9/bin:$PATH
ENV TORCH_CUDA_ARCH_LIST=9.0
ENV MAX_JOBS=32
RUN pip install -q ninja packaging setuptools wheel cmake
RUN pip install -v --no-build-isolation --no-deps --force-reinstall --no-binary :all: lmcache==0.5.2 \
      > /tmp/lmc_build.log 2>&1 || (tail -60 /tmp/lmc_build.log && false)
RUN python3 -c "import lmcache.v1.platform" 2>&1 | tee /tmp/lmc_verify.log; \
    grep -qiE "Failed to import backend" /tmp/lmc_verify.log && (echo "C_OPS STILL BROKEN"; cat /tmp/lmc_verify.log; false) || echo "C_OPS OK"
