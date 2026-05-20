"""
下载 科学与工程计算 第4周 星期二 第3大节 (缺失的那节课)
然后用 GPU 进行转录
"""
import os
import sys
import io
import time

# Force UTF-8 stdout
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import utils
import m3u8dl

APP_PATH = utils.get_app_path()
OUTPUT = os.path.join(APP_PATH, "output")
COURSE_DIR = os.path.join(OUTPUT, "科学与工程计算-screen")

def sanitize(name):
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, '_')
    return name.strip()

# ========== Step 0: Check existing ==========
print("=" * 60)
print("Step 0: Check existing files")
print("=" * 60)
mp4s = sorted([f for f in os.listdir(COURSE_DIR) if f.endswith('.mp4')])
print(f"Found {len(mp4s)} mp4 files:")
for f in mp4s:
    sz = os.path.getsize(os.path.join(COURSE_DIR, f)) / 1024 / 1024
    print(f"  {f} ({sz:.0f}MB)")

has_week4_tue = any("第4周 星期二" in f for f in mp4s)
print(f"\n第4周 星期二 exists: {has_week4_tue}")

# Also check _broken
broken_dir = os.path.join(OUTPUT, "_broken")
if os.path.isdir(broken_dir):
    broken = [f for f in os.listdir(broken_dir) if "第4周 星期二" in f]
    if broken:
        print(f"Found in _broken: {broken}")

# ========== Step 1: Download if needed ==========
target_name = "科学与工程计算-熊春光-第4周 星期二 第3大节"
mp4_path = os.path.join(COURSE_DIR, f"{target_name}.mp4")

if not os.path.exists(mp4_path):
    print("\n" + "=" * 60)
    print("Step 1: Download VGA stream")
    print("=" * 60)

    utils.read_auth()

    # Find course ID for 科学与工程计算
    down_list_path = os.path.join(APP_PATH, "down_list.txt")
    with open(down_list_path) as f:
        course_ids = [line.strip() for line in f if line.strip()]

    target_cid = None
    videoList = None
    name = None
    prof = None
    for cid in course_ids:
        try:
            vl, n, p = utils.get_course_info(cid)
            if "工程计算" in n:
                target_cid = cid
                videoList = vl
                name = n
                prof = p
                print(f"Found course: {cid} -> {n} ({p})")
                break
        except:
            pass

    if not target_cid:
        print("ERROR: Could not find 科学与工程计算")
        sys.exit(1)

    # Find week 4 Tuesday session
    target_session = None
    for v in videoList:
        title = v.get("title", "")
        if "第4周" in title and "星期二" in title:
            target_session = v
            print(f"Found target session: {title} (id={v.get('id')})")
            break

    if not target_session:
        print("ERROR: Could not find 第4周 星期二 session")
        sys.exit(1)

    vga_url = target_session["videos"][0].get("vga", "")
    if not vga_url:
        print("ERROR: No VGA stream")
        sys.exit(1)

    print(f"Downloading: {target_name}")
    path = "output/科学与工程计算-screen"
    m3u8dl.M3u8Download(vga_url, path, target_name)
    print("[OK] Download complete")
else:
    sz = os.path.getsize(mp4_path) / 1024 / 1024
    print(f"\n[SKIP] MP4 already exists ({sz:.0f}MB)")

# ========== Step 2: Transcribe with GPU ==========
print("\n" + "=" * 60)
print("Step 2: GPU Transcription (whisper large-v3)")
print("=" * 60)

transcript_dir = os.path.join(COURSE_DIR, "transcripts", target_name)
txt_path = os.path.join(transcript_dir, f"{target_name}.txt")

if os.path.exists(txt_path):
    print(f"[SKIP] Transcript already exists: {txt_path}")
else:
    if not os.path.exists(mp4_path):
        print(f"ERROR: MP4 not found: {mp4_path}")
        sys.exit(1)

    sz = os.path.getsize(mp4_path) / 1024 / 1024
    print(f"Input: {mp4_path} ({sz:.0f}MB)")

    os.makedirs(transcript_dir, exist_ok=True)

    from audio_transcriber_gpu import AudioTranscriber
    transcriber = AudioTranscriber(
        model_size="large-v3",
        device="cuda",
        compute_type="float16",
        language="zh",
    )

    t0 = time.time()
    result = transcriber.transcribe_file(
        mp4_path,
        output_dir=transcript_dir,
        output_txt=True,
        output_srt=True,
    )
    elapsed = time.time() - t0
    print(f"\n[OK] Transcription done in {elapsed/60:.1f} minutes")
    print(f"  Segments: {len(result['segments'])}")
    print(f"  TXT: {result['txt_path']}")
    print(f"  SRT: {result['srt_path']}")

print("\n" + "=" * 60)
print("ALL DONE!")
print("=" * 60)
