#!/usr/bin/env Rscript

# Mouse cell-type annotation using Seurat + scMayoMap.
# Usage: Rscript mouse_annotation.R <input_csv> <outfile_dir> [mouse]
# Output: <outfile_dir>/<sample>_cell_type.csv, indexed by 0-based cell
# position in the input matrix, as required by modules/gene_merge.py.

prepare_mouse_counts <- function(count_table) {
    count_table <- as.data.frame(count_table)
    if (nrow(count_table) == 0L || ncol(count_table) < 2L) {
        stop("Mouse annotation requires a non-empty cells-by-genes CSV with cell IDs in the first column.")
    }
    cell_ids <- as.character(count_table[[1]])
    if (anyNA(cell_ids) || any(!nzchar(trimws(cell_ids))) || anyDuplicated(cell_ids)) {
        stop("Cell IDs must be non-empty and unique.")
    }

    # Keep identifiers out of the numeric matrix and preserve singleton axes.
    counts <- as.matrix(count_table[, -1, drop = FALSE])
    suppressWarnings(storage.mode(counts) <- "double")
    if (any(!is.finite(counts)) || any(counts < 0)) {
        stop("Expression counts must be finite, non-negative numbers.")
    }
    counts <- t(counts)
    rownames(counts) <- names(count_table)[-1]
    colnames(counts) <- cell_ids
    if (anyNA(rownames(counts)) || any(!nzchar(trimws(rownames(counts))))) {
        stop("Gene names must be non-empty.")
    }
    # Retain the existing policy of keeping the first duplicate gene.
    counts <- counts[!duplicated(rownames(counts)), , drop = FALSE]
    if (any(colSums(counts) == 0)) {
        stop("Every input cell must have non-zero total expression.")
    }
    counts
}

read_mouse_counts <- function(input_file) {
    # Cell IDs such as "001" and "1" must remain distinct identifiers.
    prepare_mouse_counts(data.table::fread(
        input_file, data.table = FALSE, check.names = FALSE,
        colClasses = list(character = 1L)
    ))
}

mouse_pca_parameters <- function(scaled_counts) {
    if (ncol(scaled_counts) < 3L) {
        stop("Mouse annotation requires at least 3 cells for PCA.")
    }
    variances <- apply(scaled_counts, 1, var)
    features <- rownames(scaled_counts)[is.finite(variances) & variances > 0]
    npcs <- min(50L, ncol(scaled_counts) - 1L, length(features) - 1L)
    if (npcs < 2L) {
        stop("Mouse annotation requires at least 3 variable genes for PCA.")
    }
    list(features = features, npcs = npcs)
}

mouse_cluster_labels <- function(scores, clusters) {
    labels <- rep("Unknown", length(clusters))
    if (is.null(scores) || nrow(scores) == 0L || ncol(scores) == 0L) {
        return(labels)
    }
    scores <- as.matrix(scores)
    if (is.null(rownames(scores)) || is.null(colnames(scores))) {
        stop("scMayoMap scores must have cluster and cell-type names.")
    }
    cluster_labels <- vapply(seq_len(nrow(scores)), function(i) {
        values <- scores[i, ]
        if (any(!is.finite(values)) || max(values) <= 0) {
            return("Unknown")
        }
        label <- colnames(scores)[which.max(values)]
        # Database labels may be tissue:celltype or plain celltype strings.
        label <- trimws(sub("^[^:]*:", "", label))
        if (is.na(label) || !nzchar(label)) "Unknown" else label
    }, character(1))
    names(cluster_labels) <- rownames(scores)
    matched <- cluster_labels[as.character(clusters)]
    labels[!is.na(matched)] <- matched[!is.na(matched)]
    labels
}

write_mouse_labels <- function(labels, input_file, outfile_dir) {
    if (length(labels) == 0L || anyNA(labels) || any(!nzchar(trimws(labels)))) {
        stop("Mouse annotation must produce a non-empty label for every cell.")
    }
    celltype_col <- data.frame(labels, stringsAsFactors = FALSE)
    rownames(celltype_col) <- seq_along(labels) - 1L
    colnames(celltype_col) <- "0"
    sample <- sub("\\.csv$", "", basename(input_file))
    if (!dir.exists(outfile_dir)) {
        dir.create(outfile_dir, recursive = TRUE)
    }
    output_file <- file.path(outfile_dir, paste0(sample, "_cell_type.csv"))
    # Publish only a complete CSV, so interrupted writes cannot look finished.
    pending_file <- tempfile(pattern = ".mouse-labels-", tmpdir = outfile_dir)
    on.exit(unlink(pending_file), add = TRUE)
    write.csv(celltype_col, file = pending_file, row.names = TRUE)
    if (!file.rename(pending_file, output_file)) {
        stop("Could not publish mouse annotation result: ", output_file)
    }
    cat("[INFO] Data exported:", output_file, "\n")
    invisible(output_file)
}

