param([switch]$SkipInstall)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.buildvenv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -3.11 -m venv (Join-Path $PSScriptRoot '.buildvenv')
}
if (-not $SkipInstall) {
    & $python -m pip install --disable-pip-version-check --only-binary=realtimetts-qwen-native -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости сборки' }
}
$cpuFolder = Join-Path $PSScriptRoot 'cpu_backend'
if (-not (Test-Path -LiteralPath (Join-Path $cpuFolder 'qwentts_cpp_cpu\lib\qwen.dll'))) {
    & $python -m pip install --disable-pip-version-check --no-deps --only-binary=:all: --target $cpuFolder realtimetts-qwen-native-cpu==0.4.2
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось подготовить CPU backend Qwen' }
}
& $python -m PyInstaller --noconfirm --clean LyonaSubtitleReader.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller завершился с ошибкой' }
Write-Host 'Готово:' (Join-Path $PSScriptRoot 'dist\LyonaSubtitleReader_Streamer\LyonaSubtitleReader_Streamer.exe')
