import asyncio
import pandas as pd
import json
import os
import time
import requests
import urllib3
import re
from flask import Flask, render_template_string, request
from datetime import datetime
import pandas_ta as ta

app = Flask(__name__)

ANALYSIS_CACHE_TTL_SECONDS = int(os.environ.get("ANALYSIS_CACHE_TTL_SECONDS", "0"))
ANALYSIS_CACHE = {}


# --- Yahoo Finance Direct API (crumb-based, no yfinance library) ---
_YF_SESSION = None
_YF_CRUMB = None
_YF_VERIFY_SSL = os.environ.get("YF_VERIFY_SSL", "false").lower() in {"1", "true", "yes"}

if not _YF_VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_YF_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _refresh_yf_session():
    global _YF_SESSION, _YF_CRUMB
    session = requests.Session()
    session.headers.update({
        "User-Agent": _YF_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })

    def _valid_crumb(text):
        """A crumb is a short token, never HTML."""
        return text and 3 < len(text) < 200 and "<" not in text

    def _try_consent(resp):
        """Auto-submit GDPR consent form if Yahoo redirected to consent page."""
        try:
            text = resp.text
            if "csrfToken" not in text:
                return
            csrf = re.search(r'name=["\']csrfToken["\']\s+value=["\']([^"\']+)["\']', text)
            sess_id = re.search(r'name=["\']sessionId["\']\s+value=["\']([^"\']+)["\']', text)
            if csrf and sess_id:
                session.post(
                    "https://consent.yahoo.com/v2/collectConsent",
                    params={"sessionId": sess_id.group(1)},
                    data={
                        "csrfToken": csrf.group(1),
                        "sessionId": sess_id.group(1),
                        "originalDoneUrl": "https://finance.yahoo.com/",
                        "namespace": "yahoo",
                        "agree": ["agree", "agree"],
                    },
                    timeout=10, verify=_YF_VERIFY_SSL,
                )
        except Exception:
            pass

    def _try_getcrumb():
        for host in ("query2", "query1"):
            try:
                r = session.get(
                    f"https://{host}.finance.yahoo.com/v1/test/getcrumb",
                    timeout=10, verify=_YF_VERIFY_SSL,
                )
                if r.status_code == 200 and _valid_crumb(r.text.strip()):
                    return r.text.strip()
            except Exception:
                pass
        return None

    def _extract_crumb_from_page(text):
        m = re.search(r'"crumb"\s*:\s*"([^"]{5,}?)"', text)
        if m:
            candidate = m.group(1)
            try:
                candidate = candidate.encode("utf-8").decode("unicode_escape")
            except Exception:
                pass
            if _valid_crumb(candidate):
                return candidate
        return None

    crumb = None

    # Strategy 1: visit finance.yahoo.com homepage (auto-handles GDPR consent), then getcrumb
    try:
        r = session.get("https://finance.yahoo.com/", timeout=15, verify=_YF_VERIFY_SSL)
        _try_consent(r)
        crumb = _try_getcrumb()
    except Exception:
        pass

    # Strategy 2: visit a specific quote page, extract crumb from page JS
    if not crumb:
        try:
            page = session.get(
                "https://finance.yahoo.com/quote/AAPL",
                timeout=15, verify=_YF_VERIFY_SSL,
            )
            _try_consent(page)
            crumb = _try_getcrumb() or _extract_crumb_from_page(page.text)
        except Exception:
            pass

    # Strategy 3: fc.yahoo.com consent bypass (EU cloud IPs / GDPR flow)
    if not crumb:
        try:
            session.get("https://fc.yahoo.com", timeout=10, verify=_YF_VERIFY_SSL)
            page = session.get(
                "https://finance.yahoo.com/quote/AAPL",
                timeout=15, verify=_YF_VERIFY_SSL,
            )
            _try_consent(page)
            crumb = _try_getcrumb() or _extract_crumb_from_page(page.text)
        except Exception:
            pass

    # Strategy 4: probe chart API without crumb — works on many US cloud IPs
    if not crumb:
        try:
            r = session.get(
                "https://query2.finance.yahoo.com/v8/finance/chart/AAPL",
                params={"range": "1d", "interval": "1h", "includePrePost": "false"},
                timeout=12, verify=_YF_VERIFY_SSL,
            )
            if r.status_code == 200:
                crumb = ""  # crumb-less mode — API responded without it
        except Exception:
            pass

    if crumb is None:
        raise RuntimeError("No se pudo obtener el crumb de Yahoo Finance")

    _YF_SESSION = session
    _YF_CRUMB = crumb
    return _YF_SESSION, _YF_CRUMB


