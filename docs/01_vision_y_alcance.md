# 01 — Visión y alcance

**Producto:** GALBUST CMMS
**Estado:** borrador v0.1 (2026-09-24)

## 1. Problema

Las empresas productivas pequeñas y medianas (manufactura, agroindustria,
alimentos, packing, maestranzas, constructoras y transportistas con flota
propia) mantienen sus equipos con planillas Excel, cuadernos y WhatsApp. El
resultado es conocido:

- Preventivos que dependen de la memoria de una persona: se atrasan o se olvidan.
- Fallas que se reparan pero no se registran, así que nadie sabe qué equipo
  falla más ni cuánto cuesta.
- Cero indicadores: no se puede justificar un reemplazo, un técnico adicional
  o un contrato de servicio.
- Historial que se pierde cuando se va el encargado de mantención.

Existen CMMS, pero para una pyme fallan en tres cosas:

1. **Precio**: licencias por usuario pensadas para plantas grandes.
2. **Implementación**: semanas de configuración, consultores, catálogos vacíos
   que hay que llenar desde cero antes de ver valor.
3. **Complejidad de uso**: pantallas diseñadas para planificadores de
   mantenimiento, no para un técnico con guantes o un operador que solo quiere
   avisar que la máquina se detuvo.

## 2. Usuarios objetivo

| Persona | Quién es | Qué necesita |
|---|---|---|
| **Jefe de mantención / dueño** | Responsable del área, a veces el mismo dueño o gerente de operaciones. Poco tiempo. | Saber qué toca hacer esta semana, qué está detenido y cuánto cuesta. Configurar una vez y olvidarse. |
| **Técnico** | Mecánico, eléctrico o contratista. Trabaja en terreno, desde el celular. | Ver sus OT, ejecutar el checklist rápido, sacar fotos, cerrar. |
| **Operador / solicitante** | Operador de máquina, chofer, supervisor de producción. | Avisar una falla en segundos, idealmente escaneando un QR. |
| **Gerencia / solo lectura** | Gerente, dueño, cliente externo. | Indicadores simples: disponibilidad, costos, cumplimiento. |
| **Superadmin de plataforma** | GALBUST. | Dar de alta organizaciones, planes, soporte, carga asistida. |

## 3. Propuesta de valor

> **"Tu mantención ordenada en un día, no en tres meses."**
> Un CMMS que se configura solo a partir de plantillas, que genera las órdenes
> de trabajo automáticamente y que cualquier técnico entiende sin capacitación.

Principio rector: **el camino por defecto es el automático**. Cada decisión de
diseño se evalúa con la pregunta *¿esto le agrega un paso al cliente?*

## 4. Diferenciadores frente a CMMS existentes

Móvil, QR, plantillas, KPIs y precio pyme ya los ofrecen MaintainX, Limble,
UpKeep y, en Chile, Fracttal. Son **lo mínimo para competir**, no el motivo de
compra. GALBUST se diferencia en tres cosas:

| Diferenciador | Por qué importa | Cómo se materializa |
|---|---|---|
| **Cumplimiento normativo chileno listo** | Es el motivo de compra: la pyme no compra "orden", compra no ser multada ni tener un accidente. Los CMMS globales no lo harán para Chile. | Planes precargados por normativa (calderas y autoclaves DS 10, grúas horquilla, extintores NCh 1433, instalaciones eléctricas y de gas SEC, DS 594, revisión técnica de flota), con vencimientos, certificados como evidencia y **reporte listo para la fiscalización** (SEREMI, SEC, mutual). El contenido exacto se prioriza según el rubro de los pilotos (por definir). |
| **El conocimiento no se va con el técnico** | Es el motivo para quedarse: las pymes dependen de uno o dos expertos; la rotación borra años de experiencia. El valor crece con cada mes de uso. | Cada cierre de OT captura síntoma, causa y acción (dictable por voz). Un asistente con IA responde al técnico desde el activo con el historial y los manuales **de ese equipo**, citando la fuente (ADR-018). |
| **Puesta en marcha hecha por nosotros** | Es la barrera de entrada que la competencia deja al cliente. | "Mándanos tu Excel, fotos de las placas y tu cuaderno; en 48 horas tienes el sistema funcionando." Servicio manual al principio; lo repetido se automatiza (importación Excel, plantillas y, después, extracción desde fotos y manuales). |

**Base esperable (paridad con el mercado):**

