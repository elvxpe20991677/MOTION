# Essai Blender 5.2 LTS (phase 2, optionnelle) — 31/10/2026

**Statut : essai seulement. La référence reste bpy 5.0.1 sous CPython 3.11.15** (`.venv-blender`,
`versions.json`, `constraints-blender.txt` inchangés). Basculer change des pixels et deux versions
épinglées : décision de l'utilisateur.

## Sources vérifiées
- **Blender 5.2 LTS** : sortie le 14/07/2026, support jusqu'en juillet 2028
  (blender.org/releases/5-2/ ; code sous GPL-2.0-or-later).
- **bpy sur PyPI** (pypi.org/project/bpy) :
  - 5.2.0 à 5.2.3 ; la 5.2.3 est du 22/10/2026 ;
  - roues `cp313` uniquement (`requires_python ==3.13.*`, roue Windows x86_64 de 339 Mo) ;
  - bpy 5.0.1 exige `==3.11.*`.

## Installation de l'essai (venv séparé, rien de partagé avec la référence)
```bash
uv venv --python 3.13.13 .venv-blender52
uv pip install --python .venv-blender52/Scripts/python.exe "bpy==5.2.3"
```
Paquets relevés dans ce venv :
- bpy 5.2.3, numpy 2.5.4, zstandard 0.25.0, requests 2.34.2, cython 3.3.0 ;
- attrs 26.1.0, cattrs 26.2.1, certifi 2026.10.23, charset-normalizer 3.5.2, idna 3.20 ;
- typing-extensions 4.16.0, urllib3 2.8.0.

`MOGRAPH_BLENDER_VENV` (nouvelle variable, défaut `.venv-blender`) fait utiliser ce venv par le
pipeline, sans rien modifier d'autre.

## Mesures (machine de référence : Ryzen 24 threads, RTX 4060 Laptop, pilote 617.42)

| Contrôle | Résultat |
|---|---|
| `tests/verify_step13_blender.py` avec bpy 5.2.3 | **37 PASS, 0 FAIL** (backend compatible sans modification) |
| Reproductibilité CPU 5.2.3 (même image, deux rendus) | bit-exact |
| OptiX 5.2.3 | non bit-exact (écart max 52/65535), comme avec 5.0.1 : le contrat n'utilise que le CPU |
| Plaque `bounce__set` de `demo_clay_3d` (72 images RGBA 16 bits 1080²), 5.0.1 vs 5.2.3 | **0/72 images identiques** ; PSNR min 62,3 dB, médian 63,0 dB (échelle 16 bits) ; écart moyen 0,06 niveau 8 bits ; 0,5 % des pixels à plus d'un niveau 8 bits ; écart max 6038/65535, sur les bords des objets (anticrénelage, échantillonnage) |
| Temps de rendu de la plaque | 1,72 s/image (5.0.1) contre 1,77 s/image (5.2.3) |
| Pipeline complet `all demo_clay_3d` avec 5.2.3 | porte de déterminisme 8/8, QC **CONFORME** |

Visuellement, aucune différence n'est perceptible. Image de comparaison, avec l'écart amplifié ×16 :
`build/_essai_blender52/comparaison_image67.png`. Mesures détaillées :
`build/_essai_blender52/comparaison_demo_clay_3d.json` (fichiers non versionnés, régénérables avec le
script de comparaison décrit ci-dessus).

## Ce qu'impliquerait une bascule (non faite)
1. `versions.json` : `blender_python` 3.11.15 → 3.13.13, `bpy` 5.0.1 → 5.2.3 ; nouveau
   `constraints-blender.txt` ; `setup.ps1` / CI (venv Blender en 3.13).
2. Les empreintes des plaques 3D (et donc des maîtres et livrables des scènes 3D) changent : nouvelle
   baseline de référence, comparée avec `--allow-pixel-change`. Les scènes 2D ne sont pas touchées.
3. Intérêt : support LTS jusqu'en 2028 (5.0 n'est pas LTS). Pas de gain de vitesse mesuré, et aucune
   fonction de 5.2 n'est utilisée par le backend.

Recommandation : basculer quand une raison concrète apparaît (correctif ou fonction de 5.2 nécessaire,
fin de support de 5.0), en une fois, avec une nouvelle baseline. La compatibilité est établie.
