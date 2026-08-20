# Feature 06 — Order and Fill State Machine

> **API 文件唯一來源：實作前必須直接讀取[元大 SPARK API 官方入口](https://www.yuanta.com.tw/file-repository/content/API/page/index.html)及其下方連結的 API 說明文件、範例、元件下載與換版資訊。不得使用專案內既有資料夾、舊 SDK 文件、舊範例或既有程式碼反推 API 規格；若與本 prompt 其他描述衝突，以官方線上文件當下內容為準。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；以具型別的 Python 狀態模型實作委託、成交與冪等控制。

## 任務

實作唯一有權將交易意圖轉成元大委託的 order manager。以券商委託回報與成交回報為事實來源，不能把「API 送出成功」視為已成交，也不能用行情推測成交。

## 狀態模型

至少區分 `Created`、`Submitting`、`Acknowledged`、`PartiallyFilled`、`Filled`、`CancelPending`、`Cancelled`、`Rejected`、`Unknown`。為每張委託保存本地 intent ID、client correlation ID、券商委託號、商品契約、買賣別、開平語意、數量、累計成交量、成交均價、時間戳及原始回報摘要。定義允許的狀態轉移並容忍 callback 重複與亂序。

## 必須實作

- 送單前原子地持久化 intent 與唯一冪等鍵，再呼叫 API；同一 intent 不可由重試或重複 K 棒再次送出。
- 每個帳號／契約同一時間只允許一個改變持倉的 workflow；新訊號不得越過活動委託。
- 將委託回報與成交回報分開處理，依券商鍵值去重；部分成交必須更新實際成交量。
- 未成交逾時、部分成交、拒單、查無委託、回報缺欄位或狀態無法判定時，標成安全暫停，禁止自動重送。
- 重啟或斷線後先查詢券商委託、成交與持倉，關聯本地紀錄；無法唯一關聯即 `Unknown`。
- 嚴格檢查最大 2 口與方向，使用送單前持倉加上所有可能成交的活動委託計算最壞曝險。

## 除錯日誌需求

- 每筆 order intent 記錄本地 order ID、workflow/correlation ID、商品契約、方向、開平、數量、建立原因、冪等鍵與呼叫券商前的持久化結果。
- 每次狀態轉移須記錄 from/to、觸發事件、券商單號遮罩、累計／本次成交量、剩餘量、broker error code、事件序號及重複／亂序判定；非法轉移須保留完整診斷。
- timeout、取消、拒單、部分成交與 `Unknown` 須記錄後續查詢及「未重送」決策，並能由 fill ID 串回原 intent；不得記錄完整帳號或敏感原始 payload。

## 驗收

用模擬 adapter 測試正常成交、同步 callback、回報先於函式返回、部分成交、拒單、逾時後晚到成交、重複成交、亂序、取消競態及程式在提交瞬間崩潰。任何不確定案例都必須停在可人工處理的狀態，且測試證明沒有第二張重複委託。
