#-- pvnet/main_nn.py, lic, 20241212
from datetime import datetime, timedelta
from scipy.stats import spearmanr
from pandas.errors import PerformanceWarning
import os
os.environ['OMP_NUM_THREADS'] = '1'
import numpy as np
import torch
import torch.nn as nn
import pandas as pd
import argparse
import random
import pyreadr
import pickle
import re
import copy
import warnings
from model_nn import *
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=PerformanceWarning)

#-- declaration
qdata_dir = '/home/data/q/'
fundrep_dir = '/home/data/q/research/repl_fund_stock/'
fundkeyext_dir = '/home/data/q/research/fundkey_univ/'
univ_base_dir = '/home/data/iqdata/univ/'
univ_ex_dir = '/home/data/q/research/stock_pool/'
forcast_univs = ['ss50','zz100','hs300','zz500','zz800','zz1000','zz2000','gz2000','zzhl','fundkey','broad_univ','broad_univ_withzz800','broad']
dataset_map = { # first element for bar_type, remaining elements for features
    'day1' : ['daybar','adjopen','adjhigh','adjlow','adjclose','adjvwap','turnover'],
    'day2' : ['daybar','adjopen','adjhigh','adjlow','adjclose','adjvwap','volume','amount'],
    'week1': ['weekbar','adjopen','adjhigh','adjlow','adjclose','adjvwap','turnover'],
    'week2': ['weekbar','adjopen','adjhigh','adjlow','adjclose','adjvwap','volume','amount']
}
output_root = '/data/lic/pvnet/output/'

#-- param 
parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--device', type=str, default='')
parser.add_argument('--retrain', type=int, default=0)
parser.add_argument('--univ', type=str, default='all')
parser.add_argument('--model', type=str, default='agru')
parser.add_argument('--dataset', type=str, default='day1')
args = parser.parse_args()
args.features = dataset_map[args.dataset]
args.num_features = len(args.features)-1
args.device = args.device if torch.cuda.is_available() else 'cpu'
args.device = 'cuda:{}'.format(args.seed%torch.cuda.device_count()) if args.device=='' else args.device
device = torch.device(args.device)
seed_torch(2023+args.seed)
print(args)

#-- util
def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)        
    return None

def split_sample_dates(dates, train_ratio=0.9):
    idx = round(len(dates)*train_ratio)
    train_dates = dates[:idx]
    valid_dates = dates[idx:]
    return train_dates, valid_dates

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

def get_univ_prefix(univ_name):
    univ_prefix = univ_name
    if univ_name in ['ss50','zz100','hs300','zz500','zz800','zz1000','zz2000','gz2000','zzhl']:
        univ_prefix = univ_name+'_forcast'
    elif univ_name=='broad':
        univ_prefix = 'broad_m'
    elif univ_name=='YangQi':
        univ_prefix = 'gxsoex'
    elif univ_name=='gxyqzz':
        univ_prefix = 'gxsoe'
    return univ_prefix

def get_univ_dir(univ_name):
    if univ_name in ['ss50','zz100','hs300','zz500','zz800','zz1000','zz2000','gz2000','zzhl']:
        univ_dir = univ_base_dir+univ_name+'_forcast/'
    elif univ_name=='broad':
        univ_dir = univ_base_dir+'broad_m/'
    elif univ_name=='GuoXinBmk':
        univ_dir = univ_ex_dir+'GuoXinBmk/'
    elif univ_name=='ChengTongBmk':
        univ_dir = univ_ex_dir+'ChengTongBmk/'
    elif univ_name=='YangQi':
        univ_dir = univ_ex_dir+'gxsoex/'
    elif univ_name=='gxyqzz':
        univ_dir = univ_ex_dir+'gxsoe/'
    elif univ_name[:7]=='fundrep':
        univ_dir = fundrep_dir+univ_name+'/'
    elif univ_name in ['fundkeyind1','fundkeyr','fundkeyf']: # fundkey extension
        univ_dir = fundkeyext_dir+univ_name+'/'
    else: # by default
        univ_dir = univ_base_dir+univ_name+'/'
    return univ_dir

