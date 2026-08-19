# Feature 01 — Solution Foundation

> **API 文件唯一來源：實作前必須直接讀取[元大 SPARK API 官方入口](https://www.yuanta.com.tw/file-repository/content/API/page/index.html)及其下方連結的 API 說明文件、範例、元件下載與換版資訊。不得使用專案內既有資料夾、舊 SDK 文件、舊範例或既有程式碼反推 API 規格；若與本 prompt 其他描述衝突，以官方線上文件當下內容為準。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發。建立 `pyproject.toml`、固定 Python 版本、虛擬環境、型別檢查、lint 與 pytest；不得以其他語言取代核心系統。

## 任務

建立一個可在 Windows 長時間執行的 Python 元大期貨自動交易桌面程式骨架。依元大 SPARK API 官方線上文件選用其正式支援的 Python/Windows 元件與位元數，並將券商整合封裝在 Python adapter，不能讓 UI 或策略直接依賴券商物件。將查閱日期、元件版本、下載來源、平台／位元數選擇與部署方式記錄成 ADR。

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
