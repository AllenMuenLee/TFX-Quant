# tfx-quant {{ version }} — 發行說明

- 發行日期：{{ date }}
- 原始碼版本：`{{ source_revision }}`
- 建置時工作目錄有未提交變更：{{ dirty }}

## 本次變更

- （填寫）

## 已知限制與待確認項目

- 正式環境真實成交的手續費／期交稅是否由元大成交回報帶入尚待確認；確認前一律標示
  `provisional`（`application/trade_reports/fee_model.py` 的 `PROVISIONAL_FEE_MODEL`）。
- `instrument_master.example.json` 中 MXF 的 `order_commodity_code` 仍為空字串，正式
  下單前必須向元大 `FunctionList.xls` 覆核後填入。
- `trading_calendar.example.json` 的假日為網路查詢種子值，正式使用前須以 TAIFEX 官方
  行事曆覆核。

## 升級注意事項

- 安裝程式會在覆蓋程式檔前自動停止執行中的程式、備份
  `%LOCALAPPDATA%\tfx_quant` 下所有 `*.sqlite3` 至 `backup\pre-upgrade-<時間>\`，
  並執行資料完整性檢查；檢查失敗時安裝中止且保留舊版。
- 需要復原時：`runtime\python.exe -m tfx_quant.packaging.migrate --restore-latest`。

## 驗證

- 安裝檔 SHA-256、`build-manifest.json`、原始碼 tag 與本文件版本必須一致
  （見 `docs/acceptance-checklist.md`）。
