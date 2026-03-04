import os
from datetime import datetime, timedelta
import yfinance as yf
from fastapi import APIRouter, BackgroundTasks
import requests
import pandas as pd
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, BroadcastRequest, TextMessage

from google import genai
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

router = APIRouter()

# 環境変数 (LINE Messaging API と Gemini API)
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# LINE Messaging API クライアントの初期化
line_client = None
if LINE_CHANNEL_ACCESS_TOKEN:
    print(f"[INIT] LINE_CHANNEL_ACCESS_TOKEN is set (length: {len(LINE_CHANNEL_ACCESS_TOKEN)}). Initializing line_client...")
    configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
    api_client = ApiClient(configuration)
    line_client = MessagingApi(api_client)
else:
    print("[INIT] WARNING: LINE_CHANNEL_ACCESS_TOKEN is NOT set.")

# Geminiクライアントの初期化 (APIキーがあれば)
ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

# スパム通知防止用の状態保持変数 (オンメモリ)
last_notified = {
    "resistance": {"time": None, "price": 0.0},
    "support": {"time": None, "price": 0.0},
    "range": {"time": None, "price": 0.0},
    "breakout_up": {"time": None, "price": 0.0},
    "breakout_down": {"time": None, "price": 0.0},
}

# ===== 設定パラメータ =====
COOLDOWN_HOURS = 1       # 同じ種類の通知を再送するまでの待機時間
TREND_COOLDOWN_HOURS = 4 # トレンド通知を再送するまでの待機時間(長め)
THRESHOLD = 0.10         # 現在価格と壁ゾーンの間のしきい値 (0.1円 = 10pips)
ZONE_MERGE_PIPS = 0.05   # 壁をグループ化する際の許容幅 (5pips以内は同一ゾーンとみなす)
BREAKOUT_MARGIN = 0.05   # 壁をこの値（5pips）以上超えたらブレイクアウト確定とみなす
SWING_WINDOW = 5         # スイングポイント検出の左右の確認本数

# バックグラウンドスケジューラー
_scheduler = None


# ===== ① LINE送信 =====
def send_line_message(message: str):
    if not line_client:
        print("[WARNING] LINE_CHANNEL_ACCESS_TOKEN が設定されていません。LINEへの通知はスキップされます。")
        return

    try:
        broadcast_request = BroadcastRequest(
            messages=[TextMessage(text=message.replace("\\n", "\n"))]
        )
        print("Sending BroadcastRequest to LINE API...")
        response = line_client.broadcast(broadcast_request)
        print(f"LINE Messaging API (Broadcast) response: {response}")
    except Exception as e:
        print(f"LINE Messaging API 送信エラー: {type(e).__name__} - {e}")
        if hasattr(e, 'body'):
            print(f"Error Details: {e.body}")


# ===== ② Gemini AI分析 =====
def get_ai_analysis(market_context: str) -> str:
    """豊富な相場データを基にGemini APIで分析させる"""
    if not ai_client:
        return ""

    prompt = f"""
あなたは優秀なFX（ドル円）の専属アナリストです。
以下の詳細な相場データに基づいて、トレーダーに向けて【具体的で実践的なアドバイス】を書いてください。

ルール:
- 「情報が不足」という回答は禁止。提供されたデータだけで判断すること。
- 「売り・買い・様子見」のいずれかの方向性を必ず示すこと。
- 注目すべき価格ラインや打診ポイントを具体的に示すこと。
- 文字数は150文字以内。冗長な挨拶は不要。

{market_context}
"""
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return f"\n\n🤖AIアナリストのひとこと:\n{response.text.strip()}"
    except Exception as e:
        print(f"Gemini APIエラー: {e}")
        return ""


# ===== ③ クールダウン =====
def can_notify(notify_type: str, current_price: float) -> bool:
    """同じゾーンでのスパム通知を防ぐためのロジック"""
    now = datetime.now()
    last = last_notified[notify_type]
    
    cooldown = TREND_COOLDOWN_HOURS if notify_type.startswith("trend_") else COOLDOWN_HOURS

    if last["time"] is None or (now - last["time"]) > timedelta(hours=cooldown):
        return True
    return False

def update_notify_state(notify_type: str, current_price: float):
    last_notified[notify_type]["time"] = datetime.now()
    last_notified[notify_type]["price"] = current_price


