# 02 — Requisitos

**Estado:** borrador v0.1 (2026-09-24)

Convenciones:

- Cada historia tiene un ID `MOD-NN`, una prioridad y criterios de aceptación
  (CA) verificables. Los CA son la base de los tests.
- Prioridad = fase del roadmap (ver [05_roadmap.md](05_roadmap.md)):
  **M0** (MVP‑0, demo), **M1** (MVP‑1, piloto), **F2**, **F3** (posteriores).
- "Usuario" = persona con membresía activa en la organización activa.
- Toda historia implica: aislamiento por organización, registro de auditoría en
  escrituras, textos traducibles, zona horaria de la organización/sitio.

---

## 1. Organización, sitios y usuarios (`ORG`)

**ORG-01 · Alta de organización (M0)**
Como superadmin de plataforma quiero crear una organización con plan, zona
horaria e idioma, para habilitar a un cliente.
- CA1: La organización queda con plan, cupos (activos, usuarios, MB) y período
  de prueba (fecha de término) configurables.
- CA2: Se crea la primera invitación para su administrador.
- CA3: Solo usuarios con `is_platform_admin` pueden hacerlo.

**ORG-02 · Autoregistro con prueba gratuita (M1)**
Como jefe de mantención quiero registrarme y crear mi organización yo mismo,
para probar sin hablar con un vendedor.
- CA1: El registro crea usuario + organización (plan Prueba, 30 días) + membresía
  de administrador en una sola transacción.
- CA2: Requiere verificación de correo antes de crear datos.
- CA3: Tiene límite de intentos y protección anti‑bots.
- Nota: `/signup/` guarda un `PendingSignup` (contraseña ya con hash) y envía
  el enlace; al confirmar (botón POST, para que los antivirus del correo no lo
  consuman) se crean usuario, organización y membresía en una transacción. Si
  el correo ya existe se avisa por correo, no en pantalla. Anti‑bots sin JS de
  terceros: campo trampa, tiempo mínimo y una suma firmada; límite por IP con
  el rate limit de allauth. El registro propio de allauth sigue cerrado.

**ORG-03 · Asistente de puesta en marcha (M1)**
Como administrador nuevo quiero un asistente que me guíe, para tener el sistema
funcionando en menos de una hora.
- CA1: Pasos: rubro → sitios → activos (Excel o alta rápida) → plantillas de
  planes sugeridas por clase → lecturas iniciales de medidores → resumen del
  calendario generado.
- CA2: Cada paso se puede saltar y retomar; el avance se guarda.
- CA3: Al elegir un rubro se copian a la organización las clases de activo,
  atributos y catálogo de fallas de ese rubro (copias editables, no referencias).

**ORG-04 · Jerarquía de ubicaciones (M0)**
Como administrador quiero definir sitios y áreas en árbol, para ubicar activos.
- CA1: Profundidad libre (recomendado ≤ 4); cada nodo tiene tipo (sitio, área,
  línea, otro), código único en la organización y zona horaria opcional
  (hereda de la organización).
- CA2: No se puede eliminar un nodo con activos o hijos; se puede desactivar.
- CA3: Mover un nodo actualiza la ruta de todos sus descendientes.

**ORG-05 · Invitar usuarios (M0)**
Como administrador quiero invitar por correo asignando un rol, para sumar a mi equipo.
- CA1: El enlace expira a los 7 días y es de un solo uso (token guardado como hash).
- CA2: Si el correo ya tiene cuenta, se agrega la membresía sin crear otro usuario.
- CA3: No se puede superar el cupo de usuarios del plan (mensaje claro).

**ORG-06 · Roles y alcance (M0)**
Como administrador quiero asignar un rol por membresía y, opcionalmente,
restringirla a ciertos sitios, para dar acceso limitado a contratistas o clientes.
- CA1: Roles base: `admin`, `planner` (planificador), `supervisor`, `technician`,
  `viewer` (solo lectura), `external` (cliente o contratista).
- CA2: `external` y cualquier rol con alcance limitado solo ven activos, OT,
  solicitudes e indicadores de sus sitios (incluyendo descendientes).
