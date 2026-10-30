"""Discardable PostgreSQL acceptance of real receipt atomicity and owner locking."""
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.database import Base, Usuario, Billetera, MovimientoFinanciero, AssistantWriteReceipt
from app.services.assistant_contracts import PendingCommand
from app.services.movement_commands import commit_command, digest, operation_id, snapshot

def run_acceptance(admin, migration, command_migration=None):
    """Use only an explicitly supplied disposable PostgreSQL engine and migration path."""
    schema = 'assistant_check_' + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text('CREATE SCHEMA ' + schema))
    engine = create_engine(admin.url, connect_args={'options': '-csearch_path=' + schema})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        Base.metadata.create_all(engine)
        owner, wallet = uuid4(), uuid4()
        phone = '5491100260999'
        with factory.begin() as session:
            session.add(Usuario(id=owner, nombre='PG ficticio', email='pg@example.invalid', whatsapp_id=phone))
            session.flush()
            session.add(Billetera(id=wallet, usuario_id=owner, nombre='PG caja', moneda='ARS'))
        payload = {'economic_effect': 'cash_flow', 'movement_type': 'egreso', 'amount': '5', 'currency': 'ARS', 'description': 'PG gasto',
                   'wallet_id': str(wallet), 'category_id': None, 'date': '2026-09-16'}
        pending = PendingCommand(owner_id=str(owner), operation_id=operation_id(owner, 'pg-source'),
            source_message_id='pg-source', original_text='Gasté 5', reference_date='2026-10-28', command_kind='register',
            payload=payload, command_hash=digest('register', payload), expires_at='2099-01-01T00:00:00+00:00')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: commit_command(factory, owner, phone, pending, 'pg-confirm'), range(2)))
        assert results[0]['movement']['id'] == results[1]['movement']['id']
        with factory() as session:
            assert session.query(MovimientoFinanciero).count() == 1
            assert session.query(AssistantWriteReceipt).count() == 1
        print('PASS: concurrent duplicate registration: one movement and one receipt')
        # A real PostgreSQL constraint rejects the receipt after the movement is flushed.
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {schema}.assistant_write_receipts ADD CONSTRAINT test_receipt_failure CHECK (source_message_id <> 'pg-fail')"))
        bad = pending.model_copy(update={'source_message_id': 'pg-fail', 'operation_id': operation_id(owner, 'pg-fail')})
        try:
            commit_command(factory, owner, phone, bad, 'pg-confirm-fail')
        except IntegrityError as exc:
            assert exc.orig.sqlstate == '23514'
            assert exc.orig.diag.constraint_name == 'test_receipt_failure'
        else:
            raise AssertionError('Expected receipt constraint failure')
        with factory() as session:
            assert session.query(MovimientoFinanciero).count() == 1
            assert session.query(AssistantWriteReceipt).count() == 1
        print('PASS: receipt constraint failure rolls back the financial insertion')
        with factory() as session:
            before = snapshot(session.query(MovimientoFinanciero).one())
        changes = {'movement_id': before['id'], 'expected': before, 'date': '2026-08-01'}
        correction = pending.model_copy(update={'operation_id': operation_id(owner, 'pg-correct'),
            'source_message_id': 'pg-correct', 'command_kind': 'correct_date', 'payload': changes,
            'command_hash': digest('correct_date', changes)})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: commit_command(factory, owner, phone, correction, 'pg-confirm-correct'), range(2)))
        with factory() as session:
            after = snapshot(session.query(MovimientoFinanciero).one())
            assert after['date'] == '2026-08-01'
            assert {k: v for k, v in before.items() if k != 'date'} == {k: v for k, v in after.items() if k != 'date'}
            assert session.query(AssistantWriteReceipt).count() == 2
        print('PASS: concurrent correction replay preserves every non-date field')
        if command_migration is not None:
            batch_payload = {'items': [dict(payload, description='PG lote 1'), dict(payload, description='PG lote 2')]}
            batch = PendingCommand.model_validate({**pending.model_dump(),
                'operation_id': operation_id(owner, 'pg-batch', 'register_movement_batch'),
                'source_message_id': 'pg-batch', 'command_kind': 'register_batch',
                'payload': batch_payload, 'command_hash': digest('register_batch', batch_payload)})
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: commit_command(factory, owner, phone, batch, 'pg-batch-confirm'), range(2)))
            assert results[0]['movements'] == results[1]['movements']
            with factory() as session:
                assert session.query(MovimientoFinanciero).count() == 3
                assert session.query(AssistantWriteReceipt).filter_by(command_kind='register_batch').count() == 1
            failing = PendingCommand.model_validate({**batch.model_dump(),
                'operation_id': operation_id(owner, 'pg-batch-fail', 'register_movement_batch'), 'source_message_id': 'pg-fail'})
            try:
                commit_command(factory, owner, phone, failing, 'pg-batch-fail-confirm')
            except IntegrityError as exc:
                assert exc.orig.sqlstate == '23514'
            else:
                raise AssertionError('Expected batch receipt constraint failure')
            with factory() as session:
                assert session.query(MovimientoFinanciero).count() == 3
            target = results[0]['movements'][0]
            update_payload = {'movement_id': target['id'], 'expected': target, 'changes': {'description': 'PG editado'}}
            update = PendingCommand.model_validate({**pending.model_dump(),
                'operation_id': operation_id(owner, 'pg-update', 'update_movement'), 'source_message_id': 'pg-update',
                'command_kind': 'update', 'payload': update_payload, 'command_hash': digest('update', update_payload)})
            edited = commit_command(factory, owner, phone, update, 'pg-update-confirm')
            annul_payload = {'selected': [edited['movement'], results[0]['movements'][1]]}
            annul = PendingCommand.model_validate({**pending.model_dump(),
                'operation_id': operation_id(owner, 'pg-annul', 'annul_movements'), 'source_message_id': 'pg-annul',
                'command_kind': 'annul', 'payload': annul_payload, 'command_hash': digest('annul', annul_payload)})
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: commit_command(factory, owner, phone, annul, 'pg-annul-confirm'), range(2)))
            with factory() as session:
                assert session.query(MovimientoFinanciero).filter(MovimientoFinanciero.anulado_en.isnot(None)).count() == 2
                assert session.query(AssistantWriteReceipt).filter_by(command_kind='annul').count() == 1
                assert session.query(AssistantWriteReceipt).filter_by(command_kind='update').count() == 1
            print('PASS: batch concurrent replay and rollback; update and concurrent annul keep atomic receipts')
        # Verify the migration separately in the disposable public schema, with synthetic roles.
        sql = migration.read_text(encoding='utf-8').replace('public.', schema + '.')
        with engine.begin() as conn:
            conn.execute(text('DROP TABLE ' + schema + '.assistant_write_receipts'))
            for role in ['anon', 'authenticated', 'service_role']:
                conn.execute(text(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role}; END IF; END $$"))
            conn.exec_driver_sql(sql)
            enabled = conn.execute(text('SELECT relrowsecurity FROM pg_class WHERE oid=:table::regclass'.replace(':table::', 'CAST(:table AS text)::')), {'table': schema + '.assistant_write_receipts'}).scalar()
            assert enabled
            for role in ['anon', 'authenticated']:
                assert conn.execute(text('SELECT has_table_privilege(:role,:table,\'SELECT\')'), {'role': role, 'table': schema + '.assistant_write_receipts'}).scalar() is False
        print('PASS: SQL migration applies; RLS enabled and anon/authenticated SELECT revoked')
        if command_migration is not None:
            old_receipt = AssistantWriteReceipt(owner_id=owner, operation_id=operation_id(owner, 'migration-old'),
                command_hash=pending.command_hash, source_message_id='migration-old', confirmation_message_id='migration-old-confirm',
                movement_id=UUID(before['id']), command_kind='register', result={'preserved': True})
            with factory.begin() as session:
                session.add(old_receipt)
            sql = command_migration.read_text(encoding='utf-8-sig').replace('public.', schema + '.')
            with engine.begin() as conn:
                conn.exec_driver_sql(sql)
                # Reapplying also exercises the ORM/canonical constraint-name case.
                conn.exec_driver_sql(sql)
                assert conn.execute(text('SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:name AS regclass)'),
                    {'name': schema + '.assistant_write_receipts'}).scalar()
                for role in ['anon', 'authenticated']:
                    assert not conn.execute(text("SELECT has_table_privilege(:role,:table,'SELECT')"),
                        {'role': role, 'table': schema + '.assistant_write_receipts'}).scalar()
            with factory() as session:
                assert session.query(AssistantWriteReceipt).filter_by(source_message_id='migration-old').one().result == {'preserved': True}
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"UPDATE {schema}.assistant_write_receipts SET command_kind='arbitrary'"))
            except IntegrityError as exc:
                assert exc.orig.sqlstate == '23514' and exc.orig.diag.constraint_name == 'assistant_receipt_kind_check'
            else:
                raise AssertionError('New CHECK must remain a finite allowlist')
            print('PASS: command-kind migration preserves existing receipts/RLS and rejects arbitrary commands')
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
        admin.dispose()
