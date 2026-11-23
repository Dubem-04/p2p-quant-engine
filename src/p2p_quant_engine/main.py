import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from p2p_quant_engine.analytics.report import ReportGenerator
from p2p_quant_engine.collector.bybit_p2p import (
    BybitCollectorConfig,
    BybitP2PCollector,
)
from p2p_quant_engine.execution.manager import ExecutionConfig, ExecutionManager
from p2p_quant_engine.portfolio.ledger import PortfolioLedger
from p2p_quant_engine.pricing.quoter import DynamicQuoter
from p2p_quant_engine.pricing.reference import ReferencePriceEstimator
from p2p_quant_engine.risk.account_rotator import AccountRotator, BankAccount
from p2p_quant_engine.risk.guardrails import RiskGuardrails

DEFAULT_ACCOUNTS_CONFIG_PATH = Path("data/bank_accounts.json")


def _load_configured_accounts(config_path: Path) -> list[BankAccount]:
    """Load registered bank account definitions from JSON if present."""
    if not config_path.exists():
        return []
    raw_list = json.loads(config_path.read_text(encoding="utf-8"))
    return [
        BankAccount(
            account_id=item["account_id"],
            bank_name=item["bank_name"],
            account_number=item["account_number"],
            daily_volume_limit=float(item["daily_volume_limit"]),
            daily_tx_limit=int(item["daily_tx_limit"]),
        )
        for item in raw_list
    ]


