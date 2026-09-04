# 安裝手冊

## 1. 系統需求

| 項目 | 需求 |
|---|---|
| 作業系統 | Windows 10 或 Windows 11（32 或 64 位元皆可） |
| 權限 | 安裝本程式**不需要**系統管理員權限（安裝至使用者目錄）。安裝元大 API 元件時**需要**系統管理員權限。 |
| 磁碟空間 | 至少 400 MB 可用空間 |
| Python | 免另外安裝。安裝檔內含固定版本的 32 位元 Python 3.11 執行環境與所有相依套件。 |
| Visual C++ | Microsoft Visual C++ 2015–2022 可轉散發套件（x86）。元大 API 元件也需要，通常由元大安裝包附的 `vcredist_x86.exe` 提供。 |
| 網路 | 需能連線至元大交易主機與行情主機（見第 2 節）。 |

本系統的所有程式碼與 UI 均以 **32 位元**執行，因為元大行情 OCX（`YuantaQuote_v2.1.2.9.ocx`）
沒有 64 位元版本。這是硬性限制，安裝檔已處理，操作人員不需要自行安裝 Python。

## 2. 元大 API 前置作業

元大提供兩套獨立的 API（交易、行情），需分別向元大**申請使用權限**。

**安裝檔有兩種版本：**

- **內含元件版**（`build-manifest.json` 的 `vendor_bundle` 非 null）：安裝過程可自動把
  元大交易／行情 OCX 複製到 `C:\Yuanta\API` / `C:\Yuanta\QAPI`、以系統管理員註冊，
  並安裝 Microsoft VC++ x86 執行環境（見第 3 節的「元件安裝」勾選項，會出現一次 UAC
  提示）。此版本內含元大專有元件，僅在發行者具備元大再散布授權時方可交付。
- **純程式版**（`vendor_bundle` 為 null）：**不含**元大元件，須依下方步驟自行安裝。

無論哪個版本，**API 使用權限仍須自行向元大申請**。下列為自行安裝步驟
（純程式版必須；內含元件版若未勾選自動安裝時亦適用）。

### 2.1 交易 API（下單、帳戶、成交、持倉、查詢）

1. 向元大期貨申請「元大期貨 API（BToC API）」使用權限。
2. 取得元件資料夾後，將 **32 位元**版（`API` 資料夾）整包複製到 `C:\Yuanta\API`。
   - 安裝完成後**不可**再搬動資料夾內任何檔案，否則 API 會執行異常。
3. 以**系統管理員**身分執行 `C:\Yuanta\API\install_YTFutOrdAP.bat` 完成 OCX 註冊。
4. 連線位置（由 API 自動使用，供防火牆設定參考）：
   - 測試主機：`apitest.yuantafutures.com.tw` Port 80
   - 正式主機：`api.yuantafutures.com.tw` Port 80 / 443

### 2.2 行情 API（即時行情、60 分 K 來源）

1. 另外向元大申請「行情 API」使用權限（一般 API 申請預設只開放交易 API）。
2. 將行情元件資料夾（`QAPI`）整包複製到 `C:\Yuanta\QAPI`。
3. 以**系統管理員**身分執行 `C:\Yuanta\QAPI\install_ytocx.bat` 完成 OCX 註冊。
4. 連線位置：
   - Domain：`apiquote.yuantafutures.com.tw`
   - T 盤 Port：80 或 443；T+1 盤 Port：82 或 442

### 2.3 憑證

- 交易帳號為期貨帳號，格式為 `F` 開頭（例：`F` + 分公司代號 + 帳號）。
- 元大登入需要憑證匯入至本機使用者的憑證存放區。
- 本程式的登入視窗提供「憑證匯入」欄位（憑證檔路徑、憑證密碼、匯入按鈕），
  會以 Windows 內建 `certutil -importpfx -user` 將 `.pfx` 匯入目前使用者的個人憑證存放區
  （不需系統管理員權限）。也可事先自行匯入。
