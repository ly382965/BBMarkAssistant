$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.appvenv/Scripts/python.exe')) {
    uv python install 3.12 --install-dir .runtime --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) { throw '安装项目内 Python 失败' }
    $runtimePython = Get-ChildItem -LiteralPath .runtime -Directory -Filter 'cpython-3.12*' | Sort-Object Name -Descending | Select-Object -First 1
    uv venv .appvenv --python (Join-Path $runtimePython.FullName 'python.exe')
    if ($LASTEXITCODE -ne 0) { throw '创建 Python 环境失败' }
    uv pip install --python .appvenv/Scripts/python.exe -e .
    if ($LASTEXITCODE -ne 0) { throw '安装依赖失败' }
}
# Existing source installations also need the new PDF rendering dependency.
& .appvenv/Scripts/python.exe -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('pypdfium2') else 1)"
if ($LASTEXITCODE -ne 0) {
    uv pip install --python .appvenv/Scripts/python.exe -e .
    if ($LASTEXITCODE -ne 0) { throw '更新应用依赖失败' }
}
& .appvenv/Scripts/python.exe -m bb_assistant @args