# ===== ④ Stage 1: スイングポイント検出（プライスアクション） =====
def detect_swing_points(df: pd.DataFrame, window: int):
    """
    確定済みスイングハイ/ローを検出する。
    各ポイントに価格・時刻・ヒゲ比率・タイプを返す。
    window: 左右何本の足で比較するか
    """
    if df.empty or len(df) < window * 2 + 1:
        return []

    df = df.copy()
    points = []

    # 確定済みのスイングポイントのみ（最新のwindow本は未確定なので除外）
    for i in range(window, len(df) - window):
        row = df.iloc[i]
        high = float(row['High'])
        low = float(row['Low'])
        open_price = float(row['Open'])
        close = float(row['Close'])
        timestamp = df.index[i]

        body = abs(close - open_price)
        if body < 0.001:
            body = 0.001  # ゼロ除算防止

        # --- スイングハイ（天井）判定 ---
        is_swing_high = True
        for j in range(i - window, i + window + 1):
            if j == i:
                continue
            if float(df.iloc[j]['High']) > high:
                is_swing_high = False
                break

        if is_swing_high:
            upper_wick = high - max(open_price, close)
            wick_ratio = upper_wick / body
            points.append({
                "price": high,
                "timestamp": timestamp,
                "wick_ratio": round(wick_ratio, 2),
                "type": "resistance",
                "high": high,
                "low": low,
                "open": open_price,
                "close": close,
            })

        # --- スイングロー（底）判定 ---
        is_swing_low = True
        for j in range(i - window, i + window + 1):
            if j == i:
                continue
            if float(df.iloc[j]['Low']) < low:
                is_swing_low = False
                break

        if is_swing_low:
            lower_wick = min(open_price, close) - low
            wick_ratio = lower_wick / body
            points.append({
                "price": low,
                "timestamp": timestamp,
                "wick_ratio": round(wick_ratio, 2),
                "type": "support",
                "high": high,
                "low": low,
                "open": open_price,
                "close": close,
            })

    return points


# ===== ⑤ Stage 2: 壁のグループ化 + 反応回数カウント =====
def group_price_zones(swing_points: list, merge_distance: float):
    """
    近い価格帯（merge_distance以内）のスイングポイントを1つのゾーンに統合する。
    反応回数・ヒゲの質から強さ（★）を算出。
    """
    if not swing_points:
        return []

    # 価格でソート
    sorted_points = sorted(swing_points, key=lambda x: x["price"])

    zones = []
    current_zone = {
        "points": [sorted_points[0]],
        "type": sorted_points[0]["type"],
    }

    for point in sorted_points[1:]:
        # 同じゾーン内（merge_distance以内）かつ同じタイプならマージ
        zone_avg = sum(p["price"] for p in current_zone["points"]) / len(current_zone["points"])
        if abs(point["price"] - zone_avg) <= merge_distance and point["type"] == current_zone["type"]:
            current_zone["points"].append(point)
        else:
            zones.append(current_zone)
            current_zone = {
                "points": [point],
                "type": point["type"],
            }
    zones.append(current_zone)

    # ゾーンの統計情報を算出
    result = []
    for zone in zones:
        pts = zone["points"]
        reaction_count = len(pts)
        avg_price = sum(p["price"] for p in pts) / reaction_count
        avg_wick = sum(p["wick_ratio"] for p in pts) / reaction_count

        # 強さ判定: 反応回数 + ヒゲの質
        # 反応1回=★, 2回=★★, 3回以上=★★★
        # ヒゲ比率が2.0以上なら+★（上限★★★）
        stars = min(reaction_count, 3)
        if avg_wick >= 2.0 and stars < 3:
            stars += 1

        # 反応履歴（新しい順）
        reactions = sorted(pts, key=lambda x: x["timestamp"], reverse=True)

        result.append({
            "zone_price": round(avg_price, 3),
            "type": zone["type"],
            "reaction_count": reaction_count,
            "strength": stars,
            "strength_str": "★" * stars,
            "avg_wick_ratio": round(avg_wick, 2),
            "reactions": reactions,
        })

    return result


