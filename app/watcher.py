"""Once-daily nest watch for the abandoned-clutch phase.

The main capture/analyze/recorder pipeline is paused. This module runs a
single Gemini classification on a fresh snapshot once per day and fires
an ntfy alert if anything has changed from the abandoned-but-intact baseline
(i.e. the nest is no longer simply "five sky-blue eggs unattended").
"""

from __future__ import annotations

import logging
from pathlib import Path

from .analyzer import Analyzer
from .capture import capture_snapshot
from .config import Config
from .db import Database
from .monitor import send_ntfy

log = logging.getLogger(__name__)

# event_types that indicate "nothing has changed" — just unattended eggs / empty cup.
_UNCHANGED = {"eggs_visible", "empty", "unknown"}


def daily_nest_check(cfg: Config, db: Database) -> None:
    """Capture one snapshot, analyse it, alert on any change from baseline."""
    log.info("daily nest check: capturing snapshot")
    capture_snapshot(cfg, db)
    row = db.latest_snapshot()
    if not row or row["analyzed"]:
        # nothing to analyze (e.g. capture failed)
        log.warning("no fresh snapshot to analyze")
        return

    analyzer = Analyzer(cfg, db)
    if not analyzer.enabled:
        log.warning("analyzer disabled (no GEMINI_API_KEY); skipping nest-check analysis")
        return

    analyzer._analyze_snapshot(row)

    # Re-read so we have narrative/event_type from the just-inserted event
    with db.connect() as c:
        ev = c.execute(
            "SELECT event_type, narrative, confidence FROM events "
            "WHERE source = 'snapshot' AND source_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()

    if not ev:
        log.warning("snapshot analysed but no event row was written")
        return

    event_type = ev["event_type"]
    narrative = ev["narrative"]
    conf = ev["confidence"] or 0.0

    if event_type in _UNCHANGED:
        log.info("daily nest check: no change (%s, conf=%.2f)", event_type, conf)
        # quiet confirmation so the user knows the check ran
        send_ntfy(
            cfg,
            title="Redtail · daily nest check",
            message=f"No change at the nest. ({event_type})",
            priority="min",
            tags=["mag_right"],
        )
        return

    # Something is different — alert with high priority.
    log.warning("daily nest check: CHANGE DETECTED (%s)", event_type)
    send_ntfy(
        cfg,
        title="Redtail · change at the nest!",
        message=f"{event_type}: {narrative}",
        priority="high",
        tags=["bird"],
    )
