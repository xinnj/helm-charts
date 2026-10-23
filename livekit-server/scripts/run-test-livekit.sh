#!/usr/bin/env bash
#
# Set up a Python venv, load connection details, and run the LiveKit + STUN/TURN
# connectivity test (test-livekit.py).
#
# Connection details (never hard-coded here) come from the environment or a local
# env file. Priority:
#   1. Variables already exported in your shell.
#   2. An env file: $ENV_FILE (default: <this dir>/.env). Copy .env.example.
#
# Usage:
#   ./run-test-livekit.sh [args passed through to test-livekit.py]
#   ./run-test-livekit.sh -v
#   ENV_FILE=/path/to/qa.env ./run-test-livekit.sh --timeout 8
#   LIVEKIT_URL=wss://host LIVEKIT_API_KEY=k LIVEKIT_API_SECRET=s ./run-test-livekit.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_SCRIPT="$SCRIPT_DIR/test-livekit.py"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
ENV_FILE="${ENV_FILE:-$SCRIPT_DIR/.env}"

# --- Load connection details from an env file if present ---------------------
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi

# --- Validate required config (do NOT put secrets in this script) ------------
missing=()
[ -n "${LIVEKIT_URL:-}" ]        || missing+=("LIVEKIT_URL")
[ -n "${LIVEKIT_API_KEY:-}" ]    || missing+=("LIVEKIT_API_KEY")
[ -n "${LIVEKIT_API_SECRET:-}" ] || missing+=("LIVEKIT_API_SECRET")
if [ "${#missing[@]}" -ne 0 ]; then
  echo "Missing required env var(s): ${missing[*]}" >&2
  echo "Export them, or create '$ENV_FILE' (copy .env.example)." >&2
  exit 2
fi

# --- Create the venv and install deps once -----------------------------------
if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "Creating venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  # test-livekit.py needs livekit.api (token) + livekit.protocol (protobuf) +
  # websockets for phases 1-4, aioice for the phase 5 ICE check, aiortc for
  # phases 6-7, and playwright for the phase 8 real-browser check.
  "$VENV_DIR/bin/pip" install --quiet livekit-api websockets \
    "aioice>=0.10,<1" aiortc "playwright>=1.64,<2"
fi

# A venv created by an older version of this script may lack a dependency.
if ! "$VENV_DIR/bin/python" -c "import aioice" >/dev/null 2>&1; then
  echo "Installing missing dependency aioice ..."
  "$VENV_DIR/bin/pip" install --quiet "aioice>=0.10,<1"
fi
if ! "$VENV_DIR/bin/python" -c "import aiortc" >/dev/null 2>&1; then
  echo "Installing missing dependency aiortc ..."
  "$VENV_DIR/bin/pip" install --quiet aiortc
fi
if ! "$VENV_DIR/bin/python" -c "import playwright" >/dev/null 2>&1; then
  echo "Installing missing dependency playwright ..."
  if ! "$VENV_DIR/bin/pip" install --quiet "playwright>=1.64,<2"; then
    echo "warning: playwright install failed; Phase 8 will be skipped" >&2
  fi
fi

# Phase 8 runs the official livekit-client SDK in Chrome. Serve a pinned local
# bundle; the pinned CDN is only a fallback when npm is unavailable.
BROWSER_DIR="$SCRIPT_DIR/browser"
BROWSER_SDK="$BROWSER_DIR/node_modules/livekit-client/dist/livekit-client.esm.mjs"
if [ ! -f "$BROWSER_SDK" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "Installing browser dependency livekit-client ..."
    if ! (cd "$BROWSER_DIR" && npm ci --no-audit --no-fund --loglevel=error); then
      echo "warning: npm ci failed; Phase 8 will try the pinned CDN" >&2
    fi
  else
    echo "warning: npm not found; Phase 8 will try the pinned CDN" >&2
  fi
fi

# --- Run (pass through any CLI args, e.g. -v / --insecure-tls / --timeout) ----
exec "$VENV_DIR/bin/python" "$PY_SCRIPT" "$@"
