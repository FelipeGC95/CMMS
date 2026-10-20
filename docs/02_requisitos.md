# 02 — Requisitos

**Estado:** borrador v0.1 (2026-09-24)

Convenciones:

- Cada historia tiene un ID `MOD-NN`, una prioridad y criterios de aceptación
  (CA) verificables. Los CA son la base de los tests.
- Prioridad = fase del roadmap (ver [05_roadmap.md](05_roadmap.md)):
  **M0** (MVP‑0, demo), **M1** (MVP‑1, piloto), **F2**, **F3** (posteriores).
- "Usuario" = persona con membresía activa en la organización activa.
- Toda historia implica: aislamiento por organización, registro de auditoría en
  escrituras, textos traducibles, zona horaria de la organización/sitio.

---

## 1. Organización, sitios y usuarios (`ORG`)

**ORG-01 · Alta de organización (M0)**
Como superadmin de plataforma quiero crear una organización con plan, zona
horaria e idioma, para habilitar a un cliente.
- CA1: La organización queda con plan, cupos (activos, usuarios, MB) y período
  de prueba (fecha de término) configurables.
- CA2: Se crea la primera invitación para su administrador.
- CA3: Solo usuarios con `is_platform_admin` pueden hacerlo.

**ORG-02 · Autoregistro con prueba gratuita (M1)**
Como jefe de mantención quiero registrarme y crear mi organización yo mismo,
para probar sin hablar con un vendedor.
- CA1: El registro crea usuario + organización (plan Prueba, 30 días) + membresía
  de administrador en una sola transacción.
- CA2: Requiere verificación de correo antes de crear datos.
- CA3: Tiene límite de intentos y protección anti‑bots.
- Nota: `/signup/` guarda un `PendingSignup` (contraseña ya con hash) y envía
  el enlace; al confirmar (botón POST, para que los antivirus del correo no lo
  consuman) se crean usuario, organización y membresía en una transacción. Si
  el correo ya existe se avisa por correo, no en pantalla. Anti‑bots sin JS de
  terceros: campo trampa, tiempo mínimo y una suma firmada; límite por IP con
  el rate limit de allauth. El registro propio de allauth sigue cerrado.

**ORG-03 · Asistente de puesta en marcha (M1)**
Como administrador nuevo quiero un asistente que me guíe, para tener el sistema
funcionando en menos de una hora.
- CA1: Pasos: rubro → sitios → activos (Excel o alta rápida) → plantillas de
  planes sugeridas por clase → lecturas iniciales de medidores → resumen del
  calendario generado.
- CA2: Cada paso se puede saltar y retomar; el avance se guarda.
- CA3: Al elegir un rubro se copian a la organización las clases de activo,
  atributos y catálogo de fallas de ese rubro (copias editables, no referencias).

**ORG-04 · Jerarquía de ubicaciones (M0)**
Como administrador quiero definir sitios y áreas en árbol, para ubicar activos.
- CA1: Profundidad libre (recomendado ≤ 4); cada nodo tiene tipo (sitio, área,
  línea, otro), código único en la organización y zona horaria opcional
  (hereda de la organización).
- CA2: No se puede eliminar un nodo con activos o hijos; se puede desactivar.
- CA3: Mover un nodo actualiza la ruta de todos sus descendientes.

**ORG-05 · Invitar usuarios (M0)**
Como administrador quiero invitar por correo asignando un rol, para sumar a mi equipo.
- CA1: El enlace expira a los 7 días y es de un solo uso (token guardado como hash).
- CA2: Si el correo ya tiene cuenta, se agrega la membresía sin crear otro usuario.
- CA3: No se puede superar el cupo de usuarios del plan (mensaje claro).

**ORG-06 · Roles y alcance (M0)**
Como administrador quiero asignar un rol por membresía y, opcionalmente,
restringirla a ciertos sitios, para dar acceso limitado a contratistas o clientes.
- CA1: Roles base: `admin`, `planner` (planificador), `supervisor`, `technician`,
  `viewer` (solo lectura), `external` (cliente o contratista).
- CA2: `external` y cualquier rol con alcance limitado solo ven activos, OT,
  solicitudes e indicadores de sus sitios (incluyendo descendientes).
- CA3: La matriz rol → permisos está en código y documentada; los tests cubren
  cada permiso sensible. Roles a medida sobre un rol base: ORG-12.

**ORG-07 · Varias organizaciones por usuario (M0)**
Como usuario con acceso a varias organizaciones quiero cambiar de organización
activa, para trabajar con cada cliente por separado.
- CA1: Selector visible si tiene más de una membresía; recuerda la última.
- CA2: Al cambiar, ningún dato de la organización anterior queda visible
  (incluido caché de navegador en páginas sensibles).

**ORG-08 · MFA (M1)**
Como usuario quiero activar segundo factor (TOTP + códigos de recuperación);
como administrador quiero exigirlo a mi organización.
- CA1: Si la organización lo exige, el usuario sin MFA es forzado a configurarlo
  al entrar.
- Nota: por defecto es obligatorio para administradores y opcional para el
  resto; cada organización puede exigirlo a todos o dejarlo opcional
  (`mfa_policy`). Con varias organizaciones manda la activa (ADR-022).

**ORG-09 · Marca por organización (M1)**
Como administrador quiero subir mi logo y elegir un color primario, para que la
app y los reportes lleven mi marca.
- CA1: El color se valida: si no alcanza contraste AA para su uso, el sistema
  ajusta el tono o el color de texto automáticamente y lo informa.
- CA2: Tokens de estado y accesibilidad no se sobrescriben.

**ORG-10 · Cupos y módulos por plan (M1)**
- CA1: Alcanzar el 80 % de un cupo muestra aviso; el 100 % bloquea la creación
  (no la lectura ni la edición).
- CA2: Los módulos no incluidos no aparecen en la navegación y sus URL/API
  responden 403 con mensaje de plan.
- Nota: el cupo de activos se revisa en `create_asset` (web y API) y en la
  importación Excel (todo o nada) con `organizations.services.check_quota`;
  cuentan todos los activos, también los dados de baja. El aviso del 80 %
  aparece al crear o importar activos y en Configuración. El mapa módulo ↔ URL
  está en `apps/organizations/modules.py` (`assets`, `work_orders`,
  `preventive`, `requests`, `api`, `exports`); la API exige además el módulo
  `api`. El inicio con los KPI no se bloquea (portada de todos los planes). Un
  plan sin módulos listados no restringe módulos. Pendiente: ocultar los botones
  de exportar dentro de las listas (hoy responden 403).

**ORG-11 · SSO OIDC (F3, plan Empresa)**
Como administrador quiero que mi equipo entre con la cuenta de la empresa (Microsoft
Entra ID, Google Workspace, Okta…), sin crear usuarios a mano ni manejar otra
contraseña.
- CA1: En `/settings/sso/` (solo administradores, módulo `sso`, solo plan Empresa) se
  configura un proveedor OpenID Connect por organización: URL del emisor o de
  descubrimiento, ID de cliente y secreto. Al guardar se lee y valida el
  descubrimiento (https, emisor igual a la URL, sin direcciones privadas: defensa SSRF
  de los webhooks). El secreto se guarda cifrado y nunca se vuelve a mostrar; en
  blanco conserva el guardado. «Probar conexión» lee descubrimiento y claves (JWKS) y
  revisa el secreto, y deja el resultado a la vista. La pantalla muestra la URI de
  redirección y los scopes para registrar la aplicación.
- CA2: La organización declara sus dominios de correo. Un dominio se verifica con un
  registro DNS TXT `_galbust-verification.<dominio>` =
  `galbust-domain-verification=<código>`. Sin dominio verificado no hay ingreso por
  SSO, alta automática ni exigencia. Se rechazan dominios mal escritos y de correo
  personal (gmail.com, outlook.com…).
- CA3: Un dominio verificado pertenece a una sola organización en toda la plataforma
  (índice único parcial); mientras está pendiente otras pueden reclamarlo, así nadie
  lo bloquea sin ser dueño, pero la primera que lo verifica se queda con él.
- CA4: En el login, «Entrar con SSO» pide el correo y redirige al IdP de la
  organización dueña del dominio (descubrimiento por dominio). Flujo authorization
  code con PKCE (S256), `state` y `nonce` de un solo uso que vencen a los 10 minutos.
  El id_token se valida siempre: firma con las claves del IdP (solo algoritmos
  asimétricos), emisor, audiencia (`azp` si hay varias), vigencia y nonce.
- CA5: Se enlaza con una cuenta (existente o nueva) por correo solo si el correo es de
  un dominio verificado de la organización del intento y el IdP no dice
  `email_verified: false`. El IdP de una organización nunca abre sesión con un correo
  de un dominio de otra.
- CA6: Alta automática: si quien entra no tiene usuario o membresía en la organización,
  se crea el usuario (sin contraseña, correo verificado) y la membresía con el **rol
  por defecto** de la configuración: un rol base o un rol personalizado (ORG-12),
  nunca administrador (lo valida el servicio y, si alguien lo escribiera en la base,
  cae a técnico). Una invitación pendiente para ese correo manda: entra con su rol.
  Respeta el cupo de usuarios del plan (miembros activos + invitaciones pendientes);
  si está lleno, no entra y ve un mensaje claro. Queda en la auditoría
  (`source: sso`), igual que la configuración y los dominios. Una membresía
  desactivada no se reactiva por SSO.
- CA7: «Exigir SSO» (opcional): los correos de los dominios verificados no entran con
  contraseña (se les manda al SSO) ni reciben enlace para recuperarla. La regla la
  define el dominio del correo, no la organización activa. Excepción: los
  administradores de la organización dueña del dominio conservan contraseña + MFA
  como acceso de emergencia (el aviso de recuperar contraseña es el mismo para todos,
  no revela quién es administrador).
- CA8: Al entrar por SSO no se pide TOTP: el SSO cuenta como factor fuerte para la
  organización de ese IdP (ADR-022/032). Quien entra con contraseña, incluidos los
  administradores, sigue con la política de MFA de su organización; al cambiar a otra
  organización rige la de esa.
- CA9: Sin configuración SSO activa (o sin el módulo en el plan) nada cambia: login,
  recuperación de contraseña y MFA funcionan como antes.
- CA10: Una organización no ve, edita, verifica ni usa la configuración o los
  dominios de otra; su IdP no puede abrir sesión en otra (servicio, vistas y RLS).
- No cumplido / fuera de v1: SAML 2.0 (solo OIDC); aprovisionamiento SCIM y bajas
  automáticas (quien deja la empresa sigue con su membresía hasta que el admin la
  desactive); cerrar sesiones abiertas con contraseña al activar «Exigir SSO» (siguen
  hasta vencer); cierre de sesión en el IdP (single logout); mapear grupos o roles
  del IdP a roles de GALBUST; varios IdP por organización; endpoints multiempresa
  como `common` de Entra ID (se usa la URL del inquilino); SSO en la API v1 (sigue
  con tokens de organización). Implementación: ADR-032.

