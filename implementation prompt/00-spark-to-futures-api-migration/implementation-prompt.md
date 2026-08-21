# Feature 00 — 從 SPARK API 遷移至元大期貨交易 API

> **本步驟必須先於 Feature 01–16 執行。API 規格唯一來源是專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/)；禁止再查用 SPARK API 網站、舊 SPARK SDK 或既有 SPARK adapter 反推新 API。資料夾缺少或文件未明載的項目不得猜測，須列入 migration report 的 blocker。**

## 目標

完整盤點並移除 SPARK API 遺留，依本機文件建立元大期貨交易 API 的最小、安全設定。市場價格與所有 OHLCV 一律改由 `yfinance` 提供；期貨 API 僅負責登入、帳戶、委託、成交、持倉與交易查詢。先產出差異清單，再修改程式、設定、測試與文件；不得只改名稱而保留舊 SPARK 常數或行為。

## 必須刪除

- 所有 SPARK 套件、COM/ActiveX、DLL、ProgID、CLSID、registry、host、port、endpoint、`ReqType`、`SetMap`、行情 server、行情登入、行情訂閱、tick callback、heartbeat 與重連設定。
- SPARK 專用帳號欄位、憑證 key、環境變數、`.env` 範例、設定 schema、UI 欄位、installer prerequisite、版本檢查及啟動參數；秘密只能刪除設定鍵或安全儲存項目，不得把其值輸出到 log 或 migration report。
- 舊 SPARK client／adapter／DTO／event binding、mock、fixture、測試、文件與依賴中已無用途的部分。刪除前逐一確認沒有仍被交易流程引用；不可留下會被誤選的 dead fallback。
- 任何從元大／SPARK API 收市場價格、tick、bid/ask、OHLCV 或歷史 K 棒的路徑，以及交易回報價推導行情、無資料時合成價格的邏輯。

## 必須新增或重新確認

- 逐項閱讀 `交易API元件及說明文件/`，記錄文件檔名、版本、雜湊／修改日期、支援的 Python 與 Windows 位元數、安裝／註冊方式，以及登入、帳戶、委託、成交、持倉、查詢所需的實際類別、方法、事件、欄位和錯誤碼。
- 只建立文件明載的期貨交易 API 設定。至少涵蓋：執行環境（正式／測試，若文件支援）、元件路徑／版本、帳號選擇、登入憑證的安全儲存 reference、timeout、有限重試、callback dispatcher、交易／回報／查詢 readiness；實際設定鍵名稱須依文件與專案 schema 決定，不得照抄舊 SPARK 名稱。
- 將 readiness 拆成 broker trading、order reports、queries 與 yfinance market data。不得再有 SPARK market-data readiness；任一必要能力失效即安全暫停，恢復後須 reconciliation 並由使用者重新啟動。
- 更新 dependency injection、設定驗證、redaction、installer、README、ADR、UI 文案及自動化測試。設定 migration 必須 fail closed：不接受舊 key、不靜默套預設、不自動把舊秘密複製到新 key。

## 執行順序

1. 建立 inventory：列出所有檔案、依賴、設定鍵、環境變數、Windows 元件與 runtime object 中的 SPARK／行情耦合。
2. 依本機文件建立 old-to-new matrix，標示 `delete`、`replace`、`retain`、`blocked` 及證據來源；未確認前禁止連線或送單。
3. 先建立新 ports/adapters 與設定 schema，再切換 composition root；接著移除舊實作與依賴，避免半套雙路徑。
4. 將全部市場價格 consumer 改接單一 yfinance adapter，包含策略、forming／closed bars、UI、staleness、回補、replay 與測試；禁止 broker-price fallback。
5. 執行靜態搜尋、設定驗證、單元／整合測試與 opt-in smoke test；真實 API smoke test 預設跳過且禁止送單。

## 驗收

- 全專案搜尋不再出現有效的 SPARK import、類別、ProgID／CLSID、endpoint、行情設定或 runtime 分支；若名稱僅存在 migration 文件，須有清楚註記。
- 舊 SPARK 設定啟動時會回報已廢止並拒絕啟動，不會被忽略或轉用；log 不含帳密、token、完整帳號或憑證內容。
- 使用 mock 期貨交易 API 可完成登入、帳戶選擇、委託／成交／持倉查詢；使用 mock yfinance 可完成市場價格取得、正規化、持久化與 stale／gap 安全暫停。
- 斷開 yfinance 時不得從期貨 API 或交易回報補價格；斷開交易 API 時不得因 yfinance 正常而視為可交易。
- 交付 `migration-report.md`，包含刪除項、建立項、old-to-new matrix、未決 blocker、文件證據、測試結果、人工安裝／憑證清理步驟與 rollback 方法。
