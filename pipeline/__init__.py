"""Pipeline mograph : scène JSON -> images déterministes -> HEVC / ProRes -> QC.

Modules :
    config          constantes qui influencent la sortie (source unique de vérité)
    scene           validation + compilation d'une scène en plans et tâches Blender
    web_render      pilote Chromium/Playwright (capture image par image, manifeste, déterminisme)
    blender_render  lanceur du backend bpy (Python 3.11) pour les plaques 3D
    compose         séquence maître (liens physiques + fondus en alpha prémultiplié)
    encode          FFmpeg : HEVC Main10 hvc1, ProRes 422 HQ / 4444
    audio           piste de test déterministe (aevalsrc), mesures
    qc              contrôle qualité automatique (ffprobe + contenu + zones sûres + déterminisme)
"""

__version__ = "1.0.0"
