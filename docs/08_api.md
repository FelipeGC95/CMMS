# 08 · Guía de la API v1

Guía funcional de la API REST: qué resuelve cada endpoint, sus reglas y
ejemplos de uso. La decisión técnica está en ADR-005
(`docs/04_arquitectura.md`); el contrato exacto, en `/api/schema/`.
Actualizada al 2026-10-11 (inventario, INV-17).

## Resumen

La API v1 permite que otro sistema haga, sin pasar por la pantalla, lo mismo que un planificador o un técnico hacen en la web: consultar y dar de alta activos, cargar lecturas de medidores, crear y mover órdenes de trabajo, responder su checklist con fotos y cerrarlas, reportar fallas y, con el módulo de inventario, mantener el catálogo de repuestos, registrar entradas, conteos y transferencias, y consumir repuestos en una OT.

Resuelve tres necesidades concretas:

- **App móvil o cliente sin conexión:** el técnico ejecuta la OT completa (iniciar, checklist, fotos, cierre) y los reintentos no duplican nada gracias a `client_uuid`.
- **Integración con otros sistemas (ERP, BI, planillas):** leer el catálogo de activos y el historial de OT, o crear OT y avisos desde otro sistema.
- **Telemetría y medidores:** un PLC, SCADA o script envía horómetros y contadores, y los preventivos por uso se disparan solos.

Toda la API usa los mismos servicios que la web: las reglas, permisos, auditoría y aislamiento entre organizaciones son idénticos. No hay una segunda versión de las reglas que pueda desalinearse.

## Conceptos base

Toda petición lleva un token de organización; el token decide de qué empresa son los datos, qué puede hacer y cuántas peticiones por minuto acepta.

### Token y autenticación

1. Un administrador entra a **Más → Tokens de API** (`/settings/api-tokens/`) y crea un token con nombre (p. ej. «ERP»), alcance y vencimiento (nunca, 30 días, 90 días o 1 año).
2. El token completo (`gbt_…`) se muestra **una sola vez**; la base de datos guarda solo su hash y un prefijo visible para reconocerlo.
3. Cada petición lo envía en el encabezado:

```http
Authorization: Bearer gbt_1a2b3c4d_XXXXXXXXXXXXXXXXXXXX
```

No hay sesión ni cookies, así que no aplica CSRF. Un token revocado, vencido, inexistente o de una organización inactiva responde **401**.

### Qué puede hacer un token

| Aspecto | Comportamiento |
| --- | --- |
| Organización | La del token; no se elige ni se envía. Un id de otra empresa responde igual que uno inexistente (404 o error de validación). |
| Rol efectivo | Planificador sobre toda la organización (sin restricción por sitio). |
| Alcance `read` | Solo `GET`. Cualquier escritura → 403. |
| Alcance `write` | Lectura y escritura: activos, lecturas, OT, checklist, fotos, solicitudes. |
| Nunca puede | Administrar equipo, auditoría, configuración ni otros tokens. |
| Autoría | Cada token tiene su propio usuario de servicio (`API · <nombre>`): la auditoría muestra qué integración hizo cada cambio. |
| Zona horaria | La de la organización del token. |
| Límite de tasa | `600/min` por token por defecto (variable `API_RATE_LIMIT`); al superarlo → 429. |

### Paginación

Toda lista es paginada: `?page=2&page_size=100` (50 por defecto, máximo 200). La respuesta trae `count`, `next`, `previous` y `results`.

```json
{
  "count": 134,
  "next": "https://…/api/v1/assets/?page=2",
  "previous": null,
  "results": [ … ]
}
```

### Idempotencia con `client_uuid`

Un cliente móvil puede perder la señal justo después de enviar. Para que el reintento no duplique, las altas aceptan un `client_uuid` que genera el propio cliente:

- **Lecturas, OT, solicitudes, fotos:** reenviar el mismo `client_uuid` devuelve el registro ya creado, sin crear otro.
- **Cierre de OT:** reenviar la misma clave devuelve la OT actual (200); usar esa clave en otra OT → **409 conflict**.
- **Fotos:** misma clave en otro objeto → **409**.
- **Transiciones:** repetir el mismo estado destino no hace nada; con `client_uuid`, reenviar el cambio devuelve la OT actual aunque ya haya avanzado (en otra OT → **409**).
- **Respuesta de checklist:** repetir la misma respuesta no hace nada.
- **Carga masiva de lecturas:** idempotente por (medidor, fecha, valor), sin `client_uuid`.

### Errores

Todos los errores tienen la misma forma, con textos en español:

```json
{
  "error": {
    "code": "invalid",
    "message": "Revisa los datos enviados.",
    "details": { "labor_minutes": ["Este campo es requerido."] }
  }
}
```

