"""Human labels depend on embeddings and kNN, with positional output indices."""
import sys
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from modules.annotation import AnnotationHuman


class HumanAnnotationTests(unittest.TestCase):
    def test_labels_are_predicted_without_neighbor_graph_computation(self):
        annotation = AnnotationHuman.__new__(AnnotationHuman)
        annotation.align_dataset = lambda data, genes: data
        annotation.lognorm_counts = lambda data: data
        annotation.cell_annotation = MagicMock()
        annotation.cell_annotation.gene_order = ["GeneA", "GeneB"]
        embeddings = np.array([[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]])
        annotation.cell_annotation.get_embeddings.return_value = embeddings
        annotation.cell_annotation.get_predictions_kNN.return_value = (
            pd.Series(["A", "B", "A"]), None, None, None)
        graph = MagicMock(side_effect=AssertionError("Neighbor graph is not a label input"))
        fake_scanpy = types.SimpleNamespace(pp=types.SimpleNamespace(neighbors=graph),
                                            tl=types.SimpleNamespace(umap=graph))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.csv"
            pd.DataFrame([[2, 1], [1, 2], [3, 1]], columns=["GeneA", "GeneB"],
                         index=["cell1", "cell2", "cell3"]).to_csv(source)
            with patch.dict(sys.modules, {"scanpy": fake_scanpy}):
                annotation(str(source), specie="human", output_dir=str(root / "out"))
            labels = pd.read_csv(root / "out/human/sample/sample_cell_type.csv", index_col=0)
            self.assertEqual(labels.index.tolist(), [0, 1, 2])
            self.assertEqual(labels.iloc[:, 0].tolist(), ["A", "B", "A"])
            self.assertFalse((root / "out/human/sample.tmp").exists())
        graph.assert_not_called()
        np.testing.assert_array_equal(annotation.cell_annotation.get_predictions_kNN.call_args.args[0], embeddings)


if __name__ == "__main__":
    unittest.main()
