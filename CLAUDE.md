# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multilingual AI translation toolkit for video/audio content. Independent Python scripts, each implementing a complete pipeline — no shared library, no build system (`monter.py` is the exception: it imports its transcription from `resumer.py` and `clipper.py`).

- **`traduire.py`** — Video subtitle translation (6 passes: WhisperX → Claude analysis → Claude translation → Claude review → re-segmentation → ffmpeg burn)
- **`doubler-mp3-batch.py`** — Batch audio dubbing to MP3 (7 passes: WhisperX → Pyannote → Demucs → Claude translation → TTS synthesis → normalization → assembly). No timing constraints, output can be longer than source. TTS backend: Qwen3-TTS.
- **`doubler.py`** — Video dubbing with voice-over mixing (11 passes: adds isochronic adaptation, two-pass TTS with speed adjustment, voice-over mixing with ducking). TTS backends: Qwen3-TTS (default), ElevenLabs.
- **`clipper.py`** — Viral clip extraction (6 passes: WhisperX → Claude clip selection → optional Claude translation → ffmpeg cut → ASS karaoke subtitles → ffmpeg burn). Selects best passages via `--criteria`, outputs Instagram-style karaoke subtitles (word-by-word groups on black background).
- **`lire.py`** — Read-aloud version of an Antithèse article (6 passes: cookie auth + Bricks Builder extraction → editorial cleanup → voicing plan with per-level pauses → Qwen3-TTS via bridge → **WhisperX verification + targeted re-synthesis** → EBU R128 + MP3 with ID3 tags). Reference voices come from the `voix/` bank; no timing constraints.

  Pass 5 re-transcribes **each chunk** (never the finished MP3 — WhisperX silently drops passages on 10-minute files) and compares it with the exact text that was sent to the bridge. Chunks above 12 % WER, or with a run of ≥4 consecutive missing/added words, are regenerated up to 3 times; since Qwen3-TTS samples (`do_sample=True`), a retry almost always fixes a glitch. Comparison runs on a French phonetic reduction so that silent plurals, digits vs spelled numbers, acronym spacing, Swiss proper nouns WhisperX cannot spell (`Sonderegger`/`Sonderreger`) and letter-name homophones (`Air2030`/`R2030`, both /ɛʁ/) never trigger a pointless re-synthesis. Skip with `--sans-verification`.
- **`nettoyer.py`** — Light-touch audio restoration for dhamma talks (7 passes: analysis/hum detection → 48 kHz conditioning + 60 Hz high-pass → denoise → optional gentle leveling → linear BS.1770 loudness normalization (no limiter unless bell peaks force it) → MP3 LAME V0 → DNSMOS before/after QC + JSON report). Denoise engines via `--moteur`: `auto` (default — DeepFilterNet3 first, DNSMOS-arbitrated fallback to MossFormer2 when a voice reacts badly to DFN), `dfn`, `mossformer2` (ClearerVoice in dedicated `clearvoice` conda env via `mossformer_bridge.py`; caveat: compresses dynamics ~8 dB), `afftdn` (gentlest, modest cleaning). Hybrid iZotope RX workflow via `--exporter-rx` / `--importer-rx`. Best engine is per-recording — trust ears over metrics.

