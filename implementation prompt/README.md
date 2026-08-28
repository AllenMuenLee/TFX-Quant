# 元大期貨自動交易系統：Implementation Prompts

本目錄把客戶需求拆成可獨立開發與驗收的 feature。每個 prompt 都是可直接交給實作者或 coding agent 的工作指令。所有元大期貨交易 API 功能一律以專案根目錄的 [`交易API元件及說明文件/`](../交易API元件及說明文件/) 及其內的 API 說明、Python 範例、元件、版本資訊為唯一規格來源；不得再以 SPARK API 網站、舊 SPARK SDK、既有 SPARK 程式碼或記憶推導新介面。若資料夾不存在、內容不完整或互相矛盾，必須停止該 API 實作並列出待確認項目。實作時須記錄文件檔名、版本與查閱日期。

所有市場價格、即時／延遲行情、OHLCV 與歷史 K 棒一律透過 `yfinance` adapter 取得；元大期貨 API 僅用於登入、帳戶、委託、成交、持倉及交易查詢，不得用來訂閱或補齊市場價格。不得混用 SPARK、元大行情 callback、其他行情商、合成 tick 或手工價格作為 fallback。yfinance 缺值、延遲、斷線或 ticker 無法可靠映射時保留 gap、進入 degraded／安全暫停，禁止猜值或以交易回報價格冒充行情。

## 強制技術要求：Python

- 整套系統必須使用 Python 開發；正式支援的 Python 版本需固定並記錄於專案設定，建議選用仍在安全維護期且與元大 API 相容的版本。
- Windows 桌面 UI、策略、資料庫、報表、測試、安裝與元大 API adapter 均以 Python 為主，不得改以 C#、Java 或其他語言重新實作核心系統。
- 若元大正式 API 僅能透過 COM/ActiveX、原生 DLL 或特定位元數執行，應使用 Python 相容的封裝方式隔離於 adapter；若確實需要極小型非 Python bridge，必須先記錄理由並取得客戶同意，核心交易與安全邏輯仍須保留在 Python。
- 使用 `pyproject.toml` 管理套件、工具與固定版本，提供可重現的虛擬環境、lint、型別檢查及 pytest 測試指令。

## 全域不可妥協規則

- 最大淨持倉為 2 口，交易商品固定且只允許小台指（MXF）；UI 的小台／大台切換只改變市場行情監看，不得改變委託商品。
- 反手必須先收到原部位完全平倉的確定成交回報，再建立反向 1 口。
- 部分成交、未成交逾時、拒單、斷線、委託狀態不明或持倉不一致時，進入安全暫停並等待人工確認。
- 狀態不明的委託不得自動重送；所有送單必須具備冪等識別與可追蹤性。
- 系統自動比較本機 expected position 與元大線上持倉；查詢失敗或偵測手機 App／其他來源造成的異動時自動安全暫停。UI 不提供人工重新查詢或人工同步按鈕。
- 04:55 前執行收盤平倉流程，不留倉。08:45、09:45 不建立新倉，日盤最早 10:45 才能依 60 分 K 判斷建倉。
- 金額、時間、契約、買賣別、口數及損益都需採明確型別；禁止用浮點數保存金額。

## 建議實作順序

1. `00-spark-to-futures-api-migration`
2. `01-solution-foundation`
3. `02-yuanta-api-session`、`03-instrument-contract-selection`
4. `04-market-data-and-60m-bars`
5. `06-order-and-fill-state-machine`
6. `05-strategy-signal-engine`、`07-safe-reversal-and-scaling`
7. `08-position-reconciliation`、`09-connectivity-and-safe-pause`
8. `10-risk-eod-and-emergency-flatten`
9. `14-persistence-and-recovery`、`13-logging-errors-and-audit`
10. `11-pnl-and-trade-reports`、`12-windows-desktop-ui`
11. `15-simulation-and-automated-tests`
12. `16-installer-and-documentation`

## Feature 索引

| 編號 | Feature | 核心產出 |
|---|---|---|
| 00 | SPARK-to-Futures API migration | 移除舊 SPARK 設定並建立期貨交易 API 與 yfinance 設定基線 |
| 01 | Solution foundation | Windows 技術棧、領域模型、模組界線與設定 |
| 02 | Yuanta API session | 登入、憑證、帳號、API 執行緒與登出 |
| 03 | Instrument selection | 小台／大台行情切換與自動近月顯示；交易固定 MXF |
| 04 | Market data and bars | yfinance 行情、60 分 K 正規化與交易時段 |
| 05 | Strategy signal engine | 連續紅黑 K、加碼與進場時段規則 |
| 06 | Order/fill state machine | 委託、成交、部分成交、拒單與冪等防護 |
| 07 | Safe reversal/scaling | 完全平倉後反手、最多 2 口 |
| 08 | Position reconciliation | 自動持倉核對、查詢／差異異常安全暫停 |
| 09 | Connectivity/safe pause | 斷線、重連、狀態不明與安全暫停 |
| 10 | Risk/EOD/emergency | 04:55 平倉、緊急平倉與風險閘門 |
| 11 | P&L/reports | 每日、每月實際成交損益與明細 |
| 12 | Windows UI | 啟動、停止、暫停、監控、緊急操作與一般文字日誌視窗 |
| 13 | Logging/audit | 錯誤、交易紀錄與可稽核事件鏈 |
| 14 | Persistence/recovery | 重啟恢復、快照與未決委託保守處理 |
| 15 | Simulation/tests | 模擬 API、歷史回放、故障注入與驗收測試 |
| 16 | Installer/docs | Windows 安裝檔、原始碼交付與操作說明 |
