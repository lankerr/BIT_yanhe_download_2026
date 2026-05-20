"""
下载两门新课(67170 扩频测量方法与应用, 67096 卫星通信理论与应用)
然后只做转录 (不做PPT提取)

使用与 batch_download_screen.py 相同的下载方式
"""
import os
import sys
import io
import time

# Force UTF-8 stdout
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import m3u8dl
import utils


NEW_COURSES = ['67170', '67096']
APP_PATH = utils.get_app_path()
OUTPUT = os.path.join(APP_PATH, "output")


def sanitize(name):
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, '_')
    return name.strip()


def phase1_download():
    """下载两门新课的 VGA 视频 (完全模仿 batch_download_screen.py)"""
    print("=" * 60)
    print("Phase 1: Download new courses")
    print("=" * 60)

    utils.read_auth()
    if not utils.test_auth(courseID=NEW_COURSES[0]):
        print("ERROR: Token expired, update auth.txt")
        sys.exit(1)
    print("[OK] Token valid\n")

    total_dl = 0
    total_skip = 0

    for cid in NEW_COURSES:
        videoList, courseName, professor = utils.get_course_info(cid)
        # batch_download_screen.py 的路径格式
        path = f"output/{courseName}-screen"

        print(f"\n--- {courseName} - {professor} ({len(videoList)} sessions) ---")

        for i, c in enumerate(videoList):
            name = courseName + "-" + professor + "-" + c["title"]
            name = sanitize(name)

            print(f"\n  [{i+1}/{len(videoList)}] {c['title']}")

            # check existing
            mp4_check = os.path.join(APP_PATH, path, f"{name}.mp4")
            if os.path.exists(mp4_check):
                sz = os.path.getsize(mp4_check) / 1024 / 1024
                print(f"    [SKIP] already exists ({sz:.0f}MB)")
                total_skip += 1
                continue

            if not c.get("videos") or not c["videos"]:
                print(f"    [WARN] no video info")
                continue
            vga_url = c["videos"][0].get("vga", "")
            if not vga_url:
                print(f"    [WARN] no VGA stream")
                continue

            try:
                print(f"    >> Downloading VGA...")
                m3u8dl.M3u8Download(vga_url, path, name)
                total_dl += 1
                print(f"    [OK] done")
            except Exception as e:
                print(f"    [FAIL] {e}")
            time.sleep(1)

    print(f"\nDownload finished: new={total_dl}, skipped={total_skip}")
    return total_dl


def phase2_transcribe():
    """只对新课程做转录(跳过PPT提取)"""
    print("\n" + "=" * 60)
    print("Phase 2: Transcribe new courses")
    print("=" * 60)

    # 找到新课程目录
    new_dirs = []
    for cid in NEW_COURSES:
        videoList, courseName, professor = utils.get_course_info(cid)
        folder = f"{courseName}-screen"
        course_dir = os.path.join(OUTPUT, folder)
        if os.path.isdir(course_dir):
            new_dirs.append((course_dir, courseName))
        else:
            print(f"[WARN] Directory not found: {course_dir}")

    if not new_dirs:
        print("No course directories found, skipping transcription")
        return

    # Import transcriber
    from audio_transcriber_gpu import AudioTranscriber

    transcriber = AudioTranscriber(
        model_size="large-v3",
        device="cuda",
        compute_type="float16",
        language="zh",
    )

    total_done = 0
    total_skip = 0

    for course_dir, courseName in new_dirs:
        print(f"\n--- Transcribing: {courseName} ---")
        mp4s = sorted([f for f in os.listdir(course_dir) if f.endswith('.mp4')])
        print(f"  Found {len(mp4s)} mp4 files")

        for mp4_name in mp4s:
            base = mp4_name[:-4]
            mp4_path = os.path.join(course_dir, mp4_name)
            transcript_dir = os.path.join(course_dir, "transcripts", base)
            txt_path = os.path.join(transcript_dir, f"{base}.txt")

            if os.path.exists(txt_path):
                print(f"  [SKIP] {base} (already transcribed)")
                total_skip += 1
                continue

            os.makedirs(transcript_dir, exist_ok=True)
            print(f"  [{total_done+1}] Transcribing {base}...")
            try:
                transcriber.transcribe(mp4_path, transcript_dir, base)
                total_done += 1
                print(f"  [OK] {base}")
            except Exception as e:
                print(f"  [FAIL] {base}: {e}")

    print(f"\nTranscription finished: new={total_done}, skipped={total_skip}")


if __name__ == "__main__":
    t0 = time.time()
    phase1_download()
    phase2_transcribe()
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"All done! Total time: {elapsed/60:.0f} minutes")
    print(f"{'='*60}")
