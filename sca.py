# -*- coding: utf-8 -*-
import os
import time
import logging
import pandas as pd
import numpy as np
import psycopg2
from psycopg2.extras import execute_values, RealDictCursor
from binance.client import Client
from datetime import datetime, timedelta, timezone
from decouple import config
from typing import List, Optional, Dict, Tuple
from threading import Thread
from flask import Flask
from scipy.signal import find_peaks
from sklearn.cluster import DBSCAN
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================================================================
# --- 1. الإعدادات العامة ونظام التسجيل (Logging) ---
# ==============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('combined_analyzer.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('CombinedCryptoAnalyzer')

# ==============================================================================
# --- 2. تحميل متغيرات البيئة والثوابت ---
# ==============================================================================
try:
    API_KEY: str = config('BINANCE_API_KEY')
    API_SECRET: str = config('BINANCE_API_SECRET')
    DB_URL: str = config('DATABASE_URL')
    logger.info("✅ [Config] تم تحميل متغيرات البيئة بنجاح.")
except Exception as e:
    logger.critical(f"❌ [Config] فشل في تحميل المتغيرات البيئية الأساسية. تأكد من وجود ملف .env: {e}")
    exit(1)

# --- ثوابت عامة ---
RUN_INTERVAL_HOURS: int = 2  # الفاصل الزمني الرئيسي للتشغيل (4 ساعات)
CRYPTO_LIST_FILENAME: str = 'crypto_list.txt'
MAX_WORKERS: int = 10 # لعمليات التحليل المتوازية
API_RETRY_ATTEMPTS: int = 3
API_RETRY_DELAY: int = 5

# --- ثوابت حاسبة إيشيموكو (Ichimoku) ---
ICHIMOKU_TIMEFRAME: str = '15m'
ICHIMOKU_DATA_LOOKBACK_DAYS: int = 30
ICHIMOKU_TENKAN_PERIOD: int = 9
ICHIMOKU_KIJUN_PERIOD: int = 26
ICHIMOKU_SENKOU_B_PERIOD: int = 52
ICHIMOKU_CHIKOU_SHIFT: int = -26
ICHIMOKU_SENKOU_SHIFT: int = 26

# --- ثوابت ماسح الدعم والمقاومة (S/R) ---
SR_DATA_FETCH_DAYS_1H: int = 30
SR_DATA_FETCH_DAYS_15M: int = 7
SR_DATA_FETCH_DAYS_5M: int = 3
SR_ATR_PERIOD: int = 14
SR_CLUSTER_EPS_PERCENT: float = 0.0015
SR_CONFLUENCE_ZONE_PERCENT: float = 0.002

# ==============================================================================
# --- 3. دوال مشتركة (Binance, Database, Utilities) ---
# ==============================================================================

def get_binance_client() -> Optional[Client]:
    """Initializes and returns the Binance client."""
    try:
        client = Client(API_KEY, API_SECRET)
        client.ping()
        logger.info("✅ [Binance] تم الاتصال بواجهة برمجة تطبيقات Binance بنجاح.")
        return client
    except Exception as e:
        logger.error(f"❌ [Binance] فشل الاتصال بواجهة برمجة التطبيقات: {e}")
        return None

def init_db() -> Optional[psycopg2.extensions.connection]:
    """Initializes and returns a new database connection."""
    try:
        conn = psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)
        logger.info("✅ [DB] تم إنشاء اتصال جديد بقاعدة البيانات بنجاح.")
        return conn
    except Exception as e:
        logger.error(f"❌ [DB] فشل الاتصال بقاعدة البيانات: {e}")
        return None

