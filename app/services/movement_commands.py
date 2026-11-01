"""Confirmed movement commands: one transaction for mutation and durable receipt."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from uuid import UUID

from app.models.database import AssistantWriteReceipt, Billetera, Categoria, MovimientoFinanciero, Usuario
from app.services.finance import FinanceService
from app.services.assistant_contracts import amount_text


def movement_presentation(result):
    if result['command_kind'] == 'register_batch':
        return 'Registré estos movimientos:\n' + '\n'.join(
            f"{row['description']}: {row['currency']} {amount_text(row['amount'])}, fecha {row['date']}." for row in result['movements'])
    row = result['movement']
    if result['command_kind'] == 'annul':
        count = len(result['movements'])
        noun = 'movimiento' if count == 1 else 'movimientos'
        adjective = 'confirmado' if count == 1 else 'confirmados'
        return f"Anulé {count} {noun} {adjective}."
    if result['command_kind'] == 'update':
        return f"Actualicé {row['description']}: {row['currency']} {amount_text(row['amount'])}, fecha {row['date']}."
    action = 'Registré' if result['command_kind'] == 'register' else 'Corregí solo la fecha de'
    return f"{action} {row['description']}: {row['currency']} {amount_text(row['amount'])}, fecha {row['date']}."


def digest(kind, payload):
    return hashlib.sha256(json.dumps({'kind': kind, 'payload': payload}, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def operation_id(owner_id, message_id, operation_key=None, slot=0):
    if not message_id:
        raise ValueError('A stable inbound message ID is required')
    # Preserve the receipt lookup for earlier IDs; new operations include their contract key.
    identity = f'{owner_id}:{message_id}:{slot}' if operation_key is None else f'{owner_id}:{message_id}:{operation_key}:{slot}'
    return hashlib.sha256(identity.encode()).hexdigest()


def snapshot(movement):
    return {'id': str(movement.id), 'amount': str(movement.cantidad), 'currency': movement.moneda,
            'description': movement.descripcion, 'date': movement.fecha_movimiento.isoformat(),
            'wallet_id': str(movement.billetera_id), 'category_id': str(movement.categoria_id),
            'movement_type': movement.tipo}


def receipt_result(factory, owner_id, *, confirmation_message_id=None, op_id=None):
    with factory() as session:
        q = session.query(AssistantWriteReceipt).filter(AssistantWriteReceipt.owner_id == owner_id)
        if confirmation_message_id:
            q = q.filter(AssistantWriteReceipt.confirmation_message_id == confirmation_message_id)
        elif op_id:
            q = q.filter(AssistantWriteReceipt.operation_id == op_id)
        else:
            return None
        receipt = q.first()
        if receipt is None:
            return None
        result = dict(receipt.result)
        # Existing receipts remain immutable; presentation is a projection on read.
        result.setdefault('presentation', movement_presentation(result))
        return result


def validate_dependencies(session, owner_id, wallet_id, currency, category_id):
    wallet = session.query(Billetera).filter(Billetera.id == UUID(wallet_id),
        Billetera.usuario_id == owner_id, Billetera.moneda == currency).first()
    if wallet is None:
        raise ValueError('La billetera ya no está disponible')
    if category_id:
        category = session.query(Categoria).filter(Categoria.id == UUID(category_id),
            Categoria.usuario_id == owner_id, Categoria.esta_eliminado.is_(False)).first()
        if category is None:
            raise ValueError('La categoría ya no está disponible')
        return category
    return None


def commit_command(factory, owner_id, phone, pending, confirmation_message_id, *, now=None):
    """Serialize this owner's commands and replay committed results before mutating."""
    now = now or datetime.now(timezone.utc)
    if not confirmation_message_id:
        raise ValueError('La confirmación necesita identificador de mensaje')
    if pending.owner_id != str(owner_id) or pending.command_hash != digest(pending.command_kind, pending.payload):
        raise ValueError('La propuesta no corresponde a esta cuenta o cambió')
    with factory.begin() as session:
        user = session.query(Usuario).filter(Usuario.id == owner_id, Usuario.whatsapp_id == phone).with_for_update().first()
        if user is None:
            raise ValueError('La cuenta ya no está disponible')
        prior = session.query(AssistantWriteReceipt).filter(
            AssistantWriteReceipt.owner_id == owner_id,
            AssistantWriteReceipt.operation_id == pending.operation_id).first()
        if prior:
            if prior.command_hash != pending.command_hash:
                raise ValueError('Esta operación ya tiene otro contenido')
            return {**dict(prior.result), 'replayed': True}
        if datetime.fromisoformat(pending.expires_at) <= now:
            raise ValueError('La propuesta venció; volvé a enviarla')
        payload = pending.payload
        movement_rows, registrations = None, []
        if pending.command_kind == 'register_batch':
            identifiers = []
            for index, item in enumerate(payload['items']):
                category = validate_dependencies(session, owner_id, item['wallet_id'], item['currency'], item.get('category_id'))
                registered = FinanceService.register_movement_with_category(phone,
                    f'{pending.source_message_id}:batch:{index}', pending.original_text, item['movement_type'],
                    Decimal(item['amount']), item['currency'], item['description'], category.nombre if category else None,
                    create_category_if_missing=False, fecha_movimiento=datetime.fromisoformat(item['date']).date(),
                    wallet_id=item['wallet_id'], session=session, economic_effect=item.get('economic_effect'))
                if registered.status != 'registered':
                    raise ValueError('No se guardó el lote completo: ' + registered.status)
                identifiers.append(UUID(registered.movement_id))
                registrations.append({'owner_id': str(owner_id), 'patron_hash': registered.patron_hash})
            movement_id = identifiers[0]
            movement_rows = [snapshot(session.get(MovimientoFinanciero, identifier)) for identifier in identifiers]
        elif pending.command_kind == 'annul':
            selected = payload['selected']
            if len(selected) == 1:
                mutation = FinanceService.annul_movement(phone, selected[0]['id'], selected[0], session=session)
            else:
                mutation = FinanceService.annul_movements(phone, selected, session=session)
            if mutation.status != 'annulled':
                raise ValueError('No se anuló el conjunto: ' + mutation.status)
            movement_id = UUID(selected[0]['id'])
            movement_rows = selected
        elif pending.command_kind == 'update':
            movement_id = UUID(payload['movement_id'])
            mutation = FinanceService.update_movement(phone, str(movement_id), payload['changes'], payload['expected'], session=session)
            if mutation.status != 'updated':
                raise ValueError('No se actualizó el movimiento: ' + mutation.status)
        elif pending.command_kind == 'register':
            category = validate_dependencies(session, owner_id, payload['wallet_id'], payload['currency'], payload.get('category_id'))
            result = FinanceService.register_movement_with_category(
                phone, pending.source_message_id, pending.original_text,
                payload['movement_type'], Decimal(payload['amount']), payload['currency'],
                payload['description'], category.nombre if category else None,
                create_category_if_missing=False,
                fecha_movimiento=datetime.fromisoformat(payload['date']).date(),
                wallet_id=payload['wallet_id'], session=session,
                economic_effect=payload.get('economic_effect'),
            )
            if result.status != 'registered':
                raise ValueError('No se guardó el movimiento: ' + result.status)
            movement_id = UUID(result.movement_id)
        elif pending.command_kind == 'correct_date':
            movement_id = UUID(payload['movement_id'])
            row = session.query(MovimientoFinanciero).filter(MovimientoFinanciero.id == movement_id,
                MovimientoFinanciero.usuario_id == owner_id, MovimientoFinanciero.anulado_en.is_(None)).with_for_update().first()
            if row is None:
                raise ValueError('El movimiento ya no está disponible')
            category_id = str(row.categoria_id) if row.categoria_id else None
            validate_dependencies(session, owner_id, str(row.billetera_id), row.moneda, category_id)
            result = FinanceService.update_movement(phone, str(movement_id), {'fecha': payload['date']},
                payload['expected'], session=session)
            if result.status != 'updated':
                raise ValueError('No se corrigió el movimiento: ' + result.status)
        else:
            raise ValueError('Tipo de comando desconocido')
        row = session.get(MovimientoFinanciero, movement_id)
        stored = {'status': 'committed', 'operation_id': pending.operation_id,
                  'command_kind': pending.command_kind, 'movement': snapshot(row),
                  'revision': pending.version,
                  'version_token': hashlib.sha256(f'{pending.version}:{pending.authorization_hash or pending.command_hash}'.encode()).hexdigest()[:16]}
        category = session.get(Categoria, row.categoria_id) if row.categoria_id else None
        stored['category_name'] = category.nombre if category else 'Sin categoría'
        if pending.command_kind == 'register':
            stored.update(owner_id=str(owner_id), patron_hash=result.patron_hash)
        elif pending.command_kind in {'correct_date', 'update'}:
            stored['before'] = payload['expected']
        if movement_rows is not None:
            stored['movements'] = movement_rows
        if registrations:
            stored['registrations'] = registrations
        stored['presentation'] = movement_presentation(stored)
        session.add(AssistantWriteReceipt(owner_id=owner_id, operation_id=pending.operation_id,
            command_hash=pending.command_hash, source_message_id=pending.source_message_id,
            confirmation_message_id=confirmation_message_id, movement_id=movement_id,
            command_kind=pending.command_kind, result=stored))
        session.flush()
        return stored
