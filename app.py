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
import json
import asyncio
import requests_cache

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

def get_business_dates_info():
    """Calculates the target evaluation dates for the ETF data based on the 15:30 JST rule."""
    jst = pytz.timezone('Asia/Tokyo')
    now = datetime.now(jst)
    
    target_date = now
    if now.hour < 15 or (now.hour == 15 and now.minute < 30):
        target_date -= timedelta(days=1)
        
    recent_b_days = get_business_days_list(target_date, 300)
    
    return {
        "target": recent_b_days[0],
        "target_str": recent_b_days[0].strftime('%Y-%m-%d')
    }

@app.get("/api/fetch_etfs", response_class=JSONResponse)
async def fetch_etfs():
    """
    Scrapes JPX ETF page and returns the list of ETFs.
    Does NOT fetch Yahoo Finance data (left to the client).
    """
    logger = logging.getLogger("uvicorn.error")
    
    url = "https://www.jpx.co.jp/equities/products/etfs/issues/01.html"
    try:
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Failed to fetch JPX page: {str(e)}"})
        
    soup = BeautifulSoup(response.content, 'html.parser')
    
    table = soup.find('table')
    if not table:
         return JSONResponse(status_code=500, content={"error": "Could not find the ETF table on the JPX page."})
         
    tbody = table.find('tbody')
    rows = tbody.find_all('tr') if tbody else table.find_all('tr')
    
    valid_rows = [r for r in rows if len(r.find_all(['td', 'th'])) >= 5 and r.find_all(['td', 'th'])[0].name != 'th']
    
    etf_list = []
    
    for row in valid_rows:
        tds = row.find_all(['td', 'th'])
        benchmark = tds[0].get_text(strip=True)
        code_text = tds[1].get_text(strip=True)
        name = tds[2].get_text(strip=True)
        management = tds[3].get_text(strip=True)
        fee = tds[4].get_text(strip=True)
        
        code_match = ''.join(filter(str.isalnum, code_text))
        
        if code_match:
            etf_list.append({
                "benchmark": benchmark,
                "code": code_text,
                "clean_code": code_match, # For frontend to use in YF API
                "name": name,
                "management": management,
                "fee": fee
            })

    dates = get_business_dates_info()
    return {"status": "success", "data": etf_list, "target_date": dates['target_str']}

@app.get("/api/proxy/yfinance/{ticker}", response_class=JSONResponse)
async def proxy_yfinance(ticker: str):
    """
    Proxies a single yfinance request for the frontend to distribute load and not do bulk fetches.
    """
    try:
        # We need data to calculate 1 year diffs and predict dividends
        end_date = datetime.now() + timedelta(days=1)
        start_date = end_date - timedelta(days=550)
        
        yf_ticker = yf.Ticker(ticker)
        
        hist = await asyncio.to_thread(
            yf_ticker.history, 
            start=start_date.strftime('%Y-%m-%d'), 
            end=end_date.strftime('%Y-%m-%d')
        )
        
        if hist.empty:
            return {"status": "error", "error": f"No data found for {ticker}"}
            
        # Convert index (dates) to strings for JSON
        hist_dict = {}
        for index, row in hist.iterrows():
            date_str = index.strftime('%Y-%m-%d')
            hist_dict[date_str] = {
                "Close": float(row['Close']),
                "Dividends": float(row['Dividends']) if 'Dividends' in row else 0.0
            }
            
        return {"status": "success", "data": hist_dict}
        
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
