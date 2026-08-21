# Feature 15 — Simulator, Replay, and Automated Acceptance Tests

> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；使用 pytest 建立 broker simulator、虛擬時鐘、回放與故障注入測試。

## 任務

建立完全不連真實帳號的 broker simulator、虛擬時鐘與行情 replay harness，讓所有策略與安全案例可重現。測試環境必須在視覺及設定上與正式環境明確區分，避免誤送真單。

## 模擬能力

- 播放 tick、歷史 K 棒、交易日與時鐘；可控制速度、暫停及跳到指定時間。
- 分別模擬期貨 API 的登入、送單、ack、部分／全部成交、拒單、取消、回報重複／亂序／延遲、查詢矛盾與通道斷線，以及 yfinance 的空資料、延遲、stale、rate limit、schema 變更與 ticker mapping 失敗。
- 模擬手機 App 改變持倉，以及程式在 workflow 任意持久化點崩潰與重啟。
- 使用與正式 adapter 相同的 application interface，不得在策略內加入 `if simulator` 分支。

## 必備驗收情境

1. 兩紅 K 空手建多 1 口，符合設定後加至 2，永不超過 2。
2. 兩黑 K 平多 2；只有全平成交及查詢為 0 後才空 1。
3. 空單鏡像案例。
4. 08:45、09:45 不建倉，10:45 可判斷；04:55 全平且不再開倉。
5. 平倉部分成交、拒單、逾時、斷線與 Unknown 全部暫停且不反手、不重送。
6. 手機 App 改倉觸發暫停；確認同步後採元大實際持倉、重置訊號並保持待人工重新啟動。
7. 重複 bar、重複 callback、晚到成交與 crash recovery 不造成重複委託。

## 除錯日誌需求

- 每次模擬／回放記錄 scenario ID、fixture/version、random seed、虛擬 clock、速度、注入故障、事件序號及 simulator 狀態，使失敗可用同一輸入完全重現。
- 模擬 broker 記錄收到的 request、排程的 ack/fill/query response、延遲、重複／亂序設定及斷線點；只使用假資料，並在每筆事件清楚標示 `simulation=true`。
- 測試失敗輸出最後相關事件、狀態機轉移、未滿足 invariant、pending orders／events 與 correlation chain；coverage 或大量成功案例不得輸出無界逐 tick log。

## 品質門檻

Domain/application 測試不得需要元大 DLL；關鍵狀態機採 property/state-transition tests，加入「最大曝險 ≤2」及「非 flat 不可反手」不變量。提供一鍵測試指令、測試資料說明、coverage 報告與一份可供客戶簽核的 UAT checklist。真實 API smoke test 必須獨立標記、預設跳過且預設禁止送單。
