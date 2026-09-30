#!/usr/bin/env bash
# Commandes d'encodage exactes de la scène « eclosion_urbaine » (pipeline/encode.py).
# À lancer depuis n'importe où (bash out/<scene>/encode_commands.sh) : le script se place
# à la racine du projet, tous les chemins sont relatifs à cette racine.
# Images : 744 à 24 i/s ; durée 31 s.
set -euo pipefail
SCRIPT_PATH="${0//\\//}"
cd "$(dirname "$SCRIPT_PATH")/../.."
# FFmpeg du PATH, ou celui imposé par MOGRAPH_FFMPEG (même règle que pipeline/config.py).
FFMPEG="${MOGRAPH_FFMPEG:-ffmpeg}"

# hevc_main10 -> out/eclosion_urbaine/eclosion_urbaine_hevc_main10.mp4
"$FFMPEG" -y -hide_banner -nostdin -framerate 24 -start_number 0 -i build/eclosion_urbaine/master/%06d.png -i assets/audio/eclosion_urbaine_musique.wav -map 0:v:0 -map 1:a:0 -frames:v 744 -vf zscale=rangein=full:range=limited:primariesin=709:primaries=709:transferin=709:transfer=709:matrix=709:dither=error_diffusion,format=yuv420p10le -c:v libx265 -preset slow -crf 18 -pix_fmt yuv420p10le -profile:v main10 -x265-params colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited:aq-mode=3:no-sao=1:keyint=48:min-keyint=24 -tag:v hvc1 -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv -movflags +faststart+write_colr -af atrim=end=31,loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-14.60:measured_TP=-0.93:measured_LRA=11.70:measured_thresh=-25.16:offset=-0.51:linear=true:print_format=none,aresample=48000,apad=whole_dur=31,atrim=end=31 -c:a aac -b:a 320k -ar 48000 out/eclosion_urbaine/eclosion_urbaine_hevc_main10.partial.mp4
mv -f out/eclosion_urbaine/eclosion_urbaine_hevc_main10.partial.mp4 out/eclosion_urbaine/eclosion_urbaine_hevc_main10.mp4

# prores_422hq -> out/eclosion_urbaine/eclosion_urbaine_prores_422hq.mov
"$FFMPEG" -y -hide_banner -nostdin -framerate 24 -start_number 0 -i build/eclosion_urbaine/master/%06d.png -i assets/audio/eclosion_urbaine_musique.wav -map 0:v:0 -map 1:a:0 -frames:v 744 -vf zscale=rangein=full:range=limited:primariesin=709:primaries=709:transferin=709:transfer=709:matrix=709:dither=error_diffusion,format=yuv422p10le -c:v prores_ks -profile:v 3 -vendor apl0 -pix_fmt yuv422p10le -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv -movflags +write_colr -timecode 01:00:00:00 -af atrim=end=31,loudnorm=I=-14:TP=-1.5:LRA=11:measured_I=-14.60:measured_TP=-0.93:measured_LRA=11.70:measured_thresh=-25.16:offset=-0.51:linear=true:print_format=none,aresample=48000,apad=whole_dur=31,atrim=end=31 -c:a pcm_s24le -ar 48000 out/eclosion_urbaine/eclosion_urbaine_prores_422hq.partial.mov
mv -f out/eclosion_urbaine/eclosion_urbaine_prores_422hq.partial.mov out/eclosion_urbaine/eclosion_urbaine_prores_422hq.mov
