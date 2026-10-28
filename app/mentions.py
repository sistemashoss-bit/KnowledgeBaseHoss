"""Menciones en grupos del chat: `@Nombre` de un participante y `@todos`.

El mensaje se guarda como texto plano (se lee bien en vistas previas y en
notificationservice); quién quedó mencionado se deduce del texto contra los
participantes del grupo, en el servidor, así que no se puede mencionar ni
notificar a alguien de fuera. El autocompletado del compositor solo ayuda a
escribir el nombre exacto.
"""
import re

from markupsafe import Markup, escape

ALL = "todos"

_TAG_RE = re.compile(r"(<[^>]+>)")


def display_name(user) -> str:
    return user.name or user.email


def _pattern(names) -> re.Pattern | None:
    # El más largo primero: "@Ana López" antes que "@Ana".
    alts = sorted({n for n in names if n}, key=len, reverse=True)
    if not alts:
        return None
    return re.compile(
        r"(?<![\w@])@(" + "|".join(re.escape(n) for n in alts) + r")(?!\w)", re.IGNORECASE,
    )


def find(content: str, users) -> tuple[set, bool]:
    """(ids mencionados, si mencionó @todos) en `content`, entre `users`."""
    if not content or "@" not in content:
        return set(), False
    by_name: dict = {}
    for u in users:
        by_name.setdefault(display_name(u).lower(), set()).add(u.id)
    pat = _pattern([*by_name, ALL])
    hits = {m.group(1).lower() for m in pat.finditer(content)}
    ids = {uid for name in hits for uid in by_name.get(name, ())}
    return ids, ALL in hits


def highlight(html, names, me: str = "") -> Markup:
    """Resalta las menciones en el HTML ya escapado (salida de linkify). Solo
    toca el texto: nunca dentro de etiquetas ni del texto de un enlace."""
    if not names or "@" not in str(html):
        return Markup(html)
    pat = _pattern([str(escape(n)) for n in names] + [ALL])
    me_l = str(escape(me)).lower()

    def repl(m: re.Match) -> str:
        name = m.group(1).lower()
        cls = "mention-me" if name == me_l or name == ALL else "mention"
        return f'<span class="{cls}">{m.group(0)}</span>'

    out, in_link = [], False
    for part in _TAG_RE.split(str(html)):
        if part.startswith("<"):
            if part.lower().startswith("<a"):
                in_link = True
            elif part.lower().startswith("</a"):
                in_link = False
            out.append(part)
        else:
            out.append(part if in_link else pat.sub(repl, part))
    return Markup("".join(out))
