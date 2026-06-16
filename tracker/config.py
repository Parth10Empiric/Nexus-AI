"""
Central configuration for the Nexus AI window tracker daemon (Phase 1.1).

Keeping every tunable value in one place means you never have to hunt
through the logic files to change behaviour. Edit values here, restart the
daemon, done.
"""

from pathlib import Path

# --- Paths -----------------------------------------------------------------
# Project root = the folder that contains the `tracker/` package.
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"

# The local SQLite database the spec asks for.
DB_PATH = DATA_DIR / "local_logs.db"

# --- Polling behaviour ------------------------------------------------------
# How often (seconds) we look at the active window. The spec says 5 seconds.
POLL_INTERVAL_SECONDS = 5

# Even if the user never switches windows, write a "heartbeat" row at least
# this often so the timeline shows they were still active. Set to None to
# disable heartbeats entirely (then we only ever log on change).
HEARTBEAT_SECONDS = 300  # 5 minutes

# --- Noise filtering --------------------------------------------------------
# Application names (lower-cased) that are background OS chrome, not real work.
# If the active window belongs to one of these, we discard the sample.
IGNORED_APP_NAMES = {
    "gnome-shell",
    "gjs",
    "plasmashell",
    "mutter",
    "xfdesktop",
    "x-nautilus-desktop",
}

# Window titles (lower-cased, exact match) that are never meaningful.
IGNORED_TITLES = {
    "desktop",
    "@!0,0;bdh",          # gnome-shell internal title
    "",
}

# If the title is shorter than this it's almost always noise (e.g. a single
# stray character from a transient popup). Set to 0 to disable.
MIN_TITLE_LENGTH = 1

# ===========================================================================
# Phase 3.1 — Active File Source Reader
# ===========================================================================

# Where the workspace keyword -> absolute path map lives. If this file is
# missing we fall back to DEFAULT_WORKSPACE_MAP below.
WORKSPACE_CONFIG_PATH = ROOT_DIR / "workspace_config.json"

# Fallback workspace map used only if workspace_config.json is absent.
# Keys are keywords searched (case-insensitively) inside the window title;
# values are the absolute project roots to scan for the active file.
DEFAULT_WORKSPACE_MAP = {
    "Nexus AI": str(ROOT_DIR),
}

# Directories we must NEVER descend into while searching. These are huge,
# irrelevant, or dangerous to read, and skipping them keeps the scan fast.
IGNORED_DIRS = {
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    "venv",
    ".venv",
    "env",
    ".env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "target",          # Rust / Java
    ".next",
    ".nuxt",
    ".cache",
    "coverage",
    ".idea",
    ".gradle",
}

# File extensions we treat as binary / non-source and never read.
IGNORED_FILE_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".obj", ".dll", ".dylib",
    ".exe", ".bin", ".class", ".jar", ".war",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
    ".pdf", ".zip", ".gz", ".tar", ".7z", ".rar", ".bz2", ".xz",
    ".mp3", ".mp4", ".mov", ".avi", ".mkv", ".wav", ".flac",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".lock", ".db", ".sqlite", ".sqlite3", ".db-journal",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}

# Never read a file larger than this (bytes). Protects against multi-MB files
# stalling the loop or bloating the database. Spec: under 500 KB.
MAX_FILE_SIZE_BYTES = 500 * 1024  # 500 KB

# Hard cap on how many files the recursive search will visit before giving up,
# so an enormous workspace can never make one tick run long.
MAX_FILES_SCANNED = 20_000

# ===========================================================================
# Phase 3.2 — File-Change Event Hook (watchdog)
# ===========================================================================

# Trailing-edge debounce window (seconds) per file path. Editors fire several
# modify events per save; we wait this long after the LAST event before
# reading, collapsing a save burst into one read and ensuring the write has
# settled. 0.5s is comfortably above typical editor write bursts.
OBSERVER_DEBOUNCE_SECONDS = 0.5

# ===========================================================================
# Phase 3.3 — Automated Prompt Context Mixer
# ===========================================================================

