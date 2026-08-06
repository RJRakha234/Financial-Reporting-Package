"""Corporate action records and their price/volume adjustment factors."""

from __future__ import annotations

import csv
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

_COMMENT_PREFIX: Final[str] = "#"


class MissingCumPriceError(ValueError):
    """Raised when a value-based action cannot be priced.

    ``DIVIDEND``, ``DEMERGER`` and ``RIGHTS`` adjustments are all defined
    relative to the last cum close. Without that close there is no correct
    factor, and defaulting to 1.0 would silently leave a real gap unadjusted —
    exactly the failure mode this module exists to prevent.
    """


class ActionType(StrEnum):
    SPLIT = "SPLIT"
    BONUS = "BONUS"
    RIGHTS = "RIGHTS"
    DIVIDEND = "DIVIDEND"
    DEMERGER = "DEMERGER"
    MERGER = "MERGER"

    @property
    def changes_share_count(self) -> bool:
        """Whether historical volume must be rescaled alongside price."""
        return self in _SHARE_COUNT_ACTIONS

    @property
    def needs_cum_price(self) -> bool:
        return self in _VALUE_BASED_ACTIONS


_SHARE_COUNT_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.SPLIT, ActionType.BONUS, ActionType.RIGHTS}
)
_VALUE_BASED_ACTIONS: Final[frozenset[ActionType]] = frozenset(
    {ActionType.DIVIDEND, ActionType.DEMERGER, ActionType.RIGHTS}
)


@dataclass(frozen=True, slots=True)
class CorporateAction:
    """One action against one symbol, effective from ``ex_date``."""

    symbol: str
    ex_date: dt.date
    action_type: ActionType
    ratio_new: float | None = None
    ratio_old: float | None = None
    value_per_share_inr: float | None = None
    rights_issue_price_inr: float | None = None
    price_factor_override: float | None = None
    note: str = ""
    source: str = "seed"

    def price_factor(self, cum_close: float | None = None) -> float:
        """Multiplier applied to every bar strictly BEFORE :attr:`ex_date`.

        A 1:2 split returns 0.5: a pre-split ₹2,200 print becomes ₹1,100, which
        is what it is worth in post-split shares.
        """
        if self.price_factor_override is not None:
            return self.price_factor_override

        if self.action_type in (ActionType.SPLIT, ActionType.BONUS):
            return self._old_over_new()

        if self.action_type is ActionType.DIVIDEND:
            price = self._require_cum(cum_close)
            dividend = self._require_value("value_per_share_inr")
            return (price - dividend) / price

        if self.action_type is ActionType.DEMERGER:
            # NSE discovers the demerged entity's value in a special pre-open and
            # strips it from the parent's close. Arithmetically a dividend.
            price = self._require_cum(cum_close)
            stripped = self._require_value("value_per_share_inr")
            return (price - stripped) / price

        if self.action_type is ActionType.RIGHTS:
            # Theoretical ex-rights price: the blended value of held plus
            # newly-subscribed shares, expressed as a fraction of the cum close.
            price = self._require_cum(cum_close)
            issue_price = self._require_value("rights_issue_price_inr")
            new, old = self._require_ratio()
            terp = (old * price + new * issue_price) / (old + new)
            return terp / price

        # MERGER: the surviving line changes identity entirely; there is no
        # meaningful continuous price series to splice without an explicit factor.
        raise ValueError(
            f"{self.action_type.value} for {self.symbol} on {self.ex_date} requires "
            "price_factor_override"
        )

    def volume_factor(self, cum_close: float | None = None) -> float:
        """Multiplier applied to pre-``ex_date`` volume.

        Only actions that change the share count rescale volume. A dividend does
        not create shares, so its volume factor is 1.0 even though its price
        factor is not.
        """
        if not self.action_type.changes_share_count:
            return 1.0
        if self.action_type is ActionType.RIGHTS:
            new, old = self._require_ratio()
            return (old + new) / old
        if self.price_factor_override is not None:
            return 1.0 / self.price_factor_override
        return 1.0 / self._old_over_new()

    def _old_over_new(self) -> float:
        new, old = self._require_ratio()
        return old / new

    def _require_ratio(self) -> tuple[float, float]:
        if self.ratio_new is None or self.ratio_old is None:
            raise ValueError(
                f"{self.action_type.value} for {self.symbol} on {self.ex_date} needs "
                "ratio_new and ratio_old"
            )
        if self.ratio_new <= 0 or self.ratio_old <= 0:
            raise ValueError(f"non-positive ratio for {self.symbol} on {self.ex_date}")
        return self.ratio_new, self.ratio_old

    def _require_value(self, field: str) -> float:
        value = getattr(self, field)
        if value is None or value <= 0:
            raise ValueError(
                f"{self.action_type.value} for {self.symbol} on {self.ex_date} needs {field}"
            )
        result: float = value
        return result

    def _require_cum(self, cum_close: float | None) -> float:
        if cum_close is None or cum_close <= 0:
            raise MissingCumPriceError(
                f"{self.action_type.value} for {self.symbol} on {self.ex_date} needs the last "
                "cum close; refusing to guess an adjustment factor"
            )
        return cum_close


class CorporateActionSet:
    """All known actions, indexed by symbol and ordered by ex-date."""

    def __init__(self, actions: list[CorporateAction]) -> None:
        by_symbol: dict[str, list[CorporateAction]] = defaultdict(list)
        for action in actions:
            by_symbol[action.symbol].append(action)
        self._by_symbol: dict[str, list[CorporateAction]] = {
            symbol: sorted(items, key=lambda a: a.ex_date) for symbol, items in by_symbol.items()
        }

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_symbol.values())

    @property
    def symbols(self) -> frozenset[str]:
        return frozenset(self._by_symbol)

    def for_symbol(self, symbol: str) -> list[CorporateAction]:
        return list(self._by_symbol.get(symbol, ()))

    def between(self, symbol: str, start: dt.date, end: dt.date) -> list[CorporateAction]:
        """Actions with ``start < ex_date <= end``."""
        return [a for a in self.for_symbol(symbol) if start < a.ex_date <= end]

    @classmethod
    def from_csv(cls, path: Path) -> CorporateActionSet:
        with path.open("r", encoding="utf-8") as handle:
            lines = [ln for ln in handle if not ln.lstrip().startswith(_COMMENT_PREFIX)]
        actions: list[CorporateAction] = []
        for row in csv.DictReader(lines):
            actions.append(
                CorporateAction(
                    symbol=row["symbol"].strip().upper(),
                    ex_date=dt.date.fromisoformat(row["ex_date"].strip()),
                    action_type=ActionType(row["action_type"].strip().upper()),
                    ratio_new=_optional_float(row.get("ratio_new")),
                    ratio_old=_optional_float(row.get("ratio_old")),
                    value_per_share_inr=_optional_float(row.get("value_per_share_inr")),
                    rights_issue_price_inr=_optional_float(row.get("rights_issue_price_inr")),
                    price_factor_override=_optional_float(row.get("price_factor_override")),
                    note=(row.get("note") or "").strip(),
                    source=(row.get("source") or "seed").strip(),
                )
            )
        return cls(actions)


def _optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    return float(stripped) if stripped else None
