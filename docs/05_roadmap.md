# 05 — Roadmap

**Estado:** borrador v0.3 (2026-09-27): MVP‑0 hecho; código del MVP‑1 hecho, falta activar producción y el piloto.

> **Supuesto de dedicación: 15 h/semana.** Aún no está confirmado; ajústalo y
> reescalo las semanas. Las estimaciones incluyen tests y un margen de ~20 %
> para imprevistos. 1 semana = 15 h.

## Resumen

| Fase | Objetivo | Semanas | Acumulado |
|---|---|---|---|
| **F0 · Fundaciones** | Repo, CI, tenancy, auth, sistema de diseño | 4 | 4 |
| **MVP‑0 · Demo** | Activos, medidores, OT con checklist, solicitudes y fallas | 10 | 14 |
| **MVP‑1 · Piloto** | Preventivos automáticos, backlog, KPIs, puesta en marcha, producción | 14 | 28 |
| **F2 · Terreno y stock** | Offline, asistente IA, inventario, condición, KPIs ampliados | ~20 | ~48 |
| **F3 · Escala comercial** | SSO, roles personalizados, cobro, idiomas, API ampliada | ~12 | ~60 |

Aproximadamente **6,5 meses hasta el piloto** con 15 h/semana.

## v0.1 · Demo (hecha el 2026-09-24)

Versión de demostración local para mostrar a prospectos; no es para producción.

**Incluye:** multi-tenancy que falla cerrado con tests de aislamiento, login y
membresías con roles, cambio de organización, ubicaciones, clases y activos
con QR y etiquetas imprimibles, aviso de falla por QR con foto (crea solicitud
+ OT correctiva y detiene el activo), ciclo de vida de la OT (ADR-008),
checklist móvil con mediciones y rangos, cierre con horas y modo/causa de
falla, planes preventivos por calendario con generación automática y
reprogramación flotante, tablero con disponibilidad, MTBF, MTTR y
cumplimiento, tokens de marca con modo claro/oscuro verificados por test,
admin de Django acotado a la organización para configurar, y `seed_demo`.

**Diferencias con el diseño que se cierran en F0 / MVP:** SQLite en vez de
PostgreSQL; pip en vez de uv; login de Django en vez de allauth (sin MFA);
sin API, auditoría, notificaciones, medidores, versionado de planes ni
importación Excel; archivos en disco local (servidos solo tras verificar la
organización) en vez de R2; sin CI ni despliegue.

## Hecho después de la v0.1

- **Medidores y preventivos por uso (AST-07, PM-02, parte de PM-04/05):**
  horómetro/odómetro por activo, lecturas con validación (no retroceder
  salvo reinicio, aviso si > 24 h/día), uso diario promedio, planes por
  calendario, por uso o mixtos (lo que ocurra primero), OT generada al
  registrar la lectura que alcanza el umbral y reprogramación desde la
  lectura de cierre.

- **Versiones de planes (PM-01..04, ADR-016):** borrador → publicar, versiones
  inmutables, repuestos y herramientas previstos, modo fijo o flotante,
  tolerancia, recálculo de vencimientos al cambiar la frecuencia y OT con
  la versión con que se generó. Pendiente: absorción de planes (PM-05 CA3)
  y pantallas propias fuera del admin.

## F0 · Fundaciones (4 semanas)

Parte de F0 ya existe gracias a la v0.1 (tenancy, estructura de apps, tokens),
por lo que se estima en ~3 semanas.

**Avance (2026-09-24):** hechos 0.1 a 0.4: GitHub Actions
con PostgreSQL 16 (Python 3.11 y 3.13), uv (`uv.lock`, CI con
`uv sync --locked`), pre-commit (ruff, lock, `requirements*.txt` exportados
para Render y migraciones), chequeo de migraciones y `check --deploy`, y
soporte de `DATABASE_URL`. Además: tests de permisos por rol y de cantidad de
consultas. 0.3: django-allauth con login por correo, verificación obligatoria,
recuperación y cambio de contraseña (el admin entra por el mismo login),
Argon2, registro cerrado hasta ORG-02, catálogo `Plan` (cupos provisionales,
se hacen cumplir en ORG-10), estado y fin de prueba en `Organization` y última
organización recordada al iniciar sesión (ORG-07). Pendiente: despliegue
(0.5, requiere cuentas de Render, Cloudflare, Sentry y el proveedor de correo).

