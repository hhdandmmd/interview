@echo off
chcp 65001 >nul
rem 双击入口固定使用可执行文件所在位置，演示结果写入用户目录。
set "FINPIPE_DIR=%~dp0"
if not exist "%LOCALAPPDATA%\Finpipe" mkdir "%LOCALAPPDATA%\Finpipe"
set "FINPIPE_RESULT=%LOCALAPPDATA%\Finpipe\demo_result.json"
"%FINPIPE_DIR%finpipe.exe" demo --workspace "%LOCALAPPDATA%\Finpipe\demo" > "%FINPIPE_RESULT%"
set "FINPIPE_EXIT=%ERRORLEVEL%"
type "%FINPIPE_RESULT%"
echo.
echo 演示结束，退出码：%FINPIPE_EXIT%
echo 结果目录：%LOCALAPPDATA%\Finpipe\demo
echo 运行摘要：%FINPIPE_RESULT%
if "%FINPIPE_EXIT%"=="0" "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -Command "$result = Get-Content -LiteralPath $env:FINPIPE_RESULT -Raw -Encoding UTF8 | ConvertFrom-Json; if ($result.export_path) { $report = Join-Path $result.export_path 'report.html'; if (Test-Path -LiteralPath $report -PathType Leaf) { Write-Host ('报告路径：' + $report); Write-Host '复制报告路径到浏览器或资源管理器打开。' } }"
pause
exit /b %FINPIPE_EXIT%
