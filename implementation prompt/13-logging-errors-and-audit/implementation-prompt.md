# Feature 13 — Logging, Errors, and Audit Trail

> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；採用 Python 結構化 logging 與可追蹤 correlation context。

## 任務

建立結構化、可搜尋且不可混淆順序的執行紀錄，支援問題診斷、交易稽核與 UI 顯示，同時保護帳密與個資。

## 必須實作（依目前程式邊界）

- 使用 Python `logging` 輸出結構化 JSON event；每筆包含 UTC、台北時間、process-wide 單調序號、correlation ID、workflow ID 與呼叫端提供的結構化欄位。
- 提供 DEBUG／INFO／WARNING／ERROR／CRITICAL helper；無法 JSON 原生序列化的 domain value 以 `str()` 降級，logging 不得使業務流程崩潰。
- 帳號只保留末四碼，其餘遮罩；密碼及憑證只記錄是否存在，不記錄內容。敏感欄位仍由呼叫端在進入 telemetry API 前遮罩。
- UI 提供「查看所有日誌」按鈕及獨立視窗，顯示本次啟動後的日誌。使用一般單行時間／severity／來源／訊息格式、持續更新、可捲動，並以 10,000 筆有界記憶體緩衝保存。
- 使用 SQLite `audit_events` 保存結構化事件。反手 workflow 的生命週期事件標記為 critical audit；critical audit 寫入失敗時輸出獨立 stderr 告警，並透過既有 `attempt_safe_pause()` 將策略轉為 `PAUSED_SAFE`（若狀態只能 fault，則轉為 `FAULTED`）。普通診斷事件寫入失敗不得觸發策略暫停。
- 提供受控診斷模式，只能指定一個 workflow ID 或 order ID，提高該目標的 DEBUG 詳細度；每次啟用必須同時具有自動到期時間及最大事件筆數，任一限制到達後立即失效，不改變全域 logger level。
- 提供反手 workflow 匯出：以 workflow ID 查詢 `audit_events`，按全域單調序號輸出 UTF-8 JSON Lines。匯出應包含相同 workflow ID 的反手啟動、起始持倉查詢、平倉委託、委託回報／成交、零持倉確認、反向建倉與完成事件；查無資料時明確失敗，不產生空白匯出。

## 明確不在本 Feature 範圍

- rolling file、非同步 queue、batch、sampling/drop metrics、保存期限與 retention cleanup。
- 強制 event-name catalog、事件分類 enum、payload schema/version 驗證及遞迴 sink-side redaction。
- 全域例外 hook、繁中錯誤轉譯，以及 stack trace 的自動敏感資料清洗。
- 通用 audit UI 或匯出 UI；本 Feature 僅提供可由 application/UI 呼叫的反手 JSONL 匯出函式。

## 驗收

測試 UTC／台北時間、單調序號、correlation scope、UI 單行 formatter、帳號遮罩、critical 與 non-critical audit 寫入失敗的不同結果、診斷模式到期與容量上限，以及反手 workflow JSONL 匯出的 workflow 隔離與序號順序。完整反手案例應可追到反手啟動／trigger key、起始持倉、平倉委託與成交、零持倉確認、反向建倉及完成；目前不要求直接嵌入原始 K 棒 payload。
