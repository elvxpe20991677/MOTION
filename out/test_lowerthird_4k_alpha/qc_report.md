# QC test_lowerthird_4k_alpha : CONFORME

**Verdict : CONFORME**

- Scène : Bandeau nominatif 4K à couche alpha
- Format : 16:9 4k, 25 i/s, 50 images (2.0 s), sortie 3840x2160, alpha : oui
- Contrôles : 23 PASS, 0 FAIL, 2 WARN, 4 SKIP
- Généré le 2026-09-24T08:36:38+00:00 ; ffmpeg version 6.1.1-3ubuntu5

## Livrables

| Profil | Fichier | Taille (octets) | Échecs bloquants |
|---|---|---|---|
| prores_4444 | /home/user/MOTION/out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_prores_4444.mov | 27000140 | 0 |
| hevc_main10_flat | /home/user/MOTION/out/test_lowerthird_4k_alpha/test_lowerthird_4k_alpha_flat_hevc_main10.mp4 | 570166 | 0 |

## Contrôles

| Statut | Id | Contrôle | Attendu | Mesuré | Bloquant |
|---|---|---|---|---|---|
| **PASS** | `prores_4444.readable` | prores_4444 : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `prores_4444.codec` | prores_4444 : codec vidéo | prores | prores | oui |
| **PASS** | `prores_4444.profile` | prores_4444 : profil du codec | 4444 | 4444 | oui |
| **PASS** | `prores_4444.pix_fmt` | prores_4444 : format de pixel | yuva444p10le ou yuva444p12le | yuva444p12le | oui |
| **PASS** | `prores_4444.resolution` | prores_4444 : résolution | 3840x2160 | 3840x2160 | oui |
| **PASS** | `prores_4444.fps` | prores_4444 : cadence r_frame_rate et avg_frame_rate | 25/1 | r_frame_rate=25/1 avg_frame_rate=25/1 | oui |
| **PASS** | `prores_4444.frames` | prores_4444 : nombre d'images compté (nb_read_frames) | 50 | 50 | oui |
| **PASS** | `prores_4444.duration` | prores_4444 : durée vidéo (± 1/2 image) | seconds=2, tolerance_s=0.02 | 2 | oui |
| **PASS** | `prores_4444.color` | prores_4444 : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **PASS** | `prores_4444.timecode` | prores_4444 : piste timecode tmcd | tmcd=oui, timecode=01:00:00:00 | tmcd=oui, timecode=01:00:00:00 | oui |
| **PASS** | `prores_4444.alpha` | prores_4444 : alpha réel (image médiane) | frame=25, alpha_min_below=255 | frame=25, min=0, max=255, pixels_below_255=7716640, fraction_below_255=0.930343 | oui |
| **WARN** | `prores_4444.black` | prores_4444 : noir (informatif : scène à fond transparent) | max_black_s=0, pix_th=0.03, informative=oui | black_s=0.48 | non |
| **SKIP** | `prores_4444.audio` | prores_4444 : piste audio | aucune (scène sans audio) | aucune | non |
| **SKIP** | `prores_4444.loudness` | prores_4444 : loudness EBU R128 | — | — | non |
| **PASS** | `hevc_main10_flat.readable` | hevc_main10_flat : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `hevc_main10_flat.codec` | hevc_main10_flat : codec vidéo | hevc | hevc | oui |
| **PASS** | `hevc_main10_flat.profile` | hevc_main10_flat : profil du codec | Main 10 | Main 10 | oui |
| **PASS** | `hevc_main10_flat.pix_fmt` | hevc_main10_flat : format de pixel | yuv420p10le | yuv420p10le | oui |
| **PASS** | `hevc_main10_flat.resolution` | hevc_main10_flat : résolution | 3840x2160 | 3840x2160 | oui |
| **PASS** | `hevc_main10_flat.fps` | hevc_main10_flat : cadence r_frame_rate et avg_frame_rate | 25/1 | r_frame_rate=25/1 avg_frame_rate=25/1 | oui |
| **PASS** | `hevc_main10_flat.frames` | hevc_main10_flat : nombre d'images compté (nb_read_frames) | 50 | 50 | oui |
| **PASS** | `hevc_main10_flat.duration` | hevc_main10_flat : durée vidéo (± 1/2 image) | seconds=2, tolerance_s=0.02 | 2 | oui |
| **PASS** | `hevc_main10_flat.hvc1` | hevc_main10_flat : étiquette HEVC hvc1 | hvc1 | hvc1 | oui |
| **PASS** | `hevc_main10_flat.color` | hevc_main10_flat : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **WARN** | `hevc_main10_flat.black` | hevc_main10_flat : noir (informatif : scène à fond transparent) | max_black_s=0, pix_th=0.03, informative=oui | black_s=0.48 | non |
| **SKIP** | `hevc_main10_flat.audio` | hevc_main10_flat : piste audio | aucune (scène sans audio) | aucune | non |
| **SKIP** | `hevc_main10_flat.loudness` | hevc_main10_flat : loudness EBU R128 | — | — | non |
| **PASS** | `scene.safe_zone` | scène : zones sûres (relevés __layout) | zone=title, rect_px=[96, 54, 1824, 1026], max_continuous_out_s=0.5, epsilon_px=1 | max_continuous_out_s=0.2, excursions=1, records=10 | oui |
| **PASS** | `scene.determinism` | scène : déterminisme (rendu témoin dans un navigateur neuf) | ok=oui, total_min=1, matches_equal_total=oui | ok=oui, total=8, matches=8, checked=8 | oui |

## Détails des échecs

Aucun échec bloquant.

## Avertissements (non bloquants)

- `prores_4444.black` : 0.480 s de noir détectées ; attendu pour une scène à alpha (zones transparentes aplaties sur noir) : à vérifier à l'œil.
- `hevc_main10_flat.black` : 0.480 s de noir détectées ; attendu pour une scène à alpha (zones transparentes aplaties sur noir) : à vérifier à l'œil.
