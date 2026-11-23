import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from p2p_quant_engine.analytics.report import ReportGenerator
from p2p_quant_engine.portfolio.ledger import LedgerConfig, PortfolioLedger
from p2p_quant_engine.risk.account_rotator import AccountRotator, BankAccount


@pytest.fixture
def populated_ledger(tmp_path: Path) -> PortfolioLedger:
    """Build a ledger containing both in-window and out-of-window trades."""
    ledger = PortfolioLedger(config=LedgerConfig(state_path=tmp_path / "ledger.json"))
    ledger.start_ask_cycle(
        crypto_amount=10_000.0,
        acquisition_price_fiat=1595.00,
        funding_fee_fiat=500.0,
    )
    ledger.start_bid_cycle(fiat_amount=10_000_000.0, funding_fee_fiat=50.0)

    # Trade 1: SELL 1,000 USDT @ 1608.00 (settled Nov 20, 2026 10:00 UTC)
    ledger.lock_order("ord_1", side="SELL", crypto_amount=1_000.0, price_fiat=1608.00)
    f1 = ledger.settle_order("ord_1", exchange_fee_fiat=0.0, bank_fee_fiat=75.0)
    f1.settled_at = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc).isoformat()

    # Trade 2: BUY 500 USDT @ 1592.00 (settled Nov 20, 2026 12:00 UTC)
    ledger.lock_order("ord_2", side="BUY", crypto_amount=500.0, price_fiat=1592.00)
    f2 = ledger.settle_order("ord_2", exchange_fee_fiat=100.0, bank_fee_fiat=100.0)
    f2.settled_at = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc).isoformat()

    # Trade 3: Older SELL outside report date (settled Nov 10, 2026)
    ledger.lock_order("ord_old", side="SELL", crypto_amount=200.0, price_fiat=1605.00)
    f3 = ledger.settle_order("ord_old", exchange_fee_fiat=0.0, bank_fee_fiat=75.0)
    f3.settled_at = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc).isoformat()

    # Align cycle start dates to Nov 20, 2026 WAT
    wat = timezone(timedelta(hours=1))
    assert ledger.active_ask_cycle is not None
    assert ledger.active_bid_cycle is not None
    ledger.active_ask_cycle.started_at = datetime(
        2026, 11, 20, 8, 0, tzinfo=wat
    ).isoformat()
    ledger.active_bid_cycle.started_at = datetime(
        2026, 11, 20, 8, 0, tzinfo=wat
    ).isoformat()

    return ledger


def test_timeframe_filtering_and_net_pnl_metrics(
    populated_ledger: PortfolioLedger, tmp_path: Path
) -> None:
    """Verify report filters to target WAT day and computes exact fees & net PnL."""
    rotator = AccountRotator(
        accounts=[
            BankAccount(
                account_id="gtb_1",
                bank_name="GTBank",
                account_number="0123456789",
                daily_volume_limit=5_000_000.0,
                daily_tx_limit=20,
            )
        ],
        state_file=tmp_path / "accounts.json",
    )

    generator = ReportGenerator(ledger=populated_ledger, account_rotator=rotator)
    report = generator.generate(start=date(2026, 11, 20), end=date(2026, 11, 20))

    assert report.sell_trade_count == 1
    assert report.buy_trade_count == 1
    assert report.total_crypto_sold == 1_000.0
    assert report.total_crypto_acquired == 500.0
    assert report.total_fiat_inflow == 1_608_000.0
    assert report.total_fiat_outflow == 796_000.0

    # Sell spread = (1608.00 - 1595.05) * 1000 = 12,950.00
    # Sell net pnl = 12,950.00 - 75.00 = 12,875.00
    # Minus window funding fees (500.00 + 50.00) = 12,325.00
    assert report.gross_sell_spread_fiat == 12_950.0
    assert report.funding_fees_fiat == 550.0
    assert report.realized_net_pnl_fiat == 12_325.0
    assert len(report.cycles) == 2
    assert "gtb_1" in report.bank_headroom_snapshot


