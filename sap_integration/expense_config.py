"""Versioned configuration exchange. Never serialize database IDs or SAP data."""

import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction

from sap_integration.category_rules import digest
from sap_integration.models import SAPExpenseCategory


FIELDS = ("key", "label", "kind", "exact_codes", "patterns", "is_active")
FORMAT = "sap-wuerzburg-expense-categories"
MAX_CONFIG_BYTES = 256_000


def local_config_path():
    return Path(settings.SAP_DATA_DIR) / "config" / "finanzpositionskategorien.json"


def load_local_config():
    """Institution-specific lists stay in local data, never in shipped code."""
    try:
        with local_config_path().open("rb") as source:
            data = source.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise ValidationError("Keine lesbare lokale Kategorienvorlage vorhanden. Bitte eine Konfigurationsdatei importieren oder Kategorien manuell anlegen.") from error
    return read_config(data)


def export_config():
    return {"format": FORMAT, "version": 1, "categories": list(
        SAPExpenseCategory.objects.order_by("key").values(*FIELDS)
    )}


def read_config(data):
    if len(data) > MAX_CONFIG_BYTES:
        raise ValidationError("Die Konfiguration darf höchstens 256 KB groß sein.")
    try:
        payload = json.loads(data)
    except (ValueError, UnicodeDecodeError) as error:
        raise ValidationError("Die Datei enthält kein gültiges JSON.") from error
    if not isinstance(payload, dict) or payload.get("format") != FORMAT or payload.get("version") != 1:
        raise ValidationError("Erwartet wird eine Kategorienkonfiguration für Finanzpositionen in Version 1.")
    rows = payload.get("categories")
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValidationError("Die Datei darf höchstens 200 Kategorien enthalten.")
    keys = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(FIELDS):
            raise ValidationError("Jede Kategorie muss genau Kennung, Bezeichnung, Kostenart, Nummern, Muster und Aktivstatus enthalten.")
        if not isinstance(row["is_active"], bool) or any(not isinstance(row[k], str) for k in ("key", "label", "kind")):
            raise ValidationError("Ungültige Feldtypen in der Kategorienkonfiguration.")
        category = SAPExpenseCategory(**row)
        category.full_clean(validate_unique=False, validate_constraints=False)
        if category.key in keys:
            raise ValidationError(f"Doppelte Kategoriekennung: {category.key}")
        keys.add(category.key)
    return payload


def preview_config(payload):
    existing = {row["key"]: row for row in export_config()["categories"]}
    return [{"incoming": row, "existing": existing.get(row["key"]),
             "status": "new" if row["key"] not in existing else (
                 "same" if row == existing[row["key"]] else "conflict"
             )} for row in payload["categories"]]


@transaction.atomic
def apply_config(payload, *, expected_digest, replace_keys):
    # Lock existing configuration and reject previews based on an older state.
    list(SAPExpenseCategory.objects.select_for_update().all())
    if digest(export_config()) != expected_digest:
        raise ValidationError("Die Konfiguration wurde inzwischen geändert. Bitte die Vorschau erneut öffnen.")
    payload = read_config(json.dumps(payload).encode())
    count = 0
    for entry in preview_config(payload):
        row = entry["incoming"]
        if entry["status"] == "new" or (entry["status"] == "conflict" and row["key"] in replace_keys):
            SAPExpenseCategory.objects.update_or_create(key=row["key"], defaults={k: v for k, v in row.items() if k != "key"})
            count += 1
    return count
