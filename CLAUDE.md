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

**Estado actual:** Python 3.11+ con uv (`uv.lock`; `requirements*.txt` se
generan desde el lock para Render, no se editan a mano), pre-commit, htmx
(sin Alpine aún), django-allauth (login por correo con verificación; registro
cerrado; usuarios nuevos con `accounts.services.create_user`). Base de datos
por `DATABASE_URL` (SQLite local si no está definida; el CI corre sobre
PostgreSQL 16, así que el código no debe depender de SQLite). MFA, DRF,
Procrastinate y R2 entran en MVP (docs/05_roadmap.md).

## Comandos

```powershell
uv sync; uv run pre-commit install                   # una vez
uv run python manage.py migrate
uv run python manage.py seed_demo --reset             # datos de demostración
uv run python manage.py runserver 0.0.0.0:8000
uv run python manage.py generate_preventive_work_orders
uv run pytest
uv run ruff check . ; uv run ruff format .
uv run python manage.py makemigrations --check --dry-run
uv add <paquete>  /  uv add --dev <paquete>            # dependencias (pre-commit regenera requirements*.txt)
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
- **Móvil completo (ADR-017):** toda funcionalidad, de cualquier rol, se
  completa desde un teléfono de 360 px sin scroll horizontal; probar cada
  vista a 360 px. Cortes solo en 720 px y 1100 px. Toda `<td>` de una
  `.table` lleva `data-label`. Todo destino del menú de escritorio tiene
  acceso en la navegación móvil.
- Todo componente nuevo se agrega a `/design-system/`; toda combinación de
  colores nueva se agrega a `CONTRAST_PAIRS` en `tests/ui/test_contrast.py`.
- Sin JS ni estilos en línea (CSP estricta).

## Tests

- pytest; `tests/isolation/`, `tests/permissions/`, `tests/ui/` y
  `tests/test_query_counts.py` (toda lista nueva se agrega ahí).
- Tests de servicios con base de datos real (Postgres), no mocks del ORM.
- KPIs probados con fixtures de resultado conocido.
- Consultas acotadas con `django_assert_max_num_queries` en listas.
- No se hace merge con CI en rojo (`.github/workflows/ci.yml`).

## Git

- Commits en inglés, en imperativo, pequeños y con tests.
- Commits con la identidad de GitHub `FelipeGC95`.
- Nunca commitear `.env`, credenciales ni datos de clientes.

## Trabajo en paralelo

- Una sesión orquesta; el trabajo lo hacen subagentes en segundo plano, cada
  uno en su propio worktree (`isolation: "worktree"`). Nunca dos agentes en el
  mismo directorio o rama.
- Cada tarea sale del `main` actual (`feat/<tema>`, `fix/<tema>`) y entra a
  `main` apenas está verificada. No apilar ramas.
- No correr en paralelo dos tareas que toquen la misma app o creen migraciones
  en la misma app; la segunda se inicia después de integrar la primera.
- Los trabajadores no hacen merge a `main`; el orquestador verifica, actualiza
  la rama con `main` si hace falta y la integra.

## Definición de terminado

Una tarea está terminada solo cuando:

1. Cumple los criterios de aceptación del requisito (`docs/02_requisitos.md`)
   y lo que no se hizo queda dicho explícitamente.
2. Tiene tests de servicio, aislamiento (`tests/isolation/`), permisos y, si
   hay lista nueva, `tests/test_query_counts.py`.
3. Pasan `pytest`, `ruff check`, `ruff format --check` y
   `makemigrations --check --dry-run`, con la rama actualizada con `main`.
4. Si toca la interfaz: textos con `gettext`, `data-label` en tablas, enlaces
   nuevos también en "Más" (`/menu/`), y capturas revisadas a 360, 1024 y
   1440 px (los tests no detectan superposiciones).
5. Está en su rama remota; tras integrarla, el despliegue en Render responde
   `/healthz` y las rutas nuevas.
