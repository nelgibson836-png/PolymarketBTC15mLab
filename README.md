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
