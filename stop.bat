@echo off
setlocal
chcp 65001 >nul
title 超级投研 Agent - 停止服务

echo 正在停止超级投研 Agent 服务（端口 8000 / 8501）...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R ":8000[ ]*LISTENING"') do (
    echo   停止后端 PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R ":8501[ ]*LISTENING"') do (
    echo   停止看板 PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
echo 完成。如需重新启动请双击 start.bat。
timeout /t 3 >nul
