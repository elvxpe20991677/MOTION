# QC demo_voix : CONFORME

**Verdict : CONFORME**

- Scène : Démo voix off (phase 4)
- Format : 16:9 1080p, 25 i/s, 75 images (3.0 s), sortie 1920x1080, alpha : non
- Contrôles : 18 PASS, 0 FAIL, 0 WARN, 0 SKIP
- Généré le 2026-10-01T18:40:25+00:00 ; ffmpeg version 9.0.2-full_build-www.gyan.dev

## Livrables

| Profil | Fichier | Taille (octets) | Échecs bloquants |
|---|---|---|---|
| hevc_main10 | C:\Users\perro\Desktop\MOTION\.claude\worktrees\motion-pipeline-evolution-582d24\out\demo_voix\demo_voix_hevc_main10.mp4 | 1429305 | 0 |

## Provenance

- IA utilisée (déclaration du 2026-10-01) : rôles voice
  - ElevenLabs / eleven_multilingual_v2 — voix off
- Mention : Voix off synthétique (ElevenLabs).
- Assets produits par IA (assets/ai/) : `assets/ai/voice/demo_voix_da284e889ada.mp3`, `assets/ai/voice/demo_voix_da284e889ada.words.json`
  - `assets/ai/voice/demo_voix_da284e889ada.mp3` (audio, SHA-256 `7010acc8e7dfb366…`) : ElevenLabs / eleven_multilingual_v2, généré le 2026-10-01, licence : Conditions d'utilisation ElevenLabs (offre starter), coût 11 caractères ElevenLabs décomptés (27 envoyés), source api ; prompt : « Bonjour, ceci est un essai. »
  - `assets/ai/voice/demo_voix_da284e889ada.words.json` (text, SHA-256 `6edf6eaa023b5787…`) : ElevenLabs / eleven_multilingual_v2, généré le 2026-10-01, licence : Conditions d'utilisation ElevenLabs (offre starter), coût 11 caractères ElevenLabs décomptés (27 envoyés), source api ; prompt : « Bonjour, ceci est un essai. »

## Contrôles

| Statut | Id | Contrôle | Attendu | Mesuré | Bloquant |
|---|---|---|---|---|---|
| **PASS** | `hevc_main10.readable` | hevc_main10 : fichier présent et lisible | 1 flux vidéo | 1 flux vidéo | oui |
| **PASS** | `hevc_main10.codec` | hevc_main10 : codec vidéo | hevc | hevc | oui |
| **PASS** | `hevc_main10.profile` | hevc_main10 : profil du codec | Main 10 | Main 10 | oui |
| **PASS** | `hevc_main10.pix_fmt` | hevc_main10 : format de pixel | yuv420p10le | yuv420p10le | oui |
| **PASS** | `hevc_main10.resolution` | hevc_main10 : résolution | 1920x1080 | 1920x1080 | oui |
| **PASS** | `hevc_main10.fps` | hevc_main10 : cadence r_frame_rate et avg_frame_rate | 25/1 | r_frame_rate=25/1 avg_frame_rate=25/1 | oui |
| **PASS** | `hevc_main10.frames` | hevc_main10 : nombre d'images compté (nb_read_frames) | 75 | 75 | oui |
| **PASS** | `hevc_main10.duration` | hevc_main10 : durée vidéo (± 1/2 image) | seconds=3, tolerance_s=0.02 | 3 | oui |
| **PASS** | `hevc_main10.hvc1` | hevc_main10 : étiquette HEVC hvc1 | hvc1 | hvc1 | oui |
| **PASS** | `hevc_main10.color` | hevc_main10 : métadonnées couleur (bt709 x3, plage tv) | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | color_primaries=bt709, color_transfer=bt709, color_space=bt709, color_range=tv | oui |
| **PASS** | `hevc_main10.levels` | hevc_main10 : niveaux vidéo (EBU R103 v3.0) | reserved_codes=aucun (4..1019), ebu_r103_v3_gamut=Y et R'G'B' filtrés dans 20..984 sur au moins 99 % de chaque image, ebu_r103_v3_nominal=Y et R'G'B' filtrés d… | y_min=154, y_max=940, c_min=392, c_max=825, frames_raw_outside_55_966=0, frames=75, r103_frames_out_of_gamut=0, r103_max_area_pct=0.0005, r103_worst_frame=11, … | non |
| **PASS** | `hevc_main10.black` | hevc_main10 : noir non voulu (blackdetect) | max_black_s=0, pix_th=0.03, informative=non | black_s=0 | oui |
| **PASS** | `hevc_main10.audio` | hevc_main10 : piste audio (codec, 48 kHz, durée) | codec=aac, sample_rate=48000, duration_s=3, tolerance_s=0.065 | codec=aac, sample_rate=48000, duration_s=3 | oui |
| **PASS** | `hevc_main10.loudness` | hevc_main10 : loudness EBU R128 (intégré, true peak) | integrated_lufs=-14, tolerance_lu=1, true_peak_max_dbtp=-1 | integrated_lufs=-13.1, true_peak_dbtp=-1.5, lra=20 | oui |
| **PASS** | `scene.safe_zone` | scène : zones sûres (relevés __layout) | zone=title, rect_px=[96, 54, 1824, 1026], max_continuous_out_s=0.5, epsilon_px=1 | max_continuous_out_s=0, excursions=0, records=32 | oui |
| **PASS** | `scene.determinism` | scène : déterminisme (rendu témoin dans un navigateur neuf) | ok=oui, total_min=1, matches_equal_total=oui | ok=oui, total=4, matches=4, checked=4 | oui |
| **PASS** | `scene.ai_disclosure` | scène : déclaration d'usage de l'IA | provenance.ai_disclosure (ai_used: true, rôles, outils, date) dès qu'un asset de assets/ai/ est utilisé | ai_assets=[assets/ai/voice/demo_voix_da284e889ada.mp3, assets/ai/voice/demo_voix_da284e889ada.words.json], declared=oui, ai_used=oui, roles=[voice] | non |
| **PASS** | `scene.ai_lock` | scène : assets IA gelés (assets.lock.json) | chaque asset de assets/ai/ utilisé est verrouillé, présent et d'empreinte identique | ai_assets=2, problems=0 | oui |

## Détails des échecs

Aucun échec bloquant.
