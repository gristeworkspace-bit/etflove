import os
from datetime import datetime, timedelta
import yfinance as yf
from fastapi import APIRouter, BackgroundTasks
import requests
import pandas as pd
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, BroadcastRequest, TextMessage

from google import genai

router = APIRouter()

# 環境変数 (LINE Messaging API と Gemini API)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# LINE Messaging API クライアントの初期化
line_client = None
if LINE_CHANNEL_ACCESS_TOKEN:
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    api_client = ApiClient(configuration)
    line_client = MessagingApi(api_client)

# Geminiクライアントの初期化 (APIキーがあれば)
ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

# スパム通知防止用の状態保持変数 (オンメモリ)
last_notified = {
    "long_top": {"time": None, "price": 0.0},
    "long_bottom": {"time": None, "price": 0.0},
    "short_top": {"time": None, "price": 0.0},
    "short_bottom": {"time": None, "price": 0.0},
    "range": {"time": None, "price": 0.0},
}

COOLDOWN_HOURS = 2  # 同じ種類の通知を再送するまでの待機時間
THRESHOLD = 0.10    # 現在価格と壁の間のしきい値 (0.1円 = 10pips以内なら接近とみなす)
RANGE_THRESHOLD = 0.30 # レンジ幅のしきい値(高値と安値の差が30pips以内ならレンジと判定)

def send_line_message(message: str):
    if not line_client:
        print("[WARNING] LINE_CHANNEL_ACCESS_TOKEN が設定されていません。LINEへの通知はスキップされます。")
        return
        
    try:
        broadcast_request = BroadcastRequest(
            messages=[TextMessage(text=message)]
        )
        line_client.broadcast(broadcast_request)
        print("LINE Messaging API (Broadcast) 経由で通知を送信しました。")
    except Exception as e:
        print(f"LINE Messaging API 送信エラー: {e}")

def get_ai_analysis(market_context: str) -> str:
    """Gemini APIを使って相場状況を分析させる"""
    if not ai_client:
        return ""
        
    prompt = f"""
あなたは優秀なFX（ドル円）の専属アナリストです。
以下の現在の相場状況に基づいて、トレーダーに向けて【端的で客観的な一言アドバイス】を書いてください。
文字数は100文字以内で、冗長な挨拶は不要です。

【現在の相場状況】
{market_context}
"""
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return f"\\n\\n🤖AIアナリストのひとこと:\\n{response.text.strip()}"
    except Exception as e:
        print(f"Gemini APIエラー: {e}")
        return ""

def can_notify(notify_type: str, current_price: float) -> bool:
    """同じゾーンでのスパム通知を防ぐためのロジック"""
    now = datetime.now()
    last = last_notified[notify_type]
    
    if last["time"] is None or (now - last["time"]) > timedelta(hours=COOLDOWN_HOURS):
        return True
    
    # 連続通知防止
    return False

def update_notify_state(notify_type: str, current_price: float):
    last_notified[notify_type]["time"] = datetime.now()
    last_notified[notify_type]["price"] = current_price

def extract_levels(df: pd.DataFrame, window_size: int):
    """ローリングを使って山（天井）と谷（底）を抽出する"""
    if df.empty or len(df) < window_size * 2 + 1:
        return [], []
        
    df = df.copy()
    rolling_max = df['High'].rolling(window=window_size*2+1, center=True).max()
    rolling_min = df['Low'].rolling(window=window_size*2+1, center=True).min()
    
    # 前後の指定期間内で一番高い/低い場合、そこをピーク/ボトムとする
    df['Top'] = df['High'][df['High'] == rolling_max]
    df['Bottom'] = df['Low'][df['Low'] == rolling_min]
    
    tops = df['Top'].dropna().tolist()
    bottoms = df['Bottom'].dropna().tolist()
    return tops, bottoms

def check_proximity(current_price: float, levels: list, threshold: float):
    """現在価格が過去の壁に近づいているかチェックし、最も近い壁を返す"""
    closest_level = None
    min_diff = float('inf')
    
    for level in levels:
        diff = abs(current_price - level)
        if diff <= threshold and diff < min_diff:
            min_diff = diff
            closest_level = level
            
    return closest_level

def is_in_range(df: pd.DataFrame, max_range_pips: float):
    """指定期間の最高値と最安値の差が一定以内であればレンジ相場と判定する"""
    if df.empty:
        return False, 0, 0
    max_price = df['High'].max()
    min_price = df['Low'].min()
    if (max_price - min_price) <= max_range_pips:
        return True, max_price, min_price
    return False, max_price, min_price

