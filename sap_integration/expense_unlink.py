"""Explicitly detach imported expenses while retaining a complete archive."""

import json

from django.core import serializers
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from projects.models import OtherBudgetItemTransaction, SAPFund
from sap_integration.category_rules import digest
from sap_integration.models import SAPExpenseAudit, SAPExpenseLink, SAPExpenseUnlinkArchive


def unlink_preview(link_ids):
    ids = sorted(set(link_ids))
    links = list(SAPExpenseLink.objects.filter(pk__in=ids).select_related(
        "fund", "planner_transaction__budget_item__project",
        "remainder_transaction__budget_item__project",
    ).order_by("pk"))
    if not ids or len(links) != len(ids):
        raise ValidationError("Die Auswahl hat sich geändert. Bitte die Verknüpfungen neu auswählen.")
    snapshots = []
    for link in links:
        entries = {link.planner_transaction_id: link.planner_transaction}
        if link.remainder_transaction_id:
            entries[link.remainder_transaction_id] = link.remainder_transaction
        records = [link, *link.audit_entries.order_by("pk"), *entries.values()]
        snapshots.append(json.loads(serializers.serialize("json", records)))
    return links, snapshots, digest(snapshots)


@transaction.atomic
def unlink_expenses(link_ids, *, expected_state, user):
    ids = sorted(set(link_ids))
    fund_ids = SAPExpenseLink.objects.filter(pk__in=ids).values_list("fund_id", flat=True)
    # Use the same fund lock as expense imports, so an import cannot replace
    # a link while its history is being archived.
    list(SAPFund.objects.select_for_update().filter(pk__in=fund_ids).order_by("pk"))
    locked = list(SAPExpenseLink.objects.select_for_update().filter(pk__in=ids))
    entry_ids = {link.planner_transaction_id for link in locked}
    entry_ids.update(link.remainder_transaction_id for link in locked if link.remainder_transaction_id)
    entries = list(OtherBudgetItemTransaction.objects.select_for_update().filter(pk__in=entry_ids).order_by("pk"))
    list(SAPExpenseAudit.objects.select_for_update().filter(link_id__in=ids))
    links, snapshots, current_state = unlink_preview(ids)
    if current_state != expected_state:
        raise ValidationError("Verknüpfungen oder Planung wurden inzwischen geändert. Bitte die Vorschau neu öffnen.")

    SAPExpenseAudit.objects.filter(link_id__in=ids).delete()
    SAPExpenseLink.objects.filter(pk__in=ids).delete()
    remaining = SAPExpenseLink.objects.filter(
        Q(planner_transaction_id__in=entry_ids) | Q(remainder_transaction_id__in=entry_ids)
    )
    still_linked = set(remaining.values_list("planner_transaction_id", flat=True))
    still_linked.update(remaining.values_list("remainder_transaction_id", flat=True))
    prefixes = tuple(f"WUE:{link.year}:{link.fund.fund_number}:" for link in links)
    cleared = []
    for entry in entries:
        if entry.pk not in still_linked and (entry.sap_id or "").startswith(prefixes):
            entry.sap_id = ""
            entry.save(update_fields=["sap_id"])
            cleared.append(entry.pk)
    for link, records in zip(links, snapshots):
        related_ids = {link.planner_transaction_id, link.remainder_transaction_id}
        SAPExpenseUnlinkArchive.objects.create(
            original_link_id=link.pk, fund_number=link.fund.fund_number, year=link.year,
            budget_label=str(link.planner_transaction.budget_item), created_by=user,
            snapshot={"records": records, "cleared_reference_ids": sorted(related_ids.intersection(cleared))},
        )
    return len(links)
