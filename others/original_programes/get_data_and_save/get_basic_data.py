#!/usr/bin/env python
#-*- utf-8 -*-

'''
Created on Jan 14 2022
@author: yuye zhang
@email: zhangyuye@bosera.com
'''

import h5py
#coding=utf-8
import sys,os,datetime
import numpy as np
import pandas as pd

from factor_creator import Get_data_fromdatabase
from get_data_from_shared_disk import Get_data_from_sshared_disk
from Get_data_from_winddatabase import Get_data_from_winddatabase




#实际要用
test_df1=Get_data_fromdatabase()
# test_df1.basic_financail_data_creator()
test_df1.get_valuation_data_creator()

test_df1.get_IndexMembers()

Get_data_from_sshared_disk=Get_data_from_sshared_disk()
Get_data_from_winddatabase=Get_data_from_winddatabase()

# test_df1.get_HK_stk_basic(reload_tradedays=300)
test_df1.get_HK_stk_basic()
test_df1.get_stk_adj_price()
# test_df1.basic_financail_data_creator(reload_tradedays=300)
test_df1.basic_financail_data_creator()

test_df1.get_wind_index_885000_WI()
test_df1.get_IndexMembers()
# test_df1.get_Ashare_daliy_derivative_financial_indicators(reload_tradedays=300)
test_df1.get_Ashare_daliy_derivative_financial_indicators()
test_df1.get_wind_CBondCurveCNBD()
test_df1.get_wind_EV_DATA()
test_df1.get_wind_FCF_DATA()

test_df1.con_forecast_roll_stk()
test_df1.get_AShareBalanceSheet()

Get_data_from_winddatabase.Get_MembersCITICS()

print('data is setted')