def get_univ_pool(univ_name, rbldate):
    univ_dir = get_univ_dir(univ_name=univ_name)
    univ_prefix = get_univ_prefix(univ_name=univ_name)
    univ_dates = os.listdir(univ_dir)
    univ_dates = list(filter(lambda s: re.search(r'\d{8}.csv', s), univ_dates))
    univ_dates = sorted([int(s[(-12):(-4)]) for s in univ_dates])
    if univ_name in forcast_univs:
        univ_date = [x for x in univ_dates if x<=rbldate][-1:]
    else:
        univ_date = [x for x in univ_dates if x<rbldate][-1:]
    if len(univ_date)==0: return None
    univ_path = univ_dir+'/'+univ_prefix+'.'+str(univ_date[0])+'.csv'
    univ_pool = pd.read_csv(univ_path, header=None, names=['id','weight'])
    univ_pool['id'] = ['{0:0>6}'.format(s) for s in univ_pool['id']]
    univ_pool = univ_pool.dropna()
    univ_pool['weight'] = univ_pool['weight']/sum(univ_pool['weight'])
    return univ_pool

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
    date_map = date_map[(date_map['sigdate']>=date_from)&(date_map['sigdate']<=date_to)]
    date_map = date_map.sort_values(by='rbldate', ascending=True).reset_index(drop=True)
    return date_map

#-- data loader
def load_daybar_byyear(label_shift, feature_lag, years):
    selected_cols = [s+'_lag{}'.format(feature_lag-i-1) for s in args.features[1:] for i in range(feature_lag)]
    dats = pd.DataFrame()
    for year in sorted(years):
        daybar_path = output_root+'data/{}_f{}/{}.{}.pkl'.format(args.features[0], feature_lag, args.features[0], year)
        if not os.path.exists(daybar_path): continue
        dat = pd.read_pickle(daybar_path).loc[:,selected_cols]
        label_path = output_root+'data/label_p{}/label.{}.pkl'.format(label_shift, year)
        if os.path.exists(label_path):
            label = pd.read_pickle(label_path)
            label = label.loc[:,['label_adj']].rename(columns={'label_adj':'label'})
            dat = pd.merge(dat, label, how='inner', left_index=True, right_index=True)
        else:
            dat['label'] = np.nan
        dats = pd.concat([dats, dat], axis=0)
        print(daybar_path, label_path)
    return dats

