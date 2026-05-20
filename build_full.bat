@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ============================================================
echo   延河课堂下载器 - 完整版 打包
echo   含下载 + PPT 提取 + Whisper 转录
echo   目标体积 ~500MB（不含模型，首次启动自动下载到 %%APPDATA%%）
echo ============================================================
echo.

REM 1. 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERR] 未找到 Python，请先安装 Python 3.9+
    pause & exit /b 1
)

REM 2. 检查打包依赖
python -c "import PyInstaller, faster_whisper, cv2, imagehash, pptx, customtkinter" >nul 2>&1
if errorlevel 1 (
    echo [INFO] 安装 / 补齐依赖 ...
    pip install -r requirements.txt -q || (echo [ERR] 依赖安装失败 & pause & exit /b 1)
    pip install pyinstaller -q
)

REM 3. GPU 提示
echo [STEP] 检测 GPU 环境（可选） ...
nvidia-smi -L 2>nul | findstr /R /C:"GPU" >nul
if errorlevel 1 (
    echo   [WARN] 未检测到 NVIDIA GPU。打包仍会继续，但运行时若无 CUDA：
    echo          Whisper 会自动回退 CPU + int8，large-v3 模型会比较慢。
    echo          GPU 用户：请安装 CUDA 12.x + cuDNN 9 后运行 exe。
) else (
    nvidia-smi -L
    echo   [OK] 检测到 NVIDIA GPU - 转录将以 float16 全速运行
)
echo.

REM 4. 准备 ffmpeg / ffprobe
echo [STEP] 准备 ffmpeg / ffprobe ...
python fetch_ffmpeg.py
if errorlevel 1 (
    echo [ERR] ffmpeg 准备失败，请手动放置到根目录后重试
    pause & exit /b 1
)

REM 5. 清理旧构建
echo [STEP] 清理旧构建产物 ...
if exist "build" rmdir /s /q "build"
if exist "dist\延河课堂下载器-完整版.exe" del /q "dist\延河课堂下载器-完整版.exe"

REM 6. 打包（较慢，5-10 分钟）
echo [STEP] PyInstaller 打包中（首次约 5-10 分钟）...
python -m PyInstaller --noconfirm --clean "延河课堂下载器-完整版.spec"
if errorlevel 1 (
    echo [ERR] 打包失败
    pause & exit /b 1
)

echo.
echo ============================================================
echo   打包完成
echo ============================================================
for %%A in ("dist\延河课堂下载器-完整版.exe") do echo   输出: %%~fA  ^(%%~zA bytes^)
echo.
echo   首次启动「后处理工具」时会自动下载 Whisper 模型（~3GB / large-v3）
echo   下载源：%%HF_ENDPOINT%% = https://hf-mirror.com
echo.

set /p test="是否立即启动测试？(Y/N): "
if /i "%test%"=="Y" start "" "dist\延河课堂下载器-完整版.exe"
endlocal
