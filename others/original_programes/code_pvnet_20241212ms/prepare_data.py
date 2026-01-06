#-- pvnet/prepare_data.py, lic, 20241212
from datetime import datetime, timedelta
from scipy.stats import spearmanr
from pandas.errors import PerformanceWarning
import os
import re
import pandas as pd
import numpy as np
import pickle
import argparse
import pyreadr
import warnings
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)
warnings.filterwarnings('ignore', category=PerformanceWarning)

#-- declaration
qdata_dir = '/home/data/q/'
output_root = '/data/lic/pvnet/output/'
fundrep_dir = '/home/data/q/research/repl_fund_stock/'
fundkeyext_dir = '/home/data/q/research/fundkey_univ/'
univ_base_dir = '/home/data/iqdata/univ/'
univ_ex_dir = '/home/data/q/research/stock_pool/'
forcast_univs = ['ss50','zz100','hs300','zz500','zz800','zz1000','zz2000','gz2000','zzhl','fundkey','broad_univ','broad_univ_withzz800','broad_m']
daybar_features = ['adjopen','adjhigh','adjlow','adjclose','adjvwap','volume','amount','turnover']
price_key_names = ['adjopen','adjhigh','adjlow','adjclose','adjvwap']
lag_multipliers = {
    'daybar':  1,
    'weekbar': 5
}
theme_dir = '/home/data/iqdata/alpha/CN/dyn_broad_zz800_cne5citics_size_beta/'
theme_set = {
    'alpha':    ['analyst','growth','momentum','quality','reversal','size','value1','value2'],
    'allsub1y': ['analyst_drev','analyst_drec','analyst_coverage','analyst_rec','bigdata_pa','growth_s','growth_l','growth_f',\
                'momentum_f','momentum_t','quality_cashflow','quality_profit_a','quality_safety',\
                'reversal_vol','reversal_price','reversal_value','reversal_liq','size','smartholding',\
                'value_dvd','value_pb','value_pe','value_cf','value_peg']
}

#-- param 
parser = argparse.ArgumentParser()
parser.add_argument('--retrain', type=int, default=0)
args = parser.parse_args()

#-- util
def ensure_dir(path):
    if not os.path.exists(path):
        os.makedirs(path)   
    return None

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

def extract_augstat(df, m=7):
    df_med, df_mad = df.median(), df.mad()
    df_lb, df_ub = df_med-m*df_mad, df_med+m*df_mad
    df = df.clip(lower=df_lb, upper=df_ub, axis=1)
    df_mean, df_std = df.mean(), df.std()
    # df_mean, df_std = df_med, 1.48*(df_mad+1e-12)
    df_stat = pd.concat([df_ub, df_lb, df_mean, df_std], axis=1)
    df_stat.columns = ['upper','lower','mean', 'std']
    return df_stat

def apply_augstat(series, u, l, m, s):
    out = series.copy()
    out[out>u] = u
    out[out<l] = l
    out = (out-m)/(s+1e-12)
    return out

