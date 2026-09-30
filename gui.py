#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py — Panneau de contrôle web pour la boîte à outils de traduction/doublage.

Lance un petit serveur local (Flask) qui présente, pour chaque script du repo,
un formulaire : champs, listes déroulantes, interrupteurs, sélecteur de
fichiers, aperçu de la commande et console de sortie en direct. Le montage au
stabilo (monter.py) y est intégré comme une page à part entière.

    ~/miniconda3/envs/interview/bin/python gui.py
    → ouvre http://127.0.0.1:5005

    python gui.py --verifier     compare les formulaires aux scripts et s'arrête

Le manifeste ci-dessous ne dit QUE ce qu'un script ne peut pas dire lui-même
(libellé, rangement). Valeurs par défaut, choix possibles et aide sont lus dans
les `add_argument` de chaque script : quand un script change son modèle par
défaut, le formulaire suit, et seul ce qui s'écarte du défaut est passé en
ligne de commande.

Aucune dépendance nouvelle : Flask est déjà présent dans l'env « interview ».
Identité visuelle alignée sur l'extension Chrome de traduction (sombre + orange).
"""

import argparse
import ast
import codecs
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid


def _resolve_python():
    """Interpréteur Python du toolkit, portable d'une machine à l'autre :
    surcharge TRADUCTION_PYTHON, sinon env conda « interview » ou « traduction »,
    sinon l'interpréteur courant."""
    cands = [os.environ.get("TRADUCTION_PYTHON")]
    for env in ("interview", "traduction"):
        cands.append(os.path.expanduser(f"~/miniconda3/envs/{env}/bin/python"))
    for c in cands:
        if c and os.path.exists(c):
            return c
    return sys.executable


try:
    from flask import Flask, request, jsonify, Response, send_file, abort
except ImportError:
    print("❌ Flask n'est pas installé dans cet interpréteur.")
    print(f"   Lance plutôt :  {_resolve_python()} gui.py")
    print("   Ou diagnostique l'installation :  python3 doctor.py --install")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.environ.get("TRADUCTION_INPUT_DIR", os.path.join(SCRIPT_DIR, "input"))
PYTHON_BIN = _resolve_python()
HOST = "127.0.0.1"
PORT = 5005

VOICEMAP_REQUEST_MARKER = "@@VOICEMAP_REQUEST@@"    # cf. doubler.py
VOICEMAP_DONE_MARKER = "@@VOICEMAP_DONE@@"

# ═══════════════════════════════════════════════════════════════════════════════
# MANIFESTE DES SCRIPTS
# ═══════════════════════════════════════════════════════════════════════════════
# Chaque champ :
#   name      identifiant interne (clé des valeurs)
#   flag      drapeau CLI (None → argument positionnel)
#   label     libellé affiché
#   type      source | file | dir | text | int | float | select | toggle |
#             textarea | ollama | list (plusieurs valeurs séparées par des virgules)
#   off_flag  pour un toggle « activé par défaut » : drapeau émis quand on DÉSACTIVE
#   adv       True → rangé dans la section « Options avancées »
#   depends   {champ: valeur} → affiché seulement si la condition est vraie
#   choices   seulement si le script n'en déclare pas
#   names     {valeur: libellé} pour une liste déroulante
#   placeholder
# Un script dont le fichier est absent n'apparaît pas (le dépôt public ne les a
# pas tous). Une entrée « page » ouvre une page au lieu d'un formulaire.

GROUPES = [("traduire", "Traduire"), ("monter", "Monter"),
           ("ecrire", "Écrire"), ("son", "Son")]


def C(name, flag, label, type="text", **kw):
    return dict(name=name, flag=flag, label=label, type=type, **kw)


def _langues(source="-s", cible="-t", label_cible="Langue cible"):
    return [C("source_lang", source, "Langue parlée", placeholder="en, fr, ja…"),
            C("target_lang", cible, label_cible, placeholder="en, fr, ja…")]


def _llm(label="Texte confié à", claude="--claude-model", url=True, analyse=False):
    """Bloc commun : moteur de texte, modèle local, puis réglages avancés."""
    champs = [
        C("llm", "--llm", label, "select",
          names={"local": "un modèle local (Ollama)", "claude": "Claude (clé API)"}),
        C("ollama_model", "--ollama-model", "Modèle local", "ollama",
          depends={"llm": "local"}),
    ]
    if analyse:
        champs.append(C("analysis_llm", "--analysis-llm", "Analyse préalable confiée à",
                        "select", adv=True,
                        names={"auto": "Claude si la clé est là, sinon local",
                               "claude": "Claude", "local": "un modèle local"}))
    if claude:
        champs.append(C("claude_model", claude, "Modèle Claude", adv=True,
                        depends={"llm": "claude"}))
    if url:
        champs.append(C("ollama_url", "--ollama-url", "Adresse d'Ollama", adv=True,
                        depends={"llm": "local"}))
    return champs


WHISPER = C("whisper_model", "--whisper-model", "Modèle Whisper", adv=True)
# Dernière étape facultative des scripts qui produisent une vidéo : finaliser.py
FINALISER = C("finaliser", "--finaliser", "Finaliser le son (bruit de fond, volume à la norme)",
              "toggle")
CONTEXTE = C("context", "--context", "Contexte (noms propres, sujet…)", "textarea")
STYLE = C("style", "--style", "Style des sous-titres", "select")

