#!/usr/bin/env python3
"""
bronzo-tts -- Text-to-speech with Bronzo's voice.

Commands:
  say <text>            Generate audio from text
  say --file <path>     Generate audio from a text file
  radio --ep N         Generate a Radio Bronzo episode
  voices                List available voices
  test                  Generate a test sample with current settings
  duration <text>       Estimate audio duration without generating
  config show           Show current configuration
  config set <k>=<v>    Set a config value (e.g. pitch=-20Hz, format=ogg)
  config reset          Reset configuration to factory defaults

Options:
  --format ogg|mp3|wav  Output format (default: mp3)
  --max-chars N         Max characters per chunk (default: 3000)
  --normalize           Normalize volume to -14 LUFS (loudnorm)
  --trim                Trim silence from start and end
  --dizione <file>      Custom dizione rules JSON (abbreviations, acronyms, foreign_words)
  --telegram <contact>  Send as voice note to Telegram contact (forces OGG)
  --caption "..."       Caption for Telegram voice note (requires --telegram)
  --no-config           Bypass config file and use built-in defaults
"""

__version__ = "1.9.0"

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

# --- Shared utils ---
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared import (
    json_output, json_error, CONTACTS, CONFIG_DIR, load_env,
)

PROFILES_PATH = CONFIG_DIR / "speaker_profiles.json"

try:
    import edge_tts as _edge_tts
except ImportError:
    _edge_tts = None

# Cache
CACHE_DIR = Path.home() / ".bronzo_tts" / "tmp" / "bronzo_tts_cache"

# --- Config ---
CONFIG_PATH = CONFIG_DIR / "bronzo-tts.json"

FACTORY_DEFAULTS = {
    "voice": "it-IT-GiuseppeMultilingualNeural",
    "rate": "+13%",
    "pitch": "-18Hz",
    "volume": "+50%",
    "format": "ogg",
    "max_chars": 3000,
    "radio_intro": "Radio Bronzo, episodio {ep}. {title}.",
    "radio_outro": "Questo era Radio Bronzo, episodio {ep}. Alla prossima.",
}


def _cache_key(text: str, voice: str, rate: str, pitch: str, volume: str,
               output_format: str, normalize: bool, trim: bool, pad_ms: int = 0) -> str:
    """MD5 hash of all generation parameters — used as cache filename (without extension)."""
    raw = "|".join([text, voice, rate, pitch, volume, output_format, str(normalize), str(trim), str(pad_ms)])
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str, fmt: str) -> Path | None:
    """Return cached audio path if exists, else None."""
    cached = CACHE_DIR / f"{key}.{fmt}"
    return cached if cached.exists() else None


def _cache_put(key: str, src: str, fmt: str) -> Path:
    """Copy src into cache and return cache path."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dst = CACHE_DIR / f"{key}.{fmt}"
    shutil.copy2(src, str(dst))
    return dst


def _cache_clear() -> int:
    """Remove all cached files. Returns number of files removed."""
    if not CACHE_DIR.exists():
        return 0
    count = 0
    for f in CACHE_DIR.iterdir():
        if f.is_file():
            f.unlink()
            count += 1
    return count


def _cache_info() -> dict:
    """Return cache stats (count, size bytes, path)."""
    if not CACHE_DIR.exists():
        return {"count": 0, "size_bytes": 0, "path": str(CACHE_DIR)}
    count = 0
    size = 0
    for f in CACHE_DIR.iterdir():
        if f.is_file():
            count += 1
            size += f.stat().st_size
    return {"count": count, "size_bytes": size, "path": str(CACHE_DIR)}


def load_config(no_config: bool = False) -> dict:
    """Load config from file, merged with factory defaults.
    Returns a dict with all config keys.
    """
    config = FACTORY_DEFAULTS.copy()
    if no_config:
        return config
    if CONFIG_PATH.exists():
        try:
            file_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # Only accept known keys
            for key in file_config:
                if key in config:
                    config[key] = file_config[key]
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config: dict):
    """Save config dict to file. Creates directory if needed."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _profile_to_tts_params(speaker_id: str) -> dict:
    """Load speaker profile and map metrics to TTS parameters (rate, pitch, volume).
    Returns empty dict if profile not found or metrics missing.
    """
    if not PROFILES_PATH.exists():
        return {}
    try:
        profs = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    pf = profs.get("profiles", {}).get(speaker_id)
    if not pf or not pf.get("metrics"):
        return {}

    m = pf["metrics"]
    params = {}

    # speech_rate_wpm → rate
    wpm = m.get("speech_rate_wpm", 140)
    if wpm >= 160:
        params["rate"] = "+25%"
    elif wpm >= 145:
        params["rate"] = "+15%"
    elif wpm >= 120:
        params["rate"] = "+8%"
    elif wpm >= 100:
        params["rate"] = "+0%"
    else:
        params["rate"] = "-10%"

    # dominance_pct → volume (more dominant = louder)
    dom = m.get("dominance_pct", 40)
    if dom >= 55:
        params["volume"] = "+70%"
    elif dom >= 40:
        params["volume"] = "+50%"
    elif dom >= 25:
        params["volume"] = "+40%"
    else:
        params["volume"] = "+30%"

    # avg_turn_words → pitch (long monologues = lower pitch)
    avg_words = m.get("avg_turn_words", 30)
    if avg_words >= 60:
        params["pitch"] = "-30Hz"
    elif avg_words >= 25:
        params["pitch"] = "-18Hz"
    elif avg_words >= 15:
        params["pitch"] = "-12Hz"
    else:
        params["pitch"] = "-5Hz"

    return params


# --- Dizione rules ---

# Built-in default dizione rules
DEFAULT_DIZIONE = {
    "abbreviations": {
        "es.": "per esempio",
        "ecc.": "eccetera",
        "vs.": "versus",
        "etc.": "eccetera",
        "dott.": "dottòr",
        "dr.": "dottore",
        "sig.": "signor",
        "sig.ra": "signora",
        "avv.": "avvocato",
        "prof.": "professor",
        "ing.": "ingegnere",
        "gent.mo": "gentilissimo",
        "gent.ma": "gentilissima",
        "pag.": "pagina",
        "n.": "numero",
        "nn.": "numeri",
        "a.c.": "anno corrente",
        "c.a.": "curriculum vitae",
        "p.v.": "punto vendita",
        "c.f.": "codice fiscale",
        "p.iva": "partita iva",
        "rif.": "riferimento",
        "tel.": "telefono",
        "e-mail": "email",
        "d.": "decreto",
        "art.": "articolo",
        "c.m.": "corrente mese",
        "gg.": "giorni",
        "mt": "metri",
        "km": "chilometri",
        "cm": "centimetri",
        "kg": "chilogrammi",
        "ml": "millilitri",
        "lt": "litri",
    },
    "acronyms": {
        "URL": "U R L",
        "HTML": "H T M L",
        "HTTPS": "H T T P S",
        "HTTP": "H T T P",
        "USB": "U S B",
        "PDF": "P D F",
        "GPS": "G P S",
        "SMS": "S M S",
        "NATO": "N A T O",
    },
    "foreign_words": {
        "YouTube": "Iutub",
        "youtube": "iutub",
        "upload": "appload",
        "download": "daunlòd",
        "pipeline": "pàipilain",
        "framework": "frèimuorch",
        "repository": "ripositori",
        "commit": "commìt",
        "branch": "brànci",
        "deploy": "diplòi",
        "server": "sèrver",
        "container": "contèiner",
        "docker": "dòcker",
        "kubernetes": "kubernetis",
    },
}

DIZIONE_PATH = CONFIG_DIR / "bronzo-tts-dizione.json"


