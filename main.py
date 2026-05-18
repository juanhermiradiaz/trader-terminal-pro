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

# --- CONFIGURACIÓN DE SEGURIDAD (Render) ---
TG_TOKEN = os.getenv("TG_TOKEN", "")   
TG_CHATID = os.getenv("TG_CHATID", "")

# Configuración de ruta de base de datos
if os.path.exists("/tmp"):
    DB_PATH = "/tmp/tr_terminal.db"
else:
    DB_PATH = "tr_terminal.db"

WATCHLIST = {
    "GGD.TO": {"name": "Gogold Resources"},
    "KNT.TO": {"name": "K92 Mining"},
    "SB1.DE": {"name": "Smartbroker Holding"},
    "BABA": {"name": "Alibaba Group (ADR)"},
    "ATAI": {"name": "Atai Life Sciences"},
    "NVDA": {"name": "NVIDIA"},
    "TSM": {"name": "TSMC (ADR)"}
}

def init_db():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('DROP TABLE IF EXISTS signals') 
    c.execute('''CREATE TABLE signals 
                 (ticker TEXT PRIMARY KEY, name TEXT, price_eur REAL, currency TEXT,
                  trend TEXT, buy_price REAL, stop_loss REAL, commentary TEXT, 
                  chart_json TEXT, last_updated TEXT)''')
    conn.commit(); conn.close()

