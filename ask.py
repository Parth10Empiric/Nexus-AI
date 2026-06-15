#!/usr/bin/env python3
"""
ask.py — quick TEXT-mode tester for Nexus AI (no mic / no GUI needed).

Two ways to use it:

  1. Ask about the file you currently have OPEN (the tracker must be running so
     the active file is in the database):
        python ask.py "what classes are defined in the file I have open?"

  2. Ask about a SPECIFIC file directly (no tracker needed — great for testing):
        python ask.py "list the class names" --file tracker/tts_engine.py

It runs the SAME context pipeline the voice assistant uses (situational-aware
persona + screen/code context) and prints Nexus's answer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request
from pathlib import Path

from tracker import config, context_engine, retriever
from tracker.memory_manager import get_memory


def _ask_ollama(system: str, user: str) -> str:
    req = urllib.request.Request(
        f"{config.OLLAMA_URL}/api/chat",
        data=json.dumps({
            "model": config.OLLAMA_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "keep_alive": config.OLLAMA_KEEP_ALIVE,
            "options": {"num_predict": 300},
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["message"]["content"].strip()


def _user_content_for_file(path: Path, question: str) -> str:
    code = path.read_text(encoding="utf-8", errors="replace")[: config.MAX_CONTEXT_CHARS]
    return (
        "Follow these rules: if casual, ignore the code; if about the code, use it.\n\n"
        "[ACTIVE SCREEN CONTEXT]\n"
        f"Currently viewing: {path.name}\nCode: {code}\n\n"
        f"[USER SPOKE]: {question}"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*", help="what to ask Nexus")
    ap.add_argument("--file", help="ask about THIS file directly (skips the tracker)")
    args = ap.parse_args()

    question = " ".join(args.question) or \
        "What classes are defined in the file I have open? List their names."

    if args.file:
        path = Path(args.file).expanduser().resolve()
        if not path.is_file():
            print(f"File not found: {path}")
            return 1
        user = _user_content_for_file(path, question)
        print(f"📄 File: {path.name}")
    else:
        # Full pipeline: live active file + global code + history (Phase 5.3).
        user = await retriever.retrieve_and_build(question, get_memory(), "")
        print("📄 Using your currently-open file (from the tracker).")

    print(f"🗣️  You: {question}\n…thinking…\n")
    answer = _ask_ollama(context_engine.NEXUS_SYSTEM_PROMPT, user)
    print(f"🤖 Nexus: {answer}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
