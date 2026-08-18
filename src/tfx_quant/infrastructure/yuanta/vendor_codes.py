"""Vendor status/error code tables, transcribed verbatim from the extracted PDFs.

Source: `元大BToCAPI格式.pdf` p.3-4 (`一.委託&回報連線 / 1 連線狀況 TLinkStatus`) for the
trading OCX, and `元大行情API.pdf` p.2-3 (`一.委託&回報連線`) for the quote OCX. Nothing
here is inferred — every code/message pair is copied from the vendor doc. Where the
doc doesn't state whether a code is transient/retriable, this module makes an explicit,
documented judgment call (see the `*_RETRIABLE` sets) rather than leaving it undefined.
"""

from __future__ import annotations

# --- Trading OCX (交易API) TLinkStatus ---------------------------------------------

TRADE_TLINK_SUCCESS = 2
"""lsLogonOK — 登入成功。"""

TRADE_TLINK_IN_PROGRESS: frozenset[int] = frozenset({3, 100, 201, 202})
"""lsConnecting / lsQuerySending / lsLogoning1 / lsLogoning2 — not a terminal result;
the orchestrator keeps waiting for a further `OnLogonS` callback (or the timeout)."""

TRADE_TLINK_RETRIABLE: frozenset[int] = frozenset({-1, -2})
"""lsLinkBroken / lsLinkFail — network-level faults, judged transient and worth an
automatic capped-backoff retry. Every other negative/failure code (auth, credential,
or parameter errors) is judged non-transient and requires operator intervention."""

TRADE_TLINK_MESSAGES: dict[int, str] = {
    -102: "無 API 權限",
    -101: "登入結果訊息有誤",
    -100: "無法執行登入作業：無法針對 L0001 發行訊息",
    -10: "帳號錯誤：此帳號沒有設定 MKTT",
    -9: "作業代號錯誤：找不到指定的作業功能",
    -8: "參數錯誤：Cflg",
    -7: "參數錯誤：Kind",
    -6: "參數錯誤：Stus",
    -5: "參數錯誤：Func",
    -4: "帳號錯誤",
    -3: "參數錯誤",
    -2: "網路連線失敗",
    -1: "網路連線中斷",
    0: "未進行連線",
    2: "登入成功",
    3: "線路連線中",
    4: "無效憑證",
    5: "密碼錯誤",
    100: "連線訊息傳送中",
    201: "線路已連線，正在載入設定",
    202: "線路已連線，登入訊息已送出",
}


def trade_tlink_message(code: int) -> str:
    return TRADE_TLINK_MESSAGES.get(code, f"未知的連線狀態代碼：{code}")


# --- Quote OCX (行情API) TlinkStatus + OnMktStatusChange Msg[0] ---------------------

QUOTE_TLINK_SUCCESS = 2
"""lsLogonOK — 登入成功。"""

QUOTE_TLINK_IN_PROGRESS: frozenset[int] = frozenset({1})
"""lsConnected — LinkReady, but not yet logged in; not a terminal result."""

QUOTE_TLINK_RETRIABLE: frozenset[int] = frozenset({-1, -2})
"""lsLinkBroken / lsLinkFail — network-level faults, same judgment as the trading
OCX above. `Msg[0]` failure codes below (account/credential/permission errors) are all
judged non-transient."""

QUOTE_MSG_CODE_DUPLICATE_LOGIN = "1"
"""UserIDTheSame — the only vendor-documented duplicate-login signal (quote side
only; the trading OCX has no equivalent distinct code — see
`infrastructure/yuanta/README.md`)."""

QUOTE_MSG_CODE_SUCCESS = "0"

QUOTE_MSG_CODE_MESSAGES: dict[str, str] = {
    "0": "Success",
    "1": "帳號已於他處登入 (UserIDTheSame)",
    "2": "使用者 ID 錯誤 (UserIDInvalid)",
    "3": "無權限",
    "4": "超過登入上限",
    "5": "認證連結失敗",
    "6": "密碼錯誤",
    "7": "電文停止使用",
    "A": "第一次使用，請變更密碼",
    "B": "密碼輸入錯誤，請重新輸入",
    "C": "密碼錯誤次數超過限制，請聯絡客服人員",
    "D": "非電子交易戶，無法使用本功能",
    "E": "查無此帳號資料",
    "F": "查無密碼資料",
    "G": "查無可使用帳號資料",
    "H": "尚未完成密碼歸戶作業",
    "I": "查無客戶憑證序號資料",
    "J": "需查詢8802TABLE",
    "K": "分公司代碼錯誤",
    "L": "市場別代號錯誤",
    "M": "帳號別代號錯誤",
    "N": "券商代號錯誤",
    "O": "集保帳號錯誤",
    "P": "身分證字號錯誤",
    "Q": "交易系統通路代號錯誤",
    "R": "作業代號錯誤",
    "S": "失敗",
    "X": "未知錯誤",
}


