# The AI pipeline

This document describes how Redtail uses language models — what prompts go
where, why they look the way they do, and how the **critic pass** keeps
generation honest.

## Why three different models could be involved

Three distinct generation tasks, three different cost/quality trade-offs:

| Task | What it does | Volume | Model |
|---|---|---|---|
| **Per-frame classification** | Turn one JPEG into an event row | ~280 calls/day | Gemini 2.5 Flash (cheap, fast) |
| **Daily summary + bio context** | Write the day's journal entry | ~9 calls/day (3 h cadence × 3 phases) | Gemini 2.5 Flash |
| **Critic review** | Approve or rewrite the generated entry | 2 × the above | Configurable — could be a stronger model |

The critic is *deliberately* allowed to use a different (stronger / more
expensive) provider. The pattern is: **cheap model produces, strong model
reviews.** Both calls go through the same `_critique_and_fix()` interface,
so swapping the critic between Gemini, Anthropic, and OpenAI is a one-line
env var change.

## Snapshot classification

```
JPEG bytes + SNAPSHOT_PROMPT  ──►  Gemini 2.5 Flash  ──►  JSON event
```

The prompt (`SNAPSHOT_PROMPT` in `app/analyzer.py`) is built up over the
season as the AI's failure modes were observed. Key sections:

- **Species clarification**: explicitly states the bird is a Common Redstart
  (a small passerine), not any of the species Gemini's prior is biased
  toward (falcon, kestrel, etc.).
- **Timeline facts**: first egg date, hatch date, per-day max egg count
  computed from the date. Used to constrain implausible classifications
  (you can't see chicks before the hatch date; you can't see five eggs on
  day two).
- **Visual cue catalogue**: for each `event_type`, a one-sentence description
  of what it actually looks like in this nest box's camera. The hardest
  disambiguation is `incubating` vs `chicks_visible` — both can look like
  a blurry brown mass — so the prompt explicitly tells the model to
  default to `incubating`.
- **Strict counting rules**: when an adult is on the nest, eggs and chicks
  are zero (because hidden, not visible). Numbers in the JSON output must
  come from what's plainly visible, never inferred.

### Output contract

Strict JSON, no markdown fences (the parser tolerates fences anyway):

```json
{
  "event_type": "incubating",
  "confidence": 0.92,
  "narrative": "The adult sits low in the cup with its rufous tail visible.",
  "subjects": {"adults": 1, "eggs": 0, "chicks": 0},
  "notable": null
}
```

`event_type` ∈ `{empty, adult_present, adult_arrives, adult_leaves,
eggs_visible, incubating, feeding, chicks_visible, chick_hatching, intruder,
unknown}`.

## Clip classification

Same idea, but the input is a bundle of 4 frames sampled evenly across the
clip duration. The prompt (`CLIP_PROMPT`) is shorter than the snapshot one
(the visual cues are the same) and asks for one additional field —
`label`, a 3–6 word human-readable title used for the gallery thumbnail —
plus a `keep` boolean for filtering false triggers (pure light/shadow
flicker with no bird).

```python
parts = [{"mime_type": "image/jpeg", "data": frame_bytes} for frame_bytes in frames]
prompt = CLIP_PROMPT.replace("{capture_time}", row["started_at"])
parsed = self._call_gemini(prompt, parts)
```

## Daily summary

Two LLM calls per execution, both targeted at writing prose for humans:

### 1. `DAILY_PROMPT_TEMPLATE` → summary

The day's events are formatted as a bulleted list with **local-time**
timestamps:

```
- 06:10 [adult_arrives] An adult sweeps into the box and settles into the cup.
- 06:44 [adult_leaves] The bird departs after a short brooding session.
- 08:20 [incubating] An adult is settled low across the eggs.
…
```

The prompt then asks for a warm, naturalist's 3–5 sentence journal entry.
**Three iterations of failure-mode discovery** are baked in:

1. **No location/timezone leaks.** Early versions told the model to "convert
   to Stockholm time" — and got "Throughout the day in Stockholm…" in every
   entry. Now the timestamps are converted to local in Python before they
   reach the prompt; the prompt never mentions a timezone.
2. **No meta-commentary.** The model used to write things like *"the AI's
   count fluctuated to six or seven eggs"* — debugging language that
   doesn't belong in a field journal. The prompt now lists those phrases
   explicitly and tells the model not to use them.
3. **Ground truth applied silently.** The prompt states facts like "the
   clutch is currently five eggs" but instructs the model to *apply them
   silently*, never state them as a reason for an interpretation.

### 2. `BIO_CONTEXT_PROMPT` → bio footnote

Run on the (already-approved) summary. Asks for 2–3 sentences of species
biology that *illuminate* what happened — not restate it. Same voice
constraints as the summary.

