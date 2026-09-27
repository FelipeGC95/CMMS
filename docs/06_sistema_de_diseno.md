# 06 — Sistema de diseño

**Estado:** borrador v0.1 (2026-09-24)
**Fuente de verdad de la marca:** [Marca/GALBUST-manual-de-marca.pdf](../Marca/GALBUST-manual-de-marca.pdf)
y los archivos de [Marca/](../Marca/). Esa carpeta **no se modifica**; los
archivos que usa la aplicación se copian a `static/brand/` con un comando
(`manage.py sync_brand`) para mantener un solo origen.

## 1. Principios

1. **Legible en terreno primero:** alto contraste, texto ≥ 16 px en móvil,
   objetivos táctiles ≥ 48 × 48 px en vistas de técnico (usables con guantes).
   **Toda** la aplicación se completa desde un teléfono de 360 px, sin scroll
   horizontal (ADR-017); se diseña primero a 360 px y se amplía a escritorio.
2. **Sobrio y consistente:** pocas variantes por componente; el color comunica
   estado, no decora.
3. **Nunca solo color:** todo estado lleva ícono + texto.
4. **Tokens o nada:** ningún color, fuente, radio, sombra ni espaciado escrito
   directo en un componente o plantilla.
5. **AA siempre:** toda combinación de texto/fondo ≥ 4,5:1 (texto grande
   ≥ 3:1); bordes de controles y foco ≥ 3:1.

## 2. Marca y logo

### Qué archivo usar

| Uso | Archivo |
|---|---|
| Navbar escritorio, fondo claro | `galbust-horizontal-color.svg` |
| Navbar escritorio, fondo oscuro o modo oscuro | `galbust-horizontal-negativo.svg` |
| Navbar compacta (< 1100 px) | `galbust-isotipo-color.svg` / `galbust-isotipo-blanco.svg` en oscuro |
| Favicon | `galbust-favicon.ico` + `png/favicon-*.png` |
| Ícono PWA / apple-touch | `png/galbust-app-icon-180.png`, `-512.png` |
| Login y pantallas de bienvenida | `galbust-vertical-color.svg` / `-negativo.svg` |
| Reportes impresos a una tinta | `galbust-horizontal-monocromo.svg` |

### Reglas obligatorias (del manual)

- **Área de resguardo:** la mitad del alto del isotipo por cada lado. En CSS,
  el contenedor del logo tiene `padding: calc(var(--logo-height) * 0.5)`
  (a escala: el isotipo mide lo mismo que el alto del logo horizontal).
- **Tamaño mínimo:** logo horizontal 90 px de ancho; isotipo 24 px.
- **Fondo oscuro → siempre versión negativa (blanca).** Nunca el logo a color
  sobre un fondo oscuro. El componente `logo` elige la versión según el tema,
  no la plantilla.
- **No** alterar colores, deformar, agregar sombras o contornos, rotar ni
  cambiar la tipografía. `object-fit: contain` y proporciones fijas.
- El **cian nunca se aplica al texto "GALBUST"**.

### Nombre del producto

"**GALBUST CMMS**": el logotipo GALBUST (SVG) seguido de "CMMS" en Poppins
Medium, color `--color-text-brand`, separado por el área de resguardo. "CMMS"
nunca va dentro del SVG del logo.

### Geometría derivada del isotipo

El isotipo es un marco de esquinas redondeadas (radio ≈ 30 % del lado) con una
barra en forma de píldora. De ahí salen los radios:

| Token | Valor | Uso |
|---|---|---|
| `--radius-sm` | 6px | badges, chips pequeños |
| `--radius-md` | 10px | botones, inputs |
| `--radius-lg` | 16px | tarjetas, modales |
| `--radius-pill` | 999px | badges de estado, toggles, barras de progreso (eco de la barra cian) |

## 3. Tokens

Se definen en `static/css/tokens.css`. Los componentes consumen **tokens
semánticos** (`--color-bg`, `--color-action-bg`…), nunca los primitivos
(`--galbust-blue`).

### 3.1 Primitivos (paleta del manual)

