#!/usr/bin/env python3
"""Generate narrated MP3s of daily journal entries via self-hosted TTS.

Run on a GPU host. Pulls daily summaries that don't yet have a narration
from the production DB, synthesises audio with Coqui XTTS-v2 (zero-shot,
conditioned on the reference clip in MODEL_DIR/ref.wav), copies the
resulting MP3 back to production over SSH, and updates the DB. Designed
to be invoked daily by cron after the 23:55 summary job.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL_DIR = ROOT / "model" / "Finished_model_files"
REF_WAV = MODEL_DIR / "ref.wav"
OUT_DIR = ROOT / "output"
REMOTE = os.environ.get("REDTAIL_HOST", "192.168.30.103")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("narrate")


def ssh(cmd: str, capture: bool = True) -> str:
    """Run a command on the production host over SSH."""
    full = ["ssh", REMOTE, cmd]
    if capture:
        res = subprocess.run(full, capture_output=True, text=True, check=True)
        return res.stdout
    subprocess.run(full, check=True)
    return ""


def _combine(summary: str, bio_context: str | None) -> str:
    """Return the full text that gets narrated: summary + biological context."""
    s = (summary or "").strip()
    b = (bio_context or "").strip()
    if not b:
        return s
    return f"{s}\n\n{b}"


def fetch_pending_days() -> list[tuple[str, str]]:
    """Returns [(day, narration_text), ...] for entries without a narration."""
    py = (
        "import sqlite3, json, sys\n"
        "c = sqlite3.connect('/state/events.db')\n"
        "rows = c.execute(\"SELECT day, summary, bio_context FROM daily_summaries WHERE narration_path IS NULL OR narration_path='' ORDER BY day\").fetchall()\n"
        "print(json.dumps(rows))\n"
    )
    out = ssh(f"docker exec -i redtail python3 - <<'PYEOF'\n{py}\nPYEOF\n")
    import json
    return [(d, _combine(s, b)) for d, s, b in json.loads(out.strip().splitlines()[-1])]


_SENTENCE_RE = None
def _sentences(text: str) -> list[str]:
    """Split text into sentences."""
    import re
    global _SENTENCE_RE
    if _SENTENCE_RE is None:
        _SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
    parts = [s.strip() for s in _SENTENCE_RE.split(text) if s.strip()]
    return parts or [text]


def _chunk(text: str, max_chars: int = 220) -> list[str]:
    """Group sentences into chunks.

    XTTS-v2 has two hard limits per synthesis call:
      - input: ~400 GPT tokens (~1500 chars)
      - output: ~27 seconds of audio  (this is the real ceiling)

    Aiming for ~220 char chunks gives ~12-15 s of audio each, well under the
    output limit. Most journal sentences are 80-200 chars so chunks typically
    hold 1-2 sentences.
    """
    chunks: list[str] = []
    current = ""
    for s in _sentences(text):
        if not current:
            current = s
        elif len(current) + 1 + len(s) <= max_chars:
            current = f"{current} {s}"
        else:
            chunks.append(current)
            current = s
    if current:
        chunks.append(current)
    return chunks


def synthesize(model, config, text: str, wav_out: Path) -> None:
    """Generate WAV for the given text, chunking long input to stay under XTTS limits."""
    import torch
    import torchaudio

    chunks = _chunk(text)
    waveforms = []
    for chunk in chunks:
        out = model.synthesize(
            text=chunk,
            config=config,
            speaker_wav=str(REF_WAV),
            gpt_cond_len=3,
            language="en",
            temperature=0.75,
        )
        waveforms.append(torch.tensor(out["wav"]))
        # Brief silence between chunks for natural pacing
        waveforms.append(torch.zeros(int(24000 * 0.35)))

    combined = torch.cat(waveforms).unsqueeze(0)
    torchaudio.save(str(wav_out), combined, 24000)


def wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(wav_path), "-q:a", "5", str(mp3_path)],
        check=True, capture_output=True,
    )


def deploy_to_production(day: str, mp3_path: Path) -> str:
    """Copy mp3 into the container and update the DB. Returns production path."""
    remote_tmp = f"/tmp/redtail-narration-{day}.mp3"
    subprocess.run(["scp", str(mp3_path), f"{REMOTE}:{remote_tmp}"], check=True)
    container_path = f"/data/narrations/{day}.mp3"
    ssh(f"docker exec redtail mkdir -p /data/narrations && "
        f"docker cp {remote_tmp} redtail:{container_path} && "
        f"rm {remote_tmp}")
    # Update the DB
    py = (
        "import sqlite3\n"
        "c = sqlite3.connect('/state/events.db')\n"
        f"c.execute(\"UPDATE daily_summaries SET narration_path=? WHERE day=?\", ('{container_path}', '{day}'))\n"
        "c.commit()\n"
    )
    ssh(f"docker exec -i redtail python3 - <<'PYEOF'\n{py}\nPYEOF\n")
    return container_path


def load_model():
    log.info("loading XTTS-v2 model...")
    t0 = time.time()
    from TTS.tts.configs.xtts_config import XttsConfig
    from TTS.tts.models.xtts import Xtts

    config = XttsConfig()
    config.load_json(str(MODEL_DIR / "config.json"))
    model = Xtts.init_from_config(config)
    model.load_checkpoint(config, checkpoint_dir=str(MODEL_DIR), use_deepspeed=False)
    model.cuda()
    log.info("model loaded in %.1fs", time.time() - t0)
    return model, config


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--day", help="Narrate only this specific day (YYYY-MM-DD); otherwise all pending")
    p.add_argument("--force", action="store_true", help="Re-narrate even if narration exists")
    p.add_argument("--dry-run", action="store_true", help="Show which days would be narrated and exit")
    args = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.day:
        py = (
            "import sqlite3, json\n"
            "c = sqlite3.connect('/state/events.db')\n"
            f"r = c.execute(\"SELECT day, summary, bio_context FROM daily_summaries WHERE day = '{args.day}'\").fetchone()\n"
            "print(json.dumps(list(r) if r else []))\n"
        )
        import json
        row = json.loads(ssh(f"docker exec -i redtail python3 - <<'PYEOF'\n{py}\nPYEOF\n").strip().splitlines()[-1])
        days = [(row[0], _combine(row[1], row[2]))] if row else []
    else:
        days = fetch_pending_days()

    log.info("pending narrations: %d", len(days))
    if args.dry_run:
        for d, s in days:
            log.info("  %s: %s...", d, s[:60])
        return

    if not days:
        log.info("nothing to narrate")
        return

    model, config = load_model()

    for day, summary in days:
        log.info("[%s] generating (%d chars)", day, len(summary))
        wav_path = OUT_DIR / f"{day}.wav"
        mp3_path = OUT_DIR / f"{day}.mp3"
        try:
            t0 = time.time()
            synthesize(model, config, summary, wav_path)
            wav_to_mp3(wav_path, mp3_path)
            wav_path.unlink(missing_ok=True)
            log.info("[%s] generated in %.1fs (%d KB)", day, time.time() - t0, mp3_path.stat().st_size // 1024)
            container_path = deploy_to_production(day, mp3_path)
            log.info("[%s] deployed → %s", day, container_path)
        except Exception:
            log.exception("[%s] failed", day)


if __name__ == "__main__":
    main()
