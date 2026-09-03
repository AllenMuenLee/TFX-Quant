# 除錯日誌

## 1. Log 位置

| 內容 | 位置 |
|---|---|
| 應用程式結構化 log | 目前為 stderr + 記憶體環形緩衝（日誌視窗），啟動器**不再**主動寫檔 |
| audit 事件鏈（決策→委託→成交→持倉→P&L） | `%LOCALAPPDATA%\tfx_quant\logs\audit.sqlite3` |
| 安裝 / 升級 log | `%LOCALAPPDATA%\tfx_quant\logs\installer-<UTC 時間>.log`（每行一個 JSON） |
| 升級器（migrate）log | 同上，`--log` 指定；phase 標為 `updater` |
| 升級前資料庫備份 | `%LOCALAPPDATA%\tfx_quant\backup\pre-upgrade-<UTC 時間>\` |
| 建置 log / 產物 | `installer\_build\`（`build-manifest.json`、`SHA256SUMS`、`stage\app\...`） |

> 若需要應用程式 log 落檔，可設定環境變數 `TFX_QUANT_LOG_LEVEL`（`DEBUG`/`INFO`/…）
> 並自行重導 stderr；日誌視窗（「查看所有日誌」）永遠可看最近事件。

## 2. 事件與 correlation / workflow ID 查找

每筆結構化事件都是一個 JSON 物件，含：

- `event`（事件名，如 `order_submitted`、`reconciliation_mismatch_detected`）
- `seq`（process 級單調遞增序號——可用來還原多執行緒交錯後的順序）
- `ts_utc` / `ts_taipei`（雙時戳）
- `correlation_id` / `workflow_id`（串起同一邏輯操作的所有事件）
- `severity`、`source`（logger 名）、以及各事件自帶欄位

查一個 workflow / 委託的完整時間軸（audit 資料庫）：

```python
from pathlib import Path
from tfx_quant.telemetry.audit import read_workflow_timeline

for step in read_workflow_timeline(
    Path.home() / "AppData/Local/tfx_quant/logs/audit.sqlite3", "<workflow_id 或 correlation_id>"
):
    print(step.seq, step.ts_utc, step.event, step.fields)
```

匯出一個反手 workflow 為 JSON Lines：

```python
from tfx_quant.telemetry.audit import export_reversal_chain

export_reversal_chain(audit_path, "<workflow_id>", Path("reversal-<id>.jsonl"))
```

安裝 log 內對應欄位：`event`、`step` / `step_started` / `step_finished`（含
`exit_code`、`duration_ms`）、`precheck`（`name` / `passed` / `severity` / `message`）、
`database_checked`、`upgrade_backup_finished`、`upgrade_aborted`、`run_finished`
（`exit_code`、`rollback_result`）。

## 3. 遮罩規則（不可外洩）

- 帳號：只記末 4 碼（`mask_account`）。
- 密碼、憑證、憑證密碼、token 等秘密：**只記存在／缺少**（`field_present`），永不記值。
- 安裝 log 另外把使用者目錄路徑收斂為 `<LOCALAPPDATA>` / `<APPDATA>` /
  `<USERPROFILE>` 代號，並把 `C:\Users\<名字>\...` 之類的片段改為 `C:\Users\<user>\...`。
- 診斷包**預設排除**憑證、秘密與完整帳號。

## 4. 各常見故障應擷取的事件

| 故障 | 擷取 |
|---|---|
| 登入失敗 | `OnLogonS` / `broker_login_*`、`stored_password_load_result`、`certificate_import_*`、對應 correlation chain |
| 委託拒單 / Unknown | `order_submitted`、`order_event_*`、`order_state_transitioned`、該委託 `client_order_id` 的所有事件 |
| 斷線 | `connectivity_channel_health_changed`、`ChannelHealthChanged`、`BrokerSessionReady`、重連 backoff 事件 |
| 持倉核對差異 | `position_query_*`、`reconciliation_mismatch_detected`、`PositionDiscrepancyDetected`、`ManualPositionSyncCompleted` |
| 04:55 / 緊急平倉 | `eod_flatten_*`、workflow 狀態轉移、`flat_confirmation_gate_evaluated` |
| 策略未如預期送單 | `risk_entry_window_evaluated`、`strategy_signal_order_submit_blocked_by_risk_gate`、該根 K 的 `StrategyDecision`（`rule` / `passed` / `reason`） |
| 安裝 / 升級失敗 | 該次 `installer-*.log` 全檔、`backup\pre-upgrade-*`、`build-manifest.json` |
| 未捕捉例外 | `uncaught_exception`（含 stack_trace）、`critical_audit_persistence_failed_safe_pause` |

## 5. 診斷包內容與安全匯出

程式關閉後，收集下列項目壓縮：

1. `%LOCALAPPDATA%\tfx_quant\logs\` 整個目錄（含 `audit.sqlite3` 複本、所有
   `installer-*.log`）。
2. `settings.json`（**先移除**任何可能的秘密——本專案設計上不會有，仍請確認）。
3. `build-manifest.json`、`RELEASE-NOTES-*.md`（安裝目錄內）。
4. 就緒畫面截圖（登入狀態、核對狀態、緊急平倉狀態）。
5. Windows 版本、`installer/prechecks` 的 JSON 輸出：
   `"<安裝目錄>\runtime\python.exe" -m tfx_quant.packaging.prechecks --json`。
6. 相關的 `workflow_id` / `correlation_id` 清單。

**匯出前確認**：不含憑證檔（`.pfx`）、不含任何密碼、不含完整帳號。若不確定，
先在文字編輯器搜尋帳號全碼與 `password` 字樣。
