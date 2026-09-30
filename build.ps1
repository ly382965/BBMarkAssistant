param([string]$OutputDirectory = 'dist', [switch]$SkipDependencyInstall)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($SkipDependencyInstall) {
    & .appvenv/Scripts/python.exe -c "import PyInstaller, PySide6, pypdfium2, bb_assistant"
    if ($LASTEXITCODE -ne 0) { throw '本地构建依赖不完整，请先安装依赖后重试' }
    Write-Host '复用已安装的构建依赖，不访问包下载服务。'
} else {
    uv pip install --python .appvenv/Scripts/python.exe -e '.[dev]'
    if ($LASTEXITCODE -ne 0) { throw '安装构建依赖失败' }
}
$originalBuildPath = $env:PATH
try {
    # Avoid collecting unrelated Anaconda ICU DLLs from the user's PATH.
    # Qt uses the Windows ICU ABI; conda's versioned ICU exports are incompatible.
    $env:PATH = (Join-Path $PSScriptRoot '.appvenv/Scripts') + ';' + (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    & .appvenv/Scripts/python.exe -m PyInstaller --noconfirm --clean --windowed --name BBMarkAssistant --distpath $OutputDirectory --paths src --collect-all pyustc --collect-all pypdfium2 --collect-all pypdfium2_raw --hidden-import keyring.backends.Windows --collect-submodules keyring.backends src/bb_assistant/__main__.py
    if ($LASTEXITCODE -ne 0) { throw '构建失败' }
} finally {
    $env:PATH = $originalBuildPath
}
$applicationOutput = Join-Path $OutputDirectory 'BBMarkAssistant'
Copy-Item -LiteralPath README.md,README.en.md,setup_mineru.ps1 -Destination $applicationOutput
New-Item -ItemType Directory -Path (Join-Path $applicationOutput 'scripts') -Force | Out-Null
Copy-Item -LiteralPath scripts/configure_mineru.py,scripts/mineru_local.py,scripts/mineru_text_aid.py -Destination (Join-Path $applicationOutput 'scripts')
Write-Host "输出: $applicationOutput/BBMarkAssistant.exe"
