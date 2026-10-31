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
| `AUDIT_TRUSTED_PROXIES` | Proxies delante de la app: la IP del cliente (auditoría y límite de intentos de login) es `X-Forwarded-For[-N]`. `1` con Render solo; `2` con el proxy de Cloudflare (nube naranja) delante de Render. Verificar comparando la IP de un login en `/audit/` con la propia. Quien entre directo por `*.onrender.com` puede falsear su IP con `2`. | `0`: `REMOTE_ADDR`. |
| `LOG_LEVEL`, `PROCRASTINATE_LOG_LEVEL` | Nivel de logs general y de la cola (en el worker conviene `INFO`). | `INFO` / `WARNING`. |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` | SMTP del proveedor. Con `EMAIL_HOST` se usa SMTP. | Correos a la consola; STARTTLS en el puerto 587. |
| `EMAIL_USE_SSL=1` | SSL directo en el puerto 465 (el que anuncia cPanel). Apaga `EMAIL_USE_TLS` y cambia el puerto por defecto a 465. | Apagado. |
| `EMAIL_BACKEND` | Forzar otro backend (opcional). | Según `EMAIL_HOST`. |
| `DEFAULT_FROM_EMAIL`, `SERVER_EMAIL` | Remitente (dominio verificado en el proveedor). | `GALBUST CMMS <no-reply@galbust.com>`. |
| `EMAIL_USE_QUEUE=1` | Enviar correos desde el worker (reintentos). Requiere worker corriendo. | Envío sincrónico. |
| `DOCUMENT_TEXT_USE_QUEUE=1` | Extraer el texto de los manuales PDF en el worker. Requiere worker corriendo. | Extracción al subir, en la misma petición. |
| `DATA_EXPORT_USE_QUEUE=1` | Armar la exportación completa (X-03) en el worker. Requiere worker corriendo. | Se arma al pedirla, en la misma petición. |
| `WEBHOOKS_USE_QUEUE=1` | Enviar los webhooks salientes (X-08) desde el worker, con hasta 5 intentos y backoff (1 min, 5 min, 30 min, 2 h). Requiere worker corriendo. | Un solo intento al confirmar la escritura, en la misma petición (timeout 5 s); sin reintentos. |
| `BACKUPS_ENABLED=1` | Respaldo diario (07:00 UTC) desde el worker. | La tarea diaria no hace nada. |
| `BACKUP_DIR`, `BACKUP_RETENTION_DAYS` | Carpeta local de respaldos (sin R2) y retención. | `./backups`, `30`. |
| `ACCOUNT_EMAIL_VERIFICATION` | Quitarla (queda `mandatory`) al tener proveedor de correo. | `mandatory`. |
| `ANTHROPIC_API_KEY` | Clave de la API de Anthropic para el asistente de diagnóstico (AI, ADR-018). Secreto: solo en el panel de Render, nunca en el repo. | El asistente se muestra pero avisa "El asistente no está configurado" y no llama a la API (la demo). |
| `AI_ASSISTANT_MODEL` | Modelo de las conversaciones nuevas (cada conversación guarda el suyo). Debe tener precio en `AI_MODEL_PRICES_USD`. | `claude-opus-5-5`. |
| `AI_ASSISTANT_EFFORT` | Esfuerzo del modelo (`low`, `medium`, `high`…): más esfuerzo, respuestas más pensadas, más tokens y más espera. | `medium`. |
| `AI_ASSISTANT_USE_QUEUE=1` | Cada turno del asistente corre en el worker (cola `assistant`). Requiere PostgreSQL y worker corriendo, en web y worker. | El turno corre en la misma petición (la página espera la respuesta). |
| `AI_ASSISTANT_MAX_TOKENS` | Tope de tokens de salida por llamada (incluye el razonamiento). | `16000`. |
| `AI_ASSISTANT_MAX_TOOL_ROUNDS` | Rondas de herramientas por turno; pasado el límite el modelo debe responder con lo reunido. | `6`. |
| `AI_ASSISTANT_TIMEOUT_SECONDS` | Tiempo máximo de cada llamada a la API (con 2 reintentos del SDK). | `300`. |
| `AI_MODEL_PRICES_USD` | No es variable de entorno: diccionario en `config/settings.py` con los USD por millón de tokens de cada modelo (entrada, salida, lectura y escritura de caché) para la vista de costo (§3g). Se actualiza en el código al cambiar de modelo o de precio. | — |
| `SSO_SECRET_KEY` | Clave para cifrar el secreto de cliente OIDC de cada organización (ORG-11, ADR-032). Secreto largo y aleatorio, solo en el panel de Render y en web y worker. Cambiarla invalida los secretos guardados: cada administrador debe volver a escribir el suyo en `/settings/sso/` («Probar conexión» lo avisa). | Se deriva de `SECRET_KEY` (mismo efecto si se rota `SECRET_KEY`). |
| `MERCADOPAGO_ACCESS_TOKEN`, `MERCADOPAGO_WEBHOOK_SECRET` | Cobro en línea con Mercado Pago (ORG-13, ADR-033): *Access token* de producción de la aplicación y *clave secreta* de la firma de webhooks. Secretos: solo en el panel de Render, en web y worker. Las **dos** o ninguna. | Cobro en línea inactivo: la app funciona igual, `/settings/billing/` avisa que el pago en línea no está activo y el webhook responde 503. |
| `MERCADOPAGO_TEST_PAYER_EMAIL` | Solo pruebas (sandbox): correo del comprador de prueba que se envía como `payer_email`. | Se usa el correo de facturación o el de quien contrata. |
| `MERCADOPAGO_API_URL` | Base de la API (no hace falta cambiarla). | `https://api.mercadopago.com`. |
| `UF_SOURCE_URL` | Fuente del valor diario de la UF (formato de mindicador.cl: `{"serie": [{"fecha", "valor"}]}`). | `https://mindicador.cl/api/uf`. |
| `BILLING_GRACE_DAYS` | Días con escritura tras el fin de la prueba o un cobro rechazado. | `7`. |
| `BILLING_CONTACT_EMAIL` | Correo del botón «Cotizar» (Empresa) y del aviso de pago no activo. Debe ser un buzón que alguien lea. | Vacío: no se muestra correo ni botón «Cotizar». |
| `API_RATE_LIMIT`, `FIELD_API_RATE_LIMIT` | Límite de peticiones de la API v1 (por token) y de la API de terreno `/field/api/` (por persona; formato DRF, p. ej. `600/min`). Una sincronización envía un cambio por petición: subirlo si un técnico con muchos cambios recibe 429 (el teléfono reintenta solo). | `600/min` y `600/min`. |

