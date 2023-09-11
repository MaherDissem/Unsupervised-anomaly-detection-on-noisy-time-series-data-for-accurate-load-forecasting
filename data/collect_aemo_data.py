import subprocess

for year in range(2000, 2021+1): 
    for month in ['01','02','03','04','05','06','07','08','09','10','11','12']:
        for location in ["NSW", "QLD", "VIC", "SA", "TAS"]:
            bash_command = f"curl https://aemo.com.au/aemo/data/nem/priceanddemand/PRICE_AND_DEMAND_{year}06_NSW1.csv > ./aemo_csv_data/{year}{month}{location}.csv"
            result = subprocess.run(bash_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                print("Command failed:")
                print(result.stderr)
            else:
                print(f"Downloaded {year}-{month}-{location}.csv")
