@echo off
rem ============================================================
rem 一次性（可选，强烈推荐）：自编译 PyInstaller bootloader，降低杀软误报。
rem 原理：pip 预编译的 bootloader 被海量恶意软件共用、哈希被杀软重点标记；
rem 官方文档亦将"避免杀软误报"列为自行编译的理由。自编译产生独一无二的
rem 哈希，是免费手段中降误报收益最大的单项措施。
rem 前置：MinGW-w64（gcc）。未装时到 https://winlibs.com 下载 Win64 发行版，
rem 解压后把其中的 mingw64\bin 加入 PATH 再运行本脚本。
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

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv\Scripts\python.exe，请先按 README 创建虚拟环境。
    exit /b 1
)

rem 注意：for /f 的命令串里不能出现圆括号（cmd 解析坑，会提前截断 in 子句），
rem 因此用 pyinstaller.exe --version 探测版本，而不是 python -c "print(...)"。
for /f "usebackq delims=" %%v in (`".venv\Scripts\pyinstaller.exe" --version 2^>nul`) do set "PYI_VER=%%v"
if not defined PYI_VER (
    echo [错误] .venv 中尚未安装 PyInstaller，请先运行 build_exe.bat 一次。
    exit /b 1
)
echo [1/3] PyInstaller 版本：v%PYI_VER%

rem 源码获取走 codeload 压缩包而非 git clone：git 协议在部分网络下被重置，
rem 而 codeload 与 release 资产走同一 CDN，可用 curl 直连（Win10+ 自带 curl）。
if exist "build\pyinstaller-src\setup.py" (
    echo [2/3] 复用已有源码 build\pyinstaller-src
) else (
    echo [2/3] 下载 PyInstaller v%PYI_VER% 源码包到 build\pyinstaller-src
    if not exist build mkdir build
    curl -sL -o build\pyi-src.tar.gz "https://codeload.github.com/pyinstaller/pyinstaller/tar.gz/refs/tags/v%PYI_VER%" || exit /b 1
    tar -xzf build\pyi-src.tar.gz -C build || exit /b 1
    move "build\pyinstaller-%PYI_VER%" "build\pyinstaller-src" >nul || exit /b 1
)

echo [3/3] 编译 bootloader（MinGW-w64 gcc）并把带新 bootloader 的 PyInstaller 回装进 .venv
pushd build\pyinstaller-src\bootloader
"%~dp0.venv\Scripts\python.exe" ./waf distclean all --gcc || (popd & exit /b 1)
popd
".venv\Scripts\python.exe" -m pip install .\build\pyinstaller-src || exit /b 1

echo.
echo [完成] 自编译 bootloader 已生效，请重新运行 build_exe.bat 产出最终发布包。
endlocal
