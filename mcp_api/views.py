"""MCP endpoint (Streamable HTTP transport, JSON responses only) authenticated with personal API tokens."""

import json
import logging

from django.contrib.auth.decorators import login_not_required
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import ApiToken
from .tools import ToolError, available_tools, call_tool

logger = logging.getLogger(__name__)

SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "researchgroupplanner", "title": "Research Group Planner", "version": "1.0"}
INSTRUCTIONS = (
    "Planungswerkzeug einer Forschungsgruppe: Drittmittelprojekte mit Budgets, Mitarbeitende mit Anstellungen, "
    "Finanzierungszuordnungen, Gehältern (TV-L) und Umbuchungen sowie der Abgleich mit SAP. Beträge in Euro, "
    "Datumsangaben im Format JJJJ-MM-TT. Schreibende Werkzeuge stehen nur Mitarbeitenden mit Staff-Rechten zur Verfügung."
    " SAP hat zwei getrennte Datenwege. Bei SAP-Fragen zuerst get_sap_status verwenden. "
    "Würzburg/WebGUI: get_sap_statement liest die bereits aufbereiteten Kontoauszüge inklusive Personal, "
    "get_sap_reconciliation den Reise-/Sachmittelabgleich. Dafür keine GM-Exceldateien anfordern. "
    "import_sap_export, map_sap_cost_type, create_transaction_from_sap und apply_sap_salaries gehören ausschließlich zum GM-Verfahren."
)

# JSON-RPC error codes
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603


def _error(message_id, code, message):
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _result(message_id, result):
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _handle(message, user):
    """Response for one JSON-RPC message, or None for notifications."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
        return _error(message.get("id") if isinstance(message, dict) else None, INVALID_REQUEST, "Invalid request")
    if "id" not in message:
        return None  # notification, e.g. notifications/initialized
    message_id, method, params = message["id"], message["method"], message.get("params") or {}

    if method == "initialize":
        from .sap_reads import enabled_sources

        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
        return _result(message_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS + " Aktive SAP-Verfahren: " + (", ".join(enabled_sources()) or "keines") + ".",
        })
    if method == "ping":
        return _result(message_id, {})
    if method == "tools/list":
        return _result(message_id, {"tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
                "annotations": {"readOnlyHint": not t.write, "destructiveHint": t.write},
            }
            for t in available_tools(user)
        ]})
    if method == "tools/call":
        name, arguments = params.get("name"), params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(message_id, INVALID_PARAMS, "arguments must be an object")
        try:
            data = call_tool(user, name, arguments)
        except ToolError as error:
            return _result(message_id, {"content": [{"type": "text", "text": str(error)}], "isError": True})
        except TypeError as error:
            return _result(message_id, {"content": [{"type": "text", "text": f"Ungültige Parameter: {error}"}], "isError": True})
        except Exception:
            logger.exception("MCP tool %s failed", name)
            return _error(message_id, INTERNAL_ERROR, f"Interner Fehler im Werkzeug {name}")
        text = json.dumps(data, cls=DjangoJSONEncoder, ensure_ascii=False, indent=1)
        return _result(message_id, {"content": [{"type": "text", "text": text}], "isError": False})
    return _error(message_id, METHOD_NOT_FOUND, f"Method not found: {method}")


@csrf_exempt
@login_not_required
def mcp_endpoint(request):
    if request.method != "POST":
        # No server-initiated SSE stream; clients use POST only.
        response = HttpResponse(status=405)
        response["Allow"] = "POST"
        return response

    header = request.headers.get("Authorization", "")
    token = ApiToken.authenticate(header[7:].strip() if header.lower().startswith("bearer ") else "")
    if token is None:
        response = JsonResponse(_error(None, INVALID_REQUEST, "Unauthorized: valid API token required"), status=401)
        response["WWW-Authenticate"] = 'Bearer realm="mcp"'
        return response

    try:
        payload = json.loads(request.body or b"null")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse(_error(None, PARSE_ERROR, "Parse error"), status=400)

    if isinstance(payload, list):
        responses = [r for r in (_handle(message, token.user) for message in payload) if r is not None]
        return JsonResponse(responses, safe=False) if responses else HttpResponse(status=202)
    response = _handle(payload, token.user)
    if response is None:
        return HttpResponse(status=202)
    return JsonResponse(response, encoder=DjangoJSONEncoder)