### 3. Critic pass

Each output flows through `_critique_and_fix(text, mode)`:

```
generated text  ──►  CRITIC_PROMPT (provider-configurable)
                       │
                       ▼
                "OK"  →  keep as-is
                "REVISE:\n<text>"  →  use the rewrite
                anything else  →  keep original (fail-open)
```

`CRITIC_PROMPT` is a single, explicit rule catalogue:

| Category | Examples |
|---|---|
| Forbidden words | "AI", "model", "camera", "miscount", "misidentified", "discrepancy", "monitoring" |
| Forbidden topics | location names, timezones, the feather hallucination, clock-time readings |
| Forbidden format | leading "Date:", "Day:", bare date prefix, multi-paragraph |
| Ground truth | egg count, abandonment date, hatch date |
| Voice | three-to-five-sentence (summary) or two-to-three-sentence (bio) naturalist field-journal |

The critic returns one of exactly two responses, no third option:

```
OK
```

or

```
REVISE:
<the corrected text, possibly entirely rewritten>
```

This is the **simplest possible contract** that a fail-open parser can
implement, and it works reliably across all three providers.

### Provider abstraction

`Analyzer._critic_generate(prompt) -> str` dispatches based on
`cfg.critic_provider`:

```python
if provider == "anthropic":
    import anthropic                    # lazy
    client = anthropic.Anthropic(api_key=...)
    msg = client.messages.create(model=..., messages=[...])
    return "".join(b.text for b in msg.content)

elif provider == "openai":
    from openai import OpenAI           # lazy
    client = OpenAI(api_key=...)
    resp = client.chat.completions.create(model=..., messages=[...])
    return resp.choices[0].message.content or ""

else:  # default: gemini
    return (self._gemini_critic_model or self.model).generate_content(prompt).text or ""
```

Three properties of this abstraction:

1. **Lazy imports.** If you never set `CRITIC_PROVIDER=anthropic`, the
   `anthropic` SDK is in `requirements.txt` but never actually loaded
   into memory.
2. **Fail-open.** Any exception inside `_critique_and_fix` is caught and
   the original text is kept. Adding the critic can only improve quality,
   never break a generation.
3. **Same prompt for all three.** `CRITIC_PROMPT` is provider-neutral.
   Same rules, same expected `OK` / `REVISE:` output. No per-vendor
   quirks creep into the rule catalogue.

## Snapshot of failure modes the catalogue catches

These are the regressions I have personally caught while running the
system. Each one is now in the prompt **and** the critic, so the next
occurrence is fixed at generation time without a human in the loop.

| Failure | Example output | Root cause | Where it's now caught |
|---|---|---|---|
| Date heading | `Date: 2026-05-13\n\n…` | Prompt repeated the date variable | `_strip_heading()` in code + critic |
| Bare date heading | `May 13, 2026:\n\n…` | Model "helpfully" added a heading | Same |
| Timezone leak | `Throughout the daylight hours in Stockholm…` | Prompt named the timezone | Pre-converted in Python, prompt no longer mentions tz |
| Meta-AI commentary | `…despite the AI's count occasionally fluctuating…` | The model surfaced its own uncertainty | Forbidden words list in prompt + critic |
| Feather hallucination | `…a sixth sky-blue egg…` (was a feather) | Visual ambiguity | Hard cap in summary prompt; critic re-clamps |
| Clutch-too-large | `…seven eggs visible…` | Same; also overcounting in cluster | Critic clamps to 5 |
| Chicks before hatch date | `…the chicks gaped for food…` (pre-laying) | Biologically impossible | Forbidden in snapshot prompt; critic rejects |
| Clock-time readings | `at 04:00, around 13:21` | Verbatim from the event log | Critic asks for natural phrasings |
| Multi-paragraph | Stray double newlines | Model formatting drift | Critic forbids |

## Cost notes

At the default Gemini 2.5 Flash pricing (mid-2026):

| Daily volume | Approximate cost |
|---|---|
| ~288 snapshot classifications | €0.02 |
| ~30 clip classifications | €0.01 |
| 1 final summary + 3 refresh summaries × 2 (summary + bio) | €0.005 |
| 2 critic passes per summary cycle | €0.005 |
| **Total** | **~€0.04 / day, ~€1.20 / month** |

Switching the critic to a stronger model (`claude-sonnet-4-5`, `gpt-4o`)
adds maybe €0.02/day for the same volume — well worth it for a project
where the journal is the main deliverable. Switching the primary
classifier to a heavier model would 5–10× the bill; the critic pattern
gives you the quality of a strong model on the *visible output* while
keeping the classifier cheap.
