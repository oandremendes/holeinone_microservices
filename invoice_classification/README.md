# Invoice Classification System

Automated invoice processing for scanned documents from ScanSnap ix-1600. Identifies suppliers from the Portuguese fiscal QR code (with OCR fallback), renames and organizes the PDFs, extracts line-item data with Claude, validates it against the QR, and delivers validated invoices to Odoo plus JSON artifacts.

## Features

- **QR-First Identification**: Reads the Portuguese e-Fatura QR code (via `zxing-cpp`) as the primary identification signal, with Tesseract OCR/NIF/keyword matching as fallback when no QR code is present or decodable
- **Claude Extraction**: Structured invoice data (supplier, date, totals, line items) is extracted by Claude (`claude-opus-5`) instead of the legacy OCR APIs
- **SQLite Retry Queue**: Extraction and delivery failures are queued in `state.db` and retried automatically, capped at 5 attempts before being marked permanently failed
- **QR-Gated Odoo Delivery**: Only invoices successfully identified via QR are sent on to Odoo as draft bills; everything else is held back for review
- **JSON Artifacts**: Every processed invoice writes its extracted data to `EXTRACTED/*.json` alongside the PDF for auditability
- **Date-Based Filing**: Integrated invoices are filed under `INTEGRATED/YYYY/MM/` by invoice date
- **Dedup with Supersede**: Duplicate detection by md5 hash and QR payload; a re-scanned invoice supersedes the previously queued/sent record instead of creating a duplicate entry
- **Legacy APIs Neutralized**: Parseur and Docupipe integrations are preserved in code but disabled by default; re-enable via `legacy_apis.enabled` in `config.json`
- **File Organization**: Renames files to `YYYYMMDD_Supplier.pdf` and moves to appropriate folders
- **Automatic Processing**: Systemd timer monitors Google Drive folders and processes new files

## Supported Suppliers

Known suppliers are seeded into the `state.db` supplier registry (NIF → key); QR codes with unknown NIFs are auto-registered on first sight. The OCR fallback additionally uses the keyword/template profiles in `classifier.py`.

### Invoices

| Supplier | NIF | Type |
|----------|-----|------|
| Teófilo | 500099871 | Beverages/CO2 |
| Garrafeira Soares | 501496912 | Wine & Spirits |
| Garcias | 501141243 | Wine & Spirits |
| Jose Maria Vieira (JMV) | 503858471 | Coffee/Beverages |
| Justdrinks | 508976464 | Beer/Beverages |
| Novadis | 504350900 | Beer (Heineken/Guinness) |
| Absolutly Vintage | 516001906 | Spirits |

### Receipts

| Category | Suppliers |
|----------|-----------|
| **Supplier Docs** | Magnibéria, Teófilo Guia Devolução, Teófilo Nota Crédito |
| **Supermarkets** | Intermarché, Continente, Overseas, Makro*, Pingo Doce*, Lidl |
| **Utilities** | Inframoura (water/sewage) |
| **Gas Stations** | Galp, Cepsa, Moeve, BP, Makro Gas |
| **Retail** | Worten, IKEA, Leroy Merlin, Staples, Action, Note, Wells |
| **Fast Food** | McDonald's, Burger King, Pizza Hut, Domino's |
| **Restaurants** | Mourapão, MatchPoint, Tribulum, Zorba, A Paisagem, Eurolatina, + more |
| **Hardware** | Constamarina, Constantino, Papelnet, Gilda da Silva |
| **Tolls/Parking** | Brisa, Alparques |
| **Shopping** | Partyland, Oriental Shopping, Semino Shopping |

## VPS Deployment (One-Click)

The `deploy.sh` script handles full production deployment to a VPS. It creates a dedicated `invclassificator` service user, installs all dependencies, configures systemd services, and sets up Google Drive mounting.

### First Deployment

```bash
# Clone the repo on the VPS
cd /root
git clone <repo-url> holeinone_microservices

# Deploy
cd holeinone_microservices/invoice_classification
sudo ./deploy.sh
```

