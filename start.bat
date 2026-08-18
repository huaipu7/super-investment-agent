@echo off
setlocal
chcp 65001 >nul
title 超级投研 Agent - 一键启动

set "PYEXE=D:\python\python.exe"
set "PORT_BACKEND=8000"
set "PORT_DASHBOARD=8501"

echo ============================================================
echo   超级投研 Agent - 一键启动
echo   Python : %PYEXE%
echo   后端   : http://127.0.0.1:%PORT_BACKEND%
echo   看板   : http://localhost:%PORT_DASHBOARD%
echo ============================================================
echo.

rem ---- 检测端口占用，避免重复启动（需重启请先运行 stop.bat）----
set "SKIP_BACKEND="
netstat -ano | findstr /R ":8000[ ]*LISTENING" >nul 2>&1
if not errorlevel 1 set "SKIP_BACKEND=1"

set "SKIP_DASHBOARD="
netstat -ano | findstr /R ":8501[ ]*LISTENING" >nul 2>&1
if not errorlevel 1 set "SKIP_DASHBOARD=1"

if defined SKIP_BACKEND (
    echo [提示] 后端端口 8000 已被占用，跳过启动。
) else (
    echo [1/2] 启动后端 ...
    start "Super-Investment-Agent-后端" /D "%~dp0" cmd /k "chcp 65001>nul && set PYTHONIOENCODING=utf-8 && %PYEXE% api\main.py"
)
echo.

if defined SKIP_DASHBOARD (
    echo [提示] 看板端口 8501 已被占用，跳过启动。
) else (
    echo [2/2] 启动看板 ...
    start "Super-Investment-Agent-看板" /D "%~dp0" cmd /k "chcp 65001>nul && set PYTHONIOENCODING=utf-8 && %PYEXE% -m streamlit run ui\dashboard.py --server.port 8501 --server.headless true"
)
echo.

echo 启动完成。用浏览器打开  http://localhost:%PORT_DASHBOARD%
echo 关闭弹出的两个服务窗口即可停止服务。
echo.
timeout /t 6 >nul
exit /b 0
