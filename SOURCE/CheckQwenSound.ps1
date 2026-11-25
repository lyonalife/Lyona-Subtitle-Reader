param([string]$Device = 'По умолчанию')
$ErrorActionPreference = 'Continue'
$OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$worker = Join-Path $PSScriptRoot 'LyonaWorker.exe'
$models = Join-Path $env:LOCALAPPDATA 'LyonaSubtitleReader\models'
if (-not (Test-Path -LiteralPath $worker)) {
    Write-Host 'LyonaWorker.exe is missing. Extract the complete ZIP first.'
    exit 1
}
foreach ($name in @('qwen-talker-1.7b-base-Q8_0.gguf', 'qwen-tokenizer-12hz-Q8_0.gguf')) {
    if (-not (Test-Path -LiteralPath (Join-Path $models $name))) {
        Write-Host 'Qwen model is missing. Run the setup wizard in the main app first.'
        exit 1
    }
}
Write-Host "Testing Qwen output: $Device"
$line = '{"cmd":"generate","id":1,"text":"\u041f\u0440\u0438\u0432\u0435\u0442! \u042d\u0442\u043e \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0437\u0432\u0443\u043a\u0430.","tempo":1.15,"gain":1.0,"normalize":true,"pronunciation":true,"queued_at":0}'
$raw = @($line | & $worker --qwen-worker x $Device '1.7-designed-medium-low-icl' 2>&1)
$events = @($raw | Where-Object { [string]$_ -like 'LYONA_EVENT::*' } | ForEach-Object {
    try { ([string]$_).Substring(13) | ConvertFrom-Json } catch { }
})
foreach ($event in $events) {
    if ($event.type -eq 'status' -or $event.type -eq 'error') {
        Write-Host ($event.type + ': ' + $event.message)
    }
}
$kinds = @($events | ForEach-Object { $_.type })
if ($LASTEXITCODE -eq 0 -and $kinds -contains 'ready' -and
    $kinds -contains 'first_chunk' -and $kinds -contains 'done') {
    Write-Host 'Qwen generated audio and wrote it to the selected Windows output.'
    Write-Host 'If you heard nothing, choose your actual headphones in Lyona and check Windows Volume Mixer.'
    exit 0
}
Write-Host 'Audio test did not finish. Last worker messages:'
$raw | Select-Object -Last 12 | ForEach-Object { Write-Host $_ }
exit 1
