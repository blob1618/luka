"""Movement postcommit feedback and recurring-proposal presentation."""
import asyncio
import logging
from decimal import Decimal, InvalidOperation
from uuid import UUID
from app.api.whatsapp import WhatsAppText, WhatsAppReplyButton, WhatsAppReplyButtons
from app.models.database import CandidatoGastoRecurrente, SessionLocal, Usuario
from app.services.budget import BudgetService, BudgetEvaluation, BudgetStatus
from app.services.limit import LimitService
from app.services.recurring_expense import RecurringExpenseService
from app.services.conversation_flow_contract import BUDGET_THRESHOLD_MESSAGE
from app.services.conversation_flow_runtime import ConversationFlowRuntime
from app.services.telemetry import track_phase
from app.services.dispatch_contract import DispatchResult
logger = logging.getLogger(__name__)


def _format_limit_amount(amount) -> str:
    """Formatea un monto con separador de miles '.' y decimales ',' (es-AR)."""
    try:
        decimal_amount = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return str(amount)

    if decimal_amount == decimal_amount.to_integral_value():
        decimal_amount = decimal_amount.quantize(Decimal("1"))
    else:
        decimal_amount = decimal_amount.quantize(Decimal("0.01"))

    formatted = f"{decimal_amount:,.2f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")

def _current_year() -> int:
    from datetime import date

    return date.today().year

def _limit_month_label(month: int, year: int) -> str:
    return LimitService.month_label(month, year, _current_year())

def budget_status_reply(status: BudgetStatus) -> str:
    label = _limit_month_label(status.period_start.month, status.period_start.year)
    spent = _format_limit_amount(status.spent_amount)
    limit = _format_limit_amount(status.limit_amount)
    if status.state == "exceeded":
        exceeded = _format_limit_amount(status.exceeded_amount)
        remaining = _format_limit_amount(status.remaining_amount)
        return (
            f"⚠️ *{status.category_name} — {label}*\n"
            f"Gastaste ${spent} {status.currency} de ${limit} {status.currency}.\n"
            f"Te quedan ${remaining} {status.currency} "
            f"({status.percentage}% usado).\n"
            f"Superaste el límite en ${exceeded} {status.currency}."
        )
    if status.state == "reached":
        remaining = _format_limit_amount(status.remaining_amount)
        return (
            f"🎯 *{status.category_name} — {label}*\n"
            f"Gastaste ${spent} {status.currency} de ${limit} {status.currency}.\n"
            f"Te quedan ${remaining} {status.currency} "
            f"({status.percentage}% usado).\n"
            "Alcanzaste tu límite."
        )
    remaining = _format_limit_amount(status.remaining_amount)
    return (
        f"📊 *{status.category_name} — {label}*\n"
        f"Gastaste ${spent} {status.currency} de ${limit} {status.currency}.\n"
        f"Te quedan ${remaining} {status.currency} ({status.percentage}% usado)."
    )

def budget_feedback_reply(evaluation: BudgetEvaluation) -> str:
    """Devuelve el estado de un límite aplicable, haya exceso o no."""
    budget = evaluation.budget
    print(
        "[BUDGET_EVALUATION]",
        f"movement_id={evaluation.movement_id}",
        f"status={evaluation.status}",
        f"has_limit={evaluation.has_limit}",
        f"limit_id={budget.limit_id if budget is not None else None}",
        f"state={budget.state if budget is not None else None}",
        f"percentage={budget.percentage if budget is not None else None}",
        f"should_alert={evaluation.should_alert}",
    )
    if (
        evaluation.status != "ok"
        or not evaluation.has_limit
        or budget is None
    ):
        return ""
    return budget_status_reply(budget)

def crossed_budget_thresholds(evaluations: list[BudgetEvaluation]) -> list[BudgetStatus]:
    budgets = {}
    for evaluation in evaluations:
        if evaluation.status == "ok" and evaluation.crossed_80_percent and evaluation.budget:
            budgets.setdefault(evaluation.budget.limit_id, evaluation.budget)
    return list(budgets.values())

async def append_budget_threshold_alerts(sender_phone: str, result: DispatchResult) -> None:
    alerts = []
    for budget in result.budget_threshold_alerts:
        variables = {
            "category": budget.category_name,
            "period": _limit_month_label(budget.period_start.month, budget.period_start.year),
            "currency": budget.currency,
            "limit_amount": _format_limit_amount(budget.limit_amount),
            "spent_amount": _format_limit_amount(budget.spent_amount),
            "remaining_amount": _format_limit_amount(budget.remaining_amount),
            "percentage": str(budget.percentage),
            "threshold": "80",
        }
        configured = await ConversationFlowRuntime.render_event(
            sender_phone=sender_phone,
            event_key="budget.threshold_crossed",
            variables=variables,
        )
        alerts.append(configured or WhatsAppText(BUDGET_THRESHOLD_MESSAGE.format(**variables)))
    result.followup_messages[0:0] = alerts