def setup_database_tables(conn: psycopg2.extensions.connection):
    """Creates all required tables if they don't exist."""
    if not conn:
        logger.warning("[DB] لا يوجد اتصال بقاعدة البيانات، تم تخطي إنشاء الجداول.")
        return
    
    queries = [
        """
        CREATE TABLE IF NOT EXISTS ichimoku_features (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20) NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL,
            timeframe VARCHAR(10) NOT NULL,
            tenkan_sen FLOAT,
            kijun_sen FLOAT,
            senkou_span_a FLOAT,
            senkou_span_b FLOAT,
            chikou_span FLOAT,
            UNIQUE (symbol, timestamp, timeframe)
        );
        """,
        "CREATE INDEX IF NOT EXISTS idx_ichimoku_symbol_timestamp ON ichimoku_features (symbol, timestamp DESC);",
        """
        CREATE TABLE IF NOT EXISTS support_resistance_levels (
            id SERIAL PRIMARY KEY,
            symbol TEXT NOT NULL,
            level_price DOUBLE PRECISION NOT NULL,
            level_type TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            strength NUMERIC NOT NULL,
            score NUMERIC DEFAULT 0,
            last_tested_at TIMESTAMP WITH TIME ZONE,
            details TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (symbol, level_price, timeframe, level_type, details)
        );
        """
    ]
    try:
        with conn.cursor() as cur:
            for query in queries:
                cur.execute(query)
            # Check and add 'score' column if missing (for backward compatibility)
            cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='support_resistance_levels' AND column_name='score'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE support_resistance_levels ADD COLUMN score NUMERIC DEFAULT 0;")
                logger.info("✅ [DB] تم إضافة عمود 'score' إلى جدول 'support_resistance_levels'.")

        conn.commit()
        logger.info("✅ [DB] تم فحص/إنشاء جميع الجداول المطلوبة بنجاح.")
    except Exception as e:
        logger.error(f"❌ [DB] خطأ أثناء إنشاء الجداول: {e}")
        conn.rollback()