| # | Entregable | Sem. |
|---|---|---|
| 0.1 | Esqueleto Django 5.2 con uv, settings por ambiente, ruff, pytest, pre-commit, GitHub Actions con Postgres | 0.5 |
| 0.2 | `core`: `TenantModel`, UUIDv7, contexto de tenant que falla cerrado, test genérico de aislamiento | 1 |
| 0.3 | `accounts` + `organizations`: User, Organization, Plan, Membership, selector de organización, allauth (login y verificación) | 1 |
| 0.4 | Sistema de diseño: tokens, fuentes locales, layout base (navbar, menú lateral y navegación móvil), modo claro/oscuro, `/design-system/`, test de contraste | 1 |
| 0.5 | Despliegue a staging en Render + R2 + Sentry + `/healthz` | 0.5 |

**Hecho cuando:** un usuario inicia sesión en staging, cambia entre dos
organizaciones y los tests de aislamiento pasan en CI.

## MVP‑0 · Demo (10 semanas)

**Estado (2026-09-26): hecho.** Todos los criterios M0 de ORG, AST, WO, REQ
y X-07 están en `main` y desplegados en Render, incluidos los que se habían
considerado recortables: etiquetas PDF con logo (ADR-019), OT y solicitudes
sobre ubicación con varios responsables, nombres de estado por organización,
checklist aplicable a correctivas, fotos por ítem y firma. La demo del "Hecho
cuando" funciona con `seed_demo` sin atajos.

Quedan fuera, a propósito: color de marca y el resto de ORG-09 (M1); las OT
sobre ubicación no registran falla (`FailureEvent`
exige activo, ver 03); el catálogo `.mo` no se compila hasta
que haya traducciones (F3).

| # | Entregable | Req. | Sem. |
|---|---|---|---|
| 1 | Ubicaciones en árbol | ORG-04 | 1 |
| 2 | Invitaciones, roles, alcance por sitio, tests de permisos | ORG-05/06/07 | 1.5 |
| 3 | Clases de activo con atributos, activos, jerarquía y ficha | AST-01/02/03 | 2 |
| 4 | Estados y detenciones, QR y etiquetas PDF, adjuntos con compresión | AST-04/05/06 | 1.5 |
| 5 | Medidores y lecturas | AST-07 | 0.5 |
| 6 | OT: alta, correlativos, ciclo de vida, lista y filtros | WO-01/02/05 | 1 |
| 7 | Ejecución móvil del checklist, mano de obra, repuestos y costos | WO-03/04 | 1.5 |
| 8 | Solicitudes y registro de fallas con catálogo base; cierre de correctiva con síntoma y acción realizada (base del asistente, ADR-018) | REQ-01/03 | 1 |

Si se atrasa, el ítem 4 se reduce a QR solamente (sin etiquetas PDF).

**Hecho cuando:** se puede hacer una demo completa: crear activo → avisar
falla por QR con sesión → convertirla en OT → ejecutar el checklist en el
celular → cerrarla con costos y falla registrada.

## MVP‑1 · Piloto (14 semanas)

**Estado (2026-09-27): código hecho.** Están en `main`, con CI en PostgreSQL
bajo RLS: pantallas y motor de planes (tipos de ítem, semanas/meses de
calendario con día ancla, absorción, horizonte por organización), calendario y
proyección a 12 meses, backlog priorizado y tablero semanal, KPIs con filtros
y fórmulas, exportaciones Excel/PDF, importación de activos, planes y lecturas,
manuales con texto por página, plantillas de 4 rubros y asistente de puesta en
marcha, auditoría y notificaciones, MFA (obligatorio para administradores),
autoregistro, cupos y módulos por plan, color de marca, aviso por QR sin
cuenta, API REST v1, CSP estricta, RLS (ADR-026), exportación ZIP y borrado de
la organización (ADR-027), y la infraestructura de producción inactiva por
variables de entorno (ADR-020).

