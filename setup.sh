#!/usr/bin/env bash
# Installation reproductible de mograph (Linux / macOS / WSL2), idempotente : relancer ne casse rien.
#   ./setup.sh            installe (ou complète) puis vérifie
#   ./setup.sh --tests    idem, puis rejoue les tests rapides des étapes 9, 11, 14, 15, 16
# Versions figées : requirements.txt (pip), package-lock.json (npm), bpy==5.0.1.
set -euo pipefail
cd "$(dirname "$0")"

RUN_TESTS=0
[ "${1:-}" = "--tests" ] && RUN_TESTS=1

ok()   { printf '  \033[32mOK\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mÉCHEC\033[0m %s\n' "$1" >&2; exit 1; }
step() { printf '\n== %s\n' "$1"; }

step "Prérequis"
command -v git >/dev/null || fail "git absent (sudo apt install git / brew install git)"
command -v node >/dev/null || fail "Node.js ≥ 22 absent (https://nodejs.org ou brew install node)"
command -v ffmpeg >/dev/null || fail "FFmpeg absent (sudo apt install ffmpeg / brew install ffmpeg)"
PY=""
for c in python3.12 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info[:2] != (3, 12))'; then PY="$c"; break; fi
done
[ -n "$PY" ] || fail "Python 3.12 absent (sudo apt install python3.12-venv / brew install python@3.12)"
if ! command -v uv >/dev/null; then
  echo "  uv absent : installation (Astral, fournit CPython 3.11 pour bpy)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "git, node $(node --version), $("$PY" --version), uv, $(ffmpeg -version | head -1 | cut -d' ' -f1-3)"

# zscale (libzimg), prores_ks et libx265 sont indispensables à l'encodage.
FILTERS=$(ffmpeg -hide_banner -filters 2>/dev/null)
for f in zscale premultiply alphaextract extractplanes mergeplanes blackdetect ebur128 signalstats; do
  grep -q " $f " <<<"$FILTERS" || fail "filtre FFmpeg « $f » absent : installez un FFmpeg ≥ 6.1 compilé avec libzimg"
done
ENCODERS=$(ffmpeg -hide_banner -encoders 2>/dev/null)
for e in libx265 prores_ks; do grep -q " $e " <<<"$ENCODERS" || fail "encodeur FFmpeg « $e » absent"; done
ok "FFmpeg : zscale, premultiply, extractplanes, mergeplanes, libx265, prores_ks"

step "Orchestrateur Python 3.12 (.venv)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --disable-pip-version-check -r requirements.txt
.venv/bin/python -m playwright install chromium >/dev/null
ok "$(.venv/bin/python -c 'import playwright, jsonschema, cv2, numpy; print("playwright", "jsonschema", jsonschema.__version__ if hasattr(jsonschema,"__version__") else "", "opencv", cv2.__version__, "numpy", numpy.__version__)' 2>/dev/null)"

step "Bibliothèques navigateur (npm ci)"
npm ci --silent --no-audit --no-fund
[ -f node_modules/gsap/dist/gsap.min.js ] || fail "gsap absent après npm ci"
ok "gsap 3.15.0 + polices @fontsource"

step "Blender bpy 5.0.1 (.venv-blender, CPython 3.11)"
[ -x .venv-blender/bin/python ] || uv venv --quiet --python 3.11 .venv-blender
uv pip install --quiet --python .venv-blender/bin/python bpy==5.0.1
ok "bpy $(.venv-blender/bin/python -c 'import bpy; print(bpy.app.version_string)' 2>/dev/null | tail -1)"

step "Vérification rapide"
.venv/bin/python mograph.py validate demo_flat_riso >/dev/null && ok "validate demo_flat_riso"

if [ "$RUN_TESTS" = 1 ]; then
  step "Tests rapides"
  for t in 09_presets 11_compiler 14_compose 15_encode 16_audio; do
    .venv/bin/python "tests/verify_step${t}.py" >/dev/null 2>&1 && ok "étape ${t}" || fail "tests/verify_step${t}.py (relancez-le pour le détail)"
  done
fi

printf '\nInstallation terminée. Suite : .venv/bin/python mograph.py all demo_flat_riso\n'
printf '(porte de déterminisme à refaire sur chaque nouvelle machine, SPEC étape 19)\n'