- **`monter.py`** — Text-based video/audio editing ("montage au stabilo"): local Flask page, on port 5006 when run alone and under `/montage` inside `gui.py` (routes live in `creer_blueprint()`; the page learns its prefix through the `__BASE__` placeholder). WhisperX transcript with word timestamps → the user highlights the passages to keep → ffmpeg montage with a fade at each cut (MP4, or MP3/M4A/WAV). Imports `transcribe_whisperx`/`extract_audio` from `resumer.py` and `interpolate_word_times` from `clipper.py`.

  Things that look odd but are deliberate: **transcription runs in a subprocess** (`--tache-transcrire`), because the toolkit's GPU lock is held until the process that took it exits — the server must never take it. **Cut points are not the word timestamps**: `point_calme()` looks at the energy of `audio16k.wav` and cuts in the quietest spot near the target margin (in a real silence that is the margin itself, in continuous speech the dip between two words). **Audio export decodes the source once, start to end, and picks passages on the fly** — same decoding as the transcription, so timestamps stay exact even on variable-bitrate MP3 where seeking drifts; for the same reason the in-page player gets an AAC copy of any MP3. **Video export** encodes one MKV per passage (x264 + PCM) then concatenates with the picture copied and the sound encoded once. **Two modes** (`mode` in `selection.json`): `garder` (the highlighted text makes the montage) and `couper` (the highlighted text is removed: `calculer_coupes(inverse=True)` works on the complement of the ranges, from 0 to the end of the file, and does not let a cut reach into the removed word). Kept pieces are never merged by time: a single word left out between two highlighted ranges is removed, and when what is removed is too short for two margins the cut is made once, in its middle. Each piece carries `vi`/`vo`: the picture fades to black only where at least `SEUIL_FONDU_IMAGE` (1 s) is removed, otherwise it is a straight cut; the sound always fades. Re-transcribing a montage proves that passages were removed but says nothing about a single removed word: Whisper writes the missing word back from context. **Logs**: every montage comes with `<name>.txt` (readable cut log) and `<name>.edl` (CMX 3600, cuts only, fades as comments, record side starting at 01:00:00:00; 25 fps by convention for sound-only sources; the source's embedded timecode, drop-frame included, is honoured). `chemin_de_sortie()` picks a stem free for all three files. Keep the EDL header to `TITLE` and `FCM`: OpenTimelineIO's reader rejects a comment placed before the first event (validate with `otio-cmx3600-adapter` in a throwaway venv). **Edge markers**: `transcription.json` version 2 always has a wordless token for the start and the end of the file when they last ≥ 0.3 s (`poser_les_bords`); a version-1 file is upgraded on load and the saved selection is shifted by one. Gaps of ≥ 2 s between words become "sans paroles" tokens (text `None`) that can be highlighted like a word. Projects live in `work-files/montage/<name>-<id>/` (`projet.json`, `transcription.json`, `selection.json`, `audio16k.wav`). Automatic language detection can be wrong (a UK speech was detected as Welsh, which has no alignment model): the page then asks for the language.

  **Reuse of an existing translation (2026-09-29)**: a file named like an output of `traduire.py` (`<base>_<lang>`) or `doubler.py` (`<base>_dubbed_<lang>`) is never transcribed: `chercher_texte_existant()` finds its work files by name and `reprendre_texte()` builds the tokens from them in the same subprocess (no GPU, about a second). Three kinds (`origine` in `transcription.json`): `doublage` (text = `text_adapted`), `sous-titres` (text = the `.srt` next to the video, else `text_tgt`), `origine` (the source video itself: the word timestamps of `segments.json` are taken as they are). **The position of a dubbing clip is measured, not computed**: `doubler.py`'s mix moves clips (lead-in, borrowing, gluing of sentence continuations, push-later) and saves none of it, so `situer_clip()` cross-correlates each `tts_normalized/seg_NNNN.wav` with the file's own sound (correlation 0.98–1.00 on real dubs, still exact after `finaliser.py`); if fewer than half of the clips are found the work files belong to another file and the sound is transcribed instead. Words inside a sentence are estimated (`repartir()`: share of the spoken time by word length, punctuation anchored to the pauses of the clip, or of the original voice for subtitles), hence `estime` in `transcription.json` and `JEU_ESTIME` in `calculer_coupes()` (the quiet point is also searched a little inside the word). Measured against WhisperX on two dubs: median gap 0.10–0.13 s; the large gaps were WhisperX's own errors (on a voice-over mix it moved words by 5 s). In a dubbed video the wordless tokens read "sans doublage": the original voice is heard there. The page can switch between the reused text and a transcription (`/api/retranscrire` with `texte`); the highlight is kept in seconds (`temps` in `selection.json`) and mapped onto the new tokens. `projet.json` keeps `reprise` (the dict, or `null` once a transcription was asked for).

