# Feature 04 Extension — Two-Month 1h Bar History and yfinance Backfill

> **元大期貨交易 API 文件唯一來源是專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/)；本功能不得使用該交易 API 取得任何市場價格。所有 OHLCV 與 K 棒唯一外部來源為 `yfinance`。**

> 本 prompt 獨立於同資料夾的行情與 60 分 K implementation prompt；實作已收 K 棒的持久化、載入、查詢、兩個月保存規則，以及使用 `yfinance` 回補本機資料庫缺少的 1 小時 K 棒。

## 任務

將軟體自行聚合且已確定收盤的 60 分 K 持久化，並在啟動、重連、切換契約或偵測到缺口時，使用 `yfinance` 下載目標商品過去兩個曆月的 `1h` K 棒。從 yfinance 查回且驗證成功的 K 棒必須直接寫入 local database，以實際補齊本機資料庫缺少的區間。使用者應可查閱最近兩個月的 K 棒，不可只依賴記憶體中的 DataFrame、cache 或 recent-bars deque。

`yfinance` 是歷史、最新可用價格、forming bar 與新收盤 K 棒的唯一外部市場資料來源。禁止從元大／SPARK API、交易回報或其他行情商取價。所有外部資料的 provenance 固定為 `yfinance`，並保存來源 ticker 與抓取時間；本機僅可保存、正規化及讀回這些資料，不構成另一個價格來源。

## 必須實作

- 強制使用 Python 與 `yfinance` 套件實作歷史回補 adapter，並完整標註型別。將 yfinance 呼叫隔離在 adapter／gateway，repository、缺口偵測與策略不得直接依賴第三方套件物件。
- 「過去兩個月」是以 `Asia/Taipei` 現在時間往前推兩個曆月的滾動區間且包含起點；例如 8 月 18 日的起點是 6 月 18 日。不得以固定 60 天取代。傳給 yfinance 的查詢 `start`／`end` 必須涵蓋此區間，並明確處理 `end` exclusive、時區轉換與尚未收盤的最後一根 bar。
- 先查詢本機 repository，再依預期交易時段與 canonical bar identity 計算缺少區間。只向 yfinance 查詢缺少的範圍；允許為了 API 邊界與連續性檢查在缺口兩側多取少量資料，但寫入時仍須限定在兩個月窗口與目標商品／契約。
- yfinance 查詢必須明確使用 `interval="1h"`。不得以日 K、較低週期重採樣、前收價、前一根 close、合成 tick 或空白 bar 偽造缺少的 1 小時 K 棒。
- yfinance 每一批查詢結果完成正規化與驗證後，必須在同一次 backfill workflow 中立即透過 repository 寫入 local database。不得只回傳 DataFrame 給 UI／策略、只放在記憶體 cache、只寫檔案，或等到程式結束時才落盤。workflow 只有在 database transaction 成功 commit 後，才可將該批 bar 視為已回補。
- local database 寫入須採用有界批次 transaction 與 idempotent upsert。若某批 commit 失敗，必須 rollback 該 transaction、保留原有 gap、記錄失敗並依政策重試；不得把尚未 commit 的資料加入 recent-bars、宣告 backfill 成功或提供策略使用。
- 建立可設定且可測試的「內部商品／契約 → Yahoo ticker」mapping。找不到明確 mapping 時不得猜測 ticker，也不得錯用 continuous contract 取代指定月份契約；應保留 gap、告警並將 readiness 標成 degraded。若產品明確允許 continuous contract，必須以不同 instrument identity 保存並在 UI 標示。
- yfinance 回傳資料須先正規化欄位名稱與 index，驗證 ticker、時間戳、時區、OHLC、Volume、重複列、排序及有限數值。價格使用精確 decimal；時間須轉成可無歧義還原的 UTC 與 `Asia/Taipei`。預設使用未自動調整的價格，`auto_adjust` 等價格調整選項必須明確設定，不得依賴套件預設值。
- Yahoo 的 `1h` bar 邊界必須與系統 canonical 60 分 K 的 session、`start`、`end` 定義核對。只有能唯一對齊、已確定收盤且通過交易日曆驗證的 bar 才可寫入 canonical 60 分 K 表；無法對齊、跨休市區間、時區不明或仍在形成中的 bar 一律拒絕並保留為 gap，不得為了補滿兩個月而改動時間戳。
- 每筆保存商品、契約月份、週期、交易日、session、`start`、`end`、OHLCV、來源、來源 ticker、抓取時間、完整性／gap、建立及更新時間。所有外部市場資料來源固定標示為 `yfinance`；不得出現元大即時行情或其他價格來源值。
- 以商品、契約、週期及 `start` 等欄位建立唯一 bar identity 與 database unique constraint。採用 idempotent upsert：重複下載或程序重啟不得新增重複資料；已確定收盤的 yfinance bar 如後續內容改變，須套用明確 revision policy，不得靜默覆寫。
- 相同 identity 的 yfinance OHLCV 在不同抓取批次間衝突時，保存 revision audit 與前後摘要，維持既有 canonical bar 並阻擋該區段驅動訊號，直到依明確政策解決。
- 啟動、重連、切換契約及每日交易日切換後執行 backfill workflow：計算窗口、載入本機資料、找出 gaps、按 yfinance 限制分批抓取、正規化、驗證、直接 upsert 至 local database 並 commit，再由 database 重新讀取資料以計算 gaps 與可用範圍。不得以尚未落盤的 in-memory 查詢結果判定回補完成；下載、驗證與寫入不得阻塞交易事件處理。
- 對 yfinance 的空結果、部分結果、rate limit、timeout、網路錯誤及 schema 變化實作有界重試、exponential backoff 與可觀測錯誤。不得無限重試；最終失敗時保留 gap，停止以該缺失／stale 資料驅動新交易，並將 readiness／history completeness 顯示為 degraded。
- 只有連續性檢查通過的 closed bars 可供策略 warm-up 與訊號。yfinance 回補資料可供 UI 與 warm-up，但資料來源、契約 identity、bar 邊界或 gap 有疑義時不得驅動交易訊號。
- `BarClosed` 流程可靠寫入 repository，但不得阻塞 yfinance 查詢排程或交易事件處理。資料庫不可用時使用有界重試／待寫佇列並告警；無法保證落盤時 readiness 顯示 degraded。
- 啟動及每日交易日切換後執行 retention cleanup，只刪除早於兩個曆月起點的 K 棒。清理須限定 bar 資料範圍、可測試並產生 audit 摘要，不得刪除委託、成交、持倉或稽核資料。
- UI 可依商品、契約及日期區間瀏覽資料，並顯示每根 bar 的來源、完整性、最早與最晚可用時間及尚未補齊的區段；不足兩個月時必須明確提示，不得宣稱資料完整。

