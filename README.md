# GALBUST CMMS

CMMS SaaS multi-empresa para pymes productivas: avisos de falla por QR,
órdenes de trabajo con checklist móvil y preventivos automáticos.

Documentación de diseño en [docs/](docs/) · convenciones en [CLAUDE.md](CLAUDE.md).

**Estado:** versión 0.1 de demostración (local, SQLite). Ver
[docs/05_roadmap.md](docs/05_roadmap.md).

## Levantar la demo (Windows, PowerShell)

Requisitos: Python 3.11 o superior.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py seed_demo          # --reset para recrear desde cero
.venv\Scripts\python manage.py runserver 0.0.0.0:8000
```

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
.venv\Scripts\python manage.py generate_preventive_work_orders
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
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
```

La suite incluye aislamiento entre organizaciones (`tests/isolation/`),
permisos por rol, reglas de OT y preventivos, KPIs con valores conocidos,
consultas sin N+1 y contraste WCAG AA de los tokens de diseño. GitHub Actions
la corre en cada push contra PostgreSQL 16.
