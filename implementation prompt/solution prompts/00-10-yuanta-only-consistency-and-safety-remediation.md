# Solution Prompt — Feature 00–10 元大單一行情來源與安全缺口修復

> 本 solution prompt 用於一次修復 Feature 00–10 implementation prompts 與目前程式碼之間的規格衝突、安全缺口、UI 缺漏、文件缺漏及靜態檢查失敗。必須依下列順序執行，先統一規格，再修改程式與測試；不得只讓測試通過而保留違反規格的 production path。

## 最高優先級決策

### 唯一行情來源

- 全專案的即時價格、成交行情、forming bar、closed bar、OHLCV、策略暖機資料、staleness、gap、replay 及圖表資料，唯一允許來源為專案根目錄 `行情API元件及說明文件/` 明載的元大行情 API，以及由該 API 實際收到並保存於本機的資料。
- 所有 Feature 00–16 implementation prompts 都必須明確禁止 `yfinance`、Yahoo Finance、其他行情商、網路歷史服務、交易 API 回報推導價格，以及任何 broker-price fallback。
- 元大交易 API 只負責登入、帳戶、委託、成交、持倉與交易查詢；元大行情 API 只負責文件明載的行情登入、商品註冊與即時行情事件。兩者 readiness、session、錯誤與憑證生命週期必須分開建模。
- 歷史 K 棒只能來自本機已持久化的元大即時行情事件及其聚合結果。全新安裝從零開始累積；程式啟用前或中斷期間的資料不得回補、合成、插值或由外部來源取得。
- `Snapshot`／`SnapshotUpd` 只能依官方文件解讀為當下資料及後續更新的註冊模式，不得宣稱為歷史查詢或用於填補過去缺口。
- 若本 prompt 與既有 implementation prompt、ADR、README、註解或測試衝突，以本 prompt 的「元大單一行情來源」決策為準；但實際元大 API 類別、方法、參數、事件、欄位、錯誤碼、位元數及環境仍只能取自對應的本機官方文件，不得猜測。

## 階段 1 — 先修正所有 implementation prompts

逐一修改 `implementation prompt/` 下 Feature 00–16 的主 prompt 與所有 extension prompt：

1. 搜尋並移除所有要求、允許或暗示使用 `yfinance`、Yahoo ticker、Yahoo mapping、Yahoo backfill、外部歷史行情或第三方行情 fallback 的文字。
2. 將市場資料規則統一改為：
   - API 唯一規格來源為 `行情API元件及說明文件/`。
   - production market data 唯一來源為元大行情 API 實際收到的事件。
   - 歷史資料唯一來源為本機 recorder 與 closed-bar repository。
   - recorder 啟用前與斷線期間的缺口永久保留為 gap，除非未來另有經核准且文件明載的新規格。
3. 保留 `交易API元件及說明文件/` 作為交易 session、帳戶、委託、成交、持倉及查詢的唯一規格來源，不得混用行情文件推測交易 API。
4. 修正 Feature 00、01、02、03、05–16 中所有「行情來自 yfinance」及 `yfinance market-data readiness` 描述，改成獨立的 Yuanta quote readiness。
5. 修正 Feature 03 的商品切換流程：停止舊元大行情註冊、驗證新 EASYWIN／官方行情代碼、註冊新商品、恢復本機歷史、清空形成中 K 棒與訊號狀態；不得提及 Yahoo ticker mapping。
6. 修正 Feature 04 標題、主文及 extension，使它們成為全專案行情資料血緣的權威規格。
7. 更新 `implementation prompt/README.md`，明確記錄此全域決策及 prompt precedence，避免 Feature 之間再次產生相反要求。

完成後執行大小寫不敏感搜尋。除本 prompt、migration report 中有清楚「已禁止／已刪除」語意的稽核記錄外，所有 implementation prompts 不得再出現 `yfinance`、`Yahoo Finance`、`Yahoo ticker` 或外部歷史回補要求。

## 階段 2 — 移除被禁止的 production 行情路徑

- 刪除 production code 中的 yfinance／Yahoo ports、adapters、ticker mapping、polling、history query、backfill workflow、backfill conflict schema、設定、依賴、composition wiring、UI 文案及 runtime branch。
- 至少盤點並處理目前的：
  - `application/market_data/bar_service.py`
  - `application/market_data/bar_history_backfill_service.py`
  - `application/market_data/yahoo_bar_resolution.py`
  - `application/ports/yahoo_history_query.py`
  - `application/ports/yahoo_ticker_mapping.py`
  - `infrastructure/market_data/yfinance_history_adapter.py`
  - `infrastructure/market_data/mock_yahoo_history_query.py`
  - Yahoo ticker mapping JSON 與 repository
  - `domain/bar_history_backfill.py`
  - `bar_backfill_conflicts` 相關 schema、repository API 與 audit
