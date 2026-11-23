import csv
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

from p2p_quant_engine.analytics.config import (
    CRYPTO_SYMBOL,
    DEFAULT_REPORT_TIMEZONE,
    FIAT_SYMBOL,
)
from p2p_quant_engine.portfolio.ledger import (
    AskCycleState,
    BidCycleState,
    PortfolioLedger,
    TradeFillRecord,
)
from p2p_quant_engine.risk.account_rotator import AccountRotator


def _to_utc_datetime(
    value: datetime | date, default_tz: timezone = DEFAULT_REPORT_TIMEZONE
) -> datetime:
    """Convert a date or datetime (naive or aware) into a UTC-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=default_tz).astimezone(timezone.utc)
        return value.astimezone(timezone.utc)
    # Plain date -> start of day in default_tz
    dt = datetime.combine(value, time.min, tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def _end_of_day_utc(
    value: datetime | date, default_tz: timezone = DEFAULT_REPORT_TIMEZONE
) -> datetime:
    """Convert a date to 23:59:59.999999 in default_tz (UTC), or normalize datetime."""
    if isinstance(value, datetime):
        return _to_utc_datetime(value, default_tz=default_tz)
    dt = datetime.combine(value, time.max, tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class CyclePerformanceSummary:
    """Aggregated performance metrics for a single Ask or Bid cycle."""

    cycle_id: str
    side: str  # "ASK" or "BID"
    status: str  # "ACTIVE" or "CLOSED"
    trades_in_window: int
    crypto_volume: float
    fiat_volume: float
    fees_in_window_fiat: float
    realized_pnl_in_window_fiat: float


@dataclass(frozen=True)
class PerformanceReport:
    """Comprehensive financial performance report over a specified timeframe."""

    start_iso: str
    end_iso: str
    timezone_label: str
    sell_trade_count: int
    buy_trade_count: int
    total_crypto_sold: float
    total_crypto_acquired: float
    total_fiat_inflow: float
    total_fiat_outflow: float
    weighted_avg_sell_price: float
    weighted_avg_buy_price: float
    weighted_avg_cost_basis: float
    gross_sell_spread_fiat: float
    exchange_fees_fiat: float
    bank_fees_fiat: float
    funding_fees_fiat: float
    total_fees_fiat: float
    realized_net_pnl_fiat: float
    net_profit_per_crypto_sold: float
    return_on_deployed_capital_pct: float
    cycles: list[CyclePerformanceSummary] = field(default_factory=list)
    trades: list[TradeFillRecord] = field(default_factory=list)
    bank_headroom_snapshot: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize full report to a JSON-compatible dictionary."""
        return asdict(self)

    def export_json(self, output_path: Path | str) -> Path:
        """Write full structured report to a JSON file."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        return path

    def export_csv(self, output_path: Path | str) -> Path:
        """Export filtered trade fills inside the timeframe to a CSV audit file."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "settled_at",
            "order_id",
            "cycle_id",
            "side",
            "crypto_amount",
            "fiat_amount",
            "price_fiat",
            "cost_basis_fiat_per_crypto",
            "exchange_fee_fiat",
            "bank_fee_fiat",
            "realized_net_pnl_fiat",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for trade in self.trades:
                row = asdict(trade)
                writer.writerow({k: row[k] for k in fieldnames})
        return path

    def render_text(self) -> str:
        """Render a clean, human-readable CLI summary table."""
        sep = "=" * 68
        sub = "-" * 68
        lines = [
            sep,
            f"P2P QUANT ENGINE — PERFORMANCE REPORT ({self.timezone_label})",
            f"Window: {self.start_iso.split('.')[0]}  ->  {self.end_iso.split('.')[0]}",
            sep,
            "VOLUME & EXECUTION",
            f"  Sell Trades (Ask):   {self.sell_trade_count:>6d}   | "
            f"Sold:     {self.total_crypto_sold:>12,.2f} {CRYPTO_SYMBOL}",
            f"  Buy Trades (Bid):    {self.buy_trade_count:>6d}   | "
            f"Acquired: {self.total_crypto_acquired:>12,.2f} {CRYPTO_SYMBOL}",
            f"  Fiat Inflow (NGN):   {FIAT_SYMBOL}{self.total_fiat_inflow:>14,.2f}",
            f"  Fiat Outflow (NGN):  {FIAT_SYMBOL}{self.total_fiat_outflow:>14,.2f}",
            sub,
            "PRICING & MARGINS",
            "  Avg Sell Price:      "
            f"{FIAT_SYMBOL}{self.weighted_avg_sell_price:>14,.2f}",
            "  Avg Buy Price:       "
            f"{FIAT_SYMBOL}{self.weighted_avg_buy_price:>14,.2f}",
            "  Avg Cost Basis(WAC): "
            f"{FIAT_SYMBOL}{self.weighted_avg_cost_basis:>14,.2f}",
            "  Gross Sell Spread:   "
            f"{FIAT_SYMBOL}{self.gross_sell_spread_fiat:>14,.2f}",
            sub,
            "FEES & BOTTOM-LINE PNL",
            f"  Exchange Fees:       {FIAT_SYMBOL}{self.exchange_fees_fiat:>14,.2f}",
            f"  Bank Transfer Fees:  {FIAT_SYMBOL}{self.bank_fees_fiat:>14,.2f}",
            f"  Cycle Funding Fees:  {FIAT_SYMBOL}{self.funding_fees_fiat:>14,.2f}",
            f"  Total Fees Paid:     {FIAT_SYMBOL}{self.total_fees_fiat:>14,.2f}",
            f"  REALIZED NET PNL:    {FIAT_SYMBOL}{self.realized_net_pnl_fiat:>14,.2f}",
            f"  Net Profit / {CRYPTO_SYMBOL}:   "
            f"{FIAT_SYMBOL}{self.net_profit_per_crypto_sold:>14,.2f}",
            f"  Return on Capital:   {self.return_on_deployed_capital_pct:>15.2f}%",
        ]

        if self.cycles:
            lines.extend([sub, "CYCLE BREAKDOWN"])
            lines.append(
                f"  {'Cycle':<9} {'Status':<8} {'Trades':>6} "
                f"{'Volume (' + CRYPTO_SYMBOL + ')':>15} {'Net PnL (NGN)':>18}"
            )
            for c in self.cycles:
                lines.append(
                    f"  {c.cycle_id:<9} {c.status:<8} {c.trades_in_window:>6d} "
                    f"{c.crypto_volume:>15,.2f} "
                    f"{c.realized_pnl_in_window_fiat:>18,.2f}"
                )

        if self.bank_headroom_snapshot:
            lines.extend([sub, "BANK ACCOUNT DAILY HEADROOM"])
            for acc_id, rem in self.bank_headroom_snapshot.items():
                lines.append(
                    f"  {acc_id:<18} Remaining Daily Limit: {FIAT_SYMBOL}{rem:>14,.2f}"
                )

        lines.append(sep)
        return "\n".join(lines)


