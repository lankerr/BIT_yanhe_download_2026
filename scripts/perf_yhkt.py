"""
延河场景的下载速度对照实验跑批 harness。

阶段 1：6 方案快速筛
  python scripts/perf_yhkt.py --stage 1

阶段 2：top-2 方案 K 扫描
  python scripts/perf_yhkt.py --stage 2 --variants v1_aimd_off,v_combo --workers 4 8 16 24 32

阶段 3：决胜
  python scripts/perf_yhkt.py --stage 3 --variant v_combo --workers 8 --trials 3
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

# Windows cmd 默认 GBK，强制 utf-8
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore
except Exception:
    pass

import utils  # noqa: E402
from m3u8dl_lab import M3u8DownloadLab, LabConfig, VARIANTS  # noqa: E402


def fetch_session_url(course_id: str, session_idx: int = 0,
                     dl_type: str = "screen") -> tuple[str, str, str]:
    """返回 (m3u8_url, course_name, session_title)。"""
    utils.read_auth()
    videos, name, prof = utils.get_course_info(course_id)
    if not videos or session_idx >= len(videos):
        raise RuntimeError(f"无效 session_idx {session_idx}/{len(videos)}")
    s = videos[session_idx]
    title = s.get("title") or f"session_{session_idx}"
    v = (s.get("videos") or [{}])[0]
    url = v.get("vga") if dl_type == "screen" else v.get("main")
    if not url:
        url = v.get("main") or v.get("vga")
    if not url:
        raise RuntimeError(f"session 无 url, 字段: {list(v.keys())}")
    # 加 m3u8 鉴权标记
    url = utils.encryptURL(url)
    return url, name, title


def run_one(variant_id: str, cfg: LabConfig, m3u8_url: str,
            workers: int, segments: int | None,
            out_dir: Path) -> dict:
    """单次实验。"""
    # 应用本次的 workers + max_segments
    cfg = LabConfig(**{**cfg.__dict__, "fixed_workers": workers,
                       "aimd_initial_workers": min(16, workers),
                       "max_segments": segments})
    name = f"{variant_id}_w{workers}_s{segments or 'all'}"

    # 清产物
    work_dir = out_dir / "work"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[run] {name}")
    dl = M3u8DownloadLab(m3u8_url, str(work_dir.relative_to(ROOT)), name, cfg)
    r = dl.run()

    rec = {
        "variant": variant_id,
        "workers": workers,
        "segments": segments,
        "ok": r.ok,
        "error": r.error,
        "elapsed_s": r.elapsed_s,
        "parse_m3u8_s": r.parse_m3u8_s,
        "download_s": r.download_s,
        "merge_s": r.merge_s,
        "merge_method": r.merge_method,
        "bytes_total": r.bytes_total,
        "throughput_mbps": r.throughput_mbps,
        "per_seg_ms": r.per_seg_ms,
        "segments_total": r.segments_total,
        "segments_done": r.segments_done,
        "segments_failed": r.segments_failed,
        "retry_count": r.retry_count,
        "failed_segments": r.failed_segments[:10],
        "aimd_window_history": r.aimd_window_history[-30:],  # 仅最后 30 个采样
    }
    print(f"  -> ok={r.ok} {r.elapsed_s}s  "
          f"{r.throughput_mbps} MB/s  "
          f"per_seg={r.per_seg_ms}ms  "
          f"failed={r.segments_failed}  "
          f"retries={r.retry_count}  "
          f"merge({r.merge_method})={r.merge_s}s")
    return rec


def render_report(out_dir: Path, results: list[dict], meta: dict) -> Path:
    md = out_dir / "report.md"
    L: list[str] = []
    L.append("# 延河下载速度实验")
    L.append("")
    L.append(f"- 时间：`{meta['started']}`")
    L.append(f"- 课程 `{meta['course_id']}` 第 {meta['session_idx']} 节")
    L.append(f"- 课程名：{meta['course_name']}")
    L.append(f"- 节标题：{meta['session_title']}")
    L.append(f"- 截取段数：{meta.get('segments') or 'all'}")
    L.append("")

    L.append("## 候选配置")
    L.append("")
    L.append("| 变体 | AIMD | 固定K | conn/read 超时 | 重试 | 原子计数 | 二进制合并 |")
    L.append("|------|------|------|----------------|------|----------|------------|")
    for vid, cfg in VARIANTS.items():
        if not any(r["variant"] == vid for r in results):
            continue
        L.append(f"| `{vid}` | {cfg.use_aimd} | {cfg.fixed_workers} | "
                 f"{cfg.connect_timeout}/{cfg.read_timeout} | "
                 f"{cfg.max_retries} | {cfg.use_atomic_count} | {cfg.use_binary_concat} |")
    L.append("")

    L.append("## 单次结果")
    L.append("")
    L.append("| 变体 | workers | 段数 | 耗时 | 吞吐(MB/s) | 每段ms | 失败 | 重试 | 合并 | 备注 |")
    L.append("|------|---------|------|------|------------|--------|------|------|------|------|")
    for r in results:
        merge = f"{r['merge_method']}/{r['merge_s']}s" if r['merge_method'] else "—"
        L.append(
            f"| `{r['variant']}` | {r['workers']} | {r['segments_done']}/{r['segments_total']} | "
            f"{r['elapsed_s']}s | {r['throughput_mbps']} | "
            f"{r['per_seg_ms']} | {r['segments_failed']} | {r['retry_count']} | "
            f"{merge} | {(r['error'] or '')[:40]} |"
        )
    L.append("")

    # 排序排行
    ok_only = [r for r in results if r["ok"]]
    if ok_only:
        ranked = sorted(ok_only, key=lambda r: r["elapsed_s"])
        best = ranked[0]
        L.append("## 排行（按总耗时）")
        L.append("")
        L.append(f"**最快**：`{best['variant']}` (workers={best['workers']}) {best['elapsed_s']}s, "
                 f"{best['throughput_mbps']} MB/s")
        L.append("")
        L.append("| 排名 | 变体 | workers | 耗时 | 吞吐 | 相对最快 |")
        L.append("|------|------|---------|------|------|----------|")
        for i, r in enumerate(ranked, 1):
            ratio = r["elapsed_s"] / best["elapsed_s"]
            L.append(f"| {i} | `{r['variant']}` | {r['workers']} | "
                     f"{r['elapsed_s']}s | {r['throughput_mbps']} | x{ratio:.2f} |")
        L.append("")

    L.append("---")
    L.append("")
    L.append("> 生成 by `scripts/perf_yhkt.py`. 原始 JSON 见 `results.json`。")
    md.write_text("\n".join(L), encoding="utf-8")
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, default=1, choices=[1, 2, 3])
    ap.add_argument("--course", default="40524")
    ap.add_argument("--session-idx", type=int, default=0)
    ap.add_argument("--dl-type", default="screen", choices=["screen", "video"])
    ap.add_argument("--variants", default=None,
                    help="逗号分隔，默认全部")
    ap.add_argument("--workers", type=int, nargs="+", default=None)
    ap.add_argument("--segments", type=int, default=60,
                    help="只下前 N 段（实验用）；0=全下")
    ap.add_argument("--trials", type=int, default=1)
    args = ap.parse_args()

    seg_limit = args.segments if args.segments > 0 else None

    # 选 variants
    if args.variants:
        vlist = args.variants.split(",")
    elif args.stage == 1:
        vlist = list(VARIANTS.keys())
    elif args.stage == 2:
        vlist = ["v1_aimd_off", "v_combo"]
    else:  # stage 3
        vlist = ["v_combo"]

    # 选 workers
    if args.workers:
        wlist = args.workers
    elif args.stage == 1:
        wlist = [16]
    elif args.stage == 2:
        wlist = [4, 8, 16, 24, 32]
    else:
        wlist = [8]

    # 1. 拉课程获取 m3u8 URL
    print(f"[init] 拉课程 {args.course} 第 {args.session_idx} 节 ...")
    m3u8_url, course_name, session_title = fetch_session_url(
        args.course, args.session_idx, args.dl_type
    )
    print(f"[init] {course_name} / {session_title}")

    # 2. 准备输出目录
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = ROOT / "bench" / "yhkt_results" / f"{ts}_stage{args.stage}"
    out_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "started": datetime.now().isoformat(timespec="seconds"),
        "stage": args.stage,
        "course_id": args.course,
        "course_name": course_name,
        "session_idx": args.session_idx,
        "session_title": session_title,
        "segments": seg_limit,
        "trials": args.trials,
        "variants": vlist,
        "workers_list": wlist,
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                       encoding="utf-8")

    # 3. 执行
    results: list[dict] = []
    total = len(vlist) * len(wlist) * args.trials
    print(f"[init] 共 {total} 次试验")

    counter = 0
    for vid in vlist:
        cfg = VARIANTS[vid]
        for w in wlist:
            for trial in range(1, args.trials + 1):
                counter += 1
                print(f"\n=== [{counter}/{total}] {vid} w={w} trial={trial} ===")
                rec = run_one(vid, cfg, m3u8_url, w, seg_limit, out_dir)
                rec["trial"] = trial
                results.append(rec)
                # 立即落盘，防中断丢
                (out_dir / "results.json").write_text(
                    json.dumps({"meta": meta, "results": results},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    # 4. 清产物（保留 results + report）
    work = out_dir / "work"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)

    # 5. 报告
    md = render_report(out_dir, results, meta)
    print(f"\n=== 报告：{md}")
    print(f"=== 数据：{out_dir / 'results.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
