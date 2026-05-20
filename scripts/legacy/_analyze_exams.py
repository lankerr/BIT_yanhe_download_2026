"""对每门课基于转录文本 + PPT提取结果分析考试内容
- 读取 output/*-screen/transcripts/**/*.txt
- 读取 output/*-screen/extracted_ppt/*.pptx (仅统计张数与幻灯片文件名/OCR暂略)
- 调用 Gemini CLI 生成"考试重点 / 可能考题 / 复习建议"
输出: output/考试分析/<课程名>.md
"""
import os, sys, subprocess, json
from pathlib import Path

ROOT = Path(r"C:\Users\97290\Desktop\BIT_yanhe_download_2026\output")
OUT = ROOT / "考试分析"
OUT.mkdir(exist_ok=True)

GEMINI = r"C:\Users\97290\Desktop\MCP\tools\gemini_cli.py"

PROMPT_TEMPLATE = """你是一名资深高校研究生课程助教。下面是《{course}》若干次课堂的老师讲解文字转录(可能有错别字)，以及每次课的课件张数。

请基于这些内容，产出面向期末考试/大作业的复习材料，包含：
1. **课程核心主题脉络**（3-6 条）
2. **高频出现/老师强调的关键概念与术语**（按重要程度排序，每条附 1-2 句解释）
3. **可能的考试题目**（列 8-15 题，覆盖概念、计算/推导、简答、论述、案例分析多种题型）
4. **每周重点一览表**
5. **复习建议与时间分配**

要求：
- 语言精炼，用中文；
- 对术语/公式尽量还原完整表达；
- 用 Markdown 输出，带分级标题。

---
课程名: {course}
课件张数统计: {ppt_stats}
课堂转录(按时间拼接, 总字数 {total_chars})：

{transcript}
"""

def collect_course(c: Path):
    tx_dir = c / "transcripts"
    ppt_dir = c / "extracted_ppt"
    parts = []
    if tx_dir.exists():
        for txt in sorted(tx_dir.rglob("*.txt")):
            try:
                t = txt.read_text(encoding='utf-8', errors='ignore').strip()
                if t:
                    parts.append(f"\n### {txt.stem}\n{t}")
            except Exception as e:
                print(f"  读取失败 {txt}: {e}")
    ppt_stats = {}
    if ppt_dir.exists():
        for sub in sorted(ppt_dir.iterdir()):
            if sub.is_dir():
                n = len(list(sub.glob("*.jpg"))) + len(list(sub.glob("*.png")))
                if n:
                    ppt_stats[sub.name] = n
    return "\n".join(parts), ppt_stats


def analyze(course_dir: Path):
    course = course_dir.name.replace("-screen", "")
    transcript, ppt_stats = collect_course(course_dir)
    if not transcript.strip():
        print(f"  [跳过] {course}: 尚无转录文本")
        return
    # 裁剪过长文本避免超 context
    MAX = 180_000  # 字
    if len(transcript) > MAX:
        transcript = transcript[:MAX] + "\n...(已截断)"
    prompt = PROMPT_TEMPLATE.format(
        course=course,
        ppt_stats=json.dumps(ppt_stats, ensure_ascii=False),
        total_chars=len(transcript),
        transcript=transcript,
    )
    tmp = OUT / f"_{course}.prompt.txt"
    tmp.write_text(prompt, encoding='utf-8')
    print(f"  [分析] {course} (转录 {len(transcript)} 字, {len(ppt_stats)} 次课件)")
    # 调用 gemini_cli 读文件方式
    try:
        r = subprocess.run(
            ["python", GEMINI, "--file", str(tmp)],
            capture_output=True, text=True, encoding='utf-8', timeout=300,
        )
        out = (r.stdout or "") + (("\n[STDERR]\n" + r.stderr) if r.stderr else "")
    except Exception as e:
        out = f"[调用Gemini失败] {e}"
    md = OUT / f"{course}.md"
    md.write_text(out, encoding='utf-8')
    print(f"  -> {md}")


if __name__ == "__main__":
    courses = sorted([d for d in ROOT.iterdir()
                      if d.is_dir() and d.name.endswith('-screen')])
    target = sys.argv[1] if len(sys.argv) > 1 else None
    for c in courses:
        if target and target not in c.name:
            continue
        print(f"\n=== {c.name} ===")
        analyze(c)