Para el "Hecho cuando" faltan pasos que no son código: crear las cuentas
(Render pagado con worker, R2, Sentry, correo) y seguir
`docs/07_operacion.md`; revisar el contenido de las plantillas de rubro
(marcadas como borrador); y la empresa piloto con sus datos reales.
Pendientes menores resueltos el 2026-09-27: cumplimiento con tolerancia,
completar OT y responder checklist por API, enlaces de correo con `?org=`,
preferencias según el rol, carriles por técnico en el tablero y foto del aviso
anónimo con su propio destino. Resuelto después: fotos de OT y de ítems por
API (los ítems con foto obligatoria ya se responden por API). La
organización en gracia de eliminación ya queda en solo lectura (resuelto con
ORG-13, ADR-033).

| # | Entregable | Req. | Sem. |
|---|---|---|---|
| 1 | Planes preventivos, versiones, disparadores y asignaciones | PM-01..04 | 2.5 |
| 2 | Programador automático de OT (Procrastinate), absorción de planes | PM-05 | 1.5 |
| 3 | Calendario y proyección | PM-06 | 1 |
| 4 | Backlog priorizado y tablero semanal | REQ-04/05 | 1.5 |
| 5 | KPIs básicos con tests de valor conocido | KPI-01 | 1.5 |
| 6 | Importación Excel de activos y planes; exportación Excel/PDF; manuales por activo y clase con extracción de texto por página | AST-06 CA3, AST-08, PM-08, KPI-03 | 1.5 |
| 7 | Biblioteca de plantillas (1–2 rubros) y asistente de puesta en marcha | ORG-03, PM-07 | 1.5 |
| 8 | Auditoría, notificaciones in‑app y por correo | X-01/02 | 1 |
| 9 | MFA, marca por organización, cupos y módulos por plan, aviso por QR sin cuenta | ORG-08/09/10, REQ-02 | 1 |
| 10 | Endurecimiento: RLS, exportación y borrado de organización, respaldos probados, postura de seguridad, producción | NF-*, X-03/04 | 1 |

Avance: PM-07 (biblioteca de plantillas de 4 rubros, en borrador hasta que
el dueño las revise; ADR-021) y PM-08 (importación/exportación de planes por
Excel) listos; el asistente ORG-03 reutilizará `apply_industry_template`.

**Hecho cuando:** una empresa piloto usa el sistema con sus datos reales en
producción: la carga inicial toma menos de un día y las OT preventivas se
generan solas.

## F2 · Terreno y stock (~20 semanas, el orden se decide con el piloto)

**Estado (2026-10-17):** F2 completa salvo las mejoras de UX que salgan del
piloto: PWA offline, asistente IA, inventario, mantenimiento por condición y
KPIs ampliados están hechos.

- ✔ PWA offline para técnicos (X-06, ADR-011) — hecha (2026-10): `/field/`
  con cola, sincronización, conflictos, borrado al salir, horas de apoyo y
  e2e. Queda fuera de v1: QR, repuestos y correctiva desde hallazgo sin
  conexión, ver fotos o manuales, Background Sync y push, crear/aprobar/cerrar
  OT, varias organizaciones a la vez, horas con inicio y fin, `/field/` como
  vista por defecto (se decide con el piloto) y asistente IA sin conexión.
  Después entraron los repuestos sin conexión (INV-13) y la correctiva desde
  hallazgo sin conexión (WO-06 CA2, paquete F1 de condición, 2026-10).
