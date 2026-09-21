"""Test user management for the Streamlit testing environment."""

from app.models.database import (
    Categoria,
    MovimientoFinanciero,
    Recordatorio,
    Usuario,
)


def _test_email(phone: str) -> str:
    return f"test-{phone}@luka.test"


def sync_test_user(session_factory, *, phone: str, name: str, registered: bool) -> None:
    """Vincula o desvincula el usuario de test para ese teléfono."""
    simulator = UserSimulator(session_factory)
    if registered:
        simulator.create_test_user(phone, name)
    else:
        simulator.delete_test_user(phone)


class UserSimulator:
    """CRUD operations for test users in the database."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    def create_test_user(self, phone: str, name: str) -> Usuario:
        """Create a test user. Returns existing user if phone already exists."""
        session = self._session_factory()
        try:
            existing = session.query(Usuario).filter(
                Usuario.whatsapp_id == phone
            ).first()
            if existing:
                if existing.nombre != name:
                    existing.nombre = name
                    session.commit()
                    session.refresh(existing)
                return existing

            user = Usuario(
                nombre=name,
                email=_test_email(phone),
                whatsapp_id=phone,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user
        finally:
            session.close()

    def get_user(self, phone: str) -> Usuario | None:
        """Look up a user by phone number."""
        session = self._session_factory()
        try:
            return session.query(Usuario).filter(
                Usuario.whatsapp_id == phone
            ).first()
        finally:
            session.close()

    def delete_test_user(self, phone: str) -> None:
        """Delete a test user and all associated data."""
        session = self._session_factory()
        try:
            user = session.query(Usuario).filter(
                Usuario.whatsapp_id == phone
            ).first()
            if user is None:
                return

            session.query(MovimientoFinanciero).filter(
                MovimientoFinanciero.usuario_id == user.id
            ).delete()
            session.query(Recordatorio).filter(
                Recordatorio.usuario_id == user.id
            ).delete()
            session.query(Categoria).filter(
                Categoria.usuario_id == user.id
            ).delete()
            session.delete(user)
            session.commit()
        finally:
            session.close()

    def reset_user_data(self, phone: str) -> None:
        """Delete all movements, categories, and reminders for a user. User stays."""
        session = self._session_factory()
        try:
            user = session.query(Usuario).filter(
                Usuario.whatsapp_id == phone
            ).first()
            if user is None:
                return

            session.query(MovimientoFinanciero).filter(
                MovimientoFinanciero.usuario_id == user.id
            ).delete()
            session.query(Recordatorio).filter(
                Recordatorio.usuario_id == user.id
            ).delete()
            session.query(Categoria).filter(
                Categoria.usuario_id == user.id
            ).delete()
            session.commit()
        finally:
            session.close()

    def seed_categories(self, phone: str, categories: list[str]) -> list[Categoria]:
        """Create categories for a user. Skips duplicates."""
        session = self._session_factory()
        try:
            user = session.query(Usuario).filter(
                Usuario.whatsapp_id == phone
            ).first()
            if user is None:
                raise ValueError(f"No se encontró el usuario con teléfono {phone}")

            created = []
            for cat_name in categories:
                existing = session.query(Categoria).filter(
                    Categoria.usuario_id == user.id,
                    Categoria.nombre == cat_name,
                ).first()
                if existing:
                    continue

                cat = Categoria(
                    nombre=cat_name,
                    usuario_id=user.id,
                )
                session.add(cat)
                session.flush()
                created.append(cat)

            session.commit()
            for c in created:
                session.refresh(c)
            return created
        finally:
            session.close()
