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

- **`habiller.py`** — Branding of a talk video as in `kDrive/video-workflow/2026-04-23-croire-pour-etre-heureux.mp4` (its Shotcut project `latest.mlt` is the spec): the kit's jingle (`kit-epc/jingle-final.mp4`, 8.58 s) overlaid on the first and last seconds while the talk goes on underneath, sound mixed as in the model; the white logo (`kit-epc/logo blanc.png`) top-left at the model's exact affine rect (−17.66 −69.24 562.83×316.59 in 1080×1080) in between. Input padded to 1080×1080, 30 fps, NVENC if present; output `<stem>_epc.mp4`; duration and true peak checked. Options `--sans-intro/--sans-outro/--sans-logo`, `--jingle-db`, `--logo-rect`.
- **`epc.py`** — The enpleineconscience.ch podcast page (2026-10-05): from a talk or guided-meditation video to the published episode and the branded video, in one Flask page (port 5007 alone, `/epc` inside `gui.py`, same `creer_blueprint()`/`__BASE__` pattern as `monter.py`). It only orchestrates the scripts that know the job: **analysis** = Silero VAD in a subprocess (`--vad`, torch hub cache, CPU) for where speech starts and ends + `finaliser.py --analyse-seule` parsed from its printed diagnosis (first/last *sound*, background, loudness); the page proposes the trim on the *speech* (0.4 s air before, 1 s after), which also removes mic noise before the first word that a silence-based trim would keep, and the user listens at both points in the page's player before accepting. **Bowl sounds are kept** (2026-10-05, `_sons_de_bol` in the VAD subprocess): in the two speechless margins it looks for a held note (spectral flatness < 0.03 over 100–4000 Hz, peak frequency stable within 2 % from frame to frame, ≥ 0.6 s, > 8 dB above the margin's floor); one or three strikes before the first word or after the last one extend the proposal (`bol_avant`/`bol_apres` in the analysis, 2 s of air after the last ring instead of 1) and are never reported as noise. Calibrated on the kit's bowl (flatness 0.000–0.003, speech 0.006–0.3) and verified on a real talk whose three closing strikes (1 219 Hz) sit 49 s after the last word. **Production** = `finaliser.py` on the video (`--format mp4 --debut/--fin --garder-travail`) → `<date>-<slug>_podcast.mp4`; the mp3 is encoded by `finaliser.py` again from the kept `07_final.wav` (`--format mp3 --debit 192`, no trim/denoise/leveling, so loudness and the LAME level compensation are checked on the published file), then cover and ID3 tags are added by stream copy with the exact conventions of `causeries.py` (album « Méditations guidées », artist enpleineconscience.ch, `cover-1600.jpg`); then `habiller.py` → `<date>-<slug>_epc.mp4`. **Publication** reuses `causeries.py` (loaded from `~/code/causeries-podcast`, env `EPC_CAUSERIES`; its `.env` holds the WordPress credentials): `wp/v2/podcast` post in the chosen series (list read from the site's `series` taxonomy), episode number = next one only for a series that numbers its episodes (the causeries do, the meditations don't), `published_from_file()` prevents a double publication. Publishing is a separate button after production, never automatic; pressing it again after a new production **replaces the episode's audio** when the mp3 of the same name differs in size from the online one (`filesize_raw` meta): new media uploaded, `audio_file`/`duration`/`filesize` updated on the post, old media deleted (`remplace` in `publication`). **Defaults are static (2026-10-05)**: `debruitage` and `nivelage` default to `jamais` for new projects. Lesson of the first real episode, measured against the original over the whole 56 min: the dynaudnorm leveler had lowered the voice in 60 passages by 4–43 dB (every sentence end before a silence), the user heard it as « le son de ma voix n'arrête pas d'être baissé arbitrairement … atroce » and asked for « quelque chose de beaucoup plus simple ». On a guided meditation (long silences, bowls) nothing dynamic should touch the voice: trim, silenced ranges, 60 Hz high-pass, one constant gain to the target (limiter only on the bowl peaks), fades. The selects stay available for a talk recorded in a noisy room. **Noise during a silence (2026-10-05)**: the field « Plages à faire taire » (`taire`, « 47:46-49:40, … », source times like début/fin, parsed by `plages()`) becomes `finaliser.py --taire` per range, and the analysis has a checkbox « chercher aussi les bruits dans les silences » (`chercher_bruits` → `--bruits` on the VAD subprocess, `_bruits_dans_les_silences`): in the frames outside the VAD zones (±0.5 s) and outside the bowl sounds, what exceeds the speechless floor (10th centile) by `BRUIT_SEUIL_DB` (15) and is not a held note, grouped across gaps up to 15 s, kept from 1 s of cumulated excess; each finding is listed under the player with ▶ écouter and « faire taire » (adds the range with 0.5 s of air to the field). Calibrated on a 56-min guided meditation: 12 dB gave fifteen findings including breaths; 15 dB / 1 s gave three, the real one at 51 dB above the floor. Projects in `work-files/epc/<hash>-<name>/projet.json`, outputs in `output/epc/<date>-<slug>/`. Site-specific: NOT in `sync-toolkit.sh`.

  **YouTube (2026-10-05, `epc_youtube.py`)**: the publish step first sends the branded video to the channel (`youtube_mode` = `envoyer`, the default; `lien` = paste the address of a video already online; `aucun`), adds it to the causeries playlist `PLyJbW1jeWmiuu4fT99zWZgT0db3UIqs8C`, sets the thumbnail, then creates the WordPress episode with the video link and appends the episode link to the YouTube description. The upload happens once (`publication.youtube` in `projet.json`); « Publier » with site mode `aucun` only uploads, and when nothing was produced it sends the *source* video as it is (a video already finished elsewhere), so the mp3 is required only for the WordPress episode. `EPC_PLAYLIST=""` disables the playlist (tests); the `playlist` field (default on) does the same per project. **A second page, « Publier une vidéo sur YouTube »** (`/youtube/` in the same blueprint, `/epc/youtube/` in `gui.py`, `YOUTUBE_HTML` sharing the podcast page's `STYLE`), is the short path for a video already edited elsewhere: same projects, tasks, thumbnail and publish code, with `youtube_mode=envoyer` and site mode `aucun` forced by the page. **Thumbnail** = a frame of the *source* video at `miniature_instant` (default: 3 s after the first word of the analysis, else 10 % in) + the white logo card top-right (site icon `app_icon_1024.png` cached in `~/.config/epc/icone.png` + « enpleineconscience.ch » in Lato Bold), the title in blue handwriting (Kaushan Script, OFL, downloaded once into `~/.config/epc/polices/`; tilted 3°, white halo, placed on the side of the frame with the fewest edges, size adapted to length, 4 lines max) unless a shorter `miniature_texte` is given; made on demand (`/api/miniature/<pid>`, « image du lecteur » maps the player's time back to the source by adding the chosen `debut`) and again at production as `<base>_miniature.jpg`. Credentials: OAuth desktop client in `~/.config/epc/client_secret.json` (Google Cloud project `enpleineconscience-510708` of the channel's account, YouTube Data API v3 enabled, app « Podcast enpleineconscience.ch » published (*In production*, privacy policy = the site's existing `/confidentialite-applications/` page), so the refresh token is durable), token in `youtube-token.json` after `python epc_youtube.py --connecter` (`--sans-navigateur` prints the address instead of opening a browser; also the « Connecter YouTube » button). The channel is a *brand account*: at the consent screen choose the channel, not the Google account; `connecter()` refuses a token whose channel is not `@enpleineconscience-ch`. Single scope `youtube`. Known YouTube rules: a project that has not passed YouTube's API audit gets its uploads forced to *private* (publish from YouTube Studio, or request the audit); an upload costs 1 600 of the 10 000 daily quota units; an OAuth app left « in testing » has its refresh tokens expire after 7 days (hence the app is published). Libraries `google-api-python-client` + `google-auth-oauthlib` in the `interview` env; the page runs without them (`YOUTUBE_ERREUR`).