def test_cli_text_render_and_json_csv_exports(
    populated_ledger: PortfolioLedger, tmp_path: Path
) -> None:
    """Verify CLI table rendering and JSON/CSV file exports."""
    generator = ReportGenerator(ledger=populated_ledger)
    report = generator.generate(start=date(2026, 11, 20), end=date(2026, 11, 20))

    rendered = report.render_text()
    assert "P2P QUANT ENGINE — PERFORMANCE REPORT (WAT)" in rendered
    assert "REALIZED NET PNL:" in rendered

    json_file = report.export_json(tmp_path / "exports" / "report.json")
    csv_file = report.export_csv(tmp_path / "exports" / "trades.csv")

    parsed_json = json.loads(json_file.read_text(encoding="utf-8"))
    assert parsed_json["sell_trade_count"] == 1

    with csv_file.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert {r["order_id"] for r in rows} == {"ord_1", "ord_2"}


def test_invalid_date_range_raises_value_error(
    populated_ledger: PortfolioLedger,
) -> None:
    """Verify end date earlier than start date raises ValueError."""
    generator = ReportGenerator(ledger=populated_ledger)
    with pytest.raises(ValueError, match="cannot be earlier than start"):
        generator.generate(start=date(2026, 11, 21), end=date(2026, 11, 20))


def _sample_report(**overrides):  # type: ignore[no-untyped-def]
    from p2p_quant_engine.analytics.report import (
        CyclePerformanceSummary,
        PerformanceReport,
    )

    base = dict(
        start_iso="2026-11-23T00:00:00+01:00",
        end_iso="2026-11-23T23:59:59.999999+01:00",
        timezone_label="WAT",
        sell_trade_count=1,
        buy_trade_count=0,
        total_crypto_sold=10.0,
        total_crypto_acquired=0.0,
        total_fiat_inflow=14_560.0,
        total_fiat_outflow=0.0,
        weighted_avg_sell_price=1_456.0,
        weighted_avg_buy_price=0.0,
        weighted_avg_cost_basis=1_425.04,
        gross_sell_spread_fiat=309.6,
        exchange_fees_fiat=0.0,
        bank_fees_fiat=0.0,
        funding_fees_fiat=0.0,
        total_fees_fiat=0.0,
        realized_net_pnl_fiat=309.6,
        net_profit_per_crypto_sold=30.96,
        return_on_deployed_capital_pct=0.0,
        cycles=[
            CyclePerformanceSummary(
                "ASK-0003", "ACTIVE", "ACTIVE", 1, 10.0, 14_560.0, 0.0, 309.6
            )
        ],
        bank_headroom_snapshot={"gtb_dry": 4_965_880.0, "gtb_long_name": 12.5},
    )
    base.update(overrides)
    return PerformanceReport(**base)


def test_rendered_report_columns_are_aligned() -> None:
    """Money/percent rows share one right edge and nothing overflows the box."""
    rendered = _sample_report().render_text().splitlines()

    money_labels = (
        "Fiat Inflow",
        "Fiat Outflow",
        "Avg Sell",
        "Avg Buy",
        "Avg Cost",
        "Gross Sell",
        "Exchange Fees",
        "Bank Transfer",
        "Cycle Funding",
        "Total Fees",
        "REALIZED",
        "Net Profit",
        "Return on Capital",
    )
    widths = {len(line) for line in rendered if line.lstrip().startswith(money_labels)}
    assert len(widths) == 1, f"misaligned money/percent rows: {sorted(widths)}"
    assert max(len(line) for line in rendered) <= 68  # fits the separator box

    # Bank headroom amounts line up across accounts of different name length.
    bank = [line for line in rendered if "Remaining Daily Limit" in line]
    assert len({len(line) for line in bank}) == 1


def test_large_and_negative_values_keep_alignment() -> None:
    """Realistic worst cases (millions, losses) must not break the right edge."""
    rendered = (
        _sample_report(
            total_fiat_inflow=123_456_789.12,
            realized_net_pnl_fiat=-1_234_567.89,
            return_on_deployed_capital_pct=-12.34,
        )
        .render_text()
        .splitlines()
    )
    widths = {
        len(line)
        for line in rendered
        if line.lstrip().startswith(("Fiat Inflow", "REALIZED", "Return on Capital"))
    }
    assert len(widths) == 1
