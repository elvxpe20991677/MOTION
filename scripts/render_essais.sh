#!/usr/bin/env bash
# Rend les 9 scènes d'essai UNE PAR UNE (Linux/macOS) ; --draft pour des préversions ;
# ESSAIS="01 07" pour une sélection. Journal : build/_essais/<essai>.log ; vidéos : out/<essai>/.
cd "$(dirname "$0")/.."
DRAFT=""; [ "${1:-}" = "--draft" ] && DRAFT="--draft"
PY=.venv/bin/python
ALL="essai_01_2d_explainer essai_04_reel_musique essai_05_vhs_glitch essai_06_craie essai_08_video_soustitres essai_09_declinaisons essai_07_bandeau_4k_alpha essai_03_mixte essai_02_3d_produit"
mkdir -p build/_essais
fails=0
for e in $ALL; do
  n=${e:6:2}
  if [ -n "${ESSAIS:-}" ] && [[ " $ESSAIS " != *" $n "* ]]; then continue; fi
  log=build/_essais/$e.log; t0=$(date +%s)
  if [ "$e" = essai_09_declinaisons ]; then
    $PY mograph.py batch scenes/essai_09_declinaisons.json --data scenes/essai_09_declinaisons.csv $DRAFT > "$log" 2>&1; code=$?
  else
    $PY mograph.py validate "$e" --layout > "$log" 2>&1 && $PY mograph.py all "$e" $DRAFT >> "$log" 2>&1; code=$?
  fi
  v=$(python3 -c "import json;print(json.load(open('out/$e/qc_report.json'))['verdict'])" 2>/dev/null || echo "-")
  echo "$e code=$code $(( ($(date +%s)-t0) )) s QC=$v" | tee -a build/_essais/bilan.txt
  [ $code -ne 0 ] && fails=$((fails+1))
done
exit $fails
