# 2026 可用性测试报告

测试日期：2026-05-22  
测试课程：`67092` 空中目标探测前沿技术（王锐）  
测试环境：Windows，Python 3.9，NVIDIA RTX 5070 Laptop GPU，`auth.txt` 已登录

## 结论

截至 2026-05-22，下载器对当前延河课堂仍可用。课程 `67092` 已完成从课程列表获取、HLS 下载、MP4 合并、PPT 提取、TXT/SRT 转写的端到端实测。

## 本次修复

- 更新 2026 年延河 HLS 路径签名：m3u8 插入目录由旧 `_100` 对应值切换为当前 `_200` 对应值，解决课程 `67092` 首次实测时出现的 HTTP 403。
- 新增 VPN/代理自动直连：延河 API、m3u8、ts 分片、音频下载均使用 `trust_env=False` 的直连会话，不读取系统代理环境变量，降低 VPN、系统代理、透明代理导致的 403、ProxyError、超时和卡死概率。
- 新增长音频分块转写：超过 30 分钟的音频自动切成约 20 分钟一段逐段转写，再把时间戳拼回原视频时间线，解决 98 分钟课程一次性送入 Whisper 时内存分配失败的问题。
- 优化重复运行体验：已有 TXT 时不再提前加载 `large-v3` 模型，直接跳过，避免无意义等待。
- 完整版 spec 收紧依赖范围，避免 PyInstaller 把 `torch`、`torchvision`、`pandas`、`bokeh`、`selenium` 等非必需大包卷入导致构建内存不足。

## 67092 实测结果

命令：

```powershell
python -u scripts\smoke_download_course.py 67092 --post-process --device auto --compute-type float16
```

结果：

- 课程列表：1 个课次，视频 ID `548960`
- HLS 分片：297/297 下载成功
- MP4：`output\空中目标探测前沿技术-screen\空中目标探测前沿技术-王锐-第12周 星期三 第4大节.mp4`
- MP4 大小：约 776.7 MB
- 下载耗时：约 6.5 分钟
- PPT：59 页，约 13.91 MB
- TXT：2448 段，约 26,277 字符
- SRT：同步生成
- 最终 smoke 校验：`ARTIFACTS mp4=1 pptx=1 txt=1`

## 双版本打包测试

已重新构建：

- `dist\延河课堂下载器-简易版.exe`，约 182.1 MB
- `dist\延河课堂下载器-完整版.exe`，约 295.5 MB

bundle 冒烟检查：

- 简易版：可启动，`ffmpeg.exe` / `ffprobe.exe` 均存在
- 完整版：可启动，`ffmpeg.exe` / `ffprobe.exe` 均存在，包含 `cv2`、`ctranslate2` 等后处理依赖

性能冒烟：

```text
延河课堂下载器-简易版.exe  182.1 MB  启动约 4.02s  存活 True
延河课堂下载器-完整版.exe  295.5 MB  启动约 4.01s  存活 True
```

## 注意事项

- 仍需要有效的延河课堂 Token，即 `auth.txt` 或 GUI 中填写的 32 位 Token。
- Whisper 模型不内置到 exe；首次转写会下载模型缓存，之后复用本机缓存。
- VPN/代理自动直连是默认行为，适合大多数同学的系统代理/VPN 场景；如果学校网络本身要求特定代理，仍可通过关闭代理或配置白名单排查。
