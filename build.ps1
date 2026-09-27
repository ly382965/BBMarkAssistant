param([string]$OutputDirectory = 'dist')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
uv pip install --python .appvenv/Scripts/python.exe -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw '安装构建依赖失败' }
$originalBuildPath = $env:PATH
try {
    # Avoid collecting unrelated Anaconda ICU DLLs from the user's PATH.
    # Qt uses the Windows ICU ABI; conda's versioned ICU exports are incompatible.
    $env:PATH = (Join-Path $PSScriptRoot '.appvenv/Scripts') + ';' + (Join-Path $env:SystemRoot 'System32') + ';' + $env:SystemRoot
    & .appvenv/Scripts/python.exe -m PyInstaller --noconfirm --clean --windowed --name BBMarkAssistant --distpath $OutputDirectory --paths src --collect-all pyustc --hidden-import keyring.backends.Windows --collect-submodules keyring.backends src/bb_assistant/__main__.py
    if ($LASTEXITCODE -ne 0) { throw '构建失败' }
} finally {
    $env:PATH = $originalBuildPath
}
$applicationOutput = Join-Path $OutputDirectory 'BBMarkAssistant'
Copy-Item -LiteralPath README.md,README.en.md,setup_mineru.ps1 -Destination $applicationOutput
New-Item -ItemType Directory -Path (Join-Path $applicationOutput 'scripts') -Force | Out-Null
Copy-Item -LiteralPath scripts/configure_mineru.py,scripts/mineru_local.py -Destination (Join-Path $applicationOutput 'scripts')
Write-Host "输出: $applicationOutput/BBMarkAssistant.exe"
