# Feature 15 — Simulator, Replay, and Automated Acceptance Tests

> **平台硬性限制：整個系統只能使用 x32；不得使用 x86 或 x64。原因是行情 API 僅提供 x32 版本。所有開發、相依套件、執行環境、測試、建置與部署均須遵守此限制。**

> 強制使用 Python 開發；使用 pytest 建立 broker simulator、虛擬時鐘、回放與故障注入測試。

## 任務

建立完全不連真實帳號的 broker simulator、虛擬時鐘與行情 replay harness，讓所有策略與安全案例可重現。測試環境必須在視覺及設定上與正式環境明確區分，避免誤送真單。

## 模擬能力

- 播放 tick、歷史 K 棒、交易日與時鐘；可控制速度、暫停及跳到指定時間。
- 模擬登入、行情、送單、ack、部分／全部成交、拒單、取消、回報重複／亂序／延遲、查詢矛盾與各通道斷線。
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

## 品質門檻

Domain/application 測試不得需要元大 DLL；關鍵狀態機採 property/state-transition tests，加入「最大曝險 ≤2」及「非 flat 不可反手」不變量。提供一鍵測試指令、測試資料說明、coverage 報告與一份可供客戶簽核的 UAT checklist。真實 API smoke test 必須獨立標記、預設跳過且預設禁止送單。