SCRIPTS = [
    # ── Traduire ──────────────────────────────────────────────────────────────
    {
        "id": "traduire", "file": "traduire.py", "groupe": "traduire",
        "label": "Sous-titres", "icon": "💬",
        "desc": "Traduit et incruste les sous-titres",
        "fields": [
            C("source", None, "Vidéo ou adresse YouTube", "source", required=True),
            *_langues(),
            *_llm(analyse=True),
            STYLE, CONTEXTE,
            C("dubbing", "--dubbing", "Doublage audio en plus", "toggle"),
            FINALISER,
            C("skip_burn", "--skip-burn", "Fichier SRT seul, sans incrustation", "toggle"),
            C("skip_review", "--skip-review", "Sauter la relecture", "toggle"),
            C("output", "-o", "Vidéo produite", "file", adv=True),
            C("ocr", "--ocr", "Lire les sous-titres déjà incrustés (OCR)", "toggle", adv=True),
            C("demucs", None, "Isoler la voix avant de transcrire", "toggle",
              off_flag="--no-demucs", adv=True),
            C("resume", "--resume", "Reprendre (segments JSON)", "file", adv=True),
            C("srt_only", "--srt-only", "Incruster un SRT existant", "file", adv=True),
            C("delogo", "--delogo", "Masquer un logo", placeholder="X:Y:L:H", adv=True),
            C("vad_onset", "--vad-onset", "Détection de parole : seuil de début", "float", adv=True),
            C("vad_offset", "--vad-offset", "Détection de parole : seuil de fin", "float", adv=True),
            WHISPER,
            C("cookies", "--cookies", "Cookies (JSON)", "file", adv=True),
        ],
    },
    {
        "id": "doubler", "file": "doubler.py", "groupe": "traduire",
        "label": "Doublage vidéo", "icon": "🎬",
        "desc": "Double la vidéo, la voix d'origine en fond",
        "fields": [
            C("video", None, "Vidéo ou adresse YouTube", "source", required=True),
            *_langues(),
            C("tts", "--tts", "Voix produite par", "select",
              names={"qwen3tts": "Qwen3-TTS (local)", "elevenlabs": "ElevenLabs (clé API)"}),
            *_llm(analyse=True),
            C("voiceover", None, "Garder la voix d'origine en fond", "toggle",
              off_flag="--no-voiceover"),
            C("vo_style", "--vo-style", "Mixage de la voix d'origine", "select",
              depends={"voiceover": "true"},
              names={"jt": "journal télévisé (doublage couvrant)",
                     "jt-flat": "journal télévisé, fond constant et bas",
                     "arte": "Arte (doux)", "bbc": "BBC (intermédiaire)"}),
            C("ref_voice", "--ref-voice", "Voix de référence (WAV)", "file"),
            C("ref_voices", "--ref-voices", "Dossier de voix", "dir"),
            C("map_voices", "--map-voices", "Choisir la voix de chaque locuteur", "toggle"),
            C("clone_original", "--clone-original", "Cloner la voix d'origine", "toggle"),
            FINALISER,
            C("gender", "--gender", "Genre des voix", "select",
              choices=["auto", "male", "female"],
              names={"auto": "estimé", "male": "homme", "female": "femme"}),
            C("speakers", "--speakers", "Nombre de locuteurs", "int"),
            CONTEXTE,
            C("output", "-o", "Vidéo produite", "file", adv=True),
            C("remove_music", "--remove-music", "Retirer la musique", "toggle", adv=True),
            C("keep_original", "--keep-original", "Part de voix d'origine gardée (0 à 1)", "float", adv=True),
            C("dual_audio", "--dual-audio", "Deux pistes audio", "toggle", adv=True),
            C("use_srt", "--use-srt", "Traduction déjà faite (SRT)", "file", adv=True),
            C("vo_lead_in", "--vo-lead-in", "Voix d'origine seule avant le doublage (ms)", "int", adv=True),
            C("vo_lead_out", "--vo-lead-out", "Voix d'origine seule après le doublage (ms)", "int", adv=True),
            C("vo_duck_db", "--vo-duck-db", "Baisse de la voix d'origine (dB)", "int", adv=True),
            C("elevenlabs_voice", "--elevenlabs-voice", "Voix ElevenLabs (identifiant)", adv=True,
              depends={"tts": "elevenlabs"}),
            C("elevenlabs_model", "--elevenlabs-model", "Modèle ElevenLabs", adv=True,
              depends={"tts": "elevenlabs"}),
            C("skip", "--skip", "Ignorer le début jusqu'à (MM:SS)", adv=True),
            C("skip_review", "--skip-review", "Sauter la relecture", "toggle", adv=True),
            C("skip_checks", "--skip-checks", "Sauter les vérifications", "toggle", adv=True),
            C("skip_isochrony", "--skip-isochrony", "Sauter l'ajustement des durées", "toggle", adv=True),
            C("skip_normalize", "--skip-normalize", "Sauter la normalisation du volume", "toggle", adv=True),
            C("fix_pitch", "--fix-pitch", "Corriger la hauteur de voix", "toggle", adv=True),
            C("audio_only", "--audio-only", "Son seul, sans contrainte de durée", "toggle", adv=True),
            C("audio_only_pause", "--audio-only-pause", "Pause entre les phrases (ms)", "int", adv=True,
              depends={"audio_only": "true"}),
            C("audio_only_speaker_pause", "--audio-only-speaker-pause",
              "Pause au changement de locuteur (ms)", "int", adv=True,
              depends={"audio_only": "true"}),
            C("skip_video", "--skip-video", "Produire le son mixé, pas la vidéo", "toggle", adv=True),
            C("onlydub", "--onlydub", "Doublage seul", "toggle", adv=True),
            C("watermark", "--watermark", "Filigrane", "toggle", adv=True),
            C("segments", "--segments", "Reprendre (segments JSON)", "file", adv=True),
            WHISPER,
            C("cookies", "--cookies", "Cookies (JSON)", "file", adv=True),
        ],
    },
    {
        "id": "doubler_mp3", "file": "doubler-mp3-batch.py", "groupe": "traduire",
        "label": "Doublage audio", "icon": "🎙️",
        "desc": "Double en MP3, par lot, sans contrainte de durée",
        "fields": [
            C("file", "--file", "Un fichier précis (sinon tout le dossier)", "source"),
            *_langues(),
            *_llm(label="Traduction confiée à", analyse=True),
            C("speakers", "--speakers", "Nombre de locuteurs", "int"),
            C("gender", "--gender", "Genre des voix", "select",
              names={"auto": "estimé", "male": "homme", "female": "femme"}),
            CONTEXTE,
            C("pause", "--pause", "Pause entre les phrases (ms)", "int", adv=True),
            C("speaker_pause", "--speaker-pause", "Pause au changement de locuteur (ms)", "int", adv=True),
            C("segments", "--segments", "Reprendre (segments JSON)", "file", adv=True),
            C("skip_review", "--skip-review", "Sauter la relecture", "toggle", adv=True),
            C("skip_checks", "--skip-checks", "Sauter les vérifications", "toggle", adv=True),
            WHISPER,
        ],
    },
    {
        "id": "traduire_pro", "file": "traduire-pro.py", "groupe": "traduire",
        "label": "Sous-titres Pro", "icon": "🎞️",
        "desc": "Sous-titres avec contrôle des coupes, résumé et doublage",
        "fields": [
            C("source", None, "Vidéo ou adresse YouTube", "source", required=True),
            *_langues(),
            STYLE, CONTEXTE,
            *_llm(),
            C("no_dubbing", "--no-dubbing", "Sans doublage", "toggle"),
            C("audit_cuts", "--audit-cuts", "Contrôler les coupes", "toggle"),
            FINALISER,
            C("num_speakers", "--num-speakers", "Nombre de locuteurs", "int"),
            C("skip_summary", "--skip-summary", "Sauter le résumé", "toggle", adv=True),
            C("skip_review", "--skip-review", "Sauter la relecture", "toggle", adv=True),
            C("skip_burn", "--skip-burn", "Sans incrustation", "toggle", adv=True),
            C("no_trim_music", "--no-trim-music", "Ne pas couper la musique", "toggle", adv=True),
            C("max_cps", "--max-cps", "Caractères par seconde, au plus", "int", adv=True),
            C("delogo", "--delogo", "Masquer un logo", placeholder="X:Y:L:H", adv=True),
            C("ocr", "--ocr", "Lire les sous-titres déjà incrustés (OCR)", "toggle", adv=True),
            C("resume", "--resume", "Reprendre (JSON)", "file", adv=True),
            WHISPER,
        ],
    },
    {
        "id": "sous_titrer_docx", "file": "sous_titrer_docx.py", "groupe": "traduire",
        "label": "Sous-titres depuis un DOCX", "icon": "🗂️",
        "desc": "Cale une traduction déjà écrite sur la vidéo",
        "fields": [
            C("video", None, "Vidéo", "source", required=True),
            C("docx", None, "Traduction (DOCX)", "file", required=True),
            STYLE,
            FINALISER,
            C("srt_only", "--srt-only", "Fichier SRT seul, sans incrustation", "toggle"),
            *_llm(label="Calage confié à", claude=None),
            C("resume", "--resume", "Reprendre (segments calés, JSON)", "file", adv=True),
            C("whisperx_json", "--whisperx-json", "Transcription déjà faite (JSON)", "file", adv=True),
        ],
    },
    # ── Monter ────────────────────────────────────────────────────────────────
    {
        "id": "monter", "file": "monter.py", "groupe": "monter", "page": "/montage/",
        "label": "Montage au stabilo", "icon": "🖍️",
        "desc": "Surligner le texte, le montage suit",
    },
    {
        "id": "clipper", "file": "clipper.py", "groupe": "monter",
        "label": "Extraits choisis", "icon": "✂️",
        "desc": "Trouve les meilleurs passages, sous-titres mot à mot",
        "fields": [
            C("source", None, "Vidéo ou adresse", "source", required=True),
            C("criteria", "--criteria", "Ce qu'on cherche", placeholder="le passage le plus marquant"),
            C("max_clips", "-n", "Nombre d'extraits, au plus", "int"),
            C("duration", "--duration", "Durée en secondes (min-max)"),
            C("source_lang", "-s", "Langue parlée", placeholder="en, fr, ja…"),
            C("target_lang", "-t", "Traduire en", placeholder="en, fr, ja…"),
            *_llm(label="Choix confié à"),
            CONTEXTE,
            FINALISER,
            C("post", "--post", "Publier un extrait déjà fait", "toggle", adv=True),
            C("skip_burn", "--skip-burn", "Sans incrustation", "toggle", adv=True),
            C("words_per_group", "--words-per-group", "Mots affichés à la fois", "int", adv=True),
            C("speaker", "--speaker", "Locuteur", adv=True),
            C("date", "--date", "Date", adv=True),
            C("url", "--url", "Adresse d'origine (pour la publication)", adv=True),
            C("pre_segments", "--pre-segments", "Transcription déjà faite (JSON)", "file", adv=True),
            C("resume", "--resume", "Reprendre (extraits JSON)", "file", adv=True),
            WHISPER,
            C("cookies", "--cookies", "Cookies (JSON)", "file", adv=True),
        ],
    },
    # ── Écrire ────────────────────────────────────────────────────────────────
    {
        "id": "resumer", "file": "resumer.py", "groupe": "ecrire",
        "label": "Résumé", "icon": "📄",
        "desc": "Résumé structuré en PDF et EPUB",
        "fields": [
            C("input", None, "Vidéo, adresse YouTube ou Apollo", "source", required=True),
            C("source", "-s", "Langue parlée", placeholder="détectée si vide"),
            C("target", "-t", "Langue du résumé", placeholder="en, fr, ja…"),
            *_llm(label="Rédaction confiée à", analyse=True),
            C("pages", "--pages", "Nombre de pages visé", "int", placeholder="selon la durée"),
            CONTEXTE,
            C("html", "--html", "Produire aussi une page HTML", "toggle"),
            C("output_dir", "--output-dir", "Copier le résultat dans", "dir", adv=True),
            C("resume", "--resume", "Reprendre (segments JSON)", "file", adv=True),
            C("cookies", "--cookies", "Cookies Apollo (JSON)", "file", adv=True),
        ],
    },
    {
        "id": "transcrire", "file": "transcrire.py", "groupe": "ecrire",
        "label": "Entretien en DOCX", "icon": "📝",
        "desc": "Transcrit un entretien et le réécrit en français écrit",
        "fields": [
            C("input", None, "Vidéo, son ou adresse YouTube", "source"),
            C("invites", "--invites", "Invités", "list", placeholder="Sophie Martin, Paul Durand"),
            C("interviewers", "--interviewers", "Intervieweurs", "list"),
            C("output", "-o", "Document produit (DOCX)", "file"),
            C("playlist", "--playlist", "Choisir dans la liste de lecture Antithèse", "toggle"),
            C("ignore", "--ignore", "Ignorer le début jusqu'à (MM:SS)"),
            C("model", "--model", "Modèle Claude", adv=True),
            WHISPER,
            C("batch_size", "--batch-size", "Taille des lots Whisper", "int", adv=True),
            C("raw_only", "--raw-only", "Transcription brute seulement", "toggle", adv=True),
            C("skip_heuristics", "--skip-heuristics", "Sauter la correction automatique des locuteurs", "toggle", adv=True),
            C("skip_claude_fix", "--skip-claude-fix", "Sauter la correction des locuteurs par Claude", "toggle", adv=True),
            C("resume", "--resume", "Reprendre à l'étape", "select", adv=True),
        ],
    },
    # ── Son ───────────────────────────────────────────────────────────────────
    {
        "id": "finaliser", "file": "finaliser.py", "groupe": "son",
        "label": "Prêt à diffuser", "icon": "🎧",
        "desc": "Finition podcast : silences, bruit de fond, volume à la norme",
        "fields": [
            C("entree", None, "Fichier son ou vidéo, ou dossier", "source", required=True),
            C("sortie", "-o", "Dossier de sortie", "dir", placeholder="à côté de la source"),
            C("format", "--format", "Produire", "select",
              names={"auto": "selon la source (MP4 ou MP3)", "mp3": "un son MP3",
                     "m4a": "un son M4A", "wav": "un son WAV", "mp4": "une vidéo MP4"}),
            C("canaux", "--canaux", "Canaux", "select",
              names={"mono": "mono (la norme pour la parole)", "stereo": "stéréo"}),
            C("debruitage", "--debruitage", "Bruit de fond", "select",
              names={"auto": "retiré s'il y en a un", "jamais": "ne pas y toucher",
                     "toujours": "toujours débruiter"}),
            C("nivelage", "--nivelage", "Niveaux de voix inégaux", "select",
              names={"auto": "égalisés s'ils le sont beaucoup", "jamais": "ne pas y toucher",
                     "toujours": "toujours égaliser"}),
            C("analyse_seule", "--analyse-seule", "Diagnostic seulement, sans rien écrire", "toggle"),
            C("sans_rognage", "--sans-rognage", "Garder les silences de début et de fin", "toggle"),
            C("sur_place", "--sur-place", "Remplacer le fichier (même nom, même durée)", "toggle"),
            C("titre", "--titre", "Titre"),
            C("auteur", "--auteur", "Auteur"),
            C("album", "--album", "Émission"),
            C("debut", "--debut", "Commencer à (MM:SS)", adv=True, placeholder="au premier son"),
            C("fin", "--fin", "Finir à (MM:SS)", adv=True, placeholder="au dernier son"),
            C("fondu_debut", "--fondu-debut", "Fondu d'entrée (s)", "float", adv=True),
            C("fondu_fin", "--fondu-fin", "Fondu de sortie (s)", "float", adv=True),
            C("fondu_image", "--fondu-image", "Vidéo : fondu de l'image aussi", "toggle", adv=True),
            C("lufs", "--lufs", "Volume visé (LUFS)", "float", adv=True,
              placeholder="-19 en mono, -16 en stéréo"),
            C("limiteur_max", "--limiteur-max", "Ce qu'on peut retirer aux crêtes (dB)", "float", adv=True),
            C("reduction", "--reduction", "Atténuation du bruit, au plus (dB)", "int", adv=True,
              placeholder="8, ou 12 si le fond est fort"),
            C("debit", "--debit", "Débit du son (kbit/s)", "int", adv=True,
              placeholder="128 en mono, 192 en stéréo"),
            C("passe_haut", "--passe-haut", "Couper les graves sous (Hz)", "int", adv=True),
            C("cpu", "--cpu", "Débruiter sans la carte graphique", "toggle", adv=True),
            C("forcer", "--forcer", "Refaire même si le résultat existe", "toggle", adv=True),
            C("garder_travail", "--garder-travail", "Garder les fichiers intermédiaires", "toggle", adv=True),
        ],
    },
    {
        "id": "nettoyer", "file": "nettoyer.py", "groupe": "son",
        "label": "Nettoyage", "icon": "🧹",
        "desc": "Débruite et normalise une causerie, à toucher léger",
        "fields": [
            C("entree", None, "Fichier audio ou dossier", "source", required=True),
            C("sortie", "-o", "Dossier de sortie", "dir", placeholder="à côté de la source"),
            C("moteur", "--moteur", "Débruitage", "select",
              names={"auto": "automatique", "dfn": "DeepFilterNet3",
                     "mossformer2": "MossFormer2", "afftdn": "spectral, très doux"}),
            C("reduction", "--reduction", "Atténuation du bruit (dB)", "float"),
            C("niveler", "--niveler", "Niveler doucement le volume", "toggle"),
            C("stereo", "--stereo", "Conserver la stéréo", "toggle"),
            C("lufs", "--lufs", "Volume visé (LUFS)", "float", adv=True,
              placeholder="-19 en mono, -16 en stéréo"),
            C("passe_haut", "--passe-haut", "Couper les graves sous (Hz)", "float", adv=True),
            C("cpu", "--cpu", "Débruiter sans la carte graphique", "toggle", adv=True),
            C("exporter_rx", "--exporter-rx", "Préparer le fichier pour iZotope RX, puis s'arrêter", "toggle", adv=True),
            C("importer_rx", "--importer-rx", "Reprendre avec le fichier débruité dans RX", "file", adv=True),
            C("forcer", "--forcer", "Refaire même si le résultat existe", "toggle", adv=True),
            C("garder_travail", "--garder-travail", "Garder les fichiers intermédiaires", "toggle", adv=True),
        ],
    },
    {
        "id": "lire", "file": "lire.py", "groupe": "son",
        "label": "Article lu", "icon": "🔊",
        "desc": "Lit à haute voix un article d'Antithèse",
        "fields": [
            C("url", None, "Adresse de l'article", "text", required=True,
              placeholder="https://www.antithese.info/articles/…"),
            C("voix", "--voix", "Voix", "select", choices="voix"),
            C("output", "-o", "Fichier produit (MP3)", "file"),
            C("texte_seul", "--texte-seul", "Montrer le texte qui sera lu, sans le lire", "toggle"),
            C("no_titles", "--no-titles", "Remplacer les intertitres par un silence", "toggle"),
            C("sans_signature", "--sans-signature", "Ne pas annoncer l'auteur", "toggle"),
            C("sans_verification", "--sans-verification", "Sauter le contrôle par réécoute", "toggle", adv=True),
            C("garder_wav", "--garder-wav", "Garder les fichiers intermédiaires", "toggle", adv=True),
            C("cookies", "--cookies", "Cookies (JSON)", "file", adv=True),
        ],
    },
]

