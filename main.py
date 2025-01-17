from __future__ import division, print_function

import argparse
import os.path as osp
import random
import time

import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.nn.utils import clip_grad_norm_

# from qm9_dataset import QM9
from torch_geometric.datasets import QM9

# from torch_geometric.data import DataLoader
from torch_geometric.loader import DataLoader
from warmup_scheduler import GradualWarmupScheduler

import wandb
from model import Config, MXMNet
from utils import EMA, load_ckp, save_ckp

parser = argparse.ArgumentParser()
parser.add_argument("--wandb", type=bool, default=False, help="Flag for W&B.")
parser.add_argument("--gpu", type=int, default=0, help="GPU number.")
parser.add_argument("--seed", type=int, default=920, help="Random seed.")
parser.add_argument(
    "--epochs", type=int, default=900, help="Number of epochs to train."
)
parser.add_argument("--lr", type=float, default=1e-4, help="Initial learning rate.")
parser.add_argument(
    "--scheduler", type=str, default="ExponentialLR", help="Scheduler Type"
)
parser.add_argument(
    "--ea_gamma", type=int, default=0.9961697, help="Exponential rate in ExponentialLR"
)
parser.add_argument(
    "--one_cycle_lr_total_steps",
    type=int,
    default=1000,
    help="Total steps in OneCycleLR",
)
parser.add_argument("--wd", type=float, default=0, help="Weight decay value.")
parser.add_argument("--n_layer", type=int, default=6, help="Number of hidden layers.")
parser.add_argument("--dim", type=int, default=128, help="Size of input hidden units.")
parser.add_argument("--dataset", type=str, default="QM9", help="Dataset")
parser.add_argument("--batch_size", type=int, default=128, help="Batch size")
parser.add_argument(
    "--target", type=int, default="7", help="Index of target (0~11) for prediction"
)
parser.add_argument(
    "--cutoff", type=float, default=5.0, help="Distance cutoff used in the global layer"
)
# parser.add_argument('--pooling', type=str, default='sum', help='Type of pooling to be used for graph embedding')
parser.add_argument("--dagnn", type=bool, default=False, help="Flag to enable DAGNN")
parser.add_argument(
    "--virtual_node", type=bool, default=False, help="Flag to use Virtual Node Module"
)
parser.add_argument(
    "--auxiliary_layer", type=bool, default=False, help="Flag to use Auxiliary Layer"
)
parser.add_argument(
    "--checkpoint_dir", type=str, default="checkpoint", help="Checkpoint directory"
)
parser.add_argument("--checkpoint_path", type=str, default=None, help="Checkpoint path")

args = parser.parse_args()
if args.wandb:
    wandb.init(config=args)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
if torch.cuda.is_available():
    torch.cuda.set_device(args.gpu)


def set_seed(seed):
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    np.random.seed(seed)
    random.seed(seed)


target = args.target
if target in [7, 8, 9, 10]:
    target = target + 5
set_seed(args.seed)

targets = [
    "mu (D)",
    "a (a^3_0)",
    "e_HOMO (eV)",
    "e_LUMO (eV)",
    "delta e (eV)",
    "R^2 (a^2_0)",
    "ZPVE (eV)",
    "U_0 (eV)",
    "U (eV)",
    "H (eV)",
    "G (eV)",
    "c_v (cal/mol.K)",
]


def test(loader):
    error = 0
    ema.assign(model)

    for data in loader:
        data = data.to(device)
        output = model(data)
        error += (output - data.y).abs().sum().item()
    ema.resume(model)
    return error / len(loader.dataset)


class MyTransform(object):
    def __call__(self, data):
        data.y = data.y[:, target]
        return data


# Download and preprocess dataset
path = osp.join(osp.dirname(osp.realpath(__file__)), ".", "data", "QM9")
dataset = QM9(path, transform=MyTransform()).shuffle()
print("# of graphs:", len(dataset))

# Split dataset
train_dataset = dataset[:110000]
val_dataset = dataset[110000:120000]
test_dataset = dataset[120000:]

# Load dataset
train_loader = DataLoader(
    train_dataset, batch_size=args.batch_size, shuffle=True, worker_init_fn=args.seed
)
test_loader = DataLoader(test_dataset, batch_size=args.batch_size, shuffle=False)
val_loader = DataLoader(val_dataset, batch_size=args.batch_size, shuffle=False)

