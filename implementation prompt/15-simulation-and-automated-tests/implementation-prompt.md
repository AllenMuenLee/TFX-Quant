# Feature 15 — 測試環境（模擬下單）與自動化驗收測試

> **API 文件唯一來源：交易相關實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/)；行情相關實作前必須直接讀取 [`行情API元件及說明文件/`](../../行情API元件及說明文件/)。只有各自資料夾內的元大官方說明、Python 範例、元件與版本資訊可作為對應 adapter 行為的依據。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例、既有非元大 adapter、yfinance、其他行情商或網路資料反推 API。資料夾缺少、文件未明載、內容矛盾或本 prompt 與官方文件衝突時，須停止相關實作並列為 blocker。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數、商品代碼與能力都必須有對應官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。測試 fixture 只能重現文件已記載的介面與事件，不得被正式執行路徑當成行情來源。**

> 強制使用 Python 開發；使用 pytest 建立 broker simulator、故障注入與自動化驗收測試。

## 任務

只有兩種執行環境，由 `settings.environment` 決定，不再有任何 `--mock` / `--uat` 旗標或獨立離線模擬 runtime：

- **正式環境（`PRODUCTION`）**：交易與行情皆為元大真實 OCX，交易連正式主機。
- **測試環境（`TEST`）**：委託、成交、取消、持倉、帳務全部由本機 broker simulator 執行——它不對任何伺服器送出任何東西；**行情仍為元大即時真實行情**（依官方行情 API 正常登入、訂閱、接收並先落盤）。取代任何「登入交易 API 的 UAT／測試環境」流程——已無交易測試主機。

測試環境的策略、K 棒、訊號、風控、workflow、持久化與查詢 read model 與正式環境完全相同，不得在策略內加入 `if simulator` 分支。行情事件必須先落盤再聚合；不得以 fixture、replay、合成行情或模擬行情取代即時行情。測試環境與正式環境必須在視覺及設定上明確區分，並以 fail-closed 保證測試環境無法誤送真單（交易 adapter 一定是本機 simulator）。

## 模擬能力

- broker simulator 只替換 broker／execution adapter。模擬成交須由實際送單意圖及當下已落盤的市場行情決定；成交模型、滑價、手續費、交易稅與拒單規則須可設定、具版本並完整記錄（`application/trade_reports/fee_model.py` 的 `FillFeeModel`／`simulation_fee_model` 設定區塊）；不得偷看未來行情。
- 模擬期貨交易 API 的登入、送單、ack、部分／全部成交、拒單、取消、回報重複／亂序／延遲、查詢矛盾、Unknown 與通道斷線（`MockTradeGateway`／`MockBrokerSession` 的 `simulate_*`）。
- 行情測試以假的 `QuoteGateway` host（`tests/desktop/test_test_environment.py::FakeYuantaQuoteComHost`）重現文件記載的 `SetMktLogon`、`OnMktStatusChange`、`AddMktReg`／`DelMktReg`、`OnRegError`、`Snapshot`／`Update`／`SnapshotUpd`、`OnGetMktAll`、盤前 `TolMatchQty = -1`、無效欄位、重複／亂序／遲到、重連、stale、缺口、不完整 bar 與程序重啟。不得模擬或宣稱存在官方文件未記載的歷史行情查詢。
- 模擬手機 App 改變持倉，以及程式在 workflow 任意持久化點崩潰與重啟。
- 商品 identity 必須完整保留。策略產生的交易意圖永遠只能是小台指（MXF）；UI 選擇的大台／小台行情只能改變監看內容，不得改變 simulator 中的交易商品。
- 測試環境啟動時不得要求或接受任何交易 API 登入資料（本機 simulator 只見保留帳號 `TEST-SIMULATION`）；若交易 adapter 不是本機 simulator，啟動必須 fail closed（`desktop.composition.assert_test_env_fail_closed`）。行情登入憑證獨立保存（`credentials.QUOTE_KEYRING_SERVICE_NAME`），測試環境只取得行情所需能力。

## 測試環境 UI 與報表

