#!/usr/bin/env python3
"""lmcache-daos 아키텍처 / 실험환경 도식 (연구자 공유용, 저채도)."""
import os
import sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle, Circle

KF = "Noto Sans CJK JP"     # 한글 포함
MF = "DejaVu Sans Mono"     # ASCII 전용 (한글 글리프 없음)
plt.rcParams["font.family"] = KF

INK = "#1c1c1c"
GREY = "#666666"
LINE = "#9a9a9a"
FILL = "#f5f4f2"
FILL2 = "#eceae6"
ACC = "#2d5f86"
ACCF = "#e6eef5"
WARN = "#8c5a1e"


def _fam(s, want):
    """모노 요청이라도 비ASCII가 섞이면 한글 폰트로 강등 (글리프 깨짐 방지)."""
    if want != "m":
        return KF
    try:
        s.encode("ascii")
    except UnicodeEncodeError:
        if any(ord(c) > 0x2500 for c in s):
            return KF
        for c in s:
            if 0x1100 <= ord(c) <= 0x11FF or 0xAC00 <= ord(c) <= 0xD7A3:
                return KF
    return MF


def box(ax, x, y, w, h, title=None, lines=None, fill=FILL, edge=LINE, lw=0.9,
        ts=9.0, ls=7.4, tcol=INK, mono_title=False, tal="left", pad=1.5,
        ha_body="left", dash=None):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle="round,pad=0,rounding_size=0.7",
                       fc=fill, ec=edge, lw=lw, zorder=2)
    if dash:
        p.set_linestyle(dash)
    ax.add_patch(p)
    ty = y + h - pad - 0.3
    if title:
        tx = x + pad if tal == "left" else x + w / 2.0
        ax.text(tx, ty, title, ha=("left" if tal == "left" else "center"),
                va="top", fontsize=ts, color=tcol, fontweight="bold",
                family=(MF if mono_title else KF), zorder=3)
        ty -= ts * 0.135 + 0.72
    for ln, fam in (lines or []):
        tx = x + pad if ha_body == "left" else x + w / 2.0
        ax.text(tx, ty, ln, ha=("left" if ha_body == "left" else "center"),
                va="top", fontsize=ls, color=GREY, family=_fam(ln, fam), zorder=3)
        ty -= ls * 0.145 + 0.44
    return ty


def table(ax, x, y, w, rows, ls=7.0, labw=None, right=True, col=GREY):
    """(라벨, 값) 표. 값은 모노, 라벨은 한글 폰트. 값이 ''이면 라벨만 폭 전체."""
    yy = y
    for lab, val in rows:
        ax.text(x, yy, lab, fontsize=ls, color=col, family=KF, va="top", zorder=3)
        if val:
            if right:
                ax.text(x + w, yy, val, fontsize=ls, color=col, va="top",
                        ha="right", family=_fam(val, "m"), zorder=3)
            else:
                ax.text(x + labw, yy, val, fontsize=ls, color=col, va="top",
                        family=_fam(val, "m"), zorder=3)
        yy -= ls * 0.145 + 0.55
    return yy


def marker(ax, x, y, n, r=1.15):
    ax.add_patch(Circle((x, y), r, fc="white", ec=WARN, lw=1.0, zorder=5))
    ax.text(x, y - 0.06, str(n), ha="center", va="center", fontsize=7.2,
            color=WARN, fontweight="bold", zorder=6)


def arrow(ax, p0, p1, col=INK, lw=1.0, style="-|>", ms=7, dash=None, rad=0.0):
    a = FancyArrowPatch(p0, p1, arrowstyle=style, mutation_scale=ms,
                        color=col, lw=lw, zorder=4, shrinkA=0, shrinkB=0,
                        connectionstyle="arc3,rad=%.2f" % rad)
    if dash:
        a.set_linestyle(dash)
    ax.add_patch(a)


def newax(figsize):
    fig = plt.figure(figsize=figsize, facecolor="white")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    return fig, ax


