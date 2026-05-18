import asyncio
import yfinance as yf
import pandas as pd
import sqlite3
import json
import threading
import time
import os
import traceback
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
    if len(AGENT_LOGS) > 15: AGENT_LOGS.pop()
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
    add_log("🚀 Agente v10.0 iniciado")
    init_db()
    while True:
        try:
            add_log("Sincronizando divisas...")
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
                        add_log(f"Sin datos para {info['name']}")
                        continue
                    
                    df = df.ffill().fillna(0)
                    df.ta.rsi(append=True)
                    df.ta.macd(append=True)
                    df.ta.ema(length=20, append=True)
                    df.ta.ema(length=50, append=True)
                    df.ta.bbands(length=20, std=2, append=True)
                    
                    rate = u_e if isin.startswith("US") else c_e if isin.startswith("CA") else 1.0
                    
                    def get_safe_col(df, key, mult=1.0):
                        match = [c for c in df.columns if key.lower() in c.lower()]
                        if not match: return [0.0]*60
                        col = match[0]
                        if key.lower() == 'macd' and 'h' not in col.lower() and 's' not in col.lower():
                            m_pure = [c for c in match if 's' not in c.lower() and 'h' not in c.lower()]
                            if m_pure: col = m_pure[0]
                        elif key.lower() == 'macds':
                            m_sig = [c for c in match if 's' in c.lower()]
                            if m_sig: col = m_sig[0]
                        elif key.lower() == 'macdh':
                            m_h = [c for c in match if 'h' in c.lower()]
                            if m_h: col = m_h[0]
                        elif key.lower() == 'bbu':
                            m_u = [c for c in match if 'u' in c.lower()]
                            if m_u: col = m_u[0]
                        elif key.lower() == 'bbl':
                            m_l = [c for c in match if 'l' in c.lower()]
                            if m_l: col = m_l[0]
                        return [round(float(x)*mult, 2) for x in df[col].fillna(0).tolist()[-60:]]

                    prices = [round(x*rate, 2) for x in df['Close'].tolist()[-60:]]
                    ema20 = get_safe_col(df, 'EMA_20', rate)
                    ema50 = get_safe_col(df, 'EMA_50', rate)
                    
                    data = {
                        'name': info['name'], 'price_eur': prices[-1], 'currency': 'EUR',
                        'trend': "Alcista ↑" if prices[-1] > ema20[-1] else "Bajista ↓",
                        'buy_price': round(ema20[-1], 2), 'stop_loss': round(ema50[-1]*0.97, 2),
                        'commentary': f"RSI: {get_safe_col(df, 'RSI')[-1]} | Sinc: {datetime.now().strftime('%H:%M:%S')}",
                        'last_updated': datetime.now().strftime("%H:%M:%S"),
                        'chart': {
                            'dates': df.index.strftime('%Y-%m-%d').tolist()[-60:],
                            'prices': prices,
                            'ema20': ema20, 'ema50': ema50,
                            'rsi': get_safe_col(df, 'RSI'),
                            'macd': get_safe_col(df, 'macd', rate),
                            'macds': get_safe_col(df, 'macds', rate),
                            'macdh': get_safe_col(df, 'macdh', rate),
                            'bbu': get_safe_col(df, 'BBU', rate),
                            'bbl': get_safe_col(df, 'BBL', rate)
                        }
                    }
                    save_to_db(isin, data)
                    add_log(f"✅ {info['name']} guardado.")
                except Exception as e:
                    add_log(f"Error {info['name']}: {str(e)[:50]}")
            
            add_log("Ciclo completo. Esperando 10 min.")
            time.sleep(600)
        except Exception as e:
            add_log(f"Error Global: {str(e)[:50]}")
            time.sleep(30)

threading.Thread(target=agent_loop, daemon=True).start()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8"><title>Terminal PRO Trader v10.0</title>
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

    {% if not stocks %}
    <div class="alert alert-info text-center">
        <div class="spinner-border spinner-border-sm me-2"></div>
        <b>Sincronizando sistemas efímeros de Render...</b><br>
        Espera 15 segundos y refresca la página para ver los primeros datos.
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
            <div class="col-md-4"><div class="buy-zone text-center"><small class="text-info fw-bold">COMPRA IDEAL</small><div class="h3 fw-bold text-white">{{ "%.2f"|format(s.buy_price or 0) }} €</div></div></div>
            <div class="col-md-4"><div class="stop-zone text-center"><small class="text-danger fw-bold">STOP LOSS</small><div class="h3 fw-bold text-white">{{ "%.2f"|format(s.stop_loss or 0) }} €</div></div></div>
        </div>
        <div class="p-2 bg-dark rounded mb-4" style="border-left: 4px solid #00a2ff;"><b>Análisis:</b> {{ s.commentary }}</div>
        <div id="chart-{{ s.isin.replace('.','-') }}" style="height: 600px;"></div>
    </div>
    {% endfor %}

    <div class="p-3 rounded mt-5" style="background:#000; border:1px solid #333; font-family:monospace; font-size:0.75rem; color:#00ff00;">
        <b>[ESTADO DEL AGENTE EN LA NUBE]</b><br>
        Archivo DB: {{ db_info.path }} | Acciones: {{ stocks|length }}/7<br>
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
            Plotly.newPlot('chart-' + s.isin.replace('.','-'), traces, {
                grid: { rows: 3, cols: 1, pattern: 'independent', roworder: 'top to bottom' },
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false, margin: {t:10, b:40, l:50, r:10},
                xaxis: { gridcolor: '#222', tickfont: {color: '#777'} },
                yaxis: { domain: [0.6, 1], gridcolor: '#222' },
                yaxis2: { domain: [0.3, 0.55], gridcolor: '#222' },
                yaxis3: { domain: [0, 0.25], gridcolor: '#222', range: [0, 100] }
            }, {responsive: true});
        } catch (err) {
            console.error("Error en " + s.name, err);
        }
    });
</script>
</body>
</html>
"""

@app.route('/')
def index():
    stocks = get_from_db()
    db_info = {"path": DB_PATH}
    return render_template_string(HTML_TEMPLATE, stocks=stocks, db_info=db_info, logs=AGENT_LOGS)

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
