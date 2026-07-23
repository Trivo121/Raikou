#!/usr/bin/env bash
# Raikou backend deploy: pull latest main from GitHub onto this EC2 box.
#
# Run as the ubuntu user (EC2 Instance Connect terminal):
#   curl -fsSL https://raw.githubusercontent.com/Trivo121/Raikou/main/deployment/deploy-from-github.sh | bash
#
# The script is ordered so nothing stops until the new code's production
# config validators pass against this box's real .env.  A full copy of the
# previous code is kept and a one-line rollback is printed at the end.
set -euo pipefail

REPO_URL="https://github.com/Trivo121/Raikou.git"
APP_DIR="/home/ubuntu/backend"
VENV="/home/ubuntu/sar_env"
SRC_DIR="/home/ubuntu/raikou-src"
TS="$(date +%Y%m%d-%H%M%S)"

log() { printf '\n=== %s ===\n' "$*"; }

log "1/8 Fetch latest main from GitHub"
rm -rf "$SRC_DIR"
git clone --depth 1 "$REPO_URL" "$SRC_DIR"
git -C "$SRC_DIR" log -1 --oneline

log "2/8 Pre-flight: new config against this box's .env (service untouched)"
if [ ! -f "$APP_DIR/.env" ]; then
  echo "FATAL: $APP_DIR/.env not found; refusing to continue." >&2
  exit 1
fi
# The new config hard-requires Redis in production. If the key is missing but
# a local redis answers, wire it up; otherwise stop before touching anything.
if ! grep -q '^REDIS_URL=' "$APP_DIR/.env"; then
  if (exec 3<>/dev/tcp/127.0.0.1/6379) 2>/dev/null; then
    exec 3>&- 3<&- || true
    echo 'REDIS_URL=redis://localhost:6379/0' >> "$APP_DIR/.env"
    echo "Added REDIS_URL=redis://localhost:6379/0 (local redis detected)."
  else
    echo "WARNING: .env has no REDIS_URL and nothing listens on 6379."
    echo "The production config check below will report exactly what is missing."
  fi
fi
PREFLIGHT="/home/ubuntu/raikou-preflight-$TS"
mkdir -p "$PREFLIGHT"
cp -r "$SRC_DIR/backend/app" "$PREFLIGHT/app"
cp "$APP_DIR/.env" "$PREFLIGHT/.env"
(
  cd "$PREFLIGHT"
  ENVIRONMENT=production "$VENV/bin/python" -c \
    "from app.core.config import settings; print('Config OK for production:', settings.PROJECT_NAME)"
)
rm -rf "$PREFLIGHT"

log "3/8 Stop API service"
sudo systemctl stop sar-backend || true

log "4/8 Back up current code -> /home/ubuntu/backend-backup-$TS"
cp -a "$APP_DIR" "/home/ubuntu/backend-backup-$TS"

log "5/8 Sync new backend code (preserving .env and sessions/)"
rsync -a --delete \
  --exclude '.env' --exclude 'sessions' --exclude '__pycache__' --exclude '*.pyc' \
  "$SRC_DIR/backend/" "$APP_DIR/"

log "6/8 Install python dependencies"
"$VENV/bin/pip" install -q --upgrade -r "$APP_DIR/requirements.txt"

log "7/8 Apply idempotent DB migrations (Jul 22-23 set)"
"$VENV/bin/python" - "$SRC_DIR" "$APP_DIR/.env" <<'PY'
import pathlib
import sys

import psycopg

src, env_path = sys.argv[1], sys.argv[2]
env = {}
for line in pathlib.Path(env_path).read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
dsn = env.get("SUPABASE_DB_URL")
if not dsn:
    sys.exit("SUPABASE_DB_URL missing from .env; cannot apply migrations")

# Every file below is idempotent (create-or-replace / drop-if-exists),
# so re-running this script is safe.
files = [
    "20260722000000_m3_enqueue_task_variable_fix.sql",
    "20260722001000_m3_processing_job_attempt_bound.sql",
    "20260722002000_m5_grounded_chat_message_modes.sql",
    "20260722003000_m4_rebuild_ready_scene.sql",
    "20260723000000_m5_scene_record_message_modes.sql",
]
mig_dir = pathlib.Path(src) / "supabase" / "migrations"
with psycopg.connect(dsn) as conn:
    for name in files:
        sql = (mig_dir / name).read_text()
        with conn.transaction():
            conn.execute(sql)
            version, mig_name = name.split("_", 1)
            try:
                with conn.transaction():
                    conn.execute(
                        "insert into supabase_migrations.schema_migrations(version, name)"
                        " values (%s, %s) on conflict (version) do nothing",
                        (version, mig_name.removesuffix(".sql")),
                    )
            except Exception:
                pass  # CLI bookkeeping table may not exist; the migration itself applied.
        print("applied", name)
    row = conn.execute(
        "select pg_get_constraintdef(oid) from pg_constraint"
        " where conname = 'messages_mode_check'"
    ).fetchone()
    print("messages_mode_check =>", (row or ["<missing>"])[0][:400])
PY

log "8/8 Restart services and verify"
sudo systemctl restart sarchat-vllm \
  || echo "WARN: sarchat-vllm restart failed (journalctl -u sarchat-vllm -n 50)"
# Workers/dispatcher only exist if these units were installed on this box.
for unit in raikou-outbox-dispatcher raikou-worker-cpu@1 raikou-worker-gpu@1; do
  sudo systemctl restart "$unit" 2>/dev/null && echo "restarted $unit" || true
done
sudo systemctl start sar-backend
sleep 4
echo "healthz: $(curl -s -m 5 localhost:8000/healthz || echo unreachable)"
echo "readyz:  $(curl -s -m 5 localhost:8000/readyz || echo unreachable)"
echo "vllm:    $(curl -s -m 10 localhost:8001/v1/models | head -c 200 || echo 'still loading - check journalctl -u sarchat-vllm')"

cat <<EOF

Deploy complete. Previous code kept at: /home/ubuntu/backend-backup-$TS
Rollback (if needed):
  sudo systemctl stop sar-backend && rm -rf $APP_DIR && mv /home/ubuntu/backend-backup-$TS $APP_DIR && sudo systemctl start sar-backend

IMPORTANT (spot instance): this box reverts to its AMI on the next spot
interruption. Once verified, bake a fresh AMI from the console
(Instance -> Actions -> Image and templates -> Create image).
EOF