| HTTP | `code` | Cuándo |
| --- | --- | --- |
| 400 | `invalid` | Datos inválidos o regla de negocio incumplida (transición no permitida, ítem obligatorio sin responder). |
| 401 | `not_authenticated` / `authentication_failed` | Sin token, token mal formado, revocado o vencido. |
| 403 | `permission_denied` | Token `read` intentando escribir, o acción fuera del rol. |
| 404 | `not_found` | No existe o es de otra organización. |
| 409 | `conflict` | `client_uuid` ya usado en otra operación. (La API de terreno además usa `cancelled`, `locked`, `organization_changed` y `upgrade_required`; ver más abajo.) |
| 429 | `throttled` | Límite de peticiones por minuto superado. |

### Documentación interactiva

- `/api/docs/`: Swagger UI para probar cada endpoint con un token.
- `/api/schema/`: esquema OpenAPI 3 para generar clientes (Postman, openapi-generator).

## Mapa de endpoints

Son 9 recursos bajo `/api/v1/` con 36 operaciones; las escrituras exigen un token `write`. Los 4 de inventario (repuestos, bodegas, stock y movimientos) exigen además el módulo `inventory` del plan.

| Recurso | Método y ruta | Qué resuelve |
| --- | --- | --- |
| Activos | `GET /assets/` | Listar el catálogo, filtrar por código, texto, ubicación, clase o estado. |
| Activos | `GET /assets/{id}/` | Ficha de un activo. |
| Activos | `POST /assets/` | Dar de alta un activo desde otro sistema. |
| Activos | `PATCH /assets/{id}/` | Actualizar datos de un activo (solo los campos enviados). |
| Medidores | `GET /meters/` | Saber qué medidores tiene un activo y su valor actual. |
| Medidores | `GET /meters/{id}/` | Detalle de un medidor (uso diario promedio incluido). |
| Lecturas | `GET /readings/` | Historial de lecturas por medidor o activo. |
| Lecturas | `GET /readings/{id}/` | Una lectura. |
| Lecturas | `POST /readings/` | Registrar una lectura (dispara preventivos por uso). |
| Lecturas | `POST /readings/bulk/` | Cargar hasta 2.000 lecturas en un envío. |
| OT | `GET /work-orders/` | Listar OT por alcance, estado, tipo, activo o ubicación. |
| OT | `GET /work-orders/{id}/` | Detalle de una OT. |
| OT | `POST /work-orders/` | Crear una OT (preventiva, correctiva, mejora, inspección…). |
| OT | `POST /work-orders/{id}/transition/` | Cambiar el estado (planificar, iniciar, pausar, cerrar, cancelar). |
| OT | `POST /work-orders/{id}/complete/` | Completar la OT con horas, notas, falla, lectura y firma. |
| Checklist | `GET /work-orders/{id}/checklist-items/` | Ver los ítems a responder, con cuántas fotos tiene cada uno. |
| Checklist | `POST /work-orders/{id}/checklist-items/{item_id}/answer/` | Responder un ítem (OK/No OK/N.A., medición, texto, firma). |
| Fotos | `POST /work-orders/{id}/photos/` | Subir una foto a la OT. |
| Fotos | `POST /work-orders/{id}/checklist-items/{item_id}/photos/` | Subir una foto a un ítem del checklist. |
| Adjuntos | `GET /work-orders/{id}/attachments/` | Listar los adjuntos de la OT y sus ítems (solo metadatos). |
| Repuestos de la OT | `GET /work-orders/{id}/parts/` | Repuestos y materiales usados en la OT. |
| Repuestos de la OT | `POST /work-orders/{id}/parts/` | Registrar un repuesto usado: del catálogo (descuenta stock y reserva) o texto libre. |
| Repuestos | `GET /parts/` | Catálogo de repuestos: filtrar por código, texto o categoría. |
| Repuestos | `GET /parts/{id}/` | Ficha de un repuesto (costo promedio con permiso de costos). |
| Repuestos | `POST /parts/` | Alta idempotente por código: si ya existe, devuelve el existente. |
| Repuestos | `PATCH /parts/{id}/` | Editar datos del catálogo o desactivar (`is_active: false`). |
| Bodegas | `GET /warehouses/` y `GET /warehouses/{id}/` | Bodegas visibles (la por defecto primero). |
| Stock | `GET /stock/` y `GET /stock/{id}/` | Saldo por repuesto y bodega: existencia, reservado, disponible, mínimo; filtro `below_min`. |
| Movimientos | `GET /stock-movements/` y `GET /stock-movements/{id}/` | Libro de movimientos por repuesto, bodega, OT o tipo. |
| Movimientos | `POST /stock-movements/` | Entrada (`receipt`), conteo (`count`) o transferencia (`transfer`). |
| Solicitudes | `GET /requests/` | Listar avisos de falla por estado. |
| Solicitudes | `GET /requests/{id}/` | Detalle de un aviso y su OT. |
| Solicitudes | `POST /requests/` | Reportar una falla: crea el aviso y su OT correctiva. |

