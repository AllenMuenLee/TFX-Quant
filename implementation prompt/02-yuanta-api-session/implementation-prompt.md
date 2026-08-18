# Feature 02 — Yuanta Futures API Session

> **平台硬性限制：整個系統只能使用 x32；不得使用 x86 或 x64。原因是行情 API 僅提供 x32 版本。所有開發、相依套件、執行環境、測試、建置與部署均須遵守此限制。**

> 強制使用 Python 開發；元大 COM/ActiveX/DLL 整合須封裝成具型別介面的 Python adapter。

## 任務

依使用者提供且有權使用的元大期貨正式 API、SDK、憑證與範例，實作登入、帳號選擇、連線生命週期與登出 adapter。不要猜測 DLL、ProgID、事件名稱、回傳碼或欄位；將實際版本與限制記錄在文件中。

## 必須實作

- 啟動前檢查 API 安裝、DLL/COM 註冊、位元數、版本、憑證與必要權限，回報可操作的中文錯誤。
- 支援登入事件、逾時、失敗、重複登入、登出、被動斷線及 session 失效。
- 列出可用期貨帳號，使用明確設定或讓 UI 選擇；若找不到唯一目標帳號，不得啟動策略。
- API 呼叫若要求 STA/UI thread，建立專用 dispatcher 並保證 callback 不阻塞；callback 內容立即複製為內部 DTO。
- 提供 capability/readiness 狀態：登入、行情、交易、回報、查詢各自獨立，不把「已登入」等同「可交易」。
- 登入後依序完成委託查詢、成交查詢、持倉查詢和行情訂閱，全部成功才發出 session-ready 事件。
- 帳密與憑證使用 Windows Credential Manager、DPAPI 或 SDK 官方機制；log 必須遮蔽帳號與秘密。

## 失敗處理

登入或初始化失敗時不得重複狂試；採有上限退避並允許取消。交易中 session 異常立即通知安全暫停模組。關閉程式時先停止策略與完成必要查詢，再取消訂閱及登出，不得在未知委託仍可能有效時宣稱安全關閉。

## 介面與驗收

定義 `IBrokerSession` 與事件 DTO，使其可由模擬 adapter 取代。加入針對成功、逾時、錯誤碼、重複 callback、callback 亂序及中途斷線的測試。以 staging/mock 提供登入 smoke test；真實帳號測試必須是明確 opt-in，且預設禁止送單。
