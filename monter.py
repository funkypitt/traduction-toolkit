#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monter.py — Montage vidéo et audio au stabilo
==============================================
Transcrit une vidéo ou un enregistrement, affiche le texte, et laisse passer
au stabilo les passages à garder. Le montage est ensuite produit à partir du
texte surligné, avec un fondu à chaque coupe.

    ~/miniconda3/envs/interview/bin/python monter.py
    ~/miniconda3/envs/interview/bin/python monter.py entretien.mp4
    → ouvre http://127.0.0.1:5006

Ce que fait l'outil :
  1. WhisperX (celui de resumer.py) → transcription + horodatage mot par mot
  2. Page locale                    → texte à surligner, lecteur synchronisé
  3. Points de coupe                → placés dans le creux le plus calme autour
                                      de chaque passage (jamais au ras d'un mot)
  4. ffmpeg                         → montage vidéo (MP4) ou audio (MP3/M4A/WAV)

La transcription tourne dans un sous-processus : le verrou GPU du toolkit est
tenu jusqu'à la fin du processus qui le prend, il ne doit donc jamais être
pris par le serveur lui-même.

Aucune dépendance nouvelle : Flask, numpy et soundfile sont dans l'env
« interview ».
"""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from collections import deque
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR  = Path(os.path.abspath(__file__)).parent
INPUT_DIR   = Path(os.environ.get("TRADUCTION_INPUT_DIR",  str(SCRIPT_DIR / "input")))
WORK_DIR    = Path(os.environ.get("TRADUCTION_WORK_DIR",   str(SCRIPT_DIR / "work-files")))
OUTPUT_DIR  = Path(os.environ.get("TRADUCTION_OUTPUT_DIR", str(SCRIPT_DIR / "output")))
MONTAGE_DIR = WORK_DIR / "montage"

HOST = "127.0.0.1"
PORT = 5006

# Un trou d'au moins SEUIL_PAUSE secondes entre deux mots devient un jeton
# « sans paroles », visible et surlignable comme un mot (musique, silence, ou
# passage que WhisperX a laissé tomber).
SEUIL_PAUSE = 2.0
# Au tout début et à la toute fin du fichier, le repère apparaît dès SEUIL_BORD :
# c'est là que traînent les bruits de micro qu'on veut pouvoir retirer.
SEUIL_BORD = 0.3
VERSION_JETONS = 2          # 2 : repères de début et de fin

FONDU_DEFAUT = 0.5          # secondes
FONDU_MAX = 3.0
# Un raccord qui retire moins que ça (un mot, une hésitation) se fait en coupe
# franche à l'image : un fondu au noir y ferait clignoter l'écran.
SEUIL_FONDU_IMAGE = 1.0
ANTI_CLIC = 0.01            # fondu minimal, même avec « fondu = 0 »

EXT_VIDEO = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv",
             ".mpg", ".mpeg", ".ts", ".mts", ".3gp", ".ogv"}
EXT_AUDIO = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".oga", ".opus", ".aac",
             ".wma", ".aiff", ".aif", ".amr", ".mka"}

# Ce que le navigateur lit tel quel ; le reste passe par une copie de travail.
LECTURE_DIRECTE_VIDEO = {".mp4", ".m4v", ".mov", ".webm"}
CODECS_VIDEO_NAVIGATEUR = {"h264", "vp8", "vp9", "av1"}
CODECS_AUDIO_NAVIGATEUR = {"aac", "mp3", "opus", "vorbis", "flac"}
# Pas le MP3 : sans table d'index, le navigateur estime la position et le
# curseur dérive par rapport au texte.
LECTURE_DIRECTE_AUDIO = {".m4a", ".ogg", ".oga", ".opus", ".wav", ".flac"}

LANGUES = [("", "Détection automatique"), ("fr", "Français"), ("en", "Anglais"),
           ("de", "Allemand"), ("es", "Espagnol"), ("it", "Italien"),
           ("pt", "Portugais"), ("nl", "Néerlandais"), ("ru", "Russe"),
           ("pl", "Polonais"), ("ja", "Japonais"), ("zh", "Chinois")]


def _resolve_python():
    """Interpréteur Python du toolkit (même règle que gui.py)."""
    cands = [os.environ.get("TRADUCTION_PYTHON")]
    for env in ("interview", "traduction"):
        cands.append(os.path.expanduser(f"~/miniconda3/envs/{env}/bin/python"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return sys.executable


PYTHON_BIN = _resolve_python()


# ═══════════════════════════════════════════════════════════════════════════════
# OUTILS
# ═══════════════════════════════════════════════════════════════════════════════

def lire_json(chemin, defaut=None):
    try:
        with open(chemin, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return defaut


def ecrire_json(chemin, donnees):
    """Écriture atomique : jamais de fichier à moitié écrit."""
    chemin = Path(chemin)
    tmp = chemin.with_name(chemin.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False)
    os.replace(tmp, chemin)


def sonder(chemin: str) -> dict:
    """ffprobe → {type, duree, video:{…}, audio:{…}} ou {} si illisible."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:stream=codec_type,codec_name,width,height,"
           "r_frame_rate,sample_rate,channels:stream_disposition=attached_pic",
           "-of", "json", chemin]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        brut = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}
    video, audio = None, None
    for s in brut.get("streams", []):
        genre = s.get("codec_type")
        if genre == "video" and video is None:
            # La pochette d'un MP3 est une « piste vidéo » : on l'ignore.
            if (s.get("disposition") or {}).get("attached_pic"):
                continue
            ips = 0.0
            try:
                num, den = (s.get("r_frame_rate") or "0/1").split("/")
                ips = float(num) / float(den) if float(den) else 0.0
            except ValueError:
                pass
            video = {"codec": s.get("codec_name", ""), "largeur": s.get("width", 0),
                     "hauteur": s.get("height", 0), "ips": round(ips, 3)}
        elif genre == "audio" and audio is None:
            audio = {"codec": s.get("codec_name", ""),
                     "frequence": int(s.get("sample_rate") or 0) or 48000,
                     "canaux": int(s.get("channels") or 0) or 2}
    try:
        duree = float(brut.get("format", {}).get("duration") or 0)
    except ValueError:
        duree = 0.0
    if audio is None and video is None:
        return {}
    return {"type": "video" if video else "audio", "duree": duree,
            "video": video, "audio": audio}


def _slug(nom: str) -> str:
    s = re.sub(r"[^\w.-]+", "_", nom, flags=re.UNICODE).strip("_.")
    return (s or "fichier")[:60]


def id_source(chemin: str) -> str:
    st = os.stat(chemin)
    cle = f"{os.path.abspath(chemin)}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(cle.encode("utf-8")).hexdigest()[:10]


def dossier_du_projet(pid: str):
    if not re.fullmatch(r"[0-9a-f]{10}", pid or ""):
        return None
    if MONTAGE_DIR.is_dir():
        for d in MONTAGE_DIR.iterdir():
            if d.is_dir() and d.name.endswith("-" + pid):
                return d
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSCRIPTION (sous-processus)
# ═══════════════════════════════════════════════════════════════════════════════

def construire_jetons(segments, duree: float) -> list:
    """Segments WhisperX → jetons [texte, début, fin, phrase].
    Un jeton dont le texte est None est un passage sans paroles."""
    from clipper import interpolate_word_times, distribute_words_uniformly

    mots = []
    for k, seg in enumerate(segments):
        ws = interpolate_word_times(seg.words, seg.start, seg.end)
        if not ws:
            ws = distribute_words_uniformly(seg.text, seg.start, seg.end)
        for w in ws:
            texte = (w.get("word") or "").strip()
            if texte:
                mots.append([texte, float(w["start"]), float(w["end"]), k])

    return jetons_depuis_mots(mots, duree)


def jetons_depuis_mots(mots: list, duree: float) -> list:
    """Mots [texte, début, fin, phrase] → jetons, passages sans paroles compris."""
    # Horodatages strictement ordonnés, sans chevauchement
    prec = 0.0
    for m in mots:
        m[1] = max(m[1], prec)
        prec = m[1]
    for i, m in enumerate(mots):
        m[2] = max(m[2], m[1] + 0.01)
        if i + 1 < len(mots) and m[2] > mots[i + 1][1]:
            m[2] = max(m[1], mots[i + 1][1])
    if duree > 0:
        for m in mots:
            m[1] = min(m[1], duree)
            m[2] = min(m[2], duree)

    jetons = []
    curseur = 0.0
    for m in mots:
        if m[1] - curseur >= SEUIL_PAUSE:
            jetons.append([None, round(curseur, 3), round(m[1], 3), -1])
        jetons.append([m[0], round(m[1], 3), round(m[2], 3), m[3]])
        curseur = m[2]
    if duree - curseur >= SEUIL_PAUSE:
        jetons.append([None, round(curseur, 3), round(duree, 3), -1])
    return poser_les_bords(jetons, duree)[0]


def poser_les_bords(jetons: list, duree: float) -> tuple:
    """Ajoute le repère sans paroles du début et celui de la fin quand ils
    manquent. Retourne (jetons, décalage des numéros : 0 ou 1)."""
    decalage = 0
    if jetons and jetons[0][0] is not None and jetons[0][1] >= SEUIL_BORD:
        jetons = [[None, 0.0, jetons[0][1], -1]] + jetons
        decalage = 1
    if jetons and jetons[-1][0] is not None and duree - jetons[-1][2] >= SEUIL_BORD:
        jetons = jetons + [[None, jetons[-1][2], round(duree, 3), -1]]
    return jetons, decalage


def tache_transcrire(dossier: str, langue: str):
    """Point d'entrée du sous-processus de transcription."""
    dossier = Path(dossier)
    projet = lire_json(dossier / "projet.json")
    if not projet:
        print(f"❌ Projet illisible : {dossier}")
        sys.exit(1)

    if projet.get("reprise"):
        try:
            jetons = reprendre_texte(dossier, projet, projet["reprise"])
        except Exception as ex:
            jetons = None
            print(f"⚠️  Le texte déjà produit n'a pas pu être repris ({ex}).")
        if jetons:
            ecrire_json(dossier / "transcription.json",
                        {"version": VERSION_JETONS,
                         "langue": projet["reprise"].get("langue", ""),
                         "origine": projet["reprise"]["genre"],
                         "estime": projet["reprise"]["genre"] != "origine",
                         "jetons": jetons})
            n_mots = sum(1 for j in jetons if j[0] is not None)
            print(f"   ✅ Texte prêt : {n_mots} mots, repris sans nouvelle transcription")
            return
        print("🎙️  Le son est transcrit à la place.")

    from resumer import extract_audio, transcribe_whisperx

    wav = dossier / "audio16k.wav"
    if not wav.exists():
        tmp = dossier / "audio16k.tmp.wav"
        extract_audio(projet["source"], str(tmp))
        os.replace(tmp, wav)

    try:
        segments, detectee = transcribe_whisperx(str(wav), langue or None)
    except ValueError as ex:
        # WhisperX n'a pas de modèle d'alignement pour toutes les langues, et
        # la détection automatique se trompe parfois (musique en ouverture).
        m = re.search(r"align-model for language: (\S+)", str(ex))
        if not m:
            raise
        if langue:
            print(f"❌ La langue « {m.group(1)} » ne peut pas être horodatée "
                  "mot par mot. Choisissez-en une autre.")
        else:
            print(f"❌ La langue n'a pas été reconnue (« {m.group(1)} » "
                  "trouvé). Choisissez la langue parlée, puis recommencez.")
        sys.exit(1)
    if not segments:
        print("❌ Aucune parole trouvée dans ce fichier.")
        sys.exit(1)

    jetons = construire_jetons(segments, projet.get("duree") or 0.0)
    ecrire_json(dossier / "transcription.json",
                {"version": VERSION_JETONS, "langue": detectee, "jetons": jetons})
    n_mots = sum(1 for j in jetons if j[0] is not None)
    print(f"   ✅ Texte prêt : {n_mots} mots")


# ═══════════════════════════════════════════════════════════════════════════════
# REPRISE D'UN TEXTE DÉJÀ PRODUIT (sous-titrage, doublage)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Une vidéo sortie de traduire.py ou de doubler.py a déjà son texte, et c'est le
# texte traduit qu'on veut lire en montant : celui des sous-titres, ou celui
# que dit la voix doublée. Il est repris des fichiers de travail, sans WhisperX.
#
# Ce qui est mesuré : les moments où la voix parle (horodatage mot par mot de
# la voix d'origine pour des sous-titres ; place réelle de chaque clip de
# doublage, retrouvée dans le son du fichier, et pauses de ce clip).
# Ce qui est estimé : la place de chaque mot traduit, réparti sur ces moments à
# la mesure de sa longueur, les fins de phrase calées sur les pauses.

RE_LANGUE = r"[a-z]{2,3}"
PAUSE_PAROLE = 0.15         # en dessous, un creux fait partie de la parole
PAUSE_REPERE = 0.25         # une pause sert de repère aux fins de phrase à partir de là
CORRELATION_MIN = 0.35      # en dessous, le clip n'est pas dans le son du fichier
RETROUVES_MIN = 0.5         # part des clips à retrouver pour croire au rapprochement
# Autour d'un mot dont la place est estimée, le point calme se cherche aussi
# un peu à l'intérieur du mot.
JEU_ESTIME = 0.25
# Doublage : la voix d'origine s'entend seule un instant avant la voix doublée,
# et un instant après. Ces instants vont avec la phrase ; au-delà, ce qui reste
# de la voix d'origine devient un passage à part, qu'on garde ou qu'on retire.
AVANT_DOUBLAGE = 3.0
APRES_DOUBLAGE = 1.5
FIN_DE_PHRASE = ".!?…"
PONCTUATION = FIN_DE_PHRASE + ",;:—–"
FERMANTS = "»\"”’')]   "


def lire_segments(chemin) -> list:
    """segments.json de traduire.py ou de doubler.py."""
    data = lire_json(chemin)
    if isinstance(data, dict):
        data = data.get("segments")
    segments = []
    for d in data or []:
        try:
            debut, fin = float(d["start"]), float(d["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if fin <= debut:
            continue
        segments.append({
            "index": d.get("index"), "d": debut, "f": fin,
            "source": (d.get("text") or "").strip(),
            "traduit": (d.get("text_tgt") or d.get("text_fr") or "").strip(),
            "dit": (d.get("text_adapted") or d.get("text_tgt")
                    or d.get("text_fr") or d.get("text") or "").strip(),
            "mots": d.get("words") or [],
            "locuteur": d.get("speaker") or ""})
    segments.sort(key=lambda g: g["d"])
    return segments


def lire_srt(chemin) -> list:
    """Sous-titres SRT → [(début, fin, texte)]."""
    try:
        brut = Path(chemin).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    temps = r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})"
    repliques = []
    for bloc in re.split(r"\n\s*\n", brut.replace("\r", "")):
        m = re.search(temps + r"\s*-->\s*" + temps + r"[^\n]*\n(.*)", bloc, re.S)
        if not m:
            continue
        g = m.groups()
        debut = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")) / 1000
        fin = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")) / 1000
        texte = " ".join(re.sub(r"<[^>]+>|\{\\[^}]*\}", "", g[8]).split())
        if texte and fin > debut:
            repliques.append((debut, fin, texte))
    repliques.sort()
    return repliques


