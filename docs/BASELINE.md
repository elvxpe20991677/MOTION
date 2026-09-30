# BASELINE — état de référence avant l'évolution de MOTION (phase 0)

Mesuré le **30/09/2026** sur la machine de référence (CONTRACT §0). Chaque phase suivante rejoue la
même mesure et se compare à celle-ci : **toute régression bloque la phase** (correction ou retour
arrière). Données brutes complètes : [`docs/baseline/phase-0.json`](baseline/phase-0.json).

## 1. Base de code mesurée

| Élément | Valeur |
|---|---|
| Branche | `feat/phase-0-audit-baseline` |
| Point de départ | `claude/eloquent-hypatia-p581pj` (4956ece, « Essai 8 ») + instantané du travail non commité de la copie principale (59e69f9, 32 fichiers : Éclosion Urbaine, Ondea One, évolutions du compilateur, du runtime, de l'encodeur et des tests ; `.env` exclu) |
| Tag de sauvegarde | `sauvegarde/avant-phase-0` → 59e69f9 |
| Copie principale | `C:/Users/perro/Desktop/MOTION` **non modifiée** (lue seulement) |

Pourquoi pas `main` : `main` (518ab97) et `origin/main` (a8ebed9) n'ont ni les calques `video` /
`subtitles`, ni `beats`, `sheets`, `preview`, ni `verify_features`, ni la CI, ni Éclosion Urbaine,
sur lesquels repose la feuille de route. `origin/main` porte en plus un commit (a8ebed9, une ligne
ajoutée à `context.txt`) absent de la branche de travail : conflit trivial à prévoir à la fusion.

## 2. Méthode (rejouable)

```bash
.venv/Scripts/python.exe scripts/baseline.py run --label phase-N          # ~7 min
.venv/Scripts/python.exe scripts/baseline.py compare docs/baseline/phase-0.json build/_baseline/phase-N/baseline.json
```
`run` enchaîne, un par un : les 8 scripts `tests/verify_step*.py` puis `tests/verify_features.py`,
puis sur la scène de référence **`demo_flat_riso`** (16:9, 1080p, 25 i/s, 2 plans, fondu de 10
images, graphique en barres, sans audio ni 3D : 165 images maître) dans un `build/` et un `out/`
isolés et vidés : `render` → `determinism` → `compose` → `encode` → `qc`. Il relève l'empreinte
SHA-256 des pixels de **toutes** les images des plans et du maître, le MD5 des images décodées et le
SHA-256 des fichiers de chaque livrable, le verdict QC et les versions exactes.
`compare` sort avec un code ≠ 0 si un test régresse, si une empreinte d'image change ou si le
verdict QC change (`--allow-pixel-change` / `--allow-encode-change` pour un changement voulu et
documenté). Test négatif fait : une empreinte et un test altérés sont bien signalés (code 1).

## 3. Versions exactes

| Élément | Version |
|---|---|
| OS / CPU | Windows 11 10.0.26200 natif, AMD Ryzen 9 7845HX, 24 threads, 64 Go |
| GPU | RTX 4060 Laptop 8 Go, pilote 616.92 |
| FFmpeg | **9.0.2** full_build gyan.dev (`winget`), via le PATH |
| Python orchestrateur | 3.12.10 |
| pip (`.venv`) | playwright 1.56.0, jsonschema 4.23.0, opencv-python-headless 4.12.0.88, numpy 2.2.6 ; dépendances indirectes : attrs 26.1.0, greenlet 3.5.6, jsonschema-specifications 2025.9.1, pyee 13.0.1, referencing 0.37.0, rpds-py 2026.6.3, typing_extensions 4.16.0 |
| Chromium | 141.0.7390.37 (playwright build v1194) |
| Node / npm / uv | 24.19.0 / 12.0.2 / 0.11.7 |
| npm | gsap 3.15.0, @fontsource/* 5.3.0 (10 familles) |
| Blender (`.venv-blender`) | bpy 5.0.1 sous CPython 3.11.15 ; numpy 1.26.4, cython 3.3.0, requests 2.34.2, zstandard 0.25.0, certifi 2026.7.22, charset-normalizer 3.5.1, idna 3.20, urllib3 2.8.0 |

Les environnements du worktree ont été recréés à l'identique de la copie principale (`pip freeze`,
`npm ls`, `uv pip freeze` comparés par `diff` : identiques).

**Écarts documentaires relevés et corrigés dans le même commit** : CONTRACT §0 indiquait FFmpeg
8.1.2 (réel : 9.0.2), Node 24.17.0 (réel : 24.19.0), CPython 3.11 (précisé : 3.11.15).
**Écart laissé à la phase 1** : la CI (`.github/workflows/tests.yml`) installe FFmpeg 6.1.1 (paquet
Ubuntu 24.04) et Node 22, sans vérifier de version ; les dépendances indirectes pip et bpy ne sont
épinglées nulle part (`requirements.txt` ne fige que 4 paquets).

## 4. Résultats — deux exécutions complètes consécutives

| Script | Exécution A | Exécution B | Durée |
|---|---|---|---|
| verify_step09_presets | 327 PASS, 0 FAIL | identique | 0,5 s |
| verify_step11_compiler | 147 PASS, 0 FAIL | identique | 1,7 s |
| verify_step12_web | 26 PASS, 0 FAIL (8 INFO) | identique | 83 s |
| verify_step13_blender | 37 PASS, 0 FAIL (10 INFO) | identique | 37 s |
| verify_step14_compose | 32 PASS, 0 FAIL | identique | 1,2 s |
| verify_step15_encode | 80 PASS, 0 FAIL (5 INFO) | identique | 3,8 s |
| verify_step16_audio | 37 PASS, 0 FAIL (2 INFO) | identique | 10 s |
| verify_step17_qc | 134 PASS, 0 FAIL, 2 WARN ¹ | identique | 55 s |
| verify_features | 51 PASS, 0 FAIL (2 INFO) | identique | 166 s |

¹ WARN attendus : niveaux hors EBU R103 de livrables **volontairement** fautifs du jeu de test.

### Scène de référence `demo_flat_riso`

| Mesure | Valeur (A = B) |
|---|---|
| Porte de déterminisme | **8/8** images témoins identiques au bit près (navigateur neuf, ordre `chart:89, title:80, chart:0, chart:5, title:61, title:84, title:0, chart:83`) |
| Empreinte des 175 images des plans (85 + 90) | `e40eae413f4726e09f11199eb689e16d1a8328f8edd0e255375f8d3f79bef8cc` |
| Empreinte des 165 images maître | `e03ac824e272c91947c44b130a0872dceb2cbd9ca8b958573df131cda20d05c2` |
| HEVC Main10 (images décodées, MD5) | `9f3dc569313862707f7e6e686f177214` — fichier identique à l'octet (4 712 253 o) |
| ProRes 422 HQ (images décodées, MD5) | `b1336f6d7a79687ab610ef59785d278e` — fichier identique à l'octet (160 485 208 o) |
| QC | **CONFORME** : 26 PASS, 0 FAIL, 0 WARN, 4 SKIP (audio et loudness : scène sans son) |
| Temps | render 33 s, determinism 7 s, compose 2 s, encode 13 s, qc 5 s |

**Reproductibilité** : exécutions A et B identiques sur tous les points (`compare` : « baseline
identique »), y compris les octets des fichiers HEVC et ProRes. **Contre-épreuve historique** : les
175 images des plans sont identiques au bit près à celles du rendu du 24/09/2026 conservé dans
`C:/Users/perro/Desktop/MOTION/build/demo_flat_riso/` (autre installation, même machine).

## 5. Hygiène des secrets

- `.env` contient 2 variables (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`) ; aucune valeur n'est
  citée ici ni dans les journaux.
- Audit (valeurs lues par script, jamais affichées) : `git log --all -p -- .env .env.*` = vide ;
  `git log --all -S <valeur>` = 0 commit pour chaque valeur ; 0 occurrence dans les 122 fichiers
  suivis. **La clé n'a jamais été committée : aucune révocation nécessaire.**
- `.gitignore` ignore désormais `.env` et `.env.*` (sauf `.env.example`, modèle sans valeurs).
- Attention : la copie principale garde l'ancien `.gitignore` tant que cette branche n'y est pas
  fusionnée ; d'ici là, un `git add .` y indexerait `.env`.

## 6. Nettoyage proposé (NON appliqué — décision à prendre)

| Élément | Constat | Proposition |
|---|---|---|
| `archive-bCq7IK/` | 9,2 Mo : une seule archive `gk_3.1.75_windows_amd64.zip` (CLI GitKraken), déjà ignorée par git (`archive-*/`), présente seulement dans la copie principale | la supprimer ou la déplacer hors du dépôt (aucun effet sur git) |
| `context.txt` | suivi par git ; identique à `docs/SPEC.md` à 2 lignes de consignes près (« reprends là où il s'est coupé » ; sur `origin/main`, une 3e : « n'exécute aucune commande système ») | `git rm context.txt` (contenu conservé dans `docs/SPEC.md` et l'historique) ; supprime aussi le conflit à venir avec `origin/main` |
| Branche `claude/motion-pipeline-evolution-582d24` | créée pour la session sur `origin/main`, inutilisée | la supprimer après fusion des phases |
| Dépendances indirectes | non épinglées (pip et bpy) | fichier de contraintes figé (phase 1, avec l'alignement de la CI) |
