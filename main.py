import json
import os
import time
from datetime import datetime, timezone, timedelta

import gspread
import yfinance as yf
from google.oauth2.service_account import Credentials
from pykrx import stock as krx

KST = timezone(timedelta(hours=9))
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SETUP_TAB = "설정(Setup)"
DATA_LOG_TAB = "데이터로그(Data_Log)"
ERROR_LOG_TAB = "에러로그(Error_Log)"


def get_gspread_client() -> gspread.Client:
    creds_json = os.environ["GOOGLE_CREDENTIALS_JSON"]
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def open_spreadsheet(client: gspread.Client) -> gspread.Spreadsheet:
    url = os.environ["SPREADSHEET_URL"]
    return client.open_by_url(url)


def load_tickers(spreadsheet: gspread.Spreadsheet) -> list[dict]:
    ws = spreadsheet.worksheet(SETUP_TAB)
    rows = ws.get_all_records()  # 헤더 행을 키로 자동 파싱
    return [
        {"ticker": str(r["ticker"]).strip(), "종목명": str(r["종목명"]).strip()}
        for r in rows
        if str(r.get("수집여부(Y/N)", "")).strip().upper() == "Y"
    ]


def is_korean_stock(ticker: str) -> bool:
    return ticker.isdigit() and len(ticker) == 6


def fetch_kr_stock(ticker: str) -> dict:
    today = datetime.now(KST).strftime("%Y%m%d")
    df = krx.get_market_ohlcv_by_date(today, today, ticker)
    if df.empty:
        raise ValueError(f"pykrx: {ticker} 데이터 없음 (장 미개장 또는 상장폐지)")
    row = df.iloc[-1]
    close = float(row["종가"])
    volume = int(row["거래량"])

    # 등락률: pykrx 컬럼명 '등락률' 또는 직접 계산
    if "등락률" in df.columns:
        change_rate = float(row["등락률"])
    else:
        prev_close = float(row["시가"])  # 근사값 fallback
        change_rate = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0

    return {"현재가": close, "등락률": change_rate, "거래량": volume}


def fetch_foreign_stock(ticker: str) -> dict:
    info = yf.Ticker(ticker)
    hist = info.history(period="2d")
    if hist.empty or len(hist) < 1:
        raise ValueError(f"yfinance: {ticker} 데이터 없음")
    close = float(hist["Close"].iloc[-1])
    volume = int(hist["Volume"].iloc[-1])
    if len(hist) >= 2:
        prev_close = float(hist["Close"].iloc[-2])
        change_rate = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
    else:
        change_rate = 0.0
    return {"현재가": close, "등락률": change_rate, "거래량": volume}


def collect(ticker: str, name: str) -> dict:
    now = datetime.now(KST)
    base = {
        "수집일자": now.strftime("%Y-%m-%d"),
        "수집시간": now.strftime("%H:%M:%S"),
        "ticker": ticker,
        "종목명": name,
    }
    if is_korean_stock(ticker):
        data = fetch_kr_stock(ticker)
    else:
        data = fetch_foreign_stock(ticker)
    return {**base, **data, "상태": "정상"}


def append_data_log(ws: gspread.Worksheet, row: dict) -> None:
    values = [
        row["수집일자"],
        row["수집시간"],
        row["ticker"],
        row["종목명"],
        row["현재가"],
        row.get("등락률", ""),
        row.get("거래량", ""),
        row["상태"],
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
    print(f"수집 대상 종목 수: {len(tickers)}")

    data_ws = spreadsheet.worksheet(DATA_LOG_TAB)
    error_ws = spreadsheet.worksheet(ERROR_LOG_TAB)

    for item in tickers:
        ticker = item["ticker"]
        name = item["종목명"]
        print(f"  수집 중: {ticker} ({name})", end=" ")
        try:
            row = collect(ticker, name)
            append_data_log(data_ws, row)
            print(f"-> 정상 (현재가: {row['현재가']}, 등락률: {row['등락률']}%, 거래량: {row['거래량']})")
        except Exception as e:
            error_msg = str(e)
            print(f"-> 오류: {error_msg}")
            now = datetime.now(KST)
            error_row = {
                "수집일자": now.strftime("%Y-%m-%d"),
                "수집시간": now.strftime("%H:%M:%S"),
                "ticker": ticker,
                "종목명": name,
                "현재가": "",
                "등락률": "",
                "거래량": "",
                "상태": "오류",
            }
            append_data_log(data_ws, error_row)
            append_error_log(error_ws, ticker, error_msg)
        time.sleep(1.5)

    print("=== Stock Log 완료 ===")


if __name__ == "__main__":
    main()