# ----------------------------------------------------------------- Figure 1
def fig_arch():
    fig, ax = newax((11.8, 8.4))

    ax.text(2.0, 97.6, "그림 1.  lmcache-daos — vLLM/LMCache 의 KV-cache 를 "
                       "DAOS 에 오프로드하는 경로",
            fontsize=12.2, color=INK, fontweight="bold", va="top")
    ax.text(2.0, 93.8, "파란 테두리 = 본 프로젝트가 구현한 부분.   "
                       "①②③ = 측정에서 드러난 병목 지점 (셋 모두 DAOS 외부).",
            fontsize=8.4, color=GREY, va="top")

    # ---------------- 서빙 노드
    CX, CW = 2.0, 44.0
    box(ax, CX, 4.6, CW, 84.6, fill="white", edge="#c9c6c0", lw=1.0)
    ax.text(CX + 1.8, 87.5, "서빙 노드   client-6", fontsize=9.8, color=INK,
            fontweight="bold", va="top")
    ax.text(CX + 1.8, 84.4, "H100 NVL 94 GB · Rocky 10.2 · podman "
                            "(kvsup-ucx-lmc:local)", fontsize=7.3, color=GREY, va="top")

    ix, iw = CX + 2.0, CW - 4.0

    box(ax, ix, 74.1, iw, 8.4, title="vLLM 0.18",
        lines=[("API server (단일 프로세스) · prefill / decode · GPU KV cache", "k"),
               ("Qwen3-1.7B / 14B  ·  --hf-overrides YaRN", "m")], fill=FILL2)
    marker(ax, ix + iw - 2.6, 78.3, 3)

    box(ax, ix, 64.1, iw, 8.4, title="LMCache 0.5.2   (컨테이너 내 소스 빌드)",
        lines=[("KV 청크 조립·분해 · chunk_size = 256 token", "k"),
               ("c_ops CUDA kernel : H2D scatter / gather", "m")], fill=FILL2)
    marker(ax, ix + iw - 2.6, 68.3, 2)

    box(ax, ix, 56.9, iw, 5.6, ts=8.4,
        title="RemoteBackend  →  DynamicConnectorAdapter",
        lines=[('remote_url = "plugin://daos/kvpool2/kv2s16"', "m")], fill=FILL2)

    # 커넥터 (본 프로젝트)
    box(ax, ix - 0.7, 30.3, iw + 1.4, 25.0, fill=ACCF, edge=ACC, lw=1.8)
    ax.text(ix + 0.9, 54.0, "lmcache_daos   (DaosConnector)", fontsize=9.8,
            color=ACC, fontweight="bold", va="top")
    ax.text(ix + iw - 0.9, 54.0, "LMCache RemoteConnector 구현", fontsize=7.0,
            color=ACC, va="top", ha="right")

    sw = (iw - 3.0) / 2.0
    box(ax, ix + 0.8, 42.9, sw, 9.4, title="connector.py", mono_title=True, ts=7.8,
        lines=[("get / put · exists · list", "m"),
               ("batched_get / batched_put", "m"),
               ("remove_sync → 원격 eviction 경로", "k"),
               ("blocking 호출은 thread-pool 로 분리", "k")],
        fill="white", edge="#b7cad9", ls=6.3)
    box(ax, ix + 1.8 + sw, 42.9, sw, 9.4, title="serde.py", mono_title=True, ts=7.8,
        lines=[("[prefix 8B][meta][payload]", "m"),
               ("meta = RemoteMetadata 직렬화", "k"),
               ("파일이 자기 자신을 기술 →", "k"),
               ("read 시 stat/index 조회 불필요", "k")],
        fill="white", edge="#b7cad9", ls=6.3)
    box(ax, ix + 0.8, 32.7, sw, 9.4, title="dfs_binding.py", mono_title=True, ts=7.8,
        lines=[("ctypes  →  libdfs", "m"),
               ("dfs_sys_connect / open / read /", "m"),
               ("write / close / remove_type", "m")],
        fill="white", edge="#b7cad9", ls=6.3)
    box(ax, ix + 1.8 + sw, 32.7, sw, 9.4, title="streaming.py", mono_title=True, ts=7.8,
        lines=[("completion-ordered 스트리밍", "k"),
               ("read 와 H2D 오버랩 (커넥터 단독", "k"),
               ("실측 33.7 GB/s = 1.55x)", "m"),
               ("LMCache 상류 API 개방 대기", "k")],
        fill="white", edge="#bdbab5", ls=6.3, dash=(0, (3, 2)))
    ax.text(ix + 0.9, 31.9, "키 매핑:  CacheEngineKey → sha256 → DFS 경로 (flat)",
            fontsize=6.9, color=ACC, va="top")

    box(ax, ix, 20.1, iw, 8.6, ts=8.4, title="DAOS 클라이언트 라이브러리 · daos_agent",
        lines=[("libdaos / libdfs / libgurt / libcart / libabt", "m"),
               ("호스트 lib64 직마운트는 glibc 충돌 → /daoslib 로 큐레이션 주입", "k"),
               ("agent fabric_ifaces  domain: mlx5_0:1  ← 포트 표기 필수", "k")],
        fill=FILL)

    box(ax, ix, 11.3, iw, 7.2, ts=8.4,
        title="mercury 2.4.1  +  UCX 1.20   (provider  ucx+rc_v)",
        lines=[("libna_plugin_ucx.so — 패키징 누락이 UCX 활성화의 실제 관문", "k"),
               ("libfabric verbs;ofi_rxm 은 대용량 read 를 조용히 손상 (30개 중 3–10개)", "k")],
        fill=FILL)

    box(ax, ix + iw * 0.16, 5.7, iw * 0.68, 4.0, tal="center", ts=7.6,
        title="ConnectX  mlx5_0  ·  ens255np0  192.168.10.60/24",
        mono_title=True, fill=FILL2)

    for y0, y1 in [(74.1, 72.5), (64.1, 62.5), (56.9, 55.3), (30.3, 28.7),
                   (20.1, 18.5), (11.3, 9.7)]:
        arrow(ax, (ix + iw / 2.0, y0), (ix + iw / 2.0, y1), col="#8d8d8d", lw=0.9, ms=6)

    # ---------------- 패브릭
    FX = 47.4
    ax.add_patch(Rectangle((FX, 4.6), 6.6, 84.6, fc="#fafaf9", ec="none", zorder=1))
    for yy in (4.6, 89.2):
        ax.plot([FX, FX + 6.6], [yy, yy], color="#dcdad5", lw=0.8, zorder=1)
    ax.text(FX + 3.3, 87.5, "400GbE\nRoCE v2", fontsize=8.2, color=INK,
            ha="center", va="top", fontweight="bold")
    ax.text(FX + 3.3, 81.0, "RDMA\nib_write_bw\n42–45 GB/s", fontsize=6.8,
            color=GREY, ha="center", va="top")

    arrow(ax, (FX + 0.5, 47.0), (FX + 6.1, 47.0), col=ACC, lw=1.7, ms=9)
    ax.text(FX + 3.3, 49.6, "store", fontsize=7.6, color=ACC, ha="center", family=MF)
    ax.text(FX + 3.3, 44.8, "dfs_sys_write", fontsize=6.4, color=GREY,
            ha="center", va="top", family=MF)

    arrow(ax, (FX + 6.1, 34.0), (FX + 0.5, 34.0), col=ACC, lw=1.7, ms=9)
    ax.text(FX + 3.3, 36.6, "retrieve", fontsize=7.6, color=ACC, ha="center", family=MF)
    ax.text(FX + 3.3, 31.8, "dfs_sys_read", fontsize=6.4, color=GREY,
            ha="center", va="top", family=MF)

    # ---------------- DAOS 측
    DX, DW = 55.0, 43.0
    box(ax, DX, 4.6, DW, 84.6, fill="white", edge="#c9c6c0", lw=1.0)
    ax.text(DX + 1.8, 87.5, "스토리지   ExaStor 2.8  (DAOS 2.8.0, port/2.8-wsd)",
            fontsize=9.8, color=INK, fontweight="bold", va="top")
    ax.text(DX + 1.8, 84.4, "cell1 / cell2 듀얼 컨트롤러 · 2 rank",
            fontsize=7.3, color=GREY, va="top")

    jx, jw = DX + 2.0, DW - 4.0
    hw = (jw - 2.0) / 2.0
    for k, (nm, rk) in enumerate([("cell1", "rank 0"), ("cell2", "rank 1")]):
        box(ax, jx + k * (hw + 2.0), 68.6, hw, 12.6, ts=8.6,
            title="%s  ·  %s" % (nm, rk),
            lines=[("daos_engine", "m"), ("targets 8", "m"),
                   ("SCM  class:ram  80G", "m"), ("NVMe  x8", "m"),
                   ("mlx5_0 / mlx5_1", "m")], fill=FILL2, ls=6.8)

    ax.text(jx + jw / 2.0, 67.2, "동일 /24 에 두 포트 → ARP flux 회피용 정책 라우팅 + "
                                 "arp_ignore (런타임 적용)",
            fontsize=6.7, color=WARN, ha="center", va="top")

    box(ax, jx, 55.6, jw, 8.4, title="pool  kvpool2", mono_title=True, ts=8.8,
        lines=[("2 rank · ntarget 16", "m"),
               ("SCM 16 GB (8 GB/rank)  ·  NVMe 400 GB", "m")], fill=FILL)

    box(ax, jx, 39.0, jw, 14.6, title="container  kv2s16   (POSIX / DFS)",
        mono_title=True, ts=8.8,
        lines=[("--file-oclass=S16", "m"),
               ("--chunk-size=4194304        ← 4 MiB", "m"),
               ("--properties=rd_fac:0", "m"),
               ("기본 1 MiB 는 per-chunk RPC 오버헤드로 read 3.8x 손실", "k"),
               ("최적 규칙:  chunk ≈ 파일크기 ÷ 랭크당 타깃수", "k")], fill=FILL)
    marker(ax, jx + jw - 2.6, 50.8, 1)

    box(ax, jx, 21.0, jw, 15.6, title="객체 레이아웃",
        lines=[("KV chunk = 256 token = 28 MiB  (Qwen3-1.7B, BF16)", "m"),
               ("→  1 chunk = DFS 파일 1개 = DAOS array 객체", "k"),
               ("→  4 MiB DFS chunk 7개로 분할", "k"),
               ("→  16 target 에 스트라이프", "k"),
               ("sustained read 34.5 GB/s   (1 MiB 기본값에서는 9.2)", "k")],
        fill=FILL2)

    ty = box(ax, jx, 6.4, jw, 13.0, title="측정된 상한 —  read ⊕ H2D 직렬 합성",
             fill="white", edge="#bdbab5", dash=(0, (3, 2)))
    table(ax, jx + 1.5, ty, jw - 3.0, [
        ("커넥터 read", "33.6 GB/s   (62 ms / 2.07 GB)"),
        ("c_ops H2D", "45.8 GB/s   (45 ms)"),
        ("직렬 합성 (현행)", "19.4 GB/s   = 실측 단일·집계"),
        ("완전 오버랩 (이론 상한)", "33.6 GB/s   = 1.73x"),
    ], ls=7.0)

    # ---------------- 병목 각주
    ax.plot([2.0, 98.0], [3.5, 3.5], color="#dcdad5", lw=0.8)
    for (n, txt), x0 in zip([
            (1, "DFS chunk_size 기본 1 MiB — read 9.2 → 34.5 GB/s (3.8x)"),
            (2, "LMCache c_ops 비활성 (이미지 CUDA 불일치) — retrieve 3.3 → 20.4 GB/s (6.2x)"),
            (3, "API 서버 프롬프트 토크나이즈 직렬화 (GIL) — 집계 8.7 → 19.4 GB/s")],
            [2.6, 32.0, 67.0]):
        marker(ax, x0, 1.7, n, r=1.05)
        ax.text(x0 + 1.9, 1.7, txt, fontsize=6.9, color=GREY, va="center")

    return fig