## 除錯日誌需求

- 記錄 `history_backfill_requested/result`、內部 instrument identity、Yahoo ticker、查詢區間、`interval="1h"`、批次、耗時、重試次數、原始筆數、驗證拒絕筆數、upsert／duplicate／conflict 筆數及剩餘 gaps。不得記錄憑證、cookie 或大量原始 payload。
- 記錄 yfinance 回傳 index 的原始時區、正規化後 UTC／`Asia/Taipei` 時間、bar 邊界比對結果，以及因 forming、休市、錯誤 ticker、無法對齊或 OHLCV 無效而拒絕的原因。
- 記錄 `bar_persist_requested/result`、bar identity、source、insert／upsert／duplicate／revision 判定、queue depth、資料庫耗時及完整性狀態；不得在 yfinance 查詢排程內同步輸出大量 payload。
- retention cleanup 須記錄 cutoff、限定條件、掃描／刪除筆數、transaction 結果及 audit ID；回補、寫入或清理失敗須記錄 readiness degraded 與是否阻擋訊號。

## 驗收

- 使用固定 clock 測試月底、跨年、閏年及不同月份天數的兩個曆月邊界，驗證 yfinance 查詢窗口與 repository retention 使用相同 cutoff，並正確處理夜盤跨午夜的交易日與 session。
- mock yfinance adapter；測試 `interval="1h"`、明確的價格調整選項、start／end 邊界、timezone-aware index、MultiIndex／一般欄位、空結果、部分結果、重複列、亂序、NaN、timeout、rate limit 及重試耗盡。
- 本機兩個月資料全空時，成功下載、驗證並直接寫入 local database 中可取得的 closed 1h bars；清空 process memory 並重啟後仍可完全由 database 查回。第二次執行相同 backfill 後資料筆數不增加，證明流程已持久化且 idempotent。
- 本機只有部分資料時，只回補缺少區間；回補後重新計算 gaps。yfinance 沒有提供的時段仍保持 gap，UI 顯示實際可用範圍，策略不跨 gap 產生訊號。
- 不同批次抓取的 yfinance bar 重疊且內容相同時維持單筆 canonical record；內容衝突時不靜默覆寫，產生 revision audit、degraded 狀態並阻擋該區段訊號。
- 驗證 Yahoo 1h 邊界無法對齊系統 60 分 K 定義時不寫入 canonical 表；不得透過移動時間戳或重採樣讓測試通過。
- ticker mapping 缺失、指定月份契約不存在或只找到 continuous ticker 時不得猜測替代品；保留 gap 並輸出可操作的錯誤資訊。
- 模擬資料庫寫入失敗、部分批次失敗及重試成功，確認 yfinance 查詢排程與交易事件處理不阻塞、transaction 範圍正確、最終不重複且失敗不會靜默遺失。
- 驗證 database commit 失敗時不得把該批 yfinance bars 視為已回補、不得更新完整性為 complete，也不得讓 UI／策略僅從記憶體看到未落盤資料；成功 commit 後須由 repository 重新查詢並確認筆數、identity、OHLCV 與 provenance。
- 重啟後可由 repository 還原最近兩個月 K 棒與紅／黑／十字狀態；邊界外資料被清理，邊界內資料與 provenance 不受影響。

> **yfinance 能力不得臆測：實作前須依目前安裝版本的公開 API 與實際回傳 fixture 確認參數、可查詢期間、interval、時區、欄位結構及錯誤行為。所有第三方差異須封裝在 adapter；無法可靠取得或驗證的期間保留為 gap。**
> **交易安全優先：yfinance 是非官方第三方歷史資料來源，不是元大或交易所的成交紀錄。任何來源、契約、時間邊界或完整性疑義都不得以推測補值，也不得在驗證完成前驅動交易訊號。**
