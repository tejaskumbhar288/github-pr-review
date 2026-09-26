#!/usr/bin/env bash
#
# Deploy the webhook receiver and worker to Fly.
#
# Everything here was verified locally before it was written: the image builds,
# imports the app, resolves ruff, and answers /healthz with a 200. What this
# script cannot do for you is the part that needs an account - `fly auth login`
# and a managed Redis - so it checks for both and stops with an instruction
# rather than a stack trace.
#
#   ./scripts/deploy-fly.sh              # deploy
#   ./scripts/deploy-fly.sh --secrets    # push secrets only
#   ./scripts/deploy-fly.sh --webhook    # repoint the App's webhook only
#
set -euo pipefail

cd "$(dirname "$0")/.."

APP="${FLY_APP:-senior-review-bot}"
ENV_FILE="${ENV_FILE:-.env}"

die() { printf '\nerror: %s\n' "$1" >&2; exit 1; }
note() { printf '\n==> %s\n' "$1"; }

command -v flyctl >/dev/null 2>&1 || command -v fly >/dev/null 2>&1 || die \
  "flyctl is not installed. Get it with:
    curl -L https://fly.io/install.sh | sh
  then: fly auth login"

FLY="$(command -v flyctl || command -v fly)"

"$FLY" auth whoami >/dev/null 2>&1 || die \
  "not logged in to Fly. Run: fly auth login"

[ -f "$ENV_FILE" ] || die "$ENV_FILE not found. Copy .env.example and fill it in."

# Read a value from .env without sourcing it. Sourcing is what breaks on the
# PEM: its header contains spaces, so an unquoted value turns into
# `RSA: command not found`.
getenv() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1 | sed 's/^"//; s/"$//'; }

require() {
  local value
  value="$(getenv "$1")"
  [ -n "$value" ] || die "$1 is empty in $ENV_FILE"
  printf '%s' "$value"
}

push_secrets() {
  note "pushing secrets to $APP"

  local key_path pem
  key_path="$(getenv GITHUB_PRIVATE_KEY_PATH)"
  pem="$(getenv GITHUB_PRIVATE_KEY)"

  # Fly injects secrets as environment variables and gives you no filesystem to
  # mount a key onto, so the inline form is the only one that works here. It is
  # also the one that survives running as an unprivileged uid: a bind-mounted
  # key keeps its host ownership, and a key readable only by you is unreadable
  # to the process that needs it.
  if [ -z "$pem" ] && [ -n "$key_path" ]; then
    [ -f "$key_path" ] || die "GITHUB_PRIVATE_KEY_PATH points at a missing file: $key_path"
    note "converting $key_path to the inline form"
    pem="$(python3 -c 'import sys; print(open(sys.argv[1]).read().strip().replace(chr(10), chr(92)+"n"))' "$key_path")"
  fi
  [ -n "$pem" ] || die "set GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH in $ENV_FILE"

  local redis
  redis="$(getenv REDIS_URL)"
  case "$redis" in
    ""|*localhost*|*127.0.0.1*)
      die "REDIS_URL points at localhost, which does not exist on Fly.
  Provision a managed instance first, e.g.:
    fly redis create          # or an Upstash / managed Redis URL
  then set REDIS_URL in $ENV_FILE to the URL it prints." ;;
  esac

  "$FLY" secrets set --app "$APP" --stage \
    GEMINI_API_KEY="$(require GEMINI_API_KEY)" \
    GITHUB_APP_ID="$(require GITHUB_APP_ID)" \
    GITHUB_WEBHOOK_SECRET="$(require GITHUB_WEBHOOK_SECRET)" \
    GITHUB_PRIVATE_KEY="$pem" \
    REDIS_URL="$redis"

  # Optional: tracing works without these, as a no-op.
  local lf_public lf_secret
  lf_public="$(getenv LANGFUSE_PUBLIC_KEY)"
  lf_secret="$(getenv LANGFUSE_SECRET_KEY)"
  if [ -n "$lf_public" ] && [ -n "$lf_secret" ]; then
    "$FLY" secrets set --app "$APP" --stage \
      LANGFUSE_PUBLIC_KEY="$lf_public" LANGFUSE_SECRET_KEY="$lf_secret"
  fi

  note "secrets staged; they apply on the next deploy"
}

repoint_webhook() {
  # The same PATCH /app/hook/config call used for the tunnel. Doing it here
  # rather than by hand is not laziness: the secret has three copies that must
  # agree - .env, the running process, and GitHub's App config - and a rotation
  # that updates two of them fails closed, which is indistinguishable from an
  # attack in the logs.
  note "repointing the GitHub App webhook at https://$APP.fly.dev/webhook"
  python3 - "$APP" <<'PY'
import sys, pathlib, re, json, time
sys.path.insert(0, str(pathlib.Path.cwd()))
import asyncio, httpx
from app.config import get_settings
from app.github.auth import GitHubAppAuth, load_private_key

app_name = sys.argv[1]
settings = get_settings()
key = load_private_key(settings.github_private_key, settings.github_private_key_path)

async def main():
    auth = GitHubAppAuth(settings.github_app_id, key, settings.github_api)
    try:
        jwt = auth.app_jwt()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.patch(
                f"{settings.github_api}/app/hook/config",
                headers={"Authorization": f"Bearer {jwt}",
                         "Accept": "application/vnd.github+json"},
                json={"url": f"https://{app_name}.fly.dev/webhook",
                      "secret": settings.github_webhook_secret,
                      "content_type": "json"},
            )
            r.raise_for_status()
            print("webhook now:", r.json().get("url"))
    finally:
        await auth.close()

asyncio.run(main())
PY
}

case "${1:-deploy}" in
  --secrets) push_secrets ;;
  --webhook) repoint_webhook ;;
  deploy|"")
    push_secrets
    note "deploying"
    "$FLY" deploy --app "$APP"
    note "waiting for the receiver to answer"
    for _ in $(seq 1 30); do
      if curl -fsS "https://$APP.fly.dev/healthz" >/dev/null 2>&1; then
        echo "healthz ok"
        repoint_webhook
        note "done. Reopen a PR to test a real delivery end to end."
        exit 0
      fi
      sleep 2
    done
    die "deployed, but https://$APP.fly.dev/healthz never answered. Check: fly logs --app $APP"
    ;;
  *) die "unknown option: $1 (expected --secrets, --webhook, or no argument)" ;;
esac