# ═══════════════════════════════════════════════════════════════════════════════
# LECTURE DES OPTIONS DANS LES SCRIPTS
# ═══════════════════════════════════════════════════════════════════════════════

INCONNU = object()
_MODULES = {}       # chemin → (mtime, constantes, options)


def _module(fichier):
    """(constantes de module, options argparse) d'un script, relus s'il change."""
    chemin = fichier if os.path.isabs(fichier) else os.path.join(SCRIPT_DIR, fichier)
    try:
        mtime = os.stat(chemin).st_mtime
    except OSError:
        return {}, {}
    cache = _MODULES.get(chemin)
    if cache and cache[0] == mtime:
        return cache[1], cache[2]
    try:
        with open(chemin, encoding="utf-8") as f:
            arbre = ast.parse(f.read())
    except (OSError, SyntaxError):
        return {}, {}

    consts = {}
    for n in arbre.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            consts[n.targets[0].id] = n.value
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
            consts[n.target.id] = n.value

    # import nettoyer as N → N.CONSTANTE se lit dans nettoyer.py
    for n in arbre.body:
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    consts["__alias__" + a.asname] = ast.Constant(a.name)

    options = {}
    _MODULES[chemin] = (mtime, consts, options)     # posé avant : les renvois entre scripts
    # Ce qu'un script emprunte à un autre (from traduire import SUBTITLE_STYLES)
    for n in arbre.body:
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0 \
                and os.path.exists(os.path.join(SCRIPT_DIR, n.module + ".py")):
            autres, _ = _module(n.module + ".py")
            for a in n.names:
                if a.name in autres:
                    consts.setdefault(a.asname or a.name, autres[a.name])
    for n in ast.walk(arbre):
        if not (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"):
            continue
        noms = [a.value for a in n.args
                if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        if not noms:
            continue
        info = {"noms": noms}
        for k in n.keywords:
            if k.arg in ("default", "choices", "action", "nargs", "help"):
                v = _valeur(k.value, consts)
                if v is not INCONNU:
                    info[k.arg] = v
        for nom in noms:
            options[nom] = info
    return consts, options


def _valeur(n, consts, profondeur=0):
    """Évalue ce qu'on peut d'une expression sans exécuter le script : littéraux,
    constantes de module, `autre_script.CONSTANTE`, `list(DICO.keys())`."""
    if profondeur > 6:
        return INCONNU
    suite = lambda x, c=consts: _valeur(x, c, profondeur + 1)
    if isinstance(n, ast.Constant):
        return n.value
    if isinstance(n, ast.Name):
        return suite(consts[n.id]) if n.id in consts else INCONNU
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
        alias = consts.get("__alias__" + n.value.id)
        autres, _ = _module((alias.value if alias is not None else n.value.id) + ".py")
        return suite(autres[n.attr], autres) if n.attr in autres else INCONNU
    if isinstance(n, (ast.List, ast.Tuple)):
        vals = [suite(e) for e in n.elts]
        return INCONNU if any(v is INCONNU for v in vals) else vals
    if isinstance(n, ast.Dict):
        cles = [suite(k) for k in n.keys if k is not None]
        return INCONNU if any(k is INCONNU for k in cles) else {k: None for k in cles}
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
        v = suite(n.operand)
        return -v if isinstance(v, (int, float)) else INCONNU
    if isinstance(n, ast.JoinedStr):
        morceaux = []
        for p in n.values:
            v = suite(p.value) if isinstance(p, ast.FormattedValue) else suite(p)
            morceaux.append("…" if v is INCONNU else str(v))
        return "".join(morceaux)
    if isinstance(n, ast.Call):
        f = n.func
        if isinstance(f, ast.Name) and f.id in ("list", "sorted", "tuple") and len(n.args) == 1:
            v = suite(n.args[0])
            return list(v) if isinstance(v, (list, dict)) else INCONNU
        if isinstance(f, ast.Attribute) and f.attr == "keys" and not n.args:
            v = suite(f.value)
            return list(v) if isinstance(v, dict) else INCONNU
    return INCONNU


def _voix_disponibles():
    """Les voix de la banque, nommées comme lire.py les nomme
    (homme1-ok.wav → homme1)."""
    dossier = os.path.join(SCRIPT_DIR, "voix")
    try:
        return sorted(re.sub(r"-ok$", "", os.path.splitext(f)[0])
                      for f in os.listdir(dossier)
                      if f.lower().endswith((".wav", ".mp3", ".flac")))
    except OSError:
        return []


def scripts_resolus():
    """Le manifeste, complété par ce que disent les scripts. Les scripts absents
    et les champs dont l'option n'existe plus sont retirés."""
    resultat = []
    for spec in SCRIPTS:
        if not os.path.exists(os.path.join(SCRIPT_DIR, spec["file"])):
            continue
        if spec.get("page") and not MONTAGE_DISPONIBLE:
            continue
        s = dict(spec)
        s["groupe_nom"] = dict(GROUPES).get(spec.get("groupe"), "")
        if "fields" in spec:
            _, options = _module(spec["file"])
            champs = []
            for f in spec["fields"]:
                f = dict(f)
                cle = f.get("flag") or f.get("off_flag")
                if cle:
                    o = options.get(cle)
                    if o is None:
                        continue                    # option disparue du script
                    if o.get("help"):
                        f["help"] = re.sub(r"\s+", " ", str(o["help"])).replace("%%", "%")
                    if f.get("off_flag"):
                        f["default"] = True
                    elif f["type"] != "toggle":
                        d = o.get("default")
                        if isinstance(d, list):
                            d = ", ".join(str(x) for x in d)
                        f["script_default"] = d
                        f["default"] = "" if d is None else d
                    if f["type"] == "select":
                        if f.get("choices") == "voix":
                            f["choices"] = _voix_disponibles()
                        elif isinstance(o.get("choices"), list):
                            f["choices"] = o["choices"]
                        f.setdefault("choices", [])
                        if f["default"] == "" and "" not in f["choices"]:
                            f["choices"] = [""] + list(f["choices"])
                champs.append(f)
            s["fields"] = champs
        resultat.append(s)
    ordre = [g for g, _ in GROUPES]
    resultat.sort(key=lambda s: ordre.index(s["groupe"]) if s.get("groupe") in ordre else 99)
    return resultat


def verifier():
    """Compare les formulaires aux scripts. Retourne le nombre d'options périmées."""
    perimees = 0
    for spec in SCRIPTS:
        if "fields" not in spec:
            continue
        if not os.path.exists(os.path.join(SCRIPT_DIR, spec["file"])):
            print(f"\n{spec['file']} : absent, entrée masquée")
            continue
        _, options = _module(spec["file"])
        vus = set()
        lignes = []
        for f in spec["fields"]:
            cle = f.get("flag") or f.get("off_flag")
            if not cle:
                continue
            o = options.get(cle)
            if o is None:
                lignes.append(f"   ✗ {cle} n'existe plus dans le script (« {f['label']} »)")
                perimees += 1
                continue
            vus.update(o["noms"])
            bascule = o.get("action") in ("store_true", "store_false")
            if bascule != (f["type"] == "toggle"):
                lignes.append(f"   ✗ {cle} : " + ("interrupteur dans le script, champ ici"
                              if bascule else "champ dans le script, interrupteur ici"))
                perimees += 1
            if o.get("nargs") in ("+", "*") and f["type"] != "list":
                lignes.append(f"   ✗ {cle} accepte plusieurs valeurs : il faut le type « list »")
                perimees += 1
        absentes = sorted({o["noms"][-1] for nom, o in options.items()
                           if nom.startswith("-") and nom not in ("-h", "--help")
                           and not (set(o["noms"]) & vus)})
        print(f"\n{spec['file']}")
        for l in lignes:
            print(l)
        if absentes:
            print("   options du script sans champ : " + ", ".join(absentes))
        elif not lignes:
            print("   ✓ tout correspond")
    return perimees


# ═══════════════════════════════════════════════════════════════════════════════
# CONSTRUCTION DE LA COMMANDE
# ═══════════════════════════════════════════════════════════════════════════════

def _vrai(v):
    return v is True or str(v).lower() == "true"


def _egal(a, b):
    """« 0 » saisi dans un champ numérique vaut le 0.0 du script."""
    if str(a) == str(b):
        return True
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


def build_command(script_id, values):
    """Reconstruit la liste d'arguments depuis le manifeste + les valeurs saisies.
    Ce qui vaut le défaut du script n'est pas répété."""
    spec = next(s for s in scripts_resolus() if s["id"] == script_id)
    positionals, options = [], []

    def visible(field):
        dep = field.get("depends")
        if not dep:
            return True
        return all(str(values.get(k, "")).lower() == str(v).lower() for k, v in dep.items())

    for f in spec.get("fields", []):
        if not visible(f):
            continue
        flag, ftype = f.get("flag"), f.get("type", "text")
        val = values.get(f["name"], None)

        if ftype == "toggle":
            on = _vrai(val)
            if f.get("off_flag"):           # activé par défaut → drapeau quand on coupe
                if not on:
                    options.append(f["off_flag"])
            elif on and flag:
                options.append(flag)
            continue

        if val is None or str(val).strip() == "":
            continue
        val = str(val).strip()

        if flag is None:                    # argument positionnel
            positionals.append(val)
            continue
        defaut = f.get("script_default")
        if defaut is not None and _egal(val, defaut):
            continue
        if ftype == "list":
            elements = [e.strip() for e in val.split(",") if e.strip()]
            if elements:
                options.append(flag)
                options.extend(elements)
        else:
            options.append(flag)
            options.append(val)

    return spec, positionals, options


def full_command(script_id, values):
    spec, pos, opt = build_command(script_id, values)
    return [PYTHON_BIN, spec["file"]] + pos + opt


def lisible(cmd):
    """La commande telle qu'on la taperait dans le dossier du toolkit."""
    return " ".join(["python"] + [shlex.quote(c) for c in cmd[1:]])


# ═══════════════════════════════════════════════════════════════════════════════
# GESTION DES PROCESSUS
# ═══════════════════════════════════════════════════════════════════════════════

RUNS = {}            # id → état d'une exécution
RUNS_LOCK = threading.Lock()


def _ligne(run, texte):
    """Une ligne complète de la sortie. Les marqueurs de doubler.py (choix des
    voix) ne sont pas affichés : ils ouvrent ou ferment la question."""
    t = texte.strip()
    if t.startswith(VOICEMAP_REQUEST_MARKER):
        try:
            with open(t[len(VOICEMAP_REQUEST_MARKER):].strip(), encoding="utf-8") as f:
                run["question"] = json.load(f)
        except (OSError, ValueError):
            run["lines"].append("⚠️  Demande de choix des voix illisible")
            return
        run["vquestion"] += 1
        return
    if t.startswith(VOICEMAP_DONE_MARKER):
        run["question"] = None
        run["vquestion"] += 1
        return
    run["lines"].append(texte)


def _avaler(run, texte):
    """Découpe la sortie en lignes. Un retour chariot seul (barres de
    progression) réécrit la ligne en cours au lieu d'en empiler des centaines ;
    une ligne sans fin (question posée par input()) reste visible."""
    for morceau in re.split(r"(\r\n|\n|\r)", texte):
        if morceau in ("\n", "\r\n"):
            _ligne(run, run["partiel"])
            run["partiel"] = ""
            run["retour"] = False
        elif morceau == "\r":
            run["retour"] = True
        elif morceau:
            if run["retour"]:
                run["partiel"] = ""
                run["retour"] = False
            run["partiel"] += morceau
    run["vpartiel"] += 1


def _reader(run, proc):
    decodeur = codecs.getincrementaldecoder("utf-8")(errors="replace")
    fd = proc.stdout.fileno()
    while True:
        try:
            brut = os.read(fd, 65536)
        except OSError:
            break
        if not brut:
            break
        with RUNS_LOCK:
            _avaler(run, decodeur.decode(brut))
    code = proc.wait()
    with RUNS_LOCK:
        if run["partiel"]:
            _ligne(run, run["partiel"])
            run["partiel"] = ""
            run["vpartiel"] += 1
        run["question"] = None
        run["vquestion"] += 1
        run["done"] = True
        run["code"] = code


def start_run(script_id, values):
    cmd = full_command(script_id, values)
    run_id = uuid.uuid4().hex[:12]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd, cwd=SCRIPT_DIR, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=0, start_new_session=True,
    )
    run = {"id": run_id, "script": script_id, "proc": proc, "cmd": lisible(cmd),
           "lines": [], "partiel": "", "vpartiel": 0, "retour": False,
           "question": None, "vquestion": 0,
           "done": False, "code": None, "debut": time.time()}
    with RUNS_LOCK:
        RUNS[run_id] = run
    threading.Thread(target=_reader, args=(run, proc), daemon=True).start()
    return run


def stop_run(run_id):
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if not run or run["done"]:
        return False
    try:
        os.killpg(os.getpgid(run["proc"].pid), signal.SIGTERM)
        time.sleep(0.5)
        if run["proc"].poll() is None:
            os.killpg(os.getpgid(run["proc"].pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def send_input(run_id, texte):
    """Répond à une question posée par le script (input())."""
    with RUNS_LOCK:
        run = RUNS.get(run_id)
        if not run or run["done"]:
            return False
        try:
            run["proc"].stdin.write((texte + "\n").encode("utf-8"))
            run["proc"].stdin.flush()
        except (OSError, ValueError):
            return False
        # La réponse termine la ligne de la question, comme dans un terminal.
        run["lines"].append(run["partiel"] + texte)
        run["partiel"] = ""
        run["vpartiel"] += 1
    return True


def list_ollama_models():
    """Les modèles réellement installés (sans ceux qui ne servent qu'à indexer)."""
    models = []
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=8)
        for line in out.stdout.splitlines()[1:]:
            name = line.split()[0] if line.split() else ""
            if name and not re.search(r"embed|bge-|minilm", name):
                models.append(name)
    except Exception:
        pass
    return models


def browse(path):
    path = os.path.abspath(os.path.expanduser(path or SCRIPT_DIR))
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        path = SCRIPT_DIR
    dirs, files = [], []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                dirs.append(name)
            else:
                files.append(name)
    except PermissionError:
        pass
    return {"path": path, "parent": os.path.dirname(path), "dirs": dirs, "files": files}


# ═══════════════════════════════════════════════════════════════════════════════
# SERVEUR FLASK
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# Le montage au stabilo est une page à part, servie par ce même serveur.
try:
    import monter
    app.register_blueprint(monter.creer_blueprint(), url_prefix="/montage")
    MONTAGE_DISPONIBLE = True
except Exception as _ex:                    # monter.py absent (dépôt public) ou cassé
    MONTAGE_DISPONIBLE = False
    if os.path.exists(os.path.join(SCRIPT_DIR, "monter.py")):
        print(f"⚠️  Montage au stabilo indisponible : {_ex}")


def _run_ou_404(run_id):
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if not run:
        abort(404)
    return run


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/favicon.ico")
def favicon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#14161a"/>'
           '<circle cx="16" cy="16" r="7" fill="#ff8a3d"/></svg>')
    return Response(svg, mimetype="image/svg+xml")


@app.route("/api/scripts")
def api_scripts():
    return jsonify({"scripts": scripts_resolus(),
                    "depart": INPUT_DIR if os.path.isdir(INPUT_DIR) else SCRIPT_DIR})


@app.route("/api/ollama")
def api_ollama():
    return jsonify(list_ollama_models())


@app.route("/api/browse")
def api_browse():
    return jsonify(browse(request.args.get("path", "")))


@app.route("/api/preview", methods=["POST"])
def api_preview():
    data = request.get_json(force=True)
    return jsonify({"cmd": lisible(full_command(data["script"], data.get("values", {})))})


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True)
    run = start_run(data["script"], data.get("values", {}))
    return jsonify({"id": run["id"], "cmd": run["cmd"]})