def _save_configured_accounts(config_path: Path, accounts: list[BankAccount]) -> None:
    """Save static bank account definitions so AccountRotator reloads them."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = [
        {
            "account_id": acc.account_id,
            "bank_name": acc.bank_name,
            "account_number": acc.account_number,
            "daily_volume_limit": acc.daily_volume_limit,
            "daily_tx_limit": acc.daily_tx_limit,
        }
        for acc in accounts
    ]
    config_path.write_text(json.dumps(serialized, indent=2), encoding="utf-8")


def _build_rotator(accounts_cfg_path: Path) -> AccountRotator:
    """Instantiate AccountRotator hydrated with saved account definitions."""
    accounts = _load_configured_accounts(accounts_cfg_path)
    state_path = accounts_cfg_path.parent / "state" / "account_state.json"
    return AccountRotator(accounts=accounts, state_file=state_path)


def cmd_status(args: argparse.Namespace) -> None:
    """Display active Ask/Bid cycles, WAC, bank limits, and kill-switch status."""
    ledger = PortfolioLedger()
    guardrails = RiskGuardrails()
    rotator = _build_rotator(Path(args.accounts_config))

    print("=" * 64)
    print("P2P QUANT ENGINE — SYSTEM STATUS")
    print("=" * 64)
    ks = guardrails.kill_switch
    ks_label = f"TRIPPED ({ks.reason})" if ks.tripped else "ARMED / HEALTHY"
    print(f"Kill Switch: {ks_label} | API Errors: {ks.consecutive_api_errors}")

    print("-" * 64)
    if ledger.active_ask_cycle:
        ask = ledger.active_ask_cycle
        print(
            f"Active Ask Cycle: {ask.cycle_id} | "
            f"Avail: {ask.available_crypto:,.2f} USDT | "
            f"Locked: {ask.locked_crypto:,.2f} USDT | "
            f"WAC: ₦{ask.avg_cost_basis_fiat:,.2f} | "
            f"Net PnL: ₦{ask.realized_net_pnl_fiat:,.2f}"
        )
    else:
        print("Active Ask Cycle: None (run 'start-ask' to fund)")

    if ledger.active_bid_cycle:
        bid = ledger.active_bid_cycle
        print(
            f"Active Bid Cycle: {bid.cycle_id} | "
            f"Avail: ₦{bid.available_fiat:,.2f} | "
            f"Locked: ₦{bid.locked_fiat:,.2f} | "
            f"Acquired: {bid.total_crypto_acquired:,.2f} USDT"
        )
    else:
        print("Active Bid Cycle: None (run 'start-bid' to fund)")

    print("-" * 64)
    if rotator.accounts:
        print("Bank Accounts:")
        for acc in rotator.accounts.values():
            print(
                f"  {acc.account_id:<14} ({acc.bank_name}) [{acc.status.value}] | "
                f"Rem Vol: ₦{acc.remaining_volume:,.2f} | "
                f"Rem Tx: {acc.remaining_tx}"
            )
    else:
        print("Bank Accounts: None registered (run 'add-bank')")
    print("=" * 64)


def cmd_start_ask(args: argparse.Namespace) -> None:
    """Start or replenish an Ask (sell crypto) inventory cycle."""
    ledger = PortfolioLedger()
    cycle = ledger.start_ask_cycle(
        crypto_amount=args.crypto,
        acquisition_price_fiat=args.cost_price,
        funding_fee_fiat=args.funding_fee,
    )
    print(
        f"Opened {cycle.cycle_id}: {cycle.available_crypto:,.2f} USDT "
        f"@ effective WAC ₦{cycle.avg_cost_basis_fiat:,.2f}"
    )


def cmd_start_bid(args: argparse.Namespace) -> None:
    """Start or replenish a Bid (buy crypto) NGN capital cycle."""
    ledger = PortfolioLedger()
    cycle = ledger.start_bid_cycle(
        fiat_amount=args.fiat,
        funding_fee_fiat=args.funding_fee,
    )
    print(
        f"Opened {cycle.cycle_id}: ₦{cycle.available_fiat:,.2f} "
        f"(funding fee: ₦{cycle.funding_fee_fiat:,.2f})"
    )


def cmd_add_bank(args: argparse.Namespace) -> None:
    """Register a Nigerian bank account for rotation and daily limit tracking."""
    cfg_path = Path(args.accounts_config)
    existing = _load_configured_accounts(cfg_path)
    filtered = [a for a in existing if a.account_id != args.account_id]
    filtered.append(
        BankAccount(
            account_id=args.account_id,
            bank_name=args.bank_name,
            account_number=args.account_number,
            daily_volume_limit=args.daily_limit,
            daily_tx_limit=args.max_tx,
        )
    )
    _save_configured_accounts(cfg_path, filtered)
    # Trigger AccountRotator save so state file reflects the new account
    _build_rotator(cfg_path)
    print(
        f"Registered bank '{args.account_id}' ({args.bank_name}) "
        f"with daily limit ₦{args.daily_limit:,.2f}."
    )


def cmd_log_trade(args: argparse.Namespace) -> None:
    """Manually record a completed BUY or SELL fill into the ledger."""
    ledger = PortfolioLedger()
    rotator = _build_rotator(Path(args.accounts_config))
    fiat_amount = round(args.crypto * args.price, 2)

    ledger.lock_order(
        order_id=args.order_id,
        side=args.side,
        crypto_amount=args.crypto,
        price_fiat=args.price,
    )
    fill = ledger.settle_order(
        order_id=args.order_id,
        exchange_fee_fiat=args.exchange_fee,
        bank_fee_fiat=args.bank_fee,
    )

    if args.bank_id and args.bank_id in rotator.accounts:
        rotator.record_transaction(args.bank_id, fiat_amount)

    print(
        f"Logged {fill.side} order '{fill.order_id}' in {fill.cycle_id}: "
        f"{fill.crypto_amount:,.2f} USDT @ ₦{fill.price_fiat:,.2f} "
        f"(Net PnL: ₦{fill.realized_net_pnl_fiat:,.2f})"
    )


def cmd_kill_switch(args: argparse.Namespace) -> None:
    """Manually trip or reset the emergency circuit breaker."""
    guardrails = RiskGuardrails()
    if args.action == "trip":
        guardrails.trip_kill_switch(args.reason or "Manual operator halt via CLI")
        print(f"KILL SWITCH TRIPPED: {guardrails.kill_switch.reason}")
    else:
        guardrails.reset_kill_switch()
        print("Kill switch reset. Engine armed.")


def cmd_run(args: argparse.Namespace) -> None:
    """Run one or continuous market-making ticks against Bybit P2P."""
    dry_run = not args.live
    payment_tuple = tuple(args.payment) if args.payment else ()

    collector = BybitP2PCollector(
        config=BybitCollectorConfig(
            payment_methods=payment_tuple,
            dry_run=dry_run,
        )
    )
    ledger = PortfolioLedger()
    guardrails = RiskGuardrails()
    rotator = _build_rotator(Path(args.accounts_config))

    manager = ExecutionManager(
        exchange=collector,
        estimator=ReferencePriceEstimator(),
        quoter=DynamicQuoter(),
        guardrails=guardrails,
        ledger=ledger,
        account_rotator=rotator,
        config=ExecutionConfig(
            ask_ad_id=args.ask_ad_id,
            bid_ad_id=args.bid_ad_id,
            poll_interval_seconds=args.interval,
        ),
    )

    mode_label = "DRY-RUN (no live ad edits)" if dry_run else "LIVE EXECUTION"
    print(f"Starting P2P Quant Engine [{mode_label}]...")

    ticks_done = 0
    try:
        while True:
            summary = manager.run_tick(now_monotonic=time.monotonic())
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            fv = (
                f"₦{summary.fair_value_fiat:,.2f}" if summary.fair_value_fiat else "N/A"
            )
            bid = (
                f"₦{summary.quoted_bid_fiat:,.2f}" if summary.quoted_bid_fiat else "OFF"
            )
            ask = (
                f"₦{summary.quoted_ask_fiat:,.2f}" if summary.quoted_ask_fiat else "OFF"
            )

            print(
                f"[{ts}] Allowed={summary.market_allowed} | FV={fv} | "
                f"Bid={bid} (active={summary.bid_ad_active}) | "
                f"Ask={ask} (active={summary.ask_ad_active}) | "
                f"Locked={len(summary.newly_locked_orders)} "
                f"Settled={len(summary.settled_orders)} "
                f"Flagged={len(summary.newly_flagged_orders)}"
            )
            for note in summary.notes:
                print(f"  [!] {note}")

            if dry_run and collector.dry_run_actions:
                for act in collector.dry_run_actions:
                    print(f"  [DRY-RUN] {act}")
                collector.dry_run_actions.clear()

            ticks_done += 1
            if args.max_ticks is not None and ticks_done >= args.max_ticks:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped cleanly by operator.")
    finally:
        collector.close()


def cmd_report(args: argparse.Namespace) -> None:
    """Generate a timeframe-filtered performance report from the ledger."""
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    ledger = PortfolioLedger()
    rotator = _build_rotator(Path(args.accounts_config))
    generator = ReportGenerator(ledger=ledger, account_rotator=rotator)
    report = generator.generate(start=start_date, end=end_date)

    print(report.render_text())
    if args.json_out:
        out_j = report.export_json(args.json_out)
        print(f"Exported JSON report -> {out_j}")
    if args.csv_out:
        out_c = report.export_csv(args.csv_out)
        print(f"Exported CSV trades  -> {out_c}")


def build_parser() -> argparse.ArgumentParser:
    """Construct the CLI argument parser and all subcommands."""
    parser = argparse.ArgumentParser(
        prog="p2p-engine",
        description="Automated USDT/NGN P2P Market-Making Engine CLI",
    )
    parser.add_argument(
        "--accounts-config",
        default=str(DEFAULT_ACCOUNTS_CONFIG_PATH),
        help="Path to bank accounts JSON definition file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="Show active cycles, WAC, and bank limits")
    p_status.set_defaults(func=cmd_status)

    # start-ask
    p_ask = sub.add_parser("start-ask", help="Fund/replenish an Ask (sell) cycle")
    p_ask.add_argument("--crypto", type=float, required=True, help="USDT funded")
    p_ask.add_argument(
        "--cost-price", type=float, required=True, help="Unit acquisition cost (NGN)"
    )
    p_ask.add_argument(
        "--funding-fee", type=float, default=0.0, help="Bulk sourcing fee (NGN)"
    )
    p_ask.set_defaults(func=cmd_start_ask)

    # start-bid
    p_bid = sub.add_parser("start-bid", help="Fund/replenish a Bid (buy) cycle")
    p_bid.add_argument("--fiat", type=float, required=True, help="NGN capital funded")
    p_bid.add_argument(
        "--funding-fee", type=float, default=0.0, help="Bulk deposit fee (NGN)"
    )
    p_bid.set_defaults(func=cmd_start_bid)

    # add-bank
    p_bank = sub.add_parser("add-bank", help="Register a Nigerian bank account")
    p_bank.add_argument("--account-id", required=True, help="Short ID (e.g. gtb_1)")
    p_bank.add_argument("--bank-name", required=True, help="Bank name (e.g. GTBank)")
    p_bank.add_argument("--account-number", required=True, help="10-digit NUBAN")
    p_bank.add_argument(
        "--daily-limit", type=float, required=True, help="Daily NGN limit"
    )
    p_bank.add_argument("--max-tx", type=int, default=20, help="Max daily transactions")
    p_bank.set_defaults(func=cmd_add_bank)

    # log-trade
    p_trade = sub.add_parser("log-trade", help="Manually log a completed P2P trade")
    p_trade.add_argument("--order-id", required=True, help="Exchange/OTC order ID")
    p_trade.add_argument(
        "--side", choices=["BUY", "SELL"], required=True, help="Trade side"
    )
    p_trade.add_argument("--crypto", type=float, required=True, help="USDT quantity")
    p_trade.add_argument("--price", type=float, required=True, help="Unit price (NGN)")
    p_trade.add_argument(
        "--exchange-fee", type=float, default=0.0, help="Exchange fee (NGN)"
    )
    p_trade.add_argument(
        "--bank-fee", type=float, default=0.0, help="Bank transfer fee (NGN)"
    )
    p_trade.add_argument(
        "--bank-id", default=None, help="Optional bank account_id to update volume"
    )
    p_trade.set_defaults(func=cmd_log_trade)

    # kill-switch
    p_ks = sub.add_parser("kill-switch", help="Trip or reset emergency circuit breaker")
    p_ks.add_argument("action", choices=["trip", "reset"], help="Action to perform")
    p_ks.add_argument("--reason", default="", help="Reason when tripping kill switch")
    p_ks.set_defaults(func=cmd_kill_switch)

    # run
    p_run = sub.add_parser("run", help="Run market-making loop against Bybit P2P")
    p_run.add_argument(
        "--live",
        action="store_true",
        help="Enable live ad price/status updates (default is dry-run)",
    )
    p_run.add_argument("--ask-ad-id", default=None, help="Bybit Sell Ad ID")
    p_run.add_argument("--bid-ad-id", default=None, help="Bybit Buy Ad ID")
    p_run.add_argument(
        "--payment", nargs="*", default=None, help="Bybit payment IDs (e.g. 14)"
    )
    p_run.add_argument(
        "--interval", type=float, default=15.0, help="Polling interval in seconds"
    )
    p_run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        help="Stop after N ticks (omit to run continuously)",
    )
    p_run.set_defaults(func=cmd_run)

    # report
    p_rep = sub.add_parser("report", help="Generate timeframe performance report")
    p_rep.add_argument("--start", required=True, help="Start date (YYYY-MM-DD, WAT)")
    p_rep.add_argument("--end", required=True, help="End date (YYYY-MM-DD, WAT)")
    p_rep.add_argument("--json-out", default=None, help="Optional JSON export path")
    p_rep.add_argument("--csv-out", default=None, help="Optional CSV export path")
    p_rep.set_defaults(func=cmd_report)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