def run_analysis_task(force: bool = False):
    print(f"[{datetime.now()}] 価格チェックを開始します... (force={force})")
    
    try:
        ticker = yf.Ticker('JPY=X')
        
        # 1. 短期データ（過去2日、15分足）の取得と壁の抽出
        # 左右5本（=1時間15分）の中で最高値・最安値となるポイントを壁（短期）とみなす
        df_short = ticker.history(period='2d', interval='15m')
        short_tops, short_bottoms = extract_levels(df_short, window_size=5)
        
        # 2. 長期データ（過去14日、1時間足）の取得と壁の抽出
        # 左右10本（=10時間）の中で最高値・最安値となるポイントを壁（中長期）とみなす
        df_long = ticker.history(period='14d', interval='1h')
        long_tops, long_bottoms = extract_levels(df_long, window_size=10)

        # 3. 超短期のレンジ判定（過去12時間、15分足）
        df_very_short = df_short.tail(48) # 15分足×48本 ＝ 12時間

        if df_short.empty or df_long.empty:
            print("yfinanceから価格データの取得に失敗しました。")
            return
            
        try:
            # yfinanceの最新のリアルタイム価格（fast_info）を取得
            current_price = ticker.fast_info['lastPrice']
        except Exception:
            # 取得に失敗した場合は、15分足の最後の終値をフォールバックとして使用
            current_price = float(df_short['Close'].iloc[-1].item()) if hasattr(df_short['Close'].iloc[-1], 'item') else float(df_short['Close'].iloc[-1])
            
        print(f"現在価格: {current_price:.3f}円")
        
        message = ""
        ai_context = ""
        
        # --- 強制テスト通知 ---
        if force:
            test_msg = f"\\n【🔧テスト通知】Renderからの手動トリガー成功！\\n現在価格: {current_price:.2f}円\\n※相場状況にかかわらず強制送信しました。"
            test_context = f"現在価格は{current_price:.2f}円です。これはシステムのテスト送信です。"
            test_msg += get_ai_analysis(test_context)
            send_line_message(test_msg)
            print("強制テスト通知を送信しました。")
            return

        # --- レンジ判定 ---
        in_range, range_top, range_bottom = is_in_range(df_very_short, RANGE_THRESHOLD)
        if in_range and can_notify("range", current_price):
            message += f"\\n【📉レンジ相場】直近12時間は狭いレンジ（もみ合い）になっています！\\n上限: {range_top:.2f}円\\n下限: {range_bottom:.2f}円\\n現在価格: {current_price:.2f}円\\n※ブレイクアウトにご注意ください。"
            ai_context = f"過去12時間は {range_bottom:.2f}円から{range_top:.2f}円のレンジ相場。現在価格は{current_price:.2f}円。"
            update_notify_state("range", current_price)

        # --- 長期の強い壁を優先的に判定 ---
        closest_long_top = check_proximity(current_price, long_tops, THRESHOLD)
        if closest_long_top and can_notify("long_top", current_price):
            base_msg = f"\\n【🔥激アツ】過去14日間の強い天井（レジスタンス帯）に接近中！\\n壁の価格: {closest_long_top:.2f}円\\n現在価格: {current_price:.2f}円"
            message += base_msg + "\\n※反発下落の可能性が高まっています。"
            ai_context = f"現在価格{current_price:.2f}円。過去14日間の強力なレジスタンス({closest_long_top:.2f}円)に接近中。"
            update_notify_state("long_top", current_price)

        closest_long_bottom = check_proximity(current_price, long_bottoms, THRESHOLD)
        if closest_long_bottom and can_notify("long_bottom", current_price):
            base_msg = f"\\n【🔥激アツ】過去14日間の強い底（サポート帯）に接近中！\\n壁の価格: {closest_long_bottom:.2f}円\\n現在価格: {current_price:.2f}円"
            message += base_msg + "\\n※反発上昇の可能性が高まっています。"
            ai_context = f"現在価格{current_price:.2f}円。過去14日間の強力なサポート({closest_long_bottom:.2f}円)に接近中。"
            update_notify_state("long_bottom", current_price)
            
        # --- 短期の直近の壁を判定（長期壁がなければ） ---
        if not message and not in_range:
            closest_short_top = check_proximity(current_price, short_tops, THRESHOLD)
            if closest_short_top and can_notify("short_top", current_price):
                message += f"\\n【⚠️注意】過去2日間の直近の天井に接近中！\\n壁の価格: {closest_short_top:.2f}円\\n現在価格: {current_price:.2f}円"
                ai_context = f"現在価格{current_price:.2f}円。直近2日間のレジスタンス({closest_short_top:.2f}円)に接近中。"
                update_notify_state("short_top", current_price)

            closest_short_bottom = check_proximity(current_price, short_bottoms, THRESHOLD)
            if closest_short_bottom and can_notify("short_bottom", current_price):
                message += f"\\n【⚠️注意】過去2日間の直近の底に接近中！\\n壁の価格: {closest_short_bottom:.2f}円\\n現在価格: {current_price:.2f}円"
                ai_context = f"現在価格{current_price:.2f}円。直近2日間のサポート({closest_short_bottom:.2f}円)に接近中。"
                update_notify_state("short_bottom", current_price)

        # メッセージがあればAIに分析させて送信
        if message:
            if ai_context:
                message += get_ai_analysis(ai_context)
                
            send_line_message(message)
            print("通知を送信しました:" + message)
        else:
            print("現在はサポート/レジスタンスラインから離れています。またはレンジ内です。")

    except Exception as e:
        print(f"エラーが発生しました: {e}")

@router.get("/fx_health")
def read_root():
    return {"status": "ok", "message": "FX Bottom/Top Bot is running."}

@router.get("/trigger")
def trigger_analysis(background_tasks: BackgroundTasks, force: bool = False):
    """
    cron-job.org 等からこのエンドポイントを定期的に叩くことで、
    Renderのスリープを防ぎつつバックグラウンドで価格判定と通知を行います。
    ?force=true をつけると条件無視で強制通知テストができます。
    """
    background_tasks.add_task(run_analysis_task, force)
    return {"status": "Analysis triggered in background", "force": force}
