"""Valor diario de la UF (ORG-13 CA3): fuente pública, tabla con fecha y respaldo en el último valor.

La fuente por defecto es mindicador.cl (`UF_SOURCE_URL`), que publica la serie de la UF del Banco
Central. La tarea diaria la consulta y guarda el valor del día; las pantallas y los cobros leen la
tabla (nunca esperan a la red). Si la fuente falla se usa el último valor guardado; si no hay
ninguno, no se cobra y se avisa.
"""

import datetime
import logging
import zoneinfo
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from . import http
from .models import UfValue

logger = logging.getLogger(__name__)

CHILE = zoneinfo.ZoneInfo("America/Santiago")


class UfSourceError(Exception):
    pass


def chile_today(now=None) -> datetime.date:
    return (now or timezone.now()).astimezone(CHILE).date()


def fetch_uf() -> tuple[datetime.date, Decimal]:
    """(fecha, valor) más reciente de la fuente. Formato mindicador: {"serie": [{"fecha", "valor"}]}."""
    try:
        status, data = http.request_json("GET", settings.UF_SOURCE_URL)
    except http.HttpError as error:
        raise UfSourceError(str(error)) from error
    if status != 200 or not isinstance(data, dict):
        raise UfSourceError(f"La fuente de la UF respondió {status}")
    serie = data.get("serie")
    if not isinstance(serie, list) or not serie or not isinstance(serie[0], dict):
        raise UfSourceError("La fuente de la UF no trae la serie esperada")
    try:
        moment = datetime.datetime.fromisoformat(str(serie[0]["fecha"]).replace("Z", "+00:00"))
        value = Decimal(str(serie[0]["valor"])).quantize(Decimal("0.01"))
    except (KeyError, ValueError, InvalidOperation) as error:
        raise UfSourceError("La fuente de la UF trae un valor ilegible") from error
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.UTC)
    if not Decimal("1000") < value < Decimal("1000000"):
        raise UfSourceError(f"Valor de la UF fuera de rango: {value}")
    return moment.astimezone(CHILE).date(), value


def refresh_uf_value() -> UfValue | None:
    """Consulta la fuente y guarda el valor del día. Si falla, lo registra y devuelve None."""
    try:
        date, value = fetch_uf()
    except UfSourceError as error:
        logger.warning("No se pudo obtener la UF: %s", error)
        return None
    row, _created = UfValue.objects.update_or_create(
        date=date, defaults={"value": value, "source": settings.UF_SOURCE_URL[:100]}
    )
    return row


def current_uf_value(*, today=None) -> UfValue | None:
    """Valor de hoy o, si no está, el último guardado antes (respaldo cuando la fuente falla)."""
    today = today or chile_today()
    return UfValue.objects.filter(date__lte=today).order_by("-date").first()


def uf_for_charge(*, today=None) -> UfValue | None:
    """Para cobrar: el de la tabla; si la tabla está vacía, una consulta a la fuente."""
    return current_uf_value(today=today) or refresh_uf_value()
