# Feature 01 — Solution Foundation

> **平台硬性限制：整個系統只能使用 x32；不得使用 x86 或 x64。原因是行情 API 僅提供 x32 版本。所有開發、相依套件、執行環境、測試、建置與部署均須遵守此限制。**

> 強制使用 Python 開發。建立 `pyproject.toml`、固定 Python 版本、虛擬環境、型別檢查、lint 與 pytest；不得以其他語言取代核心系統。

## 任務

建立一個可在 Windows 長時間執行的 Python 元大期貨自動交易桌面程式骨架。優先採用元大正式 API 明確支援的 Python/Windows 整合方式；若 API 為 COM/ActiveX 或限制特定位元數，必須將限制封裝在 Python adapter，不能讓 UI 或策略直接依賴券商物件。行情 API 僅提供 x32，因此 Python、所有相依套件、UI framework 與部署產物都必須採用 x32，禁止選用 x86 或 x64；查閱隨附的正式 API 文件與範例後，將具體版本與部署方式記錄成 ADR。

## 必須實作

- 以 Python packages 分層為 `domain`、`application`、`infrastructure.yuanta`、`persistence`、`desktop`、`tests`；依賴方向指向 domain。
- 定義不可變領域型別：商品、契約月份、交易帳號、方向、口數、價格、時間戳、K 棒、策略訊號、實際持倉、委託、成交、損益。
- 建立單一事件協調器；券商 callback 先轉成內部事件，再交由序列化處理佇列，避免 UI thread、API thread 與策略 thread 競態。
- 定義策略狀態：`Stopped`、`Starting`、`Running`、`PausedSafe`、`Stopping`、`Faulted`，並列出合法轉移。
- 建立強型別設定與啟動驗證：帳號別名、環境、選定商品、契約、自動／手動契約設定、時區 `Asia/Taipei`、04:55 平倉時間、最大 2 口。敏感資料不得寫入原始碼或一般 log。
- 所有服務使用 dependency injection；時間、識別碼、資料庫、券商 API 都要可替換，方便測試。

## 安全限制

預設啟動後不得自動送單。只有 API 已登入、帳號確定、行情有效、委託查詢完成、持倉同步成功、無未知委託且使用者明確按下啟動時才可進入 `Running`。任何未捕捉例外應轉入安全暫停，而非繼續交易。

## 交付與驗收

- 可編譯的 solution、模組說明、ADR、範例設定與秘密管理說明。
- 啟動診斷畫面能顯示各模組 readiness，但不洩漏帳密。
- 單元測試證明非法狀態轉移、口數超過 2、無效商品／契約及不合法金額會被拒絕。
- `README` 提供還原、建置、執行與測試指令；CI 可在沒有元大 API 的環境以 mock 編譯及測試。
