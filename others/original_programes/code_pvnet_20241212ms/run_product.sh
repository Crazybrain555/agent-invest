#!/usr/bin/sh
export PATH=/usr/local/python311/bin/:$PATH

#-- prepare data
python -B /data/lic/pvnet/prepare_data.py 

#-- generate alpha: nn
python -B /data/lic/pvnet/main_nn.py --dataset=day1 --model=agru
python -B /data/lic/pvnet/main_nn.py --dataset=day2 --model=agru
python -B /data/lic/pvnet/main_nn.py --dataset=week1 --model=agru
python -B /data/lic/pvnet/main_nn.py --dataset=week2 --model=agru

python -B /data/lic/pvnet/main_nn.py --dataset=day1 --model=bigru
python -B /data/lic/pvnet/main_nn.py --dataset=day2 --model=bigru
python -B /data/lic/pvnet/main_nn.py --dataset=week1 --model=bigru
python -B /data/lic/pvnet/main_nn.py --dataset=week2 --model=bigru

rsync -av /data/lic/pvnet/output/alpha_nn_all/ /home/space_weizk/alpha_lic_pvnet/

#-- generate alpha: dt
python -B /data/lic/pvnet/main_dt.py --dataset=alpha --model=lgb
python -B /data/lic/pvnet/main_dt.py --dataset=alpha --model=xgb
rsync -av /data/lic/pvnet/output/alpha_dt_all/ /home/space_weizk/alpha_lic_pvnet/

#-- generate alpha: nndt(merge)
python -B /data/lic/pvnet/merge_alpha.py
rsync -av /data/lic/pvnet/output/alpha_nndt_all/ /home/space_weizk/alpha_lic_pvnet/

#-- log_result
rsync -av /data/lic/pvnet/output/model_nn_all/*.csv /home/space_weizk/alpha_lic_pvnet/log_result/
rsync -av /data/lic/pvnet/output/model_dt_all/*.csv /home/space_weizk/alpha_lic_pvnet/log_result/