@app.route("/api/runs")
def api_runs():
    """L'exécution en cours, pour qu'une page rechargée retrouve sa console."""
    with RUNS_LOCK:
        encours = [r for r in RUNS.values() if not r["done"]]
    encours.sort(key=lambda r: r["debut"])
    return jsonify([{"id": r["id"], "script": r["script"], "cmd": r["cmd"]}
                    for r in encours])


@app.route("/api/stop/<run_id>", methods=["POST"])
def api_stop(run_id):
    return jsonify({"stopped": stop_run(run_id)})


@app.route("/api/input/<run_id>", methods=["POST"])
def api_input(run_id):
    data = request.get_json(force=True)
    return jsonify({"ok": send_input(run_id, str(data.get("texte", "")))})


@app.route("/api/voix/<run_id>", methods=["POST"])
def api_voix(run_id):
    """Écrit le choix des voix attendu par doubler.py (--map-voices)."""
    run = _run_ou_404(run_id)
    data = request.get_json(force=True)
    with RUNS_LOCK:
        question = run["question"]
    if not question or not question.get("response_file"):
        return jsonify({"ok": False}), 409
    permises = {v["path"] for v in question.get("voices", [])}
    choix = {loc: v for loc, v in (data.get("map") or {}).items() if v in permises}
    cible = question["response_file"]
    with open(cible + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"map": choix}, f, ensure_ascii=False)
    os.replace(cible + ".tmp", cible)
    return jsonify({"ok": True})