- 逐一確認沒有 consumer 後才移除；不得留下可被 composition、設定或 import 誤選的 dead fallback。
- 將仍有價值的通用 bar identity、coverage、retention、revision audit 與 gap 模型改為元大本機資料血緣，不得只改類別名稱而保留遠端回補行為。
- 刪除或重寫所有以 yfinance 行為為正確結果的測試。新測試只能使用 fake 元大 COM event source、本機 repository fixture 與可控 clock，不得連接真實帳號或外部行情服務。
- 從 `pyproject.toml`、lockfile、installer、README、CI 與範例設定移除無用途的 yfinance／pandas 相依與設定。

## 階段 3 — 完成 production broker 查詢，禁止 fail-open

### 活動委託

- `TradeGatewayPort.query_open_orders()` 不得固定回傳空集合，也不得將未解析、查詢失敗、timeout、缺欄位或 callback 尚未完成解讀成「沒有活動委託」。
- 依 `交易API元件及說明文件/` 實作實際委託查詢與 typed mapping；保存券商單號、商品／完整契約、方向、開平、數量、累計成交量、狀態與時間。
- 查詢必須能區分「官方確認為零筆」與「未知／未完成／解析失敗」。後者必須使 queries/order-reports readiness 為 false、阻止 session-ready 或進入 `PausedSafe`，且禁止任何新單、同步、反手、加碼或 flatten 的下一步。

### 實際持倉

- 依官方文件完成非零持倉 callback 的 typed parsing，不得因 `row_count != 0` 就將 session 判定為登入失敗，也不得只支援零持倉。
- 僅比較已選交易帳號與完整契約；其他契約持倉仍須保留並顯示高優先警示。
- 非零列無法唯一映射到受控商品主檔時必須 fail closed，保留原始欄位的安全摘要與 blocker，不得清空 cache 或假裝為零。

### Session-ready

- `BrokerSessionReady` 只能在登入、明確帳號選擇、交易、委託回報、委託查詢、成交查詢、持倉查詢及獨立元大行情 readiness 全部成功後發出。
- 每項查詢需以 query/workflow ID 關聯 request、callback、完成狀態與 snapshot；舊 snapshot 不得被當成新查詢結果。
- 加入零筆、一筆、多筆、部分 callback、重複、亂序、timeout、缺欄位及解析失敗測試，證明任何不確定狀態都不會 session-ready。

## 階段 4 — 完成 Feature 03 商品與契約 UI

- UI 必須同時支援小台指／大台指與明確契約年、月選擇，並顯示中文商品名、交易 API 商品代碼、行情 API 商品代碼及完整契約。
- 契約一律自動解析最近可交易月份，UI 只顯示 `YYYY-MM`，不得提供手動契約模式或契約選單。
- 行情商品切換只改變元大行情註冊與圖表 identity；所有交易委託固定為小台指（MXF），OrderManager 必須拒絕非 MXF 委託。
- 切換必須依序完成：安全暫停、取消舊行情註冊、驗證主檔與行情代碼、載入新契約本機歷史、註冊新行情及清空 forming bar／連續 K／訊號狀態。
- 策略執行中必須拒絕行情切換；持倉或活動／未知委託不得被行情切換改寫或重新歸屬。

## 階段 5 — 完成 Feature 08 自動 reconciliation UI

- 在登入、成交、反手 flat gate、重連、定時輪詢與回到前景時自動查詢，並比較本機 expected position 與元大線上實際持倉。
- desktop UI 只顯示遮罩帳號、MXF 完整契約、本機紀錄、線上紀錄、snapshot 時間與一致／異常狀態，不提供「重新查詢」或「確認同步」按鈕。
- 查詢失敗、方向／口數差異或其他契約非平倉部位立即進入安全暫停；不得自動覆寫 expected baseline，也不得送單修正差異。

## 階段 6 — 完成 Feature 10 緊急平倉 UI 與安全 workflow

- 提供清楚且高優先級的「緊急平倉」按鈕。
- 按鈕必須先停止新訊號並進入安全暫停，再重新查詢活動／未知委託及實際持倉，顯示並要求確認帳號遮罩、商品、完整契約與實際口數。
- 確認只能綁定最新 query snapshot；持倉或委託狀態改變時必須重新確認。
- UI 必須呼叫既有的安全 emergency-flatten application workflow，不得直接呼叫 broker API、一般 reversal 或自行組合雙倍數量反手單。
- 部分成交、拒單、斷線、timeout、未知狀態或最終持倉非零時持續顯示高優先警報；只有新的券商查詢精確確認為零才可顯示完成。
- 程式在 04:55 後啟動且發現持倉時不得自動平倉；保持 `PausedSafe`，明確提示使用者操作緊急平倉按鈕。

## 階段 7 — 完整呈現本機行情 coverage

- market-data UI 必須顯示：
  - 「僅為本機元大即時行情紀錄」；
  - recorder 首次可用時間；
  - requested 與 available range；
  - 最早／最晚 closed bar；
  - complete bar 數量；
  - gap／不完整區間；
  - 最後行情事件時間；
  - stale、coverage 與 strategy readiness。
