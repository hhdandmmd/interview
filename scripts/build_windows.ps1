# 使用项目虚拟环境构建，资源固定随包带入，不依赖运行时工作目录。
param([string]$BuildPython = ".build-venv\Scripts\python.exe")
$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $ProjectRoot
try {
    & $BuildPython -m PyInstaller --noconfirm --clean --onedir --name finpipe --paths src `
        --add-data "src/finpipe/resources;finpipe/resources" `
        --exclude-module matplotlib --exclude-module scipy --exclude-module IPython `
        --exclude-module sklearn --exclude-module numba --exclude-module pytest `
        --exclude-module tkinter --exclude-module notebook --exclude-module dask `
        scripts/finpipe_entry.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller构建失败" }
} finally { Pop-Location }