def _get_yf_session():
    global _YF_SESSION, _YF_CRUMB
    if _YF_SESSION is not None and _YF_CRUMB is not None:
        return _YF_SESSION, _YF_CRUMB
    return _refresh_yf_session()


def _yf_chart(ticker, range_="1y", interval="1d"):
    """Fetch Yahoo Finance chart data. Retries once on auth error."""
    for attempt in range(2):
        session, crumb = _get_yf_session()
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        params = {
            "range": range_,
            "interval": interval,
            "includePrePost": "false",
        }
        if crumb:  # omit crumb param in crumb-less mode
            params["crumb"] = crumb
        resp = session.get(url, params=params, timeout=15, verify=_YF_VERIFY_SSL)
        if resp.status_code in (401, 403) and attempt == 0:
            _refresh_yf_session()
            continue
        if resp.status_code == 429:
            raise RuntimeError("rate_limit")
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()  # pragma: no cover


def _download_history(ticker):
    """Return (DataFrame with OHLCV, currency_str, current_market_price_or_None)."""
    data = _yf_chart(ticker, range_="1y", interval="1d")
    result = (data.get("chart") or {}).get("result")
    if not result:
        return pd.DataFrame(), "USD", None
    result = result[0]
    meta = result.get("meta", {})
    currency = meta.get("currency", "USD")
    current_price = meta.get("regularMarketPrice")
    timestamps = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote", [{}])[0]
    if not timestamps:
        return pd.DataFrame(), currency, current_price
    df = pd.DataFrame(
        {
            "Open":   quotes.get("open",   [None] * len(timestamps)),
            "High":   quotes.get("high",   [None] * len(timestamps)),
            "Low":    quotes.get("low",    [None] * len(timestamps)),
            "Close":  quotes.get("close",  [None] * len(timestamps)),
            "Volume": quotes.get("volume", [None] * len(timestamps)),
        },
        index=pd.to_datetime(
            [datetime.utcfromtimestamp(t) for t in timestamps]
        ),
    )
    df.index.name = "Date"
    return df, currency, current_price


def _get_last_price(ticker):
    """Return the last market price for a ticker (used for FX rates)."""
    try:
        data = _yf_chart(ticker, range_="1d", interval="1m")
        result = (data.get("chart") or {}).get("result")
        if result:
            return result[0].get("meta", {}).get("regularMarketPrice")
    except Exception:
        pass
    return None


def get_cached_analysis(identifier):
    cached = ANALYSIS_CACHE.get(identifier)
    if not cached:
        return None

    if time.time() - cached["timestamp"] > ANALYSIS_CACHE_TTL_SECONDS:
        ANALYSIS_CACHE.pop(identifier, None)
        return None

    return cached["data"]


def set_cached_analysis(identifier, data):
    ANALYSIS_CACHE[identifier] = {"timestamp": time.time(), "data": data}

# --- Mapeo ISIN a Ticker ---
ISIN_TO_TICKER = {
    "CA38045Y1025": "GGD.TO", "CA4991131083": "KNT.TO", "DE000A2GS609": "SB1.DE",
    "US01609W1027": "BABA", "US04650F1012": "ATAI", "US67066G1040": "NVDA", "US8740391003": "TSM"
}

_ISIN_PATTERN = re.compile(r'^[A-Z]{2}[A-Z0-9]{10}$')

