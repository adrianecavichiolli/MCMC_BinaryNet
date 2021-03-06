import math
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Callable, Union, List, Dict
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import torch
import torch.nn as nn
import torch.utils.data
from sklearn.metrics import mutual_info_score
from sklearn import cluster
from torch.autograd import Variable

from monitor.batch_timer import Schedule
from monitor.mutual_info.kraskov_knn import get_mi as mutual_info_score_knn
from monitor.viz import VisdomMighty


class MutualInfo(ABC):

    log2e = math.log2(math.e)

    def __init__(self, estimate_size: int = np.inf, compression_range=(0.50, 0.999), debug=False):
        """
        :param estimate_size: number of samples to estimate MI from
        :param compression_range: min & max acceptable quantization compression range
        """
        self.estimate_size = estimate_size
        self.compression_range = compression_range
        self.debug = debug
        self.n_bins = defaultdict(int)
        self.max_trials_adjust = 10
        self.layers = {}
        self.activations = defaultdict(list)
        self.quantized = {}
        self.information = {}
        self.is_active = False
        self.eval_loader = None

    @property
    def n_bins_default(self):
        return 20

    def register(self, layer: nn.Module, name: str):
        self.layers[name] = (layer, layer.forward)  # immutable

    def update(self, model: nn.Module):
        if self.eval_loader is None:
            # did you forget to call .prepare()?
            return
        self.start_listening()
        if not self.is_active:
            # not ready yet
            return
        use_cuda = torch.cuda.is_available()
        for batch_id, (images, labels) in enumerate(iter(self.eval_loader)):
            if use_cuda:
                images = images.cuda()
            model(Variable(images, volatile=True))
            if batch_id * self.eval_loader.batch_size >= self.estimate_size:
                break
        self.finish_listening()

    def decorate_evaluation(self, func: Callable):
        def wrapped(*args, **kwargs):
            self.start_listening()
            res = func(*args, **kwargs)
            self.finish_listening()
            return res
        print(f"Decorated '{func.__name__}' function to save layers' activations for MI estimation")
        return wrapped

    def prepare(self, loader: torch.utils.data.DataLoader):
        self.eval_loader = loader
        inputs = []
        targets = []
        for images, labels in iter(loader):
            inputs.append(images)
            targets.append(labels)
            if len(inputs) * loader.batch_size >= self.estimate_size:
                break
        self.activations['input'] = self.process(layer_name='input', activations=inputs)
        self.activations['target'] = self.process(layer_name='target', activations=targets)

    @Schedule(epoch_update=0, batch_update=5)
    def start_listening(self):
        for name, (layer, forward_orig) in self.layers.items():
            if layer.forward == forward_orig:
                layer.forward = self.wrap_forward(layer_name=name, forward_orig=forward_orig)
        self.is_active = True

    def finish_listening(self):
        if not self.is_active:
            return
        for name, (layer, forward_orig) in self.layers.items():
            layer.forward = forward_orig
        self.is_active = False
        self.save_information_async()

    def wrap_forward(self, layer_name, forward_orig):
        def forward_and_save(input):
            assert self.is_active, 'Did you forget to start the job?'
            output = forward_orig(input)
            self.save_activations(layer_name, output)
            return output
        return forward_and_save

    def save_activations(self, layer_name: str, tensor: torch.autograd.Variable):
        if sum(map(len, self.activations[layer_name])) < self.estimate_size:
            self.activations[layer_name].append(tensor.data.cpu().clone())

    @abstractmethod
    def process(self, layer_name: str, activations: List[torch.FloatTensor]):
        activations = torch.cat(activations, dim=0)
        size = min(len(activations), self.estimate_size)
        activations = activations[: size]
        return activations

    def hidden_layer_names(self):
        return [name for name in self.activations if name not in ('input', 'target')]

    def compute_mutual_info(self, x, y) -> float:
        return mutual_info_score(x, y) * self.log2e

    def plot_quantized_hist(self, viz: VisdomMighty):
        for name, layer_quantized in self.quantized.items():
            _, counts = np.unique(layer_quantized, return_counts=True)
            counts.sort()
            counts = counts[::-1]
            viz.bar(Y=np.arange(len(counts), dtype=int), X=counts, win=f'{name} MI hist', opts=dict(
                xlabel='bin ID',
                ylabel='# items',
                title=f'{name} MI quantized histogram',
            ))

    @Schedule(epoch_update=1)
    def plot_quantized_dispersion(self, viz: VisdomMighty):
        if any(n_bins == 0 for n_bins in self.n_bins.values()):
            # algorithm doesn't use binning
            return
        legend = []
        for name in self.quantized.keys():
            legend.append(f'{name} ({self.n_bins[name]} bins)')
        if len(set(self.n_bins[name] for name in self.quantized.keys())) == 1:
            # all layers have the same n_bins
            n_bins = self.n_bins['input']
            counts = np.zeros(shape=(len(self.quantized), n_bins), dtype=np.int32)
            for layer_id, (name, layer_quantized) in enumerate(self.quantized.items()):
                _, layer_counts = np.unique(layer_quantized, return_counts=True)
                counts[layer_id, :len(layer_counts)] = layer_counts
            viz.boxplot(X=counts.transpose(), win='MI hist', opts=dict(
                ylabel='# items in one bin',
                title='MI quantized dispersion (smaller is better)',
                legend=legend,
            ))
        else:
            viz.boxplot(X=np.vstack(self.quantized.values()).transpose(), win='MI hist', opts=dict(
                ylabel='bin ID dispersion',
                title='MI inverse quantized dispersion (smaller is worse)',
                legend=legend,
            ))
        if self.debug:
            self.plot_quantized_hist(viz)

    def _compute_async(self, args):
        name, activations = args
        quantized = self.process(name, activations)
        info_x = self.compute_mutual_info(self.activations['input'], quantized)
        info_y = self.compute_mutual_info(self.activations['target'], quantized)
        return name, quantized, info_x, info_y, self.n_bins[name]

    def save_information_async(self):
        self.quantized = dict(input=self.activations['input'])
        with ProcessPoolExecutor() as executor:
            args = [(hname, self.activations.pop(hname)) for hname in self.hidden_layer_names()]
            for (hname, quantized, info_x, info_y, n_bins) in executor.map(self._compute_async, args):
                self.information[hname] = (info_x, info_y)
                self.quantized[hname] = quantized
                self.n_bins[hname] = n_bins

    def save_information(self):
        self.quantized = dict(input=self.activations['input'])
        for hname in self.hidden_layer_names():
            self.quantized[hname] = self.process(hname, self.activations.pop(hname))
            info_x = self.compute_mutual_info(self.activations['input'], self.quantized[hname])
            info_y = self.compute_mutual_info(self.activations['target'], self.quantized[hname])
            self.information[hname] = (info_x, info_y)

    def plot(self, viz):
        assert not self.is_active, "Wait, not finished yet."
        if len(self.information) == 0:
            return
        legend = []
        ys = []
        xs = []
        self.plot_quantized_dispersion(viz)
        for layer_name, (info_x, info_y) in list(self.information.items()):
            ys.append(info_y)
            xs.append(info_x)
            legend.append(layer_name)
            del self.information[layer_name]
        title = 'Mutual information plane'
        if len(ys) > 1:
            ys = [ys]
            xs = [xs]
        viz.line(Y=np.array(ys), X=np.array(xs), win=title, opts=dict(
            xlabel='I(X, T), bits',
            ylabel='I(T, Y), bits',
            title=title,
            legend=legend,
        ), update='append' if viz.win_exists(title) else None)


