"""Catálogo curado de monedas: códigos, defaults y alias."""
import pytest

from app.services.currencies import (
    default_wallet_name,
    is_supported,
    resolve_alias,
    supported_codes,
    supported_text,
)


def test_supported_codes_are_the_curated_list():
    assert supported_codes() == ("ARS", "USD", "BRL", "EUR")
    assert supported_text() == "ARS, USD, BRL o EUR"


@pytest.mark.parametrize("value,expected", [
    ("BRL", True), ("eur", True), ("JPY", False), ("", False), (None, False)])
def test_is_supported(value, expected):
    assert is_supported(value) is expected


@pytest.mark.parametrize("value,expected", [
    ("BRL", "Reales"), ("EUR", "Euros"), ("ARS", "Pesos"), ("USD", "Dólares")])
def test_default_wallet_names(value, expected):
    assert default_wallet_name(value) == expected


@pytest.mark.parametrize("value,expected", [
    ("reales", "BRL"), ("real", "BRL"), ("euros", "EUR"), ("dólares", "USD"),
    ("pesos", "ARS"), ("inventada", None)])
def test_alias_resolution(value, expected):
    assert resolve_alias(value) == expected