## Guía por funcionalidad

Cada bloque explica qué problema resuelve, las reglas que aplica y un ejemplo real de petición y respuesta. En los ejemplos, `$T` es el token y `$API` es `https://galbust-cmms.onrender.com/api/v1`.

### 1. Activos: el catálogo de equipos

**Qué resuelve:** mantener sincronizado el inventario de equipos con un ERP o planilla, y que otras integraciones encuentren un activo por su código sin conocer su id interno.

**Filtros de la lista:** `code` (exacto, sin distinguir mayúsculas), `search` (texto en código o nombre), `location` (incluye sub-ubicaciones), `asset_class`, `status` (`operational`, `stopped`, `in_maintenance`). Los activos dados de baja no aparecen.

**Ejemplo: buscar la envasadora por código.**

```bash
curl -H "Authorization: Bearer $T" "$API/assets/?code=ENV-01"
```

```json
{
  "count": 1,
  "results": [{
    "id": "0192f1a4-…",
    "code": "ENV-01",
    "name": "Envasadora línea 1",
    "asset_class_code": "ENV",
    "location_code": "PLANTA-1",
    "status": "operational",
    "criticality": "A",
    "manufacturer": "Krones",
    "attributes": { "potencia_kw": 15 }
  }]
}
```

**Alta y edición:** `POST /assets/` exige `code`, `name` y `asset_class` (id); opcionales `location`, `parent` (activo padre), `criticality` (`A`/`B`/`C`), fabricante, modelo, serie, año, fecha de puesta en servicio y `attributes` (atributos propios de la clase). `PATCH` cambia solo los campos enviados. El código es único por organización.

### 2. Medidores y lecturas: mantenimiento por uso

**Qué resuelve:** que el horómetro, cuentakilómetros o contador de ciclos llegue solo al CMMS y que los planes preventivos por uso («cada 500 horas») generen su OT cuando corresponde, sin que nadie digite lecturas.

**Reglas (las mismas que la web):**

- La lectura no puede ser negativa.
- En un medidor acumulativo el valor no puede bajar, salvo que se marque `is_reset: true` (cambio de horómetro).
- Si falta `read_at`, se usa la hora actual.
- Registrar la lectura recalcula el uso diario promedio y dispara los preventivos por uso que vencen.
- Las lecturas llegadas por la API quedan con `source: "api"`.

**Ejemplo: enviar una lectura del horómetro.**

```bash
curl -X POST "$API/readings/" \
  -H "Authorization: Bearer $T" -H "Content-Type: application/json" \
  -d '{"meter": "0192f1b0-…", "value": "12480.5",
       "read_at": "2026-09-29T08:00:00-03:00",
       "client_uuid": "7b1e…"}'
```

**Carga masiva (`POST /readings/bulk/`):** hasta 2.000 filas por envío, con las mismas reglas que el CSV de la web. Cada fila identifica el medidor por `meter` (id) o por `asset_code` si el activo tiene un solo medidor. Acepta fechas `aaaa-mm-dd hh:mm` o `dd-mm-aaaa hh:mm`. Las filas válidas se guardan y las inválidas se informan una a una, sin detener el lote.

```json
// petición
{ "rows": [
  { "asset_code": "ENV-01", "value": "12480", "read_at": "2026-09-29 08:00" },
  { "asset_code": "COMP-02", "value": "-5", "read_at": "2026-09-29 08:00" }
]}

// respuesta
{ "created": 1, "duplicates": 0, "errors": 1,
  "rows": [
    { "row": 1, "asset_code": "ENV-01", "result": "create", "errors": [] },
    { "row": 2, "asset_code": "COMP-02", "result": "error",
      "errors": ["La lectura no puede ser negativa."] }
  ]}
```

Reenviar el mismo lote no duplica: las filas ya cargadas vuelven como duplicadas.

### 3. Órdenes de trabajo: crear, listar y mover

**Qué resuelve:** que otro sistema cree trabajo (p. ej. una alarma de SCADA crea una OT predictiva) y que un tablero externo consulte las OT pendientes o atrasadas.

**Filtros de la lista:** `scope` (`open`, `overdue`, `done`, `all`), `status`, `work_type`, `asset`, `location`.