- 憑證密碼只會存入 Windows 認證管理員（DPAPI 保護），不會寫入任何設定檔或 log。

### 2.4 執行環境（模擬 / 正式）

由設定檔的 `environment` 決定，沒有任何 `--mock` / `--uat` 旗標：

| 值 | 交易 | 行情 |
|---|---|---|
| `TEST` | 本機模擬下單，**不對任何伺服器送出任何委託** | 元大即時真實行情（正常登入、訂閱、落盤） |
| `PRODUCTION` | 元大正式交易主機 | 元大即時真實行情 |

- 交易 API 已無獨立測試主機。`TEST` 環境用本機模擬器取代，並以 fail-closed 保證
  測試環境不可能誤送真單。
- `TEST` 環境啟動時**不會**要求或接受任何交易 API 登入資料（模擬器只認保留帳號
  `TEST-SIMULATION`）；只需要輸入行情登入帳密。

## 3. 安裝本程式

有兩種安裝方式，擇一即可：

- **安裝檔**：執行 `tfx-quant-setup.exe`（見下方步驟）。
- **資料夾 + 指令碼**：把整個 bundle 資料夾複製到目標電腦，執行 `install-all.bat`
  （可直接雙擊）。此指令碼會安裝程式、建立資料目錄與捷徑，並（內含元件版）自動安裝
  VC++ 與元大 OCX（一次 UAC）。參數：`-DependenciesOnly` 只裝相依元件；`-AppOnly`
  只裝程式；`-DryRun` 只顯示不執行；`-Uninstall [-RemoveData] [-RemoveYuanta]`
  解除安裝（預設保留資料與 `C:\Yuanta`）。

### 3.a 使用 `tfx-quant-setup.exe`

