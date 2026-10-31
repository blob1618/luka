"""Seed y reset de un usuario sintético para el E2E de staging.

El teléfono es solo un identificador: los webhooks se inyectan firmados y las
respuestas se capturan, así que Meta nunca lo ve y no hace falta un número real.
"""
from uuid import uuid4

from app.models import database
from app.models.database import (
    AssistantWriteReceipt,
    AvisoRecordatorio,
    Billetera,
    CandidatoGastoRecurrente,
    LimiteCategoria,
    MovimientoFinanciero,
    Recordatorio,
    Usuario,
)


def reset_test_user(phone: str, name: str = 'Staging') -> dict:
    """Crea el usuario si falta y deja su estado financiero en cero."""
    with database.SessionLocal() as session:
        user = session.query(Usuario).filter_by(whatsapp_id=phone).first()
        if user is None:
            user = Usuario(
                id=uuid4(),
                nombre=name,
                email=f'{phone}@staging.invalid',
                whatsapp_id=phone,
                auth_user_id=uuid4(),
            )
            session.add(user)
            session.flush()

        session.query(AssistantWriteReceipt).filter_by(owner_id=user.id).delete()
        session.query(MovimientoFinanciero).filter_by(usuario_id=user.id).delete()
        session.query(AvisoRecordatorio).filter_by(usuario_id=user.id).delete()
        session.query(Recordatorio).filter_by(usuario_id=user.id).delete()
        session.query(LimiteCategoria).filter_by(usuario_id=user.id).delete()
        session.query(CandidatoGastoRecurrente).filter_by(usuario_id=user.id).delete()

        wallet = (
            session.query(Billetera)
            .filter_by(usuario_id=user.id, moneda='ARS')
            .first()
        )
        if wallet is None:
            wallet = Billetera(usuario_id=user.id, nombre='Pesos', moneda='ARS')
            session.add(wallet)
            session.flush()

        session.commit()
        return {
            'user_id': str(user.id),
            'phone': phone,
            'wallet_id': str(wallet.id),
            'wallet_name': wallet.nombre,
            'reset': True,
        }
