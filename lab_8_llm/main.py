"""
Laboratory work.
Working with Large Language Models.
"""
# pylint: disable=too-few-public-methods, undefined-variable, too-many-arguments, super-init-not-called, duplicate-code

from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import torch
from datasets import load_dataset
from evaluate import load
from pandas import DataFrame
from torch.utils.data import DataLoader
from torch.utils.data.dataset import Dataset
from torchinfo import summary
from transformers import BertForSequenceClassification, BertTokenizer

from core_utils.llm.llm_pipeline import AbstractLLMPipeline
from core_utils.llm.metrics import Metrics
from core_utils.llm.raw_data_importer import AbstractRawDataImporter
from core_utils.llm.raw_data_preprocessor import AbstractRawDataPreprocessor, ColumnNames
from core_utils.llm.task_evaluator import AbstractTaskEvaluator
from core_utils.llm.time_decorator import report_time


class RawDataImporter(AbstractRawDataImporter):
    """
    A class that imports the HuggingFace dataset.
    """

    @report_time
    def obtain(self) -> None:
        """
        Download a dataset.

        Raises:
            TypeError: In case of downloaded dataset is not pd.DataFrame
        """
        self._raw_data = load_dataset(self._hf_name,
                                      split='train').to_pandas()


class RawDataPreprocessor(AbstractRawDataPreprocessor):
    """
    A class that analyzes and preprocesses a dataset.
    """

    def analyze(self) -> dict:
        """
        Analyze a dataset.

        Returns:
            dict: Dataset key properties
        """

        dataset_properties = {'dataset_columns': self._raw_data.shape[1],
                              'dataset_duplicates': self._raw_data.duplicated().sum(),
                              'dataset_empty_rows': self._raw_data.isna().sum().sum(),
                              'dataset_number_of_samples': self._raw_data.shape[0],
                              'dataset_sample_max_len': self._raw_data['neutral'].str.len().max(),
                              'dataset_sample_min_len': self._raw_data['neutral'].str.len().min()}
        return dataset_properties

    @report_time
    def transform(self) -> None:
        """
        Apply preprocessing transformations to the raw dataset.
        """

        self._data = self._raw_data.drop_duplicates().rename(
            columns={'neutral': ColumnNames.SOURCE.value,
                     'toxic': ColumnNames.TARGET.value}).reset_index(drop=True)
        self._data[ColumnNames.TARGET.value] = self._data[ColumnNames.TARGET.value].replace(
            {'true': 1, 'false': 0})


class TaskDataset(Dataset):
    """
    A class that converts pd.DataFrame to Dataset and works with it.
    """

    def __init__(self, data: DataFrame) -> None:
        """
        Initialize an instance of TaskDataset.

        Args:
            data (pandas.DataFrame): Original data
        """
        self._data = data

    def __len__(self) -> int:
        """
        Return the number of items in the dataset.

        Returns:
            int: The number of items in the dataset
        """

        return len(self._data)

    def __getitem__(self, index: int) -> tuple[str, ...]:
        """
        Retrieve an item from the dataset by index.

        Args:
            index (int): Index of sample in dataset

        Returns:
            tuple[str, ...]: The item to be received
        """
        item_by_index = (self._data['source'].iloc[index],)
        return item_by_index

    @property
    def data(self) -> DataFrame:
        """
        Property with access to preprocessed DataFrame.

        Returns:
            pandas.DataFrame: Preprocessed DataFrame
        """
        return self._data


