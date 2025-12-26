import os
import time
import json
import hmac
import base64
import requests
import logging
import pandas as pd
import numpy as np
import warnings
import math
from datetime import datetime, timezone
from dotenv import load_dotenv
from logging.handlers import RotatingFileHandler
from urllib.parse import urlencode
from collections import deque

warnings.filterwarnings("ignore")
load_dotenv()

# --- 日誌設定 ---
if not os.path.exists('logs'): os.makedirs('logs')
if not os.path.exists('pnl_data'): os.makedirs('pnl_data')

log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler = RotatingFileHandler('logs/cbp_grid.log', maxBytes=5*1024*1024, backupCount=5)
file_handler.setFormatter(log_formatter)
file_handler.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
console_handler.setLevel(logging.INFO)

logger = logging.getLogger('root')
logger.setLevel(logging.INFO)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# --- 設定檔 ---
class Config:
    API_KEY = os.getenv("OKX_API_KEY")
    SECRET_KEY = os.getenv("OKX_SECRET_KEY")
    PASSPHRASE = os.getenv("OKX_PASSPHRASE")
    
    _sim_env = str(os.getenv("IS_SIMULATION", "1")).lower()
    IS_SIMULATION = _sim_env in ("true", "1", "yes", "on")
    BASE_URL = "https://www.okx.com"
    
    # 交易對
    PAIRS = ["BTC-USDT-SWAP"] 
    COINBASE_PRODUCT = "BTC-USD"
    
    # 網格與基礎交易設定
    GRID_LEVELS = 5
    BASE_SPREAD = 0.002
    MIN_SZ = {"BTC-USDT-SWAP": 1}
    ORDER_AMOUNT_USDT = 100         # 基礎下單金額 (當 Tanh=0 時)
    CONTRACT_VAL = {"BTC-USDT-SWAP": 0.01} 
    
    # --- Coinbase Premium 策略參數 ---
    TIMEFRAME = "4H"              # 策略主週期 (信號計算)
    CBP_MA = 20                   # Premium Moving Average window
    
    # 濾網 (Filters)
    TAU_LO = 0.006                # Band-pass lower bound
    TAU_HI = 0.010                # Band-pass upper bound
    WR_N = 5                      # William %R window
    WR_LO = -80.0                 # Oversold boundary
    WR_HI = -20.0                 # Overbought boundary
    
    # Tanh Position Sizing
    Z_WINDOW = 20                 # Z-score window
    PS_FLOOR = 0.3                # 基礎倉位比例 (30%)
    PS_ALPHA = 2.5                # Tanh 曲線斜率
    PS_THR = 0.2                  # 噪音過濾門檻
    PS_MAX_MULTIPLIER = 4.0       # 最大放大倍率 (對應 Lmax)

    # --- MCMC 風控參數 (更新版) ---
    MCMC_LOOKAHEAD_MINUTES = 10   # 預測未來 10 分鐘
    MCMC_STEP_SECONDS = 30        # 每步 30 秒
    MCMC_SIM_COUNT = 3000         # 模擬 3000 條路徑
    MCMC_CRASH_THRESHOLD = 0.60   # 若 > 60% 路徑崩盤則觸發防禦

    # 一般風控
    BASE_STOP_LOSS_PCT = 0.02
    IV_HALT_THRESHOLD = 1.00 

# --- 數學工具函數 ---
def tanh_sizing(z_score, floor, alpha, thr):
    """
    計算倉位係數
    Formula: size = floor + (1 - floor) * tanh(alpha * max(|z| - thr, 0))
    """
    if np.isnan(z_score) or np.isinf(z_score):
        return floor
    az = abs(z_score)
    u = max(az - thr, 0.0)
    factor = floor + (1.0 - floor) * np.tanh(alpha * u)
    return factor

def williams_r(high, low, close, n):
    """計算 William %R"""
    hh = high.rolling(int(n)).max()
    ll = low.rolling(int(n)).min()
    return -100.0 * (hh - close) / (hh - ll + 1e-12)

