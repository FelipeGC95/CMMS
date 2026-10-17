"""Asistente de diagnóstico de la demo (AI-01..05, D3): manuales y conversaciones ya respondidas.

Lo usa `seed_demo`. La demo (Render) no tiene `ANTHROPIC_API_KEY` ni worker, así que nada de
esto llama a la API: las conversaciones se arman con lo mismo que usa un turno real, sin el
modelo. El contexto del activo sale de `tools.asset_context` y las fuentes consultadas, de
ejecutar las herramientas reales (`tools.run_tool`) con la membresía de quien pregunta; las
respuestas son textos fijos cuyas citas se validan con `validate_citations` contra lo que
entregaron el contexto y las herramientas, como en `services.run_turn`. Si una cita no
corresponde a una fuente entregada, el seed falla (no se siembra una cita rota).

Los manuales PDF se generan con reportlab en modo invariante (mismos bytes en cada
arranque): Render no tiene disco y pierde los archivos al dormir, así que `restore_manual_files`
los vuelve a escribir en la misma ruta cuando faltan.

Tokens y modelo de ejemplo plausibles (Opus 5.5) para que la vista de costo del admin tenga datos.
"""

import datetime
import io

from django.core.files.base import ContentFile
from django.core.management.base import CommandError

from apps.assets.models import Meter, MeterKind
from apps.assistant import llm, tools
from apps.assistant.citations import MARKER, _ids, validate_citations
from apps.assistant.models import AssistantConversation, AssistantMessage
from apps.attachments.models import Attachment, Purpose, TargetType
from apps.attachments.services import add_attachment, extract_document_text
from apps.work.models import FailureEvent

MODEL = "claude-opus-5-5"
Role, Kind, Status = AssistantMessage.Role, AssistantMessage.Kind, AssistantMessage.Status

# Manuales: archivo, (tipo de destino, código del activo o de la clase), páginas (líneas).
MANUALS = [
    (
        "Manual Ulma VTI 250.pdf",
        (TargetType.ASSET, "ENV-01"),
        [
            [
                "Ulma VTI 250 - Envasadora vertical",
                "Manual de operación y mantenimiento (extracto)",
                "1. Seguridad  2. Sellado vertical  3. Arrastre de film  4. Lubricación",
            ],
            [
                "1. Seguridad",
                "Antes de intervenir: bloqueo y etiquetado de la energía eléctrica y neumática.",
                "Purgue el aire del circuito y espere que las mordazas se enfríen bajo 40 °C.",
                "No opere la máquina con las puertas de protección abiertas.",
            ],
            [
                "2. Sellado vertical",
                "La mordaza vertical trabaja entre 140 y 170 °C según el film.",
                "Si la bolsa sale abierta o con el sello débil, revise en este orden:",
                "a) temperatura real de la mordaza (termocupla y resistencia de mordaza);",
                "b) estado del teflón de la mordaza: reemplácelo si tiene quemaduras o cortes;",
                "c) presión de cierre de la mordaza: 4 a 5 bar;",
                "d) tiempo de sellado según la velocidad de la línea.",
            ],
            [
                "3. Arrastre de film",
                "Los motores de arrastre mueven las correas de tracción del film.",
                "Corriente nominal del motor de arrastre: 10 a 12 A.",
                "Ruido o vibración: revise la lubricación del reductor y los rodamientos.",
            ],
        ],
    ),
    (
        "Manual Atlas Copco GA22.pdf",
        (TargetType.ASSET, "CMP-01"),
        [
            [
                "Atlas Copco GA22 - Compresor de tornillo",
                "Manual de instrucciones (extracto)",
                "1. Alarmas y paradas  2. Mantenimiento preventivo",
            ],
            [
                "1. Alarmas y paradas",
                "Parada por alta temperatura de salida del elemento (sobre 110 °C).",
                "Causas posibles: nivel de aceite bajo; enfriador de aceite sucio u obstruido;",
                "válvula termostática dañada; ventilador de enfriamiento detenido;",
                "temperatura ambiente de la sala sobre 40 °C o mala ventilación.",
                "Temperatura de descarga normal en operación: 70 a 95 °C.",
            ],
            [
                "2. Mantenimiento preventivo",
                "Cada 2.000 horas: limpiar los enfriadores de aceite y de aire con aire",
                "a baja presión, en sentido contrario al flujo de enfriamiento.",
                "Cada 4.000 horas: cambiar aceite, filtro de aceite y separador.",
            ],
        ],
    ),
    (
        "Manual Bitzer LH84E unidad condensadora.pdf",
        (TargetType.ASSET_CLASS, "FRI"),
        [
            [
                "Bitzer LH84E - Unidad condensadora",
                "Instrucciones de servicio (extracto)",
                "1. Protecciones  2. Contactor y motor",
            ],
            [
                "1. Protecciones",
                "Presostato de baja: detiene el compresor cuando la presión de succión baja",
                "del ajuste. Ciclos cortos (el compresor parte y se detiene a los pocos segundos)",
                "indican falta de refrigerante por fuga, evaporador con hielo, válvula de",
                "expansión obstruida o presostato mal ajustado.",
                "Burbujas en el visor de líquido con la unidad en marcha: falta refrigerante.",
            ],
            [
                "2. Contactor y motor",
                "Revise los contactos del contactor: picaduras o contactos pegados causan fallas",
                "de partida. Corriente del compresor en marcha: 18 a 24 A.",
            ],
        ],
    ),
]