annotate_mouse <- function(input_file, outfile_dir, species = "mouse") {
    if (species != "mouse") {
        stop("This annotation script supports only mouse.")
    }
    suppressPackageStartupMessages({
        library(Seurat)
        library(data.table)
        library(dplyr)
        library(ggplot2)
        library(scMayoMap)
    })
    if (!requireNamespace("MAST", quietly = TRUE)) {
        stop("Mouse annotation requires the MAST R package for marker detection.")
    }

    cat("[INFO] Reading input file:", input_file, "\n")
    counts <- read_mouse_counts(input_file)
    if (ncol(counts) < 3L || nrow(counts) < 3L) {
        stop("Mouse annotation requires at least 3 cells and 3 genes.")
    }
    input_cell_ids <- colnames(counts)
    seurat.obj <- CreateSeuratObject(counts = counts)
    seurat.obj$percent.mt <- PercentageFeatureSet(object = seurat.obj, pattern = "^mt-")
    seurat.obj <- NormalizeData(object = seurat.obj, verbose = FALSE)
    seurat.obj <- FindVariableFeatures(object = seurat.obj, verbose = FALSE)
    seurat.obj <- ScaleData(object = seurat.obj, verbose = FALSE)
    if (utils::packageVersion("SeuratObject") >= "5.0.0") {
        scaled_counts <- GetAssayData(seurat.obj, layer = "scale.data")
    } else {
        scaled_counts <- GetAssayData(seurat.obj, slot = "scale.data")
    }
    pca <- mouse_pca_parameters(scaled_counts)
    seurat.obj <- RunPCA(object = seurat.obj, features = pca$features,
                         npcs = pca$npcs, verbose = FALSE)
    dims <- seq_len(min(10L, ncol(Embeddings(seurat.obj, reduction = "pca"))))
    seurat.obj <- FindNeighbors(object = seurat.obj, dims = dims,
                                k.param = min(20L, ncol(seurat.obj) - 1L), verbose = FALSE)
    seurat.obj <- FindClusters(object = seurat.obj, verbose = FALSE)

    # A single cluster has no comparison group for differential expression.
    scores <- NULL
    clusters <- seurat.obj$seurat_clusters
    if (length(unique(clusters)) > 1L) {
        seurat.markers <- FindAllMarkers(seurat.obj, test.use = "MAST", verbose = FALSE)
        if (nrow(seurat.markers) > 0L) {
            # scMayoMap cannot score a completely empty marker/database join.
            eligible <- !is.na(seurat.markers$p_val_adj) & seurat.markers$p_val_adj <= 0.05 &
                !is.na(seurat.markers$pct.1) & seurat.markers$pct.1 >= 0.25
            matched <- toupper(seurat.markers$gene) %in% scMayoMapDatabase$gene
            if (any(eligible & matched)) {
                scores <- scMayoMap(data = seurat.markers,
                                   database = scMayoMapDatabase)$annotation.norm
            }
        }
    }
    labels <- mouse_cluster_labels(scores, clusters)
    if (any(labels == "Unknown")) {
        warning(sum(labels == "Unknown"), " cells have no supported cell-type prediction; labelled Unknown.")
    }
    # Explicitly restore CSV row order before writing positional indices.
    cell_order <- match(input_cell_ids, colnames(seurat.obj))
    if (anyNA(cell_order) || length(labels) != length(input_cell_ids)) {
        stop("Seurat cell identities no longer match the input CSV.")
    }
    write_mouse_labels(labels[cell_order], input_file, outfile_dir)
}

if (sys.nframe() == 0L) {
    args <- commandArgs(trailingOnly = TRUE)
    if (length(args) < 2L || length(args) > 3L) {
        stop("Usage: Rscript mouse_annotation.R <input_csv> <outfile_dir> [mouse]")
    }
    annotate_mouse(args[1], args[2], if (length(args) == 3L) args[3] else "mouse")
}
