import argparse
import glob
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Iterable, List


def _sorted_files(pattern: str) -> List[str]:
    return sorted(glob.glob(pattern))


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _ensure_files(files: Iterable[str], pattern: str) -> List[str]:
    resolved = list(files)
    if not resolved:
        raise FileNotFoundError(f"No files matched pattern: {pattern}")
    return resolved


def _run_samples(instance, files, *, step, continue_on_error=False, error_log=None, **kwargs):
    failures = 0
    for file in files:
        try:
            print(f"[{step}] Processing {file}", flush=True)
            instance(data=file, **kwargs)
        except Exception as exc:
            if not continue_on_error:
                raise
            failures += 1
            record = {"step": step, "file": file, "error": str(exc),
                      "exception": type(exc).__name__, "traceback": traceback.format_exc()}
            print(f"[{step}] Failed {file}: {exc}", file=sys.stderr, flush=True)
            if error_log:
                with open(error_log, "a") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    return failures


def data_filtering_pipeline(input_pattern: str, species_name: str, output_dir: str, base_dir: str, **run_options) -> int:
    from modules import Filter

    filter_ref_path = os.path.join(base_dir, "gene_data", "cell_ref")
    filter_instance = Filter(ref_path=filter_ref_path)
    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    return _run_samples(filter_instance, files, step="filter", specie=species_name,
                        output_dir=output_dir, **run_options)


def data_normalization_pipeline(input_pattern: str, species_name: str, output_dir: str, base_dir: str, **run_options) -> int:
    from modules import GeneDataNormalization

    ref_path = os.path.join(base_dir, "gene_data", "cell_ref")
    normalization_instance = GeneDataNormalization(species_name, ref_path)
    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    return _run_samples(normalization_instance, files, step="normalize", output_dir=output_dir, **run_options)


def data_annotation_pipeline(
    input_pattern: str,
    species_name: str,
    output_dir: str,
    base_dir: str,
    annotation_model_path: str,
    **run_options,
) -> int:
    from modules import AnnotationHuman
    from modules import AnnotationMouse
    from modules import AnnotationOtherSpecie

    homologous_gene_dir = None
    if species_name == "human":
        annotation_instance = AnnotationHuman(
            annotation_path=annotation_model_path,
            python_module_path=base_dir,
        )
    elif species_name == "mouse":
        annotation_instance = AnnotationMouse(
            python_module_path=base_dir,
            r_script_path=os.path.join(base_dir, "scripts", "mouse_annotation.R"),
        )
    else:
        homologous_gene_dir = os.path.join(base_dir, "gene_data", "homologous_gene")
        annotation_instance = AnnotationOtherSpecie(
            annotation_path=annotation_model_path,
            python_module_path=base_dir,
        )

    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    return _run_samples(annotation_instance, files, step="annotate", specie=species_name,
                        output_dir=output_dir, homologous_gene_dir=homologous_gene_dir, **run_options)


def gene_mapping_pipeline(input_pattern: str, species_name: str, output_dir: str, base_dir: str, **run_options) -> int:
    from modules import GeneMapping

    mapping_ref_path = os.path.join(base_dir, "gene_data", "cell_ref")
    mapping_instance = GeneMapping(ref_path=mapping_ref_path, output_dir=output_dir)
    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    return _run_samples(mapping_instance, files, step="map", specie=species_name,
                        output_dir=output_dir, **run_options)


def gene_merging_pipeline(
    annotation_dir: str,
    mapping_dir: str,
    species_name: str,
    output_dir: str,
    metadata_path: str,
) -> None:
    from modules import SpeciesDataProcessor

    merge_instance = SpeciesDataProcessor(species_name, annotation_dir, mapping_dir, output_dir, metadata_path)
    merge_instance()


