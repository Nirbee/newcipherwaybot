"""mask_personal: what an API key sees of e-mails, phones and embedded credentials."""

from __future__ import annotations

from src.web.routes.admin.api_key_auth import (
    generate_api_key,
    hash_api_key,
    key_path_blocked,
    mask_email,
    mask_for_key,
    mask_personal,
    mask_phone,
)


def test_mask_email_and_phone_formats() -> None:
    assert mask_email("alice@ya.ru") == "a***@ya.ru"
    assert mask_phone("+79991234512") == "+7***12"
    assert mask_phone("8 (999) 123-45-12") == "8***12"
    assert mask_phone("12") == "***"


def test_personal_fields_masked_recursively() -> None:
    data = {
        "items": [
            {
                "email": "bob.smith@mail.example.com",
                "phone": "+7 999 123 45 67",
                "phone_number": "89991234567",
                "contact_email": "Bob@Gmail.com",
                "email_verified": True,
                "username": "bob_tg",
                "telegram_id": 5072755730,
                "subscription_label": "@bob_tg",
            }
        ],
        "nested": {"user": {"email": None, "phone": ""}},
    }
    out = mask_personal(data)
    row = out["items"][0]
    assert row["email"] == "b***@mail.example.com"
    assert row["phone"] == "+7***67"
    assert row["phone_number"] == "8***67"
    assert row["contact_email"] == "B***@Gmail.com"
    assert row["email_verified"] is True
    # Telegram usernames and ids stay visible
    assert row["username"] == "bob_tg"
    assert row["telegram_id"] == 5072755730
    assert row["subscription_label"] == "@bob_tg"
    assert out["nested"] == {"user": {"email": None, "phone": ""}}


def test_emails_and_plus_phones_inside_free_text() -> None:
    out = mask_personal(
        {
            "text": "пишите на help@cipher.example или звоните +7 (999) 123-45-12",
            "label": "client@ya.ru",
            "flag": "optional",
        }
    )
    assert out["text"] == "пишите на h***@cipher.example или звоните +7***12"
    assert out["label"] == "c***@ya.ru"
    assert out["flag"] == "optional"


def test_ids_dates_and_links_left_alone() -> None:
    data = {
        "created_at": "2026-10-24T10:00:00+00:00",
        "username": "user_5072755730",
        "plan_id": "12345678901",
        "note": "заказ 79991234567",  # a bare digit run in free text is not a phone
        "url": "https://t.me/cipherway_bot?start=ref_abc",
        "email_template": "Ваш код: {code}",
    }
    assert mask_personal(data) == data


def test_credentials_redacted() -> None:
    out = mask_personal(
        {
            "install_code": "AB12CD",
            "claim_url": "https://t.me/bot?start=claim_x",
            "subscription": {
                "subscription_url": "https://sub.example/abc",
                "short_id": "Ab3dE5gH",
                "status": "active",
            },
            "panel_url": "https://panel.example.com",
            "nothing": {"install_code": None},
        }
    )
    assert out["install_code"] == "***"
    assert out["claim_url"] == "***"
    assert out["subscription"] == {
        "subscription_url": "***",
        "short_id": "***",
        "status": "active",
    }
    assert out["panel_url"] == "***"
    assert out["nothing"] == {"install_code": None}


def test_key_format_and_closed_paths() -> None:
    key = generate_api_key()
    assert key.startswith("cwk_") and len(key) == 44
    assert hash_api_key(key) == hash_api_key(key) and len(hash_api_key(key)) == 64
    assert key_path_blocked("/api/admin/admins")
    assert key_path_blocked("/api/admin/admin-keys/1/log")
    assert key_path_blocked("/api/admin/bot-menu/export")
    assert key_path_blocked("/api/admin/bot-menu/export/")
    assert key_path_blocked("/api/admin/site")
    assert key_path_blocked("/api/admin/users/panel-sync")
    assert not key_path_blocked("/api/admin/bot-menu")
    assert not key_path_blocked("/api/admin/users/5")


def test_russian_mobiles_from_8_and_cyrillic_domains() -> None:
    out = mask_personal(
        {
            "text": "звоните 8 (999) 123-45-67 или 89991234567, почта иван@почта.рф",
            "note": "тел.89991234567",
            "order": "заказ 189991234567",  # inside a longer digit run: not a phone
            "landline": "8 495 123-45-67",  # not a mobile (9xx) — left alone
            "vless": "uuid@1.2.3.4",  # numeric «zone» is not an e-mail
        }
    )
    assert out["text"] == "звоните 8***67 или 8***67, почта и***@почта.рф"
    assert out["note"] == "тел.8***67"
    assert out["order"] == "заказ 189991234567"
    assert out["vless"] == "uuid@1.2.3.4"
    assert out["landline"] == "8 495 123-45-67"


def test_path_rules_promocodes_withdrawals_settings() -> None:
    promos = {"items": [{"code": "SUMMER2026", "used": 3}], "total_activations": 3}
    assert mask_for_key("/api/admin/promocodes", promos) == {
        "items": [{"code": "SU****", "used": 3}],
        "total_activations": 3,
    }
    # «code» on other screens is not a secret
    topics = {"items": [{"code": "daily_report"}]}
    assert mask_for_key("/api/admin/report-topics", topics) == topics

    wd = {"items": [{"details": "2200 1234 5678 9012", "method": "card"}, {"details": "TQ1"}]}
    assert mask_for_key("/api/admin/withdrawals", wd) == {
        "items": [{"details": "****9012", "method": "card"}, {"details": "****"}]
    }
    assert mask_for_key("/api/admin/tickets", {"details": "abc"}) == {"details": "abc"}

    settings = {
        "params": [
            {"key": "POSTBACK_URL_PURCHASE", "value": "https://trk.example/pb?token=SECRET"},
            {"key": "POSTBACK_URL_TRIAL", "value": ""},
            {"key": "TRIAL_DAYS", "value": 3},
        ]
    }
    out = mask_for_key("/api/admin/settings", settings)
    assert [r["value"] for r in out["params"]] == ["***", "", 3]
