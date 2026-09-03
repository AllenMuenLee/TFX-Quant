# 維護文件

## 1. 架構

分層，相依方向一律指向 `domain`（由 `import-linter` 於 CI 機械式驗證，契約定義於
`pyproject.toml [tool.importlinter]`）：

```
tfx_quant.desktop ─────┐
                        ├──> tfx_quant.application ──> tfx_quant.domain
tfx_quant.persistence ──┘
tfx_quant.infrastructure (含 .yuanta) ──> tfx_quant.application ──> tfx_quant.domain
tfx_quant.telemetry     — 任何層皆可 import（不在任何 forbidden 清單）
tfx_quant.packaging     — 安裝／升級支援碼，可依賴 persistence + telemetry + infrastructure
```

- **組合根**：`src/tfx_quant/desktop/composition.py` 是唯一把具體服務接起來的地方。
  `build_services()` 依 `settings.environment` 選交易 adapter（`PRODUCTION` → 真實
  `LegacyBroker` OCX；`TEST` → 本機 `MockBrokerSession`/`MockTradeGateway`）。行情在
  兩種環境都是真實元大行情 OCX（`YuantaQuoteComHost`）。
- **事件模型**：`EventCoordinator` 把所有 broker callback / UI / 策略執行緒的事件序列化到
  單一處理迴圈，訂閱者程式碼不會自我競爭。
- **執行緒**：OCX 只能在 wx UI 執行緒建立與操作；`ConnectivityMonitor` 從
  `threading.Timer` 執行緒重連時透過 `wx.CallAfter` 派回 UI 執行緒。
- **啟動流程**（`desktop/__main__.py` → `desktop/app.py`）：
  `configure_logging` → `load_settings` → `build_services` → `event_coordinator.start`
  → `auto_select_startup_instrument` →（TEST）`start_test_env_broker_session` →
  `order_manager` / `reconciliation_service` / `connectivity_monitor` / `risk_supervisor`
  / `signal_engine_service` 逐一 `start()` → `install_audit_handler`。

### 與規格書的落差（維護時務必知悉）

`domain.strategy_state.StrategyStateMachine` 與
`application.safety.startup_safety_gate`（九項 `SafetyChecklist`）已實作，但**目前沒有
任何程式路徑把狀態機轉入 `STARTING`/`RUNNING`**，也沒有對應的 UI。因此：

- 策略引擎（`signal_engine_service`）在程式啟動後即持續運作，送單與否由**事件驅動的
  旗標**把關（行情 stale/gap、活動委託、`position_state_uncertain`、風控進場時窗），
  **不是**由 `StrategyStateMachine` 把關。
- `ConnectivityMonitor` / `PositionReconciliationService` 的「自動安全暫停」只在狀態機
  為 `RUNNING` 時才實際轉態，因此目前只會**記錄**問題並（對持倉差異）設定
  `position_state_uncertain` 旗標擋下新進場，而不會產生需要人工「恢復」的暫停狀態。
- 補完項目：策略啟動／暫停／停止 UI + 把 `StartupSafetyGate` 接為硬性前置條件
  （Feature 05 / 12 範疇）。

## 2. 資料庫

