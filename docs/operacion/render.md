# Despliegue en Render (ambiente de demostración)

Todo el ambiente está descrito en [`render.yaml`](../../render.yaml) (un
*Blueprint*): servidor web, base PostgreSQL 16, disco para fotos y tarea
diaria de preventivos. Render lo crea con unos pocos clics y vuelve a
desplegar solo cada vez que se sube un cambio a `main`.

**Costo aproximado:** ~USD 15/mes (web starter ~7, Postgres basic-256mb ~6,
disco 1 GB ~0,25, cron ~1). Verifica los precios en la pantalla de
confirmación antes de aceptar.

## Antes de empezar

- El código que quieres publicar debe estar en `main` y con el CI en verde
  (<https://github.com/FelipeGC95/CMMS/actions>).
- Inventa una contraseña para los usuarios demo (mínimo 12 caracteres, sin
  reutilizar otra). Será `DEMO_PASSWORD`. **No uses la del README**: el sitio
  quedará en internet.

## Pasos

1. Crea una cuenta en <https://render.com> con **Sign in with GitHub**.
2. Agrega una tarjeta de pago en *Account Settings → Billing* (los planes
   usados son pagados).
3. En el panel: **New → Blueprint**.
4. **Connect GitHub** y da acceso al repositorio `FelipeGC95/CMMS` (puedes
   limitarlo solo a ese repositorio).
5. Elige el repositorio. Render lee `render.yaml` y muestra lo que va a crear:
   `cmms-db`, `galbust-cmms`, `galbust-cmms-preventivos` y el grupo
   `cmms-shared`.
6. Ponle un nombre al Blueprint (ej. `galbust-cmms-demo`) y, cuando lo pida,
   ingresa `DEMO_PASSWORD`.
7. **Apply / Deploy Blueprint.** El primer despliegue tarda 5–10 minutos:
   crea la base, instala dependencias, aplica migraciones y carga los datos
   demo.
8. Abre el servicio **galbust-cmms**: la URL aparece arriba, del tipo
   `https://galbust-cmms.onrender.com` (o con un sufijo si el nombre estaba
   tomado).
9. Entra con `jefe@demo.cl` y tu `DEMO_PASSWORD`.

## Después del primer despliegue

- **Etiquetas QR:** imprímelas desde la URL de Render; así el celular las
  abre desde cualquier red, sin depender del Wi‑Fi del PC.
- **Actualizaciones:** cada push o merge a `main` despliega solo. Las
  migraciones corren antes de cambiar la versión (`preDeployCommand`).
- **Reiniciar los datos demo:** en el servicio → *Shell* ejecuta
  `python manage.py seed_demo --reset`. ⚠️ Borra **toda** la base.
- **Preventivos:** el cron `galbust-cmms-preventivos` corre todos los días a
  las 09:00 UTC; para probarlo ahora usa *Trigger Run*.
- **Logs y errores:** pestaña *Logs* de cada servicio.
- **Respaldos:** Postgres pagado incluye recuperación a un punto en el tiempo
  (ver pestaña *Recovery* de `cmms-db`).

## Dominio propio (opcional)

Servicio → *Settings → Custom Domains → Add*, por ejemplo
`cmms.galbust.cl`. Render indica el registro DNS (CNAME) que debes crear
donde administras el dominio; el certificado HTTPS es automático. Agrega el
dominio también en la variable `DJANGO_ALLOWED_HOSTS` del servicio.

## Limitaciones de este ambiente

- Es un ambiente de demostración: usuarios y datos ficticios, sin MFA ni
  respaldos probados. No cargues datos reales de clientes todavía (ver
  MVP‑1 en [05_roadmap.md](../05_roadmap.md)).
- Las fotos van a un disco de Render (1 GB). En el MVP pasan a Cloudflare R2
  (ADR-014).
- El disco impide despliegues sin corte: cada deploy tiene unos segundos de
  indisponibilidad.
