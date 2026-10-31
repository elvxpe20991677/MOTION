# Phase 8 (expérimentale, optionnelle) — évaluation des fournisseurs locaux gratuits — 31/10/2026

**Décision : non implémentée.** La feuille de route demande d'abandonner et de le dire si un modèle
dépasse 8 Go de VRAM ou sort des licences autorisées, et de ne faire les phases 2, 8 et 9 « que si
utiles ». Rien n'a été téléchargé ni installé.

## Vérifications aux sources officielles

| Modèle | Licence (source) | VRAM annoncée (source) | Conclusion |
|---|---|---|---|
| **Wan 2.2 TI2V-5B** (image → vidéo) | Apache 2.0 (github.com/Wan-Video/Wan2.2, huggingface.co/Wan-AI/Wan2.2-TI2V-5B) | « au moins 24 Go de VRAM (ex. RTX 4090) » en 720p, AVEC `--offload_model True --convert_model_dtype --t5_cpu` ; aucun chiffre plus bas n'est publié ; « moins de 9 minutes » pour 5 s en 720p, sans préciser le GPU | **Abandonné** : 24 Go > 8 Go (RTX 4060 Laptop). Les « 8 Go » des guides communautaires ne sont pas confirmés par la source officielle |
| **SAM 3** (mattes) | « SAM License », licence propre à Meta (github.com/facebookresearch/sam3) | non publiée (848 M de paramètres ; CUDA ≥ 12.6, PyTorch ≥ 2.7, Python ≥ 3.12) | **Non fait** : poids **soumis à demande d'accès** sur Hugging Face (compte de l'utilisateur, acceptation d'une licence non standard) → décision de l'utilisateur |
| **TripoSR** (image → maillage) | MIT, code et poids (github.com/VAST-AI-Research/TripoSR) | « environ 6 Go de VRAM » avec les options par défaut | Faisable sur 8 Go et sous licence autorisée, mais **peu utile** : les plaques 3D de mograph sont procédurales (primitives, `plates`), et un maillage d'image unique ne s'intègre pas aux presets 3D sans travail de matériau |

## Si l'utilisateur veut aller plus loin
- **TripoSR** : faisable dans un venv séparé (`--experimental`), sortie gelée par
  `assets add … --source local --kind mesh`. Coût : PyTorch CUDA (plusieurs Go) et le téléchargement des poids
  (taille non vérifiée).
- **SAM 3** : demander l'accès sur huggingface.co/facebook/sam3 (ou sam3.1), lire la SAM License, puis
  relancer l'évaluation.
- **Vidéo générative** : rester sur l'import manuel depuis Gemini/Flow (phase 3), seule voie compatible
  avec 8 Go de VRAM et le budget.
