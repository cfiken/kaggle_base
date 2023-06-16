import os
import sys

IS_DEBUG = True
IS_KAGGLE = False

if IS_KAGGLE:
    package_paths = [
        '/kaggle/input/git-mykaggle/mykaggle/'
    ]

    for pth in package_paths:
        sys.path.append(pth)

from typing import Any, Optional, Dict, Tuple
from pathlib import Path
import gc
import yaml
import pickle
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
import torch
from torch import nn
from torch.nn import functional as F
from torch.utils.data import Dataset, DataLoader
from torch.cuda.amp import autocast, GradScaler
from torch.optim.lr_scheduler import LRScheduler
from transformers import AutoConfig, AutoTokenizer, AutoModel, PreTrainedTokenizer, PreTrainedModel
from transformers import get_linear_schedule_with_warmup, get_cosine_schedule_with_warmup
from fastprogress.fastprogress import master_bar, progress_bar

from mykaggle.lib.ops import sigmoid
from mykaggle.lib.routine import fix_seed, get_logger, parse
from mykaggle.model.common import AttentionHead
from mykaggle.trainer.base import Mode
from mykaggle.lib.logger.logger import Logger
from mykaggle.lib.logger.factory import LoggerFactory, LoggerType

#
# Settings
#

S = yaml.safe_load('''
name: 'nlp_bert_binary_classification'
competition: sample
do_training: true
do_inference: true
do_submit: true
seed: 1019
device: cuda
training:
    use_amp: true
    num_gpus: 1
    train_file: train.csv
    test_file: test.csv
    input_column: question_text
    target_column: target
    num_folds: 5
    do_cv: true
    cv: stratified
    train_only_fold:
    learning_rate: 0.00003
    num_epochs: 3
    batch_size: 16
    test_batch_size: 16
    num_accumulations: 2
    num_workers: 4
    scheduler: LinearDecayWithWarmUp
    batch_scheduler: true
    max_length: 512
    warmup_epochs: 0.3
    logger_verbose_step: 100
    ckpt_callback_verbose: true
    val_check_interval: 1000000
    optimizer: AdamW
    weight_decay: 0.0
    loss: ce
    loss_reduction: mean
    use_only_fold: false
model:
    model_name: microsoft/deberta-v3-base
    model_type: custom_head
    use_pretrained: true
    layer_norm_eps: 0.0000001
    dropout_rate: 0.1
    custom_head_types: ['cls'] # ['cls', 'attn', 'avg', 'max', 'conv']
    custom_head_ensemble: avg
    num_use_layers_as_output: 0
    num_reinit_layers: 0
    ckpt_from:
''')
ST = S['training']
SM = S['model']

#
# Prepare
#

fix_seed()
LOGGER = get_logger(__name__)

if not IS_KAGGLE:
    torch.multiprocessing.set_sharing_strategy('file_system')

if IS_KAGGLE:
    DATADIR = Path('/kaggle/input/') / S["competition"]
    CKPTDIR = Path('/kaggle/input/ckpt-mykaggle/') / S['name']
    OUTPUTDIR = Path('/kaggle/working')
else:
    DATADIR = Path('./data/quora/')
    CKPTDIR = Path('./ckpt/') / S['name']
    OUTPUTDIR = CKPTDIR
    if not CKPTDIR.exists():
        CKPTDIR.mkdir()


#
# Load Data
#


DF_TRAIN = pd.read_csv(DATADIR / ST['train_file'])
DF_TEST = pd.read_csv(DATADIR / ST['test_file'])
DF_SUB = pd.read_csv(DATADIR / 'sample_submission.csv')
if IS_DEBUG:
    DF_TRAIN = DF_TRAIN.iloc[:10000]
    DF_TEST = DF_TEST.iloc[:10000]
    DF_SUB = DF_SUB.iloc[:10000]

FOLD_COLUMN = 'fold'

if S['do_training'] and (FOLD_COLUMN not in DF_TRAIN.columns or ST['do_cv']):
    from mykaggle.trainer.cv_strategy import CVStrategy
    cv = CVStrategy.create(ST['cv'], ST['num_folds'])
    DF_TRAIN = cv.split_and_set(DF_TRAIN, y_column=ST['target_column'])
LOGGER.info(f'Training data: {len(DF_TRAIN)}, Test data: {len(DF_TEST)}')

#
# Dataset and Dataloader
#


