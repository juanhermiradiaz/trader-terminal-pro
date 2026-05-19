import asyncio
import yfinance as yf
import pandas as pd
import json
import os
import requests
from flask import Flask, render_template_string, request
from datetime import datetime
import pandas_ta as ta

app = Flask(__name__)

# --- Mapeo ISIN a Ticker ---
ISIN_TO_TICKER = {
    "CA38045Y1025": "GGD.TO", "CA4991131083": "KNT.TO", "DE000A2GS609": "SB1.DE",
    "US01609W1027": "BABA", "US04650F1012": "ATAI", "US67066G1040": "NVDA", "US8740391003": "TSM"
}

def analyze_stock(identifier):
    ticker = ISIN_TO_TICKER.get(identifier.upper(), identifier.upper())
    try:
        # 1. Obtener Divisas
        fx = yf.download(["EURUSD=X", "EURCAD=X"], period="1d", progress=False)['Close']
        u_e, c_e = 0.92, 0.68
        if not fx.empty:
            if 'EURUSD=X' in fx: u_e = 1 / fx['EURUSD=X'].iloc[-1]
            if 'EURCAD=X' in fx: c_e = 1 / fx['EURCAD=X'].iloc[-1]

        # 2. Descargar Datos
        stock = yf.Ticker(ticker)
        df = stock.history(period="1y")
        if df.empty or len(df) < 50:
            return {"error": f"No hay datos suficientes para {identifier}."}
        
        df = df.ffill().fillna(0)
        df.ta.rsi(length=14, append=True)
        df.ta.macd(append=True)
        df.ta.ema(length=20, append=True)
        df.ta.ema(length=50, append=True)
        df.ta.bbands(length=20, append=True)
        
        currency = stock.info.get('currency', 'USD')
        rate = u_e if currency == 'USD' else c_e if currency == 'CAD' else 1.0
        
        # 3. Extraer Indicadores (Safe Mode)
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
        rsi_val = rsi[-1]
        
        # --- GENERAR ARGUMENTOS TÉCNICOS ---
        args = []
        trend_status = "ALCISTA" if curr_p > ema20[-1] else "BAJISTA"
        
        if trend_status == "ALCISTA":
            args.append(f"Dominio comprador: precio sobre EMA20 ({ema20[-1]}€).")
        else:
            args.append(f"Presión vendedora: precio bajo EMA20 ({ema20[-1]}€).")

        if macd[-1] > macds[-1]:
            args.append("MACD con impulso positivo (Línea amarilla sobre roja).")
        else:
            args.append("MACD con impulso negativo, debilidad de momento.")

        if rsi_val < 35:
            args.append(f"RSI en {rsi_val:.1f} (Sobreventa). Rebote probable.")
        elif rsi_val > 65:
            args.append(f"RSI en {rsi_val:.1f} (Sobrecompra). Riesgo de corrección.")
        else:
            args.append(f"RSI neutral ({rsi_val:.1f}). Sin agotamiento.")

        if curr_p < bbl[-1] * 1.02:
            args.append("Precio en base de Bollinger. Soporte estadístico.")

        rec = "MANTENER"
        if trend_status == "ALCISTA" and rsi_val < 60 and macd[-1] > macds[-1]: rec = "COMPRA"
        if trend_status == "BAJISTA" and rsi_val > 65: rec = "VENTA"
        if rsi_val < 30: rec = "COMPRA (Rebote)"

        return {
            'name': stock.info.get('longName', identifier),
            'price_eur': curr_p, 'trend': trend_status,
            'buy_price': round(ema20[-1], 2), 'stop_loss': round(ema50[-1] * 0.97, 2),
            'recommendation': rec, 'arguments': args,
            'chart': {
                'dates': df.index.strftime('%Y-%m-%d').tolist()[-60:],
                'prices': prices, 'ema20': ema20, 'ema50': ema50,
                'rsi': rsi, 'macd': macd, 'macds': macds, 'macdh': macdh,
                'bbu': bbu, 'bbl': bbl
            }
        }
    except Exception as e: return {"error": str(e)}

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
    <meta charset="UTF-8"><title>Analista Pro v1.2.1</title>
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
        .rec-MANTENER { color: #fbbf24; font-weight: bold; }
    </style>
</head>
<body>
<div class="container py-5">
    <h1 class="text-center text-info fw-bold mb-5">Terminal Técnica On-Demand</h1>
    <form method="POST" class="input-group mb-5 shadow-lg" style="max-width: 600px; margin: 0 auto;">
        <input type="text" name="id" class="form-control bg-dark text-white" placeholder="Ticker o ISIN (ej: NVDA)" required>
        <button type="submit" class="btn btn-info px-4">ANALIZAR</button>
    </form>
    {% if result %}
        {% if result.error %}<div class="alert alert-danger">{{ result.error }}</div>
        {% else %}
        <div class="card-stock">
            <div class="row align-items-center mb-4 text-center">
                <div class="col-md-4 text-md-start">
                    <h2 class="fw-bold mb-0">{{ result.name }}</h2>
                    <div class="h2 text-success">{{ "%.2f"|format(result.price_eur) }} €</div>
                    <span class="badge bg-secondary">{{ result.trend }}</span>
                </div>
                <div class="col-md-4"><div class="buy-box"><small class="text-info">ENTRADA</small><div class="h3 text-white mb-0">{{ result.buy_price }} €</div></div></div>
                <div class="col-md-4"><div class="stop-box"><small class="text-danger">STOP LOSS</small><div class="h4 text-white mb-0">{{ result.stop_loss }} €</div></div></div>
            </div>
            <div class="arg-box mb-4">
                <h5 class="text-info">🤖 Informe Estratégico: <span class="rec-{{ result.recommendation.split(' ')[0] }}">{{ result.recommendation }}</span></h5>
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
                { x: c.dates, y: c.macdh, name: 'Hist', type: 'bar', marker: {color: '#38bdf8'}, yaxis: 'y2' },
                { x: c.dates, y: c.rsi, name: 'RSI', type: 'scatter', line: {color: '#ff00ff'}, yaxis: 'y3' }
            ];
            Plotly.newPlot('main-chart', traces, {
                grid: { rows: 3, cols: 1, pattern: 'independent', roworder: 'top to bottom' },
                paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                showlegend: false, margin: {t:10, b:40, l:50, r:10},
                xaxis: { gridcolor: '#222' },
                yaxis: { domain: [0.6, 1], gridcolor: '#222' },
                yaxis2: { domain: [0.3, 0.55], gridcolor: '#222' },
                yaxis3: { domain: [0, 0.25], gridcolor: '#222', range: [0, 100] }
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