Proveedor de correo (SMTP, ambos con plan gratuito para empezar):

- **Postmark:** `EMAIL_HOST=smtp.postmarkapp.com`, `EMAIL_PORT=587`,
  `EMAIL_HOST_USER` y `EMAIL_HOST_PASSWORD` = el *Server API token*.
- **Brevo:** `EMAIL_HOST=smtp-relay.brevo.com`, `EMAIL_PORT=587`,
  `EMAIL_HOST_USER` = login SMTP, `EMAIL_HOST_PASSWORD` = clave SMTP.
- **Buzón del hosting (cPanel, el actual):** `EMAIL_HOST=mail.galbust.com`,
  `EMAIL_USE_SSL=1` (puerto 465), `EMAIL_HOST_USER=no-reply@galbust.com`,
  `EMAIL_HOST_PASSWORD` = la clave del buzón.

El dominio del remitente es `galbust.com`: verificarlo en el proveedor (SPF,
DKIM y DMARC en el DNS) antes de enviar, o los correos rebotan o caen en spam.
`no-reply@` no necesita buzón; `BILLING_CONTACT_EMAIL` sí. En producción con
`DEBUG=0`, `manage.py check` (y cada `migrate` del arranque) avisa con
`core.W001` si no hay SMTP y `core.W002` si falta `BILLING_CONTACT_EMAIL`.

