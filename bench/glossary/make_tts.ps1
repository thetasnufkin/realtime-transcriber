# make_tts.ps1 - make the audio of the glossary test set with Windows text-to-speech (Haruka, ja-JP).
#   powershell -NoProfile -ExecutionPolicy Bypass -File make_tts.ps1 -Tsv <sentences.tsv> -OutDir <dir>
#   Writes 16 kHz mono 16-bit WAV files 01.wav, 02.wav, ... (one per sentence, in file order).
#   The audio is generated locally and is not committed (only the sentences are).
# Keep this file ASCII only: PowerShell 5.1 reads it in the ANSI code page.
param([Parameter(Mandatory)][string]$Tsv, [Parameter(Mandatory)][string]$OutDir)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voice = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ja-JP' } | Select-Object -First 1
if (-not $voice) { Write-Host '[ERROR] No Japanese voice (ja-JP) is installed.'; exit 1 }
$synth.SelectVoice($voice.VoiceInfo.Name)
$format = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$i = 0
foreach ($line in Get-Content -LiteralPath $Tsv -Encoding UTF8) {
    if ($line -match '^\s*(#|$)') { continue }
    $i++
    $text = ($line -split "`t")[1]
    $out = Join-Path $OutDir ('{0:D2}.wav' -f $i)
    $synth.SetOutputToWaveFile($out, $format)
    $synth.Speak($text)
    $synth.SetOutputToNull()
}
$synth.Dispose()
Write-Host "Wrote $i files with voice $($voice.VoiceInfo.Name) to $OutDir"