#-- update rawdata
def update_rawdata_daybar(date_from, date_to):
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    #-- rawdata/daybar
    daybar_output_dir = output_root+'data/rawdata/daybar/'
    ensure_dir(daybar_output_dir)
    upd_dates = np.array(dates.loc[(dates['tdate']>=date_from)&(dates['tdate']<=date_to), 'tdate'])
    upd_years = [x//10000 for x in upd_dates]
    upd_years_unique = np.unique(upd_years)
    selected_cols = ['tdate','id','adjopen','adjhigh','adjlow','adjclose','adjvwap','ret','volume','amount','turnover']
    for year in upd_years_unique:
        selected_dates = [upd_dates[i] for i,x in enumerate(upd_years) if x==year]
        data_path = daybar_output_dir+'daybar.'+str(year)+'.pkl'
        daily_cache = pd.DataFrame()
        if os.path.exists(data_path):
            his_data = pd.read_pickle(data_path)
            his_data = his_data.loc[~his_data['tdate'].isin(selected_dates)]
            if len(his_data)>0:
                selected_dates = np.setdiff1d(selected_dates, his_data['tdate'])
                daily_cache = his_data
        for tdate in selected_dates:
            daily = load_rdata(qdata_dir, 'mkt/stock_daily', tdate)
            if daily is None: continue
            daily['id_prefix'] = [s[0] for s in daily['id']]
            daily['vwap'] = daily['amount']/daily['volume']*10
            daily['adjopen'] = daily['open']*daily['adjfactor']
            daily['adjhigh'] = daily['high']*daily['adjfactor']
            daily['adjlow'] = daily['low']*daily['adjfactor']
            daily['adjclose'] = daily['close']*daily['adjfactor']
            daily['adjvwap'] = daily['vwap']*daily['adjfactor']
            daily['adjvwap'] = daily['adjvwap'].fillna(daily['adjclose'])
            daily['ret'] = daily['close']/daily['pclose']-1
            daily['turnover'] = daily['volume']/daily['floatshr']/100
            daily['turnover'] = daily['turnover'].fillna(0)
            daily = daily.loc[daily['id_prefix'].isin(['0','3','6']), selected_cols]
            daily_cache = pd.concat([daily_cache, daily], axis=0)
            print(tdate, len(daily))
        daily_cache = daily_cache.sort_values(by='tdate', ascending=True).reset_index(drop=True)
        daily_cache.to_pickle(data_path)
        print(year, len(daily_cache))
    return None

def update_rawdata_weekbar(date_from, date_to):
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    #-- rawdata/weekbar(-5)
    weekbar_lag = lag_multipliers['weekbar']
    weekbar_output_dir = output_root+'/data/rawdata/weekbar/'
    ensure_dir(weekbar_output_dir)
    daybar_output_dir = output_root+'data/rawdata/daybar/'
    idx_from = max(dates.index[dates['tdate']<=date_from])
    date_from_real = int(dates.loc[max(0, idx_from-weekbar_lag+1), 'tdate'])
    print('date: from {} to {} -> from {} to {}'.format(date_from, date_to, date_from_real, date_to))
    upd_dates = np.array(dates.loc[(dates['tdate']>=date_from_real)&(dates['tdate']<=date_to), 'tdate'])
    upd_years = np.unique([x//10000 for x in upd_dates])
    daybar_cache = pd.DataFrame()
    for year in upd_years:
        data_path = daybar_output_dir+'daybar.'+str(year)+'.pkl'
        if not os.path.exists(data_path): continue
        daybar = pd.read_pickle(data_path)
        daybar = daybar[(daybar['tdate']>=date_from_real)&(daybar['tdate']<=date_to)]
        daybar_cache = pd.concat([daybar_cache, daybar], axis=0)
    daybar_cache = daybar_cache.set_index(['tdate','id']).sort_index().dropna()
    #-- add lag features
    selected_cols = ['adjopen','adjhigh','adjlow','adjclose','adjvwap','volume','amount','turnover']
    lag_key_names = ['adjopen','adjhigh','adjlow','volume','amount','turnover']
    for key_name in lag_key_names:
        this_column = daybar_cache[key_name].unstack()
        for i in range(weekbar_lag):
            name = key_name+'_lag'+str(weekbar_lag-i-1)
            if i==(weekbar_lag-1):
                daybar_cache[name] = daybar_cache[key_name]
            else:
                daybar_cache[name] = this_column.shift(weekbar_lag-i-1).stack()
        print(key_name)
    #-- deal with ohlcwva
    daybar_cache['adjfactor'] = daybar_cache['adjvwap']/(daybar_cache['amount']/daybar_cache['volume']*10)
    daybar_cache['adjopen'] = daybar_cache['adjopen_lag{}'.format(weekbar_lag-1)]
    daybar_cache['adjhigh'] = daybar_cache[['adjhigh_lag{}'.format(weekbar_lag-i-1) for i in range(weekbar_lag)]].max(axis=1)
    daybar_cache['adjlow'] = daybar_cache[['adjlow_lag{}'.format(weekbar_lag-i-1) for i in range(weekbar_lag)]].min(axis=1)
    daybar_cache['volume'] = daybar_cache[['volume_lag{}'.format(weekbar_lag-i-1) for i in range(weekbar_lag)]].sum(axis=1)
    daybar_cache['amount'] = daybar_cache[['amount_lag{}'.format(weekbar_lag-i-1) for i in range(weekbar_lag)]].sum(axis=1)
    daybar_cache['turnover'] = daybar_cache[['turnover_lag{}'.format(weekbar_lag-i-1) for i in range(weekbar_lag)]].sum(axis=1)
    daybar_cache['adjvwap'] = daybar_cache['amount']/daybar_cache['volume']*10*daybar_cache['adjfactor']
    daybar_cache['adjvwap'] = daybar_cache['adjvwap'].fillna(daybar_cache['adjclose'])
    tdate_index = daybar_cache.index.get_level_values('tdate')
    weekbar_cache = daybar_cache.loc[(tdate_index>=date_from), selected_cols]
    #-- output
    upd_dates = np.unique(weekbar_cache.index.get_level_values('tdate'))
    upd_years = [x//10000 for x in upd_dates]
    upd_years_unique = np.unique(upd_years)
    for year in upd_years_unique:
        selected_dates = [upd_dates[i] for i,x in enumerate(upd_years) if x==year]
        weekbar = weekbar_cache.loc[selected_dates].reset_index()
        data_path = weekbar_output_dir+'weekbar.'+str(year)+'.pkl'
        if os.path.exists(data_path):
            his_data = pd.read_pickle(data_path)
            his_data = his_data.loc[~his_data['tdate'].isin(selected_dates)]
            if len(his_data)>0:
                weekbar = pd.concat([his_data, weekbar], axis=0)
        weekbar = weekbar.sort_values(by='tdate', ascending=True).reset_index(drop=True)
        weekbar.to_pickle(data_path)
        print(year, len(weekbar))
    return None

#-- generate label
def gen_label(label_shift, date_from, date_to, corr_window, corr_rank_num):
    min_rank_num = int(corr_rank_num*0.667) # at least
    daybar_dir = output_root+'data/rawdata/daybar/'
    #-- deal with date
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    idx_from = max(dates.index[dates['tdate']<=date_from])
    idx_to = min(dates.index[dates['tdate']>=date_to])
    date_from_real = int(dates.loc[max(0, idx_from-corr_window+1), 'tdate'])
    date_to_real = int(dates.loc[min(len(dates), idx_to+label_shift+1), 'tdate'])
    print('date: from {} to {} -> from {} to {}'.format(date_from, date_to, date_from_real, date_to_real))
    #-- load daybar_cache
    daybar_cache = pd.DataFrame()
    for f in os.listdir(daybar_dir):
        file_year = int(f[(-8):(-4)])
        if (file_year<date_from_real//10000) or (file_year>date_to_real//10000): continue # use date_to_real
        daybar = pd.read_pickle(daybar_dir+f)
        daybar = daybar.loc[:,['tdate','id','adjclose','ret']]
        daybar_cache = pd.concat([daybar_cache, daybar], axis=0)
        print(file_year)
    daybar_cache = daybar_cache[(daybar_cache['tdate']>=date_from_real)&(daybar_cache['tdate']<=date_to_real)]
    daybar_cache = daybar_cache.set_index(['tdate','id']).sort_index().dropna()
    print(daybar_cache)
    #-- generate label_raw
    adjclose = daybar_cache['adjclose'].unstack()
    label_close_from = (adjclose.shift(-1).stack()) # T+1
    label_close_to = (adjclose.shift(-label_shift-1).stack()) # T+label_shift+1
    daybar_cache['label_raw'] = label_close_to/label_close_from-1
    daybar_dates = sorted(daybar_cache.index.get_level_values('tdate').unique())
    label_raw_cache = pd.DataFrame()
    for i in range(corr_window, len(daybar_dates)):
        tdate = daybar_dates[i]
        if tdate>date_to: break
        label_raw = daybar_cache.loc[tdate, ['label_raw']].dropna()
        if len(label_raw)==0: break
        corr_dates = daybar_dates[(i-corr_window):(i)]
        corr_cache = daybar_cache.loc[corr_dates].reset_index()
        corr_cache = corr_cache.pivot_table(index='tdate', columns='id', values='ret')
        idx = sorted(list(set(label_raw.index) & set(corr_cache.columns))) # idx.sort()
        corr_cache = corr_cache[idx]
        label_raw = label_raw.reindex(index=idx)
        corr_mat = corr_cache.corr(min_periods=5)
        rho_mat = corr_mat.rank(axis=0, method='dense', ascending=False).fillna(1e8)
        rho_mat = rho_mat.apply(lambda rank_num: rank_num<=corr_rank_num)
        label_raw_mean = rho_mat.apply(lambda col: label_raw.loc[col].mean() if sum(col)>=min_rank_num else label_raw.mean())
        label_raw_std = rho_mat.apply(lambda col: label_raw.loc[col].std() if sum(col)>=min_rank_num else label_raw.std())
        label_raw['label_adj'] = (label_raw-label_raw_mean.T)/(label_raw_std.T+1e-12)
        label_raw['tdate'] = tdate
        label_raw['id'] = label_raw.index
        label_raw_cache = pd.concat([label_raw_cache, label_raw], axis=0)
        print('label gen', tdate)
    if len(label_raw_cache)>0:
        label_raw_cache = label_raw_cache.set_index(['tdate','id'])
    return label_raw_cache

def gen_label_byyear(label_shift=10, corr_window=240, corr_rank_num=30, year=2013):
    output_dir = output_root+'data/label_p{}/'.format(label_shift) # , corr_window, corr_rank_num
    ensure_dir(output_dir)
    date_from = year*10000+101
    date_to = year*10000+1231
    dats = gen_label(label_shift=label_shift, date_from=date_from, date_to=date_to, corr_window=corr_window, corr_rank_num=corr_rank_num)
    if dats is not None:
        dats.to_pickle(output_dir+'label.'+str(year)+'.pkl')
    return None

def gen_label_product(label_shift=10, corr_window=240, corr_rank_num=30, date_from=20130101, date_to=20130131):
    #-- merge to output/data/
    output_dir = output_root+'data/label_p{}/'.format(label_shift) # , corr_window, corr_rank_num
    ensure_dir(output_dir)
    #-- adjust date_from
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    idx_from = max(dates.index[dates['tdate']<=date_from])
    date_from_real = int(dates.loc[max(0, idx_from-label_shift), 'tdate'])
    print('date: from {} to {} -> from {} to {}'.format(date_from, date_to, date_from_real, date_to))
    #-- gen label
    dats = gen_label(label_shift=label_shift, date_from=date_from_real, date_to=date_to, corr_window=corr_window, corr_rank_num=corr_rank_num)
    if (dats is None) or (len(dats)==0): return None
    dats['year'] = dats.index.get_level_values('tdate')//10000
    dat_years = np.unique(dats['year'])
    for year in dat_years:
        path = output_dir+'label.'+str(year)+'.pkl'
        dat = dats[dats['year']==year].drop('year', axis=1)
        if os.path.exists(path):
            his = pd.read_pickle(path)
            his_dates = np.unique(his.index.get_level_values('tdate'))
            dat_dates = np.unique(dat.index.get_level_values('tdate'))
            his_dates_tokeep = sorted(set(his_dates)-set(dat_dates))
            print(his_dates_tokeep, dat_dates)
            dat = pd.concat([his.loc[his_dates_tokeep], dat], axis=0)
        dat = dat.sort_index()
        dat.to_pickle(path)
        print(dat, year, date_from, date_to)
    return None

#-- generate features theme
def gen_themecache(theme_set_name, date_from, date_to):
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    theme_names = theme_set[theme_set_name]
    theme_map = pd.DataFrame()
    for theme_name in theme_names:
        current_theme_dir = theme_dir+theme_name+'/'
        theme_dates = os.listdir(current_theme_dir)
        theme_dates = list(filter(lambda s: re.search(r'\d{8}.csv', s), theme_dates))
        theme_dates = sorted([int(s[(-12):(-4)]) for s in theme_dates])
        min_theme_date = min(theme_dates)
        max_theme_date = max(theme_dates)
        standard_dates = dates.loc[(dates['tdate']>=min_theme_date)&(dates['tdate']<=max_theme_date), 'tdate']
        # coverage = len(theme_dates)/len(standard_dates)
        theme_map = theme_map.append({'theme_name': theme_name, 'date_from': min_theme_date, 'date_to': max_theme_date,\
            'data_len': len(theme_dates)}, ignore_index=True)
    print(theme_map)
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    theme_dates = dates.loc[(dates['tdate']>=date_from)&(dates['tdate']<=date_to), 'tdate']
    theme_cache = pd.DataFrame()
    for theme_date in theme_dates:
        # tdate_idx = dates.index[dates['tdate']==theme_date]-1
        # tdate = int(dates.loc[tdate_idx, 'tdate'])
        # print(theme_date, tdate)
        for theme_name in theme_names:
            theme_path = theme_dir+theme_name+'/'+theme_name+'.'+str(theme_date)+'.csv'
            if not os.path.exists(theme_path): continue
            theme_data = pd.read_csv(theme_path, header=None, names=['id','sig'])
            theme_data['id'] = ['{0:0>6}'.format(s) for s in theme_data['id']]
            theme_data['theme'] = theme_name
            theme_data['tdate'] = theme_date # tdate
            theme_cache = pd.concat([theme_cache, theme_data], axis=0)
    if len(theme_cache)==0:
        return None
    dat = theme_cache.pivot_table(index=['tdate','id'], columns='theme', values='sig').fillna(0)
    return dat

def gen_themecache_byyear(theme_set_name='alpha', year=2013):
    output_dir = output_root+'data/themecache_{}/'.format(theme_set_name)
    ensure_dir(output_dir)
    date_from = year*10000+101
    date_to = year*10000+1231
    dats = gen_themecache(theme_set_name=theme_set_name, date_from=date_from, date_to=date_to)
    if dats is not None:
        dats.to_pickle(output_dir+'themecache.'+str(year)+'.pkl')
    return None

def gen_themecache_product(theme_set_name='alpha', date_from=20130101, date_to=20130131):
    #-- merge to output/data/
    output_dir = output_root+'data/themecache_{}/'.format(theme_set_name)
    ensure_dir(output_dir)
    dats = gen_themecache(theme_set_name=theme_set_name, date_from=date_from, date_to=date_to)
    dats['year'] = dats.index.get_level_values('tdate')//10000
    dat_years = np.unique(dats['year'])
    for year in dat_years:
        path = output_dir+'themecache.'+str(year)+'.pkl'
        dat = dats[dats['year']==year].drop('year', axis=1)
        if os.path.exists(path):
            his = pd.read_pickle(path)
            his_dates = np.unique(his.index.get_level_values('tdate'))
            dat_dates = np.unique(dat.index.get_level_values('tdate'))
            his_dates_tokeep = sorted(set(his_dates)-set(dat_dates))
            print(his_dates_tokeep, dat_dates)
            dat = pd.concat([his.loc[his_dates_tokeep], dat], axis=0)
        dat = dat.sort_index()
        dat.to_pickle(path)
        print(dat, year, date_from, date_to)
    return None



#-- generate features daybar
def gen_daybar_augstat(bar_type='daybar', feature_lag=30, begin_year=2002, end_year=2012):
    daybar_dir = output_root+'data/rawdata/{}/'.format(bar_type)
    output_dir = output_root+'data/{}_augstat_f{}/'.format(bar_type, feature_lag)
    ensure_dir(output_dir)
    lag_mult = lag_multipliers[bar_type]
    # years = [end_year-b for b in range(year_num, -1, -1)]
    years = range(begin_year, end_year+1)
    daybar_cache = pd.DataFrame()
    for year in years:
        daybar_path = daybar_dir+bar_type+'.'+str(year)+'.pkl'
        if not os.path.exists(daybar_path): continue
        daybar = pd.read_pickle(daybar_path)
        daybar = daybar.loc[:,['tdate','id']+daybar_features]
        daybar_cache = pd.concat([daybar_cache, daybar], axis=0)
        print(year)
    daybar_cache = daybar_cache.set_index(['tdate','id']).sort_index().dropna()
    #-- add lag features
    all_key_names = daybar_cache.columns.copy()
    for key_name in all_key_names:
        this_column = daybar_cache[key_name].unstack()
        for i in range(feature_lag):
            name = key_name+'_lag'+str(feature_lag-i-1)
            if i==(feature_lag-1):
                daybar_cache[name] = daybar_cache[key_name]
            else:
                daybar_cache[name] = this_column.shift((feature_lag-i-1)*lag_mult).stack()
        print(key_name)
    #-- rescaling
    # price_cols = [s+'_lag{}'.format(feature_lag-i-1) for s in price_key_names for i in range(feature_lag)]
    # price_base = daybar_cache['adjclose_lag0'].copy()
    # volume_cols = ['volume_lag'+str(feature_lag-i-1) for i in range(feature_lag)]
    # volume_base = daybar_cache[volume_cols].mean(axis=1)
    # amount_cols = ['amount_lag'+str(feature_lag-i-1) for i in range(feature_lag)]
    # amount_base = daybar_cache[amount_cols].mean(axis=1)
    # for key_name in all_key_names:
    #     if key_name=='turnover': continue
    #     for i in range(feature_lag): # 0:(feature_lag-1)
    #         name = key_name+'_lag'+str(feature_lag-i-1)
    #         if key_name in price_key_names:
    #             daybar_cache[name] /= price_base
    #         elif key_name=='volume':
    #             daybar_cache[name] /= volume_base
    #         elif key_name=='amount':
    #             daybar_cache[name] /= amount_base
    #     print(key_name)
    price_base = daybar_cache['adjclose_lag0'].copy()
    for key_name in price_key_names:
        for i in range(feature_lag):
            name = key_name+'_lag'+str(feature_lag-i-1)
            daybar_cache[name] /= price_base
        print(key_name)
    #-- calc stats
    daybar_cache = daybar_cache[daybar_cache['volume']>0].drop(all_key_names, axis=1) # turnover
    augstat = extract_augstat(daybar_cache)
    augstat.to_pickle(output_dir+'augstat.'+str(end_year)+'.pkl')
    augstat.to_csv(output_dir+'augstat.'+str(end_year)+'.csv')
    return None

def gen_daybar(bar_type, feature_lag, date_from, date_to, augstat_year=None):
    daybar_dir = output_root+'data/rawdata/{}/'.format(bar_type)
    #-- deal with date
    lag_mult = lag_multipliers[bar_type]
    dates = load_rdata(qdata_dir, 'info/trading_day').reset_index(drop=True)
    idx_from = max(dates.index[dates['tdate']<=date_from])
    idx_to = min(dates.index[dates['tdate']>=date_to])
    date_from_real = dates.loc[max(0, idx_from-feature_lag*lag_mult+1), 'tdate']
    print('date: from {} to {} -> from {} to {}'.format(date_from, date_to, date_from_real, date_to))
    #-- load aug stat
    augstat_dir = output_root+'data/{}_augstat_f{}/'.format(bar_type, feature_lag)
    if augstat_year is None:
        augstat_years = [int(f[-8:-4]) for f in os.listdir(augstat_dir) if f.endswith('.pkl')]
        year_from_real = int(date_from_real//10000)
        if year_from_real<=min(augstat_years):
            augstat_year = min(augstat_years)
        else:
            augstat_year = max([x for x in augstat_years if x<year_from_real])
    augstat_path = augstat_dir+'augstat.'+str(augstat_year)+'.pkl'
    augstat = pd.read_pickle(augstat_path)
    print(augstat, augstat_path)
    #-- load daybar cache
    daybar_cache = pd.DataFrame()
    for f in os.listdir(daybar_dir):
        file_year = int(f[(-8):(-4)])
        if (file_year<date_from_real//10000) or (file_year>date_to//10000): continue # don't use date_to_real
        daybar = pd.read_pickle(daybar_dir+f)
        daybar = daybar.loc[(daybar['tdate']>=date_from_real)&(daybar['tdate']<=date_to), ['tdate','id']+daybar_features]
        daybar_cache = pd.concat([daybar_cache, daybar], axis=0)
        print(file_year)
    daybar_cache = daybar_cache.set_index(['tdate','id']).sort_index().dropna()
    #-- add lag features
    all_key_names = daybar_cache.columns.copy()
    for key_name in all_key_names:
        this_column = daybar_cache[key_name].unstack()
        for i in range(feature_lag):
            name = key_name+'_lag'+str(feature_lag-i-1)
            if i==(feature_lag-1):
                daybar_cache[name] = daybar_cache[key_name]
            else:
                daybar_cache[name] = this_column.shift((feature_lag-i-1)*lag_mult).stack()
        print(key_name)
    #-- rescaling
    # price_cols = [s+'_lag{}'.format(feature_lag-i-1) for s in price_key_names for i in range(feature_lag)]
    # price_base = daybar_cache['adjclose_lag0'].copy()
    # volume_cols = ['volume_lag'+str(feature_lag-i-1) for i in range(feature_lag)]
    # volume_base = daybar_cache[volume_cols].mean(axis=1)
    # amount_cols = ['amount_lag'+str(feature_lag-i-1) for i in range(feature_lag)]
    # amount_base = daybar_cache[amount_cols].mean(axis=1)
    # for key_name in all_key_names:
    #     if key_name=='turnover': continue
    #     for i in range(feature_lag): # 0:(feature_lag-1)
    #         name = key_name+'_lag'+str(feature_lag-i-1)
    #         if key_name in price_key_names:
    #             daybar_cache[name] /= price_base
    #         elif key_name=='volume':
    #             daybar_cache[name] /= volume_base
    #         elif key_name=='amount':
    #             daybar_cache[name] /= amount_base
    #     print(key_name)
    price_base = daybar_cache['adjclose_lag0'].copy()
    for key_name in price_key_names:
        for i in range(feature_lag): # 0:(feature_lag-1)
            name = key_name+'_lag'+str(feature_lag-i-1)
            daybar_cache[name] /= price_base
        print(key_name)
    #-- apply aug
    daybar_cache = daybar_cache[daybar_cache['volume']>0].drop(all_key_names, axis=1)
    for key_name in daybar_cache.columns:
        u, l, m, s = augstat.loc[key_name].tolist()
        daybar_cache[key_name] = apply_augstat(daybar_cache[key_name], u, l, m, s)
    #-- output
    tdate_index = daybar_cache.index.get_level_values('tdate')
    daybar_cache = daybar_cache.loc[(tdate_index>=date_from)&(tdate_index<=date_to)]
    return daybar_cache

def gen_daybar_byyear(bar_type='daybar', feature_lag=30, year=2013, augstat_year=None):
    output_dir = output_root+'data/{}_f{}/'.format(bar_type, feature_lag)
    ensure_dir(output_dir)
    date_from = year*10000+101
    date_to = year*10000+1231
    dat = gen_daybar(bar_type=bar_type, feature_lag=feature_lag, date_from=date_from, date_to=date_to, augstat_year=augstat_year)
    if dat is not None:
        dat.to_pickle(output_dir+bar_type+'.'+str(year)+'.pkl')
    return None

def gen_daybar_product(bar_type='daybar', feature_lag=30, date_from=20130101, date_to=20130131, augstat_year=None):
    output_dir = output_root+'data_product/{}_f{}/'.format(bar_type, feature_lag)
    ensure_dir(output_dir)
    dat = gen_daybar(bar_type=bar_type, feature_lag=feature_lag, date_from=date_from, date_to=date_to, augstat_year=augstat_year)
    dat.to_pickle(output_dir+bar_type+'_product.pkl')
    print(dat, date_from, date_to)
    return None

#-- launcher: backtest
# update_rawdata_daybar(date_from=20020101, date_to=20241212)
# update_rawdata_weekbar(date_from=20020101, date_to=20241212)
# gen_daybar_augstat(bar_type='daybar', feature_lag=30, begin_year=2002, end_year=2012)
# gen_daybar_augstat(bar_type='weekbar', feature_lag=30, begin_year=2002, end_year=2012)
# for year in range(2003, 2025): # 2003, 2025
#     gen_label_byyear(year=year, label_shift=10, corr_window=240, corr_rank_num=30)
#     gen_daybar_byyear(bar_type='daybar', feature_lag=30, year=year, augstat_year=2012)
#     gen_daybar_byyear(bar_type='weekbar', feature_lag=30, year=year, augstat_year=2012)
#     gen_themecache_byyear(theme_set_name='alpha', year=year)
#     gen_themecache_byyear(theme_set_name='allsub1y', year=year)
# exit()

#-- launcher: retrain
if args.retrain>0:
    year = int(args.retrain)
    print(year)
    gen_daybar_byyear(bar_type='daybar', feature_lag=30, year=year, augstat_year=2012)
    gen_daybar_byyear(bar_type='weekbar', feature_lag=30, year=year, augstat_year=2012)
    exit()

#-- launcher: product
today = datetime.now()
date_from = int((today+timedelta(days=-11)).strftime('%Y%m%d'))
date_to = int((today+timedelta(days=-1)).strftime('%Y%m%d'))
print(date_from, date_to)
update_rawdata_daybar(date_from=date_from, date_to=date_to)
update_rawdata_weekbar(date_from=date_from, date_to=date_to)
gen_daybar_product(bar_type='daybar', feature_lag=30, date_from=date_from, date_to=date_to, augstat_year=2012)
gen_daybar_product(bar_type='weekbar', feature_lag=30, date_from=date_from, date_to=date_to, augstat_year=2012)
gen_themecache_product(theme_set_name='alpha', date_from=date_from, date_to=date_to)
gen_themecache_product(theme_set_name='allsub1y', date_from=date_from, date_to=date_to)
gen_label_product(label_shift=20, corr_window=240, corr_rank_num=30, date_from=date_from, date_to=date_to)
gen_label_product(label_shift=10, corr_window=240, corr_rank_num=30, date_from=date_from, date_to=date_to)
gen_label_product(label_shift=5, corr_window=240, corr_rank_num=30, date_from=date_from, date_to=date_to)

