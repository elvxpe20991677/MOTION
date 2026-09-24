# Checklist QC humaine

Le QC automatique (`python mograph.py qc <scène>`, rapport `out/<scène>/qc_report.md`) vérifie ce
qu'une machine peut mesurer. Cette liste couvre ce qu'un œil et une oreille doivent valider avant
livraison. Cocher chaque case pour chaque livrable ; une case non cochée bloque la livraison.

## 0. Conditions de contrôle
- [ ] Moniteur calibré Rec.709 : gamma 2.4, blanc D65, 100 cd/m², pièce en lumière tamisée.
- [ ] Lecture image par image possible (DaVinci Resolve, mpv `.`/`,`, ou VLC `e`).
- [ ] `out/<scène>/qc_report.md` indique **CONFORME** et le déterminisme est à 100 %.
- [ ] `out/<scène>/versions.txt` et `out/<scène>/encode_commands.sh` sont présents.

## 1. Image par image (points critiques)
- [ ] **Coupes** : l'image avant et l'image après la coupe sont propres (pas d'image vide, pas
      d'image dupliquée, pas de saut de texture).
- [ ] **Fondus** : chaque image du fondu contient les deux plans ; progression régulière (smoothstep),
      sans « flash » de fond ni assombrissement anormal au milieu du fondu.
- [ ] **Impacts** : l'image d'impact (fin de la 1re étape d'un `slam`, contact de la balle 3D)
      tombe exactement sur le temps musical (comparer au pic de la forme d'onde).
- [ ] Première et dernière image : pas de résidu d'animation inattendu.
- [ ] Animations « on twos » (preset handmade_chalk) : chaque pose tenue 2 images exactement.

## 2. Typographie et graphisme
- [ ] Aucune police de repli (glyphes, accents, « € », apostrophes typographiques dans la bonne police).
- [ ] Pas de mot coupé sur deux lignes sans intention ; interlettrage et interlignage conformes au preset.
- [ ] Textes à l'intérieur de la zone sûre titre (overlay de zone sûre activé dans Resolve) ;
      en 9:16, rien de lisible sous l'interface (bas 22 %, droite 12 %).
- [ ] Graphiques : les barres ne dépassent jamais leur valeur finale ; compteur et barre arrivent
      ensemble ; valeurs et libellés lisibles.
- [ ] Grain, papier, balayage : présents mais discrets, pas de motif répétitif visible (tuilage).

## 3. Couleur et niveaux (DaVinci Resolve)
- [ ] Projet en Rec.709 Gamma 2.4 ; clip interprété en **Rec.709** (pas de décalage de teinte à l'import).
- [ ] Forme d'onde (Waveform) en 10 bits : noirs à **64**, blancs à **940**, rien en dessous de 64
      ni au-dessus de 940 (plage « vidéo » / limited).
- [ ] Vecteurscope : couleurs saturées dans les limites, pas de dérive de teinte des tons chair ou neutres.
- [ ] Pas de banding visible dans les dégradés et les vignettes (10 bits en sortie).

## 4. Alpha (ProRes 4444)
- [ ] Incrusté sur un fond **clair** (blanc ou gris 80 %) : pas de liseré sombre autour des bords.
- [ ] Incrusté sur un fond **sombre** (noir ou gris 20 %) : pas de halo clair autour des bords.
- [ ] Interprétation de l'alpha dans Resolve : **droit (straight)** ; les bords flous de mouvement
      restent doux sur les deux fonds.
- [ ] La version HEVC « aplatie sur noir » correspond visuellement au 4444 incrusté sur noir.

## 5. Son
- [ ] Synchronisation image/son vérifiée sur au moins trois impacts.
- [ ] Pas de clic en début ou fin de fichier ; fin de piste calée sur la dernière image.
- [ ] Niveau conforme à la cible (réseaux −14 LUFS, diffusion −23 LUFS) : confirmé par le rapport QC.

## 6. Fichiers
- [ ] `mediainfo` (ou l'inspecteur de Resolve) : HEVC Main 10 `hvc1` / ProRes 422 HQ / ProRes 4444,
      bt709 / bt709 / bt709, plage limitée, fps exact, nombre d'images exact, timecode 01:00:00:00 en ProRes.
- [ ] Lecture fluide dans le lecteur de la plateforme cible (QuickTime / navigateur / téléphone pour le 9:16).
- [ ] Nom de fichier conforme : `<scène><suffixe>_<profil>.mp4|.mov`.

## Validation
| Scène | Livrables | Contrôleur | Date | Visa |
|---|---|---|---|---|
| demo_flat_riso | HEVC + ProRes 422 HQ | | | |
| demo_vhs_kinetic | HEVC + ProRes 422 HQ | | | |
| demo_clay_3d | HEVC + ProRes 422 HQ | | | |
| test_lowerthird_4k_alpha | ProRes 4444 + HEVC aplati | | | |