- CA3: La matriz rol → permisos está en código y documentada; los tests cubren
  cada permiso sensible.

**ORG-07 · Varias organizaciones por usuario (M0)**
Como usuario con acceso a varias organizaciones quiero cambiar de organización
activa, para trabajar con cada cliente por separado.
- CA1: Selector visible si tiene más de una membresía; recuerda la última.
- CA2: Al cambiar, ningún dato de la organización anterior queda visible
  (incluido caché de navegador en páginas sensibles).

**ORG-08 · MFA (M1)**
Como usuario quiero activar segundo factor (TOTP + códigos de recuperación);
como administrador quiero exigirlo a mi organización.
- CA1: Si la organización lo exige, el usuario sin MFA es forzado a configurarlo
  al entrar.
- Nota: por defecto es obligatorio para administradores y opcional para el
  resto; cada organización puede exigirlo a todos o dejarlo opcional
  (`mfa_policy`). Con varias organizaciones manda la activa (ADR-022).

**ORG-09 · Marca por organización (M1)**
Como administrador quiero subir mi logo y elegir un color primario, para que la
app y los reportes lleven mi marca.
- CA1: El color se valida: si no alcanza contraste AA para su uso, el sistema
  ajusta el tono o el color de texto automáticamente y lo informa.
- CA2: Tokens de estado y accesibilidad no se sobrescriben.

**ORG-10 · Cupos y módulos por plan (M1)**
- CA1: Alcanzar el 80 % de un cupo muestra aviso; el 100 % bloquea la creación
  (no la lectura ni la edición).
- CA2: Los módulos no incluidos no aparecen en la navegación y sus URL/API
  responden 403 con mensaje de plan.
- Nota: el cupo de activos se revisa en `create_asset` (web y API) y en la
  importación Excel (todo o nada) con `organizations.services.check_quota`;
  cuentan todos los activos, también los dados de baja. El aviso del 80 %
  aparece al crear o importar activos y en Configuración. El mapa módulo ↔ URL
  está en `apps/organizations/modules.py` (`assets`, `work_orders`,
  `preventive`, `requests`, `api`, `exports`); la API exige además el módulo
  `api`. El inicio con los KPI no se bloquea (portada de todos los planes). Un
  plan sin módulos listados no restringe módulos. Pendiente: ocultar los botones
  de exportar dentro de las listas (hoy responden 403).

**ORG-11 · SSO SAML/OIDC (F3, plan Empresa)**

**ORG-12 · Roles personalizados (F3, plan Profesional+)**

## 2. Activos (`AST`)

**AST-01 · Clases de activo con atributos (M0)**
Como administrador quiero definir clases (ej. "Grúa horquilla") con atributos
propios (capacidad, combustible…), para registrar datos técnicos homogéneos.
- CA1: Tipos de atributo: texto, número (con unidad), fecha, sí/no, lista.
- CA2: Cada clase indica su categoría ISO 14224 de referencia y nivel típico
  (equipo, sistema, componente).
- CA3: Cambiar la definición no borra valores existentes; los atributos
  eliminados quedan ocultos.

**AST-02 · Alta y ficha de activo (M0)**
- CA1: Campos: código (único en la organización), nombre, clase, ubicación,
  activo padre (opcional), estado, criticidad (A/B/C), fabricante, modelo,
  serie, año, fecha de puesta en servicio, atributos de la clase.
- CA2: La ficha muestra: datos, medidores, planes asignados, OT abiertas,
  historial (OT cerradas, fallas, cambios de estado), documentos y fotos.
- CA3: Alta rápida: solo código, nombre y clase son obligatorios.

**AST-03 · Jerarquía activo → sistema → componente (M0)**
- CA1: Un activo puede tener hijos; no se permiten ciclos.
- CA2: El historial y los KPI del padre pueden incluir los de los hijos (opción
  "incluir componentes").

