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
RESULT_VALIDATOR="$SCRIPT_DIR/../modules/annotation_result.py"
PYTHON="${PYTHON:-python3}"

animal="${3:-${ANIMAL:-mouse}}"
input_dir="${1:-${INPUT_DIR:-/mnt/cstr/celldata/h5ad/kun/csv_mouse}}"
output_dir="${2:-${OUTPUT_DIR:-/mnt/cstr/celldata/control/data_annotation_20260701}}"
jobs="${4:-${JOBS:-4}}"

if [ "$animal" != mouse ]; then
    echo "[ERROR] This annotation driver supports only mouse." >&2
    exit 1
fi
if ! [[ "$jobs" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] jobs must be a positive integer." >&2
    exit 1
fi
command -v "$PYTHON" >/dev/null || { echo "[ERROR] Python 3 is required for result validation." >&2; exit 1; }

# Annotation function
annotate_file() (
    set -euo pipefail
    local file="$1"

    if [ ! -f "$file" ]; then
        echo "[ERROR] File not found: $file" >&2
        return 1
    fi

    echo "[INFO] Processing file: $file"
    local gsm
    gsm="$(basename "$file" .csv)"
    local outfile_dir="$output_dir/$animal/$gsm"
    local tmpfile="$output_dir/$animal/$gsm.tmp"
    local outfile="$outfile_dir/${gsm}_cell_type.csv"
    local log_file="$outfile_dir/logs.txt"

    mkdir -p "$output_dir/$animal"
    # noclobber uses exclusive creation, matching Python's open(..., 'x').
    if ! (set -o noclobber; : > "$tmpfile") 2>/dev/null; then
        if [ -e "$tmpfile" ] || [ -L "$tmpfile" ]; then
            echo "[INFO] Annotation already in progress: $file"
            return 0
        fi
        echo "[ERROR] Could not create annotation lock: $tmpfile" >&2
        return 1
    fi
    trap 'rm -f -- "$tmpfile"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if "$PYTHON" "$RESULT_VALIDATOR" "$outfile" "$file"; then
        echo "[INFO] Annotation already completed: $file"
        return 0
    fi

    mkdir -p "$outfile_dir"
    echo "Annotation Start: $(date)" >> "$log_file"
    echo "Processing file: $file" >> "$log_file"

    local start_time end_time duration
    start_time="$(date +%s)"

    if ! Rscript "$R_SCRIPT" "$file" "$outfile_dir" "$animal"; then
        echo "[ERROR] Rscript failed for file: $file" >&2
        echo "Rscript failed for file: $file" >> "$log_file"
        return 1
    fi
    if ! "$PYTHON" "$RESULT_VALIDATOR" "$outfile" "$file"; then
        echo "[ERROR] Rscript did not produce a valid result: $outfile" >&2
        echo "Missing, incomplete or invalid result: $outfile" >> "$log_file"
        return 1
    fi

    end_time="$(date +%s)"
    duration=$((end_time - start_time))
    echo "Annotation completed in $duration seconds"
    echo "Annotation completed in $duration seconds" >> "$log_file"
)

# Export function and variables for the parallel subshells.
export -f annotate_file
export R_SCRIPT RESULT_VALIDATOR PYTHON output_dir animal

# Find all target CSV files and annotate them in parallel.
find "$input_dir" -type f -name "*.csv" ! -name '._*' -print0 | \
    xargs -0 -n 1 -P "$jobs" bash -c 'if [ "$#" -gt 0 ]; then annotate_file "$@"; fi' _

echo "All mouse annotation tasks completed."
exit 0
