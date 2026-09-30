#!/usr/bin/env python3
"""
Finition « prêt à diffuser » d'un enregistrement ou d'une vidéo — finaliser.py
==================================================================================
Prend un fichier monté (son ou vidéo) et en fait un fichier prêt à publier en
podcast : début et fin propres, fond débarrassé d'un éventuel bruit, volume à
la norme. Tout en un, et d'abord ne pas nuire : chaque correction n'est
appliquée que si l'analyse la justifie, et le rapport dit ce qui a été fait,
ce qui a été laissé, et pourquoi.

Le script s'appuie sur nettoyer.py, dont les réglages ont été mesurés sur du
matériel réel (passe-haut, mélange dry/wet de DeepFilterNet, gain constant,
contrôle DNSMOS). Ce qu'il ajoute : la décision (faut-il débruiter ? niveler ?
rogner ?), la vidéo, les canaux, les fondus, et les formats de diffusion.

Passes :
  1. Canaux             → sortie MONO par défaut (--canaux stereo pour garder
                          deux canaux). Les incohérences entre gauche et droite
                          sont corrigées avant tout mélange, sans quoi le mono
                          serait pire que l'original :
                          · canal vide            → l'autre est pris seul
                          · phase inversée        → un canal est retourné (sinon
                                                    la voix s'annule en mono)
                          · décalage dans le temps → les canaux sont recalés
                                                    (sinon le mono sonne creux)
                          · niveaux inégaux       → rééquilibrés, si les deux
                                                    canaux portent bien une voix
  2. Conditionnement    → passe-haut 60 Hz, notchs sur la seule ronflette détectée
  3. Analyse et rognage → saturation, fond (niveau, régularité, nature), sonie,
                          étendue dynamique, mesurés une fois la ronflette
                          retirée ; coupe des silences de début et de fin, en
                          gardant un peu d'air avant le premier son et après
                          le dernier
  4. Débruitage         → SEULEMENT si un bruit de fond régulier est détecté :
                          DeepFilterNet3 à faible dose (8 dB, 12 dB si le fond
                          est fort), contrôlé par DNSMOS. Si la voix en souffre,
                          essai d'une soustraction spectrale douce ; si elle en
                          souffre encore, pas de débruitage du tout.
                          Un fond qui n'est pas un bruit (musique, ambiance)
                          est laissé intact.
  5. Nivelage           → SEULEMENT si l'étendue dynamique est très grande
                          (voix de niveaux très inégaux) ; réglages lents, qui
                          ne pompent pas
  6. Normalisation      → gain constant vers -16 LUFS (stéréo) ou -19 LUFS
                          (mono), plafond de crête vraie ; limiteur en dernier
                          recours seulement
  7. Fondus             → entrée et sortie, courts, jamais sur un mot
  8. Encodage           → MP3 à débit constant (le débit variable gêne la
                          navigation dans certains lecteurs de podcasts), M4A,
                          WAV ; ou MP4 pour une vidéo (image recopiée telle
                          quelle quand rien n'est rogné). Titre, auteur,
                          pochette et chapitres de la source sont conservés.
  9. Contrôle           → mesure du fichier FINAL (sonie, crête vraie, étendue
                          dynamique, durée), DNSMOS avant/après, rapport JSON

Usage :
  python finaliser.py entretien.wav                 # → entretien_podcast.mp3 (mono)
  python finaliser.py entretien.wav --canaux stereo # garder la stéréo
  python finaliser.py entretien.mp4                 # → entretien_podcast.mp4
  python finaliser.py entretien.mp4 --format mp3    # le son d'une vidéo
  python finaliser.py entretien.wav --analyse-seule # le diagnostic, sans rien écrire
  python finaliser.py dossier/ -o prets/            # tous les fichiers d'un dossier
  python finaliser.py e.wav --debruitage jamais     # fond musical : ne pas y toucher
  python finaliser.py e.wav --titre "Épisode 12" --auteur "Nom"
  python finaliser.py video_fr.mp4 --sur-place      # le fichier garde son nom et sa durée
                                                    # (ce que fait --finaliser dans
                                                    # traduire.py, doubler.py, clipper.py…)

Prérequis (env conda `interview`) : ceux de nettoyer.py
  pip install deepfilternet pyloudnorm speechmos
"""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

import nettoyer as N

# ── Réglages ─────────────────────────────────────────────────────────────────
TRAME_S = 0.05              # pas de la courbe de niveau
SILENCE_NUMERIQUE_DB = -95.0

# Fond : écart entre la parole (9e décile des niveaux) et le fond (5e centile :
# même dans une parole dense, un vingtième du temps est fait de pauses)
CENTILE_FOND = 5
# 40 dB : un enregistrement de studio publié (mesuré : 44 dB) n'est pas touché,
# une salle ou un souffle audible au casque (mesurés : 34 à 37 dB) le sont.
ECART_FOND_PROPRE_DB = 40.0     # au-dessus : rien à débruiter
ECART_FOND_FORT_DB = 30.0       # en dessous : le fond est fort
REDUCTION_LEGERE_DB = 8         # mélange dry/wet : 40 % d'original conservé
REDUCTION_FORTE_DB = 12         # 25 % d'original conservé
REGULARITE_MAX_DB = 5.0         # le fond d'un bruit régulier varie peu dans le temps
PART_TONALE_MAX = 0.25          # au-delà, le fond est fait de notes : de la musique

# Rognage : ce qu'on garde avant le premier son et après le dernier
AIR_AVANT_S = 0.4
AIR_APRES_S = 1.0
ROGNAGE_MIN_S = 0.3             # on ne coupe pas pour moins que ça
SEUIL_SON_MIN_DB = -60.0

FONDU_DEBUT_S = 0.25
FONDU_FIN_S = 0.6
FONDU_MIN_S = 0.03              # anti-clic, quand le son commence ou finit au ras

ETENDUE_NIVELAGE_LU = 14.0      # au-delà, les niveaux sont trop inégaux
# La parole brute a des crêtes 18 à 22 dB au-dessus de sa sonie : atteindre la
# cible demande presque toujours de rogner quelques crêtes. Au-delà de
# LIMITEUR_MAX_DB, on préfère un fichier un peu moins fort à des crêtes écrasées.
LIMITEUR_MAX_DB = 4.0
PLAFOND_FINAL_DBTP = -1.0       # ce que les plateformes demandent au fichier final

# Canaux
CANAL_VIDE_DB = 30.0            # un canal plus faible que l'autre de tant est vide
MEME_SIGNAL = 0.85              # corrélation (une fois recalés) : même prise de son
RETARD_MAX_S = 0.020            # décalage cherché entre les canaux
RETARD_MIN_ECH = 3              # en dessous, le creux tombe au-delà de 8 kHz : inaudible
ECART_NIVEAUX_MIN_DB = 1.5      # en dessous, on ne rééquilibre pas
ECART_NIVEAUX_MAX_DB = 15.0     # au-delà, on ne devine plus : on signale
VOIX_ECART_MIN_DB = 25.0        # un canal « porte une voix » si ses niveaux varient d'autant
DEBITS_MP3 = {1: 128, 2: 192}   # kbit/s, débit constant
DEBITS_AAC = {1: 96, 2: 160}
EXTENSIONS_VIDEO = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
CODECS_IMAGE_MP4 = {"h264", "hevc", "av1", "mpeg4"}


# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def db(x):
    return 20 * math.log10(max(float(x), 1e-12))


def hms(t):
    t = max(0.0, t)
    return f"{int(t // 3600):d}:{int(t % 3600 // 60):02d}:{t % 60:06.3f}"