# ===== ⑤-B Stage 2.5: トレンド判定 (プライスアクション / ダウ理論) =====
def analyze_trend_pa(swing_points: list) -> dict:
    """
    スイングハイ・ローの切り上げ・切り下げ（ダウ理論）を用いて現在のトレンド状態を判定する。
    戻り値: {"status": "up"|"down"|"neutral", "details": str}
    """
    # 昇順（古い順）にソート
    sorted_pts = sorted(swing_points, key=lambda x: x["timestamp"])
    
    highs = [p for p in sorted_pts if p["type"] == "resistance"]
    lows = [p for p in sorted_pts if p["type"] == "support"]
    
    # 高値・安値のそれぞれが直近2個以上あるか確認
    if len(highs) < 2 or len(lows) < 2:
        return {"status": "neutral", "details": "トレンドを判定するためのスイングポイントが不足しています"}

    # 直近の高値と、その1個前の高値を比較 (High1 = 古い, High2 = 新しい)
    high1, high2 = highs[-2]["price"], highs[-1]["price"]
    # 直近の安値と、その1個前の安値を比較 (Low1 = 古い, Low2 = 新しい)
    low1, low2 = lows[-2]["price"], lows[-1]["price"]

    # 上昇トレンド定義: 高値の切り上げ (Higher High) AND 安値の切り上げ (Higher Low)
    if high2 > high1 and low2 > low1:
        return {"status": "up", "details": "高値と安値が切り上がっています（上昇のダウ成立）"}
        
    # 下落トレンド定義: 高値の切り下げ (Lower High) AND 安値の切り下げ (Lower Low)
    if high2 < high1 and low2 < low1:
        return {"status": "down", "details": "高値と安値が切り下がっています（下降のダウ成立）"}

    # どちらでもない (レンジ、三角持ち合い、トレンド転換中など)
    return {"status": "neutral", "details": "高値・安値の方向感が揃っていません（レンジ・または転換中）"}


# ===== ⑥ Stage 3: メッセージ生成 =====
def build_alert_message(zone: dict, current_price: float, alert_type: str) -> tuple:
    """
    壁ゾーンの情報からLINE通知メッセージとAIコンテキストを生成する。
    alert_type: "resistance", "support", "range"
    """
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    zone_price = zone["zone_price"]
    diff_pips = abs(current_price - zone_price) * 100  # 円→pips変換

    if alert_type == "resistance":
        emoji = "🔥"
        label = "強い天井（レジスタンス帯）"
        action = "反発下落の可能性があります"
    elif alert_type == "support":
        emoji = "🔥"
        label = "強い底（サポート帯）"
        action = "反発上昇の可能性があります"
    else:
        emoji = "⚠️"
        label = "膠着状態（レンジ）"
        action = "ブレイクアウトに警戒してください"

    msg = f"📊 ドル円アラート（{now_str}）\n\n"
    msg += f"【{emoji}{label}】{zone_price:.2f}円付近に接近中\n"
    msg += f"　壁の強さ: {zone['strength_str']}（過去{zone['reaction_count']}回反発）\n"
    msg += f"　現在価格: {current_price:.2f}円（壁まで{diff_pips:.0f}pips）\n"
    msg += f"\n【根拠】\n"

    # 過去の反応履歴（最大3件）
    for reaction in zone["reactions"][:3]:
        ts = reaction["timestamp"]
        if hasattr(ts, 'strftime'):
            ts_str = ts.strftime("%m/%d %H:%M")
        else:
            ts_str = str(ts)[:16]

        wick = reaction["wick_ratio"]
        if reaction["type"] == "resistance":
            if wick >= 2.0:
                desc = "長い上ヒゲで強く反落"
            elif wick >= 1.0:
                desc = "上ヒゲで反落"
            else:
                desc = "実体で到達後に反落"
        else:
            if wick >= 2.0:
                desc = "長い下ヒゲで強く反発"
            elif wick >= 1.0:
                desc = "下ヒゲで反発"
            else:
                desc = "実体で到達後に反発"

        msg += f"・{ts_str} {desc}（{reaction['price']:.2f}円）\n"

    msg += f"\n※{action}"

    ai_context = (
        f"現在価格{current_price:.2f}円。"
        f"{zone_price:.2f}円付近の{label}に接近中。"
        f"過去{zone['reaction_count']}回反発しており、壁の強さは{zone['strength_str']}。"
        f"壁までの距離は{diff_pips:.0f}pips。"
        f"平均ヒゲ比率{zone['avg_wick_ratio']:.1f}（高いほど拒否が強い）。"
    )

    return msg, ai_context