- **`gui.py`** — Local control panel (Flask, port 5005; installed as the `traduction-gui` .deb launcher, which only checks `/api/scripts` and opens a browser window). Reworked 2026-09-28.

  The `SCRIPTS` manifest holds only what a script cannot say itself (label, grouping, field type). **Defaults, choices and help are read from each script's `add_argument` calls by AST** (`_module`/`_valeur`, no import, no execution; resolves module constants, `other_script.CONSTANT`, `from traduire import X`, `list(DICT.keys())`), and only values that differ from the script's default reach the command line. Never put a `default` back in the manifest: that is how the panel once forced `qwen3.6:27b` on `resumer.py` after the script had moved to `mistral-small`. Run `python gui.py --verifier` after changing a script's options. Scripts whose file is missing are hidden (the public toolkit has fewer files), and so is the montage entry when `monter.py` cannot be imported.

  The console reads the child's output in raw chunks, not lines: a lone `\r` rewrites the current line (progress bars), an unterminated line stays visible (an `input()` prompt), and the input row writes to the child's stdin. `doubler.py --map-voices` is answered through its file handshake (`@@VOICEMAP_REQUEST@@` → dialog → `voicemap_response.json`), the same protocol the extension daemon uses. `monter.py`'s blueprint is mounted under `/montage` and shown in an iframe.