def quote_msg_message(msg: str) -> str:
    code = msg[0] if msg else "X"
    return QUOTE_MSG_CODE_MESSAGES.get(code, f"未知的訊息代碼：{code!r}")


# --- Quote OCX AddMktReg/DelMktReg RegErrCode ---------------------------------------

REG_ERR_SUCCESS = 0

REG_ERR_MESSAGES: dict[int, str] = {
    0: "註冊成功",
    1: "註冊商品錯誤（長度 <4 或 >13）",
    2: "註冊方式錯誤",
    3: "連線未完成",
}


def reg_err_message(code: int) -> str:
    return REG_ERR_MESSAGES.get(code, f"未知的註冊錯誤代碼：{code}")


# --- CLSIDs/IIDs, verified directly from each shipped .ocx's embedded type library --

# Extracted this session via `comtypes.client.GetModule(ocx_path)` against the actual
# vendor .ocx files (not COM-registered on this dev machine, but `GetModule` reads the
# type library resource straight from the file, no registration required) — these are
# exact, not inferred from the PDFs.

TRADE_PROGID_32BIT_CLSID = "{70AA5E9A-4564-4C29-9403-4407FE8A7358}"
TRADE_PROGID_32BIT_DISPATCH_IID = "{7AC14464-3996-4402-8CF9-1514159E157E}"
TRADE_PROGID_32BIT_EVENTS_IID = "{90467C91-11D5-4AE4-BE11-5052E55869C8}"

TRADE_PROGID_64BIT_CLSID = "{361303F7-8986-4DAC-9887-3E9FDA110FB9}"
TRADE_PROGID_64BIT_DISPATCH_IID = "{CB868718-B94E-4259-991F-D1569A70433E}"
TRADE_PROGID_64BIT_EVENTS_IID = "{32B3B217-8408-41FD-8922-B22B3DAD2474}"

QUOTE_PROGID_CLSID = "{8E7FB42A-1137-467E-98C6-830C9B02EA82}"
QUOTE_PROGID_DISPATCH_IID = "{48BD1D0C-21D8-4B72-8850-9909A7D8C205}"
QUOTE_PROGID_EVENTS_IID = "{E49166B8-9C1F-442E-A538-CD98E80CFCB1}"

# --- IMPORTANT: the quote OCX's *live* type library disagrees with 元大行情API.pdf --
#
# This session generated Python bindings straight from `YuantaQuote_v2.1.2.9.ocx`'s
# embedded type library and found it does NOT match the PDF for several members —
# the shipped binary is evidently a newer/extended build than what the PDF documents.
# Confirmed, exact (from the type library, not the PDF):
#
#   SetMktLogon(user: BSTR, pass: BSTR, ip: BSTR, port: BSTR, ReqType: int,
#               SetMap: int) -> None
#       -- PDF only documents 4 params (User, pass, IP, PORT); the live control
#          requires two more, `ReqType`/`SetMap`, with no description anywhere in the
#          extracted PDF.
#   AddMktReg(symbol: BSTR, updmode: BSTR, ReqType: int, SetMap: int) -> int
#       -- PDF documents `UpdateMode` as an int (1/2/4); the live dispinterface
#          marshals it as BSTR (pass the stringified code, e.g. "1"). Also gained
#          ReqType/SetMap.
#   DelMktReg(symbol: BSTR, ReqType: int) -> int
#   OnMktStatusChange(Status: int, Msg: BSTR, ReqType: int)
#   OnRegError(symbol: BSTR, updmode: int, ErrCode: int, ReqType: int)
#   OnGetMktAll(..., ReqType: int)  -- same 19 fields as the PDF, plus a trailing
#       ReqType.
#
# `ReqType`/`SetMap` are not documented anywhere in the extracted PDF or docx — this
# adapter passes 0 for both (see `quote_ocx_adapter.py`) as the least-surprising
# default. **This is an explicit, flagged gap, not a guess dressed up as fact**: their
# real meaning needs confirming against current vendor support/documentation before
# production use. Regenerate bindings from the installed .ocx
# (`comtypes.client.GetModule(path)`) rather than trusting this file or the PDF
# blindly if the vendor ships a newer build.
QUOTE_REQ_TYPE_DEFAULT = 0
QUOTE_SET_MAP_DEFAULT = 0
