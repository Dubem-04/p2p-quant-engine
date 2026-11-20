from pathlib import Path

import pytest

from p2p_quant_engine.main import main
from p2p_quant_engine.portfolio.config import DEFAULT_LEDGER_STATE_PATH


def test_cli_end_to_end_cycle_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify CLI subcommands initialize cycles, log trades, and render reports."""
    monkeypatch.chdir(tmp_path)
    accounts_cfg = str(tmp_path / "data" / "bank_accounts.json")

    assert (
        main(
            [
                "--accounts-config",
                accounts_cfg,
                "add-bank",
                "--account-id",
                "gtb_1",
                "--bank-name",
                "GTBank",
                "--account-number",
                "0123456789",
                "--daily-limit",
                "5000000",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "--accounts-config",
                accounts_cfg,
                "start-ask",
                "--crypto",
                "5000",
                "--cost-price",
                "1595.0",
                "--funding-fee",
                "250.0",
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "--accounts-config",
                accounts_cfg,
                "log-trade",
                "--order-id",
                "manual_001",
                "--side",
                "SELL",
                "--crypto",
                "1000",
                "--price",
                "1608.0",
                "--bank-fee",
                "75.0",
                "--bank-id",
                "gtb_1",
            ]
        )
        == 0
    )

    assert Path(DEFAULT_LEDGER_STATE_PATH).exists()
    assert main(["--accounts-config", accounts_cfg, "status"]) == 0
    captured = capsys.readouterr().out
    assert "ASK-0001" in captured
    assert "gtb_1" in captured
