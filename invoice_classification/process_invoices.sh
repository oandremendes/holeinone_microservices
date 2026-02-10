#!/bin/bash
# Invoice Classification Auto-Processor
# Runs via systemd timer to process new invoices from Google Drive

set -e

# Configuration
PROJECT_DIR="$HOME/claudeprojects/invoice_classification"
VENV_DIR="$PROJECT_DIR/venv"
BASE_DIR="$HOME/GoogleDrive/ScanSnap"
LOG_TAG="invoice-classifier"

# Folders to monitor
FOLDERS=(
    "$BASE_DIR"
    "$BASE_DIR/Receipts"
)

# Check if base directory exists (rclone mounted)
if [ ! -d "$BASE_DIR" ]; then
    logger -t "$LOG_TAG" "Base directory not found: $BASE_DIR (rclone not mounted?)"
    exit 0
fi

# Activate virtual environment
cd "$PROJECT_DIR"
source "$VENV_DIR/bin/activate"

# Process each folder
for SOURCE_DIR in "${FOLDERS[@]}"; do
    # Skip if folder doesn't exist
    if [ ! -d "$SOURCE_DIR" ]; then
        continue
    fi

    # Check if there are any PDF files to process (only in this folder, not subfolders)
    PDF_COUNT=$(find "$SOURCE_DIR" -maxdepth 1 -type f \( -name "*.pdf" -o -name "*.PDF" \) 2>/dev/null | wc -l)

    if [ "$PDF_COUNT" -eq 0 ]; then
        # No files to process in this folder
        continue
    fi

    logger -t "$LOG_TAG" "Found $PDF_COUNT PDF(s) in $SOURCE_DIR"

    # Run classifier with upload enabled
    python classifier.py process "$SOURCE_DIR" --upload 2>&1 | while read line; do
        logger -t "$LOG_TAG" "$line"
    done
done

logger -t "$LOG_TAG" "Processing complete"
