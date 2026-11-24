#!/usr/bin/env bash
# Weekly data refresh. Runs on the BUILD host (loki): downloads the kaikki.org
# dump if it changed, rebuilds the SQLite database, sanity-checks it, ships it
# to the SERVE host (zephyr) and swaps it in atomically. The web app notices the
# new inode on its next request and reopens — no restart.
#
# Cron (loki):  0 5 * * 0  /opt/gloss/app/deploy/refresh.sh >> /opt/gloss/refresh.log 2>&1
#
# Env overrides: GLOSS_HOME (/opt/gloss), SERVE_HOST (zephyr), SERVE_DB (/opt/gloss/data/gloss.sqlite),
#                FORCE=1 to rebuild even when the dump is unchanged.

set -euo pipefail

GLOSS_HOME=${GLOSS_HOME:-/opt/gloss}
APP=${APP:-$GLOSS_HOME/app}
DATA=$GLOSS_HOME/data
BUILD=$GLOSS_HOME/build
SERVE_HOST=${SERVE_HOST:-zephyr}
SERVE_DB=${SERVE_DB:-/opt/gloss/data/gloss.sqlite}
URL=https://kaikki.org/dictionary/raw-wiktextract-data.jsonl.gz
DUMP=$DATA/raw-wiktextract-data.jsonl.gz
MIN_WORDS=${MIN_WORDS:-4000000}

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
mkdir -p "$DATA" "$BUILD"

log "refresh start"
before=$(stat -c %Y "$DUMP" 2>/dev/null || echo 0)
# -z: only download if remote is newer than our copy; -R: keep remote mtime
curl -sS -L -R -z "$DUMP" -o "$DUMP" "$URL" || { log "download failed"; exit 1; }
after=$(stat -c %Y "$DUMP" 2>/dev/null || echo 0)
if [ "$before" = "$after" ] && [ "${FORCE:-0}" != "1" ] && [ -f "$BUILD/gloss.sqlite" ]; then
  log "dump unchanged ($(date -u -d @"$after" +%F)); nothing to do"
  exit 0
fi
log "dump dated $(date -u -d @"$after" +%F), $(du -h "$DUMP" | cut -f1); building"

cd "$APP"
NEW=$BUILD/gloss.sqlite.new
rm -f "$NEW" "$NEW.building"
"$APP/.venv/bin/python" -m gloss.ingest "$DUMP" "$NEW"

n_words=$(sqlite3 "$NEW" "select value from meta where key='n_words'")
n_edges=$(sqlite3 "$NEW" "select value from meta where key='n_edges'")
if [ "${n_words:-0}" -lt "$MIN_WORDS" ]; then
  log "sanity check FAILED: only $n_words words (< $MIN_WORDS); keeping old build"
  exit 2
fi
mv -f "$NEW" "$BUILD/gloss.sqlite"
log "built: $n_words words, $n_edges edges, $(du -h "$BUILD/gloss.sqlite" | cut -f1)"

log "shipping to $SERVE_HOST:$SERVE_DB"
rsync -a --inplace=no --partial "$BUILD/gloss.sqlite" "$SERVE_HOST:$SERVE_DB.tmp"
ssh "$SERVE_HOST" "mv -f '$SERVE_DB.tmp' '$SERVE_DB'"
log "swapped in on $SERVE_HOST; refresh done"
