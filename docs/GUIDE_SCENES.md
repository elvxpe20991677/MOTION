# Écrire une scène mograph (guide pratique)

Le format complet est dans `schema/scene.schema.json` (VS Code l'utilise pour l'autocomplétion) et le
README §3. Ce guide donne les règles qui font qu'une scène passe `validate` du premier coup, puis un
prompt prêt à coller dans un assistant IA pour qu'il écrive des scènes à votre place.

## 1. Démarrer
```
python mograph.py new --list                       # modèles disponibles
python mograph.py new ma_video --template titre    # scenes/ma_video.json
python mograph.py preview ma_video                 # aperçu en direct, rechargé à chaque sauvegarde
```

## 2. Les dix règles qui évitent 90 % des refus
1. **Durées entières en images** : `durée × fps` entier. À 25 i/s : multiples de 0,04 s ; à 24 i/s :
   de 1/24 s (0,5 · 1 · 1,5 … sûrs) ; à 30 i/s : de 1/30 s. Même règle pour `transition_in.dur`.
2. **Temps d'une animation** (`at`) : secondes **locales au plan**. Pour la musique : `"beat:4"`,
   `"marker:drop"`, avec décalage `"beat:4-0.1"` (temps **global**, exige `audio.bpm`).
3. **Pas d'easing linéaire** (`none`, `linear`) sauf `"allowLinear": true` (défilement continu).
4. **Pas deux animations de la même propriété en même temps** sur un calque (le message propose
   l'heure où décaler).
5. **Couleurs** : nom de la palette du preset (`bg`, `fg`, `primary`, `secondary`, `accent`…) ou
   `#RRGGBB`. Couleurs 3D : `#RRGGBB` sans alpha.
6. **Positions** : `"50%"` du cadre, `"safe:50%"` de la zone sûre (conseillé pour tout texte), ou
   pixels logiques (1920×1080, 1080×1920, 1080×1080, même en 4K).
7. **Zones sûres** : ne jamais coller un texte au bord (`safe:0%`) ; garder ≥ 2 % de marge.
   `validate --layout` le vérifie en quelques secondes.
8. **Glyphes** : chaque caractère doit exister dans la police du preset (aucune police de repli) ;
   l'erreur nomme le caractère fautif.
9. **Médias sous `assets/`** : images, vidéos, sous-titres, audio ; chemins relatifs à la racine.
10. **Fond transparent** (`format.alpha: true`) : pas de `background` sur les plans, livrable
    `prores_4444` obligatoire (ajouter un HEVC `"suffix": "_flat"` pour un aperçu aplati).

## 3. Recettes
- **Un mot par temps musical** : `{"move": "slam", "at": "beat:3", "sync": "end"}` — la FIN de la
  première étape tombe pile sur le temps (l'impact est entendu et vu ensemble).
- **Enchaîner deux plans** : `"transition_in": {"type": "push", "dur": 0.4, "direction": "left"}`.
- **Chiffres qui poussent** : calque `chart` + `grow {at, dur, stagger, ease}` (pas de back/elastic :
  une barre ne dépasse jamais sa valeur).
- **Vidéo de fond** : `{"type": "video", "src": "assets/video/plan.mp4", "w": 1920, "h": 1080,
  "fit": "cover", "start": 2.0, "z": -1}` puis textes au-dessus.
- **Sous-titres** : `{"id": "st", "type": "subtitles", "src": "assets/subs/voix.srt"}` dans le plan
  (temps SRT = temps global de la vidéo ; `offset` pour recaler).
- **Musique** : `python mograph.py beats assets/audio/musique.wav --scene ma_video --write` écrit
  `bpm`, `offset` et le marqueur `drop` ; `"normalize": true` règle le volume.
- **Déclinaisons** : `{{colonne}}` dans le modèle + `batch modele.json --data lignes.csv`.

## 4. Prompt pour un assistant IA
Copier le bloc ci-dessous, puis ajouter votre brief. Joindre `schema/scene.schema.json` et le preset
choisi (`presets/<id>.json`) si l'assistant accepte des fichiers. Valider ensuite avec
`python mograph.py validate <fichier>` et renvoyer les messages d'erreur tels quels à l'assistant :
ils sont écrits pour être corrigés sans autre explication.

```text
Tu es directeur artistique motion design. Écris UNE scène JSON pour le moteur « mograph »
(schéma draft 2020-12 : schema/scene.schema.json). Contraintes STRICTES :
- racine : version "1.0", id (minuscules, chiffres, « - » ou « _ », jamais « __ »), title, brief,
  seed, preset (flat_riso | kinetic_signal | vhs_glitch | handmade_chalk | clay_3d | photoreal_3d),
  format {aspect 16:9|9:16|1:1, resolution 1080p|4k, fps 24|25|30|50|60, alpha}, outputs, qc, shots ;
- durée × fps ENTIER pour chaque plan et chaque transition (ex. 25 i/s -> multiples de 0,04 s) ;
- « at » en secondes LOCALES au plan ; « beat:N » seulement si audio.bpm est défini ;
- aucun easing linéaire ; pas deux tweens de la même propriété qui se chevauchent sur un calque ;
- couleurs = noms de la palette du preset ou #RRGGBB ; mouvements (« move ») = ceux du preset ;
- textes positionnés en « safe:N% » avec au moins 2 % de marge au bord ; tailles lisibles
  (titre ≥ 90 px, corps ≥ 36 px en 1080p) ; pas plus de 7 mots à l'écran à la fois ;
- transitions : cut | crossfade | wipe | push (objet {type, dur, direction}) ;
- storyboard : une intention par plan.
Réponds UNIQUEMENT par le JSON, sans commentaire.
Brief : <votre brief ici>
```
