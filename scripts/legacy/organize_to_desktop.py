"""
把延河课堂下载的录播资料（PPT截图+转录文本）整理到桌面"研一下课件"文件夹
- 转录文本(.txt/.srt): 直接复制到本地（文件小）
- 课件截图: 创建快捷方式指向 H 盘（图片太多，避免从云盘复制）
"""
import os
import shutil
import subprocess

SRC_BASE = r"H:\我的云端硬盘\YanheRecordings_2026Spring"
DST_BASE = r"C:\Users\97290\Desktop\研一下课件"

# 课程映射: 延河名称前缀 -> 桌面文件夹名
COURSE_MAP = {
    "大模型技术原理与实践": "大模型技术课程2026",
    "毫米波系统理论、技术及应用": "毫米波系统理论技术及应用",
    "科学与工程计算": "科学与工程计算",
    "自然辩证法概论": "自然辩证法",
    "雷达目标智能识别": "雷达目标智能识别",
}


def copy_tree_if_new(src, dst):
    """复制文件夹，跳过已存在的文件"""
    os.makedirs(dst, exist_ok=True)
    copied = 0
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            c = copy_tree_if_new(s, d)
            copied += c
        else:
            if not os.path.exists(d):
                shutil.copy2(s, d)
                copied += 1
    return copied


def create_shortcut(target_path, shortcut_path, description=""):
    """用 PowerShell 创建 .lnk 快捷方式"""
    ps_cmd = f'''
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut("{shortcut_path}")
$sc.TargetPath = "{target_path}"
$sc.Description = "{description}"
$sc.Save()
'''
    subprocess.run(["powershell", "-Command", ps_cmd],
                   capture_output=True, timeout=10)


def organize():
    ppt_dir = os.path.join(SRC_BASE, "extracted_ppt")
    tr_dir = os.path.join(SRC_BASE, "transcripts")

    for yanhe_prefix, desktop_folder in COURSE_MAP.items():
        dst_course = os.path.join(DST_BASE, desktop_folder)
        os.makedirs(dst_course, exist_ok=True)

        # 录播资料子文件夹
        dst_replay = os.path.join(dst_course, "延河录播资料")
        os.makedirs(dst_replay, exist_ok=True)

        print(f"\n{'='*60}")
        print(f"课程: {yanhe_prefix} -> {desktop_folder}")
        print(f"{'='*60}")

        # 课件截图: 创建快捷方式指向 H 盘中该课程的截图文件夹
        shortcut_path = os.path.join(dst_replay, "课件截图(H盘).lnk")
        if not os.path.exists(shortcut_path) and os.path.exists(ppt_dir):
            # 找到该课程所有PPT截图文件夹的父目录
            course_ppt_folders = [f for f in os.listdir(ppt_dir)
                                  if f.startswith(yanhe_prefix) and os.path.isdir(os.path.join(ppt_dir, f))]
            if course_ppt_folders:
                create_shortcut(ppt_dir, shortcut_path, f"{yanhe_prefix} 课件截图")
                print(f"  课件截图: 创建快捷方式 -> {ppt_dir}")
                # 同时写一个索引文件列出有哪些截图文件夹
                index_path = os.path.join(dst_replay, "课件截图索引.txt")
                with open(index_path, "w", encoding="utf-8") as f:
                    f.write(f"课件截图位于: {ppt_dir}\n\n")
                    f.write(f"本课程截图文件夹:\n")
                    for folder in sorted(course_ppt_folders):
                        full = os.path.join(ppt_dir, folder)
                        n_imgs = len([x for x in os.listdir(full) if x.endswith(('.jpg', '.png'))])
                        f.write(f"  {folder} ({n_imgs} 张)\n")
        else:
            print(f"  课件截图: 快捷方式已存在或无源")

        # 复制转录文本（文件小，直接复制）
        tr_copied = 0
        if os.path.exists(tr_dir):
            for folder in sorted(os.listdir(tr_dir)):
                if folder.startswith(yanhe_prefix):
                    src = os.path.join(tr_dir, folder)
                    if os.path.isdir(src):
                        dst = os.path.join(dst_replay, "转录文本", folder)
                        c = copy_tree_if_new(src, dst)
                        tr_copied += c
        print(f"  转录文本: 复制了 {tr_copied} 个新文件")


def write_grading_info():
    """写入各课程考核方式"""
    info = """# 2026春 研一下课程考核方式汇总

## 1. 雷达目标智能识别 (李枫)
- **出勤**: 10%
- **课程设计**: 90%
- 提交截止: 2025年12月31日前
- 提交邮箱: karl1820@sina.com
- 资料下载: bit_atr@163.com (密码: bit_atr12345678)

## 2. 毫米波系统理论、技术及应用 (朱凯强)
- **不考试**
- **作业** + **课程结课报告**
- 群签到考勤（出勤率、抬头率会被AI监测）
- 32学时，课程相对轻松

## 3. 科学与工程计算 (熊春光)
- **平时分(作业)** + **期末卷面考试** = 总评
- 平时作业: 3-4次编程作业（写代码），只要自己做的都给满分
- ⚠️ 抄袭查重：一旦发现程序雷同，平时分归零
- ⚠️ 缺勤处罚：点名未到，每次扣一半平时分（100→50→25→...）
- 不及格则不给平时分，只显示卷面分数
- 老师会在课上提示考试重点内容

## 4. 大模型技术原理与实践 (林知微)
- **作业**: 根据情况决定作业量及课堂研讨
- **考察方式**: 大作业、论文初稿或综述报告
- 每周三个模块: 科研实战方法论(红色课件) + 基础知识专题(绿色课件) + 作业
- 会布置经典论文阅读（12-14篇）和代码复现

## 5. 自然辩证法概论 (朱冬香)
- **写一篇论文**
- 共5次课，第5次课前提交论文到邮箱
- 从第2周开始可以陆续做作业

---
*信息来源: 各课程第一节课PPT截图及转录文本*
*提取日期: 2026年4月9日*
"""
    path = os.path.join(DST_BASE, "课程考核方式汇总.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(info)
    print(f"\n已写入: {path}")


if __name__ == "__main__":
    organize()
    write_grading_info()
    print("\n✅ 整理完成！")
