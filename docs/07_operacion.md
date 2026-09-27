# 07 · Operación de producción (runbook)

La infraestructura de producción ya está en el código, pero **apagada**: cada
pieza se activa sola al definir sus variables de entorno. La demo en Render
(`render.yaml`, plan gratis) no define ninguna, así que sigue igual que antes;
lo único nuevo que expone es `/readyz`. Decisión en ADR-020
(`docs/04_arquitectura.md`).

## 1. Variables de entorno

| Variable | Para qué | Sin definir |
|---|---|---|
| `DATABASE_URL` | PostgreSQL. Con Postgres se instala la cola Procrastinate (tablas `procrastinate_*` vía `migrate`). | SQLite local, sin cola. |
| `R2_BUCKET`, `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | Archivos en Cloudflare R2 (las **cuatro** o ninguna). Descargas por URL firmada de 5 minutos tras verificar permisos. | Disco local (`MEDIA_ROOT`). |
| `SENTRY_DSN` | Errores a Sentry, sin datos personales. | Sentry apagado. |
| `SENTRY_ENVIRONMENT` | `production`, `staging`… | `production`. |
| `SENTRY_TRACES_SAMPLE_RATE` | Muestreo de rendimiento (0–1). | `0`. |
| `SENTRY_RELEASE` | Versión; en Render se toma `RENDER_GIT_COMMIT`. | — |
| `LOG_FORMAT=json` | Logs JSON por línea con `request_id` (cabecera `X-Request-ID`). | Texto plano. |
| `LOG_LEVEL`, `PROCRASTINATE_LOG_LEVEL` | Nivel de logs general y de la cola (en el worker conviene `INFO`). | `INFO` / `WARNING`. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | SMTP del proveedor. Con `EMAIL_HOST` se usa SMTP. | Correos a la consola. |
| `EMAIL_BACKEND` | Forzar otro backend (opcional). | Según `EMAIL_HOST`. |
| `DEFAULT_FROM_EMAIL`, `SERVER_EMAIL` | Remitente (dominio verificado en el proveedor). | `GALBUST CMMS <no-reply@galbust.cl>`. |
| `EMAIL_USE_QUEUE=1` | Enviar correos desde el worker (reintentos). Requiere worker corriendo. | Envío sincrónico. |
| `DOCUMENT_TEXT_USE_QUEUE=1` | Extraer el texto de los manuales PDF en el worker. Requiere worker corriendo. | Extracción al subir, en la misma petición. |
| `DATA_EXPORT_USE_QUEUE=1` | Armar la exportación completa (X-03) en el worker. Requiere worker corriendo. | Se arma al pedirla, en la misma petición. |
| `BACKUPS_ENABLED=1` | Respaldo diario (07:00 UTC) desde el worker. | La tarea diaria no hace nada. |
| `BACKUP_DIR`, `BACKUP_RETENTION_DAYS` | Carpeta local de respaldos (sin R2) y retención. | `./backups`, `30`. |
| `ACCOUNT_EMAIL_VERIFICATION` | Quitarla (queda `mandatory`) al tener proveedor de correo. | `mandatory`. |

Proveedor de correo (SMTP, ambos con plan gratuito para empezar):

- **Postmark:** `EMAIL_HOST=smtp.postmarkapp.com`, `EMAIL_PORT=587`,
  `EMAIL_HOST_USER` y `EMAIL_HOST_PASSWORD` = el *Server API token*.
- **Brevo:** `EMAIL_HOST=smtp-relay.brevo.com`, `EMAIL_PORT=587`,
  `EMAIL_HOST_USER` = login SMTP, `EMAIL_HOST_PASSWORD` = clave SMTP.

## 2. Procesos

- **Web:** `gunicorn config.wsgi:application`. Salud: `/healthz` (vida) y
  `/readyz` (base y tablas de la cola; 503 si fallan).
- **Worker:** `python manage.py procrastinate worker` (necesita PostgreSQL).
  Ejecuta las tareas y las periódicas:
  - cada hora, `maintenance.enqueue_preventive_generation`: una tarea por
    organización activa que genera sus OT preventivas (PM-05 CA1) con
    `tenant_context`;
  - 06:30 UTC, limpieza de trabajos terminados (> 7 días);
  - 07:00 UTC, respaldo (solo con `BACKUPS_ENABLED=1`);
  - 08:00 UTC, `organizations.purge_organizations`: borra los ZIP de
    exportación vencidos y purga las organizaciones cuyo borrado venció
    (X-04, sección 3c).
- **Migraciones:** `python manage.py migrate` en el *pre-deploy command*.
- Diagnóstico de la cola: `python manage.py procrastinate healthchecks`.

## 3. Respaldos (NF-09)

- Además de los respaldos gestionados de Render Postgres, `python manage.py
  backup_database` corre `pg_dump --format=custom` y guarda
  `backups/cmms-AAAAMMDDTHHMMSSZ.dump` en el bucket R2 (o en `BACKUP_DIR`
  sin R2). Borra los de más de `BACKUP_RETENTION_DAYS` días. Los respaldos
  nunca van bajo `org/`.
- El `pg_dump` debe ser de la misma versión mayor que el servidor (16) o más
  nueva. Verificar `pg_dump --version` en la shell del worker de Render; si no
  está o es más antiguo, correr el worker con una imagen Docker que lo incluya.

### Restaurar

1. Descargar el respaldo: panel de R2 → bucket → `backups/` (o con
   `aws s3 cp s3://<bucket>/backups/<archivo>.dump . --endpoint-url
   <R2_ENDPOINT_URL>`).
2. Crear una base vacía (en staging para la prueba trimestral; en producción
   una base nueva, nunca sobre la actual).
3. Restaurar:
   `pg_restore --no-owner --no-acl --clean --if-exists -d "<DATABASE_URL destino>" <archivo>.dump`
   (las políticas RLS se crean después de cargar los datos; si se restaura
   sobre tablas que ya tienen RLS, anteponer `PGOPTIONS="-c app.bypass_rls=on"`).
4. Apuntar `DATABASE_URL` del servicio a la base restaurada (o copiar los
   datos), correr `python manage.py migrate` y verificar `/readyz`, el login y
   una OT.
5. Registrar la prueba (fecha, respaldo usado, tiempo, resultado) en
   `docs/operacion/`.

## 3b. Row-Level Security (NF-07, ADR-026)

- **Rol de base:** la aplicación (web y worker) se conecta con un rol **sin
  `SUPERUSER` ni `BYPASSRLS`**; si no, PostgreSQL ignora las políticas. El rol
  dueño de las tablas sirve (las políticas usan `FORCE`). En Render el usuario
  de la base no es superusuario. En local o en otro proveedor:
  `CREATE ROLE cmms_app LOGIN PASSWORD '…' NOSUPERUSER NOBYPASSRLS;` y
  `ALTER DATABASE cmms OWNER TO cmms_app;` (el CI hace lo mismo).
- **Instalación:** automática en cada `migrate` (receptor `post_migrate`); no
  hay que escribir migraciones para tablas nuevas de `TenantModel`.
- **Verificar:** `python manage.py rls_status` (sale con error si falta una
  política y advierte si el rol ignora RLS; `--apply` las reaplica). En SQL:
  `SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE
  relname = 'assets_asset';` y `SELECT tablename, policyname FROM pg_policies;`.
  Prueba rápida en `psql` con el rol de la app: `SELECT count(*) FROM
  assets_asset;` debe dar 0 sin fijar `app.current_organization`.
- **Consultas de plataforma a mano** (soporte, métricas): `SET app.bypass_rls =
  'on';` en la sesión de `psql`, o `SET app.current_organization = '<uuid>';`
  para ver una sola organización. En código: `platform_context()`.
- **Respaldos:** `backup_database` ya corre `pg_dump --enable-row-security`
  con el bypass; un `pg_dump` manual necesita lo mismo
  (`PGOPTIONS="-c app.bypass_rls=on" pg_dump --enable-row-security …`).

## 3c. Exportación y borrado de organizaciones (X-03, X-04, ADR-027)

- **Exportación:** el administrador la pide en Configuración → "Exportar
  todos los datos". El ZIP queda en `org/<id>/exports/` y vence a los 7 días.
  Sin worker se arma en la misma petición.
- **Borrado:** el administrador lo pide en Configuración → "Eliminar la
  organización" (contraseña + nombre). Queda `pending_deletion` 30 días; se
  sigue usando con un aviso.
- **Cancelar:** cualquier administrador de la organización, con el botón del
  aviso. Soporte, sin administrador disponible: `manage.py shell` →
  `services.cancel_organization_deletion(membership=<membresía admin>)` dentro
  de `tenant_context(org)` (o en el admin de Django: estado `active` y vaciar
  `deletion_requested_at`, `deletion_scheduled_for` y `deletion_requested_by`).
- **Purga:** diaria a las 08:00 UTC en el worker. Sin worker, un cron diario:
  `python manage.py purge_organizations` (`--dry-run` lista las vencidas sin
  tocarlas). Es idempotente. Tras la purga queda la lápida (`status=deleted`,
  `deleted_at`, `purge_summary`) y una línea de log "Organización … purgada".
- **Respaldos:** la purga no toca los respaldos. Los datos borrados siguen en
  los respaldos de Render y en `backups/` hasta que vencen
  (`BACKUP_RETENTION_DAYS`, 30 días por defecto; los de Render según su plan).
  Así se informa al cliente: borrado definitivo a los 30 días de gracia más
  la retención de respaldos. Restaurar un respaldo anterior a la purga
  revive la organización: tras restaurar, volver a correr
  `purge_organizations`.
- **Usuarios:** las cuentas no se borran; quien solo pertenecía a esa
  organización entra y ve "sin organización". Borrar la cuenta es un pedido
  aparte (admin de Django).

## 4. Checklist para activar producción

Cuando existan las cuentas, en este orden:

1. **Render (pagado):** crear el Blueprint desde
   `render.production.example.yaml` (web + worker + Postgres con respaldos) o
   ajustar los servicios a mano. Definir `DJANGO_ALLOWED_HOSTS` con el dominio.
2. **Base:** confirmar que `migrate` creó las tablas `procrastinate_*`
   (`/readyz` responde `ok`).
3. **Worker:** levantarlo con `PROCRASTINATE_LOG_LEVEL=INFO`; ver en los logs
   la tarea horaria de preventivos. Quitar `generate_preventive_work_orders`
   del arranque web (ya lo hace el worker).
4. **R2:** crear el bucket privado (sin acceso público), un token de API con
   permiso de lectura/escritura solo a ese bucket y definir las cuatro `R2_*`
   en web y worker. Subir un adjunto y abrirlo: debe redirigir a una URL de
   `*.r2.cloudflarestorage.com` que vence en 5 minutos.
5. **Correo:** verificar el dominio (SPF, DKIM, DMARC) en Postmark o Brevo,
   definir `EMAIL_*` y `DEFAULT_FROM_EMAIL`, quitar
   `ACCOUNT_EMAIL_VERIFICATION=optional`. Probar una invitación. Opcional:
   `EMAIL_USE_QUEUE=1` en web y worker.
6. **Sentry:** crear el proyecto Django, definir `SENTRY_DSN` y
   `SENTRY_ENVIRONMENT` en web y worker; provocar un error de prueba.
7. **Logs:** `LOG_FORMAT=json` en web y worker.
8. **Respaldos:** `BACKUPS_ENABLED=1` en el worker; al día siguiente, ver el
   archivo en `backups/` y hacer la primera prueba de restauración (§3).
9. **Health check** de Render en `/readyz` para el web (o mantener
   `/healthz` si se prefiere no reiniciar ante caídas breves de la base).
