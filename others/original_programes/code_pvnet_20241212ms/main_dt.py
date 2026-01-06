#-- pvnet/main_dt.py, lic, 20241212
from datetime import datetime, timedelta
from scipy.stats import spearmanr
from pandas.errors import PerformanceWarning
import numpy as np
import pandas as pd
import lightgbm as lgb
import xgboost as xgb
import os
import re
import time
import pyreadr
import argparse
import warnings
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=PerformanceWarning)

#-- declaration
qdata_dir = '/home/data/q/'
theme_dir = '/home/data/iqdata/alpha/CN/dyn_broad_zz800_cne5citics_size_beta/'
theme_set = {
    'alpha': ['analyst','growth','momentum','quality','reversal','size','value1','value2'],
    'allsub1y': ['analyst_drev','analyst_drec','analyst_coverage','analyst_rec','bigdata_pa','growth_s','growth_l','growth_f',\
                'momentum_f','momentum_t','quality_cashflow','quality_profit_a','quality_safety',\
                'reversal_vol','reversal_price','reversal_value','reversal_liq','size','smartholding',\
                'value_dvd','value_pb','value_pe','value_cf','value_peg']
}
output_root = '/data/lic/pvnet/output/'

#-- param 
parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--device', type=str, default='')
parser.add_argument('--univ', type=str, default='all')
parser.add_argument('--model', type=str, default='lgb')
parser.add_argument('--dataset', type=str, default='alpha')
args = parser.parse_args()
print(args)

#-- util
def load_rdata(root, uri, suffix=None, near=True):
    elements = uri.split('/')
    filename = elements[-1]
    if suffix is None:
        path = root+'/'+uri+'/'+filename+'.rdata'
    else:
        path = root+'/'+uri+'/'+filename+'.'+str(suffix)+'.rdata'
    if not os.path.exists(path):
        return None
    rdata = pd.DataFrame(list(pyreadr.read_r(path).values())[0])
    return rdata

def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)
    return None

def split_sample_dates(dates, train_ratio=0.9):
    if train_ratio>=1:
        return dates, None
    idx = round(len(dates)*train_ratio)
    train_dates = dates[:idx]
    valid_dates = dates[idx:]
    return train_dates, valid_dates

def get_datemap(date_from, date_to, freq):
    rbldates = []
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    #-- pick rbldates
    if freq=='all' or freq=='day':
        rbldates = sorted(dates['tdate'])[1:]
    elif freq=='month':
        rbldates = dates.loc[dates.index[dates['month_end']==1]+1, 'tdate']
    elif freq=='monthend':
        rbldates = dates.loc[dates['month_end']==1, 'tdate']
    elif freq=='quarter':
        rbldates = dates.loc[dates.index[dates['quarter_end']==1]+1, 'tdate']
    elif freq=='quarterend':
        rbldates = dates.loc[dates['quarter_end']==1, 'tdate']
    elif freq=='week':
        rbldates = dates.loc[dates.index[dates['week_end']==1]+1, 'tdate']
    elif freq=='weekend':
        rbldates = dates.loc[dates['week_end']==1, 'tdate']
    elif freq=='year':
        rbldates = dates.loc[dates.index[dates['year_end']==1]+1, 'tdate']
    elif freq=='yearend':
        rbldates = dates.loc[dates['year_end']==1, 'tdate']
    elif bool(re.search(r'\d*d\d*', freq)): # 5d* or 20d*
        datenew = pd.read_csv(qdata_dir+'info/trading_day/date_new.csv').rename(columns={'date': 'tdate'})
        nums = [int(x) for x in freq.split('d')]
        if nums[0]==20 and nums[1]<=20: # 20d*
            temp = datenew.loc[(datenew['week_num']==math.ceil(nums[1]/5))&(datenew['day_num']==(nums[1]-(math.ceil(nums[1]/5)-1)*5)), 'tdate']
        elif nums[0]==5 and nums[1]<=5: # 5d*
            temp = datenew.loc[datenew['day_num']==nums[1], 'tdate']
        for temp_date in temp:
            rbldate = [x for x in dates['tdate'] if x>=temp_date][0]
            rbldates.append(rbldate)
    if(len(rbldates)==0): return None
    rbldates = sorted(np.unique(rbldates))
    idx = list(map(lambda x: list(dates['tdate']).index(x)-1, rbldates))
    sigdates = dates.loc[idx, 'tdate']
    date_map = pd.DataFrame({'rbldate': rbldates, 'sigdate': sigdates})
    # date_map = date_map[(date_map['rbldate']>=date_from)&(date_map['rbldate']<=date_to)]
    date_map = date_map[(date_map['sigdate']>=date_from)&(date_map['sigdate']<=date_to)]
    date_map = date_map.sort_values(by='rbldate', ascending=True).reset_index(drop=True)
    return date_map