def chercher_texte_existant(chemin: str, duree: float):
    """Les fichiers de travail qui vont avec ce fichier, d'après son nom :
    {genre, segments, srt, clips, langue}, ou None. Genres : « doublage »
    (vidéo de doubler.py), « sous-titres » (vidéo de traduire.py), « origine »
    (la vidéo de départ : sa transcription existe déjà, mot par mot)."""
    nom = Path(chemin).stem
    trouve = None
    m = re.fullmatch(rf"(.+)_dubbed_({RE_LANGUE})", nom)
    if m:
        travail = WORK_DIR / f"{m.group(1)}_dubbing_work"
        if (travail / "segments.json").is_file():
            trouve = {"genre": "doublage", "segments": str(travail / "segments.json"),
                      "clips": str(travail), "langue": m.group(2)}
    m = re.fullmatch(rf"(.+)_({RE_LANGUE})", nom)
    if m and not trouve:
        base = m.group(1)
        segments = WORK_DIR / base / f"{base}_segments.json"
        if segments.is_file():
            srt = [c for c in (Path(chemin).with_suffix(".srt"), OUTPUT_DIR / f"{nom}.srt")
                   if c.is_file()]
            trouve = {"genre": "sous-titres", "segments": str(segments),
                      "srt": str(srt[0]) if srt else "", "langue": m.group(2)}
    if not trouve:
        for segments in (WORK_DIR / nom / f"{nom}_segments.json",
                         WORK_DIR / f"{nom}_dubbing_work" / "segments.json"):
            if segments.is_file():
                trouve = {"genre": "origine", "segments": str(segments), "langue": ""}
                break
    if not trouve:
        return None

    # Le même nom ne suffit pas : le texte doit tenir dans la durée du fichier
    segments = lire_segments(trouve["segments"])
    if not segments or (duree > 0 and segments[-1]["f"] > duree + 1.0):
        return None
    if trouve["genre"] == "origine":
        if not any(g["mots"] for g in segments):
            return None
    elif not any(g["dit" if trouve["genre"] == "doublage" else "traduit"] for g in segments):
        return None
    return trouve


def moments_de_parole(x, sr: int) -> list:
    """Signal d'une voix seule → [[début, fin], …] en secondes : les moments
    où elle parle, séparés par ses pauses."""
    import numpy as np

    total = len(x) / float(sr)
    pas = max(1, int(sr * 0.010))
    n = len(x) // pas
    if n < 5:
        return [[0.0, total]]
    e = np.sqrt((x[:n * pas].reshape(n, pas) ** 2).mean(axis=1))
    fort = float(np.percentile(e, 95))
    if fort <= 1e-5:
        return [[0.0, total]]
    parle = e > max(fort * 0.03, 1e-4)          # 30 dB sous les passages forts
    moments = []
    debut = None
    for i in range(n + 1):
        actif = i < n and bool(parle[i])
        if actif and debut is None:
            debut = i
        elif not actif and debut is not None:
            a, b = debut * pas / sr, i * pas / sr
            if moments and a - moments[-1][1] < PAUSE_PAROLE:
                moments[-1][1] = b
            elif b - a >= 0.03 or not moments:
                moments.append([a, b])
            debut = None
    return moments or [[0.0, total]]


def reunir_les_mots(mots: list) -> list:
    """Mots horodatés d'une voix [(début, fin), …] → moments de parole."""
    moments = []
    for a, b in sorted(mots):
        if b <= a:
            continue
        if moments and a - moments[-1][1] < PAUSE_PAROLE:
            moments[-1][1] = max(moments[-1][1], b)
        else:
            moments.append([a, b])
    return moments


def mots_du_texte(texte: str) -> list:
    """Les mots d'un texte ; une ponctuation isolée par une espace (« mot ? »,
    guillemets français) reste avec son mot."""
    mots, ouvrant = [], ""
    for m in texte.split():
        if not any(c.isalnum() for c in m):
            if m[0] in "«“([¿¡" or not mots:
                ouvrant += m + "\u00a0"
            else:
                mots[-1] += "\u00a0" + m
            continue
        mots.append(ouvrant + m)
        ouvrant = ""
    return mots


def _force(mot: str) -> int:
    """2 : le mot finit une phrase ; 1 : il porte une autre ponctuation."""
    nu = mot.rstrip(FERMANTS)
    if nu and nu[-1] in FIN_DE_PHRASE:
        return 2
    return 1 if nu and nu[-1] in PONCTUATION else 0


def _etaler(mots: list, poids: list, moments: list) -> list:
    """Répartit des mots sur le temps parlé de quelques moments."""
    parle = sum(b - a for a, b in moments)
    total = float(sum(poids))
    if parle <= 0 or total <= 0:
        return []

    def instant(position, fin_de_mot):
        # position dans le temps parlé → instant ; à la frontière de deux
        # moments, un début de mot va au moment suivant, une fin au précédent
        reste = position
        for k, (a, b) in enumerate(moments):
            longueur = b - a
            if reste < longueur or (fin_de_mot and reste <= longueur + 1e-9) \
                    or k == len(moments) - 1:
                return a + min(max(reste, 0.0), longueur)
            reste -= longueur
        return moments[-1][1]

    places, cumul = [], 0.0
    for mot, w in zip(mots, poids):
        d = instant(cumul / total * parle, False)
        cumul += w
        f = instant(cumul / total * parle, True)
        places.append([mot, d, max(f, d)])
    return places


def repartir(mots: list, moments: list) -> list:
    """Place des mots d'un texte sur les moments où la voix parle. Chaque mot
    reçoit une part du temps parlé à la mesure de sa longueur ; les pauses de
    la voix sont attribuées aux ponctuations les plus proches, et le texte est
    réparti entre ces repères. Retourne [[mot, début, fin], …]."""
    moments = [[a, b] for a, b in moments if b > a]
    if not mots or not moments:
        return []
    poids = [len(m) + 1 for m in mots]
    total = float(sum(poids))
    parle = sum(b - a for a, b in moments)

    # Frontières de mots qui portent une ponctuation : position attendue dans
    # le temps parlé
    frontieres, cumul = [], 0.0
    for k in range(1, len(mots)):
        cumul += poids[k - 1]
        force = _force(mots[k - 1])
        if force:
            frontieres.append((k, cumul / total * parle, force))

    # Pauses de la voix, les plus longues d'abord
    pauses, cumul = [], 0.0
    for j in range(len(moments) - 1):
        cumul += moments[j][1] - moments[j][0]
        longueur = moments[j + 1][0] - moments[j][1]
        if longueur >= PAUSE_REPERE:
            pauses.append((longueur, j, cumul))
    pauses.sort(reverse=True)

    tolerance = max(1.0, 0.2 * parle)
    reperes = []                    # (frontière de mots, pause) : paires retenues
    for _, j, position in pauses:
        mieux = None
        for k, attendue, force in frontieres:
            ecart = abs(attendue - position)
            if ecart > tolerance or any(k == k2 or (k < k2) != (j < j2)
                                        for k2, j2 in reperes):
                continue
            note = ecart + (0.0 if force == 2 else 0.6)
            if mieux is None or note < mieux[0]:
                mieux = (note, k)
        if mieux:
            reperes.append((mieux[1], j))
    reperes.sort()

    places = []
    k0, j0 = 0, 0
    for k, j in reperes + [(len(mots), len(moments) - 1)]:
        places += _etaler(mots[k0:k], poids[k0:k], moments[j0:j + 1])
        k0, j0 = k, j + 1
    return places


def extraire_son(source: str, wav: Path):
    """Le son du fichier en WAV 16 kHz mono (le même que pour une transcription)."""
    if wav.exists():
        return
    tmp = wav.with_name("audio16k.tmp.wav")
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", source, "-vn",
                        "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", str(tmp)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError("le son du fichier n'a pas pu être lu")
    os.replace(tmp, wav)


