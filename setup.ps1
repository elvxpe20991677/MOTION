# Installation reproductible de mograph sous Windows 11 (PowerShell 5.1 ou 7), idempotente.
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1              installe (ou complète) puis vérifie
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Tests       idem + tests rapides (étapes 9, 11, 14, 15, 16)
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Defender    idem + exclusion Defender de build\ (admin)
# Prérequis installables par winget (le script indique la commande exacte s'il en manque un) :
#   winget install Git.Git Python.Python.3.12 OpenJS.NodeJS.LTS astral-sh.uv Gyan.FFmpeg
# Versions figées : requirements.txt (pip), package-lock.json (npm), bpy==5.0.1.
param([switch]$Tests, [switch]$Defender)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
# Sortie des outils en UTF-8 (accents des messages du pipeline).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

function Ok([string]$m)   { Write-Host "  OK  $m" -ForegroundColor Green }
function Step([string]$m) { Write-Host "`n== $m" }
function Fail([string]$m) { Write-Host "  ÉCHEC  $m" -ForegroundColor Red; exit 1 }
# Les commandes natives ne lèvent pas d'exception en PowerShell 5.1 : on vérifie le code de sortie.
function Run([string]$what, [scriptblock]$cmd) {
    & $cmd
    if ($LASTEXITCODE -ne 0) { Fail "$what (code $LASTEXITCODE)" }
}
function Has([string]$exe) { return [bool](Get-Command $exe -ErrorAction SilentlyContinue) }

Step "Prérequis"
if (-not (Has "git"))    { Fail "git absent : winget install Git.Git" }
if (-not (Has "node"))   { Fail "Node.js absent : winget install OpenJS.NodeJS.LTS" }
if (-not (Has "uv"))     { Fail "uv absent : winget install astral-sh.uv (puis rouvrez PowerShell)" }
if (-not (Has "ffmpeg")) { Fail "FFmpeg absent : winget install Gyan.FFmpeg (build « full », avec zscale)" }
# Python 3.12 via le lanceur py (installé avec Python.org) ou python du PATH.
$py = $null
if (Has "py") {
    & py -3.12 -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = @("py", "-3.12") }
}
if (-not $py -and (Has "python")) {
    $v = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
    if ($v -eq "3.12") { $py = @("python") }
}
if (-not $py) { Fail "Python 3.12 absent : winget install Python.Python.3.12" }
$ffv = ((& ffmpeg -hide_banner -version | Select-Object -First 1) -split " ")[2]
Ok ("git, node " + (& node --version) + ", Python 3.12, uv, FFmpeg " + $ffv)

$filters = (& ffmpeg -hide_banner -filters 2>$null) -join "`n"
foreach ($f in @("zscale", "premultiply", "alphaextract", "extractplanes", "mergeplanes", "blackdetect", "ebur128", "signalstats")) {
    if ($filters -notmatch " $f ") { Fail "filtre FFmpeg « $f » absent : installez le build « full » (winget install Gyan.FFmpeg)" }
}
$encoders = (& ffmpeg -hide_banner -encoders 2>$null) -join "`n"
foreach ($e in @("libx265", "prores_ks")) {
    if ($encoders -notmatch " $e ") { Fail "encodeur FFmpeg « $e » absent : build « full » requis" }
}
Ok "FFmpeg : zscale, premultiply, extractplanes, mergeplanes, libx265, prores_ks"

Step "Orchestrateur Python 3.12 (.venv)"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $pyExe = $py[0]; $pyArgs = @($py | Select-Object -Skip 1) + @("-m", "venv", ".venv")
    Run "création de .venv" { & $pyExe @pyArgs }
}
Run "pip install -r requirements.txt" { & $venvPy -m pip install --quiet --disable-pip-version-check -r requirements.txt }
Run "playwright install chromium" { & $venvPy -m playwright install chromium }
Ok "playwright, jsonschema, opencv, numpy (versions de requirements.txt) + Chromium"

Step "Bibliothèques navigateur (npm ci)"
Run "npm ci" { & npm ci --silent --no-audit --no-fund }
if (-not (Test-Path "node_modules\gsap\dist\gsap.min.js")) { Fail "gsap absent après npm ci" }
Ok "gsap 3.15.0 + polices @fontsource"

Step "Blender bpy 5.0.1 (.venv-blender, CPython 3.11)"
$bpyPy = Join-Path $PSScriptRoot ".venv-blender\Scripts\python.exe"
if (-not (Test-Path $bpyPy)) { Run "uv venv .venv-blender" { & uv venv --quiet --python 3.11 .venv-blender } }
Run "uv pip install bpy==5.0.1" { & uv pip install --quiet --python $bpyPy bpy==5.0.1 }
$bpyVer = & $bpyPy -c "import bpy; print(bpy.app.version_string)" 2>$null | Select-Object -Last 1
Ok "bpy $bpyVer"
# OptiX : le backend choisit OPTIX si une carte NVIDIA est détectée (device « auto »).
if (Has "nvidia-smi") {
    $gpu = & nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>$null | Select-Object -First 1
    Ok "GPU NVIDIA : $gpu (Cycles OptiX utilisé par défaut)"
} else {
    Write-Host "  (aucun GPU NVIDIA détecté : les plaques 3D seront rendues sur le CPU)"
}

if ($Defender) {
    Step "Exclusion Windows Defender (accélère l'écriture des séquences PNG)"
    $build = if ($env:MOGRAPH_BUILD_DIR) { $env:MOGRAPH_BUILD_DIR } else { Join-Path $PSScriptRoot "build" }
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    try { Add-MpPreference -ExclusionPath $build; Ok "exclusion ajoutée : $build" }
    catch { Write-Host "  Exclusion impossible (lancez PowerShell en administrateur) : $($_.Exception.Message)" -ForegroundColor Yellow }
}

Step "Vérification rapide"
Run "mograph.py validate demo_flat_riso" { & $venvPy mograph.py validate demo_flat_riso | Out-Null }
Ok "validate demo_flat_riso"

if ($Tests) {
    Step "Tests rapides"
    foreach ($t in @("09_presets", "11_compiler", "14_compose", "15_encode", "16_audio")) {
        & $venvPy "tests\verify_step$t.py" *> $null
        if ($LASTEXITCODE -ne 0) { Fail "tests\verify_step$t.py (relancez-le pour le détail)" }
        Ok "étape $t"
    }
}

Write-Host "`nInstallation terminée. Suite : .\.venv\Scripts\python.exe mograph.py all demo_flat_riso"
Write-Host "(porte de déterminisme à refaire sur cette machine, SPEC étape 19)"
Write-Host "Conseils rendus longs : sur secteur, mode performances, veille désactivée sur secteur, --detach."
