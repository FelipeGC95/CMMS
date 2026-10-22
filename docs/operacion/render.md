# Despliegue en Render (demo)

Todo el ambiente está descrito en [`render.yaml`](../../render.yaml) (un
*Blueprint*): un servidor web en el plan **Starter** y una base PostgreSQL 16
en el **plan gratuito**. Render vuelve a desplegar solo cada vez que se sube un
cambio a `main`.

El servidor web pasó a Starter el 2026-10-21 porque el plan gratis bloquea el
SMTP saliente (puertos 25, 465 y 587): ningún correo salía y cada envío vencía
por *timeout*. Con Starter el servidor tampoco se duerme.

## Qué implica el plan gratis (base de datos y lo que queda)

| Limitación | Efecto en la demo | Cómo se resuelve |
|---|---|---|
| La base gratuita vence a los 30 días | Render la elimina (avisa antes por correo) | Recrearla (ver abajo); los datos demo se cargan solos |
| Sin disco persistente | Las fotos subidas se pierden al dormir o redesplegar | Aceptable en una demo en vivo |
| Sin tareas programadas | — | Los preventivos del día se generan en cada arranque |

Al arrancar (y al despertar), el servidor ejecuta en orden: migraciones →
`seed_demo --reset-if-stale` (carga la demo si la base está vacía; si la demo
existe pero se cargó antes de inventario o condición, la recrea completa una
vez y se pierde lo creado a mano en ella) → generación de preventivos →
gunicorn.

## Antes de empezar

- El código que quieres publicar debe estar en `main` y con el CI en verde
  (<https://github.com/FelipeGC95/CMMS/actions>).
- Inventa una contraseña para los usuarios demo (mínimo 12 caracteres, sin
  reutilizar otra). Será `DEMO_PASSWORD`. **No uses la del README**: el sitio
  quedará en internet.

## Pasos

1. Crea una cuenta en <https://render.com> con **Sign in with GitHub**. No
   hace falta tarjeta.
2. En el panel: **New → Blueprint**.
3. **Connect GitHub** y da acceso al repositorio `FelipeGC95/CMMS` (puedes
   limitarlo solo a ese repositorio).
4. Elige el repositorio. Render lee `render.yaml` y muestra lo que va a crear:
   `cmms-db` y `galbust-cmms`, ambos *Free*.
5. Ponle un nombre al Blueprint (ej. `galbust-cmms-demo`) y, cuando lo pida,
   ingresa `DEMO_PASSWORD`.
6. **Apply / Deploy Blueprint.** El primer despliegue tarda 5–10 minutos.
7. Abre el servicio **galbust-cmms**: la URL aparece arriba, del tipo
   `https://galbust-cmms.onrender.com` (o con un sufijo si el nombre estaba
   tomado).
8. Entra con `jefe@demo.cl` y tu `DEMO_PASSWORD`.

## Uso habitual

- **Etiquetas QR:** imprímelas desde la URL de Render; así el celular las
  abre desde cualquier red.
- **Actualizaciones:** cada push o merge a `main` despliega solo.
- **Logs y errores:** pestaña *Logs* del servicio.
- **Reiniciar los datos demo** (por ejemplo, antes de otra presentación):
  recrea la base como se explica en la sección siguiente.

## Cuando la base gratuita vence (cada 30 días)

1. En Render abre `cmms-db` → *Settings → Delete Database*.
2. Abre el Blueprint → **Manual Sync**: Render vuelve a crear `cmms-db` y
   conecta el servicio.
3. En `galbust-cmms` → **Manual Deploy → Deploy latest commit**. Al arrancar,
   `seed_demo` recarga los datos de ejemplo.

## Pasar al plan pagado (piloto con datos reales)

Aproximadamente USD 15/mes. En `render.yaml`:

- `databases[0].plan: basic-256mb` (permanente, con recuperación a un punto
  en el tiempo).
- Servicio web `plan: starter`, un `disk` en `/var/data` con
  `MEDIA_ROOT=/var/data/media`, y mover `migrate` a `preDeployCommand`.
- Un servicio `type: cron` con `python manage.py
  generate_preventive_work_orders` todos los días.
- Quitar `seed_demo` del arranque.

Antes de cargar datos reales faltan además MFA, auditoría, respaldos
probados y archivos en R2 (MVP‑1 en [05_roadmap.md](../05_roadmap.md)).

## Dominio propio (opcional)

Servicio → *Settings → Custom Domains → Add*, por ejemplo
`cmms.galbust.cl`. Render indica el registro DNS (CNAME) que debes crear
donde administras el dominio; el certificado HTTPS es automático. Agrega el
dominio también en la variable `DJANGO_ALLOWED_HOSTS` del servicio.
