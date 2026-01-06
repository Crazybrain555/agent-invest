#!/usr/bin/env python
#-*- utf-8 -*-

'''
Created on Jan 24 2022
@author: yuye zhang
@email: zhangyuye@bosera.com
'''



import h5py
#coding=utf-8
import sys,os,datetime
import numpy as np
import pandas as pd
# from numba import jit
# import warnings

import sys
import pymssql
import datetime

import time
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import json

# import statsmodels.api as sm
import statsmodels.formula.api as sm
# import csv
import pickle

import pandas as pd
import psycopg2
from numba import jit
from pandas.tseries.offsets import BDay
from sklearn.linear_model import LinearRegression
from tqdm import tqdm



def close_form_OLS(X, y):
    theta = np.matmul(np.matmul(np.linalg.inv(np.matmul(X.T, X)), X.T), y)
    resid = y - np.matmul(X, theta)
    return theta, resid

def windcode_to_id(windcode):
    return int(str(windcode).split('.')[0])


def setting_startdate_and_saving_path_dataframe(dir_path, doc_path, reload_tradedays):
    if reload_tradedays == 'all':
        start_date = int('20020101')
        start_date_halfyear = int('20010601')
    else:
        n = reload_tradedays
        start_date = int((datetime.date.today() - BDay(n)).strftime('%Y%m%d'))
        start_date_halfyear = int((datetime.date.today() - BDay(n + 125)).strftime('%Y%m%d'))

    # check the path exist or not
    if not os.path.exists('./{}/'.format(dir_path)):
        print('# {} path not exist , creating...... '.format(dir_path))
        os.makedirs('./{}/'.format(dir_path))
    else:
        print('{} has already existed '.format(dir_path))

    if not os.path.exists('./{}/{}'.format(dir_path, doc_path)):
        start_date = int('20020101')
        original_data = pd.DataFrame()
    else:
        original_data = pd.read_pickle('./{}/{}'.format(dir_path, doc_path))

    return start_date, original_data

def setting_startdate_and_saving_path_dictionary(dir_path, doc_path, reload_tradedays):
    if reload_tradedays == 'all':
        start_date = int('20020101')
        start_date_halfyear = int('20010601')
    else:
        n = reload_tradedays
        start_date = int((datetime.date.today() - BDay(n)).strftime('%Y%m%d'))
        start_date_halfyear = int((datetime.date.today() - BDay(n + 125)).strftime('%Y%m%d'))

    # check the path exist or not
    if not os.path.exists('./{}/'.format(dir_path)):
        print('# {} path not exist , creating...... '.format(dir_path))
        os.makedirs('./{}/'.format(dir_path))
    else:
        print('{} has already existed '.format(dir_path))

    if not os.path.exists('./{}/{}'.format(dir_path, doc_path)):
        start_date = int('20020101')
        original_data = {}
    else:
        original_data = pd.read_pickle('./{}/{}'.format(dir_path, doc_path))

    return start_date, original_data


def change_dataframe_windcode_to_id(dataframe, column_name='S_INFO_WINDCODE'):
    dataframe = dataframe[~ dataframe[column_name].str.contains('T')]
    dataframe = dataframe[~ dataframe[column_name].str.contains('BJ|A')]
    dataframe[column_name] = dataframe[column_name].apply(windcode_to_id)

    return dataframe

def merge_dict(dir1,dir2):
    dir2.update(dir1)
    return dir2

def save_doc_pickle(dataframe,dir_path,doc_path):
    fileHandle = open('./{}/{}'.format(dir_path, doc_path), 'wb')
    pickle.dump(dataframe, fileHandle)
    fileHandle.close()


