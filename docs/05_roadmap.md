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
Pendientes menores: cumplimiento de preventivos con tolerancia
(`is_on_time` aún sin usar en KPIs), completar OT por API, enlaces de correo
sin `?org=`, preferencias de notificación que muestran tipos que el rol no
recibe, carriles por técnico en el tablero.

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

- PWA offline para técnicos (ADR-011) — 5 semanas.
- Asistente de diagnóstico con IA (AI-01..05, ADR-018): herramientas de
  búsqueda con alcance, conversación en el worker, citas, cupos y conjunto de
  evaluación con casos del piloto — 4 semanas.
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