1. 執行 `tfx-quant-setup.exe`。
2. 安裝程式會先檢查 Windows 版本與磁碟空間（不足才中止）。
3. 預設安裝至使用者目錄（`%LOCALAPPDATA%\Programs\tfx-quant`），不需系統管理員權限。
4. **元件安裝（僅內含元件版）**：安裝選項會有一項
   「安裝並註冊元大 API 元件與 Microsoft VC++ x86 執行環境」，預設**勾選**。
   - 勾選時，安裝結束前會出現**一次 UAC 提示**；同意後自動安裝 VC++ 執行環境、
     複製元大 OCX 至 `C:\Yuanta` 並註冊。過程記錄於
     `%LOCALAPPDATA%\tfx_quant\logs\vendor-install-*.log`。
   - 取消 UAC 或取消勾選時，元件仍留在 `<安裝目錄>\vendor\`，可稍後以系統管理員執行
     `<安裝目錄>\install-all.bat -DependenciesOnly`，或依第 2 節手動安裝。程式仍可先以
     「模擬」環境啟動。
5. 安裝程式會建立下列資料目錄（最小權限，僅目前使用者）：
   - `%LOCALAPPDATA%\tfx_quant\config` — 設定檔
   - `%LOCALAPPDATA%\tfx_quant\logs` — 應用程式與安裝／升級 log、audit 資料庫
   - `%LOCALAPPDATA%\tfx_quant\backup` — 升級前資料庫備份
   - `%LOCALAPPDATA%\tfx_quant\data` — 保留
6. **安裝程式不會預先填入任何帳號或密碼。**

### 3.1 設定檔

首次啟動前，於 `%LOCALAPPDATA%\tfx_quant\config\settings.json` 建立設定檔（可複製
程式目錄 `src\tfx_quant\desktop\settings.example.json` 修改）。若該檔不存在，程式會使用
內建範例設定啟動。

必填／受檢欄位（`validate_startup` 會在啟動時嚴格驗證，錯誤即中止並顯示原因）：

| 欄位 | 說明 |
|---|---|
| `account_alias` | 非機密的帳號別名（例：`primary`），不是真正的帳號 |
| `environment` | `TEST` 或 `PRODUCTION` |
| `selected_instrument` | 初始行情監看商品：`MXF` 或 `TXF`（交易一律 MXF） |
| `contract_selection_mode` | `AUTO`（近月自動解析） |
| `timezone_id` | 必須為 `Asia/Taipei` |
| `eod_flatten_local_time` | 必須為 `04:55:00` |
| `max_net_lots` | 1–2，硬上限 2 |

## 4. 升級

1. 執行新版安裝檔即可。安裝程式會偵測既有版本並自動：
   - 透過 `AppMutex` 要求關閉執行中的程式；
   - 在覆蓋任何檔案前，用**舊版**內建的 Python 執行
     `python -m tfx_quant.packaging.migrate --apply`：
     對 `%LOCALAPPDATA%\tfx_quant` 下所有 `*.sqlite3` 做完整性檢查，並整份複製到
     `backup\pre-upgrade-<時間>\`；
   - 檢查失敗（資料庫毀損或版本比本程式新）時**中止升級**，舊版與資料保持不變，
     並於安裝 log 留下清楚訊息。
2. 手動復原：
   ```
   "<安裝目錄>\runtime\python.exe" -m tfx_quant.packaging.migrate --restore-latest
   ```

## 5. 解除安裝

- 從「設定 → 應用程式」或開始功能表解除安裝。
- **預設保留**所有交易資料、log、備份與設定（`%LOCALAPPDATA%\tfx_quant`）。
- 互動式解除安裝會詢問是否一併刪除該目錄，預設為「否」；選「是」才會刪除。
- **靜默解除安裝（`/VERYSILENT`）永遠不會刪除該目錄。** 若確實要在靜默模式一併移除，
  必須明確加上 `/REMOVEUSERDATA` 參數。
- **`C:\Yuanta`（元大元件）與其 OCX 註冊不會被移除**，以免影響其他使用同一元件的工具。
  要移除時，以系統管理員執行 `C:\Yuanta\API\uninstall_YTFutOrdAP.bat` 與
  `C:\Yuanta\QAPI\uninstall_ytocx.bat`。

## 6. 常見安裝錯誤

| 現象 | 原因與處置 |
|---|---|
| 啟動後行情或交易顯示「OCX 未建立 / ProgID 未註冊」 | 未以系統管理員執行 `install_YTFutOrdAP.bat` / `install_ytocx.bat`。以系統管理員重新執行後重開程式。ProgID 應為 `Yuanta.YuantaOrdCtrl.1`（交易）、`YUANTAQUOTE.YuantaQuoteCtrl.1`（行情）。 |
| 「元大行情 OCX 為 32 位元；請以 32 位元直譯器執行」 | 不會發生於安裝版（內建 32 位元 Python）。若從原始碼執行，請用 32 位元 Python 3.11。 |
| OCX 建立時 `E_UNEXPECTED` / 找不到相依 DLL | 缺 VC++ 2015–2022 x86 執行環境。安裝 `vcredist_x86.exe`（元大資料夾內或 Microsoft 官網）。 |
| 登入回報 `OnLogonS` TLinkStatus = 4 | 憑證錯誤（CA error）。確認憑證已匯入目前使用者的個人存放區，且為有效期內的元大憑證。 |
| 登入回報 `OnLogonS` TLinkStatus = 5 | 密碼錯誤（PassError）。 |
| 登入回報 `OnLogonS` TLinkStatus = -1 | 連線中斷（lsLinkBroken）。檢查網路與防火牆對第 2 節主機／Port 的放行。 |
| 交易 API 測試環境完全無回應 | 元大交易 API 已無測試主機。請改用 `environment: TEST`（本機模擬器 + 真實行情），或使用正式環境登入。 |
| 升級被中止，訊息提到完整性檢查失敗 | 舊資料庫毀損或版本較新。備份位於 `%LOCALAPPDATA%\tfx_quant\backup`；提供該處與安裝 log 給維護人員。舊版仍可正常使用。 |
