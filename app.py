import fastapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import requests
from bs4 import BeautifulSoup
import yfinance as yf
import pandas as pd
import jpholiday
from datetime import datetime, timedelta
import pytz
import logging
import math

app = fastapi.FastAPI(title="ETF Viewer")

# Serve the frontend files
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=HTMLResponse)
async def get_index():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


def get_business_days_list(end_date: datetime, count: int) -> list:
    """Returns a list of `count` past Japanese business days up to `end_date`"""
    days = []
    current_date = end_date
    while len(days) < count:
        # Check if it's a weekend or Japanese holiday
        if current_date.weekday() < 5 and not jpholiday.is_holiday(current_date.date()):
            days.append(current_date)
        current_date -= timedelta(days=1)
    return days

def get_target_business_dates():
    """Calculates the target evaluation dates for the ETF data based on the 15:30 JST rule."""
    jst = pytz.timezone('Asia/Tokyo')
    now = datetime.now(jst)
    
    # 15:30 rule: If before 15:30, use previous day as the target (if business day). If after 15:30, use today.
    # Note: If today is a weekend/holiday, we just look back for the most recent business day anyway.
    target_date = now
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        target_date -= timedelta(days=1)
        
    # Get the last 300 business days just to be safe for 1-year calculation (1 year ~= 245 business days).
    recent_b_days = get_business_days_list(target_date, 300)
    
    target_bday = recent_b_days[0]
    prev_bday = recent_b_days[1]
    week_ago_bday = recent_b_days[5]     # 5 business days ago
    two_weeks_ago_bday = recent_b_days[10] # 10 business days ago
    
    # 1 year ago (approx 245 business days ago)
    # We can also just subtract 365 days and find the nearest business day.
    one_year_ago_date = target_bday - timedelta(days=365)
    one_year_ago_list = get_business_days_list(one_year_ago_date, 1)
    year_ago_bday = one_year_ago_list[0]
    
    return {
        "target": target_bday,
        "prev": prev_bday,
        "week": week_ago_bday,
        "two_weeks": two_weeks_ago_bday,
        "year": year_ago_bday
    }

def format_pct_change(old_price, new_price):
    if not old_price or math.isnan(old_price) or old_price == 0:
        return None
    if not new_price or math.isnan(new_price):
        return None
    pct = ((new_price - old_price) / old_price) * 100
    return round(pct, 2)


