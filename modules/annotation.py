import os
import sys
import subprocess
import pandas as pd
import datetime

from .annotation_result import input_cell_count, valid_mouse_result


def _load_scimilarity():
    # Only human/other-species annotation needs the vendored ML stack.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from scimilarity.src.scimilarity.utils import lognorm_counts
    from scimilarity.src.scimilarity import CellAnnotation, align_dataset
    return CellAnnotation, align_dataset, lognorm_counts


def _resolve_input(afile):
    """Resolve the CSV input file and sample name from either a CSV file
    path (e.g. ``.../<gsm>/<gsm>.csv``) or the sample directory that
    contains it (e.g. ``.../<gsm>``)."""
    afile = os.fspath(afile)
    if os.path.isfile(afile):
        count_file = os.path.basename(afile)
        if count_file.endswith(".csv"):
            count_file = count_file[:-4]
        return afile, count_file
    count_file = os.path.basename(afile.rstrip("/\\"))
    return f"{afile}/{count_file}.csv", count_file


class AnnotationHuman:
    def __init__(self, annotation_path, python_module_path):
        """
        Initialize the class and set required paths.
        """
        # Set Python module paths
        sys.path.append(python_module_path)
        print("scimilarity imported!")

        # Initialize path variables
        self.annotation_path = annotation_path
        CellAnnotation, self.align_dataset, self.lognorm_counts = _load_scimilarity()
        self.cell_annotation = CellAnnotation(model_path=self.annotation_path)

    def write_logs(self, out_path, step_num, cell_num, success):
        """
        Write logs to the specified path.
        """
        write_logs_path = os.path.join(out_path, "logs.txt")
        outtxt = f"step: {step_num}: {cell_num}\n" if cell_num != "-1" else ""
        outtxt += "success\n" if success else ""

        with open(write_logs_path, 'a') as f:
            f.write(outtxt)

    def transform(self, afile, **kwargs):
        """
        Main function to perform the annotation task.
        """
        specie = kwargs.get('specie', 'human')
        output_dir = kwargs.get('output_dir', os.path.join(os.getcwd(), "annotated_data"))
        input_file, count_file = _resolve_input(afile)
        outfile_dir = f"{output_dir}/{specie}/{count_file}"
        tmpfile = f"{output_dir}/{specie}/{count_file}.tmp"

        # Check if the input directory exists
        if not os.path.exists(afile):
            print(f"[Error] Input path does not exist: {afile}")
            return

        print(f"Annotation Start, [Specie]: {specie}\n[Input Dir]: {afile} [Size]: {os.path.getsize(afile)}\n")

        if not os.path.exists(input_file):
            print(f"[Error] Input file does not exist: {input_file}")
            return

        print(f"Output Dir: {outfile_dir}")

        if os.path.exists(outfile_dir):
            print(f"Ignored, already completed: {afile}")
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)
            return

        if os.path.exists(tmpfile):
            print(f"Ignored, in progress: {afile}")
            return

        os.makedirs(outfile_dir, exist_ok=True)

        with open(tmpfile, 'w') as f:
            f.write(afile)

        # Start the annotation process
        START_TIME = datetime.datetime.now()
        try:
            import scanpy as sc
            import anndata as ad

            df = pd.read_csv(input_file, header=0, index_col=0)
            adata = ad.AnnData(df)
            print("Original rows, columns: ", adata.shape[0], adata.shape[1])

            if adata.shape[0] == 0:
                self.write_logs(outfile_dir, "7", "-1", True)
                return

            # Align, normalize, and perform dimensionality reduction
            adata = self.align_dataset(adata, self.cell_annotation.gene_order)
            adata.layers["counts"] = adata.X.copy()
            adata = self.lognorm_counts(adata)
            adata.obsm['X_scimilarity'] = self.cell_annotation.get_embeddings(adata.X)

            sc.pp.neighbors(adata, use_rep='X_scimilarity')
            sc.tl.umap(adata)

            # Generate annotation results
            predictions, nn_idxs, nn_dists, nn_stats = self.cell_annotation.get_predictions_kNN(
                adata.obsm['X_scimilarity']
            )
            type_res = pd.DataFrame(predictions.values)

            END_TIME = datetime.datetime.now()
            print(f"Time Cost: {(END_TIME - START_TIME).seconds} seconds")

            # Export results
            type_res.to_csv(os.path.join(outfile_dir, f"{count_file}_cell_type.csv"), index=True)
            print("Data exported")

        finally:
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)
            self.write_logs(outfile_dir, "7", "-1", True)

    def __call__(self, data, **kwargs):
        """
        Entry function to execute the transform method.
        """
        return self.transform(data, **kwargs)


