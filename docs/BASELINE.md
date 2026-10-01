# BASELINE — état de référence avant l'évolution de MOTION (phase 0)

Mesuré le **30/09/2026** sur la machine de référence (CONTRACT §0). Chaque phase suivante rejoue la
même mesure et se compare à celle-ci avec `scripts/baseline.py compare` ; une régression (liste
exacte au §2) bloque la phase : correction ou retour arrière. Données brutes complètes :
[`docs/baseline/phase-0.json`](baseline/phase-0.json).

## 1. Base de code mesurée

| Élément | Valeur |
|---|---|
| Branche | `feat/phase-0-audit-baseline` |
| Point de départ | `claude/eloquent-hypatia-p581pj` (4956ece, « Essai 8 ») + instantané du travail non commité de la copie principale : commit 59e69f9, **31 fichiers** (git status en signalait 32 hors `.env` ; `out/demo_flat_riso/encode_commands.sh` n'avait aucun changement de contenu). Contenu : Éclosion Urbaine, Ondea One, évolutions du compilateur, du runtime, de l'encodeur et des tests. `.env` exclu. |
| Tag de sauvegarde | `sauvegarde/avant-phase-0` → 59e69f9 |
| Commit mesuré | 112ef6f (`scripts/baseline.py` corrigé), arbre de travail propre (`git_dirty` vide) pour les deux exécutions |
| Copie principale | arbre de travail `C:/Users/perro/Desktop/MOTION` **non modifié** (lu seulement) ; le dépôt `.git`, partagé par les worktrees, reçoit la branche et le tag |

Pourquoi pas `main` : `main` (518ab97) et `origin/main` (a8ebed9) n'ont ni les calques `video` /
`subtitles`, ni `beats`, `sheets`, `preview`, ni `verify_features`, ni la CI, ni Éclosion Urbaine,
sur lesquels repose la feuille de route. `origin/main` porte un commit absent de la branche
(a8ebed9 : une ligne ajoutée à `context.txt`). La fusion est aujourd'hui **sans conflit**
(`git merge-tree` propre, `context.txt` inchangé sur la branche). Elle deviendrait un conflit
modify/delete si `context.txt` était supprimé avant : fusionner `origin/main` d'abord (voir §6).

## 2. Méthode (rejouable)

```bash
.venv/Scripts/python.exe scripts/baseline.py run --label phase-N          # ~7 min
.venv/Scripts/python.exe scripts/baseline.py compare docs/baseline/phase-0.json build/_baseline/phase-N/baseline.json
```
`run` enchaîne, un par un : tous les `tests/verify_step*.py` (découverts, ordre alphabétique), puis
`tests/verify_features.py`, puis sur la scène de référence **`demo_flat_riso`** (16:9, 1080p,
25 i/s, 2 plans de 85 et 90 images, fondu de 10 images, graphique en barres, sans audio ni 3D :
165 images maître), dans un `build/` et un `out/` isolés et vidés : `render` → `determinism` →
`compose` → `encode` → `qc`. Il relève l'empreinte SHA-256 des pixels de **toutes** les images des
plans et du maître, le MD5 des images décodées et le SHA-256 des fichiers de chaque livrable, le QC,
les versions et l'état git. Il sort en erreur si un test, une étape, la porte ou le QC échoue.

`compare A B` signale une **régression** (code ≠ 0) si, dans B : un test manque, échoue, perd des
PASS ou gagne des FAIL, WARN ou SKIP ; un nouveau test échoue ; la scène manque, une étape échoue,
la porte de déterminisme échoue ; le nombre d'images change ; une empreinte d'image change ou manque ;
un livrable manque, n'est plus décodable ou change d'images décodées ; le verdict QC change ou le QC
gagne des FAIL, WARN ou SKIP. `--allow-pixel-change` et `--allow-encode-change` tolèrent un
changement d'image **voulu et documenté**. Versions, octets d'un livrable aux images identiques et
fichiers non commités ne donnent qu'une NOTE. **Contrôle du comparateur** : 16 cas négatifs
(test absent, PASS en baisse, SKIP en hausse, scène absente, étape en échec, livrable absent,
décodage impossible, empreinte absente ou modifiée, déterminisme en échec, WARN QC en hausse,
nouveau test en échec…) donnent tous le code attendu ; un délai dépassé arrête tout l'arbre de
processus (3,2 s pour un délai de 3 s).

## 3. Versions exactes

| Élément | Version |
|---|---|
| OS / CPU | Windows 11 10.0.26200 natif, AMD Ryzen 9 7845HX, 24 threads, 64 Go |
| GPU | RTX 4060 Laptop 8 Go, pilote 616.92 |
| FFmpeg | **9.0.2** full_build gyan.dev (`winget install Gyan.FFmpeg`), via le PATH |
| Python orchestrateur | 3.12.10 |
| pip (`.venv`) | playwright 1.56.0, jsonschema 4.23.0, opencv-python-headless 4.12.0.88, numpy 2.2.6 ; dépendances indirectes : attrs 26.1.0, greenlet 3.5.6, jsonschema-specifications 2025.9.1, pyee 13.0.1, referencing 0.37.0, rpds-py 2026.6.3, typing_extensions 4.16.0 |
| Chromium | 141.0.7390.37 (playwright build v1194) |
| Node / npm / uv | 24.19.0 / 12.0.2 / 0.11.7 |
| npm | gsap 3.15.0, @fontsource/* 5.3.0 (10 familles) |
| Blender (`.venv-blender`) | bpy 5.0.1 sous CPython 3.11.15 ; numpy 1.26.4, cython 3.3.0, requests 2.34.2, zstandard 0.25.0, certifi 2026.7.22, charset-normalizer 3.5.1, idna 3.20, urllib3 2.8.0 |

Les environnements du worktree ont été recréés à l'identique de la copie principale (`pip freeze`,
`npm ls`, `uv pip freeze` comparés par `diff` : identiques).

**Écarts documentaires corrigés dans CONTRACT §0** : FFmpeg 8.1.2 (réel : 9.0.2), Node 24.17.0
(réel : 24.19.0), CPython 3.11 (précisé : 3.11.15), disque 98,6 Go (réel : 321,6 Go = 299,5 Gio
libres, `Get-PSDrive`).
**Écarts laissés à la phase 1** : la CI (`.github/workflows/tests.yml`, Ubuntu 24.04) installe
FFmpeg par `apt` sans version (6.1.1 relevé), Node 22, Python « 3.12 » et « 3.11 » sans correctif
fixé, `setup-uv` sans version d'uv, et ne vérifie aucune version ; les dépendances indirectes pip
et bpy ne sont épinglées nulle part (`requirements.txt` ne fige que 4 paquets).

## 4. Résultats — deux exécutions complètes consécutives (A et B)

| Script | Résultat (A = B) | Durée (A) |
|---|---|---|
| verify_step09_presets | 327 PASS, 0 FAIL | 0,3 s |
| verify_step11_compiler | 147 PASS, 0 FAIL | 1,2 s |
| verify_step12_web | 26 PASS, 0 FAIL (8 INFO) | 82 s |
| verify_step13_blender | 37 PASS, 0 FAIL, 0 SKIP (10 INFO) | 38 s |
| verify_step14_compose | 32 PASS, 0 FAIL | 0,8 s |
| verify_step15_encode | 80 PASS, 0 FAIL (5 INFO) | 4,7 s |
| verify_step16_audio | 37 PASS, 0 FAIL (2 INFO) | 10 s |
| verify_step17_qc | 91 PASS, 0 FAIL ¹ | 54 s |
| verify_features | 51 PASS, 0 FAIL (2 INFO) | 177 s |

¹ Le test imprime aussi, en retrait, un écho du rapport QC de son jeu conforme (43 lignes, dont
2 WARN de niveaux R103 sur des mires lavfi, verdict CONFORME) : ces lignes ne sont pas des assertions
et ne sont pas comptées.

### Scène de référence `demo_flat_riso`

| Mesure | Valeur (A = B) |
|---|---|
| Porte de déterminisme | **8/8** images témoins identiques au bit près (navigateur neuf, ordre `chart:89, title:80, chart:0, chart:5, title:61, title:84, title:0, chart:83`) |
| Empreinte des 175 images des plans (85 + 90) | `e40eae413f4726e09f11199eb689e16d1a8328f8edd0e255375f8d3f79bef8cc` |
| Empreinte des 165 images maître | `e03ac824e272c91947c44b130a0872dceb2cbd9ca8b958573df131cda20d05c2` |
| HEVC Main10 | images décodées MD5 `9f3dc569313862707f7e6e686f177214` ; fichier SHA-256 `0bf0a1af…fb13f` (4 712 253 o) |
| ProRes 422 HQ | images décodées MD5 `b1336f6d7a79687ab610ef59785d278e` ; fichier SHA-256 `784a8aba…39a85` (160 485 208 o) |
| QC | **CONFORME** : 26 PASS, 0 FAIL, 0 WARN, 4 SKIP (audio et loudness : scène sans son) |
| Temps (A) | render 36 s, determinism 6 s, compose 2 s, encode 14 s, qc 5 s |

**Reproductibilité** : A et B, mesurées au même commit, sont identiques (`compare` : « aucune
régression », aucune NOTE d'octets : les fichiers HEVC et ProRes sont identiques à l'octet).
Une première paire d'exécutions (script non durci) avait déjà donné les mêmes empreintes.
**Contre-épreuve historique** : le rendu du 24/09/2026 conservé dans la copie principale
(`build/demo_flat_riso/`, `out/demo_flat_riso/`) est identique au bit près : 175/175 images des
plans, empreinte du maître `e03ac824…`, et SHA-256 des deux livrables.

## 5. Hygiène des secrets

- `.env` contient 2 variables (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`) ; aucune valeur n'est
  citée ici ni dans les journaux (valeurs lues par script, jamais affichées).
- `git log --all -- .env` : vide ; parmi `.env.*`, seul `.env.example` (sans valeurs, commit
  2fc58a7) apparaît. `git log --all -S <valeur>` : 0 commit pour chaque valeur. 0 occurrence dans
  les 126 fichiers suivis de la branche. **La clé n'a jamais été committée : aucune révocation
  nécessaire.**
- `.gitignore` ignore désormais `.env` et `.env.*` (sauf `.env.example`).
- Attention : la copie principale garde l'ancien `.gitignore` tant que cette branche n'y est pas
  fusionnée ; d'ici là, un `git add .` y indexerait `.env`. Le dépôt GitHub est **public** : un tel
  commit poussé exposerait la clé.

## 6. Nettoyage proposé (NON appliqué — décision à prendre)

| Élément | Constat | Proposition |
|---|---|---|
| `archive-bCq7IK/` | 9,2 Mio : une seule archive `gk_3.1.75_windows_amd64.zip` (CLI GitKraken), déjà ignorée par git (`archive-*/`), présente seulement dans la copie principale | la supprimer ou la déplacer hors du dépôt (aucun effet sur git) |
| `context.txt` | suivi par git ; corps identique à `docs/SPEC.md` (lignes 2 à 531) ; en plus : une ligne « Prompt : » en tête et une consigne finale (deux sur `origin/main`, dont « n'exécute aucune commande système ») ; `SPEC.md` a en plus un en-tête de 5 lignes | fusionner `origin/main` puis `git rm context.txt` (contenu conservé dans `docs/SPEC.md` et l'historique) |
| Branche `claude/motion-pipeline-evolution-582d24` | créée pour la session sur `origin/main`, inutilisée | la supprimer après fusion des phases |
| Dépendances indirectes | non épinglées (pip et bpy) | fichier de contraintes figé (phase 1, avec l'alignement de la CI) |

## 7. Suivi des phases (comparaison à la phase 0)

| Phase | Commit mesuré | `compare` | Écarts (NOTE) | Données |
|---|---|---|---|---|
| 1 — consolidation | ad9ed78 (arbre propre) | **aucune régression** | PASS en hausse : compilateur 147 → 157, QC 91 → 107, QC de la scène 26 → 27 (contrôle `scene.ai_disclosure`) ; empreintes des 175 images, du maître et octets HEVC/ProRes identiques ; porte 8/8 | [`baseline/phase-1.json`](baseline/phase-1.json) |

Coût mesuré de la phase 1 : la mesure R103 décode toutes les images de chaque livrable (QC de
`demo_flat_riso` : 5 s → 19 s ; `verify_step17_qc` : 54 s → 175 s ; Éclosion Urbaine, 744 images :
1 min 30 s pour les deux livrables).
| 3 — assets IA gelés | 8718f1d (arbre propre) | **aucune régression** | PASS en hausse : compilateur 147 → 158, QC 91 → 108, nouveau `verify_step30_assets` (44 PASS), QC de la scène 26 → 28 (`scene.ai_disclosure`, `scene.ai_lock`) ; empreintes des images, du maître et octets HEVC/ProRes identiques ; porte 8/8 | [`baseline/phase-3.json`](baseline/phase-3.json) |
