# komventory

Personal/community inventory tool. A timestamped, append-only event log (`data/log/log.md`) fed by:

- **Hand-written notes** dropped into `data/inbox/openclaw/` (or any inbox subdir) as `.md`/`.txt`.
- **Phone audio** synced via Syncthing into `data/inbox/audio/` → transcribed by Whisper.
- **Phone video** synced via Syncthing into `data/inbox/video/` → frames every 5s + Whisper on the audio track.
- **One-time Google Doc import** of the existing `inventory_final.md` (TODO: `komventory import-gdoc <path>`).
- **Later: a sandboxed reasoning VM ("openclaw")** that reads `data/log/log.md` and drops new notes into `data/inbox/openclaw/`.

See [ROADMAP.md](ROADMAP.md) for where this is headed (Svelte frontend rewrite, the video-as-ambient-context plan, and other deferred work).

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

## Configuration

Out of the box the defaults are **self-contained**: all state lives under `./data`,
audio/video are read from `./data/inbox/{audio,video}`, and the watcher commits with
a baked-in git identity — so a fresh clone runs with no external setup. Two gitignored
files tailor it to your machine:

```sh
cp .env.example .env                              # scalar settings
cp compose.override.yml.example compose.override.yml   # host bind mounts
```

- **`.env`** — scalar knobs compose substitutes in: `KOMVENTORY_LANG`, the Whisper
  and Q&A models, a `GEMINI_API_KEY` for the Q&A feature, and `KOMVENTORY_UIDGID`
  (set to your `id -u`:`id -g` if you're not `1000:1000`).
- **`compose.override.yml`** — host-specific bind mounts, auto-merged by docker
  compose: point the containers at your real phone-sync folders (overlaid onto
  `/data/inbox/{audio,video}`), your own gitconfig, a key file, or a peer log clone.

Nothing under `data/` needs creating by hand: `data/log/` and `log.md` are made on
the first note, and `data/log/` is **auto-initialised as a git repo** so every
entry is versioned from the start. Host-to-host sync stays opt-in — add a remote
to `data/log/` (e.g. a peer clone mounted via `compose.override.yml`) and the
watcher pulls/pushes through it.

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

## Language

The UI chrome (buttons, status text) is always English. The **content** language —
LLM system prompt and answers, the question-detection heuristic, the Whisper
transcription language, and the default TTS voice — is a single switch,
`KOMVENTORY_LANG` (`en` default, or `cs`). `docker compose` reads it from a
gitignored `.env`:

```sh
cp .env.example .env      # then set KOMVENTORY_LANG=cs for Czech
```

Adding a language is one `LangPack` in `src/komventory/lang.py`. Voice selection
is independent of language — `KOMVENTORY_TTS_VOICE` overrides the language's
default Piper voice.

## PWA (note-capture surface)

`komventory-api` (compose service) serves a single-page web app on **port 3411**:

- Type a note or hit **🎙 record** to start continuous VAD-chunked recording (Silero VAD in the browser).
- Each detected utterance posts to `/api/notes/audio` → Whisper → entry in `log.md`.
- "speak back" reads transcripts back via Piper. The voice defaults to the content language's (`en_US-lessac-medium` for en, `cs_CZ-jirka-medium` for cs), auto-downloaded on first use; override with `KOMVENTORY_TTS_VOICE` — e.g. `thomcles-medium`/`thomcles-high`, a [Czech jirka fine-tune](https://huggingface.co/Thomcles/Piper-TTS-Czech).
- "answer questions" routes utterances that look like questions through `/api/ask` → the configured LLM (`KOMVENTORY_QA_MODEL`, default `gemini/gemini-2.5-flash`), grounded on `log.md`.

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

Default `large-v3`, multilingual. The transcription *language* follows `KOMVENTORY_LANG` (see [Language](#language)). On CPU with `int8` expect ~1–3× realtime; a backlog of phone videos can take hours. For faster (lower quality) runs set `KOMVENTORY_WHISPER_MODEL=medium`/`small`/`base` in `.env`, or `KOMVENTORY_WHISPER_DEVICE=cuda` if you have a GPU.

### HF finetunes (better per-language accuracy)

Set `KOMVENTORY_WHISPER_MODEL` in `.env` to a Hugging Face finetune (e.g. `mikr/whisper-large-v3-czech-cv13`) for better orthography. These ship as plain Transformers checkpoints, so they need a one-time CTranslate2 conversion that pulls in **torch** — which the container image deliberately omits to stay slim. So you build the CT2 cache **once on the host**, and the container reads it via the bind-mounted `data/cache/whisper/ct2/`:

```fish
scripts/warm-whisper-cache.fish        # reads KOMVENTORY_WHISPER_MODEL from .env
# equivalently: uv run --extra convert komventory convert-model mikr/whisper-large-v3-czech-cv13
```

After it finishes, (re)start the container — it picks the converted model up automatically. If you point the container at an unconverted finetune, transcription fails fast with a message telling you to run the script (it can't self-convert: no torch in the image). Built-in models like `large-v3` need none of this — they auto-download in CT2 form. A host-side `komventory ingest` also auto-converts on first use, since the host has the converter on `PATH`.