def lire_clip(chemin: Path, sr: int):
    """Un clip de doublage, en mono, à la fréquence du son de travail."""
    import numpy as np
    import soundfile as sf
    from math import gcd
    from scipy.signal import resample_poly

    x, f = sf.read(str(chemin), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if f != sr and len(x):
        g = gcd(int(f), int(sr))
        x = resample_poly(x, sr // g, int(f) // g).astype(np.float32)
    return x


def situer_clip(son, clip, sr: int, de: float, a: float) -> tuple:
    """Cherche le clip dans le son du fichier, entre `de` et `a` secondes.
    Retourne (début du clip en secondes, corrélation de 0 à 1)."""
    import numpy as np
    from scipy.signal import fftconvolve

    i0 = max(0, int(de * sr))
    zone = son[i0:min(len(son), int(a * sr) + len(clip))]
    if len(clip) > len(zone):
        clip = clip[:len(zone)]             # clip coupé par la fin du fichier
    if len(clip) < sr * 0.2:
        return de, 0.0
    croise = fftconvolve(zone, clip[::-1], mode="valid")
    energie = fftconvolve(zone * zone, np.ones(len(clip), dtype=np.float32), mode="valid")
    norme = np.sqrt(np.maximum(energie, 1e-9) * max(float((clip * clip).sum()), 1e-9))
    note = croise / norme
    k = int(np.argmax(note))
    return (i0 + k) / float(sr), float(note[k])


def _phrases(mots: list) -> list:
    """Numérote les phrases : mots [texte, début, fin, locuteur] → même liste
    avec, à la place du locuteur, le numéro de la phrase."""
    numero, prec = 0, None
    for k, m in enumerate(mots):
        if k and (m[3] != prec or _force(mots[k - 1][0]) == 2):
            numero += 1
        prec = m[3]
        m[3] = numero
    return mots


def jetons_du_doublage(dossier: Path, projet: dict, reprise: dict) -> list:
    import numpy as np
    import soundfile as sf

    segments = [g for g in lire_segments(reprise["segments"]) if g["dit"]]
    son, sr = sf.read(str(dossier / "audio16k.wav"), dtype="float32")
    if son.ndim > 1:
        son = son.mean(axis=1)
    travail = Path(reprise.get("clips") or "")

    mots, cherches, retrouves, fin_prec = [], 0, 0, 0.0
    for g in segments:
        clip = None
        for sous_dossier in ("tts_normalized", "tts_clips"):
            c = travail / sous_dossier / f"seg_{int(g['index'] or 0):04d}.wav"
            if c.is_file():
                clip = c
                break
        moments = None
        if clip is not None:
            x = lire_clip(clip, sr)
            if len(x) >= sr * 0.2:
                cherches += 1
                # Le mixage déplace les clips : un peu plus tard quand le
                # précédent déborde, bien plus tôt quand il colle la suite
                # d'une phrase au clip d'avant.
                de = max(0.0, min(g["d"] - 5.0, fin_prec - 1.0))
                debut, note = situer_clip(son, x, sr, max(de, g["d"] - 60.0), g["f"] + 5.0)
                if note >= CORRELATION_MIN:
                    retrouves += 1
                    moments = [[debut + a, debut + b] for a, b in moments_de_parole(x, sr)]
                    fin_prec = moments[-1][1]
        if moments is None:
            moments = [[g["d"], g["f"]]]
        places = repartir(mots_du_texte(g["dit"]), moments)
        if not places:
            continue
        if 0 < places[0][1] - g["d"] <= AVANT_DOUBLAGE:
            places[0][1] = g["d"]
        if 0 < g["f"] - places[-1][2] <= APRES_DOUBLAGE:
            places[-1][2] = g["f"]
        mots += [[m, d, f, g["locuteur"]] for m, d, f in places]

    if cherches and retrouves < cherches * RETROUVES_MIN:
        raise RuntimeError(f"le doublage n'est pas celui de ce fichier : {retrouves} "
                           f"clip(s) retrouvé(s) dans le son sur {cherches}")
    if cherches:
        print(f"   📍 {retrouves} clip(s) de doublage sur {cherches} retrouvé(s) dans le son")
    else:
        print("   ⚠️  Clips de doublage absents : le texte est placé d'après "
              "l'horodatage de la voix d'origine")
    return jetons_depuis_mots(_phrases(mots), projet.get("duree") or 0.0)


def jetons_des_sous_titres(projet: dict, reprise: dict) -> list:
    segments = lire_segments(reprise["segments"])
    repliques = lire_srt(reprise["srt"]) if reprise.get("srt") else []
    if not repliques:
        repliques = [(g["d"], g["f"], g["traduit"]) for g in segments if g["traduit"]]

    # Chaque mot de la voix d'origine va à la réplique qui le contient, sinon
    # à la plus proche (ce qui n'a pas été traduit n'est à aucune réplique)
    voix = []
    for g in segments:
        if not g["traduit"]:
            continue
        for w in g["mots"]:
            try:
                voix.append((float(w["start"]), float(w["end"])))
            except (KeyError, TypeError, ValueError):
                continue
    voix.sort()
    parts = [[] for _ in repliques]
    r = 0
    for a, b in voix:
        milieu = (a + b) / 2.0
        while r + 1 < len(repliques) and milieu >= repliques[r][1] and \
                abs(milieu - repliques[r + 1][0]) <= abs(milieu - repliques[r][1]):
            r += 1
        if repliques:
            parts[r].append((a, b))

    mots = []
    for (debut, fin, texte), part in zip(repliques, parts):
        moments = reunir_les_mots(part) or [[debut, fin]]
        mots += [[m, d, f, ""] for m, d, f in repartir(mots_du_texte(texte), moments)]
    return jetons_depuis_mots(_phrases(mots), projet.get("duree") or 0.0)


def jetons_d_origine(projet: dict, reprise: dict) -> list:
    from types import SimpleNamespace

    segments = [SimpleNamespace(words=g["mots"], start=g["d"], end=g["f"], text=g["source"])
                for g in lire_segments(reprise["segments"]) if g["mots"] or g["source"]]
    return construire_jetons(segments, projet.get("duree") or 0.0)


def reprendre_texte(dossier: Path, projet: dict, reprise: dict) -> list:
    """Les jetons du projet, à partir de ce que traduire.py ou doubler.py ont
    laissé. Le son de travail est extrait au passage : le calcul des points de
    coupe en a besoin."""
    noms = {"doublage": "texte du doublage", "sous-titres": "texte des sous-titres",
            "origine": "transcription déjà faite"}
    print(f"📄 Reprise : {noms.get(reprise['genre'], reprise['genre'])}")
    extraire_son(projet["source"], dossier / "audio16k.wav")
    if reprise["genre"] == "doublage":
        return jetons_du_doublage(dossier, projet, reprise)
    if reprise["genre"] == "sous-titres":
        return jetons_des_sous_titres(projet, reprise)
    return jetons_d_origine(projet, reprise)


def plages_depuis_temps(jetons: list, temps: list) -> list:
    """Un surlignage donné en secondes → plages de jetons (quand le texte
    change, ce qui était surligné le reste)."""
    choisis = []
    for i, j in enumerate(jetons):
        milieu = (j[1] + j[2]) / 2.0
        if any(a <= milieu <= b for a, b in temps):
            choisis.append([i, i])
    return [list(p) for p in plages_propres(choisis, len(jetons))]


# ═══════════════════════════════════════════════════════════════════════════════
# POINTS DE COUPE
# ═══════════════════════════════════════════════════════════════════════════════

def marges(fondu: float) -> tuple:
    """(marge visée avant, marge visée après, recherche maximale)."""
    avant = min(max(fondu * 0.6, 0.15), 0.5)
    apres = min(max(fondu * 0.8, 0.25), 0.6)
    return avant, apres, max(avant, apres) + 0.25


def point_calme(son, a: float, b: float, vise: float) -> float:
    """Instant le plus calme de [a, b] le plus proche de `vise`.
    Dans un vrai silence, tous les instants se valent : on tombe sur `vise`.
    Dans une parole continue, on tombe dans le creux entre deux mots."""
    import numpy as np

    vise = min(max(vise, a), b)
    if b - a < 0.03 or son is None:
        return vise
    sr = son.samplerate
    pas = max(1, int(sr * 0.010))
    debut = max(0, int((a - 0.015) * sr))
    fin = min(son.frames, int((b + 0.015) * sr))
    if fin - debut < pas * 3:
        return vise
    son.seek(debut)
    x = son.read(fin - debut, dtype="float32", always_2d=True).mean(axis=1)
    n = len(x) // pas
    if n < 3:
        return vise
    e = np.sqrt((x[:n * pas].reshape(n, pas) ** 2).mean(axis=1) + 1e-12)
    e = np.convolve(e, np.ones(3) / 3.0, mode="same")
    t = (debut + (np.arange(n) + 0.5) * pas) / sr
    dans = (t >= a) & (t <= b)
    if not dans.any():
        return vise
    seuil = max(float(e[dans].min()) * 2.0, 1e-4)
    calmes = dans & (e <= seuil)
    if not calmes.any():
        return vise
    tc = t[calmes]
    return float(tc[int(np.argmin(np.abs(tc - vise)))])


def plages_propres(plages: list, n: int) -> list:
    """Plages de jetons triées, bornées, et réunies quand elles se touchent."""
    brutes = []
    for p in plages:
        try:
            i, j = int(p[0]), int(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        i, j = max(0, min(i, j)), min(n - 1, max(i, j))
        if i <= j:
            brutes.append((i, j))
    propres = []
    for i, j in sorted(brutes):
        if propres and i <= propres[-1][1] + 1:
            propres[-1] = (propres[-1][0], max(j, propres[-1][1]))
        else:
            propres.append((i, j))
    return propres


def complement(propres: list, n: int) -> list:
    """Tout ce qui n'est pas dans les plages."""
    reste, debut = [], 0
    for i, j in propres:
        if i > debut:
            reste.append((debut, i - 1))
        debut = j + 1
    if debut < n:
        reste.append((debut, n - 1))
    return reste


def calculer_coupes(dossier: Path, jetons: list, plages: list, fondu: float,
                    duree: float, inverse: bool = False, estime: bool = False) -> list:
    """Plages de jetons [i, j] → morceaux gardés {d, f, fi, fo, vi, vo, plage},
    en secondes. Avec `inverse`, les plages sont ce qu'on retire : on garde tout
    le reste, du début du fichier à sa fin. Avec `estime`, la place des mots
    n'est pas mesurée (texte repris d'une traduction) : le point calme se
    cherche des deux côtés de la frontière."""
    n = len(jetons)
    propres = plages_propres(plages, n)
    if inverse:
        propres = complement(propres, n)
    if not propres:
        return []

    fin_media = duree if duree > 0 else jetons[-1][2]
    cible_av, cible_ap, portee = marges(fondu)
    # L'horodatage d'un mot est juste à quelques centièmes près : en gardant, on
    # laisse déborder d'autant sur le voisin pour ne pas rogner le mot gardé ;
    # en coupant, on ne déborde pas, pour ne rien laisser du mot retiré.
    debord = 0.0 if inverse else 0.04
    jeu = JEU_ESTIME if estime else 0.0

    son = None
    wav = dossier / "audio16k.wav"
    if wav.exists():
        try:
            import soundfile as sf
            son = sf.SoundFile(str(wav))
        except Exception:
            son = None

    coupes = []
    try:
        for i, j in propres:
            d_brut, f_brut = jetons[i][1], jetons[j][2]
            # Mots voisins (un passage sans paroles ne borne rien)
            k = i - 1
            while k >= 0 and jetons[k][0] is None:
                k -= 1
            fin_prec = jetons[k][2] if k >= 0 else 0.0
            k = j + 1
            while k < n and jetons[k][0] is None:
                k += 1
            deb_suiv = jetons[k][1] if k < n else fin_media

            a = max(0.0, fin_prec - debord - jeu, d_brut - portee)
            d = point_calme(son, min(a, d_brut), min(d_brut + jeu, f_brut),
                            d_brut - cible_av)
            b = min(fin_media, deb_suiv + debord + jeu, f_brut + portee)
            f = point_calme(son, max(f_brut - jeu, d), max(b, f_brut),
                            f_brut + cible_ap)
            if inverse and i == 0:
                d = 0.0
            if inverse and j == n - 1:
                f = fin_media
            if coupes and d < coupes[-1]["f"]:
                # Ce qui est retiré entre les deux est trop court pour deux
                # marges : on coupe une seule fois, en son milieu.
                milieu = (jetons[coupes[-1]["plage"][1] + 1][1] + jetons[i - 1][2]) / 2.0
                milieu = min(max(milieu, coupes[-1]["d"] + 0.05), f)
                coupes[-1]["f"] = d = milieu
            if f - d < 0.05:
                continue

            if fondu > 0:
                fi = min(max(d_brut - d, 0.03), fondu)
                fo = min(max(f - f_brut, 0.03), fondu)
            else:
                fi = fo = ANTI_CLIC

            coupes.append({"d": d, "f": f, "fi": fi, "fo": fo, "plage": [i, j]})
    finally:
        if son is not None:
            son.close()

    for k, c in enumerate(coupes):
        c["vi"] = k == 0 or c["d"] - coupes[k - 1]["f"] >= SEUIL_FONDU_IMAGE
        c["vo"] = k == len(coupes) - 1 or coupes[k + 1]["d"] - c["f"] >= SEUIL_FONDU_IMAGE
    for c in coupes:
        moitie = (c["f"] - c["d"]) / 2.0
        c["d"], c["f"] = round(c["d"], 3), round(c["f"], 3)
        c["fi"] = round(min(c["fi"], moitie), 3)
        c["fo"] = round(min(c["fo"], moitie), 3)
    return coupes


# ═══════════════════════════════════════════════════════════════════════════════
# PRODUCTION DU MONTAGE
# ═══════════════════════════════════════════════════════════════════════════════

class Annule(Exception):
    pass


def chemin_de_sortie(source: str, ext: str) -> Path:
    """Un nom libre pour le montage ET ses deux journaux : un même nom ne sert
    qu'à une production (montage.mp4, montage.txt, montage.edl)."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = Path(source).stem
    k = 1
    while True:
        nom = f"{base}_montage" + (f"-{k}" if k > 1 else "")
        if not any((OUTPUT_DIR / f"{nom}.{e}").exists()
                   for e in ("mp4", "mp3", "m4a", "wav", "txt", "edl")):
            return OUTPUT_DIR / f"{nom}.{ext}"
        k += 1


# ═══════════════════════════════════════════════════════════════════════════════
# JOURNAL DES COUPES
# ═══════════════════════════════════════════════════════════════════════════════

def _hmsm(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, r = divmod(ms, 3600000)
    m, r = divmod(r, 60000)
    sec, ms = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d}.{ms:03d}"


def _sec(t: float, chiffres: int = 3) -> str:
    """Une durée en secondes, écrite à la française."""
    return f"{t:.{chiffres}f}".replace(".", ",") + " s"


def _texte(jetons: list, a: int, b: int) -> str:
    mots = []
    for t in jetons[max(0, a):b + 1]:
        mots.append(t[0] if t[0] is not None
                    else f"[{_sec(t[2] - t[1], 1)} sans paroles]")
    return " ".join(mots)


def _paragraphe(texte: str, retrait: str) -> list:
    import textwrap
    return textwrap.wrap(texte, width=72, initial_indent=retrait,
                         subsequent_indent=retrait) or [retrait + "(sans paroles)"]


def journal_texte(sortie: Path, projet: dict, jetons: list, coupes: list,
                  mode: str, fondu: float, fondu_image: bool, fmt: str) -> str:
    """Le journal lisible : chaque morceau gardé, et ce qui est retiré autour."""
    duree = projet.get("duree") or 0.0
    garde = sum(c["f"] - c["d"] for c in coupes)
    video = fmt == "mp4"
    L = ["JOURNAL DES COUPES", "=" * 18, "",
         f"Fichier d'origine : {projet['source']}",
         f"                    durée {_hmsm(duree)}",
         f"Montage produit   : {sortie}",
         f"                    durée {_hmsm(garde)}, le "
         + time.strftime("%Y-%m-%d à %H:%M"),
         "Réglage           : le texte marqué est "
         + ("retiré, tout le reste est gardé" if mode == "couper" else "gardé"),
         f"Fondu demandé     : {_sec(fondu, 1)} (le fondu du son ne mord jamais sur un mot :",
         "                    il est plus court quand la coupe est serrée)"]
    if video:
        L.append("Image             : " + (
            "coupes franches" if not (fondu_image and fondu > 0) else
            f"fondu au noir là où {_sec(SEUIL_FONDU_IMAGE, 0)} ou plus est retirée, "
            "coupe franche ailleurs"))
    L += [f"Morceaux gardés   : {len(coupes)}",
          f"Retiré en tout    : {_hmsm(max(0.0, duree - garde))}", "",
          "Les instants sont en heures:minutes:secondes.millièmes.", ""]

    def retire(debut, fin, a, b):
        if fin - debut < 0.05:
            return
        L.append(f"  RETIRÉ  {_hmsm(debut)} → {_hmsm(fin)}   ({_sec(fin - debut)})")
        L.extend(_paragraphe(_texte(jetons, a, b), "    ") if a <= b
                 else ["    (sans paroles)"])
        L.append("")

    n = len(jetons)
    retire(0.0, coupes[0]["d"], 0, coupes[0]["plage"][0] - 1)
    position = 0.0
    for k, c in enumerate(coupes):
        dur = c["f"] - c["d"]
        L.append("-" * 72)
        L.append(f"MORCEAU {k + 1}")
        L.append(f"  dans l'origine   {_hmsm(c['d'])} → {_hmsm(c['f'])}   ({_sec(dur)})")
        L.append(f"  dans le montage  {_hmsm(position)} → {_hmsm(position + dur)}")
        L.append(f"  son              fondu d'entrée {_sec(c['fi'])}, "
                 f"fondu de sortie {_sec(c['fo'])}")
        if video:
            if fondu_image and fondu > 0:
                L.append("  image            "
                         + ("entrée en fondu" if c.get("vi", True) else "entrée franche")
                         + ", "
                         + ("sortie en fondu au noir" if c.get("vo", True)
                            else "sortie franche"))
            else:
                L.append("  image            entrée et sortie franches")
        L.append("  texte")
        L.extend(_paragraphe(_texte(jetons, c["plage"][0], c["plage"][1]), "    "))
        L.append("")
        position += dur
        if k + 1 < len(coupes):
            retire(c["f"], coupes[k + 1]["d"], c["plage"][1] + 1,
                   coupes[k + 1]["plage"][0] - 1)
    retire(coupes[-1]["f"], duree, coupes[-1]["plage"][1] + 1, n - 1)
    return "\n".join(L) + "\n"


def _code_temporel(images: int, base: int, saute: bool) -> str:
    """Nombre d'images → HH:MM:SS:II. Avec `saute` (29,97 et 59,94 images/s,
    « drop frame »), deux ou quatre numéros d'image sont sautés chaque minute,
    sauf toutes les dix minutes."""
    if saute:
        d = 2 if base == 30 else 4
        par_dix, par_minute = base * 600 - d * 9, base * 60 - d
        dix, reste = divmod(images, par_dix)
        images += d * 9 * dix
        if reste >= d:
            images += d * ((reste - d) // par_minute)
    ii = images % base
    ss = (images // base) % 60
    mm = (images // (base * 60)) % 60
    hh = images // (base * 3600)
    return f"{hh:02d}:{mm:02d}:{ss:02d}{';' if saute else ':'}{ii:02d}"


def _images_depuis_code(code: str, base: int) -> int:
    """HH:MM:SS:II (ou ;II) → nombre d'images, pour le décalage d'origine."""
    saute = ";" in code
    try:
        hh, mm, ss, ii = (int(x) for x in re.split(r"[:;]", code.strip()))
    except ValueError:
        return 0
    images = ((hh * 60 + mm) * 60 + ss) * base + ii
    if saute:
        d = 2 if base == 30 else 4
        minutes = hh * 60 + mm
        images -= d * (minutes - minutes // 10)
    return images


def code_d_origine(source: str) -> str:
    """Le code temporel inscrit dans le fichier par la caméra, s'il y en a un."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format_tags=timecode:stream_tags=timecode", "-of", "json", source],
            capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""
    code = (d.get("format", {}).get("tags", {}) or {}).get("timecode", "")
    for st in d.get("streams", []):
        code = code or (st.get("tags", {}) or {}).get("timecode", "")
    return code if re.fullmatch(r"\d\d:\d\d:\d\d[:;]\d\d", code or "") else ""


def journal_edl(sortie: Path, projet: dict, coupes: list, fmt: str) -> str:
    """Liste de montage CMX 3600, pour refaire le montage dans un logiciel.
    Elle ne décrit que des coupes franches : les fondus sont en commentaire."""
    ips = ((projet.get("video") or {}).get("ips") or 0.0) if projet["type"] == "video" else 0.0
    if not 1.0 <= ips <= 120.0:
        ips = 25.0              # son seul, ou cadence illisible
    base = int(round(ips))
    code = code_d_origine(projet["source"]) if projet["type"] == "video" else ""
    saute = ";" in code and base in (30, 60)
    origine = _images_depuis_code(code, base) if code else 0
    canaux = (projet.get("audio") or {}).get("canaux", 2)
    if fmt == "mp4":
        piste = "AA/V" if canaux >= 2 else "B"
    else:
        piste = "AA" if canaux >= 2 else "A"

    titre = re.sub(r"[^A-Za-z0-9 _.-]+", "_", sortie.stem)[:60]
    # Rien d'autre que ces deux lignes avant le premier plan : des lecteurs de
    # listes de montage refusent un commentaire placé là (vu avec OpenTimelineIO).
    L = [f"TITLE: {titre}",
         "FCM: " + ("DROP FRAME" if saute else "NON-DROP FRAME"),
         ""]
    notes = [f"* MONTAGE AU STABILO - {time.strftime('%Y-%m-%d %H:%M')}",
             f"* CADENCE : {ips:g} IMAGES/S, CODES COMPTES EN BASE {base}"
             + ("" if projet["type"] == "video" else " (SON SEUL : CADENCE DE CONVENTION)"),
             "* ORIGINE : " + (f"CODE TEMPOREL DU FICHIER {code}" if code
                              else "LE FICHIER COMMENCE A 00:00:00:00"),
             "* LE MONTAGE COMMENCE A 01:00:00:00",
             "* COUPES FRANCHES SEULEMENT : LES FONDUS SONT INDIQUES EN COMMENTAIRE"]
    largeur = max(3, len(str(len(coupes))))
    position = base * 3600          # 01:00:00:00, toujours sans saut à cet instant
    if saute:
        position = _images_depuis_code("01:00:00;00", base)
    for k, c in enumerate(coupes):
        debut = int(round(c["d"] * ips))
        fin = max(debut + 1, int(round(c["f"] * ips)))
        n = fin - debut
        L.append(f"{k + 1:0{largeur}d}  {'AX':<8} {piste:<5} C        "
                 f"{_code_temporel(origine + debut, base, saute)} "
                 f"{_code_temporel(origine + fin, base, saute)} "
                 f"{_code_temporel(position, base, saute)} "
                 f"{_code_temporel(position + n, base, saute)}")
        L.append(f"* FROM CLIP NAME: {projet['nom']}")
        L.append(f"* SOURCE FILE: {projet['source']}")
        L.append(f"* FONDU SON : ENTREE {c['fi']:.3f} S, SORTIE {c['fo']:.3f} S")
        if fmt == "mp4":
            L.append("* IMAGE : ENTREE " + ("EN FONDU" if c.get("vi", True) else "FRANCHE")
                     + ", SORTIE " + ("EN FONDU AU NOIR" if c.get("vo", True) else "FRANCHE"))
        L.append("")
        position += n
    return "\n".join(L + notes) + "\n"


def ecrire_journaux(sortie: Path, projet: dict, jetons: list, coupes: list,
                    mode: str, fondu: float, fondu_image: bool, fmt: str) -> list:
    """Écrit montage.txt et montage.edl à côté du montage."""
    image = fondu_image and fondu > 0
    coupes_edl = coupes if image else [dict(c, vi=False, vo=False) for c in coupes]
    ecrits = []
    for ext, contenu in (
            ("txt", journal_texte(sortie, projet, jetons, coupes, mode, fondu,
                                  fondu_image, fmt)),
            ("edl", journal_edl(sortie, projet, coupes_edl, fmt))):
        chemin = sortie.with_suffix("." + ext)
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(contenu)
        ecrits.append(str(chemin))
    return ecrits


def _ffmpeg_suivi(cmd: list, tache: dict, journal: str, sur_temps=None):
    """Lance ffmpeg avec -progress, suit l'avancement, s'arrête sur annulation."""
    cmd = cmd[:1] + ["-nostdin", "-v", "error", "-progress", "pipe:1", "-nostats"] + cmd[1:]
    with open(journal, "w") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
    tache["proc"] = proc
    try:
        for ligne in proc.stdout:
            if tache.get("annulee"):
                proc.kill()
                break
            if sur_temps and ligne.startswith("out_time_us="):
                try:
                    sur_temps(int(ligne.split("=", 1)[1]) / 1e6)
                except ValueError:
                    pass
        proc.wait()
    finally:
        tache["proc"] = None
    if tache.get("annulee"):
        raise Annule()
    if proc.returncode != 0:
        try:
            with open(journal) as f:
                detail = f.read()[-600:].strip()
        except OSError:
            detail = ""
        raise RuntimeError(detail or "ffmpeg a échoué")


def produire_video(tache: dict, source: str, coupes: list, fondu: float,
                   fondu_image: bool, sortie: Path):
    """Un fichier intermédiaire par passage (image x264 + son non compressé),
    puis assemblage : l'image est recopiée telle quelle, le son n'est encodé
    qu'une fois."""
    total = sum(c["f"] - c["d"] for c in coupes)
    tmp = Path(tempfile.mkdtemp(prefix="montage_", dir=str(MONTAGE_DIR)))
    try:
        fait = 0.0
        morceaux = []
        for k, c in enumerate(coupes):
            dur = c["f"] - c["d"]
            tache["message"] = f"Passage {k + 1} sur {len(coupes)}"
            vf = []
            if fondu_image and fondu > 0:
                fv = min(fondu, dur / 2.0)
                if c.get("vi", True):
                    vf.append(f"fade=t=in:st=0:d={fv:.3f}")
                if c.get("vo", True):
                    vf.append(f"fade=t=out:st={dur - fv:.3f}:d={fv:.3f}")
            af = [f"afade=t=in:st=0:d={c['fi']:.3f}:curve=hsin",
                  f"afade=t=out:st={max(0.0, dur - c['fo']):.3f}:d={c['fo']:.3f}:curve=hsin"]
            morceau = tmp / f"passage_{k:04d}.mkv"
            cmd = ["ffmpeg", "-y", "-ss", f"{c['d']:.3f}", "-i", source,
                   "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0"]
            if vf:
                cmd += ["-vf", ",".join(vf)]
            cmd += ["-af", ",".join(af),
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
                    str(morceau)]
            base = fait

            def avance(t, base=base, dur=dur):
                tache["progression"] = 0.96 * (base + min(t, dur)) / total

            _ffmpeg_suivi(cmd, tache, str(tmp / "ffmpeg.log"), avance)
            morceaux.append(morceau)
            fait += dur

        tache["message"] = "Assemblage"
        liste = tmp / "liste.txt"
        with open(liste, "w", encoding="utf-8") as f:
            for m in morceaux:
                f.write(f"file '{m}'\n")
        partiel = sortie.with_name(sortie.stem + ".partiel" + sortie.suffix)
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(liste),
               "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", str(partiel)]
        try:
            _ffmpeg_suivi(cmd, tache, str(tmp / "ffmpeg.log"),
                          lambda t: tache.update(
                              progression=0.96 + 0.04 * min(t / total, 1.0)))
            os.replace(partiel, sortie)
        finally:
            if partiel.exists():
                partiel.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def produire_audio(tache: dict, source: str, coupes: list, audio: dict,
                   fmt: str, sortie: Path):
    """Le son est décodé une seule fois, du début à la fin, et les passages
    sont prélevés au vol, à l'échantillon près (même décodage que pour la
    transcription : les horodatages tombent juste, quel que soit le format)."""
    import numpy as np

    sr, ch = audio["frequence"], audio["canaux"]
    codec = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
             "m4a": ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
             "wav": ["-c:a", "pcm_s16le"]}[fmt]
    partiel = sortie.with_name(sortie.stem + ".partiel" + sortie.suffix)
    journal = tempfile.NamedTemporaryFile(prefix="montage_", suffix=".log",
                                          delete=False)
    brut = ["-f", "f32le", "-ar", str(sr), "-ac", str(ch)]
    dec = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", source, "-vn",
         "-map", "0:a:0", "-acodec", "pcm_f32le"] + brut + ["-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    enc = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-y", "-v", "error"] + brut + ["-i", "-"]
        + codec + [str(partiel)],
        stdin=subprocess.PIPE, stderr=journal)

    bornes = [(int(round(c["d"] * sr)), int(round(c["f"] * sr)),
               max(1, int(c["fi"] * sr)), max(1, int(c["fo"] * sr)))
              for c in coupes]
    dernier = bornes[-1][1]
    taille = ch * 4
    bloc = sr                       # une seconde à la fois
    pos, k = 0, 0
    tache["message"] = "Prélèvement des passages"
    try:
        while k < len(bornes):
            if tache.get("annulee"):
                raise Annule()
            octets = dec.stdout.read(bloc * taille)
            if not octets:
                break
            n = len(octets) // taille
            x = np.frombuffer(octets[:n * taille], dtype="<f4").reshape(n, ch)
            while k < len(bornes):
                d, f, fi, fo = bornes[k]
                if d >= pos + n:
                    break
                a, b = max(d, pos), min(f, pos + n)
                if b > a:
                    idx = np.arange(a, b, dtype=np.float64)
                    g = (np.clip((idx - d) / fi, 0.0, 1.0)
                         * np.clip((f - idx) / fo, 0.0, 1.0))
                    g = 0.5 - 0.5 * np.cos(np.pi * g)
                    morceau = x[a - pos:b - pos] * g[:, None].astype(np.float32)
                    enc.stdin.write(morceau.astype("<f4").tobytes())
                if f <= pos + n:
                    k += 1
                else:
                    break
            pos += n
            tache["progression"] = 0.97 * min(pos / max(dernier, 1), 1.0)
        tache["message"] = "Encodage"
        enc.stdin.close()
        if enc.wait() != 0:
            journal.flush()
            with open(journal.name) as f:
                raise RuntimeError(f.read()[-600:].strip() or "ffmpeg a échoué")
        os.replace(partiel, sortie)
    except BrokenPipeError:
        with open(journal.name) as f:
            raise RuntimeError(f.read()[-600:].strip() or "ffmpeg a échoué")
    finally:
        for p in (dec, enc):
            if p.poll() is None:
                p.kill()
        try:
            dec.stdout.close()
        except OSError:
            pass
        journal.close()
        os.unlink(journal.name)
        if partiel.exists():
            partiel.unlink()


