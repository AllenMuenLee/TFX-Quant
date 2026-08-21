# Feature 13 — Logging, Errors, and Audit Trail

> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；採用 Python 結構化 logging 與可追蹤 correlation context。

## 任務

建立結構化、可搜尋且不可混淆順序的執行紀錄，支援問題診斷、交易稽核與 UI 顯示，同時保護帳密與個資。

## 必須實作

- 定義事件分類：session、market data、bar、strategy decision、risk decision、order intent、broker request、order report、fill、position query、reconciliation、user action、system/error。
- 每筆事件含 UTC 與台北時間、單調序號、severity、correlation/workflow ID、策略狀態、商品契約及結構化 payload；保存原始券商錯誤碼但遮蔽敏感資料。
- 策略每次「有動作或無動作」都記錄輸入 bar ID、持倉、規則結果及阻擋原因，讓事後能重現決策。
- 使用 rolling files 與資料庫 audit table；設定保存期限、容量上限與匯出功能。log 寫入失敗需告警，關鍵交易 audit 無法持久化時安全暫停。
- 全域例外處理不得吞錯；將技術錯誤轉為可行動的繁中訊息，同時保留 stack trace 供支援人員查閱。
- UI 顯示目前錯誤與歷史紀錄，可依時間、severity、商品、order/workflow ID 篩選。
- 記錄使用者的啟動、暫停、停止、同步、緊急平倉及確認內容，但不記錄密碼／憑證／完整帳號。

## 除錯日誌需求

- 定義並文件化 DEBUG／INFO／WARNING／ERROR／CRITICAL 的使用準則、事件 schema、必要欄位、event-name catalog、correlation 傳播規則與 payload 版本；schema 驗證失敗本身也須可觀測。
- 記錄 logger 啟動、sink readiness、queue depth、批次寫入耗時、輪替、保留清理、drop／sampling 數及 file/database sink 失敗；關鍵 audit 寫入失敗須有獨立告警與安全暫停事件。
- 提供受控診斷模式以提高特定 workflow／order ID 的詳細度，須有自動到期與容量上限；測試證明密碼、token、完整帳號及敏感 exception/payload 在所有 sink、匯出與 stack trace 中皆被遮蔽。

## 驗收

測試高頻行情下不阻塞交易佇列、檔案輪替、磁碟滿、資料庫鎖定、敏感欄位遮蔽、跨 thread 事件排序及 correlation chain。從一個反手案例的匯出紀錄應能完整追到 K 棒、策略意圖、平倉、零持倉確認與反向建倉。
