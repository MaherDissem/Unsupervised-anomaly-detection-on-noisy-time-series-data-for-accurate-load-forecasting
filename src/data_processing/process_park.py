import argparse
import os
import glob
import numpy as np
import pandas as pd

import sys
sys.path.append("./src") # TODO: fix this hack

from data_processing.synth_anomaly import SynthLoadAnomaly
from utils.utils import set_seed


def parse_args():
    parser = argparse.ArgumentParser(description="Prepare data for anomaly detection model training and evaluation.")
    parser.add_argument("--raw_data_root",        type=str,   default="dataset/raw/Park/Commercial/30_minutes", help="Path to raw data root")
    parser.add_argument("--trg_save_data",        type=str,   default="dataset/processed/Park/Commercial/30_minutes", help="Path to save processed data")
    parser.add_argument("--load_feature_name",    type=str,   default="Power (kW)", help="Name of the load feature")
    parser.add_argument("--date_feature_name",    type=str,   default="Time", help="Name of the date_time feature")
    parser.add_argument("--day_size",             type=int,   default=48, help="Size of a day")
    parser.add_argument("--n_days",               type=int,   default=1, help="Number of days")
    parser.add_argument("--day_stride",           type=int,   default=1, help="Day stride for sliding window")
    parser.add_argument("--contam_ratio",         type=float, default=0.1, help="Contamination ratio (percentage of days with anomalies))") # train data contam depends on window size (nbr of days in a window)
    parser.add_argument("--contam_clean_ratio",   type=float, default=0.8, help="Clean data save ratio (forcasting model is later evaluated on this clean data)")
    parser.add_argument("--ad_split_ratio",       type=float, default=0.7, help="Anomaly detection train-test split ratio")
    parser.add_argument("--seed",                 type=int,   default=0, help="Random seed")
    parser.add_argument("--log_file",             type=str,   default="results/results.txt", help="Path of file to log to") # quantiles must be saved to later scale back metrics
    args = parser.parse_args()
    return args