- **`monter.py`** — Text-based video/audio editing ("montage au stabilo"): local Flask page, on port 5006 when run alone and under `/montage` inside `gui.py` (routes live in `creer_blueprint()`; the page learns its prefix through the `__BASE__` placeholder). WhisperX transcript with word timestamps → the user highlights the passages to keep → ffmpeg montage with a fade at each cut (MP4, or MP3/M4A/WAV). Imports `transcribe_whisperx`/`extract_audio` from `resumer.py` and `interpolate_word_times` from `clipper.py`.

  Things that look odd but are deliberate: **transcription runs in a subprocess** (`--tache-transcrire`), because the toolkit's GPU lock is held until the process that took it exits — the server must never take it. **Cut points are not the word timestamps**: `point_calme()` looks at the energy of `audio16k.wav` and cuts in the quietest spot near the target margin (in a real silence that is the margin itself, in continuous speech the dip between two words). **Audio export decodes the source once, start to end, and picks passages on the fly** — same decoding as the transcription, so timestamps stay exact even on variable-bitrate MP3 where seeking drifts; for the same reason the in-page player gets an AAC copy of any MP3. **Video export** encodes one MKV per passage (x264 + PCM) then concatenates with the picture copied and the sound encoded once. **Two modes** (`mode` in `selection.json`): `garder` (the highlighted text makes the montage) and `couper` (the highlighted text is removed: `calculer_coupes(inverse=True)` works on the complement of the ranges, from 0 to the end of the file, and does not let a cut reach into the removed word). Kept pieces are never merged by time: a single word left out between two highlighted ranges is removed, and when what is removed is too short for two margins the cut is made once, in its middle. Each piece carries `vi`/`vo`: the picture fades to black only where at least `SEUIL_FONDU_IMAGE` (1 s) is removed, otherwise it is a straight cut; the sound always fades. Re-transcribing a montage proves that passages were removed but says nothing about a single removed word: Whisper writes the missing word back from context. **Logs**: every montage comes with `<name>.txt` (readable cut log) and `<name>.edl` (CMX 3600, cuts only, fades as comments, record side starting at 01:00:00:00; 25 fps by convention for sound-only sources; the source's embedded timecode, drop-frame included, is honoured). `chemin_de_sortie()` picks a stem free for all three files. Keep the EDL header to `TITLE` and `FCM`: OpenTimelineIO's reader rejects a comment placed before the first event (validate with `otio-cmx3600-adapter` in a throwaway venv). **Edge markers**: `transcription.json` version 2 always has a wordless token for the start and the end of the file when they last ≥ 0.3 s (`poser_les_bords`); a version-1 file is upgraded on load and the saved selection is shifted by one. Gaps of ≥ 2 s between words become "sans paroles" tokens (text `None`) that can be highlighted like a word. Projects live in `work-files/montage/<name>-<id>/` (`projet.json`, `transcription.json`, `selection.json`, `audio16k.wav`). Automatic language detection can be wrong (a UK speech was detected as Welsh, which has no alignment model): the page then asks for the language.

  **Reuse of an existing translation (2026-09-29)**: a file named like an output of `traduire.py` (`<base>_<lang>`) or `doubler.py` (`<base>_dubbed_<lang>`) is never transcribed: `chercher_texte_existant()` finds its work files by name and `reprendre_texte()` builds the tokens from them in the same subprocess (no GPU, about a second). Three kinds (`origine` in `transcription.json`): `doublage` (text = `text_adapted`), `sous-titres` (text = the `.srt` next to the video, else `text_tgt`), `origine` (the source video itself: the word timestamps of `segments.json` are taken as they are). **The position of a dubbing clip is measured, not computed**: `doubler.py`'s mix moves clips (lead-in, borrowing, gluing of sentence continuations, push-later) and saves none of it, so `situer_clip()` cross-correlates each `tts_normalized/seg_NNNN.wav` with the file's own sound (correlation 0.98–1.00 on real dubs, still exact after `finaliser.py`); if fewer than half of the clips are found the work files belong to another file and the sound is transcribed instead. Words inside a sentence are estimated (`repartir()`: share of the spoken time by word length, punctuation anchored to the pauses of the clip, or of the original voice for subtitles), hence `estime` in `transcription.json` and `JEU_ESTIME` in `calculer_coupes()` (the quiet point is also searched a little inside the word). Measured against WhisperX on two dubs: median gap 0.10–0.13 s; the large gaps were WhisperX's own errors (on a voice-over mix it moved words by 5 s). In a dubbed video the wordless tokens read "sans doublage": the original voice is heard there. The page can switch between the reused text and a transcription (`/api/retranscrire` with `texte`); the highlight is kept in seconds (`temps` in `selection.json`) and mapped onto the new tokens. `projet.json` keeps `reprise` (the dict, or `null` once a transcription was asked for).

