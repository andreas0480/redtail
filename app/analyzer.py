import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import google.generativeai as genai

from .config import Config
from .db import Database
from .util import ensure_dir, now_iso, today_str

log = logging.getLogger(__name__)

EVENT_TYPES = [
    "empty",
    "adult_present",
    "adult_arrives",
    "adult_leaves",
    "eggs_visible",
    "incubating",
    "feeding",
    "chicks_visible",
    "chick_hatching",
    "intruder",
    "unknown",
]

# Confirmed by owner: first egg 2026-05-08. One per day. Clutch still growing (5th egg 2026-05-12).
# Earliest hatch = ~14 days after last egg; use 2026-05-28 as conservative floor.
FIRST_EGG_DATE = "2026-05-08"
EARLIEST_HATCH_DATE = "2026-05-28"

SNAPSHOT_PROMPT = f"""You are analyzing a single frame from a Common Redstart (Phoenicurus phoenicurus / Swedish "rödstjärt") nest-box camera.
The bird is a small passerine — NOT a falcon, NOT a kestrel, NOT a raptor.

CURRENT TIMELINE FACTS (use these to constrain your answer):
- First egg laid: {FIRST_EGG_DATE}. Redstarts lay exactly ONE egg per day in the morning.
- Before {FIRST_EGG_DATE}: zero eggs in the nest. Any bright object is nest material, not an egg.
- Earliest possible hatch date: {EARLIEST_HATCH_DATE}
- The image is timestamped {{capture_time}}. Days since {FIRST_EGG_DATE} = max possible eggs.
- KNOWN VISUAL HAZARD: a curled white feather rests at the top-right edge of the egg cluster. It is light-coloured and oval, and is consistently misclassified as a sixth sky-blue egg. Disregard pale objects at the cluster's edge that do not match the cluster's blue tint. The current clutch is FIVE eggs as of 2026-05-12; if you count more than five, recount excluding the feather.

VISUAL CUES — what each event_type ACTUALLY looks like:
- "empty": nest cup visible with no adult and no eggs. Use when you see ONLY moss/grass/feathers.
- "eggs_visible": you can clearly see one or more sky-blue eggs in the cup. No adult covering them. Count strictly.
- "incubating": an adult is sitting on the nest. Brown/grey FEATHERED MASS filling the cup. Blurry body, wing, or tail covering the cup = INCUBATING.
- "adult_present": adult in the box but not sitting on eggs (perched on rim, standing beside cup).
- "adult_arrives" / "adult_leaves": only use for clips, not snapshots.
- "chicks_visible": STRICT — only naked/downy nestlings with gaping yellow mouths. FORBIDDEN before {EARLIEST_HATCH_DATE}.
- "chick_hatching": egg with chick partly emerging. Same date constraint.
- "feeding": adult holding prey item AND visible chicks with open mouths. FORBIDDEN before {EARLIEST_HATCH_DATE}.
- "intruder": clearly non-Redstart species only.
- "unknown": image too poor to classify.

RULES:
1. If image date < {EARLIEST_HATCH_DATE}: chicks_visible, chick_hatching, and feeding are FORBIDDEN.
2. Count only eggs you can plainly see. If an adult sits on the nest, eggs=0 and chicks=0.
3. If image date < {FIRST_EGG_DATE}: eggs=0 regardless of what you think you see.

OUTPUT — return ONLY this JSON (no markdown):
{{
  "event_type": one of {EVENT_TYPES},
  "confidence": 0.0-1.0,
  "narrative": "one short present-tense sentence",
  "subjects": {{"adults": int, "eggs": int, "chicks": int}},
  "notable": "optional string for unusual things"
}}"""

CLIP_PROMPT = f"""You are analyzing a short motion-triggered clip from a Common Redstart (Phoenicurus phoenicurus) nest-box camera.
The bird is a small passerine — NOT a raptor.

CURRENT TIMELINE FACTS:
- First egg laid: {FIRST_EGG_DATE}. One egg per day. Before {FIRST_EGG_DATE}: no eggs.
- Earliest possible hatch: {EARLIEST_HATCH_DATE}
- Clip timestamp: {{capture_time}}

RULES:
- Count only eggs/chicks clearly visible. If an adult covers the cup, eggs=0 and chicks=0.
- chicks_visible, chick_hatching, and feeding are FORBIDDEN before {EARLIEST_HATCH_DATE}.
- Use keep=false ONLY for clips showing pure light/shadow flicker with no bird.

Return ONLY this JSON:
{{
  "keep": true/false,
  "event_type": one of {EVENT_TYPES},
  "confidence": 0.0-1.0,
  "label": "3-6 word human-readable title describing the action (e.g. 'Female arrives and settles', 'Adult departs nest')",
  "narrative": "one clear sentence describing what happens in this clip"
}}"""