def _resolve_isin_to_ticker(isin: str) -> str:
    """Query Yahoo Finance search to resolve an unmapped ISIN to a ticker symbol."""
    session, _crumb = _get_yf_session()
    try:
        resp = session.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": isin, "quotesCount": 5, "newsCount": 0, "enableFuzzyQuery": False},
            timeout=10,
            verify=_YF_VERIFY_SSL,
        )
        if resp.status_code != 200:
            app.logger.warning("ISIN search %s → HTTP %s", isin, resp.status_code)
            return isin
        data = resp.json()
        # Response structure: {"quotes": [{"symbol": "VOW3.DE", ...}], ...}
        quotes = data.get("quotes", [])
        if quotes:
            symbol = quotes[0].get("symbol", isin)
            app.logger.info("ISIN %s resolved to %s", isin, symbol)
            return symbol
        app.logger.warning("ISIN search %s returned no quotes: %s", isin, data)
    except Exception as exc:
        app.logger.warning("ISIN resolve error for %s: %s", isin, exc)
    return isin


def analyze_stock(identifier):
    normalized_identifier = identifier.upper()
    cached_result = get_cached_analysis(normalized_identifier)
    if cached_result:
        return cached_result

    ticker = ISIN_TO_TICKER.get(normalized_identifier, normalized_identifier)
    # If still looks like an ISIN (not in static map), try dynamic resolution
    if ticker == normalized_identifier and _ISIN_PATTERN.match(normalized_identifier):
        ticker = _resolve_isin_to_ticker(normalized_identifier)
    try:
        # 1. Obtener Divisas
        u_e, c_e = 0.92, 0.68
        try:
            fx_usd = _get_last_price("EURUSD=X")
            u_e = 1 / fx_usd if fx_usd else 0.92
            fx_cad = _get_last_price("EURCAD=X")
            c_e = 1 / fx_cad if fx_cad else 0.68
        except Exception:
            pass

        # 2. Descargar Datos
        df, currency, market_price = _download_history(ticker)
        if df.empty or len(df) < 50:
            return {"error": f"No hay datos suficientes para {identifier}."}
        
        # 3. Cálculos Técnicos
        df = df.ffill().fillna(0)
        df.ta.rsi(length=14, append=True)
        df.ta.macd(append=True)
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.bbands(length=20, append=True)
        df.ta.atr(length=14, append=True)
        
        rate = u_e if currency == 'USD' else c_e if currency == 'CAD' else 1.0
        
        def gs(df, k, m=1.0):
            cols = [c for c in df.columns if k.lower() in c.lower()]
            if not cols: return [0.0]*60
            col = cols[0]
            if k.lower() == 'macd' and len(match := [c for c in cols if 's' not in c.lower() and 'h' not in c.lower()]) > 0: col = match[0]
            if k.lower() == 'macds' and len(match := [c for c in cols if 's' in c.lower()]) > 0: col = match[0]
            if k.lower() == 'macdh' and len(match := [c for c in cols if 'h' in c.lower()]) > 0: col = match[0]
            if k.lower() == 'bbu' and len(match := [c for c in cols if 'u' in c.lower()]) > 0: col = match[0]
            if k.lower() == 'bbl' and len(match := [c for c in cols if 'l' in c.lower()]) > 0: col = match[0]
            return [round(float(x)*m, 2) for x in df[col].fillna(0).tolist()[-60:]]

        limit = 60
        prices = [round(float(x)*rate, 2) for x in df['Close'].tolist()][-limit:]
        ema20 = gs(df, 'EMA_20', rate)
        ema50 = gs(df, 'EMA_50', rate)
        rsi = gs(df, 'RSI')
        macd = gs(df, 'macd', rate)
        macds = gs(df, 'macds', rate)
        macdh = gs(df, 'macdh', rate)
        bbu = gs(df, 'BBU', rate)
        bbl = gs(df, 'BBL', rate)
        
        curr_p = prices[-1]
        # Use real-time market price if available (regularMarketPrice from API meta)
        if market_price:
            curr_p = round(float(market_price) * rate, 2)
        rsi_val = rsi[-1]
        
        # --- GENERAR ARGUMENTOS ---
        args = []
        trend_status = "ALCISTA" if curr_p > ema20[-1] else "BAJISTA"
        if trend_status == "ALCISTA":
            args.append(f"Fuerza alcista confirmada: cotiza sobre EMA20 ({ema20[-1]}€).")
        else:
            args.append(f"Debilidad estructural: cotiza bajo EMA20 ({ema20[-1]}€).")

        if macd[-1] > macds[-1]:
            args.append("MACD en fase de aceleración positiva.")
        else:
            args.append("MACD indicando pérdida de impulso alcista.")

        if rsi_val < 35:
            args.append(f"RSI en {rsi_val:.1f} (Sobreventa). Potencial rebote técnico.")
        elif rsi_val > 65:
            args.append(f"RSI en {rsi_val:.1f} (Sobrecompra). Riesgo de corrección.")

        rec = "NEUTRAL"
        if trend_status == "ALCISTA" and rsi_val < 60 and macd[-1] > macds[-1]: rec = "COMPRA"
        if trend_status == "BAJISTA" and rsi_val > 65: rec = "VENTA"
        if rsi_val < 30: rec = "COMPRA (Rebote)"

        entry_price = round(ema20[-1], 2)
        # ATR-based stop: entry - 1.5 × ATR14 (adapted to actual volatility)
        atr_col = next((c for c in df.columns if 'ATR' in c.upper()), None)
        atr_val = float(df[atr_col].dropna().iloc[-1]) * rate if atr_col else abs(entry_price * 0.05)
        stop_price = round(entry_price - 1.5 * atr_val, 2)
        risk_pct = round((entry_price - stop_price) / entry_price * 100, 1) if entry_price else 0

        result = {
            'name': ticker,
            'price_eur': curr_p, 'trend': trend_status,
            'buy_price': entry_price, 'stop_loss': stop_price,
            'risk_pct': risk_pct,
            'recommendation': rec, 'arguments': args,
            'chart': {
                'dates': df.index.strftime('%Y-%m-%d').tolist()[-limit:],
                'prices': prices, 'ema20': ema20, 'ema50': ema50,
                'rsi': rsi, 'macd': macd, 'macds': macds, 'macdh': macdh,
                'bbu': bbu, 'bbl': bbl
            }
        }
        set_cached_analysis(normalized_identifier, result)
        return result
    except RuntimeError as e:
        if "rate_limit" in str(e):
            stale_result = ANALYSIS_CACHE.get(normalized_identifier, {}).get("data")
            if stale_result:
                return stale_result
            return {
                "error": "Yahoo Finance está limitando temporalmente las peticiones. Espera unos minutos y vuelve a intentarlo."
            }
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}

