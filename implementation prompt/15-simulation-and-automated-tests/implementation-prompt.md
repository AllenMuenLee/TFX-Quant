# Feature 15 — Simulator, Replay, and Automated Acceptance Tests

> **API 文件唯一來源：交易模擬實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/)；行情模擬實作前必須直接讀取 [`行情API元件及說明文件/`](../../行情API元件及說明文件/)。只有各自資料夾內的元大官方說明、Python 範例、元件與版本資訊可作為對應 adapter 行為的依據。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例、既有非元大 adapter、yfinance、其他行情商或網路資料反推 API。資料夾缺少、文件未明載、內容矛盾或本 prompt 與官方文件衝突時，須停止相關實作並列為 blocker。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數、商品代碼與能力都必須有對應官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。測試 fixture 只能重現文件已記載的介面與事件，不得被正式執行路徑當成行情來源。**

> 強制使用 Python 開發；使用 pytest 建立 broker simulator、虛擬時鐘、回放與故障注入測試。

## 任務

建立完全不連真實帳號的 broker simulator、fake COM 行情 event source、虛擬時鐘與行情 replay harness，讓 Feature 04 的即時行情記錄／60 分鐘 K 棒聚合及 Feature 05 的策略與安全案例可重現。Replay 只能使用測試 fixture，或本機 recorder 先前實際持久化的行情事件與 closed bars；不得取得、合成或回補 recorder 啟用前及斷線期間的歷史行情。測試環境必須在視覺及設定上與正式環境明確區分，避免誤送真單。

## 模擬能力

- 播放文件允許的 `OnGetMktAll` fixture、本機已持久化的行情事件／closed bars、交易日與時鐘；可控制速度、暫停及跳到指定事件時間。跳時不得越過未處理事件、暗中補洞或合成 tick、K 棒與成交量。
- 模擬行情 API 的 `SetMktLogon`、`OnMktStatusChange`、`AddMktReg`／`DelMktReg`、`OnRegError`、`Snapshot`／`Update`／`SnapshotUpd` 與 `OnGetMktAll`；涵蓋登入／註冊錯誤、盤前 `TolMatchQty = -1`、無效欄位、重複／亂序／遲到事件、重連、stale、缺口、不完整 bar 及程序重啟。不得模擬或宣稱存在官方文件未記載的歷史行情查詢。
- 模擬期貨交易 API 的登入、送單、ack、部分／全部成交、拒單、取消、回報重複／亂序／延遲、查詢矛盾、Unknown 與通道斷線。
- 模擬手機 App 改變持倉，以及程式在 workflow 任意持久化點崩潰與重啟。
- 使用與正式 adapter 相同的 application interface，不得在策略內加入 `if simulator` 分支。
- 商品 identity 必須完整保留。策略產生的交易意圖永遠只能是小台指（MXF）；UI 選擇的大台／小台行情只能改變監看內容，不得改變 simulator 中的交易商品。

## 必備驗收情境

1. 全新安裝沒有歷史資料；只從登入後收到且先落盤的有效行情逐步形成 60 分鐘 K 棒。斷線期間保留 gap，重連不得補值；資料不足、stale、gap 或 bar 不完整時策略維持 blocked／degraded。
2. 驗證 60 分鐘邊界、跨午夜、forming／closed 狀態、`BarClosed` 僅發出一次，以及紅／黑／十字 K 定義；十字 K 中斷連續計數。
3. 空手時兩根紅 K、35MA 向上且所有閘門通過，建立 MXF 多單第 1 口；必須等實際成交後再由下一根紅 K 加碼第 2 口，永不超過 2 口。空單案例須方向鏡像。
4. 錯向或未明確斜率、35MA 樣本不足，以及最近 5 根 35MA 幅度小於 10 點時不得進場或加碼；幅度恰等於 10 點須可通過走平過濾。這些過濾不得阻止任何風控平倉。
5. 單口以第 1 口實際成交價、雙口以第 2 口實際成交價驗證多空鏡像的 300 點停損；雙口命中時產生單一全平 workflow，不得只平第 2 口。缺少可靠成交價時安全暫停。
6. 獲利恰達 300 點後才啟動追蹤；最大有利點數可繼續上移，回吐達 30% 時依經確認的整體部位公式全平。部分成交期間以實際剩餘部位維持風控，只有全平完成才重置持倉週期狀態。
7. 08:45、09:45 不建倉，10:45 才可判斷；04:55 全平且不再開倉。涵蓋重啟或斷線錯過 04:55，且同時命中時遵守「04:55／緊急風控、停損、獲利回吐、進場／加碼」優先序。
8. 平倉部分成交、拒單、逾時、斷線與 Unknown 全部暫停，且不反手、不盲目重送；非 flat、存在活動委託或狀態不確定時不得建立反向部位。
9. 手機 App 改倉、其他契約持倉或線上查詢失敗皆由自動核對觸發安全暫停；UI 不提供人工重新查詢或同步按鈕，也不得自動覆寫本機 baseline。
10. 重複 bar、重複 callback、亂序／晚到行情與成交、相同時鐘事件、程序重啟及 crash recovery 不造成重複 decision、intent、closed bar 或委託。
11. UI 切換大台／小台監看行情後，所有交易 decision、intent、委託與部位斷言仍只使用 MXF；其他商品不得被策略下單。

## 除錯日誌需求

- 每次模擬／回放記錄 scenario ID、fixture/version、random seed、虛擬 clock、速度、注入故障、事件序號及 simulator 狀態，使失敗可用同一輸入完全重現。
- 行情 replay 記錄商品 identity、原始 `MatchTime`、解析後 UTC／`Asia/Taipei`、接收 session／序號、資料品質、stale、gap、bar 完整性與 `BarClosed` identity；清楚區分測試 fixture 與本機 recorder 資料來源。
- 模擬 broker 記錄收到的 request、排程的 ack/fill/query response、延遲、重複／亂序設定及斷線點；只使用假資料，並在每筆事件清楚標示 `simulation=true`。
- 測試失敗輸出最後相關事件、狀態機轉移、未滿足 invariant、pending orders／events 與 correlation chain；coverage 或大量成功案例不得輸出無界逐 tick log。

## 品質門檻

Domain/application 測試不得需要元大 DLL；關鍵狀態機採 property/state-transition tests，至少加入「MXF 最大曝險 ≤2」、「非 flat 不可反手」、「資料不完整或不確定時不得增加曝險」、「closed bar／decision／intent／委託 identity 冪等」及「監看商品不改變交易商品」不變量。行情測試須證明事件先落盤再聚合，且不得依賴網路、外部行情或歷史回補。提供一鍵測試指令、測試資料來源與格式說明、coverage 報告及可供客戶簽核的 UAT checklist。真實 API smoke test 必須獨立標記、預設跳過且預設禁止送單。
