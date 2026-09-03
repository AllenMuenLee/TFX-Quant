# 驗收清單（可供客戶簽核）

在一台**乾淨且符合條件**的 Windows 10/11 VM 上執行。每項通過後簽名／註記。

## A. 前置

| # | 項目 | 通過 | 備註 |
|---|---|---|---|
| A1 | VM 為全新 Windows 10/11，未安裝過本程式，`C:\Yuanta` 不存在 | ☐ | |
| A2 | 已向元大取得交易＋行情 API 使用權限（帳號／憑證） | ☐ | |
| A3 | 取得安裝檔 `tfx-quant-setup.exe` 與 `release-manifest.json` | ☐ | |
| A4 | 確認安裝檔類型：`build-manifest.json` 的 `vendor_bundle` — 內含元件版（非 null）或純程式版（null） | ☐ | |
| A5 | 純程式版才需要：已依[安裝手冊第 2 節](installation-manual.md)以系統管理員註冊元大交易（`Yuanta.YuantaOrdCtrl.1`）、行情（`YUANTAQUOTE.YuantaQuoteCtrl.1`）API 與 VC++ x86 執行環境 | ☐ | |

## B. 版本一致性核對

| # | 項目 | 通過 | 備註 |
|---|---|---|---|
| B1 | 安裝檔 SHA-256 == `release-manifest.json` 的 `installer.sha256` == 隨附 `SHA256SUMS` | ☐ | |
| B2 | `release-manifest.json` 的 `source_revision` == 交付的原始碼 git tag/commit | ☐ | |
| B3 | `release-manifest.json` 的 `source_dirty` 為 `false` | ☐ | |
| B4 | 安裝後 `<安裝目錄>\build-manifest.json` 的 `source_revision` 與 B2 相同 | ☐ | |
| B5 | 文件版本（本清單、[安裝手冊](installation-manual.md)、[維護文件](maintenance.md)）與交付版本一致 | ☐ | |
| B6 | 若有簽章：`release-manifest.json` 的 `signature.signed` 為 `true` 且可用 `signtool verify /pa` 驗證 | ☐ | |

## C. 安裝