# --- 統計預測與 MCMC 引擎 ---
class StatisticalPredictor:
    def __init__(self):
        # 權重: [1m, 5m, 30m, 60m] - 近期權重較高
        self.weights = np.array([0.45, 0.35, 0.15, 0.05])
        logger.info(f"Statistical Predictor Initialized | Weights: {self.weights}")

    def predict_future_vol(self, feats_df):
        """
        輸入: 包含 ['vol_1m', 'vol_5m', 'vol_30m', 'vol_60m'] 的 DataFrame
        輸出: 預測的未來「每分鐘」波動率標準差
        """
        try:
            if feats_df is None or feats_df.empty:
                return 0.002 

            # 提取特徵值
            v1 = feats_df['vol_1m'].values[0]
            v5 = feats_df['vol_5m'].values[0]
            v30 = feats_df['vol_30m'].values[0]
            v60 = feats_df['vol_60m'].values[0]
            
            vols = np.array([v1, v5, v30, v60])
            pred_vol = np.dot(vols, self.weights)
            return max(pred_vol, 0.0001)
        except Exception as e:
            logger.error(f"Vol Prediction error: {e}")
            return 0.002

    def run_mcmc_simulation(self, current_price, pred_vol_1m, drift=0.0):
        """
        執行 3000 條路徑的蒙地卡羅模擬
        :param pred_vol_1m: 預測的「每分鐘」波動率
        """
        n_sims = Config.MCMC_SIM_COUNT        # 3000
        step_sec = Config.MCMC_STEP_SECONDS   # 30
        total_min = Config.MCMC_LOOKAHEAD_MINUTES # 10
        
        # 計算步數: 10分鐘 / 0.5分鐘 = 20步
        steps_per_min = 60 / step_sec
        n_steps = int(total_min * steps_per_min) 
        
        # 時間增量 dt (以分鐘為單位)
        dt = step_sec / 60.0 
        
        # 調整波動率: sigma_step = sigma_1m * sqrt(dt)
        sigma_step = pred_vol_1m * np.sqrt(dt)
        mu_step = drift * dt

        # 向量化模擬 (幾何布朗運動)
        # 產生 [3000, 20] 的隨機矩陣
        Z = np.random.normal(0, 1, (n_sims, n_steps))
        
        drift_term = mu_step - 0.5 * (sigma_step ** 2)
        log_rets = drift_term + sigma_step * Z
        cum_log_rets = np.cumsum(log_rets, axis=1)
        
        price_paths = current_price * np.exp(cum_log_rets)
        return price_paths

# --- 特徵工程 (用於波動率計算) ---
class FeatureEngine:
    def calc_vol(self, series):
        if len(series) < 2: return 0.0
        return np.std(np.diff(np.log(np.array(series, dtype=float))))

    def compute_features(self, pair, trades, candles):
        try:
            if not trades or not candles: return None
            t_df = pd.DataFrame(trades['data'])
            # K線: [ts, o, h, l, c, ...]
            c_df = pd.DataFrame(candles['data'], columns=["ts","o","h","l","c","v","vc","vq","cf"])
            t_df['ts'] = t_df['ts'].astype(int)
            t_df['px'] = t_df['px'].astype(float)
            c_df['c'] = c_df['c'].astype(float)
            c_df = c_df.iloc[::-1].reset_index(drop=True) 
            
            curr_ts = int(time.time() * 1000)
            
            # 1m vol: 優先用 tick data
            t_1m = t_df[t_df['ts'] > (curr_ts - 60000)]
            if len(t_1m) > 10:
                vol_1m = self.calc_vol(t_1m['px'])
            else:
                vol_1m = self.calc_vol(c_df.tail(2)['c'])
            
            vol_5m = self.calc_vol(c_df.tail(5)['c'])
            vol_30m = self.calc_vol(c_df.tail(30)['c'])
            vol_60m = self.calc_vol(c_df.tail(60)['c'])
            
            return pd.DataFrame([[vol_1m, vol_5m, vol_30m, vol_60m]], 
                                columns=['vol_1m', 'vol_5m', 'vol_30m', 'vol_60m'])
        except Exception as e:
            logger.error(f"Feature Error: {e}")
            return None

