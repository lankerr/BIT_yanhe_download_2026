import os
import re

OUTPUT_DIR = "c:/Users/97290/Desktop/BIT_yanhe_download_2026/output"

def parse_time(time_str):
    parts = time_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return 0

def analyze_transcripts():
    results = []
    
    for course in os.listdir(OUTPUT_DIR):
        course_dir = os.path.join(OUTPUT_DIR, course)
        if not os.path.isdir(course_dir):
            continue
        transcript_dir = os.path.join(course_dir, "transcripts")
        if not os.path.isdir(transcript_dir):
            continue
        
        for session in os.listdir(transcript_dir):
            session_dir = os.path.join(transcript_dir, session)
            txt_path = os.path.join(session_dir, f"{session}.txt")
            if not os.path.exists(txt_path):
                continue
            
            with open(txt_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            segments = []
            for line in lines:
                if line.startswith("="):
                    break
                m = re.match(r'^\[(.*?)-(.*?)\]\s*(.*)$', line)
                if m:
                    start_str, end_str, text = m.groups()
                    start_sec = parse_time(start_str)
                    end_sec = parse_time(end_str)
                    segments.append((start_sec, end_sec, text.strip()))
            
            if not segments:
                continue

            max_gap = 0
            gap_pos = None
            
            for i in range(1, len(segments)):
                gap = segments[i][0] - segments[i-1][1]
                if gap > max_gap:
                    max_gap = gap
                    gap_pos = (segments[i-1][1], segments[i][0])
            
            num_pattern_count = 0
            for sec in segments:
                text = sec[2]
                if re.match(r'^[0-9\.]+$', text) or len(text) <= 3:
                    num_pattern_count += 1
            
            # Very loose definition to find the bad ones:
            # Over 100 short meaningless segments
            suspect = num_pattern_count > 100

            results.append({
                "course": course,
                "session": session,
                "max_gap": max_gap,
                "gap_pos": gap_pos,
                "suspect": suspect,
                "num_count": num_pattern_count
            })

    with open("c:/Users/97290/Desktop/BIT_yanhe_download_2026/analysis_report.txt", "w", encoding='utf-8') as outf:
        outf.write("===== Analysis of Silences and Hallucinations =====\n")
        outf.write("[SUSPECT HALLUCINATION]\n")
        for r in results:
            if r['suspect']:
                outf.write(f"- {r['course']} / {r['session']} (Short/Num lines: {r['num_count']})\n")
        
        outf.write("\n[LONG BREAKS (> 5 mins)]\n")
        for r in results:
            if not r['suspect'] and r['max_gap'] > 300:
                start_gap = r['gap_pos'][0] // 60
                end_gap = r['gap_pos'][1] // 60
                outf.write(f"- {r['course']} / {r['session']}: {r['max_gap']} secs break between {start_gap:02d}m - {end_gap:02d}m\n")

if __name__ == "__main__":
    analyze_transcripts()