**AST-04 · Estados y detenciones (M0)**
- CA1: Estados: operativo, detenido, en mantención, de baja.
- CA2: Cada cambio de estado abre/cierra un período (`AssetStatusPeriod`) con
  motivo y OT/falla asociada; estos períodos alimentan la disponibilidad.
- CA3: Dar de baja un activo cancela sus asignaciones de plan (con confirmación).

**AST-05 · Código QR (M0)**
- CA1: Cada activo tiene un QR con un token no adivinable (no el ID interno).
- CA2: Impresión de etiquetas en lote (PDF, varias por hoja) con logo de la
  organización.
- CA3: Escanear el QR con sesión abre la ficha; sin sesión ver `REQ-02`.

**AST-06 · Documentos y fotos (M0)**
- CA1: Adjuntos de hasta 20 MB; imágenes comprimidas y redimensionadas
  (lado mayor ≤ 2000 px) al subir.
- CA2: El uso cuenta para el cupo de almacenamiento de la organización.
- CA3 (M1): Los manuales (PDF) se adjuntan al activo o a su clase de activo;
  los de la clase aplican a todos sus activos. El worker extrae el texto por
  página para la búsqueda y el asistente (`AI-*`, ADR-018).

**AST-07 · Medidores y lecturas (M0)**
Como técnico quiero registrar horómetro/odómetro, para que los preventivos por
uso se calculen solos.
- CA1: Tipos: horas, km, ciclos, otro (unidad libre). Acumulativo o no.
- CA2: Una lectura acumulativa menor a la anterior se rechaza, salvo que se
  marque como "reinicio/cambio de medidor".
- CA3: Lecturas con fuente: manual, importación, API, OT (al cerrar).
- CA4: Advertencia si la lectura implica un uso diario anómalo (ej. > 24 h/día).

**AST-08 · Importación Excel de activos (M1)**
- CA1: Plantilla descargable con columnas y validaciones.
- CA2: Vista previa con errores por fila antes de confirmar; nada se guarda si
  el usuario no confirma.
- CA3: Importación idempotente por código de activo (actualiza si existe).

**AST-09 · Importación de lecturas por CSV/API (M1)**
- CA1: CSV con código de activo (o id de medidor), valor, fecha y reinicio opcional;
  vista previa con errores por fila antes de confirmar (mismo patrón que AST-08).
- CA2: Idempotente por (medidor, fecha, valor); origen "importación" (web) o "API".
- CA3: Lo mismo por API (`POST /api/v1/readings/bulk/`), con resultado por fila.

## 3. Mantenimiento preventivo (`PM`)

**PM-01 · Planes con tareas y checklist (M1)**
Como planificador quiero definir un plan (ej. "Mantención 250 h grúa horquilla")
con ítems de checklist, repuestos, herramientas y duración estimada.
- CA1: Tipos de ítem: check (OK/No OK/N.A.), medición numérica (con unidad y
  rango aceptable), texto, foto obligatoria, firma.
- CA2: Repuestos y herramientas como texto + cantidad (catálogo de inventario en F2).

**PM-02 · Disparadores (M1)**
- CA1: Por calendario (cada N días/semanas/meses), por medidor (cada N unidades)
  o mixto: vence **lo que ocurra primero**.
- CA2: Modo de reprogramación: *fijo* (desde la fecha/lectura programada) o
  *flotante* (desde la última ejecución real).
- CA3: Tolerancia configurable (ej. ±10 %) usada para el cumplimiento.

**PM-03 · Versionado (M1)**
- CA1: Editar un plan publicado crea una versión borrador; al publicarla, las
  OT futuras usan la nueva versión.
- CA2: Una OT guarda la versión con que se generó y una copia (snapshot) de su
  checklist; cambiar el plan nunca altera OT existentes.

**PM-04 · Asignación de planes a activos (M1)**
- CA1: Asignación individual, en lote o automática por clase de activo.
- CA2: Cada asignación calcula su próximo vencimiento (fecha y/o lectura).

**PM-05 · Generación automática de OT (M1)**
- CA1: Una tarea periódica (cada hora) genera OT para vencimientos dentro del
  horizonte configurable (por defecto 14 días) o ya vencidos por medidor.
