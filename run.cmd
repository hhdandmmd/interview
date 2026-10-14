@echo off
chcp 65001 >nul
setlocal
rem 在项目根目录读取config.py；交付包优先使用自带程序，源码使用项目虚拟环境。
cd /d "%~dp0"
if exist "%~dp0windows\finpipe\finpipe.exe" (
    "%~dp0windows\finpipe\finpipe.exe" --settings "%~dp0config.py"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
) else (
    echo 请先按README安装源码依赖，或使用包含Windows程序的完整交付包。
    pause
    exit /b 2
)
set "FINPIPE_EXIT=%ERRORLEVEL%"
echo.
echo 运行结束，退出码：%FINPIPE_EXIT%
pause
exit /b %FINPIPE_EXIT%
