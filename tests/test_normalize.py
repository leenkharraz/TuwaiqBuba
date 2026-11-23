from decimal import Decimal

from app.normalize import (country_to_location, names_compatible, norm_currency, norm_ident, parse_amount,
                           parse_iso_date, providers_compatible)


def test_amounts_use_decimal_and_arabic_digits():
    assert parse_amount("١٬٢٥٠٫٥٠") == Decimal("1250.50")
    assert parse_amount("1,250.50") == Decimal("1250.50")
    assert parse_amount("SAR 575.00") == Decimal("575.00")
    assert parse_amount("12,5") == Decimal("12.5")
    assert parse_amount("abc") is None
    assert Decimal("0.1") + Decimal("0.2") == parse_amount("0.3")


def test_currency_is_never_guessed():
    assert norm_currency("SAR") == ("SAR", None)
    assert norm_currency("ر.س") == ("SAR", None)
    assert norm_currency("$")[0] is None
    assert norm_currency("ريال")[0] is None


def test_dates_must_be_unambiguous_iso():
    assert parse_iso_date("2026-11-16").isoformat() == "2026-11-16"
    assert parse_iso_date("03/04/2026") is None
    assert parse_iso_date("2026-02-30") is None


def test_name_and_provider_matching():
    assert names_compatible("Reem Khalid Almutairi", "Reem Khalid Al Mutairi") is True
    assert names_compatible("Reem Khalid", "Sara Ahmed") is False
    assert names_compatible("ريم خالد", "Reem Khalid") is None  # different scripts: never auto-matched
    assert providers_compatible("Al Waha Medical Complex", "AL WAHA DEMO MEDICAL COMPLEX") is True
    assert providers_compatible("مجمع الواحة الطبي / Al Waha", "Ibtisama Dental Clinic") is False
    assert norm_ident("INV-24-10871") == norm_ident("inv 2410871")
    assert country_to_location("Saudi Arabia") == "domestic"
    assert country_to_location("United Arab Emirates") == "international"
    assert country_to_location(None) is None