Si el servidor de correo falla (timeout, clave mala, puerto bloqueado), la app
registra el error y sigue: recuperar clave y verificar correo muestran «No
pudimos enviar el correo»; la invitación y la solicitud de registro quedan
guardadas para reenviarlas. Un *timeout* desde Render con el SMTP probado desde
otra red indica que Render bloquea el SMTP saliente (plan gratis): pasar el
servicio web a un plan pagado o enviar por la API HTTP del proveedor.

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
    (X-04, sección 3c);
  - 10:00 UTC, `billing.run_daily`: guarda la UF del día, actualiza el monto
    de cada suscripción en Mercado Pago, aplica bajas de plan programadas y
    envía los avisos de prueba e impago (ORG-13, sección 3i). Sin worker:
    `python manage.py billing_daily` desde un cron diario;
  - lunes 09:00 UTC, `inventory.enqueue_reconciliation`: una tarea por
    organización activa que compara el stock con el libro de movimientos,
    sin corregir (INV-14, sección 3e);
  - 08:30 UTC, `webhooks.enqueue_purge`: una tarea por organización que borra
    las entregas de webhook terminadas de más de 90 días (X-08, sección 3j).

  Además atiende las colas de tareas a pedido (`email`, `documents`,
  `notifications`, `assistant`…). El worker por defecto escucha todas; si los
  turnos del asistente (hasta un par de minutos cada uno) atrasan al resto, se
  levanta un segundo worker solo para él:
  `python manage.py procrastinate worker --queues=assistant` (sección 3g).
- **Migraciones:** `python manage.py migrate` en el *pre-deploy command*.
- Diagnóstico de la cola: `python manage.py procrastinate healthchecks`.

## 2b. Sin worker: tareas diarias con GitHub Actions

Mientras no se paga el worker (primera versión con la web Starter y una base
gratuita, p. ej. Neon), `.github/workflows/scheduled-jobs.yml` corre una vez al
día (10:00 UTC) los comandos equivalentes a las periódicas: respaldo, OT
preventivas, `billing_daily`, `send_due_notifications`, `purge_organizations`,
`purge_webhook_deliveries` y, los lunes, `inventory_reconcile`. Ocupa ~2 minutos
diarios (≈60 de los 2.000 mensuales). Si un paso falla, la corrida queda en rojo
y GitHub avisa por correo; los demás pasos corren igual.

Activarlo: GitHub → Settings → Secrets and variables → Actions.

- **Secretos** (los mismos valores que el servicio web de Render):
  `DATABASE_URL` (la URL **externa** y directa, no la interna de Render ni la
  *pooled* de Neon), `DJANGO_SECRET_KEY`, `SITE_URL` (p. ej.
  `https://galbust-cmms.onrender.com`, para los enlaces de los correos),
  `R2_BUCKET`, `R2_ENDPOINT_URL`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`,
  `EMAIL_HOST`, `EMAIL_USE_SSL`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` y,
  si hay cobro, `MERCADOPAGO_ACCESS_TOKEN` y `MERCADOPAGO_WEBHOOK_SECRET`;
  `SENTRY_DSN` opcional.
- **Variable** `PG_MAJOR`: versión mayor de la base (17 por defecto); se instala
  ese `pg_dump`. Restaurar con `pg_restore` de la misma versión o más nueva.
- Sin `DATABASE_URL` el workflow no hace nada; sin R2 se omite el respaldo.
- Probar: Actions → Tareas diarias → *Run workflow* (corre también la
  reconciliación de inventario).

Limitaciones frente al worker: las OT preventivas se generan una vez al día (y
en cada arranque del servicio de demo), no cada hora; los correos y webhooks
salen dentro de la petición, sin reintentos (dejar vacías las variables
`*_USE_QUEUE`). GitHub pausa las programaciones de un repositorio sin actividad
por 60 días. Al contratar el worker (§4), desactivar el workflow (Actions →
Tareas diarias → *Disable workflow*) para no duplicar avisos ni cobros.

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

## 3d. PWA de terreno sin conexión (X-06, ADR-011)

- **Versión del cliente (`MIN_CLIENT_VERSION`):** constante en
  `apps/api/framework.py` (no es variable de entorno); el cliente declara la
  suya en `CLIENT_VERSION` de `static/field/api.js`. Se sube **solo** cuando
  un cambio del contrato de `/field/api/` deja de servir a la versión anterior
  (campo obligatorio nuevo, forma distinta de una respuesta o de la cola): subir
  primero `CLIENT_VERSION` del JS y `MIN_CLIENT_VERSION` en el mismo despliegue.
  Los teléfonos con la versión vieja reciben 409 `upgrade_required`, pausan la
  cola sin perder cambios y muestran "Actualizar". Un cambio aditivo no la sube.
