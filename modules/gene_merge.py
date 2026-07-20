import os
import re
import csv
import numpy as np
import pandas as pd


class SpeciesDataProcessor:
    def __init__(self, specie_name, annotation_dir, mapping_dir, merge_dir, metadata_path):
        """
        Initialize the class with paths and the species name.

        ``annotation_dir`` is the annotation output root; it holds the
        ``<sample>_cell_type.csv`` files this step consumes.
        """
        self.specie_name = specie_name
        self.annotation_dir = os.path.join(annotation_dir, specie_name)
        self.mapping_dir = os.path.join(mapping_dir, specie_name)
        self.merge_dir = os.path.join(merge_dir, specie_name)
        self.metadata_path = metadata_path

    @staticmethod
    def write_logs(out_path, log_text):
        """
        Write logs to a specified path.
        """
        log_file = os.path.join(out_path, "logs.txt")
        with open(log_file, 'a') as f:
            f.write(log_text)

    @staticmethod
    def append_line(text_str, target_file):
        """
        Append a line of text to a file.
        """
        with open(target_file, "a") as file:
            file.write(text_str + "\n")

    def get_sample_organ_array(self):
        """
        Load the sample-organ data for the current species.
        """
        specie_file = os.path.join(self.metadata_path, f"{self.specie_name}.xlsx")
        specie_data = pd.read_excel(specie_file, index_col=0).fillna(0)
        return specie_data[specie_data['Organ'] != 0]

    def process(self):
        """
        Main method to process data for the current species.
        """
        sample_organ_array = self.get_sample_organ_array()
        sample_num = 0

        for index, row in sample_organ_array.iterrows():
            sample_name = index
            organ_name = re.sub(r'\s', '', row["Organ"])

            sample_cell_type_dir = f"{self.annotation_dir}/{sample_name}"
            sample_gene_mapping_dir = f"{self.mapping_dir}/{sample_name}"

            cell_type_file = f"{sample_cell_type_dir}/{sample_name}_cell_type.csv"
            cell_mapping_file = f"{sample_gene_mapping_dir}/{sample_name}.csv"

            if not (os.path.exists(cell_type_file) and os.path.exists(cell_mapping_file)):
                continue

            sample_num += 1
            out_str = f"sample_num: {sample_num} start, sample_cell_type_dir: {sample_cell_type_dir}\n"
            os.makedirs(self.merge_dir, exist_ok=True)

            print(out_str)
            self.write_logs(self.merge_dir, out_str)

            try:
                cell_types = pd.read_csv(cell_type_file, header=None)
                cell_mapping_data = np.loadtxt(cell_mapping_file, delimiter=',', dtype=int)
            except pd.errors.EmptyDataError:
                self.append_line(cell_type_file, "empty_data.txt")
                continue

            self.process_cell_types(organ_name, cell_types, cell_mapping_data)

        print(f"Total samples processed for {self.specie_name}: {sample_num}")

    def process_cell_types(self, organ_name, cell_types_data, cell_mapping_data):
        """
        Process cell types and save the data to corresponding files.
        """
        grouped = cell_types_data.groupby(1)
        for key, group in grouped:
            print(f"Processing category: {key}")

            current_cell_type = re.sub(r'\s', '', key)
            current_cell_type = re.sub(r'[^\w-]', '-', current_cell_type)

            target_merge_file = f"{self.merge_dir}/{organ_name}_{current_cell_type}.csv"
            current_cell_type_indexes = np.array(group[0])
            current_cell_type_data = cell_mapping_data[current_cell_type_indexes, :]

            with open(target_merge_file, 'a', newline='') as file:
                writer = csv.writer(file, delimiter=',')
                writer.writerows(current_cell_type_data)

    def __call__(self):
        """
        Entry point to execute the processing.
        """
        self.process()

# Example usage for a single species
# if __name__ == "__main__":
#     filter_dir = ""  # Replace with the actual path
#     mapping_dir = ""  # Replace with the actual path
#     merge_dir = ""  # Replace with the actual path
#     metadata_path = "metadata_path"  # Replace with the actual path
#
#     species_name = "human"  # Example: Process data for humans
#
#     processor = SpeciesDataProcessor(
#         specie_name=species_name,
#         filter_dir=filter_dir,
#         mapping_dir=mapping_dir,
#         merge_dir=merge_dir,
#         metadata_path=metadata_path
#     )
#
#     processor()  # Execute processing for the given species
