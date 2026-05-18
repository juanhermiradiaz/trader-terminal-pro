import asyncio
import yfinance as yf
import pandas as pd
import sqlite3
import json
import threading
import time
import os
import requests
from flask import Flask, render_template_string
from datetime import datetime
import pandas_ta as ta

app = Flask(__name__)

# --- CONFIGURACIÓN DE RUTA ---
if os.name == 'nt':
    DB_PATH = os.path.join(os.getcwd(), "tr_terminal.db")
else:
    DB_PATH = "/tmp/tr_terminal.db"

WATCHLIST = {
    "CA38045Y1025": {"name": "Gogold Resources", "ticker": "GGD.TO"},
    "CA4991131083": {"name": "K92 Mining", "ticker": "KNT.TO"},
    "DE000A2GS609": {"name": "Smartbroker Holding", "ticker": "SB1.DE"},
    "US01609W1027": {"name": "Alibaba Group (ADR)", "ticker": "BABA"},
    "US04650F1012": {"name": "Atai Life Sciences", "ticker": "ATAI"},
    "US67066G1040": {"name": "NVIDIA", "ticker": "NVDA"},
    "US8740391003": {"name": "TSMC (ADR)", "ticker": "TSM"}
}

AGENT_LOGS = []

def add_log(msg):
    global AGENT_LOGS
    now = datetime.now().strftime("%H:%M:%S")
    AGENT_LOGS.insert(0, f"[{now}] {msg}")
    if len(AGENT_LOGS) > 10: AGENT_LOGS.pop()
    print(f"DEBUG: {msg}")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS signals 
                 (isin TEXT PRIMARY KEY, name TEXT, price_eur REAL, currency TEXT,
                  trend TEXT, buy_price REAL, stop_loss REAL, commentary TEXT, 
                  chart_json TEXT, last_updated TEXT)''')
    conn.commit()
    conn.close()

def save_to_db(isin, s):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)''',
                  (isin, s['name'], s['price_eur'], s['currency'], s['trend'], 
               s['buy_price'], s['stop_loss'], s['commentary'], json.dumps(s['chart']), s['last_updated']))
        conn.commit()
        conn.close()
    except Exception as e:
        add_log(f"Error DB: {e}")

def get_from_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM signals ORDER BY name ASC').fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except: return []

def agent_loop():
    add_log(f"Iniciando Agente en {DB_PATH}")
    init_db()
    while True:
        try:
            add_log("Descargando divisas...")
            fx = yf.download(["EURUSD=X", "EURCAD=X"], period="1d", progress=False)['Close']
            u_e, c_e = 0.92, 0.68
            if not fx.empty:
                if 'EURUSD=X' in fx: u_e = 1 / fx['EURUSD=X'].iloc[-1]
                if 'EURCAD=X' in fx: c_e = 1 / fx['EURCAD=X'].iloc[-1]

            for isin, info in WATCHLIST.items():
                try:
                    add_log(f"Analizando {info['name']}...")
                    stock = yf.Ticker(info['ticker'])
                    df = stock.history(period="1y")
                    if df.empty: 
                        add_log(f"Sin datos para {info['ticker']}")
                        continue
                    
                    df = df.ffill().fillna(0)
                    df.ta.rsi(append=True); df.ta.macd(append=True); df.ta.ema(length=20, append=True); df.ta.ema(length=50, append=True); df.ta.bbands(append=True)
                    
                    rate = u_e if isin.startswith("US") else c_e if isin.startswith("CA") else 1.0
                    
                    def get_safe_col(key, mult=1.0):
                        match = [c for c in df.columns if key.lower() in c.lower()]
                        if not match: return [0.0]*60
                        col = match[0]
                        if key.lower() == 'macd':
                            m_pure = [c for c in match if 's' not in c.lower() and 'h' not in c.lower()]
                            if m_pure: col = m_pure[0]
                        return [round(float(x)*mult, 2) for x in df[col].fillna(0).tolist()[-60:]]

                    p_eur = df['Close'].iloc[-1] * rate
                    ema20 = get_safe_col('EMA_20', rate)
                    ema50 = get_safe_col('EMA_50', rate)
                    
                    data = {
                        'name': info['name'], 'price_eur': p_eur, 'currency': 'EUR',
                        'trend': "Alcista ↑" if p_eur > ema20[-1] else "Bajista ↓",
                        'buy_price': round(ema20[-1], 2), 'stop_loss': round(ema50[-1]*0.97, 2),
                        'commentary': f"RSI: {get_safe_col('RSI')[-1]}",
                        'last_updated': datetime.now().strftime("%H:%M:%S"),
                        'chart': {
                            'dates': df.index.strftime('%Y-%m-%d').tolist()[-60:],
                            'prices': [round(x*rate, 2) for x in df['Close'].tolist()[-60:]],
                            'ema20': ema20, 'ema50': ema50, 'rsi': get_safe_col('RSI'),
                            'macd': get_safe_col('macd', rate), 'macds': get_safe_col('macds', rate), 'macdh': get_safe_col('macdh', rate),
                            'bbu': get_safe_col('BBU', rate), 'bbl': get_safe_col('BBL', rate)
                        }
                    }
                    save_to_db(isin, data)
                    add_log(f"{info['name']} guardado.")
                except Exception as e:
                    add_log(f"Error {info['name']}: {e}")
            
            add_log("Ciclo completado. Esperando 10 min.")
            time.sleep(600)
        except Exception as e:
            add_log(f"Error Global: {e}")
            time.sleep(30)

