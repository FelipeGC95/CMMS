# GALBUST CMMS

CMMS SaaS multi-empresa para pymes productivas: avisos de falla por QR,
órdenes de trabajo con checklist móvil y preventivos automáticos.

Documentación de diseño en [docs/](docs/) · convenciones en [CLAUDE.md](CLAUDE.md).

**Estado:** versión 0.1 de demostración (local, SQLite). Ver
[docs/05_roadmap.md](docs/05_roadmap.md).

## Levantar la demo (Windows, PowerShell)

Requisitos: [uv](https://docs.astral.sh/uv/)
(`winget install astral-sh.uv`); uv descarga Python si hace falta.

```powershell
uv sync
uv run python manage.py migrate
uv run python manage.py seed_demo          # --reset para recrear desde cero
uv run python manage.py runserver 0.0.0.0:8000
```

Sin uv también funciona con pip: `python -m venv .venv` y
`.venv\Scripts\python -m pip install -r requirements-dev.txt` (archivo
generado desde `uv.lock`, mismas versiones).

Abre <http://localhost:8000>. Contraseña de todos los usuarios demo:
`demo-galbust-2026`.

| Usuario | Rol | Para mostrar |
|---|---|---|
| `jefe@demo.cl` | Administrador (tiene 2 organizaciones) | Tablero, KPIs, backlog, configuración, cambio de organización |
| `tecnico@demo.cl` | Técnico | "Mis OT", ejecutar un checklist en el celular |
| `operador@demo.cl` | Solo lectura | Avisar una falla por QR |
| `plataforma@galbust.cl` | Superadmin GALBUST | Alta de organizaciones y usuarios en `/admin/` |

### Usarla desde el celular

1. El celular y el PC deben estar en la misma red Wi‑Fi.
2. Averigua la IP del PC (`ipconfig`, por ejemplo `192.168.1.20`) y permite
   Python en el Firewall de Windows si lo pregunta.
3. En el celular abre `http://192.168.1.20:8000`.
4. **Imprime las etiquetas QR entrando por la IP, no por `localhost`**: el QR
   guarda la dirección con la que se generó la página.

### Generar los preventivos del día

```powershell
uv run python manage.py generate_preventive_work_orders
```

En producción lo ejecutará una tarea programada diaria.

## Guion sugerido (10 minutos)

1. **El problema** (1 min): planillas, preventivos olvidados, fallas que no
   quedan registradas.
2. **Tablero** como `jefe@demo.cl` (2 min): disponibilidad, MTBF, MTTR y
   cumplimiento calculados solos; hay un equipo crítico detenido y una OT
   vencida.
3. **Aviso de falla por QR** como `operador@demo.cl`, desde el celular (2 min):
   escanear la etiqueta → "¿Qué pasó?" → foto → enviar. El equipo queda
   detenido y se crea la OT correctiva con prioridad automática.
4. **Ejecución en terreno** como `tecnico@demo.cl` (3 min): "Mis OT" → abrir un
   preventivo → marcar OK / No OK con botones grandes, ingresar una medición
   fuera de rango (queda en rojo sola) → completar con horas trabajadas. El
   plan se reprograma solo.
5. **Cierre** (2 min): de vuelta en el tablero, el equipo vuelve a operativo y
   los indicadores se actualizan. Configuración en `/admin/` y el paso
   siguiente (piloto con sus datos, carga asistida desde Excel).

## Publicar en internet

Ver [docs/operacion/render.md](docs/operacion/render.md): el ambiente de
demostración se crea gratis en Render desde [`render.yaml`](render.yaml).

## Usuarios y correo

El acceso usa django-allauth: se entra con correo y contraseña, y el correo
debe estar verificado (los usuarios demo ya lo están). No hay registro
público; los usuarios se crean en `/admin/` como superadmin y al primer
ingreso reciben el enlace de verificación. En local los correos se imprimen
en la consola de `runserver`; en producción se configura SMTP con
`EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` y
`DEFAULT_FROM_EMAIL`. `ACCOUNT_EMAIL_VERIFICATION=optional` permite entrar
sin verificar mientras no haya correo configurado.

## Base de datos

Sin configuración usa SQLite (`db.sqlite3`). Para PostgreSQL define
`DATABASE_URL`, por ejemplo
`postgres://usuario:clave@localhost:5432/cmms`.

## Sistema de diseño

Con sesión iniciada, `/design-system/` muestra todos los tokens y
componentes. El botón de tema de la barra superior alterna Sistema / Claro /
Oscuro.

## Tests y calidad

```powershell
uv run pre-commit install      # una vez: ruff, lock y migraciones en cada commit
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

La suite incluye aislamiento entre organizaciones (`tests/isolation/`),
permisos por rol, reglas de OT y preventivos, KPIs con valores conocidos,
consultas sin N+1 y contraste WCAG AA de los tokens de diseño. GitHub Actions
la corre en cada PR hacia `main` con código, contra PostgreSQL 16 (ver docs/04, Pipeline).
