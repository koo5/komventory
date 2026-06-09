#!/usr/bin/env fish
# Pre-build the CTranslate2 cache for an HF Whisper finetune ON THE HOST.
#
# The containerised watcher/api deliberately ship without torch (see
# model_convert.py), so they can't convert HF finetunes themselves — they only
# read the converted model from the bind-mounted data/cache/whisper/ct2/ dir.
# This script does that one-time host-side build (pulling torch via the
# `convert` extra), after which the container picks the model up automatically.
#
# Usage:
#   scripts/warm-whisper-cache.fish [model]
#
# With no arg it reads KOMVENTORY_WHISPER_MODEL from .env. Built-in multilingual
# models (e.g. large-v3 — no "/" in the name) need no conversion and are skipped.

set -l repo (cd (dirname (status filename))/..; and pwd)
cd $repo

set -l model $argv[1]
if test -z "$model"; and test -f .env
    # Grab the last uncommented KOMVENTORY_WHISPER_MODEL=... line, strip quotes.
    set model (grep -E '^[[:space:]]*KOMVENTORY_WHISPER_MODEL=' .env | tail -n1 \
        | string replace -r '^[^=]*=' '' | string trim --chars=' "\'')
end
if test -z "$model"
    set model large-v3
end

if not string match -q '*/*' -- "$model"
    echo "Model '$model' is a built-in CT2 model — nothing to convert."
    exit 0
end

echo "Converting $model → data/cache/whisper/ct2/ (one-time; pulls torch via --extra convert)…"
uv run --extra convert komventory convert-model "$model"