# Substrings that identify THIS project's own files/windows. If the active
# context matches any of these (case-insensitive), we must NOT inject it into
# the AI prompt — otherwise the assistant would read its own source and loop.
SELF_EXCLUDE_MARKERS = {
    "nexus ai",
    "nexus_ai",
    "nexus-ai",
}

# ---------------------------------------------------------------------------
# Phase 5.3 — Focus classification (the "Stale Context Bug" fix).
# The orchestrator fetches the LATEST OS window from `activity_log` and decides
# whether the user is actually looking at their editor (so the background code
# file IS the screen) or at a browser/terminal (so the code is just background).
# Matching is case-insensitive substring against "<app_name> <title>".
# ---------------------------------------------------------------------------
EDITOR_APP_MARKERS = {
    "visual studio code", "vscode", "vs code", "code - oss", "code",
    "cursor", "windsurf", "sublime text", "sublime", "intellij", "pycharm",
    "webstorm", "neovim", "nvim", "vim", "nano", "emacs", "gedit", "kate",
    "atom", "zed", "jetbrains",
}
BROWSER_APP_MARKERS = {
    "google chrome", "chromium", "chrome", "firefox", "mozilla", "brave",
    "microsoft edge", "edge", "opera", "vivaldi", "safari", "librewolf",
}
TERMINAL_APP_MARKERS = {
    "gnome-terminal", "terminal", "konsole", "xterm", "alacritty", "kitty",
    "tmux", "terminator", "tilix", "wezterm", "ptyxis", "bash", "zsh",
}

# Master switch for the self-exclusion guard. Set False when you ARE developing
# Nexus AI itself and want to ask the assistant about its OWN files (e.g.
# "what classes are in test_tracker.py?"). True = never inject Nexus AI's own
# code (the original anti-recursion behaviour, for when it's a tool you use
# while working on OTHER projects).
EXCLUDE_SELF_CONTEXT = False

# Upper bound on how much code we inject into a single prompt. The DB stores up
# to 500KB, but a 1.5B model's attention degrades on huge contexts and latency
# rises. We keep the head of the file (imports/class defs matter most) and note
# any truncation so the model knows the snippet is partial.
MAX_CONTEXT_CHARS = 6_000

# ===========================================================================
# Phase 4.1 — Local Microphone Stream Capturer
# ===========================================================================

# Audio format. These three values are REQUIRED by faster-whisper (Step 4.2),
# which expects 16kHz mono 16-bit PCM. Capturing natively in this format avoids
# any CPU resampling/downmixing later.
AUDIO_SAMPLE_RATE = 16_000      # Hz  (Whisper's native rate)
AUDIO_CHANNELS = 1              # mono
AUDIO_DTYPE = "int16"           # 16-bit signed PCM

# PortAudio callback block size (frames per callback). Smaller = lower latency,
# slightly more callbacks. 1600 frames = 100ms at 16kHz — a good balance.
AUDIO_BLOCKSIZE = 1600

# Safety cap: auto-stop a recording after this many seconds so a stuck/held key
# can never grow the in-memory buffer without bound.
AUDIO_MAX_SECONDS = 120

# The push-to-talk chord. Hold these together to record; release to stop.
# Values are pynput key names; modifiers accept either left/right variant.
PTT_MODIFIER = "ctrl"           # ctrl_l or ctrl_r
PTT_KEY = "space"

# ===========================================================================
# Phase 4.2 — Local Speech-to-Text (faster-whisper)
# ===========================================================================

# Model + CPU optimizations tuned for the i5-6500 (4 cores, no GPU).
STT_MODEL_SIZE = "base.en"      # English-only base: best speed/accuracy on CPU
STT_DEVICE = "cpu"
STT_COMPUTE_TYPE = "int8"       # 8-bit quantization: <1GB RAM, faster CPU math
STT_CPU_THREADS = 4             # one thread per physical core
STT_BEAM_SIZE = 1               # greedy decode: fastest; bump to 5 for accuracy

