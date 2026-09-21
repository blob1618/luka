"""Budget compensation proposals derived from category limits and movements."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from app.models.database import LimiteCategoria, SessionLocal, Usuario
from app.services.budget import ARGENTINA_TZ, BudgetService, BudgetStatus


COMPENSATION_TTL_MINUTES = 30
_MONEY = Decimal("0.01")


@dataclass(frozen=True)
class CompensationAllocation:
    limit_id: str
    category_id: str
    category_name: str
    before_limit: Decimal
    after_limit: Decimal
    spent_amount: Decimal
    available_before: Decimal

    def to_dict(self) -> dict:
        return {
            "limit_id": self.limit_id,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "before_limit": str(self.before_limit),
            "after_limit": str(self.after_limit),
            "spent_amount": str(self.spent_amount),
            "available_before": str(self.available_before),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompensationAllocation":
        raw = dict(d)
        for key in (
            "before_limit",
            "after_limit",
            "spent_amount",
            "available_before",
        ):
            raw[key] = Decimal(str(raw[key]))
        return cls(**raw)


@dataclass(frozen=True)
class CompensationProposal:
    proposal_id: str
    user_id: str
    currency: str
    period_start: date
    period_end: date
    amount: Decimal
    target: CompensationAllocation
    donors: list[CompensationAllocation]
    created_at: str
    expires_at: str
    snapshot: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id,
            "user_id": self.user_id,
            "currency": self.currency,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "amount": str(self.amount),
            "target": self.target.to_dict(),
            "donors": [donor.to_dict() for donor in self.donors],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "snapshot": dict(self.snapshot),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompensationProposal":
        raw = dict(d)
        raw["amount"] = Decimal(str(raw["amount"]))
        raw["period_start"] = date.fromisoformat(raw["period_start"])
        raw["period_end"] = date.fromisoformat(raw["period_end"])
        raw["target"] = CompensationAllocation.from_dict(raw["target"])
        raw["donors"] = [
            CompensationAllocation.from_dict(donor) for donor in raw["donors"]
        ]
        raw["snapshot"] = {
            str(key): str(value)
            for key, value in dict(raw.get("snapshot") or {}).items()
        }
        return cls(**raw)


@dataclass
class CompensationProposalResult:
    status: str
    message: str
    proposal: CompensationProposal | None = None


@dataclass
class CompensationApplyResult:
    status: str
    message: str
    proposal: CompensationProposal | None = None


class BudgetCompensationService:
    """Builds compensation proposals and applies them atomically."""

    @staticmethod
    def _matches(category_name: str, name: str | None) -> bool:
        return (
            name is not None
            and category_name.strip().casefold() == name.strip().casefold()
        )

    @staticmethod
    def _normalize_requested(value: Any) -> Decimal | None:
        try:
            amount = Decimal(str(value))
            if not amount.is_finite():
                return None
            return amount.quantize(_MONEY)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _allocation(status: BudgetStatus, delta: Decimal) -> CompensationAllocation:
        before = status.limit_amount.quantize(_MONEY)
        return CompensationAllocation(
            limit_id=status.limit_id,
            category_id=status.category_id,
            category_name=status.category_name,
            before_limit=before,
            after_limit=before + delta,
            spent_amount=status.spent_amount.quantize(_MONEY),
            available_before=status.remaining_amount.quantize(_MONEY),
        )

    @classmethod
    def build_proposal(
        cls,
        user_id: Any,
        *,
        target_category: str | None = None,
        source_category: str | None = None,
        requested_amount: Decimal | None = None,
        reference_date: date | None = None,
        currency: str | None = None,
    ) -> CompensationProposalResult:
        parsed_user = BudgetService._uuid(user_id)
        if parsed_user is None:
            return CompensationProposalResult("invalid_data", "invalid user id")

        reference_date = reference_date or BudgetService._today()
        normalized_currency = None
        if currency is not None:
            normalized_currency = BudgetService._currency(currency)
            if normalized_currency is None:
                return CompensationProposalResult("invalid_data", "invalid currency")

        requested = None
        if requested_amount is not None:
            requested = cls._normalize_requested(requested_amount)
            if requested is None or requested <= 0:
                return CompensationProposalResult(
                    "invalid_data", "invalid requested amount"
                )

        session = SessionLocal()
        try:
            statuses = BudgetService.query_statuses(
                session,
                parsed_user,
                reference_date,
                currency=normalized_currency,
            )
            candidates = [
                status for status in statuses if status.state == "exceeded"
            ]
            if target_category is not None:
                candidates = [
                    status
                    for status in candidates
                    if cls._matches(status.category_name, target_category)
                ]
            if not candidates:
                return CompensationProposalResult(
                    "no_excess", "no exceeded budget found"
                )
            target = min(
                candidates,
                key=lambda status: (-status.exceeded_amount, status.category_name),
            )

            donors = [
                status
                for status in statuses
                if status.category_id != target.category_id
                and status.currency == target.currency
                and status.period_start == target.period_start
                and status.period_end == target.period_end
                and status.remaining_amount > 0
            ]
            donor_total = sum(
                (donor.remaining_amount for donor in donors), Decimal("0")
            )
            if donor_total <= 0:
                return CompensationProposalResult(
                    "no_funds", "no category has available budget"
                )

            desired = (
                requested if requested is not None else target.exceeded_amount
            ).quantize(_MONEY)
            if desired <= 0:
                return CompensationProposalResult(
                    "invalid_data", "invalid requested amount"
                )
            amount = min(desired, donor_total).quantize(_MONEY)

            ordered = sorted(
                donors,
                key=lambda status: (
                    0 if cls._matches(status.category_name, source_category) else 1,
                    -status.remaining_amount,
                    status.category_name,
                ),
            )
            allocations = []
            pending = amount
            for donor in ordered:
                if pending <= 0:
                    break
                taken = min(donor.remaining_amount.quantize(_MONEY), pending)
                if taken <= 0:
                    continue
                allocations.append(cls._allocation(donor, -taken))
                pending -= taken
            amount -= pending
            if amount <= 0:
                return CompensationProposalResult(
                    "no_funds", "no category has available budget"
                )

            now = datetime.now(ARGENTINA_TZ)
            target_allocation = cls._allocation(target, amount)
            proposal = CompensationProposal(
                proposal_id=uuid4().hex,
                user_id=str(parsed_user),
                currency=target.currency,
                period_start=target.period_start,
                period_end=target.period_end,
                amount=amount,
                target=target_allocation,
                donors=allocations,
                created_at=now.isoformat(),
                expires_at=(
                    now + timedelta(minutes=COMPENSATION_TTL_MINUTES)
                ).isoformat(),
                snapshot={
                    allocation.limit_id: str(allocation.before_limit)
                    for allocation in [target_allocation, *allocations]
                },
            )
            message = (
                "partial compensation proposal created"
                if amount < desired
                else "compensation proposal created"
            )
            return CompensationProposalResult("ok", message, proposal)
        except Exception as exc:
            print(f"[COMPENSATION_PROPOSAL] Error: {type(exc).__name__}: {exc}")
            return CompensationProposalResult(
                "error", "could not build compensation proposal"
            )
        finally:
            session.close()

    @staticmethod
    def _is_consistent(proposal: CompensationProposal, ids: list[UUID]) -> bool:
        if proposal.amount <= 0 or not proposal.donors:
            return False
        if len(ids) != len(set(ids)):
            return False
        for allocation in [proposal.target, *proposal.donors]:
            expected = proposal.snapshot.get(allocation.limit_id)
            if expected is None:
                return False
            try:
                if Decimal(str(expected)) != Decimal(str(allocation.before_limit)):
                    return False
            except (InvalidOperation, ValueError):
                return False
        if proposal.target.after_limit != (
            proposal.target.before_limit + proposal.amount
        ):
            return False
        if proposal.target.after_limit < 0:
            return False
        planned = Decimal("0")
        for donor in proposal.donors:
            delta = donor.before_limit - donor.after_limit
            if delta <= 0 or donor.after_limit < 0:
                return False
            planned += delta
        return planned == proposal.amount

    @staticmethod
    def _snapshot_matches(row: LimiteCategoria, expected: str | None) -> bool:
        if expected is None:
            return False
        try:
            return Decimal(str(row.cantidad_max)) == Decimal(str(expected))
        except (InvalidOperation, ValueError):
            return False

    @classmethod
    def apply(
        cls,
        proposal: CompensationProposal | dict,
        *,
        now: datetime | None = None,
    ) -> CompensationApplyResult:
        if isinstance(proposal, dict):
            try:
                proposal = CompensationProposal.from_dict(proposal)
            except Exception:
                return CompensationApplyResult("invalid_data", "invalid proposal")
        elif not isinstance(proposal, CompensationProposal):
            return CompensationApplyResult("invalid_data", "invalid proposal")

        current = now or datetime.now(ARGENTINA_TZ)
        try:
            if datetime.fromisoformat(proposal.expires_at) < current:
                return CompensationApplyResult("expired", "proposal expired")
        except (TypeError, ValueError):
            return CompensationApplyResult("invalid_data", "invalid expiration")

        try:
            parsed_user = UUID(proposal.user_id)
            ids = [
                UUID(proposal.target.limit_id),
                *(UUID(donor.limit_id) for donor in proposal.donors),
            ]
        except (TypeError, ValueError, AttributeError):
            return CompensationApplyResult("invalid_data", "invalid limit reference")

        if not cls._is_consistent(proposal, ids):
            return CompensationApplyResult("invalid_data", "inconsistent proposal")

        session = SessionLocal()
        try:
            user_row = (
                session.query(Usuario)
                .filter(Usuario.id == parsed_user)
                .with_for_update()
                .first()
            )
            if user_row is None:
                return CompensationApplyResult("stale", "limits changed since proposal")

            rows = (
                session.query(LimiteCategoria)
                .filter(
                    LimiteCategoria.id.in_(ids),
                    LimiteCategoria.usuario_id == parsed_user,
                )
                .with_for_update()
                .all()
            )
            by_id = {str(row.id): row for row in rows}
            if len(by_id) != len(set(ids)):
                return CompensationApplyResult("stale", "limits changed since proposal")

            allocations = [proposal.target, *proposal.donors]
            for allocation in allocations:
                if not cls._snapshot_matches(
                    by_id[allocation.limit_id],
                    proposal.snapshot.get(allocation.limit_id),
                ):
                    return CompensationApplyResult(
                        "stale", "limits changed since proposal"
                    )

            statuses = BudgetService.query_statuses(
                session,
                parsed_user,
                proposal.period_start,
                currency=proposal.currency,
            )
            status_by_limit = {status.limit_id: status for status in statuses}

            target_status = status_by_limit.get(proposal.target.limit_id)
            if (
                target_status is None
                or target_status.exceeded_amount < proposal.amount
            ):
                return CompensationApplyResult("stale", "budget excess changed")

            for donor in proposal.donors:
                donor_status = status_by_limit.get(donor.limit_id)
                if donor_status is None or donor_status.remaining_amount < (
                    donor.before_limit - donor.after_limit
                ):
                    return CompensationApplyResult("stale", "donor funds changed")

            target_row = by_id[proposal.target.limit_id]
            target_row.cantidad_max = target_row.cantidad_max + proposal.amount
            for donor in proposal.donors:
                donor_row = by_id[donor.limit_id]
                donor_row.cantidad_max = donor_row.cantidad_max - (
                    donor.before_limit - donor.after_limit
                )
            session.commit()
            return CompensationApplyResult(
                "applied", "compensation applied", proposal
            )
        except IntegrityError as exc:
            session.rollback()
            print(f"[COMPENSATION_APPLY] Integrity error: {type(exc).__name__}: {exc}")
            return CompensationApplyResult(
                "persistence_error", "could not apply compensation"
            )
        except Exception as exc:
            session.rollback()
            print(f"[COMPENSATION_APPLY] Error: {type(exc).__name__}: {exc}")
            return CompensationApplyResult(
                "persistence_error", "could not apply compensation"
            )
        finally:
            session.close()