- CA2: Nunca hay dos OT abiertas de la misma asignación (idempotencia).
- CA3: Si un plan de mayor alcance vence junto a uno menor del mismo activo
  (ej. 500 h y 250 h), se puede configurar que el mayor "absorba" al menor.

**PM-06 · Calendario de preventivos (M1)**
- CA1: Vista mensual/semanal y proyección a 12 meses (vencimientos por medidor
  estimados con el uso diario promedio).

**PM-07 · Biblioteca de plantillas de planes (M1)**
- CA1: Plantillas por clase de activo; al adoptarlas se copian a la organización.

**PM-08 · Importación/exportación de planes por Excel (M1)**

## 4. Órdenes de trabajo (`WO`)

**WO-01 · Crear OT (M0)**
- CA1: Tipos: preventiva, correctiva, predictiva/por condición, mejora, inspección.
- CA2: Campos: activo (o ubicación), título, descripción, prioridad
  (1 urgente – 4 baja), fecha límite, estimación, responsables.
- CA3: Número correlativo por organización y tipo, asignado por el servidor
  (ej. `OT-C-000124`), sin huecos por concurrencia.

**WO-02 · Ciclo de vida (M0)**
- CA1: Estados: `requested` → `planned` → `scheduled` → `in_progress` ⇄
  `on_hold` → `completed` → `closed`; `cancelled` desde cualquier estado previo
  a `closed`.
- CA2: Pasos opcionales por organización: "requiere aprobación para cerrar"
  (si está apagado, `completed` pasa a `closed` automáticamente).
- CA3: Cada transición registra quién, cuándo, desde/hacia y comentario.
- CA4: Transiciones inválidas se rechazan en el servicio, no solo en la UI.
- CA5: Nombres visibles de los estados configurables por organización.

**WO-03 · Ejecución en móvil (M0)**
Como técnico quiero ejecutar el checklist desde el celular.
- CA1: Pantalla de ejecución con un ítem por tarjeta, botones ≥ 48 px, guardado
  automático por ítem.
- CA2: Por ítem: resultado, observación, fotos, medición (validada contra rango;
  fuera de rango se marca No OK y sugiere crear correctiva).
- CA3: Al completar: lectura de medidor (si el plan lo pide), firma y comentario.
- CA4: No se puede completar con ítems obligatorios sin responder.

**WO-04 · Mano de obra, repuestos y costos (M0)**
- CA1: Registro de técnicos con horas (inicio/fin o duración); costo hora por
  técnico configurable, copiado a la OT al registrar.
- CA2: Repuestos consumidos (texto, cantidad, costo unitario) y otros costos
  (servicios externos).
- CA3: Totales por OT calculados y guardados al cerrar.

**WO-05 · Lista y filtros (M0)**
- CA1: Filtros por estado, tipo, activo, sitio, responsable, fechas; paginación
  obligatoria; "Mis OT" para técnicos.

**WO-06 · Correctiva desde hallazgo (M1)**
- CA1: Desde un ítem No OK se crea una OT correctiva vinculada, con activo y
  foto precargados.

## 5. Solicitudes, fallas y backlog (`REQ`)

**REQ-01 · Solicitud de trabajo (M0)**
Como cualquier usuario autorizado quiero avisar una falla o pedir un trabajo
desde el celular.
- CA1: Campos mínimos: activo (o ubicación), descripción, foto opcional,
  "¿el equipo está detenido?".
- CA2: Si está detenido, el activo pasa a estado *detenido* y se abre un evento
  de falla con hora de inicio.
- CA3: El planificador acepta (convierte en OT), rechaza (con motivo) o fusiona
  con otra solicitud.

**REQ-02 · Aviso por QR sin cuenta (M1, activable por organización)**
- CA1: Si la organización lo habilita, escanear el QR sin sesión muestra un
  formulario mínimo (nombre, descripción, foto).
