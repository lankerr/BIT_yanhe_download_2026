@echo off
chcp 65001 >nul
echo ============================================
echo   延河课堂下载器 - EXE打包工具
echo ============================================
echo.

REM 检查Python环境
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到Python，请先安装Python
    pause
    exit /b 1
)

REM 检查PyInstaller
python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo [安装] 正在安装PyInstaller...
    pip install pyinstaller -q
)

echo.
echo [步骤1] 清理旧的打包文件...
if exist "build" rmdir /s /q "build"
if exist "dist\延河课堂下载器.exe" del "dist\延河课堂下载器.exe"

echo [步骤2] 开始打包（这可能需要几分钟）...
echo.

REM 使用PyInstaller打包
python -m PyInstaller ^
    --name="延河课堂下载器" ^
    --onefile ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --icon=yhkt.ico ^
    --hidden-import=customtkinter ^
    --hidden-import=PIL ^
    --hidden-import=_tkinter ^
    --collect-all=customtkinter ^
    gui_app.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败！
    pause
    exit /b 1
)

echo.
echo ============================================
echo   打包完成！
echo ============================================
echo.
echo 输出文件: dist\延河课堂下载器.exe
echo.

REM 显示文件大小
for %%A in ("dist\延河课堂下载器.exe") do (
    set size=%%~zA
)
echo 文件大小: %size% 字节
echo.

REM 测试运行
set /p test="是否立即测试运行？(Y/N): "
if /i "%test%"=="Y" (
    echo.
    echo [启动] 正在启动程序...
    start "" "dist\延河课堂下载器.exe"
)

pause
