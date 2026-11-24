"""Export one mapped sample as an AnnData file with raw counts."""

import json
import os
from pathlib import Path
import tempfile

import anndata as ad
from anndata.utils import make_index_unique
import numpy as np
import pandas as pd
from scipy import sparse

from .annotation_result import valid_mouse_result


def load_metadata(path):
    """Index optional CSV or CSV.gz metadata by the sample's SourceID."""
    if path is None:
        return {}
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if "SourceID" not in frame:
        raise ValueError("h5ad metadata requires a SourceID column")
    if frame["SourceID"].str.strip().eq("").any() or not frame["SourceID"].is_unique:
        raise ValueError("h5ad metadata SourceID values must be nonempty and unique")
    return frame.set_index("SourceID", drop=False).to_dict(orient="index")


class H5adExporter:
    """Combine Filter barcodes, Map counts and optional labels and metadata."""

    chunk_rows = 256

    def __init__(self, species, mapping_dir, annotation_dir, output_dir, metadata_path=None):
        self.species = species
        self.mapping_dir = Path(mapping_dir) / species
        self.annotation_dir = Path(annotation_dir) / species
        self.output_dir = Path(output_dir) / species
        gene_order = Path(mapping_dir) / f"{species}_mapping.txt"
        genes = gene_order.read_text(encoding="utf-8").splitlines()
        if not genes or any(not gene.strip() for gene in genes):
            raise ValueError(f"Gene order must contain one nonempty ID per line: {gene_order}")
        self.var = pd.DataFrame(
            {"gene_id": genes, "mapping_column": np.arange(len(genes), dtype=np.int64)},
            index=make_index_unique(pd.Index(genes, name="gene_index"), join="-dup"),
        )
        self.metadata = load_metadata(metadata_path)

    def __call__(self, data):
        filtered = Path(data)
        sample = filtered.stem
        barcodes = pd.read_csv(filtered, usecols=[0], dtype=str, keep_default_na=False).iloc[:, 0].tolist()
        if not barcodes:
            print(f"[h5ad] Skipped {sample}: no cells passed Filter")
            return
        if any(not barcode.strip() for barcode in barcodes):
            raise ValueError(f"{sample}: empty cell barcode")
        matrix = self.mapping_dir / sample / f"{sample}.csv"
        labels_file = self.annotation_dir / sample / f"{sample}_cell_type.csv"
        labels = [None] * len(barcodes)
        annotation_status = "unavailable"
        if labels_file.exists():
            if not valid_mouse_result(labels_file, len(barcodes)):
                raise ValueError(f"{sample}: annotation labels do not match the filtered cell rows")
            labels = pd.read_csv(labels_file, index_col=0, dtype={"0": str}).iloc[:, 0].tolist()
            annotation_status = "complete"
        metadata = self.metadata.get(sample, {})
        tissue = (metadata.get("Tissue") or metadata.get("Organ") or "").strip() or None
        obs = pd.DataFrame(
            {"cell_barcode": barcodes, "sample_id": pd.Categorical([sample] * len(barcodes)),
             "species": pd.Categorical([self.species] * len(barcodes)),
             "filtered_row": np.arange(len(barcodes), dtype=np.int64),
             "cell_type": pd.Categorical(labels), "tissue": pd.Categorical([tissue] * len(barcodes)),
             "annotation_status": pd.Categorical([annotation_status] * len(barcodes))},
            index=make_index_unique(pd.Index(barcodes, name="cell_id"), join="-dup"),
        )
        counts = self.read_counts(matrix, len(barcodes))
        adata = ad.AnnData(X=counts, obs=obs, var=self.var.copy())
        adata.uns["sample_metadata_json"] = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
        adata.uns["sccompass_export"] = {
            "format_version": "1", "matrix_semantics": "raw_counts", "sample_id": sample,
            "species": self.species, "annotation_status": annotation_status,
            "normalization_applied": False, "hf_generated": False,
        }
        self.write_verified(adata, self.output_dir / f"{sample}.h5ad")

    def read_counts(self, matrix, expected_rows):
        blocks = []
        rows = 0
        with pd.read_csv(matrix, header=None, dtype=np.int64, chunksize=self.chunk_rows) as chunks:
            for chunk in chunks:
                if chunk.shape[1] != len(self.var):
                    raise ValueError(f"Matrix column count differs from gene order: {matrix}")
                block = sparse.csr_matrix(chunk.to_numpy(copy=False))
                if block.nnz and block.data.min() < 0:
                    raise ValueError(f"Mapped counts must be nonnegative: {matrix}")
                rows += block.shape[0]
                if rows > expected_rows:
                    break
                blocks.append(block)
        if rows != expected_rows:
            raise ValueError(f"Matrix row count differs from filtered cell count: {matrix}")
        return sparse.vstack(blocks, format="csr")

    def write_verified(self, adata, output):
        """Publish only a complete file whose counts and metadata round-trip."""
        output.parent.mkdir(parents=True, exist_ok=True)
        adata.strings_to_categoricals()
        fd, name = tempfile.mkstemp(prefix=f".{output.stem}.", suffix=".h5ad", dir=output.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            adata.write_h5ad(temporary, compression="gzip", compression_opts=1)
            check = ad.read_h5ad(temporary, backed="r")
            try:
                if check.shape != adata.shape or check.X.dtype != adata.X.dtype:
                    raise ValueError(f"h5ad matrix shape or dtype changed: {output}")
                pd.testing.assert_frame_equal(check.obs, adata.obs)
                pd.testing.assert_frame_equal(check.var, adata.var)
                if check.uns["sample_metadata_json"] != adata.uns["sample_metadata_json"]:
                    raise ValueError(f"h5ad sample metadata changed: {output}")
                stored_counts = check.X
                for start in range(0, adata.n_obs, self.chunk_rows):
                    rows = slice(start, start + self.chunk_rows)
                    expected = adata.X[rows]
                    stored = stored_counts[rows, :]
                    if not all(np.array_equal(getattr(stored, field), getattr(expected, field))
                               for field in ("data", "indices", "indptr")):
                        raise ValueError(f"h5ad counts changed at row {start}: {output}")
            finally:
                check.file.close()
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