# --- Coinbase Client ---
class CoinbaseClient:
    def __init__(self, timeout=10):
        self.base = "https://api.exchange.coinbase.com"
        self.s = requests.Session()
        self.timeout = timeout

    def fetch_candles(self, product_id, granularity=3600, limit=300):
        """
        抓取 Coinbase 現貨 K線 (預設 1H)
        """
        try:
            end = pd.Timestamp.utcnow().tz_localize(None) 
            start = end - pd.Timedelta(seconds=granularity * int(limit))
            
            url = f"{self.base}/products/{product_id}/candles"
            params = {
                "start": start.isoformat(), 
                "end": end.isoformat(), 
                "granularity": str(granularity)
            }
            r = self.s.get(url, params=params, headers={"User-Agent": "cbp-bot"}, timeout=self.timeout)
            
            if r.status_code != 200:
                return pd.DataFrame()

            data = r.json()
            if not data: return pd.DataFrame()

            # [time, low, high, open, close, volume]
            cdf = pd.DataFrame(data, columns=["time","low","high","open","close","volume"])
            cdf["ts"] = pd.to_datetime(cdf["time"].astype(int), unit="s", utc=True)
            cdf = cdf.sort_values("ts").set_index("ts")
            cdf["close"] = pd.to_numeric(cdf["close"], errors="coerce")
            return cdf
        except Exception as e:
            logger.error(f"[Coinbase] Fetch failed: {e}")
            return pd.DataFrame()

