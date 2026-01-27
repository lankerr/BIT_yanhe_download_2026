# BIT_yanhe_download 2026

> **2026增强版** - 多线程自适应下载 + 现代化GUI + 一键exe

---

## 原版介绍

本项目可下载[延河课堂 (yanhekt.cn)](https://www.yanhekt.cn/recordCourse)中的课程视频。延河课堂是北京理工大学的在线课堂，提供了大量的课程视频，但是没有提供下载功能。本项目可以下载指定课程的摄像头和屏幕信号，包括无权限的课程。

- **原项目**: [AuYang261/BIT_yanhe_download](https://github.com/AuYang261/BIT_yanhe_download)
- **2026版本**: [lankerr/BIT_yanhe_download_2026](https://github.com/lankerr/BIT_yanhe_download_2026)

欢迎提出建议和 star ！

---

## 原版存在的问题

| 问题 | 描述 | 影响 |
|------|------|------|
| **固定线程数** | 无法根据网络状况调整 | 网络差时容易失败，网络好时速度慢 |
| **无死锁检测** | 下载卡死无法自动恢复 | 需要手动重启程序 |
| **不支持VPN** | 开启代理会报错 | 很多用户无法使用 |
| **WebUI依赖** | 需要启动服务器 | 占用资源，操作复杂 |
| **无独立exe** | 需要Python环境 | 普通用户难以使用 |
| **无进度显示** | 不知道下载进度 | 用户体验差 |

---

## 2026版本改进

### 1. 多线程自适应下载引擎

| 特性 | 原版 | 2026版 |
|------|------|--------|
| 线程策略 | 固定线程数 | 慢启动(16) + 动态调整(1-64) |
| 失败重试 | 立即重试 | 指数退避 + 线程减半 |
| 内存管理 | 无界队列 | 有界队列(2x线程数) |
| 超时处理 | 固定超时 | 自适应超时(40-120s) |

### 2. Watchdog 死锁监控

- 自动检测卡死的下载任务（120秒无活动）
- 自动重启卡死的任务
- 避免单个任务阻塞整体进度

### 3. 现代化GUI（极客黑风格）

- **GitHub Dark 配色**: #0d1117 背景 + #58a6ff 强调色
- **并发控制**: 滑块调整 4-64 线程
- **实时进度**: 每任务进度条 + active/max 线程显示
- **便捷操作**: 打开输出目录、重试失败任务

### 4. 独立exe程序

- 无需安装Python环境
- 双击即用
- 文件大小约30MB

### 5. VPN/代理友好

- 智能检测代理冲突
- 清晰的错误提示
- 支持添加白名单

---

## 快速开始

### 下载exe（推荐）

直接下载 [Release 1.0](https://github.com/lankerr/BIT_yanhe_download_2026/releases/latest) 中的 `延河课堂下载器.exe`

1. 双击运行 `延河课堂下载器.exe`
2. 输入课程ID（5位数字）
3. 输入Token（首次需要）
4. 选择视频并开始下载

### 获取Token步骤

1. 在浏览器打开 https://www.yanhekt.cn 并登录
2. 按 F12 打开开发者工具，切换到「控制台」标签
3. 在控制台输入以下代码并按回车：
   ```
   javascript:alert(JSON.parse(localStorage.auth).token)
   ```
   或者直接输入：`JSON.parse(localStorage.auth).token`
4. 复制弹出的认证码（32位字符）粘贴到输入框

---

## 原版使用说明

### 下载指定课程

在[延河课堂 (yanhekt.cn)](https://www.yanhekt.cn/recordCourse)中找到想下载的课程，以链接为 `https://www.yanhekt.cn/course/40524 `的课程为例，复制地址栏最后的五位编号 40524。

**注意**: 是课程列表的链接（以 `yanhekt.cn/course/五位编号` 开头），不是视频界面的链接（以 `yanhekt.cn/session/六位编号` 开头）。

### 登录延河课堂

新版的延河课堂要求登录才能查看课程列表，故需要先自行登录延河课堂。登录后，在延河课堂的页面的地址栏输入如下代码（注意，浏览器会自动去掉前缀"javascript:"，故直接复制粘贴后需手动补上）：

```
javascript:alert(JSON.parse(localStorage.auth).token)
```

回车后会弹出提示框，复制该身份认证码。

或者可以按 `F12` 键打开"控制台"，在其中输入上述代码，也能得到身份认证码。

### 命令行使用

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

---

## 打包（开发者）

```bash
# 一键打包
build_exe.bat

# 或手动打包
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

- **Windows 10/11**
- **Python 3.9+** （仅源码运行需要）
- 无需安装FFmpeg（已内置）

---

## 注意事项

1. 需要关闭代理/VPN，或将 `yanhekt.cn` 加入白名单
2. Token有效期约24小时，过期需重新获取
3. 下载的视频保存在 `output/` 目录
4. 支持断点续传

---

## 版本历史

- **2026 v1.0** - 多线程自适应下载 + Watchdog监控 + 现代化GUI + 独立exe
- **v2.0** (原版) - WebUI + 字幕生成

---

## 许可证

MIT License

---

## 致谢

- 原项目 [AuYang261/BIT_yanhe_download](https://github.com/AuYang261/BIT_yanhe_download)
- CustomTkinter - 现代化UI框架
