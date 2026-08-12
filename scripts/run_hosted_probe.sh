#!/bin/sh
# Run scripts/probe_local_grounder.py against a HOSTED OpenAI-compatible
# endpoint (default: Together AI) without ever exposing the API key.
#
# The key is sourced at RUNTIME from an operator-named env file and travels
# only via the environment into the probe process. This script never prints
# it; do not add any echo of the environment here.
#
# Usage:
#   OPENADAPT_ENV_FILE=/path/to/.env \
#   scripts/run_hosted_probe.sh MODEL_ID OUT_JSON [PRICE_IN PRICE_OUT [MAX_TOKENS]]
#
#   MODEL_ID    e.g. Qwen/Qwen3.5-9B
#   OUT_JSON    e.g. benchmark/hosted_grounder_probe/results_qwen3_5_9b.json
#   PRICE_IN    USD per 1M input tokens (optional, for the spend estimate)
#   PRICE_OUT   USD per 1M output tokens (optional)
#   MAX_TOKENS  completion budget per call (optional; adapter default 256 —
#               raise for a hosted reasoning model)
#
# The env file must define the key under the name in OPENADAPT_KEY_VAR
# (default: TOGETHERAI_API_KEY).
set -eu

MODEL="${1:?usage: run_hosted_probe.sh MODEL_ID OUT_JSON [PRICE_IN PRICE_OUT [MAX_TOKENS]]}"
OUT="${2:?usage: run_hosted_probe.sh MODEL_ID OUT_JSON [PRICE_IN PRICE_OUT [MAX_TOKENS]]}"
PRICE_IN="${3:-0}"
PRICE_OUT="${4:-0}"
MAX_TOKENS="${5:-256}"

ENV_FILE="${OPENADAPT_ENV_FILE:?set OPENADAPT_ENV_FILE to the .env file holding the API key}"
KEY_VAR="${OPENADAPT_KEY_VAR:-TOGETHERAI_API_KEY}"

# Source the env file; auto-export everything it defines.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# Re-export the named key under the name the probe expects. eval only ever
# expands the VARIABLE NAME here; the value itself is never interpolated into
# a command line or printed.
eval "OPENADAPT_GROUNDER_API_KEY=\"\${${KEY_VAR}:-}\""
export OPENADAPT_GROUNDER_API_KEY
if [ -z "$OPENADAPT_GROUNDER_API_KEY" ]; then
    echo "error: $KEY_VAR is not defined in $ENV_FILE" >&2
    exit 1
fi

export OPENADAPT_GROUNDER_BASE_URL="${OPENADAPT_GROUNDER_BASE_URL:-https://api.together.xyz/v1}"
export OPENADAPT_GROUNDER_MODEL="$MODEL"

exec uv run python scripts/probe_local_grounder.py \
    --out "$OUT" --price-in "$PRICE_IN" --price-out "$PRICE_OUT" \
    --max-tokens "$MAX_TOKENS"
