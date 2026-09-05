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
        ("UCX 판정 (28 MB x 30)", "30/30   (verbs 3-10/30)"),
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


# ----------------------------------------------------------------- Figure 3
def fig_why():
    """KV-cache 워크로드의 성질 → DAOS 객체 모델 → 실측 근거."""
    fig, ax = newax((13.4, 8.6))

    ax.text(2.0, 97.8, "그림 3.  KV-cache 를 DAOS 에 두는 구조적 이유와 실측",
            fontsize=12.6, color=INK, fontweight="bold", va="top")
    ax.text(2.0, 94.2, "워크로드의 성질 하나하나가 DAOS 객체 모델의 어떤 성질과 맞물리는지, "
                       "그리고 그것이 실측에서 어떤 수치로 나타나는지.",
            fontsize=8.4, color=GREY, va="top")

    C1, W1 = 2.0, 29.0          # 워크로드
    C2, W2 = 34.5, 30.5         # DAOS 메커니즘
    C3, W3 = 68.5, 29.5         # 실측

    for x, w, t, sub in [(C1, W1, "KV-cache 워크로드의 성질", "vLLM + LMCache 가 만드는 I/O"),
                         (C2, W2, "DAOS 객체 모델이 주는 것", "이 성질과 맞물리는 구조"),
                         (C3, W3, "실측", "본 저장소 · deploy/README.md §8")]:
        ax.text(x + 1.0, 90.6, t, fontsize=9.4, color=INK, fontweight="bold", va="top")
        ax.text(x + 1.0, 87.6, sub, fontsize=7.0, color=GREY, va="top")
    ax.plot([C1, C3 + W3], [86.2, 86.2], color="#cfccc6", lw=0.9)

    LANES = [
        ("청크는 크고, 한 번 쓰고 전량 읽는다",
         ["KV chunk 1개 = 28 MiB (chunk_size 256, BF16)",
          "write-once · immutable · 부분 갱신 없음"],
         "array 객체 + DFS chunk 스트라이프",
         ["파일 하나가 DFS chunk 단위로 쪼개져",
          "여러 target 에 분산된다.",
          "규칙: chunk ≈ 파일크기 ÷ 랭크당 타깃수"],
         [("chunk 1 MiB", "9.17 GB/s"),
          ("chunk 4 MiB", "34.50 GB/s"),
          ("chunk 16 MiB", "9.59 GB/s")],
         "청크 수가 곧 병렬 target 수 — 16 MiB 는 청크 2개뿐이라 target 2개만 쓴다"),

        ("조회는 정확 키 point lookup 이다",
         ["LMCache 가 토큰 해시로 키를 계산 —",
          "검색·범위질의·스캔이 없다. 객체 수는 수십만"],
         "디렉터리 엔트리 = 디렉터리 객체의 dkey",
         ["해시로 찾으므로 선형 스캔이 아니고,",
          "중앙 MDS 도 없다. 별도 인덱스를",
          "유지할 필요가 없다."],
         [("exists  1k 객체", "0.039 ms"),
          ("exists  80k 객체", "0.041 ms"),
          ("read    80k 객체", "0.080 ms")],
         "1k → 80k 에서 추세 없음 — POSIX/NFS 직관이 적용되지 않는다"),

        ("메타는 아주 작고 데이터는 아주 크다",
         ["청크 메타(shape/dtype/fmt) ~28 B 대 본문 28 MiB",
          "메타만 따로 조회할 이유가 없다"],
         "SCM(메타) + NVMe(데이터) 2티어",
         ["메타는 SCM, 본문은 NVMe.  커넥터는",
          "메타를 객체 안에 넣어 (prefix+meta+payload)",
          "open 1 + read 2 로 끝낸다."],
         [("객체당 메타", "4.1 KB"),
          ("28 MiB 청크에서 메타 여유", "~500x"),
          ("손익분기 객체 크기", "61 KB")],
         "chunk_size=1 의 112 KiB 도 61 KB 보다 크다 → 메타가 먼저 차지 않는다"),

        ("재사용이 노드 경계를 넘는다",
         ["다른 노드·다른 프로세스가 넣은 prefix 를 그대로",
          "쓴다. 노드 로컬 계층으로는 불가능한 재사용"],
         "공유 네임스페이스 + 클라이언트 직접 접근",
         ["게이트웨이나 프록시 없이 각 노드의",
          "daos_agent 가 같은 컨테이너에 직접 붙는다.",
          "쓴 노드와 읽는 노드가 대칭이다."],
         [("크로스노드 재사용", "149/149 hit"),
          ("같은 조건 로컬 NVMe", "83% miss"),
          ("2노드 동시 집계", "32.8 GB/s")],
         "avg TTFT 444 ms — 기록 노드(371 ms)의 84%"),

        ("서빙 경로는 지연에 민감하다",
         ["TTFT 예산 안에서 GB 단위를 옮겨야 한다.",
          "커널 경유 계층이 하나 늘면 그대로 TTFT 로 온다"],
         "사용자 공간 direct path + RDMA",
         ["dfuse·커널 VFS 를 거치지 않고 libdfs 를",
          "직접 호출한다. UCX RDMA 로 NIC 에서",
          "NVMe/SCM 까지 복사 단계가 짧다."],
         [("커넥터 read", "33.6 GB/s"),
          ("retrieve (단일요청)", "21.4 GB/s"),
          ("단일노드 raw read", "34.27 GB/s")],
         "발견된 병목 3개는 모두 DAOS 외부였다 (그림 1 참조)"),
    ]

    y = 84.2
    LH = 10.4
    for req_t, req_l, mech_t, mech_l, ev, note in LANES:
        y0 = y - LH
        box(ax, C1, y0, W1, LH, title=req_t, ts=8.2, ls=6.9,
            lines=[(t, "k") for t in req_l], fill=FILL2)
        box(ax, C2, y0, W2, LH, title=mech_t, ts=8.2, ls=6.9,
            lines=[(t, "k") for t in mech_l], fill=FILL)
        ty = box(ax, C3, y0, W3, LH, title=None, fill="white", edge=ACC, lw=1.2)
        table(ax, C3 + 1.5, y0 + LH - 2.2, W3 - 3.0, ev, ls=7.2, col=INK)
        ax.text(C3 + 1.5, y0 + 1.9, note, fontsize=6.5, color=GREY, va="top")

        mid = y0 + LH * 0.58
        arrow(ax, (C1 + W1 + 0.6, mid), (C2 - 0.6, mid), col="#8d8d8d", lw=1.0, ms=7)
        arrow(ax, (C2 + W2 + 0.6, mid), (C3 - 0.6, mid), col=ACC, lw=1.0, ms=7)
        y = y0 - 1.3

    # ---- 하단: E2E 효과 / 비교 범위
    ax.plot([C1, C3 + W3], [24.2, 24.2], color="#cfccc6", lw=0.9)

    ty = box(ax, C1, 4.6, 44.0, 18.0, title="E2E 효과  (client-6 · H100 NVL + DAOS 2 rank · Qwen3-14B)",
             ts=9.0, fill="white", edge=ACC, lw=1.3)
    table(ax, C1 + 1.6, ty, 40.8, [
        ("hit TTFT — 컨텍스트 8K", "151 ms   (recompute 대비 3.8x)"),
        ("hit TTFT — 컨텍스트 127K", "2129 ms  (recompute 대비 11.8x)"),
        ("long-doc-qa 100 GB / 12 inflight", "avg 371 ms · 21.36 GB/s (11.7x)"),
        ("Hub v4 최종 TTFT 배수", "17.7x  (158 ms vs 2812 ms)"),
        ("UCX 판정 (28 MB x 30)", "30/30  (libfabric 3-10/30)"),
    ], ls=7.2, col=INK)

    ty = box(ax, 49.0, 4.6, 49.0, 18.0, title="비교 범위와 한계  —  이 그림이 주장하지 않는 것",
             ts=9.0, ls=7.0, fill="#fbf9f5", edge=WARN, lw=1.1,
             lines=[
                 ("다른 원격 백엔드(Redis · 공유 POSIX FS · 오브젝트 스토리지)", "k"),
                 ("와의 비교는 미측정이다. 위 대비는 모두 노드 로컬 계층", "k"),
                 ("(GPU KV · CPU · 로컬 NVMe) 기준이다.", "k"),
                 ("", "k"),
                 ("· 백킹이 NVMe 여야 한다 — ZFS zvol 풀에서는 0.95x, 손실", "k"),
                 ("· retrieve 상한은 read ⊕ H2D 직렬 합성 (19.4–21.4 GB/s).", "k"),
                 ("  스트리밍으로 33.7 GB/s 확보했으나 상류 API 대기", "k"),
                 ("· 위 배수는 노드 로컬 계층 대비이며, 워킹셋이 서버", "k"),
                 ("  메모리에 상주 가능한 구간의 값이 섞여 있다", "k"),
                 ("· 30/30 은 libfabric 손상 판정용이며 무결성 증명이 아니다 —", "k"),
                 ("  ~1% 손상률에서 30회는 67–89% 확률로 통과한다 (사후 확인)", "k"),
             ])

    ax.text(2.0, 2.0, "출처: 본 저장소 README (ExaCI5-4 CI, 메타데이터 스케일 시험) · "
                      "deploy/README.md §8 (client-6) · 「DAOS KV-cache over RoCE v4」",
            fontsize=6.8, color="#a8a8a8", va="bottom")
    return fig


