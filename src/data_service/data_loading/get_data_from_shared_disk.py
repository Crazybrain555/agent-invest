#!/usr/bin/env python
#-*- utf-8 -*-

'''
Created on Feb 24 2022
@author: yuye zhang
@email: zhangyuye@bosera.com
'''
# import win32wnet
import h5py
#coding=utf-8
import sys,os,datetime
import numpy as np
import pandas as pd
from matplotlib import ticker
from numba import jit
import warnings

import sys
import pymssql
import datetime

import time
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import json

from pandas.tseries.offsets import BDay
import csv
import pickle
from tqdm import tqdm
import warnings
import re

# Python 通过all()判断列表(list)中所有元素是否都包含某个字符串(string)--from baidu
def list_str_filter(*strings,list_sample):
    list_string = list(string for string in strings )
    list_text = list_sample
    all_words = list(filter(lambda text: all([word in text for word in list_string]), list_text))
    return  all_words

def windcode_to_id(windcode):
    return int(windcode.split('.')[0])


def setting_startdate_and_saving_path_dataframe(dir_path, doc_path, reload_tradedays):
    if reload_tradedays == None:
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


def merge_dict(dir1,dir2):
    dir2.update(dir1)
    return dir2

def save_doc_pickle(dataframe,dir_path,doc_path):
    fileHandle = open('./{}/{}'.format(dir_path, doc_path), 'wb')
    pickle.dump(dataframe, fileHandle)
    fileHandle.close()




