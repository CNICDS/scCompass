#!/bin/bash
#
# Standalone mouse annotation driver.
#
# Runs mouse_annotation.R over every CSV under an input directory, in
# parallel, writing one "<sample>_cell_type.csv" per sample. This is the
# shell equivalent of the `annotate` step in main.py for mouse; it exists
# for batch runs outside the Python pipeline.
#
# Usage:
#   ./annotation_mouse.sh [input_dir] [output_dir] [species] [jobs]
#
# Defaults can also be overridden via the ANIMAL, INPUT_DIR, OUTPUT_DIR
# and JOBS environment variables.

set -euo pipefail

# Resolve this script's directory so mouse_annotation.R is found regardless
# of the caller's working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
R_SCRIPT="$SCRIPT_DIR/mouse_annotation.R"

animal="${3:-${ANIMAL:-mouse}}"
input_dir="${1:-${INPUT_DIR:-/mnt/cstr/celldata/h5ad/kun/csv_mouse}}"
output_dir="${2:-${OUTPUT_DIR:-/mnt/cstr/celldata/control/data_annotation_20260701}}"
jobs="${4:-${JOBS:-4}}"

# Annotation function
annotate_file() {
    local file="$1"

    if [ ! -f "$file" ]; then
        echo "[ERROR] File not found: $file"
        return
    fi

    echo "[INFO] Processing file: $file"
    local gsm
    gsm="$(basename "$file" .csv)"
    local outfile_dir="$output_dir/$animal/$gsm"
    local tmpfile="$output_dir/$animal/$gsm.tmp"
    local outfile="$outfile_dir/${gsm}_cell_type.csv"
    local log_file="$outfile_dir/logs.txt"

    if [ -f "$outfile" ]; then
        echo "[INFO] Annotation already completed: $file"
        return
    fi

    if [ -f "$tmpfile" ]; then
        echo "[INFO] Annotation already in progress: $file"
        return
    fi

    mkdir -p "$outfile_dir"
    touch "$tmpfile"
    echo "Annotation Start: $(date)" >> "$log_file"
    echo "Processing file: $file" >> "$log_file"

    local start_time end_time duration
    start_time="$(date +%s)"

    if ! Rscript "$R_SCRIPT" "$file" "$outfile_dir" "$animal"; then
        echo "[ERROR] Rscript failed for file: $file"
        echo "Rscript failed for file: $file" >> "$log_file"
        rm -f "$tmpfile"
        return
    fi

    end_time="$(date +%s)"
    duration=$((end_time - start_time))
    echo "Annotation completed in $duration seconds"
    echo "Annotation completed in $duration seconds" >> "$log_file"

    rm -f "$tmpfile"
}

# Export function and variables for the parallel subshells.
export -f annotate_file
export R_SCRIPT output_dir animal

# Find all target CSV files and annotate them in parallel.
find "$input_dir" -name "*.csv" -print0 | \
    xargs -0 -n 1 -P "$jobs" bash -c 'annotate_file "$@"' _

echo "All mouse annotation tasks completed."
exit 0
