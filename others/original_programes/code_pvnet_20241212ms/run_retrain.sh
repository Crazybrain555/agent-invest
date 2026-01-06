#!/usr/bin/sh
export PATH=/usr/local/python311/bin/:$PATH

#-- prepare data
year=$(( $(date +%Y) - 1 ))
echo $year
python -B /data/lic/pvnet/prepare_data.py --retrain=$year

#-- train model
datasets=("week1" "week2" "day1" "day2")
num=${#datasets[@]}
for ((i=0;i<=$num-1;i++)) ; do
  dataset=${datasets[$i]}
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=0 --dataset=$dataset --model=agru &
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=1 --dataset=$dataset --model=agru &
  wait
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=2 --dataset=$dataset --model=agru &
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=3 --dataset=$dataset --model=agru &
  wait
done

for ((i=0;i<=$num-1;i++)) ; do
  dataset=${datasets[$i]}
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=0 --dataset=$dataset --model=bigru &
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=1 --dataset=$dataset --model=bigru &
  wait
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=2 --dataset=$dataset --model=bigru &
  python -B /data/lic/pvnet/main_nn.py --retrain=$year --seed=3 --dataset=$dataset --model=bigru &
  wait
done
