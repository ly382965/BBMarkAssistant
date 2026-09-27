param(
    [ValidateSet('modelscope', 'huggingface', 'auto')]
    [string]$ModelSource = 'modelscope',
    [string]$MineruPackage = 'mineru>=4.0.7,<5',
    [string[]]$AppData
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw '需要 uv，请先安装 uv 或使用项目已有的 uv 可执行文件运行本脚本。'
}
$env:UV_CACHE_DIR = Join-Path $PSScriptRoot '.runtime/uv-cache'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$mineruPython = Join-Path $PSScriptRoot '.mineru-venv/Scripts/python.exe'
$configureScript = Join-Path $PSScriptRoot 'scripts/configure_mineru.py'
$localWrapper = Join-Path $PSScriptRoot 'scripts/mineru_local.py'
if (-not (Test-Path -LiteralPath $configureScript) -or -not (Test-Path -LiteralPath $localWrapper)) {
    throw '缺少 scripts/configure_mineru.py 或 scripts/mineru_local.py，请使用完整的应用安装目录。'
}
if (-not (Test-Path -LiteralPath $mineruPython)) {
    uv python install 3.12 --install-dir .runtime --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) { throw '安装项目内 Python 失败' }
    $runtimePython = Get-ChildItem -LiteralPath .runtime -Directory -Filter 'cpython-3.12*' | Sort-Object Name -Descending | Select-Object -First 1
    if (-not $runtimePython) { throw '未找到项目内 Python 3.12' }
    uv venv .mineru-venv --python (Join-Path $runtimePython.FullName 'python.exe')
    if ($LASTEXITCODE -ne 0) { throw '创建 MinerU 环境失败' }
}
uv pip install --python $mineruPython $MineruPackage
if ($LASTEXITCODE -ne 0) { throw '安装 MinerU 失败；请检查官方平台要求' }

& $mineruPython $configureScript --models-only
if ($LASTEXITCODE -ne 0) { throw '生成 MinerU 本地模型配置失败' }
Write-Host '正在下载 Standard OCR 模型，模型与缓存均保存在当前项目目录。'
& $mineruPython $localWrapper models download --tier standard --small-backend onnx --vlm-engine llama-cpp --source $ModelSource
if ($LASTEXITCODE -ne 0) { throw 'MinerU 模型下载失败；应用 OCR 设置尚未更新，重新运行可续传' }
& $mineruPython $localWrapper models verify --tier standard --small-backend onnx --vlm-engine llama-cpp
if ($LASTEXITCODE -ne 0) { throw 'MinerU 模型验证失败；应用 OCR 设置尚未更新' }

$configureArgs = @($configureScript, '--configure-app')
foreach ($dataPath in $AppData) {
    $configureArgs += @('--app-data', $dataPath)
}
& $mineruPython @configureArgs
if ($LASTEXITCODE -ne 0) { throw 'MinerU 已安装，但更新应用 OCR 设置失败；请查看输出和设置文件权限' }
Write-Host 'MinerU 与 Standard 模型已安装并验证，OCR 本地命令已配置。请关闭并重新打开 BB 作业批改助手。'
Write-Host '模型完整性验证不等于实际文档识别；在应用中选择一份 PDF 执行 OCR 即可检查识别效果。'
