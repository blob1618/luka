"""The limit journey's domain routes, shared by validation and the admin map.

Examples describe accepted input; routing always belongs to the dispatcher and
LimitService, never to an administrator-supplied expression or success message.
"""

LIMIT_JOURNEY = "limit.creation"
LIMIT_START_MESSAGE = (
    "🎯 Indicá categoría, monto y mes para crear tu límite.\n\n"
    "Por ejemplo: Comida, 80.000 pesos, octubre."
)
LIMIT_STAGES = {
    "limit.listed": "Tus límites",
    "limit.started": "Pedir categoría, monto y mes",
    "limit.missing_data": "Completar datos faltantes",
    "limit.year_confirmation": "Confirmar el año",
    "limit.created": "Límite creado",
    "limit.updated": "Límite actualizado",
    "limit.cancelled": "Operación cancelada",
}

DATA_OUTCOMES = [
    {"event": "limit.missing_data", "label": "Faltan datos"},
    {"event": "limit.year_confirmation", "label": "El mes ya pasó este año"},
    {"event": "category.confirmation_required", "label": "La categoría no existe"},
    {"event": "limit.created", "label": "Datos válidos · guardado exitoso"},
    {"event": "limit.updated", "label": "Edición válida · guardado exitoso"},
]
LIMIT_TEXT_RESPONSES = {
    "limit.started": [{"id": "datos", "label": "Indica categoría, monto y mes", "example": "Comida, 80.000 pesos, octubre", "outcomes": DATA_OUTCOMES}],
    "limit.missing_data": [{"id": "completar", "label": "Completa o corrige los datos", "example": "El mes es octubre", "outcomes": DATA_OUTCOMES}],
    "limit.year_confirmation": [{"id": "confirmar-ano", "label": "Confirma el año por texto", "example": "Sí", "outcomes": DATA_OUTCOMES[2:]}],

}
for event in list(LIMIT_TEXT_RESPONSES):
    LIMIT_TEXT_RESPONSES[event].append({
        "id": "cancelar-" + event.split(".")[1], "label": "Cancela por texto",
        "example": "Cancelar", "outcomes": [{"event": "limit.cancelled", "label": "Sin guardar cambios"}],
    })
LIMIT_ACTION_OUTCOMES = {
    "start_limit": [{"event": "limit.started", "label": "Iniciar creación"}],
    "cancel_pending_operation": [{"event": "limit.cancelled", "label": "Sin guardar cambios"}],
    "reject_limit": [{"event": "limit.cancelled", "label": "Sin guardar cambios"}],
    "confirm_limit_year": DATA_OUTCOMES[2:],
}


LIMIT_SUBFLOWS = {
    "category.confirmation_required": {
        "label": "Crear categoría",
        "description": "Subflujo compartido con el registro de movimientos. Abrir para editar ↗",
        "outcomes": DATA_OUTCOMES[3:],
    },
}


def limit_journey_definition():
    """A single publishable resource with all domain entry points."""
    bodies = {
        "limit.listed": "{summary}\n\n🎯 ¿Querés crear un límite?",
        "limit.started": LIMIT_START_MESSAGE,
        "limit.missing_data": "📝 Me falta {missing_field}. Indicá ese dato para continuar.",
        "limit.year_confirmation": "📅 Ese mes ya pasó. ¿Aplicamos el límite en {year}?",
        "limit.created": "✅ Límite guardado: {category}, {amount} {currency}, {period}.",
        "limit.updated": "✅ Límite actualizado: {category}, {amount} {currency}, {period}.",
        "limit.cancelled": "Listo, cancelé la configuración del límite. ",
    }
    actions = {
        "limit.listed": [("start_limit", "Crear límite")],
        "limit.started": [("cancel_pending_operation", "Cancelar")],
        "limit.missing_data": [("cancel_pending_operation", "Cancelar")],
        "limit.year_confirmation": [("confirm_limit_year", "Sí, continuar"), ("reject_limit", "Cancelar")],
    }
    nodes = []
    for event, body in bodies.items():
        node = {"id": event.split(".")[1], "type": "text", "body": body}
        if event in actions:
            node.update(type="reply_button", options=[
                {"id": node["id"] + "-" + action, "title": title, "action": action}
                for action, title in actions[event]
            ])
        nodes.append(node)
    return {
        "start_node": "listed", "nodes": nodes,
        "event_nodes": {event: event.split(".")[1] for event in LIMIT_STAGES},
        "response_examples": {},
    }
