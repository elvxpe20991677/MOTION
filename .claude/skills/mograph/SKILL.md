---
name: mograph
description: Écrire, valider, prévisualiser et rendre une vidéo de motion design avec le pipeline déterministe MOTION (mograph) — scène JSON, presets, animations calées sur la musique ou la voix off, rendu contrôlé (déterminisme, QC). À utiliser dès qu'on parle de créer ou modifier une scène, une vidéo, un titre animé, un reel, une préversion ou un rendu mograph.
when_to_use: Création ou correction d'une scène dans scenes/, idée de vidéo à transformer en scène (mograph brief), suivi d'un rendu, relecture des planches contact.
argument-hint: "[idée ou scène]"
---

# mograph — motion design déterministe

Une vidéo = UNE scène JSON (`scenes/<id>.json`, schéma `schema/scene.schema.json`) compilée puis rendue
sans aucun appel d'IA. Référence complète : [docs/GUIDE_SCENES.md](../../../docs/GUIDE_SCENES.md) (règles,
recettes) et [docs/CONTRACT.md](../../../docs/CONTRACT.md) (normatif).

## Démarche
1. **Partir d'une idée** : `python mograph.py brief "<idée>" [--preset flat_riso] [--aspect 16:9]` écrit une
   scène VALIDE dans `scenes/` (Claude par `claude -p`, abonnement ; au plus 3 essais corrigés par le
   compilateur ; une fois pour toutes : `claude auth login`). Ou partir d'un modèle :
   `python mograph.py new <id> --template titre`.
2. **Valider** : `python mograph.py validate <scène>` (outil MCP `validate`). Les messages d'erreur sont
   écrits pour être appliqués tels quels : corriger, revalider, jusqu'à zéro erreur.
3. **Préversion** : `python mograph.py all <scène> --draft` (outil MCP `draft`, `confirm: true`), suivie par
   `status <scène> --draft`.
4. **Relecture** : `python mograph.py sheets <scène> --draft --vision` (planches contact + relecture
   visuelle par Claude : avertissements seulement, jamais bloquants ; `out/<scène>_draft/vision_qc.md`).
5. **Rendu final** : `python mograph.py all <scène>` (outil MCP `all`, `confirm: true`) : déterminisme vérifié,
   QC automatique (`out/<scène>/qc_report.md`).

Ne jamais lancer `all`, `draft` ni `beats --write` sans l'accord de l'utilisateur : ils remplacent des
fichiers.

## Les dix règles (docs/GUIDE_SCENES.md §2)
1. Durées entières en images : `durée × fps` entier (25 i/s : multiples de 0,04 s).
2. `at` en secondes LOCALES au plan ; musique : `"beat:4"` (exige `audio.bpm`), `"marker:drop"` ; voix off :
   `"word:3"`.
3. Pas d'easing linéaire (`none`, `linear`) sauf `"allowLinear": true`.
4. Pas deux animations de la même propriété en même temps sur un calque.
5. Couleurs : noms de la palette du preset (`bg`, `fg`, `primary`…) ou `#RRGGBB`.
6. Positions : `"50%"` du cadre, `"safe:50%"` de la zone sûre (conseillé pour tout texte), ou pixels.
7. Zones sûres : jamais de texte collé au bord ; garder ≥ 2 % de marge.
8. Glyphes : chaque caractère doit exister dans la police du preset.
9. Médias sous `assets/` (chemins relatifs à la racine) ; sorties d'IA GELÉES sous `assets/ai/`
   (`python mograph.py assets add …`) avec `provenance.ai_disclosure` dans la scène.
10. Fond transparent (`format.alpha: true`) : pas de `background` sur les plans.

## Outils MCP (serveur `mograph`, `.mcp.json`)
`validate`, `preview`, `draft`, `all`, `status`, `beats`, `sheets` : sorties JSON structurées ; une erreur
d'exécution revient avec `isError: true` et un message actionnable (scène refusée : appliquer les messages
du compilateur tels quels) ; `draft`, `all` et `beats` (écriture) exigent `confirm: true` ; un rendu est
détaché : suivre avec `status` (`draft: true` pour une préversion) ; un second rendu de la même scène est
refusé tant que le premier tourne.

## Musique et voix
- Musique d'IA : la geler (`assets add … --source manual_import`), puis `python mograph.py beats
  assets/ai/audio/<piste> --scene <scène> --write` ; tempo variable signalé : caler par `marker:`.
- Voix off : bloc `voiceover`, puis `python mograph.py voice <scène> --dry-run` (estimation, garde-fou de
  quota) avant toute génération.