def build_range_message(res_zone: dict, sup_zone: dict, current_price: float) -> tuple:
    """天井と底の両方に挟まれている場合のレンジメッセージ"""
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    range_width = abs(res_zone["zone_price"] - sup_zone["zone_price"]) * 100

    msg = f"📊 ドル円アラート（{now_str}）\n\n"
    msg += f"【⚠️膠着状態】天井と底に挟まれています\n"
    msg += f"　天井: {res_zone['zone_price']:.2f}円 {res_zone['strength_str']}（{res_zone['reaction_count']}回反発）\n"
    msg += f"　底　: {sup_zone['zone_price']:.2f}円 {sup_zone['strength_str']}（{sup_zone['reaction_count']}回反発）\n"
    msg += f"　現在: {current_price:.2f}円\n"
    msg += f"　レンジ幅: 約{range_width:.0f}pips\n"
    msg += f"\n※力を溜めている状態です。どちらかにブレイクアウトする可能性が高まっています。"

    ai_context = (
        f"現在価格{current_price:.2f}円。"
        f"レジスタンス{res_zone['zone_price']:.2f}円（{res_zone['strength_str']}）と"
        f"サポート{sup_zone['zone_price']:.2f}円（{sup_zone['strength_str']}）に挟まれた"
        f"約{range_width:.0f}pipsの狭いレンジ。ブレイクアウト警戒。"
    )

    return msg, ai_context


# ===== ⑥-A2: ブレイクアウトメッセージ生成 =====
def build_breakout_message(zone: dict, current_price: float, direction: str) -> tuple:
    """
    壁を突き抜けた場合のブレイクアウト通知メッセージを生成する。
    direction: "up"（上方ブレイク）or "down"（下方ブレイク）
    """
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    zone_price = zone["zone_price"]
    diff_pips = abs(current_price - zone_price) * 100

    if direction == "up":
        emoji = "🚀"
        label = "上方ブレイクアウト"
        wall_label = "天井（レジスタンス）"
        action = "上昇トレンド加速の可能性があります"
    else:
        emoji = "💥"
        label = "下方ブレイクアウト"
        wall_label = "底（サポート）"
        action = "下落トレンド加速の可能性があります"

    msg = f"📊 ドル円アラート（{now_str}）\n\n"
    msg += f"【{emoji}{label}】{zone_price:.2f}円の{wall_label}を突破！\n"
    msg += f"　突破した壁の強さ: {zone['strength_str']}（過去{zone['reaction_count']}回反発していた壁）\n"
    msg += f"　現在価格: {current_price:.2f}円（壁から{diff_pips:.0f}pips突破）\n"
    msg += f"\n【根拠】\n"

    # 過去の反応履歴（最大3件）
    for reaction in zone["reactions"][:3]:
        ts = reaction["timestamp"]
        if hasattr(ts, 'strftime'):
            ts_str = ts.strftime("%m/%d %H:%M")
        else:
            ts_str = str(ts)[:16]
        msg += f"・{ts_str} に{zone_price:.2f}円付近で反発していた\n"

    msg += f"\n※{action}"

    ai_context = (
        f"現在価格{current_price:.2f}円。"
        f"{zone_price:.2f}円の{wall_label}（{zone['strength_str']}、過去{zone['reaction_count']}回反発）を"
        f"{diff_pips:.0f}pips突破した{label}が発生。"
        f"トレンドの継続か、ダマシで戻るかの判断が重要。"
    )

    return msg, ai_context


# ===== ⑥-C: トレンドメッセージ生成 (プライスアクション版) =====
def build_trend_message(trend_info: dict, current_price: float) -> tuple:
    """トレンド発生/継続時のLINE通知メッセージを作成"""
    now_str = datetime.now().strftime("%Y/%m/%d %H:%M")
    
    if trend_info["status"] == "up":
        emoji = "📈"
        trend_name = "上昇トレンド"
        color = "買い優勢"
    else:
        emoji = "📉"
        trend_name = "下落トレンド"
        color = "売り優勢"
        
    msg = f"📊 ドル円トレンド通知（{now_str}）\n\n"
    msg += f"【{emoji}{trend_name}】{color}の相場になっています\n"
    msg += f"　サイン: {trend_info['details']}\n"
    msg += f"　現在価格: {current_price:.2f}円\n\n"
    
    msg += "※ダウ理論に基づき、直近の波形（プライスアクション）からトレンドを判定しています。トレンド方向への順張りが有効な場面です。"

    ai_context = (
        f"現在価格{current_price:.2f}円。"
        f"{trend_info['details']}のサインが出現。"
        f"{trend_name}（{color}）と判定されました。ダウ理論に基づいたトレンド状況に合わせたアドバイスを。"
    )

    return msg, ai_context