def get_validated_symbols(client: Client, filename: str) -> List[str]:
    """Reads a list of symbols from a file and validates them against Binance."""
    if not client:
        logger.error("❌ [Symbol Validation] Binance client not available.")
        return []
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(script_dir, filename)
        if not os.path.exists(file_path):
            logger.error(f"❌ ملف قائمة العملات غير موجود في {file_path}")
            return []
        with open(file_path, 'r', encoding='utf-8') as f:
            raw_symbols = {line.strip().upper() for line in f if line.strip() and not line.startswith('#')}
        
        formatted_symbols = {f"{s}USDT" if not s.endswith('USDT') else s for s in raw_symbols}
        exchange_info = client.get_exchange_info()
        active_symbols = {s['symbol'] for s in exchange_info['symbols'] if s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING'}
        
        validated_list = sorted(list(formatted_symbols.intersection(active_symbols)))
        logger.info(f"✅ [Symbols] تم العثور على {len(validated_list)} عملة صالحة للمعالجة.")
        return validated_list
    except Exception as e:
        logger.error(f"❌ [Symbol Validation] خطأ: {e}", exc_info=True)
        return []

def fetch_historical_data(client: Client, symbol: str, interval: str, days: int) -> Optional[pd.DataFrame]:
    """Fetches historical kline data from Binance with a retry mechanism."""
    if not client: return None
    for attempt in range(API_RETRY_ATTEMPTS):
        try:
            start_str = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            klines = client.get_historical_klines(symbol, interval, start_str)
            if not klines:
                logger.warning(f"⚠️ [{symbol}] لم يتم العثور على بيانات على فريم {interval}.")
                return None
            
            df = pd.DataFrame(klines, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore'])
            numeric_cols = ['open', 'high', 'low', 'close', 'volume']
            df = df[numeric_cols + ['timestamp']]
            for col in numeric_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
            df.set_index('timestamp', inplace=True)
            return df.dropna()
        except Exception as e:
            logger.error(f"❌ [{symbol}] خطأ في جلب البيانات (محاولة {attempt + 1}/{API_RETRY_ATTEMPTS}): {e}")
            if attempt < API_RETRY_ATTEMPTS - 1:
                time.sleep(API_RETRY_DELAY)
    logger.critical(f"❌ [{symbol}] فشل جلب البيانات بعد {API_RETRY_ATTEMPTS} محاولات.")
    return None

# ==============================================================================
# --- 4. قسم حاسبة إيشيموكو (Ichimoku) ---
# ==============================================================================

def calculate_ichimoku(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates all Ichimoku Cloud components."""
    high, low, close = df['high'], df['low'], df['close']
    df['tenkan_sen'] = (high.rolling(window=ICHIMOKU_TENKAN_PERIOD).max() + low.rolling(window=ICHIMOKU_TENKAN_PERIOD).min()) / 2
    df['kijun_sen'] = (high.rolling(window=ICHIMOKU_KIJUN_PERIOD).max() + low.rolling(window=ICHIMOKU_KIJUN_PERIOD).min()) / 2
    df['senkou_span_a'] = ((df['tenkan_sen'] + df['kijun_sen']) / 2).shift(ICHIMOKU_SENKOU_SHIFT)
    df['senkou_span_b'] = ((high.rolling(window=ICHIMOKU_SENKOU_B_PERIOD).max() + low.rolling(window=ICHIMOKU_SENKOU_B_PERIOD).min()) / 2).shift(ICHIMOKU_SENKOU_SHIFT)
    df['chikou_span'] = close.shift(ICHIMOKU_CHIKOU_SHIFT)
    return df

def save_ichimoku_to_db(conn: psycopg2.extensions.connection, symbol: str, df_ichimoku: pd.DataFrame, timeframe: str):
    """Saves the calculated Ichimoku features to the database using ON CONFLICT."""
    if not conn or df_ichimoku.empty: return
    
    df_to_save = df_ichimoku[['tenkan_sen', 'kijun_sen', 'senkou_span_a', 'senkou_span_b', 'chikou_span']].copy()
    ichimoku_cols = df_to_save.columns.tolist()
    df_to_save.dropna(subset=ichimoku_cols, how='all', inplace=True)
    if df_to_save.empty: return

    df_to_save.reset_index(inplace=True)
    data_to_insert = [(symbol, row[0], timeframe) + tuple(row[1:]) for row in df_to_save[['timestamp'] + ichimoku_cols].to_numpy()]
    cols = ['symbol', 'timestamp', 'timeframe'] + ichimoku_cols
    update_cols = [f"{col} = EXCLUDED.{col}" for col in ichimoku_cols]
    query = f"INSERT INTO ichimoku_features ({', '.join(cols)}) VALUES %s ON CONFLICT (symbol, timestamp, timeframe) DO UPDATE SET {', '.join(update_cols)};"
    
    try:
        with conn.cursor() as cur:
            execute_values(cur, query, data_to_insert)
        conn.commit()
        logger.info(f"💾 [Ichimoku] تم حفظ/تحديث {len(data_to_insert)} سجل لـ {symbol}.")
    except Exception as e:
        logger.error(f"❌ [DB-Ichimoku] خطأ في حفظ بيانات {symbol}: {e}")
        conn.rollback()

def run_ichimoku_analysis(client: Client, conn: psycopg2.extensions.connection, symbols: List[str]):
    """Main function for the Ichimoku calculation part."""
    logger.info("🚀 [Ichimoku] بدء دورة حساب مؤشر إيشيموكو...")
    if not symbols:
        logger.warning("[Ichimoku] لا توجد عملات لتحليلها.")
        return

    for symbol in symbols:
        logger.info(f"--- ⏳ [Ichimoku] جاري المعالجة: {symbol} ---")
        try:
            df_ohlc = fetch_historical_data(client, symbol, ICHIMOKU_TIMEFRAME, ICHIMOKU_DATA_LOOKBACK_DAYS)
            if df_ohlc is None or df_ohlc.empty:
                logger.warning(f"[Ichimoku] لم يتم جلب بيانات لـ {symbol}. سيتم التخطي.")
                continue
            
            df_with_ichimoku = calculate_ichimoku(df_ohlc)
            save_ichimoku_to_db(conn, symbol, df_with_ichimoku, ICHIMOKU_TIMEFRAME)
        except Exception as e:
            logger.error(f"❌ [Ichimoku] خطأ حرج أثناء معالجة {symbol}: {e}", exc_info=True)
        time.sleep(1) # تأخير بسيط بين الطلبات
    logger.info("✅ [Ichimoku] انتهت دورة حساب مؤشر إيشيموكو.")

# ==============================================================================
# --- 5. قسم ماسح الدعم والمقاومة (S/R) ---
# ==============================================================================

def calculate_atr(df: pd.DataFrame, period: int) -> float:
    """Calculates Average True Range (ATR)."""
    if df.empty or len(df) < period: return 0
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/period, adjust=False).mean()
    return atr.iloc[-1] if not atr.empty else 0

def find_price_action_levels(df: pd.DataFrame, atr_value: float) -> List[Dict]:
    """Finds support and resistance levels based on price action peaks."""
    lows, highs = df['low'].to_numpy(), df['high'].to_numpy()
    prominence = atr_value * 0.7 
    if prominence == 0: prominence = highs.mean() * 0.01

    low_peaks_indices, _ = find_peaks(-lows, prominence=prominence, width=3)
    high_peaks_indices, _ = find_peaks(highs, prominence=prominence, width=3)

    def cluster_levels(prices: np.ndarray, indices: np.ndarray, level_type: str) -> List[Dict]:
        if len(indices) < 2: return []
        points = prices[indices].reshape(-1, 1)
        eps_value = points.mean() * SR_CLUSTER_EPS_PERCENT
        if eps_value == 0: return []
        db = DBSCAN(eps=eps_value, min_samples=2).fit(points)
        clustered = []
        for label in set(db.labels_):
            if label != -1:
                mask = (db.labels_ == label)
                cluster_indices = indices[mask]
                clustered.append({
                    "level_price": float(prices[cluster_indices].mean()),
                    "level_type": level_type,
                    "strength": int(len(cluster_indices)),
                    "last_tested_at": df.index[cluster_indices[-1]].to_pydatetime()
                })
        return clustered
        
    return cluster_levels(lows, low_peaks_indices, 'support') + cluster_levels(highs, high_peaks_indices, 'resistance')

def calculate_fibonacci_levels(df: pd.DataFrame) -> List[Dict]:
    """Calculates Fibonacci retracement levels."""
    if df.empty: return []
    max_high, min_low = df['high'].max(), df['low'].min()
    diff = max_high - min_low
    if diff <= 0: return []
    
    fib_levels = []
    for ratio in [0.236, 0.382, 0.5, 0.618, 0.786]:
        details_s = f"Fib Support {ratio*100:.1f}%" + (" (Golden)" if ratio == 0.618 else "")
        fib_levels.append({"level_price": float(max_high - diff * ratio), "level_type": 'fib_support', "strength": 2, "details": details_s, "last_tested_at": None})
        details_r = f"Fib Resistance {ratio*100:.1f}%" + (" (Golden)" if ratio == 0.618 else "")
        fib_levels.append({"level_price": float(min_low + diff * ratio), "level_type": 'fib_resistance', "strength": 2, "details": details_r, "last_tested_at": None})
    return fib_levels

def find_confluence_zones(levels: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Finds confluence zones where multiple levels cluster together."""
    if not levels: return [], []
    levels.sort(key=lambda x: x['level_price'])
    confluence_zones, used_indices = [], set()
    for i in range(len(levels)):
        if i in used_indices: continue
        current_zone_levels = [levels[i]]
        current_zone_indices = {i}
        for j in range(i + 1, len(levels)):
            if j in used_indices: continue
            price_i, price_j = levels[i]['level_price'], levels[j]['level_price']
            if price_i > 0 and (abs(price_j - price_i) / price_i) <= SR_CONFLUENCE_ZONE_PERCENT:
                current_zone_levels.append(levels[j])
                current_zone_indices.add(j)
        
        if len(current_zone_levels) > 1:
            used_indices.update(current_zone_indices)
            total_strength = sum(l['strength'] for l in current_zone_levels)
            if total_strength == 0: continue
            avg_price = sum(l['level_price'] * l['strength'] for l in current_zone_levels) / total_strength
            timeframes = sorted(list(set(str(l['timeframe']) for l in current_zone_levels)))
            details = sorted(list(set(l['level_type'] for l in current_zone_levels)))
            last_tested = max((l['last_tested_at'] for l in current_zone_levels if l['last_tested_at']), default=None)
            confluence_zones.append({
                "level_price": avg_price, "level_type": 'confluence', "strength": float(total_strength),
                "timeframe": ",".join(timeframes), "details": ",".join(details), "last_tested_at": last_tested
            })
            
    remaining_levels = [level for i, level in enumerate(levels) if i not in used_indices]
    return confluence_zones, remaining_levels

def calculate_level_score(level: Dict) -> int:
    """Calculates a score for a given level to rank its importance."""
    score = float(level.get('strength', 1)) * 10
    if level.get('level_type') == 'confluence': score *= 1.5
    if 'Golden' in level.get('details', ''): score += 25
    return int(score)

def analyze_single_symbol_sr(symbol: str, client: Client) -> List[Dict]:
    """Analyzes a single symbol for S/R levels across multiple timeframes."""
    logger.info(f"--- [S/R] بدء تحليل الدعم والمقاومة لـ: {symbol} ---")
    raw_levels = []
    timeframes_config = {
        '1h':  {'days': SR_DATA_FETCH_DAYS_1H},
        '15m': {'days': SR_DATA_FETCH_DAYS_15M},
        '5m':  {'days': SR_DATA_FETCH_DAYS_5M}
    }

    for tf, config in timeframes_config.items():
        df = fetch_historical_data(client, symbol, tf, config['days'])
        if df is not None and not df.empty:
            atr = calculate_atr(df, SR_ATR_PERIOD)
            pa_levels = find_price_action_levels(df, atr)
            fib_levels = calculate_fibonacci_levels(df) if tf == '15m' else []
            
            for level in pa_levels + fib_levels:
                level['timeframe'] = tf
            raw_levels.extend(pa_levels + fib_levels)
        else:
            logger.warning(f"⚠️ [{symbol}-{tf}] تعذر جلب بيانات S/R، سيتم التخطي.")
    
    if not raw_levels: return []

    confluence, singles = find_confluence_zones(raw_levels)
    final_levels = confluence + singles
    for level in final_levels:
        level['symbol'] = symbol
        level['score'] = calculate_level_score(level)
        
    logger.info(f"--- ✅ [S/R] انتهى تحليل {symbol}، تم العثور على {len(final_levels)} مستوى نهائي.")
    return final_levels

def save_sr_levels_to_db(conn: psycopg2.extensions.connection, all_levels: List[Dict]):
    """Batch saves all found S/R levels to the database, deleting old ones first."""
    if not all_levels:
        logger.info("ℹ️ [DB-S/R] لا توجد مستويات لحفظها.")
        return
    
    logger.info(f"⏳ [DB-S/R] جاري حفظ {len(all_levels)} مستوى في قاعدة البيانات...")
    try:
        with conn.cursor() as cur:
            symbols = list(set(level['symbol'] for level in all_levels))
            cur.execute("DELETE FROM support_resistance_levels WHERE symbol = ANY(%s);", (symbols,))
            logger.info(f"[DB-S/R] تم حذف البيانات القديمة لـ {len(symbols)} عملة.")
            
            insert_query = """
                INSERT INTO support_resistance_levels (symbol, level_price, level_type, timeframe, strength, score, last_tested_at, details) 
                VALUES %s ON CONFLICT (symbol, level_price, timeframe, level_type, details) DO NOTHING;
            """
            values = [(l['symbol'], l['level_price'], l['level_type'], l['timeframe'], l['strength'], l['score'], l.get('last_tested_at'), l.get('details')) for l in all_levels]
            execute_values(cur, insert_query, values)
        conn.commit()
        logger.info(f"✅ [DB-S/R] تم حفظ جميع مستويات الدعم والمقاومة بنجاح.")
    except Exception as e:
        logger.error(f"❌ [DB-S/R] خطأ أثناء الحفظ المجمع: {e}", exc_info=True)
        conn.rollback()

def run_sr_analysis(client: Client, conn: psycopg2.extensions.connection, symbols: List[str]):
    """Main function for the S/R analysis part."""
    logger.info("🚀 [S/R] بدء دورة تحليل الدعم والمقاومة...")
    if not symbols:
        logger.warning("[S/R] لا توجد عملات لتحليلها.")
        return

    all_final_levels = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_symbol = {executor.submit(analyze_single_symbol_sr, symbol, client): symbol for symbol in symbols}
        for i, future in enumerate(as_completed(future_to_symbol)):
            symbol = future_to_symbol[future]
            try:
                symbol_levels = future.result()
                if symbol_levels: all_final_levels.extend(symbol_levels)
                logger.info(f"🔄 [S/R] ({i+1}/{len(symbols)}) تمت معالجة نتائج {symbol}.")
            except Exception as e:
                logger.error(f"❌ [S/R] خطأ فادح أثناء تحليل {symbol}: {e}", exc_info=True)

    if all_final_levels:
        all_final_levels.sort(key=lambda x: x.get('score', 0), reverse=True)
        save_sr_levels_to_db(conn, all_final_levels)
    
    logger.info("✅ [S/R] انتهت دورة تحليل الدعم والمقاومة.")

# ==============================================================================
# --- 6. المهمة الرئيسية المجدولة وخادم الويب ---
# ==============================================================================

def main_analysis_job():
    """
    The main scheduled job that runs both Ichimoku and S/R analysis in a loop.
    """
    while True:
        logger.info(f" ciclo de análisis combinado... Próxima ejecución en {RUN_INTERVAL_HOURS} horas.")
        
        client = None
        conn = None
        
        try:
            # تهيئة الاتصالات مرة واحدة في بداية كل دورة
            client = get_binance_client()
            conn = init_db()

            if not client or not conn:
                logger.critical("❌ فشل في تهيئة الاتصالات. سيتم تخطي هذه الدورة.")
            else:
                # التأكد من وجود الجداول
                setup_database_tables(conn)
                
                # الحصول على قائمة العملات مرة واحدة
                symbols_to_process = get_validated_symbols(client, CRYPTO_LIST_FILENAME)
                
                if not symbols_to_process:
                    logger.warning("⚠️ لا توجد عملات صالحة للمعالجة في هذه الدورة.")
                else:
                    # --- تشغيل تحليل إيشيموكو ---
                    run_ichimoku_analysis(client, conn, symbols_to_process)
                    
                    # --- تشغيل تحليل الدعم والمقاومة ---
                    run_sr_analysis(client, conn, symbols_to_process)

        except Exception as e:
            logger.critical(f"❌ حدث خطأ غير متوقع في حلقة العمل الرئيسية: {e}", exc_info=True)
        finally:
            # إغلاق الاتصال بقاعدة البيانات في نهاية كل دورة
            if conn:
                conn.close()
                logger.info("✅ [DB] تم إغلاق اتصال قاعدة البيانات لهذه الدورة.")
        
        logger.info(f"✅ اكتملت الدورة. سيتم الانتظار لمدة {RUN_INTERVAL_HOURS} ساعات.")
        time.sleep(RUN_INTERVAL_HOURS * 60 * 60)

# --- إعداد خادم الويب (Flask) ---
app = Flask(__name__)
@app.route('/')
def health_check():
    """Health check endpoint for the hosting platform."""
    return "✅ Combined Crypto Analyzer (Ichimoku + S/R) service is running.", 200

# --- نقطة انطلاق البرنامج ---
if __name__ == "__main__":
    # تشغيل مهمة التحليل الرئيسية في خيط منفصل
    analysis_thread = Thread(target=main_analysis_job, daemon=True)
    analysis_thread.start()
    
    # تشغيل خادم الويب في الخيط الرئيسي
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🌐 بدء تشغيل خادم فحص الحالة على المنفذ {port}")
    # استخدم 'waitress' أو 'gunicorn' في بيئة الإنتاج بدلاً من app.run()
    app.run(host='0.0.0.0', port=port)
