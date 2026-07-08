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
  # websockets. livekit-api pulls livekit-protocol/protobuf/pyjwt.
  "$VENV_DIR/bin/pip" install --quiet livekit-api websockets
fi

# --- Run (pass through any CLI args, e.g. -v / --insecure-tls / --timeout) ----
exec "$VENV_DIR/bin/python" "$PY_SCRIPT" "$@"
