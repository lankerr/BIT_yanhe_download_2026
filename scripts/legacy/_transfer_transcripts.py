import os
import shutil

src_dir = r"c:\Users\97290\Desktop\BIT_yanhe_download_2026\output"
dst_dir = r"c:\Users\97290\Desktop\研一下课件"

mapping = {
    "大模型技术原理与实践-screen": "大模型技术课程2026",
    "毫米波系统理论、技术及应用-screen": "毫米波系统理论技术及应用",
    "科学与工程计算-screen": "科学与工程计算",
    "雷达目标智能识别-screen": "雷达目标智能识别",
    "自然辩证法概论-screen": "自然辩证法",
    "扩频测量方法与应用-screen": "扩频测量方法与应用",
    "卫星通信理论与应用-screen": "卫星通信理论与应用"
}

def copy_transcriptions():
    for src_folder, dst_folder in mapping.items():
        src_path = os.path.join(src_dir, src_folder)
        if not os.path.exists(src_path):
            print(f"[Skip] Source not found: {src_path}")
            continue
            
        dst_path = os.path.join(dst_dir, dst_folder)
        os.makedirs(dst_path, exist_ok=True)
            
        for sub in ["extracted_ppt", "transcripts"]:
            sub_src = os.path.join(src_path, sub)
            sub_dst = os.path.join(dst_path, sub)
            if os.path.exists(sub_src):
                print(f"Copying {sub} for {dst_folder}...")
                shutil.copytree(sub_src, sub_dst, dirs_exist_ok=True)

if __name__ == "__main__":
    copy_transcriptions()
    print("All transcripts and PPTs transferred successfully.")
