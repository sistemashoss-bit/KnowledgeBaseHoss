"""Límites de uso de MAX (Valkey). Si Valkey no está, no limita (igual que el
resto de rate limits de la app); el tope de tools por invocación sigue."""
from app import valkey_client as vk
from app.config import settings


def busy_key(conv_id) -> str:
    return f"max:busy:{conv_id}"


def acquire_busy(conv_id) -> bool:
    """Una invocación a la vez por chat."""
    return vk.acquire_lock(busy_key(conv_id), settings.assistant_busy_ttl_seconds)


def release_busy(conv_id) -> None:
    vk.release_lock(busy_key(conv_id))


def check(user_id, conv_id) -> tuple[str, int] | None:
    """Cuenta la invocación y devuelve (motivo, segundos de espera) si se pasó
    de algún límite, o None si puede seguir. Orden: ráfaga → diario → grupo
    (si uno bloquea, los siguientes no se cuentan)."""
    checks = [
        ("minute", f"max:user:{user_id}:min", settings.assistant_user_per_minute, 60),
        ("day", f"max:user:{user_id}:day", settings.assistant_user_per_day, 86400),
        ("conversation", f"max:conv:{conv_id}:hour", settings.assistant_conv_per_hour, 3600),
    ]
    for reason, key, limit, window in checks:
        if vk.hit_rate_limit(key, limit, window):
            return reason, vk.ttl(f"rl:{key}") or window
    return None


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
    if reason == "conversation":
        return f"Este grupo llegó al límite de consultas por hora. Intenten de nuevo en unos {minutes} min."
    return "Ya estoy respondiendo en este chat; pregúntame de nuevo en un momento."
