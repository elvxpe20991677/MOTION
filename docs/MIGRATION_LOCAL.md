# Rapatrier mograph en local (Windows 11) et le rendre 100 % opérationnel

Machine cible : Windows 11, AMD Ryzen (8 cœurs / 16 threads), RTX 4060 Laptop 140 W, 64 Go DDR5.
Dossier cible : `C:\Users\perro\Desktop\MOTION`. Dépôt : `https://github.com/elvxpe20991677/MOTION`.

---------------------------------------------------------------------------------------------------
## 1. Logiciels à installer (tous)

Commandes à lancer dans **PowerShell** (winget est intégré à Windows 11). Redémarrer PowerShell
après les installations pour que le PATH soit à jour.

| # | Logiciel | Rôle | Obligatoire | Installation |
|---|---|---|---|---|
| 1 | Mises à jour Windows 11 | pilotes système, sécurité | oui | Paramètres → Windows Update |
| 2 | Pilote NVIDIA **Studio** récent (≥ 550) | OptiX (plaques 3D sur la RTX 4060) | oui | NVIDIA App ou nvidia.com (pilote Studio) |
| 3 | Microsoft Visual C++ Redistributable 2015-2022 x64 | requis par bpy et OpenCV | oui | `winget install Microsoft.VCRedist.2015+.x64` |
| 4 | Git for Windows | récupérer le dépôt | oui | `winget install Git.Git` |
| 5 | Python **3.12** (python.org, avec le lanceur `py`) | orchestrateur | oui | `winget install Python.Python.3.12` |
| 6 | Node.js **22 LTS** | bibliothèques du navigateur (GSAP, polices) | oui | `winget install OpenJS.NodeJS.LTS` |
| 7 | uv (Astral) | fournit Python 3.11 pour Blender (bpy) | oui | `winget install astral-sh.uv` |
| 8 | FFmpeg « full » (gyan.dev, avec zscale) | encodage, QC | oui | `winget install Gyan.FFmpeg` |
| 9 | Chromium (Playwright) | rendu des images | oui | installé par `setup.ps1` (pas à la main) |
| 10 | bpy 5.0.1 (Blender en module) | rendu 3D Cycles | oui | installé par `setup.ps1` |
| 11 | Claude Code | exécuter le prompt ci-dessous | pour le prompt | `npm install -g @anthropic-ai/claude-code` |
| 12 | VS Code | écrire les scènes (autocomplétion par le schéma) | conseillé | `winget install Microsoft.VisualStudioCode` |
| 13 | VLC (ou mpv) | lecture image par image | conseillé | `winget install VideoLAN.VLC` |
| 14 | MediaInfo | inspection des fichiers livrés | conseillé | `winget install MediaArea.MediaInfo.GUI` |
| 15 | DaVinci Resolve Studio 21 | scopes, contrôle broadcast, finition | conseillé | blackmagicdesign.com (déjà installé) |
| 16 | Blender 5.0 (application) | inspecter une plaque 3D | facultatif | `winget install BlenderFoundation.Blender` |
| 17 | Windows Terminal | confort (UTF-8, onglets) | facultatif | intégré à Windows 11 |

Réglages Windows (une fois) :
- **Chemins longs** (node_modules) : PowerShell en administrateur →
  `New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force`
  puis `git config --global core.longpaths true`.
