"""Cloud-neutral delivery choices; no connectors, authorization, or file transfer.

Verified access is an attestation supplied by the host after an actual tool check.
This module validates its shape and separates it from selecting a service. It
cannot independently authenticate that attestation or execute the external action.
"""

from __future__ import annotations

import copy
import re
from urllib.parse import unquote, urlsplit

from .errors import ValidationError

_CATALOG = [
    {"id": "google_drive", "label": "Google Drive", "url": "https://www.google.com/intx/en/drive/",
     "note": "Место хранения файлов. Доступ конкретного агента проверяется отдельно."},
    {"id": "yandex_disk", "label": "Яндекс Диск", "url": "https://360.yandex.ru/disk/",
     "note": "Можно выбрать для хранения пакета; это не означает наличие подключения у агента."},
    {"id": "mail_cloud", "label": "Облако Mail", "url": "https://cloud.mail.ru/",
     "note": "Облачное хранилище Mail. Подключение почты само по себе не предоставляет доступ к файлам."},
    {"id": "sberdisk", "label": "Диск (sberdisk.ru)", "url": "https://sberdisk.ru/",
     "note": "Сервис на sberdisk.ru. Не определяется автоматически по упоминанию почты Рамблер."},
    {"id": "onedrive", "label": "OneDrive", "url": "https://www.microsoft.com/en-us/microsoft-365/onedrive/online-cloud-storage",
     "note": "Хранилище Microsoft. Права отправителя и получателя могут различаться."},
    {"id": "dropbox", "label": "Dropbox", "url": "https://www.dropbox.com/features/cloud-storage",
     "note": "Место хранения и передачи файлов; доступ агента требует отдельной проверки."},
    {"id": "other", "label": "Другое хранилище", "url": None,
     "note": "Укажите название фактически используемого сервиса. Название не подтверждает его возможности или подключение."},
    {"id": "local", "label": "Папка на компьютере", "url": None,
     "note": "Подходит, если файлы доступны в текущей среде. Папка на другом компьютере не становится общей автоматически."},
    {"id": "chat_upload", "label": "Файл в чате", "url": None,
     "note": "Пользователь может скачать пакет и загрузить его в чат получателя. Это отдельные действия."},
]
_PROVIDERS = {item["id"] for item in _CATALOG}
_LABELS = {item["id"]: item["label"] for item in _CATALOG}
_LOCATION_FIELDS = {"provider", "label", "resource_id", "access", "capabilities", "evidence"}
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_SECRET = re.compile(r"(?i)\b(?:password|passwd|access[_-]?token|refresh[_-]?token|api[_-]?key|secret|token|cookie|authorization|signature)[\"']?\s*[:=]\s*[\"']?\S+|\bbearer\s+\S+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def _require(condition, message):
    if not condition:
        raise ValidationError(message)


def _text(value, label):
    _require(isinstance(value, str) and bool(value.strip()), f"{label}: nonempty text required")
    _require(not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value), f"{label}: control characters are not allowed")


def _safe_text(value):
    """Reject recognizable credential assignments and URLs carrying parameters.

    This is not a general secret scanner. Route metadata must contain only labels,
    stable identifiers, and a description of the actual access check.
    """
    decoded = unquote(unquote(value))
    _require(not _SECRET.search(decoded), "Credentials must not be stored in route metadata")
    for match in _URL.finditer(decoded):
        parsed = urlsplit(match.group(0).rstrip(".,;!)"))
        _require(not parsed.query and not parsed.fragment and not parsed.username and not parsed.password,
                 "Signed, parameterized, credential-bearing or fragment URLs must not be stored in route metadata")


def _qualified(resource_id, provider):
    _require(resource_id is not None, "Verified access requires a stable qualified resource_id")
    parts = resource_id.split("::")
    _require(len(parts) == 3 and all(part and part == part.strip() for part in parts)
             and not re.search(r"[\r\n\t]", resource_id),
             "Use namespace::account-or-owner::resource as resource_id")
    _require(re.fullmatch(r"[a-z][a-z0-9_.-]*", parts[0]) is not None, "Invalid resource namespace")
    expected = {"local": "filesystem", "chat_upload": "chat"}.get(provider, provider)
    if provider != "other":
        _require(parts[0] == expected, "Resource namespace does not match the selected provider")
    return parts


def provider_catalog():
    """Return verified official service links, not a connector-availability list."""
    return copy.deepcopy(_CATALOG)


def normalize_location(value):
    """Normalize a choice while keeping unverified rights unavailable.

    Only provider is required for known choices; other requires a custom label.
    Missing capabilities are false. URLs are not resource identities, and simply
    selecting a provider never upgrades access to verified.
    """
    _require(isinstance(value, dict) and "provider" in value and not set(value) - _LOCATION_FIELDS,
             "Location requires provider and supported location fields only")
    provider = value["provider"]
    _require(isinstance(provider, str) and provider in _PROVIDERS,
             "Unknown provider; use other with the actual service label")
    label = value.get("label", _LABELS[provider] if provider != "other" else None)
    _text(label, "location.label")
    resource_id = value.get("resource_id")
    if resource_id is not None:
        _text(resource_id, "resource_id")
        _require(not re.search(r"[a-z][a-z0-9+.-]*://", unquote(unquote(resource_id)), re.IGNORECASE),
                 "A URL is not a stable account-qualified resource identity")
    access = value.get("access", "unknown")
    _require(isinstance(access, str) and access in {"unknown", "manual", "verified"}, "Invalid access status")
    caps = value.get("capabilities", {})
    _require(isinstance(caps, dict) and not set(caps) - {"read", "write"}, "Capabilities must contain read/write only")
    capabilities = {key: caps.get(key, False) for key in ("read", "write")}
    _require(all(type(item) is bool for item in capabilities.values()), "Capabilities must be booleans")
    evidence = value.get("evidence")
    if evidence is not None:
        _text(evidence, "access evidence")
    for text in (label, resource_id, evidence):
        if text is not None:
            _safe_text(text)
    if access == "verified":
        _qualified(resource_id, provider)
        _text(evidence, "Verified access evidence")
        _require(_URL.fullmatch(evidence.strip()) is None,
                 "A service URL alone does not demonstrate actual access")
    else:
        _require(not any(capabilities.values()), "Agent capabilities require verified access, not a selected service or manual route")
    return {"provider": provider, "label": label, "resource_id": resource_id,
            "access": access, "capabilities": capabilities, "evidence": evidence}