def check_candidate_proposal(
    user_id: UUID | str | None,
    patron_hash: str | None,
) -> dict | None:
    if not user_id or not patron_hash:
        return None
    with track_phase("db"):
        session = SessionLocal()
        try:
            u_id = UUID(str(user_id)) if not isinstance(user_id, UUID) else user_id
            cand = (
                session.query(CandidatoGastoRecurrente)
                .join(Usuario, Usuario.id == CandidatoGastoRecurrente.usuario_id)
                .filter(
                    CandidatoGastoRecurrente.usuario_id == u_id,
                    CandidatoGastoRecurrente.patron_hash == patron_hash,
                    CandidatoGastoRecurrente.estado == "pendiente",
                    Usuario.proactivo_habilitado.is_(True),
                )
                .first()
            )
            if cand and RecurringExpenseService.is_candidate_eligible_for_proposal(cand):
                return {
                    "id": str(cand.id),
                    "concepto": cand.concepto or cand.descripcion_normalizada or "este gasto",
                    "dia_estimado": cand.dia_estimado,
                }
            return None
        except Exception as exc:
            logger.debug("[RECURRING_PROPOSAL_CHECK_ERROR] %s: %s", type(exc).__name__, exc)
            return None
        finally:
            session.close()

def _mark_proposal_followup(
    result: DispatchResult,
    candidate_id: str,
    message: WhatsAppReplyButtons,
) -> None:
    result.followup_messages.append(message)
    result.proposal_candidate_id = candidate_id
    result.proposal_delivery_mode = "followup"
    result.proposal_followup_message = message

def _append_proposal_to_primary(
    result: DispatchResult,
    candidate_id: str,
    proposal_text: str,
    buttons: tuple[WhatsAppReplyButton, ...],
    followup: WhatsAppReplyButtons,
    body: str,
) -> None:
    candidate_reply = f"{body}\n\n{proposal_text}"
    if len(candidate_reply) <= 1024:
        result.reply_text = candidate_reply
        result.reply_message = WhatsAppReplyButtons(body=candidate_reply, buttons=buttons)
        result.proposal_candidate_id = candidate_id
        result.proposal_delivery_mode = "primary"
    else:
        _mark_proposal_followup(result, candidate_id, followup)

def attach_recurring_proposal(result: DispatchResult) -> None:
    proposal = result.debug_info.get("pending_recurring_proposal")
    if not proposal:
        return
    candidate_id = proposal["id"]
    proposal_text = (
        f"💡 Noté que solés pagar *{proposal['concepto']}* alrededor del día "
        f"{proposal['dia_estimado']}. ¿Querés que te avise 3 días antes de cada vencimiento?"
    )
    buttons = (
        WhatsAppReplyButton(id=f"rec_cand:accept:{candidate_id}", title="Sí, avisame"),
        WhatsAppReplyButton(id=f"rec_cand:reject:{candidate_id}", title="No, gracias"),
    )
    followup = WhatsAppReplyButtons(body=proposal_text, buttons=buttons)
    if result.reply_message is None:
        _append_proposal_to_primary(
            result, candidate_id, proposal_text, buttons, followup, result.reply_text
        )
    elif isinstance(result.reply_message, WhatsAppText):
        _append_proposal_to_primary(
            result,
            candidate_id,
            proposal_text,
            buttons,
            followup,
            result.reply_message.body,
        )
    else:
        _mark_proposal_followup(result, candidate_id, followup)

async def apply_movement_feedback(sender_phone, result, committed):
    """Adapter for the two movement receipts currently supported by this domain."""
    movement = committed.get('movement')
    if movement is None:
        return
    try:
        if committed.get('movements'):
            evaluations = await asyncio.to_thread(BudgetService.evaluate_movements, [row['id'] for row in committed['movements']])
        else:
            evaluations = [await asyncio.to_thread(BudgetService.evaluate_movement, movement['id'])]
        for evaluation in evaluations:
            feedback = budget_feedback_reply(evaluation)
            if feedback:
                result.reply_text += '\n\n' + feedback
        result.budget_threshold_alerts = crossed_budget_thresholds(evaluations)
    except Exception as exc:
        logger.warning('pilot_budget_feedback_failed error=%s', type(exc).__name__)
    if isinstance(result.reply_message, WhatsAppText):
        result.reply_message = WhatsAppText(result.reply_text)
    registrations = committed.get('registrations') or ([committed] if committed.get('command_kind') == 'register' else [])
    for registration in registrations:
        proposal = await asyncio.to_thread(check_candidate_proposal, registration.get('owner_id'), registration.get('patron_hash'))
        if proposal:
            result.debug_info['pending_recurring_proposal'] = proposal
            attach_recurring_proposal(result)
            break  # The existing transport presents one recurring proposal at a time.
    await append_budget_threshold_alerts(sender_phone, result)