class ReportGenerator:
    """Generates timeframe-filtered financial reports from PortfolioLedger data."""

    def __init__(
        self,
        ledger: PortfolioLedger,
        account_rotator: Optional[AccountRotator] = None,
        report_timezone: timezone = DEFAULT_REPORT_TIMEZONE,
    ) -> None:
        self.ledger = ledger
        self.account_rotator = account_rotator
        self.report_timezone = report_timezone

    def _collect_all_cycles(
        self,
    ) -> tuple[dict[str, AskCycleState], dict[str, BidCycleState]]:
        """Map all completed and active Ask and Bid cycles by cycle_id."""
        ask_map: dict[str, AskCycleState] = {
            c.cycle_id: c for c in self.ledger.completed_ask_cycles
        }
        if self.ledger.active_ask_cycle is not None:
            ask_map[self.ledger.active_ask_cycle.cycle_id] = (
                self.ledger.active_ask_cycle
            )

        bid_map: dict[str, BidCycleState] = {
            c.cycle_id: c for c in self.ledger.completed_bid_cycles
        }
        if self.ledger.active_bid_cycle is not None:
            bid_map[self.ledger.active_bid_cycle.cycle_id] = (
                self.ledger.active_bid_cycle
            )

        return ask_map, bid_map

    def generate(
        self,
        start: datetime | date,
        end: datetime | date,
    ) -> PerformanceReport:
        """
        Compute performance metrics across [start, end] inclusive.

        Naive dates default to 00:00:00 (start) and 23:59:59.999999 (end) in WAT.
        """
        start_utc = _to_utc_datetime(start, default_tz=self.report_timezone)
        end_utc = _end_of_day_utc(end, default_tz=self.report_timezone)

        if end_utc < start_utc:
            raise ValueError("Report end timestamp cannot be earlier than start.")

        filtered_trades: list[TradeFillRecord] = []
        for trade in self.ledger.trade_history:
            settled_utc = datetime.fromisoformat(trade.settled_at).astimezone(
                timezone.utc
            )
            if start_utc <= settled_utc <= end_utc:
                filtered_trades.append(trade)

        sell_trades = [t for t in filtered_trades if t.side == "SELL"]
        buy_trades = [t for t in filtered_trades if t.side == "BUY"]

        total_crypto_sold = sum(t.crypto_amount for t in sell_trades)
        total_fiat_inflow = sum(t.fiat_amount for t in sell_trades)
        total_crypto_acquired = sum(t.crypto_amount for t in buy_trades)
        total_fiat_outflow = sum(t.fiat_amount for t in buy_trades)

        avg_sell_price = (
            total_fiat_inflow / total_crypto_sold if total_crypto_sold > 0 else 0.0
        )
        avg_buy_price = (
            total_fiat_outflow / total_crypto_acquired
            if total_crypto_acquired > 0
            else 0.0
        )
        weighted_wac = (
            sum(t.cost_basis_fiat_per_crypto * t.crypto_amount for t in sell_trades)
            / total_crypto_sold
            if total_crypto_sold > 0
            else 0.0
        )
        gross_sell_spread = sum(
            (t.price_fiat - t.cost_basis_fiat_per_crypto) * t.crypto_amount
            for t in sell_trades
        )

        exchange_fees = sum(t.exchange_fee_fiat for t in filtered_trades)
        bank_fees = sum(t.bank_fee_fiat for t in filtered_trades)

        # Include funding fees for cycles started inside the report window
        ask_map, bid_map = self._collect_all_cycles()
        funding_fees = 0.0
        deployed_capital_fiat = 0.0

        for ask_c in ask_map.values():
            started_utc = datetime.fromisoformat(ask_c.started_at).astimezone(
                timezone.utc
            )
            if start_utc <= started_utc <= end_utc:
                funding_fees += ask_c.funding_fee_fiat
                deployed_capital_fiat += (
                    ask_c.initial_crypto * ask_c.avg_cost_basis_fiat
                )

        for bid_c in bid_map.values():
            started_utc = datetime.fromisoformat(bid_c.started_at).astimezone(
                timezone.utc
            )
            if start_utc <= started_utc <= end_utc:
                funding_fees += bid_c.funding_fee_fiat
                deployed_capital_fiat += bid_c.initial_fiat

        total_fees = exchange_fees + bank_fees + funding_fees

        # SELL fills already deduct sell-side fees in realized_net_pnl_fiat;
        # BUY-side fees are capitalized into WAC, so only subtract funding fees.
        sell_net_pnl = sum(t.realized_net_pnl_fiat for t in sell_trades)
        realized_net_pnl = sell_net_pnl - funding_fees

        net_per_crypto = (
            realized_net_pnl / total_crypto_sold if total_crypto_sold > 0 else 0.0
        )
        roc_pct = (
            (realized_net_pnl / deployed_capital_fiat) * 100.0
            if deployed_capital_fiat > 0
            else 0.0
        )

        # Build per-cycle summaries for cycles touched in this window
        cycle_summaries: list[CyclePerformanceSummary] = []
        touched_cycle_ids = sorted({t.cycle_id for t in filtered_trades})
        for cid in touched_cycle_ids:
            c_trades = [t for t in filtered_trades if t.cycle_id == cid]
            c_crypto = sum(t.crypto_amount for t in c_trades)
            c_fiat = sum(t.fiat_amount for t in c_trades)
            c_fees = sum(t.exchange_fee_fiat + t.bank_fee_fiat for t in c_trades)

            if cid in ask_map:
                ask_obj = ask_map[cid]
                status = "CLOSED" if ask_obj.closed_at is not None else "ACTIVE"
                c_pnl = sum(t.realized_net_pnl_fiat for t in c_trades)
                cycle_summaries.append(
                    CyclePerformanceSummary(
                        cycle_id=cid,
                        side="ASK",
                        status=status,
                        trades_in_window=len(c_trades),
                        crypto_volume=round(c_crypto, 6),
                        fiat_volume=round(c_fiat, 2),
                        fees_in_window_fiat=round(c_fees, 2),
                        realized_pnl_in_window_fiat=round(c_pnl, 2),
                    )
                )
            elif cid in bid_map:
                bid_obj = bid_map[cid]
                status = "CLOSED" if bid_obj.closed_at is not None else "ACTIVE"
                cycle_summaries.append(
                    CyclePerformanceSummary(
                        cycle_id=cid,
                        side="BID",
                        status=status,
                        trades_in_window=len(c_trades),
                        crypto_volume=round(c_crypto, 6),
                        fiat_volume=round(c_fiat, 2),
                        fees_in_window_fiat=round(c_fees, 2),
                        realized_pnl_in_window_fiat=0.0,
                    )
                )

        bank_snapshot: dict[str, float] = {}
        if self.account_rotator is not None:
            for acc_id, acc in self.account_rotator.accounts.items():
                bank_snapshot[acc_id] = round(acc.remaining_volume, 2)

        start_local = start_utc.astimezone(self.report_timezone).isoformat()
        end_local = end_utc.astimezone(self.report_timezone).isoformat()

        return PerformanceReport(
            start_iso=start_local,
            end_iso=end_local,
            timezone_label=self.report_timezone.tzname(None) or "UTC+1",
            sell_trade_count=len(sell_trades),
            buy_trade_count=len(buy_trades),
            total_crypto_sold=round(total_crypto_sold, 6),
            total_crypto_acquired=round(total_crypto_acquired, 6),
            total_fiat_inflow=round(total_fiat_inflow, 2),
            total_fiat_outflow=round(total_fiat_outflow, 2),
            weighted_avg_sell_price=round(avg_sell_price, 2),
            weighted_avg_buy_price=round(avg_buy_price, 2),
            weighted_avg_cost_basis=round(weighted_wac, 2),
            gross_sell_spread_fiat=round(gross_sell_spread, 2),
            exchange_fees_fiat=round(exchange_fees, 2),
            bank_fees_fiat=round(bank_fees, 2),
            funding_fees_fiat=round(funding_fees, 2),
            total_fees_fiat=round(total_fees, 2),
            realized_net_pnl_fiat=round(realized_net_pnl, 2),
            net_profit_per_crypto_sold=round(net_per_crypto, 2),
            return_on_deployed_capital_pct=round(roc_pct, 2),
            cycles=cycle_summaries,
            trades=filtered_trades,
            bank_headroom_snapshot=bank_snapshot,
        )
