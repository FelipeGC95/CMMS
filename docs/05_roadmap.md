# 05 — Roadmap

**Estado:** borrador v0.1 (2026-09-24)

> **Supuesto de dedicación: 15 h/semana.** Aún no está confirmado; ajústalo y
> reescalo las semanas. Las estimaciones incluyen tests y un margen de ~20 %
> para imprevistos. 1 semana = 15 h.

## Resumen

| Fase | Objetivo | Semanas | Acumulado |
|---|---|---|---|
| **F0 · Fundaciones** | Repo, CI, tenancy, auth, sistema de diseño | 4 | 4 |
| **MVP‑0 · Demo** | Activos, medidores, OT con checklist, solicitudes y fallas | 10 | 14 |
| **MVP‑1 · Piloto** | Preventivos automáticos, backlog, KPIs, puesta en marcha, producción | 14 | 28 |
| **F2 · Terreno y stock** | Offline, inventario, condición, KPIs ampliados | ~16 | ~44 |
| **F3 · Escala comercial** | SSO, roles personalizados, cobro, idiomas, API ampliada | ~12 | ~56 |

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

## F0 · Fundaciones (4 semanas)

Parte de F0 ya existe gracias a la v0.1 (tenancy, estructura de apps, tokens),
por lo que se estima en ~3 semanas.

**Avance (2026-09-24):** hechos 0.2 y 0.4 completos; de 0.1, GitHub Actions
con PostgreSQL 16 (Python 3.11 y 3.13), ruff, chequeo de migraciones y
`check --deploy`, y soporte de `DATABASE_URL`. Además: tests de permisos por
rol y de cantidad de consultas. Pendiente: uv y pre-commit (0.1), allauth y
Plan (0.3), despliegue (0.5, requiere cuentas de Render, Cloudflare y Sentry).

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

| # | Entregable | Req. | Sem. |
|---|---|---|---|
| 1 | Ubicaciones en árbol | ORG-04 | 1 |
| 2 | Invitaciones, roles, alcance por sitio, tests de permisos | ORG-05/06/07 | 1.5 |
| 3 | Clases de activo con atributos, activos, jerarquía y ficha | AST-01/02/03 | 2 |
| 4 | Estados y detenciones, QR y etiquetas PDF, adjuntos con compresión | AST-04/05/06 | 1.5 |
| 5 | Medidores y lecturas | AST-07 | 0.5 |
| 6 | OT: alta, correlativos, ciclo de vida, lista y filtros | WO-01/02/05 | 1 |
| 7 | Ejecución móvil del checklist, mano de obra, repuestos y costos | WO-03/04 | 1.5 |
| 8 | Solicitudes y registro de fallas con catálogo base | REQ-01/03 | 1 |

Si se atrasa, el ítem 4 se reduce a QR solamente (sin etiquetas PDF).

**Hecho cuando:** se puede hacer una demo completa: crear activo → avisar
falla por QR con sesión → convertirla en OT → ejecutar el checklist en el
celular → cerrarla con costos y falla registrada.

## MVP‑1 · Piloto (14 semanas)

| # | Entregable | Req. | Sem. |
|---|---|---|---|
| 1 | Planes preventivos, versiones, disparadores y asignaciones | PM-01..04 | 2.5 |
| 2 | Programador automático de OT (Procrastinate), absorción de planes | PM-05 | 1.5 |
| 3 | Calendario y proyección | PM-06 | 1 |
| 4 | Backlog priorizado y tablero semanal | REQ-04/05 | 1.5 |
| 5 | KPIs básicos con tests de valor conocido | KPI-01 | 1.5 |
| 6 | Importación Excel de activos y planes; exportación Excel/PDF | AST-08, PM-08, KPI-03 | 1.5 |
| 7 | Biblioteca de plantillas (1–2 rubros) y asistente de puesta en marcha | ORG-03, PM-07 | 1.5 |
| 8 | Auditoría, notificaciones in‑app y por correo | X-01/02 | 1 |
| 9 | MFA, marca por organización, cupos y módulos por plan, aviso por QR sin cuenta | ORG-08/09/10, REQ-02 | 1 |
| 10 | Endurecimiento: RLS, exportación y borrado de organización, respaldos probados, postura de seguridad, producción | NF-*, X-03/04 | 1 |

**Hecho cuando:** una empresa piloto usa el sistema con sus datos reales en
producción: la carga inicial toma menos de un día y las OT preventivas se
generan solas.

## F2 · Terreno y stock (~16 semanas, el orden se decide con el piloto)

- PWA offline para técnicos (ADR-011) — 5 semanas.
- Inventario: repuestos, bodegas, stock mínimo, reservas y consumos — 5 semanas.
- Mantenimiento por condición: umbrales, alertas, ingesta CSV/API — 3 semanas.
- KPIs ampliados, Pareto de fallas y costos por activo — 2 semanas.
- Correctiva desde hallazgo y mejoras de UX surgidas del piloto — 1 semana.

## F3 · Escala comercial (~12 semanas)

- SSO SAML/OIDC (Entra ID) para el plan Empresa.
- Roles personalizados.
- Cobro recurrente (Stripe, Mercado Pago o Flow) y autogestión del plan.
- Traducción a inglés y portugués.
- API ampliada, webhooks y tokens con más alcances.
- Base de datos dedicada para el plan Empresa (si hay demanda).

## Riesgos del roadmap

| Riesgo | Señal | Respuesta |
|---|---|---|
| El MVP‑1 se alarga | Más de 4 semanas de atraso | Sacar el tablero semanal y el aviso QR sin cuenta del MVP‑1. |
| El piloto pide offline antes de lo previsto | Técnicos sin señal en planta | Adelantar la PWA offline y posponer los KPIs ampliados. |
| La puesta en marcha no baja de 1 hora | Medición en el piloto | Invertir en plantillas y en la importación Excel antes que en funciones nuevas. |
