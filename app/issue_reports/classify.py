"""Clasificación de reportes del widget con Jev (TypeSafe), vía OpenRouter.

Jev no genera texto: recibe un `state` y preguntas tipadas y devuelve opciones
con probabilidades y confianza. Le preguntamos departamento, severidad y dos
señales sí/no; la prioridad final la decide el código (`_priority`), no el
modelo, para que el tono del reporte ("URGENTE!!!") no la suba por sí solo.

Instrucciones y criterios en inglés: es el idioma principal de Jev. El reporte
del usuario va tal cual (español) dentro del state.
"""
import asyncio
import logging

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, RetryPolicy, Score

from app.config import settings
from app.models import Department, PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_URGENT

log = logging.getLogger(__name__)

# Debajo de esta confianza el reporte se queda en el departamento de triage.
DEPARTMENT_MIN_CONFIDENCE = 0.5
# Umbral para tomar como "sí" una señal Noul.
NOUL_YES = 0.7

_SEVERITY_LEVELS = [
    "Cosmetic or visual issue; the reporter's work is not affected",
    "A feature fails or is degraded, but a workaround exists",
    "Blocks the reporter from completing their work; no workaround",
    "Blocks sales or the operation of a branch or store",
]
_SEVERITY_PRIORITY = [PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_URGENT]


def _priority(severity: float, multi_user: float, feature_request: float) -> str:
    if feature_request >= NOUL_YES:
        return PRIORITY_LOW
    level = max(0, min(round(severity), len(_SEVERITY_PRIORITY) - 1))
    # Varios afectados sube un nivel, salvo lo cosmético.
    if multi_user >= NOUL_YES and 0 < level < len(_SEVERITY_PRIORITY) - 1:
        level += 1
    return _SEVERITY_PRIORITY[level]


async def classify(state: dict, departments: list[Department]) -> dict | None:
    """Devuelve {"department_id", "priority", "jev"} o None si Jev falla o
    tarda más de `jev_timeout_seconds`. `department_id` es None cuando Jev no
    está seguro (o no hay departamentos con triage_description): el llamador
    usa el de triage."""
    candidates = [d for d in departments if (d.triage_description or "").strip()]
    questions = {
        "severity": Score(
            instructions="How much does the reported problem impact the reporter's work, based only on the facts described (not on the tone)?",
            criteria=_SEVERITY_LEVELS,
        ),
        "multi_user": Noul(instructions="The report says that more than one person is affected by the problem"),
        "feature_request": Noul(
            instructions="The report asks for a new feature or an improvement, instead of reporting that something is broken",
        ),
    }
    if len(candidates) > 1:
        questions["department"] = Choice(
            instructions="Which department should handle this internal issue report?",
            criteria={str(d.id): d.triage_description.strip() for d in candidates},
        )

    try:
        async with AsyncTypeSafeClient(
            api_key=settings.openrouter_api_key,
            base_url="https://openrouter.ai/api",
            model=settings.jev_model,
            retry=RetryPolicy(max_retries=1),
        ) as client:
            resp = await asyncio.wait_for(
                client.system_one(state, questions), timeout=settings.jev_timeout_seconds,
            )
    except Exception:
        log.exception("jev classification failed")
        return None

    a = resp.answers
    severity, multi_user, feature = a["severity"], a["multi_user"], a["feature_request"]
    jev = {
        "model": resp.model,
        "severity": {"score": severity.score, "confidence": severity.confidence},
        "multi_user": multi_user.noul,
        "feature_request": feature.noul,
    }

    department_id = None
    if "department" in a:
        dept = a["department"]
        jev["department"] = {
            "choice": dept.choice, "confidence": dept.confidence, "probabilities": dept.probabilities,
        }
        if dept.confidence >= DEPARTMENT_MIN_CONFIDENCE:
            department_id = dept.choice
    elif len(candidates) == 1:
        department_id = str(candidates[0].id)

    return {
        "department_id": department_id,
        "priority": _priority(severity.score, multi_user.noul, feature.noul),
        "jev": jev,
    }