# --- OKX Client ---
class OkxClient:
    def __init__(self):
        self.headers = {
            "OK-ACCESS-KEY": Config.API_KEY,
            "OK-ACCESS-PASSPHRASE": Config.PASSPHRASE,
            "x-simulated-trading": "1" if Config.IS_SIMULATION else "0",
            "Content-Type": "application/json"
        }

    def _sign(self, timestamp, method, request_path, body=""):
        message = str(timestamp) + method + request_path + str(body)
        mac = hmac.new(bytes(Config.SECRET_KEY, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
        return base64.b64encode(mac.digest()).decode()

    def _request(self, method, endpoint, params=None, body=None, retries=3):
        for i in range(retries):
            try:
                timestamp = datetime.now(timezone.utc).isoformat()[:-9] + 'Z'
                body_str = json.dumps(body) if body else ""
                request_path = f"{endpoint}?{urlencode(params)}" if method == "GET" and params else endpoint
                url = Config.BASE_URL + request_path
                headers = self.headers.copy()
                headers["OK-ACCESS-SIGN"] = self._sign(timestamp, method, request_path, body_str)
                headers["OK-ACCESS-TIMESTAMP"] = timestamp

                if method == "GET":
                    resp = requests.get(url, headers=headers, timeout=5)
                else:
                    resp = requests.post(url, data=body_str, headers=headers, timeout=5)
                
                data = resp.json()
                if data.get('code') == '0': return data
                if data.get('code') in ['51008', '50001']: return None 
                logger.error(f"[API] {endpoint}: {data.get('code')} - {data.get('msg')}")
                return None
            except Exception as e:
                time.sleep(0.5)
        return None
    
    def _safe_float(self, val):
        try: return float(val) if val else 0.0
        except: return 0.0

    def get_market_data(self, instId, bar="1m"):
        # 獲取指定週期的 K 線
        trades = self._request("GET", "/api/v5/market/trades", params={"instId": instId, "limit": 100})
        candles = self._request("GET", "/api/v5/market/candles", params={"instId": instId, "bar": bar, "limit": 100})
        return trades, candles

    def get_position_details(self, instId):
        res = self._request("GET", "/api/v5/account/positions", params={"instId": instId})
        if res and res.get('data'):
            pos_data = res['data'][0]
            return self._safe_float(pos_data['pos']), self._safe_float(pos_data['upl']), self._safe_float(pos_data['avgPx'])
        return 0.0, 0.0, 0.0

    def get_account_equity(self):
        res = self._request("GET", "/api/v5/account/balance", params={"ccy": "USDT"})
        if res and res.get('data'):
            return self._safe_float(res['data'][0]['details'][0]['eq'])
        return 0.0

    def cancel_all_orders(self, instId):
        pending = self._request("GET", "/api/v5/trade/orders-pending", params={"instId": instId})
        if pending and pending.get('data'):
            ids = [{"instId": instId, "ordId": o['ordId']} for o in pending['data']]
            for i in range(0, len(ids), 20):
                self._request("POST", "/api/v5/trade/cancel-batch-orders", body=ids[i:i+20])
    
    def close_position(self, instId):
        logger.warning(f"[{instId}] EMERGENCY CLOSE!")
        self._request("POST", "/api/v5/trade/close-position", body={"instId": instId, "mgnMode": "cross", "ccy": "USDT", "autoCxl": True})

    def set_position_mode(self, pos_mode="net_mode"):
        res = self._request("POST", "/api/v5/account/set-position-mode", body={"posMode": pos_mode})
        if res and res.get('code') == '0':
            logger.info(f"Position mode set to {pos_mode}")

# --- 核心信號引擎 (CBP + WR) ---
class SmartSignalEngine:
    def __init__(self, cb_client: CoinbaseClient):
        self.cb_client = cb_client
        self.last_analysis_time = 0
        self.cache_z = 0.0
        self.cache_valid = False

    def fetch_and_calculate(self, okx_candles_raw):
        """
        整合 OKX 與 Coinbase 數據，計算 Z-Score
        """
        try:
            # 1. 處理 OKX 數據 (轉為 DataFrame)
            cols = ["ts","open","high","low","close","vol","volCcy","volCcyQuote","confirm"]
            df_okx = pd.DataFrame(okx_candles_raw['data'], columns=cols)
            df_okx["ts"] = pd.to_datetime(df_okx["ts"].astype(np.int64), unit="ms", utc=True)
            for c in ["open","high","low","close"]:
                df_okx[c] = pd.to_numeric(df_okx[c])
            df_okx = df_okx.sort_values("ts").set_index("ts")

            # 2. 抓取 Coinbase 數據 (1H)
            df_cb = self.cb_client.fetch_candles(Config.COINBASE_PRODUCT, granularity=3600, limit=200)
            if df_cb.empty:
                logger.warning("Coinbase data empty.")
                return 0.0

            # 3. 數據對齊 (Resample to 4H)
            df_cb_4h = df_cb["close"].resample("4H").last().dropna()
            
            # 合併數據
            common_idx = df_okx.index.intersection(df_cb_4h.index)
            if len(common_idx) < 20:
                logger.warning("Not enough overlapping data.")
                return 0.0
            
            df = df_okx.loc[common_idx].copy()
            df["cb_close"] = df_cb_4h.loc[common_idx]

            # 4. 計算指標
            # CBP = (Coinbase / OKX) - 1
            df["cbp"] = (df["cb_close"] / df["close"]) - 1.0
            
            # Excess = CBP - MA(CBP)
            df["cbp_ma"] = df["cbp"].rolling(Config.CBP_MA).mean()
            df["excess"] = df["cbp"] - df["cbp_ma"]
            
            # William %R
            df["wr"] = williams_r(df["high"], df["low"], df["close"], Config.WR_N)
            
            # Z-Score of Excess
            mu = df["excess"].rolling(Config.Z_WINDOW).mean()
            std = df["excess"].rolling(Config.Z_WINDOW).std(ddof=0)
            df["z"] = (df["excess"] - mu) / (std + 1e-9)
            
            # 5. 取最新一筆
            last_row = df.iloc[-1]
            z_val = last_row["z"]
            excess_val = last_row["excess"]
            wr_val = last_row["wr"]
            
            # 6. 濾網判定 (Gate Logic)
            gate_band = (abs(excess_val) >= Config.TAU_LO) and (abs(excess_val) <= Config.TAU_HI)
            gate_wr = (wr_val > Config.WR_LO) and (wr_val < Config.WR_HI)
            is_gate_open = gate_band and gate_wr
            
            logger.info(f"[Signal] Z:{z_val:.2f} | Exc:{excess_val:.5f} | WR:{wr_val:.1f} | Gate:{is_gate_open}")

            return z_val if is_gate_open else 0.0

        except Exception as e:
            logger.error(f"Signal Calc Error: {e}")
            return 0.0

    def get_latest_z(self, okx_candles_raw):
        # 緩存機制：每 5 分鐘更新一次 CBP 信號
        if time.time() - self.last_analysis_time > 300 or not self.cache_valid:
            self.cache_z = self.fetch_and_calculate(okx_candles_raw)
            self.last_analysis_time = time.time()
            self.cache_valid = True
        return self.cache_z

class GridTrader:
    def __init__(self, client):
        self.client = client

    def _fmt(self, instId, val, is_price=True):
        if not is_price: return str(int(val))
        return f"{val:.1f}" if "BTC" in instId else f"{val:.4f}"

    def execute_grid(self, instId, price, mode, spread_factor=1.0, size_multiplier=1.0):
        self.client.cancel_all_orders(instId)
        if mode == "HALT":
            logger.warning(f"[{instId}] HALT MODE.")
            return

        orders = []
        
        # --- 倉位計算 (Tanh) ---
        base_usdt = Config.ORDER_AMOUNT_USDT
        # size_multiplier (0.3 ~ 1.0) * Max Multiplier
        target_usdt = base_usdt * size_multiplier * Config.PS_MAX_MULTIPLIER
        
        face_val = Config.CONTRACT_VAL.get(instId, 0.01) 
        contract_price_usdt = price * face_val
        sz_calc = int(target_usdt / contract_price_usdt)
        final_sz = max(Config.MIN_SZ.get(instId, 1), sz_calc)
        sz_str = str(final_sz)
        
        logger.info(f"[{instId}] Sizing: Factor={size_multiplier:.2f} -> Order=${target_usdt:.1f} ({sz_str} cont)")

        spread = Config.BASE_SPREAD * spread_factor
        td_mode = "cross"

        if mode == "LONG_AGGRESSIVE":
            for i in range(1, Config.GRID_LEVELS + 1):
                orders.append({"instId":instId, "tdMode":td_mode, "side":"buy", "ordType":"limit", 
                               "px":self._fmt(instId, price*(1-spread*0.8*i)), "sz":sz_str})
                if i <= 2: 
                    orders.append({"instId":instId, "tdMode":td_mode, "side":"sell", "ordType":"limit", 
                                   "px":self._fmt(instId, price*(1+spread*i)), "sz":sz_str})

        elif mode == "NEUTRAL":
            for i in range(1, Config.GRID_LEVELS + 1):
                orders.append({"instId":instId, "tdMode":td_mode, "side":"buy", "ordType":"limit", 
                               "px":self._fmt(instId, price*(1-spread*i)), "sz":sz_str})
                orders.append({"instId":instId, "tdMode":td_mode, "side":"sell", "ordType":"limit", 
                               "px":self._fmt(instId, price*(1+spread*i)), "sz":sz_str})

        elif mode == "SHORT_DEFENSIVE":
            for i in range(1, Config.GRID_LEVELS + 1):
                orders.append({"instId":instId, "tdMode":td_mode, "side":"sell", "ordType":"limit", 
                               "px":self._fmt(instId, price*(1+spread*1.2*i)), "sz":sz_str})
            orders.append({"instId":instId, "tdMode":td_mode, "side":"buy", "ordType":"limit", 
                           "px":self._fmt(instId, price*(1-spread*4)), "sz":sz_str})

        if orders:
            res = self.client._request("POST", "/api/v5/trade/batch-orders", body=orders)
            logger.info(f"[{instId}] Grid Placed: {len(orders)} orders | Mode: {mode}")

class PnLTracker:
    def __init__(self):
        self.history = []
        self.last_export_time = time.time()
        
    def update(self, pair, price, equity, pos_size, upl, mode, z_score, crash_prob):
        self.history.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "pair": pair, "price": price, "total_equity": equity,
            "position": pos_size, "unrealized_pnl": upl, 
            "mode": mode, "z_score": z_score, "crash_prob": crash_prob
        })
        
    def check_export(self):
        if time.time() - self.last_export_time > 300:
            if not self.history: return
            pd.DataFrame(self.history).to_csv(f"pnl_data/pnl_{datetime.now().strftime('%Y%m%d_%H%M')}.csv", index=False)
            self.history = [] 
            self.last_export_time = time.time()