# ----------------------------------------------------------------- Figure 2
def fig_bed():
    fig, ax = newax((11.8, 7.4))

    ax.text(2.0, 97.2, "그림 2.  실험환경 토폴로지 및 확인된 수치",
            fontsize=12.2, color=INK, fontweight="bold", va="top")
    ax.text(2.0, 92.6, "H100 노드 2대 + DAOS 2 rank, 400GbE RoCE 단일 포트.  "
                       "client-7 은 크로스노드 KV 재사용 검증에만 사용.",
            fontsize=8.4, color=GREY, va="top")

    # 서빙 노드 (세로 배치)
    for k, (nm, alt, ip, os_, drv, role) in enumerate([
            ("client-6", "io500-6", "192.168.10.60/24", "Rocky 10.2 · podman 5.8.2",
             "driver 610.57.04", "주 측정 노드"),
            ("client-7", "io500-7", "192.168.10.17/24", "Rocky 8.10 · podman 4.9.4",
             "driver 610.43.02", "크로스노드 검증")]):
        by = 72.5 - k * 17.5
        box(ax, 3.0, by, 21.5, 15.5, ts=9.0, ls=6.6,
            title="%s  (%s)" % (nm, alt),
            lines=[("H100 NVL  95830 MiB  ·  " + drv, "m"),
                   (os_, "m"),
                   ("vLLM 0.18 + LMCache 0.5.2", "m"),
                   ("lmcache-daos  DaosConnector", "m"),
                   ("daos_agent  (domain mlx5_0:1)", "m"),
                   ("ens255np0  " + ip + "   (RoCE)", "m"),
                   ("ens4f1     10.100.230.x  (mgmt)", "m")],
            fill=("white" if k == 0 else "#fbfaf9"),
            edge=(ACC if k == 0 else "#c9c6c0"), lw=(1.6 if k == 0 else 0.9))
        ax.text(23.0, by + 13.7, role, fontsize=6.9, color=(ACC if k == 0 else GREY),
                ha="right", va="top")

    # 스위치
    box(ax, 30.0, 62.5, 12.0, 11.0, ts=9.2, tal="center", ha_body="center",
        title="400GbE", lines=[("RoCE v2 switch", "m"), ("192.168.10.0/24", "m")],
        fill=FILL2)

    # DAOS
    box(ax, 47.0, 51.0, 50.0, 37.0, fill="white", edge="#c9c6c0")
    ax.text(48.8, 86.4, "ExaStor 2.8  듀얼 컨트롤러  (DAOS 2 rank)", fontsize=9.4,
            color=INK, fontweight="bold", va="top")
    for k, (nm, rk, p0, p1) in enumerate([("cell1", "rank 0", "192.168.10.82", "192.168.10.81"),
                                          ("cell2", "rank 1", "192.168.10.84", "192.168.10.83")]):
        box(ax, 48.8 + k * 23.5, 65.5, 22.0, 16.2, ts=8.6, ls=6.7,
            title="%s  ·  %s" % (nm, rk),
            lines=[("daos_engine", "m"), ("targets 8", "m"),
                   ("SCM  class:ram  80G", "m"), ("NVMe  x8", "m"),
                   ("mlx5_0  " + p0, "m"), ("mlx5_1  " + p1, "m")], fill=FILL2)
    box(ax, 48.8, 54.0, 46.7, 8.0, ts=8.0,
        title="pool kvpool2   ·   container kv2s16",
        mono_title=True,
        lines=[("2 rank / ntarget 16 · SCM 16 GB · NVMe 400 GB", "m"),
               ("POSIX · oclass S16 · DFS chunk 4 MiB · rd_fac:0", "m")], fill=FILL)

    arrow(ax, (24.7, 79.0), (29.8, 71.5), col=ACC, lw=1.3, ms=8, style="<|-|>")
    arrow(ax, (24.7, 61.0), (29.8, 65.5), col=ACC, lw=1.3, ms=8, style="<|-|>")
    arrow(ax, (42.2, 68.0), (46.8, 68.0), col=ACC, lw=1.3, ms=8, style="<|-|>")
    ax.text(36.0, 75.5, "RDMA  42–45 GB/s", fontsize=7.0, color=ACC,
            ha="center", va="bottom")

    ax.text(3.0, 53.0, "접속 경로:  tta1 → cell1 → client-*   "
                       "(client 노드 간 직결 SSH 없음 — cell1 을 릴레이로 사용)",
            fontsize=7.0, color=GREY, va="top")

    # 소프트웨어 핀
    ty = box(ax, 3.0, 28.0, 44.0, 18.5, title="소프트웨어 구성 핀", fill="white",
             edge="#c9c6c0")
    table(ax, 4.6, ty, 40.8, [
        ("DAOS", "2.8.0  ExaStor backport (port/2.8-wsd)"),
        ("전송", "UCX 1.20.0 / mercury 2.4.1 · ucx+rc_v"),
        ("", ""),
        ("서빙", "vLLM 0.18 + LMCache 0.5.2 (source build)"),
        ("모델", "Qwen3-1.7B / 14B · BF16 · YaRN 64K/127K"),
        ("이미지", "kvsup-ucx-lmc:local (2-stage, 22.4 GB)"),
        ("LMCache 설정", "chunk_size 256 · remote_serde naive"),
        ("", "enable_async_loading: True"),
    ], ls=7.0, labw=10.5, right=False)
    ax.text(15.1, ty - 2 * (7.0 * 0.145 + 0.55), "libfabric 1.22 는 대용량 read 손상으로 배제",
            fontsize=7.0, color=WARN, va="top")

    # 결과
    ty = box(ax, 49.0, 28.0, 48.0, 18.5, title="확인된 주요 수치", fill="white",
             edge=ACC, lw=1.3)
    table(ax, 50.6, ty, 44.8, [
        ("TTFT — KV hit vs recompute", "158 ms / 2812 ms  =  17.7x"),
        ("retrieve (단일 요청)", "19.4 GB/s"),
        ("집계 (concurrency 4)", "20.9 GB/s"),
        ("raw sustained read (chunk 4 MiB)", "34.5 GB/s"),
        ("데이터 무결성 (28 MB x 30)", "30/30   (verbs 3-10/30)"),
        ("크로스노드 재사용 (client-7)", "149/149 hit · avg 444 ms"),
        ("GPUDirect (GDR) 상한", "21.3 GB/s  → +10%, 종료"),
        ("남은 최대 레버 — 스트리밍 청크 전달", "1.73x  (상류 변경 필요)"),
    ], ls=7.0)

    # 계측 주의
    ax.plot([2.0, 98.0], [26.0, 26.0], color="#dcdad5", lw=0.8)
    ax.text(2.6, 24.2, "계측 주의  —  모르면 결과가 조용히 무효가 되는 것들",
            fontsize=8.2, color=WARN, va="top", fontweight="bold")
    cautions = [
        "성능 판정은 클라이언트 wall-clock TTFT 만 사용 — enable_async_loading 하에서 "
        "LMCache 가 보고하는 throughput 은 동기 구간만 계상해 과대 보고한다.",
        "max_local_cpu_size ≥ (최대 컨텍스트 KV × 동시요청수). 초과하면 "
        "local_cpu_backend.allocate() 실패가 에러 없이 cache miss 로 처리된다.",
        "긴 프롬프트는 prompt_token_ids 로 전달 — 텍스트 토크나이즈가 API 서버 GIL 에서 "
        "직렬화되어 13.7K 토큰에서 TTFT 를 4.2x 왜곡한다.",
        "class: ram SCM 은 휘발성 → 재시작 시 dmg storage format 필요. "
        "정책 라우팅 / arp_ignore 는 런타임 적용이라 재부팅 시 소멸.",
        "크로스노드 히트에는 모델경로 · served-model-name · chunk_size · TP · "
        "PYTHONHASHSEED · YaRN factor 가 노드 간 완전히 일치해야 한다 "
        "(다르면 히트해도 KV 값이 틀린다).",
    ]
    yy = 20.6
    for c in cautions:
        ax.text(3.2, yy, "·", fontsize=8.0, color=WARN, va="top")
        ax.text(4.6, yy, c, fontsize=7.0, color=GREY, va="top")
        yy -= 3.4

    ax.text(2.0, 1.4, "출처: lmcache-daos README / deploy/README.md, "
                      "「DAOS KV-cache over RoCE v4」 (2026-08)",
            fontsize=6.8, color="#a8a8a8", va="bottom")
    return fig