class MutualInfoBin(MutualInfo):

    def process(self, layer_name: str, activations: List[torch.FloatTensor]) -> np.ndarray:
        activations = super().process(layer_name, activations)
        if layer_name == 'target':
            assert isinstance(activations, (torch.LongTensor, torch.IntTensor))
            activations = activations.numpy()
        else:
            activations = activations.view(activations.shape[0], -1)
            if layer_name not in self.n_bins:
                self.n_bins[layer_name] = self.adjust_bins(layer_name, activations)
            activations = self.quantize(layer_name, activations, n_bins=self.n_bins[layer_name])
        return activations

    def adjust_bins(self, layer_name: str, activations: torch.FloatTensor) -> int:
        n_bins = self.n_bins_default
        compression_min, compression_max = self.compression_range
        for trial in range(self.max_trials_adjust):
            digitized = self.digitize(layer_name, activations, n_bins)
            unique = np.unique(digitized, axis=0)
            compression = (len(activations) - len(unique)) / len(activations)
            if compression > compression_max:
                n_bins *= 2
            elif compression < compression_min:
                n_bins = max(2, int(n_bins / 2))
                if n_bins == 2:
                    break
            else:
                break
        return n_bins

    @abstractmethod
    def digitize(self, layer_name: str, activations: torch.FloatTensor, n_bins: int) -> np.ndarray:
        pass

    def quantize(self, layer_name: str, activations: torch.FloatTensor, n_bins: int) -> np.ndarray:
        digitized = self.digitize(layer_name, activations, n_bins=n_bins)
        unique, inverse = np.unique(digitized, return_inverse=True, axis=0)
        return inverse


