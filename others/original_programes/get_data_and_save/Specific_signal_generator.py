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
    zj_NIM = signal
    AIndexMembersCITICS1 = IndexMembers
    stk_size_df = size

    zj_NIM_neutralized = zj_NIM
    AIndexMembersCITICS1 = AIndexMembersCITICS1.reindex(index=self.Tradedays_list,
                                                        columns=self.stk_pool).fillna(method='ffill')
    stk_size_df = stk_size_df.reindex(index=self.Tradedays_list, columns=self.stk_pool).fillna(method='ffill')
    for num, date in tqdm(enumerate(zj_NIM.index)):
        y = zj_NIM.iloc[num].dropna()
        X = pd.DataFrame(columns=sorted(AIndexMembersCITICS1.iloc[-1].dropna().unique().tolist()) + ['size'],
                         index=y.index)
        temp_dataframe = pd.pivot_table(AIndexMembersCITICS1.iloc[num].reset_index(),
                                        index='index', columns=date, aggfunc={date: 'count'}).fillna(0)
        temp_dataframe.columns = temp_dataframe.columns.get_level_values(1)
        temp_dataframe = temp_dataframe.reindex(index=X.index, columns=X.columns[:-1])
        X.iloc[:, :-1] = temp_dataframe.fillna(0)
        X.iloc[:, -1] = stk_size_df.iloc[num].fillna(stk_size_df.iloc[num].mean())

        m1 = LinearRegression(fit_intercept=True, copy_X=True, n_jobs=1)
        m1.fit(X, y)
        # 残差等于
        resid = y - m1.predict(X)
        resid = resid.reindex(index=zj_NIM.columns)
        zj_NIM_neutralized.iloc[num] = resid

    return zj_NIM_neutralized


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



