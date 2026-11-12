# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

<#
.SYNOPSIS
  Final speed test of the RWKV model sizes on a collection copy (CPU only).

.DESCRIPTION
  Runs rslib/src/rwkv/app_speed_bench.rs (release build) for every size and
  mode: the state build after a size switch (cold: a fresh process per run;
  warm: repeated in one process), the cache load at profile open (cold and
  warm), retrievability scoring of every reviewed card after each, and the
  stored state bytes per card and note. Each cold run is its own process, so
  its peak RAM is that operation's. Then times the Python side of a build on
  the same history. Results: one JSON line per run in $OutDir/results.jsonl,
  medians in $OutDir/summary.md.

  The collection is copied once into $OutDir/collection.anki2 and only that
  copy is opened. The script refuses any path under AppData\Roaming\Anki2.
  It checks the machine is quiet first (GPU idle, CPU load low) unless
  -AllowBusy is given.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File qt/tools/rwkv_speed_test.ps1 `
    -Collection C:/Users/jonat/workspace/wt/speed-test/.local/collection/collection.anki2
#>
param(
    [Parameter(Mandatory = $true)][string]$Collection,
    [string]$OutDir = "",
    [string[]]$Runtimes = @(
        "big-pair", "big-one", "big-instant",
        "small-pair", "small-one", "small-curve", "small-instant",
        "tiny-pair", "tiny-instant"
    ),
    [int]$Repeats = 5,
    [switch]$AllowBusy,
    [switch]$SkipPython,
    [int]$MaxOtherCpuPercent = 10
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
if (-not $OutDir) {
    $OutDir = Join-Path $repo (".local/speed-test-" + (Get-Date -Format "yyyyMMdd-HHmm"))
}
$Collection = (Resolve-Path $Collection).Path
if ($Collection -match "AppData\\Roaming\\Anki2" -or $Collection -match "AppData/Roaming/Anki2") {
    throw "Refusing a live profile path; copy the collection first."
}
New-Item -ItemType Directory -Force $OutDir | Out-Null
$results = Join-Path $OutDir "results.jsonl"
$log = Join-Path $OutDir "run.log"

function Log([string]$text) {
    $line = "{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $text
    Write-Output $line
    Add-Content -Path $log -Value $line -Encoding utf8
}

# --- Quiet check -------------------------------------------------------------
function Get-CpuLoad {
    $samples = @()
    for ($i = 0; $i -lt 5; $i++) {
        $samples += (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
        Start-Sleep -Seconds 2
    }
    return ($samples | Measure-Object -Average).Average
}
$gpu = "n/a"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpu = (& nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader) -join "; "
}
$cpu = Get-CpuLoad
Log "before: CPU load $cpu% (10 s average), GPU $gpu"
if (-not $AllowBusy) {
    if ($cpu -gt $MaxOtherCpuPercent) { throw "CPU load $cpu% > $MaxOtherCpuPercent%; wait for a quiet window or pass -AllowBusy." }
    if ($smi) {
        $util = [int](((& nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits) | Select-Object -First 1).Trim())
        if ($util -gt 5) { throw "GPU busy ($util%); wait for the GPU queue to be idle or pass -AllowBusy." }
    }
}

# --- Build ---------------------------------------------------------------------
$env:PATH = "$env:USERPROFILE\.cargo\bin;$env:PATH"
if (-not $env:PROTOC) {
    $protoc = "C:/Users/jonat/workspace/wt/anki-tiny/out/extracted/protoc/bin/protoc.exe"
    if (Test-Path (Join-Path $repo "out/extracted/protoc/bin/protoc.exe")) {
        $protoc = Join-Path $repo "out/extracted/protoc/bin/protoc.exe"
    }
    $env:PROTOC = $protoc
}
Push-Location $repo
try {
    Log "building the release test binary"
    $exe = $null
    # Through cmd so that cargo's progress on stderr never becomes a PowerShell error.
    $messages = & cmd /c "cargo test --release -p anki --lib --no-run --message-format=json 2>nul"
    foreach ($message in $messages) {
        # The name test first: each -match replaces $Matches.
        if ($message -match '"name":"anki"' -and $message -match '"executable":"([^"]+)"') {
            $exe = $Matches[1] -replace '\\\\', '\'
        }
    }
    if (-not $exe) { throw "could not find the anki test binary" }
    Log "binary: $exe"
} finally {
    Pop-Location
}

# --- Collection copy -----------------------------------------------------------
$work = Join-Path $OutDir "work"
New-Item -ItemType Directory -Force $work | Out-Null
$copy = Join-Path $OutDir "collection.anki2"
Copy-Item -Path $Collection -Destination $copy -Force
if (Test-Path "$Collection-wal") { Copy-Item -Path "$Collection-wal" -Destination "$copy-wal" -Force }
Log ("collection copy: {0:N0} MB" -f ((Get-Item $copy).Length / 1MB))

function Invoke-Bench([string]$runtime, [string]$ops, [int]$iterations, [string]$label, [hashtable]$extra = @{}) {
    $env:ANKI_SPEED_COLLECTION = $copy
    $env:ANKI_SPEED_RUNTIMES = $runtime
    $env:ANKI_SPEED_OPS = $ops
    $env:ANKI_SPEED_ITERATIONS = "$iterations"
    $env:ANKI_SPEED_WORK_DIR = $work
    $env:ANKI_SPEED_OUT = $results
    $env:ANKI_SPEED_LABEL = $label
    foreach ($key in $extra.Keys) { Set-Item -Path "env:$key" -Value $extra[$key] }
    Remove-Item env:RAYON_NUM_THREADS -ErrorAction SilentlyContinue
    & $exe rwkv_app_speed_benchmark --ignored --nocapture --test-threads 1 | Out-File -Append -Encoding utf8 $log
    if ($LASTEXITCODE -ne 0) { throw "bench failed: $runtime $ops" }
    foreach ($key in $extra.Keys) { Remove-Item -Path "env:$key" -ErrorAction SilentlyContinue }
}

# --- Runs --------------------------------------------------------------------
foreach ($runtime in $Runtimes) {
    Log "$runtime : cold build + score x$Repeats"
    for ($i = 0; $i -lt $Repeats; $i++) { Invoke-Bench $runtime "build,score" 1 "cold-build" }
    Log "$runtime : cold load + score x$Repeats"
    for ($i = 0; $i -lt $Repeats; $i++) { Invoke-Bench $runtime "load,score" 1 "cold-load" }
    Log "$runtime : warm build x$Repeats (after one untimed)"
    Invoke-Bench $runtime "build" ($Repeats + 1) "warm-build"
    Log "$runtime : warm load x$Repeats (after one untimed)"
    Invoke-Bench $runtime "load" ($Repeats + 1) "warm-load"
    Log "$runtime : state bytes"
    Invoke-Bench $runtime "state" 1 "state"
}

# --- Python side ---------------------------------------------------------------
if (-not $SkipPython) {
    $pyenv = "C:/Users/jonat/workspace/wt/anki-tiny/out/pyenv/Scripts/python.exe"
    $generated = "C:/Users/jonat/workspace/wt/anki-tiny/out"
    if (Test-Path (Join-Path $repo "out/pyenv/Scripts/python.exe")) {
        $pyenv = Join-Path $repo "out/pyenv/Scripts/python.exe"
        $generated = Join-Path $repo "out"
    }
    if (Test-Path $pyenv) {
        $dump = Join-Path $work "history.bin"
        Log "Python side: dumping the native history (collection data, stays in $work)"
        Invoke-Bench "small-curve" "build" 1 "dump" @{ ANKI_SPEED_DUMP_HISTORY = $dump }
        & $pyenv -I (Join-Path $repo "qt/tools/rwkv_speed_python_overhead.py") $dump --repeat $Repeats `
            --path (Join-Path $repo "pylib") --path "$generated/pylib" `
            --path (Join-Path $repo "qt") --path "$generated/qt" | Add-Content -Path $results -Encoding utf8
        Remove-Item $dump -Force
    } else {
        Log "Python side skipped: no out/pyenv"
    }
}

$cpu = Get-CpuLoad
Log "after: CPU load $cpu% (10 s average)"
& python (Join-Path $repo "qt/tools/rwkv_speed_summary.py") $results | Out-File -Encoding utf8 (Join-Path $OutDir "summary.md")
Get-Content (Join-Path $OutDir "summary.md")
Log "done: $OutDir"