**ORG-12 · Roles personalizados (F3, plan Profesional+)**
Como administrador quiero crear roles a medida (p. ej. «Bodeguero») a partir de
un rol base, para dar a cada persona solo los permisos que necesita.
- CA1: Un rol personalizado es nombre (único en la organización, sin distinguir
  mayúsculas) + rol base (cualquiera salvo `admin`) + permisos. Al crearlo parte
  con los permisos de su rol base; en `/settings/roles/` se marcan o desmarcan,
  agrupados por área y con etiquetas en español. El rol base no cambia después
  (se crea otro rol).
- CA2: El selector de rol del equipo y de las invitaciones ofrece los roles base
  y los personalizados activos. El equipo, el menú «Más» y la invitación muestran
  el nombre del rol personalizado (el equipo, también su base). La membresía guarda
  `role` = rol base y `custom_role`.
- CA3: Los permisos de administración (`team.manage`, `audit.view`,
  `organization.manage`, `api_token.manage`, `assistant.view_all`) nunca se dan con
  un rol personalizado: lo valida el servicio (no solo el formulario) y `has_perm`
  los ignora aunque estuvieran guardados. Solo los administradores gestionan roles.
- CA4: El rol personalizado concede y quita permisos efectivos (`has_perm`) en la
  web, la API de terreno y las notificaciones; los responsables asignables de una
  OT siguen el permiso vigente de ejecutar OT.
- CA5: El rol base sigue definiendo el alcance obligatorio de `external`, la vista
  por defecto «Mis OT», el panel de técnico del inicio, los carriles y la capacidad
  del tablero semanal y el MFA de administradores; la regla «siempre queda un
  administrador» no cambia.
- CA6: Módulo de plan `custom_roles` (Profesional y Empresa). Sin el módulo la
  pantalla responde 403 con mensaje de plan, el selector no ofrece roles
  personalizados y cada membresía cae a los permisos de su rol base sin borrar
  nada; si el plan recupera el módulo, vuelven a regir.
- CA7: Sin borrado físico: un rol se desactiva, y no se puede desactivar mientras
  lo tenga una persona activa o una invitación pendiente. Crear, editar,
  desactivar y asignar quedan en la auditoría.
- CA8: Una organización no ve, edita, asigna ni referencia roles de otra
  (servicio, formulario, modelo y RLS).
- No cumplido / fuera de v1: cambiar el rol base de un rol existente, clonar
  roles, permisos por objeto o por sitio distintos del alcance de la membresía,
  roles para tokens de API (siguen como planificador) y exponer roles en la API v1
  (la API no expone membresías; `bootstrap` de terreno devuelve el rol base y los
  permisos efectivos). Implementación: ADR-031.

**ORG-13 · Cobro recurrente y autogestión del plan (F3)**
Como administrador quiero contratar, cambiar o cancelar el plan y pagarlo solo, sin
escribirle a nadie; y como dueño de GALBUST quiero cobrar en pesos un precio fijado en UF.
- CA1: Proveedor Mercado Pago, suscripciones (`preapproval`) en CLP, detrás de una interfaz
  pequeña (`billing.gateway`). Sin `MERCADOPAGO_ACCESS_TOKEN` y `MERCADOPAGO_WEBHOOK_SECRET`
  el cobro en línea queda inactivo: la app funciona como antes y `/settings/billing/` lo dice
  (ADR-020). Toda llamada saliente tiene timeout, defensa SSRF y no sigue redirecciones.
- CA2: Precios en UF editables por el superadmin en el plan: Básico 1,5 UF/mes; Profesional
  4 UF/mes + 1 UF por cada 100 activos (o fracción) sobre 300 (cuentan todos los activos,
  como el cupo); Empresa «desde 9 UF», cotizado: el superadmin fija el precio acordado en la
  organización y solo entonces puede pagarlo en línea. Anual = 10 meses. Cupón administrable
  (porcentaje y duración en meses desde que se aplica, p. ej. «Lanzamiento» 50 % por 12
  meses). El monto se calcula siempre en el servidor y se redondea al peso (0,5 hacia arriba).
- CA3: UF del día desde una fuente pública (mindicador.cl por defecto) guardada con fecha; si
  la fuente falla se usa el último valor guardado; sin ninguno no se cobra y se avisa. La
  tarea diaria (y `manage.py billing_daily`) actualiza el monto de cada suscripción activa en
  Mercado Pago con la UF del día y los tramos de activos.
- CA4: En `/settings/billing/` (solo `organization.manage`; enlace en Configuración y en
  «Más»): plan, estado con ícono + texto, fin de la prueba o período pagado, próximo cobro en
  UF y CLP estimado, descuento vigente y UF usada. Sin suscripción: elegir Básico o
  Profesional y mensual o anual → checkout de Mercado Pago. Con suscripción: subir de plan es
  inmediato (el monto nuevo rige desde el próximo cobro, sin prorrateo); bajar se programa
  para el fin del período y solo si la organización cabe en los cupos del plan menor (si no,
  mensaje con el cupo excedido); elegir el plan actual anula el cambio programado. Cancelar:
  no se renueva y el acceso sigue hasta el fin del período pagado. Empresa: botón «Cotizar».
- CA5: Impago y fin de prueba: avisos in-app y por correo (X-02, tipo `billing`, a los
  administradores) 7 y 1 días antes del fin de la prueba, al fallar un cobro, al entrar en
  gracia y al quedar en solo lectura. Gracia de 7 días (`BILLING_GRACE_DAYS`); luego **solo
  lectura**: se ve y se exporta, no se crea ni se edita en la web, la API v1 (402
  `payment_required`) ni `/field/api/` (402; la PWA deja el cambio en cola y lo envía al
  pagar). La interfaz es coherente con la solo lectura: no muestra botones, formularios ni
  accesos de crear, editar o ejecutar (solo ver, exportar Excel/PDF, imprimir etiquetas y, al
  administrador, Facturación y «Pagar»); la PWA muestra un aviso. Los administradores siempre
  pueden entrar a Facturación y pagar. Al aprobarse un pago se reactiva sola. Nunca se borran datos por impago. La misma solo lectura rige durante
  el borrado programado (X-04).
- CA6: Webhook entrante `/billing/webhooks/mercadopago/`: valida la firma `x-signature`;
  siempre consulta el recurso a la API de Mercado Pago (no confía en el cuerpo); idempotente
  por id de evento; sin sesión ni CSRF solo en esa ruta; nunca responde 500 por datos
  inesperados (503 si la API de Mercado Pago no responde, para que reintente). Registra cada
  pago (CLP, UF aplicada, estado, id de Mercado Pago) y la auditoría.
- CA7: Datos de facturación: RUT validado con dígito verificador, razón social, giro,
  dirección y correo de facturación. Historial de pagos paginado.
- CA8: Organizaciones existentes y demo: marca «facturación manual (exenta)» que el
  superadmin controla; la migración la deja encendida en las existentes (salvo las del
  autoregistro en prueba) y es el valor por defecto al crear una en el admin. Las nuevas de
  `/signup/` entran en prueba de 30 días sin exención. La demo es exenta y muestra un
  historial de pagos de ejemplo.
- CA9: Una organización no ve, edita ni paga por otra: `external_reference` opaco por
  organización, el webhook solo toca la organización que guardó esa suscripción (y con la
  misma referencia), un id de pago es de una sola organización (servicio, vistas y RLS).
- No cumplido / fuera de v1: boletas o facturas electrónicas del SII (Mercado Pago envía su
  comprobante); prorrateo al subir de plan; cambiar de mensual a anual con una suscripción
  activa (se cancela y se vuelve a contratar al terminar el período); puesta en marcha como
  pago único (se cobra por fuera y se registra a mano en el admin si hace falta); otros
  proveedores (Flow, Stripe); que el administrador cambie su medio de pago dentro de
  GALBUST (se hace en Mercado Pago); cupones que ingresa el cliente (los aplica el
  superadmin). Implementación: ADR-033.

## 2. Activos (`AST`)

**AST-01 · Clases de activo con atributos (M0)**
Como administrador quiero definir clases (ej. "Grúa horquilla") con atributos
propios (capacidad, combustible…), para registrar datos técnicos homogéneos.
- CA1: Tipos de atributo: texto, número (con unidad), fecha, sí/no, lista.
- CA2: Cada clase indica su categoría ISO 14224 de referencia y nivel típico
  (equipo, sistema, componente).
- CA3: Cambiar la definición no borra valores existentes; los atributos
  eliminados quedan ocultos.

**AST-02 · Alta y ficha de activo (M0)**
- CA1: Campos: código (único en la organización), nombre, clase, ubicación,
  activo padre (opcional), estado, criticidad (A/B/C), fabricante, modelo,
  serie, año, fecha de puesta en servicio, atributos de la clase.
- CA2: La ficha muestra: datos, medidores, planes asignados, OT abiertas,
  historial (OT cerradas, fallas, cambios de estado), documentos y fotos.
- CA3: Alta rápida: solo código, nombre y clase son obligatorios.

**AST-03 · Jerarquía activo → sistema → componente (M0)**
- CA1: Un activo puede tener hijos; no se permiten ciclos.
- CA2: El historial y los KPI del padre pueden incluir los de los hijos (opción
  "incluir componentes").

**AST-04 · Estados y detenciones (M0)**
- CA1: Estados: operativo, detenido, en mantención, de baja.
- CA2: Cada cambio de estado abre/cierra un período (`AssetStatusPeriod`) con
  motivo y OT/falla asociada; estos períodos alimentan la disponibilidad.
- CA3: Dar de baja un activo cancela sus asignaciones de plan (con confirmación).

**AST-05 · Código QR (M0)**
- CA1: Cada activo tiene un QR con un token no adivinable (no el ID interno).
- CA2: Impresión de etiquetas en lote (PDF, varias por hoja) con logo de la
  organización.
- CA3: Escanear el QR con sesión abre la ficha; sin sesión ver `REQ-02`.

**AST-06 · Documentos y fotos (M0)**
- CA1: Adjuntos de hasta 20 MB; imágenes comprimidas y redimensionadas
  (lado mayor ≤ 2000 px) al subir.
- CA2: El uso cuenta para el cupo de almacenamiento de la organización.
- CA3 (M1): Los manuales (PDF) se adjuntan al activo o a su clase de activo;
  los de la clase aplican a todos sus activos. El worker extrae el texto por
  página para la búsqueda y el asistente (`AI-*`, ADR-018).

**AST-07 · Medidores y lecturas (M0)**
Como técnico quiero registrar horómetro/odómetro, para que los preventivos por
uso se calculen solos.
- CA1: Tipos: horas, km, ciclos, otro (unidad libre). Acumulativo o no.
- CA2: Una lectura acumulativa menor a la anterior se rechaza, salvo que se
  marque como "reinicio/cambio de medidor".
- CA3: Lecturas con fuente: manual, importación, API, OT (al cerrar).
- CA4: Advertencia si la lectura implica un uso diario anómalo (ej. > 24 h/día).

**AST-08 · Importación Excel de activos (M1)**
- CA1: Plantilla descargable con columnas y validaciones.
- CA2: Vista previa con errores por fila antes de confirmar; nada se guarda si
  el usuario no confirma.
- CA3: Importación idempotente por código de activo (actualiza si existe).

