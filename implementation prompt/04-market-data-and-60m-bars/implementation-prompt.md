# Feature 04 — Market Data and 60-Minute Bars

> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：yfinance ticker、參數、interval、回傳欄位、時區、更新頻率與可用期間必須依目前鎖定版本及測試 fixture 驗證；未明載或無法驗證者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；行情 adapter 與 K 棒聚合器須提供完整型別註記並可由 pytest 獨立測試。

## 任務

建立單一 yfinance 市場資料 adapter 與可測試的 60 分鐘 K 棒正規化流程，使用台灣期貨交易所交易日／交易時段語意。先取得客戶確認的 60 分 K 切點，並以 yfinance 實際 timestamp／interval 行為驗證；將時段日曆及切點做成設定，不得自行以整點猜測。禁止建立或保留元大／SPARK 行情 adapter。

## 必須實作

- 透過 yfinance 對選定契約的受控 Yahoo ticker 定期查詢市場價格與 `interval="1h"` OHLCV，驗證 ticker、契約 identity、時間戳、欄位、價格、成交量及資料新鮮度。找不到可靠 mapping 時 fail closed。
- 將 yfinance bar 正規化為 `Asia/Taipei` 與 UTC；K 棒只在確定收盤後發出一次 `BarClosed`。重複、亂序、修訂或仍在形成中的資料必須有明確策略，不得合成 tick。
- 正確跨越日盤、夜盤、午夜、週末、休市、提早收盤與無成交區間；交易日不可直接等同曆日。
- 啟動或重連後只使用 yfinance 與本機已保存的 yfinance bars 重建最近 K 棒。若資料有 gap、重疊、修訂或來源 identity 不一致，暫停訊號產生並告警；不得改向元大期貨 API 取價。
- 定義紅 K 為 `close > open`、黑 K 為 `close < open`；十字 K `close == open` 必須中斷連續紅／黑計數。
- 08:45、09:45 的已收 K 可供畫面顯示與計數規則測試，但建倉閘門不得放行；最早 10:45 才可判斷建倉。將確切 bar label/close-time 語意記錄清楚。

## 除錯日誌需求

- 行情診斷須記錄 yfinance ticker、查詢範圍、interval、請求／回應時間、首末 timestamp、列數、gap、重複、亂序、修訂、stale 狀態、rate limit、重試與延遲；避免在 INFO log 輸出完整 DataFrame。
- 每根 K 棒記錄 identity、交易日／session、start/end、OHLCV、來源 ticker、抓取時間、完整性、收盤原因及 `BarClosed` 發送序號；晚到或修訂資料須記錄其歸屬與採取的處置。
- gap／重連重建須記錄資料來源、查詢範圍、重疊／衝突、連續性檢查及訊號是否被阻擋，時間同時保留 UTC 與 `Asia/Taipei`。

## 驗收

使用固定 yfinance DataFrame fixture 驗證欄位正規化、OHLCV、bar 邊界、亂序／重複／修訂資料、午夜、重連補洞及十字 K。畫面可看到 yfinance 有提供且尚未收盤的 forming bar、最近 closed bars、紅黑判斷、來源 ticker、最後抓取／資料時間與 stale 狀態。資料 stale 時不得送出新委託。
