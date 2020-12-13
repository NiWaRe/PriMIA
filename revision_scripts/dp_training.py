from tqdm import tqdm

import torch as th
import syft as sy
from torchvision import datasets, transforms
from opacus import PrivacyEngine, utils, privacy_analysis as tf_privacy
from module_modification import convert_batchnorm_modules

from os.path import dirname, abspath
from sys import path as syspath
from inspect import getfile, currentframe
from torchvision.datasets.folder import default_loader

import albumentations as a
from PIL import Image
from numpy import array, all as np_all, full_like
from collections import Counter

currentdir = dirname(abspath(getfile(currentframe())))
parentdir = dirname(currentdir)
syspath.insert(0, parentdir)
from torchlib.models import (
    conv_at_resolution,  # pylint:disable=import-error
    resnet18,
    vgg16,
)

BATCH_SIZE = 1
NOISE_MULTIPLIER = 0.1
MAX_GRAD_NORM = 1.3
LR = 1e-3
ALPHAS = range(2, 32)
TARGET_DELTA = 1e-1


class AlbumentationsTorchTransform:
    def __init__(self, transform, **kwargs):
        # print("init albu transform wrapper")
        self.transform = transform
        self.kwargs = kwargs

    def __call__(self, img):
        # print("call albu transform wrapper")
        if Image.isImageType(img):
            img = array(img)
        elif th.is_tensor(img):
            img = img.cpu().numpy()
        img = self.transform(image=img, **self.kwargs)["image"]
        # if img.max() > 1:
        #     img = a.augmentations.functional.to_float(img, max_value=255)
        img = th.from_numpy(img)
        if img.shape[-1] < img.shape[0]:
            img = img.permute(2, 0, 1)
        return img


def make_model():
    model_args = {
        "pretrained": False,
        "num_classes": 3,
        "in_channels": 3,
        "adptpool": False,
        "input_size": 224,
        "pooling": "max",
    }
    model = resnet18(**model_args)
    model = convert_batchnorm_modules(model)
    return model


def send_new_models(local_model, models):
    with th.no_grad():
        for w_id, remote_model in models.items():
            for new_param, remote_param in zip(
                local_model.parameters(), remote_model.parameters()
            ):
                worker = remote_param.location
                remote_value = new_param.send(worker)
                remote_param.set_(remote_value)
                models[w_id] = remote_model


def federated_aggregation(local_model, models):
    with th.no_grad():
        for local_param, *remote_params in zip(
            *([local_model.parameters()] + [model.parameters() for model in models])
        ):
            param_stack = th.zeros(*remote_params[0].shape)
            for remote_param in remote_params:
                param_stack += remote_param.copy().get()
            param_stack /= len(remote_params)
            local_param.set_(param_stack)


def aggregation(
    local_model, models, workers, crypto_provider, weights=None, secure=True,
):

    local_keys = local_model.state_dict().keys()
    ids = [name if type(name) == str else name.id for name in workers]
    remote_keys = []
    for id_ in ids:
        remote_keys.extend(list(models[id_].state_dict().keys()))

    c = Counter(remote_keys)
    assert np_all(
        list(c.values()) == full_like(list(c.values()), len(workers))
    ) and list(c.keys()) == list(local_keys)
    fresh_state_dict = dict()
    for key in list(local_keys):
        if "num_batches_tracked" in str(key):
            continue
        local_shape = local_model.state_dict()[key].shape
        remote_param_list = []
        for worker in workers:
            if secure:
                remote_param_list.append(
                    (
                        models[worker if type(worker) == str else worker.id]
                        .state_dict()[key]
                        .encrypt(
                            workers=workers,
                            crypto_provider=crypto,
                            protocol="fss",
                            precision_fractional=16,
                        )
                        * (
                            weights[worker if type(worker) == str else worker.id]
                            if weights
                            else 1
                        )
                    ).get()
                )
            else:
                remote_param_list.append(
                    models[worker if type(worker) == str else worker.id]
                    .state_dict()[key]
                    .data.get()
                    .copy()
                    * (
                        weights[worker if type(worker) == str else worker.id]
                        if weights
                        else 1
                    )
                )

        remote_shapes = [p.shape for p in remote_param_list]
        assert len(set(remote_shapes)) == 1 and local_shape == next(
            iter(set(remote_shapes))
        ), "Shape mismatch AFTER sending and getting"
        if secure:
            sumstacked = (
                th.sum(  # pylint:disable=no-member
                    th.stack(remote_param_list), dim=0  # pylint:disable=no-member
                )
                .get()
                .float_prec()
            )
        else:
            sumstacked = th.sum(  # pylint:disable=no-member
                th.stack(remote_param_list), dim=0  # pylint:disable=no-member
            )
        fresh_state_dict[key] = sumstacked if weights else sumstacked / len(workers)
    local_model.load_state_dict(fresh_state_dict)
    return local_model