每個功能各用一個獨立的 SQLite 檔與獨立連線（絕不共用，避免鎖競爭）。位置：
`%LOCALAPPDATA%\tfx_quant\`，可由設定檔對應欄位覆寫。

| 檔案 | 內容 | 設定欄位 |
|---|---|---|
| `market_data.sqlite3` | 自聚合的 60 分 K 歷史 + 原始行情事件 | `market_data_db_path` |
| `orders.sqlite3` | `OrderManager` 的委託意圖 | `order_db_path` |
| `reversal_workflows.sqlite3` | 反手工作流程 | `reversal_workflow_db_path` |
| `position_baselines.sqlite3` | 預期持倉基準 | `position_baseline_db_path` |
| `eod_flatten_workflows.sqlite3` | 04:55 / 緊急平倉工作流程 | `eod_flatten_workflow_db_path` |
| `fill_ledger.sqlite3` | 只增不改的成交台帳（P&L 基礎） | `fill_ledger_db_path` |
| `logs/audit.sqlite3` | 結構化 audit 事件鏈（決策→成交→P&L 時間軸） | `audit_db_path` |

- 連線一律 `PRAGMA foreign_keys = ON`。`persistence/database.py` 的
  `open_managed_database` 另提供 `PRAGMA journal_mode = WAL` + 完整性檢查 + 遷移前備份
  的受管理生命週期，但目前**未接入執行中的 app**（各 repo 以 `CREATE TABLE IF NOT
  EXISTS` 自建結構）。
- 「migration 驗證」目前的實質內容 = 完整性檢查 + 版本天花板檢查（見第 5 節）。

## 3. 設定

`settings.json`（範本：`src/tfx_quant/desktop/settings.example.json`）。
`application/settings/trading_settings.py` 的 `validate_startup()` 是唯一的驗證入口，
啟動時任一違規即以清楚訊息中止：

- `timezone_id` 必須 `Asia/Taipei`；`eod_flatten_local_time` 必須 `04:55`；
  `max_net_lots` 介於 1–2。
- `selected_instrument` 必須為已定義商品；`contract_selection_mode` 為 `AUTO`。
- `instrument_master_path` / `trading_calendar_path` 為 `null` 時使用內建種子檔：
  - `instrument_master.example.json`：MXF 的 `order_commodity_code` 仍為空字串，
    正式下單前**必須**向元大 `FunctionList.xls` 覆核填入並標記 `tradable`。
  - `trading_calendar.example.json`：假日為網路查詢種子值，須以 TAIFEX 官方行事曆覆核。
- **憑證與密碼絕不進入設定檔**。元大帳密於登入視窗輸入；勾選「安全儲存密碼」才會寫入
  Windows 認證管理員（DPAPI），keyring service name：
  `tfx_quant.yuanta`（交易）、`tfx_quant.yuanta.quote`（行情）、
  `certificate-import-password`（憑證 PFX 密碼）。

## 4. 記錄（log）

見專文[除錯日誌](debug-logging.md)。重點：

- 結構化事件（每筆一個 JSON 物件），process 級單調遞增序號 + UTC 與 Asia/Taipei
  雙時戳；`correlation_id` / `workflow_id` 串起一個邏輯操作。
- 呼叫端負責在值進入 log 前遮罩敏感資訊（`telemetry/masking.py`：`mask_account`
  保留末 4 碼；`field_present` 只記存在與否）。
- Sink：stderr + 記憶體環形緩衝（日誌視窗）+ `SqliteAuditHandler`（audit 事件持久化）。
- audit 寫入失敗（critical）會觸發安全暫停嘗試。
- `DiagnosticMode`：對單一 workflow / order 的有界、自動到期的 DEBUG 詳細度提升
  （預設 10 分鐘 / 1000 筆），不改變 process 級 log level。

## 5. 備份與還原

- 升級前備份：`python -m tfx_quant.packaging.migrate --apply` 會把
  `%LOCALAPPDATA%\tfx_quant` 下所有 `*.sqlite3`（含 `-wal` / `-shm`）整份複製到
  `backup\pre-upgrade-<UTC 時間>\`，然後執行檢查。
- 檢查（`--check`）：對每個資料庫做 `PRAGMA integrity_check`、讀 `PRAGMA user_version`；
  任一毀損或 `user_version` 高於本 build 支援上限（`SUPPORTED_MAX_USER_VERSION`，
  = `persistence.database.LATEST_SCHEMA_VERSION`）→ 結束碼非 0（升級中止）。
- 還原：`python -m tfx_quant.packaging.migrate --restore-latest` 取最新
  `pre-upgrade-*` 快照，逐檔驗證完整性（`persistence.database.restore_backup`）後
  複製回原位，並清掉會誤用的舊 `-wal` / `-shm`。
- 手動備份：直接複製整個 `%LOCALAPPDATA%\tfx_quant`（程式關閉時）。

## 6. API 版本矩陣

| 項目 | 交易 API | 行情 API |
|---|---|---|
| 元件資料夾 | `C:\Yuanta\API`（32 位元） | `C:\Yuanta\QAPI` |
| OCX | `YuantaOrd.ocx` | `YuantaQuote_v2.1.2.9.ocx` |
| 相依 DLL | `YuantaOrdLib.dll`、`YuantaCAPIDLL.dll` | — |
| ProgID | `Yuanta.YuantaOrdCtrl.1` | `YUANTAQUOTE.YuantaQuoteCtrl.1` |
| 註冊 bat（系統管理員） | `install_YTFutOrdAP.bat` | `install_ytocx.bat` |
| 主機（測試 / 正式） | `apitest.` / `api.yuantafutures.com.tw`，Port 80 / 443 | `apiquote.yuantafutures.com.tw`，T 盤 80/443、T+1 盤 82/442 |
| 位元數 | 與直譯器相同（本專案 32 位元） | 僅 32 位元 |
| 建立方式 | `AtlAxCreateControlEx` + wx window handle（非無視窗 `CreateObject`） | 同左 |
| 文件唯一來源 | `交易API元件及說明文件/` | `行情API元件及說明文件/` |

- 應用程式本身固定 Python 3.11（32 位元）+ `installer/requirements.lock`（釘死並附
  hash 的相依套件）。安裝檔內含 embeddable CPython，客戶端不需自行配置。
- 交易 API 已無獨立測試主機；測試以 `environment: TEST`（本機模擬器）進行。

## 7. 建置與測試

| 指令 | 用途 |
|---|---|
| `.venv\Scripts\ruff check .` / `ruff format --check .` | Lint / 格式 |
| `.venv\Scripts\mypy src` | 型別檢查 |
| `.venv\Scripts\lint-imports` | 分層相依檢查 |
| `.venv\Scripts\pytest` | 全套離線測試（不需元大 DLL、不連網） |
| `.venv\Scripts\pytest -m test_env` | 測試環境驗收（真實行情形狀的假 quote host + 本機模擬器） |
| `.venv\Scripts\pytest --co -m real_api` | 列出（不執行）真實 API smoke test |
| `py -3.11-32 installer\build.py` | 產生 stage 樹 + 建置 manifest + checksum + 授權清單 |
| `py -3.11-32 installer\make_installer.py` | 編譯安裝檔（需 Inno Setup 6）+ 可選簽章 + release manifest |

CI（`.github/workflows/ci.yml`，`windows-latest` + 32 位元 Python）跑上述 lint / mypy
/ import-linter / pytest（僅用 mock gateway），並另有 `package` job 執行
`installer/build.py` 驗證建置腳本健康。

### 「symbols」與現場比對

Python 沒有 PDB。可供現場二進位與原始碼精確比對的是：

- 原始碼 git tag / commit（記於 `build-manifest.json` 的 `source_revision`）；
- 釘死並附 hash 的 `installer/requirements.lock`；
- `build-manifest.json`（工具版本、embeddable Python 檔 hash、每個相依套件的 hash、
  staged 檔數）與 `SHA256SUMS`（每個檔的 hash）；
- `release-manifest.json`（安裝檔 SHA-256 ↔ source revision ↔ build manifest ↔ 簽章）。

## 8. 故障排查

| 現象 | 檢查 |
|---|---|
| OCX 建立失敗 / ProgID 未註冊 | 以系統管理員執行對應 `install_*.bat`；確認位元數；VC++ x86 執行環境 |
| `E_UNEXPECTED` / 找不到相依 DLL | `vcredist_x86.exe`；`os.add_dll_directory`（`preload_control`）路徑；元件資料夾未被搬動 |
| 登入 `OnLogonS` TLinkStatus 4 / 5 / -1 | 4 = 憑證錯誤；5 = 密碼錯誤；-1 = 連線中斷（網路/防火牆） |
| 交易 API 測試環境無回應 | 元大已無交易測試主機；改用 `environment: TEST` 或正式環境 |
| 啟動時設定驗證失敗 | 依訊息修正 `settings.json`（時區、平倉時間、口數、商品） |
| 升級被中止（完整性檢查失敗） | `backup\pre-upgrade-*` + 安裝 log；資料庫毀損或版本較新；舊版仍可用 |
| DB「schema is newer than supported」 | 該資料庫由較新版程式寫入；請安裝相符或更新版本，勿降級使用 |
| 行情長時間 stale / 有缺口 | 行情連線 / T 盤 T+1 盤 Port；缺口不補值屬正常；策略會維持 blocked |
| 未實現 P&L 顯示未知 | 無有效即時報價；不以合成價補值，屬正常 |