# ===========================================================================
# Phase 4.3 — Local Text-to-Speech Output (Piper TTS)
# ===========================================================================

# Pre-downloaded Piper ONNX voice model (sits in the project root). Piper finds
# the matching <model>.json config automatically next to it.
TTS_MODEL_PATH = ROOT_DIR / "en_US-lessac-medium.onnx"

# Characters that mark the end of a speakable sentence. As soon as one appears
# in the token stream we flush that sentence to the synth queue (low latency).
TTS_SENTENCE_ENDINGS = ".?!\n"

# Playback is written to the output device in small blocks so an interrupt is
# honoured within ~one block instead of waiting for a whole sentence.
TTS_PLAYBACK_BLOCK = 2048

# Don't read code aloud symbol-by-symbol. Instead speak a short summary like
# "Code block, 12 lines, shown on screen." (the full code still renders in UI).
TTS_SUMMARIZE_CODE_BLOCKS = True

# ===========================================================================
# Phase 4.4 — Conversational Orchestrator
# ===========================================================================

# Local Ollama endpoint + model used for the voice conversation.
OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5-coder:1.5b"

# Keep the LLM RESIDENT in RAM between turns. Without this Ollama unloads the
# ~1GB model after ~5min idle and the next turn pays a multi-second reload. The
# single biggest latency win for a back-and-forth conversation.
OLLAMA_KEEP_ALIVE = "30m"

# Cap spoken answers so replies are short and fast (voice ≠ essays). 0 = no cap.
OLLAMA_NUM_PREDICT = 220

# How many recent (user, assistant) turns Nexus Ten remembers and replays to
# the model each turn, so it has short-term conversational memory.
CONV_HISTORY_TURNS = 5

# ===========================================================================
# Phase 4.5 — UI Orchestrator & Wake-Word Context Engine
# ===========================================================================

# ###########################################################################
# 🎙️  VOICE CONTROL — CHANGE THE WAKE WORD AND STOP WORDS HERE
# ###########################################################################
#
# WAKE WORD: which phrase starts a session. MUST be one of openwakeword's
# pretrained models (there is no pretrained "hey nexus" — a custom phrase needs
# a trained model: https://github.com/dscripka/openWakeWord). Choices:
#     "hey_jarvis", "hey_mycroft", "alexa", "hey_rhasspy"
WAKE_WORD_MODEL = "hey_jarvis"

# STOP WORDS: say any of these (as the whole sentence) to TURN THE AGENT OFF.
STOP_WORDS = (
    "bye", "bye bye", "goodbye", "good bye", "see you", "see ya",
    "sleep", "go to sleep", "stop listening", "that's all", "thats all",
    "thank you bye", "okay bye", "ok bye",
)

# STOP PHRASES: stop the agent if any of these appear anywhere in the sentence
# (the wake/persona name variants).
STOP_PHRASES = (
    "bye jarvis", "goodbye jarvis", "sleep jarvis",
    "bye nexus", "goodbye nexus", "sleep nexus",
)
# ###########################################################################

WAKE_THRESHOLD = 0.5            # score 0..1 above which the wake word fires
WAKE_FRAME_SAMPLES = 1280       # 80ms @ 16kHz — openwakeword's expected chunk

# After waking, capture the question until this much trailing silence, or the
# max duration, whichever comes first (energy-based endpointing).
WAKE_ENDPOINT_SILENCE_MS = 900
WAKE_MAX_UTTERANCE_SEC = 10
WAKE_SILENCE_RMS = 350          # int16 RMS below this counts as silence

# How many recent window/file changes to feed Nexus Ten as "recent history".
OMNISCIENT_HISTORY = 5

# Local WebSocket bridge the React UI connects to for toggle + state events.
UI_WS_HOST = "127.0.0.1"
UI_WS_PORT = 8765

