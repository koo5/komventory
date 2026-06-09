# Roadmap

Direction and deferred work. Not a backlog — the small stuff lives in code TODOs
and `data/log/tasks.md`. This is for the decisions worth remembering.

## Frontend rewrite to Svelte

The capture surface (`frontend/app.js`) is a single ~650-line vanilla-JS file. It
has earned its keep as a prototype, but the moment we add a second view it'll
fight us. The plan is to rewrite it in **Svelte** (likely SvelteKit, static
adapter so it stays a single-origin PWA served by the FastAPI app).

What we'd want out of the move:

- Component boundaries for the pieces that are currently tangled imperative DOM:
  the log feed, the status bar, the record/VAD controls, the per-entry promote
  affordance.
- Reactive state instead of the manual `setStatus`/`resetStatus` + `recOn`
  bookkeeping. The "resting status is derived from recording state" idea (already
  factored into `resetStatus()`) becomes a derived store.
- A real place to grow toward multi-view (browse/search, the video layer below,
  per-location filtering) without each feature bolting onto one file.

Non-goals: changing the API surface or the single-origin/no-CORS deployment. The
Svelte app should drop into `/static` exactly as the current one does.

## Video: ambient context, not narration

We sync phone video into `data/inbox/video/` and today do the dumb thing —
`frames.py` extracts one frame every `VIDEO_FRAME_INTERVAL_S` and Whisper
transcribes the audio track. The tempting next step is "always-on narration":
run a vision model over the stream and auto-write log entries. **We decided
against that**, and the reasoning is worth keeping:

**Why not always-on narration.** What's locally feasible today:

- Object detection (YOLO, GroundingDINO): "there's a screwdriver in frame." Solid.
- Generic captioning (Moondream-2B, Florence-2, SmolVLM): "a person holding a
  tool near a shelf." A few FPS on modest CPU/GPU, and reads as noise ~90% of
  the time.
- "X placed in Y" event detection: **not a solved problem.** Action-recognition
  models won't reliably tell *your* bins apart without per-item training.

The real failure mode is signal-to-noise. Always-on narration floods `log.md`
with "person walking, no change, hand visible" for every one useful "vrtačka v
červené přepravce" — which directly attacks the property we've protected hardest:
an append-only, hand-curated, `chmod 444` log of real prose.

**The direction instead — video as ambient context, keyed off the audio note.**

- Record/keep video, indexed by timestamp. Don't auto-narrate, don't auto-emit.
- When the audio-note pipeline fires (you actually spoke), grab the *synchronized*
  frame or short clip. That's `frames.py`'s job extended from "finished MP4s" to
  a live tail.
- A vision model runs **only** on those grabbed clips, paired with what you just
  said. "Vrtačka do bedny" + a frame of it happening = one high-quality entry
  with visual evidence attached — not a hundred junk ones.
- For "what did I do yesterday" curiosity mode, let the LLM pull frames from the
  indexed archive on demand. Lazy, not eager.

This is also the smaller engineering ask: extend frame extraction to a live
stream triggered by the audio surface, rather than build always-on inference.

A point in our favor for later: because we capture video *with* audio, and the
log is something we can prune / filter / refine / run QA over, we're not forced
to get detection perfect up front — noisy candidate layers can be distilled into
the curated log rather than dumped straight into it.

**Skipped definitively: Rhasspy.** Its sweet spot is fixed-vocabulary intents
("turn off the lights"). Free-form Czech notes plus open-domain questions about
your stuff is the opposite shape; nothing in it earns its weight here.

## Parking lot

Smaller deferred items, lower commitment:

- **openclaw (phase 2):** a sandboxed reasoning VM that reads `data/log/log.md`
  and drops new `*.note.md` into `data/inbox/openclaw/`. The inbox path and
  ingest already exist; the VM does not.
- **Google Doc import:** one-time ingest of the existing `inventory_final.md`
  (`komventory import-gdoc <path>` — wired but flagged TODO in the README).
- **DNS-01 / public hostname:** the LAN PWA runs behind Caddy with an internal
  TLS cert; a real `jj.hillview.cz` cert via DNS-01 is deferred.