The script will:
1. Create system user `invclassificator`
2. Install system packages (tesseract-ocr, poppler-utils, rclone, fuse3, etc.)
3. Deploy app files to `/home/invclassificator/invoice_classification/`
4. Set up Python venv with headless OpenCV (no GUI dependencies)
5. Copy `config.json` and `drivek.json` if present in source, or create from template
6. Configure rclone for Google Drive mounting
7. Create and enable system-level systemd services:
   - `rclone-gdrive-invclassificator.service` - Google Drive FUSE mount
   - `invoice-classifier.service` - oneshot invoice processor
   - `invoice-classifier.timer` - runs every 5 minutes (configurable)
8. Start all services

### Deploying Updates

After adding features or fixing bugs:

```bash
cd /root/holeinone_microservices
git pull
sudo ./invoice_classification/deploy.sh
```

The script is **idempotent** - it skips what's already done (user, packages), syncs code changes, updates dependencies, and preserves configuration files (`config.json`, `drivek.json`).

### Configurable Timer Interval

```bash
# Default: 5 minutes
sudo ./deploy.sh

# Override with environment variable
TIMER_INTERVAL=1min sudo ./deploy.sh
TIMER_INTERVAL=15min sudo ./deploy.sh
```

### Post-Deploy Manual Steps

If `config.json` or `drivek.json` weren't available during deployment:

```bash
# Edit API keys
sudo nano /home/invclassificator/invoice_classification/config.json

# Copy Google Service Account key
sudo cp /path/to/drivek.json /home/invclassificator/invoice_classification/drivek.json
sudo chown invclassificator:invclassificator /home/invclassificator/invoice_classification/drivek.json
sudo chmod 600 /home/invclassificator/invoice_classification/drivek.json
sudo systemctl restart rclone-gdrive-invclassificator
```

### Service Management

```bash
# Check status
systemctl status rclone-gdrive-invclassificator    # Google Drive mount
systemctl status invoice-classifier.timer           # Timer status
systemctl list-timers invoice-classifier.timer      # Next run time

# View logs
journalctl -t invoice-classifier -f                 # Classifier logs
journalctl -u rclone-gdrive-invclassificator -f     # rclone logs
tail -f /var/log/rclone-invclassificator.log         # rclone file log

# Manual trigger
sudo systemctl start invoice-classifier.service      # Run once now
```

## Local Installation (Development)

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install system dependencies (Ubuntu/Debian)
sudo apt-get install tesseract-ocr tesseract-ocr-por poppler-utils