# ----------------------------------------------------------------- Figure 4
def fig_code():
    """MR !6 구현 코드 구조 — 모듈·API 표면·경로별 시퀀스·동시성 모델."""
    fig, ax = newax((16.0, 10.4))

    ax.text(2.0, 98.0, "그림 4.  MR !6 구현 코드 구조",
            fontsize=12.8, color=INK, fontweight="bold", va="top")
    ax.text(2.0, 95.0, "브랜치 streaming-get-and-client6-assets · 105 files, +59,681 / -80.  "
                       "구현 본체는 lmcache_daos/ 1,701 줄이고 나머지는 시험·재현·배포 자산이다.",
            fontsize=8.4, color=GREY, va="top")

    def flow(x, y, rows, ls=6.4, notex=None, rail=True):
        """번호 붙은 시퀀스. rows = [(본문, 우측 주석 or None)]"""
        dy = ls * 0.145 + 0.62
        y0 = y
        for i, (txt, note) in enumerate(rows, 1):
            ax.text(x, y, "%d" % i, fontsize=ls - 0.4, color=ACC,
                    va="top", ha="right", family=MF, fontweight="bold")
            ax.text(x + 0.9, y, txt, fontsize=ls, color=INK, va="top",
                    family=_fam(txt, "m"))
            if note:
                ax.text(notex, y, note, fontsize=ls - 0.5, color=WARN, va="top",
                        family=_fam(note, "m"))
            y -= dy
        if rail:
            ax.plot([x - 1.5, x - 1.5], [y0 + 0.9, y + dy - 0.3],
                    color="#d6d3ce", lw=0.8, zorder=1)
        return y

    # ---------------- (A) 모듈 맵
    ty = box(ax, 2.0, 58.0, 28.0, 34.0, title="A. MR !6 이 건드린 코드", ts=9.2,
             fill="white", edge="#c9c6c0")
    lines_a = [
        ("lmcache_daos/            구현 본체", "k"),
        ("  connector.py     619  M  DaosConnector", "m"),
        ("  dfs_binding.py   476  M  DfsSys (ctypes)", "m"),
        ("  daos_event.py    454  A  EQ 바인딩 ※미사용", "k"),
        ("  streaming.py      87  A  stream_completions", "m"),
        ("  serde.py          59  M  프레이밍", "k"),
        ("shim/daos_evshim.c  58  A  sizeof(daos_event_t)", "m"),
        ("", "k"),
        ("gpudirect/            A   별도 데이터 평면", "k"),
        ("  patches/          6개  DAOS · mercury · UCX", "k"),
        ("  apply-patches.sh · README(1,374줄) · PLAN", "k"),
        ("", "k"),
        ("tests/   55개  게이트 · 마이크로벤치 · C 재현기", "k"),
        ("bench/   17개  vLLM+LMCache E2E 하네스", "k"),
        ("deploy/ · doc/figures/   환경 재구성 · 그림", "k"),
    ]
    yy = ty
    for t, f in lines_a:
        ax.text(3.5, yy, t, fontsize=6.5, color=GREY, va="top", family=_fam(t, f))
        yy -= 6.5 * 0.145 + 0.52
    ax.plot([3.5, 28.5], [yy + 0.2, yy + 0.2], color="#d6d3ce", lw=0.8)
    ax.text(3.5, yy - 0.9, "커넥터는 CPU MemoryObj 를 반환하는 RemoteConnector\n"
                           "계약 위에 있어 GPU-direct 가 아니다 — gpudirect/ 는\n"
                           "GPU 버퍼에 직접 읽고 쓰는 별도 데이터 평면이다.",
            fontsize=6.4, color=WARN, va="top", linespacing=1.5)

    # ---------------- (B) API 표면
    box(ax, 31.5, 58.0, 32.5, 34.0, title="B. DaosConnector — API 표면", ts=9.2,
        fill="white", edge=ACC, lw=1.3)
    yb = 87.4
    for head_t, rows in [
            ("LMCache 가 호출하는 RemoteConnector 계약",
             [("async exists / exists_sync", None),
              ("async get / async put", None),
              ("async list / async close", None),
              ("remove_sync", "원격 eviction 유일 경로")]),
            ("opt-in 훅  (support_* 가 계약을 연다)",
             [("\u2713 support_batched_get   -> batched_get", None),
              ("\u2713 support_batched_put   -> batched_put", None),
              ("\u00d7 support_batched_get_non_blocking", "구현은 보존"),
              ("\u2713 support_stream_get    -> stream_get", "상류 API 없음"),
              ("\u00b7 batched_contains", "상속, 측정근거 미구현")]),
            ("내부",
             [("_parse_daos_url  plugin://<pool>/<cont>[?sys=]", None),
              ("_key_to_path     '/' + sha256(key)  (flat)", None),
              ("_prep_write / _put_sync / _get_sync", None),
              ("_release / _drop_put_ref / _run", None)])]:
        ax.text(33.0, yb, head_t, fontsize=7.0, color=ACC, va="top",
                fontweight="bold")
        yb -= 1.9
        for t, n in rows:
            if t[0] in "\u2713\u00d7\u00b7":
                ax.text(34.2, yb, t[0], fontsize=6.4,
                        color=(ACC if t[0] == "\u2713" else GREY), va="top")
                ax.text(35.6, yb, t[2:], fontsize=6.4, color=INK, va="top",
                        family=_fam(t[2:], "m"))
            else:
                ax.text(34.2, yb, t, fontsize=6.4, color=INK, va="top",
                        family=_fam(t, "m"))
            if n:
                ax.text(53.0, yb, n, fontsize=6.0, color=GREY, va="top")
            yb -= 6.4 * 0.145 + 0.55
        yb -= 0.9

    # ---------------- (C) 동시성
    ty = box(ax, 65.5, 58.0, 32.5, 34.0, title="C. 동시성 모델", ts=9.2,
             fill="white", edge="#c9c6c0")
    lines_c = [
        ("LMCache asyncio loop", "k"),
        ("  └ _run() = loop.run_in_executor(self._pool, fn, ...)", "m"),
        ("ThreadPoolExecutor(max_workers=16,", "m"),
        ("                   thread_name_prefix='daos-io')", "m"),
        ("  └ 공유 DfsSys 핸들 1개  (mflags=RDWR, sflags=0)", "k"),
        ("      = dfs_sys 디렉터리 캐시 + 락 모두 ON", "k"),
        ("  └ libdfs 호출이 GIL 을 놓는다 → 진짜 병렬", "k"),
    ]
    yy = ty
    for t, f in lines_c:
        ax.text(67.0, yy, t, fontsize=6.5, color=GREY, va="top", family=_fam(t, f))
        yy -= 6.5 * 0.145 + 0.52
    yy -= 0.6
    ax.text(67.0, yy, "sflags 선택 근거 (28 MiB · 16 threads · GB/s)",
            fontsize=6.6, color=ACC, va="top", fontweight="bold")
    yy -= 2.0
    for t in ["NO_CACHE|NO_LOCK  perthread   32.54 / 34.78",
              "0 (cache+lock)    perthread   33.64 / 33.13",
              "0 (cache+lock)    shared      33.73 / 32.90"]:
        ax.text(68.2, yy, t, fontsize=6.3, color=INK, va="top", family=MF)
        yy -= 1.5
    ax.text(68.2, yy, "→ 락은 대용량 read 에서 사실상 공짜. NO_LOCK 을 위해",
            fontsize=6.3, color=GREY, va="top")
    yy -= 1.5
    ax.text(68.2, yy, "   두었던 per-thread 핸들 풀은 이제 불필요해졌다.",
            fontsize=6.3, color=GREY, va="top")
    yy -= 2.6
    ax.text(67.0, yy, "streaming.stream_completions(loop, pool, fn, items, 16)",
            fontsize=6.6, color=ACC, va="top", fontweight="bold", family=MF)
    yy -= 2.0
    for t in ["완료 순서로 (index, result) 를 yield — 인덱스 순서로",
              "주면 느린 청크 하나가 뒤를 전부 막는다",
              "슬롯은 yield 전에 선충전 → 소비자가 바쁜 동안에도 read 지속",
              "예외는 배치가 아니라 청크 단위로 전달 (RFC 요구사항)"]:
        ax.text(68.2, yy, "· " + t, fontsize=6.3, color=GREY, va="top")
        yy -= 1.5

    # ---------------- (D) read 경로
    ty = box(ax, 2.0, 28.5, 46.5, 28.0,
             title="D. read 경로 — _get_sync(path)      open 1회 · 복사 0회", ts=9.2,
             fill="white", edge=ACC, lw=1.3)
    y = flow(4.6, ty - 0.4, [
        ("dfs_sys_open(RDONLY)", "ENOENT → None"),
        ("read_obj(0, 8 + _HDR_CAP)   # prefix+meta 한 번에", "_HDR_CAP = 512"),
        ("serde.parse_prefix(hdr[:8]) → meta_len, payload_len", "짧으면 → None"),
        ("RemoteMetadata.deserialize(meta)", "실패 → None"),
        ("local_cpu_backend.allocate(shapes, dtypes, fmt)", "None → miss"),
        ("dest = (c_char*n).from_buffer(memory_obj 뷰)", "무복사 alias"),
        ("read_obj_into(obj, off, payload_len, dest)", "C 호출이 GIL 해제"),
        ("got != payload_len → _release() 후 None", "잘린 객체 = miss"),
        ("dfs_sys_close(obj)   # finally", None),
    ], ls=6.5, notex=34.0)
    ax.plot([4.0, 47.0], [y + 0.4, y + 0.4], color="#d6d3ce", lw=0.8)
    ax.text(4.0, y - 0.6, "bench_readpath_merge.py — 32 x 28 MiB, GB/s",
            fontsize=6.4, color=ACC, va="top", fontweight="bold")
    yy = y - 2.6
    for t in ["                              1 thread   16 threads",
              "main 방식 (open 4 · copy 2)       2.78        2.61",
              "무복사, 검사 없음                14.37       33.55",
              "무복사 + 위 검사 전부            12.31       32.75"]:
        ax.text(5.0, yy, t, fontsize=6.3, color=INK, va="top", family=_fam(t, "m"))
        yy -= 1.4
    ax.text(5.0, yy - 0.2, "→ 안전 검사 비용 2%. main 방식은 GIL 보유 복사 2회 때문에 "
                           "확장 자체가 안 된다.", fontsize=6.3, color=GREY, va="top")

    # ---------------- (E) write 경로
    ty = box(ax, 51.5, 28.5, 46.5, 28.0,
             title="E. write 경로 — put() / _prep_write / _put_sync", ts=9.2,
             fill="white", edge=ACC, lw=1.3)
    y = flow(54.1, ty - 0.4, [
        ("view = memoryview(byte_array).cast('B') ; n = len(view)", None),
        ("RemoteMetadata(n, shapes, dtypes, fmt).serialize()", "~28 B"),
        ("header = serde.prefix_pack(meta_len, n) + meta", "~36 B"),
        ("src = (c_char*n).from_buffer_copy(view)", "기본: 복사"),
        ("      from_buffer(view) = alias", "DAOS_UNSAFE_ALIAS_STORE=1"),
        ("open_rdwr_create(path)", None),
        ("write_obj_from(0, len(header), hdr)   # 작은 헤더", None),
        ("write_obj_from(len(header), n, src)   # bulk", None),
        ("dfs_sys_close(obj)   # finally", None),
        ("_drop_put_ref(memory_obj)", "serializer 가 올린 ref 반납"),
    ], ls=6.5, notex=82.0)
    ax.plot([53.5, 96.5], [y + 0.4, y + 0.4], color="#d6d3ce", lw=0.8)
    ax.text(53.5, y - 0.6, "왜 이 모양인가", fontsize=6.4, color=ACC, va="top",
            fontweight="bold")
    yy = y - 2.6
    for t in ["3-copy(bytes→pack→string_buffer, ≈1 GB/s) → alias(+5178 → +65 ms @8K)",
              "→ 지금은 복사가 기본. alias 는 그 자체로 불안전하다 — batched_put 은",
              "   async submit 이라 LMCache 가 기다리지 않고 MemoryObj 를 재활용한다.",
              "복사가 손상률을 낮춘다는 증거는 없다 (100% CI[83.9,100] ↔ 85% CI[64.0,94.8])."]:
        ax.text(54.5, yy, t, fontsize=6.3, color=GREY, va="top")
        yy -= 1.4

    # ---------------- (F) 미사용·스위치
    ty = box(ax, 2.0, 2.5, 46.5, 25.0, title="F. 미사용·보류 코드와 진단 스위치",
             ts=9.2, fill=FILL, edge="#c9c6c0")
    yy = ty
    for t, f, c in [
            ("daos_event.py (454줄) + shim/daos_evshim.c — DAOS event queue 바인딩", "k", INK),
            ("· 핫패스에 연결하지 않는다. EQ 당 eqx_lock 이 submit 과 완료를 직렬화해", "k", GREY),
            ("  큐를 어떻게 배치해도 7–12 GB/s, 블로킹 스레드풀은 34.3 GB/s.", "k", GREY),
            ("· 완료 순서 전달에 EQ 가 필요 없다 — resolve 된 future 가 곧 완료다.", "k", GREY),
            ("· ctypes 로 daos_event_t ABI 를 선언하므로 256 B canary + shim 으로", "k", GREY),
            ("  sizeof 를 검증한다 (verify_abi / verify_abi_or_die).", "k", GREY),
            ("", "k", GREY),
            ("환경 스위치 (기본 전부 off)", "k", INK),
            ("DAOS_BG_PROF=1             batched_get 벽시계 계측  [CONN-BG]", "m", GREY),
            ("DAOS_UNSAFE_ALIAS_STORE=1  store 무복사 alias (버그 재현용)", "m", GREY),
            ("DAOS_READ_VIA_BYTEARRAY=1  read 를 사설 bytearray 경유 (진단용)", "m", GREY),
            ("", "k", GREY),
            ("측정으로 범위를 좁힌 결과 — 안 만든 것에도 근거가 있다", "k", INK),
            ("batched_contains  순차 8.8 ms ↔ fan-out 9.4 ms (0.9x) → 상속 유지", "k", GREY),
            ("디렉터리 fanout   조회가 1k→80k 에서 평탄 → 불필요", "k", GREY)]:
        ax.text(3.6, yy, t, fontsize=6.2, color=c, va="top", family=_fam(t, f),
                fontweight=("bold" if c == INK and t else "normal"))
        yy -= 6.2 * 0.145 + 0.45

    # ---------------- (G) 현재 상태
    ty = box(ax, 51.5, 2.5, 46.5, 25.0,
             title="G. 현재 상태 — DAOS 읽기 손상 조사 (2026-09-01 기준)", ts=9.2,
             fill="#fbf9f5", edge=WARN, lw=1.2)
    yy = ty
    for t, f, c in [
            ("완전 스톡 2.9.100 클라이언트(--build-deps=yes)도 34/3840 실패,", "k", INK),
            ("서버를 2.8.0-rc3 으로 되돌려도 68/5120(1.33%) 로 같은 버그.", "k", INK),
            ("→ 커넥터 · LMCache · GPU-direct 패치 · 2.9 신규성 모두 배제.", "k", INK),
            ("   dc_array · vos_aggregate · vos_csum_recalc · src/bio 는", "k", GREY),
            ("   두 버전 간 바이트 동일하고 전송 계층만 바뀌었다.", "k", GREY),
            ("", "k", GREY),
            ("서명   읽기 버퍼의 4 MiB chunk 하나가 같은 offset 의 다른 객체", "k", GREY),
            ("       데이터를 담는다. 다시 읽으면 위치가 바뀐다 → read 쪽.", "k", GREY),
            ("트리거 읽기·쓰기가 섞일 때. read-only 동시성과 단일 객체 arm 은", "k", GREY),
            ("       두 버전 모두 깨끗하다.", "k", GREY),
            ("기각   aggregation (reclaim off 에서도 78/8960) · alignment", "k", GREY),
            ("       (A/B 0.68% vs 0.37%) 둘 다 원인이 아니다.", "k", GREY),
            ("주의   과거 '30/30 무결성' 은 부재의 증거가 아니었다 — 이 손상률", "k", WARN),
            ("       에서 30회는 67–89% 확률로 그냥 통과한다.", "k", WARN),
            ("D · E 는 코드 구조로는 확정이다. 최신 상태는 gpudirect/", "k", WARN),
            ("DAOS-CONCURRENT-READ-CORRUPTION.md §12 · §13.", "m", WARN)]:
        ax.text(53.1, yy, t, fontsize=6.2, color=c, va="top", family=_fam(t, f))
        yy -= 6.2 * 0.145 + 0.45

    ax.text(2.0, 1.2, "출처: 저장소 코드 직접 확인 (lmcache_daos/ · shim/ · gpudirect/) · "
                      "git diff origin/main...HEAD · 커밋 d0821c1",
            fontsize=6.6, color="#a8a8a8", va="bottom")
    return fig


