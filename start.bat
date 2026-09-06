@echo off
rem ============================================================
rem 求职作战室 一键启动（Windows）
rem 后端 FastAPI 与前端页面由同一进程托管（backend/main.py 静态托管 frontend/），
rem 启动后会自动打开浏览器；关闭本窗口即停止服务。
rem 可选参数：端口号（默认 8787），例如：start.bat 9000
rem ============================================================
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
title 求职作战室

set "PORT=8787"
if not "%~1"=="" set "PORT=%~1"

if exist ".venv\Scripts\python.exe" goto venv_ok

echo [错误] 未找到虚拟环境解释器 .venv\Scripts\python.exe
echo 请先在项目根目录执行以下命令创建虚拟环境并安装依赖：
echo     python -m venv .venv
echo     .venv\Scripts\python.exe -m pip install fastapi uvicorn openai openpyxl websocket-client requests
echo.
pause
exit /b 1

:venv_ok
echo [启动] 求职作战室 http://127.0.0.1:%PORT% （关闭本窗口即停止服务）
rem 延迟 2 秒打开浏览器，给服务留出监听时间
start "" cmd /c "timeout /t 2 /nobreak >nul & start "" http://127.0.0.1:%PORT%"
".venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port %PORT%

echo.
echo [已停止] 服务已退出。
pause
