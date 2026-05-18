import asyncio
import yfinance as yf
import pandas as pd
import json
import threading
import time
import os
import requests
from flask import Flask, render_template_string
from datetime import datetime
import pandas_ta as ta

app = Flask(__name__)

# --- ALMACÉN DE DATOS EN MEMORIA (Más rápido y fiable en la nube) ---
DATA_STORE = {
    "stocks": [],
    "last_sync": "Nunca"
}

WATCHLIST = {
    "GGD.TO": {"name": "Gogold Resources"},
    "KNT.TO": {"name": "K92 Mining"},
    "SB1.DE": {"name": "Smartbroker Holding"},
    "BABA": {"name": "Alibaba Group (ADR)"},
    "ATAI": {"name": "Atai Life Sciences"},
    "NVDA": {"name": "NVIDIA"},
    "TSM": {"name": "TSMC (ADR)"}
}

def agent_loop():
    print("--- 💹 AGENTE EURO-PRO v9.5 (Memory Mode) ---")
    
    while True:
        try:
            print(f"📡 [{datetime.now().strftime('%H:%M:%S')}] Escaneando mercado...")
            
            # Obtener divisas
            usd_to_eur, cad_to_eur = 0.92, 0.68
            try:
                fx = yf.download(["EURUSD=X", "EURCAD=X"], period="1d", progress=False)['Close']
                if not fx.empty:
                    if 'EURUSD=X' in fx: usd_to_eur = 1 / fx['EURUSD=X'].iloc[-1]
                    if 'EURCAD=X' in fx: cad_to_eur = 1 / fx['EURCAD=X'].iloc[-1]
            except: pass

            temp_stocks = []
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
                        return [round(float(x) * mult, 2) for x in df[col].tolist()]

                    limit = 60
                    dates = df.index.strftime('%Y-%m-%d').tolist()[-limit:]
                    prices = [round(float(x) * rate, 2) for x in df['Close'].tolist()][-limit:]
                    ema20_l = get_safe_col(df, 'EMA_20', rate)[-limit:]
                    ema50_l = get_safe_col(df, 'EMA_50', rate)[-limit:]
                    rsi_l = get_safe_col(df, 'RSI')[-limit:]
                    
                    stock_data = {
                        'ticker': ticker,
                        'name': info['name'], 
                        'price_eur': prices[-1],
                        'trend': "Alcista ↑" if prices[-1] > ema20_l[-1] else "Bajista ↓",
                        'buy_price': round(ema20_l[-1], 2), 
                        'stop_loss': round(ema50_l[-1] * 0.97, 2),
                        'commentary': f"RSI: {rsi_l[-1]:.1f}.",
                        'chart_json': json.dumps({
                            'dates': dates, 'prices': prices, 'ema20': ema20_l, 'ema50': ema50_l,
                            'rsi': rsi_l, 'macd': get_safe_col(df, 'macd', rate)[-limit:],
                            'macds': get_safe_col(df, 'macds', rate)[-limit:], 'macdh': get_safe_col(df, 'macdh', rate)[-limit:],
                            'bbu': get_safe_col(df, 'BBU', rate)[-limit:], 'bbl': get_safe_col(df, 'BBL', rate)[-limit:]
                        })
                    }
                    temp_stocks.append(stock_data)
                    print(f"   ✅ {info['name']} analizado.")
                except Exception as e:
                    print(f"   ❌ Error en {ticker}: {e}")

            # Actualizar el almacén global
            DATA_STORE["stocks"] = temp_stocks
            DATA_STORE["last_sync"] = datetime.now().strftime("%H:%M:%S")
            
            print(f"--- ✨ Sincronización Exitosa: {DATA_STORE['last_sync']} ---")
            time.sleep(120)
            
        except Exception as e: 
            print(f"❌ Error Crítico: {e}")
            time.sleep(30)

# Lanzar el agente inmediatamente
threading.Thread(target=agent_loop, daemon=True).start()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8"><title>Terminal PRO Cloud</title>
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
    <div class="d-flex justify-content-between align-items-center mb-4">
        <h1 class="fw-bold text-info">Terminal de Trading Pro</h1>
        <div class="text-end text-muted small">Última actualización: {{ last_sync }}</div>
    </div>
    
    <div class="legend-card p-4 shadow text-center">
        <div class="row">
            <div class="col-md-3 border-end border-secondary"><span class="kpi-title">EMA 20 (Blanca)</span><br><small>Entrada Corto Plazo.</small></div>
            <div class="col-md-3 border-end border-secondary"><span class="kpi-title" style="color:#fbbf24">EMA 50 (Amarilla)</span><br><small>Soporte de Seguridad.</small></div>
            <div class="col-md-3 border-end border-secondary"><span class="kpi-title" style="color:#f87171">MACD / HIST</span><br><small>Barras = Aceleración.</small></div>
            <div class="col-md-3"><span class="kpi-title" style="color:#ff00ff">RSI (Fuerza)</span><br><small>Fuerza del mercado.</small></div>
        </div>
    </div>

    {% if not stocks %}
    <div class="alert alert-info text-center">
        <div class="spinner-border spinner-border-sm me-2"></div>
        Analizando mercados... La primera carga tarda unos 20 segundos. Refresca ahora.
    </div>
    {% endif %}

    {% for s in stocks %}
    <div class="stock-card">
        <div class="row align-items-center mb-4">
            <div class="col-md-4">
                <h2 class="fw-bold mb-1">{{ s.name }}</h2>
                <div class="h3 text-success">{{ "%.2f"|format(s.price_eur) }} €</div>
                <span class="badge {% if '↑' in s.trend %}bg-success{% else %}bg-danger{% endif %}">{{ s.trend }}</span>
            </div>
            <div class="col-md-4"><div class="buy-zone text-center"><small class="text-info fw-bold uppercase">COMPRA IDEAL</small><div class="h3 fw-bold text-white">{{ s.buy_price }} €</div></div></div>
            <div class="col-md-4"><div class="stop-zone text-center"><small class="text-danger fw-bold uppercase">STOP LOSS</small><div class="h3 fw-bold text-white">{{ s.stop_loss }} €</div></div></div>
        </div>
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
    return render_template_string(HTML_TEMPLATE, stocks=DATA_STORE["stocks"], last_sync=DATA_STORE["last_sync"])

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
