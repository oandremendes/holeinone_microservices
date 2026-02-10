# Invoice Classification System

Automated invoice classification for scanned documents from ScanSnap ix-1600. Identifies suppliers by analyzing PDF invoices using OCR and pattern matching, then renames, organizes, and uploads to OCR APIs for data extraction.

## Features

- **Supplier Classification**: Identifies invoices and receipts from 65+ suppliers using NIF (tax ID) matching
- **Date Extraction**: Extracts invoice/receipt issue date from OCR text (multiple formats supported)
- **File Organization**: Renames files to `YYYYMMDD_Supplier.pdf` and moves to appropriate folders
- **Dual API Support**: Routes invoices to Parseur, receipts to Docupipe (with workflow support)
- **Automatic Processing**: Systemd timer monitors Google Drive folders and processes new files
- **~90% Accuracy**: Reliable classification using Portuguese tax ID (NIF) as primary identifier

## Supported Suppliers

### Invoices (→ Parseur)

| Supplier | NIF | Type |
|----------|-----|------|
| Teófilo | 500099871 | Beverages/CO2 |
| Garrafeira Soares | 501496912 | Wine & Spirits |
| Garcias | 501141243 | Wine & Spirits |
| Jose Maria Vieira (JMV) | 503858471 | Coffee/Beverages |
| Justdrinks | 508976464 | Beer/Beverages |
| Novadis | 504350900 | Beer (Heineken/Guinness) |
| Absolutly Vintage | 516001906 | Spirits |

### Receipts (→ Docupipe)

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

\* With Docupipe workflow for automatic standardization

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
# Edit config.json with your Parseur and Docupipe API keys
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

### Process and Move Files
```bash
python classifier.py process
```
Classifies, renames, and moves files:
- **MATCHED/**: Known suppliers → `YYYYMMDD_Supplier.pdf`
- **REVIEW/**: Unknown suppliers → original filename

### Process and Upload to OCR APIs
```bash
python classifier.py process --upload
```
Same as above, plus uploads each classified document to the appropriate OCR API:
- **Invoices** → Parseur (supplier-specific mailboxes)
- **Receipts** → Docupipe (base64 upload)

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
├── classifier.py          # Main classification logic
├── api_config.py          # Supplier → API routing table
├── parseur_client.py      # Parseur API client (invoices)
├── docupipe_client.py     # Docupipe API client (receipts)
├── process_invoices.sh    # Auto-processing script for systemd
├── deploy.sh              # One-click VPS deployment script
├── config.json            # API keys (not in git)
├── config.example.json    # Config template
├── drivek.json            # Google Service Account key (not in git)
├── requirements.txt       # Python dependencies
├── venv/                  # Virtual environment
├── invoices_example/      # Source invoices to process
├── MATCHED/               # Output: classified invoices
├── REVIEW/                # Output: unclassified invoices
└── templates/             # Reference templates (optional)
```

## How It Works

1. **PDF to Image**: Converts first page of PDF to image (200 DPI)
2. **OCR**: Extracts text using Tesseract with Portuguese language
3. **NIF Matching**: Searches for supplier tax IDs (95% confidence)
4. **Keyword Fallback**: If no NIF found, matches supplier keywords
5. **Date Extraction**: Finds invoice date, avoiding due dates
6. **File Operations**: Renames and moves to appropriate folder

## Classification Methods

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

# Process and move files
python classifier.py process ~/GoogleDrive/ScanSnap

# Process with API upload (Parseur + Docupipe)
python classifier.py process ~/GoogleDrive/ScanSnap --upload
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

Each folder has its own `MATCHED/` and `REVIEW/` subfolders for output.

## Dependencies

- **pdf2image**: PDF to image conversion
- **pytesseract**: OCR engine wrapper
- **opencv-python**: Image processing
- **scikit-image**: Template matching (SSIM)
- **watchdog**: Folder monitoring (for future automation)

System requirements:
- **poppler-utils**: PDF rendering (`pdftoppm`)
- **tesseract-ocr**: OCR engine
- **tesseract-ocr-por**: Portuguese language pack
