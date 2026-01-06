@echo off
chcp 65001 >nul
echo ====================================
echo    Master Scheduler 启动脚本
echo ====================================
echo.

REM 检查虚拟环境是否存在
if not exist ".venv\Scripts\activate.bat" (
    echo [错误] 未找到虚拟环境 .venv
    echo 请确保已创建虚拟环境：python -m venv .venv
    echo.
    pause
    exit /b 1
)

echo [信息] 激活虚拟环境...
call .venv\Scripts\activate.bat

REM 检查schedule库是否已安装
python -c "import schedule" 2>nul
if errorlevel 1 (
    echo [警告] schedule 库未安装，正在安装...
    pip install schedule
    if errorlevel 1 (
        echo [错误] 安装 schedule 库失败
        pause
        exit /b 1
    )
    echo [信息] schedule 库安装成功
)

echo [信息] 启动 Master Scheduler...
echo [提示] 按 Ctrl+C 可以停止调度器
echo.

REM 运行主调度器
python master_scheduler.py

echo.
echo [信息] Master Scheduler 已停止
pause