**Crear:** `work_type` (`preventive`, `corrective`, `predictive`, `improvement`, `inspection`), `title` y **exactamente uno** entre `asset` y `location`. Opcionales: `description`, `priority` (1 urgente, 2 alta, 3 normal por defecto, 4 baja), `due_on`, `estimated_minutes`, `client_uuid`. El correlativo visible (`code`, p. ej. `OT-D-00042`) lo asigna el sistema.

```bash
curl -X POST "$API/work-orders/" -H "Authorization: Bearer $T" \
  -H "Content-Type: application/json" \
  -d '{"work_type": "predictive", "title": "Vibración alta en rodamiento",
       "asset": "0192f1a4-…", "priority": 2, "client_uuid": "c3d9…"}'
```

**Ciclo de vida (`POST /work-orders/{id}/transition/`):** se envía `to_status` y opcionales `comment`, `client_uuid` (idempotencia) y `at` (hora real del cambio si se hizo sin conexión; ver «Horas informadas por el cliente»). Transiciones válidas:

| Desde | Puede pasar a |
| --- | --- |
| `requested` (solicitada) | `planned`, `scheduled`, `in_progress`, `cancelled` |
| `planned` (planificada) | `scheduled`, `in_progress`, `cancelled` |
| `scheduled` (programada) | `planned`, `in_progress`, `cancelled` |
| `in_progress` (en ejecución) | `on_hold`, `completed`, `cancelled` |
| `on_hold` (en espera) | `in_progress`, `cancelled` |
| `completed` (completada) | `closed`, `in_progress` (reabrir) |
| `closed` / `cancelled` | ninguno (estados finales) |

Repetir el estado actual no hace nada (idempotente). Pasar a `completed` por esta ruta falla si quedan ítems obligatorios sin responder; para cerrar con datos se usa `complete`.

### 4. Checklist: ejecutar la pauta en terreno

**Qué resuelve:** que el técnico responda la pauta de la OT desde un teléfono o tablet, incluso con conexión intermitente.

`GET /work-orders/{id}/checklist-items/` devuelve los ítems en orden, con `item_type`, rango (`min_value`, `max_value`, `unit`), si es obligatorio y `photo_count`. La respuesta depende del tipo:

| Tipo de ítem | Qué se envía a `…/answer/` |
| --- | --- |
| Verificación | `result`: `ok`, `not_ok` o `na` |
| Medición | `value_number` (fuera de rango queda como No OK automáticamente) |
| Texto | `value_text` |
| Firma | `signature` (data URL PNG en base64) |
| Foto obligatoria | Se responde subiendo la foto a `…/checklist-items/{item_id}/photos/`; después `answer` solo guarda la observación. Sin foto → 400. |

Todos aceptan `note` (observación, hasta 500 caracteres). Reenviar la misma respuesta devuelve el ítem sin cambios, también con la OT ya completada.

Con `answered_at` (hora en que se respondió sin conexión) **gana la última**: si el ítem ya tiene una respuesta posterior, la enviada no se aplica, queda en la auditoría como descartada y la respuesta trae `"superseded": true` con el ítem vigente. La respuesta siempre incluye `answered_at` y `superseded`.

Mediciones que alimentan una variable (CBM-06): el ítem trae `meter_kind` y `meter_name` (vacíos si no alimenta ninguna) y `meter_reading` (id de la lectura, `null` hasta completar). Al completar la OT (`…/complete/`, igual en la web y en terreno), cada ítem enlazado con `value_number` registra una lectura de esa variable del activo (origen OT, fecha = `answered_at` o el término), la crea si el activo no la tiene y pasa por las reglas de condición. Una sola vez por ítem: reabrir y completar de nuevo no la repite.

```json
// POST /work-orders/{id}/checklist-items/{item_id}/answer/
{ "value_number": "4.2", "note": "Medido en caliente" }
```

### 5. Fotos y adjuntos: evidencia del trabajo

**Qué resuelve:** dejar evidencia visual del antes y después, o del hallazgo, en la OT o en el ítem exacto del checklist.

- Envío en `multipart/form-data`: campo `file` (JPEG, PNG, WebP o HEIC, hasta 20 MB), `client_uuid` y `taken_at` opcionales. Con `taken_at` anterior al término, la foto se acepta aunque la OT ya esté completada o cerrada (sin cambiar el resultado del ítem); en una OT cancelada, nunca.
- El servidor comprime a JPEG (lado mayor ≤ 2.000 px) y borra los metadatos, incluida la ubicación GPS.
- Solo en OT abiertas y visibles; cuenta contra el cupo de almacenamiento de la organización.
- Responde **201** con los metadatos; reenviar el mismo `client_uuid` devuelve la misma foto con **200**.
- La API **nunca entrega una URL** del archivo: se descarga desde la web, con permisos y URL firmada de corta duración.

