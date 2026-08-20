from __future__ import annotations

from tfx_quant.telemetry.masking import field_present, mask_account


def test_mask_account_keeps_last_four_characters() -> None:
    assert mask_account("1234567") == "***4567"


def test_mask_account_masks_short_values_entirely() -> None:
    assert mask_account("123") == "***"


def test_mask_account_never_returns_the_original_value() -> None:
    account_no = "8812345678"
    masked = mask_account(account_no)
    assert masked != account_no
    assert account_no[-4:] in masked
    assert account_no[:-4] not in masked


def test_field_present_true_for_non_blank_string() -> None:
    assert field_present("secret") is True


def test_field_present_false_for_none_and_blank() -> None:
    assert field_present(None) is False
    assert field_present("") is False
    assert field_present("   ") is False


def test_field_present_true_for_non_string_truthy_and_falsy_values() -> None:
    assert field_present(0) is True
    assert field_present(False) is True
    assert field_present([]) is True