# ===== ⑥-B: AIに渡す豊富な相場コンテキストの構築 =====
def build_full_ai_context(df: pd.DataFrame, current_price: float, zones: list, alert_context: str) -> str:
    """
    AIに渡すための包括的な相場データを構築する。
    - 直近の値動きサマリー（高値・安値・方向感）
    - 検出された全ゾーン（壁）の一覧
    - 今回のアラート内容
    """
    context = "【アラート内容】\n"
    context += alert_context + "\n\n"

    # --- 直近の値動きサマリー ---
    context += "【直近の値動き】\n"

    if not df.empty and len(df) >= 2:
        # 直近24時間（15分足×96本）の値動き
        recent = df.tail(96) if len(df) >= 96 else df
        recent_high = float(recent['High'].max())
        recent_low = float(recent['Low'].min())
        recent_open = float(recent['Open'].iloc[0])
        recent_close = float(recent['Close'].iloc[-1])
        change = recent_close - recent_open
        direction = "上昇" if change > 0 else "下落" if change < 0 else "横ばい"

        context += f"直近24時間: 高値{recent_high:.2f}円 / 安値{recent_low:.2f}円 / 値幅{(recent_high - recent_low)*100:.0f}pips\n"
        context += f"方向: {direction}（{change:+.2f}円 / {change*100:+.0f}pips）\n"

        # 直近4時間（15分足×16本）のトレンド
        very_recent = df.tail(16) if len(df) >= 16 else df
        vr_open = float(very_recent['Open'].iloc[0])
        vr_close = float(very_recent['Close'].iloc[-1])
        vr_change = vr_close - vr_open
        vr_dir = "上昇中" if vr_change > 0.02 else "下落中" if vr_change < -0.02 else "もみ合い"
        context += f"直近4時間の勢い: {vr_dir}（{vr_change:+.2f}円）\n"

    context += f"現在価格: {current_price:.2f}円\n\n"

    # --- 全ゾーン一覧（壁の地図） ---
    context += "【検出された壁（価格帯）の一覧】\n"
    res_zones = sorted([z for z in zones if z["type"] == "resistance"], key=lambda z: z["zone_price"])
    sup_zones = sorted([z for z in zones if z["type"] == "support"], key=lambda z: z["zone_price"], reverse=True)

    if res_zones:
        context += "▲ レジスタンス（上値の壁）:\n"
        for z in res_zones[:5]:  # 上位5つ
            dist = (z["zone_price"] - current_price) * 100
            context += f"  {z['zone_price']:.2f}円 {z['strength_str']} 反応{z['reaction_count']}回 (現在価格から{dist:+.0f}pips)\n"

    if sup_zones:
        context += "▼ サポート（下値の壁）:\n"
        for z in sup_zones[:5]:  # 上位5つ
            dist = (z["zone_price"] - current_price) * 100
            context += f"  {z['zone_price']:.2f}円 {z['strength_str']} 反応{z['reaction_count']}回 (現在価格から{dist:+.0f}pips)\n"
            
    return context