- 即時畫面需區分 forming bar 與 closed bars，並顯示行情連線及商品註冊狀態。
- 空資料、運行未滿兩個月或存在 gap 時不得只顯示 bar 數，也不得暗示歷史完整。
- coverage metadata 必須直接來自 local history service/repository 的一致 snapshot，不得由 UI 猜測。

## 階段 8 — 文件與 migration deliverables

- 建立 `docs/adr/` 並補齊 README 已連結的 ADR，或同步修正所有無效連結。ADR 至少記錄：
  - Python／Windows 位元數與元件選擇；
  - 交易 API adapter 與 callback dispatcher；
  - 元大行情 API adapter 與本機-only歷史資料血緣；
  - session readiness；
  - order/fill state machine；
  - position reconciliation；
  - reconnect/safe pause；
  - 04:55 與 emergency flatten。
- 建立 `migration-report.md`，包含：
  - 刪除的 yfinance／Yahoo／外部 backfill 項目；
  - 移除的舊 SPARK runtime、設定及 fallback；
  - 元大交易與行情元件的本機文件檔名、版本、雜湊或修改日期及查閱日期；
  - old-to-new matrix；
  - 未決 blocker；
  - 設定與資料庫 migration；
  - 測試結果；
  - 人工安裝、元件註冊、憑證清理與 rollback 步驟。
- 更新 README、範例設定、installer 與 UI 文案，確保不再宣稱或暗示 yfinance、Yahoo 或可外部回補兩個月歷史。

## 階段 9 — 測試與靜態驗收

### 必須新增的安全測試

- 活動委託查詢非空、零筆、timeout、callback 不完整、重複、亂序及解析失敗。
- 非零持倉、零持倉、多契約持倉、未知商品、缺欄位及查詢暫時失敗。
- 上述任何未知狀態都不得進入 session-ready、不得自動送單、不得完成 reconciliation 或宣稱 flatten 完成。
- 自動近月只讀顯示、行情商品切換不改寫持倉／委託，以及非 MXF 委託被拒絕。
- 自動持倉核對唯讀 UI，包含查詢失敗、差異與其他契約持倉的安全暫停。
- 緊急平倉 UI 的確認、部分成交、拒單、晚到成交、斷線及最終零持倉查詢。
- 全新資料庫、本機資料不足兩個月、斷線 gap、重啟、跨午夜、retention、revision audit 與 stale/readiness 顯示。
- 靜態測試證明 production import graph、composition、設定及依賴中不存在 yfinance／Yahoo／外部 history/backfill path。

### 必須通過的命令

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy src
.venv\Scripts\lint-imports.exe
```

所有命令必須成功。不得用全域 `ignore_missing_imports`、跳過 production modules、刪除安全測試或降低型別規則來掩蓋錯誤。

## 最終靜態稽核

完成後對 prompts、source、tests、設定、文件與 dependency metadata 執行大小寫不敏感搜尋，至少涵蓋：

```text
yfinance
Yahoo Finance
Yahoo ticker
yahoo_history
backfill
SPARK
legacy fallback
return ()
NotImplementedError
```

- `yfinance`／Yahoo／外部 backfill 不得存在於任何有效 production path、測試期待、設定或依賴；只允許出現在明確記錄「已禁止／已移除」的 migration audit 文件。
- SPARK 名稱只允許存在於 migration history 的刪除紀錄，不得存在有效 import、class、設定、endpoint、ProgID、CLSID 或 runtime branch。
- `return ()`、`NotImplementedError` 必須逐一人工審查；任何委託、成交、持倉或 readiness 查詢不得以此假裝安全結果。
- 所有日誌與錯誤仍須通過敏感資料遮蔽，不得輸出登入 ID 明文、完整帳號、密碼、憑證或完整原始 callback payload。

## 完成定義

只有同時滿足以下條件才可宣告完成：

1. Feature 00–16 prompts 對行情來源的描述完全一致，全部禁止 yfinance 與外部歷史回補。
2. production code 只有元大行情 API → raw event recorder → 60 分鐘聚合 → local closed-bar repository 這一條行情資料血緣。
3. production broker 能可靠查詢並解析實際活動委託、成交與非零持倉；任何未知狀態均 fail closed。
4. session-ready、反手、加碼、reconciliation、04:55 與 emergency flatten 不再依賴空集合或零持倉假設。
5. 自動契約年月顯示、自動持倉核對、緊急平倉及一般文字日誌視窗均由 desktop UI 完整呈現，且交易固定 MXF。
6. UI 如實揭露本機歷史 coverage、gap、stale 與 readiness。
7. ADR、migration report、README、設定及 installer 與實作一致。
8. 全部測試與靜態檢查通過，且沒有真實帳號送單或外部行情依賴。