class AnnotationMouse:
    def __init__(self, python_module_path, r_script_path=None):
        """
        Initialize the class and set required paths.

        ``r_script_path`` points at ``scripts/mouse_annotation.R``; it defaults
        to the copy shipped alongside the project root (``python_module_path``).
        """

        # Set Python module paths
        sys.path.append(python_module_path)

        # Locate the Rscript driver. Mouse annotation runs Seurat + scMayoMap
        # through an external Rscript process rather than rpy2, so the R logic
        # is shared with the standalone scripts/annotation_mouse.sh driver.
        self.r_script_path = r_script_path or os.path.join(
            python_module_path, "scripts", "mouse_annotation.R"
        )
        print("Mouse annotation uses Seurat + scMayoMap via Rscript.")

    @staticmethod
    def _valid_result(result_file, expected_cells=None):
        """Check the single-column, positional-index contract used by merge."""
        return valid_mouse_result(result_file, expected_cells)

    def write_logs(self, out_path, step_num, cell_num, success):
        """
        Write logs to the specified path.
        """
        write_logs_path = os.path.join(out_path, "logs.txt")
        outtxt = f"step: {step_num}: {cell_num}\n" if cell_num != "-1" else ""
        outtxt += "success\n" if success else ""

        # Append log content to the log file
        with open(write_logs_path, 'a') as f:
            f.write(outtxt)

    def transform(self, afile, **kwargs):
        """
        Main function to perform the annotation task.
        """
        specie = kwargs.get('specie', 'mouse')  # Default specie is 'mouse'
        if specie != 'mouse':
            raise ValueError("Mouse annotation supports only mouse.")
        output_dir = kwargs.get('output_dir', os.path.join(os.getcwd(), "annotated_data"))

        input_file, count_file = _resolve_input(afile)
        outfile_dir = f"{output_dir}/{specie}/{count_file}"
        tmpfile = f"{output_dir}/{specie}/{count_file}.tmp"
        result_file = os.path.join(outfile_dir, f"{count_file}_cell_type.csv")

        if not os.path.isfile(input_file):
            raise FileNotFoundError(f"Mouse annotation input file not found: {input_file}")

        print(f"Annotation Start, [Specie]: {specie}\n[Input File]: {input_file} [Size]: {os.path.getsize(input_file)}\n")
        print(f"Output Dir: {outfile_dir}")

        # Acquire the same lock used by the shell driver atomically. Never
        # remove another worker's lock, even if its output already exists.
        os.makedirs(os.path.dirname(tmpfile), exist_ok=True)
        try:
            lock = open(tmpfile, 'x')
        except FileExistsError:
            print(f"Ignored, in progress: {afile}")
            return

        try:
            with lock:
                lock.write(os.fspath(afile))

            expected_cells = input_cell_count(input_file)
            if self._valid_result(result_file, expected_cells):
                print(f"Ignored, already completed: {afile}")
                return

            if not os.path.isfile(self.r_script_path):
                raise FileNotFoundError(
                    f"Mouse annotation R script not found: {self.r_script_path}"
                )

            os.makedirs(outfile_dir, exist_ok=True)
            START_TIME = datetime.datetime.now()
            subprocess.run(
                ["Rscript", self.r_script_path, input_file, outfile_dir, specie],
                check=True,
            )
            if not self._valid_result(result_file, expected_cells):
                raise RuntimeError(f"Mouse annotation did not produce a valid result: {result_file}")

            END_TIME = datetime.datetime.now()
            print(f"Time Cost: {(END_TIME - START_TIME).seconds} seconds")
            self.write_logs(outfile_dir, "7", "-1", True)
            print("Data exported")
        finally:
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)

    def __call__(self, data, **kwargs):
        """
        Entry function to execute the transform method.
        """
        return self.transform(data, **kwargs)