- **`gui.py`** — Local control panel (Flask, port 5005; installed as the `traduction-gui` .deb launcher, which only checks `/api/scripts` and opens a browser window). Reworked 2026-09-28.

  The `SCRIPTS` manifest holds only what a script cannot say itself (label, grouping, field type). **Defaults, choices and help are read from each script's `add_argument` calls by AST** (`_module`/`_valeur`, no import, no execution; resolves module constants, `other_script.CONSTANT`, `from traduire import X`, `list(DICT.keys())`), and only values that differ from the script's default reach the command line. Never put a `default` back in the manifest: that is how the panel once forced `qwen3.6:27b` on `resumer.py` after the script had moved to `mistral-small`. Run `python gui.py --verifier` after changing a script's options. Scripts whose file is missing are hidden (the public toolkit has fewer files), and so is the montage entry when `monter.py` cannot be imported.

  The console reads the child's output in raw chunks, not lines: a lone `\r` rewrites the current line (progress bars), an unterminated line stays visible (an `input()` prompt), and the input row writes to the child's stdin. `doubler.py --map-voices` is answered through its file handshake (`@@VOICEMAP_REQUEST@@` → dialog → `voicemap_response.json`), the same protocol the extension daemon uses. `monter.py`'s blueprint is mounted under `/montage` and shown in an iframe.

