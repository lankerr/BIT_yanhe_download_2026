"""
Download and post-process only selected Yanhe courses.

Rules:
- Only target course IDs are considered.
- If a matching txt transcript already exists under Desktop/研一下课件 or output/,
  the session is skipped.
- Newly downloaded mp4 files are processed into extracted_ppt/ and transcripts/.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

import batch_process
import m3u8dl
import utils


APP_ROOT = Path(utils.get_app_path())
OUTPUT_ROOT = APP_ROOT / "output"
DESKTOP_COURSE_ROOT = Path.home() / "Desktop" / "研一下课件"

SESSION_TITLE_RE = re.compile(r"第\d+周\s+星期[一二三四五六日天]\s+第\d+大节")


@dataclass(frozen=True)
class TargetCourse:
    course_id: str
    desktop_folder: str


DEFAULT_TARGETS = [
    TargetCourse("66554", "毫米波系统理论技术及应用"),
    TargetCourse("67096", "卫星通信理论与应用"),
]


def sanitize(name: str) -> str:
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()


def safe_print(*parts: object) -> None:
    print(*parts, flush=True)


def transcript_index(paths: Iterable[Path]) -> tuple[set[str], set[str]]:
    stems: set[str] = set()
    titles: set[str] = set()
    for root in paths:
        if not root.exists():
            continue
        for txt in root.rglob("*.txt"):
            try:
                if txt.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            stems.add(txt.stem)
            text_for_title = f"{txt.parent.name} {txt.stem}"
            titles.update(SESSION_TITLE_RE.findall(text_for_title))
    return stems, titles


def matching_transcript_exists(
    file_stem: str,
    session_title: str,
    desktop_folder: str,
    output_course_dir: Path,
) -> bool:
    desktop_dir = DESKTOP_COURSE_ROOT / desktop_folder
    stems, titles = transcript_index([desktop_dir, output_course_dir])
    return file_stem in stems or session_title in titles


def copy_tree(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    copied = 0
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or item.stat().st_size != target.stat().st_size:
            shutil.copy2(item, target)
            copied += 1
    return copied


def transfer_outputs(course_dir: Path, desktop_folder: str) -> None:
    desktop_dir = DESKTOP_COURSE_ROOT / desktop_folder
    desktop_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["extracted_ppt", "transcripts"]:
        copied = copy_tree(course_dir / sub, desktop_dir / sub)
        safe_print(f"  transfer {sub}: {copied} files")


def check_auth() -> None:
    if not utils.read_auth():
        raise RuntimeError("auth.txt is empty. Please update it with a fresh Yanhe token.")
    res = requests.get("https://cbiz.yanhekt.cn/v1/user", headers=utils.headers, timeout=(10, 30))
    try:
        data = res.json()
    except Exception as exc:
        raise RuntimeError(f"Could not verify auth: {res.text[:200]}") from exc
    if data.get("code") != 0:
        raise RuntimeError(f"Yanhe auth is not logged in: {data.get('message')}")


def fetch_course(course_id: str) -> tuple[str, str]:
    res = requests.get(
        f"https://cbiz.yanhekt.cn/v1/course?id={course_id}&with_professor_badges=true",
        headers=utils.headers,
        timeout=(10, 30),
    )
    data = res.json()
    if data.get("code") not in (0, "0"):
        raise RuntimeError(f"course {course_id}: {data.get('message')}")
    course = data["data"]
    course_name = course["name_zh"].strip()
    professors = course.get("professors") or []
    professor = professors[0]["name"].strip() if professors else "未知教师"
    return course_name, professor


def fetch_sessions(course_id: str) -> list[dict]:
    attempts = [
        {"course_id": course_id, "with_page": False},
        {"course_id": course_id, "with_page": True, "page": 1, "page_size": 200},
        {"course_id": course_id},
    ]
    for params in attempts:
        res = requests.get(
            "https://cbiz.yanhekt.cn/v2/course/session/list",
            params=params,
            headers=utils.headers,
            timeout=(10, 30),
        )
        payload = res.json()
        if payload.get("code") not in (0, "0"):
            continue
        data = payload.get("data")
        if isinstance(data, list) and data:
            return data
        if isinstance(data, dict):
            rows = data.get("data") or data.get("list") or []
            if rows:
                return rows
    return []


def session_vga_url(session: dict) -> str:
    videos = session.get("videos") or []
    if not videos:
        return ""
    return videos[0].get("vga") or ""


def download_missing(target: TargetCourse, max_workers: int) -> Path:
    course_name, professor = fetch_course(target.course_id)
    sessions = fetch_sessions(target.course_id)
    if not sessions:
        raise RuntimeError(
            f"{course_name} ({target.course_id}) has no visible sessions. "
            "Refresh auth.txt, then rerun."
        )

    course_dir = OUTPUT_ROOT / f"{course_name}-screen"
    course_dir.mkdir(parents=True, exist_ok=True)
    work_dir = f"output/{course_name}-screen"

    safe_print(f"\n=== {course_name} - {professor} ({len(sessions)} sessions) ===")
    downloaded = skipped_txt = skipped_mp4 = failed = 0

    seen: set[str] = set()
    for idx, session in enumerate(sessions, 1):
        title = session.get("title") or f"第{idx}节"
        file_stem = sanitize(f"{course_name}-{professor}-{title}")
        if file_stem in seen:
            continue
        seen.add(file_stem)

        if matching_transcript_exists(file_stem, title, target.desktop_folder, course_dir):
            safe_print(f"  [{idx}/{len(sessions)}] SKIP txt: {title}")
            skipped_txt += 1
            continue

        mp4_path = course_dir / f"{file_stem}.mp4"
        if mp4_path.exists() and mp4_path.stat().st_size > 1024 * 1024:
            safe_print(f"  [{idx}/{len(sessions)}] SKIP mp4 exists: {title}")
            skipped_mp4 += 1
            continue

        vga_url = session_vga_url(session)
        if not vga_url:
            safe_print(f"  [{idx}/{len(sessions)}] WARN no VGA: {title}")
            failed += 1
            continue

        safe_print(f"  [{idx}/{len(sessions)}] download: {title}")
        try:
            m3u8dl.M3u8Download(vga_url, work_dir, file_stem, max_workers=max_workers)
            downloaded += 1
        except Exception as exc:
            failed += 1
            safe_print(f"    FAIL: {exc}")
        time.sleep(1)

    safe_print(
        f"  summary: downloaded={downloaded}, skipped_txt={skipped_txt}, "
        f"skipped_mp4={skipped_mp4}, failed={failed}"
    )
    return course_dir


def postprocess_course(course_dir: Path, model: str, device: str, compute_type: str) -> None:
    mp4s = list(course_dir.glob("*.mp4"))
    if not mp4s:
        safe_print(f"  no mp4 to process: {course_dir.name}")
        return
    batch_process.batch_process_all(
        str(course_dir),
        do_slides=True,
        do_transcribe=True,
        model_size=model,
        device=device,
        compute_type=compute_type,
        language="zh",
    )


def parse_extra_course(value: str) -> TargetCourse:
    if ":" not in value:
        raise argparse.ArgumentTypeError("Use COURSE_ID:DesktopFolder")
    course_id, desktop_folder = value.split(":", 1)
    course_id = course_id.strip()
    desktop_folder = desktop_folder.strip()
    if not course_id.isdigit() or not desktop_folder:
        raise argparse.ArgumentTypeError("Use COURSE_ID:DesktopFolder")
    return TargetCourse(course_id, desktop_folder)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Download selected Yanhe courses only.")
    parser.add_argument(
        "--extra-course",
        action="append",
        type=parse_extra_course,
        default=[],
        help="Add another target as COURSE_ID:DesktopFolder, e.g. 12345:昆虫",
    )
    parser.add_argument("--max-workers", type=int, default=24)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--no-transfer", action="store_true")
    args = parser.parse_args()

    check_auth()

    targets = DEFAULT_TARGETS + list(args.extra_course)
    safe_print("Targets:")
    for target in targets:
        safe_print(f"  - {target.course_id} -> {target.desktop_folder}")

    processed: list[tuple[Path, str]] = []
    for target in targets:
        course_dir = download_missing(target, args.max_workers)
        processed.append((course_dir, target.desktop_folder))

    if not args.download_only:
        for course_dir, desktop_folder in processed:
            safe_print(f"\n=== postprocess {course_dir.name} ===")
            postprocess_course(course_dir, args.model, args.device, args.compute_type)
            if not args.no_transfer:
                transfer_outputs(course_dir, desktop_folder)

    safe_print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