```bash
curl -X POST "$API/work-orders/$OT/photos/" -H "Authorization: Bearer $T" \
  -F "file=@rodamiento.jpg" -F "client_uuid=5e0a…"
```

`GET /work-orders/{id}/attachments/` lista los adjuntos de la OT y de sus ítems (nombre, tipo, tamaño, dimensiones, sha256).

### 6. Cierre de OT: registrar lo que se hizo

**Qué resuelve:** cerrar el trabajo con los datos que alimentan los indicadores (MTTR, MTBF, costos, historial de fallas) sin pasar por la pantalla.

`POST /work-orders/{id}/complete/` aplica exactamente el formulario de cierre de la web:

- **Siempre:** `labor_minutes` (0 a 1.440). Opcional `notes` (acción realizada) y `signature`.
- **Si el plan usa medidor:** `meter_value` obligatorio (la lectura queda registrada).
- **Correctiva sobre un activo:** `impact` (`total_stop`, `partial`, `none`), `symptom`, `failure_started_at` y `failure_ended_at`; y si la organización lo exige, `failure_mode` y `failure_cause` (ids del catálogo de fallas). Opcionales `failure_mechanism`, `detection_method` y `component` (activo hijo).
- **Todos los ítems obligatorios** del checklist deben estar respondidos.
- `client_uuid` evita cerrar dos veces; en otra OT → 409.
- Repuestos (módulo `inventory`): completar por la API **no consume** los repuestos previstos (en la web sí se ofrecen pre-marcados); se registran antes, uno a uno. Al cerrar o cancelar la OT, sus reservas pendientes se liberan solas.
- `at` opcional: hora real de término si se completó sin conexión (fija término, cierre, fin de la falla y la fecha que reprograma el plan).

```json
{
  "labor_minutes": 95,
  "notes": "Cambio de rodamiento lado acople",
  "impact": "total_stop",
  "symptom": "Ruido y vibración",
  "failure_started_at": "2026-09-29T07:10:00-03:00",
  "failure_ended_at": "2026-09-29T09:05:00-03:00",
  "failure_mode": "0192e7…",
  "failure_cause": "0192e8…",
  "client_uuid": "a41f…"
}
```

### 7. Solicitudes: reportar una falla

**Qué resuelve:** que un operador, otro sistema o un bot de mensajería avise una falla en un paso; el aviso crea su OT correctiva en estado `requested`, lista para que el planificador la tome.

- `description` y **exactamente uno** entre `asset` y `location`.
- `asset_stopped: true` solo con activo: sube la prioridad sugerida y fija el vencimiento para hoy.
- Prioridad sugerida: activo detenido de criticidad A → urgente; otro activo detenido → alta; resto → normal.
- Idempotente por `client_uuid`.

```json
// POST /requests/
{ "asset": "0192f1a4-…", "description": "Fuga de aceite en reductor",
  "asset_stopped": true, "client_uuid": "e2b7…" }

// respuesta 201
{ "number": 118, "status": "pending", "work_order_code": "OT-C-00213", … }
```

Estados de la solicitud: `pending`, `accepted`, `rejected`, `merged` (fusionada con otra).

### 8. Inventario: catálogo, stock y movimientos (INV-17)

**Qué resuelve:** que el ERP o una planilla mantenga el catálogo de repuestos, registre las compras recibidas y los conteos, y consulte qué está bajo mínimo; y que una app móvil registre el repuesto usado en una OT. Usa los mismos servicios que `/inventory/` en la web (ADR-028): costo promedio ponderado por repuesto, libro de movimientos inmutable y stock negativo permitido.

- Exige el módulo `inventory` del plan (Prueba, Profesional y Empresa); sin él, los cuatro recursos y enviar `part` en `…/parts/` responden 403 con el mensaje de plan. El texto libre en la OT sigue disponible.
- **Costos** (`avg_cost`, `last_cost`, `unit_cost`, `total_cost`, `avg_cost_after`, `cost`) solo con el permiso `inventory.cost_view`; si no, vienen en `null`. El token actúa como planificador, así que los ve.
- El saldo no se edita: solo cambia con movimientos. Bodegas, mínimos, reservas y devoluciones se gestionan en la web.

**Repuestos.** `POST /parts/` es idempotente por código (sin distinguir mayúsculas): si ya existe, responde **200** con el existente sin cambiarlo; si no, **201**. `PATCH` cambia solo los campos enviados; `is_active: false` lo desactiva (sale de los buscadores; `?include_inactive=true` lo vuelve a listar).

