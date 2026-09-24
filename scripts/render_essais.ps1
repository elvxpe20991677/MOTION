# Rend les 9 scènes d'essai UNE PAR UNE (du plus léger au plus lourd) puis affiche un bilan.
#   powershell -ExecutionPolicy Bypass -File .\scripts\render_essais.ps1            tout, rendu final
#   powershell -ExecutionPolicy Bypass -File .\scripts\render_essais.ps1 -Draft     préversions rapides
#   powershell -ExecutionPolicy Bypass -File .\scripts\render_essais.ps1 -Only 01,07
# Chaque essai : validate --layout (zones sûres) puis all (plaques 3D, web, déterminisme, encodage,
# planches contact, QC). Journal complet : build\_essais\<essai>.log ; vidéos : out\<essai>\.
param([switch]$Draft, [string[]]$Only)

$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path $PSScriptRoot -Parent)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "Lancez d'abord setup.ps1 (venv absent)." -ForegroundColor Red; exit 1 }

$essais = @(
    "essai_01_2d_explainer", "essai_04_reel_musique", "essai_05_vhs_glitch", "essai_06_craie",
    "essai_08_video_soustitres", "essai_09_declinaisons", "essai_07_bandeau_4k_alpha",
    "essai_03_mixte", "essai_02_3d_produit"
)
if ($Only) { $essais = $essais | Where-Object { $n = $_.Substring(6, 2); $Only -contains $n } }
$logs = "build\_essais"; New-Item -ItemType Directory -Force -Path $logs | Out-Null
$bilan = @()

foreach ($e in $essais) {
    Write-Host "`n=== $e ===" -ForegroundColor Cyan
    $log = Join-Path $logs "$e.log"
    $t0 = Get-Date
    if ($e -eq "essai_09_declinaisons") {
        $args_ = @("mograph.py", "batch", "scenes\essai_09_declinaisons.json", "--data", "scenes\essai_09_declinaisons.csv")
        if ($Draft) { $args_ += "--draft" }
        & $py @args_ *> $log
        $code = $LASTEXITCODE
    } else {
        & $py mograph.py validate $e --layout *> $log
        $code = $LASTEXITCODE
        if ($code -eq 0) {
            $args_ = @("mograph.py", "all", $e)
            if ($Draft) { $args_ += "--draft" }
            & $py @args_ *>> $log
            $code = $LASTEXITCODE
        } else {
            Write-Host "  mise en page NON CONFORME : voir $log" -ForegroundColor Yellow
        }
    }
    $dur = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    $verdict = if ($Draft) { "préversion" } else { "?" }
    $qc = "out\$e\qc_report.json"
    if (-not $Draft -and (Test-Path $qc)) { $verdict = (Get-Content $qc -Raw -Encoding UTF8 | ConvertFrom-Json).verdict }
    if ($e -eq "essai_09_declinaisons") { $verdict = if ($code -eq 0) { "3 vidéos" } else { "ÉCHEC" } }
    $etat = if ($code -eq 0) { "OK" } else { "ÉCHEC" }
    Write-Host ("  {0}  code {1}  {2} min  {3}" -f $etat, $code, $dur, $verdict) -ForegroundColor $(if ($code -eq 0) { "Green" } else { "Red" })
    $bilan += [pscustomobject]@{ Essai = $e; Etat = $etat; Code = $code; Minutes = $dur; Verdict = $verdict }
}

Write-Host "`n=== BILAN ===" -ForegroundColor Cyan
$bilan | Format-Table -AutoSize
Write-Host "Vidéos : out\<essai>\ ; planches contact : out\<essai>\planche_contact.png ; journaux : $logs\"
if ($bilan | Where-Object { $_.Code -ne 0 }) { exit 1 } else { exit 0 }
