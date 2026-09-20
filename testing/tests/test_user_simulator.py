"""Tests for UserSimulator service."""

import pytest

from testing.services.user_simulator import UserSimulator, sync_test_user
from app.models.database import Categoria, Usuario


class TestTestUserFixture:
    def test_fixture_creates_persisted_user(self, test_user):
        user = test_user["user"]
        session = test_user["session"]

        assert user.whatsapp_id == "5491112345678"
        assert user.id is not None
        assert session.get(Usuario, user.id) is user


class TestCreateUser:
    def test_creates_user_with_phone_and_name(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        user = sim.create_test_user("5491100001111", "María Test")

        assert user.whatsapp_id == "5491100001111"
        assert user.nombre == "María Test"
        assert user.id is not None

    def test_create_duplicate_returns_existing(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        user1 = sim.create_test_user("5491100001111", "María")
        user2 = sim.create_test_user("5491100001111", "María Distinta")

        assert user1.id == user2.id

    def test_create_duplicate_updates_name(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        sim.create_test_user("5491100001111", "María")
        user = sim.create_test_user("5491100001111", "María Distinta")

        assert user.nombre == "María Distinta"
        assert sim.get_user("5491100001111").nombre == "María Distinta"

    def test_get_user_returns_none_when_not_exists(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        assert sim.get_user("9999999999") is None


class TestSyncTestUser:
    def test_registered_crea_el_usuario(self, in_memory_db):
        sync_test_user(
            in_memory_db["SessionLocal"],
            phone="5491100001111",
            name="María",
            registered=True,
        )

        user = UserSimulator(in_memory_db["SessionLocal"]).get_user("5491100001111")
        assert user is not None
        assert user.nombre == "María"

    def test_registered_recupera_usuario_existente(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        existing = sim.create_test_user("5491100001111", "Original")

        sync_test_user(
            in_memory_db["SessionLocal"],
            phone="5491100001111",
            name="Otro Nombre",
            registered=True,
        )

        assert sim.get_user("5491100001111").id == existing.id

    def test_unregistered_elimina_el_usuario(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        sim.create_test_user("5491100001111", "María")

        sync_test_user(
            in_memory_db["SessionLocal"],
            phone="5491100001111",
            name="María",
            registered=False,
        )

        assert sim.get_user("5491100001111") is None

    def test_unregistered_sin_usuario_no_falla(self, in_memory_db):
        sync_test_user(
            in_memory_db["SessionLocal"],
            phone="9999999999",
            name="Nadie",
            registered=False,
        )


class TestDeleteUser:
    def test_deletes_existing_user(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        sim.create_test_user("5491100001111", "María")
        sim.delete_test_user("5491100001111")

        assert sim.get_user("5491100001111") is None

    def test_delete_nonexistent_user_no_error(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        sim.delete_test_user("9999999999")  # should not raise


class TestResetUserData:
    def test_clears_movements_and_categories(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        user = sim.create_test_user("5491100001111", "María")
        sim.seed_categories("5491100001111", ["comida", "transporte"])

        sim.reset_user_data("5491100001111")

        # User still exists
        assert sim.get_user("5491100001111") is not None
        # But categories are gone
        session = in_memory_db["SessionLocal"]()
        cats = session.query(Categoria).filter(Categoria.usuario_id == user.id).all()
        session.close()
        assert len(cats) == 0

    def test_reset_nonexistent_user_no_error(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        sim.reset_user_data("9999999999")  # should not raise


class TestSeedCategories:
    def test_creates_categories_for_user(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        sim.create_test_user("5491100001111", "María")
        cats = sim.seed_categories("5491100001111", ["comida", "transporte", "salud"])

        assert len(cats) == 3
        assert {c.nombre for c in cats} == {"comida", "transporte", "salud"}

    def test_seed_idempotent_no_duplicates(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        sim.create_test_user("5491100001111", "María")
        sim.seed_categories("5491100001111", ["comida", "comida"])

        session = in_memory_db["SessionLocal"]()
        cats = session.query(Categoria).all()
        session.close()
        assert len(cats) == 1

    def test_seed_without_user_raises(self, in_memory_db):
        sim = UserSimulator(in_memory_db["SessionLocal"])
        with pytest.raises(ValueError, match="usuario"):
            sim.seed_categories("9999999999", ["comida"])
