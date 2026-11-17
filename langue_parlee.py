#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
langue_parlee.py — Quelle langue est parlée, trouvée avant de transcrire
=========================================================================
Whisper laissé à lui-même devine la langue sur les trente premières secondes.
C'est là que commencent souvent les enregistrements : musique, générique, un
bonjour bref, un accent. Et quand il se trompe, il n'écrit pas n'importe quoi :
il écrit la mauvaise langue couramment — une causerie en français revient en
traduction anglaise soignée, et personne ne le voit avant de la lire.

Ici la langue est cherchée sur plusieurs passages répartis dans
l'enregistrement : neuf endroits regardés, les trois où Silero entend le plus
de parole gardés, et les probabilités de Whisper moyennées sur ces trois-là.
La langue trouvée est ensuite donnée à Whisper pour toute la transcription.

Les mêmes passages vérifient une langue choisie à la main : quand
l'enregistrement en parle clairement une autre, l'outil demande avant de
passer des minutes à transcrire dans la mauvaise langue.

Mêmes règles et mêmes seuils que Language.kt de readers-speech (Android).
"""

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

TAUX = 16_000
# Trente secondes à 16 kHz : ce que Whisper regarde pour une estimation.
FENETRE = 30 * TAUX
# Passages dont les estimations sont moyennées.
PASSAGES = 3
# Endroits regardés pour les trouver (Silero est rapide, l'encodeur de Whisper non).
CANDIDATS = 9
# Un passage compte comme parlé au-delà de cette part de parole.
PAROLE = 0.4
# Pour la vérification : ce dont la détection doit être sûre, et combien la
# langue choisie doit être improbable.
SURE = 0.7
IMPROBABLE = 0.1


@dataclass
class Entendue:
    """Ce qui a été entendu : la langue, sa probabilité moyenne, toutes les
    probabilités moyennes, le nombre de passages écoutés."""
    langue: str
    confiance: float
    probas: dict = field(default_factory=dict)
    passages: int = 0

    def proba(self, code: str) -> float:
        return float(self.probas.get(code, 0.0))

    def en_dict(self) -> dict:
        principales = dict(sorted(self.probas.items(), key=lambda kv: -kv[1])[:5])
        return {"langue": self.langue, "confiance": round(self.confiance, 3),
                "passages": self.passages,
                "probas": {k: round(v, 3) for k, v in principales.items()}}


def candidats(total: int, nombre: int = CANDIDATS) -> list:
    """Où regarder, en échantillons, dans un enregistrement de `total`
    échantillons : `nombre` fenêtres réparties, la première et la dernière à
    l'écart des bords mêmes (c'est là que vivent les génériques). Un
    enregistrement plus court qu'une fenêtre en fait une seule."""
    if total <= FENETRE:
        return [0]
    reste = total - FENETRE
    n = max(1, min(nombre, max(1, total // FENETRE)))
    places = [reste * (2 * i + 1) // (2 * n) for i in range(n)]
    return sorted(set(places))


def choisir(parts: list) -> list:
    """À partir de la part de parole de chaque candidat ((place, part), part à
    None quand Silero n'a pas pu servir), les passages à soumettre à Whisper :
    les plus parlés, jusqu'à PASSAGES. Sans détecteur, des passages répartis.
    Aucun quand rien ne ressemble à de la parole."""
    if not parts:
        return []
    if any(p is None for _, p in parts):
        pas = max(1.0, len(parts) / PASSAGES)
        return sorted({parts[min(int(i * pas + pas / 2), len(parts) - 1)][0]
                       for i in range(min(PASSAGES, len(parts)))})
    classes = sorted(parts, key=lambda x: -x[1])
    parles = [x for x in classes if x[1] >= PAROLE]
    if parles:
        return sorted(x[0] for x in parles[:PASSAGES])
    # Peu de parole nulle part : le meilleur endroit, s'il en a un peu.
    return [classes[0][0]] if classes[0][1] > 0.05 else []


def combiner(estimations: list) -> Optional[Entendue]:
    """La moyenne de la probabilité de chaque langue sur les passages ; None
    quand il n'y en a aucun."""
    utiles = [e for e in estimations if e]
    if not utiles:
        return None
    somme = {}
    for e in utiles:
        for k, v in e.items():
            somme[k] = somme.get(k, 0.0) + float(v)
    moy = {k: v / len(utiles) for k, v in somme.items()}
    meilleure = max(moy, key=moy.get)
    return Entendue(meilleure, moy[meilleure], moy, len(utiles))


def suggerer(choisie: str, entendue: Optional[Entendue]) -> Optional[str]:
    """Si la langue choisie à la main n'est clairement pas celle qui est
    parlée : la langue à proposer à la place, sinon None. Clairement veut dire
    que la détection est sûre (SURE) et ne laisse presque aucune chance à la
    langue choisie (IMPROBABLE) — un accent, ou deux langues mêlées, ne doivent
    pas faire poser la question."""
    if entendue is None or not choisie or entendue.langue == choisie:
        return None
    if entendue.confiance >= SURE and entendue.proba(choisie) < IMPROBABLE:
        return entendue.langue
    return None


def part_de_parole(son: np.ndarray) -> Optional[float]:
    """Part du passage où Silero entend de la parole (None s'il manque)."""
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:
        return None
    if len(son) == 0:
        return 0.0
    try:
        # Sans marge autour des morceaux ni fusion des courts silences : on
        # mesure la parole, on ne prépare pas un découpage.
        morceaux = get_speech_timestamps(
            son, VadOptions(speech_pad_ms=0, min_silence_duration_ms=100))
    except Exception:
        return None
    parle = sum(m["end"] - m["start"] for m in morceaux)
    return min(1.0, parle / len(son))


def probabilites(modele, son: np.ndarray) -> dict:
    """Les probabilités de chaque langue selon Whisper sur un passage de 30 s
    au plus. `modele` est un WhisperModel de faster-whisper (celui que WhisperX
    garde dans `pipeline.model`)."""
    _, _, toutes = modele.detect_language(audio=np.ascontiguousarray(son, dtype=np.float32),
                                          language_detection_segments=1)
    return {k: float(v) for k, v in toutes}


def detecter(modele, son: np.ndarray,
             sur_etape: Optional[Callable[[int], None]] = None) -> Optional[Entendue]:
    """Détection sur tout un enregistrement (float 16 kHz mono). None quand
    aucune parole n'a été entendue. `sur_etape` reçoit 0–100."""
    etape = sur_etape or (lambda _: None)
    total = len(son)
    places = candidats(total)
    parts = []
    for i, a in enumerate(places):
        etape(i * 30 // len(places))
        parts.append((a, part_de_parole(son[a:a + FENETRE])))
    retenus = choisir(parts)
    estimations = []
    for i, a in enumerate(retenus):
        etape(30 + i * 70 // max(1, len(retenus)))
        estimations.append(probabilites(modele, son[a:a + FENETRE]))
    etape(100)
    return combiner(estimations)


# Nom de chaque langue tel qu'on le dit dans une phrase française.
NOMS = {
    "fr": "français", "en": "anglais", "de": "allemand", "es": "espagnol",
    "it": "italien", "pt": "portugais", "nl": "néerlandais", "ru": "russe",
    "pl": "polonais", "ja": "japonais", "zh": "chinois", "ar": "arabe",
    "cy": "gallois", "sv": "suédois", "da": "danois", "no": "norvégien",
    "nn": "norvégien", "fi": "finnois", "el": "grec", "tr": "turc",
    "uk": "ukrainien", "cs": "tchèque", "hu": "hongrois", "ro": "roumain",
    "he": "hébreu", "hi": "hindi", "ko": "coréen", "fa": "persan",
    "ca": "catalan", "la": "latin", "sk": "slovaque", "hr": "croate",
    "sr": "serbe", "bg": "bulgare", "id": "indonésien", "vi": "vietnamien",
    "th": "thaï", "ga": "irlandais", "eu": "basque", "gl": "galicien",
    "lb": "luxembourgeois", "br": "breton", "sa": "sanskrit", "pi": "pali",
}


def nom(code: str) -> str:
    return NOMS.get(code, code or "?")


# ─── Essai sans interface ────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import time

    ap = argparse.ArgumentParser(description="Langue parlée d'un fichier, sur plusieurs passages.")
    ap.add_argument("fichiers", nargs="+")
    ap.add_argument("--modele", default="small")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--choisie", default="", help="langue choisie à la main, à vérifier")
    a = ap.parse_args()

    from faster_whisper import WhisperModel
    from faster_whisper.audio import decode_audio
    m = WhisperModel(a.modele, device=a.device,
                     compute_type="int8" if a.device == "cpu" else "float16")
    for f in a.fichiers:
        son = decode_audio(f, sampling_rate=TAUX)
        t0 = time.time()
        e = detecter(m, son)
        t1 = time.time()
        premier = m.detect_language(audio=son, language_detection_segments=1)
        print(f"{f}: {len(son)/TAUX:.0f} s · plusieurs passages → "
              f"{e.en_dict() if e else None} en {t1-t0:.1f} s · "
              f"30 premières s → {premier[0]} {premier[1]:.3f}"
              + (f" · question : {suggerer(a.choisie, e)}" if a.choisie else ""))
