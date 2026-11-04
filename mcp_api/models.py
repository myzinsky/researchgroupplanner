import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class ApiToken(models.Model):
    """Personal token for the MCP endpoint; only its hash is stored."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens",
                             verbose_name="Benutzer")
    name = models.CharField("Bezeichnung", max_length=100, help_text="z. B. „Claude Desktop Laptop“")
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    prefix = models.CharField("Anfang", max_length=8, editable=False)
    is_active = models.BooleanField("Aktiv", default=True)
    created_at = models.DateTimeField("Angelegt", auto_now_add=True)
    last_used_at = models.DateTimeField("Zuletzt verwendet", null=True, blank=True, editable=False)

    class Meta:
        ordering = ["user__username", "name"]
        verbose_name = "API-Token"
        verbose_name_plural = "API-Tokens"

    def __str__(self):
        return f"{self.user} – {self.name} ({self.prefix}…)"

    def set_new_key(self):
        """Generate a new token, store its hash and return the plain token (shown only once)."""
        token = "rgp_" + secrets.token_urlsafe(32)
        self.key_hash = hash_token(token)
        self.prefix = token[:8]
        return token

    @classmethod
    def authenticate(cls, token):
        """Active token (with active user) for a plain token, or None; updates the last use."""
        if not token:
            return None
        api_token = cls.objects.select_related("user").filter(key_hash=hash_token(token), is_active=True).first()
        if api_token is None or not api_token.user.is_active:
            return None
        now = timezone.now()
        if api_token.last_used_at is None or (now - api_token.last_used_at).total_seconds() > 60:
            cls.objects.filter(pk=api_token.pk).update(last_used_at=now)
        return api_token