print("Loaded the QM9 dataset. Target property: ", targets[args.target])

# Load model
config = Config(
    dim=args.dim,
    n_layer=args.n_layer,
    cutoff=args.cutoff,
    virtual_node=args.virtual_node,
    auxiliary_layer=args.auxiliary_layer,
    dagnn=args.dagnn,
)
model = MXMNet(config)
model = model.to(device)
if args.wandb:
    wandb.watch(model, log_freq=100)
print("Loaded the MXMNet.")

optimizer = optim.Adam(
    model.parameters(), lr=args.lr, weight_decay=args.wd, amsgrad=False
)
if args.scheduler == "MultiStepLR":
    after_scheduler = torch.optim.lr_scheduler.MultiStepLR(
        optimizer, milestones=[15, 30, 60], gamma=0.2
    )
    scheduler = GradualWarmupScheduler(
        optimizer, multiplier=1.0, total_epoch=1, after_scheduler=after_scheduler
    )
elif args.scheduler == "OneCycleLR":
    scheduler = torch.optim.lr_scheduler.OneCycleLR(
        optimizer=optimizer, max_lr=args.lr, total_steps=args.one_cycle_lr_total_steps
    )
else:
    after_scheduler = torch.optim.lr_scheduler.ExponentialLR(
        optimizer, gamma=args.ea_gamma
    )
    scheduler = GradualWarmupScheduler(
        optimizer, multiplier=1.0, total_epoch=1, after_scheduler=after_scheduler
    )

start_epoch = 0
if args.checkpoint_path:
    model, optimizer, start_epoch, valid_loss_min, scheduler = load_ckp(
        args.checkpoint_path, model, optimizer, scheduler
    )
    print("optimizer = ", optimizer)
    print("start_epoch = ", start_epoch)
    print("valid_loss_min = {:.6f}".format(valid_loss_min))


ema = EMA(model, decay=0.999)

print("================================================================================")
print("                                Start training:")
print("================================================================================")

best_epoch = None
best_val_loss = None

for epoch in range(start_epoch, args.epochs):
    loss_all = 0
    step = 0
    model.train()

    for data in train_loader:
        data = data.to(device)

        optimizer.zero_grad()

        output = model(data)
        loss = F.l1_loss(output, data.y)
        loss_all += loss.item() * data.num_graphs
        loss.backward()
        clip_grad_norm_(model.parameters(), max_norm=1000, norm_type=2)
        optimizer.step()

        curr_epoch = epoch + float(step) / (len(train_dataset) / args.batch_size)
        scheduler.step(curr_epoch)
        ema(model)
        step += 1

    train_loss = loss_all / len(train_loader.dataset)
    val_loss = test(val_loader)
    if args.wandb:
        wandb.log({"train_loss": loss})
        wandb.log({"val_mae": val_loss})
    checkpoint = {
        "epoch": epoch + 1,
        "valid_loss_min": val_loss,
        "state_dict": model.state_dict(),
        "optimizer": optimizer.state_dict(),
        "scheduler": scheduler.state_dict(),
    }
    is_best = False
    if best_val_loss is None or val_loss <= best_val_loss:
        test_loss = test(test_loader)
        if args.wandb:
            wandb.log({"test_mae": test_loss})
        best_epoch = epoch
        best_val_loss = val_loss
        is_best = True
    if (epoch + 1) % 5 == 0:
        checkpoint_path = f"{args.checkpoint_dir}/target-{args.target}-{epoch+1}-train-{train_loss:.3f}-val-{val_loss:.3f}.cpt"
        best_model_path = f"{args.checkpoint_dir}/target-{args.target}-best-epoch.cpt"
        save_ckp(checkpoint, is_best, checkpoint_path, best_model_path)
    print(
        "Epoch: {:03d}, LR: {:.07f}, LLR: {:0.07f}, Train MAE: {:.7f}, Validation MAE: {:.7f}, "
        "Test MAE: {:.7f}".format(
            epoch + 1,
            optimizer.param_groups[0]["lr"],
            scheduler.get_last_lr()[0],
            train_loss,
            val_loss,
            test_loss,
        )
    )

print(
    "==================================================================================="
)
print("Best Epoch:", best_epoch)
print("Best Test MAE:", test_loss)