- 測試環境必須使用與正式模式相同的 P&L、持倉、委託、成交與交易報告 view-model（`tfx_quant.desktop.view_models`）及相同計算路徑；不得提供只在測試環境存在的簡化報表。畫面須可顯示已實現 P&L、未實現 P&L、總 P&L、持倉成本、成交價、數量、方向、手續費、交易稅、滑價、委託／成交時間、交易原因與完整 correlation chain。
- P&L 必須以 broker simulator 的實際模擬成交為成本基礎，未實現損益以同一真實市場行情來源的最新有效價格評價；行情 stale、斷線、gap 或價格無效時要顯示資料品質與最後更新時間，不得以合成價格補值。
- 正式與測試環境共用相同 report schema、查詢與匯出格式；每筆模擬成交、已實現交易、報表與匯出都不可移除地標示 `simulation=true`（`LedgerFill.simulation` 欄位＋ `fill_ledger` 資料表欄位），並顯示醒目的「交易模擬／不會送出真單」環境標誌。
- 模擬交易報告須可依交易日、商品、方向、workflow／correlation ID 查詢，並能從策略 decision 追溯到 intent、模擬委託、ack、fill、持倉變化及 P&L（audit 時間軸來自 `telemetry/audit.py` 的 audit sqlite）。重啟後須由持久化事件重建出相同結果，不得因重算產生重複交易或不同報表。

## 必備驗收情境

1. 全新安裝沒有歷史資料；只從登入後收到且先落盤的有效行情逐步形成 60 分鐘 K 棒。斷線期間保留 gap，重連不得補值；資料不足、stale、gap 或 bar 不完整時策略維持 blocked／degraded。
2. 驗證 60 分鐘邊界、跨午夜、forming／closed 狀態、`BarClosed` 僅發出一次，以及紅／黑／十字 K 定義；十字 K 中斷連續計數。
3. 空手時兩根紅 K、20MA 向上且所有閘門通過，建立 MXF 多單第 1 口；必須等實際成交後再由下一根紅 K 加碼第 2 口，永不超過 2 口。空單案例須方向鏡像。
4. 錯向或未明確斜率、20MA 樣本不足，以及最近 5 根 20MA 幅度小於 10 點時不得進場或加碼；幅度恰等於 10 點須可通過走平過濾。這些過濾不得阻止任何風控平倉。
5. 單口以第 1 口實際成交價、雙口以第 2 口實際成交價驗證多空鏡像的 300 點停損；雙口命中時產生單一全平 workflow，不得只平第 2 口。缺少可靠成交價時安全暫停。
6. 獲利恰達 300 點後才啟動追蹤；最大有利點數可繼續上移，回吐達 30% 時依經確認的整體部位公式全平。部分成交期間以實際剩餘部位維持風控，只有全平完成才重置持倉週期狀態。
7. 08:45、09:45 不建倉，10:45 才可判斷；04:55 全平且不再開倉。涵蓋重啟或斷線錯過 04:55，且同時命中時遵守「04:55／緊急風控、停損、獲利回吐、進場／加碼」優先序。
8. 平倉部分成交、拒單、逾時、斷線與 Unknown 全部暫停，且不反手、不盲目重送；非 flat、存在活動委託或狀態不確定時不得建立反向部位。
9. 手機 App 改倉、其他契約持倉或線上查詢失敗皆由自動核對觸發安全暫停；UI 不提供人工重新查詢或同步按鈕，也不得自動覆寫本機 baseline。
10. 重複 bar、重複 callback、亂序／晚到行情與成交、相同時鐘事件、程序重啟及 crash recovery 不造成重複 decision、intent、closed bar 或委託。
11. UI 切換大台／小台監看行情後，所有交易 decision、intent、委託與部位斷言仍只使用 MXF；其他商品不得被策略下單。
12. 測試環境以真實即時市場行情連續運行，證明交易 API 從未登入或連線、所有 execution 均由本機 simulator 產生；UI 能以正式模式相同的畫面與計算顯示持倉、委託、成交、已實現／未實現／總 P&L 及完整交易報告，且重啟後結果一致。

## 除錯日誌需求

- 每次自動化測試記錄 scenario／test id、fixture、注入故障、事件序號及 simulator 狀態，使失敗可用同一輸入重現。
- 行情處理記錄商品 identity、原始 `MatchTime`、解析後 UTC／`Asia/Taipei`、接收 session／序號、資料品質、stale、gap、bar 完整性與 `BarClosed` identity。
- 模擬 broker 記錄收到的 request、排程的 ack/fill/query response、延遲、重複／亂序設定及斷線點；只使用假資料，並在每筆成交事件清楚標示 `simulation=true`。
- 測試失敗輸出最後相關事件、狀態機轉移、未滿足 invariant、pending orders／events 與 correlation chain；coverage 或大量成功案例不得輸出無界逐 tick log。