@app.route("/api/son/<run_id>")
def api_son(run_id):
    """Échantillon d'un locuteur ou voix de référence, pour le choix des voix.
    Seuls les fichiers cités par la question en cours sont servis."""
    run = _run_ou_404(run_id)
    chemin = request.args.get("chemin", "")
    with RUNS_LOCK:
        question = run["question"] or {}
    permis = ({s.get("sample") for s in question.get("speakers", [])}
              | {v.get("path") for v in question.get("voices", [])})
    if chemin not in permis or not os.path.isfile(chemin):
        abort(404)
    return send_file(chemin, conditional=True)


@app.route("/api/stream/<run_id>")
def api_stream(run_id):
    def gen():
        sent, vpartiel, vquestion = 0, -1, 0
        yield "retry: 2000\n\n"
        while True:
            with RUNS_LOCK:
                run = RUNS.get(run_id)
                if not run:
                    yield "event: error\ndata: run introuvable\n\n"
                    return
                lines = run["lines"][sent:]
                sent += len(lines)
                partiel = run["partiel"] if run["vpartiel"] != vpartiel else None
                vpartiel = run["vpartiel"]
                question = INCONNU
                if run["vquestion"] != vquestion:
                    vquestion, question = run["vquestion"], run["question"]
                done, code = run["done"], run["code"]
                reste = len(run["lines"]) - sent
            for ln in lines:
                yield "data: " + json.dumps(ln) + "\n\n"
            if partiel is not None:
                yield "event: partiel\ndata: " + json.dumps(partiel) + "\n\n"
            if question is not INCONNU:
                yield "event: voix\ndata: " + json.dumps(question) + "\n\n"
            if done and reste == 0:
                yield "event: done\ndata: " + json.dumps({"code": code}) + "\n\n"
                return
            time.sleep(0.25)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ═══════════════════════════════════════════════════════════════════════════════
