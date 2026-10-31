from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models.database import (
    Billetera,
    MovimientoFinanciero,
    SessionLocal,
    Usuario,
)


@dataclass
class WalletItem:
    wallet_id: str
    name: str
    currency: str
    created_at: Any = None


@dataclass
class WalletCreationResult:
    status: str
    message: str = ""
    wallet_id: str | None = None
    name: str | None = None
    currency: str | None = None


@dataclass
class WalletSelectionResult:
    status: str
    message: str = ""
    wallet_id: str | None = None
    wallet_name: str | None = None
    currency: str | None = None

    @property
    def name(self) -> str | None:
        return self.wallet_name


@dataclass
class WalletRenameResult:
    status: str
    message: str = ""
    wallet_id: str | None = None
    name: str | None = None
    currency: str | None = None


@dataclass
class WalletBalanceItem:
    wallet_id: str
    name: str
    currency: str
    income: Decimal
    expense: Decimal
    balance: Decimal

    @property
    def wallet_name(self) -> str:
        return self.name


class WalletService:
    SUPPORTED_CURRENCIES = {"ARS", "USD"}
    DEFAULT_NAMES = {"ARS": "Pesos", "USD": "Dólares"}

    @classmethod
    def create_wallet(
        cls,
        user_id: UUID | str,
        name: str | None = None,
        currency: str | None = None,
        session=None,
    ) -> WalletCreationResult:
        clean_currency = str(currency).strip().upper() if currency is not None else ""
        if clean_currency not in cls.SUPPORTED_CURRENCIES:
            return WalletCreationResult(status="invalid_data", message="Currency must be ARS or USD")

        clean_name = (name.strip() if isinstance(name, str) else "") or cls.DEFAULT_NAMES[clean_currency]
        if len(clean_name) > 100:
            return WalletCreationResult(status="invalid_data", message="Wallet name is too long")

        try:
            parsed_user_id = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
        except (ValueError, TypeError):
            return WalletCreationResult(status="user_not_found", message="Invalid user id")

        owns_session = session is None
        db_session = SessionLocal() if owns_session else session
        try:
            user = db_session.query(Usuario).filter(Usuario.id == parsed_user_id).first()
            if user is None:
                return WalletCreationResult(status="user_not_found", message="User not found")

            existing = (
                db_session.query(Billetera)
                .filter(
                    Billetera.usuario_id == user.id,
                    Billetera.moneda == clean_currency,
                )
                .first()
            )
            if existing is not None:
                return WalletCreationResult(
                    status="wallet_already_exists",
                    message="User already has a wallet for this currency",
                    wallet_id=str(existing.id),
                    name=existing.nombre,
                    currency=existing.moneda,
                )

            wallet = Billetera(
                usuario_id=user.id,
                nombre=clean_name,
                moneda=clean_currency,
            )
            if db_session.get_bind().dialect.name == "sqlite":
                connection = db_session.connection()
                driver_connection = connection.connection.driver_connection
                if not driver_connection.in_transaction:
                    # sqlite3's legacy mode does not begin a physical transaction for SELECT.
                    connection.exec_driver_sql("BEGIN")
            with db_session.begin_nested():
                db_session.add(wallet)
                db_session.flush()
            if owns_session:
                db_session.commit()
            return WalletCreationResult(
                status="created",
                message="Wallet created",
                wallet_id=str(wallet.id),
                name=wallet.nombre,
                currency=wallet.moneda,
            )
        except IntegrityError:
            if owns_session:
                db_session.rollback()
            duplicate = (
                db_session.query(Billetera)
                .filter(
                    Billetera.usuario_id == parsed_user_id,
                    Billetera.moneda == clean_currency,
                )
                .first()
            )
            if duplicate is not None:
                return WalletCreationResult(
                    status="wallet_already_exists",
                    message="User already has a wallet for this currency",
                    wallet_id=str(duplicate.id),
                    name=duplicate.nombre,
                    currency=duplicate.moneda,
                )
            return WalletCreationResult(status="persistence_error", message="Could not create wallet")
        except Exception:
            if owns_session:
                db_session.rollback()
            return WalletCreationResult(status="persistence_error", message="Could not create wallet")
        finally:
            if owns_session:
                db_session.close()

    @classmethod
    def rename_wallet(
        cls,
        user_id: UUID | str,
        wallet_id: UUID | str,
        name: str,
        session=None,
    ) -> WalletRenameResult:
        clean_name = name.strip() if isinstance(name, str) else ""
        if not clean_name:
            return WalletRenameResult(status="invalid_data", message="Wallet name cannot be empty")
        if len(clean_name) > 100:
            return WalletRenameResult(status="invalid_data", message="Wallet name is too long")

        try:
            parsed_user_id = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
            parsed_wallet_id = UUID(str(wallet_id)) if not isinstance(wallet_id, UUID) else wallet_id
        except (ValueError, TypeError):
            return WalletRenameResult(status="invalid_selection", message="Invalid wallet id")

        owns_session = session is None
        db_session = SessionLocal() if owns_session else session
        try:
            wallet = (
                db_session.query(Billetera)
                .filter(Billetera.id == parsed_wallet_id, Billetera.usuario_id == parsed_user_id)
                .first()
            )
            if wallet is None:
                return WalletRenameResult(
                    status="wallet_not_found",
                    message="Wallet not found or not owned by user",
                )
            wallet.nombre = clean_name
            db_session.flush()
            if owns_session:
                db_session.commit()
            return WalletRenameResult(
                status="renamed",
                message="Wallet renamed",
                wallet_id=str(wallet.id),
                name=wallet.nombre,
                currency=wallet.moneda,
            )
        except Exception:
            if owns_session:
                db_session.rollback()
            return WalletRenameResult(status="persistence_error", message="Could not rename wallet")
        finally:
            if owns_session:
                db_session.close()

    @classmethod
    def list_wallets(
        cls,
        user_id: UUID | str,
        currency: str | None = None,
        session=None,
    ) -> list[WalletItem]:
        try:
            parsed_user_id = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
        except (ValueError, TypeError):
            return []

        owns_session = session is None
        db_session = SessionLocal() if owns_session else session
        try:
            query = db_session.query(Billetera).filter(Billetera.usuario_id == parsed_user_id)
            if currency is not None and str(currency).strip():
                query = query.filter(Billetera.moneda == str(currency).strip().upper())
            wallets = query.order_by(
                Billetera.moneda.asc(),
                Billetera.creado_en.asc(),
                Billetera.id.asc(),
            ).all()
            return [
                WalletItem(
                    wallet_id=str(w.id),
                    name=w.nombre,
                    currency=w.moneda,
                    created_at=w.creado_en,
                )
                for w in wallets
            ]
        finally:
            if owns_session:
                db_session.close()

    @classmethod
    def resolve_selection(
        cls,
        session,
        user_id: UUID | str,
        currency: str | None = None,
        wallet_id: UUID | str | None = None,
    ) -> WalletSelectionResult:
        try:
            parsed_user_id = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
        except (ValueError, TypeError):
            return WalletSelectionResult(status="user_not_found", message="Invalid user id")

        user = session.query(Usuario).filter(Usuario.id == parsed_user_id).first()
        if user is None:
            return WalletSelectionResult(status="user_not_found", message="User not found")

        if currency is not None and not str(currency).strip():
            return WalletSelectionResult(status="invalid_data", message="Currency cannot be empty")
        clean_currency = str(currency).strip().upper() if currency is not None else None
        if clean_currency is not None and clean_currency not in cls.SUPPORTED_CURRENCIES:
            return WalletSelectionResult(status="invalid_data", message="Currency must be ARS or USD")

        if wallet_id is not None:
            val = str(wallet_id).strip()
            if not val:
                return WalletSelectionResult(status="invalid_selection", message="Invalid wallet id")
            try:
                parsed_wallet_id = UUID(val) if not isinstance(wallet_id, UUID) else wallet_id
            except (ValueError, TypeError):
                return WalletSelectionResult(status="invalid_selection", message="Invalid wallet id")

            wallet = (
                session.query(Billetera)
                .filter(
                    Billetera.id == parsed_wallet_id,
                    Billetera.usuario_id == user.id,
                )
                .first()
            )
            if wallet is None or wallet.moneda not in cls.SUPPORTED_CURRENCIES:
                return WalletSelectionResult(
                    status="invalid_selection",
                    message="Wallet not found or not owned by user",
                )

            if clean_currency is not None and clean_currency != wallet.moneda:
                return WalletSelectionResult(
                    status="currency_mismatch",
                    message="Currency mismatch with selected wallet",
                )

            return WalletSelectionResult(
                status="selected",
                message="Wallet selected",
                wallet_id=str(wallet.id),
                wallet_name=wallet.nombre,
                currency=wallet.moneda,
            )

        target_currency = clean_currency if clean_currency is not None else "ARS"
        wallet = (
            session.query(Billetera)
            .filter(
                Billetera.usuario_id == user.id,
                Billetera.moneda == target_currency,
            )
            .first()
        )

        if wallet is None:
            return WalletSelectionResult(
                status="needs_wallet_creation",
                message=f"No wallet found for currency {target_currency}",
                currency=target_currency,
            )
        return WalletSelectionResult(
            status="selected",
            message="Wallet selected",
            wallet_id=str(wallet.id),
            wallet_name=wallet.nombre,
            currency=wallet.moneda,
        )

    @classmethod
    def get_balances_own_user(
        cls,
        user_id: UUID | str,
        session=None,
    ) -> list[WalletBalanceItem]:
        try:
            parsed_user_id = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
        except (ValueError, TypeError):
            return []

        owns_session = session is None
        db_session = SessionLocal() if owns_session else session
        try:
            wallets = (
                db_session.query(Billetera)
                .filter(Billetera.usuario_id == parsed_user_id)
                .order_by(Billetera.nombre.asc())
                .all()
            )
            if not wallets:
                return []

            totals_query = (
                db_session.query(
                    MovimientoFinanciero.billetera_id,
                    MovimientoFinanciero.moneda,
                    MovimientoFinanciero.tipo,
                    func.sum(MovimientoFinanciero.cantidad).label("total"),
                )
                .join(
                    Billetera,
                    (MovimientoFinanciero.billetera_id == Billetera.id)
                    & (MovimientoFinanciero.usuario_id == Billetera.usuario_id)
                    & (MovimientoFinanciero.moneda == Billetera.moneda),
                )
                .filter(
                    MovimientoFinanciero.usuario_id == parsed_user_id,
                    MovimientoFinanciero.anulado_en.is_(None),
                )
                .group_by(
                    MovimientoFinanciero.billetera_id,
                    MovimientoFinanciero.moneda,
                    MovimientoFinanciero.tipo,
                )
                .all()
            )

            totals: dict[tuple[Any, str, str], Decimal] = {}
            for row in totals_query:
                b_id, mon, tipo, total = row[0], row[1], row[2], row[3]
                if total is not None:
                    totals[(b_id, str(mon), str(tipo).lower())] = Decimal(str(total))

            result = []
            for w in wallets:
                income = totals.get((w.id, w.moneda, "ingreso"), Decimal("0"))
                expense = totals.get((w.id, w.moneda, "egreso"), Decimal("0"))
                balance = cls.calculate_balance_arithmetic(income, expense)
                result.append(
                    WalletBalanceItem(
                        wallet_id=str(w.id),
                        name=w.nombre,
                        currency=w.moneda,
                        income=income,
                        expense=expense,
                        balance=balance,
                    )
                )
            return result
        finally:
            if owns_session:
                db_session.close()

    get_balances = get_balances_own_user

    @staticmethod
    def calculate_balance_arithmetic(income: Decimal | str, expense: Decimal | str) -> Decimal:
        d1 = Decimal(str(income))
        d2 = Decimal(str(expense))
        needed_prec = max(28, len(d1.as_tuple().digits) + abs(d1.as_tuple().exponent) + len(d2.as_tuple().digits) + abs(d2.as_tuple().exponent) + 10)
        with localcontext() as ctx:
            ctx.prec = needed_prec
            return d1 - d2
