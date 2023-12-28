import argparse
import os
import glob
import numpy as np
import pandas as pd

import sys
sys.path.append("./src")

from utils.utils import set_seed

def parse_args():
    parser = argparse.ArgumentParser(description="Prepare data for anomaly detection model training and evaluation.")
    parser.add_argument("--raw_data_csv",         type=str,   default="dataset/processed/AEMO/NSW/load_cleaned.csv", help="Path to raw data root")
    parser.add_argument("--trg_save_data",        type=str,   default="dataset/processed/AEMO/NSW/lf_cleaned", help="Path to save processed data")
    
    parser.add_argument("--feat_feature_name",    type=str,   default="TOTALDEMAND", help="Name of the feat feature")
    parser.add_argument("--date_feature_name",    type=str,   default="date", help="Name of the date_time feature")

    parser.add_argument("--day_size",             type=int,   default=48, help="Size of a day")
    parser.add_argument("--n_days",               type=int,   default=3, help="Number of days")
    parser.add_argument("--day_stride",           type=int,   default=1, help="Day stride for sliding window")

    parser.add_argument("--seed",                 type=int,   default=0, help="Random seed")
    parser.add_argument("--log_file",             type=str,   default="results/results.txt", help="Path of file to log to") # quantiles must be saved to later scale back metrics
    args = parser.parse_args()
    return args


def run(args):
    set_seed(args.seed)
    data = pd.read_csv(args.raw_data_csv)

    def extract_consec_days(feat, day0, n_days, day_size):
        """return n_days consecutive days starting at day0 from feat dataframe"""

        sequence, gt = [], []
        start = day0
        end = start + day_size

        for day in range(n_days):
            sequence.extend(feat[start: end])
            start += day_size
            end += day_size
        return np.array(sequence), np.array(gt)

    def build_dataset(data, n_days, day_size, day_stride):
        """
            build a dataset from feat dataframe using a sliding window of size n_days and stride of 1 day 
            while contamining the data with synthetic anomalies
        """        
        time_wind = []
        gt_time_wind = []
        feat = data[args.feat_feature_name].values

        day_idx = 0
        while day_idx < len(feat)//day_size - n_days:
            day0 = day_idx*day_size
            sequence, gt = extract_consec_days(feat, day0, n_days, day_size)

            time_wind.append(sequence)
            gt_time_wind.append(gt)
            day_idx += day_stride

        return time_wind, gt_time_wind


    windows, gt_windows = build_dataset(data, args.n_days, args.day_size, args.day_stride)

    # save data
    # remove existing files in save target root folder
    existing_files = glob.glob(os.path.join(args.trg_save_data, "*", "*.npy"))
    for f in existing_files:
        os.remove(f)

    # crete save target folders if they don't exist
    os.makedirs(os.path.join(args.trg_save_data, "data"), exist_ok=True)
    # os.makedirs(os.path.join(args.trg_save_data, "gt"), exist_ok=True)

    # save data
    for i, (sample, sample_gt) in enumerate(zip(windows, gt_windows)):
        if np.isnan(sample).any(): continue
        np.save(os.path.join(args.trg_save_data, "data", f"{i}.npy"), sample)
        # np.save(os.path.join(args.trg_save_data, "gt", f"{i}.npy"), sample_gt)

    # log results
    print(args, file=open(args.log_file, "a"))
    print(f"Number of train windows: {len(windows)}", file=open(args.log_file, "a"))


if __name__ == "__main__":
    args = parse_args()
    run(args)
    print("Done!")
