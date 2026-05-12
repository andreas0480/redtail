---
description: Review the last ~24h of redtail AI events, correct anomalies in the production DB
---

You are doing a daily quality-control pass on the Common Redstart nest box event log running on .30.103.

**Context to keep in mind:**
- This is a Common Redstart (*Phoenicurus phoenicurus*), NOT a kestrel.
- First egg laid **2026-05-08**. One egg per day: May 8=1st, May 9=2nd, May 10=3rd, May 11=4th, May 12=5th. Clutch still growing.
- Incubation lasts 12-14 days from the last egg. Earliest plausible hatch is **2026-05-28** (conservative).
- Any "chicks_visible", "chick_hatching", or "feeding" event dated before 2026-05-28 is biologically impossible — must be corrected.
- Any event claiming eggs before 2026-05-08 is a false detection — the female was preparing the nest.
- Adult on the nest (brown feathered mass covering the cup, no visible eyes/beak) = `incubating`, not `chicks_visible`.
- Be conservative with corrections — preserve uncertainty where it's genuine.

**Workflow:**

1. Run the prepare phase:

```
cd /home/belitz/redtail && python review.py prepare --since 36 --out ./review_packet
```

This pulls suspect events (low confidence, biologically impossible categories, intruders) into `./review_packet/`, with the underlying images at `./review_packet/images/event_<id>_<filename>.jpg`.

2. Read `./review_packet/manifest.json` to see what's flagged and why.

3. For each suspect event, use the Read tool on its image (Read can display JPEGs). Look at the actual frame. Decide if the AI got it right.

4. Compose `./review_packet/patch.json` — a JSON array. Each element is one of:

   ```json
   {"event_id": 123, "set": {"event_type": "incubating", "narrative": "Adult sits on the nest, eggs hidden under her body.", "confidence": 0.95}}
   ```
   or
   ```json
   {"event_id": 124, "delete": true}
   ```

   You may include `"day": "2026-05-04"` on any patch to flag the day for daily-summary rewrite.

5. Apply the patch:

```
cd /home/belitz/redtail && python review.py apply ./review_packet/patch.json
```

This updates the production DB on .30.103 and regenerates the daily summary for any affected days.

6. Finish with a short note summarizing what was changed (counts by category, anything biologically notable).

**Heuristics for corrections:**
- `chicks_visible` or `feeding` before 2026-05-28 → almost always actually `incubating` (if adult body is in the cup) or `eggs_visible` (if the cup is clear)
- `intruder` → check carefully; if it's just a Redstart adult, change to `adult_present` or `adult_arrives`
- `empty` where the image shows even partial bird → `adult_present`
- Very low confidence (<0.5) on `unknown` is fine, leave it

If something is genuinely ambiguous from the image, leave it alone — don't manufacture certainty.