- ✔ Asistente de diagnóstico con IA (AI-01..05, ADR-018) — hecho (2026-10,
  paquetes A1–A3): herramientas de búsqueda con alcance, conversación en el
  worker, citas validadas, foto, nota de cierre, "¿Te sirvió?", cupos por
  plan, costo por organización y mes en el admin y demo con conversaciones
  de ejemplo. Queda fuera de v1 (ver ADR-018): asistente sin conexión o en
  `/field/`, endpoints en la API v1, voz propia, `pgvector`, índices GIN, OCR
  de manuales escaneados, respuesta token a token, varias fotos o PDF por
  pregunta y compartir conversaciones. El conjunto de evaluación se arma con
  las marcas del piloto (comparar Opus 5.5 con Sonnet 5.5).
  - Diseño aprobado el 2026-10-17: AI-01..05 afinados
    ([02_requisitos.md](02_requisitos.md) §10), modelo en
    [03_modelo_de_datos.md](03_modelo_de_datos.md) §2.12 y ADR-018
    actualizado. Claude Opus 5.5 con esfuerzo `medium` (se compara con
    Sonnet 5.5 con las marcas del piloto); módulo `assistant` solo en
    Profesional (100 conversaciones/mes) y Empresa (500); la demo trae
    conversaciones ya respondidas y, sin clave de la API, avisa que no está
    configurado.
  - Paquetes (uno a la vez, por rama). Orden: A1 → A2 → A3.

    | Paquete | Contenido | Apps | Depende de | Inicio |
    |---|---|---|---|---|
    | ✔ A1 · Docs + núcleo | Docs 02/03/04/05; `apps/assistant` (modelos, migraciones, RLS), cupo en `Plan` + migración de datos, módulo y permisos, `TargetType` de la foto, selectores de búsqueda, herramientas, `llm.py`, servicios, tarea, ajustes; tests de servicio (cliente falso), aislamiento, RLS y permisos (hecho 2026-10) | assistant, organizations, accounts (permisos), attachments | — | al inicio |
    | ✔ A2 · Pantallas | "Pedir ayuda" en la ficha del activo, por QR y en la OT; conversación (texto ≥ 16 px, foto, fuentes con ícono + texto, aviso de seguridad, consulta htmx, no configurado / cupo agotado); lista (propias; todas para el administrador) paginada; "Más"; design system; nota de cierre y "¿Te sirvió?" en el formulario de completar OT; query counts; capturas 360/1024/1440 (hecho 2026-10) | assistant (vistas), work, ui | A1 | tras A1 |
    | ✔ A3 · Seed y cierre | `seed_demo` con conversaciones de ejemplo y plan con el módulo para la organización demo; exportación y purga; vista de costo en el admin; docs 07 (variables `ANTHROPIC_API_KEY`, `AI_ASSISTANT_*`), 08 si aplica y cierre de 02/03/05; recrear la demo de Render si hace falta (hecho 2026-10) | core, organizations, docs | A2 | al final |
