# 第三方套件授權清單

安裝檔內含固定版本的 32 位元 CPython 3.11 執行環境與下列相依套件。每次建置會由
`installer/build.py` 產生一份對應該次版本的 `third_party_licenses.txt`，隨安裝檔一起
交付（位於安裝目錄）。本文件為概覽；以該次交付的 `third_party_licenses.txt` 與各套件
`*.dist-info` 內的授權檔為準。

## 直接相依（`pyproject.toml [project].dependencies` / `installer/requirements.in`）

| 套件 | 授權 |
|---|---|
| pydantic | MIT |
| tzdata | Apache-2.0 |
| wxPython | wxWindows Library Licence（LGPL 衍生，含連結例外） |
| comtypes | MIT |
| keyring | MIT |

## 間接相依（由上述套件帶入，釘死於 `installer/requirements.lock`）

annotated-types (MIT)、backports.tarfile (MIT)、importlib_metadata (Apache-2.0)、
jaraco.classes / jaraco.context / jaraco.functools (MIT)、more-itertools (MIT)、
numpy (BSD-3-Clause，由 wxPython 帶入)、pydantic_core (MIT)、pywin32-ctypes
(BSD-3-Clause)、typing_extensions (PSF)、typing-inspection (MIT)、zipp (MIT)。

## 直譯器

CPython 3.11（Python Software Foundation License）— python.org 的
`python-3.11.x-embed-win32.zip`，SHA-256 釘死於
`src/tfx_quant/packaging/build_support.py` 並於每次建置驗證。

## 不隨本專案散布的元件

元大期貨**交易 API**（`YuantaOrd.ocx`、`YuantaOrdLib.dll`、`YuantaCAPIDLL.dll` 等）與
**行情 API**（`YuantaQuote_v2.1.2.9.ocx`）為元大提供的專有元件，**無再散布權**，
不包含在安裝檔內，由客戶依[安裝手冊](installation-manual.md)另行向元大取得並安裝。
