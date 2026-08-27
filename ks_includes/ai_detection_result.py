DEFECT_MODEL_KEYS = {
    "SPAGHETTI": "spaghetti",
    "NOZZLE_BLOB": "nozzle_blob",
    "FOREIGN_OBJECT": "foreign_object",
    "UNDER_EXTRUSION": "under_extrusion",
    "OVER_EXTRUSION": "over_extrusion",
    "WARPING": "warping",
}


def canonical_defect_type(value):
    """Return the normalized public V3 defect type."""
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    public_value = raw_value.upper()
    return public_value


def canonical_defect_map(value):
    """Normalize policy map keys to public V3 defect types."""
    if not isinstance(value, dict):
        return {}
    return {
        canonical_defect_type(defect_type): item
        for defect_type, item in value.items()
        if canonical_defect_type(defect_type)
    }


def is_auto_pause_fault(result):
    """Return whether a V3 latest result represents an active AI pause."""
    if not isinstance(result, dict):
        return False
    action = result.get("action")
    if not isinstance(action, dict):
        return False
    return bool(
        str(result.get("riskLevel") or "").upper() == "CRITICAL"
        and str(result.get("incidentState") or "").upper() in ("OPEN", "RECOVERING")
        and str(action.get("type") or "").upper() == "PAUSE"
        and str(action.get("state") or "").upper() in ("SENT", "ACKNOWLEDGED")
    )


def result_signature(result):
    """Build a stable signature so the same fault is not shown repeatedly."""
    if not isinstance(result, dict):
        return None
    action = result.get("action") if isinstance(result.get("action"), dict) else {}
    return (
        result.get("captureId"),
        result.get("incidentId"),
        result.get("capturedAt"),
        action.get("actionId"),
        action.get("state"),
        result.get("evidencePath"),
    )


def current_auto_pause_fault(status):
    """Return the current session's confirmed AI pause result, if any."""
    if not isinstance(status, dict):
        return None
    if (
        str(status.get("printState") or "").lower() != "paused"
        and status.get("pauseResumePaused") is not True
    ):
        return None

    print_job_id = str(status.get("printJobId") or "")
    latest = status.get("latest")
    gate = status.get("autoPauseGate")
    if not print_job_id or not isinstance(latest, dict) or not isinstance(gate, dict):
        return None
    if str(latest.get("printJobId") or "") != print_job_id:
        return None
    if str(gate.get("printJobId") or "") != print_job_id:
        return None
    if str(gate.get("phase") or "").upper() != "PAUSED_CONFIRMED":
        return None

    incident_id = str(latest.get("incidentId") or "")
    action = latest.get("action")
    if not incident_id or not isinstance(action, dict):
        return None
    if str(gate.get("incidentId") or "") != incident_id:
        return None
    if str(gate.get("actionId") or "") != str(action.get("actionId") or ""):
        return None
    return latest if is_auto_pause_fault(latest) else None