#-- for backtest
def run_backtest_daybar_byyear(label_shift, feature_lag, begin_year=2003, end_year=2013):
    output_dir = output_root+'model_nn_'+args.univ+'/'
    ensure_dir(output_dir)
    #-- load model of last_year
    seed_torch(2023+args.seed)
    main_model = setup_nn_model(model_id=args.model, input_size=args.num_features, device=device)
    optimizer = torch.optim.Adam(main_model.parameters(), lr=0.0001)
    # loss_fn = nn.MSELoss().to(device)
    model_path = output_dir+'model_{}_{}_p{}_f{}_{}_{}.pth'.format(args.model, args.dataset, label_shift, feature_lag, args.seed, end_year-1)
    if os.path.exists(model_path):
        print(model_path)
        main_model.load_state_dict(torch.load(model_path))
    best_ic, num_epoch = 0, 0
    max_epoch = 200
    early_stop_epoch = 20
    train_ratio = 0.90 #
    #-- data prepare 
    daybar_years = list(range(begin_year, end_year+1)) # [end_year-b for b in range(year_num, -1, -1)]
    dats = load_daybar_byyear(label_shift=label_shift, feature_lag=feature_lag, years=daybar_years).dropna()
    dat_dates = np.unique(dats.index.get_level_values('tdate'))
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    sample_dates = sorted(dates.loc[(dates['tdate']>=begin_year*10000+101)&(dates['tdate']<=end_year*10000+1231), 'tdate'])
    sample_dates = [d for d in sample_dates if d%10000!=1231] # ignore ****1231, just a trick
    train_dates, valid_dates = split_sample_dates(sample_dates, train_ratio=train_ratio)
    valid_dates = valid_dates[:-(label_shift+1)]
    train_dates = [d for d in train_dates if d in dat_dates]
    valid_dates = [d for d in valid_dates if d in dat_dates]
    print('train:', min(train_dates), max(train_dates), len(train_dates))
    print('valid:', min(valid_dates), max(valid_dates), len(valid_dates))
    print(dats)
    #-- iter
    log_result = pd.DataFrame()
    loss_list, rankiclist, corrlist = [], [], []
    for cnt in range(max_epoch):
        total_loss, step = 0, 0
        #-- train
        main_model.train()
        for i in range(len(train_dates)):
            tdate = train_dates[i]
            dat = dats.loc[tdate]
            if args.univ!='all':
                univ_data = get_univ_pool(univ_name=args.univ, rbldate=tdate)
                if univ_data is not None:
                    dat = dat.loc[dat.index.isin(univ_data['id'])]
            features = dat.drop(['label'], axis=1).to_numpy(copy=False)
            # dat['label'] = dat['label'].rank()/len(dat)
            label = torch.tensor(dat['label']).unsqueeze(1).to(device).to(torch.float32)
            with torch.no_grad():
                label = (label-label.mean(axis=0))/(label.std(axis=0)+1e-12)
            num_id = len(dat) # [N, F, T]->[N, T, F]
            train_data = torch.tensor(features.reshape(num_id, args.num_features, feature_lag)).permute(0, 2, 1).to(device).to(torch.float32)
            optimizer.zero_grad()
            pred, fv = main_model(train_data)
            corr = torch.mm(fv.t(), fv)/num_id
            corr = corr-torch.diag_embed(torch.diag(corr))
            loss = -(pred*label).mean()+0.5*(corr*corr).mean() # loss_fn(pred, label)
            total_loss += loss.item()
            step += 1
            loss.backward()
            nn.utils.clip_grad_value_(main_model.parameters(), 1.0)
            optimizer.step()
            torch.cuda.empty_cache()
        total_loss = total_loss/step
        loss_list.append(total_loss)
        #-- validation
        iclist, rholist = [], []
        main_model.eval()
        for i in range(len(valid_dates)):
            tdate = valid_dates[i]
            dat = dats.loc[tdate]
            if args.univ!='all':
                univ_data = get_univ_pool(univ_name=args.univ, rbldate=tdate)
                if univ_data is not None:
                    dat = dat.loc[dat.index.isin(univ_data['id'])]
            features = dat.drop(['label'], axis=1).to_numpy(copy=False)
            # dat['label'] = dat['label'].rank()/len(dat)
            num_id = len(dat) # [N, F, T]->[N, T, F]
            valid_data = torch.tensor(features.reshape(num_id, args.num_features, feature_lag)).permute(0, 2, 1).to(device).to(torch.float32)
            with torch.no_grad():
                pred, fv = main_model(valid_data)
                fv_norm = (fv-fv.mean(0))/(fv.std(0)+1e-12)
            corr = torch.mm(fv_norm.t(), fv_norm)/num_id
            corr = corr-torch.diag_embed(torch.diag(corr))
            corr = abs(corr).mean().cpu().detach().numpy()
            rholist.append(corr)
            this_ic, p_value = spearmanr(a=fv_norm.mean(1).squeeze().cpu().detach().numpy(), b=dat['label'], nan_policy='omit')
            iclist.append(this_ic)
            log_result = log_result.append({'cnt': cnt, 'epoch': num_epoch, 'tdate': tdate, 'rank_ic': this_ic, 'corr': corr, 'best_ic': best_ic, 'total_loss': total_loss}, 
                ignore_index=True)
            # torch.cuda.empty_cache()
        ic = np.mean(iclist)
        rho = np.mean(rholist)
        rankiclist.append(ic)
        corrlist.append(rho)
        log_result.to_csv(output_dir+'log_result_{}_{}_p{}_f{}_{}_{}.csv'.format(args.model, args.dataset, label_shift, feature_lag, args.seed, end_year), index=0)
        print(end_year, cnt, num_epoch, ic, best_ic, total_loss, rho)
        #-- early stop
        if ic<=best_ic:
            num_epoch += 1
            if num_epoch >= early_stop_epoch: break
        else:
            num_epoch = 0
            best_ic = ic
            path = output_dir+'model_{}_{}_p{}_f{}_{}_{}.pth'.format(args.model, args.dataset, label_shift, feature_lag, args.seed, end_year)
            torch.save(main_model.state_dict(), path) # use pth suffix for model
    return loss_list, rankiclist, corrlist