- **Actualización del Service Worker:** su versión cambia en cada despliegue
  (nombres con hash + `RENDER_GIT_COMMIT`); no hay que hacer nada.
- **Íconos:** `static/brand/galbust-app-icon-192.png`, `-512.png` y
  `-maskable-512.png` (exportados de `Marca/`, que no se edita; el maskable
  ocupa el cuadrado completo, con test). Si cambia la marca, exportarlos de
  nuevo con los mismos nombres.
- **Límite:** `FIELD_API_RATE_LIMIT` (sección 1).
- **Datos en el teléfono:** IndexedDB `galbust-field`; se borra al "Salir"
  (pide confirmar si hay cambios sin enviar) y los datos descargados se
  borran al cambiar de organización o si entra otra persona. Soporte: si un
  técnico "perdió" cambios, preguntar si salió con "Salir y descartar".
- **E2E (Playwright, no corre en cada push):**
  - En local (SQLite o `DATABASE_URL`):
    ```powershell
    uv run python manage.py migrate
    uv run python manage.py seed_demo --reset
    uv run python manage.py runserver 127.0.0.1:8765 --noreload
    # en otra terminal:
    $env:E2E_BASE_URL = "http://127.0.0.1:8765"
    $env:E2E_SHOTS = "C:\ruta\capturas"   # opcional
    uv run --no-project --with playwright --with pytest pytest -c tests/e2e/pytest.ini tests/e2e
    ```
    La primera vez: `uv run --no-project --with playwright playwright install chromium`.
    `seed_demo --reset` antes de cada corrida (las pruebas completan y cancelan OT).
  - En GitHub: Actions → CI → *Run workflow* (`workflow_dispatch`) corre solo
    el job `e2e` (Postgres, `seed_demo`, `runserver`, chromium) y sube las
    capturas y el log del servidor como artefacto. El botón aparece cuando el
    workflow con `workflow_dispatch` está en `main`.

## 3e. Inventario: reconciliación y stock negativo (INV-14, ADR-028)

- **Qué se revisa:** el saldo por repuesto y bodega (`StockItem`) es una caché
  del libro de movimientos (`StockMovement`, inmutable). `python manage.py
  inventory_reconcile [--organization <slug>]` verifica (a) existencia = Σ
  movimientos; (b) reservado = Σ pendientes de reservas activas; (c) cada
  consumo de OT enlazado tiene su salida con la cantidad opuesta. Sin `--fix`
  solo lista las diferencias (una línea por diferencia y el total).
- **Tarea semanal:** lunes 09:00 UTC en el worker
  (`inventory.reconcile_stock` por organización). Si hay diferencias deja un
  `WARNING` "Inventario descuadrado en <slug>: N diferencias (tipos)" en el
  log del worker (y en Sentry si su integración de logs captura advertencias;
  por defecto Sentry las guarda como migas, no como evento). Sin worker, un
  cron semanal: `python manage.py inventory_reconcile`.
- **Corregir un descuadre:** primero entender la causa (un cambio hecho a mano
  en la base, un error de código). Luego `python manage.py inventory_reconcile
  --organization <slug> --fix`: reescribe existencia y reservado desde el libro
  y deja la auditoría `stock_reconcile` con el valor anterior y el nuevo. El
  tipo (c) no se corrige solo: revisar la OT y el consumo indicados.
- **Stock negativo (no es un descuadre):** se permite a propósito (un consumo
  nunca se rechaza por falta de stock) y aparece como "Negativo ·
  regularizar" en el inicio de inventario y primero en Reposición. Se
  regulariza desde la web, no por consola: si faltó registrar una entrada,
  **Registrar entrada** con su documento y fecha real (hasta 30 días atrás);
  si el físico no coincide, **Contar** el repuesto (motivo obligatorio si
  faltan unidades o la diferencia supera el 10 %); si salió de otra bodega,
  **Transferir**. Nunca editar `StockItem` a mano.
