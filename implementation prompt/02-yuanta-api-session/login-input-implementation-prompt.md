# Feature 02 Extension — Yuanta API Login Input

> 本 prompt 獨立於同資料夾的 session implementation prompt；只實作使用者登入資料輸入、安全保存及登入參數映射，不重做完整 session lifecycle。

## 任務

依本機文件中的元大期貨交易 API 規則，提供「元大期貨 API 登入」畫面，讓本機操作人員輸入交易 API 所需登入資料，不必先手動設定環境變數或另外開啟 Windows 認證管理員。yfinance 不得共用或接收券商帳密。

## 必須實作

- 表單包含執行環境（測試／正式，預設測試）、元大歸戶 ID、登入密碼、「記住歸戶 ID」及「安全儲存密碼」。正式環境送出前須再次清楚確認。
- 密碼欄提供顯示／隱藏切換；不得把密碼寫入 log、錯誤、遙測、crash dump、JSON、`.env`、資料庫或原始碼。勾選安全儲存時僅使用 Windows Credential Manager／DPAPI；未勾選時只存在本次登入所需的記憶體，並在登出或關閉時清除。
- 第一次登入前不要求使用者猜填期貨帳號。登入成功後解析 `OnLogonS.AccList`，顯示分公司代號、帳號及子帳號；一組可自動選取，多組必須明確選擇。記住的帳號仍須存在於本次回傳清單，不接受任意輸入。
- 登入 request 與元大 API 的方法、欄位、憑證、帳號格式及 PROD／UAT 選擇，必須逐項依 `交易API元件及說明文件/` 內的 API 說明與 Python 範例映射；不得保留或沿用舊 SPARK API 的方法名稱、主機、port、ProgID、CLSID 或其他常數。
- 官方文件列出的環境、連線與元件設定由 adapter 管理；一般使用者只輸入官方登入流程要求的資料，不得要求使用者猜填未由官方文件定義的技術參數。
- 必要欄位不可空白；去除 ID 首尾空白但不得改寫密碼；禁止重複送出；連線中鎖定會改變登入語意的欄位。
- UI 建立登入 request/credentials DTO 並交給 session/application service；UI 不得直接呼叫 OCX，domain/application 層不得依賴 wx、COM、keyring 或 Windows API。保留可注入的 `CredentialSource`。

## 除錯日誌需求

- 記錄 `login_form_opened`、欄位驗證結果、環境選擇、提交／防重複提交、登入結果、`AccList` 筆數、帳號選擇與憑證保存／清除結果；只記錄欄位是否提供及帳號遮罩。
- 正式環境確認須記錄 user-action audit、確認時間與結果；不得記錄歸戶 ID 明文、完整帳號、密碼、憑證內容、控制項文字快照或 request 的敏感 `repr`。
- 登入參數映射失敗須記錄 adapter 欄位名稱、文件版本／查閱日期、失敗階段與錯誤碼，但任何值在輸出前都必須通過敏感欄位遮蔽測試。

## 驗收

- 環境與時段選擇會產生正確且經文件確認的 endpoint；一般使用者無須填 IP、port、`ReqType` 或 `SetMap`。
- ID／密碼只在記憶體中傳給期貨交易 adapter；不得傳給 yfinance adapter。畫面狀態、`repr`、log、事件及例外均不含明文密碼。
- 未選安全儲存時不持久化；選擇時僅寫入 Windows Credential Manager／DPAPI，且 UI 可清除已存憑證。
- 空白欄位、錯誤密碼、重複登入、逾時及他處登入均有可操作的中文訊息。
- 模擬零組、一組及多組 `AccList`：零組不得 session-ready；一組可自動選取；多組確認前不得查詢、訂閱或啟動策略。
- UI 測試不得使用真實帳密；真實 smoke test 必須明確 opt-in、預設測試環境且禁止送單，輸出不得包含歸戶 ID、完整帳號或密碼。
> **API 文件唯一來源：實作前必須直接讀取專案根目錄 [`交易API元件及說明文件/`](../../交易API元件及說明文件/) 內的元大期貨交易 API 說明、Python 範例、元件與版本資訊。禁止使用 SPARK API 網站、舊 SPARK SDK、舊範例或既有 SPARK 程式碼反推 API；若與本 prompt 其他描述衝突，以該資料夾內文件為準。資料夾缺少、文件未明載或內容矛盾時，須停止相關實作並列為 blocker。市場價格、行情與 OHLCV 不屬於此交易 API 規格，一律使用 `yfinance`。**
> **不得臆測：API 名稱、參數、回傳值、事件、錯誤碼、登入方式、環境、平台、位元數與能力都必須有上述本機文件依據；既有 prompt 中的舊方法名稱、主機、port、ProgID、CLSID 或其他常數一律不得沿用，除非 `交易API元件及說明文件/` 仍明載。**