def load_dizione_rules(custom_path: str = None) -> dict:
    """Load dizione rules: built-in defaults + file overrides + custom overrides.
    Returns a dict with keys: abbreviations, acronyms, foreign_words.
    File and custom path are optional — no error if missing.
    """
    rules = {
        "abbreviations": dict(DEFAULT_DIZIONE["abbreviations"]),
        "acronyms": dict(DEFAULT_DIZIONE["acronyms"]),
        "foreign_words": dict(DEFAULT_DIZIONE["foreign_words"]),
    }

    # Merge from config file (medium priority)
    if DIZIONE_PATH.exists():
        try:
            file_rules = json.loads(DIZIONE_PATH.read_text(encoding="utf-8"))
            for category in ("abbreviations", "acronyms", "foreign_words"):
                if category in file_rules:
                    rules[category].update(file_rules[category])
        except (json.JSONDecodeError, OSError):
            pass

    # Merge from --dizione flag (highest priority)
    if custom_path:
        custom = Path(custom_path)
        if custom.exists():
            try:
                custom_rules = json.loads(custom.read_text(encoding="utf-8"))
                for category in ("abbreviations", "acronyms", "foreign_words"):
                    if category in custom_rules:
                        rules[category].update(custom_rules[category])
            except (json.JSONDecodeError, OSError):
                json_error(f"Invalid JSON in dizione file: {custom_path}")

    return rules


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting symbols from text before TTS.
    Preserves the content, strips formatting wrappers.
    """
    result = text

    # 1. Remove fenced code blocks (```...```) with their content
    result = re.sub(r'```[\s\S]*?```', '', result)

    # 2. Remove inline code (`...`)
    result = re.sub(r'`([^`]+)`', r'\1', result)

    # 3. Remove HTML tags
    result = re.sub(r'<[^>]+>', '', result)

    # 4. Convert images ![alt](url) → alt text
    result = re.sub(r'!\[([^\]]*)\]\([^)]+\)', r'\1', result)

    # 5. Convert links [text](url) → text
    result = re.sub(r'\[([^\]]*)\]\([^)]+\)', r'\1', result)

    # 6. Strip bold/italic wrappers (**text**, __text__, *text*, _text_, ~~text~~)
    # Bold/italic with double markers first (to avoid partial matches)
    result = re.sub(r'\*\*(.+?)\*\*', r'\1', result)
    result = re.sub(r'__(.+?)__', r'\1', result)
    # Single marker: asterisk not preceded by word char, not followed by another asterisk
    # Closing asterisk: not followed by a word char (typical for inline markdown)
    result = re.sub(r'(?<!\w)\*(?!\*)(.+?)\*(?![\w*])', r'\1', result)
    result = re.sub(r'(?<!\w)_(?!_)(.+?)_(?![\w_])', r'\1', result)
    # Strikethrough
    result = re.sub(r'~~(.+?)~~', r'\1', result)

    # 7. Remove heading markers (#) at line start
    result = re.sub(r'^#{1,6}\s+', '', result, flags=re.MULTILINE)

    # 8. Remove bullet list markers (-, *, +) at line start
    result = re.sub(r'^[\s]*[-*+]\s+', '', result, flags=re.MULTILINE)

    # 9. Remove numbered list markers (1., 2., etc.) at line start
    result = re.sub(r'^[\s]*\d+[.)]\s+', '', result, flags=re.MULTILINE)

    # 10. Remove blockquote markers (>) at line start
    result = re.sub(r'^[\s]*>\s?', '', result, flags=re.MULTILINE)

    # 11. Remove horizontal rules (---, ***, ___, ===) on their own line
    result = re.sub(r'^[\s]*[-*_=]{3,}[\s]*$', '', result, flags=re.MULTILINE)

    # 12. Remove table separators (|---| pattern) and table pipes inline
    result = re.sub(r'^[\s]*\|[-|\s]+\|[\s]*$', '', result, flags=re.MULTILINE)
    result = re.sub(r'\|', ' ', result)

    # 13. Remove structural [TAGS] (e.g. [APERTURA], [NOTIZIE — ...], [RIFLESSIONE], [CHIUSURA])
    result = re.sub(r'^\[[A-ZÀ-Ü][^\]]*\]\s*$', '', result, flags=re.MULTILINE | re.IGNORECASE)

    # 14. Remove standalone emoji / broadcast symbols at line starts
    result = re.sub(r'^[\U0001F000-\U0001FAFF\u2600-\u27FF\u2300-\u23FF\uFE00-\uFEFF]+\s*', '', result, flags=re.MULTILINE)

    # 15. Collapse multiple blank lines into one
    result = re.sub(r'\n{3,}', '\n\n', result)

    # 16. Final trim
    result = result.strip()

    return result


def _dedup_consecutive_words(text: str) -> str:
    """Remove consecutive duplicate words (3+ occurrences reduced to 1).

    Handles:
      - "Un Un gatto" → "Un gatto"
      - "della della" → "della"
      - "123 123 test" → "123 test"

    Does NOT deduplicate:
      - single-letter words ("a a", "e e") — common in Italian
      - words separated by punctuation ("Ciao. Ciao")
    """
    # Pattern: boundary, capture word (2+ word chars), then same word repeated
    # Separated by whitespace only (not punctuation)
    deduped = re.sub(
        r'\b((?:\w){2,})\s+(?=\1(?:\s|$))',
        '',
        text,
        flags=re.UNICODE,
    )
    # Run twice to handle triplets: "la la la" → first pass → "la la" → second → "la"
    deduped = re.sub(
        r'\b((?:\w){2,})\s+(?=\1(?:\s|$))',
        '',
        deduped,
        flags=re.UNICODE,
    )
    return deduped


def preprocess_text(text: str, dizione_rules: dict = None) -> str:
    """Apply dizione rules to text before TTS generation.
    dizione_rules: dict with keys 'abbreviations', 'acronyms', 'foreign_words'.
    First strips markdown formatting, then applies dizione rules.
    """
    if dizione_rules is None:
        dizione_rules = load_dizione_rules()

    abbrevs = dizione_rules.get("abbreviations", {})
    acronyms = dizione_rules.get("acronyms", {})
    foreign_words = dizione_rules.get("foreign_words", {})

    result = text

    # Step 0: Strip markdown formatting (always, no flag needed)
    result = _strip_markdown(result)

    # Step 1: Dedup consecutive duplicate words ("Un Un gatto" → "Un gatto")
    result = _dedup_consecutive_words(result)

    # Step 2: Handle "..." → pause
    result = result.replace("...", ", ,")

    # Step 3: Lowercase isolated capital letters ("Fase A" → "Fase a")
    # Edge TTS reads single capital letters in a foreign accent; lowercase forces Italian.
    # MUST run BEFORE acronym expansion (below), otherwise expanded acronym letters
    # like "A I" would be lowercased to "a i".
    result = re.sub(r'(?<=\s)([A-Z])(?=\s|[\.!?,\-–—:;]|$)', lambda m: m.group(1).lower(), result)

    # Step 4: Handle ALL CAPS words (2+ letters) — only replace known acronyms, rest as-is
    def handle_caps(match):
        word = match.group(0)
        if word in acronyms:
            return acronyms[word]
        return word  # let edge-tts handle pronunciation

    result = re.sub(r'\b[A-Z]{2,}\b', handle_caps, result)

    # Step 4b: Handle ALL CAPS after apostrophe — catches "L'AI", "dell'AI", etc.
    def handle_apos_caps(match):
        word = match.group(0)
        if word in acronyms:
            return acronyms[word]
        return word

    result = re.sub(r"(?<=['’])[A-Z]{2,}\b", handle_apos_caps, result)

    # Step 4c: Strip apostrophe from "L'" + acronym → "elle AI" → then acronym expansion
    # edge-tts reads "L'A I" as "la i"; we need "elle A I"
    result = re.sub(r"\bL['’](?=[A-Z])", "elle ", result)
    result = re.sub(r"\bl['’](?=[A-Z])", "elle ", result)

    # Step 4d: Strip apostrophe from other contractions before acronyms
    # "dell'AI" → "dell AI", "all'AI" → "all AI"
    result = re.sub(r"(['’])(?=[A-Z]{2,})", " ", result)

    # 3. Expand abbreviations — word boundaries ONLY, case-insensitive
    for abbr in sorted(abbrevs.keys(), key=len, reverse=True):
        # Always require word boundary at START to avoid matching inside words
        # For dotted abbreviations, the dot itself handles the end boundary
        escaped = re.escape(abbr)
        pattern = re.compile(r'\b' + escaped, re.IGNORECASE)
        result = pattern.sub(abbrevs[abbr], result)

    # 4. Italianize foreign words (case-preserving, word boundaries)
    for foreign, italian in sorted(foreign_words.items(), key=lambda x: len(x[0]), reverse=True):
        escaped = re.escape(foreign)
        # Case-preserving replacement
        def replace_case(match, it=italian):
            orig = match.group(0)
            if orig[0].isupper():
                return it.capitalize()
            return it.lower()
        pattern = re.compile(r'\b' + escaped + r'\b', re.IGNORECASE)
        result = pattern.sub(replace_case, result)

    # 5. Format currency: €480 → "480 euro", €1.250,50 → "1250 euro e 50 centesimi"
    def format_currency(match):
        num = match.group(1)
        # Strip trailing period (sentence punctuation, not part of number)
        if num.endswith('.'):
            num = num[:-1]
            trailing = '.'
        else:
            trailing = ''
        # Remove thousands separators (dots), keep decimal comma
        num_clean = num.replace('.', '')
        if ',' in num_clean:
            parts = num_clean.split(',')
            return f"{parts[0]} euro e {parts[1]} centesimi{trailing}"
        return f"{num_clean} euro{trailing}"
    result = re.sub(r'€([\d.,]+)', format_currency, result)

    # 7. Break "YouTube Ha" → "YouTube, ha" (prevent H aspiration)
    result = re.sub(r'\b(YouTube)\s+(ha|ho|hei|hai|hanno|ho)\b', r'\1, \2', result, flags=re.IGNORECASE)

    # 7. Clean up multiple spaces
    result = re.sub(r'\s+', ' ', result).strip()

    return result


def split_text(text: str, max_chars: int = 3000) -> list:
    """Split long text into chunks at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    while len(text) > max_chars:
        # Try to split at sentence/paragraph boundary within the first max_chars
        chunk = text[:max_chars]
        split_at = -1
        # Priority 1: paragraph boundaries (best audio flow)
        for sep in ['\n\n', '\n']:
            pos = chunk.rfind(sep)
            if pos > split_at:
                split_at = pos + len(sep) - 1  # last char of separator
        # Priority 2: sentence-ending punctuation
        if split_at < 0:
            for sep in ['. ', '! ', '? ', '.\n', '!\n', '?\n', '.\r', '!\r', '?\r']:
                pos = chunk.rfind(sep)
                if pos > split_at:
                    split_at = pos + len(sep) - 1  # position of punctuation char

        # Fallback: last comma/semicolon
        if split_at < max_chars // 2:
            for sep in [', ', '; ', ': ']:
                pos = chunk.rfind(sep)
                if pos > split_at:
                    split_at = pos + len(sep) - 2  # position of comma

        # Last resort: hard split at max_chars word boundary
        if split_at < max_chars // 2:
            split_at = chunk.rfind(' ', 0, max_chars)
            if split_at < 1:
                split_at = max_chars

        chunks.append(text[:split_at + 1].strip())
        text = text[split_at + 1:].strip()

    if text.strip():
        chunks.append(text.strip())

    return chunks


