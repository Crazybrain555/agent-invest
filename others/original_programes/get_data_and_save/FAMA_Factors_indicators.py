
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


class Get_FAMA_data():


    def __init__(self):
        # df_stk_pct_data.columns = df_stk_pct_data.columns.astype('str')
        # self.stk_pct_data = df_stk_pct_data
        # self.date = df_stk_pct_data.index
        print('data_processor_start')
        self.con_wind_db = pymssql.connect('v-wind', 'trade', 'trade', 'wind_quant',charset='cp936')
        self.con_gogoal_db = pymssql.connect('p-ma-mars', 'sig', 'sig', 'FundRiskControl2',charset='cp936')

        self.end_date = int(datetime.date.today().strftime('%Y%m%d'))



    # @nb.jit()
    # def IS_FAMA3FACTOR_factor_form(self,excess_return_of_stk, market_risk, SMB, HML):
    #     df_IS = pd.DataFrame(index=SMB.index, columns=SMB.columns)
    #     fama_df = pd.DataFrame(columns=['EXC_R', 'MKT', 'SMB', 'HML'], index=list(range(60)))
    #
    #     for i in tqdm(range(len(df_IS.index) - 60)):
    #         print(i)
    #
    #         for j in range(len(df_IS.columns)):
    #             # print(df_IS.columns[j],j)
    #             # fama_df=fama_df1.copy()
    #             fama_df.loc[:, 'EXC_R'] = excess_return_of_stk.iloc[i:i + 60, j].values
    #             fama_df.loc[:, 'MKT'] = market_risk[i:i + 60].values
    #             fama_df.loc[:, 'SMB'] = SMB.iloc[i:i + 60, j].values
    #             fama_df.loc[:, 'HML'] = HML.iloc[i:i + 60, j].values
    #             # fama_df=fama_df.dropna()
    #             if len(fama_df.dropna()) < 30:
    #                 residual = np.nan
    #                 df_IS.iloc[i + 60, j] = residual
    #             else:
    #                 model = sm.ols(formula='EXC_R~MKT+SMB+HML', data=fama_df).fit()
    #                 residual = model.resid.skew()
    #                 df_IS.iloc[i + 60, j] = residual
    #
    #     return df_IS
    # @nb.jit()
    def IS_return_factor_form(self,excess_return_of_stk,least_num=45,T_days=66,doc_name='factor'):

        # check the path exist or not
        if not os.path.exists('./factor_Idiosyncratic_skewness/'):
            print('# factor_Idiosyncratic_skewness path not exist , creating...... ')
            os.makedirs('./factor_Idiosyncratic_skewness/')
        else:
            print('factor_Idiosyncratic_skewness has already existed ')

        df_IS = pd.DataFrame(index=excess_return_of_stk.index, columns=excess_return_of_stk.columns)
        fama_df = pd.DataFrame(columns=['EXC_R'], index=list(range(T_days)))

        for i in tqdm(range(len(df_IS.index) - T_days)):
            # print(i)

            for j in range(len(df_IS.columns)):
                # print(df_IS.columns[j],j)
                # fama_df=fama_df1.copy()
                fama_df.loc[:, 'EXC_R'] = excess_return_of_stk.iloc[i:i + T_days, j].values


                # fama_df=fama_df.dropna()
                if len(fama_df.dropna()) < least_num:
                    residual = np.nan
                    df_IS.iloc[i + T_days, j] = residual
                    # print(0)
                # @nb.jit()
                else:

                    tmp_fama_df=fama_df.dropna().copy()

                    residual=pd.DataFrame(fama_df.loc[:, 'EXC_R']).skew()[0]
                    df_IS.iloc[i + T_days, j] = residual


                    # model = sm.ols(formula='EXC_R~MKT+SMB+HML+RMW+CMA', data=fama_df).fit()
                    # residual = model.resid.skew()
                    # df_IS.iloc[i + 60, j] = residual

        df_IS=df_IS.iloc[T_days:,:]

        fileHandle = open('./factor_Idiosyncratic_skewness/Factor_return_skew__IS_T{}_{}.pkl'.format(T_days,doc_name), 'wb')
        pickle.dump(df_IS, fileHandle)
        fileHandle.close()

        return df_IS

        # @nb.jit()


    # @nb.jit()
    def IS_FAMA5FACTOR_factor_form(self,excess_return_of_stk, market_risk, SMB, HML,RMW,CMA,least_num=45,T_days=66,doc_name='factor'):

        # check the path exist or not
        if not os.path.exists('./factor_Idiosyncratic_skewness/'):
            print('# factor_Idiosyncratic_skewness path not exist , creating...... ')
            os.makedirs('./factor_Idiosyncratic_skewness/')
        else:
            print('factor_Idiosyncratic_skewness has already existed ')

        df_IS = pd.DataFrame(index=excess_return_of_stk.index, columns=excess_return_of_stk.columns)
        fama_df = pd.DataFrame(columns=['EXC_R','ALPHA', 'MKT', 'SMB', 'HML','RMW','CMA'], index=list(range(T_days)))

        for i in tqdm(range(len(df_IS.index) - T_days)):
            # print(i)

            for j in range(len(df_IS.columns)):
                # print(df_IS.columns[j],j)
                # fama_df=fama_df1.copy()
                fama_df.loc[:, 'EXC_R'] = excess_return_of_stk.iloc[i:i + T_days, j].values
                fama_df.loc[:, 'ALPHA'] = 1
                fama_df.loc[:, 'MKT'] = market_risk[i:i + T_days].values
                fama_df.loc[:, 'SMB'] = SMB.iloc[i:i + T_days].values
                fama_df.loc[:, 'HML'] = HML.iloc[i:i + T_days].values
                fama_df.loc[:, 'RMW'] = RMW.iloc[i:i + T_days].values
                fama_df.loc[:, 'CMA'] = CMA.iloc[i:i + T_days].values

                # fama_df=fama_df.dropna()
                if len(fama_df.dropna()) < least_num:
                    residual = np.nan
                    df_IS.iloc[i + T_days, j] = residual
                    # print(0)
                # @nb.jit()
                else:

                    tmp_fama_df=fama_df.dropna().copy()
                    X=tmp_fama_df[['ALPHA', 'MKT', 'SMB', 'HML', 'RMW', 'CMA']].values
                    y=tmp_fama_df[['EXC_R']].values
                    theta, resid = close_form_OLS(X, y)
                    residual=pd.DataFrame(resid).skew()[0]
                    df_IS.iloc[i + T_days, j] = residual


                    # model = sm.ols(formula='EXC_R~MKT+SMB+HML+RMW+CMA', data=fama_df).fit()
                    # residual = model.resid.skew()
                    # df_IS.iloc[i + 60, j] = residual

        df_IS=df_IS.iloc[T_days:,:]

        fileHandle = open('./factor_Idiosyncratic_skewness/Factor_FAMA5factors_IS_T{}_{}.pkl'.format(T_days,doc_name), 'wb')
        pickle.dump(df_IS, fileHandle)
        fileHandle.close()

        return df_IS

        # @nb.jit()

    def IS_FAMA3FACTOR_factor_form(self, excess_return_of_stk, market_risk, SMB, HML, least_num=45,T_days=66,
                                   doc_name='factor_T66'):

        # check the path exist or not
        if not os.path.exists('./factor_Idiosyncratic_skewness/'):
            print('# factor_Idiosyncratic_skewness path not exist , creating...... ')
            os.makedirs('./factor_Idiosyncratic_skewness/')
        else:
            print('factor_Idiosyncratic_skewness has already existed ')

        df_IS = pd.DataFrame(index=excess_return_of_stk.index, columns=excess_return_of_stk.columns)
        fama_df = pd.DataFrame(columns=['EXC_R', 'ALPHA', 'MKT', 'SMB', 'HML'], index=list(range(T_days)))

        for i in tqdm(range(len(df_IS.index) - T_days)):
            # print(i)

            for j in range(len(df_IS.columns)):
                # print(df_IS.columns[j],j)
                # fama_df=fama_df1.copy()
                fama_df.loc[:, 'EXC_R'] = excess_return_of_stk.iloc[i:i + T_days, j].values
                fama_df.loc[:, 'ALPHA'] = 1
                fama_df.loc[:, 'MKT'] = market_risk[i:i + T_days].values
                fama_df.loc[:, 'SMB'] = SMB.iloc[i:i + T_days].values
                fama_df.loc[:, 'HML'] = HML.iloc[i:i + T_days].values


                # fama_df=fama_df.dropna()
                if len(fama_df.dropna()) < least_num:
                    residual = np.nan
                    df_IS.iloc[i + T_days, j] = residual
                    # print(0)
                # @nb.jit()
                else:

                    tmp_fama_df = fama_df.dropna().copy()
                    X = tmp_fama_df[['ALPHA', 'MKT', 'SMB', 'HML']].values
                    y = tmp_fama_df[['EXC_R']].values
                    theta, resid = close_form_OLS(X, y)
                    residual = pd.DataFrame(resid).skew()[0]
                    df_IS.iloc[i + T_days, j] = residual

                    # model = sm.ols(formula='EXC_R~MKT+SMB+HML+RMW+CMA', data=fama_df).fit()
                    # residual = model.resid.skew()
                    # df_IS.iloc[i + 60, j] = residual

        df_IS = df_IS.iloc[T_days:, :]

        fileHandle = open('./factor_Idiosyncratic_skewness/Factor_fama3_IS_T{}_{}.pkl'.format(T_days,doc_name), 'wb')
        pickle.dump(df_IS, fileHandle)
        fileHandle.close()

        return df_IS

    def similarnone_dataframe(self, exampledata, data_to_change):

        for i in tqdm(range(len(exampledata.index))):
            # nan_stk_series=exampledata.columns[exampledata.iloc[i].isnull()]
            data_to_change.loc[exampledata.index[i], exampledata.columns[exampledata.iloc[i].isnull()]]=np.nan
        return data_to_change


    #写一个能自动对其的函数
    #def Samilize_form(*data)





    # @nb.jit()
    def FAMA5_factor_form(self,Mkt_pct,Bond_pct,stk_pct_data,size_data,BM_data,profit_data,investment_data):
        print('请务必把数据对齐')
        # 对齐
        stk_pct_data_columns_list=stk_pct_data.columns.tolist()
        size_data_columns_list = size_data.columns.tolist()
        BM_data_data_columns_list = BM_data.columns.tolist()
        profit_data_data_columns_list = profit_data.columns.tolist()
        investment_data_data_columns_list = investment_data.columns.tolist()

        stk_pct_data_index_list = stk_pct_data.index.tolist()
        size_data_index_list = size_data.index.tolist()
        BM_data_data_index_list = BM_data.index.tolist()
        profit_data_data_index_list = profit_data.index.tolist()
        investment_data_data_index_list = investment_data.index.tolist()

        Common_columns_list=list(set(stk_pct_data_columns_list)&set(size_data_columns_list)&set(BM_data_data_columns_list)
                                 &set(profit_data_data_columns_list)&set(investment_data_data_columns_list))

        Common_index_list = list(
            set(stk_pct_data_index_list) & set(size_data_index_list) & set(BM_data_data_index_list)
            & set(profit_data_data_index_list) & set(investment_data_data_index_list))

        BM_data =BM_data.loc[:, BM_data.columns.isin(Common_columns_list)]
        size_data = size_data.loc[:, size_data.columns.isin(Common_columns_list)]
        stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(Common_columns_list)]
        profit_data = profit_data.loc[:, profit_data.columns.isin(Common_columns_list)]
        investment_data = investment_data.loc[:, investment_data.columns.isin(Common_columns_list)]

        stk_pct_data=stk_pct_data[stk_pct_data.index.isin(Common_index_list)]
        size_data = size_data[size_data.index.isin(Common_index_list)]
        BM_data = BM_data[BM_data.index.isin(Common_index_list)]
        profit_data = profit_data[profit_data.index.isin(Common_index_list)]
        investment_data = investment_data[investment_data.index.isin(Common_index_list)]


        # #初步对齐
        # size_data = size_data.loc[:, size_data.columns.isin(BM_data.columns)]
        # stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(BM_data.columns)]
        # profit_data = profit_data.loc[:, profit_data.columns.isin(BM_data.columns)]
        # investment_data = investment_data.loc[:, investment_data.columns.isin(BM_data.columns)]
        #
        # size_data=size_data[size_data.index.isin(stk_pct_data.index)]
        # BM_data = BM_data[BM_data.index.isin(stk_pct_data.index)]
        # profit_data = profit_data[profit_data.index.isin(stk_pct_data.index)]
        # investment_data = investment_data[investment_data.index.isin(stk_pct_data.index)]
        #
        # #第二次对齐
        # size_data = size_data.loc[:, size_data.columns.isin(investment_data.columns)]
        # stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(investment_data.columns)]
        # investment_data = investment_data.loc[:, investment_data.columns.isin(investment_data.columns)]
        # BM_data = BM_data.loc[:, BM_data.columns.isin(investment_data.columns)]
        #
        # #再对齐
        # size_data = size_data.loc[:, size_data.columns.isin(profit_data.columns)]
        # stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(profit_data.columns)]
        # investment_data = investment_data.loc[:, investment_data.columns.isin(profit_data.columns)]
        # BM_data = BM_data.loc[:, BM_data.columns.isin(profit_data.columns)]

        SH_series = pd.Series(index=size_data.index[1:])
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                                                                         i].quantile(
                                                                         [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][BM_data.iloc[i] > BM_data.iloc[i].quantile(
                                                                               [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SH_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        S_N_BM_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][(BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.7]).values[0])&(BM_data.iloc[i] > BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            S_N_BM_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        SL_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][ (BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SL_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()


        BH_series = pd.Series(index=size_data.index[1:])
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][BM_data.iloc[i] >= BM_data.iloc[i].quantile(
                [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BH_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        B_N_BM_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][(BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.7]).values[0]) & (BM_data.iloc[i] > BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            B_N_BM_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        BL_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][(BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BL_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        SR_series = pd.Series(index=size_data.index[1:])
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][profit_data.iloc[i] >= profit_data.iloc[i].quantile(
                [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SR_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        S_N_ROE_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.7]).values[0]) & (profit_data.iloc[i] > profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            S_N_ROE_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:,
                                0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                     :,
                                     1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                 :, 1].sum()

        SW_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SW_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        BR_series = pd.Series(index=size_data.index[1:])
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][profit_data.iloc[i] >= profit_data.iloc[i].quantile(
                [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BR_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        B_N_ROE_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.7]).values[0]) & (profit_data.iloc[i] > profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            B_N_ROE_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:,
                                0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                     :,
                                     1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                 :, 1].sum()

        BW_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BW_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        SC_series = pd.Series(index=size_data.index[1:])
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.3]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SC_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        S_N_INV_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.7]).values[0]) & (investment_data.iloc[i] > investment_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            S_N_INV_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:,
                                 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                      :,
                                      1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                  :, 1].sum()

        SA_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] >= investment_data.iloc[i].quantile(
                [0.7]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SA_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        BC_series = pd.Series(index=size_data.index[1:])
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.3]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BC_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        B_N_INV_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.7]).values[0]) & (investment_data.iloc[i] > investment_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            B_N_INV_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:,
                                 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                      :,
                                      1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                  :, 1].sum()

        BA_series = pd.Series(index=size_data.index[1:])
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] >= investment_data.iloc[i].quantile(
                [0.7]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i+1][stk_pct_data.iloc[i+1].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BA_series[i] = (stk_S_H_pct_data_indicator_high_list.iloc[:, 0] * stk_S_H_pct_data_indicator_high_list.iloc[
                                                                              :,
                                                                              1]).sum() / stk_S_H_pct_data_indicator_high_list.iloc[
                                                                                          :, 1].sum()

        # FAMA5_factor = pd.DataFrame(columns=['SMB', 'HML', 'RMW', 'CMA', 'SH', 'SN_BM', 'SL', 'BH', 'BN_BM', 'BL',
        #                                      'SR', 'SN_ROE', 'SW', 'BR', 'BN_ROE', 'BW',
        #                                      'SC', 'SN_INV', 'SA', 'BC', 'BN_INV', 'BA'
        #                                      ])
        # Mkt_pct, Bond_pct
        FAMA5_factor = pd.DataFrame(columns=['Rmt','Rft','SMB', 'HML', 'RMW', 'CMA' ])

        FAMA5_factor['Rmt'] = Mkt_pct.iloc[:]
        FAMA5_factor['Rft'] = (((Bond_pct.iloc[:]*0.01+1)**(1/250)-1)*100)

        FAMA5_factor['SMB'] = (((SH_series + S_N_BM_series + SL_series) / 3 - (
                    BH_series + B_N_BM_series + BL_series) / 3) +
                               ((SR_series + S_N_ROE_series + SW_series) / 3 - (
                                           BR_series + B_N_ROE_series + BW_series) / 3) +
                               ((SC_series + S_N_INV_series + SA_series) / 3 - (
                                           BC_series + B_N_INV_series + BA_series) / 3)) / 3

        FAMA5_factor['HML'] = (SH_series + BH_series) / 2 - (SL_series + BL_series) / 2
        FAMA5_factor['RMW'] = (SR_series + BR_series) / 2 - (SW_series + BW_series) / 2
        FAMA5_factor['CMA'] = (SC_series + BC_series) / 2 - (SA_series + BA_series) / 2

        # FAMA5_factor['SH'] = SH_series
        # FAMA5_factor['SN_BM'] = S_N_BM_series
        # FAMA5_factor['SL'] = SL_series
        # FAMA5_factor['BH'] = BH_series
        # FAMA5_factor['BN_BM'] = B_N_BM_series
        # FAMA5_factor['BL'] = BL_series
        # FAMA5_factor['SR'] = SR_series
        # FAMA5_factor['SN_ROE'] = S_N_ROE_series
        # FAMA5_factor['SW'] = SW_series
        # FAMA5_factor['BR'] = BR_series
        # FAMA5_factor['BN_ROE'] = B_N_ROE_series
        # FAMA5_factor['BW'] = BW_series
        # FAMA5_factor['SC'] = S_N_INV_series
        # FAMA5_factor['SN_INV'] = SH_series
        # FAMA5_factor['SA'] = SA_series
        # FAMA5_factor['BC'] = BC_series
        # FAMA5_factor['BN_INV'] = B_N_INV_series
        # FAMA5_factor['BA'] = BA_series

        # check the path exist or not
        if not os.path.exists('./FAMA_factor_data/'):
            print('# FAMA_factor_data path not exist , creating...... ')
            os.makedirs('./FAMA_factor_data/')
        else:
            print('FAMA_factor_data has already existed ')

        # if not os.path.exists('./FAMA_factor_data/FAMA_factor_size_weighted_data.pkl'):
        #     start_date = int('20020101')
        #     original_stk_ROE_data = pd.DataFrame()
        # else:
        #     original_stk_ROE_data = pd.read_pickle('./FAMA_factor_data/FAMA_factor_size_weighted_data.pkl')

        fileHandle = open('./FAMA_factor_data/FAMA_factor_size_weighted_data.pkl', 'wb')
        pickle.dump(FAMA5_factor, fileHandle)
        fileHandle.close()




        return FAMA5_factor




# @nb.jit()
    def FAMA5_factor_form_equdiveide(self,Mkt_pct,Bond_pct,stk_pct_data,size_data,BM_data,profit_data,investment_data):
        # 对齐
        stk_pct_data_columns_list = stk_pct_data.columns.tolist()
        size_data_columns_list = size_data.columns.tolist()
        BM_data_data_columns_list = BM_data.columns.tolist()
        profit_data_data_columns_list = profit_data.columns.tolist()
        investment_data_data_columns_list = investment_data.columns.tolist()

        stk_pct_data_index_list = stk_pct_data.index.tolist()
        size_data_index_list = size_data.index.tolist()
        BM_data_data_index_list = BM_data.index.tolist()
        profit_data_data_index_list = profit_data.index.tolist()
        investment_data_data_index_list = investment_data.index.tolist()

        Common_columns_list = list(
            set(stk_pct_data_columns_list) & set(size_data_columns_list) & set(BM_data_data_columns_list)
            & set(profit_data_data_columns_list) & set(investment_data_data_columns_list))

        Common_index_list = list(
            set(stk_pct_data_index_list) & set(size_data_index_list) & set(BM_data_data_index_list)
            & set(profit_data_data_index_list) & set(investment_data_data_index_list))

        BM_data = BM_data.loc[:, BM_data.columns.isin(Common_columns_list)]
        size_data = size_data.loc[:, size_data.columns.isin(Common_columns_list)]
        stk_pct_data = stk_pct_data.loc[:, stk_pct_data.columns.isin(Common_columns_list)]
        profit_data = profit_data.loc[:, profit_data.columns.isin(Common_columns_list)]
        investment_data = investment_data.loc[:, investment_data.columns.isin(Common_columns_list)]

        stk_pct_data = stk_pct_data[stk_pct_data.index.isin(Common_index_list)]
        size_data = size_data[size_data.index.isin(Common_index_list)]
        BM_data = BM_data[BM_data.index.isin(Common_index_list)]
        profit_data = profit_data[profit_data.index.isin(Common_index_list)]
        investment_data = investment_data[investment_data.index.isin(Common_index_list)]

        SH_series = pd.Series(index=size_data.index)
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                                                                         i].quantile(
                                                                         [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][BM_data.iloc[i] > BM_data.iloc[i].quantile(
                                                                               [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SH_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        S_N_BM_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][(BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.7]).values[0])&(BM_data.iloc[i] > BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            S_N_BM_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        SL_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][ (BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SL_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()


        BH_series = pd.Series(index=size_data.index)
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][BM_data.iloc[i] >= BM_data.iloc[i].quantile(
                [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BH_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        B_N_BM_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][(BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.7]).values[0]) & (BM_data.iloc[i] > BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            B_N_BM_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        BL_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = BM_data.iloc[i][(BM_data.iloc[i] < BM_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BL_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        SR_series = pd.Series(index=size_data.index)
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][profit_data.iloc[i] >= profit_data.iloc[i].quantile(
                [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SR_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        S_N_ROE_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.7]).values[0]) & (profit_data.iloc[i] > profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            S_N_ROE_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        SW_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SW_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        BR_series = pd.Series(index=size_data.index)
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][profit_data.iloc[i] >= profit_data.iloc[i].quantile(
                [0.7]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BR_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        B_N_ROE_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.7]).values[0]) & (profit_data.iloc[i] > profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            B_N_ROE_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        BW_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = profit_data.iloc[i][(profit_data.iloc[i] < profit_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BW_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        SC_series = pd.Series(index=size_data.index)
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.3]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SC_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        S_N_INV_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.7]).values[0]) & (investment_data.iloc[i] > investment_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            S_N_INV_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        SA_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] < size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] >= investment_data.iloc[i].quantile(
                [0.7]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            SA_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        BC_series = pd.Series(index=size_data.index)
        for i in tqdm(range(len(SH_series))):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.3]).values[0]]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BC_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        B_N_INV_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] < investment_data.iloc[i].quantile(
                [0.7]).values[0]) & (investment_data.iloc[i] > investment_data.iloc[i].quantile(
                [0.3]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            B_N_INV_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()

        BA_series = pd.Series(index=size_data.index)
        for i in range(len(SH_series)):
            stk_s_list = size_data.iloc[i][size_data.iloc[i] > size_data.iloc[
                i].quantile(
                [0.5]).values[0]]
            stk_indicator_high_list = investment_data.iloc[i][(investment_data.iloc[i] > investment_data.iloc[i].quantile(
                [0.7]).values[0])]
            stk_S_H_name_list = list(set(stk_s_list.index) & set(stk_indicator_high_list.index))
            stk_S_H_pct_data_list = stk_pct_data.iloc[i][stk_pct_data.iloc[i].index.isin(stk_S_H_name_list)]
            stk_S_H_pct_data_indicator_high_list = pd.merge(left=stk_S_H_pct_data_list,
                                                            right=size_data.iloc[i],
                                                            left_index=True,
                                                            right_index=True, how='left').dropna()
            BA_series[i] = stk_S_H_pct_data_indicator_high_list.iloc[:, 0].mean()



        FAMA5_factor = pd.DataFrame(columns=['Rmt', 'Rft', 'SMB', 'HML', 'RMW', 'CMA'])

        FAMA5_factor['Rmt'] = Mkt_pct.iloc[:]
        FAMA5_factor['Rft'] = (((Bond_pct.iloc[:] * 0.01 + 1) ** (1 / 250) - 1) * 100)

        FAMA5_factor['SMB'] = (((SH_series + S_N_BM_series + SL_series) / 3 - (
                    BH_series + B_N_BM_series + BL_series) / 3) +
                               ((SR_series + S_N_ROE_series + SW_series) / 3 - (
                                       BR_series + B_N_ROE_series + BW_series) / 3) +
                               ((SC_series + S_N_INV_series + SA_series) / 3 - (
                                       BC_series + B_N_INV_series + BA_series) / 3)) / 3

        FAMA5_factor['HML'] = (SH_series + BH_series) / 2 - (SL_series + BL_series) / 2
        FAMA5_factor['RMW'] = (SR_series + BR_series) / 2 - (SW_series + BW_series) / 2
        FAMA5_factor['CMA'] = (SC_series + BC_series) / 2 - (SA_series + BA_series) / 2
        # check the path exist or not
        if not os.path.exists('./FAMA_factor_data/'):
            print('# FAMA_factor_data path not exist , creating...... ')
            os.makedirs('./FAMA_factor_data/')
        else:
            print('FAMA_factor_data has already existed ')

        # if not os.path.exists('./FAMA_factor_data/FAMA_factor_size_weighted_data.pkl'):
        #     start_date = int('20020101')
        #     original_stk_ROE_data = pd.DataFrame()
        # else:
        #     original_stk_ROE_data = pd.read_pickle('./FAMA_factor_data/FAMA_factor_size_weighted_data.pkl')

        fileHandle = open('./FAMA_factor_data/FAMA_factor_equally_weighted_data.pkl', 'wb')
        pickle.dump(FAMA5_factor, fileHandle)
        fileHandle.close()

        return FAMA5_factor