| Capacidad | Cómo se materializa |
|---|---|
| **Biblioteca de plantillas** | Clases de activo con atributos, planes preventivos típicos (compresor, grúa horquilla, generador, camioneta, bomba, correa transportadora, caldera, etc.) y catálogo de fallas basado en ISO 14224. El cliente adopta y ajusta, no parte en blanco. |
| **Asignación automática de planes** | Al crear un activo de una clase, se le proponen (o asignan) los planes de esa clase. Al registrar un horómetro, se recalculan vencimientos. |
| **Aviso de falla por QR** | Etiqueta QR en cada equipo: escanear → describir → foto → enviar. Sin navegar menús. |
| **Todo desde el teléfono** | Toda la aplicación, para todos los roles, se usa completa desde un teléfono (ADR-017). Botones grandes, alto contraste, flujos de 3 toques. Offline en fase 2. |
| **Indicadores estándar sin configurar** | Disponibilidad, MTBF, MTTR y cumplimiento del preventivo desde el día 1, con definiciones de EN 15341 / SMRP explicadas en pantalla. |
| **Precio pyme y en español de Chile** | Precio por cantidad de activos, usuarios ilimitados en planes medios (los técnicos no deben ser un costo marginal). Soporte en español. |

Mensaje de venta propuesto: *"Cumple la normativa de mantención sin
planillas: tus equipos al día, la evidencia lista para la fiscalización y el
conocimiento de tus técnicos guardado en el sistema."*

## 5. Qué **no** haremos (al menos hasta validar el producto)

- **No** seremos un ERP: sin compras, contabilidad, remuneraciones ni facturación
  a clientes finales. Integramos por API/Excel.
- **No** haremos un motor de workflows genérico: el ciclo de vida de la OT es fijo
  con pasos activables (ver ADR-008).
- **No** haremos gestión de activos financiera (depreciación, activo fijo contable).
- **No** haremos telemetría/IoT propia ni almacenamiento de series de tiempo en el
  MVP: solo ingesta de lecturas por API/CSV (el diseño las permite después).
- **No** haremos app nativa (iOS/Android): web responsiva y luego PWA.
- **No** haremos cobro automático en el MVP: facturación manual.
- **No** apuntamos a gran minería ni plantas con SAP PM: no competimos en
  funcionalidad profunda con Maximo/SAP.
- **No** haremos personalización a medida por cliente dentro del producto
  (código específico para un cliente). Lo configurable es por organización y
  genérico.

## 6. Supuestos y riesgos

| Supuesto / riesgo | Mitigación |
|---|---|
| Las pymes pagarán por un CMMS si la implementación es rápida. | Validar con 2–3 pilotos antes de construir fase 2. |
| Una sola persona desarrolla y opera. | Stack aburrido, un solo repositorio, PaaS gestionado, sin Redis ni microservicios. |
| La conectividad en terreno es mala. | MVP web responsivo; offline como primera prioridad de fase 2 y datos modelados para ello desde ya (UUID de cliente, idempotencia). |
| Fuga de datos entre organizaciones. | Filtrado por tenant que falla cerrado + tests de aislamiento obligatorios + RLS antes del primer cliente pagado. |
| Ley 21.719 de protección de datos personales (Chile). | Datos personales mínimos, exportación y borrado por organización, registro de auditoría, documento de postura de seguridad. |
| El asistente IA da una orientación errónea que causa un daño o accidente. | Citas obligatorias, "no lo sé" sin fuente, reglas de seguridad (bloqueo y etiquetado), aviso de que decide el técnico, evaluación con casos reales antes de liberar (ADR-018). |
| Los técnicos no registran síntoma y acción al cerrar, y el asistente no tiene de dónde aprender. | Campos cortos y dictables en el cierre de correctivas desde el MVP (REQ-03 CA3); medir su llenado en el piloto. |
| El contenido normativo es incorrecto o queda desactualizado. | Revisar las plantillas normativas con un prevencionista o experto por rubro; cada plantilla indica la norma y la fecha de revisión. |

## 7. Métricas de éxito del producto

- Tiempo desde registro hasta primera OT preventiva generada: **< 1 hora**.
- % de OT preventivas cerradas a tiempo en organizaciones activas: **> 70 %** a los 3 meses.
- Organizaciones que siguen activas a los 90 días del piloto.
- Fallas registradas por QR / total de fallas (adopción por operadores).
- % de OT correctivas cerradas con síntoma y acción registrados: **> 80 %**.
- Conversaciones del asistente marcadas como útiles: **> 60 %** (desde F2).