- ✔ Inventario: repuestos, bodegas, stock mínimo, reservas y consumos — hecho
  (2026-10, paquetes P0–P7). Queda fuera de v1: órdenes de compra,
  proveedores como entidad, lotes y series, conversión de unidades, FIFO,
  códigos de barra de repuestos, crear repuestos sin conexión e indicadores
  de inventario en `/reports/kpis/`, PDF o API.
  - Diseño aprobado el 2026-10-10: INV-01..INV-17
    ([02_requisitos.md](02_requisitos.md) §6), modelo en
    [03_modelo_de_datos.md](03_modelo_de_datos.md) §2.10 y ADR-028. Módulo
    `inventory` en los planes Prueba, Profesional y Empresa. Stock negativo
    permitido, sin órdenes de compra en v1 (solo reposición + Excel). Incluye
    los repuestos sin conexión que quedaron fuera de la PWA (INV-13).
  - Paquetes (uno por rama; no corren en paralelo dos que creen migraciones
    en la misma app):

    | Paquete | Contenido | Migraciones | Depende de | Inicio |
    |---|---|---|---|---|
    | P0 · Docs base | INV-01..17, modelo, ADR-028, roadmap | — | — | al inicio |
    | P1 · Núcleo | `apps/inventory`: modelos, servicios de stock, costo promedio, reservas, reconciliación, permisos, módulo de plan, alerta `stock_low` | inventory, organizations (datos), audit, notifications | — | al inicio |
    | P2 · Catálogo, bodegas e importación web | INV-01..INV-04 (catálogo, bodegas, stock, importación Excel/CSV) | — | P1 | tras P1 |
    | P3 · Integración con la OT | INV-09, INV-10 (manual y "Usar"), devolución al quitar, liberación al cerrar/cancelar, previstos pre-marcados en la web | work | P1 | tras P1, en paralelo con P2 y P4 |
    | P4 · Planes y reserva automática | INV-11, INV-10 CA1, "Crear catálogo desde los planes" | maintenance | P1 | tras P1, en paralelo con P2 y P3 |
    | P5 · Movimientos, reposición, alertas y Excel | INV-05..INV-07 (entrada, conteo, transferencia), INV-12, INV-14 | — | P2 | tras P2 (en paralelo con P6) |
    | P6 · API v1, API de terreno y PWA | INV-13, INV-17, docs/08, ADR-011, X-06 | — | P3 (y P4) | tras P3 y P4 |
    | P7 · Indicadores, seed y cierre de docs | INV-15, `seed_demo`, exportación y purga, cierre de docs 02–08 | — | P2–P6 | al final |
- ✔ Mantenimiento por condición: umbrales, alertas, ingesta CSV/API — hecho
  (2026-10, paquetes C0–C6 y F1, la correctiva desde hallazgo sin
  conexión). La demo (`seed_demo --reset`) trae variables, reglas, 90 días
  de lecturas y alertas en todos sus estados. Queda fuera de v1 lo listado
  en [02_requisitos.md](02_requisitos.md) §7 ("Fuera de v1"). La PWA de
  terreno muestra la variable que alimenta cada ítem de medición (F1).
  - Diseño aprobado el 2026-10-12: CBM-01..CBM-10
    ([02_requisitos.md](02_requisitos.md) §7), modelo en
    [03_modelo_de_datos.md](03_modelo_de_datos.md) §2.11 y ADR-029. Las
    variables son medidores con tipos nuevos no acumulativos (sin tablas de
    series aparte); al cruzar un umbral, alerta + OT predictiva (una abierta
    por regla y medidor; la regla puede ser "solo alertar"); los ítems de
    medición del checklist alimentan variables. Módulo `condition` en los
    planes Prueba, Profesional y Empresa. Incluye la correctiva desde
    hallazgo sin conexión (WO-06 CA2, paquete F1).
  - Paquetes (uno por rama; máx. 3 a la vez; no corren en paralelo dos que
    toquen la misma app). Orden: C0 ∥ C1 → C2 ∥ C3 ∥ C4 → C5 → C6 ∥ F1.

    | Paquete | Contenido | Apps | Depende de | Inicio |
    |---|---|---|---|---|
    | ✔ C0 · Docs base | CBM-01..10, WO-06 CA2, modelo, ADR-029, roadmap | docs | — | al inicio (∥ C1) |
    | ✔ C1 · Núcleo | `apps/condition` (modelos, reglas, evaluación, histéresis, alertas, OT predictiva, aviso `condition_alert`), tipos variable en `MeterKind` y exclusión en planes, hook en `record_meter_reading`, módulo y permisos, migraciones, tests (servicio, aislamiento, RLS, permisos) (hecho 2026-10) | condition, assets, organizations, notifications, accounts (permisos) | — | al inicio (∥ C0) |
    | ✔ C2 · Pantallas | `/condition/` (alertas, reglas, formularios), contador en el inicio, navegación y "Más", design system (CBM-03, CBM-05) (hecho 2026-10) | condition (vistas), ui | C1 | tras C1 (∥ C3, C4) |
    | ✔ C3 · Checklist → variable | CBM-06: campos en plan y OT, editor de planes, lectura al completar (hecho 2026-10) | maintenance, work | C1 | tras C1 (∥ C2, C4) |
    | ✔ C4 · Ficha del activo | CBM-01 (alta de variables), CBM-02 (CSV con tipo/nombre), CBM-07 (estado y tendencia SVG) (hecho 2026-10) | assets | C1 | tras C1 (∥ C2, C3) |
    | ✔ C5 · API v1 | CBM-09 (reglas, alertas, bulk con `kind`/`name`), docs/08 (hecho 2026-10) | api | C1–C4 | tras C2–C4 |
    | ✔ C6 · Seed y cierre | CBM-10 (`seed_demo`, exportación y purga), cierre de docs, capturas (hecho 2026-10) | core, docs | C2–C5 | al final (∥ F1) |
    | ✔ F1 · Correctiva desde hallazgo sin conexión | WO-06 CA2: endpoint de terreno, PWA, e2e, ADR-011; variable del ítem en la PWA (hecho 2026-10) | api (field), field, static/field | C5 (misma app `api`) | al final (∥ C6) |
