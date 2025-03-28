import os
from collections import Counter

import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from tqdm import tqdm
from typing import Optional

from scimilarity.src.scimilarity.zarr_dataset import ZarrDataset


class scDatasetFromList(Dataset):
    """A class to encapsulation single cell datasets from list"""
    def __init__(self, data_list, obs_celltype="celltype_name", obs_study="study"):
        """Constructor.

        Parameters
        ----------
        data_list: list
            List of single-cell datasets.
        obs_celltype: str, default: "celltype_name"
            Cell type name.
        obs_study: str, default: "study"
            Study name.
        """
        self.data_list = data_list
        self.ncells_list = [data.shape[0] for data in data_list]
        self.ncells = sum(self.ncells_list)
        self.obs_celltype = obs_celltype
        self.obs_study = obs_study

        self.data_idx = [
            n for n in range(len(self.ncells_list)) for i in range(self.ncells_list[n])
        ]
        self.cell_idx = [
            i for n in range(len(self.ncells_list)) for i in range(self.ncells_list[n])
        ]

    def __len__(self):
        return self.ncells

    def __getitem__(self, idx):
        # data, label, study
        data_idx = self.data_idx[idx]
        cell_idx = self.cell_idx[idx]
        return (
            self.data_list[data_idx].get_cell(cell_idx).A,
            self.data_list[data_idx].get_obs(self.obs_celltype)[cell_idx],
            self.data_list[data_idx].get_obs(self.obs_study)[cell_idx],
        )


class MetricLearningZarrDataModule(pl.LightningDataModule):
    """A class to encapsulate zarr data model."""
    def __init__(
        self,
        train_path: str,
        gene_order: str,
        val_path: Optional[str] = None,
        test_path: Optional[str] = None,
        obs_field: str = "celltype_name",
        batch_size: int = 1000,
        num_workers: int = 1,
    ):
        """Constructor.

        Parameters
        ----------
        train_path: str
            Path to folder containing all training datasets.
        gene_order: str
            Use a given gene order as described in the specified file. One gene
            symbol per line.
            IMPORTANT: the zarr datasets should already be in this gene order
            after preprocessing.
        val_path: str, optional
            Path to folder containing all validation datasets.
        test_path: str, optional
            Path to folder containing all test datasets.
        obs_field: str, default: "celltype_name"
            The obs key name containing celltype labels.
        batch_size: int, default: 1000
            Batch size.
        num_workers: int, default: 1
            The number of worker threads for dataloaders

        Examples
        --------
        >>> datamodule = MetricLearningZarrDataModule(
                batch_size=1000,
                num_workers=1,
                obs_field="celltype_name",
                train_path="train",
                gene_order="gene_order.tsv",
            )
        """

        super().__init__()
        self.train_path = train_path
        self.val_path = val_path
        self.test_path = test_path
        self.batch_size = batch_size
        self.num_workers = num_workers

        # gene space needs be aligned to the given gene order
        with open(gene_order, "r") as fh:
            self.gene_order = [line.strip() for line in fh]

        self.n_genes = len(self.gene_order)  # used when creating training model

        train_data_list = []
        self.train_Y = []  # text labels
        self.train_study = []  # text studies
        self.train_file_list = [
            f for f in os.listdir(self.train_path) if f.endswith(".aligned.zarr")
        ]
        for filename in tqdm(self.train_file_list):
            data_path = os.path.join(self.train_path, filename)
            if os.path.isdir(data_path):
                zarr_data = ZarrDataset(data_path)
                train_data_list.append(zarr_data)
                self.train_Y.extend(zarr_data.get_obs(obs_field).astype(str).tolist())
                self.train_study.extend(zarr_data.get_obs("study").astype(str).tolist())

        # Lazy load training data from list of zarr datasets
        self.train_dataset = scDatasetFromList(train_data_list)

        self.class_names = set(self.train_Y)
        self.label2int = {label: i for i, label in enumerate(self.class_names)}
        self.int2label = {value: key for key, value in self.label2int.items()}

        self.val_dataset = None
        if self.val_path is not None:
            val_data_list = []
            self.val_Y = []
            self.val_study = []
            self.val_file_list = [
                f for f in os.listdir(self.val_path) if f.endswith(".aligned.zarr")
            ]
            for filename in tqdm(self.val_file_list):
                data_path = os.path.join(self.val_path, filename)
                if os.path.isdir(data_path):
                    zarr_data = ZarrDataset(data_path)
                    val_data_list.append(zarr_data)
                    self.val_Y.extend(zarr_data.get_obs(obs_field).astype(str).tolist())
                    self.val_study.extend(
                        zarr_data.get_obs("study").astype(str).tolist()
                    )

            # Lazy load val data from list of zarr datasets
            self.val_dataset = scDatasetFromList(val_data_list)

        self.test_dataset = None
        if self.test_path is not None:
            test_data_list = []
            self.test_Y = []
            self.test_study = []
            self.test_file_list = [
                f for f in os.listdir(self.test_path) if f.endswith(".aligned.zarr")
            ]
            for filename in tqdm(self.test_file_list):
                data_path = os.path.join(self.test_path, filename)
                if os.path.isdir(data_path):
                    zarr_data = ZarrDataset(data_path)
                    test_data_list.append(zarr_data)
                    self.test_Y.extend(
                        zarr_data.get_obs(obs_field).astype(str).tolist()
                    )
                    self.test_study.extend(
                        zarr_data.get_obs("study").astype(str).tolist()
                    )

            # Lazy load test data from list of zarr datasets
            self.test_dataset = scDatasetFromList(test_data_list)

    def two_way_weighting(self, vec1: list, vec2: list):
        counts = pd.crosstab(vec1, vec2)
        weights_matrix = (1 / counts).replace(np.inf, 0)
        return weights_matrix.unstack().to_dict()

    def get_sampler_weights(self, labels: list, studies: Optional[list] = None):
        if studies is None:
            class_sample_count = Counter(labels)
            sample_weights = torch.Tensor([1.0 / class_sample_count[t] for t in labels])
        else:
            class_sample_count = Counter(labels)
            study_sample_count = Counter(studies)
            sample_weights = torch.Tensor(
                [
                    1.0
                    / class_sample_count[labels[i]]
                    / np.log(study_sample_count[studies[i]])
                    for i in range(len(labels))
                ]
            )
        return WeightedRandomSampler(sample_weights, len(sample_weights))

    def collate(self, batch):
        profiles, labels, studies = tuple(
            map(list, zip(*batch))
        )  # tuple([list(t) for t in zip(*batch)])
        return (
            torch.squeeze(torch.Tensor(np.vstack(profiles))),
            torch.Tensor([self.label2int[l] for l in labels]),  # text to int labels
            studies,
        )

    def train_dataloader(self):
        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
            drop_last=True,
            sampler=self.get_sampler_weights(self.train_Y, self.train_study),
            collate_fn=self.collate,
        )

    def val_dataloader(self):
        if self.val_dataset is None:
            return None
        return DataLoader(
            self.val_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
            drop_last=True,
            sampler=self.get_sampler_weights(self.val_Y, self.val_study),
            collate_fn=self.collate,
        )

    def test_dataloader(self):
        if self.test_dataset is None:
            return None
        return DataLoader(
            self.test_dataset,
            batch_size=self.batch_size,
            num_workers=self.num_workers,
            pin_memory=True,
            drop_last=True,
            sampler=self.get_sampler_weights(self.test_Y, self.test_study),
            collate_fn=self.collate,
        )
