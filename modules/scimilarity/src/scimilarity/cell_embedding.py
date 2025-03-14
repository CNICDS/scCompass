import json
import os
from typing import Optional, Tuple, Union

import hnswlib
import numpy as np
import pandas as pd
import torch
import zarr
from scipy.sparse import csr_matrix

from scimilarity.src.scimilarity.b_colors import BColors
from scimilarity.src.scimilarity.nn_models import Encoder
from scimilarity.src.scimilarity.utils import align_dataset


class CellEmbedding:
    """A class that embeds cell gene expression data using a ML model."""

    def __init__(
        self,
        model_path: str,
        use_gpu: bool = False,
        parameters: Optional[dict] = None,
        filenames: Optional[dict] = None,
        residual: bool = False,
    ):
        """Constructor.

        Parameters
        ----------
        model_path: str
            Path to the directory containing model files.
        use_gpu: bool, default: False
            Use GPU instead of CPU.
        parameters: dict, optional
            Use a dictionary of custom model parameters instead of infering from model files.
        filenames: dict, optional
            Use a dictionary of custom filenames for model files instead default.
        residual: bool, default: False
            Use residual connections.

        Examples
        --------
        >>> ce = CellEmbedding(model_path="/opt/data/model")
        """

        self.model_path = model_path
        self.use_gpu = use_gpu
        self.knn = None

        if filenames is None:
            filenames = {}

        self.filenames = {
            "model": os.path.join(
                self.model_path, filenames.get("model", "encoder.ckpt")
            ),
            "gene_order": os.path.join(
                self.model_path, filenames.get("gene_order", "gene_order.tsv")
            ),
        }

        # get gene order
        with open(self.filenames["gene_order"], "r") as fh:
            self.gene_order = [line.strip() for line in fh]

        # get neural network model
        if parameters is None:  # infer network size if not explicitly given
            with open(os.path.join(self.model_path, "layer_sizes.json"), "r") as fh:
                layer_sizes = json.load(fh)
            # keys: network.1.weight, network.2.weight, ..., network.n.weight
            layers = [
                (key, layer_sizes[key])
                for key in sorted(list(layer_sizes.keys()))
                if "weight" in key and len(layer_sizes[key]) > 1
            ]
            parameters = {
                "latent_dim": layers[-1][1][0],  # last
                "hidden_dim": [layer[1][0] for layer in layers][0:-1],  # all but last
            }

        self.n_genes = len(self.gene_order)
        self.latent_dim = parameters["latent_dim"]
        self.model = Encoder(
            n_genes=self.n_genes,
            latent_dim=parameters["latent_dim"],
            hidden_dim=parameters["hidden_dim"],
            residual=residual,
        )
        if self.use_gpu is True:
            self.model.cuda()
        self.model.load_state(self.filenames["model"])
        self.model.eval()

        self.int2label = pd.read_csv(
            os.path.join(self.model_path, "label_ints.csv"), index_col=0
        )["0"].to_dict()
        self.label2int = {value: key for key, value in self.int2label.items()}

    def load_knn_index(self, knn_file: str):
        """Load the kNN index file

        Parameters
        ----------
        knn_file: str
            Filename of the kNN index.
        """
        if os.path.isfile(knn_file):
            self.knn = hnswlib.Index(space="cosine", dim=self.model.latent_dim)
            self.knn.load_index(knn_file)
        else:
            print(
                f"{BColors.WARNING}Warning: No KNN index found at {knn_file}{BColors.ENDC}"
            )
            self.knn = None

    def get_embeddings(
        self,
        X: Union[csr_matrix, np.ndarray],
        num_cells: int = -1,
        buffer_size: int = 10000,
    ) -> np.ndarray:
        """Calculate embeddings for lognormed gene expression matrix.

        Parameters
        ----------
        X: scipy.sparse.csr_matrix, numpy.ndarray
            Gene expression matrix.
        num_cells: int, default: -1
            The number of cells to embed, starting from index 0.
            A value of -1 will embed all cells.
        buffer_size: int, default: 10000
            The number of cells to embed in one batch.

        Returns
        -------
        numpy.ndarray
            A 2D numpy array of embeddings [num_cells x latent_space_dimensions].

        Examples
        --------
        >>> from scimilarity.utils import align_dataset
        >>> ce = CellEmbedding(model_path="/opt/data/model")
        >>> embedding = ce.get_embeddings(align_dataset(data, ce.gene_order).X)
        """

        if num_cells == -1:
            num_cells = X.shape[0]

        if (
            isinstance(X, csr_matrix)
            and (
                isinstance(X.data, zarr.core.Array)
                or isinstance(X.indices, zarr.core.Array)
                or isinstance(X.indptr, zarr.core.Array)
            )
            and num_cells <= buffer_size
        ):
            X.data = X.data[...]
            X.indices = X.indices[...]
            X.indptr = X.indptr[...]

        embedding_parts = []
        with torch.inference_mode():  # disable gradients, not needed for inference
            for i in range(0, num_cells, buffer_size):
                profiles = None
                if isinstance(X, np.ndarray):
                    profiles = torch.Tensor(X[i : i + buffer_size])
                elif isinstance(X, torch.Tensor):
                    profiles = X[i : i + buffer_size]
                elif isinstance(X, csr_matrix):
                    profiles = torch.Tensor(X[i : i + buffer_size].toarray())

                if profiles is None:
                    raise RuntimeError(f"Unknown data type {type(X)}.")

                if self.use_gpu is True:
                    profiles = profiles.cuda()
                embedding_parts.append(self.model(profiles))

        if not embedding_parts:
            raise RuntimeError(f"No valid cells detected.")

        if self.use_gpu:
            # detach, move from gpu into cpu, return as numpy array
            embedding = torch.vstack(embedding_parts).detach().cpu().numpy()
        else:
            # detach, return as numpy array
            embedding = torch.vstack(embedding_parts).detach().numpy()

        if np.isnan(embedding).any():
            raise RuntimeError(f"NaN detected in embeddings.")

        return embedding

    def get_nearest_neighbors(
        self, embeddings: np.ndarray, k: int = 50, ef: int = 100
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Get nearest neighbors.
        Used by classes that inherit from CellEmbedding and have an instantiated kNN.

        Parameters
        ----------
        embeddings: numpy.ndarray
            Embeddings as a numpy array.
        k: int, default: 50
            The number of nearest neighbors.
        ef: int, default: 100
            The size of the dynamic list for the nearest neighbors.
            See https://github.com/nmslib/hnswlib/blob/master/ALGO_PARAMS.md

        Returns
        -------
        nn_idxs: numpy.ndarray
            A 2D numpy array of nearest neighbor indices [num_cells x k].
        nn_dists: numpy.ndarray
            A 2D numpy array of nearest neighbor distances [num_cells x k].

        Examples
        --------
        >>> from scimilarity.utils import align_dataset
        >>> ca = CellAnnotation(model_path="/opt/data/model")
        >>> embedding = ca.get_embeddings(align_dataset(data, ca.gene_order).X)
        >>> nn_idxs, nn_dists = get_nearest_neighbors(embeddings)
        """

        if self.knn is None:
            raise RuntimeError(
                "kNN is not initialized. If no kNN index file is found, run the method build_knn."
            )
        self.knn.set_ef(ef)
        return self.knn.knn_query(embeddings, k=k)