class MyDataset(Dataset):
    def __init__(self, s: Dict, df: pd.DataFrame, tokenizer: PreTrainedTokenizer, mode: Mode, *args, **kwargs):
        super().__init__()
        self.st = s['training']
        self.df = df
        self.inputs = df[self.st['input_column']].values
        self.labels = None
        if self.st['target_column'] in df.columns:
            self.labels = df[self.st['target_column']].values
        self.tokenizer = tokenizer
        self.mode = mode

    def __len__(self) -> int:
        return len(self.df)

    def __getitem__(self, index: int):
        input = self.inputs[index]
        inputs = self.tokenizer(
            input,
            padding='max_length',
            truncation=True,
            max_length=self.st['max_length'],
            return_tensors='pt'
        )
        for key in inputs.keys():
            inputs[key] = inputs[key][0]

        if self.labels is not None:
            label = self.labels[index]
            return inputs, label
        return inputs


def get_dataloader(
    dataset: Dataset,
    batch_size: int,
    num_workers: int,
    mode: Mode,
    *args, **kwargs
) -> DataLoader:
    dataloader = DataLoader(
        dataset,
        batch_size=batch_size,
        pin_memory=False,
        drop_last=False,
        shuffle=mode == Mode.TRAIN,
        num_workers=num_workers,
    )
    return dataloader

#
# Model
#


