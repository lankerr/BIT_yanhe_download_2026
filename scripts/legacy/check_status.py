"""Check download completion status for all courses."""
import os
import utils

utils.read_auth()
root = r"H:\我的云端硬盘\YanheRecordings_2026Spring"
course_ids = ["66046", "66554", "66891", "67005", "67225"]

def sanitize(name):
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip()

total_expected = 0
total_got = 0

for cid in course_ids:
    vl, cn, pr = utils.get_course_info(cid)
    folder = sanitize(f"{cn}-{pr}")
    cdir = os.path.join(root, folder)
    mp4s = []
    if os.path.isdir(cdir):
        mp4s = [f for f in os.listdir(cdir) if f.endswith(".mp4")]
    sessions_with_video = sum(
        1 for s in vl if s.get("videos") and s["videos"][0].get("vga")
    )
    # deduplicate sessions by title
    seen = set()
    unique_sessions = 0
    missing_titles = []
    for s in vl:
        t = s.get("title", "?")
        fname = sanitize(f"{cn}-{pr}-{t}")
        if fname in seen:
            continue
        seen.add(fname)
        if not (s.get("videos") and s["videos"][0].get("vga")):
            continue
        unique_sessions += 1
        if fname + ".mp4" not in mp4s:
            missing_titles.append(t)

    total_expected += unique_sessions
    total_got += len(mp4s)
    status = "DONE" if not missing_titles else f"MISSING {len(missing_titles)}"
    print(f"{cn}-{pr}: {len(mp4s)}/{unique_sessions} [{status}]")
    for t in missing_titles:
        print(f"  - {t}")

print(f"\nTotal: {total_got}/{total_expected}")

# Check temp dir
temp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_temp_download")
if os.path.exists(temp):
    print(f"\nTemp dir exists: {temp}")
    for d in sorted(os.listdir(temp)):
        dp = os.path.join(temp, d)
        if os.path.isdir(dp):
            items = os.listdir(dp)
            dirs = [i for i in items if os.path.isdir(os.path.join(dp, i))]
            if dirs:
                print(f"  {d}: {len(dirs)} incomplete downloads")
                for sub in dirs:
                    subp = os.path.join(dp, sub)
                    ts = len([f for f in os.listdir(subp) if f.endswith(".ts")])
                    print(f"    {sub}: {ts} ts fragments")
