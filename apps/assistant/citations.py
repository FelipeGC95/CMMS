"""Citas de las respuestas (AI-02 CA1).

Cada resultado de herramienta (y el contexto del activo) entrega fuentes con un id:
`OT-C-00012` (OT), `MAN:<adjunto>:<página>` (manual) o `MED:<medidor>` (lecturas). El modelo
cita con esos ids entre corchetes. Solo se enlazan las fuentes que el backend entregó en esa
conversación: un marcador inventado se descarta. Un párrafo sin fuente se muestra como
conocimiento general, de menor confianza.
"""

import re
from dataclasses import dataclass, field

MARKER = re.compile(r"\[([^\[\]\n]{2,200})\]")
ID_SHAPE = re.compile(r"^(OT-[A-Z]-\d+|MAN:[0-9a-fA-F-]{8,40}:\d{1,5}|MED:[0-9a-fA-F-]{8,40})$")


class SourceType:
    WORK_ORDER = "work_order"
    MANUAL = "manual"
    METER = "meter"


def _ids(inner: str) -> list[str] | None:
    """Ids de un marcador ("OT-C-1, MAN:x:2"), o None si no parece una cita."""
    parts = [part.strip() for part in re.split(r"[,;]", inner) if part.strip()]
    if not parts or not all(ID_SHAPE.match(part) for part in parts):
        return None
    return parts


def validate_citations(text: str, allowed: dict) -> tuple[str, list[dict]]:
    """Quita los marcadores que no corresponden a una fuente entregada y devuelve las citadas.

    `allowed` es {id: fuente}. El texto conserva los marcadores válidos como `[id]`.
    """
    cited: dict = {}

    def replace(match):
        ids = _ids(match.group(1))
        if ids is None:
            return match.group(0)  # corchetes que no son una cita: se dejan como están
        valid = [source_id for source_id in ids if source_id in allowed]
        for source_id in valid:
            cited.setdefault(source_id, allowed[source_id])
        return " ".join(f"[{source_id}]" for source_id in valid)

    clean = MARKER.sub(replace, text)
    clean = re.sub(r"[ \t]+([.,;:])", r"\1", clean)
    clean = re.sub(r"(?<=\S)[ \t]{2,}", " ", clean)
    return clean.strip(), list(cited.values())


@dataclass
class Segment:
    """Un párrafo de la respuesta con sus fuentes; sin fuentes = conocimiento general."""

    text: str
    sources: list = field(default_factory=list)

    @property
    def is_general(self) -> bool:
        return not self.sources


def segments(text: str, sources: list) -> list[Segment]:
    """Párrafos sin marcadores, cada uno con las fuentes (ya validadas) que cita."""
    by_id = {source["id"]: source for source in sources}
    result = []
    for paragraph in re.split(r"\n\s*\n", text or ""):
        cited: dict = {}

        def collect(match, cited=cited):
            ids = _ids(match.group(1))
            if ids is None:
                return match.group(0)
            for source_id in ids:
                if source_id in by_id:
                    cited.setdefault(source_id, by_id[source_id])
            return ""

        visible = MARKER.sub(collect, paragraph)
        visible = re.sub(r"[ \t]+([.,;:])", r"\1", visible)
        visible = re.sub(r"(?<=\S)[ \t]{2,}", " ", visible).strip()
        if visible:
            result.append(Segment(text=visible, sources=list(cited.values())))
    return result
