"""Load each pipeline step only when requested, keeping dependencies separate."""

from importlib import import_module


_EXPORTS = {
    "AnnotationHuman": "annotation",
    "AnnotationMouse": "annotation",
    "AnnotationOtherSpecie": "annotation",
    "GeneMapping": "gene_mapping",
    "SpeciesDataProcessor": "gene_merge",
    "Filter": "gene_data_filter",
    "HumanSexDetermine": "sex_determine",
    "MouseSexDetermine": "sex_determine",
    "GeneDataNormalization": "gene_data_normalization",
    "AnnotationFilter": "annotation_filter",
}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    globals()[name] = value
    return value
