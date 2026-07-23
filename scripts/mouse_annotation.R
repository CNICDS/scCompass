#!/usr/bin/env Rscript

# Mouse cell-type annotation using Seurat + scMayoMap.
#
# Usage:
#   Rscript mouse_annotation.R <input_csv> <outfile_dir> <species>
#
# Output:
#   <outfile_dir>/<sample>_cell_type.csv
#
# The output layout mirrors the human/other-species annotation path
# (modules/annotation.py): one row per cell, indexed by the cell's 0-based
# row position in the count matrix, with a single predicted cell-type column.
# The downstream `merge` step (modules/gene_merge.py) selects rows by that
# integer position, so the index MUST be 0-based integers, not cell barcodes.

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
    stop("Usage: Rscript mouse_annotation.R <input_csv> <outfile_dir> [species]")
}
input_file <- args[1]
outfile_dir <- args[2]
species <- if (length(args) >= 3) args[3] else "mouse"

suppressPackageStartupMessages({
    library(Seurat)
    library(data.table)
    library(dplyr)
    library(ggplot2)
    library(scMayoMap)
})

# Read and process the input data
cat("[INFO] Reading input file:", input_file, "\n")
count_matrix <- fread(input_file)
count_matrix2 <- t(count_matrix)
colnames(count_matrix2) <- count_matrix2[1, ]
count_matrix2 <- count_matrix2[-1, ]
# Drop duplicated gene rows so CreateSeuratObject does not fail on
# non-unique feature names.
count_matrix2 <- count_matrix2[!duplicated(rownames(count_matrix2)), ]

# Create Seurat object and run the standard clustering pipeline
seurat.obj <- CreateSeuratObject(counts = count_matrix2)
seurat.obj$percent.mt <- PercentageFeatureSet(object = seurat.obj, pattern = "^mt-")
seurat.obj <- NormalizeData(object = seurat.obj, verbose = FALSE)
seurat.obj <- FindVariableFeatures(object = seurat.obj, verbose = FALSE)
seurat.obj <- ScaleData(object = seurat.obj, verbose = FALSE)
seurat.obj <- RunPCA(object = seurat.obj, verbose = FALSE)
seurat.obj <- FindNeighbors(object = seurat.obj, verbose = FALSE)
seurat.obj <- FindClusters(object = seurat.obj, verbose = FALSE)

# Annotation using scMayoMap
seurat.markers <- FindAllMarkers(seurat.obj, method = 'MAST', verbose = FALSE)
scMayoMap.obj <- scMayoMap(data = seurat.markers, database = scMayoMapDatabase)

# Map clusters to cell types
max_col_names <- apply(scMayoMap.obj$annotation.norm, 1, function(x) colnames(scMayoMap.obj$annotation.norm)[which.max(x)])
seurat.obj@meta.data$celltype <- plyr::mapvalues(
    seurat.obj@meta.data$seurat_clusters, from = names(max_col_names), to = max_col_names
)
seurat.obj@meta.data$celltype <- as.character(seurat.obj@meta.data$celltype)
seurat.obj@meta.data$celltype <- sapply(seurat.obj@meta.data$celltype, function(x) strsplit(x, ":")[[1]][2])
celltype_col <- seurat.obj@meta.data[, 'celltype', drop = FALSE]

# Re-index by 0-based cell position and use a stable single-column layout so
# the output matches what the Python annotation path writes for other species.
rownames(celltype_col) <- seq_len(nrow(celltype_col)) - 1
colnames(celltype_col) <- "0"

# Save the results. basename() without an extension arg keeps the ".csv"
# suffix, so strip it explicitly to get "<sample>_cell_type.csv".
sample <- sub("\\.csv$", "", basename(input_file))
if (!dir.exists(outfile_dir)) {
    dir.create(outfile_dir, recursive = TRUE)
}
output_file <- file.path(outfile_dir, paste0(sample, "_cell_type.csv"))
write.csv(celltype_col, file = output_file, row.names = TRUE)
cat("[INFO] Data exported:", output_file, "\n")