# ----------------------------------------------------------------- Figure 5
def fig_mp():
    """현재 in-process 커넥터 경로 vs LMCache MP 모드 L2 어댑터 경로 — 복사 계수."""
    fig, ax = newax((15.2, 9.6))

    ax.text(2.0, 97.8, "그림 5.  LMCache MP 모드 L2 어댑터로 옮기면 복사가 늘어나는가",
            fontsize=12.6, color=INK, fontweight="bold", va="top")
    ax.text(2.0, 94.6, "결론: 늘지 않는다 — SHM 전송 컨텍스트가 켜져 있고 어댑터가 호출자 버퍼에 "
                       "직접 쓰는 한.  근거는 LMCache v0.5.2 소스.",
            fontsize=8.4, color=GREY, va="top")

    def cbadge(x, y, n, label, col=ACC):
        ax.add_patch(Circle((x, y), 1.25, fc="white", ec=col, lw=1.2, zorder=6))
        ax.text(x, y - 0.05, str(n), ha="center", va="center", fontsize=7.0,
                color=col, fontweight="bold", zorder=7)
        ax.text(x, y - 2.2, label, ha="center", va="top", fontsize=6.3, color=col)

    # ================= A. 현재 경로
    box(ax, 2.0, 58.0, 46.0, 33.0, fill="white", edge="#c9c6c0")
    ax.text(3.6, 89.4, "A. 현재 — in-process RemoteConnector", fontsize=9.4,
            color=INK, fontweight="bold", va="top")
    ax.text(3.6, 86.4, "커넥터가 vLLM 워커 프로세스 안에서 직접 호출된다",
            fontsize=6.8, color=GREY, va="top")

    box(ax, 4.0, 62.0, 11.0, 12.0, title="DAOS", ts=8.6, tal="center",
        ha_body="center", ls=6.4, fill=FILL2,
        lines=[("container", "m"), ("kv2s16", "m"), ("", "k"), ("28 MiB 청크", "k")])

    box(ax, 18.5, 60.5, 28.0, 22.5, fill="#fbfaf8", edge=ACC, lw=1.3, dash=(0, (4, 2)))
    ax.text(19.9, 81.6, "vLLM 워커 프로세스", fontsize=7.6, color=ACC,
            va="top", fontweight="bold")
    box(ax, 20.0, 71.5, 25.0, 8.4, title="LMCache 엔진 + DaosConnector", ts=7.6,
        ls=6.3, fill="white", edge="#b7cad9",
        lines=[("local_cpu_backend.allocate() 로 목적지 생성", "k"),
               ("dfs_sys_read 가 그 버퍼에 직접 write", "k")])
    box(ax, 20.0, 66.6, 25.0, 4.0, title="MemoryObj  (LocalCPUBackend, CPU)",
        ts=7.4, tal="center", fill=FILL)
    box(ax, 20.0, 61.4, 25.0, 3.8, title="GPU KV cache", ts=7.6, tal="center",
        fill=FILL2)

    arrow(ax, (15.2, 68.0), (19.8, 74.0), col=ACC, lw=1.5, ms=8)
    cbadge(16.4, 72.6, 1, "")
    arrow(ax, (32.5, 66.5), (32.5, 65.3), col=ACC, lw=1.5, ms=8)
    cbadge(37.2, 65.9, 2, "")
    ax.text(38.8, 65.9, "c_ops H2D", fontsize=6.3, color=GREY, va="center")

    ax.plot([3.6, 46.4], [59.9, 59.9], color="#d6d3ce", lw=0.8)
    ax.text(3.6, 59.2, "복사 2회 = ① 호스트 착지(RDMA write) + ② H2D.  "
                       "파이썬 레벨 복사는 0.", fontsize=6.6, color=INK, va="top")

    # ================= B. MP 경로
    box(ax, 51.0, 58.0, 47.0, 33.0, fill="white", edge=ACC, lw=1.3)
    ax.text(52.6, 89.4, "B. MP 모드 — L2 어댑터 + SHM 전송 컨텍스트", fontsize=9.4,
            color=INK, fontweight="bold", va="top")
    ax.text(52.6, 86.4, "L1 풀이 두 프로세스가 함께 매핑하는 POSIX shm 이다",
            fontsize=6.8, color=GREY, va="top")

    box(ax, 52.6, 62.0, 10.0, 12.0, title="DAOS", ts=8.6, tal="center",
        ha_body="center", ls=6.4, fill=FILL2,
        lines=[("container", "m"), ("", "k"), ("(DAOS L2", "k"), ("어댑터)", "k")])

    # 두 프로세스
    box(ax, 65.5, 74.5, 15.0, 8.6, fill="#fbfaf8", edge="#bdbab5", lw=1.0,
        dash=(0, (4, 2)), title="LMCache 서버", ts=7.4, ls=6.2,
        lines=[("StoreController", "m"), ("PrefetchController", "m")])
    box(ax, 82.0, 74.5, 14.5, 8.6, fill="#fbfaf8", edge=ACC, lw=1.2,
        dash=(0, (4, 2)), title="vLLM 워커", ts=7.4, ls=6.2,
        lines=[("shm 매핑 + cudaHostRegister", "k"), ("H2D 수행", "k"),
               ("받는 것은 디스크립터뿐", "k")])
    arrow(ax, (80.6, 76.4), (81.8, 76.4), col=GREY, lw=1.0, ms=7)

    # L1 shm 밴드
    box(ax, 65.5, 65.2, 31.0, 5.6, fill="#e6eef5", edge=ACC, lw=1.4,
        title="L1 풀  =  POSIX shm  (lmcache_l1_pool_*)", ts=7.6, tal="center",
        ha_body="center", ls=6.2,
        lines=[("서버가 만들고 워커가 같은 세그먼트를 매핑 · cudaHostRegister 로 핀", "k")])
    for x0 in (67.5, 94.0):
        ax.plot([x0, x0], [74.4, 70.9], color=ACC, lw=1.0, ls=(0, (2, 2)), zorder=3)

    box(ax, 65.5, 60.4, 31.0, 3.8, title="GPU KV cache", ts=7.6, tal="center",
        fill=FILL2)

    arrow(ax, (62.8, 68.0), (65.3, 68.0), col=ACC, lw=1.5, ms=8)
    cbadge(64.0, 71.0, 1, "")
    ax.text(79.0, 72.6, "submit_load_task(keys, objects)  →  objects = L1 슬롯",
            fontsize=6.1, color=GREY, ha="center", va="center")
    arrow(ax, (81.0, 65.1), (81.0, 64.3), col=ACC, lw=1.5, ms=8)
    cbadge(85.5, 64.7, 2, "")

    ax.plot([52.6, 96.4], [59.9, 59.9], color="#d6d3ce", lw=0.8)
    ax.text(52.6, 59.2, "복사 2회 — 동일.  프로세스 경계는 shm 매핑으로 넘으므로 "
                        "추가 복사가 없다.", fontsize=6.6, color=INK, va="top")

    # ================= C. pickle 폴백
    ty = box(ax, 2.0, 33.0, 30.0, 22.0, title="C. 함정 — pickle 폴백이면 복사 +2",
             ts=9.0, fill="#fbf9f5", edge=WARN, lw=1.2)
    yy = ty
    for t, f in [("_compute_shm_pool_info() 가 빈 풀을 돌려주는 조건", "k"),
                 ("  · shm_name 이 비었을 때", "k"),
                 ("  · use_lazy 가 켜졌을 때", "m"),
                 ("  · devdax_path 가 설정됐을 때", "m"),
                 ("", "k"),
                 ("→ 전송이 EngineDrivenContextPickle 로 떨어진다", "k"),
                 ("   store   : 청크를 pickle 직렬화 후 COMMIT_STORE", "k"),
                 ("   retrieve: 받은 바이트를 역직렬화", "k"),
                 ("", "k"),
                 ("복사 4회 (직렬화 버퍼 + 역직렬화 텐서 추가).", "k"),
                 ("MP 모드에서 복사가 실제로 늘어나는 유일한 경로이고,", "k"),
                 ("설정 실수로 조용히 빠질 수 있다 → 측정 전 체크 대상.", "k")]:
        ax.text(3.6, yy, t, fontsize=6.4, color=(WARN if t.startswith("복사 4") else GREY),
                va="top", family=_fam(t, f))
        yy -= 6.4 * 0.145 + 0.5

    # ================= D. 인터페이스 비교
    ty = box(ax, 34.5, 33.0, 63.5, 22.0,
             title="D. 두 확장점의 차이  (RemoteConnector = 우리가 구현한 것)", ts=9.0,
             fill="white", edge="#c9c6c0")
    rows = [
        ("호출 위치", "vLLM 워커 프로세스 안", "별도 LMCache 서버 (컨트롤러 스레드 2개)"),
        ("API 형태", "async def get / put / exists / list", "submit_* → query_* / pop_* (논블로킹)"),
        ("완료 통지", "await", "eventfd 3개 (store / lookup / load)"),
        ("버퍼 소유", "커넥터가 allocate() 해서 반환", "호출자가 준다 — 어댑터는 수명 관리 금지"),
        ("오류 단위", "청크당 None", "store=태스크, lookup·load=키별 Bitmap"),
        ("잠금", "없음", "lookup_and_lock / submit_unlock"),
        ("용량·축출", "list() + remove_sync(), 정책 없음", "get_usage / list_l2_keys / L2EvictionPolicy"),
        ("다중 백엔드", "하나", "--l2-adapter 반복 = 캐스케이드"),
        ("키", "CacheEngineKey → sha256 (복원 불가)", "ObjectKey (model/rank/group/hash/salt)"),
        ("out-of-tree", "plugin:// 스킴", "plugin_l2_adapter (동적 로드)"),
    ]
    yy = ty - 0.3
    ax.text(36.0, yy, "항목", fontsize=6.5, color=ACC, va="top", fontweight="bold")
    ax.text(48.0, yy, "RemoteConnector", fontsize=6.5, color=ACC, va="top",
            fontweight="bold")
    ax.text(72.5, yy, "L2AdapterInterface", fontsize=6.5, color=ACC, va="top",
            fontweight="bold")
    yy -= 1.9
    for a, b, c in rows:
        ax.text(36.0, yy, a, fontsize=6.3, color=INK, va="top")
        ax.text(48.0, yy, b, fontsize=6.3, color=GREY, va="top", family=_fam(b, "m"))
        ax.text(72.5, yy, c, fontsize=6.3, color=GREY, va="top", family=_fam(c, "m"))
        yy -= 1.62

    # ================= E. 미해결 항목 대응
    ty = box(ax, 2.0, 3.0, 46.0, 28.0,
             title="E. 우리 미해결 항목이 L2 인터페이스에서 어떻게 되는가", ts=9.0,
             fill="white", edge=ACC, lw=1.2)
    yy = ty - 0.2
    for a, b in [("스트리밍 / read⊕H2D 오버랩", "submit + eventfd + 태스크별 조회로 표현 가능"),
                 ("  (우리 stream_get 은 호출자가 없었다)", ""),
                 ("용량 정책 부재", "get_usage · list_l2_keys · L2EvictionPolicy 훅"),
                 ("list() 이름의 키 복원 불가", "ObjectKey 가 구조적이라 문제 자체가 없음"),
                 ("청크별 오류 보고", "Bitmap 으로 키 단위 성공/실패"),
                 ("_drop_put_ref 참조 카운트", "호출자가 버퍼를 주므로 소유권 다툼 소멸"),
                 ("무복사 read (우리가 얻은 것)", "obj.byte_array 에 직접 write — 그대로 유지"),
                 ("", ""),
                 ("L1 공유의 부수 효과", "같은 노드의 다른 클라이언트·재시작 프로세스가"),
                 ("", "L2 를 다시 읽지 않는다 (복사 감소 아니라 요청 감소)"),
                 ("", ""),
                 ("옮겨도 그대로인 것", ""),
                 ("DAOS 읽기 손상", "인터페이스와 무관 — DAOS 내부 결함"),
                 ("진짜 batch RPC 부재", "여전히 청크당 객체 1개 → dkey/akey 필요"),
                 ("DFS chunk 규칙", "그대로 적용 (파일크기 ÷ 랭크당 타깃수)"),
                 ("파이썬 스레드풀", "어댑터도 blocking libdfs 를 스레드로 감싼다")]:
        if a:
            ax.text(3.6, yy, a, fontsize=6.4, color=INK, va="top")
        if b:
            ax.text(23.0, yy, b, fontsize=6.4, color=GREY, va="top")
        yy -= 6.4 * 0.145 + 0.52

    # ================= F. 조건과 비용
    ty = box(ax, 51.0, 3.0, 47.0, 28.0, title="F. 조건 · 비용 · 판단", ts=9.0,
             fill=FILL, edge="#c9c6c0")
    yy = ty - 0.2
    for t, c in [("성립 조건 3개", INK),
                 ("1. SHM 전송 컨텍스트 활성 (C 의 함정을 피할 것)", GREY),
                 ("2. byte-array 어댑터로 호출자 버퍼에 직접 write", GREY),
                 ("3. GDS L1(--gds-l1-path) 과는 병용 불가 — 문서 명시.", GREY),
                 ("   DAOS 에 cuFile 드라이버가 없으니 실질 제약은 아니다", GREY),
                 ("", GREY),
                 ("늘어나는 비용", INK),
                 ("· RPC 왕복 (PREPARE/COMMIT) — 청크당이 아니라 요청당", GREY),
                 ("  상수 항으로 보이나 미측정", WARN),
                 ("· 별도 프로세스 운영 · 어댑터 재작성 (상류 800~1,300줄)", GREY),
                 ("· 파이썬 RemoteConnector → L2 브리지는 없다.", GREY),
                 ("  native_connector_l2_adapter 는 C++ pybind 전용", GREY),
                 ("", GREY),
                 ("판단", INK),
                 ("Phase 5 방향으로는 RemoteConnector 개선보다 낫다.", GREY),
                 ("단 DAOS 동시 읽기 손상이 해소되기 전에는 어느", WARN),
                 ("인터페이스로 붙여도 결과가 같으므로 착수 이유가 없다.", WARN)]:
        ax.text(52.6, yy, t, fontsize=6.4, color=c, va="top",
                fontweight=("bold" if c == INK and t else "normal"))
        yy -= 6.4 * 0.145 + 0.52

    ax.text(2.0, 1.2, "근거: LMCache v0.5.2 sdist — transfer_context/shm.py · "
                      "multiprocess/engine_context.py · distributed/l2_adapters/base.py · "
                      "docs/source/mp/l2_storage/.  상세는 doc/lmcache-mp-l2-assessment.md",
            fontsize=6.6, color="#a8a8a8", va="bottom")
    return fig

OUT = os.path.dirname(os.path.abspath(__file__))
FIGS = {
    "arch": (fig_arch, "fig1_lmcache_daos_architecture"),
    "stack": (fig_stack, "fig1b_lmcache_daos_stack"),
    "bed": (fig_bed, "fig2_lmcache_daos_testbed"),
    "why": (fig_why, "fig3_why_daos_for_kvcache"),
    "code": (fig_code, "fig4_mr6_code_structure"),
    "mp": (fig_mp, "fig5_mp_l2_vs_connector"),
}

want = sys.argv[1:] or list(FIGS)
for key in want:
    fn, name = FIGS[key]
    fig = fn()
    fig.savefig(os.path.join(OUT, name + ".png"), dpi=200, facecolor="white")
    fig.savefig(os.path.join(OUT, name + ".svg"), facecolor="white")
    print("wrote", name)
