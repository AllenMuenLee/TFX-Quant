# Feature 04 Extension — Two-Month Bar History

> 本 prompt 獨立於同資料夾的行情與 60 分 K implementation prompt；只實作已收 K 棒的持久化、載入、查詢及兩個月保存規則。

## 任務

將軟體根據自己實際收到的元大即時成交行情聚合、且已確定收盤的每一根 60 分 K 持久化，使使用者可查閱軟體過去兩個月自行收錄的 K 棒，不可只依賴記憶體中的 recent-bars deque。

元大 API 不提供歷史價格或歷史 K 棒查詢，因此本功能不實作遠端歷史回補，也不承諾在首次啟用時已有兩個月資料。可查詢範圍只涵蓋本軟體實際運行、收到有效即時行情並成功完成聚合與落盤的期間。

## 必須實作

- 「過去兩個月」是以 `Asia/Taipei` 現在時間往前推兩個曆月的滾動區間且包含邊界；例如 8 月 18 日的起點是 6 月 18 日。不得以固定 60 天取代，也不得把夜盤跨午夜錯歸曆日。
- 每筆保存商品、契約月份、週期、交易日、session、`start`、`end`、OHLCV、資料來源、完整性／gap、建立及更新時間。資料來源固定標示為本軟體由元大即時行情聚合，不得偽裝成元大官方歷史 K 棒。價格使用精確 decimal，時間必須可無歧義還原 `Asia/Taipei`。
- 以商品、契約、週期及 `start` 等欄位建立唯一 bar identity 與 database unique constraint／upsert；重複 `BarClosed`、程序重啟後的本機事件重播不得建立重複資料。
- 只有已確認收盤且通過驗證的 K 棒可寫入；forming bar 不得冒充 closed bar。晚到 tick 不得默默改寫已保存的 closed bar；若系統因修復程序更正本機聚合結果，須保存修訂時間與 audit，並重新評估 gap／訊號。
- `BarClosed` 流程可靠寫入 repository，但不得阻塞行情 callback。資料庫不可用時使用有界重試／待寫佇列並告警；無法保證落盤時 readiness 顯示 degraded。
- 啟動、重連或切換契約時，只從本機 repository 載入該商品／契約最近兩個月自行收錄的資料，依 identity 去重並依 `start` 排序。這些資料可供 UI、紅黑 K 計數及策略 warm-up，但只有通過交易日曆與連續性檢查的區段可驅動訊號。
- 禁止呼叫、設計或假設存在元大歷史行情／K 棒查詢 API；也不得用第三方行情、人工匯入、前收價、前一根 close 或合成 tick 補造缺少的 K 棒。軟體未運行、未訂閱該契約、斷線、stale 或無法確認完整性的期間一律保留為 gap，並顯示實際收錄範圍。
- 首次啟用時歷史資料為空；之後只隨軟體收到即時行情而逐步累積，最多提供滾動兩個曆月。即使累積未滿兩個月也屬正常狀態，UI 必須顯示「自 YYYY-MM-DD HH:mm 起開始收錄」及缺漏區段，不得顯示成完整兩個月。
- 啟動及每日交易日切換後執行 retention cleanup，只刪除早於兩個曆月起點的 K 棒。清理須限定資料範圍、可測試並產生 audit 摘要，不得刪除委託、成交、持倉或稽核資料。
- UI 可依商品、契約及日期區間瀏覽資料，並顯示來源、完整性、最早與最晚可用時間；不足兩個月時必須明確提示。
- 如果兩個月內的資料並沒有全部存在local的資料庫，用 https://www.yuanta.com.tw/file-repository/content/sparkapi_docs/%E8%A1%8C%E6%83%85/%E8%A1%8C%E6%83%85%E5%A0%B1%E5%83%B9%E8%A1%A8%E8%A8%82%E9%96%B1/index.html 的api查詢歷史價格，並且寫入local資料庫

## 除錯日誌需求

- 記錄 `bar_persist_requested/result`、bar identity、insert／upsert／duplicate／revision 判定、queue depth、重試次數、資料庫耗時及完整性狀態；不得在行情 callback 內同步輸出大量 payload。
- 載入與缺口處理須記錄商品契約、查詢區間、本機／官方來源、取得筆數、去重數、gap／重疊／OHLCV 衝突及是否允許策略 warm-up；遠端能力不可用時明確記錄依據與保留 gap 的結果。
- retention cleanup 須記錄 cutoff、限定條件、掃描／刪除筆數、transaction 結果及 audit ID；寫入或清理失敗須記錄 readiness degraded 與是否阻擋訊號。

## 驗收

- 固定 clock 測試月底、跨年、閏年及不同月份天數的兩個曆月邊界，並驗證夜盤跨午夜的交易日與 session。
- 重複事件、程序重啟及歷史資料重播後，相同 identity 仍只有一筆且查詢順序穩定。
- 重啟後可還原最近兩個月 K 棒與紅／黑／十字狀態；邊界外資料被清理，邊界內不受影響。
- 模擬寫入失敗、部分批次失敗及重試成功，確認 callback 不阻塞、失敗可觀測、最終不重複且不靜默遺失。
- 首次啟用時 repository 為空，系統不得嘗試向元大查詢過去資料；收到即時 tick 並完成第一根 60 分 K 後，才出現第一筆歷史紀錄。
- 模擬本機既有紀錄與新產生 K 棒的 gap、重疊及 OHLCV 衝突；一致性確認前不得解除 gap 或產生交易訊號。
- 軟體關閉、斷線或未訂閱期間不產生任何 K 棒；重新啟動後 UI 顯示真實可用範圍及 gap，不以空白、第三方資料或合成 K 棒補足。
> **API 文件唯一來源：實作前必須直接讀取[元大 SPARK API 官方入口](https://www.yuanta.com.tw/file-repository/content/API/page/index.html)及其下方連結的 API 說明文件、範例、元件下載與換版資訊。不得使用專案內既有資料夾、舊 SDK 文件、舊範例或既有程式碼反推 API 規格；若與本 prompt 其他描述衝突，以官方線上文件當下內容為準。**
> **歷史資料能力不得臆測：是否能取得歷史價格、當日分時或歷史 K 棒，以官方線上文件當下明載且期貨商品可用的能力為準；不得沿用既有 prompt 對舊 API 能力的肯定或否定。官方 API 與本機資料皆無法可靠覆蓋的期間保留為 gap。**
