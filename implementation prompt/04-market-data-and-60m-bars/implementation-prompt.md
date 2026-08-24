# Feature 04 — 元大即時行情與 60 分鐘 K 棒

> **唯一規格來源：實作本功能前，必須讀取專案根目錄 [`行情API元件及說明文件/`](../../行情API元件及說明文件/) 內的說明文件、元件及版本資訊。只有該資料夾明確記載的介面、欄位、代碼與行為可作為實作依據。禁止使用交易 API 文件、SPARK、yfinance、其他行情商、網路資料、既有非元大行情 adapter，或憑經驗推測未記載的 API 行為。文件未記載或互相矛盾之處，必須列為 blocker，不得自行補造。**

> **歷史資料限制：現有行情 API 文件只記載 `Snapshot`、`Update`、`SnapshotUpd` 註冊模式，以及由 `OnGetMktAll` 接收行情；未記載歷史價格或歷史 K 棒查詢。因此禁止向任何 API 或外部來源回補歷史價格。系統只能記錄連線後實際收到的即時行情，並從這些本機紀錄聚合、保存及讀取歷史 K 棒。開始記錄前或中斷期間的缺口必須保留並清楚標示，不得合成或回補。**

強制使用 Python 開發，並依文件所列的 32-bit Python、COM 元件及相依套件需求驗證執行環境。行情 adapter、持久化及 K 棒聚合器須有完整型別註記，且可由 pytest 隔離測試。

## 任務

建立元大行情 API adapter，完成登入、連線狀態管理、商品註冊、即時行情接收、本機持久化，以及以已記錄行情聚合 60 分鐘 K 棒。60 分 K 的交易時段、交易日歸屬及切點若不在唯一來源文件內明載，必須做成待確認設定並阻擋正式交易，不得猜測。

## 文件允許的 API 範圍

- 使用 `SetMktLogon(User, pass, IP, PORT)` 連線；帳密不得寫入程式碼或日誌。
- 依文件處理 `OnMktStatusChange(Status, Msg)`、所有 `TlinkStatus` 數值及登入訊息代碼。
- 使用 `AddMktReg(Symbol, UpdateMode)` 註冊商品，並處理 `RegErrCode`。需要當下資料與後續更新時使用文件記載的 `SnapshotUpd`；不得把 snapshot 解讀為歷史查詢。
- 使用 `DelMktReg(Symbol)` 取消註冊，並處理 `OnRegError(Symbol, UpdateMode, ErrorCode)`。
- 只從 `OnGetMktAll(...)` 接收文件記載的欄位：`Symbol`、`RefPri`、`OpenPri`、`HighPri`、`LowPri`、`UpPri`、`DnPri`、`MatchTime`、`MatchPri`、`MatchQty`、`TolMatchQty`、五檔買賣價量及盤前揭示欄位。
- 商品代碼只能使用文件明載的規則或經使用者提供並驗證的 EASYWIN 報價代碼；不得猜測、替換月份契約或使用未經確認的連續契約。

## 必須實作

- 將 COM／OCX 事件處理隔離於 adapter；領域層與 repository 不得直接依賴 COM 物件。啟動時驗證位元數、元件註冊與相依套件，失敗時提供可操作的錯誤訊息。
- 建立明確的連線狀態機。只有登入成功後才可註冊商品；斷線時將行情標成 stale、停止依賴新行情的訊號與委託，並依有界重試政策重連及重新註冊。
- 驗證所有字串欄位後才轉換。價格使用精確 decimal；數量使用適當整數型別；時間同時保存原始 `MatchTime`、解析後 UTC 與 `Asia/Taipei`。文件未定義的格式不得猜測，須保留原值、拒絕該筆正規化並告警。
- 盤前 `TolMatchQty = -1` 必須依文件視為盤前資料，不得當作負成交量或正式成交紀錄。
- 每個實際收到的行情事件須先持久化，再供 K 棒聚合使用。至少保存商品 identity、接收序號、原始欄位、正規化成交時間、接收時間、價格、單量、總量、連線 session 及資料品質狀態。
- 使用本機已持久化的有效成交資料，以可設定的交易日曆、session 與 60 分鐘邊界聚合 OHLCV。不得直接把 `OnGetMktAll` 的當日 `OpenPri`／`HighPri`／`LowPri` 當成單根 60 分 K 的 OHLC，也不得用 snapshot、前收價、前一根 close 或空白 bar 補洞。
- 成交量增量只能在文件語意與測試可證實時由 `MatchQty` 或 `TolMatchQty` 計算；重連、歸零、重複事件、亂序或無法判定時須標示 bar 不完整，不得猜測成交量。
- K 棒只有在其設定邊界已越過且完整性檢查通過後，才可持久化並發出一次 `BarClosed`。重複、亂序、遲到資料及程序重啟必須有 idempotent 策略。
- 啟動或重連後，只能讀取本機先前由此即時行情 recorder 保存的事件與 K 棒來恢復狀態。不得呼叫任何歷史行情服務。
- 缺少開始記錄前的資料、斷線缺口或不完整 bar 時，保留 gap、將 readiness 標為 degraded，並阻擋需要連續 K 棒的策略。恢復即時接收不代表過去 gap 已補齊。
- 定義紅 K 為 `close > open`、黑 K 為 `close < open`；十字 K `close == open` 中斷連續紅／黑計數。只有完整且已收盤的 K 棒可參與計數。
- 08:45、09:45 與最早 10:45 建倉規則，只有在交易日曆和 bar label／close-time 語意已由使用者確認並設定後才可啟用；否則保持 fail closed。

## 禁止事項

- 禁止加入 yfinance 或任何其他網路行情、歷史價格、CSV／fixture 生產資料來源。
- 禁止將 `Snapshot` 或 `SnapshotUpd` 宣稱為歷史回補。
- 禁止為了填滿圖表或滿足策略暖機而合成 tick、K 棒、成交量或缺口資料。
- 測試 fixture 只能測試已記載的事件處理，不得在正式執行路徑冒充行情來源。

## 持久化與可觀測性

- 行情事件及 closed bar 使用穩定 identity 與 database unique constraint，採 idempotent insert／upsert；不得靜默覆寫已收盤 K 棒，修訂須保留 audit。
- 記錄連線狀態變化、登入結果、註冊／取消註冊結果、商品代碼、UpdateMode、RegErrCode、ErrorCode、事件接收數、解析拒絕數、stale、gap、bar 完整性與 `BarClosed` 序號。
- 不得記錄帳號、密碼或完整敏感 payload。時間同時記錄 UTC 與 `Asia/Taipei`，並保留 API 原始時間字串供診斷。
- UI 顯示連線狀態、註冊狀態、最後接收時間、資料時間、stale、目前 forming bar、最近 closed bars、資料起始時間及 gap／不完整狀態；不得暗示可取得 recorder 啟用前的歷史。

## 驗收

- 以 fake COM event source 驗證完整連線狀態、登入訊息、註冊模式、註冊錯誤、取消註冊及斷線重連流程。
- 以 `OnGetMktAll` fixture 驗證欄位解析、盤前 `TolMatchQty = -1`、重複、亂序、遲到、無效數值、原始時間保存及敏感資訊遮罩。
- 驗證事件先落盤後聚合、60 分鐘邊界、跨午夜、程序重啟、idempotency、forming／closed 狀態及十字 K 規則。
- 驗證全新安裝沒有歷史資料；啟動後只能逐步累積即時紀錄。斷線造成的期間不得被補值，重連後 gap 仍可見且相關策略維持 blocked／degraded。
- 測試不得連接真實帳號或依賴外部行情服務。