def h5ad_export_pipeline(input_pattern, species, mapping_dir, annotation_dir,
                         output_dir, metadata_path, **run_options):
    from modules.h5ad_export import H5adExporter

    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    if len({Path(file).stem for file in files}) != len(files):
        raise ValueError("h5ad input files must have unique sample names")
    exporter = H5adExporter(species, mapping_dir, annotation_dir, output_dir, metadata_path)
    return _run_samples(exporter, files, step="h5ad", **run_options)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="scCompass data processing pipeline",
        epilog="Run 'python main.py metadata --help' for metadata collection, review, and CSV export.",
    )
    parser.add_argument("--species", required=True, help="Species key, e.g. human/mouse/monkey")
    parser.add_argument(
        "--project-root",
        default="/path/to/scCompass",
        help="Project root containing gene_data/ and modules/",
    )
    parser.add_argument(
        "--steps",
        nargs="+",
        required=True,
        choices=["filter", "normalize", "annotate", "map", "merge", "h5ad"],
        help="One or more pipeline steps to run in order",
    )
    parser.add_argument("--input-pattern", help="Glob for raw CSV files (used by filter)")
    parser.add_argument("--filtered-pattern", help="Glob for filtered CSV files (used by normalize/annotate/map/h5ad)")
    parser.add_argument("--annotated-pattern", help="Glob for annotated CSV files (used by map)")
    parser.add_argument("--filter-output", default="/path/to/outputs/filtered_data")
    parser.add_argument("--normalize-output", default="/path/to/outputs/normalization_data")
    parser.add_argument("--annotate-output", default="/path/to/outputs/annotated_data")
    parser.add_argument("--map-output", default="/path/to/outputs/mapping_data")
    parser.add_argument("--merge-output", default="/path/to/outputs/merged_data")
    parser.add_argument("--h5ad-output", help="Output root for one <sample>.h5ad per mapped sample")
    parser.add_argument("--h5ad-metadata", help="Optional CSV or CSV.gz with SourceID and sample metadata")
    parser.add_argument("--metadata-path", help="Metadata directory containing <species>.xlsx (used by merge)")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="Continue other samples after a failure; exit 2 if any sample failed")
    parser.add_argument("--error-log", help="Append sample failures as JSON Lines")
    parser.add_argument(
        "--annotation-model-path",
        default="/path/to/modules/scimilarity/models/annotation_model_v1",
        help="Path to scimilarity annotation model directory",
    )
    return parser


def main() -> None:
    if sys.argv[1:2] == ["metadata"]:
        entry = Path(__file__).resolve().parent / "integrations" / "metadata_v2" / "cli.py"
        raise SystemExit(subprocess.run([sys.executable, str(entry), *sys.argv[2:]], check=False).returncode)
    parser = build_parser()
    args = parser.parse_args()
    if "h5ad" in args.steps and not args.h5ad_output:
        parser.error("--h5ad-output is required when step includes 'h5ad'")
    args.project_root = _normalize_path(args.project_root)
    args.filter_output = _normalize_path(args.filter_output)
    args.normalize_output = _normalize_path(args.normalize_output)
    args.annotate_output = _normalize_path(args.annotate_output)
    args.map_output = _normalize_path(args.map_output)
    args.merge_output = _normalize_path(args.merge_output)
    args.annotation_model_path = _normalize_path(args.annotation_model_path)
    if args.metadata_path:
        args.metadata_path = _normalize_path(args.metadata_path)

    base_dir = args.project_root
    run_options = {"continue_on_error": args.continue_on_error, "error_log": args.error_log}
    if args.error_log:
        args.error_log = _normalize_path(args.error_log)
        os.makedirs(os.path.dirname(args.error_log), exist_ok=True)
        run_options["error_log"] = args.error_log
    failures = 0

    for step in args.steps:
        if step == "filter":
            if not args.input_pattern:
                raise ValueError("--input-pattern is required when step includes 'filter'")
            failures += data_filtering_pipeline(args.input_pattern, args.species, args.filter_output, base_dir, **run_options)
        elif step == "normalize":
            if not args.filtered_pattern:
                raise ValueError("--filtered-pattern is required when step includes 'normalize'")
            failures += data_normalization_pipeline(args.filtered_pattern, args.species, args.normalize_output, base_dir, **run_options)
        elif step == "annotate":
            if not args.filtered_pattern:
                raise ValueError("--filtered-pattern is required when step includes 'annotate'")
            failures += data_annotation_pipeline(
                args.filtered_pattern,
                args.species,
                args.annotate_output,
                base_dir,
                args.annotation_model_path,
                **run_options,
            )
        elif step == "map":
            if not args.filtered_pattern:
                raise ValueError("--filtered-pattern is required when step includes 'map'")
            failures += gene_mapping_pipeline(args.filtered_pattern, args.species, args.map_output, base_dir, **run_options)
        elif step == "merge":
            if not args.metadata_path:
                raise ValueError("--metadata-path is required when step includes 'merge'")
            gene_merging_pipeline(
                args.annotate_output,
                args.map_output,
                args.species,
                args.merge_output,
                args.metadata_path,
            )
        elif step == "h5ad":
            failures += h5ad_export_pipeline(
                args.filtered_pattern or os.path.join(args.filter_output, args.species, "*", "*.csv"),
                args.species,
                args.map_output,
                args.annotate_output,
                _normalize_path(args.h5ad_output),
                _normalize_path(args.h5ad_metadata) if args.h5ad_metadata else None,
                **run_options,
            )
    if failures:
        print(f"Completed with {failures} failed sample operation(s).", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