```css
:root {
  --galbust-blue: #0B2E4F;   /* Azul profundo */
  --galbust-cyan: #12B3C8;   /* Cian señal */
  --galbust-ink:  #111318;   /* Tinta */
  --galbust-mist: #F4F7FA;   /* Niebla */
}
```

### 3.2 Semánticos — modo claro

| Token | Valor | Uso | Contraste |
|---|---|---|---|
| `--color-bg` | #F4F7FA | fondo de página | — |
| `--color-surface` | #FFFFFF | tarjetas, tablas, formularios | — |
| `--color-text` | #111318 | texto principal | ≈ 17:1 sobre surface |
| `--color-text-muted` | #475569 | texto secundario | ≈ 7,6:1 sobre surface* |
| `--color-text-brand` | #0B2E4F | encabezados, texto de marca | 13,8:1 |
| `--color-link` / `--color-accent-text` | #0E7C8C | enlaces, texto cian sobre claro | 4,9:1 |
| `--color-border` | #D9E1EA | separadores decorativos | (no aplica) |
| `--color-border-strong` | #64748B | borde de inputs y controles | ≈ 4,8:1* |
| `--color-focus` | #0E7C8C | anillo de foco, 3 px + 2 px de separación | 4,9:1 |
| `--color-nav-bg` | #0B2E4F | navbar | — |
| `--color-nav-text` | #FFFFFF | texto del navbar | 13,8:1 |
| `--color-nav-active` | #12B3C8 | indicador activo en navbar | 5,5:1 sobre azul |
| `--color-action-bg` | #12B3C8 | botón primario | — |
| `--color-action-text` | #0B2E4F | texto del botón primario (**nunca blanco**) | 5,5:1 |
| `--color-action-bg-hover` | #3FC6D8 | hover primario | 6,8:1 |
| `--color-secondary-bg` | #0B2E4F | botón secundario | — |
| `--color-secondary-text` | #FFFFFF | | 13,8:1 |
| `--color-danger-bg` | #B91C1C | botón destructivo | — |
| `--color-danger-text` | #FFFFFF | | ≈ 6,5:1* |

\* Verificado por `tests/ui/test_contrast.py` (2026-09-24). El fondo del badge de información se aclaró de #E0F5F8 a #F0FAFC porque daba 4,3:1.
**El cian #12B3C8 no se usa nunca como texto sobre fondo claro (2,5:1).**

### 3.3 Estados

Distintos del cian de marca; siempre con ícono + texto.

| Estado | Texto sobre claro | Fondo tenue (badge) | Ícono | Texto en oscuro* |
|---|---|---|---|---|
| Éxito | #15803D | #DCFCE7 | check | #4ADE80 |
| Advertencia | #B45309 | #FEF3C7 | triángulo | #FBBF24 |
| Error | #B91C1C | #FEE2E2 | círculo con x | #FCA5A5 |
| Información | #0E7C8C | #F0FAFC | "i" | #5FD4E3 |
| Neutro | #475569 | #EEF2F6 | punto | #A9BCCF |

Mapeo de dominio (ejemplos): activo *operativo* → éxito; *detenido* → error;
*en mantención* → advertencia; *de baja* → neutro. OT *vencida* → error;
*en ejecución* → información; *cerrada* → éxito.

Los tokens de estado **no son sobrescribibles** por la organización.

### 3.4 Semánticos — modo oscuro

Se aplican con `:root[data-theme="dark"]`. `static/js/theme.js` se carga
en `<head>` sin `defer` y fija `data-theme` antes de pintar según la
preferencia del dispositivo (Sistema / Claro / Oscuro, botón en la barra
superior); "Sistema" sigue a `prefers-color-scheme`. Sin JavaScript la
interfaz queda en modo claro.