# --- 主程式 ---
def main():
    print(f"\n{'='*40}\nMODE: {'SIMULATION' if Config.IS_SIMULATION else 'REAL MONEY'}\n{'='*40}\n")
    print(f"Strategy: Coinbase Premium + WR + MCMC(3000)")

    client = OkxClient()
    cb_client = CoinbaseClient()
    
    # 模組初始化
    feat_engine = FeatureEngine()
    stat_predictor = StatisticalPredictor()
    signal_engine = SmartSignalEngine(cb_client)
    bot = GridTrader(client)
    pnl_tracker = PnLTracker()
    
    logger.info("Initializing Account...")
    client.set_position_mode("net_mode")
    
    last_grid_time = {pair: 0 for pair in Config.PAIRS}
    last_mcmc_check = {pair: 0 for pair in Config.PAIRS}
    
    # 緩存預測的波動率
    current_vol_data = {pair: 0.002 for pair in Config.PAIRS} 

    while True:
        try:
            total_equity = client.get_account_equity()

            for pair in Config.PAIRS:
                # 1. 獲取數據 (1m 用於 MCMC/Vol, 4H 用於 CBP Signal)
                trades, candles_1m = client.get_market_data(pair, bar="1m")
                _, candles_4h = client.get_market_data(pair, bar="4H")
                
                if not candles_1m or not candles_4h:
                    logger.warning(f"[{pair}] Waiting for OKX data...")
                    time.sleep(2)
                    continue
                
                curr_price = float(candles_1m['data'][0][4])
                pos, upl, avg_px = client.get_position_details(pair)

                # --- 2. 波動率預測 & MCMC 風控 (每 30 秒執行一次) ---
                prob_crash = 0.0
                if time.time() - last_mcmc_check[pair] > 30:
                    feats = feat_engine.compute_features(pair, trades, candles_1m)
                    if feats is not None:
                        # 預測波動率 (Weighted Vol)
                        pred_vol = stat_predictor.predict_future_vol(feats)
                        current_vol_data[pair] = pred_vol
                        
                        # 執行 MCMC (3000 paths)
                        paths = stat_predictor.run_mcmc_simulation(curr_price, pred_vol, drift=0.0)
                        
                        # 計算崩盤機率 (跌破 2%)
                        target_price_crash = curr_price * 0.98
                        min_prices = np.min(paths, axis=1)
                        hit_target = np.sum(min_prices < target_price_crash)
                        prob_crash = hit_target / Config.MCMC_SIM_COUNT
                        
                        logger.info(f"[{pair}] Vol:{pred_vol:.5f} | CrashProb:{prob_crash:.2%}")
                        last_mcmc_check[pair] = time.time()

                # --- 3. CBP 信號計算 & 倉位大小 ---
                z_score = signal_engine.get_latest_z(candles_4h)
                sizing_factor = tanh_sizing(z_score, Config.PS_FLOOR, Config.PS_ALPHA, Config.PS_THR)

                # --- 風控與止損 ---
                if pos != 0 and avg_px > 0:
                    loss_pct = (curr_price - avg_px)/avg_px if pos > 0 else (avg_px - curr_price)/avg_px
                    if loss_pct < -Config.BASE_STOP_LOSS_PCT:
                        logger.warning(f"[{pair}] STOP LOSS! Loss: {loss_pct:.2%}")
                        client.close_position(pair)
                        client.cancel_all_orders(pair)
                        time.sleep(10)
                        continue

                # --- 4. 決策邏輯 ---
                final_mode = "NEUTRAL"
                spread_factor = 1.0
                
                # MCMC 防禦優先
                if prob_crash > Config.MCMC_CRASH_THRESHOLD:
                    final_mode = "SHORT_DEFENSIVE" # 預測崩盤，強制轉空/防禦
                    spread_factor = 1.2
                    logger.warning(f"[{pair}] MCMC TRIGGER: Crash Prob {prob_crash:.1%}!")
                else:
                    # 正常 CBP 邏輯
                    if z_score > 0.5:
                        final_mode = "LONG_AGGRESSIVE"
                    elif z_score < -0.5:
                        final_mode = "SHORT_DEFENSIVE"
                    else:
                        final_mode = "NEUTRAL"
                
                # 波動率大時，網格掛寬
                if current_vol_data[pair] > 0.005:
                    spread_factor *= 1.5

                # 5. 下單執行
                is_time_up = (time.time() - last_grid_time[pair] > 30)

                if is_time_up:
                    logger.info(f"[{pair}] Exec: {final_mode} | Z:{z_score:.2f} | SizeF:{sizing_factor:.2f}")
                    bot.execute_grid(pair, curr_price, final_mode, spread_factor, size_multiplier=sizing_factor)
                    last_grid_time[pair] = time.time()
                
                pnl_tracker.update(pair, curr_price, total_equity, pos, upl, final_mode, z_score, prob_crash)

            pnl_tracker.check_export()
            time.sleep(1)

        except Exception as e:
            logger.error(f"Main Loop Error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStop.")
        temp_client = OkxClient()
        for pair in Config.PAIRS:
            temp_client.cancel_all_orders(pair)