- **`finaliser.py`** — "Podcast-ready" finishing for a sound or video file, all in one (9 passes: channels → conditioning → analysis + head/tail trim → conditional denoise → conditional leveling → loudness → fades → encoding → check of the PUBLISHED file). Built on `nettoyer.py` (`import nettoyer as N`): it reuses its measured choices and adds the decisions, video, channels, fades and delivery formats. Mono output by default (`--canaux stereo`), −19 LUFS mono / −16 stereo.

  Every correction is conditional and reported. Thresholds come from measurements, keep them unless you re-measure: denoise only if the background is less than 40 dB under speech (a published studio podcast measured 44, a hall and an audible hiss 34–37), is steady, and is not music (`nature_du_fond`: share of the background's energy carried by spectral lines; noise ≈ 0, music ≈ 0.9). Mains hum is searched **in the pauses** (under the voice the 150 Hz harmonic is masked by the voice's fundamental), and only accepted as exact multiples of 50/60 Hz ± 0.5 Hz: a chord, or a low G at 49 Hz with its harmonics, was once notched as hum. Hum is removed **before** the background is measured.

  Two encoder facts, both measured, both handled in `encoder_piste` by measuring the encoded file: LAME lowers the level by itself at constant bitrate (−0.5 dB at 128k, −0.3 at 192k, nothing in VBR), and ffmpeg's native AAC encoder sometimes adds a peak the sound does not have (+3.6 dB mid-speech, input-dependent; `-aac_pns 0` avoids it). The limiter may take at most `LIMITEUR_MAX_DB` (4 dB) from the peaks; beyond that the target loudness is lowered instead. Fades are written to a new file: soundfile cannot open an ffmpeg-written WAV in `r+` mode.

  **`--sur-place` and the `--finaliser` option of the other scripts (2026-09-29)**: `traiter_sur_place()` runs the normal pipeline into a hidden `.finition_*` folder next to the file, never trims (subtitles and work files must stay aligned with the video, and `monter.py` reuses them), then replaces the file and writes `<stem>_finition.json`. `traduire.py`, `traduire-pro.py`, `sous_titrer_docx.py`, `doubler.py` and `clipper.py` call it as a subprocess when given `--finaliser` (each has its own copy of `finaliser_le_son()`, `traduire-pro.py` imports `traduire.py`'s); a failed finishing never fails the script. `doubler.py` finishes the video before extracting automatic clips from it, and skips finishing with `--dual-audio` (`finaliser.py` keeps one sound track). The extension's daemon recognises the scripts that produce a video by the text `add_argument("--finaliser"` in their source: keep that spelling.

## Running the Scripts

```bash
# Subtitles
python traduire.py video.mp4                        # EN → FR (default)
python traduire.py video.mp4 -s ja -t en             # JA → EN
python traduire.py video.mp4 --style netflix
python traduire.py video.mp4 --resume segments.json  # resume from checkpoint

# Audio dubbing (batch — processes all MP3+MP4 in current directory)
python doubler-mp3-batch.py
python doubler-mp3-batch.py --file specific.mp3

# Video dubbing with voice-over
python doubler.py video.mp4
python doubler.py video.mp4 --no-voiceover  # pure dubbing
python doubler.py video.mp4 --tts qwen3tts  # Qwen3-TTS (FR excellent)

# Audio restoration (dhamma talks — batch or single file)
python nettoyer.py causerie.mp3                  # denoise + normalize → causerie_nettoye.mp3
python nettoyer.py dossier/ -o propre/           # batch
python nettoyer.py causerie.mp3 --reduction 10   # lighter denoise (more room tone kept)
python nettoyer.py causerie.mp3 --exporter-rx    # hybrid iZotope RX workflow

# Viral clip extraction
python clipper.py video.mp4 --criteria "passage le plus marquant"
python clipper.py video.mp4 --criteria "moment drôle" --duration 180-600 -n 2
python clipper.py video.mp4 --criteria "key insights" --target-lang fr
python clipper.py video.mp4 --resume video_clips.json --criteria "test"

# Text-based editing (opens http://127.0.0.1:5006)
python monter.py                 # choose the file in the page
python monter.py entretien.mp4   # open this file directly

# Podcast-ready finishing (sound or video)
python finaliser.py entretien.wav                 # → entretien_podcast.mp3 (mono)
python finaliser.py entretien.wav --analyse-seule # diagnosis only
python traduire.py video.mp4 --finaliser          # any video-producing script: sound finished as the last step

# Article read aloud (Antithèse)
python lire.py https://www.antithese.info/articles/mon-article/   # voix homme1 par défaut
python lire.py URL --voix femme1 -o lecture.mp3
python lire.py URL --texte-seul          # vérifier le texte lu avant de synthétiser
python lire.py URL --sans-signature      # sans « Par <auteur> »
python lire.py URL --sans-verification   # sauter la passe 5 (plus rapide, sans filet)
```

## Environment Requirements

- **`ANTHROPIC_API_KEY`** (optional — scripts default to a local LLM via Ollama; only needed with `--llm claude`). With `--analysis-llm auto` (the default), the **analysis pass** alone (glossary/proper-nouns/domain) uses Claude when this key is set — one cheap call that also improves the local translation; falls back to local otherwise.
- **`HF_TOKEN`** (required for dubbing scripts — Pyannote speaker diarization)
- **`~/.antithese_cookies.json`** (required by `lire.py` — browser cookie export for the paywall; the `wordpress_logged_in_*` cookie expires, re-export when articles come back truncated). Password fallback via `~/.antithese.json` or `ANTITHESE_USER`/`ANTITHESE_PASS`.
- **`ffmpeg`** system binary
- GPU with CUDA recommended (WhisperX, Qwen3-TTS)

```bash
# Main env (interview) — all deps
pip install whisperx anthropic torch torchaudio demucs pydub soundfile \
            numpy praat-parselmouth pyworld TTS flask --break-system-packages

# Qwen3-TTS (default dubbing backend) runs in its own conda env
conda create -n qwen3tts python=3.12
conda run -n qwen3tts pip install -U qwen-tts soundfile
conda run -n qwen3tts pip install -U flash-attn --no-build-isolation  # recommended
```

### TTS Bridge Isolation

Each TTS backend with incompatible dependencies runs in a dedicated conda env, communicating via a **bridge subprocess** (JSON-lines over stdin/stdout). Bridges protect stdout from library spam by redirecting it to stderr.

| Backend | Conda env | Bridge | Notes |
|---------|-----------|--------|-------|
| Qwen3-TTS | `qwen3tts` | `qwen3tts_bridge.py` | 1.7B Base, 10 langs, voice cloning with ref_text |

(ElevenLabs is API-based and needs no bridge. XTTS v2 was removed on 2026-09-20.)

Bridges are spawned automatically when `--tts <backend>` is used; no manual activation needed.

## Architecture Notes

### Claude's Three Roles in Every Pipeline
1. **Analyst** — content summary, glossary, tone/domain detection (`ContentAnalysis`)
2. **Translator** — contextual translation using overlapping chunk windows (60 segments, 8 overlap)
3. **Reviewer** — quality check for naturalness, coherence, contresens

### Key Constants (top of each script)
- `CLAUDE_MODEL = "claude-opus-5"` (since 2026-09-20) — the model used for all Claude calls
- `WHISPER_MODEL = "large-v3"` — transcription model
- Subtitle constraints in `traduire.py`: `MAX_CHARS_PER_LINE = 42`, `MAX_CPS = 17`
- TTS speed range in dubbing: `QWEN3TTS_SPEED_MIN = 0.70`, `QWEN3TTS_SPEED_MAX = 1.50` (Qwen3-TTS via atempo)
- Structural pauses in `lire.py`: `PAUSES` dict — title 1000 ms, byline 700, standfirst 1100, subheading 900 before / 600 after, paragraph 550, intra-block join 60

### Reference Voice Bank (`voix/`)
Eight cloned voices (`homme1-4`, `homme5-antithese`, `femme1-3`), all **mono 24 kHz, −3 dBFS peak**. Used by `doubler.py` / `doubler-mp3-batch.py` (`--ref-voices`, gender-matched via the `homme*` / `femme*` glob) and by `lire.py` (`--voix`). Pre-retouch stereo originals are kept as `voix/<name>.wav.old` — the `.wav.old` suffix deliberately stays outside the `*.wav` globs.

Two conventions that look like bugs but are deliberate, both established by listening tests on 2026-09-19:
- **No `ref_text` for bank voices.** `_get_best_ref` returns `(path, "")`, so the bridge falls back to `x_vector_only`. Qwen3-TTS's ICL mode shortens inter-sentence pauses and sounds less natural, despite the ~0.89 vs ~0.75 similarity figure in the docstring (that figure measures timbre, not prosody).
- **Full stops are kept everywhere (`lire.py` and, since 2026-09-20, the dubbing scripts, all TTS backends).** The old "nuclear fix" (`.` → `;`) targeted XTTS, now removed, and is gone for ElevenLabs too (not measured there, user's call). On Qwen3-TTS `;` leaves the sentence hanging and `.` closes it, and neither vocalises the word "point" (WhisperX/Whisper re-transcription). Listening test of 2026-09-20 in `essais-ponctuation/` (final F0 slope −27/−20 Hz/s with `.`, −9/+17 with `;`); the earlier `lire.py` measurement was `;` → +370 Hz/s, `.` → −938 Hz/s.

### Core Data Structures (dataclasses)
- **`Segment`** — atomic speech unit with timing, source text, translated text, speaker label, word-level alignment
- **`ContentAnalysis`** — summary, glossary, speakers_description, tone, domain
- **`SpeakerProfile`** (dubbing scripts) — gender, F0 median, reference clips for voice cloning
- **`ClipSelection`** (clipper.py) — clip_index, seg_start/end, start/end times, titre, justification, segments list

### TTS Bridges (`*_bridge.py`)
- Run under `~/miniconda3/envs/<backend>/bin/python`
- Protect stdout (JSON channel) by redirecting all library prints to stderr
- Commands: `init` (load model), `generate` (text → WAV at 24kHz), `quit`
- Model stays loaded for the entire dubbing session (one subprocess per run)
- Text chunking, concatenation, resampling (44.1kHz), and atempo speed adjustment remain in the main scripts
- **Qwen3-TTS specifics**: caches voice clone prompts per ref_audio; uses `x_vector_only_mode` when `ref_text` (transcript of reference) is not provided

### Resumption
All scripts save intermediate JSON files (segments, analysis) and can resume from checkpoints. Outputs are placed next to the input file with language suffix (e.g., `video_fr.mp4`, `video_fr.srt`).

### Language-Aware Processing
- Language-specific line-breaking rules (orphan word prevention for articles/prepositions)
- Politeness conventions (tu/vous, du/Sie) handled in translation prompts

## Code Style

- Written entirely in French (comments, docstrings, variable names, log messages)
- No type hints beyond dataclass fields and `Optional`
- No tests, no linter config — scripts validated through built-in `check_dependencies()` / `check_ffmpeg()`
- 4-space indentation, standard Python conventions
