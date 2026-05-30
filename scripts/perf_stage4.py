"""
阶段 4：全段验证 —— 真实生产引擎 vs 实验室胜出方案。

和 perf_yhkt.py 的关键区别：
  perf_yhkt 只跑 m3u8dl_lab（lab 的 `cur` 只是“模拟” AIMD，没有 watchdog/尾部模式）。
  本脚本直接驱动**真实的 m3u8dl.M3u8Download**，把它和 lab 候选放在同一条 297 段
  全量任务上对打——只有全段才能压出尾部卡死 / watchdog 自重启这类长尾问题。

候选用 "spec" 描述：
  prod:32          真实 m3u8dl.py，max_workers=32（GUI 默认）
  prod:16          真实 m3u8dl.py，max_workers=16
  v_winner:8       lab：删 AIMD + 二进制合并，K=8
  v1_aimd_off:8    lab：删 AIMD + ffmpeg 合并，K=8
  v_combo:16       lab：删 AIMD + 二进制合并 + 原子计数，K=16

用法：
  python scripts/perf_stage4.py --course 67092 --specs prod:32 v_winner:8 --trials 1
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

import utils  # noqa: E402
import m3u8dl  # noqa: E402
from m3u8dl_lab import M3u8DownloadLab, LabConfig, VARIANTS  # noqa: E402


WORK_REL = "bench/stage4_work"  # 相对 ROOT


class ProgressRecorder:
    """记录每次进度回调的时间戳，导出吞吐曲线 + 停滞检测。"""

    def __init__(self):
        self.t0 = time.time()
        self.samples: list[tuple[float, int, int, int, int]] = []  # (t, done, total, threads, maxthreads)
        self.first_byte_t: float | None = None

    def __call__(self, done, total, status, threads=0, max_threads=0):
        t = round(time.time() - self.t0, 2)
        if done and self.first_byte_t is None:
            self.first_byte_t = t
        # 只在 done 变化时记录，控制体积
        if not self.samples or self.samples[-1][1] != done:
            self.samples.append((t, int(done or 0), int(total or 0), int(threads or 0), int(max_threads or 0)))

    def stall_stats(self):
        """返回最长无进度间隔（秒）和发生时刻。"""
        if len(self.samples) < 2:
            return 0.0, 0.0
        max_gap = 0.0
        at = 0.0
        for (t1, d1, *_), (t2, d2, *_) in zip(self.samples, self.samples[1:]):
            gap = t2 - t1
            if gap > max_gap:
                max_gap = gap
                at = t1
        return round(max_gap, 1), round(at, 1)

    def thread_range(self):
        if not self.samples:
            return 0, 0
        mins = min(s[3] for s in self.samples)
        maxs = max(s[4] for s in self.samples)
        return mins, maxs


def fetch_vga(course_id: str, session_idx: int) -> tuple[str, str, str]:
    """返回 (raw_vga_url, course_name, session_title)。注意：未做 encryptURL。"""
    utils.read_auth()
    videos, name, prof = utils.get_course_info(course_id)
    if not videos or session_idx >= len(videos):
        raise RuntimeError(f"无效 session_idx {session_idx}/{len(videos)}")
    s = videos[session_idx]
    title = s.get("title") or f"session_{session_idx}"
    v = (s.get("videos") or [{}])[0]
    url = v.get("vga") or v.get("main")
    if not url:
        raise RuntimeError(f"session 无 url, 字段: {list(v.keys())}")
    return url, name, title


def clean_run_artifacts(work_dir: Path, name: str):
    """删除某次 run 的 mp4 / ts 目录 / m3u8，保证重测从零开始。"""
    mp4 = work_dir / f"{name}.mp4"
    tsdir = work_dir / name
    m3u8 = work_dir / f"{name}.m3u8"
    for p in (mp4, m3u8):
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    if tsdir.exists():
        shutil.rmtree(tsdir, ignore_errors=True)


def run_prod(raw_vga: str, workers: int, name: str, work_dir: Path) -> dict:
    """跑真实生产引擎 m3u8dl.M3u8Download。"""
    rec = ProgressRecorder()
    work_rel = str(work_dir.relative_to(ROOT)).replace("\\", "/")
    clean_run_artifacts(work_dir, name)

    t0 = time.time()
    err = ""
    ok = False
    final_state = {}
    try:
        dl = m3u8dl.M3u8Download(
            url=raw_vga,
            workDir=work_rel,
            name=name,
            max_workers=workers,
            progress_callback=rec,
            gui_mode=True,
        )
        ok = True
        final_state = {
            "success_sum": getattr(dl, "_success_sum", None),
            "ts_sum": getattr(dl, "_ts_sum", None),
            "tail_mode": getattr(dl, "_tail_mode", None),
            "watchdog_triggered": getattr(dl, "_watchdog_triggered", None),
            "permanently_failed": len(getattr(dl, "_permanently_failed", []) or []),
        }
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    elapsed = round(time.time() - t0, 2)

    mp4 = work_dir / f"{name}.mp4"
    size = mp4.stat().st_size if mp4.exists() else 0
    max_gap, gap_at = rec.stall_stats()
    tmin, tmax = rec.thread_range()
    return {
        "engine": "prod",
        "workers": workers,
        "ok": ok and size > 0,
        "error": err,
        "elapsed_s": elapsed,
        "bytes_total": size,
        "throughput_mbps": round(size / 1024 / 1024 / elapsed, 2) if elapsed > 0 and size else 0,
        "first_byte_s": rec.first_byte_t,
        "max_stall_s": max_gap,
        "max_stall_at_s": gap_at,
        "thread_min": tmin,
        "thread_max": tmax,
        "samples": len(rec.samples),
        **final_state,
    }


def run_lab(variant_id: str, raw_vga: str, workers: int, name: str, work_dir: Path) -> dict:
    """跑 lab 候选。lab 需要 encryptURL 过的 URL。"""
    rec = ProgressRecorder()
    cfg = VARIANTS[variant_id]
    cfg = LabConfig(**{**cfg.__dict__,
                       "fixed_workers": workers,
                       "aimd_initial_workers": min(16, workers),
                       "max_segments": None,
                       "progress_callback": rec})
    work_rel = str(work_dir.relative_to(ROOT)).replace("\\", "/")
    clean_run_artifacts(work_dir, name)

    signed_url = utils.encryptURL(raw_vga)
    dl = M3u8DownloadLab(signed_url, work_rel, name, cfg)
    r = dl.run()

    max_gap, gap_at = rec.stall_stats()
    return {
        "engine": variant_id,
        "workers": workers,
        "ok": r.ok,
        "error": r.error,
        "elapsed_s": r.elapsed_s,
        "bytes_total": r.bytes_total,
        "throughput_mbps": r.throughput_mbps,
        "first_byte_s": rec.first_byte_t,
        "max_stall_s": max_gap,
        "max_stall_at_s": gap_at,
        "parse_m3u8_s": r.parse_m3u8_s,
        "download_s": r.download_s,
        "merge_s": r.merge_s,
        "merge_method": r.merge_method,
        "segments_total": r.segments_total,
        "segments_done": r.segments_done,
        "segments_failed": r.segments_failed,
        "retry_count": r.retry_count,
    }


def render_report(out_dir: Path, results: list[dict], meta: dict) -> Path:
    md = out_dir / "report.md"
    L: list[str] = []
    L.append("# 阶段 4：全段下载验证（生产引擎 vs 实验室胜出）")
    L.append("")
    L.append(f"- 时间：`{meta['started']}`")
    L.append(f"- 课程 `{meta['course_id']}` 第 {meta['session_idx']} 节 —— {meta['course_name']} / {meta['session_title']}")
    L.append(f"- 全段（不截断），每候选 {meta['trials']} 次")
    L.append("")
    L.append("## 结果")
    L.append("")
    L.append("| 引擎 | K | 试 | 成功 | 耗时(s) | 吞吐(MB/s) | 体积(MB) | 首段(s) | 最长停滞(s)@时刻 | 尾部模式 | 看门狗 | 失败/缺片 | 备注 |")
    L.append("|------|---|----|------|---------|------------|----------|---------|------------------|----------|--------|-----------|------|")
    for r in results:
        size_mb = round(r.get("bytes_total", 0) / 1024 / 1024, 1)
        tail = r.get("tail_mode", "—")
        wd = r.get("watchdog_triggered", "—")
        failed = r.get("permanently_failed", r.get("segments_failed", "—"))
        stall = f"{r.get('max_stall_s', 0)}@{r.get('max_stall_at_s', 0)}"
        L.append(
            f"| `{r['engine']}` | {r['workers']} | {r.get('trial', 1)} | "
            f"{'✅' if r['ok'] else '❌'} | {r['elapsed_s']} | {r['throughput_mbps']} | "
            f"{size_mb} | {r.get('first_byte_s', '—')} | {stall} | {tail} | {wd} | "
            f"{failed} | {(r.get('error') or '')[:30]} |"
        )
    L.append("")

    ok_only = [r for r in results if r["ok"]]
    if ok_only:
        ranked = sorted(ok_only, key=lambda r: r["elapsed_s"])
        best = ranked[0]
        L.append("## 排行（按总耗时，仅成功项）")
        L.append("")
        L.append(f"**最快**：`{best['engine']}` K={best['workers']} → {best['elapsed_s']}s, {best['throughput_mbps']} MB/s")
        L.append("")
        L.append("| 排名 | 引擎 | K | 耗时(s) | 吞吐(MB/s) | 相对最快 |")
        L.append("|------|------|---|---------|------------|----------|")
        for i, r in enumerate(ranked, 1):
            ratio = r["elapsed_s"] / best["elapsed_s"]
            L.append(f"| {i} | `{r['engine']}` | {r['workers']} | {r['elapsed_s']} | {r['throughput_mbps']} | x{ratio:.2f} |")
        L.append("")

    L.append("---")
    L.append("> 生成 by `scripts/perf_stage4.py`，原始数据见 `results.json`。")
    md.write_text("\n".join(L), encoding="utf-8")
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", default="67092")
    ap.add_argument("--session-idx", type=int, default=0)
    ap.add_argument("--specs", nargs="+", default=["prod:32", "v_winner:8", "v1_aimd_off:8"],
                    help="候选列表，形如 prod:32 v_winner:8")
    ap.add_argument("--trials", type=int, default=1)
    args = ap.parse_args()

    print(f"[init] 拉课程 {args.course} 第 {args.session_idx} 节 ...")
    raw_vga, course_name, session_title = fetch_vga(args.course, args.session_idx)
    print(f"[init] {course_name} / {session_title}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "bench" / "results" / f"{ts}_stage4"
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = ROOT / WORK_REL
    work_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "course_id": args.course,
        "course_name": course_name,
        "session_idx": args.session_idx,
        "session_title": session_title,
        "specs": args.specs,
        "trials": args.trials,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    results: list[dict] = []
    total = len(args.specs) * args.trials
    counter = 0
    for spec in args.specs:
        eng, _, k = spec.partition(":")
        workers = int(k) if k else 16
        for trial in range(1, args.trials + 1):
            counter += 1
            name = f"{eng}_w{workers}_t{trial}"
            print(f"\n=== [{counter}/{total}] {spec} trial={trial} ===")
            t_start = time.time()
            try:
                if eng == "prod":
                    rec = run_prod(raw_vga, workers, name, work_dir)
                elif eng in VARIANTS:
                    rec = run_lab(eng, raw_vga, workers, name, work_dir)
                else:
                    print(f"  跳过未知引擎: {eng}")
                    continue
            except Exception as e:
                rec = {"engine": eng, "workers": workers, "ok": False,
                       "error": f"{type(e).__name__}: {e}", "elapsed_s": round(time.time() - t_start, 2),
                       "bytes_total": 0, "throughput_mbps": 0}
            rec["trial"] = trial
            results.append(rec)
            print(f"  -> ok={rec['ok']} {rec['elapsed_s']}s {rec.get('throughput_mbps')}MB/s "
                  f"stall={rec.get('max_stall_s')}s tail={rec.get('tail_mode', '—')} "
                  f"wd={rec.get('watchdog_triggered', '—')} err={rec.get('error', '')[:40]}")
            # 立即落盘
            (out_dir / "results.json").write_text(
                json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            # 清掉这次产物，给下一次腾空间
            clean_run_artifacts(work_dir, name)

    md = render_report(out_dir, results, meta)
    print(f"\n=== 报告：{md}")
    print(f"=== 数据：{out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