def manual_pdf(pages: list[list[str]]) -> bytes:
    """PDF con capa de texto, siempre con los mismos bytes (sin fecha ni id aleatorio)."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, invariant=1)
    for lines in pages:
        y = 760
        for number, line in enumerate(lines):
            pdf.setFont("Helvetica-Bold" if number == 0 else "Helvetica", 14 if number == 0 else 11)
            pdf.drawString(56, y, line)
            y -= 26 if number == 0 else 18
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class _Upload(ContentFile):
    content_type = "application/pdf"


def seed_manuals(*, assets, classes, admin) -> dict:
    """Sube los manuales y extrae su texto en el acto (la demo no tiene worker). {archivo: adjunto}."""
    result = {}
    for filename, (target_type, code), pages in MANUALS:
        target = assets[code] if target_type == TargetType.ASSET else classes[code]
        attachment = add_attachment(
            target_type=target_type,
            target_id=target.pk,
            uploaded_file=_Upload(manual_pdf(pages), name=filename),
            by=admin.user,
            purpose=Purpose.MANUAL,
        )
        extract_document_text(attachment=attachment)
        result[filename] = attachment
    return result


def restore_manual_files() -> int:
    """Vuelve a escribir los PDF de la demo que falten en el almacenamiento (Render sin disco)."""
    restored = 0
    by_name = {filename: pages for filename, _target, pages in MANUALS}
    for attachment in Attachment.objects.filter(purpose=Purpose.MANUAL, filename__in=by_name):
        storage, name = attachment.file.storage, attachment.file.name
        if name and not storage.exists(name):
            storage.save(name, ContentFile(manual_pdf(by_name[attachment.filename])))
            restored += 1
    return restored


# --- Conversaciones -----------------------------------------------------------------------------


def _source_id(manual, page) -> str:
    return f"MAN:{manual.pk}:{page}"


def _meter(asset, kind, name="") -> Meter:
    return Meter.objects.get(asset=asset, kind=kind, name=name)


def _failure_order(asset, description):
    return (
        FailureEvent.objects.select_related("work_order").get(asset=asset, description=description).work_order
    )


def _check_markers(text: str, allowed: dict) -> None:
    """Cada marcador de la respuesta debe ser una fuente entregada (si no, el seed está mal)."""
    for match in MARKER.finditer(text):
        for source_id in _ids(match.group(1)) or []:
            if source_id not in allowed:
                raise CommandError(f"Cita sin fuente entregada en la demo del asistente: {source_id}")


def _tokens(*, history_chars: int, new_chars: int, output_chars: int, rounds: int, first: bool) -> dict:
    """Tokens plausibles (~3,5 caracteres por token) de un turno con `rounds` llamadas."""
    prefix = 4600  # instrucciones + herramientas, cacheadas
    history = history_chars * 2 // 7
    new = new_chars * 2 // 7
    return {
        "input_tokens": 40 * rounds + new,
        "output_tokens": output_chars * 2 // 7 + 160 * rounds,
        "cache_read_tokens": (0 if first else prefix + history) + (rounds - 1) * (prefix + history + new),
        "cache_creation_tokens": (prefix if first else 0) + history + new,
    }


class _Conversation:
    """Arma una conversación con los mismos datos que dejaría un turno real (sin llamar a la API)."""

    def __init__(self, *, membership, asset, work_order=None, at, title):
        self.membership = membership
        self.at = at
        self.conversation = AssistantConversation.objects.create(
            asset=asset, work_order=work_order, started_by=membership.user, title=title, model=MODEL
        )
        self.seq = 0
        self.allowed: dict = {}
        self.history_chars = 0
        self.tool_counter = 0
        self.messages = []

    def turn(self, question: str, tool_calls: list, answer: str, refs: dict, minutes: int = 0):
        """Pregunta + respuesta. `tool_calls`: [(herramienta, entrada)] de la ronda de búsqueda."""
        first = self.seq == 0
        when = self.at + datetime.timedelta(minutes=minutes)
        content = []
        context_sources = []
        if first:
            context, context_sources = tools.asset_context(
                membership=self.membership,
                asset=self.conversation.asset,
                work_order=self.conversation.work_order,
            )
            content.append({"type": "text", "text": context, "cache_control": llm.EPHEMERAL})
        content.append({"type": "text", "text": question})
        self.seq += 1
        user = AssistantMessage.objects.create(
            conversation=self.conversation,
            role=Role.USER,
            seq=self.seq,
            text=question,
            content=content,
            sources=context_sources,
            completed_at=when,
        )
        self.messages.append((user, when))
        for source in context_sources:
            self.allowed.setdefault(source["id"], source)
        new_chars = sum(len(block["text"]) for block in content)

        turn_content, tool_sources = [], {}
        if tool_calls:
            uses, results = [], []
            for name, tool_input in tool_calls:
                self.tool_counter += 1
                tool_id = f"toolu_demo_{self.seq:02d}_{self.tool_counter:02d}"
                uses.append({"type": "tool_use", "id": tool_id, "name": name, "input": tool_input})
                outcome = tools.run_tool(
                    name=name,
                    tool_input=tool_input,
                    membership=self.membership,
                    conversation=self.conversation,
                )
                results.append({"type": "tool_result", "tool_use_id": tool_id, "content": outcome.content})
                new_chars += len(outcome.content)
                for source in outcome.sources:
                    tool_sources.setdefault(source["id"], source)
            turn_content += [{"role": "assistant", "content": uses}, {"role": "user", "content": results}]
        self.allowed.update(tool_sources)
        raw = answer.format(**refs)
        _check_markers(raw, self.allowed)
        text, cited = validate_citations(raw, self.allowed)
        turn_content.append({"role": "assistant", "content": [{"type": "text", "text": raw}]})

        usage = _tokens(
            history_chars=self.history_chars,
            new_chars=new_chars,
            output_chars=len(raw),
            rounds=2 if tool_calls else 1,
            first=first,
        )
        self.history_chars += new_chars + len(raw)
        self.seq += 1
        answered = when + datetime.timedelta(seconds=25 if tool_calls else 12)
        reply = AssistantMessage.objects.create(
            conversation=self.conversation,
            role=Role.ASSISTANT,
            seq=self.seq,
            text=text,
            content=turn_content,
            sources=cited,
            tool_sources=list(tool_sources.values()),
            status=Status.DONE,
            model=MODEL,
            tool_calls=len(tool_calls),
            completed_at=answered,
            **usage,
        )
        self.messages.append((reply, answered))

    def summary(self, text: str, minutes: int):
        when = self.at + datetime.timedelta(minutes=minutes)
        self.seq += 1
        message = AssistantMessage.objects.create(
            conversation=self.conversation,
            role=Role.ASSISTANT,
            kind=Kind.SUMMARY,
            seq=self.seq,
            text=text,
            status=Status.DONE,
            model=MODEL,
            completed_at=when,
            **_tokens(
                history_chars=self.history_chars, new_chars=400, output_chars=len(text), rounds=1, first=False
            ),
        )
        self.messages.append((message, when))
        self.conversation.summary = text
        self.conversation.summary_status = AssistantConversation.SummaryStatus.DONE

    def finish(self, *, helpful="", closed=False):
        conversation = self.conversation
        last = self.messages[-1][1]
        if helpful:
            conversation.helpful = helpful
            conversation.helpful_marked_at = last + datetime.timedelta(minutes=2)
        if closed:
            conversation.status = AssistantConversation.Status.CLOSED
        conversation.save()
        # Fechas en el pasado (solo datos demo): la lista y el costo por mes las muestran.
        AssistantConversation.objects.filter(pk=conversation.pk).update(created_at=self.at, updated_at=last)
        for message, when in self.messages:
            AssistantMessage.objects.filter(pk=message.pk).update(created_at=when, updated_at=when)
        return conversation


def seed(*, assets, classes, members, now, open_work_order):
    """Manuales + 4 conversaciones: cerrada que sirvió, cerrada en parte, abierta con nota de cierre
    propuesta sobre la OT correctiva abierta, y una abierta que dice "no lo sé"."""
    manuals = seed_manuals(assets=assets, classes=classes, admin=members["admin"])
    tech, tech2 = members["tech"], members["tech2"]
    ulma = manuals["Manual Ulma VTI 250.pdf"]
    atlas = manuals["Manual Atlas Copco GA22.pdf"]
    bitzer = manuals["Manual Bitzer LH84E unidad condensadora.pdf"]

    # 1. Envasadora 1: bolsas abiertas. Cerrada, "sí sirvió".
    env = assets["ENV-01"]
    sealing = _meter(env, MeterKind.TEMPERATURE, "Mordaza vertical")
    refs = {
        "ot": _failure_order(env, "Mordaza vertical no sella, bolsas abiertas").code,
        "med": f"MED:{sealing.pk}",
        "man": _source_id(ulma, 3),
        "temp": f"{sealing.current_value:.0f}",
    }
    chat = _Conversation(
        membership=tech,
        asset=env,
        at=now - datetime.timedelta(days=3, hours=5),
        title="Bolsas salen abiertas en el sello vertical",
    )
    chat.turn(
        "Bolsas salen abiertas en el sello vertical. Pasa en 1 de cada 10 bolsas más o menos. "
        "¿Qué reviso primero?",
        [
            ("search_work_orders", {"query": "mordaza sella bolsas abiertas", "asset_only": True}),
            ("recent_meter_readings", {"asset_code": ""}),
            ("search_manuals", {"query": "mordaza sellado temperatura"}),
        ],
        "Esto ya pasó en esta envasadora: hace unas semanas la mordaza vertical no sellaba y las "
        "bolsas salían abiertas, y se atendió como desgaste de la mordaza [{ot}].\n\n"
        "La temperatura de la mordaza vertical está en {temp} °C [{med}], dentro del rango de 140 a "
        "170 °C del manual [{man}]. Como falla solo una de cada diez bolsas, apunta más a la "
        "mordaza que a la temperatura.\n\n"
        "Revisa en este orden [{man}]:\n"
        "1. Estado del teflón de la mordaza: si tiene quemaduras o cortes, cámbialo.\n"
        "2. Presión de cierre: debe estar entre 4 y 5 bar.\n"
        "3. Tiempo de sellado, si subieron la velocidad de la línea.\n\n"
        "Antes de tocar las mordazas, bloquea y etiqueta la energía eléctrica y neumática, y "
        "espera que se enfríen.",
        refs,
    )
    chat.turn(
        "El teflón tenía un corte justo donde falla el sello. Lo cambié y probé 200 bolsas sin problemas.",
        [],
        "Bien: el corte en el teflón explica que fallara solo una parte de las bolsas. Deja "
        "registrado el cambio en la OT y revisa el teflón en la próxima mantención quincenal.\n\n"
        "Si vuelve a pasar con teflón nuevo, mide la presión de cierre de la mordaza [{man}].",
        refs,
        minutes=48,
    )
    chat.finish(helpful=AssistantConversation.Helpful.YES, closed=True)

    # 2. Compresor 1: parada por alta temperatura, asociada a la OT de esa falla. Cerrada, "en parte".
    cmp_ = assets["CMP-01"]
    discharge = _meter(cmp_, MeterKind.TEMPERATURE, "Descarga")
    stop_order = _failure_order(cmp_, "Compresor se detiene por alta temperatura")
    refs = {
        "leak": _failure_order(cmp_, "Fuga de aceite en enfriador del compresor").code,
        "med": f"MED:{discharge.pk}",
        "alarms": _source_id(atlas, 2),
        "service": _source_id(atlas, 3),
    }
    chat = _Conversation(
        membership=tech,
        asset=cmp_,
        work_order=stop_order,
        at=stop_order.created_at + datetime.timedelta(minutes=40),
        title="Se detuvo por alta temperatura y volvió a parar a la hora",
    )
    chat.turn(
        "Se detuvo por alta temperatura. Lo dejé enfriar y partió, pero a la hora volvió a parar.",
        [
            ("asset_failure_history", {"asset_code": ""}),
            ("recent_meter_readings", {"asset_code": ""}),
            ("search_manuals", {"query": "temperatura enfriador aceite"}),
        ],
        "Hace unos dos meses este compresor tuvo una fuga de aceite en el enfriador [{leak}]. Si quedó "
        "con poco aceite o el enfriador se ensució, la temperatura sube hasta la parada.\n\n"
        "El manual da estas causas para la parada por alta temperatura: nivel de aceite bajo, "
        "enfriador sucio u obstruido, válvula termostática dañada, ventilador detenido o sala sobre "
        "40 °C [{alarms}]. Las lecturas de temperatura de descarga venían subiendo [{med}].\n\n"
        "Con el equipo detenido, bloqueado y despresurizado:\n"
        "1. Revisa el nivel de aceite en el visor.\n"
        "2. Mira el enfriador por el lado del ventilador: polvo, pelusa o aceite pegado.\n"
        "3. Comprueba que el ventilador gire al partir.",
        refs,
    )
    chat.turn(
        "El aceite está bien. El enfriador está tapado con polvo y pelusa.",
        [("search_manuals", {"query": "limpiar enfriadores"})],
        "Entonces lo más probable es el enfriador sucio [{alarms}]. Límpialo con aire a baja "
        "presión, en sentido contrario al flujo de enfriamiento [{service}].\n\n"
        "No uses agua a presión: dobla las aletas y empeora el problema. Después de limpiar, "
        "deja el compresor en carga una hora y anota la temperatura de descarga.",
        refs,
        minutes=35,
    )
    chat.summary(
        "Síntoma: parada por alta temperatura; partía tras enfriar y volvía a parar a la hora.\n"
        "Causa: enfriador de aceite tapado con polvo y pelusa; nivel de aceite correcto.\n"
        "Acción: limpieza del enfriador con aire a baja presión en contra del flujo; prueba en "
        "carga con temperatura de descarga normal.",
        minutes=70,
    )
    chat.finish(helpful=AssistantConversation.Helpful.PARTIAL, closed=True)

    # 3. Cámara 1: la correctiva abierta de la demo. Abierta, con nota de cierre propuesta que
    # aparece al completar la OT (la completa el técnico que preguntó).
    fri = assets["FRI-01"]
    camera = _meter(fri, MeterKind.TEMPERATURE, "Cámara")
    refs = {
        "ot": open_work_order.code,
        "contactor": _failure_order(fri, "Cámara 1 sin frío por falla de contactor").code,
        "med": f"MED:{camera.pk}",
        "protections": _source_id(bitzer, 2),
        "motor": _source_id(bitzer, 3),
    }
    chat = _Conversation(
        membership=tech,
        asset=fri,
        work_order=open_work_order,
        at=now - datetime.timedelta(minutes=50),
        title="Compresor parte y se corta a los pocos segundos",
    )
    chat.turn(
        "Compresor parte y se corta a los pocos segundos. La cámara está en -8 °C. Ya revisé que el "
        "ventilador del condensador gira.",
        [
            ("asset_failure_history", {"asset_code": ""}),
            ("search_manuals", {"query": "presostato compresor segundos"}),
            ("search_manuals", {"query": "contactor partida"}),
            ("recent_meter_readings", {"asset_code": ""}),
        ],
        "Este equipo quedó sin frío hace un par de meses por una falla del contactor [{contactor}]. "
        "Pero si el compresor alcanza a partir, el contactor está cerrando: revisa sus contactos de "
        "todos modos, por picaduras [{motor}].\n\n"
        "Partir y cortar a los pocos segundos son ciclos cortos. El manual los asocia al presostato "
        "de baja: falta de refrigerante por fuga, evaporador con hielo, válvula de expansión "
        "obstruida o presostato mal ajustado [{protections}].\n\n"
        "La temperatura de la cámara venía estable en las lecturas registradas [{med}], así que el "
        "problema es reciente.\n\n"
        "Para seguir con la OT [{ot}]:\n"
        "1. Mira el evaporador: ¿tiene hielo?\n"
        "2. Con manómetros, anota la presión de succión justo antes del corte.\n"
        "3. Revisa el visor de líquido con la unidad en marcha.",
        refs,
    )
    chat.turn(
        "Evaporador sin hielo. El presostato de baja corta con 0,8 bar y el visor de líquido tiene burbujas.",
        [],
        "Burbujas en el visor con la unidad en marcha indican falta de refrigerante [{protections}]. "
        "Con eso, el presostato de baja está haciendo su trabajo: no lo ajustes más abajo ni lo "
        "puentees.\n\n"
        "Falta encontrar la fuga: revisa con detector las uniones, la válvula de expansión y el "
        "condensador. La carga de refrigerante la debe hacer un técnico de frío certificado.",
        refs,
        minutes=22,
    )
    chat.summary(
        "Síntoma: cámara 1 a -8 °C; el compresor parte y se corta a los pocos segundos.\n"
        "Causa probable: falta de refrigerante por fuga; el presostato de baja corta a 0,8 bar y "
        "hay burbujas en el visor de líquido.\n"
        "Acción: búsqueda de fugas con detector, reparación y carga de refrigerante por técnico de "
        "frío; verificar que la cámara baje de -18 °C.",
        minutes=30,
    )
    chat.finish()

    # 4. Grúa 2: sin manual del modelo. El asistente dice qué falta (AI-02 CA2). Abierta, otro técnico.
    grh = assets["GRH-02"]
    leak_order = _failure_order(grh, "Fuga de aceite hidráulico en cilindro de elevación")
    hours = _meter(grh, MeterKind.HOURS)
    refs = {"ot": leak_order.code, "med": f"MED:{hours.pk}"}
    chat = _Conversation(
        membership=tech2,
        asset=grh,
        work_order=leak_order,
        at=leak_order.created_at + datetime.timedelta(minutes=25),
        title="Fuga de aceite por el cilindro de elevación: ¿qué kit de sellos lleva?",
    )
    chat.turn(
        "Fuga de aceite por el cilindro de elevación: ¿qué kit de sellos lleva?",
        [
            ("search_manuals", {"query": "kit sellos cilindro elevacion"}),
            ("search_work_orders", {"query": "sellos cilindro elevacion", "asset_only": True}),
            ("recent_meter_readings", {"asset_code": ""}),
        ],
        "No lo sé: no hay manuales del Hyster H3.0FT cargados en el sistema ni otro cambio de sellos "
        "de esta grúa en el historial; la única fuga registrada es la de esta OT [{ot}].\n\n"
        "Para saber el kit falta el número de parte del cilindro: está en una placa o grabado en el "
        "cuerpo del cilindro. Con ese número y el horómetro [{med}] lo puede confirmar el "
        "distribuidor. Si subes el manual de partes a la ficha de la grúa, la próxima vez lo busco "
        "ahí.\n\n"
        "Mientras tanto, revisa el vástago: si tiene rayas o picaduras, el sello nuevo se va a "
        "romper igual. Baja la horquilla hasta el piso y alivia la presión antes de soltar "
        "cualquier conexión.",
        refs,
    )
    chat.finish()
