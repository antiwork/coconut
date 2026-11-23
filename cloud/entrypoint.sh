#!/bin/sh
# Start Coconut on a Linux host (docs/cloud.md): restore the database from S3 on an empty volume, register with the
# relay once, start the sidecar, and run the service under Litestream, which streams every committed change to S3.
set -eu
DATA=/data
DB="$DATA/coconut.sqlite3"
REPLICA="${LITESTREAM_REPLICA:?Set LITESTREAM_REPLICA to s3://bucket/path}"
mkdir -p "$DATA"
chmod 700 "$DATA"

if [ ! -f "$DB" ]; then
  # A new host: bring back the newest copy from S3, if one exists.
  litestream restore -if-replica-exists -o "$DB" "$REPLICA"
fi
if [ ! -f "$DB" ]; then
  # A first start: Litestream needs the database file, in WAL mode. The service makes its tables at start.
  sqlite3 "$DB" 'PRAGMA journal_mode=WAL;' >/dev/null
fi

python /app/cloud/sidecar.py setup --data-dir "$DATA"
python /app/cloud/sidecar.py run --data-dir "$DATA" --backend 127.0.0.1:8961 &

exec litestream replicate -exec "python -m coconut --data-dir $DATA --port 8960 --tunnel-port 8961 --provider agent-sdk" \
  "$DB" "$REPLICA"