def get_privacy_spent(
    target_delta=None, steps=None, alphas=None, sample_rate=1, noise_multiplier=None
):  # steps is the number of optimisation steps
    rdp = (
        th.tensor(tf_privacy.compute_rdp(sample_rate, noise_multiplier, 1, alphas))
        * steps
    )
    return tf_privacy.get_privacy_spent(alphas, rdp, target_delta)


hook = sy.TorchHook(th)
alice = sy.VirtualWorker(hook, id="alice")
bob = sy.VirtualWorker(hook, id="bob")
crypto = sy.VirtualWorker(hook, id="crypto_provider")
workers = [alice, bob]

sy.local_worker.is_client_worker = False

# TODO: add transforms
stats_tf = AlbumentationsTorchTransform(
    a.Compose([a.Resize(224, 224), a.RandomCrop(224, 224), a.ToFloat(max_value=255.0),])
)
dataset = datasets.ImageFolder(
    "data/server_simulation/worker1", transform=stats_tf, loader=default_loader,
)
train_datasets = dataset.federate(workers)


# the local version that we will use to do the aggregation
local_model = make_model()

models, dataloaders, optimizers, privacy_engines = {}, {}, {}, {}
for worker in workers:
    model = make_model()
    optimizer = th.optim.SGD(model.parameters(), lr=LR)  # not used at all
    model.send(worker)
    dataset = train_datasets[worker.id]
    dataloader = th.utils.data.DataLoader(
        dataset, batch_size=BATCH_SIZE, shuffle=True, drop_last=True
    )
    # privacy_engine = PrivacyEngine(
    #     model,
    #     batch_size=batch_size,
    #     sample_size=len(dataset),
    #     alphas=range(2, 32),
    #     noise_multiplier=1.2,
    #     max_grad_norm=1.0,
    #     secure_rng=False,
    # )
    # privacy_engine.attach(optimizer)

    models[worker.id] = model
    dataloaders[worker.id] = dataloader
    optimizers[worker.id] = optimizer
    # privacy_engines[worker.id] = privacy_engine

for epoch in range(10):
    # 1. Send new version of the model
    send_new_models(local_model, models)

    # 2. Train remotely the models
    for i, worker in enumerate(workers):

        dataloader = dataloaders[worker.id]
        model = models[worker.id]
        optimizer = optimizers[worker.id]

        model.train()
        criterion = th.nn.CrossEntropyLoss()
        losses = []

        total_optimisation_steps = 0  # for moments accountant, privacy

        for i, (data, target) in enumerate(tqdm(dataloader)):
            # optimizer.zero_grad() #not needed since we're playing optimiser
            output = model(data)
            loss = criterion(output, target)
            loss.backward()  # calculate gradients

            for param in model.parameters():
                param.accumulated_grads = (
                    []
                )  # container for accumulated microbatch gradients

            with th.no_grad():
                for param in model.parameters():
                    per_sample_grad = param.grad.clone()
                    th.nn.utils.clip_grad_norm_(
                        per_sample_grad, max_norm=MAX_GRAD_NORM
                    )  # clip them
                    param.accumulated_grads.append(per_sample_grad)

                for param in model.parameters():
                    param.grad = th.stack(
                        param.accumulated_grads, dim=0
                    )  # this is an identity operation cause the virtual batch size = real batch size

                for param in model.parameters():  # optimizer.step()
                    param.sub_(LR * param.grad)
                    param.add_(
                        th.normal(
                            0.0, NOISE_MULTIPLIER * MAX_GRAD_NORM, size=param.shape
                        )
                    )
                    param.grad = 0  # optimizer.zero_grad()

                total_optimisation_steps += 1

            # optimizer.step()
            losses.append(loss.get().item())

        sy.local_worker.clear_objects()
        epsilon, best_alpha = get_privacy_spent(
            target_delta=TARGET_DELTA,
            steps=total_optimisation_steps,
            alphas=ALPHAS,
            noise_multiplier=NOISE_MULTIPLIER,
        )
        print(
            f"[{worker.id}]\t"
            f"Train Epoch: {epoch} \t"
            f"Loss: {sum(losses)/len(losses):.4f} "
            f"(ε = {epsilon:.2f}, δ = {TARGET_DELTA}) for α = {best_alpha}"
        )

    # 3. Federated aggregation of the updated models
    # federated_aggregation(local_model, models)
    local_model = aggregation(local_model, models, workers, crypto)

