import os
import sys
import datetime
import pandas as pd
import numpy as np

class AnnotationFilter:
    def __init__(self, annotation_path, python_module_path):
        """
        Initialize the class and set required paths.
        """
        # Initialize path variables
        self.annotation_path = annotation_path

        # Set Python module paths
        sys.path.append(python_module_path)
        print("scimilarity imported!")

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
        Main function to perform the filtering task.
        """
        specie = kwargs.get('specie')
        output_dir = kwargs.get('output_dir', os.path.join(os.getcwd(), "filtered_data"))
        matrix_dir = kwargs.get('matrix_dir', os.path.join(os.getcwd(), "matrix_data"))
        count_file = os.path.basename(afile)

        input_file = f"{afile}/{count_file}_cell_type.csv"
        matrix_file = f"{matrix_dir}/{specie}/{count_file}/{count_file}.csv"
        outfile_dir = f"{output_dir}/{specie}/{count_file}"
        tmpfile = f"{output_dir}/{specie}/{count_file}.tmp"

        # Check if the input directory exists
        if not os.path.exists(afile):
            print(f"[Error] Input path does not exist: {afile}")
            return

        print(f"Filtering Start, [Specie]: {specie}\n[Input Dir]: {afile} [Size]: {os.path.getsize(afile)}\n")

        # Check if the input file exists
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

        # Create output directory
        os.makedirs(outfile_dir, exist_ok=True)

        # Create a temporary file to mark the process as running
        with open(tmpfile, 'w') as f:
            f.write(afile)

        # Start the filtering process
        START_TIME = datetime.datetime.now()
        try:
            # Load input data
            cell_type_df = pd.read_csv(input_file, header=None, skiprows=1)
            matrix_df = pd.read_csv(matrix_file, header=None, skiprows=1)
            print("Original rows, columns: ", matrix_df.shape[0], matrix_df.shape[1])

            # Assign column names
            matrix_df.columns = ['id'] + [f'col_{i}' for i in range(1, matrix_df.shape[1])]
            cell_type_df.columns = ['id', 'cell_type']

            # Count occurrences of each cell type
            cell_type_counts = cell_type_df['cell_type'].value_counts()

            # Identify cell types with less than 20 occurrences
            cell_types_to_remove = cell_type_counts[cell_type_counts < 20].index
            print(f"Removing cell types: {list(cell_types_to_remove)}")

            # Get IDs of the cells to remove
            ids_to_remove = cell_type_df[cell_type_df['cell_type'].isin(cell_types_to_remove)]['id']

            # Remove corresponding rows in both datasets
            matrix_df = matrix_df[~matrix_df['id'].isin(ids_to_remove)]
            cell_type_df = cell_type_df[~cell_type_df['id'].isin(ids_to_remove)]

            END_TIME = datetime.datetime.now()
            print(f"Filtering Time Cost: {(END_TIME - START_TIME).seconds} seconds")

            # Export filtered data
            print("Starting export of filtered data...")
            cell_type_df.to_csv(os.path.join(outfile_dir, f"{count_file}_cell_type.csv"), index=False, header=False)
            matrix_df.to_csv(os.path.join(outfile_dir, f"{count_file}.csv"), index=False, header=False)
            print("Filtered data successfully exported.")

        finally:
            # Remove the temporary file and write logs
            if os.path.exists(tmpfile):
                os.unlink(tmpfile)
            self.write_logs(outfile_dir, "7", "-1", True)

    def __call__(self, data, **kwargs):
        """
        Entry function to execute the transform method.
        """
        return self.transform(data, **kwargs)