**AST-09 · Importación de lecturas por CSV/API (M1)**
- CA1: CSV con código de activo (o id de medidor), valor, fecha y reinicio opcional;
  vista previa con errores por fila antes de confirmar (mismo patrón que AST-08).
- CA2: Idempotente por (medidor, fecha, valor); origen "importación" (web) o "API".
- CA3: Lo mismo por API (`POST /api/v1/readings/bulk/`), con resultado por fila.

## 3. Mantenimiento preventivo (`PM`)

**PM-01 · Planes con tareas y checklist (M1)**
Como planificador quiero definir un plan (ej. "Mantención 250 h grúa horquilla")
con ítems de checklist, repuestos, herramientas y duración estimada.
- CA1: Tipos de ítem: check (OK/No OK/N.A.), medición numérica (con unidad y
  rango aceptable), texto, foto obligatoria, firma.
- CA2: Repuestos y herramientas como texto + cantidad (catálogo de inventario en F2:
  enlace opcional al catálogo y reserva automática, INV-10 e INV-11).

**PM-02 · Disparadores (M1)**
- CA1: Por calendario (cada N días/semanas/meses), por medidor (cada N unidades)
  o mixto: vence **lo que ocurra primero**.
- CA2: Modo de reprogramación: *fijo* (desde la fecha/lectura programada) o
  *flotante* (desde la última ejecución real).
- CA3: Tolerancia configurable (ej. ±10 %) usada para el cumplimiento.

**PM-03 · Versionado (M1)**
- CA1: Editar un plan publicado crea una versión borrador; al publicarla, las
  OT futuras usan la nueva versión.
- CA2: Una OT guarda la versión con que se generó y una copia (snapshot) de su
  checklist; cambiar el plan nunca altera OT existentes.

**PM-04 · Asignación de planes a activos (M1)**
- CA1: Asignación individual, en lote o automática por clase de activo.
- CA2: Cada asignación calcula su próximo vencimiento (fecha y/o lectura).

**PM-05 · Generación automática de OT (M1)**
- CA1: Una tarea periódica (cada hora) genera OT para vencimientos dentro del
  horizonte configurable (por defecto 14 días) o ya vencidos por medidor.
- CA2: Nunca hay dos OT abiertas de la misma asignación (idempotencia).
- CA3: Si un plan de mayor alcance vence junto a uno menor del mismo activo
  (ej. 500 h y 250 h), se puede configurar que el mayor "absorba" al menor.

**PM-06 · Calendario de preventivos (M1)**
- CA1: Vista mensual/semanal y proyección a 12 meses (vencimientos por medidor
  estimados con el uso diario promedio).

**PM-07 · Biblioteca de plantillas de planes (M1)**
- CA1: Plantillas por clase de activo; al adoptarlas se copian a la organización.

**PM-08 · Importación/exportación de planes por Excel (M1)**

## 4. Órdenes de trabajo (`WO`)

**WO-01 · Crear OT (M0)**
- CA1: Tipos: preventiva, correctiva, predictiva/por condición, mejora, inspección.
- CA2: Campos: activo (o ubicación), título, descripción, prioridad
  (1 urgente – 4 baja), fecha límite, estimación, responsables.
- CA3: Número correlativo por organización y tipo, asignado por el servidor
  (ej. `OT-C-000124`), sin huecos por concurrencia.

**WO-02 · Ciclo de vida (M0)**
- CA1: Estados: `requested` → `planned` → `scheduled` → `in_progress` ⇄
  `on_hold` → `completed` → `closed`; `cancelled` desde cualquier estado previo
  a `closed`.
- CA2: Pasos opcionales por organización: "requiere aprobación para cerrar"
  (si está apagado, `completed` pasa a `closed` automáticamente).
- CA3: Cada transición registra quién, cuándo, desde/hacia y comentario.
- CA4: Transiciones inválidas se rechazan en el servicio, no solo en la UI.
- CA5: Nombres visibles de los estados configurables por organización.

**WO-03 · Ejecución en móvil (M0)**
Como técnico quiero ejecutar el checklist desde el celular.
- CA1: Pantalla de ejecución con un ítem por tarjeta, botones ≥ 48 px, guardado
  automático por ítem.
- CA2: Por ítem: resultado, observación, fotos, medición (validada contra rango;
  fuera de rango se marca No OK y sugiere crear correctiva).
- CA3: Al completar: lectura de medidor (si el plan lo pide), firma y comentario.
- CA4: No se puede completar con ítems obligatorios sin responder.

**WO-04 · Mano de obra, repuestos y costos (M0)**
- CA1: Registro de técnicos con horas (inicio/fin o duración); costo hora por
  técnico configurable, copiado a la OT al registrar.
- CA2: Repuestos consumidos (texto, cantidad, costo unitario) y otros costos
  (servicios externos). En F2 el repuesto se puede tomar del catálogo, con
  salida de stock y costo promedio (INV-09).
- CA3: Totales por OT calculados y guardados al cerrar.

**WO-05 · Lista y filtros (M0)**
- CA1: Filtros por estado, tipo, activo, sitio, responsable, fechas; paginación
  obligatoria; "Mis OT" para técnicos.

**WO-06 · Correctiva desde hallazgo (M1)**
- CA1: Desde un ítem No OK se crea una OT correctiva vinculada, con activo y
  foto precargados.
- CA2 ✔ (F2, implementado 2026-10, paquete F1): **Sin conexión** (`/field/`, X-06, ADR-011): un ítem No OK muestra
  "Avisar hallazgo" (descripción opcional y "¿el equipo está detenido?"); el
  aviso se encola con `client_uuid` después de la respuesta del ítem y antes
  de "Completar", y al sincronizar llama a `POST
  /field/api/work-orders/{id}/checklist-items/{item}/finding/` con el mismo servicio
  que la web (`create_corrective_from_item`, idempotente por ítem). Quien no
  tiene `work_order.manage` (técnico) pasa por el aviso de falla (solicitud +
  correctiva), igual que en la web. OT cancelada → 409 `cancelled`; con la
  OT ya completada o cerrada se acepta (el aviso pudo hacerse antes de
  completar; como el servicio de la web); ítem que no está No OK → 400. Las fotos del ítem
  viajan por la cola de fotos de siempre y se ven en la correctiva por el
  vínculo. Ya avisado, el ítem muestra "Hallazgo por enviar" o "Hallazgo
  avisado · <código de la OT> · Solicitud N°" (ícono + texto) y no ofrece
  avisar de nuevo; el snapshot trae `finding_code` (correctiva abierta del
  ítem) y `finding_request_number`. Si el ítem deja de estar No OK antes de enviarse, el aviso
  pendiente se retira. Idempotente también por `client_uuid` (reenviarlo
  devuelve la misma correctiva aunque la OT ya se haya completado). Un aviso
  rechazado (400) o en conflicto (409) no detiene la OT: sale de su carril
  ("Completar" se envía igual), el ítem muestra "Hallazgo no enviado" y en
  Conflictos se corrige y reenvía (400, mismo `client_uuid`; 409 `conflict`,
  uno nuevo) o se descarta (OT cancelada: solo descartar). Las insignias "Por
  enviar: N" se actualizan en vivo.

## 5. Solicitudes, fallas y backlog (`REQ`)

**REQ-01 · Solicitud de trabajo (M0)**
Como cualquier usuario autorizado quiero avisar una falla o pedir un trabajo
desde el celular.
- CA1: Campos mínimos: activo (o ubicación), descripción, foto opcional,
  "¿el equipo está detenido?".
- CA2: Si está detenido, el activo pasa a estado *detenido* y se abre un evento
  de falla con hora de inicio.
- CA3: El planificador acepta (convierte en OT), rechaza (con motivo) o fusiona
  con otra solicitud.

**REQ-02 · Aviso por QR sin cuenta (M1, activable por organización)**
- CA1: Si la organización lo habilita, escanear el QR sin sesión muestra un
  formulario mínimo (nombre, descripción, foto).
