# scCompass

scCompass 是一个用于多物种 scRNA-seq 数据预处理与整合的流水线项目，包含以下核心步骤：

- `filter`: 基础质控与基因过滤
- `normalize`: 基因表达归一化与 token 化
- `annotate`: 细胞类型注释
- `map`: 将基因映射到统一核心基因空间
- `merge`: 按物种与器官聚合细胞数据

## 1. 环境准备

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

说明：

- 注释步骤会依赖 `rpy2` 和 R 侧包（鼠标注释路径）。
- `gene_data/` 下需要保留项目自带参考文件（基因列表、token、mid values 等）。

## 2. 目录约定

- 输入原始矩阵（CSV）可自定义路径，通过 `--input-pattern` 指定
- 默认输出目录：
  - `outputs/filtered_data`
  - `outputs/normalization_data`
  - `outputs/annotated_data`
  - `outputs/mapping_data`
  - `outputs/merged_data`

## 3. 命令行使用

统一入口：`main.py`

```bash
python main.py --species <species> --steps <step1> [<step2> ...] [参数]
```

### 3.1 仅执行过滤

```bash
python main.py \
  --species human \
  --steps filter \
  --project-root /path/to/scCompass \
  --input-pattern "/path/to/data/raw/human/*.csv" \
  --filter-output /path/to/outputs/filtered_data
```

### 3.2 过滤 + 归一化 + 注释

```bash
python main.py \
  --species mouse \
  --steps filter normalize annotate \
  --project-root /path/to/scCompass \
  --input-pattern "/path/to/data/raw/mouse/*.csv" \
  --filtered-pattern "/path/to/outputs/filtered_data/mouse/*/*.csv" \
  --annotation-model-path /path/to/modules/scimilarity/models/annotation_model_v1
```

### 3.3 基因映射

```bash
python main.py \
  --species human \
  --steps map \
  --project-root /path/to/scCompass \
  --annotated-pattern "/path/to/outputs/annotated_data/human/*/*.csv" \
  --map-output /path/to/outputs/mapping_data
```

### 3.4 数据合并

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

`--metadata-path` 目录下需要包含 `<species>.xlsx`（例如 `human.xlsx`），并至少包含 `Organ` 列。

## 4. 主要参数说明

- `--species`: 物种标识，例如 `human`、`mouse`、`monkey`
- `--steps`: 要执行的步骤，支持组合：`filter normalize annotate map merge`
- `--project-root`: 项目根目录（应包含 `gene_data/` 与 `modules/`）
- `--input-pattern`: 原始 CSV 输入 glob（`filter` 必填）
- `--filtered-pattern`: 过滤后 CSV 输入 glob（`normalize`/`annotate` 必填）
- `--annotated-pattern`: 注释后 CSV 输入 glob（`map` 必填）
- `--annotation-model-path`: 注释模型路径
- `--metadata-path`: 合并步骤使用的元数据目录（`merge` 必填）

## 5. 常见问题

- `No files matched pattern`: 传入的 glob 没有匹配到文件，请检查路径和引号。
- `--xxx is required`: 你选择了某个步骤，但缺少该步骤必需参数。
- 注释步骤报 R 相关错误：请确认本机 R 环境与相关 R 包已安装。

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
