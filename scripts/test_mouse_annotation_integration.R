#!/usr/bin/env Rscript

# Opt-in end-to-end smoke test; requires all mouse annotation R dependencies.
# Run: Rscript scripts/test_mouse_annotation_integration.R
args <- commandArgs(trailingOnly = FALSE)
script <- sub("^--file=", "", args[grepl("^--file=", args)][1])
source(file.path(dirname(normalizePath(script)), "mouse_annotation.R"))

run_integration <- function() {
    set.seed(42)
    directory <- tempfile("mouse-integration-")
    dir.create(directory)
    on.exit(unlink(directory, recursive = TRUE), add = TRUE)

    # Two distinct expression programs plus background variation, with cell
    # IDs that would collide if fread inferred them as numbers.
    t_genes <- c("Cd3d", "Cd3e", "Cd3g", "Cd247", "Lck", "Cd2", "Il7r", "Tcf7")
    b_genes <- c("Ms4a1", "Cd79a", "Cd79b", "Cd74", "Cd37", "Cd22", "Cd19", "Bank1")
    genes <- c(t_genes, b_genes, paste0("Background", seq_len(484)))
    counts <- matrix(rpois(120L * length(genes), lambda = 1), nrow = 120L,
                     dimnames = list(c("001", "01", "1", paste0("cell", 4:120)), genes))
    counts[1:60, t_genes] <- rpois(60L * length(t_genes), lambda = 30)
    counts[61:120, b_genes] <- rpois(60L * length(b_genes), lambda = 30)
    input <- file.path(directory, "sample.csv")
    write.csv(counts, input)

    result <- annotate_mouse(input, file.path(directory, "output"))
    labels <- read.csv(result, row.names = 1, check.names = FALSE)
    stopifnot(identical(names(labels), "0"), nrow(labels) == 120L)
    stopifnot(identical(rownames(labels), as.character(0:119)))
    stopifnot(!anyNA(labels[[1]]), all(nzchar(labels[[1]])))
    print(table(program = rep(c("T", "B"), each = 60L), prediction = labels[[1]]))
    # Allow subtypes such as "T memory cell" and "Memory B cell".
    stopifnot(mean(grepl("\\bT\\b.*\\bcell\\b", labels[[1]][1:60],
                         ignore.case = TRUE, perl = TRUE)) > 0.9)
    stopifnot(mean(grepl("\\bB\\b.*\\bcell\\b", labels[[1]][61:120],
                         ignore.case = TRUE, perl = TRUE)) > 0.9)
    cat("PASS: full mouse annotation preserves 120 cells and identifies both synthetic programs.\n")

    small_input <- file.path(directory, "small.csv")
    write.csv(counts[c(1:4, 61:64), ], small_input)
    small_result <- annotate_mouse(small_input, file.path(directory, "small-output"))
    small_labels <- read.csv(small_result, row.names = 1, check.names = FALSE)
    stopifnot(nrow(small_labels) == 8L, identical(rownames(small_labels), as.character(0:7)))
    stopifnot(!anyNA(small_labels[[1]]), all(nzchar(small_labels[[1]])))
    cat("PASS: small mouse sample runs with fewer cells than default PCA/neighbor dimensions.\n")
}

run_integration()