class ModelCustomHeadEnsemble(nn.Module):
    def __init__(
        self,
        settings: Dict[str, Any],
        model: PreTrainedModel
    ) -> None:
        super().__init__()
        self.st = settings['training']
        self.sm = settings['model']
        self.model = model
        self.hidden_size = model.config.hidden_size  # type: ignore
        self.num_reinit_layers = self.sm.get('num_reinit_layers', 0)
        self.head_types = self.sm['custom_head_types']
        self.num_use_layers = self.sm['num_use_layers_as_output']
        output_layers = {}

        if 'attn' in self.head_types:
            self.hidden_dim = self.sm['head_hidden_dim']
            self.intermediate_dim = self.sm.get('head_intermediate_dim', self.hidden_dim)
            self.attn_head = AttentionHead(self.hidden_dim, self.intermediate_dim)
        if 'conv' in self.head_types:
            hidden_dim = self.sm.get('conv_head_hidden_dim', 256)
            kernel_size = self.sm.get('conv_head_kernel_size', 2)
            self.conv1 = nn.Conv1d(self.hidden_size, hidden_dim, kernel_size=kernel_size, padding=1)
            self.conv2 = nn.Conv1d(hidden_dim, 1, kernel_size=kernel_size, padding=1)
        if 'layers_sum' in self.head_types:
            self.layer_weight = nn.Parameter(torch.tensor([1] * self.num_use_layers, dtype=torch.float))

        for head in self.head_types:
            if 'concat' in head:
                output_layers[head] = nn.Linear(self.hidden_size * self.num_use_layers, 1)
            elif head == 'conv':
                continue
            else:
                output_layers[head] = nn.Linear(self.hidden_size, 1)
        self.output_layers = nn.ModuleDict(output_layers)

        self.dropout = nn.Dropout(self.sm['dropout_rate'])
        self.ensemble_type = self.sm['custom_head_ensemble']
        if self.ensemble_type == 'weight':
            self.ensemble_weight = nn.Linear(len(self.head_types), 1, bias=False)

        self.initialize()

    def forward(self, inputs):
        outputs = self.model(**inputs, output_hidden_states=True)
        head_features = []
        features = []
        if 'cls' in self.head_types:
            cls_state = outputs.last_hidden_state[:, 0, :]
            feature = self.output_layers['cls'](self.dropout(cls_state))
            head_features.append(cls_state)
            features.append(feature)
        if 'avg' in self.head_types:
            # input_mask = inputs['attention_mask'].unsqueeze(-1).float()
            # sum_embeddings = torch.sum(outputs.last_hidden_state * input_mask, 1)
            # avg_pool = sum_embeddings / torch.sum(input_mask, 1)
            avg_pool = torch.mean(outputs.last_hidden_state, 1)
            feature = self.output_layers['avg'](self.dropout(avg_pool))
            head_features.append(avg_pool)
            features.append(feature)
        if 'max' in self.head_types:
            # input_mask = inputs['attention_mask'].unsqueeze(-1).float()
            # max_pool = torch.max(outputs.last_hidden_state * input_mask, 1)[0]
            max_pool = torch.max(outputs.last_hidden_state, 1)[0]
            feature = self.output_layers['max'](self.dropout(max_pool))
            head_features.append(max_pool)
            features.append(feature)
        if 'attn' in self.head_types:
            attn_state = self.attn_head(outputs.last_hidden_state)
            feature = self.output_layers['attn'](self.dropout(attn_state))
            head_features.append(attn_state)
            features.append(feature)
        if 'conv' in self.head_types:
            conv_state = self.conv1(outputs.last_hidden_state.permute(0, 2, 1))
            conv_state = F.relu(self.conv2(conv_state))
            feature, _ = torch.max(conv_state, -1)
            head_features.append(conv_state)
            features.append(feature)
        if 'layers_concat' in self.head_types:
            hidden_states = outputs.hidden_states[-self.num_use_layers:]
            cat_feature = torch.cat([state[:, 0, :] for state in hidden_states], -1)
            feature = self.output_layers['layers_concat'](self.dropout(cat_feature))
            head_features.append(cat_feature)
            features.append(feature)
        if 'layers_avg' in self.head_types:
            hidden_states = torch.stack(outputs.hidden_states[-self.num_use_layers:], -1)[:, 0, :, :]
            avg_feature = torch.mean(hidden_states, -1)
            feature = self.output_layers['layers_avg'](self.dropout(avg_feature))
            head_features.append(avg_feature)
            features.append(feature)
        if 'layers_sum' in self.head_types:
            hidden_states = torch.stack(outputs.hidden_states[-self.num_use_layers:], -1)[:, 0, :, :]
            weight = self.layer_weight[None, None, :] / self.layer_weight.sum()
            weighted_sum_feature = torch.sum(hidden_states * weight, -1)
            feature = self.output_layers['layers_sum'](self.dropout(weighted_sum_feature))
            head_features.append(weighted_sum_feature)
            features.append(feature)

        outputs = torch.stack(features, -1)  # [bs, 1, num_heads]
        if len(self.head_types) > 1:
            if self.ensemble_type == 'avg':
                outputs = torch.mean(outputs, -1)
            elif self.ensemble_type == 'weight':
                weight = self.ensemble_weight.weight / torch.sum(self.ensemble_weight.weight)
                outputs = torch.sum(weight * outputs, -1)
        outputs = outputs.reshape((inputs['input_ids'].shape[0]))
        return outputs

    def initialize(self):
        if self.ensemble_type == 'weight':
            torch.nn.init.constant_(self.ensemble_weight.weight, 1.0)
        self.output_layers.apply(self._init_weight)
        for i in range(self.num_reinit_layers):
            self.model.encoder.layer[-(1 + i)].apply(self._init_weight)

    def _init_weight(self, module):
        if isinstance(module, nn.Linear):
            module.weight.data.normal_(mean=0.0, std=self.model.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()


def get_model(model_name: str, use_pretrained: bool = True, ckpt_dir: Path = CKPTDIR) -> nn.Module:
    '''
    特定の形式のモデル（BERT, RoBERTa等）を次のどちらかで作成する
    1. HuggingFace 等に上がっている pretrained parameters で初期化
    2. random initialized parameters
    2を選択する場合、モデル構造の json ファイルが必要になる。該当ファイルがない場合、ダウンロードを挟む。
    Args:
        use_pretrained: 1 or 2 を決める
        ckpt_dir: config.json のあるディレクトリ
    '''
    if use_pretrained:
        hg_model = AutoModel.from_pretrained(model_name)
    else:
        config_path = Path(ckpt_dir / 'config.json')
        if not config_path.exists():
            AutoConfig.from_pretrained(model_name).save_pretrained(OUTPUTDIR)
            ckpt_dir = OUTPUTDIR
        hg_model = AutoModel.from_config(AutoConfig.from_pretrained(ckpt_dir / 'config.json'))
    model = ModelCustomHeadEnsemble(S, hg_model)
    return model


#
# Trainer
#


def get_loss_fn(s: Dict[str, Any], **kwargs) -> nn.Module:
    return nn.BCEWithLogitsLoss(reduction=s['loss_reduction'])


def get_optimizer(name: str, lr: float, weight_decay: float, parameters, **kwargs) -> torch.optim.Optimizer:
    optimizer: torch.optim.Optimizer
    if name.lower() == 'adam':
        optimizer = torch.optim.Adam(
            parameters,
            lr=lr,
            weight_decay=weight_decay
        )
    else:
        optimizer = torch.optim.Adam(
            parameters,
            lr=lr,
            weight_decay=weight_decay
        )
    return optimizer


def get_scheduler(s: Dict[str, Any], optimizer: torch.optim.Optimizer) -> Optional[LRScheduler]:
    scheduler_name = s.get('scheduler')
    scheduler: LRScheduler
    if scheduler_name is None:
        return None
    elif scheduler_name == 'ExponentialDecay':
        scheduler = torch.optim.lr_scheduler.LambdaLR(
            optimizer, lambda e: 1 / (1 + e), last_epoch=-1
        )
    elif scheduler_name == 'LinearDecayWithWarmUp':
        scheduler = get_linear_schedule_with_warmup(
            optimizer,
            s['warmup_epochs'] * s['num_batches'],
            s['num_total_steps'],
        )
    elif scheduler_name == 'CosineDecayWithWarmUp':
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            s['warmup_epochs'] * s['num_batches'],
            s['num_total_steps'],
        )
    return scheduler


