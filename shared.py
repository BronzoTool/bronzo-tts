#!/usr/bin/env python3
"""
shared — Shared utilities for bronzo-tts standalone.

Extracted from Bronzo toolchain nanobot_utils.py.
Only the functions used by bronzo-tts are included.

Path constants use ~/.bronzo_tts/ (not ~/.bronzo_tts/) for standalone distribution.
"""

import os
import sys
import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

# ─── Path Constants ───────────────────────────────────────────────────────────

SCRIBA_DIR = Path.home() / ".bronzo_tts"
ENV_PATH = SCRIBA_DIR / ".env"
PROTECTED_INPUTS_PATH = SCRIBA_DIR / "protected_inputs.json"
