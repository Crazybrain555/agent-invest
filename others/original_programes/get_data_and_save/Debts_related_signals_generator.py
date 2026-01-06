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

#
# # 进行行业和市值中性化
# def IND_and_SIZE_neutralize(signal, IndexMembers, size):
#     Tradedays_list=signal.index
#     stk_pool=signal.columns
#
#     signal_neutralized = signal.copy()
#     IndexMembers = IndexMembers.reindex(index=Tradedays_list,
#                                         columns=stk_pool).fillna(method='ffill')
#     stk_size_df = size.reindex(index=Tradedays_list, columns=stk_pool).fillna(method='ffill')
#
#     for num, date in tqdm(enumerate(signal.index)):
#         y = signal.iloc[num].dropna()
#         X = pd.DataFrame(columns=sorted(IndexMembers.iloc[-1].dropna().unique().tolist()) + ['size'],
#                          index=y.index)
#         temp_dataframe = pd.pivot_table(IndexMembers.iloc[num].reset_index(),
#                                         index='index', columns=date, aggfunc={date: 'count'}).fillna(0)
#         temp_dataframe.columns = temp_dataframe.columns.get_level_values(1)
#         temp_dataframe = temp_dataframe.reindex(index=X.index, columns=X.columns[:-1])
#         X.iloc[:, :-1] = temp_dataframe.fillna(0)
#         X.iloc[:, -1] = np.log(stk_size_df.iloc[num].reindex())
#         X = X.dropna()
#         y = y.reindex(index=X.index)
#
#         m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
#         m1.fit(X, y)
#         # 残差等于
#         resid = y - m1.predict(X)
#         resid = resid.reindex(index=signal.columns)
#         signal_neutralized.iloc[num] = resid
#
#     return signal_neutralized


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

#
# # 进行行业和市值中性化
# def IND_and_SIZE_neutralize(signal, IndexMembers, size):
#     zj_NIM = signal
#     AIndexMembersCITICS1 = IndexMembers
#     stk_size_df = size
#
#     zj_NIM_neutralized = zj_NIM
#     AIndexMembersCITICS1 = AIndexMembersCITICS1.reindex(index=self.Tradedays_list,
#                                                         columns=self.stk_pool).fillna(method='ffill')
#     stk_size_df = stk_size_df.reindex(index=self.Tradedays_list, columns=self.stk_pool).fillna(method='ffill')
#     for num, date in tqdm(enumerate(zj_NIM.index)):
#         y = zj_NIM.iloc[num].dropna()
#         X = pd.DataFrame(columns=sorted(AIndexMembersCITICS1.iloc[-1].dropna().unique().tolist()) + ['size'],
#                          index=y.index)
#         temp_dataframe = pd.pivot_table(AIndexMembersCITICS1.iloc[num].reset_index(),
#                                         index='index', columns=date, aggfunc={date: 'count'}).fillna(0)
#         temp_dataframe.columns = temp_dataframe.columns.get_level_values(1)
#         temp_dataframe = temp_dataframe.reindex(index=X.index, columns=X.columns[:-1])
#         X.iloc[:, :-1] = temp_dataframe.fillna(0)
#         X.iloc[:, -1] = stk_size_df.iloc[num].fillna(stk_size_df.iloc[num].mean())
#
#         m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
#         m1.fit(X, y)
#         # 残差等于
#         resid = y - m1.predict(X)
#         resid = resid.reindex(index=zj_NIM.columns)
#         zj_NIM_neutralized.iloc[num] = resid
#
#     return zj_NIM_neutralized


def windcode_to_id(windcode):
    return int(windcode.split('.')[0])

def windcode_to_strid(windcode):
    return str(windcode.split('.')[0])


def setting_startdate_and_saving_path_dataframe(dir_path, doc_path, reload_tradedays):
    if reload_tradedays == 'all':
        start_date = int('20110101')
        start_date_halfyear = int('20100601')
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
        start_date = int('20110101')
        start_date_halfyear = int('20100601')
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



