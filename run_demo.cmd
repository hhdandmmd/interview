@echo off
chcp 65001 >nul
setlocal
rem 根目录统一演示入口；使用固定真实归档，不读取日常公司配置。
cd /d "%~dp0"
if not exist "%~dp0output" mkdir "%~dp0output"
if not exist "%~dp0output" (
    echo 无法创建output目录，请将完整项目解压到可写入的本地目录。
    pause
    exit /b 2
)
set "FINPIPE_RESULT=%~dp0output\demo_result.json"
if exist "%~dp0windows\finpipe\finpipe.exe" (
    "%~dp0windows\finpipe\finpipe.exe" demo --workspace "%~dp0output\demo" > "%FINPIPE_RESULT%"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" -m finpipe demo --workspace "%~dp0output\demo" > "%FINPIPE_RESULT%"
) else (
    echo 请先按README安装源码依赖，或使用包含Windows程序的完整交付包。
    pause
    exit /b 2
)
set "FINPIPE_EXIT=%ERRORLEVEL%"
if not "%FINPIPE_EXIT%"=="0" type "%FINPIPE_RESULT%"
if "%FINPIPE_EXIT%"=="0" "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$result = Get-Content -LiteralPath $env:FINPIPE_RESULT -Raw -Encoding UTF8 | ConvertFrom-Json; Write-Host ('运行状态：' + $result.status); Write-Host ('财报记录：' + $result.counts.scan_returned_reports + '；因子位置：' + ($result.counts.evaluated + $result.counts.reused)); if ($result.export_path) { Write-Host ('报告路径：' + (Join-Path $result.export_path 'report.html')); Write-Host '复制报告路径到浏览器地址栏打开。' }"
echo.
echo 演示结束，退出码：%FINPIPE_EXIT%
echo 运行摘要：%FINPIPE_RESULT%
pause
exit /b %FINPIPE_EXIT%
