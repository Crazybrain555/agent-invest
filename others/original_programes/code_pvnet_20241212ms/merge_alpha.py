#-- pvnet/merge_alpha.py, lic, 20241212
from datetime import datetime, timedelta
from pandas.errors import PerformanceWarning
import pandas as pd
import numpy as np
import warnings
import os
import re
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=PerformanceWarning)
output_root = '/data/lic/pvnet/output/'

#-- util
def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)
    return None

def winsorize_and_std(x, quans=[0.001,0.995]):
    #-- scipy.stats.mstats.winsorize
    dat = x.copy()
    for t in range(3):
        bound = np.quantile(dat, quans)
        dat[dat<bound[0]] = bound[0]
        dat[dat>bound[1]] = bound[1]
        dat = (dat-np.mean(dat))/(np.std(dat)+1e-12)
        dat = dat.fillna(0)
    return dat

def merge_alpha_byseed_nn(univ_id='all', model_id='gru', dataset='day1', label_shift=10, feature_lag=30, seeds=[0,1,2,3]):
    output_dir = output_root+'alpha_nn_{}/alpha_{}_{}_p{}_f{}_{}'.format(univ_id, model_id, dataset, label_shift, feature_lag, ''.join([str(s) for s in seeds]))
    ensure_dir(output_dir)
    alpha_dir_pattern = output_root+'alpha_nn_{}/alpha_{}_{}_p{}_f{}_{}/'
    alpha_dir0 = alpha_dir_pattern.format(univ_id, model_id, dataset, label_shift, feature_lag, seeds[0])
    for f in os.listdir(alpha_dir0):
        alpha0 = pd.read_csv(alpha_dir0+f, header=None, names=['id','alpha0'])
        for i in range(1, len(seeds)):
            alpha_dir = alpha_dir_pattern.format(univ_id, model_id, dataset, label_shift, feature_lag, seeds[i])
            alpha = pd.read_csv(alpha_dir+f, header=None, names=['id','alpha'+str(i)])
            alpha0 = pd.merge(alpha0, alpha, on='id', how='inner')
        alpha0['alpha'] = 0
        for i in range(1, len(alpha0.columns)):
            col = alpha0.iloc[:,i]
            col = (col-col.mean())/(col.std()+1e-12)
            alpha0['alpha'] += col # rank()
        alpha0['alpha'] /= len(seeds)
        alpha0 = alpha0.loc[:,['id','alpha']]
        alpha0['id'] = ['{0:0>6}'.format(s) for s in alpha0['id']]
        # alpha0['alpha'] = (alpha0['alpha']-alpha0['alpha'].mean())/(alpha0['alpha'].std()+1e-12)
        # alpha0['alpha'] = winsorize_and_std(x=alpha0['alpha'])
        alpha0.to_csv(output_dir+'/'+f, index=0, header=None)
        print(f)
    return None

def merge_alpha_bydir(alpha_dirs, new_alpha_id, date_from=None, date_to=None):
    output_dir = output_root+'alpha_nndt_all/'+new_alpha_id+'/'
    ensure_dir(output_dir)
    alpha_dates = os.listdir(alpha_dirs[0])
    alpha_dates = list(filter(lambda s: re.search(r'\d{8}.csv', s), alpha_dates))
    alpha_dates = sorted([int(s[(-12):(-4)]) for s in alpha_dates])
    if date_from is not None: # ignore date_to
        alpha_dates = sorted([d for d in alpha_dates if d>=date_from])
    alpha_names = [os.path.basename(os.path.normpath(s)) for s in alpha_dirs]
    for alpha_date in alpha_dates:
        cache = pd.DataFrame()
        for i in range(len(alpha_names)):
            dat = pd.read_csv(alpha_dirs[i]+alpha_names[i]+'.'+str(alpha_date)+'.csv', header=None, names=['id','alpha'])
            dat['alpha'] = (dat['alpha']-dat['alpha'].mean())/(dat['alpha'].std()+1e-12)
            dat['model'] = alpha_names[i]
            cache = cache.append(dat)
        dat = cache.pivot_table(index='id', columns='model', values='alpha').fillna(0)
        dat['alpha'] = dat.mean(axis=1)
        dat = dat.reset_index()
        dat[['id','alpha']].to_csv(output_dir+new_alpha_id+'.'+str(alpha_date)+'.csv', header=None, index=0)
        print(alpha_date)
    return None

#-- declaration
alpha_dirs_agruxgb_week1=[
  '/data/lic/pvnet/output/alpha_nn_all/alpha_agru_week1_p10_f30_0123/',
  '/data/lic/pvnet/output/alpha_dt_all/alpha_xgb_alpha_p5_r1440/'
]
alpha_dirs_agrulgb_week1=[
  '/data/lic/pvnet/output/alpha_nn_all/alpha_agru_week1_p10_f30_0123/',
  '/data/lic/pvnet/output/alpha_dt_all/alpha_lgb_alpha_p5_r1440/'
]
alpha_dirs_agruxgb_day1=[
  '/data/lic/pvnet/output/alpha_nn_all/alpha_agru_day1_p10_f30_0123/',
  '/data/lic/pvnet/output/alpha_dt_all/alpha_xgb_alpha_p5_r1440/'
]
alpha_dirs_agrulgb_day1=[
  '/data/lic/pvnet/output/alpha_nn_all/alpha_agru_day1_p10_f30_0123/',
  '/data/lic/pvnet/output/alpha_dt_all/alpha_lgb_alpha_p5_r1440/'
]

#-- launcher: backtest
# merge_alpha_bydir(alpha_dirs=alpha_dirs_agruxgb_week1, new_alpha_id='alpha_agruxgb_week1')
# merge_alpha_bydir(alpha_dirs=alpha_dirs_agrulgb_week1, new_alpha_id='alpha_agrulgb_week1')
# merge_alpha_bydir(alpha_dirs=alpha_dirs_agruxgb_day1, new_alpha_id='alpha_agruxgb_day1')
# merge_alpha_bydir(alpha_dirs=alpha_dirs_agrulgb_day1, new_alpha_id='alpha_agrulgb_day1')
# exit()

#-- launcher: product
today = datetime.now()
date_from = int((today+timedelta(days=-11)).strftime('%Y%m%d'))
date_to = int((today+timedelta(days=-1)).strftime('%Y%m%d'))
print(date_from, date_to)
merge_alpha_bydir(alpha_dirs=alpha_dirs_agruxgb_week1, new_alpha_id='alpha_agruxgb_week1', date_from=date_from, date_to=date_to)
merge_alpha_bydir(alpha_dirs=alpha_dirs_agrulgb_week1, new_alpha_id='alpha_agrulgb_week1', date_from=date_from, date_to=date_to)
merge_alpha_bydir(alpha_dirs=alpha_dirs_agruxgb_day1, new_alpha_id='alpha_agruxgb_day1', date_from=date_from, date_to=date_to)
merge_alpha_bydir(alpha_dirs=alpha_dirs_agrulgb_day1, new_alpha_id='alpha_agrulgb_day1', date_from=date_from, date_to=date_to)