# Configure API keys
cp config.example.json config.json
# Edit config.json with the Anthropic key and Odoo webhook settings
# (Parseur/Docupipe keys only matter if legacy_apis.enabled is turned on)
```

## Usage

### Classify Only (Preview)
```bash
source venv/bin/activate
python classifier.py
```
Shows classification results without moving files.

### Process with Dry Run
```bash
python classifier.py process --dry-run
```
Shows what would happen without actually moving files.

### Process (Phase A + Phase B)
```bash
python classifier.py process
```
Identifies (QR first, OCR fallback), renames, moves and enqueues files:
- **INTEGRATED/**: Identified invoices → `YYYYMMDD_Supplier[_nc].pdf`, queued in `state.db` for extraction
- **REVIEW/**: Unidentified invoices → original filename (a better rescan supersedes them)
- **Duplicados/**: Duplicate scans (same md5 or same fiscal identity)

The run ends with a Phase B drain: each queued invoice is extracted with Claude, validated against its QR (±2c, supplier quirks), filed into `INTEGRATED/YYYY/MM/`, written to `EXTRACTED/*.json`, and POSTed to Odoo when validation passes. Failures are retried on later runs (capped at 5 attempts).

### Drain Only (Phase B)
```bash
python classifier.py drain [--dry-run]
```
Retries the extraction/Odoo queue without classifying any new files. The production timer runs this on every tick, even when no new PDFs arrived.

### Generate Templates (Optional)
```bash
python classifier.py generate-templates
```
Creates reference template images from sample invoices for visual matching.

## File Naming Convention

- **With date**: `20250227_Soares.pdf`
- **Without date**: `2026XXXX_Soares.pdf` (current year + XXXX)
- **Duplicates**: `20250227_Soares_1.pdf`, `20250227_Soares_2.pdf`

## Project Structure

```
invoice_classification/
├── classifier.py          # CLI + OCR fallback classification
├── pipeline.py            # v2 pipeline: Phase A (identify/dedup/enqueue), Phase B (drain)
├── qr.py                  # Fiscal QR decoding (PyMuPDF + zxing-cpp + WeChat CNN)
├── state.py               # SQLite ledger/queue (state.db)
├── claude_extract.py      # Claude line-item extraction (Pydantic schema)
├── validation.py          # QR ↔ extraction validation gate (±2c, quirks)
├── odoo_send.py           # Odoo webhook payload + Drive preview link
├── artifacts.py           # Atomic EXTRACTED/*.json writes
├── api_config.py          # config.json access (Anthropic/Odoo/legacy keys)
├── parseur_client.py      # Legacy Parseur client (neutralized)
├── docupipe_client.py     # Legacy Docupipe client (neutralized)
├── process_invoices.sh    # Auto-processing script for systemd
├── deploy.sh              # One-click VPS deployment script
├── config.json            # API keys (not in git)
├── config.example.json    # Config template
├── drivek.json            # Google Service Account key (not in git)
├── state.db               # Pipeline ledger (not in git)
├── requirements.txt       # Python dependencies
├── venv/                  # Virtual environment
└── templates/             # Reference templates (optional)
```

Each scan folder gets `INTEGRATED/` (filed into `YYYY/MM/` after validation), `REVIEW/` and a `Duplicados/` sibling; the ScanSnap root holds the shared `EXTRACTED/` artifact folder.

## How It Works

Phase A — per new PDF at the top level of a scan folder:
1. **md5 dedup**: Exact rescans go to `Duplicados/` (a rescan of a failed/unidentified original supersedes it instead)
2. **QR decode**: All fiscal QRs (Portaria 195/2020) are read; supplier by NIF, date from field F, `_nc` from D, identity dedup on (NIF, ATCUD)
3. **OCR fallback**: When no QR decodes, the Tesseract pipeline (NIF substring, keywords, templates, date regexes) identifies the file
4. **File + enqueue**: Identified files are renamed `YYYYMMDD_Supplier[_nc].pdf`, moved to `INTEGRATED/` and queued; unknown files go to `REVIEW/`

Phase B — every run, for each queued/retry row (attempts < 5):
1. **Claude extraction**: Whole PDF, structured line items in integer cents
2. **Validation gate**: total/IVA vs QR (±2c, supplier quirk table), invoice ref, date, and line-sum checks
3. **Delivery**: Pass → file into `INTEGRATED/YYYY/MM/`, write `EXTRACTED/<name>.json`, POST to Odoo. Fail → JSON written, held as `needs_review`. Transient errors retry on later runs

## OCR Fallback Classification Methods

| Method | Confidence | Description |
|--------|------------|-------------|
| NIF | 0.95 | Portuguese tax ID match (most reliable) |
| Keywords | 0.50-0.90 | Company names, addresses, domains |
| Template | 0.40-1.00 | Visual logo/header matching |
| Hybrid | 0.90+ | Multiple methods agree |

## Adding New Suppliers

Edit `classifier.py` and add to `SUPPLIERS` dict:

```python
'newsupplier': SupplierProfile(
    name='newsupplier',
    display_name='New Supplier Lda',
    nif='123456789',  # Portuguese tax ID
    keywords=['newsupplier', 'unique', 'keywords'],
    header_region=(0, 0, 300, 150),  # Logo region for template matching
),
```

## ScanSnap Integration with Google Drive

The classifier integrates with ScanSnap via Google Drive using rclone. ScanSnap saves scans to Google Drive, rclone mounts the drive locally, and the classifier processes files directly.

### Setup rclone with Service Account (works on headless servers)

1. **Create Google Cloud Service Account**:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create project → Enable Google Drive API
   - Create Service Account → Download JSON key
   - Save key as `drivek.json` (excluded from git)

2. **Share Google Drive folder** with the service account email (Editor access)

3. **Install and configure rclone**:
   ```bash
   sudo apt install rclone

   # Configure with service account
   rclone config create gdrive drive service_account_file /path/to/drivek.json
   ```

4. **Test connection**:
   ```bash
   rclone lsd gdrive: --drive-shared-with-me
   ```

5. **Create systemd service for auto-mount** (`~/.config/systemd/user/rclone-gdrive.service`):
   ```ini
   [Unit]
   Description=rclone mount for Google Drive
   After=network-online.target

   [Service]
   Type=notify
   ExecStartPre=/bin/mkdir -p %h/GoogleDrive
   ExecStart=/usr/bin/rclone mount gdrive: %h/GoogleDrive \
       --drive-shared-with-me \
       --vfs-cache-mode full \
       --vfs-cache-max-age 1h \
       --poll-interval 30s
   ExecStop=/bin/fusermount -u %h/GoogleDrive
   Restart=on-failure

   [Install]
   WantedBy=default.target
   ```

6. **Enable and start**:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now rclone-gdrive.service
   loginctl enable-linger $USER  # Auto-start on boot
   ```

### Process invoices from Google Drive

```bash
# Dry run (preview)
python classifier.py process ~/GoogleDrive/ScanSnap --dry-run

# Process, move and drain (extraction + Odoo)
python classifier.py process ~/GoogleDrive/ScanSnap
```

### Alternative: Manual mount (without systemd)

```bash
rclone mount gdrive: ~/GoogleDrive --drive-shared-with-me --vfs-cache-mode full --daemon
```

## Automatic Processing (Systemd Timer)

The classifier can run automatically via a systemd timer, processing new files as they appear in Google Drive.

### Setup

1. **Timer and service files** are in `~/.config/systemd/user/`:
   - `invoice-classifier.timer` - Runs every N minutes
   - `invoice-classifier.service` - Executes the processing script

2. **Enable and start**:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now invoice-classifier.timer
   ```

3. **Check status**:
   ```bash
   systemctl --user list-timers invoice-classifier.timer
   journalctl --user -t invoice-classifier -f  # Watch logs
   ```

4. **Change interval**: Edit the timer file and reload:
   ```bash
   # Edit OnUnitActiveSec in ~/.config/systemd/user/invoice-classifier.timer
   systemctl --user daemon-reload
   ```

### Monitored Folders

The auto-processor monitors:
- `~/GoogleDrive/ScanSnap/` - Main invoice folder
- `~/GoogleDrive/ScanSnap/Receipts/` - Receipts subfolder

Each folder has its own `INTEGRATED/` and `REVIEW/` subfolders for output, and the extraction queue is drained on every run even when neither folder has new PDFs.

## Dependencies

- **pymupdf** + **zxing-cpp**: PDF rendering and fiscal QR decoding
- **opencv-contrib-python-headless**: Image processing + WeChat QR CNN fallback
- **anthropic** + **pydantic**: Claude extraction with a typed schema
- **pdf2image** / **pytesseract**: OCR fallback pipeline
- **scikit-image**: Template matching (SSIM)

System requirements:
- **poppler-utils**: PDF rendering (`pdftoppm`)
- **tesseract-ocr**: OCR engine
- **tesseract-ocr-por**: Portuguese language pack

## Acceptance test (manual)

1. Copy 3-5 PDFs from the reserved QA test set into a scratch folder.
2. `python classifier.py process /path/to/scratch --dry-run` — verify intended
   actions in the log (no moves, no API calls).
3. `python classifier.py process /path/to/scratch` — verify: files land in
   INTEGRATED/YYYY/MM/, EXTRACTED/*.json written, Odoo drafts created,
   `sqlite3 state.db "SELECT id,status,supplier_key FROM files"` shows 'sent'.
4. Re-copy one of the same PDFs and re-run: it must land in Duplicados/ with
   status 'duplicate'.
