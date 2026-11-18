import json
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Optional

# Resolves to: p2p-quant-engine/data/state/account_state.json
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STATE_FILE = PROJECT_ROOT / "data" / "state" / "account_state.json"

VOLUME_HEADROOM = 5000.0
TX_HEADROOM = 1


class AccountStatus(Enum):
    ACTIVE = "active"
    COOLDOWN = "cooldown"
    FROZEN = "frozen"


@dataclass
class BankAccount:
    account_id: str
    bank_name: str
    account_number: str
    daily_volume_limit: float
    daily_tx_limit: int
    current_volume: float = 0.0
    current_tx_count: int = 0
    status: AccountStatus = AccountStatus.ACTIVE
    last_reset_date: date = field(default_factory=date.today)

    @property
    def remaining_volume(self) -> float:
        """Return usable volume room left today after safety headroom."""
        room = self.daily_volume_limit - (self.current_volume + VOLUME_HEADROOM)
        return max(0.0, room)

    @property
    def remaining_tx(self) -> int:
        """Return usable transaction count left today after safety headroom."""
        room = self.daily_tx_limit - (self.current_tx_count + TX_HEADROOM)
        return max(0, room)

    @property
    def utilization_ratio(self) -> float:
        """Return fraction of daily volume limit currently used (0.0 to 1.0)."""
        if self.daily_volume_limit <= 0:
            return 1.0
        return self.current_volume / self.daily_volume_limit

    def can_accept(self, amount: float) -> bool:
        """Return True if active, positive amount, and within remaining limits."""
        if amount <= 0:
            return False
        if self.status != AccountStatus.ACTIVE:
            return False
        return self.remaining_tx >= 1 and self.remaining_volume >= amount

    def record_transaction(self, amount: float) -> None:
        """Update counters and switch status to COOLDOWN if limits are hit."""
        if not self.can_accept(amount):
            raise ValueError(
                f"Account '{self.account_id}' cannot accept transaction of {amount}"
            )

        self.current_volume += amount
        self.current_tx_count += 1

        if self.remaining_tx == 0 or self.remaining_volume <= 0:
            self.status = AccountStatus.COOLDOWN

    def to_dict(self) -> dict[str, Any]:
        """Serialize dynamic state for JSON storage."""
        return {
            "account_id": self.account_id,
            "current_volume": self.current_volume,
            "current_tx_count": self.current_tx_count,
            "status": self.status.value,
            "last_reset_date": self.last_reset_date.isoformat(),
        }

    def apply_saved_state(self, data: dict[str, Any]) -> None:
        """Restore dynamic counters from saved JSON data."""
        self.current_volume = float(data.get("current_volume", 0.0))
        self.current_tx_count = int(data.get("current_tx_count", 0))
        self.status = AccountStatus(data.get("status", AccountStatus.ACTIVE.value))
        saved_date = data.get("last_reset_date")
        if saved_date:
            self.last_reset_date = date.fromisoformat(saved_date)


class AccountRotator:
    def __init__(
        self,
        accounts: list[BankAccount],
        state_file: Path | str = DEFAULT_STATE_FILE,
    ) -> None:
        self.accounts: dict[str, BankAccount] = {
            acc.account_id: acc for acc in accounts
        }
        self.state_file = Path(state_file)
        self._load_state()
        self._check_daily_resets()

    def _check_daily_resets(self) -> None:
        """Reset daily counters and COOLDOWN status when a new calendar day starts."""
        today = date.today()
        state_changed = False

        for account in self.accounts.values():
            if today > account.last_reset_date:
                account.current_volume = 0.0
                account.current_tx_count = 0
                account.last_reset_date = today
                if account.status == AccountStatus.COOLDOWN:
                    account.status = AccountStatus.ACTIVE
                state_changed = True

        if state_changed:
            self._save_state()

    def get_available_account(self, amount: float) -> Optional[BankAccount]:
        """Pick the eligible active account with the lowest daily volume utilization."""
        self._check_daily_resets()

        eligible = [acc for acc in self.accounts.values() if acc.can_accept(amount)]
        if not eligible:
            return None

        return min(eligible, key=lambda acc: acc.utilization_ratio)

    def record_transaction(self, account_id: str, amount: float) -> None:
        """Record a completed transaction on the specified account and save state."""
        self._check_daily_resets()

        if account_id not in self.accounts:
            raise KeyError(f"Unknown account_id: '{account_id}'")

        self.accounts[account_id].record_transaction(amount)
        self._save_state()

    def freeze_account(self, account_id: str) -> None:
        """Manually freeze an account so it won't be selected or auto-reset."""
        if account_id not in self.accounts:
            raise KeyError(f"Unknown account_id: '{account_id}'")

        self.accounts[account_id].status = AccountStatus.FROZEN
        self._save_state()

    def _save_state(self) -> None:
        """Write current dynamic state of all accounts to JSON."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {acc_id: acc.to_dict() for acc_id, acc in self.accounts.items()}
        with self.state_file.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def _load_state(self) -> None:
        """Load saved dynamic state from disk if the state file exists."""
        if not self.state_file.exists():
            return

        with self.state_file.open("r", encoding="utf-8") as f:
            payload = json.load(f)

        for acc_id, saved_data in payload.items():
            if acc_id in self.accounts:
                self.accounts[acc_id].apply_saved_state(saved_data)
