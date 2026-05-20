from pathlib import Path
root = Path(r"C:\Users\97290\Desktop\BIT_yanhe_download_2026\output")
courses = [d for d in root.iterdir() if d.is_dir() and d.name != 'extracted_ppt']
total_mp4 = total_pptx = total_txt = 0
for c in sorted(courses):
    mp4s = [m for m in c.rglob("*.mp4") if 'extracted_ppt' not in m.parts and 'transcripts' not in m.parts]
    ppt_dir = c / "extracted_ppt"
    tx_dir = c / "transcripts"
    pptxs = list(ppt_dir.glob("*.pptx")) if ppt_dir.exists() else []
    txts = list(tx_dir.rglob("*.txt")) if tx_dir.exists() else []
    total_mp4 += len(mp4s); total_pptx += len(pptxs); total_txt += len(txts)
    print(f"\n[{c.name}]  mp4={len(mp4s)}  pptx={len(pptxs)}  txt={len(txts)}")
    done_ppt = {p.stem for p in pptxs}
    done_tx = {t.stem for t in txts}
    missing_ppt = [m.stem for m in mp4s if m.stem not in done_ppt]
    missing_tx = [m.stem for m in mp4s if m.stem not in done_tx]
    if missing_ppt:
        print(f"  缺PPT({len(missing_ppt)}):")
        for n in missing_ppt: print(f"    - {n}")
    if missing_tx:
        print(f"  缺转录({len(missing_tx)}):")
        for n in missing_tx: print(f"    - {n}")
print(f"\n合计: mp4={total_mp4}  pptx={total_pptx}  txt={total_txt}")
