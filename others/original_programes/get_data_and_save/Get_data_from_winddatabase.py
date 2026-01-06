#!/usr/bin/env python
#-*- utf-8 -*-

'''
Created on Apr 24 2022
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
import re



def close_form_OLS(X, y):
    theta = np.matmul(np.matmul(np.linalg.inv(np.matmul(X.T, X)), X.T), y)
    resid = y - np.matmul(X, theta)
    return theta, resid

def windcode_to_id(windcode):
    return int(str(windcode).split('.')[0])


def setting_startdate_and_saving_path_dataframe(dir_path, doc_path, reload_tradedays):
    if reload_tradedays == 'all':
        start_date = int('20120101')
        start_date_halfyear = int('20120601')
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
        start_date = int('20120101')
        original_data = pd.DataFrame()
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

class Get_data_from_winddatabase():
    '''
      #要注意港股的日期和a股的不一样，后续要改进
        会导致get_Index_daily_pct这个函数有问题，相当于港股没有放假但是a股没放假的需要累计没有累计 这要从
                HK_stk_pct_path=r'E:\project\ai_project\ai\zhangyuye_programe\DATA\HK_basic_factor_data\HK_stk_pct.pkl'
        stk_pct_data = pd.read_pickle(stk_pct_path)
        HKstk_pct_data = pd.read_pickle(HK_stk_pct_path)   #要注意港股的日期和a股的不一样，后续要改进 这个地方改进 改完再删
    '''


    def __init__(self):
        # df_stk_pct_data.columns = df_stk_pct_data.columns.astype('str')
        # self.stk_pct_data = df_stk_pct_data
        # self.date = df_stk_pct_data.index
        print('data_processor_start')
        self.con_wind_db = pymssql.connect('v-wind', 'trade', 'trade', 'wind_quant',charset='cp936')
        self.con_gogoal_db = pymssql.connect('p-ma-mars', 'sig', 'sig', 'FundRiskControl2',charset='cp936')

        self.end_date = int(datetime.date.today().strftime('%Y%m%d'))
        self.tradedays_start=20030101
        sql = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' and TRADE_DAYS >={} " \
              "and TRADE_DAYS <={} order by TRADE_DAYS asc".format(self.tradedays_start,self.end_date)
        data = pd.read_sql(sql, self.con_wind_db)
        self.Tradedays_list = data['TRADE_DAYS'].tolist()

        # 中国A股中信指数成份股[AIndexMembersCITICS]一级
        sql1 = "select distinct(S_INFO_WINDCODE) from wind_quant.dbo.AIndexMembersCITICS  "
        # 中国A股中信指数成份股[AIndexMembersCITICS]二级
        sql2 = "select distinct(S_INFO_WINDCODE) from wind_quant.dbo.AIndexMembersCITICS2  "
        # 中国A股中信指数成份股[AIndexMembersCITICS]三级
        sql3 = "select distinct(S_INFO_WINDCODE) from wind_quant.dbo.AIndexMembersCITICS3  "

        self.CITICS1_LIST=pd.read_sql(sql1, self.con_wind_db)['S_INFO_WINDCODE'].tolist()
        self.CITICS2_LIST = pd.read_sql(sql2, self.con_wind_db)['S_INFO_WINDCODE'].tolist()
        self.CITICS3_LIST = pd.read_sql(sql3, self.con_wind_db)['S_INFO_WINDCODE'].tolist()






    def Get_MembersCITICS(self):
        '''
        这个函数是存放日期、股票、中信行业指数的三围数据的dataframe（主要是方便通过股票找到对应行业）
        在存放一个基于词的字典：字典第一层：日期 第二层：中心行业指数 第三层 ：股票池、对应每天的收益率
        :return:
        '''

        reload_tradedays = 'all'
        setting_startdate_and_saving_path_dataframe('MembersCITICS','AIndexMembersCITICS1_dataframe.pkl',reload_tradedays)
        setting_startdate_and_saving_path_dataframe('MembersCITICS', 'AIndexMembersCITICS2_dataframe.pkl', reload_tradedays)
        setting_startdate_and_saving_path_dataframe('MembersCITICS', 'AIndexMembersCITICS3_dataframe.pkl', reload_tradedays)
        setting_startdate_and_saving_path_dataframe('signal_data/analystsentiment60/MembersHK', 'HKIndexMembersHSI_dataframe.pkl',
                                                    reload_tradedays)
        setting_startdate_and_saving_path_dataframe('signal_data/analystsentiment60/MembersHK', 'HKndexMembersHSTECH_dataframe.pkl',
                                                    reload_tradedays)


        setting_startdate_and_saving_path_dataframe('MembersCITICS','AIndexMembersCITICS1_dict.pkl',reload_tradedays)
        setting_startdate_and_saving_path_dataframe('MembersCITICS', 'AIndexMembersCITICS2_dict.pkl', reload_tradedays)
        setting_startdate_and_saving_path_dataframe('MembersCITICS', 'AIndexMembersCITICS3_dict.pkl', reload_tradedays)
        setting_startdate_and_saving_path_dataframe('signal_data/analystsentiment60/MembersHK', 'HKIndexMembersHSI_dict.pkl',
                                                    reload_tradedays)
        setting_startdate_and_saving_path_dataframe('signal_data/analystsentiment60/MembersHK', 'HKIndexMembersHSTECH_dict.pkl',
                                                    reload_tradedays)




        def Seeking_members_at_certain_date(dataframe, date):
            date = int(date)
            tmp_df = dataframe[((dataframe['S_CON_INDATE'] <= date) & (dataframe['S_CON_OUTDATE'] > date)) | (
                    (dataframe['S_CON_INDATE'] <= date) & (dataframe['S_CON_OUTDATE'].isna()))]
            tmp_list_df=tmp_df.set_index('S_CON_WINDCODE')['S_INFO_WINDCODE'].reindex\
                (index=sorted(dataframe['S_CON_WINDCODE'].unique()))

            # return pd.DataFrame() if len(tmp_df['S_CON_WINDCODE']) == 0 else tmp_list_df
            return tmp_list_df

        def get_MembersCITICS_dataframe(tradedays_list, AIndexMembersCITICS):
            # 去掉有奇葩关键词的股票
            # AIndexMembersCITICS = AIndexMembersCITICS[~AIndexMembersCITICS['S_CON_WINDCODE'].str.contains('!')]

            # AIndexMembersCITICS['S_CON_WINDCODE'] = AIndexMembersCITICS['S_CON_WINDCODE'].apply(
            #     lambda x: re.sub('A|T|B|I|J', '', x))
            # AIndexMembersCITICS['S_CON_WINDCODE'] = AIndexMembersCITICS['S_CON_WINDCODE'].apply(windcode_to_id)
            MembersCITICS1_dataframe = pd.DataFrame(index=tradedays_list,
                                                    columns=sorted(AIndexMembersCITICS['S_CON_WINDCODE'].unique()))
            AIndexMembersCITICS['S_CON_OUTDATE'] = AIndexMembersCITICS['S_CON_OUTDATE'].apply(
                lambda x: x if pd.isna(x) else int(x))
            AIndexMembersCITICS['S_CON_INDATE'] = AIndexMembersCITICS['S_CON_INDATE'].apply(
                lambda x: x if pd.isna(x) else int(x))
            for num, date in tqdm(enumerate(MembersCITICS1_dataframe.index.to_list())):
                index_date_dataframe = Seeking_members_at_certain_date(AIndexMembersCITICS, date)
                MembersCITICS1_dataframe.loc[date] = index_date_dataframe
            return MembersCITICS1_dataframe

        def get_HKMembers_dataframe(tradedays_list, AIndexMembersCITICS):

            MembersCITICS1_dataframe = pd.DataFrame(index=tradedays_list,
                                                    columns=sorted(AIndexMembersCITICS['S_CON_WINDCODE'].unique()))
            AIndexMembersCITICS['S_CON_OUTDATE'] = AIndexMembersCITICS['S_CON_OUTDATE'].apply(
                lambda x: x if pd.isna(x) else int(x))
            AIndexMembersCITICS['S_CON_INDATE'] = AIndexMembersCITICS['S_CON_INDATE'].apply(
                lambda x: x if pd.isna(x) else int(x))
            for num, date in tqdm(enumerate(MembersCITICS1_dataframe.index.to_list())):
                index_date_dataframe = Seeking_members_at_certain_date(AIndexMembersCITICS, date)
                MembersCITICS1_dataframe.loc[date] = index_date_dataframe
            return MembersCITICS1_dataframe

        # start_date=MembersCITICS_dataframe.index[0]
        # end_date=MembersCITICS_dataframe.index[-1]
        # 中国A股中信指数成份股[AIndexMembersCITICS]一级
        sql1 = "select S_INFO_WINDCODE,S_CON_WINDCODE,S_CON_INDATE,S_CON_OUTDATE,CUR_SIGN " \
              "from wind_quant.dbo.AIndexMembersCITICS order by S_CON_INDATE asc "
        # 中国A股中信指数成份股[AIndexMembersCITICS]二级
        sql2 = "select S_INFO_WINDCODE,S_CON_WINDCODE,S_CON_INDATE,S_CON_OUTDATE,CUR_SIGN " \
              "from wind_quant.dbo.AIndexMembersCITICS2 order by S_CON_INDATE asc "
        # 中国A股中信指数成份股[AIndexMembersCITICS]三级
        sql3 = "select S_INFO_WINDCODE,S_CON_WINDCODE,S_CON_INDATE,S_CON_OUTDATE,CUR_SIGN " \
              "from wind_quant.dbo.AIndexMembersCITICS3 order by S_CON_INDATE asc "
        # #香港股票指数成份股[HKStockIndexMembers]--恒生指数HSI.HI
        # sql4 = "select S_INFO_WINDCODE,S_CON_WINDCODE,S_CON_INDATE,S_CON_OUTDATE,CUR_SIGN " \
        #       "from wind_quant.dbo.HKStockIndexMembers where S_INFO_WINDCODE='HSI.HI'   order by S_CON_INDATE asc "
        #
        # #香港股票指数成份股[HKStockIndexMembers]--恒生科技指数HSTECH.HI
        # sql5 = "select S_INFO_WINDCODE,S_CON_WINDCODE,S_CON_INDATE,S_CON_OUTDATE,CUR_SIGN " \
        #       "from wind_quant.dbo.HKStockIndexMembers where S_INFO_WINDCODE='HSTECH.HI'   order by S_CON_INDATE asc "

        AIndexMembersCITICS1 = pd.read_sql(sql1, self.con_wind_db)
        AIndexMembersCITICS2 = pd.read_sql(sql2, self.con_wind_db)
        AIndexMembersCITICS3 = pd.read_sql(sql3, self.con_wind_db)
        # HKIndexMembersHSI = pd.read_sql(sql4, self.con_wind_db)
        # HKIndexMembersHSTECH = pd.read_sql(sql5, self.con_wind_db)

        tradedays_list=self.Tradedays_list

        def flit_df(df):
            df = df[~ df['S_CON_WINDCODE'].str.contains('T')]
            df = df[~ df['S_CON_WINDCODE'].str.contains('BJ')]
            df = df[~ df['S_CON_WINDCODE'].str.contains('A')]
            df['S_CON_WINDCODE']=df['S_CON_WINDCODE'].apply(windcode_to_id)

            return df

        AIndexMembersCITICS1=flit_df(AIndexMembersCITICS1)
        AIndexMembersCITICS2=flit_df(AIndexMembersCITICS2)
        AIndexMembersCITICS3=flit_df(AIndexMembersCITICS3)




        MembersCITICS1_dataframe = get_MembersCITICS_dataframe(tradedays_list, AIndexMembersCITICS1)
        MembersCITICS2_dataframe = get_MembersCITICS_dataframe(tradedays_list, AIndexMembersCITICS2)
        MembersCITICS3_dataframe = get_MembersCITICS_dataframe(tradedays_list, AIndexMembersCITICS3)


       # 存储dataframe文件 index是日期 columns是股票代码
        save_doc_pickle(MembersCITICS1_dataframe,'MembersCITICS','AIndexMembersCITICS1_dataframe.pkl')
        save_doc_pickle(MembersCITICS2_dataframe, 'MembersCITICS', 'AIndexMembersCITICS2_dataframe.pkl')
        save_doc_pickle(MembersCITICS3_dataframe, 'MembersCITICS', 'AIndexMembersCITICS3_dataframe.pkl')


        #
        # def get_Membersindex_idct(stk_pct_data,MembersCITICS1_dataframe,Mutualfund_portfolio_stk_property):
        #     MembersCITICS1_idct = {}
        #     # 用stk_pct_data index是因为三个dataframe index最后一天不一样 用stk_pct_data少了一天
        #     MembersCITICS1_dataframe=MembersCITICS1_dataframe[MembersCITICS1_dataframe.index.isin(stk_pct_data.index)]
        #     for date in tqdm(MembersCITICS1_dataframe.index):
        #         MembersCITICS1_idct[date] = {}
        #         if len(sorted(MembersCITICS1_dataframe.loc[date].dropna().astype('str').unique().tolist()))==0:
        #             pass
        #         else:
        #             for index_name in sorted(
        #                     MembersCITICS1_dataframe.loc[date].dropna().astype('str').unique().tolist()):
        #                 MembersCITICS1_idct[date][index_name] = {}
        #                 MembersCITICS1_idct[date][index_name]['portfolio'] = MembersCITICS1_dataframe.loc[date][
        #                     MembersCITICS1_dataframe.loc[date] == index_name].index.tolist()
        #                 MF_index_value_df = Mutualfund_portfolio_stk_property.loc[date][
        #                     Mutualfund_portfolio_stk_property.loc[date].
        #                         index.isin(MembersCITICS1_idct[date][index_name]['portfolio'])]
        #                 MF_index_daily_pct_df = stk_pct_data.loc[date][stk_pct_data.loc[date].
        #                     index.isin(MembersCITICS1_idct[date][index_name]['portfolio'])].reindex(
        #                     index=MF_index_value_df.index).fillna(0)
        #                 MembersCITICS1_idct[date][index_name]['daily_pct'] = (
        #                                                                                  MF_index_value_df * MF_index_daily_pct_df).sum() \
        #                                                                      / MF_index_value_df.sum() / 100
        #
        #     return MembersCITICS1_idct
        #
        # #准备dict
        # stk_pct_path=r'E:\project\ai_project\ai\zhangyuye_programe\DATA\basic_factor_data\stk_pct_data.pkl'
        # HK_stk_pct_path=r'E:\project\ai_project\ai\zhangyuye_programe\DATA\HK_basic_factor_data\HK_stk_pct.pkl'
        # stk_pct_data = pd.read_pickle(stk_pct_path)
        # HKstk_pct_data = pd.read_pickle(HK_stk_pct_path)   #要注意港股的日期和a股的不一样，后续要改进
        # #MembersCITICS1_dataframe_dict 准备function
        # MembersCITICS1_dict=get_Membersindex_idct(stk_pct_data,MembersCITICS1_dataframe,Mutualfund_portfolio_stk_property)
        # MembersCITICS2_dict = get_Membersindex_idct(stk_pct_data, MembersCITICS2_dataframe,
        #                                              Mutualfund_portfolio_stk_property)
        # MembersCITICS3_dict = get_Membersindex_idct(stk_pct_data, MembersCITICS3_dataframe,
        #                                              Mutualfund_portfolio_stk_property)
        #
        #
        # MembersHSI_dict = get_Membersindex_idct(HKstk_pct_data, MembersHSI_dataframe,
        #                                              Mutualfund_portfolio_HKstk_property)
        # MembersHSTECH_dict = get_Membersindex_idct(HKstk_pct_data, MembersHSTECH_dataframe,
        #                                              Mutualfund_portfolio_HKstk_property)
        #
        #
        # save_doc_pickle(MembersCITICS1_dict,'MembersCITICS','AIndexMembersCITICS1_dict.pkl')
        # save_doc_pickle(MembersCITICS2_dict, 'MembersCITICS', 'AIndexMembersCITICS2_dict.pkl')
        # save_doc_pickle(MembersCITICS3_dict, 'MembersCITICS', 'AIndexMembersCITICS3_dict.pkl')
        # save_doc_pickle(MembersHSI_dict, 'MembersHK', 'HKIndexMembersHSI_dict.pkl')
        # save_doc_pickle(MembersHSTECH_dict, 'MembersHK', 'HKIndexMembersHSTECH_dict.pkl')




    def Get_Fund_IndexCITICS_weight(self,fund_id,reload_tradedays='all'):
        setting_startdate_and_saving_path_dataframe(
            'Fund_AssetsPortfolio/{}/Fund_IndexCITICS_weight'.format(fund_id),
            'Fund_{}_IndexCITICS1_weight.pkl'.format(fund_id), reload_tradedays)


        Fund_STK_Assets = pd.read_pickle(
            './Fund_AssetsPortfolio/{}/Fund_StkPortfolio/Fund_{}_StkPortfolio.pkl'.format(fund_id, fund_id))

        AIndexMembersCITICS1_dataframe=pd.read_pickle('./MembersCITICS/AIndexMembersCITICS1_dataframe.pkl')
        AIndexMembersCITICS2_dataframe = pd.read_pickle('./MembersCITICS/AIndexMembersCITICS2_dataframe.pkl')
        AIndexMembersCITICS3_dataframe = pd.read_pickle('./MembersCITICS/AIndexMembersCITICS3_dataframe.pkl')
        HKIndexMembersHSI_dataframe = pd.read_pickle(
            'signal_data/analystsentiment60/MembersHK/HKIndexMembersHSI_dataframe.pkl')
        HKIndexMembersHSTECH_dataframe = pd.read_pickle(
            'signal_data/analystsentiment60/MembersHK/HKIndexMembersHSTECH_dataframe.pkl')

        def Fund_IndexCITICS_weight_processor(Fund_STK_Assets,AIndexMembersCITICS,HKIndexMembersHSI,HKIndexMembersHSTECH):
            Fund_Ashare_df=Fund_STK_Assets['AShare_stk_portfolio']
            Fund_HKstk_df=Fund_STK_Assets['HK_stk_portfolio']

            FUND_AIndex_weight=Fund_Ashare_df[['ANN_DATE','RPT_DATE','STK_ID','WEIGHT']]
            FUND_HKIndex_weight = Fund_HKstk_df[['ANN_DATE', 'RPT_DATE', 'STK_ID', 'WEIGHT']]






           #处理一下warning这个问题
            FUND_AIndex_weight['STK_ID']=FUND_AIndex_weight.copy().apply(lambda x :
                                                                  AIndexMembersCITICS.loc[x['ANN_DATE'],x['STK_ID']] if
                                                                  x['ANN_DATE'] in AIndexMembersCITICS.index else
                                                                  AIndexMembersCITICS.loc[AIndexMembersCITICS.index[0],x['STK_ID']]  ,axis=1)
            if len(Fund_HKstk_df[['ANN_DATE', 'RPT_DATE', 'STK_ID', 'WEIGHT']])!=0:
                FUND_HKIndex_weight['STK_ID'] =FUND_HKIndex_weight.copy().apply(lambda x:
                                                 HKIndexMembersHSTECH.loc[x['ANN_DATE'], x['STK_ID']] if
                                                 (x['ANN_DATE'] in HKIndexMembersHSTECH.index) and (
                                                         x['STK_ID'] in HKIndexMembersHSTECH.columns) else
                                                 HKIndexMembersHSI.loc[x['ANN_DATE'], x['STK_ID']] if
                                                 (x['ANN_DATE'] in HKIndexMembersHSI.index) and (x[
                                                                                                     'STK_ID'] in HKIndexMembersHSI.columns) else
                                                 'HSI.HI', axis=1)  # 留个口子 以后可以再细化


            FUND_Index_weight=FUND_AIndex_weight.append(FUND_HKIndex_weight).sort_values(['ANN_DATE','RPT_DATE','STK_ID'])
            FUND_Index_weight=FUND_Index_weight.groupby(['ANN_DATE', 'RPT_DATE', 'STK_ID']).sum().reset_index()

            FUND_Index_weight.columns=['ANN_DATE', 'RPT_DATE', 'INDEX_ID', 'WEIGHT']

            return FUND_Index_weight



        Fund_IndexCITICS1_weight=Fund_IndexCITICS_weight_processor(Fund_STK_Assets,AIndexMembersCITICS1_dataframe,
                                                                   HKIndexMembersHSI_dataframe,
                                                                   HKIndexMembersHSTECH_dataframe)

        Fund_IndexCITICS2_weight=Fund_IndexCITICS_weight_processor(Fund_STK_Assets,AIndexMembersCITICS2_dataframe,
                                                                   HKIndexMembersHSI_dataframe,
                                                                   HKIndexMembersHSTECH_dataframe)

        Fund_IndexCITICS3_weight=Fund_IndexCITICS_weight_processor(Fund_STK_Assets,AIndexMembersCITICS3_dataframe,
                                                                   HKIndexMembersHSI_dataframe,
                                                                   HKIndexMembersHSTECH_dataframe)



        save_doc_pickle(Fund_IndexCITICS1_weight,
                        'Fund_AssetsPortfolio/{}/Fund_IndexCITICS_weight'.format(fund_id),
                        'Fund_{}_IndexCITICS1_weight.pkl'.format(fund_id))
        save_doc_pickle(Fund_IndexCITICS2_weight,
                        'Fund_AssetsPortfolio/{}/Fund_IndexCITICS_weight'.format(fund_id),
                        'Fund_{}_IndexCITICS2_weight.pkl'.format(fund_id))
        save_doc_pickle(Fund_IndexCITICS3_weight,
                        'Fund_AssetsPortfolio/{}/Fund_IndexCITICS_weight'.format(fund_id),
                        'Fund_{}_IndexCITICS3_weight.pkl'.format(fund_id))





if __name__ == '__main__':
    getting=Get_data_from_winddatabase()
    getting.Get_MembersCITICS()


    print('good')