| Token | Valor | Nota |
|---|---|---|
| `--color-bg` | #071E34 | |
| `--color-surface` | #0B2E4F | |
| `--color-text` | #F4F7FA | ≈ 12:1 sobre surface* |
| `--color-text-muted` | #A9BCCF | ≥ 4,5:1* |
| `--color-text-brand` | #F4F7FA | |
| `--color-link` / `--color-accent-text` | #12B3C8 | 5,5:1 sobre azul profundo |
| `--color-border` | #1E4468 | decorativo* |
| `--color-border-strong` | #5B7EA0 | ≥ 3:1* |
| `--color-focus` | #12B3C8 | |
| `--color-nav-bg` | #071E34 | |
| `--color-action-bg` / `-text` | #12B3C8 / #0B2E4F | igual que en claro |
| `--color-secondary-bg` | transparente, borde #F4F7FA | el azul se confundiría con la superficie |
| `--color-secondary-text` | #F4F7FA | |
| Logo | versión negativa | obligatorio |

\* Verificado por `tests/ui/test_contrast.py` (2026-09-24).

### 3.5 Tipografía

| Token | Valor | Uso |
|---|---|---|
| `--font-brand` | "Poppins", system-ui, sans-serif | titulares, navegación, botones (500) |
| `--font-body` | "Poppins", system-ui, sans-serif | texto corrido (400) |
| `--font-ui` | "Inter", system-ui, sans-serif | tablas densas, formularios, KPIs (400/500/600) con `font-variant-numeric: tabular-nums` |
| `--font-mono` | "JetBrains Mono", ui-monospace, monospace | códigos de OT, activos, valores técnicos (400/500) |

Escala (base 16 px; relación 1,2):

| Token | px | Uso |
|---|---|---|
| `--text-xs` | 12 | metadatos (nunca en vistas de técnico) |
| `--text-sm` | 14 | tablas densas en escritorio |
| `--text-md` | 16 | cuerpo, inputs (evita el zoom en iOS) |
| `--text-lg` | 20 | títulos de tarjeta |
| `--text-xl` | 24 | títulos de página |
| `--text-2xl` | 32 | cifras de KPI |

Carga: **fuentes alojadas en `static/fonts/`** (woff2, subconjunto latino),
no desde Google Fonts. Así funcionan offline (PWA), evitan una solicitud a
terceros y simplifican la CSP. Solo los pesos 400/500/600 y
`font-display: swap`.

### 3.6 Espaciado, sombras y movimiento

- Espaciado (múltiplos de 4): `--space-1` 4px · `--space-2` 8px · `--space-3`
  12px · `--space-4` 16px · `--space-5` 24px · `--space-6` 32px · `--space-7` 48px.
- `--touch-target`: 48px (vistas de técnico), 40px mínimo en escritorio.
- Sombras: `--shadow-sm` (tarjetas) y `--shadow-md` (menús, modales), suaves y
  teñidas de azul; en modo oscuro se reemplazan por bordes.
- Movimiento: `--duration-fast` 120ms, `--duration-base` 200ms; se desactiva con
  `prefers-reduced-motion`.

## 4. Marca por organización

Una organización puede sobrescribir **solo**:

- **Logo** (SVG o PNG, se muestra en el navbar, en los reportes y en las
  etiquetas QR). Si no sube una versión para fondo oscuro, en modo oscuro su
  logo se muestra sobre una placa clara (`--color-surface` claro con radio
  `--radius-md`).
- **Color primario** (Configuración → Color de la marca) → reemplaza
  `--color-action-bg`, `--color-action-text`, `--color-action-bg-hover`,
  `--color-link`, `--color-accent-text` y `--color-nav-active`
  (`BRANDABLE_TOKENS` en `apps/organizations/branding.py`).

Se guarda solo el color elegido (`organization.primary_color`); los tonos por
uso se calculan con los fondos reales de `tokens.css`:

1. El color de texto del botón (el azul de marca `#0B2E4F` o blanco, el que
   tenga mayor contraste; si ninguno alcanza 4,5:1, aclara u oscurece el
   fondo hasta alcanzarlo). El cian nunca queda con texto blanco.
2. El hover (±8 % de luminosidad, manteniendo el contraste).
3. La variante para enlaces sobre claro y sobre oscuro (≥ 4,5:1 contra la
   superficie y el fondo de cada tema) y el indicador de navegación activa
   (≥ 3:1 sobre la barra).

