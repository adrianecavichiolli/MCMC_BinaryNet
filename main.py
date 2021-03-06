import torch
import torch.nn as nn
import torch.utils.data

from layers import ScaleLayer, BinaryDecorator, binarize_model
from trainer import TrainerGradFullPrecision, TrainerMCMC, TrainerGradBinary, TrainerMCMCTree
from utils import AdamCustomDecay


linear_features = {
    "MNIST": (28 ** 2, 10),
    "MNIST56": (5 ** 2, 2),
    "CIFAR10": (3 * 32 ** 2, 10),
}


class NetBinary(nn.Module):
    def __init__(self, conv_channels=(), fc_sizes=(), conv_kernel=3, batch_norm=True, scale_layer=False):
        super().__init__()

        conv_layers = []
        for (in_features, out_features) in zip(conv_channels[:-1], conv_channels[1:]):
            if batch_norm:
                conv_layers.append(nn.BatchNorm2d(in_features))
            conv_layers.append(nn.Conv2d(in_features, out_features, kernel_size=conv_kernel, padding=0, bias=False))
            conv_layers.append(nn.MaxPool2d(kernel_size=2))
            conv_layers.append(nn.PReLU())
        self.conv_sequential = nn.Sequential(*conv_layers)

        fc_layers = []
        for (in_features, out_features) in zip(fc_sizes[:-1], fc_sizes[1:]):
            if batch_norm:
                fc_layers.append(nn.BatchNorm1d(in_features))
            fc_layers.append(nn.Linear(in_features, out_features, bias=False))
            fc_layers.append(nn.PReLU(out_features))
        self.fc_sequential = nn.Sequential(*fc_layers)
        if scale_layer:
            self.scale_layer = ScaleLayer(size=fc_sizes[-1])
        else:
            self.scale_layer = None

    def forward(self, x):
        x = self.conv_sequential(x)
        x = x.view(x.shape[0], -1)
        x = self.fc_sequential(x)
        if self.scale_layer is not None:
            x = self.scale_layer(x)
        return x


def train_gradient(model: nn.Module = None, is_binary=True, dataset_name="MNIST"):
    if model is None:
        model = NetBinary(fc_sizes=linear_features[dataset_name])
    optimizer = AdamCustomDecay(model.parameters(), lr=1e-2, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode='min', factor=0.5, patience=10,
                                                           threshold=1e-3, min_lr=1e-4)
    if is_binary:
        model = binarize_model(model, keep_data=False)
        trainer_cls = TrainerGradBinary
    else:
        trainer_cls = TrainerGradFullPrecision
    trainer = trainer_cls(model,
                          criterion=nn.CrossEntropyLoss(),
                          dataset_name=dataset_name,
                          optimizer=optimizer,
                          scheduler=scheduler)
    trainer.train(n_epoch=500, save=False, with_mutual_info=True)


def train_mcmc(model: nn.Module = None, dataset_name="MNIST"):
    if model is None:
        model = NetBinary(fc_sizes=linear_features[dataset_name], batch_norm=False)
    model = binarize_model(model)
    trainer = TrainerMCMC(model,
                          criterion=nn.CrossEntropyLoss(),
                          dataset_name=dataset_name,
                          flip_ratio=0.1)
    trainer.train(n_epoch=500, save=False, with_mutual_info=False, epoch_update_step=3)


def set_seed(seed: int):
    import random
    import numpy as np
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


if __name__ == '__main__':
    set_seed(seed=113)
    # train_gradient(NetBinary(fc_sizes=(784, 100, 20, 10), batch_norm=False), is_binary=False, dataset_name="MNIST")
    train_gradient(NetBinary(fc_sizes=(25, 17, 10, 5, 2), batch_norm=False), dataset_name="MNIST56", is_binary=False)
    # train_mcmc(NetBinary(fc_sizes=(25, 2), batch_norm=False), dataset_name="MNIST56")
