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

FONDU_DEFAUT = 0.5          # secondes
FONDU_MAX = 3.0
ECART_FUSION = 0.30         # deux coupes plus proches que ça n'en font qu'une
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
    return jetons


def tache_transcrire(dossier: str, langue: str):
    """Point d'entrée du sous-processus de transcription."""
    dossier = Path(dossier)
    projet = lire_json(dossier / "projet.json")
    if not projet:
        print(f"❌ Projet illisible : {dossier}")
        sys.exit(1)

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
                {"langue": detectee, "jetons": jetons})
    n_mots = sum(1 for j in jetons if j[0] is not None)
    print(f"   ✅ Texte prêt : {n_mots} mots")


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


def calculer_coupes(dossier: Path, jetons: list, plages: list, fondu: float,
                    duree: float) -> list:
    """Plages de jetons [i, j] → coupes {d, f, fi, fo, plage} en secondes."""
    n = len(jetons)
    propres = []
    for p in plages:
        try:
            i, j = int(p[0]), int(p[1])
        except (TypeError, ValueError, IndexError):
            continue
        i, j = max(0, min(i, j)), min(n - 1, max(i, j))
        if i <= j:
            propres.append((i, j))
    propres.sort()
    if not propres:
        return []

    fin_media = duree if duree > 0 else jetons[-1][2]
    cible_av, cible_ap, portee = marges(fondu)

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

            a = max(0.0, fin_prec - 0.04, d_brut - portee)
            d = point_calme(son, min(a, d_brut), d_brut, d_brut - cible_av)
            b = min(fin_media, deb_suiv + 0.04, f_brut + portee)
            f = point_calme(son, f_brut, max(b, f_brut), f_brut + cible_ap)
            if f - d < 0.05:
                continue

            if fondu > 0:
                fi = min(max(d_brut - d, 0.03), fondu)
                fo = min(max(f - f_brut, 0.03), fondu)
            else:
                fi = fo = ANTI_CLIC

            if coupes and d - coupes[-1]["f"] < ECART_FUSION:
                coupes[-1]["f"] = max(f, coupes[-1]["f"])
                coupes[-1]["fo"] = fo
                coupes[-1]["plage"][1] = j
            else:
                coupes.append({"d": d, "f": f, "fi": fi, "fo": fo, "plage": [i, j]})
    finally:
        if son is not None:
            son.close()

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
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = Path(source).stem
    sortie = OUTPUT_DIR / f"{base}_montage.{ext}"
    k = 2
    while sortie.exists():
        sortie = OUTPUT_DIR / f"{base}_montage-{k}.{ext}"
        k += 1
    return sortie


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
                vf.append(f"fade=t=in:st=0:d={fv:.3f}")
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


# ═══════════════════════════════════════════════════════════════════════════════
# SERVEUR
# ═══════════════════════════════════════════════════════════════════════════════

TRANSCRIPTIONS = {}     # id projet → {proc, lignes, debut}
APERCUS = {}            # id projet → "encours" | "erreur"
TACHES = {}             # id tâche → dict
JETONS = {}             # id projet → (mtime, jetons, langue)
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
    with VERROU:
        JETONS[pid] = (mtime, data["jetons"], data.get("langue", ""))
    return data["jetons"], data.get("langue", "")


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
    t = TRANSCRIPTIONS.get(pid)
    if (dossier / "transcription.json").exists() and not (t and t["proc"].poll() is None):
        e["etat"] = "pret"
        _, e["langue"] = charger_jetons(dossier, pid)
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
        for nom in ("transcription.json", "selection.json"):
            if (dossier / nom).exists():
                (dossier / nom).unlink()
        lancer_transcription(dossier, projet, (data.get("langue") or "").strip().lower())
        return jsonify(etat_du_projet(dossier, projet))

    @app.route("/api/transcription/<pid>")
    def api_transcription(pid):
        dossier, projet = projet_ou_404(pid)
        jetons, langue = charger_jetons(dossier, pid)
        if jetons is None:
            abort(404)
        sel = lire_json(dossier / "selection.json", {}) or {}
        return jsonify({"jetons": jetons, "langue": langue,
                        "plages": sel.get("plages", []),
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
        coupes = calculer_coupes(dossier, jetons, plages, fondu,
                                 projet.get("duree") or 0.0)
        ecrire_json(dossier / "selection.json", {"plages": plages, "fondu": fondu})
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
            return jsonify({"erreur": "Aucun passage n'est surligné."}), 400
        if not os.path.isfile(projet["source"]):
            return jsonify({"erreur": "Le fichier d'origine est introuvable."}), 400
        for t in TACHES.values():
            if t["projet"] == pid and t["etat"] == "encours":
                return jsonify({"erreur": "Un montage est déjà en cours de production."}), 409

        sortie = chemin_de_sortie(projet["source"], fmt)
        tid = uuid.uuid4().hex[:12]
        tache = {"id": tid, "projet": pid, "etat": "encours", "progression": 0.0,
                 "message": "Préparation", "sortie": str(sortie), "proc": None,
                 "annulee": False, "format": fmt,
                 "duree": round(sum(c["f"] - c["d"] for c in coupes), 2)}
        TACHES[tid] = tache

        def travail():
            try:
                if fmt == "mp4":
                    produire_video(tache, projet["source"], coupes, fondu,
                                   bool(data.get("fondu_image", True)), sortie)
                else:
                    produire_audio(tache, projet["source"], coupes,
                                   projet["audio"], fmt, sortie)
                tache.update(etat="fini", progression=1.0, message="Montage terminé")
            except Annule:
                tache.update(etat="annule", message="Production arrêtée")
            except Exception as ex:
                tache.update(etat="erreur", message=str(ex) or "Échec de la production")

        threading.Thread(target=travail, daemon=True).start()
        return jsonify({"id": tid})

    def vue_tache(t):
        v = {k: t[k] for k in ("id", "etat", "progression", "message", "sortie",
                               "format", "duree")}
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
#barre{flex:none;display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--border);background:var(--panel2)}
#titre{min-width:0;flex:1}
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
#bilan .ligne{margin:10px 0 0}
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
}
</style>
</head>
<body>

<div id="accueil">
  <div class="carte">
    <h1>Montage au <mark>stabilo</mark></h1>
    <p class="chapeau">Choisissez une vidéo ou un enregistrement. Le texte s'affiche, vous surlignez ce que vous gardez, le montage se fait tout seul.</p>
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
let sel = new Uint8Array(0);
let spans = [];
let histo = [], futur = [];
let outil = 'stabilo';
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
  clearTimeout(veille);
  $('#accueil').hidden = true; $('#atelier').hidden = false;
  document.title = p.nom + ' — Montage au stabilo';
  $('#titre .n').textContent = p.nom;
  $('#titre .d').textContent = (p.type === 'video' ? 'Vidéo' : 'Enregistrement') + ' · ' + enClair(p.duree);
  $('#texte').innerHTML = '';
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
    pat.querySelector('.quoi').textContent = 'Transcription en cours' +
      (p.depuis ? ' depuis ' + enClair(p.depuis) : '') + '…';
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
  sel = new Uint8Array(J.length);
  for (const [a, b] of d.plages)
    for (let i = Math.max(0, a); i <= Math.min(J.length - 1, b); i++) sel[i] = 1;
  if ([...$('#fondu').options].some(o => parseFloat(o.value) === d.fondu)) $('#fondu').value = String(d.fondu);
  rendre(); peindre(0, J.length - 1); bilan(); boutons();
  if (plages().length) envoyer(0);
}

