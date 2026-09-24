# QC demo_flat_riso : CONFORME

**Verdict : CONFORME**

- Scène : Le rapport 2025 — explainer flat
- Format : 16:9 1080p, 25 i/s, 165 images (6.6 s), sortie 1920x1080, alpha : non
- Contrôles : 24 PASS, 0 FAIL, 0 WARN, 4 SKIP
- Généré le 2026-09-24T10:22:52+00:00 ; ffmpeg version 6.1.1-3ubuntu5

## Livrables

| Profil | Fichier | Taille (octets) | Échecs bloquants |
|---|---|---|---|
| hevc_main10 | /home/user/MOTION/out/demo_flat_riso/demo_flat_riso_hevc_main10.mp4 | 4686627 | 0 |
| prores_422hq | /home/user/MOTION/out/demo_flat_riso/demo_flat_riso_prores_422hq.mov | 160486036 | 0 |

## Contrôles

| Statut | Id | Contrôle | Attendu | Mesuré | Bloquant |
|---|---|---|---|---|---|
| **PASS** | `hevc_main10.readable` | hevc_main10 : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `hevc_main10.codec` | hevc_main10 : codec vidéo | hevc | hevc | oui |
| **PASS** | `hevc_main10.profile` | hevc_main10 : profil du codec | Main 10 | Main 10 | oui |
| **PASS** | `hevc_main10.pix_fmt` | hevc_main10 : format de pixel | yuv420p10le | yuv420p10le | oui |
| **PASS** | `hevc_main10.resolution` | hevc_main10 : résolution | 1920x1080 | 1920x1080 | oui |
| **PASS** | `hevc_main10.fps` | hevc_main10 : cadence r_frame_rate et avg_frame_rate | 25/1 | r_frame_rate=25/1 avg_frame_rate=25/1 | oui |
| **PASS** | `hevc_main10.frames` | hevc_main10 : nombre d'images compté (nb_read_frames) | 165 | 165 | oui |
| **PASS** | `hevc_main10.duration` | hevc_main10 : durée vidéo (± 1/2 image) | seconds=6.6, tolerance_s=0.02 | 6.6 | oui |
| **PASS** | `hevc_main10.hvc1` | hevc_main10 : étiquette HEVC hvc1 | hvc1 | hvc1 | oui |
| **PASS** | `hevc_main10.color` | hevc_main10 : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **PASS** | `hevc_main10.black` | hevc_main10 : noir non voulu (blackdetect) | max_black_s=0, pix_th=0.03, informative=non | black_s=0 | oui |
| **SKIP** | `hevc_main10.audio` | hevc_main10 : piste audio | aucune (scène sans audio) | aucune | non |
| **SKIP** | `hevc_main10.loudness` | hevc_main10 : loudness EBU R128 | — | — | non |
| **PASS** | `prores_422hq.readable` | prores_422hq : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `prores_422hq.codec` | prores_422hq : codec vidéo | prores | prores | oui |
| **PASS** | `prores_422hq.profile` | prores_422hq : profil du codec | HQ | HQ | oui |
| **PASS** | `prores_422hq.pix_fmt` | prores_422hq : format de pixel | yuv422p10le | yuv422p10le | oui |
| **PASS** | `prores_422hq.resolution` | prores_422hq : résolution | 1920x1080 | 1920x1080 | oui |
| **PASS** | `prores_422hq.fps` | prores_422hq : cadence r_frame_rate et avg_frame_rate | 25/1 | r_frame_rate=25/1 avg_frame_rate=25/1 | oui |
| **PASS** | `prores_422hq.frames` | prores_422hq : nombre d'images compté (nb_read_frames) | 165 | 165 | oui |
| **PASS** | `prores_422hq.duration` | prores_422hq : durée vidéo (± 1/2 image) | seconds=6.6, tolerance_s=0.02 | 6.6 | oui |
| **PASS** | `prores_422hq.color` | prores_422hq : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **PASS** | `prores_422hq.timecode` | prores_422hq : piste timecode tmcd | tmcd=oui, timecode=01:00:00:00 | tmcd=oui, timecode=01:00:00:00 | oui |
| **PASS** | `prores_422hq.black` | prores_422hq : noir non voulu (blackdetect) | max_black_s=0, pix_th=0.03, informative=non | black_s=0 | oui |
| **SKIP** | `prores_422hq.audio` | prores_422hq : piste audio | aucune (scène sans audio) | aucune | non |
| **SKIP** | `prores_422hq.loudness` | prores_422hq : loudness EBU R128 | — | — | non |
| **PASS** | `scene.safe_zone` | scène : zones sûres (relevés __layout) | zone=title, rect_px=[96, 54, 1824, 1026], max_continuous_out_s=0.5, epsilon_px=1 | max_continuous_out_s=0, excursions=0, records=207 | oui |
| **PASS** | `scene.determinism` | scène : déterminisme (rendu témoin dans un navigateur neuf) | ok=oui, total_min=1, matches_equal_total=oui | ok=oui, total=8, matches=8, checked=8 | oui |

## Détails des échecs

Aucun échec bloquant.
