#!/usr/bin/env Rscript

# Base-R regressions; Seurat and scMayoMap are not required for these helpers.
args <- commandArgs(trailingOnly = FALSE)
script <- sub("^--file=", "", args[grepl("^--file=", args)][1])
source(file.path(dirname(normalizePath(script)), "mouse_annotation.R"))

test_case <- function(name, expr) {
    force(expr)
    cat("PASS:", name, "\n")
}

expect_error <- function(expr, pattern) {
    error <- tryCatch({ force(expr); NULL }, error = identity)
    stopifnot(inherits(error, "error"), grepl(pattern, conditionMessage(error)))
}

test_case("numeric counts preserve cell order and gene names", {
    input <- data.frame(cell = c("cell_b", "cell_a"), Actb = c(1, 3), Cd3d = c(2, 4))
    counts <- prepare_mouse_counts(input)
    stopifnot(is.numeric(counts), identical(dim(counts), c(2L, 2L)))
    stopifnot(identical(colnames(counts), input$cell), identical(rownames(counts), c("Actb", "Cd3d")))
    stopifnot(identical(unname(counts), matrix(c(1, 2, 3, 4), nrow = 2)))
})

test_case("singleton matrices keep both axes", {
    one_cell <- prepare_mouse_counts(data.frame(cell = "a", G1 = 1, G2 = 2))
    one_gene <- prepare_mouse_counts(data.frame(cell = c("a", "b"), G1 = c(1, 2)))
    stopifnot(identical(dim(one_cell), c(2L, 1L)), identical(dim(one_gene), c(1L, 2L)))
})

test_case("duplicate genes keep the first copy without dropping dimensions", {
    input <- data.frame(cell = c("a", "b"), G1 = c(1, 2), G2 = c(9, 9))
    names(input) <- c("cell", "G1", "G1")
    counts <- prepare_mouse_counts(input)
    stopifnot(identical(dim(counts), c(1L, 2L)), identical(as.numeric(counts), c(1, 2)))
})

test_case("malformed counts and identifiers are rejected", {
    for (value in list(NA_real_, Inf, -1, "invalid")) {
        expect_error(prepare_mouse_counts(data.frame(cell = "a", G1 = value)), "finite, non-negative")
    }
    expect_error(prepare_mouse_counts(data.frame(cell = character(), G1 = numeric())), "non-empty")
    expect_error(prepare_mouse_counts(data.frame(cell = c("a", "a"), G1 = c(1, 2))), "unique")
    expect_error(prepare_mouse_counts(data.frame(cell = " ", G1 = 1)), "non-empty")
    expect_error(prepare_mouse_counts(data.frame(cell = "a", G1 = 0)), "non-zero")
})

test_case("PCA rank respects both cell and usable feature counts", {
    set.seed(42)
    many_genes <- matrix(rnorm(100 * 20), nrow = 100,
                         dimnames = list(paste0("G", seq_len(100)), NULL))
    few_genes <- matrix(rnorm(8 * 80), nrow = 8,
                        dimnames = list(paste0("G", seq_len(8)), NULL))
    stopifnot(mouse_pca_parameters(many_genes)$npcs == 19L)
    stopifnot(mouse_pca_parameters(few_genes)$npcs == 7L)
    few_genes[1, ] <- 0
    few_genes[2, ] <- NA_real_
    parameters <- mouse_pca_parameters(few_genes)
    stopifnot(parameters$npcs == 5L, identical(parameters$features, paste0("G", 3:8)))
})

test_case("insufficient cells or variation fail explicitly", {
    expect_error(mouse_pca_parameters(matrix(1, 5, 2)), "at least 3 cells")
    expect_error(mouse_pca_parameters(matrix(1, 5, 5)), "at least 3 variable genes")
})

test_case("unsupported clusters remain Unknown without losing cells", {
    scores <- matrix(c(0.8, 0.2, 0, 0, 0.1, 0.9, NA, 1, Inf, 0), ncol = 2, byrow = TRUE,
                     dimnames = list(c("0", "2", "3", "4", "5"), c("brain:T cell", "B cell")))
    clusters <- factor(c("3", "1", "0", "2", "4", "5", "0"))
    labels <- mouse_cluster_labels(as.data.frame(scores), clusters)
    stopifnot(identical(labels, c("B cell", "Unknown", "T cell", "Unknown", "Unknown", "Unknown", "T cell")))
    stopifnot(!anyNA(labels), length(labels) == length(clusters))
})

test_case("empty scores and singleton score dimensions are safe", {
    stopifnot(identical(mouse_cluster_labels(NULL, c("0", "1")), c("Unknown", "Unknown")))
    empty <- matrix(numeric(), nrow = 0L, ncol = 2L)
    stopifnot(identical(mouse_cluster_labels(empty, "0"), "Unknown"))
    single <- matrix(1, 1, 1, dimnames = list("0", "T cell"))
    stopifnot(identical(mouse_cluster_labels(single, c("0", "2")), c("T cell", "Unknown")))
    colnames(single) <- "tissue:"
    stopifnot(identical(mouse_cluster_labels(single, "0"), "Unknown"))
})

test_case("CSV output matches the zero-based single-column merge contract", {
    output <- tempfile("mouse-result-")
    dir.create(output)
    tryCatch({
        labels <- c("T cell", "Unknown", "B cell")
        result <- write_mouse_labels(labels, "sample.csv", output)
        saved <- read.csv(result, row.names = 1, check.names = FALSE)
        stopifnot(basename(result) == "sample_cell_type.csv")
        stopifnot(identical(names(saved), "0"), identical(rownames(saved), c("0", "1", "2")))
        stopifnot(identical(saved[[1]], labels))
        stopifnot(identical(list.files(output, all.files = TRUE, no.. = TRUE), basename(result)))
        expect_error(write_mouse_labels(c("T cell", NA), "invalid.csv", output), "every cell")
        stopifnot(!file.exists(file.path(output, "invalid_cell_type.csv")))
    }, finally = unlink(output, recursive = TRUE))
})

if (requireNamespace("data.table", quietly = TRUE)) {
    test_case("numeric-looking CSV cell IDs retain leading zeros", {
        input <- tempfile(fileext = ".csv")
        tryCatch({
            writeLines(c(",Actb,Cd3d", "001,1,2", "01,3,4", "1,5,6"), input)
            counts <- read_mouse_counts(input)
            stopifnot(identical(colnames(counts), c("001", "01", "1")))
            stopifnot(identical(unname(counts), matrix(1:6, nrow = 2) * 1.0))
        }, finally = unlink(input))
    })
} else {
    cat("SKIP: numeric-looking cell IDs (data.table is not installed).\n")
}

cat("All available mouse annotation helper regressions passed. Full Seurat/scMayoMap annotation was not run.\n")
