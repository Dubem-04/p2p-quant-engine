"""
Payment Rail Frictional Cost Matrix, Banking Limits & Counterparty Risk Engine.

Translates theoretical P2P arbitrage signals into net realizable returns by
accounting for payment rail transfer fees, ticket/daily limits, settlement delays,
and counterparty completion risk haircuts.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional, Tuple

from src.signals.spread_models import ArbitrageSignal


class CanonicalRail(str, Enum):
    BANK_TRANSFER_NGN = "BANK_TRANSFER_NGN"
    KUDA = "KUDA"
    OPAY = "OPAY"
    PALMPAY = "PALMPAY"
    REVOLUT = "REVOLUT"
    ZELLE = "ZELLE"
    WISE = "WISE"
    CHIPPER_CASH = "CHIPPER_CASH"
    GENERIC_BANK = "GENERIC_BANK"
    UNKNOWN = "UNKNOWN"


@dataclass
class RailRule:
    """Operational limits and base transfer cost for a canonical rail."""

    daily_limit_fiat: float
    max_ticket_fiat: float
    fixed_fee_fiat: float = 0.0
    variable_fee_pct: float = 0.0
    settlement_minutes: float = 5.0


@dataclass
class AdjustedArbitrageSignal:
    """Friction-adjusted actionable signal with true realizable net yield."""

    base_signal: ArbitrageSignal
    source_rail: CanonicalRail
    target_rail: CanonicalRail
    nominal_profit_fiat: float
    rail_friction_fee_fiat: float
    risk_haircut_fiat: float
    net_realizable_profit_fiat: float
    net_realizable_roi_pct: float
    is_actionable: bool
    rejection_reason: Optional[str] = None
    expected_settlement_minutes: float = 5.0


class PaymentRailNormalizer:
    """Normalizes noisy exchange payment method strings to CanonicalRail."""

    MAPPING_RULES = [
        (re.compile(r"kuda", re.IGNORECASE), CanonicalRail.KUDA),
        (re.compile(r"opay", re.IGNORECASE), CanonicalRail.OPAY),
        (re.compile(r"palmpay", re.IGNORECASE), CanonicalRail.PALMPAY),
        (re.compile(r"chipper", re.IGNORECASE), CanonicalRail.CHIPPER_CASH),
        (re.compile(r"revolut", re.IGNORECASE), CanonicalRail.REVOLUT),
        (re.compile(r"zelle", re.IGNORECASE), CanonicalRail.ZELLE),
        (re.compile(r"wise", re.IGNORECASE), CanonicalRail.WISE),
        (
            re.compile(r"(bank\s*transfer.*nigeria|ngn\s*bank|nigeria)", re.IGNORECASE),
            CanonicalRail.BANK_TRANSFER_NGN,
        ),
        (
            re.compile(r"(bank\s*transfer|wire|instant\s*bank)", re.IGNORECASE),
            CanonicalRail.GENERIC_BANK,
        ),
    ]

    @classmethod
    def normalize(cls, raw_name: str) -> CanonicalRail:
        """Resolves raw payment method text into a CanonicalRail."""
        if not raw_name:
            return CanonicalRail.UNKNOWN

        clean_name = raw_name.strip()
        for pattern, canonical in cls.MAPPING_RULES:
            if pattern.search(clean_name):
                return canonical
        return CanonicalRail.GENERIC_BANK


class PaymentRailCostMatrix:
    """
    Manages pairwise rail transfer costs, banking volume caps, and settlement latencies.
    """

    DEFAULT_RULES: Dict[CanonicalRail, RailRule] = {
        CanonicalRail.BANK_TRANSFER_NGN: RailRule(
            daily_limit_fiat=5_000_000.0,
            max_ticket_fiat=2_000_000.0,
            fixed_fee_fiat=50.0,
            variable_fee_pct=0.0,
            settlement_minutes=10.0,
        ),
        CanonicalRail.KUDA: RailRule(
            daily_limit_fiat=1_000_000.0,
            max_ticket_fiat=500_000.0,
            fixed_fee_fiat=10.0,
            variable_fee_pct=0.0,
            settlement_minutes=3.0,
        ),
        CanonicalRail.OPAY: RailRule(
            daily_limit_fiat=2_000_000.0,
            max_ticket_fiat=1_000_000.0,
            fixed_fee_fiat=10.0,
            variable_fee_pct=0.0,
            settlement_minutes=2.0,
        ),
        CanonicalRail.PALMPAY: RailRule(
            daily_limit_fiat=2_000_000.0,
            max_ticket_fiat=1_000_000.0,
            fixed_fee_fiat=10.0,
            variable_fee_pct=0.0,
            settlement_minutes=2.0,
        ),
        CanonicalRail.REVOLUT: RailRule(
            daily_limit_fiat=20_000.0,
            max_ticket_fiat=5_000.0,
            fixed_fee_fiat=0.0,
            variable_fee_pct=0.0,
            settlement_minutes=3.0,
        ),
        CanonicalRail.ZELLE: RailRule(
            daily_limit_fiat=5_000.0,
            max_ticket_fiat=2_000.0,
            fixed_fee_fiat=0.0,
            variable_fee_pct=0.0,
            settlement_minutes=5.0,
        ),
        CanonicalRail.WISE: RailRule(
            daily_limit_fiat=15_000.0,
            max_ticket_fiat=5_000.0,
            fixed_fee_fiat=0.50,
            variable_fee_pct=0.15,
            settlement_minutes=15.0,
        ),
        CanonicalRail.CHIPPER_CASH: RailRule(
            daily_limit_fiat=5_000_000.0,
            max_ticket_fiat=1_000_000.0,
            fixed_fee_fiat=0.0,
            variable_fee_pct=0.50,
            settlement_minutes=5.0,
        ),
        CanonicalRail.GENERIC_BANK: RailRule(
            daily_limit_fiat=10_000_000.0,
            max_ticket_fiat=5_000_000.0,
            fixed_fee_fiat=25.0,
            variable_fee_pct=0.05,
            settlement_minutes=15.0,
        ),
    }

    def __init__(self, custom_rules: Optional[Dict[CanonicalRail, RailRule]] = None):
        self.rules = self.DEFAULT_RULES.copy()
        if custom_rules:
            self.rules.update(custom_rules)

        # Cross-rail pairwise surcharge matrix: (from_rail, to_rail) -> (fixed_fee, var_fee_pct, extra_lag_mins)
        self.pairwise_overrides: Dict[
            Tuple[CanonicalRail, CanonicalRail], Tuple[float, float, float]
        ] = {}

    def set_pairwise_cost(
        self,
        source: CanonicalRail,
        target: CanonicalRail,
        fixed_fee: float,
        variable_fee_pct: float,
        extra_lag_minutes: float = 0.0,
    ) -> None:
        """Sets customized surcharge when funds must transition between distinct rails."""
        self.pairwise_overrides[(source, target)] = (
            fixed_fee,
            variable_fee_pct,
            extra_lag_minutes,
        )

    def calculate_transfer_friction(
        self,
        source: CanonicalRail,
        target: CanonicalRail,
        amount_fiat: float,
    ) -> Tuple[float, float]:
        """
        Calculates total transfer friction fee and expected settlement duration.
        Returns: (fee_fiat, settlement_minutes)
        """
        source_rule = self.rules.get(
            source, self.DEFAULT_RULES[CanonicalRail.GENERIC_BANK]
        )
        target_rule = self.rules.get(
            target, self.DEFAULT_RULES[CanonicalRail.GENERIC_BANK]
        )

        # Baseline cost on sending rail
        base_fee = source_rule.fixed_fee_fiat + (
            amount_fiat * (source_rule.variable_fee_pct / 100.0)
        )
        duration = max(source_rule.settlement_minutes, target_rule.settlement_minutes)

        if source == target:
            return base_fee, duration

        # Check for cross-rail friction override
        override = self.pairwise_overrides.get((source, target))
        if override:
            fixed, var_pct, extra_lag = override
            cross_fee = fixed + (amount_fiat * (var_pct / 100.0))
            return base_fee + cross_fee, duration + extra_lag

        # Default cross-rail penalty if unmapped (e.g., standard inter-bank cost)
        inter_bank_surcharge = 50.0 + (amount_fiat * 0.001)  # 0.1% friction
        return base_fee + inter_bank_surcharge, duration + 10.0


class CounterpartyRiskModel:
    """
    Evaluates merchant completion statistics to apply a quantitative haircut
    for cancellation risk, dispute lockup, and slow escrow confirmation.
    """

    def __init__(
        self,
        target_completion_rate: float = 0.98,
        min_orders_benchmark: int = 100,
        max_haircut_pct: float = 1.0,
    ):
        self.target_rate = target_completion_rate
        self.min_orders = min_orders_benchmark
        self.max_haircut_pct = max_haircut_pct

    def compute_haircut_pct(
        self,
        completion_rate: float,
        completed_orders: int,
        is_pro: bool,
    ) -> float:
        """
        Computes a percentage penalty deducted from gross yield based on merchant track record.
        """
        haircut = 0.0

        # Rate penalty: e.g. 0.08 deficit (8%) adds 0.16% penalty (deficit * 2.0)
        if completion_rate < self.target_rate:
            deficit = self.target_rate - completion_rate
            haircut += deficit * 2.0

        # Experience penalty: order deficit ratio weighted up to 0.25%
        if completed_orders < self.min_orders:
            order_deficit_ratio = (self.min_orders - completed_orders) / self.min_orders
            haircut += order_deficit_ratio * 0.25

        # Pro merchant discount: 0.10% credit
        if is_pro:
            haircut = max(0.0, haircut - 0.10)

        return min(self.max_haircut_pct, max(0.0, haircut))


class FrictionCostEngine:
    """
    Evaluates raw ArbitrageSignals through the payment rail friction and risk model.
    """

    def __init__(
        self,
        cost_matrix: Optional[PaymentRailCostMatrix] = None,
        risk_model: Optional[CounterpartyRiskModel] = None,
        min_realizable_roi_pct: float = 0.40,
    ):
        self.cost_matrix = cost_matrix or PaymentRailCostMatrix()
        self.risk_model = risk_model or CounterpartyRiskModel()
        self.min_realizable_roi_pct = min_realizable_roi_pct

    def evaluate_signal(
        self,
        signal: ArbitrageSignal,
        source_rail_name: Optional[str] = None,
        target_rail_name: Optional[str] = None,
        buy_merchant_completion_rate: float = 0.99,
        buy_merchant_completed_orders: int = 200,
        buy_merchant_is_pro: bool = True,
        sell_merchant_completion_rate: float = 0.99,
        sell_merchant_completed_orders: int = 200,
        sell_merchant_is_pro: bool = True,
    ) -> AdjustedArbitrageSignal:
        """
        Adjusts an ArbitrageSignal for banking fees, rail transitions, and counterparty risks.
        """
        # Resolve rails
        if source_rail_name:
            source_rail = PaymentRailNormalizer.normalize(source_rail_name)
        elif signal.common_payment_methods:
            source_rail = PaymentRailNormalizer.normalize(
                signal.common_payment_methods[0]
            )
        else:
            source_rail = CanonicalRail.GENERIC_BANK

        if target_rail_name:
            target_rail = PaymentRailNormalizer.normalize(target_rail_name)
        else:
            target_rail = source_rail

        amount_fiat = signal.target_fiat_volume

        # 1. Ticket and Daily Volume Limit Checks
        source_rule = self.cost_matrix.rules.get(
            source_rail, self.cost_matrix.DEFAULT_RULES[CanonicalRail.GENERIC_BANK]
        )
        if amount_fiat > source_rule.max_ticket_fiat:
            return AdjustedArbitrageSignal(
                base_signal=signal,
                source_rail=source_rail,
                target_rail=target_rail,
                nominal_profit_fiat=signal.metadata.get("net_profit_fiat", 0.0),
                rail_friction_fee_fiat=0.0,
                risk_haircut_fiat=0.0,
                net_realizable_profit_fiat=0.0,
                net_realizable_roi_pct=0.0,
                is_actionable=False,
                rejection_reason=f"Ticket amount {amount_fiat} exceeds single ticket limit {source_rule.max_ticket_fiat} for {source_rail.value}",
            )

        # 2. Banking Rail Transfer Friction
        rail_fee_fiat, settlement_minutes = (
            self.cost_matrix.calculate_transfer_friction(
                source=source_rail,
                target=target_rail,
                amount_fiat=amount_fiat,
            )
        )

        # 3. Counterparty Risk Haircut
        buy_haircut_pct = self.risk_model.compute_haircut_pct(
            completion_rate=buy_merchant_completion_rate,
            completed_orders=buy_merchant_completed_orders,
            is_pro=buy_merchant_is_pro,
        )
        sell_haircut_pct = self.risk_model.compute_haircut_pct(
            completion_rate=sell_merchant_completion_rate,
            completed_orders=sell_merchant_completed_orders,
            is_pro=sell_merchant_is_pro,
        )
        total_haircut_pct = buy_haircut_pct + sell_haircut_pct
        risk_haircut_fiat = amount_fiat * (total_haircut_pct / 100.0)

        # 4. Net Realizable Yield
        nominal_profit = signal.metadata.get("net_profit_fiat", 0.0)
        net_profit = nominal_profit - rail_fee_fiat - risk_haircut_fiat
        net_roi_pct = (net_profit / amount_fiat) * 100.0 if amount_fiat > 0 else 0.0

        is_actionable = net_roi_pct >= self.min_realizable_roi_pct
        rejection_reason = None
        if not is_actionable:
            rejection_reason = (
                f"Net realizable ROI {net_roi_pct:.2f}% below hurdle threshold {self.min_realizable_roi_pct:.2f}% "
                f"(Friction fee: {rail_fee_fiat:.2f}, Haircut: {risk_haircut_fiat:.2f})"
            )

        return AdjustedArbitrageSignal(
            base_signal=signal,
            source_rail=source_rail,
            target_rail=target_rail,
            nominal_profit_fiat=nominal_profit,
            rail_friction_fee_fiat=rail_fee_fiat,
            risk_haircut_fiat=risk_haircut_fiat,
            net_realizable_profit_fiat=net_profit,
            net_realizable_roi_pct=net_roi_pct,
            is_actionable=is_actionable,
            rejection_reason=rejection_reason,
            expected_settlement_minutes=settlement_minutes,
        )