# FRONTEND (HTML + CSS + JS embarqués) — palette de l'extension de traduction
# ═══════════════════════════════════════════════════════════════════════════════

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Traduction — Panneau de contrôle</title>
<link rel="icon" href="/favicon.ico">
<style>
:root{
  --bg:#14161a; --panel:#1c1f26; --panel2:#0c0e12; --border:#2a2e38;
  --text:#e6e8ec; --muted:#8a90a0; --accent:#ff8a3d; --accent-hover:#ffa362;
  --ok:#4ad27e; --warn:#e5b04b; --danger:#e04b4b;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;display:flex;height:100vh;overflow:hidden}
a{color:var(--accent)}
[hidden]{display:none !important}

/* Sidebar */
#sidebar{width:252px;flex:none;background:var(--panel2);border-right:1px solid var(--border);display:flex;flex-direction:column}
#sidebar .brand{padding:18px 18px 8px;font-weight:700;font-size:16px;letter-spacing:.2px}
#sidebar .brand small{display:block;color:var(--muted);font-weight:400;font-size:11px;margin-top:2px}
#scriptlist{overflow-y:auto;padding:0 6px 10px}
.groupe{font-size:10.5px;text-transform:uppercase;letter-spacing:.9px;color:var(--muted);font-weight:700;padding:14px 12px 5px}
.scriptitem{display:flex;gap:10px;align-items:center;padding:8px 12px;border-radius:10px;cursor:pointer;color:var(--text);transition:background .12s}
.scriptitem:hover{background:#181b22}
.scriptitem.active{background:#23262f;outline:1px solid var(--border)}
.scriptitem .ic{font-size:17px;width:22px;text-align:center;flex:none}
.scriptitem .t{font-weight:600}
.scriptitem .d{font-size:11px;color:var(--muted);line-height:1.25}
.scriptitem .run{width:8px;height:8px;border-radius:50%;background:var(--accent);margin-left:auto;flex:none;animation:pulse 1s infinite}

/* Main */
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#header{padding:14px 22px;border-bottom:1px solid var(--border);display:flex;align-items:baseline;gap:12px}
#header h1{font-size:17px;margin:0}
#header .sub{color:var(--muted);font-size:12px}
#body{flex:1;display:flex;min-height:0}
#cadre{flex:1;border:none;width:100%;min-height:0;background:var(--bg)}

/* Form column */
#formwrap{flex:1;overflow-y:auto;padding:20px 22px;min-width:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
.field{display:flex;flex-direction:column;justify-content:flex-end;gap:5px;min-width:0}
.field.full{grid-column:1/-1}
.field label{font-size:12px;color:var(--muted);font-weight:600}
.field .req{color:var(--accent)}
input[type=text],input[type=number],select,textarea{
  background:var(--panel);border:1px solid var(--border);color:var(--text);
  border-radius:9px;padding:9px 11px;font-size:13px;width:100%;outline:none;transition:border .12s}
input:focus,select:focus,textarea:focus{border-color:var(--accent)}
input::placeholder,textarea::placeholder{color:#5d6372}
textarea{resize:vertical;min-height:62px;font-family:inherit}
select{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 16px) 17px,calc(100% - 11px) 17px;background-size:5px 5px;background-repeat:no-repeat;padding-right:30px}
.inputrow{display:flex;gap:8px}
.inputrow input{flex:1}
.browsebtn{background:var(--panel);border:1px solid var(--border);color:var(--muted);border-radius:9px;padding:0 12px;cursor:pointer;font-size:12px;white-space:nowrap}
.browsebtn:hover{border-color:var(--accent);color:var(--text)}

/* Toggle */
.toggle{display:flex;align-items:center;gap:10px;background:var(--panel);border:1px solid var(--border);border-radius:9px;padding:9px 11px;cursor:pointer;min-height:38px}
.toggle:hover{border-color:#3a3f4c}
.toggle .sw{width:36px;height:20px;border-radius:20px;background:#33384a;position:relative;flex:none;transition:background .15s}
.toggle .sw::after{content:"";position:absolute;width:16px;height:16px;border-radius:50%;background:#cfd3dc;top:2px;left:2px;transition:left .15s}
.toggle.on .sw{background:var(--accent)}
.toggle.on .sw::after{left:18px;background:#1b1205}
.toggle .lab{font-size:12.5px;color:var(--text);line-height:1.3}

.advtoggle{margin:22px 0 10px;color:var(--muted);cursor:pointer;font-size:12px;font-weight:600;user-select:none;display:inline-flex;align-items:center;gap:6px}
.advtoggle:hover{color:var(--text)}
.advtoggle b{color:var(--accent);font-weight:700}
#advanced{display:none}
#advanced.open{display:block}

/* Console */
#console-col{width:42%;min-width:340px;max-width:680px;border-left:1px solid var(--border);display:flex;flex-direction:column;background:var(--panel2)}
#cmdbar{padding:12px 16px;border-bottom:1px solid var(--border)}
#cmdbar .lbl{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:5px}
#cmdpreview{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:#c6cad2;background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:9px 11px;white-space:pre-wrap;word-break:break-all;max-height:96px;overflow-y:auto}
#runbar{padding:10px 16px;display:flex;gap:10px;align-items:center;border-bottom:1px solid var(--border)}
.btn{border:none;border-radius:9px;padding:9px 18px;font-size:13px;font-weight:700;cursor:pointer}
.btn-run{background:var(--accent);color:#1b1205}
.btn-run:hover{background:var(--accent-hover)}
.btn-stop{background:var(--danger);color:#fff}
.btn-plain{background:var(--panel);color:var(--text);border:1px solid var(--border)}
.btn:disabled{opacity:.4;cursor:not-allowed}
#status{font-size:12px;color:var(--muted);margin-left:auto}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.dot.idle{background:#555}.dot.run{background:var(--accent);animation:pulse 1s infinite}.dot.ok{background:var(--ok)}.dot.err{background:var(--danger)}
@keyframes pulse{50%{opacity:.3}}
#console{flex:1;overflow-y:auto;padding:12px 16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;line-height:1.55;white-space:pre-wrap;word-break:break-word}
#console .l-ok{color:var(--ok)}#console .l-err{color:#ff8d8d}#console .l-warn{color:var(--warn)}#console .l-step{color:var(--accent-hover);font-weight:600}#console .l-dim{color:var(--muted)}
#console .placeholder{color:var(--muted)}
#ailleurs{padding:9px 16px;border-bottom:1px solid var(--border);font-size:12px;color:var(--muted);cursor:pointer}
#ailleurs:hover{color:var(--text)}
#saisie{display:flex;gap:8px;padding:10px 16px;border-top:1px solid var(--border)}
#saisie input{flex:1;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
#saisie .btn{padding:8px 14px}

/* Modales */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:50}
.modal.open{display:flex}
.modalbox{width:620px;max-width:92vw;max-height:84vh;background:var(--panel);border:1px solid var(--border);border-radius:14px;display:flex;flex-direction:column;overflow:hidden}
.modalhead{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.modalhead .path{font-family:ui-monospace,monospace;font-size:12px;color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}
.modalhead h2{font-size:15px;margin:0;flex:1}
.modalhead button{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer;font-size:12px}
.modalhead button:hover{border-color:var(--accent)}
.modallist{overflow-y:auto;padding:8px}
.entry{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:8px;cursor:pointer}
.entry:hover{background:#23262f}
.entry .ic{width:18px;text-align:center}
.entry.dir .ic{color:var(--accent)}
.entry.media{color:var(--text)}
.entry.file{color:var(--muted)}
.modalfoot{padding:10px 16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px}
.empty{padding:40px;text-align:center;color:var(--muted)}
.locuteur{padding:12px 10px;border-bottom:1px solid var(--border)}
.locuteur:last-child{border-bottom:none}
.locuteur .qui{font-weight:700}
.locuteur .quoi{font-size:12px;color:var(--muted);margin:2px 0 8px}
.locuteur .paire{display:grid;grid-template-columns:1fr 1fr;gap:10px;align-items:center}
.locuteur audio{width:100%;height:34px}
.locuteur .col{display:flex;flex-direction:column;gap:6px;min-width:0}
.locuteur .col span{font-size:11px;color:var(--muted);font-weight:600}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#2c313c;border-radius:6px}::-webkit-scrollbar-track{background:transparent}
</style>
</head>
<body>
<div id="sidebar">
  <div class="brand">🎛️ Traduction <small>panneau de contrôle local</small></div>
  <div id="scriptlist"></div>
</div>

<div id="main">
  <div id="header">
    <h1 id="h-title">—</h1>
    <span class="sub" id="h-desc"></span>
  </div>
  <iframe id="cadre" hidden title="Montage au stabilo"></iframe>
  <div id="body">
    <div id="formwrap">
      <div class="grid" id="main-fields"></div>
      <div class="advtoggle" id="adv-toggle"></div>
      <div id="advanced">
        <div class="grid" id="adv-fields"></div>
      </div>
    </div>
    <div id="console-col">
      <div id="cmdbar">
        <div class="lbl">Commande</div>
        <div id="cmdpreview">—</div>
      </div>
      <div id="runbar">
        <button class="btn btn-run" id="btn-run">▶ Lancer</button>
        <button class="btn btn-stop" id="btn-stop" disabled>■ Arrêter</button>
        <span id="status"><span class="dot idle"></span>prêt</span>
      </div>
      <div id="ailleurs" hidden></div>
      <div id="console"><span class="placeholder">La sortie du script s'affichera ici.</span></div>
      <form id="saisie" hidden>
        <input type="text" id="saisie-texte" placeholder="Répondre au script…" autocomplete="off" spellcheck="false">
        <button class="btn btn-plain" type="submit">Envoyer</button>
      </form>
    </div>
  </div>
</div>

<div class="modal" id="modal">
  <div class="modalbox">
    <div class="modalhead">
      <button id="m-up">Dossier parent</button>
      <span class="path" id="m-path"></span>
      <button id="m-close">Fermer</button>
    </div>
    <div class="modallist" id="m-list"></div>
    <div class="modalfoot">
      <button class="browsebtn" id="m-pickdir" style="display:none;padding:8px 12px">Choisir ce dossier</button>
    </div>
  </div>
</div>

<div class="modal" id="voix">
  <div class="modalbox" style="width:720px">
    <div class="modalhead"><h2>Quelle voix pour chaque locuteur ?</h2></div>
    <div class="modallist" id="v-list"></div>
    <div class="modalfoot">
      <button class="btn btn-run" id="v-ok">Valider</button>
    </div>
  </div>
</div>

<script>
const MEDIA = ['.mp4','.mkv','.mov','.avi','.webm','.mp3','.wav','.m4a','.flac','.ogg','.opus','.json','.srt','.docx','.ass'];
let SCRIPTS=[], OLLAMA=[], DEPART='', current=null, values={}, evtSource=null;
let modalTarget=null, modalDirMode=false, modalPath='';
let currentRun=null, runScript=null, advOpen=false;

const $=s=>document.querySelector(s);
const el=(t,c,h)=>{const e=document.createElement(t);if(c)e.className=c;if(h!=null)e.innerHTML=h;return e;};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const post=(url,corps)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(corps||{})});

async function boot(){
  const d = await (await fetch('/api/scripts')).json();
  SCRIPTS = d.scripts; DEPART = d.depart;
  OLLAMA  = await (await fetch('/api/ollama')).json();
  const list=$('#scriptlist');
  let groupe=null;
  SCRIPTS.forEach(s=>{
    if(s.groupe_nom!==groupe){ groupe=s.groupe_nom; list.appendChild(el('div','groupe',esc(groupe))); }
    const it=el('div','scriptitem');
    it.innerHTML=`<div class="ic">${s.icon}</div><div><div class="t">${esc(s.label)}</div><div class="d">${esc(s.desc)}</div></div>`;
    it.onclick=()=>select(s.id);
    it.dataset.id=s.id; list.appendChild(it);
  });
  // Une exécution lancée avant le rechargement de la page : on retrouve sa console.
  const encours = await (await fetch('/api/runs')).json();
  const voulu = location.hash.slice(1);
  if(encours.length){ select(encours[0].script); attach(encours[0].id, encours[0].script, encours[0].cmd); }
  else select(SCRIPTS.some(s=>s.id===voulu) ? voulu : SCRIPTS[0].id);
}

function select(id){
  current=SCRIPTS.find(s=>s.id===id);
  history.replaceState(null,'','#'+id);
  document.querySelectorAll('.scriptitem').forEach(e=>e.classList.toggle('active',e.dataset.id===id));
  $('#h-title').textContent=current.icon+'  '+current.label;
  $('#h-desc').textContent=current.desc;
  const page=!!current.page;
  $('#cadre').hidden=!page; $('#body').hidden=page; $('#header').hidden=page;
  if(page){ if(!$('#cadre').src) $('#cadre').src=current.page; return; }
  values={};
  current.fields.forEach(f=>{ values[f.name]= 'default' in f ? f.default : (f.type==='toggle'?false:''); });
  advOpen=false;
  render(); ailleurs();
}

function visible(f){
  if(!f.depends) return true;
  return Object.entries(f.depends).every(([k,v])=>String(values[k]).toLowerCase()===String(v).toLowerCase());
}
function modifie(f){
  if(f.type==='toggle') return !!values[f.name] !== !!f.default;
  const a=String(values[f.name]??'').trim(), b=String(f.default??'');
  if(a===b) return false;
  return !(a!=='' && b!=='' && !isNaN(a) && !isNaN(b) && Number(a)===Number(b));
}

function widget(f){
  const wrap=el('div','field'+(['textarea'].includes(f.type)?' full':''));
  if(f.help) wrap.title=f.help;
  const req=f.required?' <span class="req">*</span>':'';
  if(f.type!=='toggle') wrap.appendChild(el('label',null,esc(f.label)+req));
  const set=v=>{values[f.name]=v;updatePreview();};
  const dependu=SCRIPTS.length && current.fields.some(g=>g.depends && f.name in g.depends);

  if(f.type==='toggle'){
    const t=el('div','toggle'+(values[f.name]?' on':''));
    t.innerHTML=`<div class="sw"></div><div class="lab">${esc(f.label)}</div>`;
    t.onclick=()=>{values[f.name]=!values[f.name];t.classList.toggle('on'); dependu?render():updatePreview();};
    wrap.appendChild(t); return wrap;
  }
  if(f.type==='select'){
    const s=el('select');
    f.choices.forEach(c=>{
      const o=el('option'); o.textContent=c===''?'—':((f.names&&f.names[c])||c);
      o.value=c; if(String(values[f.name])===String(c))o.selected=true; s.appendChild(o);});
    s.onchange=()=>{set(s.value); if(dependu) render();};
    wrap.appendChild(s); return wrap;
  }
  if(f.type==='textarea'){
    const t=el('textarea'); t.value=values[f.name]||''; t.oninput=()=>set(t.value);
    wrap.appendChild(t); return wrap;
  }
  if(f.type==='ollama'){
    const s=el('select');
    const noms=OLLAMA.slice();
    if(values[f.name] && !noms.includes(values[f.name])) noms.unshift(values[f.name]);
    noms.forEach(m=>{
      const o=el('option'); o.value=m;
      o.textContent=m+(OLLAMA.includes(m)?'':' (pas installé)')+(m===f.default?' · par défaut':'');
      if(values[f.name]===m)o.selected=true; s.appendChild(o);});
    s.onchange=()=>set(s.value);
    wrap.appendChild(s); return wrap;
  }
  // text / int / float / list / file / dir / source
  const needsBrowse=['file','dir','source'].includes(f.type);
  const inp=el('input'); inp.type=(f.type==='int'||f.type==='float')?'number':'text';
  if(f.type==='float')inp.step='any';
  inp.placeholder=f.placeholder||(f.type==='source'?'fichier ou adresse':f.type==='list'?'séparés par des virgules':'');
  inp.value=values[f.name]??''; inp.oninput=()=>set(inp.value);
  inp.spellcheck=false;
  if(needsBrowse){
    const row=el('div','inputrow'); row.appendChild(inp);
    const b=el('button','browsebtn','Parcourir'); b.type='button';
    b.onclick=()=>openModal(f.name,f.type==='dir',f.type==='source');
    row.appendChild(b); wrap.appendChild(row);
  } else wrap.appendChild(inp);
  return wrap;
}

function render(){
  const mf=$('#main-fields'), af=$('#adv-fields');
  mf.innerHTML=''; af.innerHTML='';
  let advCount=0;
  current.fields.forEach(f=>{
    if(!visible(f)) return;
    const w=widget(f);
    if(f.adv){af.appendChild(w);advCount++;} else mf.appendChild(w);
  });
  $('#adv-toggle').style.display=advCount?'inline-flex':'none';
  $('#advanced').classList.toggle('open',advOpen);
  updatePreview();
}
function titreAvance(){
  const n=current.fields.filter(f=>f.adv&&visible(f)&&modifie(f)).length;
  $('#adv-toggle').innerHTML=(advOpen?'▾':'▸')+' Options avancées'+(n?` <b>· ${n} modifiée${n>1?'s':''}</b>`:'');
}

let previewTimer=null;
function updatePreview(){
  titreAvance();
  clearTimeout(previewTimer);
  previewTimer=setTimeout(async()=>{
    const r=await post('/api/preview',{script:current.id,values});
    $('#cmdpreview').textContent=(await r.json()).cmd;
  },120);
}

/* ---- file browser modal ---- */
async function openModal(target,dirMode,source){
  modalTarget=target; modalDirMode=dirMode;
  $('#m-pickdir').style.display=dirMode?'inline-block':'none';
  const v=String(values[target]||'');
  await loadDir(v.startsWith('/') ? v : (source ? DEPART : ''));
  $('#modal').classList.add('open');
}
async function loadDir(path){
  const d=await (await fetch('/api/browse?path='+encodeURIComponent(path||''))).json();
  modalPath=d.path; $('#m-path').textContent=d.path;
  const list=$('#m-list'); list.innerHTML='';
  const base=d.path.replace(/\/$/,'');
  d.dirs.forEach(name=>{
    const e=el('div','entry dir'); e.innerHTML=`<span class="ic">📁</span><span>${esc(name)}</span>`;
    e.onclick=()=>loadDir(base+'/'+name); list.appendChild(e);
  });
  if(!modalDirMode) d.files.forEach(name=>{
    const isMedia=MEDIA.some(x=>name.toLowerCase().endsWith(x));
    const e=el('div','entry '+(isMedia?'media':'file')); e.innerHTML=`<span class="ic">${isMedia?'🎬':'📄'}</span><span>${esc(name)}</span>`;
    e.onclick=()=>{ values[modalTarget]=base+'/'+name; closeModal(); render(); };
    list.appendChild(e);
  });
  if(!list.children.length) list.appendChild(el('div','empty','Ce dossier est vide.'));
}
function closeModal(){$('#modal').classList.remove('open');}
$('#m-up').onclick=()=>loadDir(modalPath.replace(/\/[^/]+\/?$/,'')||'/');
$('#m-close').onclick=closeModal;
$('#m-pickdir').onclick=()=>{values[modalTarget]=modalPath;closeModal();render();};
$('#modal').onclick=e=>{if(e.target.id==='modal')closeModal();};

/* ---- run / stream ---- */
function classify(line){
  if(/❌|Error|Traceback|Exception|❗/.test(line))return'l-err';
  if(/✅|terminé|✔/.test(line))return'l-ok';
  if(/⏳|⚠️|attention/i.test(line))return'l-warn';
  if(/^(={3,}|PASSE|🎬|🧠|📝|🎙️|🔊|═)/.test(line)||/^\s*[➤▶]/.test(line))return'l-step';
  if(/^\s+/.test(line))return'l-dim';
  return'';
}
let partielDiv=null;
function auFond(c){ return c.scrollHeight-c.scrollTop-c.clientHeight<60; }
function appendLine(line){
  const c=$('#console'), suivre=auFond(c);
  if(c.querySelector('.placeholder'))c.innerHTML='';
  const div=el('div',classify(line)); div.textContent=line||' ';
  if(partielDiv&&partielDiv.parentNode===c) c.insertBefore(div,partielDiv); else c.appendChild(div);
  if(suivre) c.scrollTop=c.scrollHeight;
}
function setPartiel(texte){
  const c=$('#console'), suivre=auFond(c);
  if(!texte){ if(partielDiv){partielDiv.remove();partielDiv=null;} return; }
  if(c.querySelector('.placeholder'))c.innerHTML='';
  if(!partielDiv||partielDiv.parentNode!==c){ partielDiv=el('div'); c.appendChild(partielDiv); }
  partielDiv.className=classify(texte); partielDiv.textContent=texte;
  if(suivre) c.scrollTop=c.scrollHeight;
}
function setStatus(cls,txt){$('#status').innerHTML=`<span class="dot ${cls}"></span>${txt}`;}
function marquer(){
  document.querySelectorAll('.scriptitem .run').forEach(e=>e.remove());
  if(!runScript) return;
  const it=document.querySelector(`.scriptitem[data-id="${runScript}"]`);
  if(it) it.appendChild(el('div','run'));
}
function ailleurs(){
  // La console montre l'exécution en cours, même lancée depuis un autre formulaire.
  const a=$('#ailleurs');
  const autre=runScript && current && runScript!==current.id;
  a.hidden=!autre;
  if(autre){ const s=SCRIPTS.find(x=>x.id===runScript); a.textContent='En cours : '+s.label+' — revenir à ce formulaire'; a.onclick=()=>select(runScript); }
}

function attach(id,script,cmd){
  currentRun=id; runScript=script; partielDiv=null;
  $('#console').innerHTML='';
  appendLine('$ '+cmd); appendLine('');
  $('#btn-run').disabled=true; $('#btn-stop').disabled=false; $('#saisie').hidden=false;
  setStatus('run','en cours…'); marquer(); ailleurs();
  evtSource=new EventSource('/api/stream/'+id);
  evtSource.onmessage=e=>appendLine(JSON.parse(e.data));
  evtSource.addEventListener('partiel',e=>setPartiel(JSON.parse(e.data)));
  evtSource.addEventListener('voix',e=>choixDesVoix(JSON.parse(e.data)));
  evtSource.addEventListener('done',e=>{
    const code=JSON.parse(e.data).code;
    setPartiel('');
    appendLine(''); appendLine(code===0?'✅ Terminé':'❌ Arrêté sur une erreur (code '+code+')');
    setStatus(code===0?'ok':'err',code===0?'terminé':'erreur');
    cleanup();
  });
  evtSource.addEventListener('error',()=>{ if(evtSource&&evtSource.readyState===2){setStatus('err','liaison perdue');cleanup();} });
}
$('#btn-run').onclick=async()=>{
  const manque=current.fields.find(f=>f.required&&visible(f)&&!String(values[f.name]||'').trim());
  if(manque){ $('#console').innerHTML=''; appendLine('❌ Il manque : '+manque.label); return; }
  const r=await post('/api/run',{script:current.id,values});
  const {id,cmd}=await r.json();
  attach(id,current.id,cmd);
};
$('#btn-stop').onclick=async()=>{ if(currentRun) await post('/api/stop/'+currentRun); };
function cleanup(){
  if(evtSource)evtSource.close(); evtSource=null; currentRun=null; runScript=null;
  $('#btn-run').disabled=false; $('#btn-stop').disabled=true; $('#saisie').hidden=true;
  $('#voix').classList.remove('open'); marquer(); ailleurs();
}
$('#saisie').onsubmit=async e=>{
  e.preventDefault();
  if(!currentRun) return;
  const t=$('#saisie-texte'); const texte=t.value; t.value='';
  await post('/api/input/'+currentRun,{texte});
};

/* ---- choix des voix (doubler.py --map-voices) ---- */
function choixDesVoix(q){
  const m=$('#voix');
  if(!q){ m.classList.remove('open'); return; }
  const son=c=>'/api/son/'+currentRun+'?chemin='+encodeURIComponent(c);
  const genre={male:'voix d\'homme',female:'voix de femme'};
  const list=$('#v-list'); list.innerHTML='';
  q.speakers.forEach(sp=>{
    const d=el('div','locuteur'); d.dataset.id=sp.id;
    const options=['<option value="">Laisser le script choisir</option>'].concat(
      q.voices.map((v,i)=>`<option value="${esc(v.path)}"${i===sp.suggested?' selected':''}>${esc(v.name)}</option>`)).join('');
    d.innerHTML=`<div class="qui">${esc(sp.id)}</div>
      <div class="quoi">${Math.round(sp.duration)} s de parole${genre[sp.gender_guess]?' · '+genre[sp.gender_guess]+' (estimation)':''}${sp.text?' · « '+esc(sp.text)+' »':''}</div>
      <div class="paire">
        <div class="col"><span>Le locuteur</span><audio controls preload="none" src="${son(sp.sample)}"></audio></div>
        <div class="col"><span>Sa voix en doublage</span><select>${options}</select><audio controls preload="none"></audio></div>
      </div>`;
    const s=d.querySelector('select'), a=d.querySelectorAll('audio')[1];
    const maj=()=>{ a.hidden=!s.value; if(s.value) a.src=son(s.value); };
    s.onchange=maj; maj();
    list.appendChild(d);
  });
  m.classList.add('open');
}
$('#v-ok').onclick=async()=>{
  const map={};
  document.querySelectorAll('#v-list .locuteur').forEach(d=>{ const v=d.querySelector('select').value; if(v) map[d.dataset.id]=v; });
  document.querySelectorAll('#v-list audio').forEach(a=>a.pause());
  await post('/api/voix/'+currentRun,{map});
  $('#voix').classList.remove('open');
};

$('#adv-toggle').onclick=()=>{ advOpen=!advOpen; $('#advanced').classList.toggle('open',advOpen); titreAvance(); };

boot();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Panneau de contrôle Traduction")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--verifier", action="store_true",
                        help="Comparer les formulaires aux options des scripts, puis s'arrêter")
    args = parser.parse_args()

    if args.verifier:
        sys.exit(1 if verifier() else 0)

    print("=" * 60)
    print("  🎛️  Panneau de contrôle Traduction")
    print("=" * 60)
    print(f"  Interpréteur : {PYTHON_BIN}")
    print(f"  Dossier      : {SCRIPT_DIR}")
    print(f"  → http://{HOST}:{args.port}")
    # Bilan santé express — failproof, ne bloque jamais le démarrage
    hints = []
    if not shutil.which("ffmpeg"):
        hints.append("ffmpeg introuvable (incrustation/audio HS)")
    if not os.path.exists(PYTHON_BIN):
        hints.append(f"interpréteur interview absent ({PYTHON_BIN})")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        hints.append("ANTHROPIC_API_KEY absente (mets --llm local, ou configure la clé)")
    if hints:
        print("-" * 60)
        for h in hints:
            print("  ⚠️  " + h)
        print("  → diagnostic complet :  python3 doctor.py")
    print("=" * 60)
    app.run(host=HOST, port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
