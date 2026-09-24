#!/usr/bin/env python3
"""Pure execution and risk primitives for the BTC 15m paper trader."""
from dataclasses import dataclass
from typing import Iterable, List, Tuple


TAKER_FEE_RATE = 0.07


def taker_fee_per_share(price: float) -> float:
    """Current eligible crypto taker fee per share at token price p."""
    price = float(price)
    if not 0.0 < price < 1.0:
        raise ValueError("price must be between 0 and 1")
    return TAKER_FEE_RATE * price * (1.0 - price)


@dataclass(frozen=True)
class Fill:
    requested_usdc: float
    filled_usdc: float
    shares: float
    gross_cost: float
    fees: float
    average_price: float
    worst_price: float
    levels_used: int

    @property
    def total_cost(self) -> float:
        return self.gross_cost + self.fees

    @property
    def fill_ratio(self) -> float:
        return self.filled_usdc / self.requested_usdc if self.requested_usdc else 0.0


def simulate_market_buy(
    asks: Iterable[Tuple[float, float]],
    budget_usdc: float,
) -> Fill:
    """Walk asks from best to worse price until the budget is consumed."""
    budget_usdc = float(budget_usdc)
    if budget_usdc <= 0:
        raise ValueError("budget_usdc must be positive")

    levels = sorted(
        (float(price), float(size))
        for price, size in asks
        if float(price) > 0 and float(size) > 0
    )
    remaining = budget_usdc
    filled_usdc = 0.0
    shares = 0.0
    gross_cost = 0.0
    fees = 0.0
    worst_price = 0.0
    levels_used = 0

    for price, size in levels:
        if remaining <= 1e-12:
            break
        level_usdc = min(remaining, price * size)
        qty = level_usdc / price
        gross_cost += level_usdc
        shares += qty
        fee = qty * taker_fee_per_share(price)
        fees += fee
        filled_usdc += level_usdc
        remaining -= level_usdc
        worst_price = price
        levels_used += 1

    average_price = gross_cost / shares if shares else 0.0
    return Fill(
        requested_usdc=budget_usdc,
        filled_usdc=filled_usdc,
        shares=shares,
        gross_cost=gross_cost,
        fees=fees,
        average_price=average_price,
        worst_price=worst_price,
        levels_used=levels_used,
    )


def net_expected_edge(model_probability: float, fill: Fill) -> float:
    """Expected profit per share after the simulated fill and fees."""
    if fill.shares <= 0:
        return float("-inf")
    total_cost_per_share = fill.total_cost / fill.shares
    return float(model_probability) - total_cost_per_share


@dataclass
class RiskState:
    initial_capital: float
    cash: float
    realized_pnl_today: float = 0.0

    @classmethod
    def create(cls, capital: float) -> "RiskState":
        capital = float(capital)
        if capital <= 0:
            raise ValueError("capital must be positive")
        return cls(initial_capital=capital, cash=capital)

    def max_position_usdc(self, max_position_fraction: float) -> float:
        return max(0.0, self.initial_capital * float(max_position_fraction))

    def can_enter(
        self,
        requested_usdc: float,
        max_position_fraction: float,
        max_daily_loss_fraction: float,
    ) -> Tuple[bool, str]:
        requested_usdc = float(requested_usdc)
        max_loss = self.initial_capital * float(max_daily_loss_fraction)
        if self.realized_pnl_today <= -max_loss:
            return False, "daily_loss_limit"
        if requested_usdc > self.max_position_usdc(max_position_fraction):
            return False, "position_limit"
        if requested_usdc > self.cash:
            return False, "insufficient_cash"
        return True, "ok"

    def reserve(self, fill: Fill) -> None:
        if fill.total_cost > self.cash + 1e-9:
            raise ValueError("fill exceeds available cash")
        self.cash -= fill.total_cost

    def settle(self, payout: float, entry_cost: float) -> float:
        payout = float(payout)
        entry_cost = float(entry_cost)
        pnl = payout - entry_cost
        self.cash += payout
        self.realized_pnl_today += pnl
        return pnl
