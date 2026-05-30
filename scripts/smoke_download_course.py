"""Smoke-test one Yanhe course by downloading and optionally post-processing it."""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch_process
import m3u8dl
import utils


def sanitize(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


def healthy_file(path: Path, min_bytes: int = 1024 * 1024) -> bool:
    return path.exists() and path.stat().st_size >= min_bytes


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser()
    parser.add_argument("course_id", nargs="?", default="67092")
    parser.add_argument("--max-workers", type=int, default=24)
    parser.add_argument("--post-process", action="store_true")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="float16")
    args = parser.parse_args()

    utils.read_auth()
    if not utils.test_auth(args.course_id):
        raise RuntimeError("auth.txt is invalid or this course has no visible sessions")

    sessions, course_name, professor = utils.get_course_info(args.course_id)
    work_dir = f"output/{course_name}-screen"
    course_dir = Path(utils.get_app_path()) / work_dir
    course_dir.mkdir(parents=True, exist_ok=True)

    print(f"COURSE {args.course_id}: {course_name} - {professor}")
    print(f"SESSIONS {len(sessions)}")

    downloaded = skipped = failed = 0
    for idx, session in enumerate(sessions, 1):
        title = session.get("title") or f"第{idx}节"
        name = sanitize(f"{course_name}-{professor}-{title}")
        mp4 = course_dir / f"{name}.mp4"
        if healthy_file(mp4):
            print(f"[{idx}/{len(sessions)}] SKIP existing mp4: {mp4.name} ({mp4.stat().st_size / 1024 / 1024:.1f} MB)")
            skipped += 1
            continue

        videos = session.get("videos") or []
        vga = videos[0].get("vga") if videos else ""
        if not vga:
            print(f"[{idx}/{len(sessions)}] FAIL no VGA: {title}")
            failed += 1
            continue

        print(f"[{idx}/{len(sessions)}] DOWNLOAD {title}")
        started = time.time()
        try:
            m3u8dl.M3u8Download(vga, work_dir, name, max_workers=args.max_workers, gui_mode=True)
            if not healthy_file(mp4):
                raise RuntimeError(f"mp4 missing or too small: {mp4}")
            elapsed = time.time() - started
            print(f"[{idx}/{len(sessions)}] OK {mp4.name} ({mp4.stat().st_size / 1024 / 1024:.1f} MB, {elapsed / 60:.1f} min)")
            downloaded += 1
        except Exception as exc:
            print(f"[{idx}/{len(sessions)}] FAIL {title}: {type(exc).__name__}: {exc}")
            failed += 1

    print(f"DOWNLOAD SUMMARY downloaded={downloaded} skipped={skipped} failed={failed}")

    if args.post_process:
        print("POSTPROCESS START")
        batch_process.batch_process_all(
            str(course_dir),
            do_slides=True,
            do_transcribe=True,
            model_size=args.model,
            device=args.device,
            compute_type=args.compute_type,
            language="zh",
        )
        print("POSTPROCESS DONE")

    mp4s = list(course_dir.glob("*.mp4"))
    pptxs = list((course_dir / "extracted_ppt").glob("*.pptx")) if (course_dir / "extracted_ppt").exists() else []
    txts = list((course_dir / "transcripts").rglob("*.txt")) if (course_dir / "transcripts").exists() else []
    print(f"ARTIFACTS mp4={len(mp4s)} pptx={len(pptxs)} txt={len(txts)} dir={course_dir}")

    if not mp4s:
        print("SMOKE FAIL: no MP4 artifact")
        return 3
    if args.post_process and (not pptxs or not txts):
        print("SMOKE FAIL: post-process artifacts missing")
        return 4

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