def save_to_db(ticker, s):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)''',
              (ticker, s['name'], s['price_eur'], s['currency'], s['trend'], 
               s['buy_price'], s['stop_loss'], s['commentary'], json.dumps(s['chart']), s['last_updated']))
    conn.commit(); conn.close()

def get_from_db():
    try:
        conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM signals ORDER BY name ASC').fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except: return []

def send_alert(msg):
    if TG_TOKEN and TG_CHATID:
        try: requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data={"chat_id": TG_CHATID, "text": msg}, timeout=10)
        except: pass

def agent_loop():
    print("--- 💹 AGENTE EURO-PRO v9.1 (Restaurado Completo) ---")
    init_db()
    send_alert("🚀 Terminal Trading Pro Iniciada")
    while True:
        try:
            fx = yf.download(["EURUSD=X", "EURCAD=X"], period="1d", progress=False)['Close']
            usd_to_eur, cad_to_eur = 0.92, 0.68
            if not fx.empty:
                if 'EURUSD=X' in fx: usd_to_eur = 1 / fx['EURUSD=X'].iloc[-1]
                if 'EURCAD=X' in fx: cad_to_eur = 1 / fx['EURCAD=X'].iloc[-1]

            for ticker, info in WATCHLIST.items():
                try:
                    stock = yf.Ticker(ticker)
                    df = stock.history(period="1y")
                    if df.empty or len(df) < 35: continue
                    df = df.ffill().fillna(0)
                    df.ta.rsi(length=14, append=True)
                    df.ta.macd(append=True)
                    df.ta.ema(length=20, append=True)
                    df.ta.ema(length=50, append=True)
                    df.ta.bbands(length=20, append=True)
                    
                    rate = usd_to_eur if ticker in ["BABA", "NVDA", "TSM", "ATAI"] else cad_to_eur if "TO" in ticker else 1.0
                    
                    def get_safe_col(df, key, mult=1.0):
                        matching = [c for c in df.columns if key.lower() in c.lower()]
                        if not matching: return [0.0] * len(df)
                        col = matching[0]
                        if key.lower() == 'macd':
                            m = [c for c in matching if 's' not in c.lower() and 'h' not in c.lower()]
                            if m: col = m[0]
                        elif key.lower() == 'macds':
                            m = [c for c in matching if 's' in c.lower()]
                            if m: col = m[0]
                        elif key.lower() == 'macdh':
                            m = [c for c in matching if 'h' in c.lower()]
                            if m: col = m[0]
                        elif key.lower() == 'bbu':
                            m = [c for c in matching if 'u' in c.lower()]
                            if m: col = m[0]
                        elif key.lower() == 'bbl':
                            m = [c for c in matching if 'l' in c.lower()]
                            if m: col = m[0]
                        return [round(float(x) * mult, 2) for x in df[col].tolist()]

                    limit = 60
                    dates = df.index.strftime('%Y-%m-%d').tolist()[-limit:]
                    prices = [round(float(x) * rate, 2) for x in df['Close'].tolist()][-limit:]
                    ema20_l = get_safe_col(df, 'EMA_20', rate)[-limit:]
                    ema50_l = get_safe_col(df, 'EMA_50', rate)[-limit:]
                    rsi_l = get_safe_col(df, 'RSI')[-limit:]
                    
                    status = "Alcista ↑" if prices[-1] > ema20_l[-1] else "Bajista ↓"
                    
                    data = {
                        'name': info['name'], 'price_eur': prices[-1], 'currency': 'EUR',
                        'trend': status, 'buy_price': round(ema20_l[-1], 2), 'stop_loss': round(ema50_l[-1] * 0.97, 2),
                        'commentary': f"Fase {status}. RSI: {rsi_l[-1]:.1f}.",
                        'last_updated': datetime.now().strftime("%H:%M:%S"),
                        'chart': {
                            'dates': dates, 'prices': prices, 'ema20': ema20_l, 'ema50': ema50_l,
                            'rsi': rsi_l, 'macd': get_safe_col(df, 'macd', rate)[-limit:],
                            'macds': get_safe_col(df, 'macds', rate)[-limit:], 'macdh': get_safe_col(df, 'macdh', rate)[-limit:],
                            'bbu': get_safe_col(df, 'BBU', rate)[-limit:], 'bbl': get_safe_col(df, 'BBL', rate)[-limit:]
                        }
                    }
                    save_to_db(ticker, data)
                    print(f"   ✅ {info['name']} OK.")
                except: continue
            time.sleep(120)
        except Exception as e: print(f"❌ Error: {e}"); time.sleep(30)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8"><title>Terminal Pro Trader</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
    <style>
        body { background: #0b0e14; color: #e2e8f0; font-family: sans-serif; }
        .legend-card { background: #141a21; border: 1px solid #334155; border-radius: 12px; margin-bottom: 30px; }
        .kpi-title { font-weight: bold; font-size: 0.8rem; color: #ffffff !important; }
        .stock-card { background: #141a21; border-radius: 20px; padding: 25px; margin-bottom: 30px; border: 1px solid #232d36; }
        .buy-zone { background: rgba(0, 162, 255, 0.15); border: 2px solid #00a2ff; padding: 15px; border-radius: 12px; }
        .stop-zone { background: rgba(248, 73, 96, 0.15); border: 2px solid #f84960; padding: 15px; border-radius: 12px; }
    </style>
</head>
<body>
<div class="container py-5">
    <h1 class="fw-bold text-info text-center mb-4">Terminal de Trading Profesional</h1>
    
    <div class="legend-card p-4 shadow">
        <div class="row text-center small">
            <div class="col-md-3 border-end border-secondary"><span class="kpi-title">EMA 20 (Blanca)</span><br>Ciclo Corto. Nivel Entrada.</div>
            <div class="col-md-3 border-end border-secondary"><span class="kpi-title" style="color:#fbbf24">EMA 50 (Amarilla)</span><br>Soporte. Nivel Salida.</div>
            <div class="col-md-3 border-end border-secondary"><span class="kpi-title" style="color:#f87171">MACD / HIST</span><br>Barras = Aceleración.</div>
            <div class="col-md-3"><span class="kpi-title" style="color:#ff00ff">RSI (Fuerza)</span><br>Termómetro del mercado.</div>
        </div>
    </div>

    {% if not stocks %}<div class="alert alert-info text-center">Iniciando análisis... Refresca en 30s.</div>{% endif %}
    {% for s in stocks %}
    <div class="stock-card">
        <div class="row align-items-center mb-4">
            <div class="col-md-4">
                <h2 class="fw-bold mb-1">{{ s.name }}</h2>
                <div class="h3 text-success">{{ "%.2f"|format(s.price_eur or 0) }} €</div>
                <span class="badge {% if '↑' in s.trend %}bg-success{% else %}bg-danger{% endif %}">{{ s.trend }}</span>
            </div>
            <div class="col-md-4"><div class="buy-zone text-center"><small class="text-info fw-bold">COMPRA IDEAL</small><div class="h3 fw-bold text-white">{{ "%.2f"|format(s.buy_price or 0) }} €</div></div></div>
            <div class="col-md-4"><div class="stop-zone text-center"><small class="text-danger fw-bold">STOP LOSS</small><div class="h3 fw-bold text-white">{{ "%.2f"|format(s.stop_loss or 0) }} €</div></div></div>
        </div>
        <div class="p-3 bg-dark rounded mb-4" style="border-left: 4px solid #00a2ff;"><b>🤖 Análisis:</b> {{ s.commentary }}</div>
        <div id="chart-{{ s.ticker }}" style="height: 600px;"></div>
    </div>
    {% endfor %}
</div>

<script>
    const stocks = {{ stocks|tojson }};
    stocks.forEach(s => {
        try {
            const c = JSON.parse(s.chart_json);
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
            Plotly.newPlot('chart-' + s.ticker, traces, {
                grid: { rows: 3, cols: 1, pattern: 'independent', roworder: 'top to bottom' },
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false, margin: {t:10, b:40, l:50, r:10},
                xaxis: { gridcolor: '#222', tickfont: {color: '#777'} },
                yaxis: { domain: [0.6, 1], gridcolor: '#222' },
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
    return render_template_string(HTML_TEMPLATE, stocks=get_from_db())

if __name__ == '__main__':
    threading.Thread(target=agent_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
