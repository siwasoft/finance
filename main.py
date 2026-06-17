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

SETUP_TAB = "Setup"
DATA_LOG_TAB = "Data_Log"
ERROR_LOG_TAB = "Error_Log"

SETUP_HEADERS = ["ticker", "종목명", "수집여부(Y/N)", "비고"]
DATA_LOG_HEADERS = ["수집일자", "수집시간", "ticker", "종목명", "현재가", "등락률", "거래량", "상태"]
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
    # 구글 시트가 005930 → 5930 으로 앞자리 0을 제거하므로 복원
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


def fetch_kr_stock(ticker: str) -> dict:
    today = datetime.now(KST).strftime("%Y%m%d")
    df = krx.get_market_ohlcv_by_date(today, today, ticker)
    if df.empty:
        raise ValueError(f"pykrx: {ticker} 데이터 없음 (장 미개장 또는 상장폐지)")
    row = df.iloc[-1]
    close = float(row["종가"])
    volume = int(row["거래량"])
    if "등락률" in df.columns:
        change_rate = round(float(row["등락률"]), 2)
    else:
        prev_close = float(row["시가"])
        change_rate = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0.0
    return {"현재가": close, "등락률": change_rate, "거래량": volume}


def fetch_foreign_stock(ticker: str) -> dict:
    info = yf.Ticker(ticker)
    hist = info.history(period="5d")  # 주말·공휴일을 고려해 5거래일치 요청
    hist = hist.dropna(subset=["Close"])
    if hist.empty:
        raise ValueError(f"yfinance: {ticker} 데이터 없음 (5거래일 내 종가 없음)")
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
    data_ws = get_or_create_worksheet(spreadsheet, DATA_LOG_TAB, DATA_LOG_HEADERS)
    error_ws = get_or_create_worksheet(spreadsheet, ERROR_LOG_TAB, ERROR_LOG_HEADERS)

    print(f"수집 대상 종목 수: {len(tickers)}")

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
