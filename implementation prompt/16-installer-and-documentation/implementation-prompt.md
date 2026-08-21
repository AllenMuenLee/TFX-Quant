# Feature 16 — Windows Installer, Source Delivery, and Documentation

> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述官方文件依據；文件未明載者須標成待確認並隔離於 adapter，不得自行補造。**

> 強制使用 Python 開發；安裝檔須封裝固定 Python runtime 與依賴，使用者不應另外手動配置開發環境。

## 任務

交付可重複建置的 Windows 安裝檔、完整原始碼與繁體中文操作／維護文件。安裝方式必須符合元大 API 的位元數、runtime、COM/DLL 註冊及授權限制；不可把無再散布權的券商元件直接打包。

## 安裝與升級

- 建立有版本號與數位簽章支援的 installer，檢查 Windows 版本、.NET/runtime、元大 API、位元數、磁碟空間與權限。
- 安裝應建立資料、log、備份與設定目錄並套用最小權限；不得預填帳密。提供安全解除安裝，預設保留交易資料並讓使用者明確選擇是否移除。
- 升級前停止程式、備份資料庫、驗證 migration；失敗時保留可復原版本與清楚訊息。
- 提供 build/release script、鎖定依賴版本、第三方授權清單、checksum 與 release notes，使乾淨環境可重建相同產物。

## 文件

- 安裝手冊：元大 API 前置作業、帳號／憑證、模擬與正式環境、常見安裝錯誤。
- 操作手冊：登入、選商品契約、啟動／暫停／停止、畫面欄位、報表、正常關閉。
- 安全 runbook：部分成交、拒單、Unknown、斷線、持倉不一致、人工同步、04:55 未平與緊急平倉的逐步處置。
- 策略規格：60 分 K 切點、紅黑定義、加碼歧義的最終選項、禁止建倉時間、反手 gate、每日重置。
- 維護文件：架構、資料庫、設定、log、備份還原、API 版本矩陣、建置測試與故障排查。

## 除錯日誌需求

- installer／updater 記錄 package/app version、OS/architecture、前置檢查結果、元大 API readiness、權限、磁碟空間、每一步驟、exit code 與 rollback 結果；帳密、授權資料及完整使用者路徑須遮蔽。
- build/release 記錄 source revision、鎖定依賴、工具版本、artifact checksum、簽章驗證及產物對應關係，讓客戶現場版本可與原始碼及 symbols 精確比對。
- 文件須列出 log 位置、事件／correlation ID 查找方式、診斷包內容、各常見故障應擷取的事件及安全匯出步驟；診斷包預設排除憑證、秘密與完整帳號。

## 驗收

在乾淨且符合條件的 Windows VM 完成安裝、首次啟動、mock UAT、升級與解除安裝。核對安裝檔、原始碼 tag、symbols、文件與 checksum 版本一致。安排交付演練：使用者能依 runbook 處理斷線、未知委託、手機 App 持倉差異及緊急平倉，且文件不得暗示軟體可取代券商端人工確認。