## 品質門檻

Domain/application 自動化測試不得需要元大 DLL；關鍵狀態機採 property/state-transition tests，至少加入「MXF 最大曝險 ≤2」、「非 flat 不可反手」、「資料不完整或不確定時不得增加曝險」、「closed bar／decision／intent／委託／ledger fill identity 冪等」及「監看商品不改變交易商品」不變量。行情測試須以直接的假物件證明事件先落盤再聚合，且不得依賴網路、外部行情或歷史回補；`test_env` 驗收測試使用真實行情「形狀」但仍以假 quote host 執行，永不連任何交易 API。提供一鍵自動化測試指令、測試資料來源與格式說明、coverage 報告，以及可供客戶簽核的測試環境 checklist。任何真實交易 API smoke test 必須獨立標記、預設跳過且預設禁止送單，不得作為驗收的必要步驟。

## 交付與指令（實作結果）

- **一鍵自動化測試指令**
  - `.venv\Scripts\pytest` — 離線全套（domain／application／desktop；不需元大 DLL、不連網）。
  - `.venv\Scripts\pytest -m test_env` — 測試環境驗收（scenario 12 + 客戶簽核 checklist），使用真實行情「形狀」的假 quote host、本機 broker simulator，永不連交易 API。
  - `.venv\Scripts\pytest --cov=tfx_quant --cov-report=term-missing --cov-report=html` — coverage 報告（設定於 `pyproject.toml [tool.coverage]`；wx panel／dialog／frame／__main__ 以 `omit` 排除，改由 view-model 測試涵蓋）。
  - `.venv\Scripts\pytest --co -m real_api` — 列出真實交易 API smoke test 但不執行。
  - `set TFX_QUANT_REAL_API=1 && .venv\Scripts\pytest -m real_api` — 明確 opt-in 執行；仍不送單。
- **測試資料來源與格式**
  - `tests/desktop/test_test_environment.py` 的 `FakeYuantaQuoteComHost` 僅實作 `行情API元件及說明文件/` 記載的 `QuoteGateway` 介面與 `OnGetMktAll` 欄位排版；為測試替身，永不出現在正式路徑。
- **可供客戶簽核的測試環境 checklist**：以可執行 pytest 交付，`tests/desktop/test_test_environment.py`（標記 `@pytest.mark.test_env`），每條簽核項目對應一個具名測試：
  - `test_build_wires_the_local_simulator_and_the_real_quote_factory` — `environment: TEST` → 本機 mock broker、真實 quote factory、`fill_ledger_service.source == "SIMULATION"`。
  - `test_trade_api_never_logged_in` / `test_quote_credentials_reach_only_the_quote_host` — 交易 API 從未登入（mock broker 只見保留帳號 `TEST-SIMULATION`）；行情帳密只到 quote host。
  - `test_all_executions_originate_from_the_simulator` — 所有成交 `source == "SIMULATION"` 且 `simulation`。
  - `test_orders_positions_pnl_report_use_the_production_view_models` — 使用正式 `tfx_quant.desktop.view_models`；委託／持倉／損益／報表／匯出全部 `simulation=true`。
  - `test_unrealized_pnl_needs_a_real_mark_and_never_synthesises` — 無有效報價時未實現／總損益為 `None`，不補值。
  - `test_drilldown_walks_trade_to_fills_to_intents` — 交易報告追溯 trade→fills→intents（+ audit 時間軸）。
  - `test_restart_rebuilds_an_identical_report_no_duplicates` — 重啟由持久化事件重建相同報表，無重複交易。
  - `test_fail_closed_and_readiness_row` / `test_fail_closed_rejects_a_non_simulator_broker` / `test_start_test_env_helpers_refuse_in_production` — `desktop.composition.assert_test_env_fail_closed`。
- **後續 blocker（記錄）**：正式模式的真實成交手續費／交易稅在 `交易API元件及說明文件/` 的成交回報是否帶入尚待確認；在確認前，正式成交的費用一律標示 `provisional`（`application/trade_reports/fee_model.py` 的 `PROVISIONAL_FEE_MODEL`），僅測試環境使用可設定具版本的 `simulation_fee_model`。
