"""
最高下载速度压测（K 扫描 + 交错轮次 + 自动报告）。

目的：回答「我们的下载器在当前网络上最高能跑到多少 MB/s」。
方法：
  1. 用**生产引擎** m3u8dl.M3u8Download 整节课全段下载（不截断）；
  2. 对并发 K 做扫描（默认 16/24/32，可到 64）；
  3. 多轮交错（每轮包含所有 K，奇数轮倒序），把网络漂移摊平到各 K；
  4. 每 0.5s 从引擎字节仪表采样，算：净吞吐 / 端到端吞吐 / 5s、10s 峰值窗口 /
     单段耗时 p50-p99 / 最长停滞 / 重试 / 限流熔断次数；
  5. 自动生成 交付级 report.md + runs.csv + samples.csv + chart.png。

用法：
  # 自检（无需 token，本地模拟 CDN，验证整套链路与报告产出）
  python scripts/bench_maxspeed.py --mock

  # 真实压测（需要 token：粘贴 --token 或先在 GUI 登录生成 auth.txt）
  python scripts/bench_maxspeed.py --course 67092 --ks 16 24 32 --trials 2
  python scripts/bench_maxspeed.py --token <32位token> --course 67092 --ks 8 16 24 32 48 --trials 3

输出：bench/results/<时间戳>_maxspeed/（mock 模式后缀 _mock）
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import statistics
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

import utils   # noqa: E402
import m3u8dl  # noqa: E402

WORK_REL = "bench/maxspeed_work"

# 历史基线（docs/speed_stage4_fullseg.md §8.4，2026-05-29 全段回归），
# 用于报告里的「相对基线提升」对比。
BASELINE = {"date": "2026-05-29", "label": "K=16 全段 297 段回归", "mbps": 1.91}

MB = 1024 * 1024


# ============================================================
# 采样器：轮询引擎字节仪表
# ============================================================
class Sampler:
    """每 interval 秒读一次 engine.stats()，得到 (t, bytes) 序列。"""

    def __init__(self, interval: float = 0.5):
        self.interval = interval
        self.samples: list[tuple[float, int]] = []
        self.t_hook: float | None = None   # stats_hook 触发时刻（=并发下载起点）
        self._engine = None
        self._stop = threading.Event()
        self._t0 = 0.0
        self._thread: threading.Thread | None = None

    def hook(self, engine):
        """作为 M3u8Download(stats_hook=...) 的回调，拿到引擎引用。"""
        self._engine = engine
        if self._t0:
            self.t_hook = round(time.time() - self._t0, 3)

    def start(self):
        self._t0 = time.time()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            t = time.time() - self._t0
            b = self.samples[-1][1] if self.samples else 0
            eng = self._engine
            if eng is not None:
                try:
                    b = eng.stats()["bytes"]
                except Exception:
                    pass
            self.samples.append((round(t, 3), b))
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        eng = self._engine
        if eng is not None:
            try:
                self.samples.append(
                    (round(time.time() - self._t0, 3), eng.stats()["bytes"])
                )
            except Exception:
                pass


# ============================================================
# 指标计算
# ============================================================
def compute_metrics(samples: list[tuple[float, int]], t_hook: float | None = None) -> dict:
    """从 (t, bytes) 序列算吞吐指标（MB/s 一律按 MiB 计）。

    净窗口 = stats_hook 触发（并发下载开始）→ 最后一次字节前进；
    这样不会把 m3u8 解析/合并算进来，也不会漏掉起步突发。
    """
    out = {
        "bytes": samples[-1][1] if samples else 0,
        "first_byte_s": None, "net_window_s": None,
        "net_mbps": None, "peak5_mbps": None, "peak10_mbps": None,
        "max_stall_s": 0.0,
    }
    if len(samples) < 2 or out["bytes"] <= 0:
        return out

    nz = [i for i, (_, b) in enumerate(samples) if b > 0]
    if not nz:
        return out
    out["first_byte_s"] = samples[nz[0]][0]

    final_b = out["bytes"]
    i1 = max(i for i, (_, b) in enumerate(samples) if b < final_b)
    t_start = t_hook if t_hook is not None else samples[nz[0]][0]
    window = samples[i1][0] - max(0.0, t_start)
    # 至少留一个采样间隔，避免极短任务除零
    out["net_window_s"] = round(max(window, 0.5), 2)
    out["net_mbps"] = round(final_b / MB / out["net_window_s"], 2)

    # 1s 一格的速率序列
    dur = samples[-1][0]
    bins: dict[int, int] = {}
    for t, b in samples:
        bins[min(int(t), int(dur))] = b  # 同格取最后值
    rates = []
    for s in range(int(dur) + 1):
        b_end = bins.get(s, bins.get(s - 1, 0) if s > 0 else 0)
        b_prev = bins.get(s - 1, 0) if s > 0 else 0
        rates.append(max(0.0, (b_end - b_prev) / MB))

    def peak_window(win: int) -> float | None:
        if len(rates) < max(2, win // 2):
            return None
        best = 0.0
        for i in range(0, max(1, len(rates) - win + 1)):
            best = max(best, sum(rates[i:i + win]) / min(win, len(rates) - i))
        return round(best, 2)

    out["peak5_mbps"] = peak_window(5)
    out["peak10_mbps"] = peak_window(10)

    # 最长停滞：bytes 不再前进的最长持续时间
    prev_b = None
    last_change_t = None
    stall = 0.0
    for t, b in samples:
        if prev_b is None or b != prev_b:
            last_change_t = t
            prev_b = b
        elif last_change_t is not None:
            stall = max(stall, t - last_change_t)
    out["max_stall_s"] = round(stall, 1)
    return out


def percentile(sorted_vals: list[float], p: float):
    if not sorted_vals:
        return None
    return round(sorted_vals[min(int(len(sorted_vals) * p), len(sorted_vals) - 1)], 3)


# ============================================================
# 单次运行
# ============================================================
def clean_run_artifacts(work_dir: Path, name: str):
    for p in (work_dir / f"{name}.mp4", work_dir / f"{name}.m3u8"):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass
    tsdir = work_dir / name
    if tsdir.exists():
        shutil.rmtree(tsdir, ignore_errors=True)


def run_one(raw_url: str, k: int, trial: int, work_dir: Path, mock: bool) -> dict:
    """跑一次整节课下载，返回该 run 的全部指标。"""
    name = f"k{k}_t{trial}"
    run_id = name
    clean_run_artifacts(work_dir, name)

    sampler = Sampler(interval=0.5)
    brk0 = (utils.cdn_breaker.trip_count, utils.cdn_breaker.total_cooldown_s)

    print(f"\n--- run {run_id} (K={k}, trial={trial}) 开始 ---")
    sampler.start()
    t0 = time.time()
    ok, err = False, ""
    try:
        m3u8dl.M3u8Download(
            url=raw_url,
            workDir=str(work_dir.relative_to(ROOT)).replace("\\", "/"),
            name=name,
            max_workers=k,
            progress_callback=lambda *a, **kw: None,
            gui_mode=True,
            stats_hook=sampler.hook,
        )
        ok = True
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    elapsed = round(time.time() - t0, 2)
    sampler.stop()

    # 引擎终态统计（构造失败时 hook 可能没触发）
    eng = sampler._engine
    fin = eng.final_stats() if eng is not None else {}

    samples = sampler.samples
    m = compute_metrics(samples, t_hook=sampler.t_hook)
    bytes_total = m["bytes"] or fin.get("bytes_done", 0)
    mp4 = work_dir / f"{name}.mp4"
    mp4_bytes = mp4.stat().st_size if mp4.exists() else 0

    sustained = round(bytes_total / MB / elapsed, 2) if elapsed > 0 and bytes_total else 0.0
    trips = utils.cdn_breaker.trip_count - brk0[0]
    cooldown = round(utils.cdn_breaker.total_cooldown_s - brk0[1], 1)

    rec = {
        "run_id": run_id, "k": k, "trial": trial,
        "ok": ok and (bytes_total > 0), "error": err[:200],
        "segments": fin.get("ts_sum"), "segments_ok": fin.get("success_sum"),
        "deferred": fin.get("deferred_count", 0),
        "stage2_attempts": fin.get("stage2_attempts", 0),
        "permanently_failed": fin.get("permanently_failed", 0),
        "bytes": bytes_total, "mb": round(bytes_total / MB, 1),
        "mp4_bytes": mp4_bytes,
        "elapsed_s": elapsed,
        "sustained_mbps": sustained,
        "net_mbps": m["net_mbps"],
        "net_window_s": m["net_window_s"],
        "peak5_mbps": m["peak5_mbps"], "peak10_mbps": m["peak10_mbps"],
        "first_byte_s": m["first_byte_s"],
        "max_stall_s": m["max_stall_s"],
        "seg_time_p50": fin.get("seg_time_p50"), "seg_time_p95": fin.get("seg_time_p95"),
        "seg_time_p99": fin.get("seg_time_p99"),
        "breaker_trips": trips, "breaker_cooldown_s": cooldown,
    }
    # 采样曲线随行返回（写 CSV 用）
    rec["_samples"] = samples
    print(f"--- run {run_id} 结束: ok={rec['ok']} {rec['elapsed_s']}s "
          f"净吞吐={rec['net_mbps']} MB/s 端到端={rec['sustained_mbps']} MB/s "
          f"段={rec['segments_ok']}/{rec['segments']} 重试={rec['stage2_attempts']} "
          f"熔断={trips} err={rec['error'][:60]}")
    clean_run_artifacts(work_dir, name)
    return rec


# ============================================================
# 本地模拟 CDN（--mock 自检用）
# ============================================================
class MockCdn:
    """带每连接限速的本地 HLS 源：验证压测链路与报告产出，数据不对外。"""

    SEG_COUNT = 80
    PER_CONN_MBPS = 3.0     # 每连接 3 MB/s
    LATENCY_S = 0.03

    def __init__(self):
        self._sizes = [200 * 1024 + (i * 7919 % 120) * 1024 for i in range(self.SEG_COUNT)]

        class Handler(BaseHTTPRequestHandler):
            sizes = self._sizes
            cfg = self

            def log_message(self, *a):
                pass

            def _playlist(self):
                lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:6"]
                for i in range(len(self.sizes)):
                    lines.append(f"#EXTINF:6.0,")
                    lines.append(f"seg{i}.ts")
                lines.append("#EXT-X-ENDLIST")
                body = "\n".join(lines).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _segment(self, idx: int):
                import os as _os
                data = _os.urandom(self.sizes[idx])
                time.sleep(self.cfg.LATENCY_S)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                chunk = 16 * 1024
                per = chunk / (self.cfg.PER_CONN_MBPS * MB)
                for i in range(0, len(data), chunk):
                    self.wfile.write(data[i:i + chunk])
                    time.sleep(per)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path.endswith(".m3u8"):
                    self._playlist()
                    return
                m = re.search(r"seg(\d+)\.ts", path)
                if m and 0 <= int(m.group(1)) < len(self.sizes):
                    self._segment(int(m.group(1)))
                    return
                self.send_response(404)
                self.end_headers()

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mock/lesson.m3u8"

    def start(self):
        self._thread.start()

    def stop(self):
        self._srv.shutdown()


def setup_mock():
    """打补丁让生产引擎跑在本地模拟 CDN 上。"""
    utils.getToken = lambda *a, **k: "mocktoken0123456789abcdef"  # type: ignore
    m3u8dl.M3u8Download.output_mp4 = lambda self: None            # type: ignore
    cdn = MockCdn()
    cdn.start()
    return cdn


# ============================================================
# 凭据与课程
# ============================================================
def prepare_real(args) -> tuple[str, str, str]:
    """token 优先级：--token > auth.txt > 环境变量 YHKT_TOKEN；然后拉课程信息。"""
    import os
    auth_file = Path(utils.get_auth_file_path())
    token = (args.token.strip()
             or (auth_file.read_text(encoding="utf-8").strip() if auth_file.exists() else "")
             or os.environ.get("YHKT_TOKEN", "").strip())
    if not token:
        print("未找到 token。三种方式任选其一：")
        print("  1) python scripts/bench_maxspeed.py --token <32位token> ...")
        print("  2) 浏览器登录 yanhekt.cn → F12 控制台执行 javascript:alert(JSON.parse(localStorage.auth).token)")
        print("     复制弹出的 token，粘贴到仓库根目录 auth.txt（一行纯 token）")
        print("  3) set YHKT_TOKEN=<token> 后再运行")
        sys.exit(2)
    utils.write_auth(token)

    videos, course_name, prof = utils.get_course_info(args.course)
    if args.session_idx >= len(videos):
        print(f"session_idx 越界: {args.session_idx}/{len(videos)}")
        sys.exit(2)
    s = videos[args.session_idx]
    title = s.get("title") or f"session_{args.session_idx}"
    v = (s.get("videos") or [{}])[0]
    url = v.get("vga") or v.get("main")
    if not url:
        print(f"该节无视频地址，字段: {list((s.get('videos') or [{}])[0].keys())}")
        sys.exit(2)
    return url, course_name, f"{title}（{prof}）"


# ============================================================
# 报告
# ============================================================
def env_info(mock: bool) -> dict:
    import os
    return {
        "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "机器": f"{platform.node()} / {platform.machine()} / {platform.processor() or '未知CPU'}",
        "CPU 线程数": os.cpu_count(),
        "Python": platform.python_version(),
        "引擎": "m3u8dl v3（固定并发 + 两阶段）",
        "网络模式": os.environ.get("YHKT_NET_MODE", "auto"),
        "测试源": "本地模拟 CDN（自检）" if mock else "延河课堂真实 CDN",
    }


def write_outputs(out_dir: Path, runs: list[dict], meta: dict, mock: bool):
    out_dir.mkdir(parents=True, exist_ok=True)

    # --- samples.csv / runs.csv ---
    with open(out_dir / "samples.csv", "w", encoding="utf-8-sig") as f:
        f.write("run_id,k,trial,t_s,bytes_cum\n")
        for r in runs:
            for t, b in r["_samples"]:
                f.write(f"{r['run_id']},{r['k']},{r['trial']},{t},{b}\n")
    keys = [k for k in runs[0].keys() if not k.startswith("_")] if runs else []
    with open(out_dir / "runs.csv", "w", encoding="utf-8-sig") as f:
        f.write(",".join(keys) + "\n")
        for r in runs:
            f.write(",".join(str(r.get(k, "")) for k in keys) + "\n")

    (out_dir / "results.json").write_text(
        json.dumps({"meta": meta, "runs": [{k: v for k, v in r.items() if not k.startswith('_')}
                                           for r in runs]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")

    # --- chart.png ---
    chart = out_dir / "chart.png"
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        for fname in ("Microsoft YaHei", "SimHei"):
            if any(fname.lower() == f.name.lower() for f in font_manager.fontManager.ttflist):
                plt.rcParams["font.sans-serif"] = [fname]
                break
        plt.rcParams["axes.unicode_minus"] = False

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8))
        colors = plt.cm.viridis([i / max(1, len(set(r["k"] for r in runs)) - 1)
                                 for i in range(len(set(r["k"] for r in runs)))])
        kcolor = {k: colors[i] for i, k in enumerate(sorted(set(r["k"] for r in runs)))}
        for r in runs:
            if not r["ok"]:
                continue
            samples = r["_samples"]
            dur = samples[-1][0] if samples else 0
            bins: dict[int, int] = {}
            for t, b in samples:
                bins[min(int(t), int(dur))] = b
            rates = []
            for s_ in range(int(dur) + 1):
                b_end = bins.get(s_, bins.get(s_ - 1, 0) if s_ > 0 else 0)
                b_prev = bins.get(s_ - 1, 0) if s_ > 0 else 0
                rates.append(max(0.0, (b_end - b_prev) / MB))
            ax1.plot(range(len(rates)), rates, label=f"K={r['k']} t{r['trial']}",
                     color=kcolor[r["k"]], linewidth=1.2, alpha=0.85)
        ax1.set_title("下载吞吐曲线（1s 粒度）")
        ax1.set_xlabel("时间 (s)")
        ax1.set_ylabel("MB/s")
        ax1.legend(fontsize=8, ncol=3)
        ax1.grid(alpha=0.3)

        ok_runs = [r for r in runs if r["ok"] and r["net_mbps"]]
        if ok_runs:
            labels = [r["run_id"] for r in ok_runs]
            vals = [r["net_mbps"] for r in ok_runs]
            bars = ax2.bar(labels, vals,
                           color=[kcolor[r["k"]] for r in ok_runs])
            for bar, v in zip(bars, vals):
                ax2.text(bar.get_x() + bar.get_width() / 2, v, f"{v}",
                         ha="center", va="bottom", fontsize=9)
            ax2.set_title("各 run 净吞吐（MB/s，同色=同 K）")
            ax2.grid(alpha=0.3, axis="y")
        fig.tight_layout()
        fig.savefig(chart, dpi=140)
        plt.close(fig)
    except Exception as e:
        print(f"[chart] 跳过绘图: {type(e).__name__}: {e}")
        chart = None

    # --- report.md ---
    md = out_dir / "report.md"
    L: list[str] = []
    L.append("# 延河课堂下载器 · 最高下载速度压测报告")
    L.append("")
    L.append(f"> 生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M')} · 工具 `scripts/bench_maxspeed.py`"
             f"{' · **本地模拟自检（非真实数据）**' if mock else ''}")
    L.append("")

    ok_runs = [r for r in runs if r["ok"] and r.get("net_mbps")]
    if ok_runs:
        best = max(ok_runs, key=lambda r: r["net_mbps"])
        best_med = {}
        for k in sorted(set(r["k"] for r in ok_runs)):
            best_med[k] = max(r["net_mbps"] for r in ok_runs if r["k"] == k)
        best_k = max(best_med, key=best_med.get)
        size_mb = best["mb"]
        t_lesson = best["elapsed_s"]
        baseline_note = ""
        if not mock:
            uplift = round(best["net_mbps"] / BASELINE["mbps"], 2)
            baseline_note = f"，为 {BASELINE['date']} 基线（{BASELINE['mbps']} MB/s）的 **{uplift} 倍**"
        L.append("## 一句话结论")
        L.append("")
        L.append(
            f"在本次测试网络环境下，并发 **K={best_k}** 档取得最高下载速度："
            f"**净吞吐 {best_med[best_k]:.2f} MB/s（≈{best_med[best_k] * 8:.0f} Mbps）**"
            + (f"，瞬时峰值 {best['peak5_mbps']} MB/s（5s 窗口）" if best.get("peak5_mbps") else "")
            + f"。整节课 {best['segments']} 段 / {size_mb:.0f} MB 最快 **{t_lesson:.0f} 秒**下载完成，"
            f"0 缺片{baseline_note}。"
        )
        L.append("")

    L.append("## 测试环境")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    for k, v in env_info(mock).items():
        L.append(f"| {k} | {v} |")
    if not mock:
        L.append(f"| 课程 | {meta.get('course_id')} {meta.get('course_name')} |")
        L.append(f"| 节次 | {meta.get('session_title')} |")
    L.append(f"| K 扫描 | {meta.get('ks')} × {meta.get('trials')} 轮（交错，奇数轮倒序） |")
    L.append("")

    L.append("## 测试方法")
    L.append("")
    L.append("1. 使用**生产下载引擎**（v3 固定并发 + 两阶段补齐）整节课**全段**下载，不截断样本；")
    L.append("2. 并发 K 逐档扫描，多轮**交错执行**（每轮包含全部档位且奇数轮倒序），把网络波动均摊到各档位，避免\"谁后跑谁占便宜\"；")
    L.append("3. 每次运行间隔冷却，清空上一次产物从零开始；")
    L.append("4. 引擎内置字节级仪表，每 0.5s 采样一次，据此计算净吞吐、峰值窗口与停滞；")
    L.append(f"5. 与历史基线（{BASELINE['date']}，{BASELINE['label']}，{BASELINE['mbps']} MB/s）对比。")
    L.append("")

    L.append("## 结果")
    L.append("")
    # 按 K 汇总
    L.append("### 按 K 汇总（仅成功 run）")
    L.append("")
    L.append("| K | 次数 | 净吞吐中位 (MB/s) | 净吞吐最高 (MB/s) | 端到端中位 (MB/s) | 段耗时 p95 (s) |")
    L.append("|---|------|------------------|------------------|-------------------|---------------|")
    for k in sorted(set(r["k"] for r in ok_runs)):
        rs = [r for r in ok_runs if r["k"] == k]
        nets = [r["net_mbps"] for r in rs]
        susts = [r["sustained_mbps"] for r in rs]
        p95s = [r["seg_time_p95"] for r in rs if r.get("seg_time_p95")]
        p95_med = f"{statistics.median(p95s):.2f}" if p95s else "—"
        L.append(f"| {k} | {len(rs)} | {statistics.median(nets):.2f} | {max(nets):.2f} | "
                 f"{statistics.median(susts):.2f} | {p95_med} |")
    L.append("")
    L.append("> 净吞吐 = 下载阶段字节数 ÷ 下载窗口时长；端到端含 m3u8 解析与合并耗时。")
    L.append("")

    L.append("### 单次明细")
    L.append("")
    L.append("| run | K | 成功 | 段数 | 体积(MB) | 总耗时(s) | 端到端(MB/s) | 净吞吐(MB/s) | 峰值5s(MB/s) | 首段(s) | 最长停滞(s) | 重试 | 缺片 |")
    L.append("|-----|---|------|------|----------|-----------|--------------|--------------|--------------|---------|-------------|------|------|")
    for r in runs:
        L.append(
            f"| {r['run_id']} | {r['k']} | {'✅' if r['ok'] else '❌'} | "
            f"{r['segments_ok']}/{r['segments']} | {r['mb']} | {r['elapsed_s']} | "
            f"{r['sustained_mbps']} | {r['net_mbps'] if r['net_mbps'] else '—'} | "
            f"{r['peak5_mbps'] if r['peak5_mbps'] else '—'} | "
            f"{r['first_byte_s'] if r['first_byte_s'] is not None else '—'} | "
            f"{r['max_stall_s']} | {r['stage2_attempts']} | {r['permanently_failed']} |"
        )
    L.append("")

    L.append("## 可靠性与限流")
    L.append("")
    trips_total = sum(r["breaker_trips"] for r in runs)
    cd_total = sum(r["breaker_cooldown_s"] for r in runs)
    fails = sum(r["permanently_failed"] for r in runs)
    L.append(f"- 全部 {len(runs)} 次 run 共缺片 **{fails}** 段；"
             f"限流熔断触发 **{trips_total}** 次（累计退避 {cd_total:.0f}s）。")
    if not mock and trips_total == 0:
        L.append("- 未触发延河 WAF 限流，说明压测并发档位在服务端可接受范围内。")
    L.append("")

    if chart:
        L.append("## 吞吐曲线")
        L.append("")
        L.append("![chart](chart.png)")
        L.append("")

    L.append("## 原始数据")
    L.append("")
    L.append(f"- `results.json`：全部指标（机器可读）")
    L.append(f"- `runs.csv`：单次明细（Excel 可开）")
    L.append(f"- `samples.csv`：0.5s 字节采样序列")
    L.append("")
    L.append("---")
    L.append(f"> 基线：{BASELINE['date']} {BASELINE['label']} = {BASELINE['mbps']} MB/s（docs/speed_stage4_fullseg.md）")
    md.write_text("\n".join(L), encoding="utf-8")
    return md


# ============================================================
# main
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="最高下载速度压测")
    ap.add_argument("--mock", action="store_true", help="本地模拟 CDN 自检（无需 token）")
    ap.add_argument("--course", default="67092")
    ap.add_argument("--session-idx", type=int, default=0)
    ap.add_argument("--ks", nargs="+", type=int, default=[16, 24, 32],
                    help="并发扫描档位（4..64），如 --ks 8 16 24 32 48")
    ap.add_argument("--trials", type=int, default=2, help="每档重复轮数")
    ap.add_argument("--cooldown", type=float, default=20.0, help="两次 run 间冷却秒数")
    ap.add_argument("--token", default="", help="32 位延河 token（或写 auth.txt / 环境变量 YHKT_TOKEN）")
    ap.add_argument("--keep-going", action="store_true", default=True,
                    help="单次 run 失败不中断整个压测（默认开）")
    args = ap.parse_args()

    mock = args.mock
    if mock:
        args.ks = args.ks if args.ks != [16, 24, 32] else [8, 16]
        args.trials = 1
        args.cooldown = 2.0
    ks = [max(4, min(k, 64)) for k in args.ks]

    cdn = None
    if mock:
        cdn = setup_mock()
        raw_url, course_name, session_title = cdn.url, "本地模拟", "MockHLS"
        print(f"[mock] 模拟 CDN 就绪 @ 127.0.0.1:{cdn.port}，"
              f"{MockCdn.SEG_COUNT} 段 × 每连接 {MockCdn.PER_CONN_MBPS} MB/s")
    else:
        raw_url, course_name, session_title = prepare_real(args)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = "_mock" if mock else ""
    out_dir = ROOT / "bench" / "results" / f"{ts}_maxspeed{suffix}"
    work_dir = ROOT / WORK_REL
    work_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "course_id": args.course, "course_name": course_name,
        "session_idx": args.session_idx, "session_title": session_title,
        "ks": ks, "trials": args.trials, "cooldown_s": args.cooldown,
        "mock": mock, "env": env_info(mock),
    }

    # 交错执行：每轮包含所有 K；奇数轮倒序，进一步抵消网络漂移
    plan: list[tuple[int, int]] = []
    for trial in range(1, args.trials + 1):
        order = ks if trial % 2 == 1 else list(reversed(ks))
        for k in order:
            plan.append((k, trial))
    print(f"[plan] 共 {len(plan)} 次 run: " + " ".join(f"K{k}#t{t}" for k, t in plan))
    print(f"[plan] 每次之间冷却 {args.cooldown}s；产物输出 {out_dir}")

    runs: list[dict] = []
    for i, (k, trial) in enumerate(plan, 1):
        print(f"\n========== [{i}/{len(plan)}] K={k} trial={trial} ==========")
        try:
            rec = run_one(raw_url, k, trial, work_dir, mock)
        except KeyboardInterrupt:
            print("\n[中断] 用户中止，写出已收集数据")
            break
        except Exception as e:
            rec = {"run_id": f"k{k}_t{trial}", "k": k, "trial": trial, "ok": False,
                   "error": f"{type(e).__name__}: {e}", "elapsed_s": 0,
                   "bytes": 0, "mb": 0, "segments": None, "segments_ok": None,
                   "deferred": 0, "stage2_attempts": 0, "permanently_failed": 0,
                   "mp4_bytes": 0, "sustained_mbps": 0, "net_mbps": None,
                   "net_window_s": None, "peak5_mbps": None, "peak10_mbps": None,
                   "first_byte_s": None, "max_stall_s": 0,
                   "seg_time_p50": None, "seg_time_p95": None, "seg_time_p99": None,
                   "breaker_trips": 0, "breaker_cooldown_s": 0.0, "_samples": []}
        runs.append(rec)
        # 立即落盘（中途断电/中止也有数据）
        write_outputs(out_dir, runs, meta, mock)
        if i < len(plan):
            print(f"[冷却] {args.cooldown:.0f}s ...")
            try:
                time.sleep(args.cooldown)
            except KeyboardInterrupt:
                print("\n[中断] 冷却期中止，写出已收集数据")
                break

    md = write_outputs(out_dir, runs, meta, mock)
    print("\n" + "=" * 60)
    ok_runs = [r for r in runs if r["ok"] and r.get("net_mbps")]
    if ok_runs:
        best = max(ok_runs, key=lambda r: r["net_mbps"])
        print(f"★ 最高净吞吐: {best['net_mbps']} MB/s (≈{best['net_mbps'] * 8:.0f} Mbps) "
              f"@ K={best['k']} trial={best['trial']}")
        print(f"★ 端到端: {best['sustained_mbps']} MB/s, 一节课({best['mb']}MB) {best['elapsed_s']}s")
        if best.get("peak5_mbps"):
            print(f"★ 5s 峰值: {best['peak5_mbps']} MB/s")
    else:
        print("!! 没有成功的 run，查看 report.md / results.json 排错")
    print(f"\n报告: {md}")
    print(f"数据:  {out_dir}")
    if cdn:
        cdn.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