class MutualInfoBinFixed(MutualInfoBin):

    def digitize(self, layer_name: str, activations: torch.FloatTensor, n_bins: int) -> np.ndarray:
        mean = activations.mean(dim=0)
        sig = activations.std(dim=0)
        dim0_min, dim0_max = mean - 2 * sig, mean + 2 * sig
        digitized = n_bins * (activations - dim0_min) / (dim0_max - dim0_min)
        digitized.clamp_(min=0, max=n_bins)
        digitized = digitized.type(torch.LongTensor).numpy()
        return digitized


class MutualInfoBinFixedFlat(MutualInfoBinFixed):

    def digitize(self, layer_name: str, activations: torch.FloatTensor, n_bins: int) -> np.ndarray:
        shape = activations.shape
        return super().digitize(layer_name, activations.view(-1), n_bins).view(shape)


class MutualInfoQuantile(MutualInfoBin):

    def digitize(self, layer_name: str, activations: torch.FloatTensor, n_bins: int) -> np.ndarray:
        activations = activations.cpu()
        bins = np.percentile(activations,
                             q=np.linspace(start=0, stop=100, num=n_bins, endpoint=True),
                             axis=0)
        digitized = np.empty_like(activations, dtype=np.int32)
        for dim in range(activations.shape[1]):
            digitized[:, dim] = np.digitize(activations[:, dim], bins[:, dim], right=True)
        return digitized


class MutualInfoKMeans(MutualInfoBin):

    def digitize(self, layer_name: str, activations: torch.FloatTensor, n_bins: int) -> np.ndarray:
        model = cluster.MiniBatchKMeans(n_clusters=n_bins)
        labels = model.fit_predict(activations)
        return labels

    def quantize(self, layer_name: str, activations: torch.FloatTensor, n_bins: int) -> np.ndarray:
        return self.digitize(layer_name, activations, n_bins=n_bins)


class MutualInfoSign(MutualInfo):

    def process(self, layer_name: str, activations: List[torch.FloatTensor]) -> np.ndarray:
        activations = super().process(layer_name, activations)
        if layer_name == 'target':
            assert isinstance(activations, (torch.LongTensor, torch.IntTensor))
            activations = activations.numpy()
        else:
            activations = activations.view(activations.shape[0], -1)
            activations.sign_()
            unique, inverse = np.unique(activations.numpy(), return_inverse=True, axis=0)
            self.n_bins[layer_name] = len(unique)
            activations = inverse
        return activations


class MutualInfoKNN(MutualInfo):

    def process(self, layer_name: str, activations: List[torch.FloatTensor]) -> np.ndarray:
        activations = super().process(layer_name, activations)
        if layer_name == 'target':
            assert isinstance(activations, (torch.LongTensor, torch.IntTensor))
            activations.unsqueeze_(dim=1)
        else:
            activations = activations.view(activations.shape[0], -1)
        return activations.numpy()

    def compute_mutual_info(self, x, y) -> float:
        return mutual_info_score_knn(x, y, k=3, estimator='ksg')
