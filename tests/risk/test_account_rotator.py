from datetime import date, timedelta
from pathlib import Path

import pytest

from p2p_quant_engine.risk.account_rotator import (
    AccountRotator,
    AccountStatus,
    BankAccount,
)


@pytest.fixture
def sample_accounts() -> list[BankAccount]:
    """Provide a fresh set of test bank accounts for each test."""
    return [
        BankAccount(
            account_id="acc_1",
            bank_name="Access Bank",
            account_number="0123456789",
            daily_volume_limit=100_000.0,  # Usable after 5k headroom: 95,000
            daily_tx_limit=5,  # Usable after 1 headroom: 4
        ),
        BankAccount(
            account_id="acc_2",
            bank_name="Zenith Bank",
            account_number="9876543210",
            daily_volume_limit=200_000.0,  # Usable after 5k headroom: 195,000
            daily_tx_limit=10,  # Usable after 1 headroom: 9
        ),
    ]


@pytest.fixture
def rotator(sample_accounts: list[BankAccount], tmp_path: Path) -> AccountRotator:
    """Create an AccountRotator backed by an isolated temporary JSON file."""
    state_file = tmp_path / "test_account_state.json"
    return AccountRotator(accounts=sample_accounts, state_file=state_file)


def test_load_balancing_picks_lowest_utilization(rotator: AccountRotator) -> None:
    # Both start at 0% utilization; acc_1 gets picked first
    first = rotator.get_available_account(20_000.0)
    assert first is not None
    assert first.account_id == "acc_1"

    # Record 20k on acc_1 (20% used). acc_2 is still at 0% used.
    rotator.record_transaction("acc_1", 20_000.0)

    # Next trade should route to acc_2 because 0% < 20%
    second = rotator.get_available_account(20_000.0)
    assert second is not None
    assert second.account_id == "acc_2"


def test_cooldown_triggered_when_volume_limit_reached(rotator: AccountRotator) -> None:
    # acc_1 has 95,000 usable volume after 5,000 headroom
    rotator.record_transaction("acc_1", 95_000.0)

    acc_1 = rotator.accounts["acc_1"]
    assert acc_1.remaining_volume == 0.0
    assert acc_1.status == AccountStatus.COOLDOWN
    assert not acc_1.can_accept(1_000.0)


def test_cooldown_triggered_when_tx_limit_reached(rotator: AccountRotator) -> None:
    # acc_1 has 4 usable transactions after 1 tx headroom
    for _ in range(4):
        rotator.record_transaction("acc_1", 1_000.0)

    acc_1 = rotator.accounts["acc_1"]
    assert acc_1.remaining_tx == 0
    assert acc_1.status == AccountStatus.COOLDOWN
    assert not acc_1.can_accept(1_000.0)


def test_returns_none_when_all_accounts_exhausted(rotator: AccountRotator) -> None:
    # Request an amount larger than every account's remaining volume
    result = rotator.get_available_account(500_000.0)
    assert result is None


def test_invalid_transactions_raise_errors(rotator: AccountRotator) -> None:
    with pytest.raises(ValueError):
        rotator.record_transaction("acc_1", -500.0)

    with pytest.raises(ValueError):
        rotator.record_transaction("acc_1", 500_000.0)

    with pytest.raises(KeyError):
        rotator.record_transaction("non_existent_id", 1_000.0)


def test_state_persists_across_restarts(
    sample_accounts: list[BankAccount], tmp_path: Path
) -> None:
    state_file = tmp_path / "persist_state.json"

    # First session: record a transaction
    rotator_session_1 = AccountRotator(accounts=sample_accounts, state_file=state_file)
    rotator_session_1.record_transaction("acc_1", 50_000.0)

    # Simulate app restart with fresh BankAccount objects pointing to the same file
    fresh_accounts = [
        BankAccount("acc_1", "Access Bank", "0123456789", 100_000.0, 5),
        BankAccount("acc_2", "Zenith Bank", "9876543210", 200_000.0, 10),
    ]
    rotator_session_2 = AccountRotator(accounts=fresh_accounts, state_file=state_file)

    assert rotator_session_2.accounts["acc_1"].current_volume == 50_000.0
    assert rotator_session_2.accounts["acc_1"].current_tx_count == 1


def test_daily_reset_restores_cooldown_but_keeps_frozen(tmp_path: Path) -> None:
    yesterday = date.today() - timedelta(days=1)
    accounts = [
        BankAccount(
            account_id="acc_cooldown",
            bank_name="Access Bank",
            account_number="1111111111",
            daily_volume_limit=100_000.0,
            daily_tx_limit=5,
            current_volume=95_000.0,
            current_tx_count=4,
            status=AccountStatus.COOLDOWN,
            last_reset_date=yesterday,
        ),
        BankAccount(
            account_id="acc_frozen",
            bank_name="OPay",
            account_number="2222222222",
            daily_volume_limit=100_000.0,
            daily_tx_limit=5,
            current_volume=50_000.0,
            current_tx_count=2,
            status=AccountStatus.FROZEN,
            last_reset_date=yesterday,
        ),
    ]

    rotator = AccountRotator(
        accounts=accounts, state_file=tmp_path / "reset_state.json"
    )

    # Cooldown account should reset to ACTIVE with 0 usage
    assert rotator.accounts["acc_cooldown"].status == AccountStatus.ACTIVE
    assert rotator.accounts["acc_cooldown"].current_volume == 0.0
    assert rotator.accounts["acc_cooldown"].current_tx_count == 0

    # Frozen account should clear counters, but STAY FROZEN
    assert rotator.accounts["acc_frozen"].status == AccountStatus.FROZEN
    assert rotator.accounts["acc_frozen"].current_volume == 0.0