def estimate_duration(text: str, char_per_sec: int = 15) -> int:
    """Estimate audio duration in seconds based on character count."""
    chars = len(text.strip())
    seconds = max(1, (chars + char_per_sec - 1) // char_per_sec)
    return seconds


# --- Helper functions for Phase A ---

TMP_DIR = Path.home() / ".bronzo_tts" / "tmp"
ASYNC_JOBS_DIR = TMP_DIR / "bronzo_tts_jobs"


def _auto_output_path(source_path: Path, output_format: str) -> Path:
    """Generate output path next to source with same basename + extension."""
    return source_path.with_suffix(f".{output_format}")


def _hash_output_path(text: str, output_format: str) -> Path:
    """Generate hash-based temp path in ~/.bronzo_tts/tmp/."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
    return TMP_DIR / f"bronzo_tts_{text_hash}.{output_format}"


def _radio_default_path(ep: int, output_format: str) -> Path:
    """Generate default output path for a Radio Bronzo episode."""
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    return TMP_DIR / f"radio_bronzo_ep{ep}.{output_format}"


# --- Phase C: Advanced Audio helpers ---

# Silence gap duration between chunks (in seconds)
CHUNK_GAP_DURATION = 0.3


def _make_silence_wav(duration: float, sample_rate: int = 24000, output_path: str = None) -> str:
    """Generate a WAV silence file of given duration (seconds).
    Returns path to the generated file.
    """
    work_dir = Path(output_path).parent if output_path else TMP_DIR
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    silence_path = work_dir / f"bronzo_silence_{uuid.uuid4().hex}.wav"
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=mono",
        "-t", str(duration), "-acodec", "pcm_s16le", str(silence_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True)
    except subprocess.CalledProcessError as e:
        json_error(f"Failed to generate silence: {e.stderr.strip()}")
    except FileNotFoundError:
        json_error("ffmpeg not found. Install ffmpeg for audio processing.")
    return str(silence_path)


def _ffmpeg_codec_for_ext(ext: str) -> str | None:
    """Return ffmpeg audio codec for a given file extension."""
    codec_map = {
        ".mp3": "libmp3lame",
        ".ogg": "libopus",
        ".wav": "pcm_s16le",
    }
    return codec_map.get(ext.lower())


def _apply_trim(filepath: str) -> str:
    """Trim silence from start and end of audio file. Modifies in place.
    Uses ffmpeg silenceremove filter with -50dB threshold.
    Returns the same filepath.
    """
    path = Path(filepath)
    if not path.exists():
        return filepath
    work_dir = path.parent
    ext = path.suffix
    codec = _ffmpeg_codec_for_ext(ext)

    # Temp intermediate files
    temp1 = work_dir / f"bronzo_trim1_{uuid.uuid4().hex}{ext}"
    temp2 = work_dir / f"bronzo_trim2_{uuid.uuid4().hex}{ext}"

    try:
        # Step 1: trim start silence
        cmd1 = [
            "ffmpeg", "-y", "-i", str(filepath),
            "-af", "silenceremove=start_periods=1:start_threshold=-50dB:start_duration=0.1",
        ]
        if codec:
            cmd1 += ["-acodec", codec]
        cmd1 += [str(temp1)]
        subprocess.run(cmd1, capture_output=True, text=True, timeout=60, check=True)

        # Step 2: reverse → trim (now removes end) → reverse back
        cmd2 = [
            "ffmpeg", "-y", "-i", str(temp1),
            "-af", "areverse,silenceremove=start_periods=1:start_threshold=-50dB:start_duration=0.1,areverse",
        ]
        if codec:
            cmd2 += ["-acodec", codec]
        cmd2 += [str(temp2)]
        subprocess.run(cmd2, capture_output=True, text=True, timeout=60, check=True)

        # Replace original with trimmed version
        shutil.move(str(temp2), str(filepath))

    except subprocess.CalledProcessError as e:
        json_error(f"Audio trim failed: {e.stderr.strip()}")
    finally:
        temp1.unlink(missing_ok=True)
        temp2.unlink(missing_ok=True)

    return filepath


def _apply_normalize(filepath: str) -> str:
    """Apply loudnorm normalization (-14 LUFS, LRA 1, TP -1). Modifies in place.
    Returns the same filepath.
    """
    path = Path(filepath)
    if not path.exists():
        return filepath
    work_dir = path.parent
    ext = path.suffix
    codec = _ffmpeg_codec_for_ext(ext)

    temp = work_dir / f"bronzo_norm_{uuid.uuid4().hex}{ext}"

    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(filepath),
            "-af", "loudnorm=I=-14:LRA=1:TP=-1",
        ]
        if codec:
            cmd += ["-acodec", codec]
        cmd += [str(temp)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True)

        shutil.move(str(temp), str(filepath))

    except subprocess.CalledProcessError as e:
        json_error(f"Audio normalization failed: {e.stderr.strip()}")
    except FileNotFoundError:
        json_error("ffmpeg not found. Install ffmpeg for audio processing.")
    finally:
        temp.unlink(missing_ok=True)

    return filepath


def _apply_pad(filepath: str, pad_ms: int = 200) -> str:
    """Add silence padding at start and end of audio file. Modifies in place.
    Uses ffmpeg adelay (start) + apad (end).
    Returns the same filepath.
    """
    path = Path(filepath)
    if not path.exists() or pad_ms <= 0:
        return filepath

    work_dir = path.parent
    ext = path.suffix
    codec = _ffmpeg_codec_for_ext(ext)
    temp = work_dir / f"bronzo_pad_{uuid.uuid4().hex}{ext}"

    pad_sec = pad_ms / 1000.0

    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(filepath),
            "-af", f"adelay={pad_ms},apad=pad_dur={pad_sec:.3f}",
        ]
        if codec:
            cmd += ["-acodec", codec]
        cmd += [str(temp)]
        subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=True)

        shutil.move(str(temp), str(filepath))

    except subprocess.CalledProcessError as e:
        json_error(f"Audio padding failed: {e.stderr.strip()}")
    except FileNotFoundError:
        json_error("ffmpeg not found. Install ffmpeg for audio processing.")
    finally:
        temp.unlink(missing_ok=True)

    return filepath


def _build_concat_list_with_gaps(chunk_files: list, output_path: str) -> str:
    """Build an ffmpeg concat file with 300ms silence gaps between chunks.
    Returns the path to the generated concat file.
    """
    work_dir = Path(output_path).parent
    concat_list = work_dir / f"bronzo_concat_{uuid.uuid4().hex}.txt"
    silence_wav = None

    try:
        if len(chunk_files) > 1:
            silence_wav = _make_silence_wav(CHUNK_GAP_DURATION, output_path=str(output_path))

        with open(concat_list, "w") as f:
            for i, chunk in enumerate(chunk_files):
                f.write(f"file '{chunk}'\n")
                if i < len(chunk_files) - 1 and silence_wav:
                    f.write(f"file '{silence_wav}'\n")

    finally:
        # We keep silence_wav alive until the concat is done — caller cleans up
        pass

    return str(concat_list), silence_wav


def _cleanup_concat_gaps(silence_wav: str | None):
    """Clean up silence gap WAV file generated by _build_concat_list_with_gaps."""
    if silence_wav:
        Path(silence_wav).unlink(missing_ok=True)


# --- End Phase C helpers ---

# --- Phase E: Telegram Integration ---

TELEGRAM_CONTACTS = {
    "eugenio": "XXXXXXXX",
    "valerio": "XXXXXXXX",
    "mario": "XXXXXXXX",
    "alice": "XXXXXXXX",
    "bruno": "XXXXXXXX",
}


def _resolve_telegram_contact(contact: str) -> str:
    """Resolve a contact name to a Telegram chat ID."""
    key = contact.lower().strip()
    if key in TELEGRAM_CONTACTS:
        return TELEGRAM_CONTACTS[key]
    if contact.lstrip("-").isdigit():
        return contact
    json_error(
        f"Contatto Telegram sconosciuto: '{contact}'. "
        f"Conosciuti: {', '.join(TELEGRAM_CONTACTS.keys())}"
    )


def _send_via_telegram(filepath: str, contact: str, caption: str = "") -> dict:
    """Send an audio file as a Telegram voice note.
    Calls the telegram CLI tool.
    Returns a dict with send status.
    """
    chat_id = _resolve_telegram_contact(contact)
    cmd = ["telegram", "send", "--voice", filepath, "--to", chat_id]
    if caption:
        cmd += ["--text", caption]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return {"status": "error", "error": result.stderr.strip()}
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return {"status": "sent", "raw": result.stdout.strip()}
    except FileNotFoundError:
        return {"status": "error", "error": "telegram CLI non trovato"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "telegram send timeout dopo 60s"}


# --- End Phase E helpers ---


def _build_radio_text(
    text: str,
    ep: int,
    title: str,
    intro_text: str | None = None,
    outro_text: str | None = None,
    no_intro: bool = False,
    no_outro: bool = False,
) -> str:
    """Build full radio episode text with intro/outro.

    Uses template variables {ep} (episode number) and {title} (episode title)
    in both intro and outro.
    """
    parts = []

    if not no_intro:
        intro = (intro_text or FACTORY_DEFAULTS["radio_intro"]).format(
            ep=ep, title=title or "Senza Titolo"
        )
        parts.append(intro)

    # Strip title from body start to avoid repetition (intro already says it)
    if title:
        # First, strip any emoji-header line (e.g. "🎙️ Radio Bronzo Ep.05 — Titolo")
        text = re.sub(
            r'^[^\w\s]*\s*Radio\s+Bronzo\s+Ep\.?\s*\d+\s*[-—–]\s*[^\n]+\n?',
            '', text, flags=re.IGNORECASE,
        )
        # Match: optional (title + punctuation + whitespace) at body start
        title_stripped = re.sub(
            r'^\s*' + re.escape(title) + r'[\.!?:;\-–—]?\s*',
            '',
            text,
            count=1,
            flags=re.IGNORECASE,
        )
        if title_stripped != text:
            text = title_stripped

    # Remove closing phrases from body to avoid outro duplication.
    # The radio command adds them automatically via intro/outro_text,
    # so they should never also appear in the body text.
    if text:
        # 0) Strip structural [TAGS] from body before processing (e.g. [APERTURA], [NOTIZIE], [CHIUSURA])
        text = re.sub(r'^\[[A-ZÀ-Ü][^\]]*\]\s*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
        # Collapse blank lines created by tag removal
        text = re.sub(r'\n{3,}', '\n\n', text)
        # 0.5) Strip "Radio Bronzo, Episodio N." self-intros (duplicates what the intro already provides)
        text = re.sub(r'Radio\s+Bronzo,?\s+[Ee]pisodio\s+\d+[\.!]?\s*', '', text, flags=re.IGNORECASE)
        # 1) "Chiudo così/qua." — global removal (Italian closing phrase)
        text = re.sub(r'Chiudo\s+(?:così|qua)[\.!]?\s*', '', text, flags=re.IGNORECASE)
        # 2) "Questo era Radio Bronzo, episodio N." — global removal
        text = re.sub(
            r'Questo\s+era\s+Radio\s+Bronzo[\.!]?(?:\s+episodio\s+\d+[\.!]?)?\s*',
            '', text, flags=re.IGNORECASE,
        )
        # 3) "Alla prossima." — global removal
        text = re.sub(r'Alla\s+prossima[\.!]?\s*', '', text, flags=re.IGNORECASE)
        # 4) Clean orphaned "episodio N" fragments left after Radio Bronzo removal
        text = re.sub(r"(?:,\s*)?(?:l['’]?\s*)?episodio\s+\d+[\.!]?\s*", '', text, flags=re.IGNORECASE)
        # Clean up leading/trailing whitespace and double punctuation left by removals
        text = re.sub(r'[^\S\n]{2,}', ' ', text)
        text = re.sub(r'[.,]{2,}', '.', text)
        text = text.strip()

    parts.append(text)

    if not no_outro:
        outro = (outro_text or FACTORY_DEFAULTS["radio_outro"]).format(
            ep=ep, title=title or "Senza Titolo"
        )
        parts.append(outro)

    return "\n\n".join(parts)


def _stream_generate_chunk(text: str, output_wav: str, voice: str, rate: str, pitch: str, volume: str) -> str:
    """Generate a WAV chunk using edge-tts streaming API (Python async).

    Returns the path to the generated WAV file.
    """
    if _edge_tts is None:
        json_error("edge-tts not installed. Run: pip install edge-tts")

    async def _run():
        communicate = _edge_tts.Communicate(
            text, voice,
            rate=rate,
            pitch=pitch,
            volume=volume,
        )
        with open(output_wav, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])

    try:
        asyncio.run(_run())
    except Exception as e:
        json_error(f"edge-tts stream failed: {e}")

    return output_wav


# --- Async job management ---

def _async_job_path(job_id: str) -> Path:
    """Return path to an async job state file."""
    return ASYNC_JOBS_DIR / f"{job_id}.json"


def _async_job_run(job_id: str, kwargs: dict) -> str:
    """Run generate_audio in a background subprocess.

    Writes params to a JSON file, spawns a child Python process that
    executes generate_audio and updates the state file when done.
    The parent process exits immediately — the child continues independently.

    Returns the job_id.
    """
    ASYNC_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().isoformat()

    # Initial state
    state = {
        "job_id": job_id,
        "status": "running",
        "started_at": started_at,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    _async_job_path(job_id).write_text(json.dumps(state), encoding="utf-8")

    # Save params for the worker process
    params = {
        "job_id": job_id,
        "started_at": started_at,
        "kwargs": kwargs,
    }
    params_path = ASYNC_JOBS_DIR / f"{job_id}_params.json"
    params_path.write_text(json.dumps(params), encoding="utf-8")

    # Spawn worker subprocess
    worker_script = Path(__file__).resolve().parent / "bronzo_tts_async_worker.py"
    try:
        subprocess.Popen(
            [sys.executable, str(worker_script), str(params_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent process group
        )
    except Exception as e:
        state["status"] = "failed"
        state["error"] = f"Failed to spawn worker: {e}"
        state["finished_at"] = datetime.now().isoformat()
        _async_job_path(job_id).write_text(json.dumps(state), encoding="utf-8")

    return job_id


def _async_job_status(job_id: str) -> dict:
    """Read the current state of an async job."""
    path = _async_job_path(job_id)
    if not path.exists():
        return {"job_id": job_id, "status": "not_found"}
    return json.loads(path.read_text(encoding="utf-8"))


def _async_job_wait(job_id: str, poll_interval: float = 1.0, timeout: int = 300) -> dict:
    """Poll for job completion. Returns final state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _async_job_status(job_id)
        if state["status"] in ("completed", "failed", "not_found"):
            return state
        time.sleep(poll_interval)
    return {"job_id": job_id, "status": "timeout", "error": f"Did not complete within {timeout}s"}


# --- End async job management ---


def _async_job_status(job_id: str) -> dict:
    """Read the current state of an async job."""
    path = _async_job_path(job_id)
    if not path.exists():
        return {"job_id": job_id, "status": "not_found"}
    return json.loads(path.read_text(encoding="utf-8"))


def _async_job_wait(job_id: str, poll_interval: float = 1.0, timeout: int = 300) -> dict:
    """Poll for job completion. Returns final state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _async_job_status(job_id)
        if state["status"] in ("completed", "failed", "not_found"):
            return state
        time.sleep(poll_interval)
    return {"job_id": job_id, "status": "timeout", "error": f"Did not complete within {timeout}s"}


# --- End async job management ---


def generate_audio(
    text: str,
    output_path: str = None,
    voice: str = None,
    rate: str = None,
    pitch: str = None,
    volume: str = None,
    preprocess: bool = True,
    dizione_rules: dict = None,
    output_format: str = "mp3",
    max_chars: int = 3000,
    normalize: bool = False,
    trim: bool = False,
    pad_ms: int = 0,
    use_cache: bool = True,
    stream: bool = False,
) -> str:
    """Generate audio from text using edge-tts. Returns the output file path.

    Supported formats: mp3 (default), ogg (OPUS 24kHz), wav (PCM).
    Long text is automatically split into chunks and concatenated.

    When use_cache=True (default), caches generated audio by content hash and
    skips edge-tts on cache hit for identical text+parameters.

    When stream=True, uses edge-tts Python streaming API instead of subprocess CLI.
    """

    voice = voice or FACTORY_DEFAULTS["voice"]
    rate = rate or FACTORY_DEFAULTS["rate"]
    pitch = pitch or FACTORY_DEFAULTS["pitch"]
    volume = volume or FACTORY_DEFAULTS["volume"]
    output_format = output_format.lower()

    if preprocess:
        text = preprocess_text(text, dizione_rules)

    # --- Content-based cache lookup ---
    key: str | None = None
    if use_cache:
        key = _cache_key(text, voice, rate, pitch, volume, output_format, normalize, trim, pad_ms)
        cached = _cache_get(key, output_format)
        if cached is not None:
            dst = output_path or str(Path.home() / ".bronzo_tts" / "tmp" / f"bronzo_tts_cached_{uuid.uuid4().hex}.{output_format}")
            shutil.copy2(str(cached), dst)
            return dst

    # Split text into chunks, then generate
    chunks = split_text(text, max_chars)

    if not output_path:
        suffix = f".{output_format}"
        fd, output_path = tempfile.mkstemp(suffix=suffix, prefix="bronzo_tts_")
        os.close(fd)

    if len(chunks) == 1 and output_format == "mp3":
        # Single chunk, default format — direct edge-tts
        if stream:
            # Use streaming Python API
            wav_temp = Path(output_path).parent / f"bronzo_stream_{uuid.uuid4().hex}.wav"
            _stream_generate_chunk(text, str(wav_temp), voice, rate, pitch, volume)
            # Convert WAV to MP3
            ffmpeg_cmd = [
                "ffmpeg", "-y", "-i", str(wav_temp),
                "-acodec", "libmp3lame", str(output_path),
            ]
            try:
                subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=60, check=True)
            except subprocess.CalledProcessError as e:
                json_error(f"ffmpeg stream conversion failed: {e.stderr.strip()}")
            finally:
                wav_temp.unlink(missing_ok=True)
        else:
            cmd = [
                "edge-tts",
                f"--voice={voice}",
                f"--rate={rate}",
                f"--pitch={pitch}",
                f"--volume={volume}",
                f"--text={text}",
                f"--write-media={output_path}",
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                if result.returncode != 0:
                    json_error(f"edge-tts failed: {result.stderr.strip()}")
            except FileNotFoundError:
                json_error("edge-tts not installed. Run: pip install edge-tts")
            except subprocess.TimeoutExpired:
                json_error("edge-tts timed out after 60s")

        # Post-processing for single chunk: trim → pad → normalize
        if trim:
            output_path = _apply_trim(output_path)
        if pad_ms > 0:
            output_path = _apply_pad(output_path, pad_ms)
        if normalize:
            output_path = _apply_normalize(output_path)

        # Save to cache (after post-processing)
        if use_cache:
            _cache_put(key, output_path, output_format)

        return output_path

    # Multi-chunk or non-mp3 format: generate WAV chunks, then convert
    work_dir = Path(output_path).parent
    chunk_files = []

    try:
        for i, chunk in enumerate(chunks):
            chunk_path = work_dir / f"bronzo_chunk_{uuid.uuid4().hex}_{i}.wav"
            chunk_files.append(chunk_path)

            if stream:
                _stream_generate_chunk(chunk, str(chunk_path), voice, rate, pitch, volume)
            else:
                cmd = [
                    "edge-tts",
                    f"--voice={voice}",
                    f"--rate={rate}",
                    f"--pitch={pitch}",
                    f"--volume={volume}",
                    f"--text={chunk}",
                    f"--write-media={str(chunk_path)}",
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    json_error(f"edge-tts chunk {i} failed: {result.stderr.strip()}")

        # Build concat list with 300ms gaps between chunks
        concat_list, silence_wav = _build_concat_list_with_gaps(chunk_files, output_path)

        try:
            # Concatenate chunks (with gaps)
            if output_format == "ogg":
                concat_wav = work_dir / f"bronzo_concat_{uuid.uuid4().hex}.wav"
                ffmpeg_concat = [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-acodec", "pcm_s16le", "-ar", "24000",
                    str(concat_wav),
                ]
                subprocess.run(ffmpeg_concat, capture_output=True, text=True, timeout=120)

                ffmpeg_ogg = [
                    "ffmpeg", "-y", "-i", str(concat_wav),
                    "-c:a", "libopus", "-b:a", "24k", "-ar", "24000",
                    str(output_path),
                ]
                subprocess.run(ffmpeg_ogg, capture_output=True, text=True, timeout=120)

                concat_wav.unlink(missing_ok=True)

            elif output_format == "wav":
                ffmpeg_concat = [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-acodec", "pcm_s16le",
                    str(output_path),
                ]
                subprocess.run(ffmpeg_concat, capture_output=True, text=True, timeout=120)

            else:
                # mp3 from WAV chunks
                ffmpeg_concat = [
                    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(concat_list),
                    "-acodec", "libmp3lame",
                    str(output_path),
                ]
                subprocess.run(ffmpeg_concat, capture_output=True, text=True, timeout=120)

        finally:
            Path(concat_list).unlink(missing_ok=True)
            _cleanup_concat_gaps(silence_wav)

    finally:
        for c in chunk_files:
            c.unlink(missing_ok=True)

    # Post-processing: trim → pad → normalize
    if trim:
        output_path = _apply_trim(output_path)
    if pad_ms > 0:
        output_path = _apply_pad(output_path, pad_ms)
    if normalize:
        output_path = _apply_normalize(output_path)

    # Save to cache (after post-processing)
    if use_cache and key is not None:
        _cache_put(key, output_path, output_format)

    return output_path


def list_voices(language: str = None) -> list:
    """List available voices, optionally filtered by language."""
    cmd = ["edge-tts", "--list-voices"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            json_error(f"Failed to list voices: {result.stderr.strip()}")
    except FileNotFoundError:
        json_error("edge-tts not installed. Run: pip install edge-tts")

    voices = []
    for line in result.stdout.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 3:
            name = parts[0]
            gender = parts[1]
            category = parts[2] if len(parts) > 2 else ""
            friendly = " ".join(parts[3:]) if len(parts) > 3 else ""
            voices.append({
                "name": name,
                "gender": gender,
                "category": category,
                "friendly": friendly,
            })

    if language:
        voices = [v for v in voices if v["name"].lower().startswith(language.lower())]

    return voices


# --- Command handlers ---

def cmd_say(args):
    """Handler for the 'say' subcommand."""
    cfg = load_config(args.no_config)
    fmt = args.format or cfg["format"]
    _auto_output = False

    # Profile-aware TTS: merge speaker profile params (CLI overrides > profile > config)
    if args.profile:
        pp = _profile_to_tts_params(args.profile)
        args.rate = args.rate or pp.get("rate")
        args.pitch = args.pitch or pp.get("pitch")
        args.volume = args.volume or pp.get("volume")

    # Force OGG for Telegram voice notes
    if args.telegram and fmt != "ogg":
        fmt = "ogg"

    # --- DIR MODE ---
    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            json_error(f"Directory not found: {args.dir}")

        txt_files = sorted(dir_path.glob("*.txt"))
        if not txt_files:
            json_output({
                "status": "ok",
                "dir": args.dir,
                "total": 0,
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "results": [],
            })
            return

        results = []
        for txt_file in txt_files:
            output_file = _auto_output_path(txt_file, fmt)

            # Skip if output already exists (idempotent)
            if output_file.exists():
                results.append({
                    "source": str(txt_file),
                    "output": str(output_file),
                    "status": "skipped",
                    "reason": "output already exists",
                })
                continue

            text = txt_file.read_text(encoding="utf-8").strip()
            if not text:
                results.append({
                    "source": str(txt_file),
                    "status": "skipped",
                    "reason": "empty file",
                })
                continue

            dizione_rules = load_dizione_rules(args.dizione)

            try:
                out = generate_audio(
                    text=text,
                    output_path=str(output_file),
                    voice=args.voice or cfg["voice"],
                    rate=args.rate or cfg["rate"],
                    pitch=args.pitch or cfg["pitch"],
                    volume=args.volume or cfg["volume"],
                    preprocess=not args.raw,
                    dizione_rules=dizione_rules,
                    output_format=fmt,
                    max_chars=args.max_chars or cfg["max_chars"],
                    normalize=args.normalize,
                    trim=args.trim,
                    pad_ms=args.pad,
                    use_cache=not args.no_cache,
                    stream=args.stream,
                )
                results.append({
                    "source": str(txt_file),
                    "output": out,
                    "size_bytes": os.path.getsize(out),
                    "status": "ok",
                })
            except SystemExit:
                raise
            except Exception as e:
                results.append({
                    "source": str(txt_file),
                    "status": "error",
                    "error": str(e),
                })

        json_output({
            "status": "ok",
            "dir": args.dir,
            "total": len(txt_files),
            "processed": sum(1 for r in results if r["status"] == "ok"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "errors": sum(1 for r in results if r["status"] == "error"),
            "results": results,
        })
        return

    # --- SINGLE FILE/TEXT MODE ---
    # Get text from argument or file
    if args.file:
        path = Path(args.file)
        if not path.exists():
            json_error(f"File not found: {args.file}")
        text = path.read_text(encoding="utf-8").strip()

        # Auto-naming: if no --output, save next to source
        if not args.output:
            args.output = str(_auto_output_path(path, fmt))
            _auto_output = True
        else:
            _auto_output = False
    elif args.text:
        text = " ".join(args.text)

        # For --text without --output, use hash-based temp path
        if not args.output:
            args.output = str(_hash_output_path(text, fmt))
            _auto_output = True
        else:
            _auto_output = False
    else:
        json_error("Provide text as argument, use --file, or use --dir")

    if not text:
        json_error("Empty text")

    # Load dizione rules (default config + file + custom --dizione)
    dizione_rules = load_dizione_rules(args.dizione)

    # Show preprocessed text if requested
    if args.show_processed:
        processed = preprocess_text(text, dizione_rules)
        json_output({
            "original": text,
            "preprocessed": processed,
            "changed": text != processed,
        })
        return

    # --- ASYNC MODE ---
    if args.async_:
        job_id = uuid.uuid4().hex[:12]
        _async_job_run(
            job_id,
            kwargs={
                "text": text,
                "output_path": args.output,
                "voice": args.voice or cfg["voice"],
                "rate": args.rate or cfg["rate"],
                "pitch": args.pitch or cfg["pitch"],
                "volume": args.volume or cfg["volume"],
                "preprocess": not args.raw,
                "dizione_rules": dizione_rules,
                "output_format": fmt,
                "max_chars": args.max_chars or cfg["max_chars"],
                "normalize": args.normalize,
                "trim": args.trim,
                "pad_ms": args.pad,
                "use_cache": not args.no_cache,
                "stream": args.stream,
            },
        )
        json_output({
            "status": "ok",
            "job_id": job_id,
            "mode": "async",
            "message": f"Generation started in background. Check: bronzo-tts status {job_id} or bronzo-tts wait {job_id}",
        })
        return

    # --- SYNC MODE ---
    # Generate audio — CLI flags override config file
    output_path = generate_audio(
        text=text,
        output_path=args.output,
        voice=args.voice or cfg["voice"],
        rate=args.rate or cfg["rate"],
        pitch=args.pitch or cfg["pitch"],
        volume=args.volume or cfg["volume"],
        preprocess=not args.raw,
        dizione_rules=dizione_rules,
        output_format=fmt,
        max_chars=args.max_chars or cfg["max_chars"],
        normalize=args.normalize,
        trim=args.trim,
        pad_ms=args.pad,
        use_cache=not args.no_cache,
        stream=args.stream,
    )

    file_size = os.path.getsize(output_path)

    # --- Telegram send (say) ---
    telegram_result = None
    if args.telegram:
        telegram_result = _send_via_telegram(output_path, args.telegram, args.caption or "")
        if _auto_output:
            try:
                os.unlink(output_path)
            except OSError:
                pass

    json_output({
        "status": "ok",
        "output": output_path,
        "size_bytes": file_size,
        "voice": args.voice or cfg["voice"],
        "rate": args.rate or cfg["rate"],
        "pitch": args.pitch or cfg["pitch"],
        "volume": args.volume or cfg["volume"],
        "format": fmt,
        "max_chars": args.max_chars or cfg["max_chars"],
        "normalize": args.normalize,
        "trim": args.trim,
        "mode": "sync",
        "stream": args.stream,
        "telegram": telegram_result,
        "preprocessed": not args.raw,
        "config_file": str(CONFIG_PATH) if not args.no_config and CONFIG_PATH.exists() else None,
    })


def cmd_radio(args):
    """Handler for the 'radio' subcommand — Radio Bronzo episode."""
    cfg = load_config(args.no_config)
    fmt = args.format or cfg["format"]
    _auto_output = False

    # Profile-aware TTS: merge speaker profile params (CLI overrides > profile > config)
    if args.profile:
        pp = _profile_to_tts_params(args.profile)
        args.rate = args.rate or pp.get("rate")
        args.pitch = args.pitch or pp.get("pitch")
        args.volume = args.volume or pp.get("volume")

    # Force OGG for Telegram voice notes
    if args.telegram and fmt != "ogg":
        fmt = "ogg"

    # --- DIR MODE: batch episodes from directory ---
    if args.dir:
        dir_path = Path(args.dir)
        if not dir_path.is_dir():
            json_error(f"Directory not found: {args.dir}")

        txt_files = sorted(dir_path.glob("*.txt"))
        if not txt_files:
            json_output({
                "status": "ok",
                "dir": args.dir,
                "total": 0,
                "processed": 0,
                "skipped": 0,
                "errors": 0,
                "results": [],
            })
            return

        results = []
        next_ep = args.ep
        for txt_file in txt_files:
            episode_title = txt_file.stem.replace("_", " ").replace("-", " ").title()
            output_file = TMP_DIR / f"radio_bronzo_ep{next_ep}.{fmt}"

            if output_file.exists():
                results.append({
                    "source": str(txt_file),
                    "output": str(output_file),
                    "ep": next_ep,
                    "title": episode_title,
                    "status": "skipped",
                    "reason": "output already exists",
                })
                next_ep += 1
                continue

            text = txt_file.read_text(encoding="utf-8").strip()
            if not text:
                results.append({
                    "source": str(txt_file),
                    "status": "skipped",
                    "reason": "empty file",
                    "ep": next_ep,
                })
                next_ep += 1
                continue

            # Build radio text with intro/outro
            radio_text = _build_radio_text(
                text=text,
                ep=next_ep,
                title=args.title or episode_title,
                intro_text=args.intro,
                outro_text=args.outro,
                no_intro=args.no_intro,
                no_outro=args.no_outro,
            )

            dizione_rules = load_dizione_rules(args.dizione)
            try:
                out = generate_audio(
                    text=radio_text,
                    output_path=str(output_file),
                    voice=args.voice or cfg["voice"],
                    rate=args.rate or cfg["rate"],
                    pitch=args.pitch or cfg["pitch"],
                    volume=args.volume or cfg["volume"],
                    preprocess=not args.raw,
                    dizione_rules=dizione_rules,
                    output_format=fmt,
                    max_chars=args.max_chars or cfg["max_chars"],
                    normalize=args.normalize,
                    trim=args.trim,
                    pad_ms=args.pad,
                    use_cache=not args.no_cache,
                    stream=args.stream,
                )
                results.append({
                    "source": str(txt_file),
                    "output": out,
                    "ep": next_ep,
                    "title": episode_title,
                    "size_bytes": os.path.getsize(out),
                    "status": "ok",
                })
            except SystemExit:
                raise
            except Exception as e:
                results.append({
                    "source": str(txt_file),
                    "ep": next_ep,
                    "status": "error",
                    "error": str(e),
                })
            next_ep += 1

        json_output({
            "status": "ok",
            "dir": args.dir,
            "total": len(txt_files),
            "processed": sum(1 for r in results if r["status"] == "ok"),
            "skipped": sum(1 for r in results if r["status"] == "skipped"),
            "errors": sum(1 for r in results if r["status"] == "error"),
            "results": results,
        })
        return

    # --- SINGLE EPISODE MODE ---
    source_title = "Senza Titolo"
    if args.file:
        path = Path(args.file)
        if not path.exists():
            json_error(f"File not found: {args.file}")
        text = path.read_text(encoding="utf-8").strip()
        source_title = path.stem.replace("_", " ").replace("-", " ").title()
    elif args.text:
        text = " ".join(args.text)
    else:
        json_error("Provide text as argument, use --file, or use --dir")

    if not text:
        json_error("Empty text")

    # Build radio text with intro/outro
    radio_text = _build_radio_text(
        text=text,
        ep=args.ep,
        title=args.title or source_title,
        intro_text=args.intro,
        outro_text=args.outro,
        no_intro=args.no_intro,
        no_outro=args.no_outro,
    )

    # Default output path
    if not args.output:
        args.output = str(_radio_default_path(args.ep, fmt))
        _auto_output = True
    else:
        _auto_output = False

    dizione_rules = load_dizione_rules(args.dizione)

    # --- ASYNC MODE (single episode only) ---
    if args.async_:
        job_id = uuid.uuid4().hex[:12]
        _async_job_run(
            job_id,
            kwargs={
                "text": radio_text,
                "output_path": args.output,
                "voice": args.voice or cfg["voice"],
                "rate": args.rate or cfg["rate"],
                "pitch": args.pitch or cfg["pitch"],
                "volume": args.volume or cfg["volume"],
                "preprocess": not args.raw,
                "dizione_rules": dizione_rules,
                "output_format": fmt,
                "max_chars": args.max_chars or cfg["max_chars"],
                "normalize": args.normalize,
                "trim": args.trim,
                "pad_ms": args.pad,
                "use_cache": not args.no_cache,
                "stream": args.stream,
            },
        )
        json_output({
            "status": "ok",
            "job_id": job_id,
            "mode": "async",
            "command": "radio",
            "episode": args.ep,
            "title": args.title or "Senza Titolo",
            "message": f"Radio Bronzo Ep.{args.ep} started in background. Check: bronzo-tts status {job_id}",
        })
        return

    # --- SYNC MODE ---
    output_path = generate_audio(
        text=radio_text,
        output_path=args.output,
        voice=args.voice or cfg["voice"],
        rate=args.rate or cfg["rate"],
        pitch=args.pitch or cfg["pitch"],
        volume=args.volume or cfg["volume"],
        preprocess=not args.raw,
        dizione_rules=dizione_rules,
        output_format=fmt,
        max_chars=args.max_chars or cfg["max_chars"],
        normalize=args.normalize,
        trim=args.trim,
        pad_ms=args.pad,
        use_cache=not args.no_cache,
        stream=args.stream,
    )

    file_size = os.path.getsize(output_path)

    # --- Telegram send (radio) ---
    telegram_result = None
    if args.telegram:
        telegram_result = _send_via_telegram(output_path, args.telegram, args.caption or "")
        if _auto_output:
            try:
                os.unlink(output_path)
            except OSError:
                pass

    json_output({
        "status": "ok",
        "command": "radio",
        "episode": args.ep,
        "title": args.title or "Senza Titolo",
        "output": output_path,
        "size_bytes": file_size,
        "voice": args.voice or cfg["voice"],
        "rate": args.rate or cfg["rate"],
        "pitch": args.pitch or cfg["pitch"],
        "volume": args.volume or cfg["volume"],
        "format": fmt,
        "max_chars": args.max_chars or cfg["max_chars"],
        "normalize": args.normalize,
        "trim": args.trim,
        "mode": "sync",
        "stream": args.stream,
        "telegram": telegram_result,
        "intro": None if args.no_intro else (args.intro or cfg.get("radio_intro")),
        "outro": None if args.no_outro else (args.outro or cfg.get("radio_outro")),
        "preprocessed": not args.raw,
        "config_file": str(CONFIG_PATH) if not args.no_config and CONFIG_PATH.exists() else None,
    })


def cmd_voices(args):
    """Handler for the 'voices' subcommand."""
    voices = list_voices(language=args.language)
    json_output({
        "count": len(voices),
        "voices": voices,
    })


def cmd_test(args):
    """Handler for the 'test' subcommand."""
    cfg = load_config(args.no_config)
    test_text = "Ciao, sono Bronzo. Questa è una prova della mia voce. Come suona?"
    fmt = args.format or cfg["format"]

    output_path = generate_audio(
        text=test_text,
        output_path=args.output or str(TMP_DIR / f"bronzo_tts_test.{fmt}"),
        voice=args.voice or cfg["voice"],
        rate=args.rate or cfg["rate"],
        pitch=args.pitch or cfg["pitch"],
        volume=args.volume or cfg["volume"],
        output_format=fmt,
    )

    file_size = os.path.getsize(output_path)
    json_output({
        "status": "ok",
        "output": output_path,
        "size_bytes": file_size,
        "text": test_text,
        "voice": args.voice or cfg["voice"],
        "rate": args.rate or cfg["rate"],
        "pitch": args.pitch or cfg["pitch"],
        "volume": args.volume or cfg["volume"],
        "format": fmt,
        "config_file": str(CONFIG_PATH) if not args.no_config and CONFIG_PATH.exists() else None,
    })


def cmd_config(args):
    """Handler for the 'config' subcommand."""

    # Default to 'show' if no subcommand given
    action = args.config_action or "show"

    if action == "show":
        cfg = load_config(args.no_config)
        using_file = CONFIG_PATH.exists() and not args.no_config
        json_output({
            "config_file": str(CONFIG_PATH) if using_file else None,
            "settings": cfg,
            "source": "file" if using_file else "factory defaults",
        })

    elif action == "set":
        if "=" not in args.kv:
            json_error("Use key=value format (e.g. pitch=-20Hz)")
        key, value = args.kv.split("=", 1)

        valid_keys = ["voice", "rate", "pitch", "volume", "format", "max_chars"]
        if key not in valid_keys:
            json_error(f"Invalid key '{key}'. Valid keys: {', '.join(valid_keys)}")

        cfg = load_config(no_config=False)

        # Type conversion for max_chars
        if key == "max_chars":
            try:
                value = int(value)
            except ValueError:
                json_error("max_chars must be an integer")

        # Validate format
        if key == "format" and value not in ("mp3", "ogg", "wav"):
            json_error("format must be mp3, ogg, or wav")

        old_value = cfg.get(key)
        cfg[key] = value
        save_config(cfg)
        json_output({
            "status": "ok",
            "key": key,
            "old_value": old_value,
            "new_value": value,
            "config_file": str(CONFIG_PATH),
        })

    elif action == "reset":
        save_config(FACTORY_DEFAULTS.copy())
        json_output({
            "status": "ok",
            "config_file": str(CONFIG_PATH),
            "settings": FACTORY_DEFAULTS,
        })


def cmd_duration(args):
    """Handler for the 'duration' subcommand."""
    if args.file:
        path = Path(args.file)
        if not path.exists():
            json_error(f"File not found: {args.file}")
        text = path.read_text(encoding="utf-8").strip()
    elif args.text:
        text = " ".join(args.text)
    else:
        json_error("Provide text as argument or use --file")

    if not text:
        json_error("Empty text")

    chars = len(text)
    seconds = estimate_duration(text)
    minutes = seconds / 60

    json_output({
        "characters": chars,
        "estimated_seconds": seconds,
        "estimated_minutes": round(minutes, 1),
        "chunks": len(split_text(text)),
    })


# --- Cache command ---

def cmd_cache(args):
    """Handler for the 'cache' subcommand."""
    if args.cache_action == "clear":
        count = _cache_clear()
        json_output({"status": "ok", "action": "clear", "files_removed": count})
    elif args.cache_action == "info":
        info = _cache_info()
        json_output({
            "status": "ok",
            "action": "info",
            "count": info["count"],
            "size_bytes": info["size_bytes"],
            "size_mb": round(info["size_bytes"] / (1024 * 1024), 2),
            "path": info["path"],
        })
    else:
        json_error("Usage: bronzo-tts cache {clear|info}")


# --- Async job commands ---

def cmd_status(args):
    """Handler for the 'status' subcommand. Shows async job state."""
    state = _async_job_status(args.job_id)
    json_output(state)


def cmd_wait(args):
    """Handler for the 'wait' subcommand. Polls until job completes."""
    state = _async_job_wait(args.job_id, poll_interval=args.poll or 1.0, timeout=args.timeout or 300)
    json_output(state)


# --- Main ---

def main():
    load_env()
    parser = argparse.ArgumentParser(
        prog="bronzo-tts",
        description="Text-to-speech with Bronzo's voice (edge-tts). "
                    "Say, radio, duration. Streaming API with --stream. Background jobs with --async. v1.8.0",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {__version__}",
    )

    sub = parser.add_subparsers(dest="command")

    # --- say ---
    p_say = sub.add_parser("say", help="Generate audio from text")
    p_say.add_argument("text", nargs="*", help="Text to speak")
    p_say.add_argument("--file", "-f", help="Read text from file")
    p_say.add_argument("--dir", help="Process all .txt files in a directory")
    p_say.add_argument("--output", "-o", help="Output file path (default: temp file)")
    p_say.add_argument("--voice", "-v", help="Voice name (overrides config)")
    p_say.add_argument("--rate", help="Speech rate (overrides config)")
    p_say.add_argument("--pitch", help="Voice pitch (overrides config)")
    p_say.add_argument("--volume", help="Volume (overrides config)")
    p_say.add_argument("--raw", action="store_true", help="Skip dizione preprocessing")
    p_say.add_argument("--dizione", help="JSON file with custom dizione rules (abbreviations, acronyms, foreign_words)")
    p_say.add_argument("--show-processed", action="store_true", help="Show preprocessed text without generating audio")
    p_say.add_argument("--format", choices=["mp3", "ogg", "wav"], help="Output audio format (overrides config)")
    p_say.add_argument("--max-chars", type=int, help="Max characters per chunk (overrides config)")
    p_say.add_argument("--normalize", action="store_true", help="Normalize volume to -14 LUFS (loudnorm)")
    p_say.add_argument("--trim", action="store_true", help="Trim silence from start and end")
    p_say.add_argument("--pad", nargs="?", const=150, type=int, default=0, metavar="MS", help="Add silence padding at start and end (default 150ms)")
    p_say.add_argument("--telegram", help="Send as voice note to Telegram contact (forces OGG format)")
    p_say.add_argument("--caption", help="Caption for Telegram voice note (requires --telegram)")
    p_say.add_argument("--no-config", action="store_true", help="Bypass config file, use built-in defaults")
    p_say.add_argument("--no-cache", action="store_true", help="Bypass content cache")
    p_say.add_argument("--stream", action="store_true", help="Use edge-tts Python streaming API instead of subprocess")
    p_say.add_argument("--async", dest="async_", action="store_true", help="Run generation in background (returns job_id)")
    p_say.add_argument("--profile", help="Speaker profile ID for profile-aware TTS (rate/pitch/volume from speaker_profiles.json)")

    # --- radio ---
    p_radio = sub.add_parser("radio", help="Generate a Radio Bronzo episode")
    p_radio.add_argument("text", nargs="*", help="Episode text")
    p_radio.add_argument("--file", "-f", help="Read text from file")
    p_radio.add_argument("--dir", help="Process all .txt files in a directory (sequential episodes)")
    p_radio.add_argument("--ep", "-e", type=int, required=True, help="Episode number")
    p_radio.add_argument("--title", "-t", help="Episode title")
    p_radio.add_argument("--intro", help="Custom intro text (overrides config radio_intro)")
    p_radio.add_argument("--outro", help="Custom outro text (overrides config radio_outro)")
    p_radio.add_argument("--no-intro", action="store_true", help="Skip intro")
    p_radio.add_argument("--no-outro", action="store_true", help="Skip outro")
    p_radio.add_argument("--output", "-o", help="Output file path (default: ~/.bronzo_tts/tmp/radio_bronzo_ep{N}.ogg)")
    p_radio.add_argument("--voice", "-v", help="Voice name (overrides config)")
    p_radio.add_argument("--rate", help="Speech rate (overrides config)")
    p_radio.add_argument("--pitch", help="Voice pitch (overrides config)")
    p_radio.add_argument("--volume", help="Volume (overrides config)")
    p_radio.add_argument("--raw", action="store_true", help="Skip dizione preprocessing")
    p_radio.add_argument("--dizione", help="JSON file with custom dizione rules (abbreviations, acronyms, foreign_words)")
    p_radio.add_argument("--format", choices=["mp3", "ogg", "wav"], help="Output audio format (overrides config, default: ogg)")
    p_radio.add_argument("--max-chars", type=int, help="Max characters per chunk (overrides config)")
    p_radio.add_argument("--normalize", action="store_true", help="Normalize volume to -14 LUFS (loudnorm)")
    p_radio.add_argument("--trim", action="store_true", help="Trim silence from start and end")
    p_radio.add_argument("--pad", nargs="?", const=150, type=int, default=150, metavar="MS", help="Add silence padding at start and end (default 150ms)")
    p_radio.add_argument("--telegram", help="Send as voice note to Telegram contact (forces OGG format)")
    p_radio.add_argument("--caption", help="Caption for Telegram voice note (requires --telegram)")
    p_radio.add_argument("--no-config", action="store_true", help="Bypass config file, use built-in defaults")
    p_radio.add_argument("--no-cache", action="store_true", help="Bypass content cache")
    p_radio.add_argument("--stream", action="store_true", help="Use edge-tts Python streaming API instead of subprocess")
    p_radio.add_argument("--async", dest="async_", action="store_true", help="Run generation in background (returns job_id)")
    p_radio.add_argument("--profile", help="Speaker profile ID for profile-aware TTS (rate/pitch/volume from speaker_profiles.json)")

    # --- voices ---
    p_voices = sub.add_parser("voices", help="List available voices")
    p_voices.add_argument("--language", "-l", help="Filter by language prefix (e.g., it-IT)")

    # --- test ---
    p_test = sub.add_parser("test", help="Generate a test sample")
    p_test.add_argument("--output", "-o", help="Output file path")
    p_test.add_argument("--voice", "-v", help="Voice name")
    p_test.add_argument("--rate", help="Speech rate")
    p_test.add_argument("--pitch", help="Voice pitch")
    p_test.add_argument("--volume", help="Volume")
    p_test.add_argument("--format", choices=["mp3", "ogg", "wav"], help="Output audio format")
    p_test.add_argument("--no-config", action="store_true", help="Bypass config file, use built-in defaults")

    # --- config ---
    p_config = sub.add_parser("config", help="Manage configuration")
    p_config.add_argument("--no-config", action="store_true", help="Show factory defaults instead of file config")
    config_sub = p_config.add_subparsers(dest="config_action")

    p_config_show = config_sub.add_parser("show", help="Show current configuration")
    p_config_show.add_argument("--no-config", action="store_true", help="Show factory defaults instead of file config")
    p_config_set = config_sub.add_parser("set", help="Set a config value (key=value)")
    p_config_set.add_argument("kv", help="Key=value (e.g. pitch=-20Hz, format=ogg, max_chars=5000)")
    p_config_reset = config_sub.add_parser("reset", help="Reset to factory defaults")

    # --- duration ---
    p_cache = sub.add_parser("cache", help="Manage content cache")
    cache_sub = p_cache.add_subparsers(dest="cache_action")
    cache_sub.add_parser("clear", help="Clear all cached audio")
    cache_sub.add_parser("info", help="Show cache statistics")

    p_duration = sub.add_parser("duration", help="Estimate audio duration without generating")
    p_duration.add_argument("text", nargs="*", help="Text to estimate")
    p_duration.add_argument("--file", "-f", help="Read text from file")
    p_duration.add_argument("--no-config", action="store_true", help="Bypass config file")

    # --- status ---
    p_status = sub.add_parser("status", help="Check status of an async job")
    p_status.add_argument("job_id", help="Job ID returned by --async")

    # --- wait ---
    p_wait = sub.add_parser("wait", help="Wait for an async job to complete")
    p_wait.add_argument("job_id", help="Job ID returned by --async")
    p_wait.add_argument("--poll", type=float, default=1.0, help="Poll interval in seconds (default: 1.0)")
    p_wait.add_argument("--timeout", type=int, default=300, help="Maximum wait time in seconds (default: 300)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    handlers = {
        "say": cmd_say,
        "radio": cmd_radio,
        "voices": cmd_voices,
        "test": cmd_test,
        "config": cmd_config,
        "cache": cmd_cache,
        "duration": cmd_duration,
        "status": cmd_status,
        "wait": cmd_wait,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
