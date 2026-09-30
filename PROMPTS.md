# Préparer un prompt pour mograph

Une fiche à remplir **avant chaque demande** envoyée à Claude (ou à un autre assistant) pour que la
demande soit comprise du premier coup. Copiez le modèle qui correspond, remplissez les champs, supprimez
les lignes inutiles, puis envoyez.

Règle d'or : **un prompt = un objectif**. Dire *quoi* (le résultat voulu), *où* (quel fichier, quel
plan), *combien* (durées, tailles, nombres) et *comment vérifier* (quelle commande doit passer).

---

## 0. En-tête commun (à mettre en haut de chaque prompt)

```text
Projet : mograph (dépôt courant).
Objectif : <une phrase : ce que je veux obtenir>
Fichier(s) : <scenes/xxx.json, pipeline/xxx.py… ou « nouveau fichier »>
Contraintes : déterministe, sans interface graphique, versions figées, messages d'erreur en français.
Vérification attendue : <ex. « python mograph.py validate scenes/xxx.json passe sans erreur »>
Ne pas : <ce qu'il ne faut PAS toucher, ex. « ne modifie pas les presets », « ne commite pas »>
```

---

## 1. Créer une nouvelle vidéo (scène)

```text
[En-tête commun]
Type : nouvelle scène
Id de la scène : <minuscules_chiffres_tirets, ex. pub_montre_15s>
Brief : <produit / message / public, 2 à 4 phrases>
Durée totale : <ex. 15 s>   Nombre de plans : <ex. 4>
Format : aspect <16:9 | 9:16 | 1:1>, résolution <1080p | 4k>, fps <24 | 25 | 30 | 50 | 60>
Fond transparent : <non | oui (livrable prores_4444 obligatoire)>
Preset (style) : <flat_riso | kinetic_signal | vhs_glitch | handmade_chalk | clay_3d | photoreal_3d>
Partir d'un modèle ? : <non | titre | citation | chiffres | lower_third | produit_3d | reel_9x16>
Textes à l'écran (mot pour mot) :
  - plan 1 : « … »
  - plan 2 : « … »
Musique : <aucune | assets/audio/xxx.wav (bpm connu ? oui/non)>
Médias fournis : <assets/images/…, assets/video/…, assets/subs/…>
Livrables : <hevc_main10 | prores_422hq | prores_4444>
Vérification : validate puis render --draft.
```

Exemple :
```text
Objectif : pub de 15 s pour une montre, style photoréaliste.
Type : nouvelle scène — id pub_montre_15s — 16:9, 1080p, 25 i/s — preset photoreal_3d.
Plans : 1) gros plan cadran 4 s  2) rotation produit 5 s  3) 3 arguments 4 s  4) logo 2 s.
Textes : « Le temps, redessiné. » / « Saphir » « Titane » « 10 ATM » / « AURA ».
Livrable : hevc_main10. Vérification : validate + render --draft. Ne commite pas.
```

---

## 2. Ajouter un modèle 3D téléchargé (external_3d)

```text
[En-tête commun]
Type : ajouter un modèle 3D externe
Scène / plan / plaque : <scenes/xxx.json → plan « id » → plaque « id »>
Source : <Poly Haven, identifiant exact : xxx_01 | Poly Haven, à chercher : « un fauteuil en cuir »
          | lien DIRECT .glb/.gltf/.zip : https://… (sha256 : … ou « calcule-le »)>
Résolution des textures : <1k (essais) | 2k (défaut) | 4k (final)>
Taille réelle : <ex. 1,2 m>  mesurée sur : <la plus grande dimension | la hauteur | la largeur | la profondeur
          | garder l'échelle du fichier>
Point de pose : <posé au sol (défaut) | centré | origine du fichier>
Position : <x, y, z en mètres, ou « à gauche du produit », « au centre »>
Rotation : <degrés X, Y, Z>
Matériau : <celui du fichier (défaut) | matériau du preset « mint » | couleur #RRGGBB>
Animation : <aucune | ex. « tourne de 0 à 90° sur Z entre 0 et 2 s »>
Vérification : validate (le modèle doit être téléchargé et la tâche Blender conforme).
```

