# Feature 14 — Persistence and Crash Recovery

> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；資料存取、migration 與 recovery coordinator 均須為 Python 實作。

## 任務

讓程式在崩潰、斷電或更新後以保守方式恢復。持久化的目的不是自動延續下單，而是找回 workflow、查明券商真實狀態並阻止重複委託。

## 必須實作

- 使用本機可靠資料庫（例如 SQLite，依技術棧決定）保存 schema version、設定版本、bar ID、策略快照、order intents、委託／成交、position baseline、workflow、損益台帳與 audit events。
- 對「先記錄 intent、再呼叫券商」提供 transaction/outbox 語意；提交前崩潰和提交後未收到回報必須可區分，但兩者都不能盲目重送。
- 啟動一律進入非交易 recovery mode：登入、查活動委託／成交／持倉，與本地資料關聯，再產生 reconciliation report。
- 任何未完成 workflow、提交中 intent、未知委託、持倉差異或資料庫損壞都保持安全暫停並要求人工確認。
- 只有無未知狀態且同步完成後才可建立新 baseline；清除策略訊號狀態並由使用者重新啟動，不追補離線期間訊號。
- 實作 migration、備份與完整性檢查。不得在未備份下破壞性升級；資料庫含敏感資料時採 Windows 存取控制／適當加密。

## 除錯日誌需求

- 資料庫啟動須記錄路徑遮罩、schema/migration version、完整性檢查、備份 ID、transaction 耗時及失敗；不得輸出資料庫內敏感內容。
- intent/outbox 寫入、broker call 邊界與狀態提交須記錄 transaction/outbox ID、冪等鍵、commit 結果及 crash-recovery checkpoint，使提交前後故障可區分。
- recovery workflow 記錄未完成 workflow／intent、券商委託成交持倉查詢、關聯結果、差異、未知項目與人工解鎖 gate；啟動期間任何「未自動重送」決策亦須留 audit。

## 驗收

在送單前、API 呼叫期間、ack 後、部分成交後、全成後及反手 flat gate 前後逐點模擬 crash。重新啟動時驗證不會新增委託、能呈現人工所需證據，且只有查詢一致後才能解鎖。加入 migration rollback/backup 與資料庫損壞測試。
