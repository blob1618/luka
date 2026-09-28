"""Reusable category creation subflow, independent of its caller's operation."""

from sqlalchemy.exc import SQLAlchemyError

from app.models.database import SessionLocal, Usuario
from app.services.finance import CategoryResult, FinanceService

CATEGORY_CREATION_EVENT = "category.confirmation_required"


def category_creation_definition():
    return {
        "start_node": "confirmar-categoria",
        "nodes": [{
            "id": "confirmar-categoria", "type": "reply_button",
            "body": "📁 No tenés la categoría *{category}*. ¿Querés crearla?",
            "options": [
                {"id": "crear", "title": "Crear categoría", "action": "confirm_category"},
                {"id": "cancelar", "title": "Cancelar", "action": "reject_category"},
            ],
        }],
    }


class CategoryCreationService:
    @staticmethod
    def confirm(sender_phone: str, category_name: str) -> CategoryResult:
        """Create/reactivate once for this user, then return to the caller.

        An existing category is a successful result, which makes retries safe
        when creation succeeded but the caller's later persistence failed.
        """
        try:
            with SessionLocal() as session:
                user = session.query(Usuario).filter(Usuario.whatsapp_id == sender_phone).first()
                if user is None:
                    return CategoryResult(status="error", message="user not found")
                user_id = user.id
        except SQLAlchemyError:
            return CategoryResult(status="error", message="could not load user")
        return FinanceService.create_category(user_id, category_name)