def seed_normal(seed):
    os.environ['PYTHONHASHSEED'] = str(seed)
    random.seed(seed) 
    np.random.seed(seed)
    return None

#-- data loader
def load_themecache_data(label_shift, years):
    themecache_dir = output_root+'data/themecache_{}/'.format(args.dataset)
    years = sorted(years)
    dats = pd.DataFrame()
    for year in years:
        themecache_path = themecache_dir+'themecache.'+str(year)+'.pkl'
        if not os.path.exists(themecache_path): continue
        dat = pd.read_pickle(themecache_path)
        label_path = output_root+'data/label_p{}/label.{}.pkl'.format(label_shift, year)
        if os.path.exists(label_path):
            label = pd.read_pickle(label_path)
            label = label.loc[:,['label_adj']].rename(columns={'label_adj':'label'})
            # label = label.unstack().T
            # label = (label-label.mean())/(label.std()+1e-12)
            # label = label.T.stack()
            dat = pd.merge(dat, label, how='left', left_index=True, right_index=True)
        else:
            dat['label'] = np.nan
        dats = dats.append(dat)
        print(themecache_path, label_path)
    return dats

#-- for backtest & product
def train_lgb(X, Y, X_valid=None, Y_valid=None, **lgbargs):
    params_lgb = {
        'boosting_type': 'gbdt',
        'objective': 'regression',
        'metric': 'l2', # rmse
        'device_type': 'cpu', # cuda
        # 'gpu_use_dp': True, 
        # 'gpu_platform_id': 0,
        # 'gpu_device_id': 1, # 0
        'linear_tree': True, # TODO: not available when device=cuda
        'num_leaves': 31, # 15
        'extra_trees': True,
        'min_data_in_leaf': 1000, # 1000
        'learning_rate': 0.1,
        # 'num_iterations': 100,
        # 'early_stopping_rounds': 50,
        'max_bin': 127, # 127
        'feature_fraction': 0.9, # min(0.9, 200/X.shape[1]),
        'bagging_fraction': 0.5, # 0.5
        'bagging_freq': 5, # 5
        # 'lambda_l1': 0,
        # 'lambda_l2': 0,
        'monotone_constraints': [1]*X.shape[1],
        'force_row_wise': True,
        'num_threads': 1,
        'verbosity': -1 # -1
    }
    params_lgb.update(**lgbargs)
    lgb_train = lgb.Dataset(X, label=Y)
    if X_valid is None: # no early_stop
        fit = lgb.train(params=params_lgb, train_set=lgb_train, num_boost_round=200)
    else: # early_stop
        params_lgb['early_stopping_rounds'] = 20
        lgb_valid = lgb.Dataset(X_valid, label=Y_valid)
        fit = lgb.train(params=params_lgb, train_set=lgb_train, num_boost_round=200, valid_sets=lgb_valid)
    return fit

