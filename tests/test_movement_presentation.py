"""Presentación de anulación: singular y plural correctos."""
from app.services.movement_commands import movement_presentation


def test_annul_presentation_uses_singular_for_one_movement():
    assert movement_presentation({'command_kind': 'annul', 'movements': [{}], 'movement': {}}) == (
        'Anulé 1 movimiento confirmado.')


def test_annul_presentation_uses_plural_for_several_movements():
    assert movement_presentation({'command_kind': 'annul', 'movements': [{}, {}], 'movement': {}}) == (
        'Anulé 2 movimientos confirmados.')
