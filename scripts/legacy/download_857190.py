import os
import sys
import io
import time
import utils
import m3u8dl

# Force UTF-8 stdout
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

COURSE_ID = '857190'
APP_PATH = utils.get_app_path()
OUTPUT = os.path.join(APP_PATH, "output")

def sanitize(name):
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, '_')
    return name.strip()

def download_and_transcribe():
    print("=" * 60)
    print(f"Downloading and Transcribing course: {COURSE_ID}")
    print("=" * 60)

    utils.read_auth()
    if not utils.test_auth(courseID=COURSE_ID):
        print("ERROR: Token expired, update auth.txt")
        sys.exit(1)
    print("[OK] Token valid\n")

    try:
        videoList, courseName, professor = utils.get_course_info(COURSE_ID)
    except Exception as e:
        print(f"Error fetching course info: {e}")
        return

    path = f"output/{courseName}-screen"
    print(f"\n--- {courseName} - {professor} ({len(videoList)} sessions) ---")

    course_dir = os.path.join(OUTPUT, f"{courseName}-screen")

    for i, c in enumerate(videoList):
        name = courseName + "-" + professor + "-" + c["title"]
        name = sanitize(name)

        print(f"\n  [{i+1}/{len(videoList)}] {c['title']}")
        
        mp4_check = os.path.join(APP_PATH, path, f"{name}.mp4")
        
        if os.path.exists(mp4_check):
            sz = os.path.getsize(mp4_check) / 1024 / 1024
            print(f"    [SKIP] Download exists ({sz:.0f}MB)")
        else:
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
                print(f"    [OK] Download done")
            except Exception as e:
                print(f"    [FAIL] Download: {e}")

        # Now do transcription for this file
        if not os.path.exists(mp4_check):
            continue
            
        print(f"    >> Transcription starting...")
        
        try:
            from audio_transcriber_gpu import AudioTranscriber
            if not hasattr(download_and_transcribe, 'transcriber'):
                download_and_transcribe.transcriber = AudioTranscriber(
                    model_size="large-v3",
                    device="cuda",
                    compute_type="float16",
                    language="zh",
                )
            transcriber = download_and_transcribe.transcriber
            
            base = name
            transcript_dir = os.path.join(course_dir, "transcripts", base)
            txt_path = os.path.join(transcript_dir, f"{base}.txt")

            if os.path.exists(txt_path):
                print(f"    [SKIP] Already transcribed")
                continue

            os.makedirs(transcript_dir, exist_ok=True)
            transcriber.transcribe(mp4_check, transcript_dir, base)
            print(f"    [OK] Transcription done")
        except Exception as e:
            print(f"    [FAIL] Transcription: {e}")

if __name__ == "__main__":
    download_and_transcribe()
