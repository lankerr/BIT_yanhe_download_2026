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

REM 5. 清理旧构建（onedir：目录形态）
echo [STEP] 清理旧构建产物 ...
if exist "build" rmdir /s /q "build"
if exist "dist\延河课堂下载器-完整版" rmdir /s /q "dist\延河课堂下载器-完整版"
if exist "dist\延河课堂下载器-完整版-onedir.zip" del /q "dist\延河课堂下载器-完整版-onedir.zip"

REM 6. 打包（较慢，5-10 分钟）
echo [STEP] PyInstaller 打包中（首次约 5-10 分钟）...
python -m PyInstaller --noconfirm --clean "延河课堂下载器-完整版.spec"
if errorlevel 1 (
    echo [ERR] 打包失败
    pause & exit /b 1
)

echo.
echo [STEP] 压缩 onedir 目录为分发 ZIP（较慢，请等待）...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\延河课堂下载器-完整版\*' -DestinationPath 'dist\延河课堂下载器-完整版-onedir.zip' -Force"
if errorlevel 1 (
    echo [ERR] ZIP 压缩失败（可直接分发 dist 目录）
) else (
    echo   分发包: dist\延河课堂下载器-完整版-onedir.zip
)

echo.
echo ============================================================
echo   打包完成（onedir 形态）
echo ============================================================
echo   目录: dist\延河课堂下载器-完整版echo   用户用法: 解压 ZIP 后运行目录内的 exe（勿单独拷出 exe）
echo   自检: 完整版.exe --syscheck   安全界面: 完整版.exe --safe-ui
echo.
echo   首次启动「后处理工具」时会自动下载 Whisper 模型（~3GB / large-v3）
echo   下载源：%%HF_ENDPOINT%% = https://hf-mirror.com
echo.

set /p test="是否立即运行 --syscheck 自检测试？(Y/N): "
if /i "%test%"=="Y" start "" /wait "dist\延河课堂下载器-完整版\延河课堂下载器-完整版.exe" --syscheck
endlocal
