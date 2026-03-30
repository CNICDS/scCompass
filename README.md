# scCompass

English | [中文](README_zh.md)

scCompass is a multi-species scRNA-seq preprocessing and integration pipeline with the following core steps:

- `filter`: basic QC and gene filtering
- `normalize`: expression normalization and tokenization
- `annotate`: cell type annotation
- `map`: map genes to a unified core-gene space
- `merge`: aggregate cells by species and organ

## 1. Environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Notes:

- The annotation step depends on `rpy2` and R packages (mouse annotation path).
- Keep reference files under `gene_data/` (gene lists, tokens, mid values, etc.).

## 2. Directory Conventions

- Input CSVs are provided via `--input-pattern`.
- Default output directories:
  - `/path/to/outputs/filtered_data`
  - `/path/to/outputs/normalization_data`
  - `/path/to/outputs/annotated_data`
  - `/path/to/outputs/mapping_data`
  - `/path/to/outputs/merged_data`

## 3. CLI Usage

Single entrypoint: `main.py`

```bash
python main.py --species <species> --steps <step1> [<step2> ...] [args]
```

### 3.1 Filter only

```bash
python main.py \
  --species human \
  --steps filter \
  --project-root /path/to/scCompass \
  --input-pattern "/path/to/data/raw/human/*.csv" \
  --filter-output /path/to/outputs/filtered_data
```

### 3.2 Filter + Normalize + Annotate

```bash
python main.py \
  --species mouse \
  --steps filter normalize annotate \
  --project-root /path/to/scCompass \
  --input-pattern "/path/to/data/raw/mouse/*.csv" \
  --filtered-pattern "/path/to/outputs/filtered_data/mouse/*/*.csv" \
  --annotation-model-path /path/to/modules/scimilarity/models/annotation_model_v1
```

### 3.3 Gene mapping

```bash
python main.py \
  --species human \
  --steps map \
  --project-root /path/to/scCompass \
  --annotated-pattern "/path/to/outputs/annotated_data/human/*/*.csv" \
  --map-output /path/to/outputs/mapping_data
```

### 3.4 Merge

```bash
python main.py \
  --species human \
  --steps merge \
  --project-root /path/to/scCompass \
  --filter-output /path/to/outputs/filtered_data \
  --map-output /path/to/outputs/mapping_data \
  --merge-output /path/to/outputs/merged_data \
  --metadata-path /path/to/metadata
```

`--metadata-path` must contain `<species>.xlsx` (for example `human.xlsx`) with at least the `Organ` column.

## 4. Key Parameters

- `--species`: species key, e.g. `human`, `mouse`, `monkey`
- `--steps`: steps to run, any combination of `filter normalize annotate map merge`
- `--project-root`: project root containing `gene_data/` and `modules/`
- `--input-pattern`: input CSV glob (required for `filter`)
- `--filtered-pattern`: filtered CSV glob (required for `normalize`/`annotate`)
- `--annotated-pattern`: annotated CSV glob (required for `map`)
- `--annotation-model-path`: annotation model path
- `--metadata-path`: metadata directory for `merge`

## 5. FAQ

- `No files matched pattern`: the glob didn’t match any files; check the path and quoting.
- `--xxx is required`: a required argument for the selected step is missing.
- R-related errors in annotation: ensure local R and required R packages are installed.

## Citation

```bibtex
@article{wang2025sccompass,
  title={scCompass: An Integrated Multi-Species scRNA-seq Database for AI-Ready},
  author={Wang, Pengfei and Liu, Wenhao and Wang, Jiajia and Liu, Yana and Li, Pengjiang and Xu, Ping and Cui, Wentao and Zhang, Ran and Long, Qingqing and Hu, Zhilong and others},
  journal={Advanced Science},
  pages={2500870},
  year={2025},
  publisher={Wiley Online Library}
}
```