class Trainer:
    '''
    与えられた train/valid データとモデルで training/validation を行うクラス
    '''

    def __init__(
        self,
        s: Dict[str, Any],
        ckptdir: Path,
        ml_logger: Logger,
        fold: int = 0,
        *args, **kwargs
    ) -> None:
        self.st = s['training']
        self.sm = s['model']
        self.ckptdir = ckptdir
        self.ml_logger = ml_logger
        self.fold = fold

        self.global_step = 0
        self.best_score = 0.0
        self.scaler = GradScaler(enabled=self.st['use_amp'])
        self.device = torch.device(s['device'])
        self.mb = master_bar(range(self.st['num_epochs']))

    def train(
        self,
        train_dataloader: DataLoader,
        valid_dataloader: DataLoader,
        model: nn.Module,
        loss_fn: nn.Module,
        optimizer: torch.optim.Optimizer,
        scheduler: Optional[LRScheduler],
        *args, **kwargs
    ) -> None:
        model.train()

        for epoch in self.mb:
            for i, batch in enumerate(progress_bar(train_dataloader, parent=self.mb)):
                inputs, labels = batch
                self.train_step(self.st, i, inputs, labels, model, loss_fn, optimizer, scheduler, len(train_dataloader))

                if (
                    self.global_step % self.st['val_check_interval'] == 0
                    and (i + 1) % self.st['num_accumulations'] == 0
                ):
                    self.evaluate(valid_dataloader, model, epoch=epoch)
                    model.train()

            self.evaluate(valid_dataloader, model, epoch=epoch)
            model.train()
        self.upload_model(self.fold)

    def train_step(
        self,
        st: Dict[str, Any],
        step: int,
        inputs,
        labels,
        model: nn.Module,
        loss_fn: nn.Module,
        optimizer: torch.optim.Optimizer,
        scheduler: Optional[LRScheduler],
        num_steps_per_epoch: int
    ):
        for key in inputs.keys():
            inputs[key] = inputs[key].to(self.device).long()
        if isinstance(labels, (tuple, list)):
            labels, weights = labels
            weights = weights.to(self.device)
        labels = labels.to(self.device).float()
        with autocast(enabled=st['use_amp']):
            outputs_tuple1 = model(inputs)
            if isinstance(outputs_tuple1, (tuple, list)):
                outputs1 = outputs_tuple1[0]
            else:
                outputs1 = outputs_tuple1
            loss = loss_fn(outputs1, labels)
        self.scaler.scale(loss).backward()

        if ((step + 1) % st['num_accumulations'] == 0) or ((step + 1) == num_steps_per_epoch):
            self.global_step += 1
            self.scaler.step(optimizer)
            self.scaler.update()
            optimizer.zero_grad()
            if scheduler is not None and st['batch_scheduler']:
                scheduler.step()
            loss_value = float(loss.detach().cpu().numpy())
            self._log_metric(f'train_loss_{self.fold}', loss_value, self.global_step, st['logger_verbose_step'])

            if self.fold == 0 and scheduler is not None:
                self._log_metric('lr', scheduler.get_last_lr()[0], self.global_step, st['logger_verbose_step'])

    def evaluate(
        self,
        valid_dataloader: DataLoader,
        model: nn.Module,
        **kwargs
    ) -> None:
        current_epoch = self.global_step * self.st['num_accumulations'] / self.st['num_batches']
        if current_epoch < self.st['warmup_epochs']:
            return
        preds, targets = self.validation(valid_dataloader, model)
        val_metric = roc_auc_score(targets, preds)
        self._log_metric(f'val_metric_{self.fold}', val_metric, step=self.global_step)
        if val_metric > self.best_score:
            if self.st['ckpt_callback_verbose']:
                self.mb.write(f'FOLD {self.fold}, best auc updated: {val_metric:.5f} from {self.best_score:.5f}')
            self.best_score = val_metric
            self.save_model(model, self.fold)

    def validation(
        self,
        dataloader: DataLoader,
        model: nn.Module,
        *args, **kwargs
    ) -> Tuple[np.ndarray, np.ndarray]:
        model.eval()
        bs = self.st['test_batch_size']
        data_size = len(dataloader.dataset)  # type: ignore
        preds = np.zeros(data_size)
        targets = np.zeros(data_size)
        for i, batch in enumerate(dataloader):
            inputs, labels = batch
            for key in inputs.keys():
                inputs[key] = inputs[key].to(self.device).long()
            labels = labels.to(self.device).float()

            with autocast(enabled=self.st['use_amp']):
                with torch.no_grad():
                    outputs = model(inputs)
                    if isinstance(outputs, (tuple, list)):
                        outputs = outputs[0]
            preds[i * bs: (i + 1) * bs] = outputs.detach().cpu().numpy()
            targets[i * bs: (i + 1) * bs] = labels.cpu().numpy()
        return preds, targets

    def save_model(self, model: nn.Module, fold: int) -> None:
        self.ml_logger.save_model(model, f'model_{fold}.pt')

    def upload_model(self, fold: int) -> None:
        LOGGER.info('Uploding model ...')
        self.ml_logger.upload_model(f'model_{fold}.pt')

    def _log_metric(self, name: str, value: float, step: int, log_interval: Optional[int] = None) -> None:
        if log_interval is None:
            self.ml_logger.log_metric(name, value, step)
        elif step % log_interval == 0:
            self.ml_logger.log_metric(name, value, step)