DAILY_PROMPT_TEMPLATE = f"""You are writing the daily journal entry for a Common Redstart (Phoenicurus phoenicurus) nest box.
Date: {{day}}
Events observed by AI today (oldest to newest):

{{events}}

GROUND TRUTH:
- First egg: {FIRST_EGG_DATE}. One egg per day. Before {FIRST_EGG_DATE}: the female was preparing the nest, not incubating eggs.
- Egg count by date: May 8=1, May 9=2, May 10=3, May 11=4, May 12=5. The clutch is currently FIVE eggs.
- KNOWN HALLUCINATION: a curled white feather rests at the top-right of the egg cluster. The AI consistently misidentifies this feather as a sixth sky-blue egg. There is NO sixth egg. Any event claiming 6 or 7 eggs is wrong — write "five eggs" or "five sky-blue eggs" instead.
- One egg per day, always laid in the morning. When egg counts increase through the day, this is the camera getting a clearer view, not new eggs being laid.
- No chicks or feeding before {EARLIEST_HATCH_DATE}.
- Timestamps are UTC; Stockholm is UTC+2 in summer. Do not write "UTC" in your output.

Write a warm, factual 3-5 sentence journal entry in English using local Stockholm time.
Focus on what the birds actually did. Do not claim eggs were laid on a day unless that date falls on or after {FIRST_EGG_DATE}.
Output only the summary text, no preamble."""

BIO_CONTEXT_PROMPT = """You are a field ornithologist writing the biological footnote for a Common Redstart (Phoenicurus phoenicurus) nest box journal.

Today's journal entry ({day}):
"{summary}"

Write exactly 2-3 sentences of biological background that directly explains the science behind what was observed today.
Draw on real species facts: breeding phenology, incubation physiology, egg-laying biology, chick development, foraging behaviour, migration, or pair-bonding as relevant to the day's events.
Be specific — connect the biology to what the camera actually saw. Write for a curious general reader, not a specialist.
Do not repeat the narrative; illuminate it.
Output only the 2-3 sentences, no heading, no preamble."""


