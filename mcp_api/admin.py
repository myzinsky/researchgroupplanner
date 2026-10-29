from django.contrib import admin, messages

from .models import ApiToken


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "prefix", "is_active", "created_at", "last_used_at")
    list_filter = ("is_active",)
    fields = ("user", "name", "is_active", "prefix", "created_at", "last_used_at")
    readonly_fields = ("prefix", "created_at", "last_used_at")

    def save_model(self, request, obj, form, change):
        token = None
        if not change:
            token = obj.set_new_key()
        super().save_model(request, obj, form, change)
        if token:
            messages.warning(
                request,
                f"Neuer Token für {obj.user}: {token} – bitte jetzt kopieren, er wird nicht noch einmal angezeigt.",
            )
