# File Storage (Azure Blob / Azurite)

Uploaded files (BDX workbooks, mappers, generated exports, contracts) are stored
in **Azure Blob Storage**. Locally we use **Azurite**, the official emulator —
the same code talks to Azurite in dev and real Azure in prod; only the
connection string changes.

The app can also fall back to storing bytes in Postgres (`STORAGE_BACKEND=db`),
so it runs even with no blob storage configured.

---

## TL;DR — run it on any machine

```bash
# 1. Python deps (pulls in azure-storage-blob)
pip install -r requirements.txt

# 2. Start Azurite (leave it running) — pick ONE:
npm install -g azurite && azurite --location ./.azurite-data --blobHost 127.0.0.1
#   or, no install:      npx azurite --location ./.azurite-data --blobHost 127.0.0.1
#   or, Docker:          see "Start Azurite" below

# 3. Configure .env (see below), then start the backend
python main.py
```

Azurite is a **separate, always-on service** (like Postgres). Start it **once**
and leave it running; then start/restart the backend as often as you like — it
connects to the already-running Azurite. Starting `main.py` does **not** start
Azurite.

---

## Configuration (`.env`)

`.env` is git-ignored, so add these on each machine. The connection string below
is Azurite's **public dev default** — identical on every machine, not a secret.

```bash
# "azure" -> blob storage (Azurite/Azure); anything else -> Postgres blob fallback
STORAGE_BACKEND=azure
# Swap ONLY this line for a real Azure account string in production.
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;
AZURE_STORAGE_CONTAINER=kavachio-uploads
# Optional virtual folder prefix inside the container.
AZURE_BLOB_PREFIX=dev
```

| Variable | Default | Purpose |
|----------|---------|---------|
| `STORAGE_BACKEND` | `db` | `azure` = blob storage; else Postgres blobs |
| `AZURE_STORAGE_CONNECTION_STRING` | Azurite dev creds | swap for real Azure in prod |
| `AZURE_STORAGE_CONTAINER` | `kavachio-uploads` | container name |
| `AZURE_BLOB_PREFIX` | *(empty)* | optional folder prefix inside the container |

---

## Two backends

| `STORAGE_BACKEND` | Azurite needed? | Where files go |
|-------------------|-----------------|----------------|
| `azure` | **yes** (or uploads/downloads error) | Azure/Azurite blob; a string pointer (`*_ref`) is saved in Postgres |
| `db` (or unset) | no | raw bytes in Postgres (`LargeBinary` columns) |

Old rows created before blob storage still download fine — reads prefer the blob
pointer, then fall back to the legacy Postgres column.

---

## Start Azurite

Azurite listens on `10000` (blob), `10001` (queue), `10002` (table).
`--location` is the **configurable storage path** on disk.

**npm (install once):**
```bash
npm install -g azurite
azurite --location ./.azurite-data --blobHost 127.0.0.1
```

**npx (no install):**
```bash
npx azurite --location ./.azurite-data --blobHost 127.0.0.1
```

**Docker (any OS, auto-restarts across reboots):**
```bash
docker run -d --restart unless-stopped \
  -p 10000:10000 -p 10001:10001 -p 10002:10002 \
  -v "$PWD/.azurite-data:/data" \
  mcr.microsoft.com/azure-storage/azurite \
  azurite --blobHost 0.0.0.0 --location /data
```

**Helper script (macOS/Linux)** — wraps the command with a configurable path:
```bash
./start-azurite.sh                       # default location: ./.azurite
AZURITE_LOCATION=/some/path ./start-azurite.sh
```

The container is created automatically on first upload — no manual setup.

### Verify it's up
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:10000/devstoreaccount1
# 403 = alive (rejecting an unsigned request); connection-refused = not running
```

---

## Production (real Azure)

No Azurite, no emulator. Just set the two vars to a real account and run the app
unchanged:

```bash
STORAGE_BACKEND=azure
AZURE_STORAGE_CONNECTION_STRING="<connection string from Azure portal / Key Vault>"
```

For managed identity instead of a key, swap `from_connection_string(...)` in
`storage.py` for `BlobServiceClient(account_url, credential=DefaultAzureCredential())`.

---

## How it works (brief)

- [`storage.py`](storage.py) is the single blob I/O module. Env is read lazily so
  it picks up `.env` regardless of import order; the Azure SDK client is cached.
- On upload, the file is written to a blob at
  `<prefix>/<category>/<tenant_id>/<uuid>.<ext>` and only that **path string** is
  stored in Postgres (`*_ref` column); the `LargeBinary` column stays NULL.
- Categories: `uploads`, `mappers`, `exports`, `contracts`.
- Reads use `storage.resolve_bytes(ref, legacy)` — blob pointer first, then the
  legacy column.

| Flow | Endpoint | Ref column |
|------|----------|-----------|
| BDX upload | `POST /bdx/upload` | `upload.source_blob_ref` |
| Mapper generate | `POST /mapper/generate` | `mappers.source_blob_ref` |
| Output export | export generate | `output_exports.blob_ref` |
| Contract upload | `POST /programs/{id}/contracts` | `contract.blob_ref` |

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Upload fails with connection error | Azurite isn't running — start it (see above) |
| Files not going to Azurite | `STORAGE_BACKEND` isn't `azure`, or `.env` not loaded |
| `ModuleNotFoundError: azure` | run `pip install -r requirements.txt` |
| Port 10000 already in use | another Azurite/process is running on that port |