def run(args):
    set_seed(args.seed)
    load = pd.DataFrame()
    sampling_rate = os.path.basename(args.raw_data_root)
    building_type = os.path.basename(os.path.dirname(args.raw_data_root))
    data_root = os.path.dirname(os.path.dirname(args.raw_data_root))

    csv_paths = glob.iglob(os.path.join(data_root, '*', sampling_rate, f"*{building_type}", "*.xlsx"))

    for csv_path in csv_paths:
        try: 
            csv_file = pd.read_excel(csv_path)
            csv_file = csv_file[[args.date_feature_name, args.load_feature_name]]
            csv_file[args.date_feature_name] = pd.to_datetime(csv_file[args.date_feature_name], format="%Y-%m-%d %H:%M:%S")
            aonm_perc = csv_file[args.load_feature_name].isna().sum()/len(csv_file[args.load_feature_name])
            zeros_perc = (csv_file[args.load_feature_name] == 0).sum()/len(csv_file[args.load_feature_name])
            if aonm_perc > 0 or zeros_perc>0.05: continue
        except Exception as e:
            print(e)
        load = pd.concat([load, csv_file], axis=0)
    
    if len(load) == 0: raise Exception("No data found!")

    load.set_index(args.date_feature_name, inplace=True)
    load.sort_index(inplace=True)
    
    # remove duplicate indices
    load = load[~load.index.duplicated()]

    # replace missing values (if any) by the value of the previous week
    # idx = pd.date_range(load.index[0], load.index[-1], freq="30T")
    # load = load.reindex(idx, fill_value=np.nan)
    # load = load.fillna(load.shift(args.day_size*7))

    # split contam data into train and test sets for anomaly detection model
    N = int(args.contam_clean_ratio*len(load))//args.day_size*args.day_size
    M = int(args.ad_split_ratio*(len(load[:N])))//args.day_size*args.day_size
    contaminated_load = load[:N]
    clean_load = load[N:]
    ad_train_load = contaminated_load[:M]
    ad_test_load = contaminated_load[M:]

    # contaminate data with synthetic anomalies
    anomaly_generator = SynthLoadAnomaly()
    def contam_load(load, contam_ratio, feature_name, day_size):
        gt = []
        n_days = len(load)//day_size
        n_contam_days = int(contam_ratio*len(load)//day_size)
        contam_days = np.random.choice(range(0, len(load)//day_size), n_contam_days, replace=False)
        for i in range(n_days):
            seq_gt = [0]*day_size
            contam = i in contam_days
            if contam:
                day_st = i*day_size
                day_end = day_st + day_size
                sequence = load[feature_name].values[day_st: day_end]
                anomalous_sequence, anom_idx = anomaly_generator.inject_anomaly(sequence)
                load[feature_name].values[day_st: day_end] = anomalous_sequence
                for anom_id in anom_idx: seq_gt[anom_id] = 1 
                gt.extend(seq_gt)
            else:
                gt.extend(seq_gt)
        return load, gt

    def extract_consec_days(load, gt_load, day0, n_days, day_size):
        """return n_days consecutive days starting at day0 from load dataframe"""

        sequence, gt = [], []
        start = day0
        end = start + day_size

        for day in range(n_days):
            sequence.extend(load[args.load_feature_name].values[start: end])
            gt.extend(gt_load[start: end])
            start += day_size
            end += day_size
        return np.array(sequence), np.array(gt)

    def build_dataset(load, n_days, day_size, day_stride, contam_ratio, contam_data=True):
        """
            build a dataset from load dataframe using a sliding window of size n_days and stride of 1 day 
            while contamining the data with synthetic anomalies
        """
        if contam_data:
            load, gt_load = contam_load(load, contam_ratio, args.load_feature_name, day_size)
        else:
            gt_load = [[0]*day_size]*(len(load)//day_size)
        
        time_wind = []
        gt_time_wind = []
        datetime_wind = []

        day_idx = 0
        while day_idx < len(load)//day_size - n_days:
            day0 = day_idx*day_size
            sequence, gt = extract_consec_days(load, gt_load, day0, n_days, day_size)

            time_wind.append(sequence)
            gt_time_wind.append(gt)
            first_date = str(load.index[day0]).replace(':', '')
            last_date = str(load.index[day0 + n_days*day_size]).replace(':', '')
            datetime_wind.append(f"{first_date} - {last_date}")
            day_idx += day_stride

        return time_wind, gt_time_wind, datetime_wind


    ad_train_windows, gt_ad_train_windows, date_ad_train_windows = build_dataset(ad_train_load, args.n_days, args.day_size, args.day_stride, args.contam_ratio, contam_data=True)
    ad_test_windows, gt_ad_test_windows, date_ad_test_windows = build_dataset(ad_test_load, args.n_days, args.day_size, args.day_stride, args.contam_ratio, contam_data=True)

    day_contam_ratio = args.contam_ratio*1/args.n_days
    datapoint_contam_ratio = np.array(gt_ad_train_windows+gt_ad_test_windows).sum() / (len(gt_ad_train_windows+gt_ad_test_windows)*args.day_size)

    # normalize data
    min_quantile = 0.01
    max_quantile = 0.99

    min_q_val = clean_load.quantile(min_quantile).item()
    max_q_val = clean_load.quantile(max_quantile).item()

    def scale_windows(windows_list, min_q_val, max_q_val):
        scaled_windows = []
        for window in windows_list:
            window = (window - min_q_val) / (max_q_val - min_q_val)
            scaled_windows.append(window)
        return scaled_windows

    ad_train_windows = scale_windows(ad_train_windows, min_q_val, max_q_val)
    ad_test_windows = scale_windows(ad_test_windows, min_q_val, max_q_val)

    # save data
    # remove existing files in save target root folder
    existing_files = glob.glob(os.path.join(args.trg_save_data, "*", "*", "*.npy"))
    for f in existing_files:
        os.remove(f)

    # crete save target folders if they don't exist
    os.makedirs(os.path.join(args.trg_save_data, "lf_test_clean", "data"), exist_ok=True)
    os.makedirs(os.path.join(args.trg_save_data, "ad_train_contam", "data"), exist_ok=True)
    os.makedirs(os.path.join(args.trg_save_data, "ad_train_contam", "gt"), exist_ok=True)
    os.makedirs(os.path.join(args.trg_save_data, "ad_test_contam", "data"), exist_ok=True)
    os.makedirs(os.path.join(args.trg_save_data, "ad_test_contam", "gt"), exist_ok=True)

    # save contam ad train data
    for i, (sample, sample_gt, sample_date) in enumerate(zip(ad_train_windows, gt_ad_train_windows, date_ad_train_windows)):
        if np.isnan(sample).any(): continue
        np.save(os.path.join(args.trg_save_data, "ad_train_contam", "data", sample_date), sample)
        np.save(os.path.join(args.trg_save_data, "ad_train_contam", "gt", sample_date), sample_gt)

    # save contam ad test data
    for i, (sample, sample_gt, sample_date) in enumerate(zip(ad_test_windows, gt_ad_test_windows, date_ad_test_windows)):
        if np.isnan(sample).any(): continue
        np.save(os.path.join(args.trg_save_data, "ad_test_contam", "data", sample_date), sample)
        np.save(os.path.join(args.trg_save_data, "ad_test_contam", "gt", sample_date), sample_gt)

    # log results
    print(args, file=open(args.log_file, "a"))
    print(f"Number of ad_train_contam windows: {len(ad_train_windows)}", file=open(args.log_file, "a"))
    print(f"Number of ad_test_contam windows: {len(ad_test_windows)}", file=open(args.log_file, "a"))

    print(f"{day_contam_ratio*100:.2f}% of days are contaminated.", file=open(args.log_file, "a"))
    print(f"{datapoint_contam_ratio*100:.2f}% of datapoints are contaminated.", file=open(args.log_file, "a"))

    print(f"min_quantile={min_quantile:0.3f} -> value={min_q_val}", file=open(args.log_file, "a"))
    print(f"max_quantile={max_quantile:0.3f} -> value={max_q_val}", file=open(args.log_file, "a"))

    # save clean load for forecasting model evaluation
    clean_load = (clean_load - min_q_val) / (max_q_val - min_q_val)
    clean_load.rename_axis("date", inplace=True)
    clean_load.fillna(0, inplace=True) # will be detected as anomaly and imputed by AI model
    clean_load.to_csv(os.path.join(args.trg_save_data, "load_clean_lf_test.csv"))

    # save contaminated load serie to infer AD/AI models after training
    contam_full_load, gt_full_load = contam_load(load, args.contam_ratio, args.load_feature_name, args.day_size) # anomaly contamination is different than for AD model training
    scaled_load = (contam_full_load - min_q_val) / (max_q_val - min_q_val)
    scaled_load.rename_axis("date", inplace=True)
    scaled_load.fillna(0, inplace=True) # will be detected as anomaly and imputed by AI model
    scaled_load.to_csv(os.path.join(args.trg_save_data, "load_contam.csv"))
    gt_full_load = pd.Series(gt_full_load, index=scaled_load.index[:len(gt_full_load)]).rename("gt", inplace=True)
    pd.Series(gt_full_load).to_csv(os.path.join(args.trg_save_data, "load_contam_gt.csv"))
    print('Dataset ready!')


if __name__ == "__main__":
    args = parse_args()
    run(args)


# Dataset saved to dataset/processed/Park\Commercial\30_minutes
# percentage of nan values: 3.89%
# Dataset saved to dataset/processed/Park\Office\30_minutes
# percentage of nan values: 2.66%
# Dataset saved to dataset/processed/Park\Residential\30_minutes
# percentage of nan values: 8.56%
# Dataset saved to dataset/processed/Park\Public\30_minutes
# percentage of nan values: 31.96%
            
# Dataset saved to dataset/processed/Park\Commercial\1_hour
# percentage of nan values: 3.97%
# Dataset saved to dataset/processed/Park\Office\1_hour
# percentage of nan values: 2.64%
# Dataset saved to dataset/processed/Park\Residential\1_hour
# percentage of nan values: 8.57%
# Dataset saved to dataset/processed/Park\Public\1_hour
# percentage of nan values: 31.90%
