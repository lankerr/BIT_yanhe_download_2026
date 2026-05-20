"""
批量下载 down_list.txt 中所有课程的电脑屏幕(VGA) MP4
不下载课堂录像，不下载音频
"""
import os
import sys
import time

import m3u8dl
import utils


def batch_download():
    # 读取课程ID列表
    app_path = utils.get_app_path()
    list_file = os.path.join(app_path, "down_list.txt")
    with open(list_file, "r", encoding="utf-8") as f:
        course_ids = [line.strip() for line in f if line.strip()]

    print(f"共找到 {len(course_ids)} 门课程待下载: {course_ids}")

    # 验证身份
    first_id = course_ids[0]
    if not utils.read_auth() or not utils.test_auth(courseID=first_id):
        auth = input("。".join(utils.auth_prompt()))
        utils.write_auth(auth)
        if not utils.test_auth(courseID=first_id):
            print("身份验证失败")
            sys.exit(1)

    # 确保输出目录存在
    output_dir = os.path.join(app_path, "output")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    total_downloaded = 0
    total_skipped = 0
    total_failed = 0

    for idx, courseID in enumerate(course_ids):
        print(f"\n{'='*60}")
        print(f"[{idx+1}/{len(course_ids)}] 正在处理课程 ID: {courseID}")
        print(f"{'='*60}")

        try:
            videoList, courseName, professor = utils.get_course_info(courseID=courseID)
            print(f"课程名: {courseName} | 教师: {professor}")
            print(f"共 {len(videoList)} 节课")

            path = f"output/{courseName}-screen"

            for i, c in enumerate(videoList):
                name = courseName + "-" + professor + "-" + c["title"]
                # 清理文件名中的非法字符
                name = name.replace("/", "_").replace("\\", "_").replace(":", "_")
                name = name.replace("*", "_").replace("?", "_").replace('"', "_")
                name = name.replace("<", "_").replace(">", "_").replace("|", "_")

                print(f"\n  [{i+1}/{len(videoList)}] {c['title']}")

                # 检查是否有 VGA(电脑屏幕) 视频
                if not c.get("videos") or not c["videos"]:
                    print(f"    ⚠ 没有视频信息，跳过")
                    total_skipped += 1
                    continue

                vga_url = c["videos"][0].get("vga", "")
                if not vga_url:
                    print(f"    ⚠ 没有电脑屏幕(VGA)视频，跳过")
                    total_skipped += 1
                    continue

                try:
                    print(f"    ▶ 下载电脑屏幕...")
                    m3u8dl.M3u8Download(vga_url, path, name)
                    total_downloaded += 1
                    print(f"    ✓ 完成")
                except Exception as e:
                    print(f"    ✗ 下载失败: {e}")
                    total_failed += 1

                # 每节课之间稍等一下，避免请求过于频繁
                time.sleep(1)

        except Exception as e:
            print(f"  ✗ 课程 {courseID} 处理失败: {e}")
            total_failed += 1

        # 课程之间等待
        time.sleep(2)

    print(f"\n{'='*60}")
    print(f"批量下载完成!")
    print(f"  成功: {total_downloaded}")
    print(f"  跳过: {total_skipped}")
    print(f"  失败: {total_failed}")
    print(f"{'='*60}")


if __name__ == "__main__":
    batch_download()
