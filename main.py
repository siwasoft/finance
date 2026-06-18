import json
import os
import time
from datetime import datetime, timezone, timedelta

import gspread
import yfinance as yf
import pandas as pd
from google.oauth2.service_account import Credentials
from pykrx import stock as krx

KST = timezone(timedelta(hours=9))
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SETUP_TAB = "setting"
DATA_LOG_TAB = "Data_Log"
ERROR_LOG_TAB = "Error_Log"

SETUP_HEADERS = ["ticker", "종목명", "수집여부(Y/N)", "비고"]
DATA_LOG_HEADERS = [
    "기준일자", "종목코드", "종목명", "현재가", "등락(금액)", "등락률", "거래량",
    "고가", "저가", "전일 종가", "per", "시가총액", "52주 신고가", "52주 신저가",
    "매출액", "손이익", "부채총계", "영업현금흐름", "수집일시", "상태"
]
ERROR_LOG_HEADERS = ["발생일시", "ticker", "에러내용"]


def get_gspread_client() -> gspread.Client:
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    url = os.environ["SPREADSHEET_URL"]
    return client.open_by_url(url)


def get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, title: str, headers: list) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(title)
        print(f"  탭 확인: '{title}' 존재")
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        print(f"  탭 생성: '{title}' (헤더 자동 추가)")
    return ws


def normalize_ticker(ticker: str) -> str:
    if ticker.isdigit() and len(ticker) <= 6:
        return ticker.zfill(6)
    return ticker


def load_tickers(spreadsheet: gspread.Spreadsheet) -> list[dict]:
    ws = get_or_create_worksheet(spreadsheet, SETUP_TAB, SETUP_HEADERS)
    rows = ws.get_all_records()
    tickers = [
        {
            "ticker": normalize_ticker(str(r["ticker"]).strip()),
            "종목명": str(r["종목명"]).strip(),
        }
        for r in rows
        if str(r.get("수집여부(Y/N)", "")).strip().upper() == "Y"
    ]
    if not tickers:
        print(f"  [안내] '{SETUP_TAB}' 탭에 수집여부(Y/N)=Y 인 종목이 없습니다. 종목을 등록해 주세요.")
    return tickers


def is_korean_stock(ticker: str) -> bool:
    return ticker.isdigit() and len(ticker) == 6


def get_kr_yfinance_ticker_and_info(ticker: str) -> tuple[str, dict, yf.Ticker]:
    # 1. KOSPI (.KS) 시도
    symbol_ks = f"{ticker}.KS"
    t_ks = yf.Ticker(symbol_ks)
    info_ks = {}
    try:
        info_ks = t_ks.info
    except Exception:
        pass
        
    # quoteType이 'EQUITY'로 정확히 수집되는 경우에만 코스피로 반환
    if info_ks and info_ks.get("quoteType") == "EQUITY":
        return symbol_ks, info_ks, t_ks

    # 2. KOSDAQ (.KQ) 시도
    symbol_kq = f"{ticker}.KQ"
    t_kq = yf.Ticker(symbol_kq)
    info_kq = {}
    try:
        info_kq = t_kq.info
    except Exception:
        pass
        
    return symbol_kq, info_kq, t_kq


def get_financial_value(df: pd.DataFrame, keywords: list[str]) -> float | str:
    if df is None or df.empty:
        return ""
    for idx in df.index:
        idx_lower = str(idx).lower().strip()
        for kw in keywords:
            if kw.lower() in idx_lower:
                val = df.loc[idx]
                if hasattr(val, "iloc"):
                    val = val.iloc[0]
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
    return ""


def get_52w_high_low(ticker_obj: yf.Ticker, info: dict) -> tuple[float | str, float | str]:
    high = info.get("fiftyTwoWeekHigh")
    low = info.get("fiftyTwoWeekLow")
    if high is None or low is None:
        try:
            hist_1y = ticker_obj.history(period="1y")
            if not hist_1y.empty:
                if high is None:
                    high = float(hist_1y["High"].max())
                if low is None:
                    low = float(hist_1y["Low"].min())
        except Exception:
            pass
    return high if high is not None else "", low if low is not None else ""


