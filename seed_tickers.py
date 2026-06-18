"""setting 탭에 초기 종목 데이터를 입력하는 1회성 스크립트"""
import json
import os

import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TICKERS = [
    ["AAPL",    "애플",      "Y", "미국주식"],
    ["005930",  "삼성전자",  "Y", "국내주식"],
    ["000660",  "SK하이닉스","Y", "국내주식"],
    ["BTC-USD", "비트코인",  "Y", "가상자산"],
    ["ETH-USD", "이더리움",  "Y", "가상자산"],
    ["011070",  "LG이노텍",  "Y", "국내주식"],
]


def main() -> None:
    creds_info = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_url(os.environ["SPREADSHEET_URL"])

    # 탭 이름을 Setup에서 setting으로 변경하여 조회
    try:
        ws = spreadsheet.worksheet("setting")
    except gspread.exceptions.WorksheetNotFound:
        # worksheet가 존재하지 않으면 생성 및 헤더 주입
        headers = ["ticker", "종목명", "수집여부(Y/N)", "비고"]
        ws = spreadsheet.add_worksheet(title="setting", rows=1000, cols=len(headers))
        ws.append_row(headers, value_input_option="USER_ENTERED")
        print("  'setting' 탭을 생성하고 헤더를 추가했습니다.")

    existing = ws.get_all_values()
    existing_tickers = set()
    if len(existing) > 1:
        existing_tickers = {row[0] for row in existing[1:] if row}

    added = 0
    for row in TICKERS:
        if row[0] not in existing_tickers:
            ws.append_row(row, value_input_option="USER_ENTERED")
            print(f"  추가: {row[0]} ({row[1]})")
            added += 1
        else:
            print(f"  스킵: {row[0]} (이미 존재)")

    print(f"\n완료: {added}개 종목 추가됨")


if __name__ == "__main__":
    main()
