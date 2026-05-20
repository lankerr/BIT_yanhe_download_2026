@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

echo ============================================================
echo   延河课堂下载器 - 简易版 打包
echo   仅含视频下载   |  目标体积 ~80MB
echo ============================================================
echo.

REM 1. 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERR] 未找到 Python，请先安装 Python 3.9+
    pause & exit /b 1
)

REM 2. 检查 PyInstaller
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [INFO] 安装 PyInstaller ...
    pip install pyinstaller -q || (echo [ERR] PyInstaller 安装失败 & pause & exit /b 1)
)

REM 3. 准备 ffmpeg / ffprobe
echo [STEP] 准备 ffmpeg / ffprobe ...
python fetch_ffmpeg.py
if errorlevel 1 (
    echo [ERR] ffmpeg 准备失败，请手动放置到根目录后重试
    pause & exit /b 1
)

REM 4. 清理旧构建
echo [STEP] 清理旧构建产物 ...
if exist "build" rmdir /s /q "build"
if exist "dist\延河课堂下载器-简易版.exe" del /q "dist\延河课堂下载器-简易版.exe"

REM 5. 打包
echo [STEP] PyInstaller 打包中（首次约 2-4 分钟）...
python -m PyInstaller --noconfirm --clean "延河课堂下载器-简易版.spec"
if errorlevel 1 (
    echo [ERR] 打包失败
    pause & exit /b 1
)

echo.
echo ============================================================
echo   打包完成
echo ============================================================
for %%A in ("dist\延河课堂下载器-简易版.exe") do echo   输出: %%~fA  ^(%%~zA bytes^)
echo.

set /p test="是否立即启动测试？(Y/N): "
if /i "%test%"=="Y" start "" "dist\延河课堂下载器-简易版.exe"
endlocal
