@echo off
REM ============================================================
REM Space Signals Data Pipeline Launcher
REM Support: init / daily / test / setup modes
REM ============================================================

setlocal enabledelayedexpansion

echo ============================================================
echo    Space Signals Data Pipeline
echo ============================================================
echo.

REM Parse command line arguments
set MODE=%1
if "%MODE%"=="" set MODE=daily

REM Change to project directory
cd /d F:\AIQuantLab
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Cannot change to F:\AIQuantLab
    pause
    exit /b 1
)

REM Create logs directory if not exists
if not exist "logs" mkdir logs

REM Check virtual environment
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found: .venv
    echo Please create it: python -m venv .venv
    pause
    exit /b 1
)

REM Activate virtual environment (skip for setup mode)
if /i not "%MODE%"=="setup" (
    echo [INFO] Activating virtual environment...
    call .venv\Scripts\activate.bat
    if %ERRORLEVEL% NEQ 0 (
        echo [ERROR] Failed to activate virtual environment
        pause
        exit /b 1
    )
)

REM Route to different modes
if /i "%MODE%"=="init" goto INIT_MODE
if /i "%MODE%"=="daily" goto DAILY_MODE
if /i "%MODE%"=="test" goto TEST_MODE
if /i "%MODE%"=="setup" goto SETUP_MODE

REM Default to daily mode
goto DAILY_MODE

:INIT_MODE
echo ============================================================
echo MODE: Initialization (from 2006-01-01)
echo ============================================================
echo.
echo [WARNING] *** CRITICAL WARNING ***
echo [WARNING] This will process ALL data from 2006 to today
echo [WARNING] Using APPEND mode - WILL CREATE DUPLICATES if data exists!
echo [WARNING] It may take HOURS or even DAYS to complete
echo [WARNING] Recommended to run on weekends or overnight
echo.
echo ============================================================
echo BEFORE PROCEEDING, PLEASE CONFIRM:
echo ============================================================
echo 1. This is your FIRST TIME running initialization
echo 2. Database tables are EMPTY or do NOT exist yet
echo 3. You have checked there is NO existing data
echo.
echo If you have already initialized before, press Ctrl+C to cancel!
echo.
pause
echo.
echo ============================================================
echo FINAL CONFIRMATION - Are you absolutely sure?
echo ============================================================
echo.
set /p CONFIRM="Type 'YES' (all caps) to continue, or anything else to cancel: "
if /i NOT "%CONFIRM%"=="YES" (
    echo.
    echo [CANCELLED] Initialization cancelled by user
    echo.
    call deactivate 2>nul
    exit /b 0
)
echo.

REM Initialize from 2006-01-01 to today
echo [INFO] Starting initialization: 2006-01-01 to today...
echo [INFO] Using append mode for faster performance (no dedup overhead)
python run_space_data_pipeline.py --latest --start-date 20060101 --mode append

REM Check result and auto-exit (no pause)
if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo [SUCCESS] Initialization completed!
    echo [%date% %time%] Init SUCCESS >> logs\init_run.log
    echo ============================================================
    echo.
    echo [INFO] Script will auto-exit in 5 seconds...
    timeout /t 5 /nobreak >nul
) else (
    echo.
    echo ============================================================
    echo [ERROR] Initialization failed, code: %ERRORLEVEL%
    echo [%date% %time%] Init FAILED code=%ERRORLEVEL% >> logs\init_run.log
    echo ============================================================
    echo.
    echo [INFO] Press any key to close...
    pause >nul
)

REM Cleanup and exit (no goto END)
call deactivate 2>nul
exit /b %ERRORLEVEL%

:DAILY_MODE
echo ============================================================
echo MODE: Daily Update (latest 10 days)
echo TIME: %date% %time%
echo ============================================================
echo.

REM Daily update: latest 10 days (covers weekends and incomplete data)
echo [INFO] Updating factor data (latest 10 days)...
echo [INFO] Using auto mode (smart detection for append/update)
python run_space_data_pipeline.py --latest --range-days 10 --mode auto

REM Check result
if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo [SUCCESS] Daily update completed!
    echo [%date% %time%] Daily SUCCESS >> logs\daily_run.log
    echo ============================================================
) else (
    echo.
    echo ============================================================
    echo [ERROR] Daily update failed, code: %ERRORLEVEL%
    echo [%date% %time%] Daily FAILED code=%ERRORLEVEL% >> logs\daily_run.log
    echo ============================================================
)
goto END