class AnnotationOtherSpecie:
    def __init__(self, annotation_path, python_module_path):
        """
        Initialize the class and set required paths.
        """
        # Set Python module paths
        sys.path.append(python_module_path)
        print("scimilarity imported!")

        # Initialize path variables
        self.annotation_path = annotation_path
        CellAnnotation, self.align_dataset, self.lognorm_counts = _load_scimilarity()
        self.cell_annotation = CellAnnotation(model_path=self.annotation_path)

    def write_logs(self, out_path, step_num, cell_num, success):
        """
        Write logs to the specified path.
        """
        write_logs_path = os.path.join(out_path, "logs.txt")
        outtxt = f"step: {step_num}: {cell_num}\n" if cell_num != "-1" else ""
        outtxt += "success\n" if success else ""

        # Append the log to the file
        with open(write_logs_path, 'a') as f:
            f.write(outtxt)

    def transform(self, afile, **kwargs):
        """
        Main function to perform the annotation task.
        """
        specie = kwargs.get('specie')
        output_dir = kwargs.get('output_dir', os.path.join(os.getcwd(), "annotated_data"))

        input_file, count_file = _resolve_input(afile)
        outfile_dir = f"{output_dir}/{specie}/{count_file}"
        tmpfile = f"{output_dir}/{specie}/{count_file}.tmp"
        homologous_gene_dir = os.path.join(kwargs.get('homologous_gene_dir'), f"human2{specie}.csv")

        # Check if the input directory exists
        if not os.path.exists(afile):
            print(f"[Error] Input path does not exist: {afile}")
            return

        print(f"Annotation Start, [Specie]: {specie}\n[Input Dir]: {afile} [Size]: {os.path.getsize(afile)}\n")

        if not os.path.exists(input_file):
            print(f"[Error] Input file does not exist: {input_file}")
            return

        if os.path.exists(outfile_dir):
            print(f"Ignored, already completed: {afile}")
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)
            return

        if os.path.exists(tmpfile):
            print(f"Ignored, in progress: {afile}")
            return

        # Create output directory
        os.makedirs(outfile_dir, exist_ok=True)

        # Create a temporary file to mark the process as running
        with open(tmpfile, 'w') as f:
            f.write(afile)

        # Start the annotation process
        START_TIME = datetime.datetime.now()
        try:
            import scanpy as sc
            import anndata as ad

            # Load data
            df = pd.read_csv(input_file, header=0, index_col=0)
            adata = ad.AnnData(df)
            print("Original rows, columns: ", adata.shape[0], adata.shape[1])

            if adata.shape[0] == 0:
                self.write_logs(outfile_dir, "7", "-1", True)
                return

            # Load homologous gene mapping
            homologous_gene_pd = pd.read_csv(homologous_gene_dir, index_col=0)
            homologous_dict = dict(zip(homologous_gene_pd.iloc[:, 1], homologous_gene_pd.iloc[:, 0]))

            # Perform homologous gene mapping
            adata.var.index = [homologous_dict.get(gene, gene) for gene in adata.var.index]
            unique_index = ~adata.var.index.duplicated()
            adata = adata[:, unique_index]

            # Align with the reference gene set
            adata = self.align_dataset(adata, self.cell_annotation.gene_order, gene_overlap_threshold=100)

            # Store raw counts in a new layer
            adata.layers["counts"] = adata.X.copy()

            # Normalize the data
            adata = self.lognorm_counts(adata)

            # Dimensionality reduction
            adata.obsm['X_scimilarity'] = self.cell_annotation.get_embeddings(adata.X)
            sc.pp.neighbors(adata, use_rep='X_scimilarity')
            sc.tl.umap(adata)

            # Perform annotation
            predictions, nn_idxs, nn_dists, nn_stats = self.cell_annotation.get_predictions_kNN(
                adata.obsm['X_scimilarity']
            )
            type_res = pd.DataFrame(predictions.values)

            END_TIME = datetime.datetime.now()
            print(f"Time Cost: {(END_TIME - START_TIME).seconds} seconds")

            # Export results
            type_res.to_csv(os.path.join(outfile_dir, f"{count_file}_cell_type.csv"), index=True)
            print("Data exported")

        finally:
            # Clean up temporary files and write logs
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)
            self.write_logs(outfile_dir, "7", "-1", True)

    def __call__(self, data, **kwargs):
        """
        Entry function to execute the transform method.
        """
        return self.transform(data, **kwargs)

# if __name__ == "__main__":
#     m_map = Annotation()
#
#     animal = ""
#     # input_dir
#     animal_dir = ""
#     # output_dir
#     output_dir = ""
#
#     for file in glob.glob(animal_dir):
#         m_map.transform(afile=file,
#                         output_dir=output_dir,
#                         specie=animal)
