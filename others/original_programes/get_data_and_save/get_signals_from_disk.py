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




#实际要用
test_df1=Get_data_from_sshared_disk()



# test_df1.get_all_theme_data(
# )

test_df1.get_all_signal_data(range=20

)
print('Get_data_from_sshared_disk ')
test_df1.get_tot_forbid_pivot_data()


print('data is setted')