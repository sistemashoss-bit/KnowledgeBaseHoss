"""Límites de uso de MAX (Valkey). Si Valkey no está, no limita (igual que el
resto de rate limits de la app); el tope de tools por invocación sigue."""
from datetime import datetime, timedelta, timezone

from app import valkey_client as vk
from app.config import settings


def busy_key(conv_id) -> str:
    return f"max:busy:{conv_id}"


def acquire_busy(conv_id) -> bool:
    """Una invocación a la vez por chat."""
    return vk.acquire_lock(busy_key(conv_id), settings.assistant_busy_ttl_seconds)


def release_busy(conv_id) -> None:
    vk.release_lock(busy_key(conv_id))


def _day_key(user_id) -> str:
    return f"max:user:{user_id}:day"


def _user_checks(user_id) -> list:
    return [
        ("minute", f"max:user:{user_id}:min", settings.assistant_user_per_minute, 60),
        ("day", _day_key(user_id), settings.assistant_user_per_day, 86400),
    ]


def _conv_check(conv_id) -> tuple:
    return ("conversation", f"max:conv:{conv_id}:hour", settings.assistant_conv_per_hour, 3600)


def _anon_check(ip: str) -> tuple:
    return ("anon", f"max:anon:{ip}:day", settings.assistant_anon_per_day, 86400)


def _first_hit(checks) -> tuple[str, int] | None:
    """Cuenta el intento en todos los límites; si alguno se pasa, lo deshace en
    todos (un intento bloqueado no gasta cupo de nada) y devuelve (motivo,
    segundos de espera) del primero que bloqueó, en el orden de `checks`."""
    hit = None
    for reason, key, limit, window in checks:
        if vk.hit_rate_limit(key, limit, window) and hit is None:
            hit = reason, vk.ttl(f"rl:{key}") or window
    if hit:
        for _, key, _, _ in checks:
            vk.undo_rate_limit(key)
    return hit


def _refund(checks) -> None:
    for _, key, _, _ in checks:
        vk.undo_rate_limit(key)


def check_user(user_id) -> tuple[str, int] | None:
    """Solo los límites por persona (widget y editor); comparten el cupo con el chat."""
    return _first_hit(_user_checks(user_id))


def check(user_id, conv_id) -> tuple[str, int] | None:
    """Cuenta la invocación y devuelve (motivo, segundos de espera) si se pasó
    de algún límite, o None si puede seguir. Si bloquea, no se cuenta en
    ninguno (p. ej. el tope del grupo no le gasta cupo a la persona)."""
    return _first_hit(_user_checks(user_id) + [_conv_check(conv_id)])


def check_ip(ip: str) -> tuple[str, int] | None:
    """Landing sin sesión: preguntas por IP."""
    return _first_hit([_anon_check(ip)])


def refund(user_id, conv_id=None) -> None:
    """Devuelve el cupo diario (y el del grupo) cuando el modelo falló y no hubo
    respuesta. El de ráfaga se queda: frena reintentos seguidos."""
    _refund([c for c in _user_checks(user_id) if c[0] == "day"] + ([_conv_check(conv_id)] if conv_id else []))


def refund_ip(ip: str) -> None:
    _refund([_anon_check(ip)])


def should_warn(subject, reason: str, wait: int) -> bool:
    """True una sola vez por ventana: evita repetir el aviso de límite."""
    return vk.set_flag(f"max:warned:{subject}:{reason}", max(wait, 1))


def limit_message(reason: str, wait: int) -> str:
    minutes = max(1, round(wait / 60))
    if reason == "minute":
        return "Vas muy rápido; dame un minuto y vuelve a preguntarme."
    if reason == "day":
        hours = max(1, round(wait / 3600))
        return f"Llegaste a tu límite de consultas de hoy. Podrás preguntarme de nuevo en unas {hours} h."
    if reason == "anon":
        hours = max(1, round(wait / 3600))
        return (f"Llegaste al límite de {settings.assistant_anon_per_day} preguntas. Inicia sesión para "
                f"seguir preguntando o vuelve en unas {hours} h.")
    if reason == "conversation":
        return f"Este grupo llegó al límite de consultas por hora. Intenten de nuevo en unos {minutes} min."
    return "Ya estoy respondiendo en este chat; pregúntame de nuevo en un momento."


# ── Consulta del cupo diario (perfil y panel de admin) ───────────────────────
# El cupo es una ventana de 24 h que empieza con la primera consulta (no a
# medianoche) y la comparten el chat, el widget y el asistente del editor.

def _usage(count: int, left: int) -> dict:
    limit = settings.assistant_user_per_day
    used = min(count, limit)  # por si bajaron el límite con la ventana abierta
    return {
        "used": used,
        "limit": limit,
        "remaining": limit - used,
        "blocked": count >= limit,
        "resets_in": left if count else 0,
        "resets_at": datetime.now(timezone.utc) + timedelta(seconds=left) if count and left else None,
    }


def daily_usage(user_id) -> dict | None:
    """Uso del cupo diario de una persona, sin contar una consulta. None si
    Valkey no está (en ese caso no se limita)."""
    raw = vk.read_rate_limit(_day_key(user_id))
    return None if raw is None else _usage(*raw)


def daily_usage_all() -> dict | None:
    """{user_id (str): uso} de quienes tienen consultas en su ventana actual."""
    raw = vk.scan_rate_limits(_day_key("*"))
    if raw is None:
        return None
    return {key.split(":")[2]: _usage(*value) for key, value in raw.items()}


def format_wait(seconds: int) -> str:
    """"3 h 20 min", "15 min", "menos de 1 min"."""
    if seconds <= 0:
        return ""
    if seconds < 60:
        return "menos de 1 min"
    h, m = divmod(-(-seconds // 60), 60)
    return f"{h} h {m} min" if h and m else (f"{h} h" if h else f"{m} min")