#-- for backtest & product
def gen_alpha_daybar_combined(label_shift=10, feature_lag=30, freq='day', date_from=20140101, date_to=20140131, use_features='product', seeds=['0','1','2','3']):
    combined_seed = ''.join(seeds)
    alpha_id = 'alpha_{}_{}_p{}_f{}_{}'.format(args.model, args.dataset, label_shift, feature_lag, combined_seed)
    output_dir = output_root+'alpha_nn_{}/{}/'.format(args.univ, alpha_id)
    ensure_dir(output_dir)
    print(output_dir)
    selected_cols = [s+'_lag{}'.format(feature_lag-i-1) for s in args.features[1:] for i in range(feature_lag)]
    model_dir = output_root+'model_nn_'+args.univ+'/'
    daybar_dir = output_root+'data/{}_f{}/'.format(args.features[0], feature_lag)
    if use_features=='product':
        daybar_dir = output_root+'data_product/{}_f{}/'.format(args.features[0], feature_lag)
        dats = pd.read_pickle(daybar_dir+'{}_product.pkl'.format(args.features[0])).loc[:,selected_cols]
        print(dats)
    datemap = get_datemap(date_from=date_from, date_to=date_to, freq=freq)
    datemap['year'] = [x//10000 for x in datemap['sigdate']]
    last_year = 0
    model_chain = None
    for i in range(len(datemap)):
        year = datemap.loc[i, 'year']
        sigdate = datemap.loc[i, 'sigdate']
        rbldate = datemap.loc[i, 'rbldate']
        print(year, sigdate, rbldate)
        if year!=last_year:
            if use_features!='product':
                dats = pd.read_pickle(daybar_dir+'{}.{}.pkl'.format(args.features[0], year)).loc[:,selected_cols]
            model_chain = {}
            for seed in seeds:
                main_model = setup_nn_model(model_id=args.model, input_size=args.num_features, device=device)
                model_path = model_dir+'model_{}_{}_p{}_f{}_{}_{}.pth'.format(args.model, args.dataset, label_shift, feature_lag, seed, year-1)
                if (use_features=='product') and (not os.path.exists(model_path)): # for product
                    model_pattern = 'model_{}_{}_p{}_f{}_{}_'.format(args.model, args.dataset, label_shift, feature_lag, seed)
                    model_files = sorted([f for f in os.listdir(model_dir) if re.search(model_pattern, f)])
                    if len(model_files)==0: return None
                    model_path = model_dir+model_files[-1]
                main_model.load_state_dict(torch.load(model_path)) # strict=False
                print(model_path, main_model)
                main_model.eval()
                model_chain[seed] = main_model
            last_year = year
        #-- begin
        if not sigdate in dats.index.get_level_values('tdate'): continue
        dat = dats.loc[sigdate].dropna()
        if args.univ!='all':
            univ_data = get_univ_pool(univ_name=args.univ, rbldate=sigdate)
            if univ_data is not None:
                dat = dat.loc[dat.index.isin(univ_data['id'])]
        features = dat.to_numpy(copy=False)
        num_id = len(dat) # [N, F, T]->[N, T, F]
        valid_data = torch.tensor(features.reshape(num_id, args.num_features, feature_lag)).permute(0, 2, 1).to(device).to(torch.float32)
        alpha_combined = None
        for seed in seeds:
            main_model = model_chain[seed]
            with torch.no_grad():
                pred, fv = main_model(valid_data)
                fv_norm = (fv-fv.mean(0))/(fv.std(0)+1e-12)
            alpha_score = fv_norm.mean(1).squeeze().cpu().detach().numpy() # pred
            # alpha_score = (alpha_score-alpha_score.mean())/(alpha_score.std()+1e-12)
            alpha = pd.DataFrame({'id':dat.index.copy(), 'alpha':alpha_score}) # .rename(columns={'id': 'seed'+seed})
            alpha['id'] = ['{0:0>6}'.format(re.sub('.SH|.SZ|.BJ', '', s)) for s in alpha['id']]
            if alpha_combined is None:
                alpha_combined = alpha
            else:
                alpha_combined = pd.merge(alpha_combined, alpha, how='inner', on='id')
        alpha_combined['alpha'] = alpha_combined.iloc[:,1:].mean(axis=1)
        alpha_combined = alpha_combined.loc[:,['id','alpha']].reset_index(drop=True)
        alpha_combined.to_csv(output_dir+alpha_id+'.'+str(rbldate)+'.csv', header=None, index=0)
        torch.cuda.empty_cache()
    return None

#-- launcher: backtest
# gen_alpha_daybar_combined(label_shift=10, feature_lag=30, freq='day', date_from=20140101, date_to=20241212, use_features='backtest')
# exit()
# for end_year in range(2013,2025): # 2013,2025
#     run_backtest_daybar_byyear(label_shift=10, feature_lag=30, begin_year=(end_year-10), end_year=end_year)
# exit()

#-- launcher: retrain
if args.retrain>0:
  end_year = int(args.retrain)
  run_backtest_daybar_byyear(label_shift=10, feature_lag=30, begin_year=(end_year-10), end_year=end_year)
  exit()

#-- launcher: product
today = datetime.now()
date_from = int((today+timedelta(days=-11)).strftime('%Y%m%d'))
date_to = int((today+timedelta(days=-1)).strftime('%Y%m%d'))
print(date_from, date_to)
gen_alpha_daybar_combined(label_shift=10, feature_lag=30, freq='day', date_from=date_from, date_to=date_to, use_features='product')

