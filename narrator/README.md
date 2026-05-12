# Narrator — Attenborough-voiced journal narrations

Generates an MP3 audio narration of each daily journal entry using XTTS-v2
fine-tuned on Sir David Attenborough audiobook audio. Runs on the GPU
machine (not in the production container) once per day after the daily
summary is written.

## Notes on licensing

XTTS-v2 is non-commercial only (Coqui Public Model License). The
Attenborough voice clone is an unauthorized community fine-tune: David
Attenborough has publicly objected to AI clones of his voice. Use is
personal/hobby only.

## Setup

Requires an NVIDIA GPU with CUDA 12.1+ and Python 3.11.

```bash
cd narrator
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python torch torchaudio --index-url https://download.pytorch.org/whl/cu121
uv pip install --python .venv/bin/python coqui-tts huggingface_hub "transformers>=4.50,<4.55"

# Download the model (~2 GB)
.venv/bin/python -c "from huggingface_hub import snapshot_download; \
  snapshot_download('drewThomasson/xtts_David_Attenborough_fine_tune', \
                    local_dir='model')"

# Extract the weights and the vocab file
cd model && unzip -j Finished_model_files.zip "Finished_model_files/model.pth" -d Finished_model_files/
cp Finished_model_files/vocab.json_ Finished_model_files/vocab.json
```

## Usage

```bash
export REDTAIL_HOST=192.168.30.103    # production host
export COQUI_TOS_AGREED=1              # required by Coqui

# All pending days (no narration yet)
.venv/bin/python narrate.py

# Specific day
.venv/bin/python narrate.py --day 2026-05-12

# Force re-narration
.venv/bin/python narrate.py --day 2026-05-12 --force

# Dry-run
.venv/bin/python narrate.py --dry-run
```

## Cron

A daily cron job runs at 00:30 (after the 23:55 summary):

```
30 0 * * * COQUI_TOS_AGREED=1 REDTAIL_HOST=192.168.30.103 /home/belitz/redtail/narrator/.venv/bin/python /home/belitz/redtail/narrator/narrate.py >> /home/belitz/redtail/narrator/cron.log 2>&1
```

## How it works

1. SSH into the production host and query the production DB for
   `daily_summaries` rows where `narration_path IS NULL`.
2. For each, run XTTS-v2 with the Attenborough fine-tune on the GPU,
   chunking long summaries by sentence (XTTS has a 400-token limit per
   synthesis call).
3. Encode the resulting WAV to MP3 (`ffmpeg -q:a 5`, ~150 KB/min).
4. SCP the MP3 to the production host, `docker cp` it into the container's
   `/data/narrations/<day>.mp3`.
5. Update `daily_summaries.narration_path` over SSH.

The production app serves the MP3 at `/media/narrations/<day>.mp3` and the
journal page renders an `<audio>` player below each entry.

## Performance

On an RTX 4070:
- Model load: ~15 s (one-time per invocation)
- Synthesis: ~6–15 s per daily summary (depends on length and chunk count)
- MP3 encode: <1 s
- Total for 10 backfilled days: ~2 minutes
