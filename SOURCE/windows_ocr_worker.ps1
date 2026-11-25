# Persistent local Windows OCR process. One cropped OBS frame per stdin line.
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Operation($op, [type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
    try { $task.Wait() } catch { throw $task.Exception.InnerException }
    return $task.Result
}
$streamType = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$writerType = [Windows.Storage.Streams.DataWriter,Windows.Storage.Streams,ContentType=WindowsRuntime]
$decoderType = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$ocrType = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$resultType = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
$languageType = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
$engine = $ocrType::TryCreateFromLanguage([Activator]::CreateInstance($languageType, @('ru')))
if (-not $engine) { throw 'Windows OCR Russian language is unavailable' }
[Console]::Out.WriteLine('LYONA_OCR::' + '{"ready":true}')
while ($null -ne ($line = [Console]::In.ReadLine())) {
    try {
        $request = $line | ConvertFrom-Json
        if ($request.cmd -eq 'exit') { break }
        $bytes = [Convert]::FromBase64String([string]$request.png)
        $stream = [Activator]::CreateInstance($streamType)
        try {
            $writer = [Activator]::CreateInstance($writerType, @($stream))
            try {
                $writer.WriteBytes($bytes)
                $null = Await-Operation ($writer.StoreAsync()) ([uint32])
                $null = $writer.DetachStream()
            } finally { $writer.Dispose() }
            $stream.Seek(0)
            $decoder = Await-Operation ($decoderType::CreateAsync($stream)) $decoderType
            $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) $bitmapType
            try {
                $result = Await-Operation ($engine.RecognizeAsync($bitmap)) $resultType
                $response = @{id=$request.id; text=[string]$result.Text}
            } finally { $bitmap.Dispose() }
        } finally { $stream.Dispose() }
    } catch {
        $response = @{id=$request.id; error=[string]$_.Exception.Message}
    }
    [Console]::Out.WriteLine('LYONA_OCR::' + ($response | ConvertTo-Json -Compress -Depth 2))
}
