import os
import pickle
import subprocess
import numpy as np
import pandas as pd
import scanpy as sc
from anndata import AnnData
from datasets import Dataset, Features, Sequence, Value


class GeneDataNormalization:
    def __init__(self, specie, ref_path):
        """
        Initialize the class with relevant parameters.
        """
        self.specie = specie
        self.ref_path = ref_path

        self.latin_maps = {
            "human": "Homo_sapiens", "mouse": "Mus_musculus", "monkey": "Macaca_mulatta",
            "zhu": "Sus_scrofa", "dashu": "Rattus_norvegicus", "mianyang": "Ovis_aries",
            "ji": "Gallus_gallus", "ma": "Equus_caballus", "guoying": "Drosophila_melanogaster",
            "banmayu": "Danio_rerio", "xianchong": "Caenorhabditis_elegans", "niu": "Bos_taurus"
        }

        self.specice_int_map = {
            "human": "00", "mouse": "01", "monkey": "02", "zhu": "03",
            "dashu": "04", "mianyang": "05", "ji": "06", "ma": "07",
            "guoying": "08", "banmayu": "09", "yang": "10", "xianchong": "11", "niu": "12"
        }

    @staticmethod
    def install_and_import(package_name):
        """Install a Python package if not already installed."""
        import importlib
        try:
            importlib.import_module(package_name)
            print(f"The package '{package_name}' is already installed.")
        except ImportError:
            print(f"Installing package: {package_name}")
            subprocess.check_call(['pip', 'install', package_name])

    @staticmethod
    def write_logs(out_path, step, message, success):
        """Write logs to a file."""
        log_file = os.path.join(out_path, "logs.txt")
        with open(log_file, 'a') as f:
            f.write(f"step: {step}: {message}\n")
            if success:
                f.write("success\n")

    @staticmethod
    def load_csv_data(path):
        """Load data from a CSV file and return an AnnData object."""
        df = pd.read_csv(path)
        return AnnData(df)

    def load_core_gene_id_list(self):
        """Load core gene IDs from a file."""
        path = os.path.join(self.ref_path, "filter_gene_list",
                            f"{self.latin_maps[self.specie]}_ensemble_filter_genelist.txt")
        with open(path, 'r') as f:
            return [line.split()[1] for line in f]

    @staticmethod
    def filter_genes(adata, gene_list):
        """Filter the data to keep only the genes in the provided list."""
        mask = adata.var.index.isin(gene_list)
        return adata[:, mask]

    def data_normalize(self, adata):
        """Normalize data based on gene mid-values."""
        mid_dict_path = os.path.join(self.ref_path, "mid_values", f"{self.specie}.pickle")
        with open(mid_dict_path, 'rb') as f:
            gene_mids = pickle.load(f)

        matrix_a = adata.X
        gene_list = adata.var.index.to_list()

        gene_nonzero_mids = np.array([gene_mids.get(gene, 1) for gene in gene_list])
        adata.X = np.nan_to_num(matrix_a.T / gene_nonzero_mids[:, None]).T
        return adata

    @staticmethod
    def tokenize_cell(gene_vector, gene_list, token_dict):
        """Convert a gene expression vector into tokenized rank value encoding."""
        nonzero_indices = np.nonzero(gene_vector)[0]
        sorted_indices = np.argsort(-gene_vector[nonzero_indices])
        genes = np.array(gene_list)[nonzero_indices][sorted_indices]

        tokens = [token_dict[gene] for gene in genes]
        values = gene_vector[nonzero_indices][sorted_indices]
        return genes.tolist(), tokens, values.tolist()

    def load_tokens(self):
        """Load token dictionary and filter for the current species."""
        token_path = os.path.join(self.ref_path, "tokens", "token_all_species_core.pickle")
        with open(token_path, 'rb') as f:
            all_tokens = pickle.load(f)

        specie_tokens = {k[2:]: v for k, v in all_tokens.items() if k[:2] == self.specice_int_map[self.specie]}
        return specie_tokens

    def transform_data(self, adata, tokens, cut_max_len):
        """Calculate rank values and tokenize the gene expression data."""
        input_genes, input_ids, lengths, values = [], [], [], []
        gene_list = adata.var.index.to_list()

        for cell_vector in adata.X:
            genes, tokenized, vals = self.tokenize_cell(cell_vector, gene_list, tokens)
            if len(tokenized) > cut_max_len:
                tokenized, vals = tokenized[:cut_max_len], vals[:cut_max_len]

            input_genes.append(genes)
            input_ids.append(tokenized)
            values.append(vals)
            lengths.append(min(len(tokenized), cut_max_len))

        return input_genes, input_ids, lengths, values

    def export_to_huggingface(self, adata, length, input_genes, input_ids, values, filename):
        """Convert the data into HuggingFace dataset format."""
        species_code = self.specice_int_map[self.specie]
        dataset = Dataset.from_dict({
            'input_ids': input_ids,
            'genes': input_genes,
            'values': values,
            'length': [[l] for l in length],
            'species': [[species_code]] * len(length),
            'gsm': [[filename]] * len(length)
        }, features=Features({
            'input_ids': Sequence(Value('string')),
            'genes': Sequence(Value('string')),
            'values': Sequence(Value('float32')),
            'length': Sequence(Value('int16')),
            'species': Sequence(Value('string')),
            'gsm': Sequence(Value('string'))
        }))
        return dataset

    @staticmethod
    def save_dataset(output_path, dataset, lengths):
        """Save the HuggingFace dataset to disk."""
        dataset.save_to_disk(output_path)
        with open(os.path.join(output_path, 'sorted_length.pickle'), 'wb') as f:
            pickle.dump(sorted(lengths), f)

    def process(self, afile, cut_max_len=-1, **kwargs):
        """Main function to process the data."""
        output_dir = kwargs.get('output_dir', os.path.join(os.getcwd(), "normalization_data"))
        gsm = os.path.basename(afile).replace('.csv', '')
        h5ad_data_path = os.path.join(output_dir, self.specie, f"{gsm}.h5ad")
        outfile_dir = os.path.join(output_dir, "hf", self.specie, gsm)
        tmpfile = f"{outfile_dir}.tmp"

        if os.path.exists(outfile_dir):
            print(f"Already processed: {afile}")
            return

        os.makedirs(outfile_dir, exist_ok=True)
        os.makedirs(os.path.join(output_dir, self.specie), exist_ok=True)
        with open(tmpfile, 'w') as f:
            f.write(afile)

        adata = self.load_csv_data(afile)
        core_genes = self.load_core_gene_id_list()
        adata = self.filter_genes(adata, core_genes)
        adata = self.data_normalize(adata)
        sc.pp.log1p(adata, base=2)
        adata.write(h5ad_data_path)

        tokens = self.load_tokens()
        if cut_max_len is None or cut_max_len < 0:
            cut_max_len = adata.shape[1]
        else:
            cut_max_len = min(cut_max_len, adata.shape[1])
        input_genes, input_ids, lengths, values = self.transform_data(adata, tokens, cut_max_len)

        dataset = self.export_to_huggingface(adata, lengths, input_genes, input_ids, values, gsm)
        self.save_dataset(outfile_dir, dataset, lengths)

        os.unlink(tmpfile)

        self.write_logs(outfile_dir, "13", "-1", True)

    def __call__(self, data, **kwargs):
        """Entry point to trigger processing."""
        self.process(data, **kwargs)
