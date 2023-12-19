import sys
sys.path.append("./src") # TODO: fix this hack

import os
import torch
import matplotlib.pyplot as plt

from .dataset import AI_Dataset # .dataset if called from pipeline.py
from model import LSTM_AE
from utils.utils import set_seed

import argparse

def parse_args():
    parser = argparse.ArgumentParser(description="Define hyperparameters for training")
    parser.add_argument("--dataset_root",        type=str,   default="dataset/processed/AEMO/test/ai_train", help="Root directory of the dataset")
    parser.add_argument("--split_ratio",         type=float, default=0.8,                                    help="Ratio for train-test split")
    parser.add_argument("--seq_len",             type=int,   default=48*1,                                   help="Sequence length")
    parser.add_argument("--no_features",         type=int,   default=1,                                      help="Number of features")
    parser.add_argument("--embedding_dim",       type=int,   default=128,                                    help="Dimension of embedding")
    parser.add_argument("--learning_rate",       type=float, default=1e-3,                                   help="Learning rate for the optimizer")
    parser.add_argument("--every_epoch_print",   type=int,   default=1,                                      help="Print results every n epochs")
    parser.add_argument("--epochs",              type=int,   default=200,                                    help="Number of training epochs")
    parser.add_argument("--patience",            type=int,   default=20,                                     help="Patience for early stopping")
    parser.add_argument("--max_grad_norm",       type=float, default=0.05,                                   help="Maximum gradient norm for gradient clipping")
    parser.add_argument("--save_eval_plots",     type=bool,  default=True,                                   help="Save evaluation plots")
    parser.add_argument("--save_folder",         type=str,   default="results/ai_eval_plots",                help="Folder to save evaluation plots")
    parser.add_argument("--seed",                type=int,   default=0,                                      help="Random seed")
    return parser.parse_args()

def get_data_loaders(dataset_root, split_ratio, seq_len, no_features):
    dataset = AI_Dataset(dataset_root,
                         is_train=True,
                         len_mask=8) # train version
    train_dataset, test_dataset = torch.utils.data.random_split(dataset, [int(split_ratio * len(dataset)), len(dataset) - int(split_ratio * len(dataset))])
    train_dataloader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=32,
        shuffle=False,
        pin_memory=True,
    )
    test_dataloader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=1,
        shuffle=False,
        pin_memory=True,
    )
    return train_dataloader, test_dataloader

def train(args):
    train_dataloader, test_dataloader = get_data_loaders(args.dataset_root, args.split_ratio, args.seq_len, args.no_features)
    model = LSTM_AE(args.seq_len, args.no_features, args.embedding_dim, args.learning_rate, args.every_epoch_print, args.epochs, args.patience, args.max_grad_norm)
    loss_history = model.fit(train_dataloader)

    if args.save_eval_plots:
        plt.plot(loss_history)
        os.makedirs(args.save_folder, exist_ok=True)
        plt.savefig(os.path.join(args.save_folder, "loss_history.png"))
        plt.clf()

        del model
        loaded_model = LSTM_AE(args.seq_len, args.no_features, args.embedding_dim, args.learning_rate, args.every_epoch_print, args.epochs, args.patience, args.max_grad_norm)
        loaded_model.load()

        for i, batch in enumerate(test_dataloader): # batch_size=1
            ts = batch["masked_data"] # torch.Size([1, 48, 1])
            mask = batch["mask"]
            gt_ts = batch["clean_data"]

            model_out = loaded_model.infer(ts)
            model_out = model_out.squeeze(0).squeeze(-1).detach().cpu()
            filled_ts = ts.clone().squeeze(0).squeeze(-1).detach().cpu()
            mask = mask.squeeze(0).squeeze(-1).detach().cpu()
            filled_ts[mask==0] = model_out[mask==0]
            gt_ts = gt_ts.squeeze(0).squeeze(-1).detach().cpu()

            plt.plot(gt_ts, label="GT: ground truth")
            plt.plot(ts.squeeze(0).squeeze(-1), label="serie with missing values")
            plt.plot(model_out, label="autoencoder's output")
            plt.plot(filled_ts.squeeze(0).squeeze(-1), label="serie with filled values")
            plt.legend()
            # plt.show()
            plt.savefig(os.path.join(args.save_folder, f"{i}.png"))
            plt.clf()
            if i > 10: break


if __name__ == "__main__":
    args = parse_args()
    set_seed(args.seed)
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    train(args)