- CA2: Protección anti‑abuso: límite por IP y por activo, captcha liviano.
- CA3: La solicitud queda marcada como "anónima" y requiere revisión.
- Implementación: el administrador lo activa en Configuración ("Avisos por QR
  sin cuenta"); el plan debe incluir Solicitudes. La página pública solo
  muestra la organización (nombre y logo) y el código y nombre del activo, sin
  navegación ni caché (`no-store`), con CSRF. Anti-abuso: campo trampa, tiempo
  mínimo y suma firmada (`apps/accounts/antibot.py`, el mismo del registro) y
  límites en el caché compartido: 5 envíos por hora por IP (cuenta también los
  fallidos) y 10 avisos al día por activo (ajustes `QR_REPORT_LIMIT_PER_IP`,
  `QR_REPORT_LIMIT_PER_ASSET`, `QR_REPORT_MIN_SECONDS`). El aviso no crea OT ni
  cambia el estado del activo aunque marque "detenido": llega a Solicitudes
  como "Anónima — requiere revisión" y avisa a planificadores y supervisores
  con el sitio a la vista; aceptarlo crea la OT correctiva (sin detener el
  activo) y le pasa la foto.

**REQ-03 · Registro de falla (M0)**
- CA1: Campos: modo, causa, mecanismo de falla, método de detección (catálogos
  estructurados según ISO 14224, contenido propio editable), componente afectado,
  inicio y fin de la detención, impacto (detención total, parcial, sin detención).
- CA2: Al cerrar una OT correctiva se pide completar la falla (modo y causa
  obligatorios si la organización lo configura).
- CA3: Al cerrar una OT correctiva se piden, en texto libre, **síntoma
  observado** (`failure_event.description`) y **acción realizada**
  (`work_order.completion_notes`), con ayuda en pantalla ("¿Qué viste? ¿Qué
  hiciste?") y campos aptos para dictado por voz. Es la base de conocimiento
  del asistente (ADR-018): sin esto no hay de dónde aprender.

**REQ-04 · Backlog priorizado (M1)**
- CA1: Backlog = OT no cerradas ni canceladas, ordenadas por puntaje:
  criticidad del activo × prioridad de la OT, desempate por fecha límite.
- CA2: Muestra horas estimadas totales y **backlog en semanas**
  (horas estimadas / capacidad semanal del equipo).

**REQ-05 · Programación semanal (M1)**
- CA1: Tablero por semana: columnas por día (y opcionalmente por técnico),
  arrastrar OT del backlog, carga vs. capacidad por técnico.
- CA2: Programar una OT la pasa a `scheduled` y notifica al responsable.

## 6. Inventario y repuestos (`INV`, F2)

Catálogo de repuestos, bodegas, stock mínimo, reservas por OT, consumos y
costo promedio ponderado. El registro de repuestos de la OT (WO-04) se enlazará
al catálogo sin migrar datos (campo `part` opcional junto al texto).

## 7. Mantenimiento por condición (`CBM`, F2)

- Reglas de umbral sobre medidores o variables (>, <, fuera de rango, tasa de
  cambio) que generan alerta y, opcionalmente, una OT o solicitud.
- Ingesta por API y CSV de lecturas de variables (temperatura, vibración…).
- MVP: solo se diseñan las tablas `measurement_point` y `reading` (ver 03).

## 8. Indicadores y reportes (`KPI`)

**KPI-01 · Indicadores básicos (M1)**
Con filtros por fecha, sitio, clase y activo:
- Disponibilidad = (tiempo calendario − detención) / tiempo calendario.
- MTBF = tiempo operativo / número de fallas con detención.
- MTTR = tiempo total de reparación / número de reparaciones.
- Cumplimiento del preventivo = OT preventivas cerradas dentro de tolerancia /
  OT preventivas vencidas en el período. "Dentro de tolerancia" es la regla de
  PM-02 CA3 (`is_on_time`): fecha límite ± tolerancia del plan, o lectura
  programada ± tolerancia; basta una. Preventivas sin plan: tolerancia cero.
  No cuentan (fuera del denominador) las canceladas ni las abiertas cuya
  ventana de tolerancia aún no termina; las abiertas con la ventana cumplida
  cuentan como incumplidas. Se calcula en una consulta, sin consultas por OT.
- CA: Cada KPI muestra su fórmula y referencia (EN 15341 / SMRP) en un tooltip.
- CA: Resultados verificados por tests con datos de fixture de valor conocido.

**KPI-02 · Indicadores ampliados (F2)**: backlog en semanas en el tiempo,
proporción preventivo/correctivo, costos por activo/clase, Pareto de fallas.

**KPI-03 · Exportación (M1)**: listas y reportes a Excel; ficha de activo y OT
a PDF con marca de la organización.

## 9. Transversales (`X`)

**X-01 · Auditoría (M1)**: toda creación, edición, eliminación, transición y
acceso de administración queda registrada (quién, qué, cuándo, desde dónde,
cambios). Consultable y filtrable por el administrador. Solo inserción.

**X-02 · Notificaciones (M1)**: en la app y por correo: OT asignada, OT por
vencer/vencida, solicitud nueva, solicitud resuelta. Preferencias por usuario.

**X-03 · Exportación completa de datos (M1)**: el administrador descarga un ZIP
con todos los datos de su organización (Excel/CSV + adjuntos). Implementado: un CSV
por tabla + archivos, armado en segundo plano, vence a los 7 días (ADR-027).

**X-04 · Borrado de organización (M1)**: a pedido, con período de gracia de
30 días; luego borrado de datos y archivos, registrado. Implementado: purga diaria
con lápida de la organización; las cuentas de usuario se conservan (ADR-027).

**X-05 · API REST (M1)**: OpenAPI en `/api/schema/`, documentación navegable,
autenticación por token de organización con alcances; endpoints de activos,
lecturas, OT y solicitudes. Desde la API también se completa una OT y se
responde su checklist con las mismas reglas que la web (ADR-005).

**X-06 · Modo offline PWA (F2)**: ver [04_arquitectura.md](04_arquitectura.md), ADR-011.

**X-07 · i18n (M0)**: todos los textos marcados para traducción; español (Chile)
completo; inglés y portugués en F3.

## 10. Asistente de diagnóstico con IA (`AI`, F2)

Ver [04_arquitectura.md](04_arquitectura.md), ADR-018. Módulo por plan.

**AI-01 · Pedir ayuda desde el activo o la OT**
Como técnico quiero describir un síntoma y recibir orientación basada en el
historial y los manuales de ese equipo.
- CA1: Botón "Pedir ayuda" en la ficha del activo (también al llegar por QR)
  y en la OT; la conversación queda asociada al activo y, si aplica, a la OT.
- CA2: El asistente ya conoce el activo (clase, modelo, medidores, fallas y
  OT recientes); el técnico no tiene que explicarlo.
- CA3: Entrada por texto (con dictado del teclado) y foto (placa, alarma,
  pieza dañada).
- CA4: Usable completo desde el teléfono (ADR-017); texto ≥ 16 px.

**AI-02 · Respuestas con fuente**
- CA1: Cada recomendación indica su origen con ícono + texto: manual
  (documento y página, enlazado), historial (código de OT, enlazado) o
  conocimiento general (marcado como de menor confianza).
- CA2: Si no hay información suficiente, lo dice y sugiere qué dato falta.
- CA3: Solo usa datos que el usuario puede ver (organización y alcance por
  sitio); probado en `tests/isolation/`.

**AI-03 · Seguridad**
- CA1: Recuerda aislar energías (bloqueo y etiquetado) antes de intervenir y
  nunca sugiere anular protecciones ni enclavamientos.
- CA2: Aviso visible: la orientación no reemplaza el criterio del técnico ni
  los procedimientos de la empresa.

**AI-04 · Cierre del ciclo**
- CA1: Al completar la OT se propone un resumen de la conversación como nota
  de cierre; el técnico lo edita o lo descarta.
- CA2: El técnico marca si la ayuda sirvió (sí / en parte / no).

**AI-05 · Cupo, costo y auditoría**
- CA1: Cupo mensual de conversaciones por organización según el plan
  (ORG-10); al agotarse se informa sin bloquear el resto del sistema.
- CA2: Cada turno guarda modelo y tokens usados; el superadmin ve el costo por
  organización.
- CA3: Conversaciones consultables por el administrador, incluidas en la
  exportación completa (X-03) y en el borrado de organización (X-04).

---

## Requisitos no funcionales

| ID | Categoría | Requisito | Verificación |
|---|---|---|---|
| NF-01 | Seguridad | OWASP Top 10 cubierto: CSRF, XSS (autoescape), inyección (ORM), control de acceso en servicios y selectors, cabeceras seguras. | Tests de permisos; `manage.py check --deploy` en CI; revisión antes de cada release. |
| NF-02 | Seguridad | HTTPS obligatorio, HSTS, cookies `Secure`/`HttpOnly`/`SameSite=Lax`. | `check --deploy`, prueba en staging. |
| NF-03 | Seguridad | Contraseñas: mínimo 10 caracteres, validadores de Django, verificación contra contraseñas filtradas (opcional). Hash Argon2. | Tests. |
| NF-04 | Seguridad | Límite de intentos de acceso por usuario e IP; bloqueo temporal. | Tests. |
| NF-05 | Seguridad | Secretos solo en variables de entorno; `.env` fuera del repo. | Revisión de secretos en CI (gitleaks). |
| NF-06 | Seguridad | Almacenamiento de objetos cifrado en reposo; URLs de descarga firmadas y de corta duración. | Configuración del proveedor. |
| NF-07 | Aislamiento | Ningún dato de una organización es accesible desde otra, por UI, API, archivos, exportaciones ni tareas. | Suite `tests/isolation/` obligatoria; RLS de PostgreSQL activo antes del piloto con datos reales (ADR-026, `tests/isolation/test_rls.py`, `manage.py rls_status`). |
| NF-08 | Privacidad | Exportación completa y borrado a pedido (X-03, X-04). | Tests + prueba manual por release. |
| NF-09 | Respaldo | Respaldo diario de la base, retención ≥ 7 días (idealmente 30), restauración probada trimestralmente en staging. | Registro de la prueba. |
| NF-10 | Rendimiento | Listas paginadas siempre; sin N+1 (tests con `django_assert_max_num_queries`); p95 < 500 ms en páginas principales con 5.000 activos por organización. | Tests y datos sintéticos. |
| NF-11 | Escala | Diseñado para cientos de organizaciones y miles de activos por organización; índices que empiezan por `organization_id`. | Revisión del modelo. |
| NF-12 | Calidad | Tests unitarios, de servicios, de aislamiento y de permisos; cobertura ≥ 80 % en `services` y `selectors`. | CI. |
| NF-13 | Calidad | Lint y formato (ruff), chequeo de migraciones, CI en cada push. | CI. |
| NF-14 | Operación | Ambientes dev, staging y producción; despliegue automático a staging en `main` y manual a producción. | Pipeline. |
| NF-15 | Operación | Monitoreo de errores (Sentry), logs estructurados, `/healthz` (vida) y `/readyz` (base de datos). | Staging. |
| NF-16 | Accesibilidad | WCAG 2.2 AA: contraste, foco visible, navegación por teclado, etiquetas; objetivos táctiles ≥ 48 px en vistas de técnico. | Test automático de contraste de tokens + revisión manual. |
| NF-17 | Móvil | Mobile first: **toda** funcionalidad (no solo la del técnico) se completa desde un teléfono de 360 px, sin scroll horizontal; alto contraste para exteriores (ADR-017). | `tests/ui/test_mobile.py` en CI + prueba a 360 px por vista + revisión en dispositivo real por release. |
| NF-18 | Tiempo | Fechas en UTC en la base; se muestran en la zona del sitio o de la organización. | Tests con zonas distintas. |
| NF-19 | i18n | Ningún texto de interfaz sin marcar para traducción. | Revisión + `makemessages` en CI. |