# --- Continuous multi-turn session (Phase 4.5 refactor) --------------------
# Voice-activity endpointing (energy based) for ACTIVE_LISTENING.
VAD_START_RMS = 400             # int16 RMS above this = speech started
VAD_SILENCE_MS = 650            # trailing silence that ends an utterance (lower
                                # = snappier; raise if it cuts you off mid-word)
VAD_MAX_UTTERANCE_SEC = 15      # hard cap on one utterance

# In ACTIVE_LISTENING, if no speech begins within this long, the session goes
# back to STANDBY so the mic isn't actively endpointing forever.
SESSION_IDLE_TIMEOUT_SEC = 12

# Barge-in guard while SPEAKING: require sustained voice ABOVE this (higher than
# VAD_START_RMS to resist the AI's own audio echo) for this long to interrupt.
BARGEIN_RMS = 900
BARGEIN_SUSTAIN_MS = 500

# Rolling conversation memory kept alive during an active session (messages,
# i.e. user+assistant lines — last 10).
SESSION_MEMORY_MESSAGES = 10

# ===========================================================================
# Phase 5.1 — Dual-Stream Vector Indexing (ChromaDB + nomic-embed-text)
# ===========================================================================

# Local persistent vector store location + the two collection names.
CHROMA_DIR = ROOT_DIR / "chroma_db"
CODEBASE_COLLECTION = "codebase_index"
ACTIVITY_COLLECTION = "activity_memory"

# Phase 5.2: the singleton memory manager's persistent store path.
NEXUS_MEMORY_DIR = ROOT_DIR / "nexus_memory_db"

# Live-index each file into the vector vault when it's saved (Ctrl+S), so the
# codebase memory stays fresh as you work. Deduped, so unchanged files are free.
AUTO_INDEX_ON_SAVE = True

# --- Phase 5.3: Hybrid Context Retrieval --------------------------------
RAG_TOP_K = 3                   # top results pulled from each collection
# Cosine DISTANCE cutoff (0 = identical, ~2 = opposite). Hits FARTHER than this
# are dropped as irrelevant — this is what makes casual questions carry no code
# context. ~0.55 keeps on-topic code/logs, drops unrelated chatter.
RAG_MAX_DISTANCE = 0.55

# Embedding model (served by the local Ollama instance).
EMBED_MODEL = "nomic-embed-text"

# Which project to index for global codebase context (override per machine).
INDEX_PROJECT_ROOT = "/home/empiric/Projects/EMPIRA_HR"

# Only these source types are indexed into codebase_index.
CODE_EXTENSIONS = {".py", ".html", ".js"}

# Chunking. RecursiveCharacterTextSplitter counts CHARACTERS; ~4 chars ≈ 1
# token, so ~500 tokens ≈ 2000 chars and ~50-token overlap ≈ 200 chars.
CHUNK_CHARS = 2000
CHUNK_OVERLAP_CHARS = 200

# Stop words/phrases are defined once in the VOICE CONTROL block above. These
# aliases keep older references working.
SESSION_END_PHRASES = STOP_PHRASES
SESSION_GOODBYE_WORDS = STOP_WORDS

# Short words that just INTERRUPT/cancel the current reply and re-listen — they
# are NOT answered as questions. Matched only when the WHOLE utterance is one
# of these (so "okay so explain X" is still treated as a real question).
SESSION_INTERRUPT_WORDS = (
    "stop", "stop ask", "stop it", "okay", "ok", "wait",
    "cancel", "enough", "never mind", "nevermind", "shut up",
)

# The session stays in ACTIVE_LISTENING and keeps re-listening forever until an
# end phrase. If True, a long silence does NOT drop to STANDBY (loop continues).
SESSION_STAY_AWAKE = True

# Arm the voice front-end automatically when the orchestrator starts, so it
# listens for the wake word immediately WITHOUT needing the UI toggle. (The UI
# toggle still works for arming/disarming on top of this.)
SESSION_AUTOSTART = True

# Faster STT alternative: set STT_MODEL_SIZE = "tiny.en" above for ~2-3x faster
# transcription on CPU (slightly lower accuracy — fine for short voice commands).