def _shared(sender, recipient):
    _require(recipient is not None, "Choose a recipient location before using shared_folder")
    _require(sender["provider"] == recipient["provider"] and sender["provider"] != "chat_upload",
             "A shared folder requires the same actual storage provider, not chat uploads")
    _require(sender["resource_id"] is not None and sender["resource_id"] == recipient["resource_id"],
             "Same service or folder title does not establish the same shared resource")
    _qualified(sender["resource_id"], sender["provider"])
    _require(sender["access"] == "verified" and sender["capabilities"]["write"],
             "Sender write access to this resource has not been verified")
    _require(recipient["access"] == "verified" and recipient["capabilities"]["read"],
             "Recipient read access to this resource has not been verified")


def create_route(config):
    _require(isinstance(config, dict) and {"sender", "copy_mode"} <= set(config)
             and not set(config) - {"sender", "recipient", "copy_mode"}, "Invalid route config")
    mode = config["copy_mode"]
    _require(isinstance(mode, str) and mode in {"user_copy", "shared_folder"}, "Invalid route copy_mode")
    sender = normalize_location(config["sender"])
    recipient = normalize_location(config["recipient"]) if config.get("recipient") is not None else None
    if mode == "shared_folder":
        _shared(sender, recipient)
    return {"format": "portable-memory-route", "schema_version": 1,
            "sender": sender, "recipient": recipient, "copy_mode": mode}


def validate_route(route):
    _require(isinstance(route, dict) and set(route) == {"format", "schema_version", "sender", "recipient", "copy_mode"}, "Invalid route fields")
    _require(route["format"] == "portable-memory-route" and type(route["schema_version"]) is int
             and route["schema_version"] == 1, "Unsupported route format")
    expected = create_route({key: route[key] for key in ("sender", "recipient", "copy_mode")})
    _require(expected == route, "Route is not normalized")


def route_view(route):
    """Describe the selected route without implying upload, copying, or delivery."""
    validate_route(route)
    sender, recipient = route["sender"], route["recipient"]
    sender_write = sender["access"] == "verified" and sender["capabilities"]["write"]
    recipient_read = recipient is not None and recipient["access"] == "verified" and recipient["capabilities"]["read"]
    pending = []
    if recipient is None:
        pending.append({"side": "recipient", "reason": "choice_pending", "detail": "Место получения ещё можно выбрать; подготовку пакета продолжайте."})
    for side, location, capability in (("sender", sender, "write"), ("recipient", recipient, "read")):
        if location is None or location["access"] == "manual":
            continue
        if location["access"] != "verified" or not location["capabilities"][capability]:
            pending.append({"side": side, "reason": "access_pending", "detail":
                            ("Запись агентом в выбранное место ещё не подтверждена." if side == "sender" else "Чтение выбранного места агентом-получателем ещё не подтверждено.")})
    same_provider = recipient is not None and sender["provider"] == recipient["provider"]
    same_resource = same_provider and sender["resource_id"] is not None and sender["resource_id"] == recipient["resource_id"]
    if route["copy_mode"] == "shared_folder":
        status = "shared_folder_ready"
        summary = "Доступ отправителя на запись и получателя на чтение одной папки подтверждён. Передача ещё не выполнена."
        steps = ["Создать и проверить пакет.", "Сохранить его в подтверждённую папку доступным инструментом.", "Получателю прочитать этот пакет и проверить состав и контрольные суммы."]
    else:
        status = "recipient_not_chosen" if recipient is None else ("access_pending" if pending else "manual_transfer_ready")
        summary = f"Выбрано место подготовки: {sender['label']}. "
        summary += (f"Место получения: {recipient['label']}. " if recipient else "Место получения пока не выбрано. ")
        summary += "Пакет можно подготовить сейчас; копирование и получение выполняются отдельно."
        steps = ["Создать и проверить пакет.", "Пользователю скачать или скопировать пакет выбранным способом.", "Передать пакет получателю; при использовании другого облака загрузить копию туда.", "Получателю проверить пакет после загрузки."]
    return {"copy_mode": route["copy_mode"], "status": status, "summary": summary,
            "sender": copy.deepcopy(sender), "recipient": copy.deepcopy(recipient),
            "same_provider": bool(same_provider), "same_resource": bool(same_resource),
            "sender_can_write": bool(sender_write), "recipient_can_read": bool(recipient_read),
            "package_creation_allowed": True, "automatic_transfer": False, "delivery_status": "not_started",
            "pending": pending, "next_steps": steps,
            "limitations": ["Выбор сервиса не устанавливает подключение и не переносит авторизацию.",
                            "Права указаны по результатам проверки в текущей среде; модуль сам не обращается к облаку.",
                            "Готовность маршрута не означает, что файл уже сохранён, скопирован или принят."]}
