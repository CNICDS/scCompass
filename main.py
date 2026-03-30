import argparse
import glob
import os
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


def data_filtering_pipeline(input_pattern: str, species_name: str, output_dir: str, base_dir: str) -> None:
    from modules import Filter

    filter_ref_path = os.path.join(base_dir, "gene_data", "cell_ref")
    filter_instance = Filter(ref_path=filter_ref_path)
    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    for file in files:
        filter_instance(data=file, specie=species_name, output_dir=output_dir)


def data_normalization_pipeline(input_pattern: str, species_name: str, output_dir: str, base_dir: str) -> None:
    from modules import GeneDataNormalization

    ref_path = os.path.join(base_dir, "gene_data", "cell_ref")
    normalization_instance = GeneDataNormalization(species_name, ref_path)
    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    for file in files:
        normalization_instance(data=file, output_dir=output_dir)


def data_annotation_pipeline(
    input_pattern: str,
    species_name: str,
    output_dir: str,
    base_dir: str,
    annotation_model_path: str,
) -> None:
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
        annotation_instance = AnnotationMouse(python_module_path=base_dir)
    else:
        homologous_gene_dir = os.path.join(base_dir, "gene_data", "homologous_gene")
        annotation_instance = AnnotationOtherSpecie(
            annotation_path=annotation_model_path,
            python_module_path=base_dir,
        )

    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    for file in files:
        annotation_instance(
            data=file,
            specie=species_name,
            output_dir=output_dir,
            homologous_gene_dir=homologous_gene_dir,
        )


def gene_mapping_pipeline(input_pattern: str, species_name: str, output_dir: str, base_dir: str) -> None:
    from modules import GeneMapping

    mapping_ref_path = os.path.join(base_dir, "gene_data", "cell_ref")
    mapping_instance = GeneMapping(ref_path=mapping_ref_path, output_dir=output_dir)
    files = _ensure_files(_sorted_files(input_pattern), input_pattern)
    for file in files:
        mapping_instance(data=file, specie=species_name, output_dir=output_dir)


def gene_merging_pipeline(
    filter_dir: str,
    mapping_dir: str,
    species_name: str,
    output_dir: str,
    metadata_path: str,
) -> None:
    from modules import SpeciesDataProcessor

    merge_instance = SpeciesDataProcessor(species_name, filter_dir, mapping_dir, output_dir, metadata_path)
    merge_instance()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="scCompass data processing pipeline")
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
        choices=["filter", "normalize", "annotate", "map", "merge"],
        help="One or more pipeline steps to run in order",
    )
    parser.add_argument("--input-pattern", help="Glob for raw CSV files (used by filter)")
    parser.add_argument("--filtered-pattern", help="Glob for filtered CSV files (used by normalize/annotate)")
    parser.add_argument("--annotated-pattern", help="Glob for annotated CSV files (used by map)")
    parser.add_argument("--filter-output", default="/path/to/outputs/filtered_data")
    parser.add_argument("--normalize-output", default="/path/to/outputs/normalization_data")
    parser.add_argument("--annotate-output", default="/path/to/outputs/annotated_data")
    parser.add_argument("--map-output", default="/path/to/outputs/mapping_data")
    parser.add_argument("--merge-output", default="/path/to/outputs/merged_data")
    parser.add_argument("--metadata-path", help="Metadata directory containing <species>.xlsx (used by merge)")
    parser.add_argument(
        "--annotation-model-path",
        default="/path/to/modules/scimilarity/models/annotation_model_v1",
        help="Path to scimilarity annotation model directory",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
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

    for step in args.steps:
        if step == "filter":
            if not args.input_pattern:
                raise ValueError("--input-pattern is required when step includes 'filter'")
            data_filtering_pipeline(args.input_pattern, args.species, args.filter_output, base_dir)
        elif step == "normalize":
            if not args.filtered_pattern:
                raise ValueError("--filtered-pattern is required when step includes 'normalize'")
            data_normalization_pipeline(args.filtered_pattern, args.species, args.normalize_output, base_dir)
        elif step == "annotate":
            if not args.filtered_pattern:
                raise ValueError("--filtered-pattern is required when step includes 'annotate'")
            data_annotation_pipeline(
                args.filtered_pattern,
                args.species,
                args.annotate_output,
                base_dir,
                args.annotation_model_path,
            )
        elif step == "map":
            if not args.annotated_pattern:
                raise ValueError("--annotated-pattern is required when step includes 'map'")
            gene_mapping_pipeline(args.annotated_pattern, args.species, args.map_output, base_dir)
        elif step == "merge":
            if not args.metadata_path:
                raise ValueError("--metadata-path is required when step includes 'merge'")
            gene_merging_pipeline(
                args.filter_output,
                args.map_output,
                args.species,
                args.merge_output,
                args.metadata_path,
            )


if __name__ == "__main__":
    main()