class LLMPipeline(AbstractLLMPipeline):
    """
    A class that initializes a model, analyzes its properties and infers it.
    """

    def __init__(
            self,
            model_name: str,
            dataset: TaskDataset,
            max_length: int,
            batch_size: int,
            device: str
    ) -> None:
        """
        Initialize an instance of LLMPipeline.

        Args:
            model_name (str): The name of the pre-trained model
            dataset (TaskDataset): The dataset used
            max_length (int): The maximum length of generated sequence
            batch_size (int): The size of the batch inside DataLoader
            device (str): The device for inference
        """
        super().__init__(model_name, dataset, max_length, batch_size, device)
        self._model: torch.nn.Module = BertForSequenceClassification.from_pretrained(
            self._model_name)
        self._tokenizer = BertTokenizer.from_pretrained(self._model_name)

    def analyze_model(self) -> dict:
        """
        Analyze model computing properties.

        Returns:
            dict: Properties of a model
        """
        torch_data = torch.ones(1, self._model.config.max_position_embeddings,
                                dtype=torch.long)
        input_data = {'input_ids': torch_data, 'attention_mask': torch_data}
        summary_result = summary(self._model,
                                 input_data=input_data,
                                 device='cpu',
                                 verbose=0)

        model_properties = {'embedding_size': self._model.config.max_position_embeddings,
                            'input_shape': {'attention_mask': list(
                                summary_result.input_size['attention_mask']),
                                'input_ids': list(summary_result.input_size['input_ids'])},
                            'max_context_length': self._model.config.max_length,
                            'num_trainable_params': summary_result.trainable_params,
                            'output_shape': summary_result.summary_list[-1].output_size,
                            'size': summary_result.total_param_bytes,
                            'vocab_size': self._model.config.vocab_size}

        return model_properties

    @report_time
    def infer_sample(self, sample: tuple[str, ...]) -> str | None:
        """
        Infer model on a single sample.

        Args:
            sample (tuple[str, ...]): The given sample for inference with model

        Returns:
            str | None: A prediction
        """
        if not self._model:
            return None
        return self._infer_batch([sample])[0]

    @report_time
    def infer_dataset(self) -> DataFrame:
        """
        Infer model on a whole dataset.

        Returns:
            pd.DataFrame: Data with predictions
        """
        data_load = DataLoader(self._dataset, batch_size=self._batch_size)
        predictions = []
        for batch in data_load:
            predictions.extend(self._infer_batch(batch))

        data_with_predictions = pd.DataFrame(
            {'target': self._dataset.data[ColumnNames.TARGET.value],
             'prediction': pd.Series(predictions)})
        return data_with_predictions

    @torch.no_grad()
    def _infer_batch(self, sample_batch: Sequence[tuple[str, ...]]) -> list[str]:
        """
        Infer model on a single batch.

        Args:
            sample_batch (Sequence[tuple[str, ...]]): Batch to infer the model

        Returns:
            list[str]: Model predictions as strings
        """

        tokens = self._tokenizer(sample_batch[0], max_length=self._max_length,
                                 padding=True,
                                 truncation=True,
                                 return_tensors='pt')
        outputs = self._model(**tokens)
        predictions = [str(output.item()) for output in list(torch.argmax(outputs.logits, dim=1))]
        return predictions


class TaskEvaluator(AbstractTaskEvaluator):
    """
    A class that compares prediction quality using the specified metric.
    """

    def __init__(self, data_path: Path, metrics: Iterable[Metrics]) -> None:
        """
        Initialize an instance of Evaluator.

        Args:
            data_path (pathlib.Path): Path to predictions
            metrics (Iterable[Metrics]): List of metrics to check
        """

        super().__init__(metrics)
        self._data_path = data_path

    @report_time
    def run(self) -> dict | None:
        """
        Evaluate the predictions against the references using the specified metric.

        Returns:
            dict | None: A dictionary containing information about the calculated metric
        """
        predictions = pd.read_csv(self._data_path)
        metric_scores = {}

        for metric in self._metrics:
            metric = Metrics[str(metric).upper()]
            metric = load(metric.value)
            scores = metric.compute(
                references=predictions['target'].tolist(),
                predictions=predictions['prediction'].tolist(), average='micro')
            metric_scores[metric.name] = scores.get(metric.name)

        return metric_scores