def fetch_kr_stock(ticker: str) -> dict:
    today = datetime.now(KST)
    start = (today - timedelta(days=7)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    
    # 1. 시세 데이터 (pykrx)
    df = krx.get_market_ohlcv_by_date(start, end, ticker)
    if df.empty:
        raise ValueError(f"pykrx: {ticker} 데이터 없음 (상장폐지 또는 조회 실패)")
    
    row = df.iloc[-1]
    close = float(row["종가"])
    high = float(row["고가"])
    low = float(row["저가"])
    volume = int(row["거래량"])
    
    # 등락금액, 등락률, 전일종가
    if len(df) >= 2:
        prev_close = float(df.iloc[-2]["종가"])
        change_val = close - prev_close
        change_rate = round((change_val / prev_close) * 100, 2)
    else:
        prev_close = ""
        change_val = ""
        change_rate = ""
        if "등락률" in df.columns:
            change_rate = round(float(row["등락률"]), 2)

    # 2. yfinance 연동을 통해 PER, 시가총액, 52주 고가/저가, 재무제표 데이터 수집
    per, market_cap = "", ""
    high_52w, low_52w, rev, net_inc, liab, ocf = "", "", "", "", "", ""
    try:
        yf_symbol, info, yf_ticker = get_kr_yfinance_ticker_and_info(ticker)
        
        # PER
        per_val = info.get("trailingPE")
        if per_val is None or per_val == "":
            per_val = info.get("forwardPE")
        if per_val is not None and per_val != "":
            per = round(float(per_val), 2)
            
        # 시가총액
        mc_val = info.get("marketCap")
        if mc_val is None or mc_val == "":
            try:
                mc_val = yf_ticker.fast_info.market_cap
            except Exception:
                pass
        if mc_val is not None and mc_val != "":
            market_cap = int(mc_val)

        # 52주 고가/저가
        high_52w, low_52w = get_52w_high_low(yf_ticker, info)
        
        # 매출액, 순이익, 부채총계, 영업현금흐름
        rev = get_financial_value(yf_ticker.financials, ["total revenue", "revenue", "매출액"])
        net_inc = get_financial_value(yf_ticker.financials, ["net income", "순이익", "당기순이익"])
        liab = get_financial_value(yf_ticker.balance_sheet, ["total liabilities", "total liabilities net minority interest", "부채"])
        ocf = get_financial_value(yf_ticker.cashflow, ["operating cash flow", "cash flow from operating activities", "영업현금"])
    except Exception:
        pass

    return {
        "현재가": close,
        "등락(금액)": change_val,
        "등락률": change_rate,
        "거래량": volume,
        "고가": high,
        "저가": low,
        "전일 종가": prev_close,
        "per": per,
        "시가총액": market_cap,
        "52주 신고가": high_52w,
        "52주 신저가": low_52w,
        "매출액": rev,
        "손이익": net_inc,
        "부채총계": liab,
        "영업현금흐름": ocf,
        "기준일자": datetime.now(KST).strftime("%Y-%m-%d")
    }


def fetch_foreign_stock(ticker: str) -> dict:
    info_ticker = yf.Ticker(ticker)
    hist = info_ticker.history(period="5d")
    hist = hist.dropna(subset=["Close"])
    if hist.empty:
        raise ValueError(f"yfinance: {ticker} 데이터 없음 (5거래일 내 종가 없음)")
        
    row = hist.iloc[-1]
    close = float(row["Close"])
    high = float(row["High"])
    low = float(row["Low"])
    volume = int(row["Volume"])
    
    # 등락금액, 등락률, 전일종가
    if len(hist) >= 2:
        prev_close = float(hist["Close"].iloc[-2])
        change_val = round(close - prev_close, 4)
        change_rate = round((change_val / prev_close) * 100, 2)
    else:
        prev_close = ""
        change_val = ""
        change_rate = ""

    # info 데이터 조회
    info = {}
    try:
        info = info_ticker.info
    except Exception:
        pass

    # PER
    per = ""
    per_val = info.get("trailingPE")
    if per_val is None or per_val == "":
        per_val = info.get("forwardPE")
    if per_val is not None and per_val != "":
        per = round(float(per_val), 2)
        
    # 시가총액
    market_cap = ""
    mc_val = info.get("marketCap")
    if mc_val is None or mc_val == "":
        try:
            mc_val = info_ticker.fast_info.market_cap
        except Exception:
            pass
    if mc_val is not None and mc_val != "":
        market_cap = int(mc_val)
    
    high_52w, low_52w = get_52w_high_low(info_ticker, info)

    # 재무 데이터
    rev, net_inc, liab, ocf = "", "", "", ""
    try:
        rev = get_financial_value(info_ticker.financials, ["total revenue", "revenue", "매출액"])
        net_inc = get_financial_value(info_ticker.financials, ["net income", "순이익", "당기순이익"])
        liab = get_financial_value(info_ticker.balance_sheet, ["total liabilities", "total liabilities net minority interest", "부채"])
        ocf = get_financial_value(info_ticker.cashflow, ["operating cash flow", "cash flow from operating activities", "영업현금"])
    except Exception:
        pass

    return {
        "현재가": close,
        "등락(금액)": change_val,
        "등락률": change_rate,
        "거래량": volume,
        "고가": high,
        "저가": low,
        "전일 종가": prev_close,
        "per": per,
        "시가총액": market_cap,
        "52주 신고가": high_52w,
        "52주 신저가": low_52w,
        "매출액": rev,
        "손이익": net_inc,
        "부채총계": liab,
        "영업현금흐름": ocf,
        "기준일자": datetime.now(KST).strftime("%Y-%m-%d")
    }


def collect(ticker: str, name: str) -> dict:
    now = datetime.now(KST)
    base = {
        "종목코드": ticker,
        "종목명": name,
        "수집일시": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if is_korean_stock(ticker):
        data = fetch_kr_stock(ticker)
    else:
        data = fetch_foreign_stock(ticker)
    return {**base, **data, "상태": "정상"}


def append_data_log(ws: gspread.Worksheet, row: dict) -> None:
    values = [
        row.get("기준일자", ""),
        row.get("종목코드", ""),
        row.get("종목명", ""),
        row.get("현재가", ""),
        row.get("등락(금액)", ""),
        row.get("등락률", ""),
        row.get("거래량", ""),
        row.get("고가", ""),
        row.get("저가", ""),
        row.get("전일 종가", ""),
        row.get("per", ""),
        row.get("시가총액", ""),
        row.get("52주 신고가", ""),
        row.get("52주 신저가", ""),
        row.get("매출액", ""),
        row.get("손이익", ""),
        row.get("부채총계", ""),
        row.get("영업현금흐름", ""),
        row.get("수집일시", ""),
        row.get("상태", "")
    ]
    ws.append_row(values, value_input_option="USER_ENTERED")


def append_error_log(ws: gspread.Worksheet, ticker: str, error_msg: str) -> None:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([now, ticker, error_msg], value_input_option="USER_ENTERED")


def main() -> None:
    print("=== Stock Log 시작 ===")
    client = get_gspread_client()
    spreadsheet = open_spreadsheet(client)

    tickers = load_tickers(spreadsheet)
    data_ws = get_or_create_worksheet(spreadsheet, DATA_LOG_TAB, DATA_LOG_HEADERS)
    print("  이전 데이터 로그 삭제 중 (Data_Log 초기화)...")
    data_ws.clear()
    data_ws.append_row(DATA_LOG_HEADERS, value_input_option="USER_ENTERED")
    error_ws = get_or_create_worksheet(spreadsheet, ERROR_LOG_TAB, ERROR_LOG_HEADERS)

    print(f"수집 대상 종목 수: {len(tickers)}")

    for item in tickers:
        ticker = item["ticker"]
        name = item["종목명"]
        print(f"  수집 중: {ticker} ({name})", end=" ")
        try:
            row = collect(ticker, name)
            append_data_log(data_ws, row)
            print(f"-> 정상 (현재가: {row['현재가']}, 등락(금액): {row['등락(금액)']}, 거래량: {row['거래량']})")
        except Exception as e:
            error_msg = str(e)
            print(f"-> 오류: {error_msg}")
            now = datetime.now(KST)
            error_row = {
                "기준일자": now.strftime("%Y-%m-%d"),
                "종목코드": ticker,
                "종목명": name,
                "현재가": "",
                "등락(금액)": "",
                "등락률": "",
                "거래량": "",
                "고가": "",
                "저가": "",
                "전일 종가": "",
                "per": "",
                "시가총액": "",
                "52주 신고가": "",
                "52주 신저가": "",
                "매출액": "",
                "손이익": "",
                "부채총계": "",
                "영업현금흐름": "",
                "수집일시": now.strftime("%Y-%m-%d %H:%M:%S"),
                "상태": "오류",
            }
            append_data_log(data_ws, error_row)
            append_error_log(error_ws, ticker, error_msg)
        time.sleep(1.5)

    print("=== Stock Log 완료 ===")


if __name__ == "__main__":
    main()
