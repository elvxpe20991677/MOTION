#!/usr/bin/env bash
# Commandes d'encodage exactes de la scène « demo_voix » (pipeline/encode.py).
# À lancer depuis n'importe où (bash out/<scene>/encode_commands.sh) : le script se place
# à la racine du projet, tous les chemins sont relatifs à cette racine.
# Images : 75 à 25 i/s ; durée 3 s.
set -euo pipefail
SCRIPT_PATH="${0//\\//}"
cd "$(dirname "$SCRIPT_PATH")/../.."
# FFmpeg du PATH, ou celui imposé par MOGRAPH_FFMPEG (même règle que pipeline/config.py).
FFMPEG="${MOGRAPH_FFMPEG:-ffmpeg}"

# hevc_main10 -> out/demo_voix/demo_voix_hevc_main10.mp4
"$FFMPEG" -y -hide_banner -nostdin -framerate 25 -start_number 0 -i build/demo_voix/master/%06d.png -i build/demo_voix/audio/mix.wav -map 0:v:0 -map 1:a:0 -frames:v 75 -vf zscale=rangein=full:range=limited:primariesin=709:primaries=709:transferin=709:transfer=709:matrix=709:dither=error_diffusion,format=yuv420p10le -c:v libx265 -preset slow -crf 18 -pix_fmt yuv420p10le -profile:v main10 -x265-params colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited:aq-mode=3:no-sao=1:keyint=50:min-keyint=25 -tag:v hvc1 -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv -movflags +faststart+write_colr -af atrim=end=3,loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-28.91:measured_TP=-14.47:measured_LRA=0.60:measured_thresh=-39.78:offset=-1.73:linear=true:print_format=none,aresample=48000,apad=whole_dur=3,atrim=end=3 -c:a aac -b:a 320k -ar 48000 out/demo_voix/demo_voix_hevc_main10.partial.mp4
mv -f out/demo_voix/demo_voix_hevc_main10.partial.mp4 out/demo_voix/demo_voix_hevc_main10.mp4
