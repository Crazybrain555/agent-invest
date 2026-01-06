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

from Specific_signal_generator import Get_specific_signals
from Debts_related_signals_generator import Get_debts_related_signals_generator




#实际要用
Get_specific_signals=Get_specific_signals()
Get_debts_related_signals_generator=Get_debts_related_signals_generator()

# test = Get_debts_related_signals_generator.Capital_structure_and_solvency_signals_creator()
stk_list = Get_specific_signals.get_signal_dcf2ev()
gpmmrose = Get_specific_signals.get_signal_gpmmrose()
Get_specific_signals.get_signal_contrli()
# test = Get_debts_related_signals_generator.Capital_structure_and_solvency_signals_creator()


print('data is setted')