class Get_debts_related_signals_generator():


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

        AIndexMembersCITICS1=pd.read_pickle(
            r'E:\AIproject\fintech\zhangyuye_programe\DATA\MembersCITICS\AIndexMembersCITICS1_dataframe.pkl')

        stk_size_df = pd.read_pickle(
            r'E:\AIproject\fintech\zhangyuye_programe\DATA\basic_factor_data\stk_size_data.pkl')

        AIndexMembersCITICS1=AIndexMembersCITICS1.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')
        stk_size_df=stk_size_df.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        self.AIndexMembersCITICS1=AIndexMembersCITICS1
        self.stk_size_df=stk_size_df


    def get_pivot_data(self,dataframe, target_columns, index='ANN_DT', columns='S_INFO_WINDCODE'):
        dataframe_pv = pd.pivot_table(dataframe, index=[index], columns=[columns], values=[target_columns])
        dataframe_pv.columns = dataframe_pv.columns.get_level_values(1)
        dataframe_pv = dataframe_pv.fillna(method='ffill')
        dataframe_pv = dataframe_pv.reindex(index=self.previous_Tradedays_list,
                                            columns=self.stk_pool).fillna(method='ffill')
        dataframe_pv = dataframe_pv.reindex(index=self.Tradedays_list,
                                            columns=self.stk_pool).fillna(method='ffill')
        return dataframe_pv



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



    def Capital_structure_and_solvency_signals_creator(self, reload_tradedays='all'):

        end_date = self.end_date
        start_date, original_Ass_lia_ratio = \
            setting_startdate_and_saving_path_dataframe('Z_signal_data/Asst_lia_ratio/', 'Asst_lia_ratio.pkl',
                                                        reload_tradedays)
        # 因为要ttm,所以数据要往前推一年
        start_date_lastyear = int(
            (datetime.datetime.strptime(str(start_date), "%Y%m%d") - BDay(270)).strftime('%Y%m%d'))
        self.start_date_lastyear=start_date_lastyear

        # 进行行业和市值中性化
        def IND_and_SIZE_neutralize(signal,IndexMembers,size):

            signal_neutralized = signal.copy()
            IndexMembers = IndexMembers.reindex(index=self.Tradedays_list,
                                                                columns=self.stk_pool).fillna(method='ffill')
            stk_size_df = size.reindex(index=self.Tradedays_list, columns=self.stk_pool).fillna(method='ffill')
            #涉及到要回归，就必须考虑禁投池，把池子以外的数据去掉
            forbid_pool_data=self.forbid_pool_data

            forbid_pool_data = forbid_pool_data.reindex(index=signal.index, columns=signal.columns)
            # 将禁投池的因子值都变成nan
            signal[forbid_pool_data == 1] = np.nan

            for num, date in tqdm(enumerate(signal.index)):
                y = signal.iloc[num].dropna()
                X = pd.DataFrame(columns=sorted(IndexMembers.iloc[-1].dropna().unique().tolist()) + ['size'],
                                 index=y.index)
                temp_dataframe = pd.pivot_table(IndexMembers.iloc[num].reset_index(),
                                                index='index', columns=date, aggfunc={date: 'count'}).fillna(0)
                temp_dataframe.columns = temp_dataframe.columns.get_level_values(1)
                temp_dataframe = temp_dataframe.reindex(index=X.index, columns=X.columns[:-1])
                X.iloc[:, :-1] = temp_dataframe.fillna(0)
                size_value=np.log(stk_size_df.iloc[num].reindex())
                X.iloc[:, -1] = (size_value-size_value.mean())/size_value.std()
                X=X.dropna()
                y=y.reindex(index=X.index)

                #去极值 X,y 都要去掉极值，采用百分位法，将最大和最小两个2.5%的分位数的值给去掉；以后有有机会再用3σ的方法
                #X，y分别删掉头尾2.5%的数
                # 对于量价类因子，首先对每一期截面因子值计算因子截面均值𝜇𝑡以及因子截面标准差𝜎𝑡，并将极
                # 值边界定义为𝜇𝑡 ± 3 ∗ 𝜎𝑡，对每一个截面超出边界的因子值作缩尾至边界的处理。
                # 对于基本面类因子，首先对每一期截面因子值计算因子值的中位数𝑚𝑒𝑑𝑡以及绝对离差中位数
                # 𝑀𝐴𝐷𝑡，𝑀𝐴𝐷𝑡 = 𝑚𝑒𝑑𝑖𝑎𝑛(|𝑋𝑖,𝑡 − 𝑚𝑒𝑑𝑡|)，其中𝑋𝑖,𝑡为股票𝑖在𝑡时刻的因子值；定义极值边界为
                # 𝑚𝑒𝑑𝑡 ±3/ 0.67449∗ 𝑀𝐴𝐷𝑡，对超出边界的因子值作缩尾至边界的处理。
                X_med=X.iloc[:, -1].mean()
                X_mad=(X.iloc[:, -1]-X_med).abs().mean()
                X_normal=X[(X.iloc[:, -1] <X_med+3.8*X_mad)&(X.iloc[:, -1] >X_med-3.8*X_mad)]             #X去极值

                y_med=y.mean()
                y_mad=(y-y_med).abs().mean()
                y_normal=y[(y<y_med+3.8*y_mad)&(y >y_med-3.8*y_mad)]                #y去极值
                #对其 index求交集
                common_index=list(set(list(X_normal.index))&set(y_normal.index))
                X_normal=X_normal.reindex(index=common_index)
                y_normal=y_normal.reindex(index=common_index)

                m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
                m1.fit(X_normal, y_normal)
                # 残差等于
                resid = y - m1.predict(X)
                resid = resid.reindex(index=signal.columns)
                signal_neutralized.iloc[num] = resid

            return signal_neutralized


        # 进行行业中性化
        def IND_neutralize(signal,IndexMembers):

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
                X=X.dropna()
                y=y.reindex(index=X.index)

                #去极值 X,y 都要去掉极值，采用百分位法，将最大和最小两个2.5%的分位数的值给去掉；以后有有机会再用3σ的方法
                #X，y分别删掉头尾2.5%的数

                #X去极值,如果只是行业中性化 就不用去X的极值了
                X_normal=X
                y_med=y.mean()
                y_mad=(y-y_med).abs().mean()
                y_normal=y[(y<y_med+3.8*y_mad)&(y >y_med-3.8*y_mad)]                #y去极值
                #对其 index求交集
                common_index=y_normal.index
                X_normal=X_normal.reindex(index=common_index)
                y_normal=y_normal.reindex(index=common_index)

                m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
                m1.fit(X_normal, y_normal)
                # 残差等于
                resid = y - m1.predict(X)
                resid = resid.reindex(index=signal.columns)
                signal_neutralized.iloc[num] = resid

            return signal_neutralized

        def save_to_debts_financing(data,doc_name):
            fileHandle = open('./Z_signal_data/Asst_lia_ratio/{}.pkl'.format(doc_name), 'wb')
            pickle.dump(data, fileHandle)
            fileHandle.close()

        #获取资产负债表信息：AShareBalanceSheet

        # 负债总额  tot_liability    TOT_LIAB
        # 资产总额  tot_assets       TOT_ASSETS
        # 预收款项  ADV_FROM_CUST    ADV_FROM_CUST
        # 合同负债  Contract_lia     CONTRACT_LIABILITIES
        # 流动负债  Current_Lia      TOT_CUR_LIAB
        # 非流动负债合计 non_current_lia  TOT_NON_CUR_LIAB
        # 归属母公司股东的权益 p_Equity     TOT_SHRHLDR_EQY_EXCL_MIN_INT
        # 股东权益（含少数股东权益） Equity  TOT_SHRHLDR_EQY_INCL_MIN_INT
        # 长期借款  long_term_bond        LT_BORROW
        # 应付债券  bond_payable          BONDS_PAYABLE
        # 固定资产  fixed_ass             FIX_ASSETS
        # 可供出售金融资产  available_securities  FIN_ASSETS_AVAIL_FOR_SALE
        # 持有至到期投资  held_to_maturity_investment   HELD_TO_MTY_INVEST
        # 长期股权投资   long_term_einvestment          LONG_TERM_EQY_INVEST
        # 流动资产   current_ass        TOT_CUR_ASSETS
        # 非流动资产  no_current_ass     TOT_NON_CUR_ASSETS
        # 全部资本投入 全部投入资本=股东权益（含少数股东权益）+（有息负债） Total_Invested_Capital
        # 存货净额    inventory      INVENTORIES
        # 货币资金    monetary_capital   MONETARY_CAP
        # 交易性金融资产 tradable_fiassest  TRADABLE_FIN_ASSETS
        # 应收票据      notes_rec    NOTES_RCV
        # 应收账款      acct_rec     ACCT_RCV
        # 其他应收款     oth_rcv      oth_rcv
        # 短期借款      ST_BORROW    ST_BORROW
        # 一年内到期的非流动负债  NON_CUR_LIAB_DUE_WITHIN_1Y NON_CUR_LIAB_DUE_WITHIN_1Y
        # 应付票据      notes_payable  notes_payable
        # 净债务=带息债务-货币资金  net_debts
        # 营运资金      =流动资产-流动负债 WORKINGCAPITAL
        # 长期应付款  LT_PAYABLE  LT_PAYABLE

        # 现金流量表   中国A股现金流量表[AShareCashFlow]
        #经营活动产生的现金流量净额   NET_CASH_FLOWS_OPER_ACT   NET_CASH_FLOWS_OPER_ACT
        #投资活动产生的现金流量净额   NET_CASH_FLOWS_INV_ACT    NET_CASH_FLOWS_INV_ACT

        #利润表AShareIncome
        # 息税前利润         EBIT      EBIT
        # 息税折旧摊销前利润  EBITDA    EBITDA

        #中国A股财务费用明细[AShareFinancialExpense]
        # 利息支出/利息费用  S_STMNOTE_INTEXP  S_STMNOTE_INTEXP
        # 利息收入         S_STMNOTE_INTINC  S_STMNOTE_INTINC
        # 净利息费用       NET_INTEREST_FEE

        #获取中国A股财务指标[AShareFinancialIndicator] 这张表，直接获取财务指标--作为非ttm的对比
        # 有形资产    tangible_asst   S_FA_TANGIBLEASSET
        # 带息负债/带息债务    Interest_Bearing_Debt  S_FA_INTERESTDEBT

        # 资产负债率  S_FA_DEBTTOASSETS
        # 流动资产/ 总资产 S_FA_CATOASSETS
        # 非流动资产/总资产 S_FA_NCATOASSETS
        # 有形资产 / 总资产 S_FA_TANGIBLEASSETSTOASSETS
        # 归属母公司股东的权益/全部投入资本 S_FA_EQUITYTOTOTALCAPITAL

        AShareBalanceSheet_signals_dict=['TOT_LIAB','TOT_ASSETS','ADV_FROM_CUST','CONTRACT_LIABILITIES','TOT_CUR_LIAB',
                                         'TOT_NON_CUR_LIAB','TOT_SHRHLDR_EQY_EXCL_MIN_INT','TOT_SHRHLDR_EQY_INCL_MIN_INT',
                                         'LT_BORROW','BONDS_PAYABLE','FIX_ASSETS','FIN_ASSETS_AVAIL_FOR_SALE','HELD_TO_MTY_INVEST',
                                         'LONG_TERM_EQY_INVEST','TOT_CUR_ASSETS','TOT_NON_CUR_ASSETS','INVENTORIES',
                                         'MONETARY_CAP','TRADABLE_FIN_ASSETS','NOTES_RCV','ACCT_RCV','OTH_RCV','ST_BORROW',
                                         'NON_CUR_LIAB_DUE_WITHIN_1Y','NOTES_PAYABLE','LT_PAYABLE']

        AShareCashFlow_signals_dict=['NET_CASH_FLOWS_OPER_ACT','NET_CASH_FLOWS_INV_ACT']
        AShareIncome_signals_dict=['EBIT','EBITDA']
        AShareFinancialExpense_signals_dict=['S_STMNOTE_INTEXP','S_STMNOTE_INTINC']
        AShareFinancialIndicator_signals_dict=['S_FA_TANGIBLEASSET','S_FA_INTERESTDEBT']

        def get_AShareBalanceSheet_signals(dict):
            tp=tuple()
            for siganl_name in dict:
                # print(siganl_name)
                tp=tp+(Get_debts_related_signals_generator.get_signal_data_from_database(self,siganl_name,'wind_quant.dbo.AShareBalanceSheet',
                                                                                      start_date_lastyear,end_date,method='1yearavg',database='wind',
                                      STATEMENT_TYPE='408001000',fillna=0),)
            return (tp)

        def get_AShareCashFlow_signals(dict):
            tp=tuple()
            for siganl_name in dict:
                # print(siganl_name)
                tp=tp+(Get_debts_related_signals_generator.get_signal_data_from_database(self,siganl_name,'wind_quant.dbo.AShareCashFlow',
                                                                                      start_date_lastyear,end_date,method='ttm',database='wind',
                                      STATEMENT_TYPE='408001000',fillna=0),)
            return (tp)

        def get_AShareIncome_signals(dict):
            tp=tuple()
            for siganl_name in dict:
                # print(siganl_name)
                tp=tp+(Get_debts_related_signals_generator.get_signal_data_from_database(self,siganl_name,'wind_quant.dbo.AShareIncome',
                                                                                      start_date_lastyear,end_date,method='ttm',database='wind',
                                      STATEMENT_TYPE='408001000',fillna=0),)
            return (tp)

        def get_AShareFinancialExpense_signals(start_date_lastyear,end_date):
            # 查找 利息费用 利息收入 计算差值 除以 有息负债
            interest_sql = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD,S_STMNOTE_INTEXP,S_STMNOTE_INTINC, S_STMNOTE_FINEXP_1 from" \
                           " wind_quant.dbo.AShareFinancialExpense  " \
                           "where STATEMENT_TYPECODE='408001000' and ANN_DT >={} " \
                           "and ANN_DT <={} order by ANN_DT asc".format(start_date_lastyear, end_date)
            interest = pd.read_sql(interest_sql, self.con_wind_db)
            interest = interest.sort_values(['ANN_DT', 'S_INFO_WINDCODE'])
            interest = interest[~ interest['S_INFO_WINDCODE'].str.contains('T')]
            interest = interest[~ interest['S_INFO_WINDCODE'].str.contains('BJ|A')]

            interest['S_INFO_WINDCODE'] = interest['S_INFO_WINDCODE'].apply(windcode_to_id)
            interest = interest.fillna(0)

            # 数据清洗，发现有缺失的nan值，主要在0930这时点，
            # 清洗逻辑：如果S_STMNOTE_INTEXP,S_STMNOTE_INTINC, S_STMNOTE_FINEXP_1这三个都是0，则变为nan，反正这个nan最后也会被前值填充
            def data_clean_fun(df):
                if df['S_STMNOTE_INTEXP'] == 0 and df['S_STMNOTE_INTINC'] == 0 and df['S_STMNOTE_FINEXP_1'] == 0:
                    df['S_STMNOTE_INTEXP'] = np.nan
                    df['S_STMNOTE_INTINC'] = np.nan
                    df['S_STMNOTE_FINEXP_1'] = np.nan
                else:
                    pass
                return df

            interest = interest.apply(data_clean_fun, axis=1)
            # 数据清洗完毕 以上 完毕
            # 计算（利息费用-利息收入） 利息费用=利息支出-利息资本化金额-利息收入 中金的这个利息费用有问题 他的意思应该是利息费用=利息支出-利息资本化金额
            # 我进行修改 利息费用=利息支出-利息资本化金额 改为 利息费用=利息支出 还是包含资本化的部分
            # interest['net_interest_fee']=interest['S_STMNOTE_INTEXP']-interest['S_STMNOTE_INTINC']-interest['S_STMNOTE_FINEXP_1']
            interest['net_interest_fee'] = interest['S_STMNOTE_INTEXP'] - interest['S_STMNOTE_INTINC']

            # 计算S_STMNOTE_INTEXP的ttm
            INTEXP_TTM = interest.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']). \
                groupby('S_INFO_WINDCODE').apply(lambda x: get_TTM_value(x, 'S_STMNOTE_INTEXP', 'S_STMNOTE_INTEXP_TTM'))
            INTEXP_TTM = INTEXP_TTM.reset_index(drop=True)
            INTEXP_TTM_pv = Get_debts_related_signals_generator.get_pivot_data(self, INTEXP_TTM,
                                                                               target_columns='S_STMNOTE_INTEXP_TTM')

            # 计算S_STMNOTE_INTINC的ttm
            INTINC_TTM = interest.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']). \
                groupby('S_INFO_WINDCODE').apply(lambda x: get_TTM_value(x, 'S_STMNOTE_INTINC', 'S_STMNOTE_INTINC_TTM'))
            INTINC_TTM = INTINC_TTM.reset_index(drop=True)
            INTINC_TTM_pv = Get_debts_related_signals_generator.get_pivot_data(self, INTINC_TTM,
                                                                               target_columns='S_STMNOTE_INTINC_TTM')

            # 计算net_interest_fee的TTM
            interest = interest.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']). \
                groupby('S_INFO_WINDCODE').apply(lambda x: get_TTM_value(x, 'net_interest_fee', 'net_interest_fee_TTM'))

            interest = interest.reset_index(drop=True)

            net_interest_fee_TTM_pv = pd.pivot_table(interest, index=['ANN_DT'], columns=['S_INFO_WINDCODE'],
                                                     values=['net_interest_fee_TTM'])
            net_interest_fee_TTM_pv.columns = net_interest_fee_TTM_pv.columns.get_level_values(1)
            net_interest_fee_TTM_pv = net_interest_fee_TTM_pv.fillna(method='ffill')
            net_interest_fee_TTM_pv = net_interest_fee_TTM_pv.reindex(index=self.previous_Tradedays_list,
                                                                      columns=self.stk_pool).fillna(method='ffill')
            net_interest_fee_TTM_pv = net_interest_fee_TTM_pv.reindex(index=self.Tradedays_list,
                                                                      columns=self.stk_pool).fillna(method='ffill')

            return INTEXP_TTM_pv,INTINC_TTM_pv,net_interest_fee_TTM_pv


        def get_AShareFinancialIndicato_signals(dict):
            tp=tuple()
            for siganl_name in dict:
                # print(siganl_name)
                tp=tp+(Get_debts_related_signals_generator.get_signal_data_from_database(self,siganl_name,'wind_quant.dbo.AShareFinancialIndicator',
                                                                                      start_date_lastyear,end_date,method='avg',database='wind',
                                      STATEMENT_TYPE=None,fillna=0),)
            return (tp)


        tot_liability, tot_assets, ADV_FROM_CUST, Contract_lia, Current_Lia,non_current_lia,p_Equity,Equity,\
        long_term_bond,bond_payable,fixed_ass,available_securities,held_to_maturity_investment,long_term_einvestment,\
        current_ass,no_current_ass,inventory,monetary_capital,tradable_fiassest,notes_rec,acct_rec,oth_rcv,ST_BORROW,\
        NON_CUR_LIAB_DUE_WITHIN_1Y,notes_payable,LT_PAYABLE=get_AShareBalanceSheet_signals(AShareBalanceSheet_signals_dict)

        # {'TOT_LIAB', 'TOT_ASSETS', 'ADV_FROM_CUST', 'CONTRACT_LIABILITIES', 'TOT_CUR_LIAB',
        #  'TOT_NON_CUR_LIAB', 'TOT_SHRHLDR_EQY_EXCL_MIN_INT', 'TOT_SHRHLDR_EQY_INCL_MIN_INT',
        #  'LT_BORROW', 'BONDS_PAYABLE', 'FIX_ASSETS', 'FIN_ASSETS_AVAIL_FOR_SALE', 'HELD_TO_MTY_INVEST',
        #  'LONG_TERM_EQY_INVEST', 'TOT_CUR_ASSETS', 'TOT_NON_CUR_ASSETS', 'INVENTORIES',
        #  'MONETARY_CAP', 'TRADABLE_FIN_ASSETS', 'NOTES_RCV', 'ACCT_RCV', 'OTH_RCV', 'ST_BORROW',
        #  'NON_CUR_LIAB_DUE_WITHIN_1Y', 'NOTES_PAYABLE', 'LT_PAYABLE'}

        NET_CASH_FLOWS_OPER_ACT,NET_CASH_FLOWS_INV_ACT=get_AShareCashFlow_signals(AShareCashFlow_signals_dict)

        EBIT,EBITDA=get_AShareIncome_signals(AShareIncome_signals_dict)
        S_STMNOTE_INTEXP,S_STMNOTE_INTINC,NET_INTEREST_FEE=get_AShareFinancialExpense_signals(start_date_lastyear,end_date)
        tangible_asst,Interest_Bearing_Debt=get_AShareFinancialIndicato_signals(AShareFinancialIndicator_signals_dict)

        Ass_lia_ratio=tot_liability/tot_assets
        Noreceipts_Ass_lia_ratio=(tot_liability-ADV_FROM_CUST-Contract_lia)/tot_assets
        Debt2lt_cap=Current_Lia/(non_current_lia+p_Equity)
        lt_ass_solvency=(Equity+long_term_bond+bond_payable)/(fixed_ass+available_securities+held_to_maturity_investment+long_term_einvestment)
        cur2tot_ass=current_ass/tot_assets
        noncur2tot_ass=no_current_ass/tot_assets
        tgble2tot_ass=tangible_asst/tot_assets
        noncur_lia2equity=non_current_lia/p_Equity
        cur_lia2equity=Current_Lia/p_Equity
        Equity2invstcap=p_Equity/(Equity+Interest_Bearing_Debt)
        ibd2totinvstcap=Interest_Bearing_Debt/(Equity+Interest_Bearing_Debt)
        curlia2totlia=Current_Lia/tot_liability
        non_curlia2totlia=non_current_lia/tot_liability
        cap_imb_ratio=no_current_ass/p_Equity

        current_ratio=current_ass/Current_Lia
        quick_ratio=(current_ass-inventory)/Current_Lia
        consquick_ratio=(monetary_capital+tradable_fiassest+notes_rec+acct_rec+oth_rcv)/Current_Lia
        cash_ratio=(monetary_capital+tradable_fiassest+notes_rec)/Current_Lia
        cash2due_debt=NET_CASH_FLOWS_OPER_ACT/(ST_BORROW+NON_CUR_LIAB_DUE_WITHIN_1Y+notes_payable)
        cashflow_cvgratio=NET_CASH_FLOWS_OPER_ACT/S_STMNOTE_INTEXP
        property_ratio=tot_liability/Equity
        equity2totlia=p_Equity/tot_liability
        equity2ibd=p_Equity/Interest_Bearing_Debt
        tgbass2totlia=tangible_asst/tot_liability
        tgbass2ibd=tangible_asst/Interest_Bearing_Debt
        tgbass2netlia=tangible_asst/(Interest_Bearing_Debt-monetary_capital)
        ebitda2netlia=EBITDA/tot_liability
        opcashflow2totlia=NET_CASH_FLOWS_OPER_ACT/tot_liability
        opcashflow2ibd=NET_CASH_FLOWS_OPER_ACT/Interest_Bearing_Debt
        opcashflow2curlia=NET_CASH_FLOWS_OPER_ACT/Current_Lia
        opcashflow2netdebts=NET_CASH_FLOWS_OPER_ACT/(Interest_Bearing_Debt-monetary_capital)
        opcashflow2noncurlia=NET_CASH_FLOWS_OPER_ACT/non_current_lia
        nonfincf2curlia=(NET_CASH_FLOWS_OPER_ACT+NET_CASH_FLOWS_INV_ACT)/Current_Lia
        nonfincf2totlia=(NET_CASH_FLOWS_OPER_ACT+NET_CASH_FLOWS_INV_ACT)/tot_liability
        ebit2intexp=EBIT/S_STMNOTE_INTEXP
        ltdebt2wcap=non_current_lia/(current_ass-Current_Lia)
        ltlia_porpt=(long_term_bond+notes_payable+LT_PAYABLE)/tot_liability
        debt2tgbass=tot_liability/tangible_asst
        ebitda2ibd=EBITDA/Interest_Bearing_Debt
        tot_lia2ebitda=tot_liability/EBITDA
        ebitda2intexp=EBITDA/NET_CASH_FLOWS_OPER_ACT

        # 做一个dic方便导入
        signal_dict={'Ass_lia_ratio':Ass_lia_ratio,'Noreceipts_Ass_lia_ratio':Noreceipts_Ass_lia_ratio,'Debt2lt_cap':Debt2lt_cap,
                     'lt_ass_solvency':lt_ass_solvency,'cur2tot_ass':cur2tot_ass,'noncur2tot_ass':noncur2tot_ass,'tgble2tot_ass':tgble2tot_ass,
                     'noncur_lia2equity':noncur_lia2equity,'cur_lia2equity':cur_lia2equity,'Equity2invstcap':Equity2invstcap,
                     'ibd2totinvstcap':ibd2totinvstcap,'curlia2totlia':curlia2totlia,'non_curlia2totlia':non_curlia2totlia,
                     'cap_imb_ratio':cap_imb_ratio,'current_ratio':current_ratio,'quick_ratio':quick_ratio,'consquick_ratio':consquick_ratio,
                     'cash_ratio':cash_ratio,'cash2due_debt':cash2due_debt,'cashflow_cvgratio':cashflow_cvgratio,'property_ratio':property_ratio,
                     'property_ratio':property_ratio,'equity2totlia':equity2totlia,'equity2ibd':equity2ibd,'tgbass2totlia':tgbass2totlia,
                     'tgbass2ibd':tgbass2ibd,'tgbass2netlia':tgbass2netlia,'ebitda2netlia':ebitda2netlia,'opcashflow2totlia':opcashflow2totlia,
                     'opcashflow2ibd':opcashflow2ibd,'opcashflow2curlia':opcashflow2curlia,'opcashflow2netdebts':opcashflow2netdebts,
                     'opcashflow2noncurlia':opcashflow2noncurlia,'nonfincf2curlia':nonfincf2curlia,'nonfincf2totlia':nonfincf2totlia,
                     'ebit2intexp':ebit2intexp,'ltdebt2wcap':ltdebt2wcap,'ltlia_porpt':ltlia_porpt,'debt2tgbass':debt2tgbass,'ebitda2ibd':ebitda2ibd,
                     'tot_lia2ebitda':tot_lia2ebitda,'ebitda2intexp':ebitda2intexp}
        #
        # for signal_name in signal_dict.keys():
        #     print(signal_name)
        #     signal_data=signal_dict[signal_name]
        #     signal_data = signal_data.replace(np.inf, np.nan)
        #     signal_data = signal_data.replace(-np.inf, np.nan)
        #
        #     signal_name_ind_neutralized=signal_name+'_indN'
        #     signal_name_indsize_neutralized=signal_name+'_sizeindN'
        #
        #     # signal_data_neutralized = IND_and_SIZE_neutralize(signal_data, self.AIndexMembersCITICS1, self.stk_size_df)
        #     # signal_data_IND_neutralized = IND_neutralize(signal_data, self.AIndexMembersCITICS1)
        #
        #     save_to_debts_financing(signal_data_neutralized, signal_name_indsize_neutralized)
        #     save_to_debts_financing(signal_data_IND_neutralized, signal_name_ind_neutralized)

        for signal_name in signal_dict.keys():
            print(signal_name)
            signal_data=signal_dict[signal_name]
            signal_data = signal_data.replace(np.inf, np.nan)
            signal_data = signal_data.replace(-np.inf, np.nan)


            save_to_debts_financing(signal_data, signal_name)





        print('great!')













if __name__ == '__main__':
    getting=Get_debts_related_signals_generator()

    end_date = getting.end_date
    start_date=getting.tradedays_start
    # 因为要ttm,所以数据要往前推一年
    start_date_lastyear = int(
        (datetime.datetime.strptime(str(start_date), "%Y%m%d") - BDay(270)).strftime('%Y%m%d'))

    # test=getting.get_signal_debts_financing()
    getting.Capital_structure_and_solvency_signals_creator()
    # getting.get_signal_data_from_database('FIN_ASSETS_AVAIL_FOR_SALE','wind_quant.dbo.AShareBalanceSheet',
    #                                       start_date_lastyear,end_date,method='1yearavg',database='wind')