- CA2: Protección anti‑abuso: límite por IP y por activo, captcha liviano.
- CA3: La solicitud queda marcada como "anónima" y requiere revisión.
- Implementación: el administrador lo activa en Configuración ("Avisos por QR
  sin cuenta"); el plan debe incluir Solicitudes. La página pública solo
  muestra la organización (nombre y logo) y el código y nombre del activo, sin
  navegación ni caché (`no-store`), con CSRF. Anti-abuso: campo trampa, tiempo
  mínimo y suma firmada (`apps/accounts/antibot.py`, el mismo del registro) y
  límites en el caché compartido: 5 envíos por hora por IP (cuenta también los
  fallidos) y 10 avisos al día por activo (ajustes `QR_REPORT_LIMIT_PER_IP`,
  `QR_REPORT_LIMIT_PER_ASSET`, `QR_REPORT_MIN_SECONDS`). El aviso no crea OT ni
  cambia el estado del activo aunque marque "detenido": llega a Solicitudes
  como "Anónima — requiere revisión" y avisa a planificadores y supervisores
  con el sitio a la vista; aceptarlo crea la OT correctiva (sin detener el
  activo) y le pasa la foto.

**REQ-03 · Registro de falla (M0)**
- CA1: Campos: modo, causa, mecanismo de falla, método de detección (catálogos
  estructurados según ISO 14224, contenido propio editable), componente afectado,
  inicio y fin de la detención, impacto (detención total, parcial, sin detención).
- CA2: Al cerrar una OT correctiva se pide completar la falla (modo y causa
  obligatorios si la organización lo configura).
- CA3: Al cerrar una OT correctiva se piden, en texto libre, **síntoma
  observado** (`failure_event.description`) y **acción realizada**
  (`work_order.completion_notes`), con ayuda en pantalla ("¿Qué viste? ¿Qué
  hiciste?") y campos aptos para dictado por voz. Es la base de conocimiento
  del asistente (ADR-018): sin esto no hay de dónde aprender.

**REQ-04 · Backlog priorizado (M1)**
- CA1: Backlog = OT no cerradas ni canceladas, ordenadas por puntaje:
  criticidad del activo × prioridad de la OT, desempate por fecha límite.
- CA2: Muestra horas estimadas totales y **backlog en semanas**
  (horas estimadas / capacidad semanal del equipo).

**REQ-05 · Programación semanal (M1)**
- CA1: Tablero por semana: columnas por día (y opcionalmente por técnico),
  arrastrar OT del backlog, carga vs. capacidad por técnico.
- CA2: Programar una OT la pasa a `scheduled` y notifica al responsable.

## 6. Inventario y repuestos (`INV`, F2)

**Estado:** en construcción (F2). Diseño aprobado el 2026-10-10; paquetes
P0–P7 en [05_roadmap.md](05_roadmap.md) y decisiones en
[04_arquitectura.md](04_arquitectura.md), ADR-028. Modelo en
[03_modelo_de_datos.md](03_modelo_de_datos.md).

Catálogo de repuestos, bodegas, stock por bodega, reservas por OT, consumos
desde la OT y costo promedio ponderado. El registro de repuestos de la OT
(WO-04) se enlaza al catálogo sin migrar datos: `part` opcional junto al texto.
Módulo de plan `inventory` ("Inventario y repuestos") en los planes Prueba,
Profesional y Empresa; Básico no lo incluye.

Decisiones de producto (2026-10-10):
- El stock negativo **se permite** siempre: un consumo nunca se rechaza por
  falta de stock (tampoco offline); se marca "Negativo · regularizar" (ícono +
  texto) y encabeza la lista de reposición.
- Al completar una preventiva en la **web**, los repuestos previstos
  pendientes aparecen pre-marcados con su cantidad en el formulario de cierre
  (se desmarcan o editan con un toque). En la API v1 y la PWA el consumo es
  explícito ("Usar"); nada se consume implícitamente.
- Técnicos y externos **no** ven costos de inventario (costo promedio, valor,
  costos de movimientos). La OT sigue mostrando sus costos como hoy (WO-04 sin
  cambios).
- En v1 solo lista de reposición + Excel; **sin órdenes de compra** (F3 si el
  piloto lo pide).

**INV-01 · Catálogo de repuestos (F2)**
Como planificador quiero un catálogo de repuestos, para dejar de escribir el
mismo repuesto como texto en cada OT y plan.
- CA1 ✔ `code` y `name` obligatorios; `unit` (por defecto "un"); `category`
  (texto con sugerencias), descripción, fabricante, n° de parte del
  fabricante, proveedor y notas.
- CA2 ✔ Código único por organización sin distinguir mayúsculas; el error se
  muestra en el campo (web y API).
- CA3 ✔ No se borra: se desactiva (desaparece de los buscadores, el historial
  sigue visible, se puede reactivar).
- CA4 ✔ "Dónde se usa" calculado: planes cuya versión vigente lo prevé (y sus
  clases) y activos donde se consumió en los últimos 24 meses (cantidad y
  última fecha).
- CA5 ✔ Lista paginada; búsqueda por código, nombre o n° de parte; filtros por
  categoría, bodega y estado (activo, bajo mínimo, sin stock, negativo).

**INV-02 · Importación por Excel/CSV (F2)**
- CA1 ✔ Plantilla `código, nombre, unidad, categoría, n° de parte, fabricante,
  proveedor, bodega, cantidad, costo unitario, stock mínimo, stock objetivo,
  ubicación en bodega`; acepta .xlsx y .csv.
- CA2 ✔ Vista previa con errores por fila; todo o nada (como AST-08).
- CA3 ✔ Idempotente por código: crea o actualiza.
- CA4 ✔ `cantidad` es un **conteo** en esa bodega (o en la por defecto): se
  registra un ajuste con motivo "import"; reimportar la misma cantidad no crea
  movimiento. Con costo y stock previo ≤ 0, el costo fija el promedio.
- CA5 ✔ Una sola transacción; auditoría con los conteos.

**INV-03 · Bodegas (F2)**
- CA1 ✔ Código (único por organización, sin distinguir mayúsculas), nombre,
  ubicación (cualquier nodo, opcional), por defecto, activa y notas.
- CA2 ✔ Al primer uso del inventario se crea sola "Bodega principal" (código
  `BOD`), marcada por defecto. Siempre hay exactamente una por defecto.
- CA3 ✔ No se desactiva una bodega con stock ≠ 0, con reservas activas o que
  sea la por defecto; el mensaje explica por qué.
- CA4 ✔ Con una sola bodega activa no se muestra el selector de bodega.
- CA5 ✔ Alcance por sitio: una membresía restringida ve las bodegas cuya
  ubicación cae en su alcance (incluidos los hijos) y las sin ubicación; no
  mueve stock de una bodega que no ve.

**INV-04 · Stock por bodega (F2)**
- CA1 ✔ Por repuesto y bodega: en existencia (`on_hand`), reservado,
  disponible = en existencia − reservado, mínimo, objetivo, ubicación en
  bodega y último movimiento.
- CA2 ✔ El saldo solo cambia por movimientos: sin edición directa en pantalla,
  API ni admin.
- CA3 ✔ Ficha del repuesto: stock por bodega, total, costo promedio (solo con
  `inventory.cost_view`) y últimos movimientos.

**INV-05 · Entrada de stock (F2)**
- CA1 ✔ Repuesto, cantidad > 0, costo unitario (opcional), bodega (por defecto
  la por defecto), documento, proveedor, nota y fecha real (por defecto ahora).
- CA2 ✔ Costo vacío → se usa el promedio vigente y el promedio no cambia. Costo
  0 explícito permitido (diluye el promedio; se advierte).
- CA3 ✔ "Guardar y registrar otra" conserva bodega y documento.
- CA4 ✔ Idempotente por `client_uuid`.

**INV-06 · Ajuste por conteo (F2)**
- CA1 ✔ Se informa la cantidad contada y se ajusta por la diferencia; si es 0
  no crea movimiento y lo informa.
- CA2 ✔ Se valoriza al promedio vigente.
- CA3 ✔ Motivo obligatorio solo si la diferencia supera el 10 % o es negativa.
- CA4 ✔ El conteo masivo es la importación (INV-02). Requiere
  `inventory.manage`.

**INV-07 · Transferencia entre bodegas (F2)**
- CA1 ✔ Origen ≠ destino, cantidad > 0; dos movimientos enlazados por
  `transfer_group` en una transacción.
- CA2 ✔ No cambia el promedio; el origen puede quedar negativo (INV-08 CA5).
- CA3 ✔ Solo visible con más de una bodega activa.

**INV-08 · Costo promedio ponderado (F2)**
- CA1 ✔ Un promedio por repuesto en la organización (`part.avg_cost`, 4
  decimales), no por bodega.
- CA2 ✔ Se recalcula solo con entradas con costo, devoluciones desde la OT y
  conteos/importaciones con costo cuando el stock es ≤ 0. Con
  `Q = Σ on_hand del repuesto en todas las bodegas antes del movimiento`:
  si Q ≤ 0 → promedio = c; si no, promedio = (Q·promedio + q·c) / (Q + q).
- CA3 ✔ Salidas, ajustes sin costo y transferencias se valorizan al promedio
  vigente y no lo cambian; cada movimiento guarda el costo unitario aplicado y
  el promedio resultante (`avg_cost_after`).
- CA4 ✔ El costo cero diluye el promedio (se advierte); stock > 0 con promedio
  0 se marca "Sin costo".
- CA5 ✔ Stock negativo permitido.
- CA6 ✔ Tests con fixtures de resultado conocido: entradas sucesivas, costo
  cero, negativo → entrada, devolución y transferencia.

**INV-09 · Consumo desde la OT (F2)**
Como técnico quiero registrar el repuesto que usé tocando el catálogo, sin
elegir bodega ni escribir costos.
- CA1 ✔ "Agregar repuesto" busca en el catálogo con sugeridos primero
  (reservados para la OT, usados antes en el activo, previstos en planes de su
  clase). El texto libre sigue posible ("No está en el catálogo": sin `part`,
  sin stock).
- CA2 ✔ Con un repuesto del catálogo, el servidor llena descripción
  («código · nombre»), unidad y costo unitario = **promedio vigente al
  consumir** (si el cliente envía costo, se ignora), crea el movimiento
  `issue` y lo enlaza en `part_usage.stock_movement`.
- CA3 ✔ Bodega automática: (1) la de la reserva de ese repuesto en la OT;
  (2) la bodega activa cuya ubicación es el ancestro más cercano de la
  ubicación del activo (o de la OT); (3) la por defecto. Solo con más de una
  bodega se ofrece cambiarla.
- CA4 ✔ Los registros de texto libre previos siguen válidos, sin migrar;
  KPI-02, PDF y Excel no cambian.
- CA5 ✔ Quitar una línea enlazada (OT no cerrada) crea una devolución
  (`return`) al costo de la salida (recalcula el promedio) y borra la línea
  como hoy. Al cerrar, el costo queda congelado como hoy (WO-04 CA3).
- CA6 ✔ Idempotente por `client_uuid`: el reenvío devuelve la misma línea aunque
  la OT ya esté cerrada; el mismo `client_uuid` en otra OT es conflicto (la
  clave se revisa antes que el estado de la OT).
- CA7 ✔ API v1 `POST /api/v1/work-orders/{id}/parts/` con `part` o
  `description`, mismo servicio que la web.

**INV-10 · Reservas por OT (F2)**
- CA1 ✔ Automática: al generar una preventiva, cada recurso de tipo repuesto
  con `part` (de la versión principal y de los planes absorbidos) crea una
  reserva por su cantidad (bodega según INV-09 CA3; repetidos se suman). Nunca
  bloquea: si no alcanza, la OT y la reposición muestran "Faltan N".
- CA2 ✔ Manual: con `work_order.manage` se agregan o quitan reservas en una OT
  abierta.
- CA3 ✔ Consumir descuenta de la reserva (`consumed_quantity`); al cubrirla
  queda `fulfilled`; consumir de más no crea otra reserva.
- CA4 ✔ Liberación al cerrar (`closed`), al cancelar (`cancelled`) o manual
  (`manual`); quitar una línea consumida no reabre la reserva.
- CA5 ✔ Invariante: `stock_item.reserved` = Σ pendientes de las reservas
  activas.
- CA6 ✔ La OT muestra "Repuestos previstos" (reservado, consumido, faltante)
  con botón "Usar" (consume el pendiente; cantidad editable).

**INV-11 · Repuestos del catálogo en planes (F2)**
- CA1 ✔ En borrador, cada recurso de tipo repuesto se puede enlazar al
  catálogo (buscador; la descripción se llena con el nombre y es editable).
- CA2 ✔ En una versión publicada, un recurso sin `part` se puede enlazar una
  sola vez (vacío → repuesto; nunca cambiar ni quitar): excepción acotada al
  ADR-016.
- CA3 ✔ "Crear catálogo desde los planes": agrupa las descripciones de
  recursos tipo repuesto sin enlace (sin distinguir mayúsculas ni espacios),
  propone código `REP-0001`… y unidad; al confirmar crea los repuestos y
  enlaza todos los recursos.
- CA4 ✔ Al publicar se conservan los enlaces; un borrador nuevo copia `part`.

**INV-12 · Mínimo, alerta y reposición (F2)**
- CA1 ✔ Mínimo y objetivo por repuesto y bodega (con una sola bodega, en la
  ficha del repuesto).
- CA2 ✔ Bajo mínimo = disponible < mínimo (con mínimo > 0); Sin stock =
  en existencia 0; Negativo = en existencia < 0. Siempre ícono + texto.
- CA3 ✔ Alerta al cruzar el umbral (de no-bajo a bajo) por un movimiento o una
  reserva: notificación `stock_low` (app y correo según preferencias) a
  quienes tienen `inventory.move` y ven la bodega; `dedupe_key`
  `stock_low:<stock_item_id>:<fecha>`; se encola con `on_commit`.
- CA4 ✔ Lista de reposición `/inventory/reorder/` por bodega: disponible,
  mínimo, objetivo, faltante de reservas y sugerido = max(objetivo, mínimo) −
  disponible (redondeado hacia arriba); primero los negativos y las reservas
  sin cubrir; exportable a Excel (módulo `exports`).
- CA5 ✔ El inicio de inventario muestra el contador "Bajo mínimo (N)".

**INV-13 · Consumo offline en la PWA (F2)**
- CA1 ✔ `reference/` incluye el catálogo activo compacto (id, código, nombre,
  unidad, categoría; tope 2.000 con `truncated`), sin costos, solo con el
  módulo `inventory` y `inventory.view`.
- CA2 ✔ `snapshot/` incluye por OT `planned_parts` (reservas con pendiente) y
  `parts` ya registrados.
- CA3 ✔ Sin señal: "Usar" un previsto o agregar un repuesto (catálogo o texto
  libre) con cantidad; se encola con `client_uuid` y `used_at` en la cola de
  la OT, antes de "Completar".
- CA4 ✔ Sin conflictos de stock; solo 409 por OT cancelada o bloqueada.
  Reenvío del mismo `client_uuid` → 200; en otra OT → 409 `conflict`.

**INV-14 · Historial, auditoría, exportación y reconciliación (F2)**
- CA1 ✔ Lista paginada de movimientos con filtros por repuesto, bodega, tipo,
  fechas y OT: quién, fecha real y de registro, cantidad con signo, costo
  unitario, saldo resultante y enlace a la OT o documento.
- CA2 ✔ Movimientos inmutables (tampoco en el admin); los errores se corrigen
  con un movimiento contrario.
- CA3 ✔ Auditoría de alta y edición de repuestos y bodegas, de cada movimiento
  (`stock_receive`, `stock_adjust`, `stock_transfer`, `stock_issue`,
  `stock_return`) y de las reservas (`reserve`, `release`).
- CA4 ✔ `manage.py inventory_reconcile [--organization <slug>] [--fix]`
  verifica (a) en existencia = Σ movimientos por ítem; (b) reservado = Σ
  pendientes de reservas activas; (c) cada `part_usage` enlazado tiene su
  movimiento con cantidad opuesta. `--fix` reescribe el saldo desde el libro y
  lo audita. Una tarea semanal sin `--fix` reporta a Sentry.
- CA5 ✔ Excel de stock (`/inventory/export.xlsx`), de movimientos filtrados y
  de reposición (módulos `exports` + `inventory`).
- CA6 ✔ Las tablas nuevas entran en la exportación completa y la purga
  (X-03, X-04, ADR-027), probado en `tests/test_org_export_delete.py`.

**INV-15 · Indicadores de inventario (F2, liviano)**
- CA1 ✔ En el inicio de inventario, solo con `inventory.cost_view`: valor del
  inventario = Σ en existencia positivo × promedio (total y por bodega);
  consumo valorizado de 12 meses (salidas − devoluciones); rotación aprox. =
  consumo 12 meses ÷ valor actual (marcada "aprox.", con "Fórmula");
  repuestos sin movimiento en 12 meses con su valor.
- CA2 ✔ Número fijo de consultas (`tests/test_query_counts.py`) y fixtures de
  valor conocido.

**INV-16 · Permisos, módulo y aislamiento (F2)**

| Permiso | admin | planner | supervisor | technician | viewer | external |
|---|---|---|---|---|---|---|
| `inventory.view` | ✔ | ✔ | ✔ | ✔ | ✔ | — |
| `inventory.cost_view` | ✔ | ✔ | ✔ | — | ✔ | — |
| `inventory.move` (entradas, transferencias) | ✔ | ✔ | ✔ | — | — | — |
| `inventory.manage` (catálogo, bodegas, conteos, mínimos, importación, crear desde planes) | ✔ | ✔ | — | — | — | — |

- CA1 ✔ Consumir en la OT = `work_order.execute` (sin cambios); reservar o
  quitar reservas = `work_order.manage`. El token de API actúa con permisos de
  planificador.
- CA2 ✔ Sin el módulo `inventory`: `/inventory/…` responde 403 con mensaje de
  plan; desaparecen la navegación y los buscadores de catálogo en OT y planes;
  el texto libre en la OT sigue; la API de inventario responde 403 y enviar
  `part` en `…/parts/` responde 403 con mensaje de plan.
- CA3 ✔ Tests de aislamiento de cada modelo, vista y endpoint, incluidos IDs
  cruzados en formularios, API y offline. RLS automático (`post_migrate` de
  `apps/core/rls.py`), verificado en `tests/isolation/test_rls.py`.

**INV-17 · API v1 de inventario (F2)**
- CA1 ✔ `GET/POST /api/v1/parts/` (alta idempotente por código: si existe, 200
  con el existente), `GET/PATCH /api/v1/parts/{id}/`; `GET
  /api/v1/warehouses/`; `GET /api/v1/stock/` (filtros `part`, `warehouse`,
  `below_min`); `GET /api/v1/stock-movements/`.
- CA2 ✔ `POST /api/v1/stock-movements/` con `kind` ∈ {`receipt`, `count`,
  `transfer`}, idempotente por `client_uuid`; `GET/POST
  /api/v1/work-orders/{id}/parts/` (INV-09).
- CA3 ✔ Paginación, errores y alcances read/write como el resto de la API;
  esquema sin advertencias (`--fail-on-warn`); documentado en
  [08_api.md](08_api.md).

**Fuera de v1:** órdenes de compra, proveedores como entidad, recepción contra
OC y precios por proveedor; conversión de unidades; lotes, series y
vencimientos; reparables/rotables; códigos de barra o QR de repuestos;
ubicaciones en bodega como árbol; BOM manual y kits; FIFO o costo estándar;
moneda configurable; integración contable; conteo cíclico y aprobación de
ajustes; KPIs de inventario en `/reports/kpis/`, PDF o API; repuestos en
plantillas de rubro; crear repuestos sin conexión; paso de inventario en el
asistente de puesta en marcha.

## 7. Mantenimiento por condición (`CBM`, F2)

**Estado:** hecho (F2, 2026-10, paquetes C0–C6); queda el paquete F1
(correctiva desde hallazgo sin conexión, WO-06 CA2). Diseño aprobado el
2026-10-12; paquetes en [05_roadmap.md](05_roadmap.md) y decisiones en
[04_arquitectura.md](04_arquitectura.md), ADR-029. Modelo en
[03_modelo_de_datos.md](03_modelo_de_datos.md) §2.11.

Variables de condición (temperatura, vibración, presión, corriente…) medidas
a mano, por CSV, por API o desde el checklist de una OT; reglas de umbral que,
al cruzarse, abren una alerta y, por defecto, una OT predictiva. Las variables
**son medidores** de tipo variable (no acumulativos): se reusan lecturas,
CSV, `POST /api/v1/readings/bulk/` y la ficha del activo, sin tablas de series
aparte. Módulo de plan `condition` ("Mantenimiento por condición") en los
planes Prueba, Profesional y Empresa; Básico no lo incluye.

Decisiones de producto (2026-10-12):
- Variables = medidores reutilizados con tipos nuevos no acumulativos (D1).
- Al cruzar el umbral: alerta + OT predictiva, una sola abierta por regla y
  medidor; la regla puede ser "solo alertar" (D2).
- Los ítems de medición del checklist alimentan una variable del activo (D3).
- Módulo `condition` en Prueba, Profesional y Empresa, como `inventory` (D4).

**CBM-01 · Variables de condición en el activo (F2)**
Como planificador quiero registrar qué variables mido en cada equipo, para
seguir su condición sin una herramienta aparte.
- CA1: Tipos nuevos de medidor: temperatura (°C), vibración (mm/s), presión
  (bar), corriente (A) y variable (otra, unidad libre). Unidad por defecto
  según el tipo, editable.
- CA2: Siempre no acumulativos: el servicio fuerza `is_cumulative = False`
  (no hay reinicio ni rechazo por lectura menor) y admiten valores negativos
  (p. ej. temperaturas bajo cero); los tipos de uso (horas, km, ciclos, otro)
  no cambian.
- CA3: Alta y edición desde la ficha del activo con `asset.manage`; la clave
  de una variable en un activo es (tipo, nombre), única por activo.
- CA4: Los tipos variable no se ofrecen como disparador de planes por uso: se
  filtran en el formulario de la versión y el servicio rechaza una versión
  con `meter_kind` variable.
- CA5: No se borran: se desactivan (el historial y las alertas siguen
  visibles).

Implementado (2026-10, paquete C4): "Agregar variable" en la sección
"Condición" de la ficha (`/assets/<id>/variables/new/`, pide además el módulo
`condition`), "Editar" (nombre y unidad; el tipo no cambia) y
"Desactivar"/"Reactivar" con `asset.manage`, en `assets.services.create_variable`,
`update_variable` y `set_variable_active` (auditados). La clave (tipo, nombre)
se compara sin distinguir mayúsculas y también contra las desactivadas (el
error invita a reactivar). "Otra variable" exige nombre. Una variable
desactivada rechaza lecturas de cualquier fuente; sus lecturas y alertas
siguen en la ficha ("N variables desactivadas").

**CBM-02 · Lecturas de variables (F2)**
- CA1: Registro manual desde la ficha del activo y desde "Registrar lectura"
  con `meter_reading.create` (sin cambios de permisos).
- CA2: CSV de lecturas (AST-09) con columnas opcionales `tipo` y `nombre` para
  elegir la variable cuando el activo tiene varias; mismo patrón de vista
  previa con errores por fila.
- CA3: `POST /api/v1/readings/bulk/` acepta `meter` (id) o `asset_code` +
  `kind` + `name`; resultado por fila como hoy.
- CA4: Al completar una OT, los ítems de medición enlazados registran su
  lectura (CBM-06).
- CA5: Todas las fuentes pasan por el mismo servicio (`record_meter_reading`)
  y son idempotentes por `client_uuid`.

Implementado (2026-10, paquete C4): CA1, CA2 y CA5 en la web. El formulario de
lectura acepta negativos solo en variables (y no muestra "reinicio"). El CSV
lee las columnas `tipo` (código, etiqueta o palabra en español: temperatura,
vibración, presión, corriente, otra, horas, km, ciclos) y `nombre`; sin ellas
un activo con varios medidores da error por fila pidiendo tipo y nombre, y un
`tipo` sin `nombre` con dos variables de ese tipo pide el nombre. Cada fila
importada lleva un `client_uuid` derivado de (medidor, fecha, valor). El
serializer `MeterReadingCreateSerializer` de la API tenía `min_value=0` y
`BulkReadingRowSerializer` no exponía `kind`/`name`: quedaron para C5 (ver
abajo).

Implementado (2026-10, paquete C5): CA3. `POST /api/v1/readings/bulk/` acepta
`kind` y `name` por fila (mismas reglas y mensajes que el CSV) y
`POST /api/v1/readings/` acepta negativos solo en variables (en medidores de
uso, 400 en `value`); una variable desactivada responde 400 (lectura suelta)
o error de fila (carga masiva). También vale para `POST /field/api/readings/`
(mismo serializer).

**CBM-03 · Reglas de condición (F2)**
Como planificador quiero definir una vez el rango normal de una variable, para
que el sistema me avise y cree la OT cuando se salga.
- CA1: Alcance: un medidor concreto, o una clase de activo + tipo + nombre de
  variable (aplica a todas las variables con esa clave en activos de la clase,
  incluidas las que se creen después).
- CA2: Operadores: mayor que (`gt`), menor que (`lt`), fuera de rango
  (`outside`, [mín, máx]) y tasa de cambio (`rate`: cambio por día respecto de
  la lectura anterior, en valor absoluto, mayor que el umbral).
- CA3: Lecturas consecutivas fuera de rango para alertar (por defecto 1;
  filtra ruido de sensores).
- CA4: Acción: "alerta y OT" (por defecto) o "solo alerta". Datos de la OT:
  título (por defecto "Condición: <variable> en <activo>"), prioridad (por
  defecto según la criticidad del activo, como en las fallas) e
  instrucciones.
- CA5: Validaciones en el servicio (web y API): en `outside` mín < máx; en
  `rate` umbral > 0; el alcance es exactamente uno (medidor o clase + clave);
  el medidor y la clase son de la organización.
- CA6: No se borran: `is_active`. Alta y edición auditadas; requieren
  `condition.manage`.

Implementado (2026-10, paquetes C1 y C2): `condition.services.create_rule`,
`update_rule` (el alcance no cambia: se crea otra regla) y `set_rule_active`,
con las validaciones de CA5 en el servicio y CHECK en la base (incluida la
prioridad 1–4). Pantallas `/condition/rules/` (lista con inactivas a pedido),
`rules/new/`, `rules/<id>/edit/` y activar/desactivar. Los umbrales admiten
negativos (p. ej. cámara de frío sobre −15 °C). Desactivar una regla
normaliza sus episodios abiertos y deja sus estados en normal.

**CBM-04 · Evaluación e histéresis (F2)**
- CA1: Cada lectura nueva se evalúa al registrarse, dentro de la misma
  transacción, contra las reglas activas que aplican a su medidor (directas y
  por clase), para todas las fuentes (manual, CSV, API, OT, terreno).
- CA2: Solo la lectura más reciente del medidor evalúa: las lecturas
  históricas (con fecha anterior a la vigente) no alertan.
- CA3: Estado por regla y medidor (normal / alarma, contador de consecutivas,
  última lectura evaluada): se alerta solo al pasar de normal a alarma.
  Mientras siga en alarma no hay alertas ni OT nuevas; se actualiza el último
  valor de la alerta abierta.
- CA4: Al volver a rango la alerta queda "normalizada" y la OT sigue su curso;
  un nuevo cruce después abre un episodio nuevo.
- CA5: Una sola OT abierta por regla y medidor: con acción "alerta y OT" se
  crea una OT predictiva (tipo `predictive`, estado planificada, creada por el
  sistema) con el activo, el título, la prioridad y las instrucciones de la
  regla. Si un episodio nuevo empieza mientras la OT del anterior sigue
  abierta, la alerta nueva se enlaza a esa OT y no se crea otra.
- CA6: Reenviar la misma lectura (`client_uuid`) no vuelve a evaluar ni
  duplica alertas u OT.
- CA7: Número acotado de consultas por lectura (índices compuestos con
  `organization_id`), probado con `django_assert_max_num_queries`.

Implementado (2026-10, paquete C1): `condition.services.evaluate_reading`,
llamada por `record_meter_reading` solo para variables y solo con la lectura
más reciente; estados con `select_for_update` en orden de regla. Si hubo
lecturas sin evaluar entre la última evaluada y la nueva (módulo apagado o
regla inactiva), el estado arranca en normal y el episodio anterior queda
normalizado. Una variable desactivada rechaza lecturas, así que no se
evalúa. CA7 en `tests/test_query_counts.py`
(`test_condition_evaluation_queries_are_bounded`).

**CBM-05 · Alertas (F2)**
- CA1: `/condition/` lista las alertas con las abiertas primero; filtros por
  estado, sitio y activo; paginada y con el alcance por sitio de la membresía.
- CA2: Cada alerta muestra regla, variable, activo, valor que la abrió, último
  valor y fecha, estado (ícono + texto: abierta, reconocida, normalizada,
  cerrada) y enlace a la OT.
- CA3: "Reconocer" con `condition.manage` (queda quién y cuándo); al cerrarse
  la OT vinculada la alerta pasa a cerrada.
- CA4: Alertas inmutables salvo sus transiciones de estado; cada transición
  auditada.
- CA5: Contador "Alertas de condición (N)" en el inicio.
- CA6: Aviso `condition_alert` (app y correo según preferencias) a quienes
  tienen `work_order.manage` y ven el sitio del activo; se encola con
  `on_commit`.

Implementado (2026-10, paquetes C1 y C2): `/condition/` (filtros estado,
sitio y activo; paginada), detalle `/condition/alerts/<id>/` y "Reconocer";
contador en el inicio y enlaces en la navegación y en "Más". Al cerrar o
cancelar la OT vinculada, `apps.work` llama a
`condition.services.close_alerts_for_work_order` (sin signals) y sus alertas
pendientes pasan a cerradas.

**CBM-06 · El checklist alimenta variables (F2)**
Como técnico quiero que la temperatura que anoto en la pauta quede como
lectura del equipo, sin registrarla dos veces.
- CA1: Un ítem de medición del plan (borrador) se puede enlazar a una variable
  por tipo + nombre (opcional); el enlace se copia al checklist de la OT.
- CA2: Al completar la OT, cada ítem enlazado con valor registra una lectura
  (origen OT, fecha = respuesta del ítem o cierre), una por ítem, idempotente
  (la OT guarda la lectura en el ítem).
- CA3: Si el activo no tiene esa variable, se crea sola con la unidad del
  ítem.
- CA4: Igual en la web, la API v1 y la PWA (mismo servicio de cierre).
- CA5: Versiones publicadas inmutables (ADR-016): el enlace se agrega en un
  borrador nuevo; los ítems existentes no cambian.

Implementado (2026-10, paquete C3): campos "Variable" y "Nombre de la
variable" en el editor de ítems del borrador y columnas opcionales en el
Excel de planes (PM-08; los archivos sin ellas se siguen leyendo); también se
copia al aplicar un checklist de plan a una OT. La lectura la registra
`work.services.record_checklist_readings`, llamada al pasar la OT a
completada. Una variable desactivada no se reactiva: el ítem no registra
lectura. La PWA de terreno aún no muestra el enlace en el ítem (sí registra
la lectura al completar).

**CBM-07 · Condición en la ficha del activo (F2)**
- CA1: Sección "Condición" con cada variable: último valor y fecha, estado
  (ícono + texto: normal, alarma, sin reglas) y enlace a sus alertas.
- CA2: Tendencia de los últimos 90 días como SVG generado en el servidor, con
  líneas de umbral de las reglas, colores por tokens y su tabla; sin JS ni
  estilos en línea; a 360 px sin scroll horizontal.
- CA3: Número fijo de consultas (`tests/test_query_counts.py`).

Implementado (2026-10, paquete C4): sección "Condición" de la ficha con el
módulo `condition` y `condition.view` (`condition.selectors.asset_condition`,
cinco consultas). Estados: normal, alarma, sin reglas, sin lecturas (tiene
reglas pero aún no hay lecturas) y desactivada. La tendencia la arma
`condition.charts.build_trend` (función pura) con las reglas activas por
medidor y por clase (`gt`/`lt` una línea, `outside` dos; `rate` no dibuja
línea); las lecturas fuera de rango se marcan en el gráfico y en la tabla
(las 30 más recientes). El enlace a alertas filtra `/condition/` por activo.
Sin el módulo o sin `condition.view`, las variables se muestran y se leen en
"Medidores".

**CBM-08 · Permisos, módulo y aislamiento (F2)**

| Permiso | admin | planner | supervisor | technician | viewer | external |
|---|---|---|---|---|---|---|
| `condition.view` | ✔ | ✔ | ✔ | ✔ | ✔ | — |
| `condition.manage` (reglas, reconocer alertas) | ✔ | ✔ | — | — | — | — |

- CA1: Registrar lecturas sigue siendo `meter_reading.create`; dar de alta
  variables, `asset.manage`. El token de API actúa con permisos de
  planificador.
- CA2: Sin el módulo `condition`: `/condition/…` responde 403 con mensaje de
  plan; desaparecen la navegación, el contador y la sección de la ficha; la
  API de condición responde 403. Las variables siguen siendo medidores y sus
  lecturas se registran, pero no se evalúan reglas.
- CA3: Tests de aislamiento de cada modelo, vista y endpoint, incluidos IDs
  cruzados (medidor, clase, regla) en formularios y API. RLS automático
  (`post_migrate` de `apps/core/rls.py`), verificado en
  `tests/isolation/test_rls.py`.

**CBM-09 · API v1 de condición (F2)**
- CA1: `GET/POST /api/v1/condition-rules/`, `GET/PATCH
  /api/v1/condition-rules/{id}/` (sin borrado: `is_active`), mismo servicio y
  validaciones que la web.
- CA2: `GET /api/v1/condition-alerts/` (filtros estado, activo), detalle y
  `POST /api/v1/condition-alerts/{id}/acknowledge/`.
- CA3: `POST /api/v1/readings/bulk/` con `kind`/`name` (CBM-02 CA3).
- CA4: Paginación, errores y alcances read/write como el resto de la API;
  esquema sin advertencias; documentado en [08_api.md](08_api.md).

Implementado (2026-10, paquete C5): CA1–CA4 en `apps/api/v1.py`
(`ConditionRuleViewSet`, `ConditionAlertViewSet`) con los servicios y
selectores de `apps/condition`. Reglas: filtros `include_inactive`,
`asset_class` y `meter` (las que aplican a la variable); `POST` idempotente
por `client_uuid` (`ApiIdempotencyKey`, sin migración); `PATCH` edita la
condición, la acción y la OT o `is_active`, y rechaza cambios de alcance (400).
Alertas: filtros `status` (o `active`) y `asset`; reconocer una alerta
normalizada o cerrada → 400 (como los errores de estado de la v1). Leer exige
`condition.view` y escribir `condition.manage` (el token, como planificador,
tiene ambos); sin el módulo `condition` → 403 con mensaje de plan. Fuera de la
API: alta y baja de variables (siguen en la ficha del activo).

**CBM-10 · Datos demo, exportación y purga (F2)**
- CA1: `seed_demo` crea variables, reglas por clase e historia con al menos un
  episodio de alarma con su OT predictiva.
- CA2: Las tablas nuevas entran en la exportación completa y la purga (X-03,
  X-04, ADR-027), probado en `tests/test_org_export_delete.py`.

Implementado (2026-10, paquete C6): CA1 en
`apps/core/management/commands/_seed_condition.py` (Alimentos del Sur): 15
variables en 10 activos (temperatura, vibración, presión, corriente), 4 reglas
por clase (compresores, envasadoras, equipos de frío) y 6 por activo (`gt`,
`lt`, `outside`, `rate`; una inactiva, reemplazada), 90 días de lecturas con
ruido y episodios evaluados por el servicio real: 2 alertas abiertas (una con
OT predictiva), 1 reconocida, 1 normalizada y 1 cerrada por su OT predictiva
completada. En la ficha aparecen todos los estados (alarma, normal, sin
reglas, sin lecturas, desactivada). El plan "Inspección mensual compresor"
enlaza "Temperatura de descarga" a la variable (CBM-06). Probado en
`tests/test_seed_demo.py` (estados coherentes con la última lectura). CA2:
las tablas de condición y el enlace `meter_reading` de los ítems de la OT
(PROTECT) entran en la exportación y la purga (descubren las tablas
`TenantModel` solas; las FK diferidas permiten borrar en cualquier orden).
Maestranza Norte (plan Prueba, con el módulo) queda sin datos de condición.

**Fuera de v1:** ingesta en tiempo real (MQTT, OPC-UA, conectores SCADA);
tablas de series de tiempo particionadas; reglas compuestas (varias
variables) o estadísticas (promedio móvil, desviación); crear solicitud en vez
de OT; umbrales por organización fuera de las reglas; predicción o IA sobre la
tendencia; gráficos interactivos con JS.

## 8. Indicadores y reportes (`KPI`)

**KPI-01 · Indicadores básicos (M1)**
Con filtros por fecha, sitio, clase y activo:
- Disponibilidad = (tiempo calendario − detención) / tiempo calendario.
- MTBF = tiempo operativo / número de fallas con detención.
- MTTR = tiempo total de reparación / número de reparaciones.
- Cumplimiento del preventivo = OT preventivas cerradas dentro de tolerancia /
  OT preventivas vencidas en el período. "Dentro de tolerancia" es la regla de
  PM-02 CA3 (`is_on_time`): fecha límite ± tolerancia del plan, o lectura
  programada ± tolerancia; basta una. Preventivas sin plan: tolerancia cero.
  No cuentan (fuera del denominador) las canceladas ni las abiertas cuya
  ventana de tolerancia aún no termina; las abiertas con la ventana cumplida
  cuentan como incumplidas. Se calcula en una consulta, sin consultas por OT.
- CA: Cada KPI muestra su fórmula y referencia (EN 15341 / SMRP) en un tooltip.
- CA: Resultados verificados por tests con datos de fixture de valor conocido.

**KPI-02 · Indicadores ampliados (F2)**: backlog en semanas en el tiempo,
proporción preventivo/correctivo, costos por activo/clase, Pareto de fallas.
Implementado (2026-10) en `/reports/kpis/` (módulo `kpis` del plan; enlace en el
inicio y en "Más"), con los filtros del tablero (período —por defecto las
últimas 12 semanas—, sitio, clase, criticidad, activo), el alcance por sitio
de la membresía y los mismos roles que ven el tablero. Selectores en
`apps/reporting/extended.py`.
- CA1 ✔ Backlog en semanas en el tiempo: un punto por semana hacia atrás desde
  el fin del período (12 por defecto, hasta 53). Misma definición que REQ-04
  CA2: horas estimadas de las OT pendientes (ni completadas, ni cerradas, ni
  canceladas; incluye las solicitadas) / capacidad semanal de los técnicos del
  alcance. Se reconstruye a cada fecha con creación, término/cierre y la fecha
  del cambio de estado a cancelada (sin snapshots nuevos; una cancelada sin
  ese registro usa su última modificación). La capacidad es la vigente: no
  hay historial de capacidad. Sin capacidad se muestran horas, no semanas.
- CA2 ✔ Planificado vs. correctivo: OT completadas en el período (por fecha
  de término, sin las canceladas) por tipo y mes; planificado = preventiva +
  predictiva/condición + inspección, correctivo = correctiva; las de mejora se
  informan aparte, fuera del %. % planificado por horas registradas (SMRP
  Planned Maintenance Percentage) y por cantidad.
- CA3 ✔ Costos por activo y por clase: OT cerradas en el período con los
  totales guardados al cierre (WO-04 CA3): mano de obra, repuestos, otros y
  total. Top 10 activos con "Ver todos" paginado (25 por página) y agregado
  por clase; las OT sobre una ubicación van en una fila aparte. Cada OT suma a
  su activo directo (no hay convención de sumar componentes al padre en
  costos: `include_components` de `asset_kpis` es una opción de la ficha).
  Moneda: no existe un campo de moneda en la organización; se muestra `$` como
  en la OT.
- CA4 ✔ Pareto de fallas por modo o por causa (catálogo ISO 14224): cantidad,
  horas de detención (fallas con detención total, como MTTR, cortadas al fin
  del período) y % acumulado, de mayor a menor según "Cantidad" o
  "Detención"; se marcan (ícono + "80 %") los códigos hasta el que cruza el
  80 %. Las fallas sin código quedan fuera del Pareto y se informan aparte.
- CA5 ✔ Cada indicador muestra su fórmula y referencia (SMRP / EN 15341 /
  ISO 14224) en el desplegable "Fórmula"; "Exportar a Excel" (módulo
  `exports`) baja un libro con una hoja de filtros y una por indicador (costos
  por activo completos).
- CA6 ✔ Gráficos SVG generados en el servidor (sin JS ni estilos en línea),
  colores por tokens, siempre con su tabla; a 360 px sin scroll horizontal.
- CA7 ✔ Cada indicador en un número fijo de consultas agregadas
  (`tests/test_query_counts.py`).
- No incluido: historial de capacidad de técnicos, moneda configurable,
  gráfico de línea del % acumulado (se muestra como barra acumulada por fila),
  exportación PDF de estos indicadores y endpoints de API.

**KPI-03 · Exportación (M1)**: listas y reportes a Excel; ficha de activo y OT
a PDF con marca de la organización.

## 9. Transversales (`X`)

**X-01 · Auditoría (M1)**: toda creación, edición, eliminación, transición y
acceso de administración queda registrada (quién, qué, cuándo, desde dónde,
cambios). Consultable y filtrable por el administrador. Solo inserción.

**X-02 · Notificaciones (M1)**: en la app y por correo: OT asignada, OT por
vencer/vencida, solicitud nueva, solicitud resuelta. Preferencias por usuario.
En F2 se suman stock bajo mínimo (`stock_low`, INV-12) y alerta de
condición (`condition_alert`, CBM-05).

**X-03 · Exportación completa de datos (M1)**: el administrador descarga un ZIP
con todos los datos de su organización (Excel/CSV + adjuntos). Implementado: un CSV
por tabla + archivos, armado en segundo plano, vence a los 7 días (ADR-027).

**X-04 · Borrado de organización (M1)**: a pedido, con período de gracia de
30 días; luego borrado de datos y archivos, registrado. Implementado: purga diaria
con lápida de la organización; las cuentas de usuario se conservan (ADR-027).

**X-05 · API REST (M1)**: OpenAPI en `/api/schema/`, documentación navegable,
autenticación por token de organización con alcances; endpoints de activos,
lecturas, OT y solicitudes. Desde la API también se completa una OT y se
responde su checklist con las mismas reglas que la web (ADR-005).

**X-06 · Modo offline PWA (F2)**: ver [04_arquitectura.md](04_arquitectura.md), ADR-011.
Como técnico quiero ejecutar mis OT sin señal y que lo hecho llegue solo al
servidor cuando vuelva la conexión. Implementado en `/field/` (2026-10).
- CA1 ✔ Entrada opcional "Modo sin conexión" en Mis OT, Mi semana y "Más"
  (quien ejecuta OT, con el módulo `work_orders`); instalable (manifest e
  íconos 192/512/maskable).
- CA2 ✔ Descarga todas las OT visibles de los próximos 7 días (asignadas
  primero, tope 200), catálogos de falla, activos y medidores; se ven sin señal
  hasta 14 días.
- CA3 ✔ Sin señal: iniciar, pausar, reanudar y completar; responder el
  checklist (con firma), fotos de ítems y de la OT, horas al completar y horas
  de apoyo, lectura de medidor y solicitud con foto; repuestos previstos
  ("Usar") y agregados del catálogo descargado o en texto libre (INV-13,
  2026-10).
- CA4 ✔ Cola con `client_uuid`: reintentar nunca duplica; correlativos al
  sincronizar; barra de estado accesible (pendientes, enviando, conflictos).
- CA5 ✔ Conflictos: OT cancelada o cerrada → conflicto con "Descartar";
  datos rechazados → "Corregir y reenviar"; checklist: gana la última respuesta
  y la otra queda en auditoría.
- CA6 ✔ Sesión vencida o MFA pendiente pausan la cola sin perder cambios;
  versión vieja del cliente → "Actualizar".
- CA7 ✔ Teléfono compartido: "Salir" borra los datos (con pendientes, pide
  confirmar y cuántos se pierden); cambiar de organización u otra persona
  borra los datos descargados; un cambio nunca se envía con la sesión de otra
  organización (409 `organization_changed`).
- CA8 ✔ Móvil 360 px, objetivos ≥ 48 px, texto ≥ 16 px, sin JS en línea
  (CSP), solo `textContent`; e2e Playwright (`tests/e2e/`, a pedido y como job
  manual del CI).
- No cumplido / fuera de v1: QR sin conexión; crear repuestos del catálogo
  sin conexión (los repuestos en sí entraron con INV-13); ver fotos o manuales sin conexión; Background Sync y
  push; crear, aprobar o cerrar OT; varias organizaciones a la vez; horas con
  inicio y fin explícitos; `/field/` como vista por defecto del técnico (se
  decide tras el piloto); asistente IA sin conexión. Los datos de IndexedDB no
  van cifrados en el teléfono (mitigado con el borrado al salir y el
  vencimiento de 14 días).
- ✔ Correctiva desde hallazgo sin conexión (WO-06 CA2, paquete F1,
  2026-10): "Avisar hallazgo" en un ítem No OK, encolado antes de
  "Completar". Los ítems de medición muestran además la variable que
  alimentan ("Alimenta Temperatura (°C) · Rodamiento", CBM-06).

**X-07 · i18n (M0)**: todos los textos marcados para traducción; español (Chile)
completo; inglés y portugués en F3.

**X-08 · Webhooks salientes (F3, parcial)**: como administrador quiero que otro
sistema (ERP, chat, BI) se entere al instante de lo que pasa en el CMMS, sin
consultar la API cada cierto tiempo. Implementado (2026-10, ADR-030).
- CA1 ✔ En "Más" → "Webhooks" (`/settings/webhooks/`, permiso
  `api_token.manage`, solo administradores, módulo `api`: Profesional y
  Empresa) se crean, editan y desactivan destinos: nombre, URL https y eventos.
  El secreto de firma se genera solo y lo ve únicamente el administrador.
- CA2 ✔ Eventos v1: `work_order.created`, `work_order.completed` y
  `work_request.created`, disparados por los mismos servicios que usan la web,
  la API y terreno, al confirmar la transacción (si se revierte, no sale nada).
- CA3 ✔ Cuerpo JSON con `id` de evento (UUID, para idempotencia), `type`,
  `created_at` (UTC), `organization` y `data` con los mismos campos que la API
  v1; cabecera `X-Galbust-Signature: t=<unix>,v1=<HMAC-SHA256>`.
- CA4 ✔ Timeout de 5 s; con worker, hasta 5 intentos con backoff; tras 5
  eventos fallidos seguidos el destino se desactiva, queda en auditoría y los
  administradores reciben el aviso `webhook_disabled` (X-02).
- CA5 ✔ Contra SSRF: se rechazan URL sin https o que resuelven a direcciones
  privadas, de loopback, link-local o reservadas, al guardar y en cada envío
  (la conexión va a la IP validada; no se siguen redirecciones).
- CA6 ✔ "Enviar prueba" (`webhook.test`) y "Últimas entregas" paginadas con
  estado, intentos, código HTTP y último error. Sin borrado físico.
- No cumplido / fuera de v1: más eventos (activos, lecturas, alertas de
  condición, cambios de estado intermedios), regenerar el secreto, reenviar a
  mano una entrega fallida, ver el cuerpo enviado en la pantalla, depurar
  entregas antiguas y reintentos sin worker.

## 10. Asistente de diagnóstico con IA (`AI`, F2)

**Estado:** hecho (F2, 2026-10, paquetes A1–A3: núcleo, pantallas, demo y
vista de costo). Lo que no entró está en "Fuera de v1" al final de esta
sección y en ADR-018. Decisiones en
[04_arquitectura.md](04_arquitectura.md), ADR-018; modelo en
[03_modelo_de_datos.md](03_modelo_de_datos.md) §2.12; paquetes en
[05_roadmap.md](05_roadmap.md).

Decisiones de producto (2026-10-17):
- Modelo por defecto Claude Opus 5.5 (`claude-opus-5-5`, ajuste
  `AI_ASSISTANT_MODEL`) con esfuerzo `medium` (`AI_ASSISTANT_EFFORT`); se
  compara con Claude Sonnet 5.5 usando las marcas "¿Te sirvió?" del piloto (D1).
- Módulo de plan `assistant` ("Asistente de diagnóstico") solo en Profesional
  y Empresa (no Prueba ni Básico), con cupo mensual de conversaciones por
  organización: Profesional 100, Empresa 500 (editable en el admin; en blanco
  = sin límite) (D2).
- La demo trae conversaciones ya respondidas, con citas a OT y manuales del
  seed; sin `ANTHROPIC_API_KEY`, "Pedir ayuda" se muestra pero avisa "El
  asistente no está configurado" y no falla (D3).

Permisos: `assistant.use` (administrador, planificador, supervisor y técnico)
y `assistant.view_all` (administrador: consulta las conversaciones de toda la
organización). Cada usuario ve las suyas; siempre con el alcance por sitio
(una conversación sobre un activo fuera del alcance no se ve). Solo quien la
inició pregunta, pide la nota de cierre o marca si sirvió.

**AI-01 · Pedir ayuda desde el activo o la OT**
Como técnico quiero describir un síntoma y recibir orientación basada en el
historial y los manuales de ese equipo.
- CA1: Botón "Pedir ayuda" en la ficha del activo (también al llegar por QR)
  y en la OT; la conversación queda asociada al activo y, si aplica, a la OT
  (que debe ser de ese mismo activo).
- CA2: El asistente ya conoce el activo (clase, fabricante, modelo,
  ubicación, estado, medidores con su último valor, últimas fallas, OT
  abiertas y la OT asociada): va en el primer mensaje; el técnico no tiene que
  explicarlo.
- CA3: Entrada por texto (con dictado del teclado) y foto (placa, alarma,
  pieza dañada), una por pregunta, comprimida como cualquier adjunto (AST-06).
- CA4: Usable completo desde el teléfono (ADR-017); texto ≥ 16 px.
- CA5: La respuesta llega sin bloquear la pantalla: se muestra "Pensando…" y
  la página consulta cada ~2 s hasta que el turno termina (en el worker con
  `AI_ASSISTANT_USE_QUEUE=1`, si no en la misma petición). No se admite otra
  pregunta mientras una espera; un turno pendiente por más de 15 minutos pasa
  a error.
- CA6: Si la API falla, rechaza la consulta o la respuesta queda truncada, el
  mensaje muestra un texto claro ("inténtalo de nuevo", "haz una pregunta más
  acotada"…) y la conversación sigue usable.

**AI-02 · Respuestas con fuente**
- CA1: Cada recomendación indica su origen con ícono + texto: manual
  (documento y página, enlazado), historial (código de OT, enlazado),
  lecturas de un medidor o conocimiento general (marcado como de menor
  confianza: todo párrafo sin fuente).
- CA2: Si no hay información suficiente, lo dice y sugiere qué dato falta.
- CA3: Solo usa datos que el usuario puede ver (organización y alcance por
  sitio); probado en `tests/isolation/`.
- CA4: Solo se enlazan fuentes que el backend entregó en esa conversación
  (contexto del activo o resultado de una herramienta); un marcador de fuente
  inventado por el modelo se descarta.

**AI-03 · Seguridad**
- CA1: Recuerda aislar energías (bloqueo y etiquetado) antes de intervenir y
  nunca sugiere anular protecciones ni enclavamientos.
- CA2: Aviso visible: la orientación no reemplaza el criterio del técnico ni
  los procedimientos de la empresa.
- CA3: Los textos de OT y manuales que lee el asistente se tratan como datos,
  no como instrucciones (inyección de instrucciones); no se envían nombres de
  personas en los resultados de las herramientas.

**AI-04 · Cierre del ciclo**
- CA1: "Proponer nota de cierre" genera un resumen de la conversación
  (síntoma, causa, acción); al completar la OT aparece pre-cargado y editable
  como nota de cierre, y el técnico puede descartarlo.
- CA2: El técnico marca si la ayuda sirvió (sí / en parte / no), en la
  conversación y al cerrar; se puede cambiar y queda en la auditoría.

**AI-05 · Cupo, costo y auditoría**
- CA1: Cupo mensual de conversaciones por organización según el plan
  (ORG-10): se cuentan las conversaciones nuevas del mes calendario en la zona
  de la organización (preguntar en una conversación ya abierta no consume
  cupo). Al agotarse se informa y no se crea la conversación, sin bloquear el
  resto del sistema.
- CA2: Cada turno guarda modelo y tokens usados (entrada, salida, leídos y
  escritos en caché, sumados en todas las rondas de herramientas, también si
  el turno falla); el superadmin ve el costo estimado por organización y mes
  (precios por modelo en `AI_MODEL_PRICES_USD`).
- CA3: Conversaciones consultables por el administrador, incluidas en la
  exportación completa (X-03) y en el borrado de organización (X-04). Abrir
  una conversación y marcar si sirvió quedan en la auditoría.

Implementado (2026-10, paquetes A1–A3): AI-01..AI-04 en `apps/assistant`
(servicios, herramientas, `llm.py`, pantallas `/assistant/` y nota de cierre
en el formulario de completar OT). AI-05 CA1 en `selectors.quota_status` y
`Plan.max_assistant_conversations`; CA2 en el admin de Django → "Costo del
asistente" (solo superadmin: tokens y costo estimado en USD por organización
y mes calendario UTC, últimos 12 meses, con `AI_MODEL_PRICES_USD` incluida la
lectura y escritura de caché; `selectors.usage_by_organization_month`); CA3:
conversaciones, mensajes y la foto de la pregunta entran a la exportación y
se borran en la purga (`tests/test_org_export_delete.py`). Demo (D3):
`apps/core/management/commands/_seed_assistant.py` carga 3 manuales PDF con
texto y 4 conversaciones ya respondidas en Alimentos del Sur (plan
Profesional, que ya incluye el módulo: no se cambió el plan), con citas a OT,
manuales y medidores validadas como en una respuesta real; una base demo sin
asistente se recrea sola en Render (`seed_demo --reset-if-stale`).

**Fuera de v1:** asistente sin conexión o dentro de la PWA `/field/` (a lo más
un enlace cuando hay conexión); endpoints del asistente en la API v1;
reconocimiento de voz propio (se usa el dictado del teclado); `pgvector` y
búsqueda semántica; índices GIN de texto completo (la búsqueda se calcula al
consultar); OCR de manuales escaneados; respuesta token a token en la pantalla;
varias fotos o PDF por pregunta; compartir conversaciones entre técnicos.

---

## Requisitos no funcionales

| ID | Categoría | Requisito | Verificación |
|---|---|---|---|
| NF-01 | Seguridad | OWASP Top 10 cubierto: CSRF, XSS (autoescape), inyección (ORM), control de acceso en servicios y selectors, cabeceras seguras. | Tests de permisos; `manage.py check --deploy` en CI; revisión antes de cada release. |
| NF-02 | Seguridad | HTTPS obligatorio, HSTS, cookies `Secure`/`HttpOnly`/`SameSite=Lax`. | `check --deploy`, prueba en staging. |
| NF-03 | Seguridad | Contraseñas: mínimo 10 caracteres, validadores de Django, verificación contra contraseñas filtradas (opcional). Hash Argon2. | Tests. |
| NF-04 | Seguridad | Límite de intentos de acceso por usuario e IP; bloqueo temporal. | Tests. |
| NF-05 | Seguridad | Secretos solo en variables de entorno; `.env` fuera del repo. | Revisión de secretos en CI (gitleaks). |
| NF-06 | Seguridad | Almacenamiento de objetos cifrado en reposo; URLs de descarga firmadas y de corta duración. | Configuración del proveedor. |
| NF-07 | Aislamiento | Ningún dato de una organización es accesible desde otra, por UI, API, archivos, exportaciones ni tareas. | Suite `tests/isolation/` obligatoria; RLS de PostgreSQL activo antes del piloto con datos reales (ADR-026, `tests/isolation/test_rls.py`, `manage.py rls_status`). |
| NF-08 | Privacidad | Exportación completa y borrado a pedido (X-03, X-04). | Tests + prueba manual por release. |
| NF-09 | Respaldo | Respaldo diario de la base, retención ≥ 7 días (idealmente 30), restauración probada trimestralmente en staging. | Registro de la prueba. |
| NF-10 | Rendimiento | Listas paginadas siempre; sin N+1 (tests con `django_assert_max_num_queries`); p95 < 500 ms en páginas principales con 5.000 activos por organización. | Tests y datos sintéticos. |
| NF-11 | Escala | Diseñado para cientos de organizaciones y miles de activos por organización; índices que empiezan por `organization_id`. | Revisión del modelo. |
| NF-12 | Calidad | Tests unitarios, de servicios, de aislamiento y de permisos; cobertura ≥ 80 % en `services` y `selectors`. | CI. |
| NF-13 | Calidad | Lint y formato (ruff), chequeo de migraciones, CI en cada push. | CI. |
| NF-14 | Operación | Ambientes dev, staging y producción; despliegue automático a staging en `main` y manual a producción. | Pipeline. |
| NF-15 | Operación | Monitoreo de errores (Sentry), logs estructurados, `/healthz` (vida) y `/readyz` (base de datos). | Staging. |
| NF-16 | Accesibilidad | WCAG 2.2 AA: contraste, foco visible, navegación por teclado, etiquetas; objetivos táctiles ≥ 48 px en vistas de técnico. | Test automático de contraste de tokens + revisión manual. |
| NF-17 | Móvil | Mobile first: **toda** funcionalidad (no solo la del técnico) se completa desde un teléfono de 360 px, sin scroll horizontal; alto contraste para exteriores (ADR-017). | `tests/ui/test_mobile.py` en CI + prueba a 360 px por vista + revisión en dispositivo real por release. |
| NF-18 | Tiempo | Fechas en UTC en la base; se muestran en la zona del sitio o de la organización. | Tests con zonas distintas. |
| NF-19 | i18n | Ningún texto de interfaz sin marcar para traducción. | Revisión + `makemessages` en CI. |