- **Demo:** `seed_demo --reset` carga bodega principal y un pañol por sitio,
  unos 30 repuestos, planes con repuestos enlazados, reservas, consumos de los
  últimos meses (para los indicadores), un repuesto bajo mínimo y uno en
  negativo; la demo queda reconciliada. Una base demo cargada antes del
  inventario se recrea sola en Render (`seed_demo --reset-if-stale` al
  arrancar); se pierde lo creado a mano en ella.

## 3f. Mantenimiento por condición (CBM, ADR-029)

- **Sin proceso aparte:** las reglas se evalúan dentro de la transacción de
  cada lectura (`record_meter_reading`); el worker solo entrega el aviso
  `condition_alert`. Sin worker las alertas y las OT predictivas se crean
  igual (el aviso por correo sale cuando el worker vuelve).
- **Módulo por plan:** `condition` va en Prueba, Profesional y Empresa. Con
  el módulo apagado las lecturas se guardan pero no se evalúan; al
  encenderlo no se reevalúa el historial: cada regla arranca en normal con la
  próxima lectura.
- **Alerta que no se cierra:** se cierra sola al cerrar o cancelar su OT
  predictiva; si la regla quedó mal definida, desactivarla normaliza sus
  episodios abiertos. No editar `ConditionState` ni `ConditionAlert` a mano
  (el admin de Django las muestra de solo lectura).
- **Demo:** `seed_demo --reset` carga en Alimentos del Sur 15 variables
  (temperatura, vibración, presión, corriente), reglas por clase y por
  activo, 90 días de lecturas y alertas abiertas, reconocida, normalizada y
  cerrada, con OT predictivas; tarda unos segundos más. Una base demo cargada
  antes de condición se recrea sola en Render (`seed_demo --reset-if-stale`
  al arrancar); en local, `seed_demo --reset`.

## 3g. Asistente de diagnóstico con IA (AI, ADR-018)

- **Apagado hasta tener la clave:** sin `ANTHROPIC_API_KEY` el asistente se
  ve (botón "Pedir ayuda", lista y conversaciones guardadas) pero avisa que no
  está configurado; no falla ni llama a la API. Así queda la demo de Render.
- **Activar (producción):**
  1. Crear la clave en la consola de Anthropic (organización de GALBUST, con
     límite de gasto mensual) y definir `ANTHROPIC_API_KEY` en el grupo de
     variables de web y worker (`render.production.example.yaml` ya la pide,
     `sync: false`).
  2. Definir `AI_ASSISTANT_USE_QUEUE=1` en web y worker: el turno corre en el
     worker (cola `assistant`) y la página consulta cada ~2 s. Sin worker, dejarla
     en blanco: el turno corre dentro de la petición (gunicorn necesita
     `--timeout` mayor que la respuesta más lenta; con 60 s una respuesta larga
     se corta).
  3. Probar con un usuario técnico: "Pedir ayuda" en la ficha de un activo con
     historial; la respuesta debe citar OT o manuales con enlace.
  4. Revisar el costo al día siguiente en el admin de Django → "Asistente de
     diagnóstico" → "Costo del asistente".
- **Módulo y cupo por plan:** `assistant` va solo en Profesional y Empresa,
  con un cupo mensual de conversaciones nuevas por organización (100 y 500;
  `Plan.max_assistant_conversations` en el admin, en blanco = sin límite). Al
  agotarse se avisa y no se crea la conversación; el resto sigue igual.
- **Costo (AI-05 CA2):** cada respuesta guarda modelo y tokens (entrada,
  salida, leídos y escritos en caché). El admin de Django muestra al
  superadmin de plataforma (solo a él) tokens y costo estimado en USD por
  organización y mes calendario UTC de los últimos 12 meses, con los precios
  por millón de tokens de `AI_MODEL_PRICES_USD` (`config/settings.py`). Al
  cambiar de modelo o de precio se actualiza ese diccionario en el mismo
  cambio; un modelo sin precio aparece como "incompleto". La factura del
  proveedor es la referencia.
