# Feature 13 — Logging, Errors, and Audit Trail

> **平台硬性限制：整個系統只能使用 x32；不得使用 x86 或 x64。原因是行情 API 僅提供 x32 版本。所有開發、相依套件、執行環境、測試、建置與部署均須遵守此限制。**

> 強制使用 Python 開發；採用 Python 結構化 logging 與可追蹤 correlation context。

## 任務

建立結構化、可搜尋且不可混淆順序的執行紀錄，支援問題診斷、交易稽核與 UI 顯示，同時保護帳密與個資。

## 必須實作

- 定義事件分類：session、market data、bar、strategy decision、risk decision、order intent、broker request、order report、fill、position query、reconciliation、user action、system/error。
- 每筆事件含 UTC 與台北時間、單調序號、severity、correlation/workflow ID、策略狀態、商品契約及結構化 payload；保存原始券商錯誤碼但遮蔽敏感資料。
- 策略每次「有動作或無動作」都記錄輸入 bar ID、持倉、規則結果及阻擋原因，讓事後能重現決策。
- 使用 rolling files 與資料庫 audit table；設定保存期限、容量上限與匯出功能。log 寫入失敗需告警，關鍵交易 audit 無法持久化時安全暫停。
- 全域例外處理不得吞錯；將技術錯誤轉為可行動的繁中訊息，同時保留 stack trace 供支援人員查閱。
- UI 顯示目前錯誤與歷史紀錄，可依時間、severity、商品、order/workflow ID 篩選。
- 記錄使用者的啟動、暫停、停止、同步、緊急平倉及確認內容，但不記錄密碼／憑證／完整帳號。

## 驗收

測試高頻行情下不阻塞交易佇列、檔案輪替、磁碟滿、資料庫鎖定、敏感欄位遮蔽、跨 thread 事件排序及 correlation chain。從一個反手案例的匯出紀錄應能完整追到 K 棒、策略意圖、平倉、零持倉確認與反向建倉。
