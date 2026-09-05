FROM kvsup:052
RUN apt-get update && apt-get install -y --no-install-recommends \
      libibverbs-dev librdmacm-dev ibverbs-providers rdma-core \
      make gcc wget ca-certificates numactl file && \
    rm -rf /var/lib/apt/lists/*
RUN cd /tmp && wget -q https://github.com/openucx/ucx/releases/download/v1.20.0/ucx-1.20.0.tar.gz && \
    tar xf ucx-1.20.0.tar.gz && cd ucx-1.20.0 && \
    ./configure --prefix=/opt/ucx --libdir=/opt/ucx/lib \
      --disable-assertions --disable-params-check --enable-mt --enable-cma \
      --without-go --without-java --without-cuda --without-gdrcopy --with-verbs \
      --without-knem --without-rocm --without-xpmem --without-fuse3 --without-ugni \
      --enable-logging --enable-debug-data --disable-doxygen-doc >/tmp/ucx_conf.log 2>&1 && \
    make -j"$(nproc)" >/tmp/ucx_make.log 2>&1 && make install >/tmp/ucx_inst.log 2>&1 && \
    cd / && rm -rf /tmp/ucx*
