# 元大期貨自動交易系統：Implementation Prompts

本目錄把客戶需求拆成 16 個可獨立開發與驗收的 feature。每個 prompt 都是可直接交給實作者或 coding agent 的工作指令。所有元大 API 功能一律以[元大 SPARK API 官方入口](https://www.yuanta.com.tw/file-repository/content/API/page/index.html)及其下方連結的 API 說明文件、範例、元件下載與換版資訊為唯一規格來源；不得讀取專案內既有資料夾、舊 SDK 文件、舊範例或既有程式碼來建立或反推 API 功能。實作時須記錄查閱日期與元件版本，且不得憑空假設介面。

## 強制技術要求：Python

- 整套系統必須使用 Python 開發；正式支援的 Python 版本需固定並記錄於專案設定，建議選用仍在安全維護期且與元大 API 相容的版本。
- Windows 桌面 UI、策略、資料庫、報表、測試、安裝與元大 API adapter 均以 Python 為主，不得改以 C#、Java 或其他語言重新實作核心系統。
- 若元大正式 API 僅能透過 COM/ActiveX、原生 DLL 或特定位元數執行，應使用 Python 相容的封裝方式隔離於 adapter；若確實需要極小型非 Python bridge，必須先記錄理由並取得客戶同意，核心交易與安全邏輯仍須保留在 Python。
- 使用 `pyproject.toml` 管理套件、工具與固定版本，提供可重現的虛擬環境、lint、型別檢查及 pytest 測試指令。

## 全域不可妥協規則

- 最大淨持倉為 2 口，且只允許小台指或大台指其中一個目前選定商品。
- 反手必須先收到原部位完全平倉的確定成交回報，再建立反向 1 口。
- 部分成交、未成交逾時、拒單、斷線、委託狀態不明或持倉不一致時，進入安全暫停並等待人工確認。
- 狀態不明的委託不得自動重送；所有送單必須具備冪等識別與可追蹤性。
- 偵測手機 App 或其他來源造成的實際持倉異動時，自動暫停。人工同步後以元大實際持倉為基準，並重置策略判斷狀態。
- 04:55 前執行收盤平倉流程，不留倉。08:45、09:45 不建立新倉，日盤最早 10:45 才能依 60 分 K 判斷建倉。
- 金額、時間、契約、買賣別、口數及損益都需採明確型別；禁止用浮點數保存金額。

## 建議實作順序

1. `01-solution-foundation`
2. `02-yuanta-api-session`、`03-instrument-contract-selection`
3. `04-market-data-and-60m-bars`
4. `06-order-and-fill-state-machine`
5. `05-strategy-signal-engine`、`07-safe-reversal-and-scaling`
6. `08-position-reconciliation`、`09-connectivity-and-safe-pause`
7. `10-risk-eod-and-emergency-flatten`
8. `14-persistence-and-recovery`、`13-logging-errors-and-audit`
9. `11-pnl-and-trade-reports`、`12-windows-desktop-ui`
10. `15-simulation-and-automated-tests`
11. `16-installer-and-documentation`

## Feature 索引

| 編號 | Feature | 核心產出 |
|---|---|---|
| 01 | Solution foundation | Windows 技術棧、領域模型、模組界線與設定 |
| 02 | Yuanta API session | 登入、憑證、帳號、API 執行緒與登出 |
| 03 | Instrument selection | 小台／大台與契約月份選擇及切換保護 |
| 04 | Market data and bars | 行情訂閱、60 分 K 聚合與交易時段 |
| 05 | Strategy signal engine | 連續紅黑 K、加碼與進場時段規則 |
| 06 | Order/fill state machine | 委託、成交、部分成交、拒單與冪等防護 |
| 07 | Safe reversal/scaling | 完全平倉後反手、最多 2 口 |
| 08 | Position reconciliation | 實際持倉核對、外部交易偵測、人工同步 |
| 09 | Connectivity/safe pause | 斷線、重連、狀態不明與安全暫停 |
| 10 | Risk/EOD/emergency | 04:55 平倉、緊急平倉與風險閘門 |
| 11 | P&L/reports | 每日、每月實際成交損益與明細 |
| 12 | Windows UI | 啟動、停止、暫停、監控與人工操作 |
| 13 | Logging/audit | 錯誤、交易紀錄與可稽核事件鏈 |
| 14 | Persistence/recovery | 重啟恢復、快照與未決委託保守處理 |
| 15 | Simulation/tests | 模擬 API、歷史回放、故障注入與驗收測試 |
| 16 | Installer/docs | Windows 安裝檔、原始碼交付與操作說明 |
