# komventory

Personal/community inventory tool. A timestamped, append-only event log (`data/log/log.md`) fed by:

- **Hand-written notes** dropped into `data/inbox/openclaw/` (or any inbox subdir) as `.md`/`.txt`.
- **Phone audio** synced via Syncthing into `data/inbox/audio/` → transcribed by Whisper.
- **Phone video** synced via Syncthing into `data/inbox/video/` → frames every 5s + Whisper on the audio track.
- **One-time Google Doc import** of the existing `inventory_final.md` (TODO: `komventory import-gdoc <path>`).
- **Later: a sandboxed reasoning VM ("openclaw")** that reads `data/log/log.md` and drops new notes into `data/inbox/openclaw/`.

Entries look like:

```markdown
## 2026-05-16T20:42:11+02:00 — source: whisper@audio/20260516-2041.m4a — loc: "Nářadí přepravka 1"

Štětec, šroubováky, řezák malý…

![[media/audio/20260516-2041.m4a]]
```

`loc:` is optional. `source:` tags the origin so a note can be regenerated (e.g. re-run Whisper with a better model) without losing track of which entry replaced which.

## Layout

```
data/
  log/log.md              source of truth, append-only
  log/media/              attachments referenced by entries
  inbox/audio/            Syncthing target for phone audio
  inbox/video/            Syncthing target for phone video
  inbox/openclaw/         (phase 2) VM drops *.note.md here
  inbox/imports/          drop the Google Doc export here
  cache/whisper/          downloaded Whisper models (persists across rebuilds)
src/komventory/           the package
docker/Dockerfile         uv + ffmpeg + faster-whisper
compose.yml               default service: `komventory watch`
```

## Running

```sh
# Build once (uv sync inside the container; first run pulls the Whisper model).
docker compose build

# Run the watcher in the foreground.
docker compose up

# One-shot sweep of inbox/ (no watcher).
docker compose run --rm komventory komventory ingest

# Process one specific file.
docker compose run --rm komventory komventory ingest /data/inbox/audio/foo.m4a

# Print resolved paths (useful when debugging bind mounts).
docker compose run --rm komventory komventory paths
```

## Local dev without Docker

```sh
uv sync
uv run komventory paths
```

## PWA (note-capture surface)

`komventory-api` (compose service) serves a single-page web app on **port 3411**:

- Type a note or hit **🎙 záznam** to start continuous VAD-chunked recording (Silero VAD in the browser).
- Each detected utterance posts to `/api/notes/audio` → Whisper → entry in `log.md`.
- "mluvit zpět" reads transcripts back in Czech (Piper, `cs_CZ-jirka-medium`, auto-downloaded on first use).
- "odpovídat na otázky" routes utterances that look like questions through `/api/ask` (LLM is **stubbed** for v1 — wire a real backend in `src/komventory/qa.py:_call_llm`).

Mobile mic requires HTTPS. Reverse-proxy from your existing Caddy (or any HTTPS front), passing the SSE endpoint through unbuffered:

```caddyfile
inv.example.org {
    reverse_proxy <docker-host>:3411 {
        # SSE: flush each event immediately.
        flush_interval -1
    }
}
```

The PWA and the API share one origin (Caddy fronts both `/` and `/api/*` at the same hostname) — no CORS, no mixed content.

## Whisper model

Default `large-v3`, multilingual, pinned to Czech (`KOMVENTORY_WHISPER_LANG=cs`). On CPU with `int8` expect ~1–3× realtime; a backlog of phone videos can take hours. Override in `compose.yml` to `medium`/`small`/`base` for faster (lower quality) runs, or set `KOMVENTORY_WHISPER_DEVICE=cuda` if you have a GPU.