class Get_data_from_sshared_disk():
    def __init__(self,
                 signal_local_path=None,
                 IPOBDates=122
                 ):
        #如果有写signal path 就建立一个文件放在signal path 里
        if not os.path.exists('./signal_data/'):
            print('# signal_data path not exist , creating...... ')
            os.makedirs('./signal_data/')
        else:
            print('file signal_data has already existed ')


        if not os.path.exists('./forbid_data/'):
            print('# forbid_data path not exist , creating...... ')
            os.makedirs('./forbid_data/')
        else:
            print('file forbid_data has already existed ')


        if signal_local_path==None:
            pass
        else:
            os.makedirs('./signal_data/{}/'.format(signal_local_path))

        #wind和照样永续的连接关联字段
        self.IPOBDates=IPOBDates
        self.con_wind_db = pymssql.connect('v-wind', 'trade', 'trade', 'wind_quant',charset='cp936')
        self.con_gogoal_db = pymssql.connect('p-ma-mars', 'sig', 'sig', 'FundRiskControl2',charset='cp936')
        #类包括路径这个关键属性
        self.signal_local_path=signal_local_path

        self.end_date = int(datetime.date.today().strftime('%Y%m%d'))
        self.tradedays_start = 20070101
        sql = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' and TRADE_DAYS >={} " \
              "and TRADE_DAYS <={} order by TRADE_DAYS asc".format(self.tradedays_start, self.end_date)
        data = pd.read_sql(sql, self.con_wind_db)
        self.Tradedays_list = data['TRADE_DAYS'].tolist()

        tradedays_start=20050101
        tradedayssql = "select TRADE_DAYS from wind_quant.dbo.AShareCalendar where S_INFO_EXCHMARKET='SSE' and TRADE_DAYS >={} " \
              "and TRADE_DAYS <={} order by TRADE_DAYS asc".format(tradedays_start, self.end_date)
        tradedata= pd.read_sql(tradedayssql, self.con_wind_db)
        self.trade_date_data_array=tradedata['TRADE_DAYS'].tolist()


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



   #设一个函数，这个函数能输入因子名称，返回一个量化投资部门网盘里该因子打分的每日打分信息，格式是三列，分别是tdate，stk_code和因子名称
    def combine_data_from_disk(self, signal_name, range=None, start_date=None, end_date=None):
        #如果不存在这个选定的因子signal_name，warning
        if not os.path.exists('\\\\space\\signal\\{}'.format(signal_name)):
            warnings.warn('signal does not exist, change signal')
        else:
            #查找对应文件夹的所有csv文件
            files= os.listdir('\\\\space\\signal\\{}'.format(signal_name))
            # 通过 函数list_str_filter，过滤出只包含raw这个字段的文件 因为有detail和raw的重复的，格式也不一样
            files = list_str_filter('raw', list_sample=files) if len(list_str_filter('raw', list_sample=files))!=0 else files
            #如果files是['prepare'] 则files为None
            files = None if len(files) == 1 and files[0] == 'prepare' else files
            if files is not None:
                # 对list排序,获取最近range项的数据累加
                files.sort(reverse=True)
                
                # 如果提供了start_date和end_date，按日期过滤文件
                if start_date is not None or end_date is not None:
                    filtered_files = []
                    for file in files:
                        try:
                            # 从文件名中提取日期 (格式: filename.YYYYMMDD.csv)
                            file_date_str = file.split('.')[1]
                            file_date = int(file_date_str)
                            
                            # 应用日期过滤
                            if start_date is not None and file_date < start_date:
                                continue
                            if end_date is not None and file_date > end_date:
                                continue
                            
                            filtered_files.append(file)
                        except (IndexError, ValueError):
                            # 如果文件名格式不正确，跳过该文件
                            continue
                    
                    files = filtered_files
                elif range is not None:
                    # 保持原有的range逻辑作为后备
                    files = files[:range]

            # 建立一个空dataframe 列明分别是tdate，stk_code和因子名称
            siganl_data = pd.DataFrame(columns=['tdate', 'stk_code', signal_name])
            file_list = []
            def change_data_formation(file):
                file_data = pd.read_csv('\\\\space\\signal\\{}\\{}'.format(signal_name, file),header=None)
                file_data.insert(0, 'tdate', file.split('.')[1])
                file_data.columns = ['tdate', 'stk_code', signal_name]
                file_data=file_data.dropna()
                #防止出现因为游T这个str搞得整个数据格式从int变成str 导致格式不一致的问题
                if file_data['stk_code'].dtypes == 'int64' or file_data['stk_code'].dtypes == 'int32' or \
                        file_data['stk_code'].dtypes == 'int' or file_data['stk_code'].dtypes == 'float':
                    file_data['stk_code'] = file_data['stk_code'].astype('int')
                else :                                                                     #file_data['stk_code'].dtypes=='O'
                    # print('{} type is str or O'.format(signal_name))
                    for index in file_data.index:
                        try :
                            file_data.loc[index, 'stk_code'] = int(file_data.loc[index, 'stk_code'])
                        except :
                            pass
                        else:
                            file_data.loc[index, 'stk_code'] = int(file_data.loc[index, 'stk_code'])

                # if file_data['stk_code'].dtypes == 'float':
                #     file_data['stk_code'] = file_data['stk_code'].astype('int')
                # elif file_data['stk_code'].dtypes!='int64':
                #     # 去掉关键字符
                #     file_data['stk_code'] = file_data['stk_code'].apply(lambda x: re.sub('A|T|B|I|J', '', x))
                #     file_data['stk_code'] = file_data['stk_code'].astype('int')
                # else:
                #     pass
                # #调试成功就隐藏这个代码
                # if file_data['stk_code'].dtypes != 'int64' and file_data['stk_code'].dtypes != 'int32' and \
                #         file_data['stk_code'].dtypes != 'int':
                #     print('{} of {} columns are not int64 int32 '.format(file,signal_name))
                # else:
                #     pass

                return(file_data)

            if files is not None:
                siganl_data = pd.concat(list(change_data_formation(file) for file in files))
                siganl_data.sort_values(by=['tdate', 'stk_code'], axis=0, ascending=True, inplace=True)
                siganl_data.drop_duplicates(subset=['tdate', 'stk_code'], keep='last', inplace=True)
                siganl_data.reset_index(drop=True, inplace=True)
            else:
                siganl_data = pd.DataFrame()

            return siganl_data

    #设计一个函数，这个函数只能建立路径保存在signal_data的文件夹中
    def save_date_from_disk(self,signal_name,range=None):

        signal_data_path = './signal_data/{}/{}.pkl'.format(signal_name, signal_name)

        if not os.path.exists('./signal_data/{}/'.format(signal_name)):
            print('# signal_data file not exist , creating...... ')
            os.makedirs('./signal_data/{}/'.format(signal_name))

        if not os.path.exists('./signal_data/{}/{}.pkl'.format(signal_name,signal_name)):
            # 调用combine_data_from_disk
            signal_data = Get_data_from_sshared_disk.combine_data_from_disk(self, signal_name).sort_values(by=['tdate','stk_code'])

            if signal_data.empty:
                print('{} is empty'.format(signal_name))
            else:
                # save signal_data 保存为pickle文件
                fileHandle = open('./signal_data/{}/{}.pkl'.format(signal_name, signal_name), 'wb')
                pickle.dump(signal_data, fileHandle)
                fileHandle.close()

                signal_data = pd.read_pickle('./signal_data/{}/{}.pkl'.format(signal_name, signal_name)) if \
                    len(pd.read_pickle('./signal_data/{}/{}.pkl'.format(signal_name, signal_name))) != 0 else \
                    Get_data_from_sshared_disk.combine_data_from_disk(self, signal_name).sort_values(by=['tdate', 'stk_code'])
                signal_data.sort_values('tdate', inplace=True)
                # 删除非int格式 通过查非字符型
                signal_data = signal_data[signal_data['stk_code'].apply(lambda x: type(x) == np.int64 or type(x) == np.int32 or type(x) == int)]

                signal_data = pd.pivot_table(signal_data, index=['tdate'], columns=['stk_code'],
                                             values=[signal_name])

                signal_data.columns = signal_data.columns.get_level_values(1)
                # save signal_data 保存为pickle文件
                fileHandle = open('./signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name), 'wb')
                pickle.dump(signal_data, fileHandle)
                fileHandle.close()
        else:
            try:
                original_signal_data = pd.read_pickle(signal_data_path)
                # 检查文件是否为空
                if original_signal_data.empty:
                    print(f"File {signal_data_path} is empty. Re-generating file.")
                    original_signal_data = pd.DataFrame()  # 如果文件为空，初始化为空数据框
            except (EOFError, ValueError) as e:
                print(f"Error reading file {signal_data_path}: {e}. Re-generating file.")
                original_signal_data = pd.DataFrame()  # 如果读取失败，则初始化为空数据框
            # 调用combine_data_from_disk
            signal_data = Get_data_from_sshared_disk.combine_data_from_disk(self, signal_name, range=range)
            if signal_data.empty:
                print('{} is empty'.format(signal_name))
            else:
                final_signal_data = original_signal_data._append(signal_data)
                # 规范格式
                final_signal_data.sort_values(by=['tdate', 'stk_code'], axis=0, ascending=True, inplace=True)
                final_signal_data.drop_duplicates(subset=['tdate', 'stk_code'], keep='last', inplace=True)
                final_signal_data.reset_index(drop=True, inplace=True)
                final_signal_data = final_signal_data.sort_values(by=['tdate', 'stk_code'])
                # save signal_data 保存为pickle文件
                fileHandle = open('./signal_data/{}/{}.pkl'.format(signal_name, signal_name), 'wb')
                pickle.dump(final_signal_data, fileHandle)
                fileHandle.close()

            signal_data = pd.read_pickle('./signal_data/{}/{}.pkl'.format(signal_name, signal_name)) if \
                len(pd.read_pickle('./signal_data/{}/{}.pkl'.format(signal_name, signal_name))) != 0 else \
                Get_data_from_sshared_disk.combine_data_from_disk(self, signal_name).sort_values(by=['tdate', 'stk_code'])
            signal_data.sort_values('tdate', inplace=True)
            # 删除非int格式 通过查非字符型
            signal_data = signal_data[signal_data['stk_code'].apply(lambda x: type(x) == np.int64 or type(x) == np.int32 or type(x) == int)]

        signal_data = pd.pivot_table(signal_data, index=['tdate'], columns=['stk_code'],
                                                   values=[signal_name])

        signal_data.columns = signal_data.columns.get_level_values(1)
        # save signal_data 保存为pickle文件
        fileHandle = open('./signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name), 'wb')
        pickle.dump(signal_data, fileHandle)
        fileHandle.close()
        print('# signal_data {}'.format(len(signal_data)))


        # if not os.path.exists('./signal_data/{}/{}_pivot.pkl'.format(signal_name,signal_name)):
        #     # 调用combine_data_from_disk
        #     signal_data = Get_data_from_sshared_disk.combine_data_from_disk(self, signal_name).sort_values(by=['tdate','stk_code'])
        #     signal_data.sort_values('tdate', inplace=True)
        #     signal_data=signal_data[signal_data['stk_code'].apply(lambda x: type(x) == np.int64 or type(x) == np.int32 or type(x) == int)]
        #     signal_data = pd.pivot_table(signal_data, index=['tdate'], columns=['stk_code'],
        #                                        values=[signal_name])
        #
        #     signal_data.columns = signal_data.columns.get_level_values(1)
        #
        #
        #     # save signal_data 保存为pickle文件
        #     fileHandle = open('./signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name), 'wb')
        #     pickle.dump(signal_data, fileHandle)
        #     fileHandle.close()
        # else:
        #     try:
        #         original_signal_data = pd.read_pickle('./signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name))
        #     except:
        #         signal_data = Get_data_from_sshared_disk.combine_data_from_disk(self, signal_name).sort_values(
        #             by=['tdate', 'stk_code'])
        #         signal_data.sort_values('tdate', inplace=True)
        #         signal_data=signal_data[signal_data['stk_code'].apply(lambda x: type(x) == np.int64 or type(x) == np.int32 or type(x) == int)]
        #         signal_data = pd.pivot_table(signal_data, index=['tdate'], columns=['stk_code'],
        #                                      values=[signal_name])
        #
        #         signal_data.columns = signal_data.columns.get_level_values(1)
        #
        #         # save signal_data 保存为pickle文件
        #         fileHandle = open('./signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name), 'wb')
        #         pickle.dump(signal_data, fileHandle)
        #         fileHandle.close()
        #     else:
        #         original_signal_data = pd.read_pickle(
        #             './signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name)) if \
        #             len(pd.read_pickle(
        #                 './signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name))) != 0 else pd.DataFrame()
        #         # 调用combine_data_from_disk
        #         signal_data = Get_data_from_sshared_disk.combine_data_from_disk(self, signal_name, range=range)
        #         signal_data.sort_values('tdate', inplace=True)
        #         signal_data=signal_data[signal_data['stk_code'].apply(lambda x: type(x) == np.int64 or type(x) == np.int32 or type(x) == int)]
        #         signal_data = pd.pivot_table(signal_data, index=['tdate'], columns=['stk_code'],
        #                                      values=[signal_name])
        #         signal_data.columns = signal_data.columns.get_level_values(1)
        #
        #         final_signal_data = original_signal_data.append(signal_data)
        #         final_signal_data = final_signal_data[~final_signal_data.index.duplicated('last')]
        #         # save signal_data 保存为pickle文件
        #         fileHandle = open('./signal_data/{}/{}_pivot.pkl'.format(signal_name, signal_name), 'wb')
        #         pickle.dump(final_signal_data, fileHandle)
        #         fileHandle.close()



    #获取signal信息
    def get_all_signal_data(self,range=None):

        # try:
        #     win32wnet.WNetAddConnection2(0, None, r'\\space\signal', None, r'space\bsshare', '!@#$QWERasdf')
        # except Exception as e:
        #     print("建立连接时出错：", e)

        signal_name_dirs_list= os.listdir(r'\\space\signal')

        signal_name_dirs_list=list(set(signal_name_dirs_list)-set(['nohup.out','high_VolVar']))

        #获取文件的成立时间
        signal_name_dirs_list.sort()

        signal_name_dirs_list=signal_name_dirs_list[:]
        for siganl_name in tqdm(signal_name_dirs_list):
            #排序signal_name_dirs_list
            print(siganl_name)
            Get_data_from_sshared_disk.save_date_from_disk(self,siganl_name,range=range)
        print('Get_data_from_sshared_disk ')

        # 设一个函数，这个函数能输入因子名称，返回一个量化投资部门网盘里该因子打分的每日打分信息，格式是三列，分别是tdate，stk_code和因子名称

    def combine_theme_data_from_disk(self, signal_name, range=None, start_date=None, end_date=None, path='\\\\space\\alpha\\dyn_broad_zz800_cne5citics_size_beta\\subtheme'):
        # 如果不存在这个选定的因子signal_name，warning
        if not os.path.exists('{}\\{}'.format(path,signal_name)):
            warnings.warn('theme does not exist, change signal')
        else:
            # 查找对应文件夹的所有csv文件
            files = os.listdir('{}\\{}'.format(path,signal_name))
            # 通过 函数list_str_filter，过滤出只包含raw这个字段的文件 因为有detail和raw的重复的，格式也不一样
            files = list_str_filter('raw', list_sample=files) if len(
                list_str_filter('raw', list_sample=files)) != 0 else files
            # 对list排序,获取最近range项的数据累加
            files.sort(reverse=True)
            
            # 如果提供了start_date和end_date，按日期过滤文件
            if start_date is not None or end_date is not None:
                filtered_files = []
                for file in files:
                    try:
                        # 从文件名中提取日期 (格式: filename.YYYYMMDD.csv)
                        file_date_str = file.split('.')[1]
                        file_date = int(file_date_str)
                        
                        # 应用日期过滤
                        if start_date is not None and file_date < start_date:
                            continue
                        if end_date is not None and file_date > end_date:
                            continue
                        
                        filtered_files.append(file)
                    except (IndexError, ValueError):
                        # 如果文件名格式不正确，跳过该文件
                        continue
                
                files = filtered_files
            elif range is not None:
                # 保持原有的range逻辑作为后备
                files = files[:range]
                
            # 建立一个空dataframe 列明分别是tdate，stk_code和因子名称
            siganl_data = pd.DataFrame(columns=['tdate', 'stk_code', signal_name])
            file_list = []

            def change_data_formation(file):
                file_data = pd.read_csv('{}\\{}\\{}'.format(path,signal_name, file), header=None)
                file_data.insert(0, 'tdate', file.split('.')[1])
                file_data.columns = ['tdate', 'stk_code', signal_name]
                file_data = file_data.dropna()
                # 防止出现因为游T这个str搞得整个数据格式从int变成str 导致格式不一致的问题
                if file_data['stk_code'].dtypes == 'int64' or file_data['stk_code'].dtypes == 'int32' or \
                        file_data['stk_code'].dtypes == 'int' or file_data['stk_code'].dtypes == 'float':
                    file_data['stk_code'] = file_data['stk_code'].astype('int')
                else:  # file_data['stk_code'].dtypes=='O'
                    # print('{} type is str or O'.format(signal_name))
                    for index in file_data.index:
                        try:
                            file_data.loc[index, 'stk_code'] = int(file_data.loc[index, 'stk_code'])
                        except:
                            pass
                        else:
                            file_data.loc[index, 'stk_code'] = int(file_data.loc[index, 'stk_code'])

                # if file_data['stk_code'].dtypes == 'float':
                #     file_data['stk_code'] = file_data['stk_code'].astype('int')
                # elif file_data['stk_code'].dtypes!='int64':
                #     # 去掉关键字符
                #     file_data['stk_code'] = file_data['stk_code'].apply(lambda x: re.sub('A|T|B|I|J', '', x))
                #     file_data['stk_code'] = file_data['stk_code'].astype('int')
                # else:
                #     pass
                # #调试成功就隐藏这个代码
                # if file_data['stk_code'].dtypes != 'int64' and file_data['stk_code'].dtypes != 'int32' and \
                #         file_data['stk_code'].dtypes != 'int':
                #     print('{} of {} columns are not int64 int32 '.format(file,signal_name))
                # else:
                #     pass

                return (file_data)

            siganl_data = pd.concat(list(change_data_formation(file) for file in files))
            siganl_data.sort_values(by=['tdate', 'stk_code'], axis=0, ascending=True, inplace=True)
            siganl_data.drop_duplicates(subset=['tdate', 'stk_code'], keep='last', inplace=True)
            siganl_data.reset_index(drop=True, inplace=True)

            return siganl_data

        # 设计一个函数，这个函数只能建立路径保存在signal_data的文件夹中

    def save_theme_date_from_disk(self, signal_name, range=None):

        if not os.path.exists('./theme/{}/'.format(signal_name)):
            print('# theme_data file not exist , creating...... ')
            os.makedirs('./theme/{}/'.format(signal_name))

        if not os.path.exists('./theme/{}/{}.pkl'.format(signal_name, signal_name)):
            # 调用combine_data_from_disk
            signal_data = Get_data_from_sshared_disk.combine_theme_data_from_disk(self, signal_name).sort_values(
                by=['tdate', 'stk_code'])
            # save signal_data 保存为pickle文件
            fileHandle = open('./theme/{}/{}.pkl'.format(signal_name, signal_name), 'wb')
            pickle.dump(signal_data, fileHandle)
            fileHandle.close()
        else:
            original_signal_data = pd.read_pickle('./theme/{}/{}.pkl'.format(signal_name, signal_name)) if \
                len(pd.read_pickle('./theme/{}/{}.pkl'.format(signal_name, signal_name))) != 0 else pd.DataFrame()
            # 调用combine_data_from_disk
            signal_data = Get_data_from_sshared_disk.combine_theme_data_from_disk(self, signal_name, range=range)
            final_signal_data = original_signal_data._append(signal_data)
            # 规范格式
            final_signal_data.sort_values(by=['tdate', 'stk_code'], axis=0, ascending=True, inplace=True)
            final_signal_data.drop_duplicates(subset=['tdate', 'stk_code'], keep='last', inplace=True)
            final_signal_data.reset_index(drop=True, inplace=True)
            final_signal_data = final_signal_data.sort_values(by=['tdate', 'stk_code'])
            # save signal_data 保存为pickle文件
            fileHandle = open('./theme/{}/{}.pkl'.format(signal_name, signal_name), 'wb')
            pickle.dump(final_signal_data, fileHandle)
            fileHandle.close()

        signal_data = pd.read_pickle('./theme/{}/{}.pkl'.format(signal_name, signal_name)) if \
            len(pd.read_pickle('./theme/{}/{}.pkl'.format(signal_name, signal_name))) != 0 else \
            Get_data_from_sshared_disk.combine_theme_data_from_disk(self, signal_name).sort_values(by=['tdate', 'stk_code'])
        signal_data.sort_values('tdate', inplace=True)
        # 删除非int格式 通过查非字符型
        signal_data = signal_data[
            signal_data['stk_code'].apply(lambda x: type(x) == np.int64 or type(x) == np.int32 or type(x) == int)]

        signal_data = pd.pivot_table(signal_data, index=['tdate'], columns=['stk_code'],
                                     values=[signal_name])

        signal_data.columns = signal_data.columns.get_level_values(1)
        # save signal_data 保存为pickle文件
        fileHandle = open('./theme/{}/{}_pivot.pkl'.format(signal_name, signal_name), 'wb')
        pickle.dump(signal_data, fileHandle)
        fileHandle.close()



    def get_all_theme_data(self,range=None,path='\\\\space\\alpha\\dyn_broad_zz800_cne5citics_size_beta\\subtheme'):
        signal_name_dirs_list=os.listdir(path)
        for siganl_name in tqdm(signal_name_dirs_list):
            print(siganl_name)
            Get_data_from_sshared_disk.save_theme_date_from_disk(self,siganl_name,range=range)
        print('Get_theme_data_from_sshared_disk ')





    #获取量化的禁投池
    def get_forbid_pool(self,range=None):

        start_date, original_forbid_data = \
            setting_startdate_and_saving_path_dataframe('forbid_data',
                                                        'forbid_data.pkl',range)

        start_date, original_forbid_pivot_data = \
            setting_startdate_and_saving_path_dataframe('forbid_data',
                                                        'forbid_pivot_data.pkl',range)


        forbit_dirs_list=os.listdir('\\\\space\\forbid')

        # 对list排序,获取最近range项的数据累加
        forbit_dirs_list.sort(reverse=True)
        if range == None:
            pass
        else:
            files = forbit_dirs_list[:range]
        # 建立一个空dataframe 列明分别是tdate，stk_code和因子名称
        forbit_data = pd.DataFrame(columns=['tdate', 'stk_code','signal'])
        file_list = []

        def change_data_formation(file):
            file_data = pd.read_csv('\\\\space\\forbid\\{}'.format(file))
            file_data.insert(0, 'tdate', file.split('.')[1])
            file_data.columns = ['tdate', 'stk_code','signal']
            file_data = file_data.dropna()
            # 防止出现因为游T这个str搞得整个数据格式从int变成str 导致格式不一致的问题
            if file_data['stk_code'].dtypes == 'int64' or file_data['stk_code'].dtypes == 'int32' or \
                    file_data['stk_code'].dtypes == 'int' or file_data['stk_code'].dtypes == 'float':
                file_data['stk_code'] = file_data['stk_code'].astype('int')
            else:  # file_data['stk_code'].dtypes=='O'
                # print('{} type is str or O'.format(signal_name))
                for index in file_data.index:
                    try:
                        file_data.loc[index, 'stk_code'] = int(file_data.loc[index, 'stk_code'])
                    except:
                        pass
                    else:
                        file_data.loc[index, 'stk_code'] = int(file_data.loc[index, 'stk_code'])

            # if file_data['stk_code'].dtypes == 'float':
            #     file_data['stk_code'] = file_data['stk_code'].astype('int')
            # elif file_data['stk_code'].dtypes!='int64':
            #     # 去掉关键字符
            #     file_data['stk_code'] = file_data['stk_code'].apply(lambda x: re.sub('A|T|B|I|J', '', x))
            #     file_data['stk_code'] = file_data['stk_code'].astype('int')
            # else:
            #     pass
            # #调试成功就隐藏这个代码
            # if file_data['stk_code'].dtypes != 'int64' and file_data['stk_code'].dtypes != 'int32' and \
            #         file_data['stk_code'].dtypes != 'int':
            #     print('{} of {} columns are not int64 int32 '.format(file,signal_name))
            # else:
            #     pass

            return (file_data)

        file_data = pd.concat(list(change_data_formation(file) for file in forbit_dirs_list))
        file_data.sort_values(by=['tdate', 'stk_code'], axis=0, ascending=True, inplace=True)
        file_data.drop_duplicates(subset=['tdate', 'stk_code'], keep='last', inplace=True)
        file_data.reset_index(drop=True, inplace=True)

        file_data = original_forbid_data._append(file_data)
        file_data = file_data[~file_data.index.duplicated('last')]


        fileHandle = open('./forbid_data/forbid_data.pkl', 'wb')
        pickle.dump(file_data, fileHandle)
        fileHandle.close()


        file_pivot_data = pd.pivot_table(file_data, index=['tdate'], columns=['stk_code'],
                                           values=['signal'])
        file_pivot_data.columns = file_pivot_data.columns.get_level_values(1)

        file_pivot_data = original_forbid_pivot_data._append(file_pivot_data)
        file_pivot_data = file_pivot_data[~file_pivot_data.index.duplicated('last')]

        fileHandle = open('./forbid_data/forbid_pivot_data.pkl', 'wb')
        pickle.dump(file_pivot_data, fileHandle)
        fileHandle.close()

        return file_pivot_data




        print('forbid_data is saved ')

    def stk_filter(self,date,trade_date_array,T=0):
        '''
        输入特定日期，整体交易日期区间（我不太想要这个，但不想改了暂时，交易间隔即多久调仓）
        :param date:
        :param trade_date_array:
        :param T:
        :return:
        '''
        change_date = date
        ###剔除上市未满半年的新股，
        idx1 = np.where(trade_date_array == change_date)[0][0]
        idx2 = idx1 - self.IPOBDates  # self.IPOBDates dates before selected into portfolio
        new_stock_date = trade_date_array[idx2]  # stock IPO must before new_stock_date then can be selected into portfolio
        # sql = "select S_INFO_WINDCODE as stk_code from wind_quant.dbo.AShareDescription where S_INFO_LISTDATE>='{}' and S_INFO_LISTDATE<='{}'".format(
        #     new_stock_date,change_date)
        sql = "select S_INFO_WINDCODE as stk_code from wind_quant.dbo.AShareDescription where S_INFO_LISTDATE>='{}'".format(
            new_stock_date)
        new_lst = pd.read_sql(sql, self.con_wind_db)
        new_lst = new_lst[~ new_lst['stk_code'].str.contains('T')]
        new_lst = new_lst[~ new_lst['stk_code'].str.contains('BJ')]
        new_lst = new_lst[~ new_lst['stk_code'].str.contains('A')]
        if len(new_lst['stk_code'])!=0:
            new_lst['stk_code'] = new_lst['stk_code'].str.split('.', expand=True)[0].astype('int')

        new_lst = new_lst['stk_code'].tolist()

        # ####剔除ST
        idx_STEntry = idx1 - 20
        # idx_STRemove = idx1 + T + 1 if (idx1 + T + 1)<=len(trade_date_array) else idx1
        idx_STRemove = idx1
        STEntry_date = trade_date_array[idx_STEntry]
        STRemove_date = trade_date_array[idx_STRemove]
        sql = "select S_INFO_WINDCODE as stk_code, ENTRY_DT, REMOVE_DT from wind_quant.dbo.AShareST " \
              "where (ENTRY_DT<='{}' and REMOVE_DT>'{}') or (ENTRY_DT<='{}' and REMOVE_DT is NULL)".format(
            STEntry_date, STRemove_date, STEntry_date)
        st_list = pd.read_sql(sql, self.con_wind_db)
        st_list = st_list[~ st_list['stk_code'].str.contains('T')]
        st_list = st_list[~ st_list['stk_code'].str.contains('BJ')]
        if len(st_list['stk_code'])!=0:
            st_list['stk_code'] = st_list['stk_code'].str.split('.', expand=True)[0].astype('int')

        st_list = st_list['stk_code'].tolist()

        ###剔除调仓日停牌
        sql = "select S_INFO_WINDCODE as stk_code,S_DQ_TRADESTATUS from wind_quant.dbo.AShareEODPrices where S_DQ_TRADESTATUSCODE!='-1' and TRADE_DT='{}'".format(
            change_date)

        status_df = pd.read_sql(sql, self.con_wind_db)
        status_df = status_df[~ status_df['stk_code'].str.contains('T')]
        status_df = status_df[~ status_df['stk_code'].str.contains('BJ')]
        status_df = status_df[(status_df['S_DQ_TRADESTATUS'] == '停牌')|(status_df['S_DQ_TRADESTATUS'] == '上市首日')]
        if len(status_df['stk_code'])!=0:
            status_df['stk_code'] = status_df['stk_code'].str.split('.', expand=True)[0].astype('int')
        status_lst = status_df['stk_code'].tolist()

        ###剔除调仓日涨跌停的股票
        sql = "select S_INFO_WINDCODE as stk_code from wind_quant.dbo.AShareEODDerivativeIndicator where UP_DOWN_LIMIT_STATUS!=0 and TRADE_DT='{}'".format(
            change_date)
        limit_lst = pd.read_sql(sql, self.con_wind_db)
        limit_lst = limit_lst[~ limit_lst['stk_code'].str.contains('T')]
        limit_lst = limit_lst[~ limit_lst['stk_code'].str.contains('BJ')]
        if len(limit_lst['stk_code'])!=0:
            limit_lst['stk_code'] = limit_lst['stk_code'].str.split('.', expand=True)[0].astype('int')
        limit_lst = limit_lst['stk_code'].tolist()

        return new_lst,st_list,status_lst,limit_lst


    #因为发现量化的禁投池不满足我的需求，我觉得可能不全，把他和我的需求做个总和
    def get_tot_forbid_pivot_data(self, range=None):
        '''
        all_forbid_pivot_data   包括所有的
        no_limt_forbid_pivot_data 仅包括我自己排除limit的
        tot_forbid_pivot_data no_limt_forbid_pivot_data加上量化投资部的

        :param range:
        :return:
        '''



        start_date, original_forbid_data = \
            setting_startdate_and_saving_path_dataframe('forbid_data',
                                                        'tot_forbid_pivot_data.pkl',range)

        #set an dataframe having  dates and stk columns from the class

        forbid_df=pd.DataFrame(index=self.Tradedays_list,columns=self.stk_pool)
        forbid_tot=pd.DataFrame(index=self.Tradedays_list,columns=self.stk_pool)
        all_forbid=pd.DataFrame(index=self.Tradedays_list,columns=self.stk_pool)

        quant_forbit_df=Get_data_from_sshared_disk.get_forbid_pool(self)

        trade_date_data_array=np.array(self.trade_date_data_array)
        quant_forbit_df=quant_forbit_df.reindex(index=forbid_df.index,columns=forbid_df.columns)

        for num, raw in tqdm(enumerate(forbid_df.index)):

            new_lst,st_list,status_lst,limit_lst=Get_data_from_sshared_disk.stk_filter(self,raw,trade_date_data_array)
            forbit_stk_list=new_lst+st_list+status_lst  #我这里没有加涨跌停的股票，因为我是为了测IC，我感觉IC最好不考虑涨跌停比较好，未来信息的原因  后来又加涨停
            forbit_and_limit_list=new_lst+st_list+status_lst +limit_lst
            forbit_stk_list=list(set(forbit_stk_list))

            #自己的禁投池版本
            forbid_df.loc[raw][forbid_df.loc[raw].index.isin(forbit_and_limit_list)]=1

            #全部 除了涨跌停的
            forbid_tot.loc[raw][forbid_tot.loc[raw].index.isin(forbit_stk_list)] = 1
            forbid_tot.loc[raw][forbid_tot.loc[raw].index.isin(quant_forbit_df.loc[raw].dropna().index)] = 1

            #全部的
            all_forbid.loc[raw][all_forbid.loc[raw].index.isin(forbit_and_limit_list)] = 1
            all_forbid.loc[raw][all_forbid.loc[raw].index.isin(quant_forbit_df.loc[raw].dropna().index)] = 1





        fileHandle = open('./forbid_data/tot_forbid_pivot_data.pkl', 'wb')
        pickle.dump(forbid_tot, fileHandle)
        fileHandle.close()
        print('tot_forbid_pivot_data  data is saved')

        fileHandle = open('./forbid_data/selfforbid_pivot_data.pkl', 'wb')
        pickle.dump(forbid_df, fileHandle)
        fileHandle.close()
        print('selfforbid_pivot_data  data is saved')


        fileHandle = open('./forbid_data/all_forbid_pivot_data.pkl', 'wb')
        pickle.dump(all_forbid, fileHandle)
        fileHandle.close()
        print('all_forbid_pivot_data  data is saved')








if __name__ == '__main__':

    getting=Get_data_from_sshared_disk()
    # getting.get_forbid_pool()
    # getting.get_tot_forbid_pivot_data()


    testdata=getting.combine_data_from_disk('high_VolVar')
    # # getting.save_date_from_disk('aeb2ev')
    # getting.get_all_signal_data()
    #
    # print('good')