class Get_data_fromdatabase():


    def __init__(self):
        # df_stk_pct_data.columns = df_stk_pct_data.columns.astype('str')
        # self.stk_pct_data = df_stk_pct_data
        # self.date = df_stk_pct_data.index
        print('data_processor_start')
        self.con_wind_db = pymssql.connect('v-wind', 'trade', 'trade', 'wind_quant',charset='cp936')
        self.con_gogoal_db = pymssql.connect('p-ma-mars', 'sig', 'sig', 'FundRiskControl2',charset='cp936')

        self.end_date = int(datetime.date.today().strftime('%Y%m%d'))
        self.tradedays_start=20020101
        sql = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' and TRADE_DAYS >={} " \
              "and TRADE_DAYS <={} order by TRADE_DAYS asc".format(self.tradedays_start,self.end_date)
        data = pd.read_sql(sql, self.con_wind_db)
        self.Tradedays_list = data['TRADE_DAYS'].tolist()

        size_sql = "select  distinct(S_INFO_WINDCODE) from  wind_quant.dbo.AShareEODPrices  " \
                           "where  TRADE_DT <={} and  TRADE_DT >={} and S_DQ_TRADESTATUSCODE=-1 ".format(self.end_date,self.tradedays_start)
        # print(sql)
        stk_data = pd.read_sql(size_sql, self.con_wind_db)
        stk_data = stk_data[~ stk_data['S_INFO_WINDCODE'].str.contains('T')]
        stk_data = stk_data[~ stk_data['S_INFO_WINDCODE'].str.contains('BJ')]
        stk_data = stk_data[~ stk_data['S_INFO_WINDCODE'].str.contains('A')]

        stk_data['S_INFO_WINDCODE']=stk_data['S_INFO_WINDCODE'].apply(windcode_to_id)
        #移除两个退市的
        stk_data=stk_data['S_INFO_WINDCODE'][~stk_data['S_INFO_WINDCODE'].isin([3,556])].dropna()
        self.stk_pool=stk_data .sort_values().values.tolist()



    def basic_financail_data_creator(self,reload_tradedays='all'):
        #𝑅𝑖𝑡 − 𝑅𝐹𝑡 = 𝛼 + 𝛽𝑖 (𝑅𝑀𝑡 − 𝑅𝐹𝑡 ) + 𝑠𝑖𝑆𝑀𝐵𝑡 + ℎ𝑖𝐻𝑀𝐿𝑂𝑡 + 𝑟𝑖𝑅𝑀𝑊𝑡 + 𝑐𝑖𝐶𝑀𝐴𝑡 + 𝜖𝑖𝑡
        end_date=self.end_date
        if reload_tradedays == 'all':
            start_date = int('20020101')
            start_date_halfyear=int('20010601')
        else:
            n = reload_tradedays
            start_date = int((datetime.date.today() - BDay(n)).strftime('%Y%m%d'))
            start_date_halfyear = int((datetime.date.today() - BDay(n+125)).strftime('%Y%m%d'))

        # check the path exist or not
        if not os.path.exists('./basic_factor_data/'):
            print('# basic_factor_data path not exist , creating...... ')
            os.makedirs('./basic_factor_data/')
        else:
            print('basic_factor_data has already existed ')


        if not os.path.exists('./basic_factor_data/pct_data_000300SH.pkl'):
            start_date = int('20020101')
            original_pct_data_000300SH=pd.DataFrame()
        else:
            original_pct_data_000300SH = pd.read_pickle('./basic_factor_data/pct_data_000300SH.pkl')

        # 𝑅𝑀𝑡 沪深三百000300.SH
        sql = "select S_INFO_WINDCODE,TRADE_DT,S_DQ_PCTCHANGE,S_DQ_CLOSE from wind_quant.dbo.AIndexEODPrices " \
              "where  s_info_windcode ='000300.SH' and  TRADE_DT>={} " \
              "and TRADE_DT<={} order by TRADE_DT asc ".format(start_date, end_date)
        pct_data_000300SH = pd.read_sql(sql, self.con_wind_db)
        # pct_data_000300SH = pct_data_000300SH[pct_data_000300SH['S_DQ_TRADESTATUS'] != '交易']
        # pct_data_000300SH = pct_data_000300SH[~ pct_data_000300SH['stk_code'].str.contains('T')]
        # pct_data_000300SH = pct_data_000300SH[~ pct_data_000300SH['stk_code'].str.contains('BJ')]

        pct_data_000300SH.sort_values('TRADE_DT', inplace=True)
        pct_data_000300SH = pd.pivot_table(pct_data_000300SH, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'],
                                      values=['S_DQ_PCTCHANGE'])

        pct_data_000300SH.columns = pct_data_000300SH.columns.get_level_values(1)
        # pct_data_000300SH = original_pct_data_000300SH.append(pct_data_000300SH)
        pct_data_000300SH = pd.concat([original_pct_data_000300SH,pct_data_000300SH])
        pct_data_000300SH = pct_data_000300SH[~pct_data_000300SH.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/pct_data_000300SH.pkl', 'wb')
        pickle.dump(pct_data_000300SH, fileHandle)
        fileHandle.close()

        print('pct_data_000300SH data is saved')

        if not os.path.exists('./basic_factor_data/pct_data_000905SH.pkl'):
            start_date = int('20020101')
            original_pct_data_000905SH = pd.DataFrame()
        else:
            original_pct_data_000905SH = pd.read_pickle('./basic_factor_data/pct_data_000905SH.pkl')


        # 𝑅𝑀𝑡 中证500  000905.SH
        sql = "select S_INFO_WINDCODE,TRADE_DT,S_DQ_PCTCHANGE,S_DQ_CLOSE from wind_quant.dbo.AIndexEODPrices " \
              "where  s_info_windcode ='000905.SH' and  TRADE_DT>={} " \
              "and TRADE_DT<={} order by TRADE_DT asc ".format(start_date, end_date)
        pct_data_000905SH = pd.read_sql(sql, self.con_wind_db)
        # pct_data_000300SH = pct_data_000300SH[pct_data_000300SH['S_DQ_TRADESTATUS'] != '交易']
        # pct_data_000300SH = pct_data_000300SH[~ pct_data_000300SH['stk_code'].str.contains('T')]
        # pct_data_000300SH = pct_data_000300SH[~ pct_data_000300SH['stk_code'].str.contains('BJ')]

        pct_data_000905SH.sort_values('TRADE_DT', inplace=True)
        pct_data_000905SH = pd.pivot_table(pct_data_000905SH, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'],
                                           values=['S_DQ_PCTCHANGE'])

        pct_data_000905SH.columns = pct_data_000905SH.columns.get_level_values(1)
        # pct_data_000905SH = original_pct_data_000905SH.append(pct_data_000905SH)
        pct_data_000905SH = pd.concat([original_pct_data_000905SH,pct_data_000905SH])
        pct_data_000905SH = pct_data_000905SH[~pct_data_000905SH.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/pct_data_000905SH.pkl', 'wb')
        pickle.dump(pct_data_000905SH, fileHandle)
        fileHandle.close()
        print('pct_data_000905SH data is saved')


        #𝑅𝐹𝑡 safe_return_rate
        if not os.path.exists('./basic_factor_data/national_bond_yiled5y.pkl'):
            start_date = int('20020101')
            original_national_bond_yiled5y = pd.DataFrame()
        else:
            original_national_bond_yiled5y = pd.read_pickle('./basic_factor_data/national_bond_yiled5y.pkl')


        yield5y_sql = "select TRADE_DT,B_ANAL_CURVENUMBER,B_ANAL_CURVETYPE,B_ANAL_CURVETERM,B_ANAL_YIELD from wind_quant.dbo.CBondCurveCNBD " \
              "where  B_ANAL_CURVENUMBER=1232 and B_ANAL_CURVETERM=5 and  TRADE_DT>={} " \
              "and TRADE_DT<={} order by TRADE_DT asc ".format(start_date, end_date)

        national_bond_yiled5y = pd.read_sql(yield5y_sql, self.con_wind_db)


        #FILL the empyty data between 20171101 and 20171231 by front value

        sql_TRADE_DAYS = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' " \
                         "and  TRADE_DAYS>={} and TRADE_DAYS<={} order by TRADE_DAYS asc".format(start_date, end_date)
        # print(sql_TRADE_DAYS)
        trade_date_data = pd.read_sql(sql_TRADE_DAYS, self.con_wind_db)  # trade dates from wind
        # trade_date_data_array = trade_date_data.values.astype(int)

        national_bond_yiled5y=pd.merge(left=trade_date_data,right=national_bond_yiled5y,left_on=['TRADE_DAYS'],
                                       right_on=['TRADE_DT'],how='left')

        national_bond_yiled5y['TRADE_DT']=national_bond_yiled5y['TRADE_DAYS']
        national_bond_yiled5y = national_bond_yiled5y.fillna(method='ffill')

        national_bond_yiled5y.sort_values('TRADE_DT', inplace=True)
        national_bond_yiled5y = pd.pivot_table(national_bond_yiled5y, index=['TRADE_DT'], columns=['B_ANAL_CURVENUMBER'],
                                           values=['B_ANAL_YIELD'])

        national_bond_yiled5y.columns = national_bond_yiled5y.columns.get_level_values(1)
        # national_bond_yiled5y = original_national_bond_yiled5y.append(national_bond_yiled5y)
        national_bond_yiled5y=pd.concat([original_national_bond_yiled5y,national_bond_yiled5y])
        national_bond_yiled5y = national_bond_yiled5y[~national_bond_yiled5y.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/national_bond_yiled5y.pkl', 'wb')
        pickle.dump(national_bond_yiled5y, fileHandle)
        fileHandle.close()
        print('national_bond_yiled5y data is saved')



        #stk_pct_data
        if not os.path.exists('./basic_factor_data/stk_pct_data.pkl'):
            start_date = int('20020101')
            original_stk_pct_data = pd.DataFrame()
        else:
            original_stk_pct_data = pd.read_pickle('./basic_factor_data/stk_pct_data.pkl')

        if not os.path.exists('./basic_factor_data/stk_adjprice_data.pkl'):
            start_date = int('20020101')
            original_stk_adjpct_data = pd.DataFrame()
        else:
            original_stk_adjpct_data = pd.read_pickle('./basic_factor_data/stk_adjprice_data.pkl')

        sql_stk_pct_data = "select S_INFO_WINDCODE,TRADE_DT,S_DQ_PCTCHANGE as stk_pct,S_DQ_ADJCLOSE as adj_price,S_DQ_TRADESTATUSCODE " \
                           " from wind_quant.dbo.AShareEODPrices WHERE " \
                           "  TRADE_DT <={}  and  TRADE_DT >={} order by TRADE_DT asc".format(end_date, start_date)

        # print(sql_stk_pct_data)
        stk_price_data = pd.read_sql(sql_stk_pct_data, self.con_wind_db)

        stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('T')]
        stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('BJ')]
        stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('A')]
        stk_price_data = stk_price_data[stk_price_data['S_DQ_TRADESTATUSCODE'] == -1]
        #去掉.后面的东西，方便对其,str格式
        stk_price_data['S_INFO_WINDCODE'] = stk_price_data['S_INFO_WINDCODE'].apply(windcode_to_id)

        stk_pct_data = pd.pivot_table(stk_price_data, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'], values=['stk_pct'])
        stk_pct_data.columns = stk_pct_data.columns.get_level_values(1)
        stk_pct_data = original_stk_pct_data._append(stk_pct_data)
        stk_pct_data = stk_pct_data[~stk_pct_data.index.duplicated('last')]

        stk_adjpct_data = pd.pivot_table(stk_price_data, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'], values=['adj_price'])
        stk_adjpct_data.columns = stk_adjpct_data.columns.get_level_values(1)
        stk_adjpct_data = original_stk_adjpct_data._append(stk_adjpct_data)
        stk_adjpct_data = stk_adjpct_data[~stk_adjpct_data.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/stk_pct_data.pkl', 'wb')
        pickle.dump(stk_pct_data, fileHandle)
        fileHandle.close()


        fileHandle = open('./basic_factor_data/stk_adjprice_data.pkl', 'wb')
        pickle.dump(stk_adjpct_data, fileHandle)
        fileHandle.close()

        print('stk_pct_data stk_adjpct_data data is saved')

        #stk_pct_data,size_data,BM_data,profit_data,investment_data
        #size_data

        if not os.path.exists('./basic_factor_data/stk_size_data.pkl'):
            start_date = int('20020101')
            original_stk_size_data = pd.DataFrame()
        else:
            original_stk_size_data = pd.read_pickle('./basic_factor_data/stk_size_data.pkl')

        if not os.path.exists('./basic_factor_data/stk_BM_data.pkl'):
            start_date = int('20020101')
            original_stk_BM_data = pd.DataFrame()
        else:
            original_stk_BM_data = pd.read_pickle('./basic_factor_data/stk_BM_data.pkl')

        sql = """
        select 
            S_INFO_WINDCODE,
            TRADE_DT,
            S_VAL_MV as market_value,
            1 / NULLIF(S_VAL_PB_NEW, 0) as BM,
            UP_DOWN_LIMIT_STATUS 
        from 
            wind_quant.dbo.AShareEODDerivativeIndicator  
        where 
            TRADE_DT <= {} and TRADE_DT >= {} 
        order by 
            TRADE_DT asc
        """.format(end_date, start_date)

        # print(sql)
        stk_BM_SIZE_data = pd.read_sql(sql, self.con_wind_db)

        stk_BM_SIZE_data = stk_BM_SIZE_data[~ stk_BM_SIZE_data['S_INFO_WINDCODE'].str.contains('T')]
        stk_BM_SIZE_data = stk_BM_SIZE_data[~ stk_BM_SIZE_data['S_INFO_WINDCODE'].str.contains('BJ')]
        #去掉.后面的东西，方便对其,str格式
        stk_BM_SIZE_data['S_INFO_WINDCODE'] = stk_BM_SIZE_data['S_INFO_WINDCODE'].apply(windcode_to_id)


        stk_size_data = pd.pivot_table(stk_BM_SIZE_data, index=['TRADE_DT'],
                                                             columns=['S_INFO_WINDCODE'], values=['market_value'])
        stk_BM_data = pd.pivot_table(stk_BM_SIZE_data, index=['TRADE_DT'],
                                                      columns=['S_INFO_WINDCODE'], values=['BM'])

        # stk_size_data.index = pd.to_datetime(stk_size_data.index)
        # stk_BM_data.index = pd.to_datetime(stk_BM_data.index)

        stk_size_data.columns = stk_size_data.columns.get_level_values(1)
        stk_BM_data.columns = stk_BM_data.columns.get_level_values(1)
        # # stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(dataframe_daily_BM_indicator.columns)]
        stk_size_data = original_stk_size_data._append(stk_size_data)
        stk_size_data = stk_size_data[~stk_size_data.index.duplicated('last')]

        stk_BM_data = original_stk_BM_data._append(stk_BM_data)
        stk_BM_data = stk_BM_data[~stk_BM_data.index.duplicated('last')]


        fileHandle = open('./basic_factor_data/stk_size_data.pkl', 'wb')
        pickle.dump(stk_size_data, fileHandle)
        fileHandle.close()

        fileHandle = open('./basic_factor_data/stk_BM_data.pkl', 'wb')
        pickle.dump(stk_BM_data, fileHandle)
        fileHandle.close()

        print('stk_size_data and stk_BM_data data is saved')


        #profit_data
        if not os.path.exists('./basic_factor_data/stk_ROE_data.pkl'):
            start_date = int('20020101')
            original_stk_ROE_data = pd.DataFrame()
        else:
            original_stk_ROE_data = pd.read_pickle('./basic_factor_data/stk_ROE_data.pkl')

        sql_daily_net_assets = "select  S_INFO_WINDCODE,TRADE_DT,NET_ASSETS_TODAY as net_assets,NET_PROFIT_PARENT_COMP_TTM " \
                               "from wind_quant.dbo.AShareEODDerivativeIndicator  " \
                               "where    TRADE_DT <={} and  TRADE_DT >={} order by TRADE_DT asc".format(end_date,
                                                                                                        start_date)

        # print(sql_daily_net_assets)
        dataframe_net_assets = pd.read_sql(sql_daily_net_assets, self.con_wind_db)
        dataframe_net_assets = dataframe_net_assets[~ dataframe_net_assets['S_INFO_WINDCODE'].str.contains('T')]
        dataframe_net_assets = dataframe_net_assets[~ dataframe_net_assets['S_INFO_WINDCODE'].str.contains('BJ')]

        dataframe_net_assets['S_INFO_WINDCODE'] = dataframe_net_assets['S_INFO_WINDCODE'].apply(windcode_to_id)

        dataframe_net_assets = dataframe_net_assets[dataframe_net_assets['net_assets']>100000000]
        # dataframe_net_assets.replace(0, np.nan, inplace=True)  # 把分母是0的去掉 不然报错
        dataframe_net_assets['ROE'] = dataframe_net_assets['NET_PROFIT_PARENT_COMP_TTM'] / dataframe_net_assets[
            'net_assets']
        dataframe_roe_assets = pd.pivot_table(dataframe_net_assets, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'],
                                              values=['ROE'])
        dataframe_roe_assets.index = pd.to_datetime(dataframe_roe_assets.index)
        dataframe_roe_assets.columns = dataframe_roe_assets.columns.get_level_values(1)

        dataframe_roe_assets = original_stk_ROE_data._append(dataframe_roe_assets)
        dataframe_roe_assets = dataframe_roe_assets[~dataframe_roe_assets.index.duplicated('last')]
        dataframe_roe_assets.index = pd.to_datetime(dataframe_roe_assets.index)
        dataframe_roe_assets.index = dataframe_roe_assets.index.strftime('%Y%m%d')

        fileHandle = open('./basic_factor_data/stk_ROE_data.pkl', 'wb')
        pickle.dump(dataframe_roe_assets, fileHandle)
        fileHandle.close()
        print('stk_ROE_data  data is saved')



        # assets growth rate
        if not os.path.exists('./basic_factor_data/stk_growthrate_data.pkl'):
            start_date = int('20010101')
            original_stk_growthrate_data = pd.DataFrame()
        else:
            original_stk_growthrate_data = pd.read_pickle('./basic_factor_data/stk_growthrate_data.pkl')

        sql_assets_value = "select S_INFO_WINDCODE ,ANN_DT,REPORT_PERIOD,STATEMENT_TYPE,TOT_ASSETS from wind_quant.dbo.AShareBalanceSheet " \
                           "where  STATEMENT_TYPE='408001000'  and  ANN_DT <={}  and  ANN_DT >={} order by S_INFO_WINDCODE," \
                           "ANN_DT,REPORT_PERIOD  asc".format(end_date, start_date_halfyear)

        # print(sql_assets_value)
        df_assets_value = pd.read_sql(sql_assets_value, self.con_wind_db)

        sql_TRADE_DAYS = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' " \
                         " and  TRADE_DAYS <={}  and  TRADE_DAYS >={}  order by TRADE_DAYS asc".format(end_date, start_date)
        trade_date_data = pd.read_sql(sql_TRADE_DAYS, self.con_wind_db)
        trade_date_data_array=trade_date_data['TRADE_DAYS'].tolist()
        # trade_date_data_array = trade_date_data.values.astype(int)



        df_assets_value.drop_duplicates(subset=['S_INFO_WINDCODE', 'ANN_DT'], keep='last', inplace=True)

        df_assets_value = df_assets_value[~ df_assets_value['S_INFO_WINDCODE'].str.contains('T')]
        df_assets_value = df_assets_value[~ df_assets_value['S_INFO_WINDCODE'].str.contains('BJ|A')]
        df_assets_value['S_INFO_WINDCODE'] = df_assets_value['S_INFO_WINDCODE'].apply(windcode_to_id)
        # use for loop directly
        df_assets_value['ASS_GRW_RATE'] = np.nan
        for i in tqdm(range(len(df_assets_value['TOT_ASSETS']) - 1)):
            if df_assets_value.iloc[i + 1, 0] == df_assets_value.iloc[i, 0]:
                df_assets_value.iloc[i + 1, 5] = df_assets_value.iloc[i + 1, 4] / df_assets_value.iloc[i, 4] - 1




        df_assets_value = pd.pivot_table(df_assets_value, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=[
            'ASS_GRW_RATE'])  # pivpt the dataframe time is the indexs and the stk are the columns
        df_assets_value.index = pd.to_datetime(df_assets_value.index)
        df_assets_value.columns = df_assets_value.columns.get_level_values(1)
        df_assets_value = df_assets_value.fillna(method='ffill')
        df_assets_value = df_assets_value.reindex(
            pd.date_range(df_assets_value.index[0], df_assets_value.index[-1], freq='D'), method='ffill') #fill the empty date data with dates

        df_assets_value.index=df_assets_value.index.strftime('%Y%m%d')

        df_assets_value=df_assets_value[df_assets_value.index.isin(trade_date_data_array)]   # use the trade_data

        df_assets_value = original_stk_growthrate_data._append(df_assets_value)
        df_assets_value = df_assets_value[~df_assets_value.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/stk_growthrate_data.pkl', 'wb')
        pickle.dump(df_assets_value, fileHandle)
        fileHandle.close()



        print('stk_growthrate_data  data is saved')

    # growth_rate_data
    def get_valuation_data_creator(self, reload_tradedays='all'):
        # Determine start date based on reload_tradedays
        if reload_tradedays == 'all':
            start_date = int('20020101')
        else:
            n = reload_tradedays
            start_date = int((datetime.date.today() - BDay(n)).strftime('%Y%m%d'))

        # Check if basic_factor_data path exists, create if not
        if not os.path.exists('./basic_factor_data/'):
            print('# basic_factor_data path not exist , creating...... ')
            os.makedirs('./basic_factor_data/')
        else:
            print('basic_factor_data has already existed ')

        # Create AShareValuationIndicator folder
        valuation_folder = './basic_factor_data/AShareValuationIndicator'
        if not os.path.exists(valuation_folder):
            os.makedirs(valuation_folder)
            print('AShareValuationIndicator folder created.')

        # Check if previous data exists for S_VAL_DIVIDENDYIELD2
        file_path = './basic_factor_data/AShareValuationIndicator/S_VAL_DIVIDENDYIELD2_data.pkl'
        if os.path.exists(file_path):
            original_data = pd.read_pickle(file_path)
        else:
            original_data = pd.DataFrame()

        # Query to fetch the data for S_VAL_DIVIDENDYIELD2
        sql = """
            SELECT S_INFO_WINDCODE, TRADE_DT, S_VAL_DIVIDENDYIELD2 
            FROM wind_quant.dbo.AShareValuationIndicator 
            WHERE TRADE_DT >= {} AND TRADE_DT <= {} 
            ORDER BY TRADE_DT ASC
        """.format(start_date, self.end_date)

        dividend_yield_data = pd.read_sql(sql, self.con_wind_db)
        dividend_yield_data.sort_values('TRADE_DT', inplace=True)

        dividend_yield_data = dividend_yield_data[~ dividend_yield_data['S_INFO_WINDCODE'].str.contains('T')]
        dividend_yield_data = dividend_yield_data[~ dividend_yield_data['S_INFO_WINDCODE'].str.contains('BJ')]
        dividend_yield_data = dividend_yield_data[~ dividend_yield_data['S_INFO_WINDCODE'].str.contains('A')]

        #去掉.后面的东西，方便对其,str格式
        dividend_yield_data['S_INFO_WINDCODE'] = dividend_yield_data['S_INFO_WINDCODE'].apply(windcode_to_id)

        # Pivot the data
        dividend_yield_data = pd.pivot_table(
            dividend_yield_data, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'], values=['S_VAL_DIVIDENDYIELD2']
        )

        # Clean up column names
        dividend_yield_data.columns = dividend_yield_data.columns.get_level_values(1)

        # Concatenate the original data with the new data (if any)
        updated_data = pd.concat([original_data, dividend_yield_data])
        updated_data = updated_data[~updated_data.index.duplicated('last')]  # Remove duplicate rows

        # Save the updated data to a pickle file
        with open(file_path, 'wb') as fileHandle:
            pickle.dump(updated_data, fileHandle)

        print(f'S_VAL_DIVIDENDYIELD2 data is saved to {file_path}')





    def get_wind_index_885000_WI(self, reload_tradedays='all'):
        end_date = self.end_date

        start_date,original_pct_data_885000WI=setting_startdate_and_saving_path_dataframe('basic_factor_data','pct_data_885000WI.pkl',reload_tradedays)

        # 与普通股票型基金指数885000.WI
        sql = "select S_INFO_WINDCODE,TRADE_DT,S_DQ_PCTCHANGE,S_DQ_CLOSE from wind_quant.dbo.CMFIndexEOD " \
              "where  s_info_windcode ='885000.WI' and  TRADE_DT>={} " \
              "and TRADE_DT<={} order by TRADE_DT asc ".format(start_date, end_date)
        pct_data_885000WI = pd.read_sql(sql, self.con_wind_db)
        # pct_data_000300SH = pct_data_000300SH[pct_data_000300SH['S_DQ_TRADESTATUS'] != '交易']
        # pct_data_000300SH = pct_data_000300SH[~ pct_data_000300SH['stk_code'].str.contains('T')]
        # pct_data_000300SH = pct_data_000300SH[~ pct_data_000300SH['stk_code'].str.contains('BJ')]

        pct_data_885000WI.sort_values('TRADE_DT', inplace=True)
        pct_data_885000WI = pd.pivot_table(pct_data_885000WI, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'],
                                           values=['S_DQ_PCTCHANGE'])
        pct_data_885000WI.columns = pct_data_885000WI.columns.get_level_values(1)
        pct_data_885000WI = pd.concat([original_pct_data_885000WI, pct_data_885000WI])
        pct_data_885000WI = pct_data_885000WI[~pct_data_885000WI.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/pct_data_885000WI.pkl', 'wb')
        pickle.dump(pct_data_885000WI, fileHandle)
        fileHandle.close()

        print('pct_data_885000WI data is saved')

    def get_wind_index_980092_CNI(self, reload_tradedays='all'):
        end_date = self.end_date

        start_date, original_pct_data_980092CNI = setting_startdate_and_saving_path_dataframe(
            'basic_factor_data', 'pct_data_980092CNI.pkl', reload_tradedays)

        # 与普通股票型基金指数980092.CNI
        sql = "select S_INFO_WINDCODE, TRADE_DT, S_DQ_PCTCHANGE, S_DQ_CLOSE from wind_quant.dbo.AIndexEODPrices " \
              "where s_info_windcode = '980092.CNI' and TRADE_DT >= {} " \
              "and TRADE_DT <= {} order by TRADE_DT asc".format(start_date, end_date)
        pct_data_980092CNI = pd.read_sql(sql, self.con_wind_db)

        pct_data_980092CNI.sort_values('TRADE_DT', inplace=True)
        pct_data_980092CNI = pd.pivot_table(pct_data_980092CNI, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'],
                                            values=['S_DQ_CLOSE'])
        pct_data_980092CNI.columns = pct_data_980092CNI.columns.get_level_values(1)
        pct_data_980092CNI = pd.concat([original_pct_data_980092CNI, pct_data_980092CNI])
        pct_data_980092CNI = pct_data_980092CNI[~pct_data_980092CNI.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/price_data_980092CNI.pkl', 'wb')
        pickle.dump(pct_data_980092CNI, fileHandle)
        fileHandle.close()

        print('pct_data_980092CNI data is saved')




    #中国A股指数成份股 dataframe是交易日，指数名称，股票代码（int格式）
    def get_IndexMembers(self,reload_tradedays='all'):

        def save_index_members(index_id):
            def Seeking_members_at_certain_date(dataframe, date):
                date = int(date)
                tmp_df = dataframe[((dataframe['S_CON_INDATE'] <= date) & (dataframe['S_CON_OUTDATE'] > date)) | (
                            (dataframe['S_CON_INDATE'] <= date) & (dataframe['S_CON_OUTDATE'].isna()))]
                return [] if len(tmp_df['S_CON_WINDCODE']) == 0 else tmp_df['S_CON_WINDCODE'].sort_values().tolist()

            end_date = self.end_date
            # index index_id
            start_date, original_indexmembers_data = setting_startdate_and_saving_path_dictionary(
                'basic_factor_data/IndexMembers', '{}.pkl'.format(index_id), reload_tradedays)
            # 提取之前的交易日 方便做for循环
            Tradedays_intlist = list(map(int, self.Tradedays_list))
            # 取所有沪深300的所有时期的成分股，再把日期变成int格式
            sql = "select S_INFO_WINDCODE,S_CON_WINDCODE,S_CON_INDATE,S_CON_OUTDATE,CUR_SIGN from wind_quant.dbo.AIndexMembers " \
                  "where  s_info_windcode in ('{}')  order by S_CON_INDATE asc ".format(index_id)
            total_indexmembers = pd.read_sql(sql, self.con_wind_db)
            total_indexmembers = total_indexmembers[
                total_indexmembers['S_INFO_WINDCODE'] ==index_id]
            # 把纳入的股票代码从str改成int
            total_indexmembers = change_dataframe_windcode_to_id(total_indexmembers, 'S_CON_WINDCODE')
            total_indexmembers['S_CON_INDATE'] = total_indexmembers['S_CON_INDATE'].astype('int')
            total_indexmembers['S_CON_OUTDATE'] = total_indexmembers['S_CON_OUTDATE'].apply(
                lambda x: x if pd.isna(x) else int(x))

            # data初始值
            new_INDEX_Members = {}  # 尝试用字典dic[tdate][index]
            new_INDEX_Members[index_id] = {}
            for date in tqdm(self.Tradedays_list[self.Tradedays_list.index('20050104'):]):  # 从2005年开始 因为05年之前没有这些指数
                new_INDEX_Members[index_id][date] = Seeking_members_at_certain_date(
                    total_indexmembers, date)

            INDEX_Members = merge_dict(new_INDEX_Members, original_indexmembers_data)

            fileHandle = open('./basic_factor_data/IndexMembers/{}.pkl'.format(index_id), 'wb')
            pickle.dump(INDEX_Members, fileHandle)
            fileHandle.close()
            print('INDEX_{}_Members data is saved'.format(index_id))

        save_index_members('000300.SH')
        save_index_members('000905.SH')
        save_index_members('000852.SH')
        save_index_members('980092.CNI')



    def get_Ashare_daliy_derivative_financial_indicators(self, reload_tradedays='all'):
        end_date = self.end_date
        start_date,original_PE=setting_startdate_and_saving_path_dataframe\
            ('basic_factor_data/Ashare_daliy_derivative_financial_indicators','stk_PE.pkl',reload_tradedays)
        start_date,original_PE_TTM=setting_startdate_and_saving_path_dataframe\
            ('basic_factor_data/Ashare_daliy_derivative_financial_indicators','stk_PE_TTM.pkl',reload_tradedays)




        sql = "select S_INFO_WINDCODE,TRADE_DT,S_VAL_PE,S_VAL_PE_TTM from wind_quant.dbo.AShareEODDerivativeIndicator " \
              "where  TRADE_DT>={} and TRADE_DT<={} order by TRADE_DT asc ".format(start_date, end_date)
        PE_DATA = pd.read_sql(sql, self.con_wind_db)

        PE_DATA = PE_DATA[~ PE_DATA['S_INFO_WINDCODE'].str.contains('T')]
        PE_DATA = PE_DATA[~ PE_DATA['S_INFO_WINDCODE'].str.contains('BJ')]
        #去掉.后面的东西，方便对其,str格式
        PE_DATA['S_INFO_WINDCODE'] = PE_DATA['S_INFO_WINDCODE'].apply(windcode_to_id)


        Daily_PE_DATA = pd.pivot_table(PE_DATA, index=['TRADE_DT'],
                                                             columns=['S_INFO_WINDCODE'], values=['S_VAL_PE'])
        Daily_PE_TTM_DATA = pd.pivot_table(PE_DATA, index=['TRADE_DT'],
                                                      columns=['S_INFO_WINDCODE'], values=['S_VAL_PE_TTM'])


        Daily_PE_DATA.columns = Daily_PE_DATA.columns.get_level_values(1)
        Daily_PE_TTM_DATA.columns = Daily_PE_TTM_DATA.columns.get_level_values(1)
        # # stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(dataframe_daily_BM_indicator.columns)]
        Daily_PE_DATA = original_PE._append(Daily_PE_DATA)
        Daily_PE_DATA = Daily_PE_DATA[~Daily_PE_DATA.index.duplicated('last')]

        Daily_PE_TTM_DATA = original_PE_TTM._append(Daily_PE_TTM_DATA)
        Daily_PE_TTM_DATA = Daily_PE_TTM_DATA[~Daily_PE_TTM_DATA.index.duplicated('last')]


        fileHandle = open('./basic_factor_data/Ashare_daliy_derivative_financial_indicators/stk_PE.pkl', 'wb')
        pickle.dump(Daily_PE_DATA, fileHandle)
        fileHandle.close()

        fileHandle = open('./basic_factor_data/Ashare_daliy_derivative_financial_indicators/stk_PE_TTM.pkl', 'wb')
        pickle.dump(Daily_PE_TTM_DATA, fileHandle)
        fileHandle.close()

        print('PE and PETTM data is saved')



    def get_HK_stk_basic(self, reload_tradedays='all'):
        end_date = self.end_date

        start_date,original_stk_pct_data=setting_startdate_and_saving_path_dataframe\
            ('HK_basic_factor_data','HK_stk_pct.pkl',reload_tradedays)
        start_date,original_adjprice_data=setting_startdate_and_saving_path_dataframe\
            ('HK_basic_factor_data','HK_stk_adjprice.pkl',reload_tradedays)



        sql_stk_pct_data = "select S_INFO_WINDCODE,TRADE_DT,(S_DQ_ADJCLOSE/S_DQ_ADJPRECLOSE-1)*100 as stk_pct ,S_DQ_ADJCLOSE as adj_price  " \
                           " from wind_quant.dbo.HKshareEODPrices WHERE  S_DQ_ADJPRECLOSE!=0  and" \
                           "  TRADE_DT <={}  and  TRADE_DT >={} order by TRADE_DT asc".format(end_date, start_date)

        # print(sql_stk_pct_data)
        stk_price_data = pd.read_sql(sql_stk_pct_data, self.con_wind_db)

        # stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('T')]
        # stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('BJ')]
        # stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('A')]
        # stk_price_data = stk_price_data[stk_price_data['S_DQ_TRADESTATUSCODE'] == -1]
        #去掉.后面的东西，方便对其,str格式
        # stk_price_data['S_INFO_WINDCODE'] = stk_price_data['S_INFO_WINDCODE'].apply(windcode_to_id)

        stk_pct_data = pd.pivot_table(stk_price_data, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'], values=['stk_pct'])
        stk_pct_data.columns = stk_pct_data.columns.get_level_values(1)
        stk_pct_data = original_stk_pct_data._append(stk_pct_data)


        stk_pct_data = stk_pct_data[~stk_pct_data.index.duplicated('last')]

        stk_adjpct_data = pd.pivot_table(stk_price_data, index=['TRADE_DT'], columns=['S_INFO_WINDCODE'], values=['adj_price'])
        stk_adjpct_data.columns = stk_adjpct_data.columns.get_level_values(1)
        stk_adjpct_data = original_adjprice_data._append(stk_adjpct_data)
        stk_adjpct_data = stk_adjpct_data[~stk_adjpct_data.index.duplicated('last')]

        save_doc_pickle(stk_pct_data,'HK_basic_factor_data','HK_stk_pct.pkl')
        save_doc_pickle(stk_adjpct_data,'HK_basic_factor_data','HK_stk_adjprice.pkl')



    #中债登债券收益率曲线[CBondCurveCNBD]
    def get_wind_CBondCurveCNBD(self, reload_tradedays='all'):
        end_date = self.end_date
        start_date,original_Enterprise_Bond_10years_yield=\
            setting_startdate_and_saving_path_dataframe('basic_factor_data/CBondCurveCNBD','Enterprise_Bond_10years_yield.pkl',reload_tradedays)

        Bond_rating_dict= {1442:'AA+',2242:'AAA-',1252:'AA',1912:'BBB+',2172:'A-',1852:'A+',1262:'AAA',2252:'AA-',1902:'A'}


        # 中债企业债收益率曲线(10年)
        sql = "select TRADE_DT,B_ANAL_CURVENUMBER,B_ANAL_CURVENAME,B_ANAL_CURVETYPE,B_ANAL_CURVETERM,B_ANAL_YIELD from wind_quant.dbo.CBondCurveCNBD " \
              "where  B_ANAL_CURVETERM=10 and B_ANAL_CURVETYPE=2 and  B_ANAL_CURVENUMBER in (1442,2242,1252,1912,2172,1852,1262,2252,1902)  and  TRADE_DT>={} " \
              "and TRADE_DT<={} order by TRADE_DT asc ".format(start_date, end_date)
        Enterprise_Bond_10years_yield = pd.read_sql(sql, self.con_wind_db)

        Enterprise_Bond_10years_yield.sort_values('TRADE_DT', inplace=True)
        Enterprise_Bond_10years_yield = pd.pivot_table(Enterprise_Bond_10years_yield, index=['TRADE_DT'], columns=['B_ANAL_CURVENUMBER'],
                                           values=['B_ANAL_YIELD'])
        Enterprise_Bond_10years_yield.columns = Enterprise_Bond_10years_yield.columns.get_level_values(1)
        #把columns的数字列名改成字符串 如AA AAA AA- A+
        column_names=list(Bond_rating_dict[x] for x in Enterprise_Bond_10years_yield.columns)
        Enterprise_Bond_10years_yield.columns=column_names
        Enterprise_Bond_10years_yield=Enterprise_Bond_10years_yield[['AAA','AAA-','AA+','AA','AA-','A+','A','A-','BBB+']]

        # 和tradedays 统一防止出现数据缺失
        # 先确立起始值
        tmp_start_date=Enterprise_Bond_10years_yield.index[0]
        for date in Enterprise_Bond_10years_yield.index:
            if date in self.Tradedays_list:
                tmp_start_date=date
                break
        Enterprise_Bond_10years_yield=Enterprise_Bond_10years_yield.reindex(index=self.Tradedays_list[self.Tradedays_list.index(tmp_start_date):])
        Enterprise_Bond_10years_yield=Enterprise_Bond_10years_yield.fillna(method='ffill')



        Enterprise_Bond_10years_yield = original_Enterprise_Bond_10years_yield._append(Enterprise_Bond_10years_yield)
        Enterprise_Bond_10years_yield = Enterprise_Bond_10years_yield[~Enterprise_Bond_10years_yield.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/CBondCurveCNBD/Enterprise_Bond_10years_yield.pkl', 'wb')
        pickle.dump(Enterprise_Bond_10years_yield, fileHandle)
        fileHandle.close()

        print('Enterprise_Bond_10years_yield data is saved')



    #自由现金流free cash flow FCF_TTM
    def get_wind_FCF_DATA(self, reload_tradedays='all'):
        end_date = self.end_date
        start_date,original_FCF_TTM=\
            setting_startdate_and_saving_path_dataframe('basic_factor_data/Ashare_daliy_derivative_financial_indicators/FCF','FCF_ALL.pkl',reload_tradedays)


        setting_startdate_and_saving_path_dataframe('basic_factor_data/Ashare_daliy_derivative_financial_indicators/FCF','FCF_ADJ_3YTTM_pivot.pkl',reload_tradedays)



        # 先算自由现金流的TTM
        sql = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD,STATEMENT_TYPE,FREE_CASH_FLOW from wind_quant.dbo.AShareCashFlow " \
              "where STATEMENT_TYPE=408001000 and ANN_DT >={} " \
              "and ANN_DT <={} order by ANN_DT asc".format(start_date, end_date)
        S_FA_FCFF = pd.read_sql(sql, self.con_wind_db)
        S_FA_FCFF = S_FA_FCFF.sort_values(['ANN_DT', 'S_INFO_WINDCODE'])
        S_FA_FCFF = S_FA_FCFF[~ S_FA_FCFF['S_INFO_WINDCODE'].str.contains('T')]
        S_FA_FCFF = S_FA_FCFF[~ S_FA_FCFF['S_INFO_WINDCODE'].str.contains('BJ|A')]

        def Calculate_ADJ_2YTTM(dataframe):
            dataframe=dataframe.dropna().sort_values()
            dataframe.iloc[0]=dataframe.iloc[1]
            dataframe.iloc[-1] = dataframe.iloc[-2]

            value=dataframe.sum()/len(dataframe)*4

            return value

        def Calculate_ADJ_3YTTM(dataframe):
            dataframe = dataframe.dropna()
            Series=pd.Series(index=dataframe.index)
            Series[-4:]=0.1+0.6/len(Series)
            Series[:-4]=0.6/len(Series)
            dataframe = dataframe.dropna().sort_values()
            dataframe.iloc[0] = dataframe.iloc[1]
            dataframe.iloc[-1] = dataframe.iloc[-2]

            value=(dataframe*Series).sum()*4

            return value

        def Fun(dataframe):
            # print(dataframe['S_INFO_WINDCODE'].iloc[0])
            dataframe = dataframe.sort_values(['REPORT_PERIOD','ANN_DT'])
            # dataframe = dataframe
            dataframe = dataframe.set_index(['REPORT_PERIOD'], drop=False)
            dataframe = dataframe[~dataframe.index.duplicated('last')]
            dataframe['FCF_TTM'] =dataframe.apply(lambda x: x['FREE_CASH_FLOW'] -
                                          dataframe.loc[str(int(x['REPORT_PERIOD']) - 10000), 'FREE_CASH_FLOW']+
                                          dataframe.loc[str(int(x['REPORT_PERIOD']) - 10000)[:4] + '1231', 'FREE_CASH_FLOW']
                if (str(int(x['REPORT_PERIOD']) - 10000) in dataframe.index) and (
                            str(int(x['REPORT_PERIOD']) - 10000)[:4] + '1231'
                            in dataframe.index) else np.nan, axis=1)

            dataframe['FCF_SEASONAL']=dataframe.apply(lambda x : x['FREE_CASH_FLOW'] if x['REPORT_PERIOD'][-4:]=='0331' else
                                                      x['FREE_CASH_FLOW']-dataframe.loc[x['REPORT_PERIOD'][:-4]+'0331', 'FREE_CASH_FLOW']
                                                      if ((x['REPORT_PERIOD'][-4:]=='0630') and x['REPORT_PERIOD'][:-4]+'0331' in dataframe.index) else
                                                      x['FREE_CASH_FLOW'] - dataframe.loc[
                                                          x['REPORT_PERIOD'][:-4] + '0630', 'FREE_CASH_FLOW']
                                                      if ((x['REPORT_PERIOD'][-4:] == '0930') and x['REPORT_PERIOD'][
                                                                                                  :-4] + '0630' in dataframe.index) else
                                                      x['FREE_CASH_FLOW'] - dataframe.loc[
                                                          x['REPORT_PERIOD'][:-4] + '0930', 'FREE_CASH_FLOW']
                                                      if ((x['REPORT_PERIOD'][-4:] == '1231') and x['REPORT_PERIOD'][
                                                                                                  :-4] + '0930' in dataframe.index) else
                                                      np.nan , axis=1

                                                      )

            dataframe['FCF_ADJ_2YTTM'] = dataframe['FCF_SEASONAL'].rolling(window=8,min_periods=6).apply( Calculate_ADJ_2YTTM )
            dataframe['FCF_ADJ_3YTTM'] = dataframe['FCF_SEASONAL'].rolling(window=12,min_periods=6).apply( Calculate_ADJ_3YTTM )

            dataframe['FCF_TTM_YOY_growth']=dataframe['FCF_TTM'].rolling(window=5,min_periods=2).apply(lambda x :(x[-1]-x[0])/abs(x[0]))
            dataframe['FCF_ADJ_2YTTM_YOY_growth'] = dataframe['FCF_ADJ_2YTTM'].rolling(window=5, min_periods=2).apply(
                lambda x: (x[-1] - x[0]) / abs(x[0]))
            dataframe['FCF_ADJ_3YTTM_YOY_growth'] = dataframe['FCF_ADJ_3YTTM'].rolling(window=5, min_periods=2).apply(
                lambda x: (x[-1] - x[0]) / abs(x[0]))



            return dataframe



            # dataframe['seasonal_free_cash_flow']=dataframe
        S_FA_FCFF['S_INFO_WINDCODE'] = S_FA_FCFF['S_INFO_WINDCODE'].apply(windcode_to_id)
        S_FA_FCFF=S_FA_FCFF.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']).groupby('S_INFO_WINDCODE').apply(Fun)

        S_FA_FCFF_pv=S_FA_FCFF.reset_index(drop=True)
        S_FA_FCFF_pv = pd.pivot_table(S_FA_FCFF_pv, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['FCF_ADJ_3YTTM'])
        S_FA_FCFF_pv.columns = S_FA_FCFF_pv.columns.get_level_values(1)
        S_FA_FCFF_pv = S_FA_FCFF_pv.fillna(method='ffill')
        S_FA_FCFF_pv=S_FA_FCFF_pv.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        #
        # S_FA_FCFF = original_FCF_TTM.append(S_FA_FCFF)
        # S_FA_FCFF = S_FA_FCFF[~S_FA_FCFF.index.duplicated('last')]

        fileHandle = open('./basic_factor_data/Ashare_daliy_derivative_financial_indicators/FCF/FCF_ALL.pkl', 'wb')
        pickle.dump(S_FA_FCFF, fileHandle)
        fileHandle.close()


        fileHandle = open('./basic_factor_data/Ashare_daliy_derivative_financial_indicators/FCF/FCF_ADJ_3YTTM_pivot.pkl', 'wb')
        pickle.dump(S_FA_FCFF_pv, fileHandle)
        fileHandle.close()

        print('FCF_ALL data is saved')



    #企业价值 EV=股权价值（总市值）+带息债务-货币资金
    def get_wind_EV_DATA(self, reload_tradedays='all'):
        end_date = self.end_date
        start_date,original_EV=\
            setting_startdate_and_saving_path_dataframe('basic_factor_data/Ashare_daliy_derivative_financial_indicators',
                                                        'stk_EV.pkl',reload_tradedays)

        # 总市值
        stk_size_data=pd.read_pickle('./basic_factor_data/stk_size_data.pkl')
        stk_size_data=stk_size_data*10000
        stk_size_data=stk_size_data.reindex(index=self.Tradedays_list,columns=stk_size_data.columns)
        # 带息债务
        sql_interst_debts = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD ,S_FA_INTERESTDEBT  " \
                           " from wind_quant.dbo.AShareFinancialIndicator WHERE " \
                           "  REPORT_PERIOD <={}  and  REPORT_PERIOD >={} order by ANN_DT asc".format(end_date, start_date)

        # print(sql_stk_pct_data)
        stk_interst_debts = pd.read_sql(sql_interst_debts, self.con_wind_db)

        stk_interst_debts = stk_interst_debts[~ stk_interst_debts['S_INFO_WINDCODE'].str.contains('T')]
        stk_interst_debts = stk_interst_debts[~ stk_interst_debts['S_INFO_WINDCODE'].str.contains('BJ')]
        stk_interst_debts = stk_interst_debts[~ stk_interst_debts['S_INFO_WINDCODE'].str.contains('A')]

        #去掉.后面的东西，方便对其,str格式
        stk_interst_debts['S_INFO_WINDCODE'] = stk_interst_debts['S_INFO_WINDCODE'].apply(windcode_to_id)


        stk_interst_debts=stk_interst_debts.sort_values(['REPORT_PERIOD'], ascending=False)
        stk_interst_debts = stk_interst_debts[~stk_interst_debts[['S_INFO_WINDCODE','ANN_DT']].duplicated(keep='first')]

        stk_interst_debts['ANN_DT'] = stk_interst_debts['ANN_DT'].fillna(stk_interst_debts['REPORT_PERIOD'])

        stk_interst_debts_data = pd.pivot_table(stk_interst_debts, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['S_FA_INTERESTDEBT'])
        stk_interst_debts_data.columns = stk_interst_debts_data.columns.get_level_values(1)
        stk_interst_debts_data = stk_interst_debts_data.fillna(method='ffill')
        stk_interst_debts_data=stk_interst_debts_data.reindex(index=self.Tradedays_list,columns=stk_size_data.columns).fillna(method='ffill')
        #注意有nan
        # stk_interst_debts_data=stk_interst_debts_data.fillna(0)

        # #计算带息债务同比增长率
        # def Fun(dataframe):
        #     dataframe = dataframe.sort_values(['REPORT_PERIOD','ANN_DT'])
        #     # dataframe = dataframe
        #     dataframe = dataframe.set_index(['REPORT_PERIOD'], drop=False)
        #     dataframe = dataframe[~dataframe.index.duplicated('last')]
        #
        #     dataframe['INTERESTDEBT_YOY_growth']=\
        #         dataframe['S_FA_INTERESTDEBT'].rolling(window=5,min_periods=2).apply(lambda x :(x[-1]-x[0])/abs(x[0]))
        #
        #     return dataframe
        #
        # stk_interst_debts_yoy = stk_interst_debts.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']).groupby('S_INFO_WINDCODE').apply(Fun)
        # stk_interst_debts_yoy=stk_interst_debts_yoy.replace(np.inf, np.nan)
        # stk_interst_debts_yoy=stk_interst_debts_yoy.reset_index(drop=True)
        # stk_interst_debts_yoy = pd.pivot_table(stk_interst_debts_yoy, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['INTERESTDEBT_YOY_growth'])
        # stk_interst_debts_yoy.columns = stk_interst_debts_yoy.columns.get_level_values(1)
        # stk_interst_debts_yoy = stk_interst_debts_yoy.fillna(method='ffill')
        # stk_interst_debts_yoy=stk_interst_debts_yoy.reindex(index=self.Tradedays_list,columns=stk_size_data.columns).fillna(method='ffill')

        # 发现有空值，用总负债填满,发现空值的是银行和券商等机构，暂时不要也罢
        # 负债总计 和 货币资金

        sql_assets = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD ,STATEMENT_TYPE,MONETARY_CAP,TOT_LIAB,TOT_CUR_ASSETS  " \
                           " from wind_quant.dbo.AShareBalanceSheet WHERE  STATEMENT_TYPE=408001000 and " \
                           "  REPORT_PERIOD <={}  and  REPORT_PERIOD >={} order by ANN_DT asc".format(end_date, start_date)

        # print(sql_stk_pct_data)
        stk_assets = pd.read_sql(sql_assets, self.con_wind_db)

        stk_assets = stk_assets[~ stk_assets['S_INFO_WINDCODE'].str.contains('T')]
        stk_assets = stk_assets[~ stk_assets['S_INFO_WINDCODE'].str.contains('BJ')]
        stk_assets = stk_assets[~ stk_assets['S_INFO_WINDCODE'].str.contains('A')]

        stk_assets['S_INFO_WINDCODE'] = stk_assets['S_INFO_WINDCODE'].apply(windcode_to_id)
        stk_assets['ANN_DT']=stk_assets['ANN_DT'].fillna(stk_assets['REPORT_PERIOD'])
        #部分没货币资金没有值的 按照流动资产1/3计算，如果都没有就是0
        stk_assets['MONETARY_CAP']=stk_assets.apply(lambda x : x['MONETARY_CAP'] if not np.isnan(x['MONETARY_CAP'])
                                                    else x['TOT_CUR_ASSETS']/3 if not np.isnan(x['TOT_CUR_ASSETS']) else 0,axis=1)
        #货币资金
        #删除重复值 取最前一个
        stk_assets=stk_assets.sort_values(['REPORT_PERIOD'], ascending=False)
        stk_assets = stk_assets[~stk_assets[['S_INFO_WINDCODE','ANN_DT']].duplicated(keep='first')]

        stk_monetary_cap_data = pd.pivot_table(stk_assets, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['MONETARY_CAP'])
        stk_monetary_cap_data.columns = stk_monetary_cap_data.columns.get_level_values(1)
        stk_monetary_cap_data = stk_monetary_cap_data.fillna(method='ffill')
        stk_monetary_cap_data=stk_monetary_cap_data.reindex(index=self.Tradedays_list,columns=stk_size_data.columns).fillna(method='ffill')

        # EV_moneyincluded = 股权价值（总市值）+带息债务
        STK_EV_moneyincluded = stk_size_data + stk_interst_debts_data.fillna(0)

        # 货币资金如果大于EV_moneyincluded的20%就以20%计算否则不变，有的股票很奇怪 比如2011年的600686 货币资金巨大 暂时不知道原因
        stk_monetary_cap_data=stk_monetary_cap_data-(stk_monetary_cap_data-STK_EV_moneyincluded*0.2).apply(lambda x : x.apply(lambda x : 0 if x<=0 else x )).fillna(0)


        # EV = 股权价值（总市值）+带息债务 - 货币资金
        STK_EV=stk_size_data+stk_interst_debts_data.fillna(0)-stk_monetary_cap_data.fillna(0)

        fileHandle = open('./basic_factor_data/Ashare_daliy_derivative_financial_indicators/stk_EV.pkl', 'wb')
        pickle.dump(STK_EV, fileHandle)
        fileHandle.close()

        print('STK_EV data is saved')



    #个股一致预期滚动数据表con_forecast_roll_stk
    def con_forecast_roll_stk(self, reload_tradedays='all'):
        end_date = self.end_date
        # 滚动一致预期净利润两年复合增长率consensus forcast net profit 2 years compound growth rate trailing twelve months
        start_date,original_con_npcgrate_2y_roll=\
            setting_startdate_and_saving_path_dataframe('Con_Forecast/con_forecast_roll_stk',
                                                        'con_npcgrate_2y_roll.pkl',reload_tradedays)

        start_date,original_con_np_roll=\
            setting_startdate_and_saving_path_dataframe('Con_Forecast/con_forecast_roll_stk',
                                                        'con_np_roll.pkl',reload_tradedays)

        date_end_date=datetime.datetime.strptime(str(end_date),'%Y%m%d').strftime('%Y-%m-%d')
        date_start_date=datetime.datetime.strptime(str(start_date),'%Y%m%d').strftime('%Y-%m-%d')

        # 滚动一致预期净利润两年复合增长率consensus
        sql_con_npcgrate_2y_roll = "select stock_code,con_date,con_np_roll,con_npcgrate_2y_roll " \
                           " from FundRiskControl2.dbo.con_forecast_roll_stk  where " \
                           "  con_date <='{}'  and  con_date >='{}'order by con_date asc".format(date_end_date, date_start_date)

        con_forecast_2y=pd.read_sql(sql_con_npcgrate_2y_roll, self.con_gogoal_db)

        con_forecast_2y['stock_code'] = con_forecast_2y['stock_code'].apply(windcode_to_id)
        con_forecast_2y['con_date'] = con_forecast_2y['con_date'].apply(lambda x : x.strftime('%Y%m%d'))

        #pivot it!
        con_forecast_2y_data = pd.pivot_table(con_forecast_2y, index=['con_date'], columns=['stock_code'], values=['con_npcgrate_2y_roll'])
        con_forecast_2y_data.columns = con_forecast_2y_data.columns.get_level_values(1)

        #做两个处理 1 把所有增长超过200都改成-10 因为基本上这种都是前面利润很低 基数低
        #2
        #3 rolling（50个交易日，至少有30个数，取最后10个的平均值） 做不到暂时这样处理

        con_forecast_2y_data[con_forecast_2y_data>200]=-10
        con_forecast_2y_data=con_forecast_2y_data.apply(lambda x
                                                        : x.rolling(window=10,min_periods=5).mean())
        con_forecast_2y_data = con_forecast_2y_data.fillna(0)
        con_forecast_2y_data=con_forecast_2y_data.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')


        con_np_roll_data = pd.pivot_table(con_forecast_2y, index=['con_date'], columns=['stock_code'], values=['con_np_roll'])
        con_np_roll_data.columns = con_np_roll_data.columns.get_level_values(1)

        con_forecast_2y_data=con_forecast_2y_data.apply(lambda x
                                                        : x.rolling(window=10,min_periods=5).mean())


        con_np_roll_data = con_np_roll_data.fillna(method='ffill')
        con_np_roll_data=con_np_roll_data.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        # 调整单位 把万元转成元 百分比去掉
        con_forecast_2y_data=con_forecast_2y_data*0.01
        con_np_roll_data=con_np_roll_data*10000



        con_forecast_2y_data = original_con_npcgrate_2y_roll._append(con_forecast_2y_data)
        con_forecast_2y_data = con_forecast_2y_data[~con_forecast_2y_data.index.duplicated('last')]


        con_np_roll_data = original_con_np_roll._append(con_np_roll_data)
        con_np_roll_data = con_np_roll_data[~con_np_roll_data.index.duplicated('last')]

        fileHandle = open('./Con_Forecast/con_forecast_roll_stk/con_npcgrate_2y_roll.pkl', 'wb')
        pickle.dump(con_forecast_2y_data, fileHandle)
        fileHandle.close()


        fileHandle = open('./Con_Forecast/con_forecast_roll_stk/con_np_roll.pkl', 'wb')
        pickle.dump(con_np_roll_data, fileHandle)
        fileHandle.close()

        print('con_np_roll is saved')



    def get_stk_adj_price(self, reload_tradedays='all'):
        end_date = self.end_date

        start_date, original_adj_avgprice = \
            setting_startdate_and_saving_path_dataframe('basic_factor_data/stk_adj_price',
                                                        'stk_adj_avgprice.pkl',reload_tradedays)

        start_date, original_adj_closeprice = \
            setting_startdate_and_saving_path_dataframe('basic_factor_data/stk_adj_price',
                                                        'stk_adj_closeprice.pkl',reload_tradedays)

        # 中国A股日行情[AShareEODPrices]
        sql_stk_price_data = "select S_INFO_WINDCODE,TRADE_DT,S_DQ_ADJCLOSE,S_DQ_ADJFACTOR*S_DQ_AVGPRICE as stk_adj_avgprice " \
                           " from wind_quant.dbo.AShareEODPrices WHERE " \
                           "  TRADE_DT <={}  and  TRADE_DT >={} order by TRADE_DT asc".format(end_date, start_date)

        stk_price_data = pd.read_sql(sql_stk_price_data, self.con_wind_db)

        stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('T')]
        stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('BJ')]
        stk_price_data = stk_price_data[~ stk_price_data['S_INFO_WINDCODE'].str.contains('A')]

        stk_price_data['S_INFO_WINDCODE'] = stk_price_data['S_INFO_WINDCODE'].apply(windcode_to_id)



        stk_adj_avgprice = pd.pivot_table(stk_price_data, index=['TRADE_DT'],
                                                             columns=['S_INFO_WINDCODE'], values=['stk_adj_avgprice'])
        stk_adj_closeprice = pd.pivot_table(stk_price_data, index=['TRADE_DT'],
                                                      columns=['S_INFO_WINDCODE'], values=['S_DQ_ADJCLOSE'])



        stk_adj_avgprice.columns = stk_adj_avgprice.columns.get_level_values(1)
        stk_adj_closeprice.columns = stk_adj_closeprice.columns.get_level_values(1)
        # # stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(dataframe_daily_BM_indicator.columns)]
        stk_adj_avgprice = original_adj_avgprice._append(stk_adj_avgprice)
        stk_adj_avgprice = stk_adj_avgprice[~stk_adj_avgprice.index.duplicated('last')]

        stk_adj_closeprice = original_adj_closeprice._append(stk_adj_closeprice)
        stk_adj_closeprice = stk_adj_closeprice[~stk_adj_closeprice.index.duplicated('last')]


        fileHandle = open('./basic_factor_data/stk_adj_price/stk_adj_avgprice.pkl', 'wb')
        pickle.dump(stk_adj_avgprice, fileHandle)
        fileHandle.close()

        fileHandle = open('./basic_factor_data/stk_adj_price/stk_adj_closeprice.pkl', 'wb')
        pickle.dump(stk_adj_closeprice, fileHandle)
        fileHandle.close()

        print('stk_adj_closeprice and stk_adj_avgprice data is saved')



    #预先保存资产负债表中的重要数据
    def get_AShareBalanceSheet(self, reload_tradedays='all'):

        # 获取带息债务 S_FA_INTERESTDEBT
        end_date = self.end_date
        start_date, original_interestdebt = \
            setting_startdate_and_saving_path_dataframe('AShareBalanceSheet/Debt',
                                                        'interestdebt.pkl',reload_tradedays)

        start_date, original_adj_interestdebt = \
            setting_startdate_and_saving_path_dataframe('AShareBalanceSheet/Debt',
                                                        'adj_interestdebt.pkl',reload_tradedays)

        # 获取带息债务--中国A股财务指标[AShareFinancialIndicator]
        sql_S_FA_INTERESTDEBT = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD,S_FA_INTERESTDEBT " \
                             " from wind_quant.dbo.AShareFinancialIndicator WHERE " \
                             "  ANN_DT <={}  and  ANN_DT >={} order by ANN_DT asc".format(end_date, start_date)

        interestdebt = pd.read_sql(sql_S_FA_INTERESTDEBT, self.con_wind_db)

        interestdebt = interestdebt[~ interestdebt['S_INFO_WINDCODE'].str.contains('T')]
        interestdebt = interestdebt[~ interestdebt['S_INFO_WINDCODE'].str.contains('BJ')]
        interestdebt = interestdebt[~ interestdebt['S_INFO_WINDCODE'].str.contains('A')]


        interestdebt['S_INFO_WINDCODE'] = interestdebt['S_INFO_WINDCODE'].apply(windcode_to_id)
        #需要有经过调整后的带息负债，要考虑到到期负债理论上要取平均数
        interestdebt=interestdebt.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD'])
        interestdebt['adj_INTERESTDEBT']=interestdebt.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']).groupby('S_INFO_WINDCODE').\
            apply(lambda x : x['S_FA_INTERESTDEBT'].rolling(window=4,min_periods=1).mean()).values


        stk_interestdebt = pd.pivot_table(interestdebt, index=['ANN_DT'],
                                          columns=['S_INFO_WINDCODE'], values=['S_FA_INTERESTDEBT'])

        stk_interestdebt.columns = stk_interestdebt.columns.get_level_values(1)
        stk_interestdebt=stk_interestdebt.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        stk_interestdebt = pd.concat([original_interestdebt,stk_interestdebt])
        stk_interestdebt = stk_interestdebt[~stk_interestdebt.index.duplicated('last')]
        stk_interestdebt=stk_interestdebt.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        fileHandle = open('./AShareBalanceSheet/Debt/interestdebt.pkl', 'wb')
        pickle.dump(stk_interestdebt, fileHandle)
        fileHandle.close()
        print('interestdebt  data is saved')

        adj_stk_interestdebt = pd.pivot_table(interestdebt, index=['ANN_DT'],
                                          columns=['S_INFO_WINDCODE'], values=['adj_INTERESTDEBT'])

        adj_stk_interestdebt.columns = adj_stk_interestdebt.columns.get_level_values(1)
        adj_stk_interestdebt = adj_stk_interestdebt.reindex(index=self.Tradedays_list, columns=self.stk_pool).fillna(
            method='ffill')

        adj_stk_interestdebt = pd.concat([original_adj_interestdebt,adj_stk_interestdebt])
        adj_stk_interestdebt = adj_stk_interestdebt[~adj_stk_interestdebt.index.duplicated('last')]
        adj_stk_interestdebt = adj_stk_interestdebt.reindex(index=self.Tradedays_list, columns=self.stk_pool).fillna(
            method='ffill')

        fileHandle = open('./AShareBalanceSheet/Debt/adj_interestdebt.pkl', 'wb')
        pickle.dump(adj_stk_interestdebt, fileHandle)
        fileHandle.close()
        print('adj_interestdebt  data is saved')


if __name__ == '__main__':
    getting=Get_data_fromdatabase()
    getting.get_wind_index_980092_CNI()

    getting.get_wind_index_885000_WI()
    getting.get_AShareBalanceSheet()

