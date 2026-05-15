# bronzo-tts — Text-to-Speech with Personality

## What is bronzo-tts

A CLI tool that converts text to speech using Microsoft Edge TTS (free), with a distinctive voice optimized for Italian.

It's the voice of Bronzo — the black cat who lives on a server. It also powers **Radio Bronzo**, the daily editorial podcast.

## Who it's for

- **Content creators**: generate voiceovers without a microphone
- **Podcasters**: produce daily audio content from text
- **Bot builders**: give voice to your AI agents
- **Anyone who wants Bronzo to speak**: Telegram voice notes, memos, narrations

## What it does

- ✅ Edge TTS — free, no API key needed
- ✅ Voice: `it-IT-GiuseppeMultilingualNeural` (+13% rate, -18Hz pitch, +50% volume)
- ✅ Formats: MP3, OGG, WAV
- ✅ Audio normalization (loudnorm -14 LUFS), silence trimming
- ✅ Smart Italian pronunciation rules (acronyms, foreign words, abbreviations)
- ✅ Telegram integration — send voice notes directly
- ✅ Radio Bronzo — auto-generated daily editorial
- ✅ Configurable (per-user JSON config, merge with defaults)

## Quick setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

**System requirements**: ffmpeg

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

### 2. First voice test

```bash
python bronzo_tts.py say "Il Daje sia con te!"
```

### 3. Listen

Voice will play automatically (defaults to MP3). Ready to go.

## Commands

| Command | Description |
|---------|-------------|
| `say <text>` | Generate audio from text |
| `say --file <path>` | Generate audio from a text file |
| `voices` | List available Edge TTS voices |
| `test` | Generate a test sample with current settings |
| `duration <text>` | Estimate audio duration without generating |
| `config show` | Show current configuration |
| `config set <k>=<v>` | Set a config value (e.g. `pitch=-20Hz`, `format=ogg`) |
| `config reset` | Reset to factory defaults |
| `radio --ep N` | Generate a Radio Bronzo episode from text file |

## Examples

```bash
# Quick speech
python bronzo_tts.py say "Ciao, tutto bene?"

# From file
python bronzo_tts.py say --file message.txt --format ogg

# Send as voice note on Telegram
python bronzo_tts.py say --file radio.txt --telegram eugenio --caption "🎙️ Radio Bronzo"

# Change voice
python bronzo_tts.py config set rate=+20%
python bronzo_tts.py config set pitch=-25Hz
```

## Dizione — Italian Pronunciation Rules

bronzo-tts applies smart rules for Italian pronunciation:

- Acronyms (API, CEO) kept as written; HTML, PDF, GPS spelled letter by letter
- `dott.` → "dottòr", `ecc.` → "eccetera"
- YouTube → "Iutub", download → "daunlòd"
- `€480` → "480 euro"
- Custom rules via `--dizione <json-file>`

## Output

Audio files are saved in the configured output directory (default: `data/tts_output/`).

---

**Version**: 1.9.0 | **Created by**: Officina Bronzo | **Skill**: `bronzo-tts`
