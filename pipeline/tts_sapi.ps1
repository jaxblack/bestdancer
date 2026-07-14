# 本周热舞 · SAPI 中文女声配音
# 读取 render_demo.py 生成的 manifest.json，逐条合成 wav。
# 用法: powershell -NoProfile -ExecutionPolicy Bypass -File pipeline\tts_sapi.ps1 2026-W29
param([Parameter(Mandatory = $true)][string]$Week)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root "output/tts/$Week"
$manifestPath = Join-Path $dir 'manifest.json'
if (-not (Test-Path $manifestPath)) { throw "manifest 不存在: $manifestPath" }

$json = Get-Content $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer

# 优先选年轻中文女声
$prefer = @('Microsoft Yaoyao', 'Microsoft Huihui', 'Microsoft Huihui Desktop')
$picked = $null
foreach ($name in $prefer) {
    $v = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Name -eq $name -and $_.Enabled } | Select-Object -First 1
    if ($v) { $picked = $name; break }
}
if (-not $picked) {
    $v = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture -eq 'zh-CN' } | Select-Object -First 1
    if ($v) { $picked = $v.VoiceInfo.Name }
}
if ($picked) { $synth.SelectVoice($picked) }
$synth.Rate = 2   # 略快，显得有活力

foreach ($item in $json.items) {
    $file = Join-Path $dir $item.name
    $synth.SetOutputToWaveFile($file)
    $synth.Speak([string]$item.text)
}
$synth.SetOutputToNull()
$synth.Dispose()
Write-Host "TTS 完成: $($json.items.Count) 段 | 音色: $picked -> $dir"
