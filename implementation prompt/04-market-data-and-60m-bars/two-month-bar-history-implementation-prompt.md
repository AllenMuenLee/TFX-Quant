# Feature 04 Extension — 兩個月本機 60 分鐘 K 棒保存與查詢

> **唯一規格來源：實作本功能前，必須讀取專案根目錄 [`行情API元件及說明文件/`](../../行情API元件及說明文件/) 內的說明文件、元件及版本資訊。只有該資料夾明確記載的介面、欄位、代碼與行為可作為 API 依據。禁止使用交易 API 文件、SPARK、yfinance、其他行情商、網路資料或未記載的 API 能力。文件未記載之處不得猜測。**

> **不得回補：現有行情 API 文件只記載即時 snapshot／update 註冊與 `OnGetMktAll` 事件，未提供歷史價格或歷史 K 棒查詢。本功能的「兩個月歷史」只代表本機保存與查詢窗口，不代表啟用時即可取得過去兩個月。資料只能來自系統實際在線期間記錄的元大行情事件及其聚合 K 棒；不得從任何 API 或外部來源填補 recorder 啟用前或斷線期間的缺口。**

本 prompt 延伸同資料夾的即時行情與 60 分 K prompt，負責 closed bar 的持久化、兩個月滾動保存、啟動載入、查詢、缺口呈現及可用性判斷。

## 任務

將由元大即時行情 recorder 聚合且已確定收盤的 60 分 K 寫入本機 database，提供最近兩個曆月內「實際已記錄」資料的查詢。全新安裝從零開始累積；若運行不足兩個月，回傳現有範圍及明確的 coverage metadata，不得製造完整兩個月的假象。

## 唯一允許的資料血緣

```text
元大行情 API 即時 OnGetMktAll 事件
  → 本機 raw market-event recorder
  → 經確認的 60 分鐘聚合規則
  → 本機 closed-bar repository
  → 歷史查詢／UI／策略暖機
```

此圖中的行情來源須沿用主 prompt 定義的 sample-based Quote host：以真實 wx window handle 和 `AtlAxCreateControlEx` 建立 32-bit OCX，並由 host 持有 control、event sink 及 advise connection。vendor callback 的 COM 簽章是 `OnGetMktAll(this, Symbol, RefPri, OpenPri, HighPri, LowPri, UpPri, DnPri, MatchTime, MatchPri, MatchQty, TolMatchQty, BestBuyQty, BestBuyPri, BestSellQty, BestSellPri, FDBPri, FDBQty, FDSPri, FDSQty, ReqType)`；由於 host 向 `GetEvents` 明確提供 event interface，comtypes 會移除 `this`，Python sink 實作的入口從 `Symbol` 開始。不得改回 PDF 的省略版簽章，也不得在 sink 再加入 `this`。`ReqType=1` 為 T 盤、`ReqType=2` 為 T+1 盤，必須隨原始事件保存或保留在 session provenance 中，以避免跨盤資料混用。

歷史讀取只能查詢上述本機 repository。任何無法追溯到已保存 raw event／closed bar 的資料均不得進入正式歷史。

## 必須實作

