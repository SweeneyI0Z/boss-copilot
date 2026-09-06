@echo off
rem ============================================================
rem 求职作战室 一键打包（Windows onedir 目录版）
rem 产物：dist\boss-copilot\（整个目录）与 dist\boss-copilot-win64.zip
rem 分发请打包整个目录，解压后双击 boss-copilot.exe 运行。
rem 首次使用前：.venv 需已按 README 创建并安装运行依赖；
rem 本脚本会向 .venv 额外安装 pyinstaller（仅构建期使用）。
rem 发布前强烈建议先执行 build_bootloader.bat 自编译 bootloader（降杀软误报）。
rem ============================================================
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先按 README 创建虚拟环境并安装依赖。
    exit /b 1
)

echo [1/4] 安装/校验打包依赖 pyinstaller（装进 .venv，仅构建期使用）
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6,<7" || exit /b 1

echo [2/4] 清理 vendor 引擎的 __pycache__（避免缓存文件混入资源目录）
for /d /r vendor %%i in (__pycache__) do @if exist "%%i" rmdir /s /q "%%i"

echo [3/4] PyInstaller 打包中（onedir + 控制台窗口）
".venv\Scripts\python.exe" -m PyInstaller boss-copilot.spec --noconfirm --clean || exit /b 1

echo [4/4] 压缩发布包 dist\boss-copilot-win64.zip
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\boss-copilot\*' -DestinationPath 'dist\boss-copilot-win64.zip' -Force" || exit /b 1

echo.
echo [完成] 产物：
echo     dist\boss-copilot\boss-copilot.exe （整个目录一起分发）
echo     dist\boss-copilot-win64.zip
echo 提示：发布后固定二进制不再随意重打包；杀软误报处置见 README「打包发布」章节。
endlocal