def sonder(fichier):
    """ffprobe : pistes, durée, étiquettes, pochette, chapitres."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:format_tags:stream=index,codec_type,codec_name,channels,"
         "sample_rate,width,height:stream_disposition=attached_pic",
         "-show_chapters", "-of", "json", str(fichier)],
        capture_output=True, text=True)
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        d = {}
    son = image = pochette = None
    sous_titres = 0
    for s in d.get("streams", []):
        if s.get("codec_type") == "audio" and son is None:
            son = s
        elif s.get("codec_type") == "video":
            if (s.get("disposition") or {}).get("attached_pic"):
                pochette = pochette or s
            else:
                image = image or s
        elif s.get("codec_type") == "subtitle":
            sous_titres += 1
    if son is None:
        raise RuntimeError("ce fichier n'a pas de son")
    return {
        "duree": float(d.get("format", {}).get("duration") or 0),
        "canaux": int(son.get("channels") or 1),
        "sr": int(son.get("sample_rate") or 0),
        "codec_son": son.get("codec_name", ""),
        "image": ({"codec": image.get("codec_name", ""), "largeur": image.get("width"),
                   "hauteur": image.get("height")} if image else None),
        "pochette": pochette is not None,
        "sous_titres": sous_titres,
        "chapitres": d.get("chapters", []),
        "etiquettes": d.get("format", {}).get("tags", {}) or {},
    }


def mesurer_ebur128(fichier):
    """Sonie intégrée, étendue dynamique (LRA) et crête vraie, par ffmpeg."""
    stderr = N.executer(
        ["ffmpeg", "-nostats", "-hide_banner", "-i", str(fichier), "-map", "0:a:0",
         "-filter_complex", "ebur128=peak=true", "-f", "null", "-"], "mesure ebur128")
    bloc = stderr[stderr.rfind("Summary:"):]
    lire = lambda motif: (float(m.group(1)) if (m := re.search(motif, bloc)) else None)
    return {"lufs": lire(r"I:\s+(-?[\d.]+) LUFS"),
            "etendue_lu": lire(r"LRA:\s+(-?[\d.]+) LU"),
            "crete_dbtp": lire(r"Peak:\s+(-?[\d.]+) dBFS")}


# ─────────────────────────────────────────────────────────────────────────────
# Passe 1 — Analyse
# ─────────────────────────────────────────────────────────────────────────────

def _niveaux_par_canal(wav):
    """Courbe de niveau (dBFS, trames de 50 ms) de chaque canal."""
    import soundfile as sf
    courbes = None
    with sf.SoundFile(str(wav)) as f:
        pas = int(TRAME_S * f.samplerate)
        for bloc in f.blocks(blocksize=pas * 200, dtype="float32", always_2d=True):
            n = len(bloc) // pas
            if not n:
                continue
            e = np.sqrt((bloc[:n * pas].reshape(n, pas, -1) ** 2).mean(axis=1))
            courbes = e if courbes is None else np.concatenate([courbes, e])
    return 20 * np.log10(np.maximum(courbes, 1e-12))


def _retard_entre_canaux(wav, actives):
    """Décalage du canal droit par rapport au gauche, par corrélation croisée
    sur quelques fenêtres de 4 s. Retourne (décalage en échantillons,
    corrélation signée à ce décalage), ou (0, corrélation simple) si les
    fenêtres ne s'accordent pas."""
    import soundfile as sf
    retards, valeurs = [], []
    with sf.SoundFile(str(wav)) as f:
        sr = f.samplerate
        taille = 4 * sr
        portee = int(RETARD_MAX_S * sr)
        pas = int(TRAME_S * sr)
        for t in actives:
            f.seek(min(int(t) * pas, max(0, f.frames - taille)))
            x = f.read(taille, dtype="float64", always_2d=True)
            if len(x) < sr:
                continue
            g, d_ = x[:, 0], x[:, 1]
            eg, ed = float(g @ g), float(d_ @ d_)
            if eg <= 0 or ed <= 0:
                continue
            nfft = 1 << int(np.ceil(np.log2(len(x) * 2)))
            c = np.fft.irfft(np.conj(np.fft.rfft(g, nfft)) * np.fft.rfft(d_, nfft), nfft)
            c = np.concatenate([c[-portee:], c[:portee + 1]]) / math.sqrt(eg * ed)
            k = int(np.argmax(np.abs(c)))
            retards.append(k - portee)
            valeurs.append(float(c[k]))
    if not retards:
        return 0, 0.0
    retard = int(np.median(retards))
    accord = np.mean([abs(r - retard) <= 1 for r in retards])
    return (retard if accord >= 0.6 else 0), float(np.median(valeurs))


def analyser_canaux(wav):
    """Ce que valent les deux canaux, l'un par rapport à l'autre."""
    import soundfile as sf
    with sf.SoundFile(str(wav)) as f:
        if f.channels == 1:
            return {"source": "mono"}
        gg = dd = gd = 0.0
        for bloc in f.blocks(blocksize=f.samplerate * 10, dtype="float64", always_2d=True):
            g, d_ = bloc[:, 0], bloc[:, 1]
            gg += float(g @ g)
            dd += float(d_ @ d_)
            gd += float(g @ d_)
        n = max(f.frames, 1)
    diag = {"source": "stereo",
            "niveau_gauche_db": round(db(math.sqrt(gg / n)), 1),
            "niveau_droite_db": round(db(math.sqrt(dd / n)), 1),
            "correlation": round(gd / math.sqrt(gg * dd), 3) if gg > 0 and dd > 0 else 0.0}
    courbes = _niveaux_par_canal(wav)
    for k, nom in ((0, "gauche"), (1, "droite")):
        c = courbes[:, k]
        c = c[c > SILENCE_NUMERIQUE_DB]
        if len(c) >= 40:
            diag[f"voix_{nom}_db"] = round(float(np.percentile(c, 90)), 1)
            diag[f"variation_{nom}_db"] = round(
                float(np.percentile(c, 90) - np.percentile(c, 10)), 1)
    ensemble = courbes.max(axis=1)
    fortes = np.flatnonzero(ensemble > np.percentile(ensemble, 60))
    if len(fortes):
        choix = fortes[np.linspace(0, len(fortes) - 1, min(8, len(fortes))).astype(int)]
        retard, valeur = _retard_entre_canaux(wav, choix)
        diag["retard_echantillons"] = retard
        diag["correlation_recalee"] = round(valeur, 3)
    return diag


def regler_les_canaux(diag, voulu):
    """La recette de mélange, et ses raisons. `voulu` : « mono » ou « stereo ».
    Recette : prendre (deux, gauche, droite), gains en dB, polarité du canal
    droit, décalage du canal droit, nombre de canaux en sortie."""
    r = {"prendre": "deux", "gain_gauche_db": 0.0, "gain_droite_db": 0.0,
         "polarite_droite": 1, "retard_droite": 0, "sortie": 2 if voulu == "stereo" else 1}
    raisons, alertes = [], []
    if diag["source"] == "mono":
        r["sortie"] = 1
        if voulu == "stereo":
            raisons.append("la source est mono : la sortie le reste")
        return r, raisons, alertes

    ng, nd = diag["niveau_gauche_db"], diag["niveau_droite_db"]
    if nd < -90 or ng - nd > CANAL_VIDE_DB:
        r["prendre"] = "gauche"
        raisons.append("le canal droit est vide : le gauche est pris seul"
                       + (", sur les deux canaux" if voulu == "stereo" else ""))
        return r, raisons, alertes
    if ng < -90 or nd - ng > CANAL_VIDE_DB:
        r["prendre"] = "droite"
        raisons.append("le canal gauche est vide : le droit est pris seul"
                       + (", sur les deux canaux" if voulu == "stereo" else ""))
        return r, raisons, alertes

    recalee = diag.get("correlation_recalee", diag["correlation"])
    meme = abs(recalee) >= MEME_SIGNAL
    if (meme and recalee < 0) or (not meme and diag["correlation"] < -0.5):
        r["polarite_droite"] = -1
        raisons.append("les deux canaux sont en opposition de phase (la voix "
                       "s'annulerait en mono) : le droit est retourné")
    retard = diag.get("retard_echantillons", 0)
    if meme and abs(retard) >= RETARD_MIN_ECH:
        ms = abs(retard) / 48.0
        if voulu == "mono":
            r["retard_droite"] = retard
            raisons.append(f"le canal {'droit' if retard > 0 else 'gauche'} est en retard de "
                           f"{ms:.1f} ms : les canaux sont recalés avant d'être réunis")
        else:
            alertes.append(f"le canal {'droit' if retard > 0 else 'gauche'} est en retard de "
                           f"{ms:.1f} ms : laissé tel quel en stéréo, mais ce fichier "
                           "sonnerait creux sur un haut-parleur unique")

    vg, vd = diag.get("voix_gauche_db"), diag.get("voix_droite_db")
    if vg is not None and vd is not None:
        ecart = vg - vd
        deux_voix = (diag.get("variation_gauche_db", 0) >= VOIX_ECART_MIN_DB
                     and diag.get("variation_droite_db", 0) >= VOIX_ECART_MIN_DB)
        if abs(ecart) >= ECART_NIVEAUX_MIN_DB:
            faible = "droit" if ecart > 0 else "gauche"
            if abs(ecart) > ECART_NIVEAUX_MAX_DB:
                alertes.append(f"le canal {faible} est plus faible de {abs(ecart):.0f} dB : "
                               "écart trop grand pour être corrigé à l'aveugle")
            elif meme or deux_voix:
                # Moitié-moitié : on monte le faible, on baisse le fort
                r["gain_gauche_db"] = round(-ecart / 2.0, 2)
                r["gain_droite_db"] = round(ecart / 2.0, 2)
                raisons.append(
                    f"le canal {faible} est plus faible de {abs(ecart):.1f} dB : "
                    + ("niveaux rééquilibrés" if meme else
                       "une voix par canal, niveaux rééquilibrés avant le mélange"))
            else:
                alertes.append(f"le canal {faible} est plus faible de {abs(ecart):.0f} dB, "
                               "mais il ne porte pas clairement une voix : laissé tel quel")
    if not raisons:
        raisons.append("les deux canaux sont cohérents"
                       + (" et identiques" if meme and abs(retard) < RETARD_MIN_ECH else ""))
    return r, raisons, alertes


