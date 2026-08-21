# Feature 02 — Yuanta Futures API Session

> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；依 `交易API元件及說明文件/` 取得的元大期貨交易 Python 元件須封裝成具型別介面的 adapter。

## 任務

依 `交易API元件及說明文件/` 實作元大期貨交易 API 的登入、帳號選擇、連線生命週期與登出 adapter。不要猜測套件、類別、方法、事件名稱、回傳碼或欄位；將文件檔名、查閱日期、實際元件版本與限制記錄在文件中。此 session 不登入或訂閱券商行情；市場資料 readiness 來自獨立的 yfinance adapter。

## 必須實作

- 啟動前檢查 API 安裝、DLL/COM 註冊、位元數、版本、憑證與必要權限，回報可操作的中文錯誤。
- 支援登入事件、逾時、失敗、重複登入、登出、被動斷線及 session 失效。
- 列出可用期貨帳號，使用明確設定或讓 UI 選擇；若找不到唯一目標帳號，不得啟動策略。
- API 呼叫若要求 STA/UI thread，建立專用 dispatcher 並保證 callback 不阻塞；callback 內容立即複製為內部 DTO。
- 提供 capability/readiness 狀態：登入、交易、回報、查詢及獨立的 yfinance 市場資料各自獨立，不把「已登入」等同「可交易」。
- 登入後依序完成委託查詢、成交查詢與持倉查詢；另由 yfinance adapter 完成 ticker 驗證及市場資料初次載入，全部成功才發出 session-ready 事件。
- 帳密與憑證使用 Windows Credential Manager、DPAPI 或 SDK 官方機制；log 必須遮蔽帳號與秘密。

## 失敗處理

登入或初始化失敗時不得重複狂試；採有上限退避並允許取消。交易中 session 異常立即通知安全暫停模組。關閉程式時先停止策略與完成必要查詢，再取消訂閱及登出，不得在未知委託仍可能有效時宣稱安全關閉。

## 除錯日誌需求

- 記錄 `session_initialize`、`login_requested/result`、`account_list_received`、各 capability readiness、查詢／訂閱步驟、被動斷線與 `logout_result`，並含 session correlation ID、環境、耗時、官方錯誤碼及狀態轉移。
- callback 須記錄 callback type、接收序號、來源 thread、到達時間、DTO 轉換結果及重複／亂序判定；帳號只可遮罩，帳密、憑證與原始敏感 payload 不得出現。
- 重試須記錄 attempt、退避時間、jitter、觸發原因與停止原因；session-ready 失敗須逐項列出尚未就緒的登入、交易、回報、查詢或 yfinance 市場資料能力。

## 介面與驗收

定義 `IBrokerSession` 與事件 DTO，使其可由模擬 adapter 取代。加入針對成功、逾時、錯誤碼、重複 callback、callback 亂序及中途斷線的測試。以 staging/mock 提供登入 smoke test；真實帳號測試必須是明確 opt-in，且預設禁止送單。
