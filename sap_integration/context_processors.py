from django.conf import settings
from sap_integration.config import financial_positions_enabled


def sap_feature(request):
    return {
        "sap_enabled": settings.SAP_ENABLED or settings.SAP_GM_IMPORT_ENABLED,
        "sap_gm_import_enabled": settings.SAP_GM_IMPORT_ENABLED,
        "sap_financial_positions_enabled": financial_positions_enabled(),
    }