@app.route('/', methods=['GET', 'POST'])
def index():
    res = None
    if request.method == 'POST':
        res = analyze_stock(request.form['id'])
    return render_template_string(HTML_TEMPLATE, result=res)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8"><title>Terminal Pro Cloud</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body { background: #0b0e14; color: #e2e8f0; font-family: sans-serif; }
        .card-stock { background: #141a21; border-radius: 15px; padding: 25px; border: 1px solid #232d36; }
        .buy-box { background: rgba(0, 162, 255, 0.1); border: 2px solid #00a2ff; padding: 15px; border-radius: 12px; }
        .stop-box { background: rgba(248, 73, 96, 0.1); border: 2px solid #f84960; padding: 15px; border-radius: 12px; }
        .arg-box { background: #1c2630; border-radius: 10px; padding: 15px; border-left: 4px solid #38bdf8; }
        .rec-COMPRA { color: #02c076; font-weight: bold; }
        .rec-VENTA { color: #f84960; font-weight: bold; }
        .rec-NEUTRAL { color: #fbbf24; font-weight: bold; }
    </style>
</head>
<body>
<div class="container py-5 text-center">
    <h1 class="text-info fw-bold mb-4">Terminal de Inteligencia</h1>
    <form method="POST" class="input-group mb-5 shadow-lg" style="max-width: 500px; margin: 0 auto;">
        <input type="text" name="id" class="form-control bg-dark text-white" placeholder="Ticker o ISIN" required>
        <button type="submit" class="btn btn-info px-4">ANALIZAR</button>
    </form>
    {% if result %}
        {% if result.error %}<div class="alert alert-danger">{{ result.error }}</div>
        {% else %}
        <div class="card-stock text-start">
            <div class="row align-items-center mb-4 text-center">
                <div class="col-md-4 text-md-start">
                    <h2 class="fw-bold mb-0">{{ result.name }}</h2>
                    <div class="h2 text-success">{{ "%.2f"|format(result.price_eur) }} €</div>
                    <span class="badge bg-secondary">{{ result.trend }}</span>
                </div>
                <div class="col-md-4"><div class="buy-box"><small class="text-info">ENTRADA</small><div class="h3 text-white mb-0">{{ result.buy_price }} €</div></div></div>
                <div class="col-md-4"><div class="stop-box"><small class="text-danger">STOP LOSS</small><div class="h4 text-white mb-0">{{ result.stop_loss }} €</div><small class="text-muted">Riesgo: {{ result.risk_pct }}%</small></div></div>
            </div>
            <div class="arg-box mb-4">
                <h5>🤖 Estrategia: <span class="rec-{{ result.recommendation.split(' ')[0] }}">{{ result.recommendation }}</span></h5>
                <ul class="small mb-0">{% for arg in result.arguments %}<li>{{ arg }}</li>{% endfor %}</ul>
            </div>
            <div id="main-chart" style="height: 650px;"></div>
        </div>
        <script>
            const c = {{ result.chart|tojson }};
            const traces = [
                { x: c.dates, y: c.prices, name: 'Precio', type: 'scatter', line: {color: '#00a2ff', width: 3}, yaxis: 'y' },
                { x: c.dates, y: c.ema20, name: 'EMA20', type: 'scatter', line: {color: '#fff', dash: 'dot'}, yaxis: 'y' },
                { x: c.dates, y: c.ema50, name: 'EMA50', type: 'scatter', line: {color: '#fbbf24', dash: 'dot'}, yaxis: 'y' },
                { x: c.dates, y: c.bbu, name: 'BBU', type: 'scatter', line: {color: 'rgba(255,255,255,0.1)'}, yaxis: 'y' },
                { x: c.dates, y: c.bbl, name: 'BBL', type: 'scatter', line: {color: 'rgba(255,255,255,0.1)'}, fill: 'tonexty', fillcolor: 'rgba(255,255,255,0.03)', yaxis: 'y' },
                { x: c.dates, y: c.macd, name: 'MACD', type: 'scatter', line: {color: '#fbbf24'}, yaxis: 'y2' },
                { x: c.dates, y: c.macds, name: 'Señal', type: 'scatter', line: {color: '#f84960'}, yaxis: 'y2' },
                { x: c.dates, y: c.macdh, name: 'Hist', type: 'bar', marker: {color: 'rgba(56,189,248,0.5)'}, yaxis: 'y2' },
                { x: c.dates, y: c.rsi, name: 'RSI', type: 'scatter', line: {color: '#ff00ff'}, yaxis: 'y3' }
            ];
            Plotly.newPlot('main-chart', traces, {
                grid: { rows: 3, cols: 1, pattern: 'independent', roworder: 'top to bottom' },
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false, margin: {t:10, b:40, l:50, r:10},
                xaxis: { gridcolor: '#222' },
                yaxis: { domain: [0.6, 1], gridcolor: '#222', title: 'Precio' },
                yaxis2: { domain: [0.3, 0.55], gridcolor: '#222', title: 'MACD' },
                yaxis3: { domain: [0, 0.25], gridcolor: '#222', title: 'RSI', range: [0, 100] }
            }, {responsive: true});
        </script>
        {% endif %}
    {% endif %}
</div>
</body>
</html>
"""

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
