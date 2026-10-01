# QC eclosion_urbaine : CONFORME

**Verdict : CONFORME**

- Scène : Éclosion Urbaine — film concept
- Format : 16:9 1080p, 24 i/s, 744 images (31.0 s), sortie 1920x1080, alpha : non
- Contrôles : 31 PASS, 0 FAIL, 0 WARN, 0 SKIP
- Généré le 2026-10-01T12:24:14+00:00 ; ffmpeg version 9.0.2-full_build-www.gyan.dev

## Livrables

| Profil | Fichier | Taille (octets) | Échecs bloquants |
|---|---|---|---|
| hevc_main10 | C:\Users\perro\Desktop\MOTION\.claude\worktrees\motion-pipeline-evolution-582d24\out\eclosion_urbaine\eclosion_urbaine_hevc_main10.mp4 | 19188825 | 0 |
| prores_422hq | C:\Users\perro\Desktop\MOTION\.claude\worktrees\motion-pipeline-evolution-582d24\out\eclosion_urbaine\eclosion_urbaine_prores_422hq.mov | 692199998 | 0 |

## Provenance

Aucun bloc `provenance` dans la scène.
- Assets produits par IA (assets/ai/) : aucun

## Contrôles

| Statut | Id | Contrôle | Attendu | Mesuré | Bloquant |
|---|---|---|---|---|---|
| **PASS** | `hevc_main10.readable` | hevc_main10 : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `hevc_main10.codec` | hevc_main10 : codec vidéo | hevc | hevc | oui |
| **PASS** | `hevc_main10.profile` | hevc_main10 : profil du codec | Main 10 | Main 10 | oui |
| **PASS** | `hevc_main10.pix_fmt` | hevc_main10 : format de pixel | yuv420p10le | yuv420p10le | oui |
| **PASS** | `hevc_main10.resolution` | hevc_main10 : résolution | 1920x1080 | 1920x1080 | oui |
| **PASS** | `hevc_main10.fps` | hevc_main10 : cadence r_frame_rate et avg_frame_rate | 24/1 | r_frame_rate=24/1 avg_frame_rate=24/1 | oui |
| **PASS** | `hevc_main10.frames` | hevc_main10 : nombre d'images compté (nb_read_frames) | 744 | 744 | oui |
| **PASS** | `hevc_main10.duration` | hevc_main10 : durée vidéo (± 1/2 image) | seconds=31, tolerance_s=0.020833 | 31 | oui |
| **PASS** | `hevc_main10.hvc1` | hevc_main10 : étiquette HEVC hvc1 | hvc1 | hvc1 | oui |
| **PASS** | `hevc_main10.color` | hevc_main10 : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **PASS** | `hevc_main10.levels` | hevc_main10 : niveaux vidéo (EBU R103 v3.0) | reserved_codes=aucun (4..1019), ebu_r103_v3_gamut=Y et R'G'B' filtrés dans 20..984 sur au moins 99 % de chaque image, ebu_r103_v3_nominal=Y et R'G'B' filtrés d… | y_min=52, y_max=1001, c_min=122, c_max=803, frames_raw_outside_55_966=105, frames=744, r103_frames_out_of_gamut=0, r103_max_area_pct=0.0138, r103_worst_frame=3… | non |
| **PASS** | `hevc_main10.black` | hevc_main10 : noir non voulu (blackdetect) | max_black_s=0, pix_th=0.03, informative=non | black_s=0 | oui |
| **PASS** | `hevc_main10.audio` | hevc_main10 : piste audio (codec, 48 kHz, durée) | codec=aac, sample_rate=48000, duration_s=31, tolerance_s=0.066667 | codec=aac, sample_rate=48000, duration_s=31 | oui |
| **PASS** | `hevc_main10.loudness` | hevc_main10 : loudness EBU R128 (intégré, true peak) | integrated_lufs=-14, tolerance_lu=1, true_peak_max_dbtp=-1 | integrated_lufs=-13.9, true_peak_dbtp=-1.3, lra=9 | oui |
| **PASS** | `prores_422hq.readable` | prores_422hq : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `prores_422hq.codec` | prores_422hq : codec vidéo | prores | prores | oui |
| **PASS** | `prores_422hq.profile` | prores_422hq : profil du codec | HQ | HQ | oui |
| **PASS** | `prores_422hq.pix_fmt` | prores_422hq : format de pixel | yuv422p10le | yuv422p10le | oui |
| **PASS** | `prores_422hq.resolution` | prores_422hq : résolution | 1920x1080 | 1920x1080 | oui |
| **PASS** | `prores_422hq.fps` | prores_422hq : cadence r_frame_rate et avg_frame_rate | 24/1 | r_frame_rate=24/1 avg_frame_rate=24/1 | oui |
| **PASS** | `prores_422hq.frames` | prores_422hq : nombre d'images compté (nb_read_frames) | 744 | 744 | oui |
| **PASS** | `prores_422hq.duration` | prores_422hq : durée vidéo (± 1/2 image) | seconds=31, tolerance_s=0.020833 | 31 | oui |
| **PASS** | `prores_422hq.color` | prores_422hq : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **PASS** | `prores_422hq.levels` | prores_422hq : niveaux vidéo (EBU R103 v3.0) | reserved_codes=aucun (4..1019), ebu_r103_v3_gamut=Y et R'G'B' filtrés dans 20..984 sur au moins 99 % de chaque image, ebu_r103_v3_nominal=Y et R'G'B' filtrés d… | y_min=76, y_max=987, c_min=126, c_max=790, frames_raw_outside_55_966=31, frames=744, r103_frames_out_of_gamut=0, r103_max_area_pct=0.0023, r103_worst_frame=567… | non |
| **PASS** | `prores_422hq.timecode` | prores_422hq : piste timecode tmcd | tmcd=oui, timecode=01:00:00:00 | tmcd=oui, timecode=01:00:00:00 | oui |
| **PASS** | `prores_422hq.black` | prores_422hq : noir non voulu (blackdetect) | max_black_s=0, pix_th=0.03, informative=non | black_s=0 | oui |
| **PASS** | `prores_422hq.audio` | prores_422hq : piste audio (codec, 48 kHz, durée) | codec=pcm_s24le, sample_rate=48000, duration_s=31, tolerance_s=0.066667 | codec=pcm_s24le, sample_rate=48000, duration_s=31 | oui |
| **PASS** | `prores_422hq.loudness` | prores_422hq : loudness EBU R128 (intégré, true peak) | integrated_lufs=-14, tolerance_lu=1, true_peak_max_dbtp=-1 | integrated_lufs=-13.9, true_peak_dbtp=-1.5, lra=9 | oui |
| **PASS** | `scene.safe_zone` | scène : zones sûres (relevés __layout) | zone=title, rect_px=[96, 54, 1824, 1026], max_continuous_out_s=0.5, epsilon_px=1 | max_continuous_out_s=0, excursions=0, records=158 | oui |
| **PASS** | `scene.determinism` | scène : déterminisme (rendu témoin dans un navigateur neuf) | ok=oui, total_min=1, matches_equal_total=oui | ok=oui, total=10, matches=10, checked=10 | oui |
| **PASS** | `scene.ai_disclosure` | scène : déclaration d'usage de l'IA | provenance.ai_disclosure (ai_used: true, rôles, outils, date) dès qu'un asset de assets/ai/ est utilisé | ai_assets=[], declared=non, ai_used=—, roles=— | non |

## Détails des échecs

Aucun échec bloquant.