- **Alimentation** : sur secteur, mode « Performances optimales », veille désactivée sur secteur.
- **GPU** : carte dédiée active (MUX / « GPU dédié » dans l'utilitaire du fabricant) pour disposer des 140 W.
- **Antivirus** : exclure `build\` (fait par `setup.ps1 -Defender` en administrateur).

---------------------------------------------------------------------------------------------------
## 2. Prompt à coller dans Claude Code (sur le PC, dans PowerShell)

Ouvrir PowerShell dans `C:\Users\perro\Desktop`, lancer `claude`, puis coller :

```text
Tu es sur mon PC Windows 11 (PowerShell). Objectif : rapatrier le projet « mograph » depuis GitHub dans
C:\Users\perro\Desktop\MOTION, le rendre 100 % opérationnel, puis générer les vidéos d'essai une par une.
Dépôt : https://github.com/elvxpe20991677/MOTION . Branche : main si la PR #1 est fusionnée, sinon
claude/eloquent-hypatia-p581pj. Travaille étape par étape ; après chaque étape, montre la sortie réelle ;
ne passe à la suivante que si elle est verte. N'affirme jamais qu'une chose marche sans l'avoir exécutée.
Ne modifie aucune version figée (requirements.txt, package-lock.json, bpy==5.0.1). En cas d'échec :
arrête-toi, montre le journal, propose une correction, attends mon accord.

1. SAUVEGARDE : si C:\Users\perro\Desktop\MOTION existe, copie-le (robocopy, en excluant .venv,
   .venv-blender, node_modules, build et les vidéos .mov/.mp4) vers C:\Users\perro\Desktop\MOTION_sauvegarde_<date>.
2. SYNCHRONISATION (les fichiers en double sont ÉCRASÉS par la version GitHub) :
   - si le dossier est un dépôt git : git remote set-url origin <dépôt> (ou add), git fetch origin,
     git checkout -B <branche> origin/<branche>, git reset --hard origin/<branche> ;
   - sinon : git init, git remote add origin <dépôt>, git fetch origin, git checkout -f -B <branche> origin/<branche> ;
   - puis git status --short : liste-moi les fichiers locaux non suivis restants SANS les supprimer.
   - git config core.longpaths true.
3. PRÉREQUIS : vérifie git, py -3.12, node (≥ 22), npm, uv, ffmpeg (filtres zscale, premultiply,
   extractplanes, mergeplanes, signalstats ; encodeurs libx265, prores_ks), nvidia-smi. Pour chaque
   manquant, donne-moi la commande winget exacte (docs\MIGRATION_LOCAL.md §1) et attends.
4. INSTALLATION : powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Tests
   (ajoute -Defender si PowerShell est en administrateur).
5. TESTS COMPLETS, un par un, avec leur code de sortie : tests\verify_step09_presets.py, 11, 12, 13, 14,
   15, 16, 17, puis tests\verify_features.py (via .\.venv\Scripts\python.exe). À l'étape 13, vérifie que
   la ligne « MOGRAPH_BLENDER périphérique » indique OPTIX (sinon : pilote NVIDIA / GPU dédié).
6. PORTE DE DÉTERMINISME sur cette machine : .\.venv\Scripts\python.exe mograph.py all demo_flat_riso ,
   puis mograph.py determinism <démo> --count 16 pour demo_flat_riso, demo_vhs_kinetic,
   test_lowerthird_4k_alpha, demo_clay_3d. Exige 16/16 partout.
7. RÉGLAGE : mesure « mograph.py render demo_flat_riso --workers N » pour N = 4, 6, 8 (après avoir supprimé
   build\demo_flat_riso\shots entre deux essais), garde le plus rapide et fixe-le :
   [Environment]::SetEnvironmentVariable("MOGRAPH_MAX_WORKERS", "<N>", "User").
8. VIDÉOS D'ESSAI, une par une : powershell -ExecutionPolicy Bypass -File .\scripts\render_essais.ps1
   (ordre : 2D, Reel, VHS, craie, vidéo+sous-titres, déclinaisons CSV, bandeau 4K alpha, mixte, 3D).
   Pour chacune : code de sortie, durée, verdict de out\<essai>\qc_report.md.
9. RAPPORT FINAL : tableau essai / durée / verdict QC / avertissements, temps par image mesurés (web,
   plaque 3D OptiX), et liste des vérifications humaines restantes (docs\MIGRATION_LOCAL.md §4).
```

---------------------------------------------------------------------------------------------------
## 3. Les vidéos d'essai (scènes dans `scenes/`)

| # | Scène | Ce qu'elle teste | Format | Durée | Temps estimé (votre PC) |
|---|---|---|---|---|---|
| 1 | `essai_01_2d_explainer` | **2D seule** : titre lettre à lettre, formes, tracé, graphique à compteurs, transitions volet + poussée | 16:9 1080p 25 | 8,8 s | ~1 min |
| 2 | `essai_02_3d_produit` | **3D seule** : photoréaliste (métal, céramique, ciel physique, f/2.8), travelling | 16:9 1080p 24 | 3 s | ~10-15 min (OptiX, 512 éch.) |
| 3 | `essai_03_mixte` | **mixte** : plaque clay (chute, écrasement, rebond) + titre composité, puis carte 2D en fondu | 1:1 1080p 24 | 4,5 s | ~3-5 min |
| 4 | `essai_04_reel_musique` | Reel 9:16, un mot par temps (sync end), flou de mouvement 8, piste test | 9:16 1080p 30 | 4,5 s | ~2 min |
| 5 | `essai_05_vhs_glitch` | textures VHS, glitch sur les temps + fenêtre, texte tapé | 16:9 1080p 25 | 4 s | ~1 min |
| 6 | `essai_06_craie` | fait main : on twos, bouillonnement, tracés, écriture | 1:1 1080p 24 | 4 s | ~1 min |
| 7 | `essai_07_bandeau_4k_alpha` | 4K transparent, flou 8 sous-images, ProRes 4444 + HEVC aplati | 16:9 4K 25 | 2,4 s | ~3-5 min |
| 8 | `essai_08_video_soustitres` | calque vidéo (mire fournie) + sous-titres SRT + cartouche | 16:9 1080p 25 | 6 s | ~1 min |
| 9 | `essai_09_declinaisons` | lot CSV : 3 cartes personnalisées (texte + graphique) | 1:1 1080p 25 | 3 × 2,8 s | ~2 min |

Commandes utiles :
- une seule vidéo : `.\.venv\Scripts\python.exe mograph.py all essai_03_mixte`
- aperçu en direct pendant qu'on écrit : `mograph.py preview essai_01_2d_explainer`
- préversion rapide : `mograph.py all essai_02_3d_produit --draft`
- sélection : `.\scripts\render_essais.ps1 -Only 01,07` ; préversions : `-Draft`
- rendu long sans garder le terminal : `mograph.py all essai_02_3d_produit --detach` puis `mograph.py status essai_02_3d_produit`

---------------------------------------------------------------------------------------------------
## 4. Check-list ordonnée pour un système 100 % correct

### Avant de générer
1. **Fusionner la PR #1** sur GitHub (sinon travailler sur la branche `claude/eloquent-hypatia-p581pj`).
2. **Sauvegarder** l'ancien dossier local (étape 1 du prompt).
3. **Synchroniser** le dépôt en écrasant les anciens fichiers (étape 2).
4. **Installer** tous les logiciels obligatoires (§1), dont le **pilote NVIDIA Studio** et le **VC++ Redistributable**.
5. **Régler Windows** : chemins longs, secteur + performances, veille désactivée, GPU dédié, exclusion Defender de `build\`.
6. **Espace disque** : ≥ 100 Go libres ; sinon `MOGRAPH_BUILD_DIR` / `MOGRAPH_OUT_DIR` vers un autre SSD.
7. **`setup.ps1 -Tests`** : tout doit être vert.
8. **Tous les tests** (étapes 9 à 17 + `verify_features.py`) ; étape 13 sur **OPTIX** ; étape 15 confirme
   la chaîne alpha exacte avec le FFmpeg 8 de Windows.
9. **Porte de déterminisme sur CE PC** (16/16 sur les 4 démos) : le déterminisme est garanti par machine.
10. **Régler `MOGRAPH_MAX_WORKERS`** (4, 6 ou 8 : garder le plus rapide).

### Pendant
11. Générer les essais **un par un** (`render_essais.ps1`), les longs en `--detach`.
12. Ne pas mettre le PC en veille, ne pas lancer de mise à jour (pilote, Windows) pendant un rendu.

### Après chaque vidéo
13. `out\<essai>\qc_report.md` : **CONFORME** ; lire les avertissements (niveaux : informatifs).
14. **Planches contact** `planche_contact.png` (et `planche_alpha.png` pour l'essai 7) : coupes, transitions, impacts.
15. **Lecture image par image** (VLC : touche E ; mpv : touche .) aux coupes, fondus, volets et impacts musicaux.
16. **DaVinci Resolve** : scopes (luminance 64-940 en 10 bits, pas de dépassement franc), interprétation
    Rec.709, alpha du ProRes 4444 incrusté sur fond clair ET sombre, loudness −14 LUFS (réseaux).
17. Cocher **`QC_CHECKLIST.md`**.

### Ensuite, en exploitation
18. Archiver : commit de la scène, du preset et de `out\<scène>\versions.txt` (+ `qc_report.md`).
19. **Refaire la porte de déterminisme** après toute mise à jour : pilote NVIDIA, FFmpeg, Playwright/Chromium, bpy.
20. Pour une nouvelle vidéo : `new` → `preview` → `validate --layout` → `all --draft` → `all` → QC + planches → Resolve.
