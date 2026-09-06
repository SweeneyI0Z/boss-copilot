@echo off
rem ============================================================
rem 一次性（可选，强烈推荐）：自编译 PyInstaller bootloader，降低杀软误报。
rem 原理：pip 预编译的 bootloader 被海量恶意软件共用、哈希被杀软重点标记；
rem 官方文档亦将"避免杀软误报"列为自行编译的理由。自编译产生独一无二的
rem 哈希，是免费手段中降误报收益最大的单项措施。
rem 前置：MinGW-w64（gcc）与 git；未装 gcc 时按下方提示安装后重试。
rem 本脚本会把自编译 bootloader 的 PyInstaller 回装进 .venv。
rem ============================================================
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"

where gcc >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 gcc（MinGW-w64），自编译 bootloader 需要它：
    echo     1. 到 https://winlibs.com 下载 Win64 发行版（带 POSIX 线程模型的即可）
    echo     2. 解压后把其中的 mingw64\bin 目录加入 PATH
    echo     3. 重新运行本脚本
    echo 跳过本步骤也可以打包（build_exe.bat 不受影响），但杀软误报概率会略高。
    exit /b 1
)

where git >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 git，请先安装：https://git-scm.com/download/win
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先按 README 创建虚拟环境。
    exit /b 1
)

for /f "usebackq delims=" %%v in (`".venv\Scripts\python.exe" -c "import PyInstaller;print(PyInstaller.__version__)" 2^>nul`) do set "PYI_VER=%%v"
if not defined PYI_VER (
    echo [错误] .venv 中尚未安装 PyInstaller，请先运行 build_exe.bat 一次。
    exit /b 1
)
echo [1/3] PyInstaller 版本：v%PYI_VER%

if exist "build\pyinstaller-src\.git" (
    echo [2/3] 复用已有源码 build\pyinstaller-src
) else (
    echo [2/3] 克隆 PyInstaller v%PYI_VER% 源码到 build\pyinstaller-src
    if not exist build mkdir build
    git clone --depth 1 --branch "v%PYI_VER%" https://github.com/pyinstaller/pyinstaller.git build\pyinstaller-src || exit /b 1
)

echo [3/3] 编译 bootloader（MinGW-w64 gcc）并把带新 bootloader 的 PyInstaller 回装进 .venv
pushd build\pyinstaller-src\bootloader
"%~dp0.venv\Scripts\python.exe" ./waf distclean all --gcc || (popd & exit /b 1)
popd
".venv\Scripts\python.exe" -m pip install .\build\pyinstaller-src || exit /b 1

echo.
echo [完成] 自编译 bootloader 已生效，请重新运行 build_exe.bat 产出最终发布包。
endlocal