def mixer(wav_entree, wav_sortie, r):
    """Applique la recette des canaux, sur toute la longueur du fichier."""
    import soundfile as sf
    gg, gd = N.db_vers_lineaire(r["gain_gauche_db"]), N.db_vers_lineaire(r["gain_droite_db"])
    gd *= r["polarite_droite"]
    retard = r["retard_droite"]
    with sf.SoundFile(str(wav_entree)) as f, sf.SoundFile(str(wav_entree)) as f2:
        sr, total = f.samplerate, f.frames
        with sf.SoundFile(str(wav_sortie), "w", samplerate=sr, channels=r["sortie"],
                          subtype="FLOAT") as g:
            pos = 0
            while pos < total:
                n = min(sr * 10, total - pos)
                x = f.read(n, dtype="float32", always_2d=True)
                if f.channels == 1:
                    gauche = droite = x[:, 0]
                else:
                    gauche, droite = x[:, 0] * gg, x[:, 1]
                    if retard:
                        # le droit est lu « retard » échantillons plus loin (ou plus tôt)
                        droite = np.zeros(n, dtype=np.float32)
                        a = pos + retard
                        da, db_ = max(a, 0), min(a + n, total)
                        if db_ > da:
                            f2.seek(da)
                            droite[da - a:db_ - a] = f2.read(
                                db_ - da, dtype="float32", always_2d=True)[:, 1]
                    droite = droite * gd
                if r["prendre"] == "gauche":
                    gauche = droite = x[:, 0]
                elif r["prendre"] == "droite":
                    gauche = droite = x[:, 1]
                if r["sortie"] == 1:
                    y = ((gauche + droite) / 2.0)[:, None]
                else:
                    y = np.stack([gauche, droite], axis=1)
                g.write(y.astype(np.float32))
                pos += n


def _pauses(niveaux, plancher, longueur=8):
    """Débuts (en trames) des passages où il n'y a que le fond, `longueur`
    trames de suite (0,4 s)."""
    calme = (niveaux <= plancher + 3.0) & (niveaux > SILENCE_NUMERIQUE_DB)
    debuts, i = [], 0
    while i + longueur <= len(calme):
        if calme[i:i + longueur].all():
            debuts.append(i)
            i += longueur
        else:
            i += 1
    if len(debuts) > 60:
        debuts = [debuts[k] for k in np.linspace(0, len(debuts) - 1, 60).astype(int)]
    return debuts