def train(s: Dict[str, Any], ml_logger: Logger, df: pd.DataFrame):
    st = s['training']
    sm = s['model']
    ml_logger.log_params(st)
    ml_logger.log_params(sm)
    tokenizer = AutoTokenizer.from_pretrained(sm['model_name'])
    tokenizer.save_pretrained(OUTPUTDIR)
    oof_preds = np.zeros((df.shape[0]))

    for fold in range(st['num_folds']):
        df_train = df[df[FOLD_COLUMN] != fold]
        df_valid = df[df[FOLD_COLUMN] == fold]
        train_ds = MyDataset(s, df_train, tokenizer, Mode.TRAIN)
        valid_ds = MyDataset(s, df_valid, tokenizer, Mode.VALID)
        train_dataloader = get_dataloader(train_ds, st['batch_size'], st['num_workers'], Mode.TRAIN)
        valid_dataloader = get_dataloader(valid_ds, st['test_batch_size'], st['num_workers'], Mode.VALID)
        st['num_batches'] = len(train_dataloader)
        st['num_total_steps'] = len(train_dataloader) * st['num_epochs']
        model = get_model(sm['model_name']).to(s['device'])

        # training
        loss_fn = get_loss_fn(st)
        optimizer = get_optimizer(st['optimizer'], st['learning_rate'], st['weight_decay'], model.parameters())
        scheduler = get_scheduler(st, optimizer)
        trainer = Trainer(s, CKPTDIR, ml_logger, fold)
        trainer.train(train_dataloader, valid_dataloader, model, loss_fn, optimizer, scheduler)

        # predict
        state_dict = torch.load(CKPTDIR / f'model_{fold}.pt')
        model.load_state_dict(state_dict)
        val_preds, _ = trainer.validation(valid_dataloader, model)
        score = roc_auc_score(df_valid[st['target_column']].values, val_preds)
        LOGGER.info(f'auc_{fold}: {score}')
        ml_logger.log_metric(f'metric_{fold}', score)
        oof_preds[df_valid.index] = val_preds
        model.model.config.save_pretrained(OUTPUTDIR)  # type: ignore
        del trainer, model, state_dict, optimizer, scheduler, loss_fn
        del train_ds, valid_ds, train_dataloader, valid_dataloader
        gc.collect()
        torch.cuda.empty_cache()
        if ST.get('use_only_fold', False):
            break

    score = roc_auc_score(df[st['target_column']].values, oof_preds)
    ml_logger.log_metric('metric', score)
    pickle.dump(oof_preds, open(OUTPUTDIR / 'oof_preds.pkl', 'wb'))
    LOGGER.info(f'training finished. Metric: {score:.3f}')
    return oof_preds


