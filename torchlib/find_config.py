import optuna as opt
from argparse import Namespace
import sys, os.path

sys.path.insert(0, os.path.split(sys.path[0])[0])

from train import main

global cmdln_args


def objective(trial: opt.trial):
    lr = trial.suggest_loguniform("lr", 1e-5, 1e-3,)
    repetitions_dataset = (
        trial.suggest_int("repetitions_dataset", 1, 2) if cmdln_args.federated else 1
    )
    epochs = 25
    if cmdln_args.federated:
        epochs = int(epochs // repetitions_dataset)
    args = Namespace(
        config="optuna",
        train_federated=cmdln_args.federated,
        data_dir="data/server_simulation" if cmdln_args.federated else "data/train",
        visdom=False,
        encrypted_inference=False,
        cuda=not cmdln_args.federated,
        websockets=False,
        batch_size=200,
        train_resolution=224,
        inference_resolution=224,
        test_batch_size=10,
        test_interval=1,
        validation_split=10,
        epochs=epochs,
        lr=lr,
        end_lr=trial.suggest_loguniform("end_lr", 1e-6, lr),
        restarts=trial.suggest_int("restarts", 0, 1),
        beta1=trial.suggest_float("beta1", 0.25, 0.95),
        beta2=trial.suggest_float("beta2", 0.9, 1.0),
        ## zero not possible but loguniform makes most sense
        weight_decay=trial.suggest_loguniform("weight_decay", 1e-12, 1e-3),
        seed=1,
        log_interval=10,
        optimizer="Adam",
        model="resnet-18",
        pretrained=True,
        weight_classes=trial.suggest_categorical("weight_classes", [True, False]),
        pooling_type="max",
        rotation=trial.suggest_int("rotation", 0, 45),
        translate=0.0,  # trial.suggest_float("translate", 0, 0.2),
        scale=trial.suggest_float("scale", 0.0, 0.5),
        shear=trial.suggest_int("shear", 0, 45),
        noise_std=trial.suggest_float("noise_std", 0.0, 0.15),
        noise_prob=trial.suggest_float("noise_prob", 0.0, 1.0),
        mixup=trial.suggest_categorical("mixup", [True, False]),
        repetitions_dataset=repetitions_dataset,
    )
    apply_albu = trial.suggest_categorical("apply albu transforms", [True, False])
    args.albu_prob = trial.suggest_float("albu_prob", 0.0, 1.0) if apply_albu else 0.0
    args.individual_albu_probs = (
        trial.suggest_float("individual_albu_probs", 0.0, 1.0) if apply_albu else 0.0
    )
    args.clahe = (
        trial.suggest_categorical("clahe", [True, False]) if apply_albu else False
    )
    args.randomgamma = (
        trial.suggest_categorical("randomgamma", [True, False]) if apply_albu else False
    )
    args.randombrightness = (
        trial.suggest_categorical("randombrightness", [True, False])
        if apply_albu
        else False
    )
    args.blur = (
        trial.suggest_categorical("blur", [True, False]) if apply_albu else False
    )
    args.elastic = (
        trial.suggest_categorical("elastic", [True, False]) if apply_albu else False
    )
    args.optical_distortion = (
        trial.suggest_categorical("optical_distortion", [True, False])
        if apply_albu
        else False
    )
    args.grid_distortion = (
        trial.suggest_categorical("grid_distortion", [True, False])
        if apply_albu
        else False
    )
    args.grid_shuffle = (
        trial.suggest_categorical("grid_shuffle", [True, False])
        if apply_albu
        else False
    )
    args.hsv = trial.suggest_categorical("hsv", [True, False]) if apply_albu else False
    args.invert = (
        trial.suggest_categorical("invert", [True, False]) if apply_albu else False
    )
    args.cutout = (
        trial.suggest_categorical("cutout", [True, False]) if apply_albu else False
    )
    args.shadow = (
        trial.suggest_categorical("shadow", [True, False]) if apply_albu else False
    )
    args.fog = trial.suggest_categorical("fog", [True, False]) if apply_albu else False
    args.sun_flare = (
        trial.suggest_categorical("sun_flare", [True, False]) if apply_albu else False
    )
    args.solarize = (
        trial.suggest_categorical("solarize", [True, False]) if apply_albu else False
    )
    args.equalize = (
        trial.suggest_categorical("equalize", [True, False]) if apply_albu else False
    )
    args.grid_dropout = (
        trial.suggest_categorical("grid_dropout", [True, False])
        if apply_albu
        else False
    )
    if args.mixup:  # pylint:disable=no-member
        args.mixup_lambda = trial.suggest_categorical(
            "mixup_lambda",
            (0.1, 0.25, 0.49999, None),  # 0.5 breaks federated weight calculation
        )
        args.mixup_prob = trial.suggest_float("mixup_prob", 0.0, 1.0)
    if cmdln_args.federated:
        args.unencrypted_aggregation = False
        args.sync_every_n_batch = trial.suggest_int("sigma", 1, 5)
        args.wait_interval = 0.1
        args.keep_optim_dict = trial.suggest_categorical(
            "keep_optim_dict", [True, False]
        )
        args.weighted_averaging = trial.suggest_categorical(
            "weighted_averaging", [True, False]
        )
    try:
        best_val_acc = main(args, verbose=False, optuna_trial=trial)
    except Exception as e:
        print(str(args))
        print(str(e))
        exit()
    return best_val_acc


if __name__ == "__main__":
    from argparse import ArgumentParser

    parser = ArgumentParser()
    parser.add_argument(
        "--federated", action="store_true", help="Search on federated setting"
    )
    parser.add_argument(
        "--num_trials", default=30, type=int, help="how many trials to perform"
    )
    cmdln_args = parser.parse_args()
    study = opt.create_study(
        study_name="federated_pneumonia"
        if cmdln_args.federated
        else "vanilla_pneumonia",
        storage="sqlite:///model_weights/pneumonia_search.db",
        load_if_exists=True,
        direction="maximize",
        # pruner=opt.pruners.MedianPruner(n_startup_trials=10, n_warmup_steps=10),
    )
    study.optimize(objective, n_trials=cmdln_args.num_trials)