def train_xgb(X, Y, X_valid=None, Y_valid=None, **xgbargs):
    mono_cons = dict.fromkeys(X.columns, 1)
    params_xgb = {
        'booster': 'gbtree',
        'objective': 'reg:squarederror',
        'eval_metric': 'auc',
        # 'importance_type': 'weight',
        'device': 'gpu', # cuda:1
        'max_depth': 5,
        # 'num_boost_round': 500,
        # 'early_stopping_rounds': 50,
        'max_bin': 127,
        'eta': 0.1,
        'min_child_weight': 1,
        'max_delta_step': 0,
        'subsample': 0.5, # bagging_fraction
        'colsample_bytree': 0.9, # feature_fraction
        # 'colsample_bylevel': 1,
        # 'colsample_bynode': 1,
        # 'gamma': 0,
        # 'lambda': 1,
        # 'alpha': 0,
        'monotone_constraints': mono_cons,
        'nthread': 1,
        'verbosity': 0
    }
    params_xgb.update(**xgbargs)
    xgb_train = xgb.DMatrix(X, label=Y)
    if X_valid is None: # no early_stop
        fit = xgb.train(params=params_xgb, dtrain=xgb_train, num_boost_round=200)
    else: # early_stop
        params_xgb['early_stopping_rounds'] = 20
        xgb_valid = xgb.DMatrix(X_valid, label=Y_valid)
        fit = xgb.train(params=params_xgb, dtrain=xgb_train, num_boost_round=200, evals=[(xgb_valid, 'valid')], verbose_eval=False)
    return fit