def finaliser_le_son(tache: dict, sortie: Path):
    """Dernière étape, facultative : le son du montage passe par finaliser.py
    (fond, volume à la norme). Le montage garde son nom et sa durée."""
    script = SCRIPT_DIR / "finaliser.py"
    if not script.exists():
        raise RuntimeError("finaliser.py est absent")
    tache.update(message="Finition du son", progression=0.0)
    proc = subprocess.Popen([PYTHON_BIN, str(script), str(sortie), "--sur-place"],
                            cwd=str(SCRIPT_DIR), text=True, errors="replace",
                            env=dict(os.environ, PYTHONUNBUFFERED="1"),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    tache["proc"] = proc
    dernieres = deque(maxlen=6)
    for ligne in proc.stdout:
        ligne = ligne.strip()
        if ligne:
            dernieres.append(ligne)
        m = re.match(r"\[(\d)/9\]", ligne)
        if m:
            tache["progression"] = (int(m.group(1)) - 1) / 9.0
    proc.wait()
    tache["proc"] = None
    if proc.returncode != 0:
        # Arrêté net, finaliser.py n'a pas pu ranger son dossier de travail
        for reste in sortie.parent.glob(".finition_*"):
            if any(f.name.startswith(sortie.stem) for f in reste.iterdir()):
                shutil.rmtree(reste, ignore_errors=True)
    if tache.get("annulee"):
        raise Annule()
    if proc.returncode != 0:
        raise RuntimeError("\n".join(dernieres) or "finaliser.py a échoué")


# ═══════════════════════════════════════════════════════════════════════════════
# SERVEUR
# ═══════════════════════════════════════════════════════════════════════════════

TRANSCRIPTIONS = {}     # id projet → {proc, lignes, debut}
APERCUS = {}            # id projet → "encours" | "erreur"
TACHES = {}             # id tâche → dict
JETONS = {}             # id projet → (mtime, jetons, langue, origine, estimé)
EXISTANTS = {}          # id projet → texte déjà produit trouvé pour ce fichier
VERROU = threading.Lock()


def charger_jetons(dossier: Path, pid: str):
    chemin = dossier / "transcription.json"
    try:
        mtime = chemin.stat().st_mtime
    except OSError:
        return None, None
    with VERROU:
        cache = JETONS.get(pid)
        if cache and cache[0] == mtime:
            return cache[1], cache[2]
    data = lire_json(chemin)
    if not data:
        return None, None
    if data.get("version", 1) < VERSION_JETONS:
        # Transcription d'avant les repères de début et de fin : on les pose sans
        # retranscrire, et le surlignage enregistré suit le décalage des numéros.
        projet = lire_json(dossier / "projet.json", {}) or {}
        data["jetons"], decalage = poser_les_bords(data["jetons"],
                                                   projet.get("duree") or 0.0)
        data["version"] = VERSION_JETONS
        sel = lire_json(dossier / "selection.json")
        if decalage and sel and sel.get("plages"):
            sel["plages"] = [[p[0] + decalage, p[1] + decalage] for p in sel["plages"]]
            ecrire_json(dossier / "selection.json", sel)
        ecrire_json(chemin, data)
        mtime = chemin.stat().st_mtime
    with VERROU:
        JETONS[pid] = (mtime, data["jetons"], data.get("langue", ""),
                       data.get("origine", ""), bool(data.get("estime")))
    return data["jetons"], data.get("langue", "")


def origine_du_texte(pid: str) -> tuple:
    """(origine, estimé) du texte chargé : origine vide pour une transcription,
    sinon « doublage », « sous-titres » ou « origine »."""
    with VERROU:
        cache = JETONS.get(pid)
    return (cache[3], cache[4]) if cache else ("", False)


def texte_existant(projet: dict):
    """Ce que traduire.py ou doubler.py ont laissé pour ce fichier (cherché une
    fois par projet)."""
    pid = projet["id"]
    with VERROU:
        if pid in EXISTANTS:
            return EXISTANTS[pid]
    trouve = chercher_texte_existant(projet["source"], projet.get("duree") or 0.0)
    with VERROU:
        EXISTANTS[pid] = trouve
    return trouve


def chemin_apercu(dossier: Path, projet: dict) -> Path:
    return dossier / ("apercu.mp4" if projet["type"] == "video" else "apercu.m4a")


def lecture_directe(projet: dict) -> bool:
    ext = Path(projet["source"]).suffix.lower()
    a = (projet.get("audio") or {}).get("codec", "")
    if projet["type"] == "video":
        v = (projet.get("video") or {}).get("codec", "")
        return (ext in LECTURE_DIRECTE_VIDEO and v in CODECS_VIDEO_NAVIGATEUR
                and a in CODECS_AUDIO_NAVIGATEUR)
    return ext in LECTURE_DIRECTE_AUDIO


def lancer_apercu(dossier: Path, projet: dict):
    """Copie de travail pour le lecteur, seulement si le navigateur en a besoin."""
    pid = projet["id"]
    cible = chemin_apercu(dossier, projet)
    if lecture_directe(projet) or cible.exists():
        return
    with VERROU:
        if APERCUS.get(pid) == "encours":
            return
        APERCUS[pid] = "encours"

    def travail():
        tmp = cible.with_name("apercu.tmp" + cible.suffix)
        if projet["type"] == "video":
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", projet["source"],
                   "-map", "0:v:0", "-map", "0:a:0", "-vf", "scale=-2:480",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(tmp)]
        else:
            cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", projet["source"],
                   "-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        with VERROU:
            if r.returncode == 0 and tmp.exists():
                os.replace(tmp, cible)
                APERCUS.pop(pid, None)
            else:
                APERCUS[pid] = "erreur"
                if tmp.exists():
                    tmp.unlink()

    threading.Thread(target=travail, daemon=True).start()