| # | 項目 | 通過 | 備註 |
|---|---|---|---|
| C1 | 執行安裝檔，不需系統管理員權限即可完成 | ☐ | |
| C2 | 安裝程式對缺少的 VC++ / 元大 API **只提示不中止**；磁碟不足才中止 | ☐ | |
| C3 | 安裝後存在 `%LOCALAPPDATA%\tfx_quant\{config,logs,backup,data}` 四個目錄 | ☐ | |
| C4 | 安裝程式**未**預填任何帳號或密碼 | ☐ | |
| C5 | `%LOCALAPPDATA%\tfx_quant\logs\installer-*.log` 內含 `run_started` … `run_finished`（`exit_code:0`），且無明文帳密或完整使用者路徑 | ☐ | |
| C6 | 內含元件版：安裝時勾選「元件安裝」→ 出現一次 UAC → `C:\Yuanta\API\YuantaOrd.ocx` 與 `C:\Yuanta\QAPI\YuantaQuote_v2.1.2.9.ocx` 存在且 ProgID 已註冊；`vendor-install-*.log` 內 `regsvr32 … exit 0` | ☐ | |
| C7 | 內含元件版：取消 UAC → 安裝仍完成，元件留在 `<安裝目錄>\vendor\`，log 記 `vendor_install_declined`，程式可以 `TEST` 環境啟動 | ☐ | |
| C8 | 內含元件版：`<安裝目錄>\vendor\` **不含**除錯 DLL（`msvcrtd.dll` / `MFC42D.DLL` / `mfco42d.dll`） | ☐ | |

## D. 首次啟動

| # | 項目 | 通過 | 備註 |
|---|---|---|---|
| D1 | 從捷徑啟動，開啟就緒畫面 | ☐ | |
| D2 | 以 `environment: TEST` 啟動時標題／橫幅明確標示「測試環境（真實行情・模擬下單）」 | ☐ | |
| D3 | 「行情登入」可用真實行情帳號登入，行情圖開始顯示即時 60 分 K（全新安裝、無歷史資料，從登入後逐步形成） | ☐ | |
| D4 | 「切換行情」可在 MXF/TXF 間切換監看，契約年月顯示「（自動近月）」 | ☐ | |
| D5 | 設定檔故意寫錯（例：時區非 Asia/Taipei）→ 啟動立即中止並顯示原因 | ☐ | |

## E. Mock UAT（測試環境驗收）

| # | 項目 | 通過 | 備註 |
|---|---|---|---|
| E1 | `pytest -m test_env` 全數通過（真實行情形狀的假 quote host + 本機模擬器，永不連交易 API） | ☐ | |
| E2 | 測試環境下委託 / 持倉 / 損益 / 報表 / 匯出全部標示 `simulation=true` | ☐ | |
| E3 | 交易報告可依交易日 / 商品 / 方向 / workflow ID 查詢，並能從 decision 追溯到 intent→委託→成交→持倉→P&L | ☐ | |
| E4 | 重新啟動後由持久化事件重建出相同報表，無重複交易 | ☐ | |
| E5 | fail-closed：交易 adapter 一定是本機模擬器（`assert_test_env_fail_closed` 通過） | ☐ | |

## F. 升級

| # | 項目 | 通過 | 備註 |
|---|---|---|---|
| F1 | 安裝一個較舊版本，產生一些交易資料，關閉程式 | ☐ | |
| F2 | 執行新版安裝檔——若程式仍開啟，安裝程式要求關閉 | ☐ | |
| F3 | `%LOCALAPPDATA%\tfx_quant\backup\pre-upgrade-<時間>\` 內有所有 `*.sqlite3` 的複本 | ☐ | |
| F4 | 升級後資料仍在（交易報表、設定不變） | ☐ | |
| F5 | 故意毀損一個 `*.sqlite3` 後再升級 → 升級**中止**、舊版保持可用、log 有清楚訊息 | ☐ | |
| F6 | `runtime\python.exe -m tfx_quant.packaging.migrate --restore-latest` 能還原到升級前狀態 | ☐ | |

## G. 解除安裝

| # | 項目 | 通過 | 備註 |
|---|---|---|---|
| G1 | 互動式解除安裝時詢問是否刪除交易資料，預設「否」 | ☐ | |
| G2 | 選「否」→ `%LOCALAPPDATA%\tfx_quant` 完整保留 | ☐ | |
| G3 | 重新安裝後沿用既有資料 | ☐ | |
| G4 | 靜默解除安裝（`/VERYSILENT`，不加 `/REMOVEUSERDATA`）→ `%LOCALAPPDATA%\tfx_quant` **完整保留** | ☐ | |
| G5 | 互動式解除安裝選「是」（或加 `/REMOVEUSERDATA`）→ `%LOCALAPPDATA%\tfx_quant` 被移除 | ☐ | |
| G6 | 任何解除安裝方式 → `C:\Yuanta` 與 OCX 註冊**保留**（不被移除） | ☐ | |

## H. 交付演練（依 [安全 runbook](safety-runbook.md)）

操作人員在指導下，依 runbook 逐步完成下列處置並口述每一步：

| # | 情境 | 通過 | 備註 |
|---|---|---|---|
| H1 | 斷線 / 重連：辨識現象、確認自動重連、必要時登出重開 | ☐ | |
| H2 | 未知委託（Unknown）：**向券商人工確認**委託真實狀態後才動作，不自行重送 | ☐ | |
| H3 | 手機 App 造成持倉差異：辨識差異來源、向券商確認真實部位、用緊急平倉或重開讓復原程序對齊 | ☐ | |
| H4 | 緊急平倉：重新查詢→核對→確認→複查為 0 | ☐ | |
| H5 | 04:55 之後才啟動而仍有部位：立即緊急平倉，不等下一個 04:55 | ☐ | |

## I. 交付聲明

| # | 項目 | 通過 |
|---|---|---|
| I1 | 所有文件均**未**暗示本軟體可取代券商端人工確認；operator 已被告知任何異常仍須向元大確認 | ☐ |
| I2 | operator 已被告知[操作手冊第 10 節](operations-manual.md#10-已知限制與規格差異)所列的已知限制（無策略啟動／暫停／停止 UI、無正式安全暫停恢復流程） | ☐ |

---

驗收人：____________________  日期：____________  簽名：____________________