- ✔ KPIs ampliados (KPI-02) — hecho (2026-10): `/reports/kpis/` con backlog
  en semanas en el tiempo, planificado vs. correctivo, costos por activo y
  clase, Pareto de fallas y exportación a Excel. Queda fuera: historial de
  capacidad, moneda configurable, PDF y API de estos indicadores.
- Correctiva desde hallazgo y mejoras de UX surgidas del piloto — 1 semana.
  La correctiva desde hallazgo sin conexión (WO-06 CA2) ya entró con el
  paquete F1 de mantenimiento por condición (ver arriba); quedan las mejoras
  del piloto.

## F3 · Escala comercial (~12 semanas)

- SSO SAML/OIDC (Entra ID) para el plan Empresa. *Hecho OIDC (2026-10):* un proveedor
  OIDC por organización (Entra ID, Google Workspace, Okta), dominios verificados por
  DNS TXT, «Entrar con SSO» por dominio, alta automática con rol por defecto y cupo,
  «Exigir SSO» con acceso de emergencia para administradores y SSO como factor
  fuerte frente al MFA (ORG-11, ADR-032). Quedan SAML, SCIM y single logout.
- Roles personalizados. *Hecho (2026-10):* rol base + permisos elegidos, sin
  permisos de administración, módulo `custom_roles` en Profesional y Empresa
  (ORG-12, ADR-031).
- Cobro recurrente (Stripe, Mercado Pago o Flow) y autogestión del plan. *Hecho (2026-10):* Mercado Pago
  (suscripciones en CLP), precios en UF con tramos, anual = 10 meses, cupones, UF diaria con respaldo,
  `/settings/billing/` (contratar, subir, bajar programado, cancelar, datos de facturación, historial),
  webhooks firmados e idempotentes, avisos, 7 días de gracia y solo lectura central con reactivación
  automática (ORG-13, ADR-033). Queda: SII, prorrateo, cambio de período con suscripción activa.
- Traducción a inglés y portugués.
- API ampliada, webhooks y tokens con más alcances. *Parcial (2026-10):* webhooks
  salientes v1 hechos (OT creada y completada, solicitud creada; X-08, ADR-030);
  quedan la API ampliada, más eventos y tokens con más alcances.
- Base de datos dedicada para el plan Empresa (si hay demanda).

## Riesgos del roadmap

| Riesgo | Señal | Respuesta |
|---|---|---|
| El MVP‑1 se alarga | Más de 4 semanas de atraso | Sacar el tablero semanal y el aviso QR sin cuenta del MVP‑1. |
| El piloto pide offline antes de lo previsto | Técnicos sin señal en planta | Adelantar la PWA offline y posponer los KPIs ampliados. |
| La puesta en marcha no baja de 1 hora | Medición en el piloto | Invertir en plantillas y en la importación Excel antes que en funciones nuevas. |
