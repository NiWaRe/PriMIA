import os

os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
import torch

#
import configparser
import argparse
import albumentations as a
from torchvision import datasets, transforms, models
from argparse import Namespace
from tqdm import tqdm
from sklearn import metrics as mt
from numpy import newaxis
from random import seed as rseed
from torchlib.utils import stats_table, Arguments  # pylint:disable=import-error
from torchlib.models import vgg16, resnet18, conv_at_resolution, getMoNet
from torchlib.dataloader import (
    AlbumentationsTorchTransform,
    CombinedLoader,
    MSD_data_images,
)
from warnings import warn
import segmentation_models_pytorch as smp
import pickle
import numpy as np

from revision_scripts.module_modification import (
    convert_batchnorm_modules,
    _batchnorm_to_bn_without_stats,
)

from torchlib.plot_stuff import plot_imgs

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data_dir",
        type=str,
        required=True,
        default="data/server_simulation/test",
        help='Select a data folder [if matches "mnist" mnist will be used].',
    )
    parser.add_argument(
        "--model_weights",
        type=str,
        required=True,
        default=None,
        help="model weights to use",
    )
    parser.add_argument("--cuda", action="store_true", help="Use CUDA acceleration.")
    parser.add_argument(
        "--segmentation", action="store_true", help="Evaluate segmentation model"
    )
    cmd_args = parser.parse_args()

    use_cuda = cmd_args.cuda and torch.cuda.is_available()

    device = torch.device("cuda" if use_cuda else "cpu")  # pylint: disable=no-member
    state = torch.load(cmd_args.model_weights, map_location=device)

    args = state["args"]
    if type(args) is Namespace:
        args = Arguments.from_namespace(args)
    args.from_previous_checkpoint(cmd_args)
    print(str(args))
    rseed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    kwargs = {"num_workers": 1, "pin_memory": True} if use_cuda else {}
    class_names = None
    if "val_mean_std" not in state.keys():
        warn("mean and std on which model is trained are unknown", category=UserWarning)
    val_mean_std = (
        state["val_mean_std"]
        if "val_mean_std" in state.keys()
        else (
            torch.tensor([0.5]),  # pylint:disable=not-callable
            torch.tensor([0.2]),  # pylint:disable=not-callable
        )
        if args.pretrained
        else (
            torch.tensor([0.5, 0.5, 0.5]),  # pylint:disable=not-callable
            torch.tensor([0.2, 0.2, 0.2]),  # pylint:disable=not-callable
        )
    )
    mean, std = val_mean_std
    mean = mean.cpu()
    std = std.cpu()
    if args.data_dir == "mnist":
        num_classes = 10
        testset = datasets.MNIST(
            "../data",
            train=False,
            transform=transforms.Compose(
                [
                    transforms.Resize(args.inference_resolution),
                    transforms.ToTensor(),
                    transforms.Normalize(mean, std),
                ]
            ),
        )
    elif cmd_args.segmentation:
        basic_tfs = [
            a.Resize(args.inference_resolution, args.inference_resolution,),
            a.RandomCrop(args.train_resolution, args.train_resolution),
            a.ToFloat(max_value=255.0),
        ]
        val_trans = a.Compose(
            [
                *basic_tfs,
                a.Normalize(mean, std, max_pixel_value=1.0),
                a.Lambda(
                    image=lambda x, **kwargs: x.reshape(
                        # add extra channel to be compatible with nn.Conv2D
                        -1,
                        args.train_resolution,
                        args.train_resolution,
                    ),
                    mask=lambda x, **kwargs: np.where(
                        # binarize masks
                        x.reshape(-1, args.train_resolution, args.train_resolution)
                        / 255.0
                        > 0.5,
                        np.ones_like(x),
                        np.zeros_like(x),
                    ).astype(np.float32),
                ),
            ]
        )
        testset = MSD_data_images(
            args.data_dir + "/test", transform=AlbumentationsTorchTransform(val_trans),
        )
    else:
        num_classes = 3

        tf = [
            a.Resize(args.inference_resolution, args.inference_resolution),
            a.CenterCrop(args.inference_resolution, args.inference_resolution),
        ]
        if hasattr(args, "clahe") and args.clahe:
            tf.append(a.CLAHE(always_apply=True, clip_limit=(1, 1)))
        tf.extend(
            [
                a.ToFloat(max_value=255.0),
                a.Normalize(
                    mean.cpu().numpy()[None, None, :],
                    std.cpu().numpy()[None, None, :],
                    max_pixel_value=1.0,
                ),
            ]
        )
        tf = AlbumentationsTorchTransform(a.Compose(tf))
        # transforms.Lambda(lambda x: x.permute(2, 0, 1)),

        loader = CombinedLoader()
        if not args.pretrained:
            loader.change_channels(1)
        testset = datasets.ImageFolder(cmd_args.data_dir, transform=tf, loader=loader)
        assert (
            len(testset.classes) == 3
        ), "We can only handle data that has 3 classes: normal, bacterial and viral"
        class_names = testset.classes

    test_loader = torch.utils.data.DataLoader(
        testset, batch_size=1, shuffle=True, **kwargs
    )
    already_loaded = False
    if args.model == "vgg16":
        model_type = vgg16
        model_args = {
            "pretrained": args.pretrained,
            "num_classes": num_classes,
            "in_channels": 1 if args.data_dir == "mnist" or not args.pretrained else 3,
            "adptpool": False,
            "input_size": args.inference_resolution,
            "pooling": args.pooling_type,
        }
    elif args.model == "simpleconv":
        if args.pretrained:
            warn("No pretrained version available")

        model_type = conv_at_resolution[args.train_resolution]
        model_args = {
            "num_classes": num_classes,
            "in_channels": 1 if args.data_dir == "mnist" or not args.pretrained else 3,
            "pooling": args.pooling_type,
        }
    elif args.model == "resnet-18":
        model_type = resnet18
        model_args = {
            "pretrained": args.pretrained,
            "num_classes": num_classes,
            "in_channels": 1 if args.data_dir == "mnist" or not args.pretrained else 3,
            "adptpool": False,
            "input_size": args.inference_resolution,
            "pooling": args.pooling_type,
        }
    elif args.model == "unet":
        if "vgg" in cmd_args.model_weights:
            encoder_name = "vgg11_bn"
        elif "mobilenet" in cmd_args.model_weights:
            encoder_name = "mobilenet_v2"
        else:
            encoder_name = "resnet18"
        # because we don't call any function but directly create the model
        already_loaded = True
        # preprocessing step due to version problem (model was saved from torch 1.7.1)
        # resnet18 can be directly replaced by vgg11 and mobilenet
        model_args = {
            "encoder_name": encoder_name,
            "classes": 1,
            "in_channels": 1,
            "activation": "sigmoid",
            "encoder_weights": None,
        }
        model = smp.Unet(**model_args)
        # model.encoder.conv1 = nn.Sequential(nn.Conv2d(1, 3, 1), model.encoder.conv1)

    elif args.model == "MoNet":
        model_type = getMoNet
        model_args = {
            "pretrained": False,  # args.pretrained,
            "activation": "sigmoid",
        }
    else:
        raise ValueError(
            "Model name not understood. Please choose one of 'vgg16, 'simpleconv', resnet-18'."
        )

    if not already_loaded:
        model = model_type(**model_args)

    if args.differentially_private:
        model = convert_batchnorm_modules(
            model, converter=_batchnorm_to_bn_without_stats
        )

    model.load_state_dict(state["model_state_dict"])
    model.to(device)
    # test method
    model.eval()

    imgs = []
    total_pred, total_target, total_scores = [], [], []
    with torch.no_grad():
        for data, target in tqdm(
            test_loader,
            total=len(test_loader),
            desc="performing inference",
            leave=False,
        ):
            if len(data.shape) > 4:
                data = data.squeeze(0)
            data, target = data.to(device), target.to(device)
            output = model(data)
            imgs.append(data)
            total_scores.append(output)
            pred = output.argmax(dim=1)
            total_pred.append(pred)
            tgts = target.view_as(pred)
            total_target.append(tgts)
            equal = pred.eq(tgts)
    imgs = torch.cat(imgs).cpu().squeeze()
    total_pred = torch.cat(total_pred).cpu().squeeze()  # pylint: disable=no-member
    total_target = torch.cat(total_target).cpu().squeeze()  # pylint: disable=no-member
    total_scores = torch.cat(total_scores).cpu().squeeze()  # pylint: disable=no-member

    if cmd_args.segmentation:
        total_pred = torch.where(
            total_scores < 0.5,
            torch.zeros_like(total_scores),
            torch.ones_like(total_scores),
        )
        diceloss = smp.utils.losses.DiceLoss(eps=1e-7)
        iou = smp.utils.metrics.IoU()(total_pred, total_target)
        # fscore = smp.utils.metrics.Fscore()(total_pred, total_target)
        print(f"Dice Loss: {diceloss(total_scores, total_target)*100.0:.2f}%")
        print(f"Dice Score: {(1.0-diceloss(total_pred, total_target))*100.0:.2f}%")
        print(f"IoU: {iou*100.0:.2f}%")
        # print(f"Fscore: {fscore*100.0:.2f}%")
        plot_imgs(
            imgs[:10], total_target[:10], total_pred[:10]
        )  # , savefig="test.png")
        exit()
        total_target = total_target.flatten().numpy().astype(np.int)
        total_scores = total_scores.flatten().numpy()
        total_pred = total_pred.flatten().numpy()
    else:
        total_pred = total_pred.numpy()
        total_target = total_target.numpy()
        total_scores = total_scores.numpy()

        total_scores -= total_scores.min(axis=1)[:, newaxis]
        total_scores = total_scores / total_scores.sum(axis=1)[:, newaxis]

    roc_auc = mt.roc_auc_score(total_target, total_scores, multi_class="ovo")
    objective = 100.0 * roc_auc
    conf_matrix = mt.confusion_matrix(total_target, total_pred)
    report = mt.classification_report(
        total_target, total_pred, output_dict=True, zero_division=0
    )
    print(
        stats_table(
            conf_matrix,
            report,
            roc_auc=roc_auc,
            matthews_coeff=mt.matthews_corrcoef(total_target, total_pred),
            class_names=class_names,
            epoch=0,
        )
    )

