import fastapi
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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
import time
import json
import asyncio

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


@app.get("/api/fetch_etfs")
async def fetch_etfs(limit: int = 20):
    """
    Scrapes JPX ETF page, then fetches Yahoo Finance data for each.
    Streams progress to the client via Server-Sent Events (SSE).
    """
    async def event_generator():
        logger = logging.getLogger("uvicorn.error")
        
        yield f"data: {json.dumps({'type': 'info', 'message': 'JPXのサイトからETF一覧を取得しています...'})}\n\n"
        
        url = "https://www.jpx.co.jp/equities/products/etfs/issues/01.html"
        try:
            response = await asyncio.to_thread(requests.get, url, timeout=10)
            response.raise_for_status()
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': f'Failed to fetch JPX page: {str(e)}'})}\n\n"
            return
            
        soup = BeautifulSoup(response.content, 'html.parser')
        
        table = soup.find('table')
        if not table:
             yield f"data: {json.dumps({'type': 'error', 'error': 'Could not find the ETF table on the JPX page.'})}\n\n"
             return
             
        tbody = table.find('tbody')
        rows = tbody.find_all('tr') if tbody else table.find_all('tr')
        
        valid_rows = [r for r in rows if len(r.find_all(['td', 'th'])) >= 5 and r.find_all(['td', 'th'])[0].name != 'th']
        total_etfs = len(valid_rows)
        if limit > 0:
            total_etfs = min(total_etfs, limit)
            
        yield f"data: {json.dumps({'type': 'start', 'total': total_etfs})}\n\n"
        
        etf_list = []
        dates = get_target_business_dates()
        logger.info(f"Target Dates: {dates}")
        
        def df_date_str(d):
            return d.strftime('%Y-%m-%d')
            
        start_fetch_date = dates['year'] - timedelta(days=550)
        end_fetch_date = dates['target'] + timedelta(days=1)
        
        count = 0
        for row in valid_rows:
            tds = row.find_all(['td', 'th'])
            benchmark = tds[0].get_text(strip=True)
            code_text = tds[1].get_text(strip=True)
            name = tds[2].get_text(strip=True)
            management = tds[3].get_text(strip=True)
            fee = tds[4].get_text(strip=True)
            
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
            
            yield f"data: {json.dumps({'type': 'progress', 'current': count + 1, 'total': total_etfs, 'code': code_text, 'name': name})}\n\n"
            
            if code_match:
                try:
                    await asyncio.sleep(0.5)
                    ticker_symbol = f"{code_match}.T"
                    ticker = yf.Ticker(ticker_symbol)
                    
                    hist = await asyncio.to_thread(ticker.history, start=df_date_str(start_fetch_date), end=df_date_str(end_fetch_date))
                    
                    if not hist.empty:
                        def get_price_for_date(target_d):
                            d_str = target_d.strftime('%Y-%m-%d')
                            past_data = hist.loc[:d_str]
                            if not past_data.empty:
                                return float(past_data.iloc[-1]['Close'])
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
                    
                    etf_data["dividend_date"] = "-"
                    etf_data["dividend_yield"] = "-"
                    
                    if 'Dividends' in hist.columns:
                        divs = hist[hist['Dividends'] > 0]
                        if not divs.empty:
                            last_year_date = hist.index[-1] - timedelta(days=365)
                            trailing_divs = divs[divs.index > last_year_date]
                            annual_div = trailing_divs['Dividends'].sum()
                            
                            if current_price and current_price > 0 and annual_div > 0:
                                calc_yield = (annual_div / current_price) * 100
                                etf_data["dividend_yield"] = f"{calc_yield:.2f}%"
                            
                            recent_divs = divs.tail(24)
                            today = datetime.now(pytz.timezone('Asia/Tokyo'))
                            
                            payout_months = sorted(list(set([d.month for d in recent_divs.index])))
                            
                            avg_day_by_month = {}
                            for m in payout_months:
                                days = [d.day for d in recent_divs.index if d.month == m]
                                avg_day_by_month[m] = int(sum(days) / len(days)) if days else 10
                                
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

        yield f"data: {json.dumps({'type': 'complete', 'status': 'success', 'data': etf_list, 'target_date': dates['target'].strftime('%Y-%m-%d')})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
