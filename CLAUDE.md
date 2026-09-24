# GALBUST CMMS — convenciones del proyecto

CMMS SaaS multi-empresa para pymes productivas. Principio rector: **el flujo
por defecto es el automático y simple**; cada funcionalidad debe reducir
pasos al cliente, no agregarlos. Una sola persona desarrolla y opera: se
prefiere lo aburrido y gestionado.

Documentación de diseño en `docs/` (leer antes de cambios grandes):
01 visión · 02 requisitos (IDs `MOD-NN`) · 03 modelo de datos · 04 arquitectura
y ADR · 05 roadmap · 06 sistema de diseño. Si una decisión contradice un ADR,
se actualiza el ADR en el mismo cambio.

## Stack

Objetivo: Python 3.13 · Django 5.2 LTS · PostgreSQL 16+ · htmx + Alpine.js ·
DRF + drf-spectacular · django-allauth · Procrastinate · django-storages (R2) ·
uv · ruff · pytest. Sin Node, Redis, Celery ni frameworks CSS.

**Estado actual (v0.1 demo):** Python 3.11 + venv/pip, SQLite local, htmx
(sin Alpine aún), login de Django. Postgres, allauth, DRF, Procrastinate y R2
entran en F0 (ver docs/05_roadmap.md). El código no debe depender de SQLite.

## Comandos

```powershell
python -m venv .venv; .venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed_demo --reset     # datos de demostración
.venv\Scripts\python manage.py runserver 0.0.0.0:8000
.venv\Scripts\python manage.py generate_preventive_work_orders
.venv\Scripts\python -m pytest
.venv\Scriptsuff check . ; .venv\Scriptsuff format .
.venv\Scripts\python manage.py makemigrations --check --dry-run
```

## Idioma

- **Código en inglés** (modelos, campos, funciones, URLs, commits).
- **Interfaz en español (es-CL)** siempre mediante `gettext` / `{% translate %}`.
  Nunca texto visible sin marcar. El español es el idioma fuente de las
  cadenas; inglés y portugués se agregan como traducciones en F3.
- Documentación y comentarios de dominio en español.

Glosario: activo = `asset` · clase de activo = `asset_class` · ubicación/sitio
= `location` (kind `site`) · medidor = `meter` · lectura = `meter_reading` ·
plan preventivo = `maintenance_plan` · OT = `work_order` · solicitud =
`work_request` · falla = `failure_event` · organización = `organization`
(tenant) · membresía = `membership` · técnico = rol `technician`.

## Multi-tenancy (reglas no negociables)

- Todo modelo de negocio hereda de `core.models.TenantModel` (UUIDv7 +
  `organization` + timestamps), **incluidas las tablas hijas**.
- El manager por defecto filtra por la organización del contexto y lanza
  `TenantNotSet` si no hay contexto. `all_tenants` solo en comandos de
  plataforma, justificado con un comentario.
- Las tareas en segundo plano reciben `organization_id` y usan
  `with tenant_context(org):`.
- Cada modelo, vista y endpoint nuevo requiere un test en `tests/isolation/`
  (la organización A no ve, edita ni referencia datos de B).
- Índices compuestos empiezan por `organization_id`; unicidades como
  `UNIQUE(organization_id, code)`.
- Archivos bajo `org/<organization_id>/…`, servidos solo con URL firmada.

## Capas

- `views.py` / serializers: delgados; validan la entrada y llaman a una
  función de servicio o selector.
- `selectors.py`: lecturas; reciben el contexto (`membership`), aplican
  permisos y alcance por sitio y evitan N+1 (`select_related`/`prefetch_related`).
- `services.py`: escrituras y reglas de negocio; argumentos por nombre
  (`*`), `transaction.atomic`, registran auditoría, encolan tareas con
  `transaction.on_commit`.
- `tasks.py`: envoltorios delgados sobre servicios.
- Dependencias: vista → servicio → selector → modelo. Los modelos no llaman
  servicios. La API y la web usan los mismos servicios.
- Escrituras que llegan desde móvil o API aceptan `client_uuid` y son
  idempotentes.

## Datos

- Fechas en UTC (`timestamptz`); se muestran en la zona del sitio o de la
  organización.
- Sin borrado físico de datos con historial: `is_active`, estado de baja o
  `cancelled`.
- Las versiones de planes publicadas son inmutables; las OT guardan un
  snapshot del checklist.
- Correlativos visibles (OT, solicitudes) con `work_order_sequence` y
  `select_for_update`.
- Listas siempre paginadas.

## Frontend y diseño

- Colores, fuentes, radios, espaciados y sombras **solo** mediante tokens de
  `static/css/tokens.css`. Nada de hex en plantillas ni en `components.css`.
- Botón primario: fondo cian #12B3C8 con texto #0B2E4F; **nunca texto blanco
  sobre cian**; el cian nunca es texto sobre fondo claro (usar
  `--color-accent-text` #0E7C8C).
- Logo: versión negativa sobre fondos oscuros, isotipo en móvil y favicon,
  respetar el área de resguardo y los tamaños mínimos; el cian nunca sobre el
  texto "GALBUST". La carpeta `Marca/` es la fuente original y no se edita;
  `static/brand/` es una copia.
- Estados siempre con ícono + texto. Vistas de técnico: objetivos táctiles
  ≥ 48 px y texto ≥ 16 px.
- Todo componente nuevo se agrega a `/design-system/`; toda combinación de
  colores nueva se agrega a `CONTRAST_PAIRS` en `tests/ui/test_contrast.py`.
- Sin JS ni estilos en línea (CSP estricta).

## Tests

- pytest + factory_boy; `tests/isolation/`, `tests/permissions/` y los tests
  de cada app.
- Tests de servicios con base de datos real (Postgres), no mocks del ORM.
- KPIs probados con fixtures de resultado conocido.
- Consultas acotadas con `django_assert_max_num_queries` en listas.
- No se hace merge con CI en rojo.

## Git

- Commits en inglés, en imperativo, pequeños y con tests.
- Commits con la identidad de GitHub `FelipeGC95`.
- Nunca commitear `.env`, credenciales ni datos de clientes.
