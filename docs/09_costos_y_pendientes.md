# 09 · Costos de operación y pendientes abiertos

Documento vivo para discutir entre socios y cerrar en cada iteración. Cada
decisión abierta tiene un ID (`D-NN`) y cada pendiente uno (`P-NN`); al
cerrarlos se marca **Cerrado** con la fecha y la decisión, y si cambia el
código se enlaza el PR.

Fuentes: estudio de mercado y monetización (2026-10-14), `docs/04_arquitectura.md`
(costo de infraestructura) y la configuración actual (`config/settings.py`).
Supuestos: UF ≈ $41.000 CLP y USD 1 ≈ $950 CLP. Son precios de lista
aproximados: **confirmarlos al contratar**.

Última revisión: 2026-10-22.

## 1. Infraestructura (USD al mes)

| Ítem | Hoy (demo) | Etapa 1 (1–10 clientes) | Etapa 2 (10–50) | Etapa 3 (50–200) |
|---|---|---|---|---|
| Render: servidor web | 0 (plan gratis) | 7 | 25 | 85 |
| Render: worker de tareas | 0 | 7 | 25 | 25–85 |
| Render: PostgreSQL | 0 | 22 | ~90 | ~250 |
| Cloudflare R2 (fotos y archivos) | 0 | 0 (10 GB gratis) | ~2 | ~15 |
| Sentry (errores) | 0 | 0 | 26 | 26–80 |
| Correo transaccional | 0 | 0 | 15 | 35 |
| Correo corporativo y herramientas | 0 | 7 | 14 | 30 |
| **Total sin IA** | **0** | **~43** | **~200** | **~500** |

El **worker** corre las tareas programadas: generación de preventivos, envío
de webhooks, actualización diaria del monto en UF de las suscripciones,
avisos de cobro y purgas. Sin worker, cada tarea necesita un cron
(`docs/07_operacion.md`).

**GitHub Actions:** gratis mientras el repositorio sea público. Si vuelve a
ser privado, los minutos se cobran (el 2026-10-20 el CI se bloqueó por eso).

## 2. Costo por función

| Función | Costo para GALBUST |
|---|---|
| Roles personalizados (ORG-12) | 0. Solo código. |
| SSO OIDC (ORG-11) | 0. El proveedor de identidad (Entra ID, Google, Okta) lo paga el cliente. |
| Webhooks y API ampliada (X-05, X-08) | ~0. Usan el servidor y el worker existentes; las entregas se purgan a los 90 días. |
| Valor de la UF (mindicador.cl) | 0. Fuente pública. |
| Cobro con Mercado Pago (ORG-13) | Comisión por cobro, ~3–4 % + IVA (confirmar, D-02). Básico (~$61.500) → ~$2.500/mes; Profesional (~$164.000) → ~$6.500/mes. |
| Asistente IA (AI-01..05) | Por uso; ver §3. |
| Boletas y facturas SII | Hoy 0 (se emiten a mano). Un emisor automático costaría ~$10.000–30.000/mes (D-05). |

## 3. Asistente IA

Modelo por defecto: `claude-opus-5-5` (USD 4 por millón de tokens de
entrada y USD 20 por millón de salida; `AI_MODEL_PRICES_USD`). Estimación:
una conversación típica (contexto del activo y manuales, ~4 preguntas)
cuesta **USD 0,20–0,50**.

| Plan | Cupo mensual | Costo con el cupo completo | Ingreso del plan |
|---|---|---|---|
| Básico / Prueba | no incluido | 0 | — |
| Profesional | 100 conversaciones | ~USD 20–50 | ~USD 175 |
| Empresa | 500 conversaciones | ~USD 100–250 | desde ~USD 390 |

El estudio de mercado estima ~USD 10/mes en la etapa 1, porque casi nadie
agota el cupo. El costo real por organización y mes está en el admin
("Costo del asistente"). Palancas: modelo más barato (`AI_MODEL` =
`claude-sonnet-5-5`, la mitad) o cupo menor (D-01).

## 4. Costos que no son infraestructura (CLP)

| Ítem | Costo |
|---|---|
| Contador | $60.000–100.000/mes |
| Puesta en marcha y asesoría por cliente | Desde ~6–8 h del fundador; crece con la información que falte levantar. Se cobra aparte, cotizada, desde $600.000 (D-03) |
| Inversión inicial (textos legales, dominio, etiquetas QR, revisión de plantillas) | ~$560.000 mínimo · ~$1,66 M recomendado |

## 5. Margen por cliente (estimado)

| Plan | Ingreso/mes | Comisión MP | IA | Margen bruto |
|---|---|---|---|---|
| Básico | ~$61.500 | ~$2.500 | 0 | ~90 % |
| Profesional | ~$164.000 | ~$6.500 | ~$5.000–45.000 | 70–95 % |