```json
// POST /parts/
{ "code": "ROD-6204", "name": "Rodamiento 6204 2RS", "unit": "un",
  "category": "Rodamientos", "manufacturer_ref": "6204-2RSH" }

// respuesta 201 (200 si el código ya existía)
{ "id": "0193…", "code": "ROD-6204", "avg_cost": "0.0000", "is_active": true, … }
```

**Stock y bajo mínimo.** `GET /stock/?below_min=true` devuelve los saldos con disponible (existencia − reservado) menor que el mínimo: la base de una orden de compra en el ERP.

**Movimientos.** `POST /stock-movements/` con `kind`:

| `kind` | Campos | Efecto |
| --- | --- | --- |
| `receipt` | `part`, `quantity` > 0, `unit_cost` (opcional), `warehouse` (vacía = la por defecto), `reference`, `supplier`, `note`, `occurred_at` | Entrada; con costo recalcula el promedio, sin costo entra al promedio vigente. |
| `count` | `part`, `counted_quantity` ≥ 0, `warehouse`, `note` | Ajusta la diferencia; `note` (motivo) obligatoria si la diferencia es negativa o supera el 10 %. Sin diferencia → 200 sin movimientos. |
| `transfer` | `part`, `quantity` > 0, `warehouse` (origen), `to_warehouse` (destino), `note` | Dos movimientos enlazados por `transfer_group`; no cambia el promedio. |

La respuesta es `{"movements": [...]}` (uno, dos o ninguno), con **201** si se creó y **200** al reenviar el mismo `client_uuid`; esa clave usada en otra operación → **409** `conflict`.

```json
// POST /stock-movements/
{ "kind": "receipt", "part": "0193…", "quantity": "10", "unit_cost": "4500",
  "reference": "FAC-10234", "supplier": "Rodamientos del Sur", "client_uuid": "7c1e…" }

// respuesta 201
{ "movements": [ { "kind": "receipt", "quantity": "10.000", "unit_cost": "4500.0000",
  "avg_cost_after": "4440.0000", "on_hand_after": "14.000", … } ] }  // antes: 4 a $4.290
```

**Repuesto usado en una OT.** `POST /work-orders/{id}/parts/` con `part` (catálogo) o `description` + `unit_cost` (texto libre), y `quantity`. Con `part` el servidor fija descripción, unidad y costo (promedio vigente al consumir; un `unit_cost` enviado se ignora), elige la bodega (la de la reserva, la más cercana al activo o la por defecto; `warehouse` para forzarla) y descuenta la reserva de la OT. Nunca se rechaza por falta de stock. Idempotente por `client_uuid`: reenviarlo devuelve la misma línea (200) aunque la OT ya esté cerrada; en otra OT → 409. Una OT cerrada o cancelada responde 400, como el resto de la v1.

```json
// POST /work-orders/{id}/parts/
{ "part": "0193…", "quantity": "2", "used_at": "2026-10-11T14:20:00-03:00", "client_uuid": "b9d0…" }

// respuesta 201
{ "description": "ROD-6204 · Rodamiento 6204 2RS", "quantity": "2.000", "unit": "un",
  "unit_cost": "4440.0000", "cost": "8880.00", "warehouse": "0193…", … }
```

## Casos de aplicación de punta a punta

Cuatro escenarios típicos de una pyme productiva, con la secuencia exacta de llamadas.

### Caso A — Técnico en terreno con app móvil (token `write`)

El técnico recibe una preventiva mensual de un compresor en una planta con señal irregular.

1. `GET /work-orders/?scope=open` — la app descarga sus OT abiertas y las guarda en el teléfono.
2. `GET /work-orders/{id}/checklist-items/` — descarga la pauta para trabajar sin conexión.
3. `POST /work-orders/{id}/transition/` con `{"to_status": "in_progress"}` — marca el inicio.
4. `POST …/checklist-items/{item_id}/answer/` por cada ítem (verificación, presión medida, observaciones).
5. `POST …/checklist-items/{item_id}/photos/` para los ítems con foto obligatoria, y `POST /work-orders/{id}/photos/` para la evidencia general.
6. `POST /work-orders/{id}/complete/` con minutos, notas, lectura del horómetro y firma.

Si la señal se corta, la app guarda cada paso en una cola y lo reenvía después con el mismo `client_uuid`: nada se duplica y el cierre no se hace dos veces.

### Caso B — Telemetría de horómetros (token `write`)

Un PLC o un script en el PC de la planta lee los horómetros de 30 equipos cada noche.