@app.get("/api/fetch_etfs", response_class=JSONResponse)
async def fetch_etfs(limit: int = 20):
    """
    Scrapes JPX ETF page, then fetches Yahoo Finance data for each.
    Allows a `limit` parameter for testing to avoid huge delays.
    Set limit=0 to fetch all (warning: slow).
    """
    logger = logging.getLogger("uvicorn.error")
    
    url = "https://www.jpx.co.jp/equities/products/etfs/issues/01.html"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to fetch JPX page: {str(e)}"})
        
    soup = BeautifulSoup(response.content, 'html.parser')
    
    # Find the main data table
    table = soup.find('table')
    if not table:
         return JSONResponse(status_code=500, content={"error": "Could not find the ETF table on the JPX page."})
         
    tbody = table.find('tbody')
    rows = tbody.find_all('tr') if tbody else table.find_all('tr')
    
    # We skip the header typically handled by thead, but if rows include header, we might start from the first data row.
    etf_list = []
    
    dates = get_target_business_dates()
    logger.info(f"Target Dates: {dates}")
    
    # Helper to format date for yf
    def df_date_str(d):
        return d.strftime('%Y-%m-%d')
        
    # Fetch 2.5 years earlier to ensure enough data for dividends pattern prediction
    start_fetch_date = dates['year'] - timedelta(days=550)
    end_fetch_date = dates['target'] + timedelta(days=1)   # exclusive upper bound
    
    count = 0
    for row in rows:
        tds = row.find_all(['td', 'th'])
        # A data row should have at least 5 columns
        if len(tds) < 5 or tds[0].name == 'th':
            continue
            
        benchmark = tds[0].get_text(strip=True)
        code_text = tds[1].get_text(strip=True)
        name = tds[2].get_text(strip=True)
        management = tds[3].get_text(strip=True)
        fee = tds[4].get_text(strip=True)
        
        # Remove any extra text from code, just get the numbers. Sometimes JPX adds a letter.
        # But Japanese ETFs in yfinance just use the 4 digit code + ".T".
        code_match = ''.join(filter(str.isalnum, code_text))
        
        etf_data = {
            "benchmark": benchmark,
            "code": code_text,
            "name": name,
            "management": management,
            "fee": fee,
            "price": None,
            "change_1d_pct": None,
            "change_1w_pct": None,
            "change_2w_pct": None,
            "change_1y_pct": None,
            "dividend_yield": None,
            "dividend_date": None
        }
        
        if code_match:
            try:
                ticker_symbol = f"{code_match}.T"
                ticker = yf.Ticker(ticker_symbol)
                
                # Fetch historical history
                hist = ticker.history(start=df_date_str(start_fetch_date), end=df_date_str(end_fetch_date))
                
                if not hist.empty:
                    # Helper to get closest price on or before a target date
                    def get_price_for_date(target_d):
                        d_str = target_d.strftime('%Y-%m-%d')
                        # filter up to date
                        past_data = hist.loc[:d_str]
                        if not past_data.empty:
                            return past_data.iloc[-1]['Close']
                        return None
                        
                    current_price = get_price_for_date(dates['target'])
                    prev_price = get_price_for_date(dates['prev'])
                    week_price = get_price_for_date(dates['week'])
                    two_week_price = get_price_for_date(dates['two_weeks'])
                    year_price = get_price_for_date(dates['year'])
                    
                    etf_data["price"] = round(current_price, 2) if current_price else None
                    etf_data["change_1d_pct"] = format_pct_change(prev_price, current_price)
                    etf_data["change_1w_pct"] = format_pct_change(week_price, current_price)
                    etf_data["change_2w_pct"] = format_pct_change(two_week_price, current_price)
                    etf_data["change_1y_pct"] = format_pct_change(year_price, current_price)
                
                # Calculate Dividend Yield and predict Next Dividend Date from history
                etf_data["dividend_date"] = "-"
                etf_data["dividend_yield"] = "-"
                
                if 'Dividends' in hist.columns:
                    divs = hist[hist['Dividends'] > 0]
                    if not divs.empty:
                        # 1. Calculate yield based on trailing 12 months
                        last_year_date = hist.index[-1] - timedelta(days=365)
                        trailing_divs = divs[divs.index > last_year_date]
                        annual_div = trailing_divs['Dividends'].sum()
                        
                        if current_price and current_price > 0 and annual_div > 0:
                            calc_yield = (annual_div / current_price) * 100
                            etf_data["dividend_yield"] = f"{calc_yield:.2f}%"
                        
                        # 2. Predict next dividend date
                        # Get the last 2 years of dividends to find the pattern
                        recent_divs = divs.tail(24)
                        
                        today = datetime.now(pytz.timezone('Asia/Tokyo'))
                        
                        # Gather the typical payout months
                        payout_months = sorted(list(set([d.month for d in recent_divs.index])))
                        
                        # Calculate average payout day for each month
                        avg_day_by_month = {}
                        for m in payout_months:
                            days = [d.day for d in recent_divs.index if d.month == m]
                            avg_day_by_month[m] = int(sum(days) / len(days)) if days else 10
                            
                        # Find the next month in the sequence
                        next_month = None
                        next_year = today.year
                        next_day = None
                        
                        for m in payout_months:
                            if m == today.month and today.day < avg_day_by_month[m]:
                                next_month = m
                                next_day = avg_day_by_month[m]
                                break
                            elif m > today.month:
                                next_month = m
                                next_day = avg_day_by_month[m]
                                break
                                
                        if not next_month:
                            # Roll over to next year's first payout
                            if len(payout_months) > 0:
                                next_month = payout_months[0]
                                next_year += 1
                                next_day = avg_day_by_month[next_month]
                                
                        if next_month and next_day:
                            etf_data["dividend_date"] = f"次回予想: {next_year}年{next_month}月{next_day}日頃"
                        
            except Exception as e:
                logger.error(f"Error fetching data for {code_match}: {e}")
                
        etf_list.append(etf_data)
        
        count += 1
        if limit > 0 and count >= limit:
            break

    return {"status": "success", "data": etf_list, "target_date": dates['target'].strftime('%Y-%m-%d')}


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
