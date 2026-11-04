"""Portable Würzburg classification rules, independent of GM E/A-Arten."""

import hashlib
import json
import re
from fnmatch import fnmatchcase

from django.core.exceptions import ValidationError


def validate_codes(values, *, patterns=False):
    if not isinstance(values, list) or len(values) > 200:
        raise ValidationError("Bitte eine Liste mit höchstens 200 Nummern bzw. Mustern angeben.")
    allowed = r"[0-9*?-]+" if patterns else r"[0-9-]+"
    for value in values:
        if not isinstance(value, str) or len(value) > 80 or not re.fullmatch(allowed, value) or not any(c.isdigit() for c in value):
            raise ValidationError("Erlaubt sind Ziffern und Bindestriche; bei Mustern zusätzlich * und ?.")
        if patterns and (not any(c.isdigit() for c in value) or not any(c in value for c in "*?")):
            raise ValidationError("Ein Muster benötigt Ziffern und mindestens einen Platzhalter (* oder ?).")
    if len(set(values)) != len(values):
        raise ValidationError("Nummern bzw. Muster dürfen nicht doppelt vorkommen.")


def validate_exact_codes(values):
    validate_codes(values)


def validate_patterns(values):
    validate_codes(values, patterns=True)


def classify(code, categories):
    """Exact rules take precedence. Multiple matches at the same level block."""
    exact = [c for c in categories if c.is_active and code in c.exact_codes]
    matches = exact or [
        c for c in categories if c.is_active and any(fnmatchcase(code, p) for p in c.patterns)
    ]
    return (matches[0] if len(matches) == 1 else None), matches


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