- **`finaliser.py`** — "Podcast-ready" finishing for a sound or video file, all in one (9 passes: channels → conditioning → analysis + head/tail trim → conditional denoise → conditional leveling → loudness → fades → encoding → check of the PUBLISHED file). Built on `nettoyer.py` (`import nettoyer as N`): it reuses its measured choices and adds the decisions, video, channels, fades and delivery formats. Mono output by default (`--canaux stereo`), −19 LUFS mono / −16 stereo.

  Every correction is conditional and reported. Thresholds come from measurements, keep them unless you re-measure: denoise only if the background is less than 40 dB under speech (a published studio podcast measured 44, a hall and an audible hiss 34–37), is steady, and is not music (`nature_du_fond`: share of the background's energy carried by spectral lines; noise ≈ 0, music ≈ 0.9). Mains hum is searched **in the pauses** (under the voice the 150 Hz harmonic is masked by the voice's fundamental), and only accepted as exact multiples of 50/60 Hz ± 0.5 Hz: a chord, or a low G at 49 Hz with its harmonics, was once notched as hum. Hum is removed **before** the background is measured.

  Two encoder facts, both measured, both handled in `encoder_piste` by measuring the encoded file: LAME lowers the level by itself at constant bitrate (−0.5 dB at 128k, −0.3 at 192k, nothing in VBR), and ffmpeg's native AAC encoder sometimes adds a peak the sound does not have (+3.6 dB mid-speech, input-dependent; `-aac_pns 0` avoids it). The limiter may take at most `LIMITEUR_MAX_DB` (4 dB) from the peaks; beyond that the target loudness is lowered instead. Fades are written to a new file: soundfile cannot open an ffmpeg-written WAV in `r+` mode.

  **`--taire DEBUT-FIN` (2026-10-05, repeatable, times on the source file)**: a stretch of room noise during a silence (people moving, a chair) is replaced by *room tone*, never by digital silence (a hole is as audible on headphones as the noise was). `taire()` runs right after the trim, on both the processed sound and the untouched reference, so denoise, leveling and the DNSMOS check all see the quiet version. Room tone = the 3 s window inside the stretch or within 20 s around it whose loudest 0.1 s frame is lowest (1.5 s then 1 s if nothing that long sits within 8 dB of the quietest frames), tiled with 0.25 s equal-power joins, every other copy reversed, and blended in over 0.2 s half-cosine fades inside the stretch; the report keeps `plan.taire` and `plan.fond_salle_pris_s`. Measured on a synthetic file: the stretch lands exactly at the floor level, the only leak is the 0.2 s fade when the noise starts right at the given bound, so give half a second of margin.

  **Leveling (2026-10-05, `nettoyer.niveler`, shared with `nettoyer.py`)**: dynaudnorm was dropped. Its threshold option (t=0.02) made the gain plunge inside every silence (down to −30 dB) with an 8–15 s ramp on each side, so every sentence end before a long silence faded out and the first word after faded in; the user heard it « affreusement » at 47:38 of a guided meditation once the room noise there had been replaced by room tone. The new leveler measures only speech frames (0.5 s frames above the 5th-centile floor + 15 dB), computes a short-term loudness over 3 s of speech frames, pulls it toward the median speech loudness within [−6, +12] dB, **holds** the gain through silences (linear interpolation between the speech before and after, never a dip), then smooths with a 15 s Gaussian. Measured on the 4-min excerpt around that cut: gain −2.4…+1.6 dB, flat (−0.3 dB) across the cut.

  **`--sur-place` and the `--finaliser` option of the other scripts (2026-09-29)**: `traiter_sur_place()` runs the normal pipeline into a hidden `.finition_*` folder next to the file, never trims (subtitles and work files must stay aligned with the video, and `monter.py` reuses them), then replaces the file and writes `<stem>_finition.json`. `traduire.py`, `traduire-pro.py`, `sous_titrer_docx.py`, `doubler.py` and `clipper.py` call it as a subprocess when given `--finaliser` (each has its own copy of `finaliser_le_son()`, `traduire-pro.py` imports `traduire.py`'s); a failed finishing never fails the script. `doubler.py` finishes the video before extracting automatic clips from it, and skips finishing with `--dual-audio` (`finaliser.py` keeps one sound track). The extension's daemon recognises the scripts that produce a video by the text `add_argument("--finaliser"` in their source: keep that spelling.

  **Voice-over mix, the glue has a ceiling (2026-10-08, `VO_MAX_AHEAD_MS` = 3000)**: `mix_audio_voiceover` glues a sentence continuation (previous segment of the same speaker not ending on final punctuation) right after the previous clip, on purpose without a floor at `seg.start`. On a slow, solemn speech whose long enumeration is one sentence for fourteen segments, with French clips 14 % shorter than the English on average and up to 37 % on a segment, the advance added up clause after clause: the dub ran 63 s ahead, then stopped, and the original was heard alone for 53 s and again for 60 s. A glued clip now never starts more than 3 s before its segment's original start; the log line says how many were held back. What remains by design: the dub cannot fill a segment it reads faster than the speaker, so a stretch of 4–10 s of original alone follows a segment the French finishes early. Check a dub with the measuring recipe (cross-correlate each `tts_normalized/seg_NNNN.wav` with `mixed_audio.wav`, as `monter.situer_clip` does; search 150 s back, the default 60 s window missed the drifted clips).

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

# enpleineconscience.ch podcast page (opens http://127.0.0.1:5007, or /epc in gui.py)
python epc.py causerie.mp4

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