Correspondance avec les champs de la scène (pour vérifier ce que l'assistant a écrit) :

| Vous dites | Champ |
|---|---|
| « taille réelle 1,2 m sur la plus grande dimension » | `"size": 1.2, "fit": "max"` |
| « 1,8 m de haut » | `"size": 1.8, "fit": "height"` |
| « garder l'échelle du fichier » | `"fit": "none"` (+ `"origin": "native"` pour garder aussi son origine) |
| « posé au sol » / « centré » | `"origin": "bottom"` / `"origin": "center"` |
| « deux fois plus grand » | `"scale": 2` (multiplie `size`) |
| « textures légères » | `"resolution": "1k"` |
| « fige le fichier » | `"sha256": "<64 caractères>"` (liens directs uniquement) |
| « le .zip contient plusieurs modèles, prends celui-ci » | `"entry": "dossier/modele.glb"` |

Exemple :
```text
Objectif : poser une table en bois derrière le produit.
Scène : scenes/essai_02_3d_produit.json → premier plan → première plaque.
Source : Poly Haven wooden_table_02, textures 2k.
Taille : 1,2 m sur la plus grande dimension, posée au sol, position [0, 1.5, 0], rotation [0, 0, 15].
Matériau du fichier. Pas d'animation. Vérification : validate. Ne commite pas.
```

---

## 3. Modifier une scène existante

```text
[En-tête commun]
Type : modification
Scène : scenes/<xxx>.json
Ce qui change (liste précise) :
  - plan « <id> » : <ex. durée 3 s → 4 s>
  - calque « <id> » : <ex. texte « … » → « … », couleur accent>
Ce qui NE doit PAS changer : <ex. timing des autres plans, musique, preset>
Vérification : validate puis render --draft (ou « montre-moi seulement le diff »).
```

---

## 4. Lancer ou reprendre un rendu

```text
[En-tête commun]
Type : rendu
Scène : scenes/<xxx>.json
Étape : <validate | validate --layout | render --draft | all --draft | all | plates | encode | qc>
Détaché : <oui (--detach, survit au terminal) | non>
Hors ligne : <non | oui (MOGRAPH_OFFLINE=1 : modèles 3D déjà en cache seulement)>
Préchargement des modèles avant le rendu : <oui | non>
Rapporte : durée, fichiers produits (out/<scène>/), résultat du QC.
```

---

## 5. Signaler une erreur

```text
[En-tête commun]
Type : erreur
Commande lancée (exacte) : <ex. python mograph.py all scenes/xxx.json>
Message d'erreur (copié tel quel, en entier) :
<coller ici>
Ce que j'attendais : <…>
Ce qui a changé depuis la dernière fois que ça marchait : <fichier modifié, nouveau modèle, rien>
Demande : <trouve la cause et corrige | explique seulement, ne touche à rien>
```

---

## 6. Demander une nouvelle fonctionnalité dans le code

```text
[En-tête commun]
Type : fonctionnalité
Besoin : <ce que je veux pouvoir écrire dans une scène, avec un exemple JSON>
Fichiers probablement concernés : <pipeline/scene.py, backends/blender_backend.py, schema/…>
Compatibilité : les scènes existantes doivent donner exactement le même rendu.
Tests : ajouter un tests/verify_xxx.py et relancer les tests touchés.
Livraison : <proposer d'abord un plan | coder directement> ; <commiter | ne pas commiter>
```

---

## 7. À dire / à ne pas dire

**À dire**
- Les chiffres exacts : durées en secondes, tailles en mètres, positions, fps.
- Les textes à l'écran **mot pour mot**, entre guillemets.
- Le fichier et l'identifiant du plan / de la plaque / du calque visés.
- L'identifiant Poly Haven exact (sensible à la casse : `ArmChair_01`) ou « cherche-moi 3 choix ».
- La commande qui doit passer à la fin (`validate`, `render --draft`…).
- Ce qu'il ne faut pas toucher, et s'il faut commiter ou non.

**À ne pas dire / ne pas faire**
- ❌ « Fais un truc stylé » sans preset, durée ni format : réponse au hasard.
- ❌ Un lien vers une **page** (Sketchfab, TurboSquid, page produit) : seul un lien de téléchargement
  direct `.glb` / `.gltf` / `.zip` fonctionne.
- ❌ « position » : le champ s'appelle `location`.
- ❌ Compter sur l'animation incluse dans un modèle téléchargé : elle est retirée, décrivez l'animation.
- ❌ « À l'échelle d'origine » en pensant à `scale: 1` : par défaut la taille est ramenée à 1 m ;
  dites « garde l'échelle du fichier ».
- ❌ Des durées impossibles : durée × fps doit être entier (à 25 i/s : multiples de 0,04 s).
- ❌ Plusieurs objectifs sans rapport dans un seul prompt (nouvelle scène + correction de bug + rendu 4K).
- ❌ Paraphraser un message d'erreur : collez-le **tel quel**.
- ❌ Modifier à la main les fichiers de `.cache/models/` : Blender refusera (empreinte différente).
  Pour retélécharger, supprimez le dossier du modèle entre deux rendus.
- ❌ Des modèles sous licence inconnue pour un usage commercial : seul Poly Haven est garanti CC0.

---

## 8. Aide-mémoire des valeurs possibles

| Réglage | Valeurs |
|---|---|
| Presets | `flat_riso`, `kinetic_signal`, `vhs_glitch`, `handmade_chalk`, `clay_3d`, `photoreal_3d` |
| Modèles de départ | `titre`, `citation`, `chiffres`, `lower_third`, `produit_3d`, `reel_9x16` |
| Aspect / résolution / fps | `16:9`, `9:16`, `1:1` / `1080p`, `4k` / `24`, `25`, `30`, `50`, `60` |
| Livrables | `hevc_main10`, `prores_422hq`, `prores_4444` (alpha) |
| Primitives 3D | `sphere`, `cube`, `rounded_cube`, `torus`, `cylinder`, `cone`, `plane`, `cyclorama` |
| Modèle externe | `provider` : `polyhaven`, `url` ; `fit` : `max`, `width`, `depth`, `height`, `none` ; `origin` : `bottom`, `center`, `native` ; `resolution` : `1k`, `2k`, `4k`, `8k` |
| Transitions | `cut`, `crossfade`, `wipe`, `push` |

Commandes utiles :
```text
python mograph.py new ma_video --template titre        # nouvelle scène depuis un modèle
python mograph.py validate scenes/ma_video.json        # valider (télécharge les modèles 3D)
python mograph.py preview ma_video                     # aperçu en direct
python mograph.py all ma_video --draft                 # préversion rapide
python mograph.py all ma_video --detach                # rendu complet détaché
python -m pipeline.asset_manager search chair          # chercher un modèle Poly Haven
python -m pipeline.asset_manager polyhaven wooden_table_02 --resolution 2k   # précharger
```

Voir aussi : `docs/GUIDE_SCENES.md` (règles d'écriture des scènes) et `schema/scene.schema.json`
(format complet).
