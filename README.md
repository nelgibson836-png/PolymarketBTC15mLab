# PolymarketBTC15mLab

Laboratorio para validar si las señales obtenidas de Binance tienen valor económico al operar mercados BTC de 15 minutos en Polymarket.

## Objetivo

Comparar la probabilidad estimada por el modelo con la probabilidad implícita en Polymarket:

`edge = P(modelo) - P(mercado)`

El proyecto comienza únicamente con datos históricos y lectura pública. No utiliza credenciales privadas ni coloca órdenes.

## Fases

1. Descubrimiento y captura de mercados BTC 15m.
2. Captura de precios históricos de los tokens.
3. Sincronización con datos BTC de Binance.
4. Backtest de edge y valor esperado.
5. Paper trading.
6. Operación real únicamente después de validar resultados.

## Fuentes

- Polymarket Gamma API para mercados y eventos.
- Polymarket CLOB API para precios históricos.
- Binance Market Lab como fuente independiente de datos BTC.

## Principio

Este repositorio permanece separado de `binance-market-lab` para evitar contaminar la validación del modelo de mercado con la validación económica específica de Polymarket.


## Paper trading

The project now includes a continuous paper trader that uses public live Binance 1m kline data and the Polymarket market WebSocket.

It simulates:
- model signal at minutes 3 and 5
- live Polymarket ask-book execution with multiple levels
- crypto taker fees
- fill depth and partial-fill rejection
- one position per market
- daily loss and position caps
- stale-data protection
- SQLite persistence
- public settlement and realized P&L

Run locally:

`pip install -r requirements.txt`

`python paper_trader.py --reset`

The paper trader never places real orders and does not read private credentials. Local SQLite files are ignored by Git.

The current paper configuration is intentionally small by default:
- initial capital: 10
- target position: 1
- minimum net edge: 5%
- minimum fill ratio: 80%
- maximum position: 20% of initial capital
- daily loss stop: 10%

These values are research controls, not performance guarantees.

## Path to live

1. Historical economic backtest.
2. Continuous paper trading with live order-book execution.
3. Risk, reconnect, duplicate, stale-data and settlement validation.
4. Small controlled live pilot only after the paper gates pass.
