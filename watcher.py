import os
import time
import hmac
import base64
import hashlib
import signal
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List

import pandas as pd
import requests
from dotenv import load_dotenv
import ta.trend

# ================== Bootstrap ==================
load_dotenv()  # load .env

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("alpha-ema-cross-watcher")

# ---- Config via ENV ----
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()

OKX_API_KEY = os.getenv("OKX_API_KEY", "").strip()
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "").strip()
OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", "").strip()
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://web3.okx.com").strip()

PROXY_URL = os.getenv("PROXY_URL", "").strip()
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "900"))  # default 15m
REQUEST_DELAY_SEC = float(os.getenv("REQUEST_DELAY_SEC", "1.2"))

EMA_WINDOWS = [int(x) for x in os.getenv("EMA_WINDOWS", "144,576").split(",")]
EMA_FAST, EMA_SLOW = EMA_WINDOWS[0], EMA_WINDOWS[1] if len(EMA_WINDOWS) > 1 else 576

TOKENS_RAW = os.getenv("TOKENS", "").strip()

if not all([TG_BOT_TOKEN, TG_CHAT_ID, OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE]):
    logger.warning("Some critical env vars are empty. Fill .env before running.")

proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

# ================== Data Model ==================
@dataclass
class Token:
    name: str
    token_id: str
    chain_id: int
    bar: str = "15m"

def parse_tokens(env_value: str) -> List[Token]:
    tokens: List[Token] = []
    if not env_value:
        return tokens
    groups = [g for g in env_value.split(";") if g.strip()]
    for g in groups:
        parts = [p.strip() for p in g.split(",")]
        if len(parts) < 3:
            logger.warning("Skip token entry (need at least name,token_id,chain_id): %s", g)
            continue
        name, token_id, chain_id = parts[0], parts[1], int(parts[2])
        bar = parts[3] if len(parts) > 3 else "15m"
        tokens.append(Token(name=name, token_id=token_id, chain_id=chain_id, bar=bar))
    return tokens

TOKENS = parse_tokens(TOKENS_RAW)
if not TOKENS:
    logger.warning("No tokens configured. Set TOKENS in .env")

# ================== Helpers ==================
def send_telegram(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.error("Telegram BOT/CHAT not configured.")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        resp = requests.get(url, params=payload, timeout=10, proxies=proxies)
        if resp.status_code != 200:
            logger.error("TG send failed [%s]: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("TG exception: %s", e)

def okx_signature(timestamp: str, method: str, request_path: str, body: str = "") -> str:
    message = timestamp + method + request_path + body
    mac = hmac.new(OKX_API_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def okx_get_candles(token_contract: str, chain_id: int, bar: str = "15m", limit: int = 2000) -> pd.DataFrame:
    path = "/api/v5/dex/market/candles"
    method = "GET"
    params = {
        "chainIndex": str(chain_id),
        "tokenContractAddress": token_contract.lower(),
        "bar": bar,
        "limit": str(limit),
    }
    from urllib.parse import urlencode
    query = "?" + urlencode(params)
    ts = str(time.time())
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": okx_signature(ts, method, path + query),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json",
    }
    url = OKX_BASE_URL + path + query
    try:
        resp = requests.get(url, headers=headers, timeout=15, proxies=proxies)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "0":
            logger.error("OKX candle error: %s", data.get("msg"))
            return pd.DataFrame()
        df = pd.DataFrame(
            data["data"],
            columns=["ts", "open", "high", "low", "close", "vol", "volUsd", "confirm"],
        )
        df[["open", "high", "low", "close", "vol", "volUsd"]] = df[
            ["open", "high", "low", "close", "vol", "volUsd"]
        ].astype(float)
        df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
        df = df.sort_values("ts", ascending=True).reset_index(drop=True)
        return df
    except Exception as e:
        logger.exception("OKX request exception: %s", e)
        return pd.DataFrame()

def calc_ema_series(close: pd.Series, window: int) -> pd.Series:
    return ta.trend.EMAIndicator(close, window=window).ema_indicator()

def detect_strict_cross(df: pd.DataFrame, fast_col: str, slow_col: str, lookback: int = 3) -> List[int]:
    signals: List[int] = []
    for i in range(-lookback, 0):
        prev = df.iloc[i - 1]
        curr = df.iloc[i]
        if pd.notna(prev[fast_col]) and pd.notna(prev[slow_col]) and pd.notna(curr[fast_col]) and pd.notna(curr[slow_col]):
            if prev[fast_col] <= prev[slow_col] and curr[fast_col] > curr[slow_col]:
                signals.append(i)
    return signals

# Graceful shutdown
STOP = False
def _handle_stop(signum, frame):
    global STOP
    STOP = True
    logger.info("Received stop signal (%s). Exiting after current loop...", signum)

signal.signal(signal.SIGINT, _handle_stop)
signal.signal(signal.SIGTERM, _handle_stop)

def run_once(tokens: List[Token]) -> None:
    for t in tokens:
        df = okx_get_candles(t.token_id, t.chain_id, bar=t.bar, limit=max(2000, EMA_SLOW + 10))
        if df.empty or len(df) < EMA_SLOW:
            time.sleep(REQUEST_DELAY_SEC)
            continue

        df[f"EMA{EMA_FAST}"] = calc_ema_series(df["close"], window=EMA_FAST)
        df[f"EMA{EMA_SLOW}"] = calc_ema_series(df["close"], window=EMA_SLOW)

        crosses = detect_strict_cross(df, f"EMA{EMA_FAST}", f"EMA{EMA_SLOW}", lookback=3)
        if crosses:
            last = df.iloc[-1]
            price = last["close"]
            e_fast = last[f"EMA{EMA_FAST}"]
            e_slow = last[f"EMA{EMA_SLOW}"]
            signal_tags = ", ".join([f"EMA{EMA_FAST}/{EMA_SLOW} 金叉@{t.bar}[{i}]" for i in crosses])
            msg = (
                f"📈 *Alpha EMA 金叉信号*\n"
                f"名称: `{t.name}`\n"
                f"价格: `{price:.6f}`\n"
                f"EMA{EMA_FAST}: `{e_fast:.6f}`  |  EMA{EMA_SLOW}: `{e_slow:.6f}`\n"
                f"周期: `{t.bar}`\n"
                f"信号: {signal_tags}"
            )
            send_telegram(msg)
            logger.info("Signal sent: %s | %s", t.name, signal_tags)

        time.sleep(REQUEST_DELAY_SEC)

def main():
    loop = 1
    while not STOP:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("========== Loop %d @ %s ==========", loop, now)
        run_once(TOKENS)
        loop += 1
        if STOP:
            break
        logger.info("Loop done. Sleeping %ss ...", POLL_INTERVAL_SEC)
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