- 「最近兩個月」定義為以 `Asia/Taipei` 查詢時間往前推兩個曆月的滾動窗口並包含起點；例如 8 月 18 日的起點為 6 月 18 日。這是 retention／query 邊界，不是 backfill 請求範圍。
- 每根 bar 保存商品與契約 identity、週期、交易日、session、`start`、`end`、OHLCV、完整性、來源事件範圍／序號、建立時間及更新 audit。provenance 必須明確標為本機元大即時行情紀錄，不得只寫籠統的 external 或 history API。
- 以商品、契約、週期及 `start` 建立唯一 identity 與 database unique constraint。寫入採有界 transaction 與 idempotent insert／upsert；commit 成功前不得加入可供 UI／策略使用的 recent-bars。
- 已收盤 bar 不得靜默覆寫。若遲到或亂序事件觸發修訂政策，須保存前後值、來源事件、原因與 audit；若無法安全修訂，維持原 bar 並標示衝突／不完整。
- 啟動、重連或切換契約時，先從本機 repository 載入窗口內資料，驗證 identity、排序、重複、bar 邊界、交易日／session 及 gap，再建立 recent-bars。不得因此發出任何外部歷史查詢。
- repository 查詢必須同時回傳 coverage：requested range、available range、最早／最晚 bar、完整 closed bar 數、gap／不完整區間、recorder 首次可用時間、最後即時接收時間及 stale 狀態。
- recorder 啟用前、程式停止、行情斷線、註冊失敗或資料被拒絕造成的區間，須保存或推導為 gap。gap 不得以 snapshot、前一根 close、零成交量 bar、插值或較大週期資料填補。
- `Snapshot`／`SnapshotUpd` 只能依行情文件作為當下最新資料及後續更新的註冊模式，不得用來重建缺失的過去 bar。
- 只有完整、已收盤且通過 canonical session／邊界驗證的 bar 可供策略暖機與紅黑 K 計數。資料量不足或窗口內存在策略所需區段的 gap 時，readiness 必須為 degraded／blocked。
- 新的即時事件持續按主 prompt 流程落盤與聚合；新 bar commit 後再更新查詢結果與 UI。歷史查詢功能本身不得成為第二個寫入來源。
- retention cleanup 只能刪除兩個月窗口之前且政策允許移除的 derived bar。若 raw event 另有稽核保存要求，須使用獨立設定；不得因 bar retention 未定義就連帶刪除 raw event。
- cleanup 使用 transaction，記錄 cutoff、候選數、刪除數與 audit ID。cleanup 失敗須 rollback，且不得影響既有查詢資料。

## 禁止事項

- 禁止實作 `history adapter`、`backfill workflow`、遠端缺口查詢或背景歷史下載。
- 禁止依賴 yfinance、Yahoo ticker、其他行情供應商、交易 API 回報、人工匯入檔案或網路服務補歷史。
- 禁止把 fixture、seed data、cache 或記憶體 DataFrame 當成 production 歷史來源。
- 禁止宣稱「兩個月完整」；除非 coverage 驗證證明整個所需交易區間都由本機即時紀錄覆蓋且沒有 gap。
- 禁止為通過測試、畫滿圖表或解除策略暖機而合成、複製或插值 OHLCV。

## 可觀測性與 UI

- 記錄 `bar_persist_requested/result`、bar identity、source event range、insert／duplicate／revision／conflict、transaction 結果及 queue depth。
- 記錄每次本機歷史查詢的 requested／available range、row count、gap count、完整性及 readiness；不得記錄完整敏感行情 payload。
- 記錄 retention cleanup 的 cutoff、候選數、刪除數、transaction 結果及 audit ID。
- UI 明確顯示「僅為本機即時紀錄」、資料起訖時間、實際 coverage、缺口、最後接收時間、stale 與 readiness。資料不足兩個月時顯示實際天數／bar 數，不得以空白或合成 bar 填滿。

## 驗收

- 全新 database 查詢兩個月窗口時回傳空資料與 `insufficient coverage`，且不產生任何外部請求。
- 以 fake clock 與本機 repository fixture 驗證兩個曆月邊界、月份長度、跨年、時區及 retention cutoff。
- 驗證 closed bar 必須先成功 commit 才可查詢；commit 失敗須 rollback，recent-bars 與 coverage 不得提前更新。
- 驗證程序重啟只從本機 database 恢復；相同 bar 重播不重複，修訂／衝突有 audit。
- 驗證 recorder 啟用前與模擬斷線期間保持 gap；重連後只能從新收到的即時事件繼續累積，gap 不會被 snapshot 或任何外部來源補齊。
- 驗證資料不足、stale、gap、契約 identity 不符或 bar 不完整時，UI 正確揭露且策略 readiness 維持 degraded／blocked。
- 驗證超出窗口資料依 retention policy 清理，窗口內 closed bars 與必要 audit 不受影響。
- 所有測試使用 fake event source 與本機 fixture，不連接真實帳號或任何外部行情／歷史服務。