def detecter_ronflette(wav, niveaux, sr):
    """La détection de nettoyer.py (sur tout le fichier), complétée par une
    recherche dans les pauses : sous la voix, une harmonique à 150 Hz est
    masquée par le fondamental de la voix ; dans les pauses, on n'entend
    qu'elle. Une famille (50 ou 60 Hz) n'est retenue que si deux de ses raies
    au moins ressortent."""
    import soundfile as sf
    utiles = niveaux[niveaux > SILENCE_NUMERIQUE_DB]
    if len(utiles) < 40:
        return list(N.detecter_ronflette(wav))
    plancher = float(np.percentile(utiles, CENTILE_FOND))
    # Dans un fond musical, des notes tombent forcément près de 100 ou 200 Hz :
    # on n'y cherche pas de ronflette, un filtre y creuserait la musique.
    tonale = nature_du_fond(wav, niveaux, sr, plancher)
    if tonale is not None and tonale > PART_TONALE_MAX:
        return []
    trouvees = list(N.detecter_ronflette(wav))
    longueur = 8
    debuts = _pauses(niveaux, plancher, longueur)
    if len(debuts) < 5:
        return trouvees
    pas = int(TRAME_S * sr)
    n = longueur * pas
    fenetre = np.hanning(n)
    somme = np.zeros(n // 2 + 1)
    with sf.SoundFile(str(wav)) as f:
        for d_ in debuts:
            f.seek(d_ * pas)
            x = f.read(n, dtype="float64", always_2d=True).mean(axis=1)
            if len(x) == n:
                somme += np.abs(np.fft.rfft(x * fenetre)) ** 2
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    spectre = 10 * np.log10(somme + 1e-30)
    for f0 in (50, 60):
        raies = []
        for k in range(1, 9):
            ecart = np.abs(freqs - f0 * k)
            bande, autour = ecart <= 3.0, (ecart > 6.0) & (ecart <= 20.0)
            i = int(np.flatnonzero(bande)[spectre[bande].argmax()])
            if spectre[i] - np.median(spectre[autour]) < N.HUM_SEUIL_DB:
                continue
            # sommet affiné par une parabole sur les trois points voisins
            a, b, c = spectre[i - 1], spectre[i], spectre[i + 1]
            pli = a - 2 * b + c
            decale = 0.5 * (a - c) / pli if pli < 0 else 0.0
            raies.append((k, float(freqs[i] + decale * (freqs[1] - freqs[0]))))
        # Le secteur est juste : les raies d'une ronflette sont les multiples
        # exacts d'une même fréquence. Celles d'un accord ne le sont pas.
        # … et cette fréquence est celle du réseau, à un demi-hertz près : un sol
        # grave (49 Hz) et ses harmoniques ne sont pas une ronflette.
        if raies:
            base = float(np.median([f / k for k, f in raies]))
            raies = ([(k, f) for k, f in raies if abs(f - base * k) <= 1.0]
                     if abs(base - f0) <= 0.5 else [])
        connue = any(abs(t - f0 * k) < 2.0 for t in trouvees for k in (1, 2, 3))
        if len(raies) >= 2 or (raies and connue):
            trouvees += [round(f, 1) for _, f in raies
                         if not any(abs(f - t) < 3.0 for t in trouvees)]
    return sorted(trouvees)


def courbe_de_niveau(wav):
    """Niveau (dBFS) par trame de 50 ms, tous canaux confondus, et saturation."""
    import soundfile as sf
    niveaux = []
    satures = 0
    crete = 0.0
    with sf.SoundFile(str(wav)) as f:
        sr = f.samplerate
        pas = int(TRAME_S * sr)
        for bloc in f.blocks(blocksize=pas * 200, dtype="float32", always_2d=True):
            mono = bloc.mean(axis=1)
            n = len(mono) // pas
            if n:
                e = np.sqrt((mono[:n * pas].reshape(n, pas) ** 2).mean(axis=1))
                niveaux.append(20 * np.log10(np.maximum(e, 1e-12)))
            a = np.abs(bloc)
            crete = max(crete, float(a.max()) if a.size else 0.0)
            # Saturation : au moins trois échantillons de suite au plafond
            plein = (a >= 0.999).any(axis=1)
            if plein.any():
                suite = plein[2:] & plein[1:-1] & plein[:-2]
                satures += int((suite[1:] & ~suite[:-1]).sum() + (1 if suite[:1].any() else 0))
    return (np.concatenate(niveaux) if niveaux else np.array([])), sr, satures, crete


def nature_du_fond(wav, niveaux, sr, plancher):
    """Le fond est-il fait de bruit ou de notes ? Part de l'énergie du fond
    (300-6000 Hz) portée par des raies qui dépassent de 6 dB le spectre lissé.
    Un bruit (souffle, ventilation, salle) donne presque 0 ; une musique donne
    beaucoup plus."""
    import soundfile as sf
    from scipy.signal import welch
    from scipy.ndimage import median_filter

    longueur = 8                       # trames de suite : 0,4 s de fond seul
    choisis = _pauses(niveaux, plancher, longueur)
    if len(choisis) < 5:
        return None
    parts = []
    with sf.SoundFile(str(wav)) as f:
        pas = int(TRAME_S * sr)
        for d_ in choisis:
            f.seek(d_ * pas)
            x = f.read(longueur * pas, dtype="float32", always_2d=True).mean(axis=1)
            if len(x) < 4096:
                continue
            freqs, psd = welch(x, fs=sr, nperseg=4096, noverlap=3072)
            bande = (freqs >= 300) & (freqs <= 6000)
            p = psd[bande]
            if p.sum() <= 0:
                continue
            lisse = median_filter(p, size=41, mode="nearest")     # ~470 Hz
            raies = p > 4.0 * np.maximum(lisse, 1e-30)            # +6 dB
            parts.append(float(p[raies].sum() / p.sum()))
    return round(float(np.median(parts)), 3) if parts else None


def analyser(wav, infos):
    """Tout ce que les décisions ont besoin de savoir."""
    niveaux, sr, satures, crete = courbe_de_niveau(wav)
    a = {"saturations": satures, "crete_db": round(db(crete), 2)}
    utiles = niveaux[niveaux > SILENCE_NUMERIQUE_DB]
    if len(utiles) < 40:
        raise RuntimeError("ce fichier est muet, ou trop court pour être analysé")

    plancher = float(np.percentile(utiles, CENTILE_FOND))
    parole = float(np.percentile(utiles, 90))
    a["fond_db"] = round(plancher, 1)
    a["parole_db"] = round(parole, 1)
    a["ecart_db"] = round(parole - plancher, 1)

    # Régularité : le fond mesuré par tranches de 30 s varie-t-il ?
    par_tranche = int(30 / TRAME_S)
    fonds = []
    for i in range(0, len(niveaux), par_tranche):
        t = niveaux[i:i + par_tranche]
        t = t[t > SILENCE_NUMERIQUE_DB]
        if len(t) >= par_tranche // 4:
            fonds.append(float(np.percentile(t, CENTILE_FOND)))
    a["regularite_db"] = (round(float(np.percentile(fonds, 75) - np.percentile(fonds, 25)), 1)
                          if len(fonds) >= 3 else 0.0)
    a["part_tonale"] = nature_du_fond(wav, niveaux, sr, plancher)

    # Premier et dernier son : au-dessus du fond, et qui dure
    seuil = max(plancher + 8.0, SEUIL_SON_MIN_DB)
    fort = niveaux > seuil
    dense = np.convolve(fort.astype(np.int16), np.ones(6, dtype=np.int16), mode="valid") >= 3
    idx = np.flatnonzero(dense)
    duree = len(niveaux) * TRAME_S
    if len(idx):
        premier = float(np.flatnonzero(fort[idx[0]:idx[0] + 6])[0] + idx[0]) * TRAME_S
        dernier = float(idx[-1] + np.flatnonzero(fort[idx[-1]:idx[-1] + 6])[-1] + 1) * TRAME_S
    else:
        premier, dernier = 0.0, duree
    a["premier_son_s"] = round(premier, 2)
    a["dernier_son_s"] = round(min(dernier, infos["duree"] or duree), 2)
    a["niveaux"], a["sr"] = niveaux, sr
    return a


def decider(a, infos, args, canaux_sortie):
    """Le plan : ce qui sera fait, et pourquoi. Rien n'est encore touché."""
    plan = {"raisons": []}
    duree = infos["duree"]

    # Rognage
    debut, fin = 0.0, duree
    if args.debut is not None:
        debut = args.debut
        plan["raisons"].append(f"début imposé à {hms(debut)}")
    elif not args.sans_rognage and a["premier_son_s"] - AIR_AVANT_S >= ROGNAGE_MIN_S:
        debut = a["premier_son_s"] - AIR_AVANT_S
        plan["raisons"].append(f"{debut:.1f} s de silence retirées au début")
    if args.fin is not None:
        fin = min(args.fin, duree)
        plan["raisons"].append(f"fin imposée à {hms(fin)}")
    elif not args.sans_rognage and duree - a["dernier_son_s"] - AIR_APRES_S >= ROGNAGE_MIN_S:
        fin = a["dernier_son_s"] + AIR_APRES_S
        plan["raisons"].append(f"{duree - fin:.1f} s de silence retirées à la fin")
    if fin - debut < 1.0:
        raise RuntimeError("il ne resterait rien après le rognage")
    plan["debut_s"], plan["fin_s"] = round(debut, 3), round(fin, 3)

    # Fondus : dans l'air qui précède le premier son et suit le dernier
    air_avant = max(0.0, a["premier_son_s"] - debut) if args.debut is None else args.fondu_debut
    air_apres = max(0.0, fin - a["dernier_son_s"]) if args.fin is None else args.fondu_fin
    plan["fondu_debut_s"] = round(min(args.fondu_debut, max(FONDU_MIN_S, air_avant)), 3)
    plan["fondu_fin_s"] = round(min(args.fondu_fin, max(FONDU_MIN_S, air_apres)), 3)

    plan["canaux_sortie"] = canaux_sortie

    # Débruitage
    tonal = a["part_tonale"] is not None and a["part_tonale"] > PART_TONALE_MAX
    if args.debruitage == "jamais":
        plan["debruitage"] = 0
        plan["raisons"].append("débruitage écarté à la demande")
    elif args.debruitage == "toujours":
        plan["debruitage"] = args.reduction or REDUCTION_LEGERE_DB
        plan["raisons"].append(f"débruitage demandé ({plan['debruitage']} dB)")
    elif a["ecart_db"] >= ECART_FOND_PROPRE_DB:
        plan["debruitage"] = 0
        plan["raisons"].append(
            f"fond propre ({a['ecart_db']:.0f} dB sous la parole) : pas de débruitage")
    elif tonal:
        plan["debruitage"] = 0
        plan["raisons"].append(
            "le fond est fait de notes (musique) et non d'un bruit : laissé intact")
    elif a["regularite_db"] > REGULARITE_MAX_DB:
        plan["debruitage"] = 0
        plan["raisons"].append(
            f"le fond change au fil du fichier (±{a['regularite_db']:.0f} dB) : ce n'est "
            "pas un bruit régulier, il est laissé intact")
    else:
        plan["debruitage"] = args.reduction or (
            REDUCTION_FORTE_DB if a["ecart_db"] < ECART_FOND_FORT_DB else REDUCTION_LEGERE_DB)
        plan["raisons"].append(
            f"bruit de fond régulier à {a['ecart_db']:.0f} dB sous la parole : "
            f"débruitage léger ({plan['debruitage']} dB au plus)")

    plan["cible_lufs"] = args.lufs if args.lufs is not None else (
        N.LUFS_CIBLE_STEREO if plan["canaux_sortie"] == 2 else N.LUFS_CIBLE_MONO)
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Passes 2 et 7 — Canaux, rognage, fondus
# ─────────────────────────────────────────────────────────────────────────────

def rogner(wav_entree, wav_sortie, debut_s, fin_s):
    """Garde [début, fin]."""
    import soundfile as sf
    with sf.SoundFile(str(wav_entree)) as f:
        sr = f.samplerate
        a, b = int(round(debut_s * sr)), min(f.frames, int(round(fin_s * sr)))
        f.seek(a)
        with sf.SoundFile(str(wav_sortie), "w", samplerate=sr, channels=f.channels,
                          subtype="FLOAT") as g:
            reste = b - a
            while reste > 0:
                x = f.read(min(reste, sr * 10), dtype="float32", always_2d=True)
                if not len(x):
                    break
                reste -= len(x)
                g.write(x)


def poser_les_fondus(wav_entree, wav_sortie, fondu_debut_s, fondu_fin_s):
    """Fondus en demi-cosinus."""
    import soundfile as sf
    with sf.SoundFile(str(wav_entree)) as f:
        sr, total = f.samplerate, f.frames
        n1 = min(int(fondu_debut_s * sr), total // 2)
        n2 = min(int(fondu_fin_s * sr), total // 2)
        with sf.SoundFile(str(wav_sortie), "w", samplerate=sr, channels=f.channels,
                          subtype="FLOAT") as g:
            pos = 0
            while pos < total:
                x = f.read(min(sr * 10, total - pos), dtype="float32", always_2d=True)
                i = np.arange(pos, pos + len(x))
                gain = np.ones(len(x), dtype=np.float64)
                if n1 > 1:
                    gain *= 0.5 - 0.5 * np.cos(np.pi * np.clip(i / n1, 0.0, 1.0))
                if n2 > 1:
                    gain *= 0.5 - 0.5 * np.cos(np.pi * np.clip((total - i) / n2, 0.0, 1.0))
                g.write((x * gain[:, None]).astype(np.float32))
                pos += len(x)


def mettre_a_niveau(wav_entree, wav_sortie, cible, limiteur_max_db, rapport):
    """Normalisation de nettoyer.py (gain constant, limiteur sur les seules
    crêtes en dernier recours), avec deux garde-fous :
      - le limiteur ne retire jamais plus de `limiteur_max_db` aux crêtes : s'il
        en fallait davantage, la sonie visée est abaissée d'autant ;
      - un limiteur qui a travaillé fait perdre quelques dixièmes de sonie : une
        seconde passe les rattrape, dans la même limite."""
    import soundfile as sf
    import pyloudnorm as pyln
    audio, sr = sf.read(str(wav_entree), dtype="float32")
    lufs = pyln.Meter(sr).integrated_loudness(audio)
    crete = N.mesurer_crete_vraie(audio.T if audio.ndim > 1 else audio, sr)
    del audio
    rapport["mesures"]["crete_sur_sonie_db"] = round(crete - lufs, 1)
    plafond_cible = lufs + limiteur_max_db + N.TP_PLAFOND_DB - crete   # cible la plus haute permise
    visee = cible
    if visee > plafond_cible:
        rapport["alertes"].append(
            f"crêtes très hautes par rapport à la voix ({crete - lufs:.0f} dB) : sonie "
            f"abaissée à {plafond_cible:.1f} LUFS au lieu de {cible:.1f}, pour ne pas "
            f"retirer plus de {limiteur_max_db:.0f} dB aux crêtes (--limiteur-max pour changer)")
        visee = plafond_cible

    def passe(v):
        avant = len(rapport["alertes"])
        N.normaliser(wav_entree, wav_sortie, v, rapport)
        # nettoyer.py signale son limiteur comme une alerte (il y est rare) ; ici
        # c'est le travail ordinaire, qui se lit dans les mesures
        rapport["alertes"][avant:] = [a for a in rapport["alertes"][avant:]
                                      if not a.startswith(("crêtes isolées", "cible réduite"))]
        x, _ = sf.read(str(wav_sortie), dtype="float32")
        return pyln.Meter(sr).integrated_loudness(x)

    obtenue = passe(visee)
    manque = visee - obtenue
    if rapport["mesures"]["limiteur"] and manque > 0.2:
        obtenue = passe(min(visee + manque, plafond_cible))
    m = rapport["mesures"]
    m["retire_aux_cretes_db"] = round(max(0.0, crete + m["gain_db"] - N.TP_PLAFOND_DB), 1) \
        if m["limiteur"] else 0.0
    m["sonie_obtenue"] = round(float(obtenue), 2)
    return visee


class _silence_des_bibliotheques:
    """DeepFilterNet écrit sur la sortie d'erreur en se chargeant (avertissement
    torchaudio, appel à git) : rien qui regarde l'utilisateur."""
    def __enter__(self):
        import os
        sys.stderr.flush()
        self.garde = os.dup(2)
        self.nul = os.open(os.devnull, os.O_WRONLY)
        os.dup2(self.nul, 2)

    def __exit__(self, *_):
        import os
        sys.stderr.flush()
        os.dup2(self.garde, 2)
        os.close(self.garde)
        os.close(self.nul)


# ─────────────────────────────────────────────────────────────────────────────
# Passe 4 — Débruitage prudent
# ─────────────────────────────────────────────────────────────────────────────

def _mono(wav):
    import soundfile as sf
    x, sr = sf.read(str(wav), dtype="float32")
    return (x.mean(axis=1) if x.ndim > 1 else x), sr


def debruiter_prudemment(wav_entree, wav_reference, travail, reduction_db, args, rapport):
    """DeepFilterNet3 à faible dose, jugé par DNSMOS. Si la voix y perd, essai
    d'une soustraction spectrale douce. Si elle y perd encore, on rend le
    fichier d'entrée : mieux vaut un fond audible qu'une voix abîmée."""
    try:
        import speechmos  # noqa: F401
        juge = True
    except ImportError:
        juge = False
        rapport["alertes"].append(
            "speechmos absent : le débruitage n'a pas pu être contrôlé par DNSMOS")

    essais = {}
    if juge:
        reference, sr = _mono(wav_reference)
        positions = N.choisir_fenetres_parole(reference, sr)
        s_ref = N.score_dnsmos(reference, sr, positions)
        essais["avant"] = s_ref
        del reference
    rapport["debruitage"] = essais

    def juger(nom, wav):
        if not juge or not s_ref:
            return True
        x, sr_ = _mono(wav)
        s = N.score_dnsmos(x, sr_, positions)
        essais[nom] = s
        return bool(s) and s_ref["sig"] - s["sig"] <= N.QC_SIG_BAISSE_MAX

    wav_dfn = travail / "04_debruite_dfn.wav"
    try:
        with _silence_des_bibliotheques():
            N.charger_dfn(args.cpu)
        N.debruiter_dfn(wav_entree, wav_dfn, reduction_db, args.cpu)
        if juger("deepfilternet", wav_dfn):
            rapport["mesures"]["debruitage"] = f"DeepFilterNet3, {reduction_db} dB au plus"
            return wav_dfn
    except ImportError:
        rapport["alertes"].append("deepfilternet absent : soustraction spectrale seule")

    wav_doux = travail / "04_debruite_doux.wav"
    nr = min(reduction_db, N.AFFTDN_NR)
    N.executer(["ffmpeg", "-y", "-hide_banner", "-i", str(wav_entree),
                "-af", f"afftdn=nr={nr}:nt=w:tn=1", "-c:a", "pcm_f32le", str(wav_doux)],
               "débruitage doux")
    if juger("soustraction_spectrale", wav_doux):
        rapport["mesures"]["debruitage"] = f"soustraction spectrale douce, {nr} dB"
        rapport["alertes"].append(
            "DeepFilterNet abîmait cette voix : soustraction spectrale douce retenue")
        return wav_doux

    rapport["mesures"]["debruitage"] = "aucun (la voix en souffrait)"
    rapport["alertes"].append(
        "le débruitage abîmait la voix quel que soit le moteur : fond laissé tel quel")
    return wav_entree


# ─────────────────────────────────────────────────────────────────────────────
# Passe 8 — Encodage
# ─────────────────────────────────────────────────────────────────────────────

def ecrire_etiquettes(chemin, infos, plan, args):
    """Étiquettes et chapitres de la source, décalés du rognage du début."""
    echappe = lambda s: re.sub(r"([=;#\\\n])", r"\\\1", str(s))
    e = dict(infos["etiquettes"])
    for cle, valeur in (("title", args.titre), ("artist", args.auteur),
                        ("album", args.album)):
        if valeur:
            e[cle] = valeur
    for cle in [c for c in e if c.lower() in ("encoder", "encoded_by", "major_brand",
                                              "minor_version", "compatible_brands",
                                              "creation_time", "timecode")]:
        del e[cle]
    lignes = [";FFMETADATA1"] + [f"{echappe(c)}={echappe(v)}" for c, v in e.items()]
    d0, d1 = plan["debut_s"], plan["fin_s"]
    gardes = 0
    for c in infos["chapitres"]:
        a, b = float(c.get("start_time", 0)), float(c.get("end_time", 0))
        a, b = max(a, d0), min(b, d1)
        if b - a < 0.5:
            continue
        lignes += ["", "[CHAPTER]", "TIMEBASE=1/1000",
                   f"START={int(round((a - d0) * 1000))}",
                   f"END={int(round((b - d0) * 1000))}",
                   f"title={echappe((c.get('tags') or {}).get('title', ''))}"]
        gardes += 1
    Path(chemin).write_text("\n".join(lignes) + "\n", encoding="utf-8")
    return gardes


# L'encodeur AAC de ffmpeg produit parfois, sur un passage donné, une crête que
# le son n'a pas (mesuré : +3,6 dB en pleine parole). Ces réglages de repli
# l'évitent ; ils sont essayés dans l'ordre, seulement si le défaut surgit.
REPLIS_AAC = [[], ["-aac_pns", "0"], ["-aac_tns", "0"], ["-aac_coder", "fast"]]
DEPASSEMENT_MAX_DB = 1.0        # ce qu'un encodeur a le droit d'ajouter aux crêtes


def encoder_piste(wav, piste, fmt, canaux, debit, rapport):
    """Encode le son seul, MESURE le fichier obtenu, et corrige ce que
    l'encodeur a changé : la norme doit être tenue par le fichier publié, pas
    par celui qui le précède.
      - LAME baisse de lui-même le niveau à débit constant (0,5 dB à
        128 kbit/s, 0,3 dB à 192) : compensé ;
      - l'encodeur AAC peut ajouter une crête : réglage de repli ;
      - en dernier recours, le niveau est baissé pour tenir le plafond."""
    frequence = 48000 if fmt in ("wav", "aac48") else 44100
    aac = fmt not in ("wav", "mp3")

    def essai(gain_db, reglage):
        filtres = ([f"volume={gain_db:.2f}dB"] if abs(gain_db) > 0.01 else []) + [
            f"aresample=resampler=soxr:precision=28:out_sample_rate={frequence}"
            + (":dither_method=triangular" if fmt == "wav" else "")]
        codec = {"wav": ["-c:a", "pcm_s16le"],
                 "mp3": ["-c:a", "libmp3lame", "-b:a", f"{debit}k"],
                 }.get(fmt, ["-c:a", "aac", "-b:a", f"{debit}k"] + reglage)
        N.executer(["ffmpeg", "-y", "-hide_banner", "-i", str(wav), "-map_metadata", "-1",
                    "-af", ",".join(filtres), "-ac", str(canaux)] + codec + [str(piste)],
                   "encodage du son")
        return mesurer_ebur128(piste)

    voulu = mesurer_ebur128(wav)
    essais = []
    for reglage in (REPLIS_AAC if aac else [[]]):
        obtenu = essai(0.0, reglage)
        essais.append((obtenu["crete_dbtp"], reglage))
        if obtenu["crete_dbtp"] <= voulu["crete_dbtp"] + DEPASSEMENT_MAX_DB:
            break
    else:
        reglage = min(essais, key=lambda e: e[0])[1]
        obtenu = essai(0.0, reglage)
    if reglage:
        rapport["mesures"]["encodeur"] = "réglage de repli : " + " ".join(reglage)

    gain = 0.0
    ecart = voulu["lufs"] - obtenu["lufs"]
    if fmt != "wav" and abs(ecart) >= 0.15:
        gain = min(ecart, PLAFOND_FINAL_DBTP - 0.2 - obtenu["crete_dbtp"])
    if obtenu["crete_dbtp"] > PLAFOND_FINAL_DBTP:
        gain = PLAFOND_FINAL_DBTP - 0.2 - obtenu["crete_dbtp"]
        rapport["alertes"].append(
            f"l'encodeur ajoutait {obtenu['crete_dbtp'] - voulu['crete_dbtp']:.1f} dB aux "
            f"crêtes quel que soit son réglage : niveau baissé de {-gain:.1f} dB pour "
            f"tenir le plafond")
    if abs(gain) >= 0.1:
        obtenu = essai(gain, reglage)
        rapport["mesures"]["compensation_encodeur_db"] = round(gain, 2)
    return obtenu


def assembler_son(piste, source, etiquettes, sortie, fmt, pochette):
    """Le son encodé, avec les étiquettes, les chapitres et la pochette."""
    if fmt == "wav":
        shutil.move(str(piste), str(sortie))
        return
    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", str(piste), "-i", str(etiquettes)]
    if pochette:
        cmd += ["-i", str(source)]
    cmd += ["-map", "0:a:0", "-c:a", "copy", "-map_metadata", "1", "-map_chapters", "1"]
    if pochette:
        cmd += ["-map", "2:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    cmd += (["-id3v2_version", "3", "-write_id3v1", "1"] if fmt == "mp3"
            else ["-movflags", "+faststart"])
    N.executer(cmd + [str(sortie)], "assemblage")


def assembler_video(piste, source, etiquettes, sortie, infos, plan, args):
    """L'image est recopiée telle quelle quand c'est possible : pas de rognage,
    pas de fondu d'image demandé, et un codec que le MP4 accepte."""
    rogne = plan["debut_s"] > 0.0 or plan["fin_s"] < infos["duree"] - 0.05
    copie = (not rogne and not args.fondu_image
             and infos["image"]["codec"] in CODECS_IMAGE_MP4)
    duree = plan["fin_s"] - plan["debut_s"]
    cmd = ["ffmpeg", "-y", "-hide_banner"]
    if rogne:
        cmd += ["-ss", f"{plan['debut_s']:.3f}"]
    cmd += ["-i", str(source), "-i", str(piste), "-i", str(etiquettes),
            "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "2", "-map_chapters", "2",
            "-t", f"{duree:.3f}"]
    if copie:
        cmd += ["-c:v", "copy"]
    else:
        if args.fondu_image or rogne:
            fi, fo = max(plan["fondu_debut_s"], 0.3), max(plan["fondu_fin_s"], 0.5)
            cmd += ["-vf", f"fade=t=in:st=0:d={fi:.3f},"
                           f"fade=t=out:st={max(0.0, duree - fo):.3f}:d={fo:.3f}"]
        cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "copy", "-movflags", "+faststart", str(sortie)]
    N.executer(cmd, "assemblage vidéo")
    return "recopiée telle quelle" if copie else "réencodée (H.264, qualité 18), avec fondus"


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def afficher_diagnostic(a, plan):
    print("  [3/9] analyse")
    print(f"        fond         {a['fond_db']:.0f} dBFS, {a['ecart_db']:.0f} dB sous la parole"
          + (f" ; part de notes {a['part_tonale']:.2f}" if a["part_tonale"] is not None else ""))
    print(f"        sonie        {a['sonie']['lufs']} LUFS, étendue dynamique "
          f"{a['sonie']['etendue_lu']} LU, crête vraie {a['sonie']['crete_dbtp']} dBTP")
    print(f"        premier son  {hms(a['premier_son_s'])} ; dernier son {hms(a['dernier_son_s'])}")
    if a["saturations"]:
        print(f"        saturation   {a['saturations']} passages au plafond")
    print("        → " + "\n        → ".join(plan["raisons"] or ["rien à corriger avant la mise à niveau"]))


def traiter_fichier(fichier, args):
    debut_chrono = time.time()
    fichier = Path(fichier)
    infos = sonder(fichier)

    fmt = args.format
    if fmt == "auto":
        fmt = "mp4" if infos["image"] else "mp3"
    if fmt == "mp4" and not infos["image"]:
        raise RuntimeError("ce fichier n'a pas d'image : choisir mp3, m4a ou wav")
    dossier = Path(args.sortie) if args.sortie else fichier.parent
    dossier.mkdir(parents=True, exist_ok=True)
    sortie = dossier / f"{fichier.stem}_podcast.{fmt}"
    if sortie.exists() and not args.forcer and not args.analyse_seule:
        print(f"  ↷ {sortie.name} existe déjà — ignoré (--forcer pour refaire)")
        return None

    rapport = {"source": str(fichier), "sortie": str(sortie), "mesures": {}, "alertes": []}
    travail = Path(tempfile.mkdtemp(prefix=f"finaliser_{fichier.stem[:30]}_"))
    try:
        # Passe 1 — canaux : les incohérences se règlent avant toute mesure
        wav_decode = travail / "01_decode.wav"
        N.decoder(fichier, wav_decode, min(infos["canaux"], 2))
        diag = analyser_canaux(wav_decode)
        recette, raisons_canaux, alertes_canaux = regler_les_canaux(diag, args.canaux)
        wav_canaux = travail / "01_canaux.wav"
        mixer(wav_decode, wav_canaux, recette)
        wav_decode.unlink()
        print(f"  [1/9] canaux : {hms(infos['duree'])}, {infos['sr']} Hz, "
              f"{infos['canaux']} canaux" + (", vidéo" if infos["image"] else "")
              + f" → sortie {'stéréo' if recette['sortie'] == 2 else 'mono'}")
        for ligne in raisons_canaux:
            print(f"        → {ligne}")
        rapport["alertes"] += alertes_canaux
        rapport["canaux"] = {"mesures": diag, "recette": recette, "raisons": raisons_canaux}

        # Passe 2 — conditionnement, avant de mesurer : la ronflette et les
        # grondements ne doivent pas passer pour un bruit de fond à débruiter
        ronflette = detecter_ronflette(wav_canaux, *courbe_de_niveau(wav_canaux)[:2])
        wav_conditionne = travail / "02_conditionne.wav"
        N.conditionner(wav_canaux, wav_conditionne, ronflette, args.passe_haut)
        print(f"  [2/9] conditionnement : passe-haut {args.passe_haut} Hz"
              + (f", filtres étroits à {ronflette} Hz (ronflette secteur)" if ronflette else ""))

        # Passe 3 — analyse, sur le son tel qu'il sera traité
        a = analyser(wav_conditionne, infos)
        a["sonie"] = mesurer_ebur128(wav_conditionne)
        a["ronflette"] = ronflette
        plan = decider(a, infos, args, recette["sortie"])
        niveler = (args.nivelage == "toujours"
                   or (args.nivelage == "auto" and (a["sonie"]["etendue_lu"] or 0)
                       > ETENDUE_NIVELAGE_LU))
        if niveler and args.nivelage == "auto":
            plan["raisons"].append(
                f"étendue dynamique très grande ({a['sonie']['etendue_lu']} LU) : "
                "nivelage lent et doux")
        plan["nivelage"] = niveler
        afficher_diagnostic(a, plan)

        if a["saturations"] > 5:
            rapport["alertes"].append(
                f"{a['saturations']} passages saturés à l'enregistrement : le script ne "
                "les répare pas, la distorsion restera audible")
        if infos["sous_titres"] and fmt == "mp4":
            rapport["alertes"].append("les sous-titres de la source ne sont pas repris")
        rapport["analyse"] = {k: v for k, v in a.items() if k not in ("niveaux", "sr")}
        rapport["plan"] = plan
        if args.analyse_seule:
            for alerte in rapport["alertes"]:
                print(f"  ⚠ {alerte}")
            return rapport

        # Le rognage : le son conditionné pour la suite, le son d'origine pour juger
        wav_rogne = travail / "03_origine_rognee.wav"
        rogner(wav_canaux, wav_rogne, plan["debut_s"], plan["fin_s"])
        wav_pret = travail / "03_pret.wav"
        rogner(wav_conditionne, wav_pret, plan["debut_s"], plan["fin_s"])
        wav_canaux.unlink()
        wav_conditionne.unlink()
        duree = plan["fin_s"] - plan["debut_s"]
        print(f"        rognage      {hms(plan['debut_s'])} → {hms(plan['fin_s'])}")

        # Passe 4 — débruitage, seulement s'il le faut
        if plan["debruitage"]:
            wav_propre = debruiter_prudemment(wav_pret, wav_rogne, travail,
                                              plan["debruitage"], args, rapport)
            print(f"  [4/9] débruitage : {rapport['mesures']['debruitage']}")
        else:
            wav_propre = wav_pret
            rapport["mesures"]["debruitage"] = "aucun"
            print("  [4/9] débruitage : aucun")

        # Passe 5 — nivelage, seulement s'il le faut
        if niveler:
            wav_nivele = travail / "05_nivele.wav"
            N.niveler(wav_propre, wav_nivele)
            print("  [5/9] nivelage : lent et doux (fenêtre de 15 s)")
        else:
            wav_nivele = wav_propre
            print("  [5/9] nivelage : aucun, la dynamique est conservée")

        # Passe 6 — normalisation à gain constant
        wav_normalise = travail / "06_normalise.wav"
        plan["cible_lufs"] = round(mettre_a_niveau(
            wav_nivele, wav_normalise, plan["cible_lufs"], args.limiteur_max, rapport), 2)
        m = rapport["mesures"]
        print(f"  [6/9] normalisation : {m['lufs_avant_gain']:.1f} LUFS → gain "
              f"{m['gain_db']:+.1f} dB"
              + (f", {m['retire_aux_cretes_db']:.1f} dB retirés aux seules crêtes"
                 if m["limiteur"] else " (gain seul, aucune crête touchée)"))

        # Passe 7 — fondus
        wav_final = travail / "07_final.wav"
        poser_les_fondus(wav_normalise, wav_final, plan["fondu_debut_s"], plan["fondu_fin_s"])
        print(f"  [7/9] fondus : entrée {plan['fondu_debut_s']:.2f} s, "
              f"sortie {plan['fondu_fin_s']:.2f} s")

        # Passe 8 — encodage
        etiquettes = travail / "etiquettes.txt"
        chapitres = ecrire_etiquettes(etiquettes, infos, plan, args)
        n = plan["canaux_sortie"]
        if fmt == "mp4":
            debit = args.debit or DEBITS_AAC[n] + 32
            piste = travail / "08_piste.m4a"
            encoder_piste(wav_final, piste, "aac48", n, debit, rapport)
            m["image"] = assembler_video(piste, fichier, etiquettes, sortie, infos, plan, args)
            print(f"  [8/9] encodage : MP4, image {m['image']} ; son AAC {debit} kbit/s"
                  + (f" ({m['encodeur']})" if "encodeur" in m else ""))
        else:
            debit = args.debit or (DEBITS_MP3 if fmt == "mp3" else DEBITS_AAC)[n]
            piste = travail / f"08_piste.{fmt}"
            encoder_piste(wav_final, piste, fmt, n, debit, rapport)
            assembler_son(piste, fichier, etiquettes, sortie, fmt,
                          infos["pochette"] and fmt in ("mp3", "m4a"))
            print(f"  [8/9] encodage : {fmt.upper()}"
                  + (" 16 bits, 48 kHz" if fmt == "wav" else f" {debit} kbit/s"
                     + (" constant" if fmt == "mp3" else "") + ", 44,1 kHz")
                  + (f", {chapitres} chapitres" if chapitres else "")
                  + (f" (niveau de l'encodeur compensé de {m['compensation_encodeur_db']:+.1f} dB)"
                     if "compensation_encodeur_db" in m else ""))

        # Passe 9 — contrôle du fichier final
        final = mesurer_ebur128(sortie)
        m["final"] = final
        m["duree_s"] = round(sonder(sortie)["duree"], 2)
        if final["crete_dbtp"] is not None and final["crete_dbtp"] > PLAFOND_FINAL_DBTP:
            rapport["alertes"].append(
                f"crête vraie du fichier final {final['crete_dbtp']} dBTP, au-dessus de "
                f"{PLAFOND_FINAL_DBTP} dBTP")
        if final["lufs"] is not None and abs(final["lufs"] - plan["cible_lufs"]) > 1.0:
            rapport["alertes"].append(
                f"sonie finale {final['lufs']} LUFS, à plus de 1 LU de la cible "
                f"({plan['cible_lufs']})")
        if abs(m["duree_s"] - duree) > 0.2:
            rapport["alertes"].append(
                f"durée inattendue : {m['duree_s']} s au lieu de {duree:.2f} s")
        try:
            import speechmos  # noqa: F401
            avant, sr = _mono(wav_rogne)
            positions = N.choisir_fenetres_parole(avant, sr)
            s_avant = N.score_dnsmos(avant, sr, positions)
            apres, _ = _mono(wav_final)
            s_apres = N.score_dnsmos(apres, sr, positions)
            if s_avant and s_apres:
                rapport["dnsmos"] = {"avant": s_avant, "apres": s_apres}
                if s_apres["sig"] < s_avant["sig"] - N.QC_SIG_BAISSE_MAX:
                    rapport["alertes"].append(
                        f"DNSMOS : la qualité de la voix a baissé ({s_avant['sig']} → "
                        f"{s_apres['sig']}) — ÉCOUTE RECOMMANDÉE")
                print(f"  [9/9] contrôle : voix {s_avant['sig']} → {s_apres['sig']}, "
                      f"fond {s_avant['bak']} → {s_apres['bak']} (DNSMOS, sur 5)")
        except ImportError:
            pass
        print(f"        fichier final : {final['lufs']} LUFS, crête vraie "
              f"{final['crete_dbtp']} dBTP, étendue dynamique {final['etendue_lu']} LU, "
              f"{hms(m['duree_s'])}")

        rapport["duree_traitement_s"] = round(time.time() - debut_chrono, 1)
        (dossier / f"{fichier.stem}_podcast.json").write_text(
            json.dumps(rapport, indent=2, ensure_ascii=False, default=str))
        for alerte in rapport["alertes"]:
            print(f"  ⚠ {alerte}")
        print(f"  ✓ {sortie} ({rapport['duree_traitement_s']:.0f} s)")
        return rapport
    finally:
        if args.garder_travail:
            print(f"  (fichiers intermédiaires conservés : {travail})")
        else:
            shutil.rmtree(travail, ignore_errors=True)


FORMATS_SUR_PLACE = {".mp4": "mp4", ".m4v": "mp4", ".mp3": "mp3", ".m4a": "m4a",
                     ".wav": "wav"}


def traiter_sur_place(fichier, args):
    """Le fichier finalisé prend la place du fichier donné : même nom, même
    durée (rien n'est rogné, pour que sous-titres et fichiers de travail
    restent calés sur lui). Le rapport est écrit à côté, en _finition.json.
    Le fichier n'est remplacé qu'une fois la finition réussie."""
    fichier = Path(fichier)
    fmt = FORMATS_SUR_PLACE.get(fichier.suffix.lower())
    if fmt is None:
        raise RuntimeError(f"la finition sur place ne sait pas écrire un fichier {fichier.suffix}")
    dossier = Path(tempfile.mkdtemp(prefix=".finition_", dir=str(fichier.parent)))
    try:
        reglages = argparse.Namespace(**vars(args))
        reglages.sortie, reglages.format = str(dossier), fmt
        reglages.sans_rognage, reglages.forcer = True, True
        reglages.debut = reglages.fin = None
        rapport = traiter_fichier(fichier, reglages)
        if not rapport or args.analyse_seule:
            return rapport
        fini = Path(rapport["sortie"])
        rapport["sortie"] = str(fichier)
        rapport["sur_place"] = True
        fini.replace(fichier)
        fichier.with_name(f"{fichier.stem}_finition.json").write_text(
            json.dumps(rapport, indent=2, ensure_ascii=False, default=str))
        print(f"  ✓ son finalisé dans {fichier.name}")
        return rapport
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


def instant(texte):
    """« 90 », « 1:30 » ou « 1:02:03.5 » → secondes."""
    try:
        parts = [float(p) for p in texte.strip().split(":")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"instant illisible : {texte}")
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


def principal():
    parseur = argparse.ArgumentParser(
        description="Finition prête à diffuser : silences de début et de fin, bruit de "
                    "fond éventuel, volume à la norme des podcasts, fondus",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parseur.add_argument("entree", help="fichier son ou vidéo, ou dossier")
    parseur.add_argument("-o", "--sortie", help="dossier de sortie (défaut : à côté de la source)")
    parseur.add_argument("--format", choices=["auto", "mp3", "m4a", "wav", "mp4"],
                         default="auto",
                         help="auto : MP4 pour une vidéo, MP3 pour un son")
    parseur.add_argument("--sur-place", action="store_true",
                         help="le fichier finalisé remplace le fichier donné : même nom, "
                              "même durée (rien n'est rogné)")
    parseur.add_argument("--analyse-seule", action="store_true",
                         help="afficher le diagnostic et ce qui serait fait, sans rien écrire")
    parseur.add_argument("--debruitage", choices=["auto", "jamais", "toujours"], default="auto",
                         help="auto : seulement si un bruit de fond régulier est détecté")
    parseur.add_argument("--reduction", type=int, default=None, metavar="DB",
                         help=f"atténuation maximale du fond (défaut {REDUCTION_LEGERE_DB}, "
                              f"{REDUCTION_FORTE_DB} si le fond est fort)")
    parseur.add_argument("--nivelage", choices=["auto", "jamais", "toujours"], default="auto",
                         help=f"auto : seulement si l'étendue dynamique dépasse "
                              f"{ETENDUE_NIVELAGE_LU:.0f} LU")
    parseur.add_argument("--lufs", type=float, default=None,
                         help=f"sonie visée (défaut {N.LUFS_CIBLE_MONO} en mono, "
                              f"{N.LUFS_CIBLE_STEREO} en stéréo)")
    parseur.add_argument("--limiteur-max", type=float, default=LIMITEUR_MAX_DB, metavar="DB",
                         help=f"ce que le limiteur a le droit de retirer aux crêtes pour "
                              f"atteindre la sonie visée (défaut {LIMITEUR_MAX_DB:.0f} dB) ; "
                              f"au-delà, le fichier sort un peu moins fort")
    parseur.add_argument("--canaux", choices=["mono", "stereo"], default="mono",
                         help="mono (défaut) : la norme pour la parole, et un fichier "
                              "deux fois plus léger ; stereo : garder deux canaux")
    parseur.add_argument("--sans-rognage", action="store_true",
                         help="ne pas couper les silences de début et de fin")
    parseur.add_argument("--debut", type=instant, default=None, metavar="MM:SS",
                         help="commencer à cet instant (au lieu du premier son)")
    parseur.add_argument("--fin", type=instant, default=None, metavar="MM:SS",
                         help="finir à cet instant (au lieu du dernier son)")
    parseur.add_argument("--fondu-debut", type=float, default=FONDU_DEBUT_S, metavar="S",
                         help=f"fondu d'entrée, en secondes (défaut {FONDU_DEBUT_S})")
    parseur.add_argument("--fondu-fin", type=float, default=FONDU_FIN_S, metavar="S",
                         help=f"fondu de sortie, en secondes (défaut {FONDU_FIN_S})")
    parseur.add_argument("--fondu-image", action="store_true",
                         help="vidéo : fondu de l'image aussi (l'image est alors réencodée)")
    parseur.add_argument("--debit", type=int, default=None, metavar="KBIT",
                         help="débit du son (défaut 128 en mono, 192 en stéréo pour le MP3)")
    parseur.add_argument("--passe-haut", type=int, default=N.PASSE_HAUT_HZ, metavar="HZ",
                         help=f"passe-haut en Hz, 0 pour désactiver (défaut {N.PASSE_HAUT_HZ})")
    parseur.add_argument("--titre", help="titre inscrit dans le fichier")
    parseur.add_argument("--auteur", help="auteur inscrit dans le fichier")
    parseur.add_argument("--album", help="nom de l'émission inscrit dans le fichier")
    parseur.add_argument("--cpu", action="store_true", help="débruiter sans la carte graphique")
    parseur.add_argument("--forcer", action="store_true", help="refaire même si le résultat existe")
    parseur.add_argument("--garder-travail", action="store_true",
                         help="conserver les fichiers intermédiaires")
    args = parseur.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("ffmpeg introuvable")
        sys.exit(1)
    try:
        import pyloudnorm  # noqa: F401
    except ImportError:
        print("pyloudnorm manquant (pip install pyloudnorm)")
        sys.exit(1)

    entree = Path(args.entree)
    if entree.is_dir():
        fichiers = sorted(f for f in entree.iterdir()
                          if f.suffix.lower() in (N.EXTENSIONS | EXTENSIONS_VIDEO)
                          and not f.stem.endswith(("_podcast", "_nettoye")))
    else:
        fichiers = [entree]
    if not fichiers or not fichiers[0].exists():
        print(f"Aucun fichier à traiter dans {entree}")
        sys.exit(1)

    print(f"─── finaliser.py — {len(fichiers)} fichier(s) ───")
    echecs = []
    for i, f in enumerate(fichiers, 1):
        print(f"[{i}/{len(fichiers)}] {f.name}")
        try:
            if args.sur_place:
                traiter_sur_place(f, args)
            else:
                traiter_fichier(f, args)
        except Exception as e:
            echecs.append((f.name, str(e)))
            print(f"  ✗ échec : {e}")
    if echecs:
        print(f"\n{len(echecs)} échec(s) :")
        for nom, err in echecs:
            print(f"  - {nom} : {err.splitlines()[0] if err else err}")
        sys.exit(1)


if __name__ == "__main__":
    principal()
