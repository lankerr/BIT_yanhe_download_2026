# BIT_yanhe_download 2026

> 延河课堂下载器 - 2026增强版

## 简介

本项目可下载[延河课堂 (yanhekt.cn)](https://www.yanhekt.cn/)中的课程视频。延河课堂是北京理工大学的在线课堂，提供了大量的课程视频，但是没有提供下载功能。本项目可以下载指定课程的摄像头和屏幕信号。

- **原项目**: [AuYang261/BIT_yanhe_download](https://github.com/AuYang261/BIT_yanhe_download)
- **2026版本仓库**: [lankerr/BIT_yanhe_download_2026](https://github.com/lankerr/BIT_yanhe_download_2026)

---

## 2026版本核心改进

基于原版进行了深度优化，主要改进如下：

### 1. 多线程自适应下载引擎

| 特性 | 原版 | 2026版 |
|------|------|--------|
| 线程策略 | 固定线程数 | 慢启动(16) + 动态调整(1-64) |
| 失败重试 | 立即重试 | 指数退避 + 线程减半 |
| 内存管理 | 无界队列 | 有界队列(2x线程数) |
| 超时处理 | 固定超时 | 自适应超时(40-120s) |

**实现位置**: [m3u8dl.py](m3u8dl.py)

```python
# 慢启动 + 动态调整策略
self._current_max_workers = 16  # 初始16线程
# 失败时: 线程减半 (乘法减少)
# 成功时: 逐步增加 (增量增长)
```

### 2. Watchdog 死锁监控

- 自动检测卡死的下载任务（120秒无活动）
- 通过 Exit 100 触发进程重启
- 避免单个任务阻塞整体进度

**实现位置**: [m3u8dl.py](m3u8dl.py) `watchdog()` 方法

### 3. 现代化 GUI（极客黑风格）

基于 CustomTkinter 的全新界面设计：

- **GitHub Dark 配色**: #0d1117 背景 + #58a6ff 强调色
- **并发控制**: 滑块调整 4-64 线程
- **实时进度**: 每任务进度条 + active/max 线程显示
- **便捷操作**: 打开输出目录、重试失败任务

**实现位置**: [gui_app.py](gui_app.py)

### 4. 获取Token说明（新增）

1. 在浏览器打开 https://www.yanhekt.cn 并登录
2. 按 F12 打开开发者工具，切换到「控制台(Console)」标签
3. 在控制台输入以下代码并按回车：
   ```
   javascript:alert(JSON.parse(localStorage.auth).token)
   ```
   或者直接输入：`JSON.parse(localStorage.auth).token`
4. 复制弹出的认证码（32位字符）粘贴到输入框

---

## 使用方法

### 方式1：GUI程序（推荐）

1. 下载 `延河课堂下载器.exe`
2. 双击运行
3. 输入课程ID（5位数字）和Token
4. 选择视频和下载类型
5. 点击「开始下载」

### 方式2：命令行

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

### 方式3：自己打包

```bash
# 打包GUI版本
python -m PyInstaller --name="延河课堂下载器" --onefile --windowed --noconfirm --clean --hidden-import=customtkinter --collect-all=customtkinter gui_app.py
```

---

## 文件说明

```
yanhedown/
├── gui_app.py          # GUI主程序
├── m3u8dl.py           # 多线程下载引擎
├── utils.py            # 工具函数
├── main.py             # 命令行入口
├── requirements.txt    # Python依赖
└── build_exe.bat       # 一键打包脚本
```

---

## 系统要求

- Windows 10/11
- Python 3.9+ （源码运行）
- 无需安装FFmpeg（已内置）

---

## 注意事项

1. 需要关闭代理/VPN，否则连接失败
2. Token有效期约24小时，过期需重新获取
3. 下载的视频保存在 `output/` 目录
4. 支持断点续传

---

## 版本历史

- **2026** - 多线程自适应下载 + Watchdog监控 + 现代化GUI
- **v2.0** - 原版WebUI + 字幕生成

---

## 许可证

MIT License

---

## 致谢

- 原项目 [AuYang261/BIT_yanhe_download](https://github.com/AuYang261/BIT_yanhe_download)
- CustomTkinter - 现代化UI框架
