# Feature 04 — Market Data and 60-Minute Bars

> **API 文件唯一來源：實作前必須直接讀取[元大 SPARK API 官方入口](https://www.yuanta.com.tw/file-repository/content/API/page/index.html)及其下方連結的 API 說明文件、範例、元件下載與換版資訊。不得使用專案內既有資料夾、舊 SDK 文件、舊範例或既有程式碼反推 API 規格；若與本 prompt 其他描述衝突，以官方線上文件當下內容為準。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；行情 adapter 與 K 棒聚合器須提供完整型別註記並可由 pytest 獨立測試。

## 任務

建立元大行情 adapter 與可測試的 60 分鐘 K 棒聚合器，使用台灣期貨交易所交易日／交易時段語意。先取得客戶確認的 60 分 K 切點與 API 時戳定義；將時段日曆及切點做成設定，不得自行以整點猜測。

## 必須實作

- 訂閱選定契約的即時成交行情，驗證商品、時間戳、價格、數量、序號與資料新鮮度。
- 以 `Asia/Taipei` 時區聚合 OHLCV；K 棒只在確定收盤後發出一次 `BarClosed`，晚到或重複 tick 必須有明確策略。
- 正確跨越日盤、夜盤、午夜、週末、休市、提早收盤與無成交區間；交易日不可直接等同曆日。
- 啟動或重連後，僅在官方線上文件明載相應歷史／分時查詢能力時使用該 API 取得資料重建最近 K 棒；否則使用本機已保存資料。若資料有 gap、重疊或來源不一致，暫停訊號產生並告警。
- 定義紅 K 為 `close > open`、黑 K 為 `close < open`；十字 K `close == open` 必須中斷連續紅／黑計數。
- 08:45、09:45 的已收 K 可供畫面顯示與計數規則測試，但建倉閘門不得放行；最早 10:45 才可判斷建倉。將確切 bar label/close-time 語意記錄清楚。

## 除錯日誌需求

- 行情診斷須以可設定的採樣／彙總方式記錄訂閱、首筆／末筆 tick、序號 gap、重複、亂序、stale 狀態與延遲；避免逐 tick 的 INFO log 阻塞 callback，必要逐筆資料只在受控 DEBUG 模式輸出。
- 每根 K 棒記錄 identity、交易日／session、start/end、OHLCV、tick count、完整性、收盤原因及 `BarClosed` 發送序號；晚到 tick 須記錄其歸屬與採取的處置。
- gap／重連重建須記錄資料來源、查詢範圍、重疊／衝突、連續性檢查及訊號是否被阻擋，時間同時保留 UTC 與 `Asia/Taipei`。

## 驗收

使用固定 tick fixture 驗證 OHLCV、邊界 tick、亂序／重複 tick、午夜、重連補洞及十字 K。畫面可看到目前 forming bar、最近 closed bars、紅黑判斷、行情最後更新時間與 stale 狀態。資料 stale 時不得送出新委託。