class Get_specific_signals():


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


    def get_pivot_data(self,dataframe, target_columns, index='ANN_DT', columns='S_INFO_WINDCODE'):
        dataframe_pv = pd.pivot_table(dataframe, index=[index], columns=[columns], values=[target_columns])
        dataframe_pv.columns = dataframe_pv.columns.get_level_values(1)
        dataframe_pv = dataframe_pv.fillna(method='ffill')
        dataframe_pv = dataframe_pv.reindex(index=self.previous_Tradedays_list,
                                            columns=self.stk_pool).fillna(method='ffill')
        dataframe_pv = dataframe_pv.reindex(index=self.Tradedays_list,
                                            columns=self.stk_pool).fillna(method='ffill')
        return dataframe_pv




    # 通过DCF模型和EV的比值来做一个新的因子
    def get_signal_dcf2ev(self, reload_tradedays='all'):
        '''
        因子公式：（自由现金流TTM /（企业债10年期利率【评级A或A+-】-(期望的净利润或自由现金流增长率-带息债务增长率*0.6)/(1+有形资产收益率)））/EV
        :param reload_tradedays:
        :return:
        '''
        end_date = self.end_date
        start_date, original_dcf2ev_data = setting_startdate_and_saving_path_dictionary(
            'Z_signal_data/dcf2ev', 'dcf2ev_pivot.pkl', reload_tradedays)

        # start_date, original_dcf2ev1_data = setting_startdate_and_saving_path_dictionary(
        #     'Z_signal_data/dcf2ev1', 'dcf2ev1_pivot.pkl', reload_tradedays)

        #先提取所需要的 自由现金流TTM 企业债利率 自由现金流增长率  带息债务增长率 有形资产收益率 还有EV
        # 取自由现金流的TTM和一致预期净利润 按照7 / 3 加权
        #自由现金流TTM

        con_np_roll_data=pd.read_pickle('./Con_Forecast/con_forecast_roll_stk/con_np_roll.pkl')         # 一致预期净利润
        FCF_ADJ_3YTTM = pd.read_pickle('./basic_factor_data/Ashare_daliy_derivative_financial_indicators/FCF/FCF_ADJ_3YTTM_pivot.pkl') #自由现金流TTM
        FCF_ADJ_3YTTM=FCF_ADJ_3YTTM.fillna(con_np_roll_data)


        #企业债10年期利率【评级A或A+-】
        Enterprise_Bond_10years_yield=pd.read_pickle('./basic_factor_data/CBondCurveCNBD/Enterprise_Bond_10years_yield.pkl')
        Enterprise_BondA_10years_yield=Enterprise_Bond_10years_yield[['A']]

        #暂时用一致预期增长率来代替 发现效果更好?
        con_npcgrate_2y_roll=pd.read_pickle('./Con_Forecast/con_forecast_roll_stk/con_npcgrate_2y_roll.pkl')

        #ev
        stk_EV=pd.read_pickle('./basic_factor_data/Ashare_daliy_derivative_financial_indicators/stk_EV.pkl')

        #通过分析师预期收益在600日的波动来确定最终的安全边际系数
        con_np_roll=pd.read_pickle('./Con_Forecast/con_forecast_roll_stk/con_np_roll.pkl')
        con_safety_pmt=con_np_roll.apply(lambda x : x.rolling(window=600,min_periods=200).std()/x.rolling(window=600,min_periods=200).mean())
        con_safety_act_pmt=(con_safety_pmt.abs()**0.25)*0.15+0.15   #可以再调整参数

        #数据清洗， 一个是从20110101开始计算，第二是nan值的处理
        FCF_ADJ_3YTTM=FCF_ADJ_3YTTM.reindex(index=self.Tradedays_list,columns=self.stk_pool)
        con_np_roll_data=con_np_roll_data.reindex(index=self.Tradedays_list,columns=self.stk_pool)
        Enterprise_BondA_10years_yield=Enterprise_BondA_10years_yield.reindex(index=self.Tradedays_list)
        Enterprise_BondA_10years_yield=Enterprise_BondA_10years_yield*0.01
        con_npcgrate_2y_roll=con_npcgrate_2y_roll.reindex(index=self.Tradedays_list,columns=self.stk_pool)
        #增长率这么算 x 在 0-0.02不变 0.02-0.5 0.02+(x-0.02)/16. x在0.5-1 0.05+（x-0.5)*0.03 x 1-2 0.065+(x-1)*0.1 x>2 0.75


        # con_npcgrate_2y_roll=np.log(1+con_npcgrate_2y_roll/2).\
        #     apply(lambda x:x.apply(lambda x: x if (x<=0.02) else 0.02+(x-0.02)*0.1875 if ((x>=0.02) and (x<=0.1))
        # else 0.035 +(x-0.1)*0.0375 if ((x>=0.1) and (x<=0.5)) else 0.05+(x-0.5)*0.03 if ((x>=0.5) and (x<=0.1))
        # else 0.065+(x-1)*0.01 if  ((x>=1) and (x<=2)) else 0.075 ))             #参数可以调整 这个是作为增长率的指标，太高了大于0.6
        con_npcgrate_2y_roll=(con_npcgrate_2y_roll/2).apply(lambda x :x.apply(change_rate))

        stk_EV=stk_EV.reindex(index=self.Tradedays_list,columns=self.stk_pool)
        con_safety_act_pmt=con_safety_act_pmt.reindex(index=self.Tradedays_list,columns=self.stk_pool)



        signal_dcf2ev_from_FCF_ADJ_3YTTM=((FCF_ADJ_3YTTM/(Enterprise_BondA_10years_yield.values-con_npcgrate_2y_roll))*(1-con_safety_act_pmt))/stk_EV


        signal_dcf2ev_from_FCF_ADJ_3YTTM=np.e**(signal_dcf2ev_from_FCF_ADJ_3YTTM-1)-1



        #发现有一些垃圾股、重组股票的往往权重非常大 需要更细致的查明原因 以方便下一步继续改进 比如600180 000918 001914
        #定个规则1.把所有因子打分超过40的全部变成-1 2.打分超过20的变成20

        def tmp_value_transfer(dataframe):
            dataframe[dataframe>30]=-1
            dataframe[dataframe>=25]=25
            return dataframe

        signal_dcf2ev_from_FCF_ADJ_3YTTM=tmp_value_transfer(signal_dcf2ev_from_FCF_ADJ_3YTTM)

        # fileHandle = open('./signal_data/dcf2ev/dcf2ev_pivot.pkl', 'wb')
        # pickle.dump(signal_dcf2ev_from_FCF_ADJ_3YTTM, fileHandle)
        # fileHandle.close()
        #
        # fileHandle = open('./signal_data/dcf2ev1/dcf2ev1_pivot.pkl', 'wb')
        # pickle.dump(signal_dcf2ev_from_con_np_roll_data, fileHandle)
        # fileHandle.close()

        # 银行房地产这两个行业不知道为什么总是会大量的出现，我怀疑是数据或者行业问题 暂时去掉这两行业，出现的就默认-0.2
        MembersCITICS = pd.read_pickle(
            r'E:\AIproject\fintech\zhangyuye_programe\DATA\MembersCITICS\AIndexMembersCITICS1_dataframe.pkl')


        stk_except=MembersCITICS.iloc[-2][MembersCITICS.iloc[-2].isin(['CI005021.WI','CI005023.WI'])].index.tolist()

        exp_signal_dcf2ev_from_FCF_ADJ_3YTTM=signal_dcf2ev_from_FCF_ADJ_3YTTM


        exp_signal_dcf2ev_from_FCF_ADJ_3YTTM.loc[:, exp_signal_dcf2ev_from_FCF_ADJ_3YTTM.columns.isin(stk_except)]=np.nan
        exp_signal_dcf2ev_from_FCF_ADJ_3YTTM=exp_signal_dcf2ev_from_FCF_ADJ_3YTTM.shift(1)


        fileHandle = open('./Z_signal_data/dcf2ev/dcf2ev_pivot.pkl', 'wb')
        pickle.dump(exp_signal_dcf2ev_from_FCF_ADJ_3YTTM, fileHandle)
        fileHandle.close()

        # fileHandle = open('./signal_data/dcf2ev/dcf2ev_pivot.pkl', 'wb')
        # pickle.dump(exp_signal_dcf2ev_from_FCF_ADJ_3YTTM, fileHandle)
        # fileHandle.close()

        print('dcf2ev_pivot is setted')





    #建立因子毛销差  Gross profit margin - ratio of expenses to sales  Gross profit margin minus rate of sales EXPENSE  (GPMMROSE)
    def get_signal_gpmmrose(self, reload_tradedays='all'):
        end_date = self.end_date
        start_date,original_gpmmrose=\
            setting_startdate_and_saving_path_dataframe('Z_signal_data/gpmmrose/','gpmmrose.pkl',reload_tradedays)

        start_date,original_gpmmrose_YOY=\
            setting_startdate_and_saving_path_dataframe('Z_signal_data/gpmmrose/','gpmmrose_YOY.pkl',reload_tradedays)
        # setting_startdate_and_saving_path_dataframe('basic_factor_data/Ashare_daliy_derivative_financial_indicators/FCF','FCF_ADJ_3YTTM_pivot.pkl',reload_tradedays)

        # lambda x: str(x).rjust(6, '0')

        #查找 毛利率和销售费用率 计算差值
        sql = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD,S_QFA_GROSSPROFITMARGIN,S_QFA_SALEEXPENSETOGR from" \
              " wind_quant.dbo.AShareFinancialIndicator " \
              "where ANN_DT >={} " \
              "and ANN_DT <={} order by ANN_DT asc".format(start_date, end_date)
        gpmmrose = pd.read_sql(sql, self.con_wind_db)
        gpmmrose = gpmmrose.sort_values(['ANN_DT', 'S_INFO_WINDCODE'])
        gpmmrose = gpmmrose[~ gpmmrose['S_INFO_WINDCODE'].str.contains('T')]
        gpmmrose = gpmmrose[~ gpmmrose['S_INFO_WINDCODE'].str.contains('BJ|A')]

        gpmmrose['S_INFO_WINDCODE'] = gpmmrose['S_INFO_WINDCODE'].apply(windcode_to_id)
        gpmmrose['s_q_gpmmrose']=gpmmrose['S_QFA_GROSSPROFITMARGIN']-gpmmrose['S_QFA_SALEEXPENSETOGR']

        def Fun(dataframe):
            dataframe['s_q_gpmmrose_YOY_growth'] = dataframe['s_q_gpmmrose'].rolling(window=5, min_periods=2).\
                apply(  lambda x: (x.iloc[-1] - x.iloc[0]) / abs(x.iloc[0]))

            return dataframe

        gpmmrose = gpmmrose.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']).groupby('S_INFO_WINDCODE').apply(Fun)

        gpmmrose_pv=gpmmrose.reset_index(drop=True)

        S_q_gpmmrose_pv = pd.pivot_table(gpmmrose_pv, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['s_q_gpmmrose'])
        S_q_gpmmrose_pv.columns = S_q_gpmmrose_pv.columns.get_level_values(1)
        S_q_gpmmrose_pv = S_q_gpmmrose_pv.fillna(method='ffill')
        S_q_gpmmrose_pv=S_q_gpmmrose_pv.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        S_q_gpmmrose_pv_YOY = pd.pivot_table(gpmmrose_pv, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['s_q_gpmmrose_YOY_growth'])
        S_q_gpmmrose_pv_YOY.columns = S_q_gpmmrose_pv_YOY.columns.get_level_values(1)
        S_q_gpmmrose_pv_YOY = S_q_gpmmrose_pv_YOY.fillna(method='ffill')
        S_q_gpmmrose_pv_YOY=S_q_gpmmrose_pv_YOY.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')



        fileHandle = open('./Z_signal_data/gpmmrose/gpmmrose.pkl', 'wb')
        pickle.dump(S_q_gpmmrose_pv, fileHandle)
        fileHandle.close()


        fileHandle = open('./Z_signal_data/gpmmrose/gpmmrose_YOY.pkl', 'wb')
        pickle.dump(S_q_gpmmrose_pv_YOY, fileHandle)
        fileHandle.close()

        print('gpmmrose data is saved')






    #建立合同负债 	CONTRACT_LIABILITIES
    def get_signal_contrli(self, reload_tradedays='all'):
        end_date = self.end_date
        start_date,original_contrli=\
            setting_startdate_and_saving_path_dataframe('Z_signal_data/contrli/','contrli.pkl',reload_tradedays)

        start_date,original_contrli_YOY=\
            setting_startdate_and_saving_path_dataframe('Z_signal_data/contrli/','contrli_YOY.pkl',reload_tradedays)

        #查找 合同负债 计算差值
        sql = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD,CONTRACT_LIABILITIES from" \
              " wind_quant.dbo.AShareBalanceSheet " \
              "where STATEMENT_TYPE='408001000' and ANN_DT >={} " \
              "and ANN_DT <={} order by ANN_DT asc".format(start_date, end_date)
        contrli = pd.read_sql(sql, self.con_wind_db)
        contrli = contrli.sort_values(['ANN_DT', 'S_INFO_WINDCODE'])
        contrli = contrli[~ contrli['S_INFO_WINDCODE'].str.contains('T')]
        contrli = contrli[~ contrli['S_INFO_WINDCODE'].str.contains('BJ|A')]

        contrli['S_INFO_WINDCODE'] = contrli['S_INFO_WINDCODE'].apply(windcode_to_id)

        def Fun(dataframe):
            dataframe['contrli_YOY_growth'] = dataframe['CONTRACT_LIABILITIES'].rolling(window=5, min_periods=2).\
                apply(  lambda x: (x.iloc[-1] - x.iloc[0]) / abs(x.iloc[0]))

            dataframe['contrli_QOQ_growth'] = dataframe['CONTRACT_LIABILITIES'].rolling(window=2, min_periods=2).\
                apply(  lambda x: (x.iloc[-1] - x.iloc[0]) / abs(x.iloc[0]))
            return dataframe

        contrli = contrli.sort_values(['S_INFO_WINDCODE', 'REPORT_PERIOD']).groupby('S_INFO_WINDCODE').apply(Fun)

        contrli_pv=contrli.reset_index(drop=True)

        S_contrli_pv = pd.pivot_table(contrli_pv, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['CONTRACT_LIABILITIES'])
        S_contrli_pv.columns = S_contrli_pv.columns.get_level_values(1)
        S_contrli_pv = S_contrli_pv.fillna(method='ffill')
        S_contrli_pv=S_contrli_pv.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        S_contrli_pv_YOY = pd.pivot_table(contrli_pv, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['contrli_YOY_growth'])
        S_contrli_pv_YOY.columns = S_contrli_pv_YOY.columns.get_level_values(1)
        S_contrli_pv_YOY = S_contrli_pv_YOY.fillna(method='ffill')
        S_contrli_pv_YOY=S_contrli_pv_YOY.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')


        S_contrli_pv_QOQ = pd.pivot_table(contrli_pv, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['contrli_QOQ_growth'])
        S_contrli_pv_QOQ.columns = S_contrli_pv_QOQ.columns.get_level_values(1)
        S_contrli_pv_QOQ = S_contrli_pv_QOQ.fillna(method='ffill')
        S_contrli_pv_QOQ=S_contrli_pv_QOQ.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')


        #查找 营业收入，方便计算占比
        rev_sql = "select S_INFO_WINDCODE,ANN_DT,REPORT_PERIOD,TOT_OPER_REV from" \
              " wind_quant.dbo.AShareIncome " \
              "where STATEMENT_TYPE='408001000' and ANN_DT >={} " \
              "and ANN_DT <={} order by ANN_DT asc".format(start_date, end_date)
        tot_rev = pd.read_sql(rev_sql, self.con_wind_db)
        tot_rev = tot_rev.sort_values(['ANN_DT', 'S_INFO_WINDCODE'])
        tot_rev = tot_rev[~ tot_rev['S_INFO_WINDCODE'].str.contains('T')]
        tot_rev = tot_rev[~ tot_rev['S_INFO_WINDCODE'].str.contains('BJ|A')]

        tot_rev['S_INFO_WINDCODE'] = tot_rev['S_INFO_WINDCODE'].apply(windcode_to_id)


        tot_rev=tot_rev.reset_index(drop=True)

        S_tot_rev_pv = pd.pivot_table(tot_rev, index=['ANN_DT'], columns=['S_INFO_WINDCODE'], values=['TOT_OPER_REV'])
        S_tot_rev_pv.columns = S_tot_rev_pv.columns.get_level_values(1)
        S_tot_rev_pv = S_tot_rev_pv.fillna(method='ffill')
        S_tot_rev_pv=S_tot_rev_pv.reindex(index=self.Tradedays_list,columns=self.stk_pool).fillna(method='ffill')

        S_contrli_pro_of_rev=S_contrli_pv/S_tot_rev_pv

        fileHandle = open('./Z_signal_data/contrli/contrli.pkl', 'wb')
        pickle.dump(S_contrli_pv, fileHandle)
        fileHandle.close()


        fileHandle = open('./Z_signal_data/contrli/contrli_YOY.pkl', 'wb')
        pickle.dump(S_contrli_pv_YOY, fileHandle)
        fileHandle.close()


        fileHandle = open('./Z_signal_data/contrli/contrli_QOQ.pkl', 'wb')
        pickle.dump(S_contrli_pv_QOQ, fileHandle)
        fileHandle.close()


        fileHandle = open('./Z_signal_data/contrli/contrli_prop_of_rev.pkl', 'wb')
        pickle.dump(S_contrli_pro_of_rev, fileHandle)
        fileHandle.close()

        print('contrli data is saved')



















if __name__ == '__main__':
    getting=Get_specific_signals()

    # test=getting.get_signal_debts_financing()

    stk_list=getting.get_signal_dcf2ev()
    gpmmrose=getting.get_signal_gpmmrose()
    getting.get_signal_contrli()



