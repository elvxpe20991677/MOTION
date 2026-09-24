#!/usr/bin/env bash
# Commandes d'encodage exactes de la scène « test_lowerthird_4k_alpha » (pipeline/encode.py).
# À lancer depuis n'importe où (bash out/<scene>/encode_commands.sh) : le script se place
# à la racine du projet, tous les chemins sont relatifs à cette racine.
# Images : 50 à 25 i/s ; durée 2 s.
set -euo pipefail
SCRIPT_PATH="${0//\\//}"
cd "$(dirname "$SCRIPT_PATH")/../.."
# FFmpeg du PATH, ou celui imposé par MOGRAPH_FFMPEG (même règle que pipeline/config.py).
FFMPEG="${MOGRAPH_FFMPEG:-ffmpeg}"

# prores_4444 -> out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_prores_4444.mov
"$FFMPEG" -y -hide_banner -nostdin -framerate 25 -start_number 0 -i build/test_lowerthird_4k_alpha/master/%06d.png -map 0:v:0 -frames:v 50 -vf 'split[c][a];[c]format=gbrp16le,zscale=rangein=full:range=limited:primariesin=709:primaries=709:transferin=709:transfer=709:matrix=709:dither=error_diffusion,format=yuv444p10le[yuv];[a]format=rgba64be,extractplanes=a,mergeplanes=format=gbrp16be,format=gbrp16le,zscale=rangein=full:range=full:matrix=709,format=yuv444p10le[a10];[yuv][a10]mergeplanes=map0s=0:map0p=0:map1s=0:map1p=1:map2s=0:map2p=2:map3s=1:map3p=0:format=yuva444p10le' -c:v prores_ks -profile:v 4 -vendor apl0 -pix_fmt yuva444p10le -alpha_bits 16 -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv -movflags +write_colr -timecode 01:00:00:00 out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_prores_4444.partial.mov
mv -f out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_prores_4444.partial.mov out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_prores_4444.mov

# hevc_main10 -> out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_flat_hevc_main10.mp4
"$FFMPEG" -y -hide_banner -nostdin -framerate 25 -start_number 0 -i build/test_lowerthird_4k_alpha/master/%06d.png -map 0:v:0 -frames:v 50 -vf 'split[c][a];[c]format=gbrp16le,zscale=rangein=full:range=full,format=gbrp16le[c16];[a]format=rgba64be,extractplanes=a,mergeplanes=format=gbrp16be,format=gbrp16le,zscale=rangein=full:range=full,format=gbrp16le[a16];[c16][a16]premultiply=inplace=0,zscale=rangein=full:range=limited:primariesin=709:primaries=709:transferin=709:transfer=709:matrix=709:dither=error_diffusion,format=yuv420p10le' -c:v libx265 -preset slow -crf 18 -pix_fmt yuv420p10le -profile:v main10 -x265-params colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited:aq-mode=3:no-sao=1:keyint=50:min-keyint=25 -tag:v hvc1 -color_primaries bt709 -color_trc bt709 -colorspace bt709 -color_range tv -movflags +faststart+write_colr out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_flat_hevc_main10.partial.mp4
mv -f out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_flat_hevc_main10.partial.mp4 out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_flat_hevc_main10.mp4
