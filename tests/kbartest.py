import os, time, datetime, struct, pathlib, sys
from datetime import datetime
from pathlib import Path
from pythonnet import load

load("coreclr")
import clr, System

##透過Clr引用系統標準函式
clr.AddReference('System.Collections')
from System.Collections.Generic import List

##宣告增加模組、DLL的路徑(windows可抓取當前路徑 Linux跟MAC需指定路徑)
dll_dir = Path(r"C:\Yuanta\SparkAPI")
sys.path.append(str(dll_dir))
if sys.platform == "win32":
    os.add_dll_directory(str(dll_dir))

##透過Clr引用YuantaSparkAPI.dll
##pythonnet引用元件不用加附檔名
try:
    clr.AddReference("YuantaSparkAPI")
except Exception as e:
    print(f"Error loading YuantaSparkAPI: {e}")
from YuantaOneAPI import (YuantaSparkAPITrader, enumLogType, 
                        enumMarketType, enumEnvironmentMode, enumStkTickSelectType,
                        KLineType)

# 建立 API 物件
objYuantaSparkAPI = YuantaSparkAPITrader()
objYuantaSparkAPI.SetLogType(enumLogType.COMMON)

def on_response(intMark, dwIndex, strIndex, objHandle, objValue):
    try:
        result = ''
        match intMark:
            case 0:  # 系統回應資訊
                result = str(objValue)
            case 1:  # 查詢回應資訊
                match strIndex:
                    case 'Login':
                        loginResult = objValue
                        status = loginResult.LoginStatus
                        strMsgCode = status.MsgCode # 訊息代碼
                        strMsgContent = status.MsgContent # 訊息內容
                        intCount = status.Count # 筆數
                        result = '{0},{1},帳號筆數:{2}\r\n'.format(strMsgCode,strMsgContent, str(intCount))
                        if strMsgCode == '0001' or strMsgCode == '00001' or intCount > 0 :
                            for i in objValue.LoginList:
                                result += f"{i.Account},{i.Name},{i.InvestorID},{i.SellerNo}\n"

                    case 'GetKLine':
                        KResult = objValue
                        kResult = KResult.KLineList

                        result = ''
                        result += 'K線查詢結果:\r\n'
                        result += '市場代碼:{0}, 商品代碼:{1}\r\n'.format(KResult.MarketNo, KResult.StockCode)
                        for i in range(kResult.Count):  
                            result += '{0},{1},{2},{3},{4},{5}\r\n'.format(kResult[i].TimeStamp,kResult[i].OpenPrice,kResult[i].HighPrice,kResult[i].LowPrice,kResult[i].ClosePrice,kResult[i].DealVol)

        if result:
            print('##================================================##\n')
            print(result)

    except Exception as error:
        print(f"處理回應時發生錯誤: {error}")

objYuantaSparkAPI.OnResponse += on_response
#測試環境帳號:UAT 正式環境:PROD
objYuantaSparkAPI.Open(enumEnvironmentMode.UAT)
time.sleep(2)

objYuantaSparkAPI.Login('S98875005091', '1234')
time.sleep(2)

daily_kline_type = KLineType(11)    #日K
#weekly_kline_type = KLineType(12)   #週K
#monthly_kline_type = KLineType(13)  #月K
objYuantaSparkAPI.GetKLine('S98875005091', daily_kline_type, enumMarketType.TWSE, "2885", "2025/01/01", "2025/03/31")

while True:
    time.sleep(2)