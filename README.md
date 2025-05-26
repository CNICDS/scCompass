# scRNA-seq Data Download and Analysis Pipeline

## I. Data Download

1. **Search GEO for Data**:
   ```
   "mus"[Organism] AND "high throughput sequencing"[Platform Technology Type] AND 10X
   ```

2. **Filter Criteria**:
   - Library strategy: RNA-Seq
   - Library source: transcriptomic or transcriptomic single cell
   - Must meet one of these conditions:
     - **Condition A**: "10X" appears in the top information section.
     - **Condition B**: "cellranger" is mentioned in the "Data processing" section.

3. **Download and Validate Files**:
   - Download the relevant **SRA files** from the GSM page.
   - Validate the downloaded files to ensure integrity:

   ```bash
   # Validate a single SRA file
   vdb-validate [sra_file]

   # Validate multiple files listed in a text file
   for sra in $(cat sra_files.txt); do
       vdb-validate $sra;
   done
   ```

---

## II. Data Analysis

### 1. Install Required Tools

- Install **sratoolkit**, **pfastq-dump**, **cellranger**, and **Seurat** (R package). Below are installation commands for the key tools:

   ```bash
   # Download sratoolkit
   wget https://ftp-trace.ncbi.nlm.nih.gov/sra/sdk/2.11.0/sratoolkit.2.11.0-ubuntu64.tar.gz

   # Install pfastq-dump
   git clone https://github.com/inutano/pfastq-dump
   cd pfastq-dump
   chmod a+x bin/pfastq-dump
   ```

- Download the **mouse reference genome** required for CellRanger:

   ```bash
   wget https://cf.10xgenomics.com/supp/cell-exp/refdata-gex-mm10-2020-A.tar.gz
   ```

### 2. Convert SRA Files to FASTQ

- Convert the downloaded **SRA files** to FASTQ format for further analysis.

   ```bash
   # Convert SRA to FASTQ using single-threaded mode
   fastq-dump --split-files --gzip [SRA_file]

   # Convert using multi-threaded mode
   pfastq-dump --threads 16 --gzip --split-files -s [SRA_file]
   ```

- Rename the output FASTQ files for compatibility with downstream analysis:

   ```bash
   # If 2 files are generated:
   mv ${i}_1.fastq.gz ${i}_S1_L001_R1_001.fastq.gz
   mv ${i}_2.fastq.gz ${i}_S1_L001_R2_001.fastq.gz

   # If 3 files are generated:
   mv ${i}_1.fastq.gz ${i}_S1_L001_I1_001.fastq.gz
   mv ${i}_2.fastq.gz ${i}_S1_L001_R1_001.fastq.gz
   mv ${i}_3.fastq.gz ${i}_S1_L001_R2_001.fastq.gz
   ```

---

### 3. Perform Quantification with CellRanger

```bash
# Example CellRanger command for quantification
cellranger count \
--id=SRR19908681 \
--localcores=16 \
--transcriptome=/data5/yana/database/cellranger_V7.0.1/mm10_cellranger/refdata-gex-mm10-2020-A/ \
--fastqs=/data5/yana/raw_data/scRNA-seq/GSE207275_aorta/ \
--sample=SRR19908681 \
--nosecondary
```

**Note**: Each sample’s output will be stored in a folder named by the sample ID.

---

### 3. Perform Quantification with CellRanger

```bash
# Example CellRanger command for quantification
cellranger count \
--id=SRR19908681 \
--localcores=16 \
--transcriptome=/data5/yana/database/cellranger_V7.0.1/mm10_cellranger/refdata-gex-mm10-2020-A/ \
--fastqs=/data5/yana/raw_data/scRNA-seq/GSE207275_aorta/ \
--sample=SRR19908681 \
--nosecondary
```

**Note**: Each sample’s output will be stored in a folder named by the sample ID.

---

### 4. Analyze Data with Seurat (R)

Below is the R code for **Seurat** to analyze the scRNA-seq data:

```R
# Load the Seurat package
library(Seurat)

# Load data from the output directory
SRR19908681.data <- Read10X(data.dir = "./outdir(SRR19908681)/outs/filtered_feature_bc_matrix")

# Create a Seurat object
SRR19908681 <- CreateSeuratObject(counts = SRR19908681.data, project = "SRR19908681", min.cells = 3, min.features = 200)

# Add mitochondrial gene percentage to metadata
SRR19908681[["percent.mt"]] <- PercentageFeatureSet(SRR19908681, pattern = "^mt-")

# Filter cells: keep cells with >200 genes and <15% mitochondrial content
SRR19908681 <- subset(SRR19908681, subset = nFeature_RNA > 200 & percent.mt < 15)

# Save RNA counts to a CSV file
write.csv(SRR19908681@assays$RNA@counts, "SRR19908681_count.csv", row.names = TRUE, quote = FALSE)
```

---

## III. Summary and Automation

- **Batch processing**: This pipeline can be wrapped into a shell script to handle multiple datasets efficiently.  
- **Customization**: Adjust paths and sample IDs as needed for your specific data.  
- **Verification**: Ensure all tools and reference files are correctly installed and accessible within your environment.

---

**Note**: This guide provides a complete workflow from data download to analysis. Ensure the integrity of each step by testing with sample data before running on large datasets.

## Citation
```
@article{wang2025sccompass,
  title={scCompass: An Integrated Multi-Species scRNA-seq Database for AI-Ready},
  author={Wang, Pengfei and Liu, Wenhao and Wang, Jiajia and Liu, Yana and Li, Pengjiang and Xu, Ping and Cui, Wentao and Zhang, Ran and Long, Qingqing and Hu, Zhilong and others},
  journal={Advanced Science},
  pages={2500870},
  year={2025},
  publisher={Wiley Online Library}
}
```
