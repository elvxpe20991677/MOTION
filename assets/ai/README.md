# assets/ai/ — sorties d'IA gelées

Chaque fichier produit par une IA (clip vidéo, image, musique, voix, mesh, matte, texte) est un
**asset gelé** : généré une seule fois, rangé ici, identifié par son SHA-256 et sa provenance dans
`assets.lock.json` (à la racine du projet). Le rendu n'appelle jamais une IA ; il **échoue** si un
asset de ce dossier UTILISÉ par la scène n'est pas dans le verrou, est absent ou a changé
(`assets verify` signale aussi les fichiers présents mais non gelés). Un asset IA doit être un vrai
fichier de ce dossier : un lien ou une jonction qui en sort est refusé.

```bash
# geler un clip téléchargé depuis Gemini/Flow (copié ici sous video/)
python mograph.py assets add ~/Téléchargements/foret.mp4 --tool "Gemini/Flow" --model "veo-3" \
  --prompt "travelling lent dans une forêt brumeuse" --license "conditions Google Flow" --cost "20 crédits"
python mograph.py assets verify     # présents, empreintes identiques, fichiers non gelés signalés
python mograph.py assets list       # provenance
```

Dans une scène : calque `{"id": "plan", "type": "ai_clip", "asset": "assets/ai/video/foret.mp4", "w": 1920, "h": 1080}`
et déclaration `provenance.ai_disclosure` (voir README §3).

**Les médias de ce dossier ne sont pas versionnés** (`.gitignore`) : le dépôt est public et les
conditions des services peuvent interdire leur redistribution. Seul `assets.lock.json` l'est. Sur une
autre machine, replacez les fichiers d'origine : `assets verify` dit lesquels manquent.
