#!/usr/bin/env bash
#
# start-azurite.sh — run the Azure Blob Storage emulator locally (no Docker).
#
# The on-disk storage location is CONFIGURABLE: override AZURITE_LOCATION (env
# var) to move where blobs are persisted. Everything else matches Azurite's
# well-known dev defaults, so the app's default AZURE_STORAGE_CONNECTION_STRING
# works with zero extra config. Swapping that connection string for a real
# Azure account later needs no code change.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Configurable storage path ────────────────────────────────────────────────
# Change this (or `export AZURITE_LOCATION=/some/path` before running) to move
# where files live on disk.
AZURITE_LOCATION="${AZURITE_LOCATION:-$SCRIPT_DIR/.azurite}"

AZURITE_HOST="${AZURITE_HOST:-127.0.0.1}"
AZURITE_BLOB_PORT="${AZURITE_BLOB_PORT:-10000}"
AZURITE_QUEUE_PORT="${AZURITE_QUEUE_PORT:-10001}"
AZURITE_TABLE_PORT="${AZURITE_TABLE_PORT:-10002}"

mkdir -p "$AZURITE_LOCATION"

echo "──────────────────────────────────────────────────────────────"
echo " Azurite storage path : $AZURITE_LOCATION"
echo " Blob endpoint        : http://$AZURITE_HOST:$AZURITE_BLOB_PORT/devstoreaccount1"
echo " Debug log            : $AZURITE_LOCATION/debug.log"
echo "──────────────────────────────────────────────────────────────"

exec azurite \
  --location  "$AZURITE_LOCATION" \
  --blobHost  "$AZURITE_HOST"  --blobPort  "$AZURITE_BLOB_PORT" \
  --queueHost "$AZURITE_HOST"  --queuePort "$AZURITE_QUEUE_PORT" \
  --tableHost "$AZURITE_HOST"  --tablePort "$AZURITE_TABLE_PORT" \
  --debug "$AZURITE_LOCATION/debug.log"