1. Una sola vez: `GET /meters/?asset_code=ENV-01` para cada equipo con varios medidores y guardar el `id` del que corresponde.
2. Cada noche: `POST /readings/bulk/` con las 30 filas (`asset_code` o `meter`, `value`, `read_at`).
3. Revisar `errors` de la respuesta (p. ej. un contador que retrocedió sin reinicio) y alertar.

Resultado: los planes «cada 500 horas» generan su OT preventiva al cruzar el umbral, sin digitación. Si el script falla a medias y se reenvía el lote, las filas ya cargadas vuelven como duplicadas.

### Caso C — Sincronización con el ERP (token `write` para altas, `read` para reportes)

El ERP es el maestro de equipos y necesita los costos y horas de mantenimiento.

1. Alta de equipo nuevo en el ERP → `GET /assets/?code=XXX`; si `count` es 0, `POST /assets/`; si existe, `PATCH /assets/{id}/` con lo que cambió.
2. Cierre contable mensual → `GET /work-orders/?scope=done` paginado, leyendo `total_cost`, `completed_at`, `asset` y `work_type`.
3. Tablero BI → un token aparte con alcance `read`, que no puede modificar nada aunque se filtre.

### Caso D — Aviso de falla desde otro canal (token `write`)

Un bot de WhatsApp o un formulario de la intranet recibe «la envasadora se detuvo».

1. `GET /assets/?search=envasadora` para resolver el activo (o `code` si el operador escaneó la etiqueta).
2. `POST /requests/` con `description`, `asset` y `asset_stopped: true`.
3. Responder al operador con el `work_order_code` devuelto (p. ej. `OT-C-00213`) para que haga seguimiento.
4. Consultar `GET /requests/{id}/` o `GET /work-orders/{id}/` para informar el avance.

El planificador ve la OT correctiva con prioridad urgente (si el activo es crítico A) y vencimiento hoy, sin haber digitado nada.

## API de terreno (`/field/api/`)

**Qué resuelve:** que la PWA del técnico (`/field/`, modo sin conexión, ADR-011) descargue sus OT y envíe lo que hizo sin señal. Es una API **interna** del mismo origen: usa la **sesión de la web** (cookie + encabezado `X-CSRFToken` en las escrituras), nunca un token, y no la necesita ninguna integración.

**Reglas comunes:**

- La membresía es la de la persona (rol y alcance por sitio, como en la web); un token de integración no entra (401).
- Exige el módulo `work_orders` del plan (`requests/` exige `requests`), **no** el módulo `api`.
- MFA: si la organización lo exige y la persona no lo configuró → 403 `mfa_required` (JSON, sin redirección). Sin sesión → 401 con `WWW-Authenticate: Session realm="field"`.
- Límite por persona: `FIELD_API_RATE_LIMIT` (`600/min` por defecto).
- Las horas de `bootstrap`, `snapshot` y `reference` van en UTC con `Z`; los decimales como texto.
- Encabezado opcional `X-Client-Version`: menor que `min_client_version` → 409 `upgrade_required` (`details.min_client_version`); `bootstrap/` responde igual.
- Encabezado opcional `X-Field-Organization` en las escrituras: la organización donde se hizo el cambio en el teléfono. Si la sesión ya es de otra (cambio de organización en otra pestaña) → 409 `organization_changed` y no se aplica nada; el teléfono repite `bootstrap/` y deja el cambio en cola.
- Errores de estado de la OT → **409** (no 400): `cancelled` (OT cancelada), `locked` (completada o cerrada), `conflict` (`client_uuid` usado en otra cosa). Los errores de inventario con esos mismos códigos (consumir o devolver repuestos de una OT cerrada o cancelada) también son 409. A diferencia de la v1, `transition/` y `complete/` nunca reabren una OT ya completada (→ 409 `locked`). El teléfono no reintenta un 409: lo muestra como conflicto.