function rendre(){
  const out = [];
  let ouvert = false, car = 0, precP = null, precF = 0;
  for (let i = 0; i < J.length; i++) {
    const [t, d, f, p] = J[i];
    if (t === null) {
      if (ouvert) { out.push('</p></div>'); ouvert = false; }
      out.push(`<div class="par"><a class="hor" data-t="${d}">${hms(d)}</a><p><span class="m pause" data-i="${i}">${enClair(f - d)} sans paroles</span></p></div>`);
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
  $('#b-produire').disabled = !n || (tache !== null);
  $('#b-ecouter').disabled = !n || !lecteur;
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

function choisirOutil(o){
  outil = o;
  $('#o-stabilo').classList.toggle('actif', o === 'stabilo');
  $('#o-gomme').classList.toggle('actif', o === 'gomme');
  document.body.classList.toggle('gomme', o === 'gomme');
}
$('#o-stabilo').onclick = () => choisirOutil('stabilo');
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
  else { dernier = a; aller(J[a][1], true); }
}
texte.addEventListener('pointerup', e => finGeste(e, true));
texte.addEventListener('pointercancel', e => finGeste(e, false));
texte.addEventListener('dblclick', e => {
  const i = jetonSous(e.clientX, e.clientY);
  if (i < 0) return;
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
      const d = await api('/api/selection/' + pid, {plages: plages(), fondu: parseFloat($('#fondu').value)});
      if (n !== numeroEnvoi || !P || P.id !== pid) return;
      coupes = d.coupes; bilan(d.duree);
    } catch(e) {}
  }, delai);
}
$('#fondu').onchange = () => envoyer(0);

function bilan(duree){
  const n = coupes.length;
  if (!J.length) { $('#bilan-duree').textContent = '—'; $('#bilan-detail').textContent = ''; $('#passages').innerHTML = ''; return; }
  if (!plages().length) {
    coupes = [];
    $('#bilan-duree').textContent = 'Rien de surligné';
    $('#bilan-detail').textContent = '';
    $('#passages').innerHTML = '<div id="vide">Glissez sur le texte pour surligner ce que vous gardez.<br>Un clic sur un mot lance la lecture à cet endroit. Un double clic surligne la phrase entière. Pour un long passage, cliquez sur son premier mot puis, majuscule enfoncée, sur son dernier.</div>';
    return;
  }
  if (duree === undefined) return;
  $('#bilan-duree').textContent = 'Montage de ' + enClair(duree);
  $('#bilan-detail').textContent = n + (n > 1 ? ' passages' : ' passage') + ' sur ' + enClair(P.duree);
  $('#passages').innerHTML = coupes.map((c, k) => {
    const mots = [];
    for (let i = c.plage[0]; i <= c.plage[1] && mots.length < 16; i++) if (J[i][0] !== null && sel[i]) mots.push(J[i][0]);
    return `<div class="passage" data-k="${k}"><div class="h">${hms(c.d)}</div>
      <div class="x">${esc(mots.join(' ')) || 'Sans paroles'}</div><div class="l">${enClair(c.f - c.d)}</div>
      <button class="r" title="Retirer ce passage">×</button></div>`;
  }).join('');
}
$('#passages').onclick = e => {
  const l = e.target.closest('.passage');
  if (!l) return;
  const c = coupes[+l.dataset.k];
  if (!c) return;
  if (e.target.closest('.r')) { changer(c.plage[0], c.plage[1], 0); return; }
  spans[c.plage[0]].scrollIntoView({block: 'center', behavior: 'smooth'});
  aller(c.d, false);
};

/* ── Production ──────────────────────────────────────────── */
$('#b-produire').onclick = async () => {
  $('#echec').hidden = true;
  try {
    const d = await api('/api/produire/' + P.id, {
      plages: plages(), fondu: parseFloat($('#fondu').value),
      format: $('#format').value, fondu_image: $('#fondu-image').checked});
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