def run_backtest_dt_roll(date_from, date_to, freq='5d1', retrain_freq='month', label_shift=10, train_window=240):
    output_dir = output_root+'model_dt_'+args.univ+'/'
    ensure_dir(output_dir)
    alpha_id = 'alpha_{}_{}_p{}_r{}'.format(args.model, args.dataset, label_shift, train_window)
    alpha_dir = output_root+'alpha_dt_{}/{}/'.format(args.univ, alpha_id)
    ensure_dir(alpha_dir)
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    dates['retrain'] = dates[retrain_freq].diff().shift(-1)
    retrain_dates = list(dates.loc[dates['retrain']>0, 'tdate'])
    first_retrain_date = max([d for d in retrain_dates if d<=date_from])
    idx_from = max(dates.index[dates['tdate']<=first_retrain_date])
    date_from_real = int(dates.loc[max(0, idx_from-train_window+1), 'tdate'])
    print('date: from {} to {} -> from {} to {}'.format(date_from, date_to, date_from_real, date_to))
    themecache_years = range(date_from_real//10000, date_to//10000+1)
    dat = load_themecache_data(label_shift=label_shift, years=themecache_years)
    dat_dates = set(dat.index.get_level_values('tdate')) # unordered set
    theme_names = [name for name in dat.columns if name!='label']
    print(dat, theme_names)
    #-- preparation
    seed_normal(2023+args.seed)
    log_result = pd.DataFrame()
    last_retrain_date = 0
    train_ratio = 1 # 0.9
    importance = None
    datemap = get_datemap(date_from=date_from, date_to=date_to, freq=freq)
    datemap = datemap[datemap['sigdate'].isin(dat_dates)]
    print(datemap)
    for i in range(len(datemap)):
        sigdate = datemap.loc[i, 'sigdate']
        rbldate = datemap.loc[i, 'rbldate']
        retrain_date = max([d for d in retrain_dates if d<=sigdate])
        if last_retrain_date!=retrain_date: # retrain
            model_path = output_dir+'model_{}_{}_p{}_r{}_{}.txt'.format(args.model, args.dataset, label_shift, train_window, retrain_date)
            if os.path.exists(model_path):
                if args.model=='lgb':
                    fit = lgb.Booster(model_file=model_path)
                    importance = list(fit.feature_importance(importance_type='gain'))
                elif args.model=='xgb':
                    fit = xgb.Booster(model_file=model_path)
                    importance = list(fit.get_score(importance_type='gain').values())
                print('model load: ', model_path)
            else: # train
                idx = max(dates.index[dates['tdate']<=retrain_date])
                sample_dates = list(dates.loc[(idx-train_window-label_shift):(idx-label_shift-1), 'tdate'])
                # sample_dates = [d for d in sample_dates if d%10000!=1231] # ignore ****1231, just a trick
                train_dates, valid_dates = split_sample_dates(sample_dates, train_ratio=train_ratio)
                train_dates = [d for d in train_dates if d in dat_dates]
                print('train:', min(train_dates), max(train_dates), len(train_dates))
                dat_train = dat.loc[train_dates].dropna() # .fillna(0)
                X = dat_train.drop(['label'], axis=1)
                Y = dat_train['label']
                X_valid, Y_valid = None, None
                if valid_dates is not None and len(valid_dates)>0:
                    valid_dates = [d for d in valid_dates if d in dat_dates]
                    print('valid:', min(valid_dates), max(valid_dates), len(valid_dates))
                    dat_valid = dat.loc[valid_dates].dropna() # .fillna(0)
                    X_valid = dat_valid.drop(['label'], axis=1)
                    Y_valid = dat_valid['label']
                if args.model=='lgb':
                    fit = train_lgb(X, Y, X_valid, Y_valid)
                    importance = list(fit.feature_importance(importance_type='gain'))
                elif args.model=='xgb':
                    fit = train_xgb(X, Y, X_valid, Y_valid)
                    importance = list(fit.get_score(importance_type='gain').values())
                fit.save_model(model_path)
                print('model train: ', model_path)
            last_retrain_date = retrain_date
        #-- pred and output
        X_pred = dat.loc[sigdate].drop(['label'], axis=1)
        if args.model=='lgb':
            Y_pred = fit.predict(X_pred)
        elif args.model=='xgb':
            Y_pred = fit.predict(xgb.DMatrix(X_pred))
        alpha = pd.DataFrame({'id': X_pred.index.copy(), 'alpha': Y_pred})
        alpha['id'] = ['{0:0>6}'.format(re.sub('.SH|.SZ|.BJ', '', s)) for s in alpha['id']]
        alpha_path = alpha_dir+alpha_id+'.'+str(rbldate)+'.csv'
        alpha.to_csv(alpha_path, header=None, index=0)
        #-- log result
        Y_real = list(dat.loc[sigdate, 'label'])
        this_ic, p_value = spearmanr(a=Y_real, b=Y_pred, nan_policy='omit')
        record = [rbldate, sigdate, retrain_date, len(Y_pred), this_ic, p_value]+importance
        log_result = log_result.append(pd.Series(record), ignore_index=True)
    #-- collect result
    log_result.columns = ['rbldate','sigdate','retrain_date','num','rank_ic','p_value']+theme_names
    log_result_path = output_dir+'log_result_{}_{}_p{}_r{}.csv'.format(args.model, args.dataset, label_shift, train_window)
    if os.path.exists(log_result_path):
        log_result_his = pd.read_csv(log_result_path)
        log_result = log_result.append(log_result_his).drop_duplicates(subset='rbldate')
    log_result = log_result.sort_values(by='rbldate', ascending=True)
    log_result.to_csv(log_result_path, index=0)
    return None

#-- launcher: backtest
# run_backtest_dt_roll(date_from=20140101, date_to=20241227, freq='day', retrain_freq='month', label_shift=5, train_window=1440)
# run_backtest_dt_roll(date_from=20140101, date_to=20241227, freq='day', retrain_freq='month', label_shift=20, train_window=480)
# exit()
# for train_window in [240,480,720,960,1200,1440]:
#     run_backtest_dt_roll(date_from=20140101, date_to=20241226, freq='5d1', retrain_freq='month', label_shift=20, train_window=train_window)
#     print(train_window)
# exit()

#-- launcher: product
today = datetime.now()
date_from = int((today+timedelta(days=-11)).strftime('%Y%m%d'))
date_to = int((today+timedelta(days=-1)).strftime('%Y%m%d'))
print(date_from, date_to)
run_backtest_dt_roll(date_from=date_from, date_to=date_to, freq='day', retrain_freq='month', label_shift=5, train_window=1440)
run_backtest_dt_roll(date_from=date_from, date_to=date_to, freq='day', retrain_freq='month', label_shift=20, train_window=480)

