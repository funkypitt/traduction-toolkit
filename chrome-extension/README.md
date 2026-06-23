# Extension Chrome « Traduction »

Un **panneau latéral** pour Chrome/Brave/Edge/Chromium qui pilote la boîte à outils
depuis le navigateur : sur une vidéo YouTube/X, tu choisis un script
(`traduire`, `doubler`, `clipper`, `resumer`…) et il télécharge la vidéo puis lance
le traitement **en local**, avec suivi de progression dans le panneau.

```
chrome-extension/
├── extension/      # l'extension à charger dans Chrome (« Load unpacked »)
│   ├── manifest.json, background.js, side_panel.{html,css,js}, icon.png
├── daemon/
│   └── traduction-daemon.py   # pont HTTP local (127.0.0.1:47318) → lance les scripts
└── install.sh      # installe le daemon en service systemd + prépare l'extension
```

## Comment ça marche

L'extension ne fait **aucun appel externe** : elle parle uniquement à un **daemon
local** (`127.0.0.1:47318`) installé sur ta machine. Le daemon :
- télécharge la vidéo de l'onglet courant avec `yt-dlp` ;
- lance le script choisi du toolkit dessus (LLM local par défaut) ;
- **sérialise les tâches GPU** (une à la fois) et renvoie la progression au panneau.

## Installation

Prérequis : avoir installé la boîte à outils (voir le README principal / `install.sh`).
Ensuite :

```bash
cd chrome-extension
./install.sh
```

Le script installe le daemon en **service systemd utilisateur** (démarrage auto,
sans sudo, chemins détectés automatiquement) puis affiche les étapes pour charger
l'extension dans Chrome :

1. `chrome://extensions` → activer **Mode développeur**
2. **Charger l'extension non empaquetée** → choisir `~/traduction-extension`
3. Sur une vidéo YouTube/X, ouvrir le panneau **Traduction**, choisir un script, lancer.

## Clés API (optionnel)

Les services systemd ne lisent pas `~/.bashrc`. Pour donner les clés au daemon,
crée `~/.config/traduction-daemon.env` (mode 600) :

```
ANTHROPIC_API_KEY=sk-ant-...
HF_TOKEN=hf_...
```
puis : `systemctl --user restart traduction-daemon`.
Sans clé Claude, tout tourne en **local** ; `HF_TOKEN` reste requis pour le doublage.

## Dépannage

| Souci | Piste |
|---|---|
| Panneau « daemon injoignable » | `systemctl --user status traduction-daemon` ; logs : `journalctl --user -u traduction-daemon -e` |
| Aucun script listé | le daemon ne trouve pas les scripts → vérifier `TRADUCTION_SCRIPTS_DIR` dans le service |
| Doublage échoue | `HF_TOKEN` manquant (diarisation) dans `~/.config/traduction-daemon.env` |
