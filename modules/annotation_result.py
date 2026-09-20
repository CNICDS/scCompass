"""Shared mouse CSV validation for the Python and standalone shell drivers.

The command-line validator uses only the Python standard library.
"""

import argparse
import csv
import sys


# These values become missing labels in the downstream pandas CSV reader.
_MISSING_LABELS = {
    "", "NA", "N/A", "n/a", "NaN", "nan", "-NaN", "-nan", "NULL", "null",
    "None", "<NA>", "#N/A", "#N/A N/A", "#NA", "1.#IND", "-1.#IND",
    "1.#QNAN", "-1.#QNAN",
}


def input_cell_count(input_file):
    """Count CSV records without loading a dense expression matrix into memory."""
    with open(input_file, encoding="utf-8-sig", newline="") as stream:
        rows = csv.reader(stream, strict=True)
        header = next(rows, [])
        if len(header) < 2:
            raise ValueError("Mouse input requires cell IDs followed by gene columns.")
        count = 0
        for row in rows:
            if len(row) != len(header) or not row[0].strip():
                raise ValueError(f"Invalid mouse input CSV record {count + 2}.")
            count += 1
        if not count:
            raise ValueError("Mouse input must contain at least one cell.")
        return count


def valid_mouse_result(result_file, expected_cells=None):
    """Require one non-missing label per cell with consecutive positional IDs."""
    try:
        with open(result_file, encoding="utf-8-sig", newline="") as stream:
            rows = csv.reader(stream, strict=True)
            if next(rows, None) != ["", "0"]:
                return False
            count = 0
            for row in rows:
                if (len(row) != 2 or row[0] != str(count)
                        or row[1].strip() in _MISSING_LABELS):
                    return False
                count += 1
            return count > 0 and (expected_cells is None or count == expected_cells)
    except (OSError, csv.Error, UnicodeError):
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_file")
    parser.add_argument("input_file")
    args = parser.parse_args()
    try:
        expected_cells = input_cell_count(args.input_file)
    except (OSError, ValueError, csv.Error, UnicodeError) as error:
        parser.exit(1, f"Invalid mouse input: {error}\n")
    sys.exit(0 if valid_mouse_result(args.result_file, expected_cells) else 1)