# ----------------------------------------------------------------- Figure 1b
def fig_stack():
    """실험환경 고유값을 뺀 SW/HW 스택 단순화 판."""
    fig, ax = newax((9.4, 9.0))

    ax.text(3.0, 97.4, "그림 1b.  lmcache-daos 소프트웨어 / 하드웨어 스택",
            fontsize=12.4, color=INK, fontweight="bold", va="top")
    ax.text(3.0, 93.9, "호스트·IP·이미지·측정치 등 실험환경 고유 값을 제외한 구조만.   "
                       "파란 테두리 = 본 프로젝트 구현.",
            fontsize=8.2, color=GREY, va="top")
    ax.text(97.0, 93.9, "↓ store (put)    ↑ retrieve (get)", fontsize=7.6,
            color=ACC, va="top", ha="right")

    X, W = 16.0, 66.0
    RX = X + W + 1.6          # 인터페이스 라벨 열
    CX = X + W / 2.0

    def band(y, h, title, sub=None, ts=9.2, ls=7.2, **kw):
        lines = [(sub, "k")] if sub else None
        box(ax, X, y, W, h, title=title, lines=lines, ts=ts, ls=ls, **kw)

    def iface(y, txt):
        arrow(ax, (CX, y + 0.85), (CX, y - 0.85), col="#8d8d8d", lw=0.9, ms=6,
              style="<|-|>")
        ax.text(RX, y, txt, fontsize=6.9, color=GREY, va="center")

    def hwband(y, h, label, cells):
        cw = (W - 2.0 * (len(cells) - 1)) / float(len(cells))
        for k, (t, sub) in enumerate(cells):
            box(ax, X + k * (cw + 2.0), y, cw, h, title=t, ts=8.4, ls=6.9,
                lines=[(sub, "k")], fill=FILL2, tal="center", ha_body="center")

    # ---- 서빙 노드 소프트웨어
    band(85.0, 6.0, "vLLM  —  추론 서빙 엔진",
         "API server · prefill / decode · GPU KV cache", fill=FILL2)
    iface(84.1, "KV 오프로드 훅")

    band(78.2, 6.0, "LMCache  —  KV-cache 계층",
         "KV 청크 관리 · CPU 캐시 계층 · c_ops CUDA 커널 (H2D scatter / gather)",
         fill=FILL2)
    iface(77.3, "RemoteConnector\n인터페이스")

    band(72.2, 5.2, "RemoteBackend  →  DynamicConnectorAdapter",
         "out-of-tree 커넥터를 plugin:// 스킴으로 로딩", fill=FILL2, ts=8.6)
    iface(71.3, "plugin:// URL 라우팅")

    # 커넥터 (본 프로젝트)
    box(ax, X, 59.0, W, 12.0, fill=ACCF, edge=ACC, lw=1.8)
    ax.text(X + 1.6, 69.8, "lmcache_daos   (DaosConnector)", fontsize=9.4,
            color=ACC, fontweight="bold", va="top")
    ax.text(X + W - 1.6, 69.8, "본 프로젝트 구현", fontsize=7.0, color=ACC,
            va="top", ha="right")
    sub = [("connector.py", "get / put · batched\nlist / remove_sync"),
           ("serde.py", "[prefix][meta][payload]\n자기 기술적 객체"),
           ("dfs_binding.py", "ctypes → libdfs\ndfs_sys_*"),
           ("streaming.py", "read ⊕ H2D 오버랩\n(상류 API 대기)")]
    cw = (W - 3.0 - 3 * 1.4) / 4.0
    for k, (t, sb) in enumerate(sub):
        box(ax, X + 1.5 + k * (cw + 1.4), 59.9, cw, 6.6, title=t,
            mono_title=True, ts=7.4, ls=6.4, tal="center", ha_body="center",
            lines=[(ln, "k") for ln in sb.split("\n")],
            fill="white", edge=("#bdbab5" if k == 3 else "#b7cad9"),
            dash=((0, (3, 2)) if k == 3 else None))
    iface(58.1, "dfs_sys_*  (ctypes)")

    band(52.0, 5.2, "DAOS 클라이언트 라이브러리  ·  daos_agent",
         "libdaos / libdfs — DFS 네임스페이스에 POSIX 유사 파일 API 제공", ts=8.6)
    iface(51.1, "CART RPC + bulk RDMA")

    band(45.0, 5.2, "mercury  +  UCX",
         "RDMA 전송 계층 — 제어는 RPC, 데이터는 bulk transfer", ts=8.6)
    iface(44.1, "verbs / rdma-core")

    # ---- 서빙 노드 하드웨어
    hwband(36.8, 6.6, "노드 HW",
           [("GPU", "HBM · KV cache"),
            ("CPU / DRAM", "청크 staging"),
            ("RDMA NIC", "RoCE 포트")])

    # ---- 패브릭
    box(ax, X, 29.0, W, 5.4, title="이더넷 패브릭  —  RoCE v2 (RDMA)",
        lines=[("KV 청크 본문은 bulk RDMA 로, 제어는 RPC 로 오간다", "k")],
        ts=8.6, ls=7.2, fill="#f0efec", tal="center", ha_body="center")
    arrow(ax, (CX, 36.7), (CX, 34.5), col=ACC, lw=1.2, ms=6, style="<|-|>")
    arrow(ax, (CX, 28.9), (CX, 27.3), col=ACC, lw=1.2, ms=6, style="<|-|>")

    # ---- DAOS 서버 소프트웨어
    band(21.0, 6.2, "daos_engine  (rank)",
         "target 별 독립 서비스 스레드 · 객체 배치와 스트라이핑 담당", ts=8.8)
    iface(20.5, "DAOS 객체 (array)")

    band(12.6, 7.4, "pool  →  container (POSIX / DFS)  →  array object",
         "KV 청크 1개 = DFS 파일 1개 = array 객체.  파일은 DFS chunk 단위로\n"
         "여러 target 에 스트라이프된다 (chunk 크기가 read 대역폭을 지배).",
         ts=8.8)
    iface(11.7, "SCM 메타 · NVMe 데이터")

    # ---- DAOS 서버 하드웨어
    hwband(4.2, 6.6, "DAOS HW",
           [("SCM 계층 (DRAM / PMem)", "메타데이터 · 소용량 I/O"),
            ("NVMe SSD", "KV 청크 본문")])

    # ---- 좌측 존 괄호
    xz = X - 3.4
    for label, y0, y1 in [("서빙 노드\nSW", 45.0, 91.0),
                          ("서빙 노드\nHW", 36.8, 43.4),
                          ("패브릭", 29.0, 34.4),
                          ("DAOS 서버\nSW", 12.6, 27.2),
                          ("DAOS 서버\nHW", 4.2, 10.8)]:
        ax.plot([xz, xz], [y0, y1], color="#bdbab5", lw=0.9, zorder=1)
        for yy in (y0, y1):
            ax.plot([xz, xz + 0.9], [yy, yy], color="#bdbab5", lw=0.9, zorder=1)
        ax.text(xz - 1.0, (y0 + y1) / 2.0, label, fontsize=7.4, color=INK,
                va="center", ha="right", ma="right", fontweight="bold",
                linespacing=1.45)

    return fig

OUT = os.path.dirname(os.path.abspath(__file__))
FIGS = {
    "arch": (fig_arch, "fig1_lmcache_daos_architecture"),
    "stack": (fig_stack, "fig1b_lmcache_daos_stack"),
    "bed": (fig_bed, "fig2_lmcache_daos_testbed"),
}

want = sys.argv[1:] or list(FIGS)
for key in want:
    fn, name = FIGS[key]
    fig = fn()
    fig.savefig(os.path.join(OUT, name + ".png"), dpi=200, facecolor="white")
    fig.savefig(os.path.join(OUT, name + ".svg"), facecolor="white")
    print("wrote", name)