Cada ajuste se informa al guardar y queda visible en la tarjeta (ORG-09 CA1).
Los tokens se sirven en `/org-theme.css?v=<color>` (sin estilos en línea, por
la CSP; caché privada de un día con ETag), enlazada en `base.html` después de
`components.css` y solo si la organización eligió un color. **No se
sobrescriben** los tokens de estado, texto, fondo, foco ni bordes (CA2).
`tests/ui/test_brand_contrast.py` repite todos los pares de `CONTRAST_PAIRS`
con colores difíciles en ambos temas. La marca GALBUST sigue visible como
"con tecnología de GALBUST" en el pie de página y en los reportes.

## 5. Componentes base

**Cortes de pantalla** (únicos; ningún componente define otros):

| Corte | Qué cambia |
|---|---|
| < 720 px | Una columna; las tablas `.table` se apilan como tarjetas con la etiqueta de `data-label`. |
| < 1100 px | Navegación compacta: isotipo, menú de escritorio oculto y navegación inferior con todos los destinos (los que no caben van en "Más"). |


Todos se muestran en `/design-system/` en ambos modos, con sus estados
(normal, hover, foco, deshabilitado, cargando, error).

| Componente | Variantes | Notas |
|---|---|---|
| **Botón** | primario, secundario, fantasma, destructivo; tamaños md (40px) y lg (48px, técnico); solo ícono con `aria-label` | Estado cargando con indicador y `aria-busy`. |
| **Input / select / textarea** | con etiqueta, ayuda y error | Etiqueta siempre visible (nunca solo placeholder); error con ícono + texto + `aria-describedby`. |
| **Checkbox / radio / switch** | | Área táctil ≥ 48px. |
| **Resultado de checklist** | OK / No OK / N.A. | Tres botones grandes con ícono + texto; el seleccionado usa el color de estado. |
| **Badge de estado** | éxito, advertencia, error, info, neutro | Píldora con fondo tenue + ícono + texto. |
| **Tabla** | densa (escritorio), tarjetas apiladas (< 720px) | Inter con cifras tabulares; encabezado fijo; ordenar y paginar con htmx. Toda `<td>` lleva `data-label`. |
| **Tarjeta** | simple, con encabezado y acciones | |
| **Tarjeta KPI** | valor, unidad, variación, meta | Cifra en Inter 600 `--text-2xl`; la variación con ícono de flecha + texto (no solo verde/rojo). |
| **Navbar** | escritorio (logo horizontal), móvil (isotipo + menú) | Fondo `--color-nav-bg`, logo negativo; selector de organización (nombre corto, sin sufijo societario), nombre de pila, tema y "Salir" con ícono. Rol, correo y "Cambiar contraseña" en "Mi cuenta" de /menu/. |
| **Navegación inferior móvil** | 4–5 destinos | Bajo 1100 px, para todos los roles. Técnico: Mis OT, Escanear QR, Avisar falla, Perfil. Otros roles: sus destinos principales + "Más" con el resto del menú de escritorio. |
| **Alerta / toast** | 4 estados | `role="status"` o `role="alert"` según urgencia. |
| **Modal / hoja inferior** | | Hoja inferior en móvil; foco atrapado; se cierra con Esc. |
| **Estado vacío** | | Ilustración mínima con el isotipo en monocromo + acción sugerida. |
| **Código** | OT, activo | JetBrains Mono, botón de copiar. |

## 6. Verificación

- `tests/ui/test_contrast.py` lee `tokens.css`, resuelve los pares declarados
  en una tabla `CONTRAST_PAIRS` (texto/fondo y control/fondo en ambos modos) y
  falla si alguno queda bajo el mínimo WCAG AA. Toda combinación nueva se
  agrega a esa tabla.
- Test que busca colores en formato hex en `templates/` y
  `static/css/components.css`: fuera de `tokens.css` no se permiten.
- `tests/ui/test_mobile.py` falla si una `.table` de `templates/` tiene
  celdas `<td>` sin `data-label`.
- Cada vista nueva se prueba a 360 px (DevTools) antes de darla por
  terminada: sin scroll horizontal y con todas sus acciones alcanzables.
- Revisión manual de cada release en un teléfono real a pleno sol y con
  guantes en las vistas de técnico.
