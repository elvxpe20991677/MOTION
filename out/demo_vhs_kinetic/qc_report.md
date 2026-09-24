# QC demo_vhs_kinetic : CONFORME

**Verdict : CONFORME**

- Scène : Tout se rejoue — typo cinétique VHS
- Format : 9:16 1080p, 30 i/s, 150 images (5.0 s), sortie 1080x1920, alpha : non
- Contrôles : 28 PASS, 0 FAIL, 2 WARN, 0 SKIP
- Généré le 2026-09-24T13:56:08+00:00 ; ffmpeg version 6.1.1-3ubuntu5

## Livrables

| Profil | Fichier | Taille (octets) | Échecs bloquants |
|---|---|---|---|
| hevc_main10 | /home/user/MOTION/out/demo_vhs_kinetic/demo_vhs_kinetic_hevc_main10.mp4 | 1062074 | 0 |
| prores_422hq | /home/user/MOTION/out/demo_vhs_kinetic/demo_vhs_kinetic_prores_422hq.mov | 145327340 | 0 |

## Contrôles

| Statut | Id | Contrôle | Attendu | Mesuré | Bloquant |
|---|---|---|---|---|---|
| **PASS** | `hevc_main10.readable` | hevc_main10 : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `hevc_main10.codec` | hevc_main10 : codec vidéo | hevc | hevc | oui |
| **PASS** | `hevc_main10.profile` | hevc_main10 : profil du codec | Main 10 | Main 10 | oui |
| **PASS** | `hevc_main10.pix_fmt` | hevc_main10 : format de pixel | yuv420p10le | yuv420p10le | oui |
| **PASS** | `hevc_main10.resolution` | hevc_main10 : résolution | 1080x1920 | 1080x1920 | oui |
| **PASS** | `hevc_main10.fps` | hevc_main10 : cadence r_frame_rate et avg_frame_rate | 30/1 | r_frame_rate=30/1 avg_frame_rate=30/1 | oui |
| **PASS** | `hevc_main10.frames` | hevc_main10 : nombre d'images compté (nb_read_frames) | 150 | 150 | oui |
| **PASS** | `hevc_main10.duration` | hevc_main10 : durée vidéo (± 1/2 image) | seconds=5, tolerance_s=0.016667 | 5 | oui |
| **PASS** | `hevc_main10.hvc1` | hevc_main10 : étiquette HEVC hvc1 | hvc1 | hvc1 | oui |
| **PASS** | `hevc_main10.color` | hevc_main10 : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **WARN** | `hevc_main10.levels` | hevc_main10 : niveaux vidéo (signalstats) | reserved_codes=aucun (4..1019), luma_r103=[55, 966] | y_min=14, y_max=956, c_min=210, c_max=832, frames_outside_r103=77, frames=150 | non |
| **PASS** | `hevc_main10.black` | hevc_main10 : noir non voulu (blackdetect) | max_black_s=0, pix_th=0.03, informative=non | black_s=0 | oui |
| **PASS** | `hevc_main10.audio` | hevc_main10 : piste audio (codec, 48 kHz, durée) | codec=aac, sample_rate=48000, duration_s=5, tolerance_s=0.058333 | codec=aac, sample_rate=48000, duration_s=5 | oui |
| **PASS** | `hevc_main10.loudness` | hevc_main10 : loudness EBU R128 (intégré, true peak) | integrated_lufs=-14, tolerance_lu=1, true_peak_max_dbtp=-1 | integrated_lufs=-14, true_peak_dbtp=-3.9, lra=1.5 | oui |
| **PASS** | `prores_422hq.readable` | prores_422hq : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `prores_422hq.codec` | prores_422hq : codec vidéo | prores | prores | oui |
| **PASS** | `prores_422hq.profile` | prores_422hq : profil du codec | HQ | HQ | oui |
| **PASS** | `prores_422hq.pix_fmt` | prores_422hq : format de pixel | yuv422p10le | yuv422p10le | oui |
| **PASS** | `prores_422hq.resolution` | prores_422hq : résolution | 1080x1920 | 1080x1920 | oui |
| **PASS** | `prores_422hq.fps` | prores_422hq : cadence r_frame_rate et avg_frame_rate | 30/1 | r_frame_rate=30/1 avg_frame_rate=30/1 | oui |
| **PASS** | `prores_422hq.frames` | prores_422hq : nombre d'images compté (nb_read_frames) | 150 | 150 | oui |
| **PASS** | `prores_422hq.duration` | prores_422hq : durée vidéo (± 1/2 image) | seconds=5, tolerance_s=0.016667 | 5 | oui |
| **PASS** | `prores_422hq.color` | prores_422hq : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **WARN** | `prores_422hq.levels` | prores_422hq : niveaux vidéo (signalstats) | reserved_codes=aucun (4..1019), luma_r103=[55, 966] | y_min=14, y_max=954, c_min=153, c_max=883, frames_outside_r103=139, frames=150 | non |
| **PASS** | `prores_422hq.timecode` | prores_422hq : piste timecode tmcd | tmcd=oui, timecode=01:00:00:00 | tmcd=oui, timecode=01:00:00:00 | oui |
| **PASS** | `prores_422hq.black` | prores_422hq : noir non voulu (blackdetect) | max_black_s=0, pix_th=0.03, informative=non | black_s=0 | oui |
| **PASS** | `prores_422hq.audio` | prores_422hq : piste audio (codec, 48 kHz, durée) | codec=pcm_s24le, sample_rate=48000, duration_s=5, tolerance_s=0.058333 | codec=pcm_s24le, sample_rate=48000, duration_s=5 | oui |
| **PASS** | `prores_422hq.loudness` | prores_422hq : loudness EBU R128 (intégré, true peak) | integrated_lufs=-14, tolerance_lu=1, true_peak_max_dbtp=-1 | integrated_lufs=-14, true_peak_dbtp=-4, lra=1.5 | oui |
| **PASS** | `scene.safe_zone` | scène : zones sûres (relevés __layout) | zone=title, rect_px=[64.8, 230.4, 950.4, 1497.6], max_continuous_out_s=0.5, epsilon_px=1 | max_continuous_out_s=0, excursions=0, records=43 | oui |
| **PASS** | `scene.determinism` | scène : déterminisme (rendu témoin dans un navigateur neuf) | ok=oui, total_min=1, matches_equal_total=oui | ok=oui, total=8, matches=8, checked=8 | oui |

## Détails des échecs

Aucun échec bloquant.

## Avertissements (non bloquants)

- `hevc_main10.levels` : 77 image(s) hors plage EBU R103 (Y 55.0..966.0) : acceptable sur le web, à corriger pour la diffusion (dépassements de compression ou couleurs saturées).
- `prores_422hq.levels` : 139 image(s) hors plage EBU R103 (Y 55.0..966.0) : acceptable sur le web, à corriger pour la diffusion (dépassements de compression ou couleurs saturées).
