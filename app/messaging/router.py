"""Router de /messaging. Las rutas viven en app/messaging/routes/ y se incluyen
en el mismo orden en que estaban en este archivo: importa porque /{conv_id}
captura cualquier segmento y las rutas literales (/signals, /stickers/...)
tienen que registrarse antes."""
from fastapi import APIRouter

from app.messaging.helpers import _after_new_message, _render_message
from app.messaging.routes import views, messages, live, sending, conversations

# _after_new_message y _render_message: los usa app/assistant desde aquí.
__all__ = ["router", "_after_new_message", "_render_message"]

router = APIRouter(prefix="/messaging", tags=["messaging"])
for _module in (views, messages, live, sending, conversations):
    router.include_router(_module.router)