- **Turno colgado:** si el worker se cae, el mensaje queda "Pensando…" y a
  los 15 minutos pasa a error al abrir la conversación; el técnico vuelve a
  preguntar. Las tareas no se reintentan solas (cobrarían dos veces).
- **Demo:** `seed_demo` carga 3 manuales PDF con texto (envasadora, compresor
  y unidad de frío) y 4 conversaciones ya respondidas con citas reales a OT,
  manuales y medidores, en distintos estados (una abierta con nota de cierre
  sobre la correctiva abierta de la cámara 1, que se ve al completarla como
  `tecnico@demo.cl`), con tokens de ejemplo para la vista de costo. No llama
  a la API. Render no tiene disco: cada arranque repone los PDF de los
  manuales si faltan. Una base demo cargada antes del asistente se recrea sola
  en Render (`seed_demo --reset-if-stale`).

## 3h. Inicio de sesión único OIDC (ORG-11, ADR-032)

Inactivo hasta que una organización del plan Empresa lo configura en
`/settings/sso/`: sin configuración activa y dominio verificado el login, la
recuperación de contraseña y el MFA no cambian. No requiere variables nuevas
(`SSO_SECRET_KEY` es recomendable en producción, §1) ni worker: el descubrimiento,
el canje del código y las claves se piden en la misma petición (timeout 5 s, con la
defensa SSRF de los webhooks), y el registro TXT se consulta con DNS público
(`dnspython`) al pulsar «Verificar».

**Registrar la aplicación en Microsoft Entra ID** (lo hace el administrador del
cliente; la pantalla muestra los mismos datos):

1. Entra ID → *Registros de aplicaciones* → *Nuevo registro*. Tipo de cuenta:
   *Solo las cuentas de este directorio organizativo* (un inquilino).
2. *URI de redirección*: plataforma **Web**, valor
   `https://<dominio de GALBUST>/accounts/sso/callback/` (debe coincidir exacto,
   con el mismo dominio por el que entran las personas).
3. *Certificados y secretos* → *Nuevo secreto de cliente*; copiar el **valor**
   (no el ID) a «Secreto del cliente». Anotar su vencimiento: al vencer, el SSO
   falla hasta pegar uno nuevo.
4. *Permisos de API*: Microsoft Graph delegados `openid`, `email` y `profile`
   (los scopes que pide GALBUST: `openid email profile`); conceder consentimiento
   de administrador.
5. *Configuración del token* → *Agregar notificación opcional* → token de id →
   `email` (sin ella Entra no envía el correo; GALBUST lo busca también en userinfo).
6. En GALBUST: «URL del emisor» =
   `https://login.microsoftonline.com/<ID de directorio (inquilino)>/v2.0`
   (no `common` ni `organizations`: su emisor es una plantilla y se rechaza),
   «ID de cliente» = *ID de aplicación (cliente)*.

**Google Workspace:** consola de Google Cloud → credenciales → *ID de cliente de
OAuth* tipo *Aplicación web*, misma URI de redirección; emisor
`https://accounts.google.com`. **Okta:** app *OIDC – Web Application*, grant
*Authorization Code*, emisor `https://<org>.okta.com` (o el del servidor de
autorización).

**Dominios:** el administrador agrega el dominio, crea en su DNS el TXT
`_galbust-verification.<dominio>` con el valor que muestra la pantalla y pulsa
«Verificar». Un dominio verificado por otra organización no se puede agregar; si
un cliente reclama un dominio que verificó otro, se revisa a mano en el admin
(`SsoDomain`) antes de quitarlo.

**Soporte:** «Probar conexión» deja el último error a la vista (emisor distinto,
claves ilegibles, secreto que no se puede descifrar). Los rechazos en el ingreso se
muestran a la persona con su causa (state o nonce inválido, correo sin verificar,
dominio no verificado, cupo lleno, membresía desactivada). Si un cliente queda sin
poder entrar con «Exigir SSO» activo, sus administradores siguen entrando con
contraseña + MFA y pueden desactivarlo.

## 3i. Cobro con Mercado Pago (ORG-13, ADR-033)

**Activar:**

1. Cuenta de Mercado Pago Chile (vendedor, idealmente empresa) y en *Tus
   integraciones* una aplicación de tipo «Suscripciones».