def lancer_transcription(dossier: Path, projet: dict, langue: str):
    pid = projet["id"]
    with VERROU:
        t = TRANSCRIPTIONS.get(pid)
        if t and t["proc"].poll() is None:
            return
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(
            [PYTHON_BIN, str(SCRIPT_DIR / "monter.py"),
             "--tache-transcrire", str(dossier), "--langue", langue or ""],
            cwd=str(SCRIPT_DIR), env=env, text=True, errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        t = {"proc": proc, "lignes": deque(maxlen=200), "debut": time.time()}
        TRANSCRIPTIONS[pid] = t

    def lecteur():
        for ligne in proc.stdout:
            ligne = ligne.strip()
            # Le bavardage des bibliothèques n'apprend rien à l'utilisateur
            # (seules les lignes du toolkit commencent par un pictogramme)
            if ligne and ord(ligne[0]) > 0x2000:
                t["lignes"].append(ligne)
        proc.wait()

    threading.Thread(target=lecteur, daemon=True).start()


def etat_du_projet(dossier: Path, projet: dict) -> dict:
    pid = projet["id"]
    e = {k: projet.get(k) for k in ("id", "nom", "source", "type", "duree", "video")}
    e["source_presente"] = os.path.isfile(projet["source"])
    existant = texte_existant(projet)
    e["existant"] = existant["genre"] if existant else ""
    e["reprise"] = (projet.get("reprise") or {}).get("genre", "")
    t = TRANSCRIPTIONS.get(pid)
    if (dossier / "transcription.json").exists() and not (t and t["proc"].poll() is None):
        e["etat"] = "pret"
        _, e["langue"] = charger_jetons(dossier, pid)
        e["origine"] = origine_du_texte(pid)[0]
    elif t and t["proc"].poll() is None:
        e["etat"] = "transcription"
        e["depuis"] = round(time.time() - t["debut"])
        e["journal"] = list(t["lignes"])[-1:] if t["lignes"] else []
    elif t:
        e["etat"] = "erreur"
        rates = [l for l in t["lignes"] if l.startswith("❌")]
        e["journal"] = rates[-1:] or list(t["lignes"])[-4:]
    else:
        e["etat"] = "attente"
    if lecture_directe(projet) or chemin_apercu(dossier, projet).exists():
        e["apercu"] = "pret"
    else:
        e["apercu"] = APERCUS.get(pid, "encours")
    return e


def creer_blueprint():
    """Les routes de l'outil, à monter à la racine (monter.py seul) ou sous un
    préfixe (gui.py le monte sous /montage)."""
    from flask import Blueprint, Response, request, jsonify, send_file, abort

    MONTAGE_DIR.mkdir(parents=True, exist_ok=True)
    app = Blueprint("montage", __name__)

    def projet_ou_404(pid):
        dossier = dossier_du_projet(pid)
        projet = lire_json(dossier / "projet.json") if dossier else None
        if not projet:
            abort(404)
        return dossier, projet

    @app.route("/")
    def index():
        # La page appelle le serveur par des adresses absolues : elle doit
        # connaître le préfixe sous lequel elle est servie.
        base = request.path.rstrip("/")
        return Response(INDEX_HTML.replace("__BASE__", base), mimetype="text/html")

    @app.route("/favicon.ico")
    def favicon():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
               '<rect width="32" height="32" rx="6" fill="#14161a"/>'
               '<rect x="5" y="11" width="22" height="10" rx="2" fill="#ffe84a"/></svg>')
        return Response(svg, mimetype="image/svg+xml")

    @app.route("/api/config")
    def api_config():
        depart = INPUT_DIR if INPUT_DIR.is_dir() else Path.home()
        return jsonify({"depart": str(depart), "langues": LANGUES,
                        "fondu": FONDU_DEFAUT})

    @app.route("/api/parcourir")
    def api_parcourir():
        chemin = os.path.expanduser(request.args.get("chemin") or str(Path.home()))
        if not os.path.isdir(chemin):
            chemin = os.path.dirname(chemin) or str(Path.home())
        if not os.path.isdir(chemin):
            chemin = str(Path.home())
        dossiers, fichiers = [], []
        try:
            for nom in os.listdir(chemin):
                if nom.startswith("."):
                    continue
                plein = os.path.join(chemin, nom)
                if os.path.isdir(plein):
                    dossiers.append(nom)
                else:
                    ext = os.path.splitext(nom)[1].lower()
                    if ext in EXT_VIDEO or ext in EXT_AUDIO:
                        fichiers.append({"nom": nom,
                                         "type": "video" if ext in EXT_VIDEO else "audio",
                                         "taille": os.path.getsize(plein)})
        except OSError:
            pass
        dossiers.sort(key=str.lower)
        fichiers.sort(key=lambda f: f["nom"].lower())
        return jsonify({"chemin": chemin, "parent": os.path.dirname(chemin),
                        "dossiers": dossiers, "fichiers": fichiers})

    @app.route("/api/projets")
    def api_projets():
        liste = []
        if MONTAGE_DIR.is_dir():
            for d in MONTAGE_DIR.iterdir():
                p = lire_json(d / "projet.json") if d.is_dir() else None
                if not p:
                    continue
                sel = lire_json(d / "selection.json", {}) or {}
                liste.append({"id": p["id"], "nom": p["nom"], "type": p["type"],
                              "duree": p.get("duree", 0), "source": p["source"],
                              "present": os.path.isfile(p["source"]),
                              "transcrit": (d / "transcription.json").exists(),
                              "passages": len(sel.get("plages", [])),
                              "vu": (d / "projet.json").stat().st_mtime})
        liste.sort(key=lambda p: -p["vu"])
        return jsonify(liste[:12])

    @app.route("/api/ouvrir", methods=["POST"])
    def api_ouvrir():
        data = request.get_json(force=True, silent=True) or {}
        chemin = os.path.abspath(os.path.expanduser((data.get("chemin") or "").strip()))
        langue = (data.get("langue") or "").strip().lower()
        if not os.path.isfile(chemin):
            return jsonify({"erreur": "Ce fichier est introuvable."}), 400
        info = sonder(chemin)
        if not info:
            return jsonify({"erreur": "Ce fichier n'est ni une vidéo ni un enregistrement lisible."}), 400
        if not info.get("audio"):
            return jsonify({"erreur": "Ce fichier n'a pas de son : il n'y a rien à transcrire."}), 400
        pid = id_source(chemin)
        dossier = dossier_du_projet(pid)
        if dossier is None:
            dossier = MONTAGE_DIR / f"{_slug(Path(chemin).stem)}-{pid}"
            dossier.mkdir(parents=True, exist_ok=True)
        projet = dict(info, id=pid, source=chemin, nom=Path(chemin).name)
        # Un fichier déjà sous-titré ou doublé a son texte : il est repris, sauf
        # si on a demandé une fois de transcrire le son de ce fichier.
        ancien = lire_json(dossier / "projet.json", {}) or {}
        if "reprise" in ancien:
            projet["reprise"] = ancien["reprise"]
        elif not (dossier / "transcription.json").exists():
            projet["reprise"] = texte_existant(projet)
        ecrire_json(dossier / "projet.json", projet)
        lancer_apercu(dossier, projet)
        if not (dossier / "transcription.json").exists():
            lancer_transcription(dossier, projet, langue)
        return jsonify(etat_du_projet(dossier, projet))

    @app.route("/api/projet/<pid>")
    def api_projet(pid):
        dossier, projet = projet_ou_404(pid)
        return jsonify(etat_du_projet(dossier, projet))

    @app.route("/api/retranscrire/<pid>", methods=["POST"])
    def api_retranscrire(pid):
        dossier, projet = projet_ou_404(pid)
        data = request.get_json(force=True, silent=True) or {}
        t = TRANSCRIPTIONS.get(pid)
        if t and t["proc"].poll() is None:
            return jsonify({"erreur": "La transcription est déjà en cours."}), 409
        if not os.path.isfile(projet["source"]):
            return jsonify({"erreur": "Le fichier d'origine est introuvable."}), 400
        # « existant » : le texte déjà traduit ; sinon le son est transcrit
        if data.get("texte") == "existant":
            with VERROU:
                EXISTANTS.pop(pid, None)
            projet["reprise"] = texte_existant(projet)
            if not projet["reprise"]:
                return jsonify({"erreur": "Aucun texte déjà produit n'a été trouvé "
                                          "pour ce fichier."}), 400
        else:
            projet["reprise"] = None
        langue = (data.get("langue") or "").strip().lower()
        existant = texte_existant(projet)
        if not langue and existant and existant["genre"] == "doublage":
            langue = existant["langue"]         # la langue de la voix doublée
        ecrire_json(dossier / "projet.json", projet)
        # Le texte change, pas ce qu'on a surligné : le surlignage est gardé
        # en secondes et retrouvera ses mots dans le nouveau texte.
        jetons, _ = charger_jetons(dossier, pid)
        sel = lire_json(dossier / "selection.json", {}) or {}
        propres = plages_propres(sel.get("plages") or [], len(jetons)) if jetons else []
        if propres:
            sel["temps"] = [[jetons[i][1], jetons[j][2]] for i, j in propres]
            sel.pop("plages", None)
            ecrire_json(dossier / "selection.json", sel)
        elif (dossier / "selection.json").exists() and not sel.get("temps"):
            (dossier / "selection.json").unlink()
        if (dossier / "transcription.json").exists():
            (dossier / "transcription.json").unlink()
        lancer_transcription(dossier, projet, langue)
        return jsonify(etat_du_projet(dossier, projet))

    @app.route("/api/transcription/<pid>")
    def api_transcription(pid):
        dossier, projet = projet_ou_404(pid)
        jetons, langue = charger_jetons(dossier, pid)
        if jetons is None:
            abort(404)
        sel = lire_json(dossier / "selection.json", {}) or {}
        if sel.get("temps") and "plages" not in sel:
            sel["plages"] = plages_depuis_temps(jetons, sel.pop("temps"))
            ecrire_json(dossier / "selection.json", sel)
        origine, estime = origine_du_texte(pid)
        return jsonify({"jetons": jetons, "langue": langue,
                        "origine": origine, "estime": estime,
                        "plages": sel.get("plages", []),
                        "mode": sel.get("mode", "garder"),
                        "fondu": sel.get("fondu", FONDU_DEFAUT)})

    def lire_demande(dossier, projet, pid):
        data = request.get_json(force=True, silent=True) or {}
        jetons, _ = charger_jetons(dossier, pid)
        if jetons is None:
            abort(404)
        try:
            fondu = float(data.get("fondu", FONDU_DEFAUT))
        except (TypeError, ValueError):
            fondu = FONDU_DEFAUT
        if not math.isfinite(fondu):
            fondu = FONDU_DEFAUT
        fondu = min(max(fondu, 0.0), FONDU_MAX)
        plages = data.get("plages") or []
        # « garder » : le surligné fait le montage ; « couper » : il en est retiré
        mode = "couper" if data.get("mode") == "couper" else "garder"
        data["mode"] = mode
        if mode == "couper" and not plages_propres(plages, len(jetons)):
            coupes = []             # rien de rayé : il n'y a pas de montage à faire
        else:
            coupes = calculer_coupes(dossier, jetons, plages, fondu,
                                     projet.get("duree") or 0.0,
                                     inverse=(mode == "couper"),
                                     estime=origine_du_texte(pid)[1])
        ecrire_json(dossier / "selection.json",
                    {"plages": plages, "fondu": fondu, "mode": mode})
        return data, fondu, coupes

    @app.route("/api/selection/<pid>", methods=["POST"])
    def api_selection(pid):
        dossier, projet = projet_ou_404(pid)
        _, _, coupes = lire_demande(dossier, projet, pid)
        return jsonify({"coupes": coupes,
                        "duree": round(sum(c["f"] - c["d"] for c in coupes), 2)})

    @app.route("/media/<pid>")
    def media(pid):
        dossier, projet = projet_ou_404(pid)
        if lecture_directe(projet):
            chemin = Path(projet["source"])
        else:
            chemin = chemin_apercu(dossier, projet)
        if not chemin.exists():
            abort(404)
        return send_file(str(chemin), conditional=True)

    @app.route("/api/produire/<pid>", methods=["POST"])
    def api_produire(pid):
        dossier, projet = projet_ou_404(pid)
        data, fondu, coupes = lire_demande(dossier, projet, pid)
        fmt = data.get("format") or ("mp4" if projet["type"] == "video" else "mp3")
        permis = ["mp3", "m4a", "wav"] + (["mp4"] if projet["type"] == "video" else [])
        if fmt not in permis:
            return jsonify({"erreur": "Ce format n'est pas proposé pour ce fichier."}), 400
        if not coupes:
            if data["mode"] == "garder" or not data.get("plages"):
                return jsonify({"erreur": "Aucun passage n'est surligné."}), 400
            return jsonify({"erreur": "Tout est rayé : il ne reste rien à monter."}), 400
        if not os.path.isfile(projet["source"]):
            return jsonify({"erreur": "Le fichier d'origine est introuvable."}), 400
        for t in TACHES.values():
            if t["projet"] == pid and t["etat"] == "encours":
                return jsonify({"erreur": "Un montage est déjà en cours de production."}), 409

        sortie = chemin_de_sortie(projet["source"], fmt)
        jetons, _ = charger_jetons(dossier, pid)
        fondu_image = bool(data.get("fondu_image", True))
        finition = bool(data.get("finaliser"))
        tid = uuid.uuid4().hex[:12]
        # finition : « » (pas demandée), « faite », ou « echec » (le montage
        # est alors laissé tel qu'il a été monté)
        tache = {"id": tid, "projet": pid, "etat": "encours", "progression": 0.0,
                 "message": "Préparation", "sortie": str(sortie), "proc": None,
                 "annulee": False, "format": fmt, "journaux": [], "finition": "",
                 "duree": round(sum(c["f"] - c["d"] for c in coupes), 2)}
        TACHES[tid] = tache

        def travail():
            try:
                if fmt == "mp4":
                    produire_video(tache, projet["source"], coupes, fondu,
                                   fondu_image, sortie)
                else:
                    produire_audio(tache, projet["source"], coupes,
                                   projet["audio"], fmt, sortie)
                try:
                    tache["journaux"] = ecrire_journaux(
                        sortie, projet, jetons, coupes, data["mode"], fondu,
                        fondu_image, fmt)
                except Exception as ex:     # le montage est fait : on le dit quand même
                    tache["journaux"] = []
                    print(f"⚠️  Journal des coupes non écrit : {ex}")
                if finition:
                    try:
                        finaliser_le_son(tache, sortie)
                        tache["finition"] = "faite"
                    except Annule:
                        raise
                    except Exception as ex:
                        tache["finition"] = "echec"
                        print(f"⚠️  Son non finalisé : {ex}")
                tache.update(etat="fini", progression=1.0, message="Montage terminé")
            except Annule:
                tache.update(etat="annule", message="Production arrêtée")
            except Exception as ex:
                tache.update(etat="erreur", message=str(ex) or "Échec de la production")

        threading.Thread(target=travail, daemon=True).start()
        return jsonify({"id": tid})

    def vue_tache(t):
        v = {k: t[k] for k in ("id", "etat", "progression", "message", "sortie",
                               "format", "duree", "journaux", "finition")}
        if t["etat"] == "fini" and os.path.exists(t["sortie"]):
            v["taille"] = os.path.getsize(t["sortie"])
        return v

    @app.route("/api/tache/<tid>")
    def api_tache(tid):
        t = TACHES.get(tid)
        if not t:
            abort(404)
        return jsonify(vue_tache(t))

    @app.route("/api/tache/<tid>/arreter", methods=["POST"])
    def api_arreter(tid):
        t = TACHES.get(tid)
        if not t:
            abort(404)
        t["annulee"] = True
        proc = t.get("proc")
        if proc is not None and proc.poll() is None:
            proc.kill()
        return jsonify({"ok": True})

    @app.route("/resultat/<tid>")
    def resultat(tid):
        t = TACHES.get(tid)
        if not t or t["etat"] != "fini" or not os.path.exists(t["sortie"]):
            abort(404)
        return send_file(t["sortie"], conditional=True)

    @app.route("/api/tache/<tid>/dossier", methods=["POST"])
    def api_dossier(tid):
        t = TACHES.get(tid)
        if not t:
            abort(404)
        try:
            subprocess.Popen(["xdg-open", os.path.dirname(t["sortie"])],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return jsonify({"erreur": "Impossible d'ouvrir le dossier."}), 500
        return jsonify({"ok": True})

    return app


def creer_app():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(creer_blueprint())
    return app


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE
# ═══════════════════════════════════════════════════════════════════════════════

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Montage au stabilo</title>
<link rel="icon" href="__BASE__/favicon.ico">
<style>
:root{
  --bg:#14161a; --panel:#1c1f26; --panel2:#0c0e12; --border:#2a2e38;
  --text:#e6e8ec; --muted:#8a90a0; --accent:#ff8a3d; --accent-hover:#ffa362;
  --ok:#4ad27e; --danger:#e04b4b;
  --papier:#f6f2e8; --encre:#23211d; --encre2:#8b8578; --filet:#e2dccd;
  --stabilo:#ffe84a; --stabilo-pre:#fff3a0; --gomme:#e9e4d8;
  --raye:#ffc9c2; --raye-pre:#ffe1dc; --raye-encre:#8f3a31;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;overflow:hidden}
[hidden]{display:none !important}
button{font:inherit}
.btn{border:1px solid var(--border);background:var(--panel);color:var(--text);border-radius:9px;padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.btn:hover{border-color:#454b5a}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn.fort{background:var(--accent);border-color:var(--accent);color:#1b1205}
.btn.fort:hover{background:var(--accent-hover)}
.btn.danger{background:transparent;border-color:var(--danger);color:#ff9d9d}
input[type=text],input[type=number],select{background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:9px;padding:8px 11px;font-size:13px;outline:none;min-width:0}
input:focus,select:focus{border-color:var(--accent)}

/* ── Accueil ── */
#accueil{height:100vh;overflow-y:auto;display:flex;justify-content:center;padding:9vh 24px 40px}
.carte{width:680px;max-width:100%}
.carte h1{font-size:26px;margin:0 0 6px;letter-spacing:.2px}
.carte h1 mark{background:var(--stabilo);color:#1b1a12;padding:0 .25em;border-radius:3px}
.carte .chapeau{color:var(--muted);margin:0 0 26px;font-size:15px}
.ligne{display:flex;gap:8px;margin-bottom:10px}
.ligne input{flex:1}
.etiquette{font-size:12px;color:var(--muted);font-weight:600;margin:14px 0 6px}
#erreur-accueil{color:#ff9d9d;min-height:20px;margin:6px 0}
#recents{margin-top:8px;display:flex;flex-direction:column;gap:6px}
.recent{display:flex;align-items:center;gap:12px;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 14px;cursor:pointer}
.recent:hover{border-color:var(--accent)}
.recent.absent{opacity:.45;cursor:default}
.recent .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.recent .d{color:var(--muted);font-size:12px;white-space:nowrap}

/* ── Atelier ── */
#atelier{height:100vh;display:flex;flex-direction:column}
#barre{flex:none;display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel2)}
#titre{min-width:160px;flex:1}
#titre .n{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#titre .d{font-size:12px;color:var(--muted)}
.outils{display:flex;border:1px solid var(--border);border-radius:9px;overflow:hidden}
.outils button{border:none;background:var(--panel);color:var(--muted);padding:8px 14px;font-weight:600;font-size:13px;cursor:pointer}
.outils button.actif{background:var(--stabilo);color:#1b1a12}
.outils button#o-gomme.actif{background:#d9d4c7}
#corps{flex:1;display:flex;min-height:0}
#cote{width:400px;flex:none;border-right:1px solid var(--border);background:var(--panel2);display:flex;flex-direction:column;min-height:0}
#ecran{flex:none;background:#000}
#ecran video{display:block;width:100%;max-height:38vh;background:#000}
#ecran audio{display:block;width:100%;margin:0}
#ecran.son{background:transparent;padding:14px 14px 4px}
#attente-apercu{padding:18px;color:var(--muted);font-size:13px;text-align:center}
#bilan{flex:none;padding:14px 16px 10px;border-bottom:1px solid var(--border)}
#bilan .g{font-size:20px;font-weight:700}
#bilan .p{color:var(--muted);font-size:12px}
#bilan .ligne{margin:10px 0 0;flex-wrap:wrap}
#passages{flex:1;overflow-y:auto;padding:8px}
.passage{display:flex;gap:10px;align-items:baseline;padding:8px 10px;border-radius:9px;cursor:pointer}
.passage:hover{background:#181b22}
.passage .h{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--accent);flex:none;width:52px}
.passage .x{flex:1;min-width:0;font-size:13px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.passage .l{font-size:11px;color:var(--muted);flex:none}
.passage .r{border:none;background:none;color:var(--muted);cursor:pointer;font-size:16px;line-height:1;padding:0 2px;flex:none}
.passage .r:hover{color:#ff9d9d}
#vide{color:var(--muted);font-size:13px;padding:18px 14px;line-height:1.6}
#production{flex:none;border-top:1px solid var(--border);padding:14px 16px}
#production .champs{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
#production label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted);font-weight:600}
#production label.case{flex-direction:row;align-items:center;gap:8px;grid-column:1/-1;color:var(--text);font-weight:400;font-size:13px}
#production .btn.fort{width:100%;padding:11px}
#jauge{height:6px;border-radius:6px;background:#262a34;overflow:hidden;margin:10px 0 6px}
#jauge i{display:block;height:100%;width:0;background:var(--accent);transition:width .3s}
#suivi-texte{font-size:12px;color:var(--muted);display:flex;gap:10px;align-items:center}
#suivi-texte span{flex:1}
#fini{font-size:13px;line-height:1.5}
#fini .chemin{font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);word-break:break-all;margin:4px 0 10px}
#fini #fini-journaux,#fini #fini-finition{font:12px/1.4 inherit;font-family:inherit;word-break:normal;margin-top:-4px}
#fini #fini-journaux:empty,#fini #fini-finition:empty{display:none}
#fini #fini-finition.echec{color:#ff9d9d}
#fini .ligne{margin:0}
#fini .ok{color:var(--ok);font-weight:700}
#echec{color:#ff9d9d;font-size:12.5px;margin-top:8px;white-space:pre-wrap;word-break:break-word}

/* ── Texte ── */
#page{flex:1;overflow-y:auto;background:var(--papier);color:var(--encre);min-width:0}
#texte{max-width:calc(72ch + 96px);margin:0 auto;padding:34px 28px 45vh 20px;font:19px/1.8 "Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;user-select:none;-webkit-user-select:none;cursor:text}
.par{display:flex;gap:16px;margin:0 0 .9em}
.par p{margin:0;flex:1;min-width:0}
.hor{flex:none;width:52px;text-align:right;font:12px/2.85 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--encre2);cursor:pointer;text-decoration:none}
.hor:hover{color:#b8530f}
.m{border-radius:3px;padding:.12em 0}
.m.s{background:var(--stabilo);box-shadow:.3em 0 0 var(--stabilo);border-radius:0}
.m.s.sd{border-radius:4px 0 0 4px}
.m.s.sf{box-shadow:none;border-radius:0 4px 4px 0}
.m.s.sd.sf{border-radius:4px}
.m.pre{background:var(--stabilo-pre);box-shadow:.3em 0 0 var(--stabilo-pre)}
.m.preg{background:var(--gomme);box-shadow:.3em 0 0 var(--gomme);color:var(--encre2)}
.m.lu{text-decoration:underline;text-decoration-color:#e0621a;text-decoration-thickness:3px;text-underline-offset:5px}
.m.pause{font:12px/1 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--encre2);border:1px dashed #cfc8b6;border-radius:20px;padding:4px 12px;box-shadow:none}
.m.pause.s,.m.pause.pre{color:var(--encre);border-style:solid;border-color:#d9c23a;border-radius:20px;box-shadow:none}
.gomme #texte{cursor:cell}
/* Mode « ce que je coupe » : le surligné est rayé */
.couper .m.s{background:var(--raye);box-shadow:.3em 0 0 var(--raye);color:var(--raye-encre);text-decoration:line-through;text-decoration-color:#c9544a}
.couper .m.s.sf{box-shadow:none}
.couper .m.s.lu{text-decoration:underline line-through;text-decoration-color:#c9544a}
.couper .m.pre{background:var(--raye-pre);box-shadow:.3em 0 0 var(--raye-pre)}
.couper .m.pause.s,.couper .m.pause.pre{border-color:#d98a82;box-shadow:none}
.couper .outils button#o-stabilo.actif,.outils button#m-couper.actif{background:var(--raye);color:#4a1712}
#modes{align-items:center}
#modes span{padding:0 4px 0 12px;font-size:12px;color:var(--muted);white-space:nowrap}
#origine{max-width:calc(72ch + 96px);margin:0 auto;padding:22px 28px 0 88px;font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:var(--encre2)}
#origine + #texte{padding-top:18px}
#origine button{font:inherit;border:none;background:none;padding:0;margin-left:6px;color:#b8530f;text-decoration:underline;cursor:pointer}
#origine button:hover{color:#8a3d08}
#patience{max-width:560px;margin:16vh auto 0;padding:0 28px;text-align:center;font:17px/1.6 "Iowan Old Style","Palatino Linotype",Georgia,serif;color:var(--encre)}
#patience .rond{width:34px;height:34px;border-radius:50%;border:3px solid var(--filet);border-top-color:#e0621a;margin:0 auto 22px;animation:tour 1s linear infinite}
#patience.erreur .rond{display:none}
#patience .fil{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--encre2);margin-top:14px;white-space:pre-wrap;word-break:break-word}
#patience .ligne{justify-content:center;margin-top:22px}
#patience select,#patience .btn{background:#fff;color:var(--encre);border-color:#d5cebd}
@keyframes tour{to{transform:rotate(360deg)}}

/* ── Choix d'un fichier ── */
#choix{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:50}
.boite{width:640px;max-width:92vw;max-height:80vh;background:var(--panel);border:1px solid var(--border);border-radius:14px;display:flex;flex-direction:column;overflow:hidden}
.boite .tete{padding:12px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.boite .tete .ou{font:12px ui-monospace,monospace;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}
.boite .liste{overflow-y:auto;padding:8px}
.entree{display:flex;gap:10px;align-items:center;padding:8px 10px;border-radius:8px;cursor:pointer}
.entree:hover{background:#262a34}
.entree .n{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.entree .t{color:var(--muted);font-size:12px;flex:none}
.entree.dossier .n{font-weight:600}

@media (max-width:900px){
  #corps{flex-direction:column}
  #cote{width:auto;border-right:none;border-bottom:1px solid var(--border);max-height:56vh;overflow-y:auto;display:block}
  #ecran video{max-height:30vh}
  #passages{overflow:visible}
  #texte{font-size:17px;padding:22px 16px 40vh 8px}
  #origine{padding:16px 16px 0 8px}
}
</style>
</head>
<body>

<div id="accueil">
  <div class="carte">
    <h1>Montage au <mark>stabilo</mark></h1>
    <p class="chapeau">Choisissez une vidéo ou un enregistrement. Le texte s'affiche, vous surlignez ce que vous gardez, ou ce que vous retirez, et le montage se fait tout seul.</p>
    <div class="ligne">
      <input type="text" id="chemin" placeholder="Emplacement du fichier" spellcheck="false">
      <button class="btn" id="b-parcourir">Parcourir…</button>
    </div>
    <div class="ligne">
      <select id="langue" title="Langue parlée"></select>
      <button class="btn fort" id="b-ouvrir">Ouvrir</button>
    </div>
    <div id="erreur-accueil"></div>
    <div class="etiquette" id="t-recents" hidden>Reprendre</div>
    <div id="recents"></div>
  </div>
</div>

<div id="atelier" hidden>
  <div id="barre">
    <div id="titre"><div class="n"></div><div class="d"></div></div>
    <div class="outils" id="modes">
      <span>Je surligne</span>
      <button id="m-garder" class="actif" title="Le montage est fait de ce qui est surligné">ce que je garde</button>
      <button id="m-couper" title="Le montage est fait de tout le reste">ce que je coupe</button>
    </div>
    <div class="outils">
      <button id="o-stabilo" class="actif" title="Surligner (S)">Stabilo</button>
      <button id="o-gomme" title="Effacer le surlignage (G)">Gomme</button>
    </div>
    <button class="btn" id="b-annuler" title="Ctrl+Z" disabled>Annuler</button>
    <button class="btn" id="b-retablir" title="Ctrl+Maj+Z" disabled>Rétablir</button>
    <button class="btn" id="b-autre">Autre fichier</button>
  </div>
  <div id="corps">
    <div id="cote">
      <div id="ecran"></div>
      <div id="bilan">
        <div class="g" id="bilan-duree">Rien de surligné</div>
        <div class="p" id="bilan-detail"></div>
        <div class="ligne">
          <button class="btn" id="b-ecouter" disabled>▶ Voir le montage</button>
          <button class="btn" id="b-effacer" disabled>Tout effacer</button>
          <button class="btn" id="b-silences" hidden>Rayer les silences</button>
        </div>
      </div>
      <div id="passages"></div>
      <div id="production">
        <div id="reglages">
          <div class="champs">
            <label>Produire
              <select id="format"></select>
            </label>
            <label>Fondu à chaque coupe
              <select id="fondu">
                <option value="0">Aucun</option>
                <option value="0.2">Bref (0,2 s)</option>
                <option value="0.5">Normal (0,5 s)</option>
                <option value="1">Lent (1 s)</option>
                <option value="1.5">Très lent (1,5 s)</option>
              </select>
            </label>
            <label class="case" id="l-image"><input type="checkbox" id="fondu-image" checked> Fondu au noir de l'image</label>
            <label class="case" title="Dernière étape : le bruit de fond est retiré s'il y en a un, et le volume est mis à la norme de diffusion"><input type="checkbox" id="finaliser"> Finaliser le son (bruit de fond, volume)</label>
          </div>
          <button class="btn fort" id="b-produire" disabled>Produire le montage</button>
        </div>
        <div id="suivi" hidden>
          <div id="jauge"><i></i></div>
          <div id="suivi-texte"><span></span><button class="btn danger" id="b-arreter">Arrêter</button></div>
        </div>
        <div id="fini" hidden>
          <div><span class="ok">Montage terminé</span> · <span id="fini-detail"></span></div>
          <div class="chemin"></div>
          <div class="chemin" id="fini-journaux"></div>
          <div class="chemin" id="fini-finition"></div>
          <div class="ligne">
            <a class="btn fort" id="b-lire" target="_blank" style="text-decoration:none">Regarder</a>
            <button class="btn" id="b-dossier">Ouvrir le dossier</button>
            <button class="btn" id="b-encore">Fermer</button>
          </div>
        </div>
        <div id="echec" hidden></div>
      </div>
    </div>
    <div id="page">
      <div id="patience" hidden>
        <div class="rond"></div>
        <div class="quoi"></div>
        <div class="fil"></div>
        <div class="ligne" id="reprise" hidden>
          <select id="langue2"></select>
          <button class="btn" id="b-reessayer">Recommencer la transcription</button>
        </div>
      </div>
      <div id="origine" hidden><span></span><button id="b-texte"></button></div>
      <div id="texte"></div>
    </div>
  </div>
</div>

<div id="choix" hidden>
  <div class="boite">
    <div class="tete">
      <button class="btn" id="c-haut">Dossier parent</button>
      <div class="ou"></div>
      <button class="btn" id="c-fermer">Fermer</button>
    </div>
    <div class="liste"></div>
  </div>
</div>

<script>
const BASE = "__BASE__";
const $ = s => document.querySelector(s);
const esc = s => s.replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

let CONFIG = null;
let P = null;            // projet ouvert
let J = [];              // jetons [texte, début, fin, phrase]
let origineTexte = '';   // '' : transcription ; sinon doublage, sous-titres, origine
let sel = new Uint8Array(0);
let spans = [];
let histo = [], futur = [];
let outil = 'stabilo';
let mode = 'garder';     // 'garder' : le surligné fait le montage ; 'couper' : il en est retiré
let coupes = [];
let lecteur = null;
let envoi = null, numeroEnvoi = 0;
let montage = -1;        // indice de la coupe en cours d'écoute, -1 = lecture normale
let tache = null;
let veille = null;

function hms(t){
  t = Math.max(0, Math.floor(t));
  const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  return (h ? h + ':' + String(m).padStart(2,'0') : m) + ':' + String(s).padStart(2,'0');
}
function court(t){      // durée d'un passage sans paroles : les dixièmes comptent quand c'est bref
  return t < 10 ? t.toFixed(1).replace('.', ',') + ' s' : enClair(t);
}
function enClair(t){
  t = Math.round(t);
  if (t < 60) return t + ' s';
  const h = Math.floor(t/3600), m = Math.floor(t%3600/60), s = t%60;
  if (h) return h + ' h ' + String(m).padStart(2,'0');
  return m + ' min' + (s ? ' ' + String(s).padStart(2,'0') : '');
}
function poids(o){
  if (o > 1e9) return (o/1e9).toFixed(1).replace('.', ',') + ' Go';
  if (o > 1e6) return Math.round(o/1e6) + ' Mo';
  return Math.max(1, Math.round(o/1e3)) + ' ko';
}
async function api(url, corps){
  const r = await fetch(BASE + url, corps === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(corps)});
  let d = null;
  try { d = await r.json(); } catch(e) {}
  if (!r.ok) throw new Error((d && d.erreur) || 'La demande a échoué.');
  return d;
}

/* ── Accueil ─────────────────────────────────────────────── */
async function demarrer(){
  CONFIG = await api('/api/config');
  for (const id of ['langue', 'langue2'])
    $('#'+id).innerHTML = CONFIG.langues.map(l => `<option value="${l[0]}">${l[1]}</option>`).join('');
  $('#fondu').value = String(CONFIG.fondu);
  const voulu = new URLSearchParams(location.search).get('ouvrir');
  if (voulu) { $('#chemin').value = voulu; ouvrir(voulu); }
  else recents();
}
async function recents(){
  const liste = await api('/api/projets');
  $('#t-recents').hidden = !liste.length;
  $('#recents').innerHTML = liste.map(p => {
    const suite = !p.present ? 'fichier introuvable'
      : p.passages ? p.passages + (p.passages > 1 ? ' passages surlignés' : ' passage surligné')
      : p.transcrit ? 'texte prêt' : 'à transcrire';
    return `<div class="recent${p.present ? '' : ' absent'}" data-c="${esc(p.source)}" title="${esc(p.source)}">
      <div class="n">${esc(p.nom)}</div><div class="d">${enClair(p.duree)} · ${suite}</div></div>`;
  }).join('');
}
$('#recents').onclick = e => {
  const r = e.target.closest('.recent');
  if (r && !r.classList.contains('absent')) ouvrir(r.dataset.c);
};
$('#b-ouvrir').onclick = () => ouvrir($('#chemin').value);
$('#chemin').onkeydown = e => { if (e.key === 'Enter') ouvrir($('#chemin').value); };

async function ouvrir(chemin){
  $('#erreur-accueil').textContent = '';
  if (!chemin.trim()) { $('#erreur-accueil').textContent = 'Indiquez un fichier.'; return; }
  try {
    const p = await api('/api/ouvrir', {chemin, langue: $('#langue').value});
    installer(p);
  } catch(e) {
    $('#accueil').hidden = false; $('#atelier').hidden = true;
    $('#erreur-accueil').textContent = e.message;
    recents();
  }
}

/* ── Choix d'un fichier ──────────────────────────────────── */
let ici = '';
async function parcourir(chemin){
  const d = await api('/api/parcourir?chemin=' + encodeURIComponent(chemin));
  ici = d.chemin;
  $('#choix .ou').textContent = d.chemin;
  $('#c-haut').disabled = !d.parent || d.parent === d.chemin;
  $('#c-haut').dataset.c = d.parent;
  const lignes = d.dossiers.map(n =>
    `<div class="entree dossier" data-d="${esc(n)}"><div class="n">${esc(n)}</div><div class="t">dossier</div></div>`
  ).concat(d.fichiers.map(f =>
    `<div class="entree" data-f="${esc(f.nom)}"><div class="n">${esc(f.nom)}</div><div class="t">${f.type === 'video' ? 'vidéo' : 'son'} · ${poids(f.taille)}</div></div>`));
  $('#choix .liste').innerHTML = lignes.join('') ||
    '<div class="entree"><div class="t">Aucune vidéo ni enregistrement dans ce dossier.</div></div>';
  $('#choix .liste').scrollTop = 0;
}
$('#b-parcourir').onclick = () => {
  $('#choix').hidden = false;
  const c = $('#chemin').value.trim();
  parcourir(c || ici || CONFIG.depart);
};
$('#c-fermer').onclick = () => { $('#choix').hidden = true; };
$('#c-haut').onclick = e => parcourir(e.target.dataset.c);
$('#choix').onclick = e => { if (e.target.id === 'choix') $('#choix').hidden = true; };
$('#choix .liste').onclick = e => {
  const l = e.target.closest('.entree');
  if (!l) return;
  const base = ici.endsWith('/') ? ici : ici + '/';
  if (l.dataset.d !== undefined) parcourir(base + l.dataset.d);
  else if (l.dataset.f !== undefined) {
    $('#chemin').value = base + l.dataset.f;
    $('#choix').hidden = true;
    ouvrir($('#chemin').value);
  }
};

/* ── Atelier ─────────────────────────────────────────────── */
function installer(p){
  P = p; J = []; sel = new Uint8Array(0); spans = []; histo = []; futur = [];
  coupes = []; montage = -1; tache = null; lecteur = null;
  afficherMode('garder');
  clearTimeout(veille);
  $('#accueil').hidden = true; $('#atelier').hidden = false;
  document.title = p.nom + ' — Montage au stabilo';
  $('#titre .n').textContent = p.nom;
  $('#titre .d').textContent = (p.type === 'video' ? 'Vidéo' : 'Enregistrement') + ' · ' + enClair(p.duree);
  $('#texte').innerHTML = ''; $('#origine').hidden = true;
  $('#ecran').innerHTML = ''; $('#ecran').className = p.type === 'video' ? '' : 'son';
  $('#format').innerHTML = (p.type === 'video' ? '<option value="mp4">Une vidéo (MP4)</option>' : '')
    + '<option value="mp3">Un son (MP3)</option><option value="m4a">Un son (M4A)</option><option value="wav">Un son (WAV)</option>';
  reglerFormat();
  for (const id of ['suivi', 'fini', 'echec']) $('#'+id).hidden = true;
  $('#reglages').hidden = false;
  bilan(); boutons();
  suivreProjet(p);
}
function reglerFormat(){
  const video = $('#format').value === 'mp4';
  $('#l-image').hidden = !video;
  $('#b-lire').textContent = video ? 'Regarder' : 'Écouter';
  $('#b-ecouter').textContent = (montage >= 0 ? '■ Arrêter' :
    (P && P.type === 'video' ? '▶ Voir le montage' : '▶ Écouter le montage'));
}
$('#format').onchange = reglerFormat;
// La case « Finaliser le son » reste comme on l'a laissée
try { $('#finaliser').checked = localStorage.getItem('montage-finaliser') === '1'; } catch(e) {}
$('#finaliser').onchange = e => {
  try { localStorage.setItem('montage-finaliser', e.target.checked ? '1' : '0'); } catch(err) {}
};

function poserLecteur(){
  if (lecteur || !P) return;
  lecteur = document.createElement(P.type === 'video' ? 'video' : 'audio');
  lecteur.controls = true; lecteur.preload = 'metadata';
  lecteur.src = BASE + '/media/' + P.id;
  lecteur.addEventListener('seeking', () => { if (!sautVoulu) finMontage(); sautVoulu = false; });
  $('#ecran').innerHTML = ''; $('#ecran').appendChild(lecteur);
}

async function suivreProjet(p){
  if (!P || p.id !== P.id) return;
  if (p.apercu === 'pret') poserLecteur();
  else if (!lecteur) $('#ecran').innerHTML = '<div id="attente-apercu">' +
    (p.apercu === 'erreur' ? 'Ce fichier ne peut pas être lu ici. Le texte et le montage restent possibles.'
                           : 'Préparation de la lecture…') + '</div>';
  const pat = $('#patience');
  if (p.etat === 'pret') {
    pat.hidden = true;
    if (!J.length) await chargerTexte();
    if (p.apercu === 'pret' || p.apercu === 'erreur') return;
  } else if (p.etat === 'transcription' || p.etat === 'attente') {
    pat.hidden = false; pat.className = ''; $('#reprise').hidden = true;
    pat.querySelector('.quoi').textContent = (p.reprise
      ? 'Reprise du texte ' + (NOMS_TEXTE[p.reprise] || 'déjà fait')
      : 'Transcription en cours' + (p.depuis ? ' depuis ' + enClair(p.depuis) : '')) + '…';
    pat.querySelector('.fil').textContent = (p.journal || []).join('\n').replace(/^\s+/, '');
  } else {
    pat.hidden = false; pat.className = 'erreur'; $('#reprise').hidden = false;
    pat.querySelector('.quoi').textContent = 'La transcription a échoué.';
    pat.querySelector('.fil').textContent = (p.journal || []).join('\n');
    return;
  }
  veille = setTimeout(async () => {
    try { suivreProjet(await api('/api/projet/' + p.id)); } catch(e) {}
  }, 1500);
}
/* Un fichier déjà sous-titré ou doublé : son texte est repris, et l'on peut
   toujours passer de ce texte à la transcription du son, ou l'inverse. */
const NOMS_TEXTE = {doublage: 'du doublage', 'sous-titres': 'des sous-titres', origine: 'déjà transcrit'};
function afficherOrigine(origine){
  const o = $('#origine'), b = $('#b-texte');
  let texte = '', bouton = '', vers = 'son';
  if (origine === 'doublage') {
    texte = "Texte du doublage, repris sans nouvelle transcription. Chaque phrase est à sa place ; à l'intérieur d'une phrase, la place des mots est estimée.";
    bouton = 'Transcrire le son à la place';
  } else if (origine === 'sous-titres') {
    texte = "Texte des sous-titres, repris sans nouvelle transcription. La voix parle une autre langue : chaque sous-titre est à sa place, la place des mots à l'intérieur est estimée.";
    bouton = 'Transcrire le son à la place';
  } else if (origine === 'origine') {
    texte = 'Texte repris de la traduction de cette vidéo, sans nouvelle transcription.';
    bouton = 'Transcrire à nouveau';
  } else if (P.existant === 'doublage' || P.existant === 'sous-titres') {
    texte = "Texte transcrit d'après le son.";
    bouton = 'Afficher le texte ' + NOMS_TEXTE[P.existant];
    vers = 'existant';
  }
  o.hidden = !texte;
  o.querySelector('span').textContent = texte;
  b.textContent = bouton; b.dataset.vers = vers;
}
$('#b-texte').onclick = async e => {
  if (tache) return;
  try {
    const p = await api('/api/retranscrire/' + P.id, {texte: e.target.dataset.vers});
    finMontage(); clearTimeout(veille);
    P = Object.assign(P, p);
    J = []; sel = new Uint8Array(0); spans = []; histo = []; futur = []; coupes = [];
    $('#texte').innerHTML = ''; $('#origine').hidden = true;
    bilan(); boutons();
    suivreProjet(p);
  } catch(err) { $('#origine span').textContent = err.message; }
};
$('#b-reessayer').onclick = async () => {
  try { suivreProjet(await api('/api/retranscrire/' + P.id, {langue: $('#langue2').value})); }
  catch(e) { $('#patience .fil').textContent = e.message; }
};
$('#b-autre').onclick = () => {
  if (lecteur) lecteur.pause();
  clearTimeout(veille); P = null;
  $('#atelier').hidden = true; $('#accueil').hidden = false;
  document.title = 'Montage au stabilo';
  history.replaceState(null, '', BASE + '/');
  recents();
};

async function chargerTexte(){
  const d = await api('/api/transcription/' + P.id);
  J = d.jetons;
  origineTexte = d.origine;
  afficherOrigine(d.origine);
  sel = new Uint8Array(J.length);
  for (const [a, b] of d.plages)
    for (let i = Math.max(0, a); i <= Math.min(J.length - 1, b); i++) sel[i] = 1;
  if ([...$('#fondu').options].some(o => parseFloat(o.value) === d.fondu)) $('#fondu').value = String(d.fondu);
  afficherMode(d.mode);
  rendre(); peindre(0, J.length - 1); bilan(); boutons();
  if (plages().length) envoyer(0);
}

function rendre(){
  const out = [];
  const sansMots = {doublage: 'sans doublage', 'sous-titres': 'sans sous-titres'}[origineTexte] || 'sans paroles';
  let ouvert = false, car = 0, precP = null, precF = 0;
  for (let i = 0; i < J.length; i++) {
    const [t, d, f, p] = J[i];
    if (t === null) {
      if (ouvert) { out.push('</p></div>'); ouvert = false; }
      const ou = i === 0 ? 'Début · ' : (i === J.length - 1 ? 'Fin · ' : '');
      out.push(`<div class="par"><a class="hor" data-t="${d}">${hms(d)}</a><p><span class="m pause" data-i="${i}" title="Cliquer pour marquer ou démarquer">${ou}${court(f - d)} ${sansMots}</span></p></div>`);
      continue;
    }
    if (!ouvert || (p !== precP && (d - precF >= 0.9 || car > 420))) {
      if (ouvert) out.push('</p></div>');
      out.push(`<div class="par"><a class="hor" data-t="${d}">${hms(d)}</a><p>`);
      ouvert = true; car = 0;
    }
    out.push(`<span class="m" data-i="${i}">${esc(t)}</span> `);
    car += t.length + 1; precP = p; precF = f;
  }
  if (ouvert) out.push('</p></div>');
  $('#texte').innerHTML = out.join('');
  spans = new Array(J.length);
  for (const s of $('#texte').querySelectorAll('.m')) spans[+s.dataset.i] = s;
}

function peindre(a, b){
  a = Math.max(0, a - 1); b = Math.min(J.length - 1, b + 1);
  for (let i = a; i <= b; i++) {
    const c = spans[i].classList, s = sel[i] === 1;
    c.toggle('s', s);
    c.toggle('sd', s && (i === 0 || !sel[i-1]));
    c.toggle('sf', s && (i === J.length - 1 || !sel[i+1]));
  }
}
function plages(){
  const r = [];
  let a = -1;
  for (let i = 0; i <= J.length; i++) {
    const s = i < J.length && sel[i] === 1;
    if (s && a < 0) a = i;
    if (!s && a >= 0) { r.push([a, i - 1]); a = -1; }
  }
  return r;
}
function changer(a, b, valeur){
  if (a > b) [a, b] = [b, a];
  let utile = false;
  for (let i = a; i <= b; i++) if (sel[i] !== valeur) { utile = true; break; }
  if (!utile) return;
  histo.push(sel.slice()); if (histo.length > 200) histo.shift();
  futur = [];
  sel.fill(valeur, a, b + 1);
  peindre(a, b); apresChangement();
}
function apresChangement(){
  finMontage(); boutons(); envoyer(350);
}
function boutons(){
  const n = plages().length;
  $('#b-annuler').disabled = !histo.length;
  $('#b-retablir').disabled = !futur.length;
  $('#b-effacer').disabled = !n;
  // Proposé en mode coupe, tant qu'il reste un passage sans paroles non rayé
  let libres = 0;
  for (let i = 0; i < J.length; i++) if (J[i][0] === null && !sel[i]) libres++;
  $('#b-silences').hidden = mode !== 'couper' || !libres;
  $('#b-silences').textContent = libres > 1 ? 'Rayer les ' + libres + ' silences' : 'Rayer le silence';
  $('#b-produire').disabled = !n || !coupes.length || (tache !== null);
  $('#b-ecouter').disabled = !n || !coupes.length || !lecteur;
}
function annuler(){
  if (!histo.length) return;
  futur.push(sel.slice()); sel = histo.pop();
  peindre(0, J.length - 1); apresChangement();
}
function retablir(){
  if (!futur.length) return;
  histo.push(sel.slice()); sel = futur.pop();
  peindre(0, J.length - 1); apresChangement();
}
$('#b-annuler').onclick = annuler;
$('#b-retablir').onclick = retablir;
$('#b-effacer').onclick = () => changer(0, J.length - 1, 0);
$('#b-silences').onclick = () => {
  const avant = sel.slice();
  let change = false;
  for (let i = 0; i < J.length; i++) if (J[i][0] === null && !sel[i]) { sel[i] = 1; change = true; }
  if (!change) return;
  histo.push(avant); if (histo.length > 200) histo.shift();
  futur = [];
  peindre(0, J.length - 1); apresChangement();
};

function choisirOutil(o){
  outil = o;
  $('#o-stabilo').classList.toggle('actif', o === 'stabilo');
  $('#o-gomme').classList.toggle('actif', o === 'gomme');
  document.body.classList.toggle('gomme', o === 'gomme');
}
$('#o-stabilo').onclick = () => choisirOutil('stabilo');
function afficherMode(m){
  mode = m === 'couper' ? 'couper' : 'garder';
  $('#m-garder').classList.toggle('actif', mode === 'garder');
  $('#m-couper').classList.toggle('actif', mode === 'couper');
  document.body.classList.toggle('couper', mode === 'couper');
  $('#o-stabilo').title = (mode === 'couper' ? 'Rayer' : 'Surligner') + ' (S)';
}
function choisirMode(m){
  if (m === mode || !J.length) return;
  afficherMode(m);
  finMontage(); coupes = []; bilan(); boutons();
  envoyer(0);
}
$('#m-garder').onclick = () => choisirMode('garder');
$('#m-couper').onclick = () => choisirMode('couper');
$('#o-gomme').onclick = () => choisirOutil('gomme');

/* ── Geste du stabilo ────────────────────────────────────── */
let ancre = -1, bout = -1, glisse = false, dernier = -1, gommeGeste = false;
let apercuA = -1, apercuB = -1, defile = 0, defileur = null;

function jetonSous(x, y){
  const el = document.elementFromPoint(x, y);
  const m = el && el.closest ? el.closest('.m') : null;
  return m && m.dataset.i !== undefined ? +m.dataset.i : -1;
}
function apercu(a, b){
  for (let i = apercuA; i >= 0 && i <= apercuB; i++) spans[i].classList.remove('pre', 'preg');
  apercuA = a; apercuB = b;
  for (let i = a; i >= 0 && i <= b; i++) spans[i].classList.add(gommeGeste ? 'preg' : 'pre');
}
const texte = $('#texte');
texte.addEventListener('pointerdown', e => {
  if (e.button !== 0 || !J.length) return;
  const i = jetonSous(e.clientX, e.clientY);
  if (i < 0) return;
  e.preventDefault();
  gommeGeste = (outil === 'gomme') !== e.altKey;
  if (e.shiftKey && dernier >= 0) { changer(dernier, i, gommeGeste ? 0 : 1); dernier = i; return; }
  ancre = bout = i; glisse = false;
  texte.setPointerCapture(e.pointerId);
});
texte.addEventListener('pointermove', e => {
  if (ancre < 0) return;
  const r = $('#page').getBoundingClientRect();
  defile = e.clientY < r.top + 50 ? -1 : e.clientY > r.bottom - 50 ? 1 : 0;
  if (defile && !defileur) defileur = setInterval(() => {
    if (!defile || ancre < 0) { clearInterval(defileur); defileur = null; return; }
    $('#page').scrollTop += defile * 14;
  }, 16);
  const i = jetonSous(e.clientX, e.clientY);
  if (i < 0 || i === bout) return;
  bout = i;
  if (bout !== ancre) glisse = true;
  if (glisse) apercu(Math.min(ancre, bout), Math.max(ancre, bout));
});
function finGeste(e, valide){
  if (ancre < 0) return;
  const a = ancre, b = bout, g = glisse;
  ancre = -1; defile = 0;
  apercu(-1, -1);
  if (!valide) return;
  if (g) { changer(a, b, gommeGeste ? 0 : 1); dernier = b; }
  else if (J[a][0] === null) { dernier = a; changer(a, a, sel[a] ? 0 : 1); }   // un passage sans paroles se marque d'un clic
  else { dernier = a; aller(J[a][1], true); }
}
texte.addEventListener('pointerup', e => finGeste(e, true));
texte.addEventListener('pointercancel', e => finGeste(e, false));
texte.addEventListener('dblclick', e => {
  const i = jetonSous(e.clientX, e.clientY);
  if (i < 0 || J[i][0] === null) return;
  let a = i, b = i;
  if (J[i][0] !== null) {
    while (a > 0 && J[a-1][3] === J[i][3]) a--;
    while (b < J.length - 1 && J[b+1][3] === J[i][3]) b++;
  }
  let plein = true;
  for (let k = a; k <= b; k++) if (!sel[k]) { plein = false; break; }
  changer(a, b, (plein || outil === 'gomme') ? 0 : 1);
});
texte.addEventListener('click', e => {
  const h = e.target.closest('.hor');
  if (h) aller(parseFloat(h.dataset.t), true);
});

/* ── Lecture ─────────────────────────────────────────────── */
let sautVoulu = false, lu = -1;
function aller(t, jouer){
  if (!lecteur) return;
  finMontage();
  sautVoulu = true;
  lecteur.currentTime = Math.max(0, t);
  if (jouer) lecteur.play().catch(() => {});
}
function jetonA(t){
  let a = 0, b = J.length - 1, r = -1;
  while (a <= b) {
    const m = (a + b) >> 1;
    if (J[m][1] <= t) { r = m; a = m + 1; } else b = m - 1;
  }
  return r;
}
function finMontage(){
  if (montage < 0) return;
  montage = -1; reglerFormat();
}
$('#b-ecouter').onclick = () => {
  if (!lecteur) return;
  if (montage >= 0) { lecteur.pause(); finMontage(); return; }
  if (!coupes.length) return;
  montage = 0; reglerFormat();
  sautVoulu = true;
  lecteur.currentTime = coupes[0].d;
  lecteur.play().catch(() => {});
};
function boucle(){
  requestAnimationFrame(boucle);
  if (!lecteur || !J.length) return;
  const t = lecteur.currentTime;
  if (montage >= 0 && !lecteur.seeking) {
    const c = coupes[montage];
    if (!c) finMontage();
    else if (t >= c.f - 0.03) {
      montage++;
      if (montage < coupes.length) { sautVoulu = true; lecteur.currentTime = coupes[montage].d; }
      else { lecteur.pause(); finMontage(); }
    }
  }
  const i = jetonA(t + 0.05);
  if (i !== lu) {
    if (lu >= 0 && spans[lu]) spans[lu].classList.remove('lu');
    lu = i;
    if (lu >= 0 && spans[lu]) {
      spans[lu].classList.add('lu');
      if (!lecteur.paused && ancre < 0) {
        const r = spans[lu].getBoundingClientRect(), p = $('#page').getBoundingClientRect();
        if (r.bottom > p.bottom - 40 || r.top < p.top + 10)
          $('#page').scrollTo({top: $('#page').scrollTop + r.top - p.top - p.height * 0.3, behavior: 'smooth'});
      }
    }
  }
}
requestAnimationFrame(boucle);

/* ── Bilan et passages ───────────────────────────────────── */
function envoyer(delai){
  clearTimeout(envoi);
  envoi = setTimeout(async () => {
    const n = ++numeroEnvoi, pid = P.id;
    try {
      const d = await api('/api/selection/' + pid, {plages: plages(), mode, fondu: parseFloat($('#fondu').value)});
      if (n !== numeroEnvoi || !P || P.id !== pid) return;
      coupes = d.coupes; bilan(d.duree); boutons();
    } catch(e) {}
  }, delai);
}
$('#fondu').onchange = () => envoyer(0);

function extrait(a, b){
  const mots = [];
  for (let i = a; i <= b && mots.length < 16; i++) if (J[i][0] !== null) mots.push(J[i][0]);
  return esc(mots.join(' ')) || 'Sans paroles';
}
function ligne(a, b, t, duree, titre){
  return `<div class="passage" data-a="${a}" data-b="${b}" data-t="${t}"><div class="h">${hms(t)}</div>
    <div class="x">${extrait(a, b)}</div><div class="l">${enClair(duree)}</div>
    <button class="r" title="${titre}">×</button></div>`;
}
function bilan(duree){
  const n = coupes.length, couper = mode === 'couper';
  if (!J.length) { $('#bilan-duree').textContent = '—'; $('#bilan-detail').textContent = ''; $('#passages').innerHTML = ''; return; }
  const pl = plages();
  if (!pl.length) {
    coupes = [];
    $('#bilan-duree').textContent = couper ? 'Rien de rayé' : 'Rien de surligné';
    $('#bilan-detail').textContent = '';
    $('#passages').innerHTML = '<div id="vide">' + (couper
      ? 'Glissez sur le texte pour rayer ce que vous retirez : le montage garde tout le reste.'
      : 'Glissez sur le texte pour surligner ce que vous gardez.')
      + '<br>Un clic sur un mot lance la lecture à cet endroit. Un clic sur un passage sans paroles le marque. Un double clic prend la phrase entière. Pour un long passage, cliquez sur son premier mot puis, majuscule enfoncée, sur son dernier.</div>';
    return;
  }
  if (duree === undefined) return;
  if (!couper) {
    $('#bilan-duree').textContent = 'Montage de ' + enClair(duree);
    $('#bilan-detail').textContent = n + (n > 1 ? ' passages' : ' passage') + ' sur ' + enClair(P.duree);
    // Un morceau gardé peut réunir deux passages surlignés voisins
    $('#passages').innerHTML = coupes.map(c => ligne(c.plage[0], c.plage[1], c.d, c.f - c.d, 'Retirer ce passage')).join('');
    return;
  }
  // Mode « ce que je coupe » : la liste montre ce qui est retiré
  $('#bilan-duree').textContent = n ? 'Montage de ' + enClair(duree) : 'Il ne reste rien';
  const retire = Math.max(0, P.duree - duree);
  $('#bilan-detail').textContent = pl.length + (pl.length > 1 ? ' coupes' : ' coupe') + ' · '
    + enClair(retire) + ' en moins sur ' + enClair(P.duree);
  $('#passages').innerHTML = pl.map(([a, b]) => ligne(a, b, J[a][1], J[b][2] - J[a][1], 'Garder ce passage')).join('');
}
$('#passages').onclick = e => {
  const l = e.target.closest('.passage');
  if (!l) return;
  const a = +l.dataset.a, b = +l.dataset.b;
  if (e.target.closest('.r')) { changer(a, b, 0); return; }
  spans[a].scrollIntoView({block: 'center', behavior: 'smooth'});
  aller(parseFloat(l.dataset.t), false);
};

/* ── Production ──────────────────────────────────────────── */
$('#b-produire').onclick = async () => {
  $('#echec').hidden = true;
  try {
    const d = await api('/api/produire/' + P.id, {
      plages: plages(), mode, fondu: parseFloat($('#fondu').value),
      format: $('#format').value, fondu_image: $('#fondu-image').checked,
      finaliser: $('#finaliser').checked});
    tache = d.id;
    $('#reglages').hidden = true; $('#fini').hidden = true; $('#suivi').hidden = false;
    $('#jauge i').style.width = '0';
    boutons(); suivreTache();
  } catch(e) { $('#echec').hidden = false; $('#echec').textContent = e.message; }
};
async function suivreTache(){
  if (!tache) return;
  let t;
  try { t = await api('/api/tache/' + tache); }
  catch(e) { setTimeout(suivreTache, 1500); return; }
  if (t.etat === 'encours') {
    $('#jauge i').style.width = Math.round(t.progression * 100) + '%';
    $('#suivi-texte span').textContent = t.message + ' · ' + Math.round(t.progression * 100) + ' %';
    setTimeout(suivreTache, 600);
    return;
  }
  $('#suivi').hidden = true;
  if (t.etat === 'fini') {
    $('#fini').hidden = false;
    $('#fini-detail').textContent = enClair(t.duree) + (t.taille ? ' · ' + poids(t.taille) : '');
    $('#fini .chemin').textContent = t.sortie;
    const noms = (t.journaux || []).map(c => c.split('/').pop().split('.').pop());
    $('#fini-journaux').textContent = noms.length
      ? 'Dans le même dossier, sous le même nom : le journal des coupes (.' + noms.join(') et la liste de montage (.') + ')'
      : '';
    $('#fini-finition').className = 'chemin' + (t.finition === 'echec' ? ' echec' : '');
    $('#fini-finition').textContent = t.finition === 'faite' ? 'Son finalisé : fond vérifié, volume à la norme.'
      : t.finition === 'echec' ? "Le son n'a pas pu être finalisé : le montage est resté tel qu'il a été monté." : '';
    $('#b-lire').href = BASE + '/resultat/' + t.id;
    $('#b-lire').textContent = t.format === 'mp4' ? 'Regarder' : 'Écouter';
    $('#b-dossier').dataset.t = t.id;
  } else {
    $('#reglages').hidden = false;
    if (t.etat === 'erreur') { $('#echec').hidden = false; $('#echec').textContent = 'Le montage n\'a pas pu être produit.\n' + t.message; }
  }
  tache = null; boutons();
}
$('#b-arreter').onclick = () => { if (tache) api('/api/tache/' + tache + '/arreter', {}); };
$('#b-dossier').onclick = e => api('/api/tache/' + e.target.dataset.t + '/dossier', {}).catch(() => {});
$('#b-encore').onclick = () => { $('#fini').hidden = true; $('#reglages').hidden = false; reglerFormat(); };

/* ── Clavier ─────────────────────────────────────────────── */
document.addEventListener('keydown', e => {
  if ($('#atelier').hidden || !$('#choix').hidden) return;
  if (e.target.matches('input, select, textarea')) return;
  const k = e.key.toLowerCase();
  if ((e.ctrlKey || e.metaKey) && k === 'z') { e.preventDefault(); e.shiftKey ? retablir() : annuler(); return; }
  if ((e.ctrlKey || e.metaKey) && k === 'y') { e.preventDefault(); retablir(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  if (k === ' ' && lecteur) { e.preventDefault(); lecteur.paused ? lecteur.play().catch(() => {}) : lecteur.pause(); }
  else if (k === 's') choisirOutil('stabilo');
  else if (k === 'g') choisirOutil('gomme');
  else if (k === 'arrowleft' && lecteur) { e.preventDefault(); aller(lecteur.currentTime - 5, false); }
  else if (k === 'arrowright' && lecteur) { e.preventDefault(); aller(lecteur.currentTime + 5, false); }
});

demarrer();
</script>
</body>
</html>
"""


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Montage vidéo et audio au stabilo : on surligne le texte, "
                    "le montage suit.")
    parser.add_argument("fichier", nargs="?", help="Vidéo ou enregistrement à ouvrir")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--sans-navigateur", action="store_true",
                        help="Ne pas ouvrir le navigateur")
    parser.add_argument("--langue", default="", help=argparse.SUPPRESS)
    parser.add_argument("--tache-transcrire", metavar="DOSSIER",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.tache_transcrire:
        tache_transcrire(args.tache_transcrire, args.langue)
        return

    try:
        import flask  # noqa: F401
    except ImportError:
        print("❌ Flask n'est pas installé dans cet interpréteur.")
        print(f"   Lance plutôt :  {PYTHON_BIN} monter.py")
        sys.exit(1)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("❌ ffmpeg introuvable. Installe-le : sudo apt install ffmpeg")
        sys.exit(1)

    url = f"http://{HOST}:{args.port}/"
    if args.fichier:
        from urllib.parse import quote
        from clipper import resolve_source
        url += "?ouvrir=" + quote(str(resolve_source(args.fichier).resolve()))

    print("=" * 60)
    print("  🖍️  Montage au stabilo")
    print("=" * 60)
    print(f"  Interpréteur : {PYTHON_BIN}")
    print(f"  Montages     : {OUTPUT_DIR}")
    print(f"  → {url}")
    print("=" * 60)
    if not args.sans_navigateur:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    creer_app().run(host=HOST, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
