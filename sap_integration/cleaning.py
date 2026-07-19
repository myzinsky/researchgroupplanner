from collections import OrderedDict, defaultdict
from decimal import Decimal


def clean_transactions(transactions):
    """Remove exact counter-bookings and group equal remaining positions.

    Actuals and commitments are handled independently. This prevents an
    actual payment from cancelling a commitment for the same amount.
    """
    remaining = _without_counter_bookings(transactions)
    grouped = OrderedDict()

    for transaction in remaining:
        key = (
            transaction["type"],
            transaction.get("business_partner", ""),
            transaction.get("position", ""),
        )
        if key not in grouped:
            grouped[key] = dict(transaction)
            grouped[key]["amount"] = Decimal(transaction["amount"])
        else:
            grouped[key]["amount"] += Decimal(transaction["amount"])

    return [
        transaction
        for transaction in grouped.values()
        if transaction["amount"] != 0
    ]


def _without_counter_bookings(transactions):
    unmatched = defaultdict(list)
    removed = set()

    for index, transaction in enumerate(transactions):
        amount = Decimal(transaction["amount"])
        if amount == 0:
            continue
        key = (transaction["type"], amount)
        opposite_key = (transaction["type"], -amount)
        if unmatched[opposite_key]:
            opposite_index = unmatched[opposite_key].pop()
            removed.update((opposite_index, index))
        else:
            unmatched[key].append(index)

    return [
        transaction
        for index, transaction in enumerate(transactions)
        if index not in removed
    ]