threading.Thread(target=agent_loop, daemon=True).start()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8"><title>Terminal PRO v9.8</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body { background: #0b0e14; color: #e2e8f0; font-family: sans-serif; }
        .stock-card { background: #141a21; border-radius: 20px; padding: 25px; margin-bottom: 30px; border: 1px solid #232d36; }
        .debug-panel { background: #000; border: 1px solid #ff00ff; padding: 15px; font-family: monospace; font-size: 0.7rem; color: #00ff00; }
    </style>
</head>
<body>
<div class="container py-5">
    <h1 class="fw-bold text-info text-center mb-4">Terminal de Trading Pro</h1>
    
    {% if not stocks %}
    <div class="alert alert-warning text-center">
        ⏳ <b>Estado:</b> El agente está analizando el mercado.<br>
        Espera 15 segundos y <b>pulsa F5</b>.
    </div>
    {% endif %}

    {% for s in stocks %}
    <div class="stock-card">
        <div class="row align-items-center mb-4">
            <div class="col-md-4">
                <h2 class="fw-bold mb-1">{{ s.name }}</h2>
                <div class="h3 text-success">{{ "%.2f"|format(s.price_eur or 0) }} €</div>
                <span class="badge {% if '↑' in s.trend %}bg-success{% else %}bg-danger{% endif %}">{{ s.trend }}</span>
            </div>
            <div class="col-md-4 text-center"><small class="text-info d-block">ENTRADA</small><div class="h3 fw-bold text-white">{{ s.buy_price }} €</div></div>
            <div class="col-md-4 text-center"><small class="text-danger d-block">STOP LOSS</small><div class="h3 fw-bold text-white">{{ s.stop_loss }} €</div></div>
        </div>
        <div id="chart-{{ s.isin }}" style="height: 500px;"></div>
    </div>
    {% endfor %}

    <div class="debug-panel mt-5">
        <b>[SYSTEM LOGS]</b><br>
        DB: {{ db_info.path }} | Acciones: {{ stocks|length }}/7<br>
        --- ÚLTIMAS TRAZAS ---<br>
        {% for log in logs %}
        {{ log }}<br>
        {% endfor %}
    </div>
</div>
<script>
    const stocks = {{ stocks|tojson }};
    stocks.forEach(s => {
        try {
            const c = JSON.parse(s.chart_json);
            const traces = [
                { x: c.dates, y: c.prices, name: 'Precio', type: 'scatter', line: {color: '#00a2ff', width: 3} },
                { x: c.dates, y: c.ema20, name: 'EMA20', type: 'scatter', line: {color: '#fff', dash: 'dot'} },
                { x: c.dates, y: c.ema50, name: 'EMA50', type: 'scatter', line: {color: '#fbbf24', dash: 'dot'} },
                { x: c.dates, y: c.macd, name: 'MACD', type: 'scatter', line: {color: '#fbbf24'}, yaxis: 'y2' },
                { x: c.dates, y: c.macdh, name: 'Hist', type: 'bar', marker: {color: '#38bdf8'}, yaxis: 'y2' },
                { x: c.dates, y: c.rsi, name: 'RSI', type: 'scatter', line: {color: '#ff00ff'}, yaxis: 'y3' }
            ];
            Plotly.newPlot('chart-' + s.isin, traces, {
                grid: { rows: 3, cols: 1, pattern: 'independent' },
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false, margin: {t:10, b:40, l:50, r:10},
                xaxis: { gridcolor: '#222' }, yaxis: { domain: [0.6, 1], gridcolor: '#222' },
                yaxis2: { domain: [0.3, 0.55], gridcolor: '#222' },
                yaxis3: { domain: [0, 0.25], gridcolor: '#222', range: [0, 100] }
            }, {responsive: true});
        } catch (err) {}
    });
</script>
</body>
</html>
"""

@app.route('/')
def index():
    stocks = get_from_db()
    db_info = {"path": DB_PATH, "now": datetime.now().strftime("%H:%M:%S")}
    return render_template_string(HTML_TEMPLATE, stocks=stocks, db_info=db_info, logs=AGENT_LOGS)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