:TEST_MODE
echo ============================================================
echo MODE: Test (2 factors, 3 days)
echo ============================================================
echo.

echo [INFO] Running test: qop_stb, b2p (latest 3 days)...
echo [INFO] Using auto mode (smart detection for append/update)
python run_space_data_pipeline.py --signals qop_stb b2p --range-days 3 --mode auto

if %ERRORLEVEL% EQU 0 (
    echo.
    echo [SUCCESS] Test completed!
) else (
    echo.
    echo [ERROR] Test failed, code: %ERRORLEVEL%
)
goto END

:SETUP_MODE
echo ============================================================
echo MODE: Setup Daily Task (Windows Task Scheduler)
echo ============================================================
echo.

REM Check if running as Administrator
net session >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] This mode requires Administrator privileges
    echo.
    echo Please run CMD as Administrator:
    echo   1. Right-click on CMD
    echo   2. Select "Run as administrator"
    echo   3. Run: F:\AIQuantLab\run_daily_space_signals.bat setup
    echo.
    pause
    exit /b 1
)

echo [INFO] Configuring Windows Task Scheduler...
echo.

REM Task configuration
set TASK_NAME=Space Signals Daily Update
set TASK_DESC=Daily update Space factor data (latest 10 days, auto mode)
set SCRIPT_PATH=%CD%\run_daily_space_signals.bat
set SCHEDULE_TIME=01:00

REM Check if task already exists
schtasks /Query /TN "%TASK_NAME%" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo [WARNING] Task already exists: %TASK_NAME%
    echo.
    set /p OVERWRITE="Delete and recreate? (Y/N): "
    if /i "!OVERWRITE!"=="Y" (
        echo [INFO] Deleting existing task...
        schtasks /Delete /TN "%TASK_NAME%" /F
        if %ERRORLEVEL% NEQ 0 (
            echo [ERROR] Failed to delete existing task
            pause
            exit /b 1
        )
        echo [SUCCESS] Existing task deleted
        echo.
    ) else (
        echo [INFO] Keeping existing task, exiting...
        pause
        exit /b 0
    )
)

REM Create the scheduled task
echo [INFO] Creating scheduled task...
echo   Name: %TASK_NAME%
echo   Time: Daily at %SCHEDULE_TIME%
echo   Script: %SCRIPT_PATH% daily
echo.

schtasks /Create ^
    /TN "%TASK_NAME%" ^
    /TR "\"%SCRIPT_PATH%\" daily" ^
    /SC DAILY ^
    /ST %SCHEDULE_TIME% ^
    /RU "%USERNAME%" ^
    /RL HIGHEST ^
    /F

if %ERRORLEVEL% EQU 0 (
    echo.
    echo ============================================================
    echo [SUCCESS] Task created successfully!
    echo ============================================================
    echo.
    echo Task Details:
    echo   - Task Name: %TASK_NAME%
    echo   - Schedule: Daily at %SCHEDULE_TIME%
    echo   - Action: Run daily update (latest 10 days, auto mode)
    echo   - User: %USERNAME%
    echo.
    echo To view/manage the task:
    echo   - Press Win+R, type: taskschd.msc
    echo   - Or run: schtasks /Query /TN "%TASK_NAME%" /V
    echo.
    echo To test the task now:
    echo   schtasks /Run /TN "%TASK_NAME%"
    echo.
    
    set /p TEST_RUN="Run task now for testing? (Y/N): "
    if /i "!TEST_RUN!"=="Y" (
        echo.
        echo [INFO] Starting task...
        schtasks /Run /TN "%TASK_NAME%"
        echo [INFO] Task started in background
        echo [INFO] Check logs\daily_run.log for results
    )
) else (
    echo.
    echo ============================================================
    echo [ERROR] Failed to create task, code: %ERRORLEVEL%
    echo ============================================================
    echo.
    echo Possible reasons:
    echo   - Insufficient permissions
    echo   - Task Scheduler service not running
    echo   - Group policy restrictions
    echo.
)

pause
exit /b 0

:END
REM Deactivate virtual environment
call deactivate 2>nul

echo.
echo [INFO] Script execution completed
echo TIME: %date% %time%
echo.
pause