| Método y ruta | Qué resuelve |
| --- | --- |
| `GET bootstrap/` | Persona, organización (zona horaria, `require_failure_details`), rol, permisos (`execute`, `request_create`, `reading_create`), días por defecto, nombres de estado, transiciones de ejecución, `server_time` y `min_client_version`. |
| `GET snapshot/?days=N` | OT visibles abiertas con fecha ≤ hoy + N (7 por defecto, máx. 14), más las en ejecución, en espera, vencidas y sin fecha; asignadas a la persona primero; tope 200 con `truncated`. Cada OT trae activo o ubicación, medidor, `meter_required`, `allowed_transitions`, el checklist con `photo_count`, `planned_parts` (reservas activas con pendiente: `part`, `code`, `name`, `unit`, `quantity`, `consumed_quantity`, `pending`; vacío sin el módulo `inventory`) y `parts` (repuestos ya registrados, sin costos). |
| `GET reference/` | Códigos de falla activos por tipo, activos visibles (tope 2.000 con `truncated`), sus medidores activos y `parts`: catálogo activo compacto (`id`, `code`, `name`, `unit`, `category`; sin costos; tope 2.000 con `parts_truncated`), solo con el módulo `inventory` y `inventory.view` (si no, vacío). |
| `POST work-orders/{id}/transition/` | Iniciar, pausar o reanudar (`in_progress`, `on_hold`, `completed`), con `client_uuid` y `at`. |
| `POST work-orders/{id}/complete/` | Cierre como en la v1, con `at`. |
| `POST work-orders/{id}/labor/` | Minutos de la persona (p. ej. un apoyo): `minutes`, `client_uuid`. 201; reenvío → 200. |
| `GET/POST work-orders/{id}/parts/` | Repuesto usado sin conexión (INV-13): `part` del catálogo o `description` (texto libre; sin `unit_cost` queda en 0), `quantity`, `unit`, `used_at` (hora real) y `client_uuid`. Sin conflictos de stock (puede quedar negativo). 201; reenvío → 200 (también con la OT ya cerrada); OT cancelada o cerrada → 409 `cancelled`/`locked`; la clave en otra OT → 409 `conflict`; `part` sin el módulo `inventory` → 403. Los costos solo con `inventory.cost_view` (el técnico no los ve). |
| `GET work-orders/{id}/checklist-items/` y `POST …/{item_id}/answer/` | Como en la v1, con `answered_at` y `superseded`. |
| `POST work-orders/{id}/photos/` y `…/checklist-items/{item_id}/photos/` | Como en la v1, con `taken_at`. |
| `GET work-orders/{id}/attachments/` | Como en la v1. |
| `POST requests/` | Aviso de falla en JSON o `multipart/form-data` con `photo` y `reported_at` (inicio de la falla); responde además `photo_warning` si la foto no cupo. |
| `POST readings/` y `GET meters/` | Lectura de medidor (origen «manual») y medidores visibles. |

La forma completa de `bootstrap` y `snapshot` está en `apps/field/fixtures/demo.json` (los tests la comparan con las respuestas reales).

### Horas informadas por el cliente

`at`, `answered_at`, `taken_at` y `reported_at` (también en la v1, opcionales) se acotan a lo creíble: vacía o futura → ahora (2 minutos de tolerancia de reloj); más de 30 días atrás → ahora; anterior a la creación de la OT → la creación. Cuando se ajusta, la auditoría guarda la hora informada y el motivo.

## Lo que la API aún no hace

La v1 cubre el flujo del técnico y las integraciones de datos; la configuración y la planificación siguen solo en la web.

| Falta | Consecuencia práctica | Cómo resolverlo hoy |
| --- | --- | --- |
| Endpoints de ubicaciones, clases de activo y catálogo de fallas | Los ids de `location`, `asset_class`, `failure_mode` y `failure_cause` no se pueden descubrir por la API. | Copiarlos desde la web, o leer `location` y `asset_class` de un activo existente. |
| Planes preventivos | No se crean ni editan planes por la API. | Web; las OT preventivas se generan solas por fecha o por lectura. |
| Asignar técnico | `assigned_to` es solo lectura: no se asigna ni se reasigna por la API. | Web. |
| Gestionar solicitudes | No se aceptan, rechazan ni fusionan avisos por la API. | Web (bandeja del planificador). |
| Descargar archivos | Los adjuntos devuelven solo metadatos, nunca una URL. | Descarga desde la web con URL firmada. |
| Borrado | No hay `DELETE` en ningún recurso (sin borrado físico por diseño). | Cancelar una OT con `transition` a `cancelled`. |
| Notificaciones salientes | No hay webhooks: un sistema externo debe consultar periódicamente. | Consultar `GET /work-orders/?scope=open` cada cierto tiempo. |
| Bodegas, mínimos, reservas y devoluciones | Solo lectura de bodegas y stock; no se crean bodegas, no se fijan mínimos, no se reservan ni se devuelven repuestos por la API. | Web (`/inventory/` y el detalle de la OT). |
| Token por usuario | El token actúa como integración (rol planificador), no como un técnico concreto. | Un token por app o integración, con nombre claro. |

### Referencias

- Swagger UI: `/api/docs/` · esquema OpenAPI: `/api/schema/`.
- Decisión y detalle técnico: ADR-005 en `docs/04_arquitectura.md`.
- Código: `apps/api/v1.py` (endpoints), `apps/api/serializers.py` (campos), `apps/api/services.py` (tokens e idempotencia); terreno en `apps/api/field_views.py`, `apps/api/field.py` y `apps/api/field_serializers.py` (ADR-011).
