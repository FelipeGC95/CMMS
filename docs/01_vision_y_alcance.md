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

| Diferenciador | Cómo se materializa |
|---|---|
| **Puesta en marcha guiada** | Asistente inicial: rubro → sitios → activos (Excel o alta rápida) → planes sugeridos → calendario generado. Meta: **primera OT preventiva generada en menos de 1 hora** desde el registro. |
| **Biblioteca de plantillas** | Clases de activo con atributos, planes preventivos típicos (compresor, grúa horquilla, generador, camioneta, bomba, correa transportadora, caldera, etc.) y catálogo de fallas basado en la estructura de ISO 14224. El cliente adopta y ajusta, no parte en blanco. |
| **Asignación automática de planes** | Al crear un activo de una clase, se le proponen (o asignan) los planes de esa clase. Al registrar un horómetro, se recalculan vencimientos. |
| **Aviso de falla por QR** | Etiqueta QR en cada equipo: escanear → describir → foto → enviar. Sin navegar menús. |
| **Móvil primero, pensado para terreno** | Botones grandes, alto contraste, flujos de 3 toques. Modo offline en fase 2. |
| **Indicadores estándar sin configurar** | Disponibilidad, MTBF, MTTR y cumplimiento del preventivo calculados desde el día 1 con definiciones de EN 15341 / SMRP explicadas en pantalla. |
| **Precio pyme y en español de Chile** | Precio por cantidad de activos, usuarios ilimitados en planes medios (los técnicos no deben ser un costo marginal). Soporte en español. |

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

## 7. Métricas de éxito del producto

- Tiempo desde registro hasta primera OT preventiva generada: **< 1 hora**.
- % de OT preventivas cerradas a tiempo en organizaciones activas: **> 70 %** a los 3 meses.
- Organizaciones que siguen activas a los 90 días del piloto.
- Fallas registradas por QR / total de fallas (adopción por operadores).
