#!/usr/bin/env fish
# Thin wrapper around curl for the komventory API. Lets you pre-authorize
# this one script in Claude Code permissions instead of blanket-allowing curl.
#
# Usage:
#   scripts/komv-test.fish <subcommand> [args...]
#
# Subcommands (any flag starting with -- after the subcommand is passed to curl):
#   recent [n]                 GET /api/log/recent?n=N             (default 10)
#   text  "<body>"             POST /api/notes/text
#   ask   "<text>" [anchor]    POST /api/ask                       (anchor optional)
#   tts   "<text>" [outfile]   POST /api/tts → outfile             (default /tmp/komv-tts.wav)
#   audio <wav-path>           POST /api/notes/audio (multipart)
#   promote <ts> <source>      POST /api/promote
#   ping                       Liveness check (HEAD /).
#   ls                         List available subcommands.
#
# Env:
#   KOMV_API   base URL                                            (default http://127.0.0.1:3411)
#   KOMV_RAW   if set, skip jq pretty-print                        (raw curl output)

set -g base (set -q KOMV_API; and echo $KOMV_API; or echo "http://127.0.0.1:3411")
set -l sub $argv[1]
set -e argv[1]

function _pretty
    if set -q KOMV_RAW
        cat
    else if command -v jq >/dev/null
        jq .
    else
        cat
    end
end

function _post_stdin
    # Pipe JSON body through stdin to avoid fish word-splitting traps with
    # embedded newlines / quotes. Endpoint path is $argv[1].
    curl -sS -X POST "$base$argv[1]" \
        -H "content-type: application/json" \
        -d @- | _pretty
end

switch "$sub"
    case recent
        set -l n (set -q argv[1]; and echo $argv[1]; or echo 10)
        curl -sS "$base/api/log/recent?n=$n" | _pretty

    case text
        if test -z "$argv[1]"
            echo "usage: komv-test text \"<body>\"" >&2
            exit 2
        end
        echo $argv[1] | jq -Rs '{body: .}' | _post_stdin /api/notes/text

    case ask
        if test -z "$argv[1]"
            echo "usage: komv-test ask \"<text>\" [anchor_source]" >&2
            exit 2
        end
        if test -n "$argv[2]"
            echo $argv[1] | jq -Rs --arg a "$argv[2]" '{text: ., anchor_source: $a}' | _post_stdin /api/ask
        else
            echo $argv[1] | jq -Rs '{text: .}' | _post_stdin /api/ask
        end

    case tts
        if test -z "$argv[1]"
            echo "usage: komv-test tts \"<text>\" [outfile]" >&2
            exit 2
        end
        set -l out (set -q argv[2]; and echo $argv[2]; or echo /tmp/komv-tts.wav)
        echo $argv[1] | jq -Rs '{text: .}' | \
            curl -sS -X POST "$base/api/tts" -H "content-type: application/json" -d @- -o "$out"
        echo "wrote $out ("(stat -c %s "$out" 2>/dev/null)" bytes)"

    case audio
        if test -z "$argv[1]"
            echo "usage: komv-test audio <wav-path>" >&2
            exit 2
        end
        if not test -f "$argv[1]"
            echo "no such file: $argv[1]" >&2
            exit 2
        end
        curl -sS -X POST "$base/api/notes/audio" -F "file=@$argv[1]" | _pretty

    case promote
        if test -z "$argv[1]" -o -z "$argv[2]"
            echo "usage: komv-test promote <timestamp> <source>" >&2
            exit 2
        end
        jq -n --arg t "$argv[1]" --arg s "$argv[2]" '{timestamp: $t, source: $s}' | \
            _post_stdin /api/promote

    case ping
        curl -sS -o /dev/null -w "status=%{http_code} time=%{time_total}s\n" "$base/api/log/recent?n=1"

    case ls ''
        echo "subcommands: recent text ask tts audio promote ping"
        echo "base url:    $base"

    case '*'
        echo "unknown subcommand: $sub" >&2
        echo "subcommands: recent text ask tts audio promote ping" >&2
        exit 2
end
