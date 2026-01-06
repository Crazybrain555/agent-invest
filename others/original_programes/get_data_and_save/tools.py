#!/usr/bin/env python
#-*- utf-8 -*-

'''
Created on August 24 2022
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
import statsmodels.api as sm


# 进行行业和市值中性化
def IND_and_SIZE_neutralize(signal, IndexMembers, size):
    Tradedays_list=signal.index
    stk_pool=signal.columns

    signal_neutralized = signal.copy()
    IndexMembers = IndexMembers.reindex(index=Tradedays_list,
                                        columns=stk_pool).fillna(method='ffill')
    stk_size_df = size.reindex(index=Tradedays_list, columns=stk_pool).fillna(method='ffill')

    for num, date in tqdm(enumerate(signal.index)):
        y = signal.iloc[num].dropna()
        X = pd.DataFrame(columns=sorted(IndexMembers.iloc[-1].dropna().unique().tolist()) + ['size'],
                         index=y.index)
        temp_dataframe = pd.pivot_table(IndexMembers.iloc[num].reset_index(),
                                        index='index', columns=date, aggfunc={date: 'count'}).fillna(0)
        temp_dataframe.columns = temp_dataframe.columns.get_level_values(1)
        temp_dataframe = temp_dataframe.reindex(index=X.index, columns=X.columns[:-1])
        X.iloc[:, :-1] = temp_dataframe.fillna(0)
        X.iloc[:, -1] = np.log(stk_size_df.iloc[num].reindex())
        X = X.dropna()
        y = y.reindex(index=X.index)

        m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
        m1.fit(X, y)
        # 残差等于
        resid = y - m1.predict(X)
        resid = resid.reindex(index=signal.columns)
        signal_neutralized.iloc[num] = resid

    return signal_neutralized


def change_rate(r):
    if r<-0.05:
        a=r

    elif r<1.1:
        a = 0.1 - 1 / (1 + (1 + r) / 1.1 + (1 + r) * (1 + r * 0.85) / 1.1 ** 2 + 11.76 * (1 + r) * (
                    1 + r * 0.85) / 1.1 ** 3)
    else:
        a= 0.07628660321358395

    return a

def close_form_OLS(X, y):
    theta = np.matmul(np.matmul(np.linalg.inv(np.matmul(X.T, X)), X.T), y)
    resid = y - np.matmul(X, theta)
    return theta, resid


# 进行行业和市值中性化
def IND_and_SIZE_neutralize(signal, IndexMembers, size):
    signal_neutralized = signal.copy()
    IndexMembers = IndexMembers.reindex(index=self.Tradedays_list,
                                        columns=self.stk_pool).fillna(method='ffill')
    stk_size_df = size.reindex(index=self.Tradedays_list, columns=self.stk_pool).fillna(method='ffill')
    for num, date in tqdm(enumerate(signal.index)):
        y = signal.iloc[num].dropna()
        X = pd.DataFrame(columns=sorted(IndexMembers.iloc[-1].dropna().unique().tolist()) + ['size'],
                         index=y.index)
        temp_dataframe = pd.pivot_table(IndexMembers.iloc[num].reset_index(),
                                        index='index', columns=date, aggfunc={date: 'count'}).fillna(0)
        temp_dataframe.columns = temp_dataframe.columns.get_level_values(1)
        temp_dataframe = temp_dataframe.reindex(index=X.index, columns=X.columns[:-1])
        X.iloc[:, :-1] = temp_dataframe.fillna(0)
        size_value = np.log(stk_size_df.iloc[num].reindex())
        X.iloc[:, -1] = (size_value - size_value.mean()) / size_value.std()
        X = X.dropna()
        y = y.reindex(index=X.index)

        m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
        m1.fit(X, y)
        # 残差等于
        resid = y - m1.predict(X)
        resid = resid.reindex(index=signal.columns)
        signal_neutralized.iloc[num] = resid

    return signal_neutralized


# 进行行业中性化
def IND_neutralize(signal, IndexMembers):
    signal_neutralized = signal.copy()
    IndexMembers = IndexMembers.reindex(index=self.Tradedays_list,
                                        columns=self.stk_pool).fillna(method='ffill')
    for num, date in tqdm(enumerate(signal.index)):
        y = signal.iloc[num].dropna()
        X = pd.DataFrame(columns=sorted(IndexMembers.iloc[-1].dropna().unique().tolist()),
                         index=y.index)
        temp_dataframe = pd.pivot_table(IndexMembers.iloc[num].reset_index(),
                                        index='index', columns=date, aggfunc={date: 'count'}).fillna(0)
        temp_dataframe.columns = temp_dataframe.columns.get_level_values(1)
        temp_dataframe = temp_dataframe.reindex(index=X.index, columns=X.columns[:])
        X.iloc[:, :] = temp_dataframe.fillna(0)
        X = X.dropna()
        y = y.reindex(index=X.index)

        m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
        m1.fit(X, y)
        # 残差等于
        resid = y - m1.predict(X)
        resid = resid.reindex(index=signal.columns)
        signal_neutralized.iloc[num] = resid

    return signal_neutralized


def windcode_to_id(windcode):
    return int(windcode.split('.')[0])

def windcode_to_strid(windcode):
    return str(windcode.split('.')[0])


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
        start_date = int('20120101')
        start_date_halfyear = int('20110601')
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



def get_TTM_value(dataframe,value_name,TTM_name,date_name='REPORT_PERIOD',announce_name='ANN_DT'):
    dataframe = dataframe.sort_values([date_name, announce_name])

    dataframe = dataframe.set_index([date_name], drop=False)
    dataframe = dataframe[~dataframe.index.duplicated('last')]

    dataframe[TTM_name] = dataframe.apply(lambda x: x[value_name] -
                                                     dataframe.loc[
                                                         str(int(x[date_name]) - 10000), value_name] +
                                                     dataframe.loc[str(int(x[date_name]) - 10000)[
                                                                   :4] + '1231', value_name]
                                                     if (str(int(x[date_name]) - 10000) in dataframe.index) and
                                                        (str(int(x[date_name]) - 10000)[:4] + '1231'in dataframe.index)
    else x[value_name] if str(x[date_name])[-4:]=='1231'

    else np.nan, axis=1)

    dataframe = dataframe.fillna(method='ffill')

    return dataframe


# 环比生成器
def get_qoq_data(signal_data):
    signal_index = signal_data.index

    def calculate_QOQ(sereis_data):
        undp_data = sereis_data.drop_duplicates(keep='first')
        qoq_data = undp_data.rolling(window=2).apply(lambda x: x[1] / x[0] - 1)
        qoq_data = qoq_data.reindex(index=signal_index).fillna(method='ffill')
        return qoq_data

    qoq_signal_data = signal_data.apply(lambda x: calculate_QOQ(x))
    return qoq_signal_data




class tool():


    def __init__(self):
        # df_stk_pct_data.columns = df_stk_pct_data.columns.astype('str')
        # self.stk_pct_data = df_stk_pct_data
        # self.date = df_stk_pct_data.index
        print('data_processor_start')
        self.con_wind_db = pymssql.connect('v-wind', 'trade', 'trade', 'wind_quant',charset='cp936')
        self.con_gogoal_db = pymssql.connect('p-ma-mars', 'sig', 'sig', 'FundRiskControl2',charset='cp936')

        self.end_date = int(datetime.date.today().strftime('%Y%m%d'))
        self.tradedays_start=20110101
        sql = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' and TRADE_DAYS >={} " \
              "and TRADE_DAYS <={} order by TRADE_DAYS asc".format(self.tradedays_start,self.end_date)
        data = pd.read_sql(sql, self.con_wind_db)
        self.Tradedays_list = data['TRADE_DAYS'].tolist()

        self.lastyear_tradedays_start=20100101
        sql = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' and TRADE_DAYS >={} " \
              "and TRADE_DAYS <={} order by TRADE_DAYS asc".format(self.lastyear_tradedays_start,self.end_date)
        lastyear_tradedaysdata = pd.read_sql(sql, self.con_wind_db)
        self.previous_Tradedays_list = lastyear_tradedaysdata['TRADE_DAYS'].tolist()

        #中国A股定期报告披露日期[AShareIssuingDatePredict]这个表数据不行  中国A股资产负债表[AShareBalanceSheet]

        IssuingDate_sql = "select distinct(REPORT_PERIOD) from wind_quant.dbo.AShareBalanceSheet " \
                          "where  ANN_DT >={}  order by REPORT_PERIOD asc".format(self.tradedays_start)
        data = pd.read_sql(IssuingDate_sql, self.con_wind_db)
        self.seasonal_report_list = data['REPORT_PERIOD'].tolist()

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

        forbid_pool_path = r'E:\AIproject\fintech\zhangyuye_programe\DATA\forbid_data\tot_forbid_pivot_data.pkl'
        self.forbid_pool_data = pd.read_pickle(forbid_pool_path)


    def get_signal_data_from_database(self,siganl_name,sheet_name,start_date,end_date,method,database='wind',
                                      STATEMENT_TYPE='408001000',fillna=0):

        '''
        :param siganl_name:
        :param sheet_name:
        :param method: 处理方式，分为 None,ttm,1yearavg,
        :param database:
        fillna='None' 要不要提前把null填充成0 如果是None就不处理，如果是0或者别的，就填充
        STATEMENT_TYPE='408001000'   如果没有，填None,是None，就空着 报表类型: 408001000: 合并报表 408004000: 合并报表(调整) 408005000: 合并报表(更正前) 408050000: 合并调整(更
        :return:
        '''

        def get_pivot_data( dataframe, target_columns, index='ANN_DT', columns='S_INFO_WINDCODE'):
            dataframe_pv = pd.pivot_table(dataframe, index=[index], columns=[columns], values=[target_columns])
            dataframe_pv.columns = dataframe_pv.columns.get_level_values(1)
            dataframe_pv = dataframe_pv.fillna(method='ffill')
            dataframe_pv = dataframe_pv.reindex(index=self.previous_Tradedays_list,
                                                columns=self.stk_pool).fillna(method='ffill')
            dataframe_pv = dataframe_pv.reindex(index=self.Tradedays_list,
                                                columns=self.stk_pool).fillna(method='ffill')
            return dataframe_pv

        def get_TTM_value(dataframe, value_name, TTM_name, date_name='REPORT_PERIOD', announce_name='ANN_DT'):
            dataframe = dataframe.sort_values([date_name, announce_name])

            dataframe = dataframe.set_index([date_name], drop=False)
            dataframe = dataframe[~dataframe.index.duplicated('last')]

            dataframe[TTM_name] = dataframe.apply(lambda x: x[value_name] -
                                                            dataframe.loc[
                                                                str(int(x[date_name]) - 10000), value_name] +
                                                            dataframe.loc[str(int(x[date_name]) - 10000)[
                                                                          :4] + '1231', value_name]
            if (str(int(x[date_name]) - 10000) in dataframe.index) and
               (str(int(x[date_name]) - 10000)[:4] + '1231' in dataframe.index)
            else x[value_name] if str(x[date_name])[-4:] == '1231'
            else np.nan, axis=1)

            dataframe = dataframe.fillna(method='ffill')

            return dataframe

        if database=='wind':
            db=self.con_wind_db

        if STATEMENT_TYPE==None:
            STATEMENT_TYPE_statement=''
        else:
            STATEMENT_TYPE_statement = 'STATEMENT_TYPE=\'{}\' and'.format(STATEMENT_TYPE)

        sql = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD,{} from" \
              " {}  where {}  ANN_DT >={} " \
              "and ANN_DT <={} order by ANN_DT asc".format(siganl_name,sheet_name, STATEMENT_TYPE_statement,
                                                           start_date,end_date)
        dataframe = pd.read_sql(sql, db)

        dataframe = dataframe[~ dataframe['S_INFO_WINDCODE'].str.contains('T')]
        dataframe = dataframe[~ dataframe['S_INFO_WINDCODE'].str.contains('BJ')]
        dataframe = dataframe[~ dataframe['S_INFO_WINDCODE'].str.contains('A')]

        dataframe['S_INFO_WINDCODE'] = dataframe['S_INFO_WINDCODE'].apply(windcode_to_id)
        dataframe=dataframe.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD'])
        dataframe=dataframe.fillna(fillna) if fillna!=None else dataframe

        if method==None:
            pv_df=get_pivot_data(dataframe,target_columns=siganl_name)
        elif method=='TTM' or method=='ttm':
            siganl_TTM_name=siganl_name+'_TTM'
            target_signal_name=siganl_TTM_name
            # 计算siganl_name的TTM
            dataframe = dataframe.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']). \
                groupby('S_INFO_WINDCODE').apply(lambda x: get_TTM_value(x, siganl_name, siganl_TTM_name))
            dataframe = dataframe.reset_index(drop=True)
            pv_df = get_pivot_data( dataframe, target_columns=target_signal_name)

        elif method=='1yearavg' or 'AVG' or 'avg' :
            siganl_avg_name=siganl_name+'_AVG'
            target_signal_name=siganl_avg_name
            # 计算siganl_name的avg
            dataframe[siganl_avg_name] = dataframe.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']).groupby(
                'S_INFO_WINDCODE'). \
                apply(lambda x: x[siganl_name].rolling(window=4, min_periods=1).mean()).values
            dataframe = dataframe.reset_index(drop=True)
            pv_df = get_pivot_data( dataframe, target_columns=target_signal_name)

        return pv_df





