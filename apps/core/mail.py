"""Envío de correo por la cola (EMAIL_USE_QUEUE=1, docs/07_operacion.md).

`QueuedEmailBackend` reemplaza a EMAIL_BACKEND sin cambiar a quienes llaman
(`send_mail`, allauth): serializa cada mensaje y el worker lo envía con
EMAIL_DELIVERY_BACKEND. Los mensajes con adjuntos se envían en el momento.
"""

from django.conf import settings
from django.core.mail import EmailMessage, EmailMultiAlternatives, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.db import transaction


def serialize_message(message: EmailMessage) -> dict:
    return {
        "subject": str(message.subject),
        "body": str(message.body),
        "from_email": message.from_email,
        "to": list(message.to),
        "cc": list(message.cc),
        "bcc": list(message.bcc),
        "reply_to": list(message.reply_to),
        "headers": dict(message.extra_headers),
        "content_subtype": message.content_subtype,
        "alternatives": [
            [str(content), mimetype] for content, mimetype in getattr(message, "alternatives", [])
        ],
    }


def deserialize_message(data: dict) -> EmailMultiAlternatives:
    message = EmailMultiAlternatives(
        subject=data["subject"],
        body=data["body"],
        from_email=data["from_email"],
        to=data["to"],
        cc=data["cc"],
        bcc=data["bcc"],
        reply_to=data["reply_to"],
        headers=data["headers"],
    )
    message.content_subtype = data.get("content_subtype", "plain")
    for content, mimetype in data["alternatives"]:
        message.attach_alternative(content, mimetype)
    return message


def delivery_connection():
    return get_connection(backend=getattr(settings, "EMAIL_DELIVERY_BACKEND", None))


def deliver_serialized(data: dict) -> int:
    """Envía un mensaje serializado con el backend real del proveedor."""
    return delivery_connection().send_messages([deserialize_message(data)]) or 0


class QueuedEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        from apps.core.tasks import send_email

        direct = [message for message in email_messages if message.attachments]
        for message in email_messages:
            if message.attachments:
                continue
            data = serialize_message(message)
            # Si la petición falla y se revierte, el correo no sale.
            transaction.on_commit(lambda data=data: send_email.defer(message=data))
        sent = len(email_messages) - len(direct)
        if direct:
            sent += delivery_connection().send_messages(direct) or 0
        return sent
