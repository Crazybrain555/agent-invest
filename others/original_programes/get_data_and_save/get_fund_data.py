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







    def Get_fund_data(self,start_date,end_date):
        '''
        这个函数是存放日期、股票、中信行业指数的三围数据的dataframe（主要是方便通过股票找到对应行业）
        在存放一个基于词的字典：字典第一层：日期 第二层：中心行业指数 第三层 ：股票池、对应每天的收益率
        :return:
        '''
        sql1 = sql1 = (
    "SELECT "
    "F_INFO_WINDCODE AS '基金主代码', "
    "F_INFO_WINDCODE AS '基金代码', "  # 注意：您没有提供一个单独的“基金代码”，所以这里使用了Wind代码作为代替。
    "F_INFO_FULLNAME AS '基金名称', "
    "F_INFO_NAME AS '基金简称', "
    "F_INFO_CORP_FUNDMANAGEMENTCOMP AS '管理人', "
    "F_INFO_CUSTODIANBANK AS '托管人', "
    "F_INFO_FIRSTINVESTTYPE AS '基金投资类型', "
    "F_INFO_SETUPDATE AS '成立日期', "
    "F_INFO_MATURITYDATE AS '到期日期', "
    "F_ISSUE_TOTALUNIT AS '发行份额', "
    "F_INFO_MANAGEMENTFEERATIO AS '管理费', "
    "F_INFO_CUSTODIANFEERATIO AS '托管费' "
    "FROM "
    "wind_quant.dbo.ChinaMutualFundDescription;"
)


        ChinaMutualFundDescription = pd.read_sql(sql1, self.con_wind_db)

        print(1)

    def ChinaMutualFundPchRedm(self,start_date,end_date):
        '''
        这个函数是存放日期、股票、中信行业指数的三围数据的dataframe（主要是方便通过股票找到对应行业）
        在存放一个基于词的字典：字典第一层：日期 第二层：中心行业指数 第三层 ：股票池、对应每天的收益率
        :return:
        '''
        sql1 = (
                "SELECT "
                "S_INFO_WINDCODE AS '基金代码', "
                "TRADE_DT AS '公告日期', "
                "F_UNIT_RPENDDATE AS '截止日期', "
            
        "F_UNIT_STARTSHARES AS '报告期期初基金总份额', "
        "F_UNIT_PURCHASE AS '报告期基金总申购份额', "
        "F_UNIT_REDEMPTION AS '报告期基金总赎回份额', "
        "F_UNIT_ENDSHARES AS '报告期期末基金总份额' "
        "FROM "
        "wind_quant.dbo.ChinaMutualFundPchRedm "
        "where  TRADE_DT <={} and  TRADE_DT >={}").format(end_date,start_date)


        ChinaMutualFundPchRedm = pd.read_sql(sql1, self.con_wind_db)

        print(1)

    def CMFHolderStructure(self, start_date, end_date):
        '''
        这个函数是存放日期、股票、中信行业指数的三围数据的dataframe（主要是方便通过股票找到对应行业）
        在存放一个基于词的字典：字典第一层：日期 第二层：中心行业指数 第三层 ：股票池、对应每天的收益率
        :return:
        '''
        sql_query = (
            "SELECT "
            "S_INFO_WINDCODE AS '基金代码', "  # 基金代码
            "SEC_ID AS '证券ID', "  # 证券ID
            "ANN_DT AS '公告日期', "  # 公告日期
            "END_DT AS '截止日期', "  # 截止日期
            "HOLDER_INSTITUTION_HOLDING AS '机构投资者持有的基金份额', "  # 机构投资者持有的基金份额
            "HOLDER_INSTITUTION_HOLDINGPCT AS '机构投资者持有的基金份额占总份额比例', "  # 机构投资者持有的基金份额占总份额比例
            "HOLDER_PERSONAL_HOLDING AS '个人投资者持有的基金份额', "  # 个人投资者持有的基金份额
            "HOLDER_PERSONAL_HOLDINGPCT AS '个人投资者持有的基金份额占比' "
            "FROM "
            "wind_quant.dbo.CMFHolderStructure "  # 请替换为您的实际数据库架构名
            "WHERE "
            "ANN_DT <= {} AND ANN_DT >= {}"
        ).format(end_date,start_date)

        # 使用sqlalchemy执行查询，并返回一个dataframe
        CMFHolderStructure = pd.read_sql(
            sql_query,
            self.con_wind_db
        )

    print(1)

    def ChinaMutualFundStockPortfolio(self, start_date, end_date):
        '''
        这个函数查询指定日期范围内的中国共同基金的持股明细。
        它返回一个包含日期、基金代码、持股信息等的dataframe。
        :param start_date: 查询开始日期
        :param end_date: 查询结束日期
        :return: 包含持股明细的dataframe
        '''
        sql_query = (
            "SELECT "
            "S_INFO_WINDCODE AS '基金Wind代码', "  # 基金Wind代码
            "F_PRT_ENDDATE AS '截止日期', "  # 截止日期
            "CRNCY_CODE AS '货币代码', "  # 货币代码
            "S_INFO_STOCKWINDCODE AS '持有股票Wind代码', "  # 持有股票Wind代码
            "F_PRT_STKVALUE AS '持有股票市值(元)', "  # 持有股票市值
            "F_PRT_STKQUANTITY AS '持有股票数量（股）', "  # 持有股票数量
            "F_PRT_STKVALUETONAV AS '持有股票市值占基金净值比例(%)', "  # 持有股票市值占基金净值比例
            "F_PRT_POSSTKVALUE AS '积极投资持有股票市值(元)', "  # 积极投资持有股票市值
            "F_PRT_POSSTKQUANTITY AS '积极投资持有股数（股）', "  # 积极投资持有股数
            "F_PRT_POSSTKTONAV AS '积极投资持有股票市值占净资产比例(%)', "  # 积极投资持有股票市值占净资产比例
            "F_PRT_PASSTKEVALUE AS '指数投资持有股票市值(元)', "  # 指数投资持有股票市值
            "F_PRT_PASSTKQUANTITY AS '指数投资持有股数（股）', "  # 指数投资持有股数
            "F_PRT_PASSTKTONAV AS '指数投资持有股票市值占净资产比例(%)', "  # 指数投资持有股票市值占净资产比例
            "ANN_DATE AS '公告日期', "  # 公告日期
            "STOCK_PER AS '占股票市值比', "  # 占股票市值比
            "FLOAT_SHR_PER AS '占流通股本比例(%)' "  # 占流通股本比例
            "FROM "
            "wind_quant.dbo.ChinaMutualFundStockPortfolio " 
            "WHERE "
            "F_PRT_ENDDATE <= {} AND F_PRT_ENDDATE >= {}"
        ).format(end_date, start_date)

        # 使用sqlalchemy执行查询，并返回一个dataframe
        df_ChinaMutualFundStockPortfolio = pd.read_sql(
            sql_query,
            self.con_wind_db  # 确保这是您的数据库连接
        )
        print(1)
        return df_ChinaMutualFundStockPortfolio



    def ChinaMutualFundBondPortfolio(self, start_date, end_date):
        # 你提供的字段映射到正确的字段名称
        sql_query = (
            "SELECT "
            "S_INFO_WINDCODE AS '基金Wind代码', "  # 基金Wind代码
            "F_PRT_ENDDATE AS '截止日期', "  # 截止日期
            "CRNCY_CODE AS '货币代码', "  # 货币代码
            "S_INFO_BONDWINDCODE AS '持有债券Wind代码', "  # 持有债券Wind代码
            "F_PRT_BDVALUE AS '持有债券市值(元)', "  # 持有债券市值(元)
            "F_PRT_BDQUANTITY AS '持有债券数量（张）', "  # 持有债券数量（张）
            "F_PRT_BDVALUETONAV AS '持有债券市值占基金净值比例(%)', "  # 持有债券市值占基金净值比例(%)
            "F_ANN_DATE AS '公告日期', "  # 公告日期
            "NUMB_NP_OS AS '非公开发行股数', "  # 非公开发行股数
            "AVRG_CLSPRICE_NPOS AS '非公开发行股期末均价' "  # 非公开发行股期末均价
            "FROM "
            "wind_quant.dbo.ChinaMutualFundBondPortfolio "  # 数据库和表的名字
            "WHERE "
            "F_PRT_ENDDATE <= '{}' AND F_PRT_ENDDATE >= '{}'"
        ).format(end_date, start_date)

        # 使用sqlalchemy执行查询，并返回一个dataframe
        df = pd.read_sql(
            sql_query,
            self.con_wind_db
        )
        print(1)
        return df


        print(1)


if __name__ == '__main__':
    getting=Get_data_from_winddatabase()
    getting.ChinaMutualFundBondPortfolio(20180101,20220101)


    print('good')