# ===== ⑦ メインの分析タスク =====
def run_analysis_task(force: bool = False):
    print(f"[{datetime.now()}] 価格チェックを開始します... (force={force})")

    # 土日は通知をスキップ（FXは土日休場のため）
    # force=True の場合はテスト用にスキップしない
    now = datetime.now()
    if not force and now.weekday() in (5, 6):  # 5=Saturday, 6=Sunday
        print(f"本日は{'土曜日' if now.weekday() == 5 else '日曜日'}のため、通知をスキップします。")
        return

    try:
        ticker = yf.Ticker('JPY=X')

        # データ取得: 過去5日の15分足（スイング検出に十分な本数を確保）
        df = ticker.history(period='5d', interval='15m')

        if df.empty:
            print("yfinanceから価格データの取得に失敗しました。")
            return

        try:
            current_price = ticker.fast_info['lastPrice']
        except Exception:
            current_price = float(df['Close'].iloc[-1].item()) if hasattr(df['Close'].iloc[-1], 'item') else float(df['Close'].iloc[-1])

        print(f"現在価格: {current_price:.3f}円")

        # --- 強制テスト通知 ---
        if force:
            # テスト時でも新ロジックの結果を含めて送信
            swing_points = detect_swing_points(df, window=SWING_WINDOW)
            zones = group_price_zones(swing_points, ZONE_MERGE_PIPS)

            res_zones = [z for z in zones if z["type"] == "resistance"]
            sup_zones = [z for z in zones if z["type"] == "support"]

            test_msg = f"📊【🔧テスト通知】（{datetime.now().strftime('%Y/%m/%d %H:%M')}）\n\n"
            test_msg += f"現在価格: {current_price:.2f}円\n"
            test_msg += f"検出された反発ポイント: {len(swing_points)}個\n"
            test_msg += f"意識される価格帯: {len(zones)}個\n\n"

            if res_zones:
                nearest_res = min(res_zones, key=lambda z: abs(z["zone_price"] - current_price))
                test_msg += f"最寄りのレジスタンス: {nearest_res['zone_price']:.2f}円 {nearest_res['strength_str']}\n"
            if sup_zones:
                nearest_sup = min(sup_zones, key=lambda z: abs(z["zone_price"] - current_price))
                test_msg += f"最寄りのサポート: {nearest_sup['zone_price']:.2f}円 {nearest_sup['strength_str']}\n"

            test_alert_context = f"現在価格{current_price:.2f}円。テスト送信。"
            if res_zones:
                test_alert_context += f"最寄りレジスタンス: {nearest_res['zone_price']:.2f}円 {nearest_res['strength_str']}。"
            if sup_zones:
                test_alert_context += f"最寄りサポート: {nearest_sup['zone_price']:.2f}円 {nearest_sup['strength_str']}。"

            # トレンド情報の追記
            trend_info = analyze_trend_pa(swing_points)
            if trend_info["status"] != "neutral":
                test_alert_context += f"現在{trend_info['details']}のトレンドが発生中。"

            full_context = build_full_ai_context(df, current_price, zones, test_alert_context)
            test_msg += get_ai_analysis(full_context)
            send_line_message(test_msg)
            print("強制テスト通知を送信しました。")
            return

        # ===== プライスアクション分析パイプライン =====

        # Step 1: スイングポイント検出
        swing_points = detect_swing_points(df, window=SWING_WINDOW)
        print(f"検出されたスイングポイント: {len(swing_points)}個")

        if not swing_points:
            print("スイングポイントが検出されませんでした。")
            return

        # Step 2: ゾーングループ化
        zones = group_price_zones(swing_points, ZONE_MERGE_PIPS)
        print(f"統合後のゾーン: {len(zones)}個")

        for z in zones:
            print(f"  [{z['type']}] {z['zone_price']:.3f}円 {z['strength_str']} (反応{z['reaction_count']}回, ヒゲ比率{z['avg_wick_ratio']:.1f})")

        # Step 3: 現在価格に最も近い壁を判定
        res_zones = [z for z in zones if z["type"] == "resistance" and z["zone_price"] >= current_price - THRESHOLD]
        sup_zones = [z for z in zones if z["type"] == "support" and z["zone_price"] <= current_price + THRESHOLD]

        # 現在価格からTHRESHOLD以内のゾーンだけに絞る
        nearby_res = [z for z in res_zones if abs(z["zone_price"] - current_price) <= THRESHOLD]
        nearby_sup = [z for z in sup_zones if abs(z["zone_price"] - current_price) <= THRESHOLD]

        # 近い順にソート
        nearby_res.sort(key=lambda z: abs(z["zone_price"] - current_price))
        nearby_sup.sort(key=lambda z: abs(z["zone_price"] - current_price))

        message = ""
        ai_context = ""

        # Step 4: アラート判定

        # --- 4a: ブレイクアウト判定 ---
        # 全てのレジスタンスゾーンを上に突き抜けているかチェック
        all_res = [z for z in zones if z["type"] == "resistance"]
        all_sup = [z for z in zones if z["type"] == "support"]

        # 上方ブレイクアウト: 現在価格がレジスタンスゾーンを BREAKOUT_MARGIN 以上超えている
        broken_res = [z for z in all_res if current_price > z["zone_price"] + BREAKOUT_MARGIN]
        # 下方ブレイクアウト: 現在価格がサポートゾーンを BREAKOUT_MARGIN 以上下回っている
        broken_sup = [z for z in all_sup if current_price < z["zone_price"] - BREAKOUT_MARGIN]

        if broken_res:
            # 最も高い（=最も重要な）突破されたレジスタンスを選択
            strongest_broken = max(broken_res, key=lambda z: z["zone_price"])
            if can_notify("breakout_up", current_price):
                message, ai_context = build_breakout_message(strongest_broken, current_price, "up")
                update_notify_state("breakout_up", current_price)

        elif broken_sup:
            # 最も低い（=最も重要な）突破されたサポートを選択
            strongest_broken = min(broken_sup, key=lambda z: z["zone_price"])
            if can_notify("breakout_down", current_price):
                message, ai_context = build_breakout_message(strongest_broken, current_price, "down")
                update_notify_state("breakout_down", current_price)

        # --- 4b: 壁への接近判定（従来ロジック） ---
        elif nearby_res and nearby_sup:
            # 天井にも底にも挟まれている = レンジ（膠着）
            if can_notify("range", current_price):
                message, ai_context = build_range_message(nearby_res[0], nearby_sup[0], current_price)
                update_notify_state("range", current_price)

        elif nearby_res:
            # レジスタンスに接近
            if can_notify("resistance", current_price):
                message, ai_context = build_alert_message(nearby_res[0], current_price, "resistance")
                update_notify_state("resistance", current_price)

        elif nearby_sup:
            # サポートに接近
            if can_notify("support", current_price):
                message, ai_context = build_alert_message(nearby_sup[0], current_price, "support")
                update_notify_state("support", current_price)

        # --- 4c: トレンド判定 (プライスアクション) ---
        # 壁への接近やブレイクアウトがない平和な時でも、明確なダウ理論成立があれば通知する
        if not message:
            trend_info = analyze_trend_pa(swing_points)
            if trend_info["status"] == "up":
                if can_notify("trend_up", current_price):
                    message, ai_context = build_trend_message(trend_info, current_price)
                    update_notify_state("trend_up", current_price)
            elif trend_info["status"] == "down":
                if can_notify("trend_down", current_price):
                    message, ai_context = build_trend_message(trend_info, current_price)
                    update_notify_state("trend_down", current_price)

        # Step 5: メッセージ送信
        if message:
            if ai_context:
                # --- トレンド状況をAIコンテキストに追記 ---
                if "trend_info" not in locals():
                    trend_info = analyze_trend_pa(swing_points)
                if trend_info["status"] != "neutral":
                    ai_context += f"\n【現在のトレンド】\n{trend_info['details']}\n"
                    
                # AIに相場全体の地図も渡す
                full_ai_context = build_full_ai_context(df, current_price, zones, ai_context)
                message += get_ai_analysis(full_ai_context)

            send_line_message(message)
            print("通知を送信しました:\n" + message)
        else:
            print("現在はサポート/レジスタンスラインから離れています。通知不要です。")

    except Exception as e:
        print(f"エラーが発生しました: {e}")