2. Probar primero con las credenciales **de prueba** y usuarios de prueba
   (comprador y vendedor): `MERCADOPAGO_ACCESS_TOKEN=TEST-…` y
   `MERCADOPAGO_TEST_PAYER_EMAIL=<correo del comprador de prueba>`.
3. En la aplicación → *Webhooks* → URL de producción
   `https://<dominio>/billing/webhooks/mercadopago/`, eventos **Planes y
   suscripciones** (`subscription_preapproval`, `subscription_authorized_payment`)
   y **Pagos** (`payment`). Copiar la *clave secreta* que muestra a
   `MERCADOPAGO_WEBHOOK_SECRET`.
4. Pasar a credenciales de producción (`APP_USR-…`), quitar
   `MERCADOPAGO_TEST_PAYER_EMAIL` y redeploy de web y worker.
5. Revisar en el admin los precios de los planes (UF), crear cupones si aplica y
   marcar como **exentas** (facturación manual) las organizaciones que se cobran
   por fuera. Las del autoregistro nacen sin exención.
6. Worker corriendo (o cron de `manage.py billing_daily`): sin él no se guarda la
   UF del día ni se actualizan montos ni salen los avisos. Si la tabla de UF está
   vacía, el primer checkout la consulta en el momento; también se puede cargar a
   mano en el admin (*Valores de la UF*).

**Cómo funciona:** el administrador elige plan y período en
`/settings/billing/`; se crea la suscripción con el monto calculado aquí y se le
redirige al checkout de Mercado Pago. Mercado Pago avisa por webhook; siempre se
consulta el recurso a su API y se actualiza la organización dueña. Cada pago
aprobado extiende `paid_through`; un cobro rechazado abre 7 días de gracia y luego
solo lectura (se reactiva sola al pagar). Eventos recibidos: admin → *Eventos del
proveedor de pagos* (los con «Error» se reintentan solos). Pagos de todas las
organizaciones: admin → *Pagos* (solo lectura).

**SII:** GALBUST aún no emite boleta ni factura electrónica; Mercado Pago envía su
comprobante de cada pago. La puesta en marcha (pago único) se cobra por fuera.

## 3j. Webhooks salientes: entregas y purga (X-08, ADR-030)

- **Con worker** (`WEBHOOKS_USE_QUEUE=1`): cada entrega es una tarea
  `webhooks.deliver` (cola `webhooks`) con hasta 5 intentos. Las importaciones
  masivas (Excel de activos, CSV de lecturas, `/readings/bulk/`) encolan sus
  eventos en tareas `webhooks.dispatch_events` de hasta 500 eventos: la
  importación no espera los envíos. Sin worker se intentan solo las primeras 20
  entregas del lote; el resto queda «fallida» y el administrador puede
  reenviarla desde el detalle de la entrega.
- **Purga:** las entregas entregadas o fallidas de más de **90 días** se borran
  cada día a las 08:30 UTC (`webhooks.enqueue_purge`, una tarea
  `webhooks.purge_deliveries` por organización). Las pendientes no se tocan. Sin
  worker, programar un cron diario:
  `python manage.py purge_webhook_deliveries` (opciones `--organization <slug>`
  y `--days N`, por defecto 90). Es un registro operativo: se borra de verdad y
  no hay respaldo aparte (los respaldos diarios de la base sí lo incluyen).
- **Secreto filtrado:** el administrador usa «Regenerar secreto» en la pantalla
  del webhook; el anterior deja de valer de inmediato (avisar al dueño del
  sistema receptor antes). Queda en la auditoría sin el valor.
- **Receptor caído:** tras 5 eventos fallidos seguidos el destino se desactiva y
  se avisa a los administradores; al reactivarlo, las entregas fallidas que
  importen se reenvían a mano (mismo `event_id`: el receptor no las duplica).

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
10. **Asistente IA (opcional):** `ANTHROPIC_API_KEY` y
    `AI_ASSISTANT_USE_QUEUE=1` en web y worker; probar y revisar el costo
    (§3g).
11. **SSO (opcional):** definir `SSO_SECRET_KEY` en web y worker antes de que
    una organización guarde su configuración (§3h).