Con unos 3 clientes pagando se cubre la infraestructura completa de la etapa
1. El costo grande es el tiempo de puesta en marcha y soporte, no los
servidores.

## 6. Decisiones abiertas

| ID | Decisión | Opciones | Estado |
|---|---|---|---|
| D-01 | Modelo y cupo del asistente IA en Profesional | Mantener Opus y 100 conv.; pasar a Sonnet; bajar el cupo | Abierta |
| D-02 | Confirmar la comisión real de Mercado Pago para suscripciones en Chile | Revisar en la cuenta al crearla | Abierta |
| D-03 | ¿Se cobra la puesta en marcha? ¿Cuánto y cómo (hoy se cobra por fuera)? | 4 UF Básico · 10 UF Profesional · cotizada Empresa | **Cerrado** 2026-10-22: se cobra aparte como asesoría, sin precio fijo ni por plan. Se cotiza según la información que ya tenga la empresa (inventario de activos, planes, historial) y el trabajo que demande; parte en $600.000 CLP (~15 UF). Pago único, fuera de Mercado Pago |
| D-04 | Plan de Render al pasar a producción: worker o cron | Worker (USD 7) recomendado | **Cerrada** 2026-10-29: worker. Producción va en un Blueprint aparte (`render.production.example.yaml`: web + worker + Postgres pagado) y la demo sigue en `render.yaml` |
| D-05 | Facturación electrónica automática (SII) | Manual por ahora · emisor (OpenFactura, Bsale, Haulmer) | Abierta |
| D-06 | ¿El repositorio sigue público? Expone código y precios; ahorra el CI | Público · privado con presupuesto de Actions | Abierta |
| D-07 | Validar con credenciales `TEST-` que Mercado Pago permite actualizar el monto de la suscripción cada mes (riesgo de ADR-033) | Prueba en sandbox antes de cobrar | Abierta |

## 7. Pendientes de producto

| ID | Pendiente | Origen | Estado |
|---|---|---|---|
| P-01 | Traducción a inglés y portugués | F3 (X-07) | **Postergado** 2026-10-29: el foco es Chile; se retoma si llega un cliente fuera de Chile |
| P-02 | Base de datos dedicada para el plan Empresa | F3, solo si hay demanda | Abierto |
| P-03 | En solo lectura, ocultar también las acciones de las pantallas secundarias (ficha de ubicación y de clase, borrador de plan, equipo, tokens, webhooks, roles, SSO) y los botones de terreno | ORG-13 CA5 | **Cerrado** 2026-10-24: esas pantallas muestran los datos con el formulario deshabilitado (`form-lock`) y sin botones de escritura; `bootstrap` de la PWA entrega los permisos de escritura en falso (`can_act`) y el teléfono oculta ejecutar, solicitar y lecturas |
| P-04 | Eventos de webhook de planes preventivos, asignación o reprogramación de OT y decisiones sobre solicitudes | X-08 | **Cerrado** 2026-10-24: `maintenance_plan.version_published`/`.activated`/`.deactivated`, `work_order.assigned`/`.rescheduled` y `work_request.accepted`/`.rejected`/`.merged`, desde los servicios y por módulo del plan (ADR-030, docs/08) |
| P-05 | Tokens de API limitados por sitio | X-05 | **Cerrado** 2026-10-24: casillas de sitios al crear el token (`ApiTokenSite`); el principal del token usa el mismo alcance por sitio que una membresía en todos los selectores; los tokens sin sitios no cambian (ADR-034, docs/08) |
| P-06 | Compactar la comparación de planes a 360 px (hoy cada plan repite su lista completa) | ORG-13 CA10 | **Cerrado** 2026-10-24: bajo 1100 px cada tarjeta dice «Todo lo de X, más:» y lista solo lo que agrega al plan que contiene (`PlanColumn.builds_on`/`added`); la tabla de escritorio no cambia |
| P-07 | Ventana de traslape al regenerar el secreto de un webhook | X-08 | **Cerrado** 2026-10-24: el anterior sigue firmando 24 h (`t=…,v1=<anterior>,v1=<nuevo>`, la nueva al final), con opción de invalidarlo de inmediato o terminar la ventana; la purga diaria lo descarta (ADR-030, docs/08) |
| P-08 | Revisar las plantillas de rubro (marcadas como borrador) | MVP-1 | Abierto |
| P-09 | Crear las cuentas de producción (Render pagado, R2, Sentry, correo, Mercado Pago) y seguir `docs/07_operacion.md` | MVP-1 / ORG-13 | Abierto |
| P-10 | Abaratar el CI antes de volver el repo a privado: correr solo en PR y `main`, `main` solo con Python 3.13, tests en paralelo con `pytest-xdist` (meta: 150–250 min/semana) | D-06 | **Cerrado** 2026-10-20: CI solo en PR hacia `main`, Python 3.13 y `pytest -n auto` (la suite pasó de ~20 a ~5 min en local) |