# ===== ⑧ バックグラウンドスケジューラー =====
def start_scheduler():
    """平日のみ1分間隔で価格チェックを自動実行するスケジューラーを起動する"""
    global _scheduler
    if _scheduler is not None:
        print("[SCHEDULER] スケジューラーは既に起動しています。")
        return

    _scheduler = BackgroundScheduler(daemon=True)
    # 月〜金の毎分0秒に実行
    _scheduler.add_job(
        run_analysis_task,
        CronTrigger(day_of_week='mon-fri', minute='*', second='0'),
        id='fx_analysis',
        name='FX価格分析（1分間隔）',
        replace_existing=True,
        misfire_grace_time=30,  # 30秒以内の遅延は許容
    )
    _scheduler.start()
    print("[SCHEDULER] FX分析スケジューラーを起動しました（平日1分間隔）")


def stop_scheduler():
    """スケジューラーを停止する"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        print("[SCHEDULER] FX分析スケジューラーを停止しました")


@router.get("/fx_health")
def read_root():
    return {"status": "ok", "message": "FX Bottom/Top Bot is running.", "scheduler": _scheduler is not None}

@router.get("/trigger")
def trigger_analysis(background_tasks: BackgroundTasks, force: bool = False):
    """
    手動テストや強制通知用エンドポイント。
    スケジューラーが平日1分間隔で自動実行するため、通常はcronからの呼び出し不要。
    cron-job.org はRenderのスリープ防止（死活監視）用として使用。
    ?force=true をつけると条件無視で強制通知テストができます。
    """
    background_tasks.add_task(run_analysis_task, force)
    return {"status": "Analysis triggered in background", "force": force}
