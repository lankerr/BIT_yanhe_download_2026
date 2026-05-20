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

CID = "66745"
APP_PATH = utils.get_app_path()
OUTPUT = os.path.join(APP_PATH, "output")
DIR_NAME = "科学(2)-screen"
COURSE_DIR = os.path.join(OUTPUT, DIR_NAME)

def sanitize(name):
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, '_')
    return name.strip()

print("=" * 60)
print(f"Downloading Course: {CID} -> {DIR_NAME}")
print("=" * 60)

utils.read_auth()
if not utils.test_auth(courseID=CID):
    print("ERROR: Token expired. Please update auth.txt")
    sys.exit(1)

videoList, name, prof = utils.get_course_info(CID)
print(f"Course Info: Name={name}, Professor={prof}, Total Sessions: {len(videoList)}")

os.makedirs(COURSE_DIR, exist_ok=True)

# 1. Download
print("\n--- Phase 1: Download ---")
for i, c in enumerate(videoList):
    title = c.get('title', f"session_{i}")
    file_name = sanitize(f"科学(2)-{prof}-{title}")
    mp4_path = os.path.join(COURSE_DIR, f"{file_name}.mp4")

    print(f"\n[{i+1}/{len(videoList)}] {title}")

    if os.path.exists(mp4_path):
        sz = os.path.getsize(mp4_path) / 1024 / 1024
        print(f"  [SKIP] Already exists ({sz:.0f}MB)")
        continue

    vga_url = None
    if c.get("videos") and len(c["videos"]) > 0:
        vga_url = c["videos"][0].get("vga", "")
    
    if not vga_url:
        print("  [WARN] No VGA stream found!")
        continue

    try:
        print("  >> Downloading...")
        m3u8dl.M3u8Download(vga_url, f"output/{DIR_NAME}", file_name)
    except Exception as e:
        print(f"  [FAIL] {e}")

# 2. Transcribe
print("\n--- Phase 2: Transcribe ---")
from audio_transcriber_gpu import batch_transcribe

batch_transcribe(
    input_dir=COURSE_DIR,
    model_size="large-v3",
    device="cuda",
    compute_type="float16",
    language="zh"
)

print("\nALL DONE!")