class Analyzer:
    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self.model = None
        if cfg.gemini_api_key:
            genai.configure(api_key=cfg.gemini_api_key)
            self.model = genai.GenerativeModel(cfg.gemini_model)
        else:
            log.warning("GEMINI_API_KEY not set, analyzer disabled")

    @property
    def enabled(self) -> bool:
        return self.model is not None

    def process_pending(self) -> int:
        if not self.enabled:
            return 0
        n = 0
        for row in self.db.pending_snapshots(limit=8):
            try:
                if self._analyze_snapshot(row):
                    n += 1
            except Exception:
                log.exception("snapshot %s analysis failed", row["id"])
                # Mark analyzed anyway so we don't get stuck in a loop on broken images
                self.db.mark_snapshot_analyzed(row["id"])
        for row in self.db.pending_clips(limit=4):
            try:
                if self._analyze_clip(row):
                    n += 1
            except Exception:
                log.exception("clip %s analysis failed", row["id"])
                self.db.mark_clip_analyzed(row["id"], keep=True, label=None)
        return n

    def _analyze_snapshot(self, row: Any) -> bool:
        path = Path(row["path"])
        if not path.exists():
            log.warning("snapshot file missing: %s", path)
            self.db.mark_snapshot_analyzed(row["id"])
            return False
        try:
            image_bytes = path.read_bytes()
        except Exception:
            self.db.mark_snapshot_analyzed(row["id"])
            return False

        prompt = SNAPSHOT_PROMPT.replace("{capture_time}", row["captured_at"])
        parsed = self._call_gemini(prompt, [{"mime_type": "image/jpeg", "data": image_bytes}])
        if not parsed:
            self.db.mark_snapshot_analyzed(row["id"])
            return False

        event_type = parsed.get("event_type") or "unknown"
        narrative = parsed.get("narrative") or "(no description)"
        confidence = _to_float(parsed.get("confidence"))

        self.db.add_event(
            occurred_at=row["captured_at"],
            source="snapshot",
            source_id=row["id"],
            event_type=event_type,
            confidence=confidence,
            narrative=narrative,
            raw_json=json.dumps(parsed),
        )
        self.db.mark_snapshot_analyzed(row["id"])
        log.info("snap %s → %s (%.2f)", path.name, event_type, confidence or 0)
        return True

    def _analyze_clip(self, row: Any) -> bool:
        path = Path(row["path"])
        if not path.exists():
            self.db.mark_clip_analyzed(row["id"], keep=False, label=None)
            return False
        frames = self._sample_clip_frames(path, count=4)
        if not frames:
            self.db.mark_clip_analyzed(row["id"], keep=True, label="unprocessable")
            return False

        # Save first frame as thumbnail
        thumb_rel = Path(row["started_at"][:10]) / f"{path.stem}.jpg"
        thumb_path = self.cfg.thumbnails_dir / "clips" / thumb_rel
        ensure_dir(thumb_path.parent)
        thumb_path.write_bytes(frames[0])

        parts = [{"mime_type": "image/jpeg", "data": f} for f in frames]
        prompt = CLIP_PROMPT.replace("{capture_time}", row["started_at"])
        parsed = self._call_gemini(prompt, parts)
        if not parsed:
            self.db.mark_clip_analyzed(row["id"], keep=True, label=None, thumbnail_path=str(thumb_path))
            return False

        keep = bool(parsed.get("keep", True))
        event_type = parsed.get("event_type") or "unknown"
        label = parsed.get("label")
        narrative = parsed.get("narrative") or "(no description)"
        confidence = _to_float(parsed.get("confidence"))

        self.db.add_event(
            occurred_at=row["started_at"],
            source="clip",
            source_id=row["id"],
            event_type=event_type,
            confidence=confidence,
            narrative=narrative,
            raw_json=json.dumps(parsed),
        )
        self.db.mark_clip_analyzed(row["id"], keep=keep, label=label, thumbnail_path=str(thumb_path))
        log.info("clip %s → %s keep=%s", path.name, event_type, keep)
        return True

    def _sample_clip_frames(self, path: Path, count: int = 4) -> list[bytes]:
        """Sample evenly-spaced frames from the clip via ffmpeg into memory."""
        try:
            duration_out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
                capture_output=True, text=True, timeout=15, check=False,
            )
            duration = float(duration_out.stdout.strip() or 0)
        except Exception:
            duration = 0.0
        if duration <= 0:
            return []

        timestamps = [duration * (i + 0.5) / count for i in range(count)]
        frames: list[bytes] = []
        for ts in timestamps:
            try:
                result = subprocess.run(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                     "-ss", f"{ts:.2f}", "-i", str(path),
                     "-frames:v", "1", "-q:v", "4",
                     "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                    capture_output=True, timeout=20, check=False,
                )
                if result.returncode == 0 and result.stdout:
                    frames.append(result.stdout)
            except Exception:
                continue
        return frames

    def _call_gemini(self, prompt: str, media_parts: list[dict], retries: int = 2) -> Optional[dict]:
        for attempt in range(retries + 1):
            try:
                resp = self.model.generate_content([prompt, *media_parts])
                return _parse_json(resp.text)
            except Exception as e:
                msg = str(e)
                # Soft-retry on transient errors only
                if attempt < retries and any(s in msg for s in ("429", "500", "502", "503", "504", "timeout", "TimeoutError")):
                    time.sleep(2 ** attempt)
                    continue
                log.warning("gemini call failed: %s", msg[:200])
                return None
        return None

    def daily_summary(self, day: Optional[str] = None) -> Optional[str]:
        if not self.enabled:
            return None
        day = day or today_str()
        events = self.db.events_for_day(day)
        if not events:
            return None

        # Pick a featured image: prioritize interesting events from snapshots
        featured_image_path = None
        priority = ["feeding", "chicks_visible", "chick_hatching", "adult_present", "incubating", "eggs_visible"]
        snapshot_events = [e for e in events if e["source"] == "snapshot"]
        for ptype in priority:
            candidates = [e for e in snapshot_events if e["event_type"] == ptype]
            if candidates:
                # Pick the one with highest confidence
                best = max(candidates, key=lambda x: x["confidence"] or 0)
                # Find the snapshot path
                with self.db.connect() as c:
                    row = c.execute("SELECT path FROM snapshots WHERE id = ?", (best["source_id"],)).fetchone()
                    if row:
                        featured_image_path = row["path"]
                        break

        # If no interesting snapshots, just take the middle one of the day
        if not featured_image_path and snapshot_events:
            mid = snapshot_events[len(snapshot_events) // 2]
            with self.db.connect() as c:
                row = c.execute("SELECT path FROM snapshots WHERE id = ?", (mid["source_id"],)).fetchone()
                if row:
                    featured_image_path = row["path"]

        lines = [
            f"- {row['occurred_at'][11:16]} [{row['event_type']}] {row['narrative']}"
            for row in events
        ]
        prompt = DAILY_PROMPT_TEMPLATE.format(day=day, events="\n".join(lines))
        try:
            resp = self.model.generate_content(prompt)
            summary = (resp.text or "").strip()
            if not summary:
                return None

            # Generate biological context paragraph
            bio_context = None
            try:
                bio_prompt = BIO_CONTEXT_PROMPT.format(day=day, summary=summary)
                bio_resp = self.model.generate_content(bio_prompt)
                bio_context = (bio_resp.text or "").strip() or None
            except Exception:
                log.warning("bio context generation failed for %s", day)

            self.db.upsert_daily_summary(
                day=day,
                summary=summary,
                events_count=len(events),
                timelapse_path=None,
                featured_image_path=featured_image_path,
                bio_context=bio_context,
                created_at=now_iso(),
            )
            log.info("daily summary for %s written (%d events)", day, len(events))
            return summary
        except Exception:
            log.exception("daily summary failed")
            return None


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _parse_json(text: Optional[str]) -> Optional[dict]:
    if not text:
        return None
    text = text.strip()
    m = _JSON_FENCE.search(text)
    if m:
        text = m.group(1)
    # Find the first { and last } in case there's leading/trailing chatter
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