def predict(
    model: nn.Module,
    df: pd.DataFrame,
    dataloader: DataLoader,
    batch_size: int,
    use_amp: bool = True
) -> np.ndarray:
    preds = np.zeros((len(df)), dtype=np.float32)
    device = torch.device(S['device'])
    model.to(device)
    model.eval()
    for i, batch in enumerate(dataloader):
        if isinstance(batch, (list, tuple)):
            inputs = batch[0]
        else:
            inputs = batch
        for key in inputs.keys():
            inputs[key] = inputs[key].to(device).long()
        with autocast(enabled=use_amp):
            with torch.no_grad():
                outputs = model(inputs)
                if isinstance(outputs, (list, tuple)):
                    outputs = outputs[0]
        preds[i * batch_size:(i + 1) * batch_size] = outputs.detach().cpu().numpy()
    return preds


def test(s: Dict[str, Any], model: nn.Module, dataloader: DataLoader, df: pd.DataFrame):
    bs = s['training']['test_batch_size']
    use_amp = s['training']['use_amp']
    preds = predict(model, df, dataloader, bs, use_amp)
    return preds


def infer(s: Dict[str, Any], df: pd.DataFrame):
    st = s['training']
    sm = s['model']
    try:
        tokenizer = AutoTokenizer.from_pretrained(CKPTDIR)
    except Exception:
        tokenizer = AutoTokenizer.from_pretrained(sm['model_name'])
        tokenizer.save_pretrained(OUTPUTDIR)
    test_preds = np.zeros((len(df)))
    for fold in range(st['num_folds']):
        LOGGER.info(f'inference fold {fold} started.')
        ds = MyDataset(s, df, tokenizer, Mode.TEST, fold)
        dataloader = get_dataloader(ds, st['test_batch_size'], st['num_workers'], Mode.TEST)
        model = get_model(sm['model_name'], use_pretrained=False)
        model.load_state_dict(torch.load(CKPTDIR / f'model_{fold}.pt'))
        preds = test(s, model, dataloader, df)
        test_preds += preds / st['num_folds']
        if st.get('use_only_fold', False):
            break
    LOGGER.info('inference finished.')
    test_preds = sigmoid(test_preds)
    pickle.dump(test_preds, open(OUTPUTDIR / 'test_preds.pkl', 'wb'))
    return test_preds


def submit(s: Dict[str, Any], df: pd.DataFrame, preds: np.ndarray) -> None:
    df[s['training']['target_column']] = preds
    df.to_csv(OUTPUTDIR / 'submission.csv', index=False)


def train_with_logger(s: Dict[str, Any], df: pd.DataFrame) -> np.ndarray:
    logger_type = LoggerType.STD if IS_KAGGLE else LoggerType.MLFLOW
    ml_logger = LoggerFactory.create(logger_type, 'cfiken', CKPTDIR)
    with ml_logger.start(experiment_name=S['competition'], run_name=S['name']):
        ml_logger.save_config(S)
        return train(s, ml_logger, df)


def run(gpu_index: int = 0):
    S['device'] = f'cuda:{gpu_index}'
    if S['do_training']:
        train_with_logger(S, DF_TRAIN.copy())
    if S['do_inference']:
        test_preds = infer(S, DF_TEST.copy())
    if S['do_submit']:
        submit(S, DF_SUB.copy(), test_preds)


if __name__ == '__main__':
    if IS_KAGGLE:
        LOGGER.info('Starting in kaggle environment')
        fix_seed(S['seed'])
        run()
    else:
        args = parse()
        LOGGER.info(f'Starting with args: {args}')
        os.environ['TOKENIZERS_PARALLELISM'] = 'true'
        fix_seed(S['seed'])
        run(int(args.gpus))
