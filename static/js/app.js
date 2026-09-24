// Comportamientos mínimos sin JS en línea (CSP estricta).
document.addEventListener("change", (event) => {
  // Cambiar de organización al elegirla (en móvil no se muestra el botón "Cambiar").
  if (event.target.matches(".org-switch select")) {
    event.target.form.requestSubmit();
  }
});

// Selector de tema: Sistema → Claro → Oscuro.
const THEME_ORDER = ["system", "light", "dark"];

function renderThemeButtons() {
  const pref = window.cmmsTheme.get();
  document.querySelectorAll("[data-theme-cycle]").forEach((button) => {
    const label = button.dataset[`label${pref.charAt(0).toUpperCase()}${pref.slice(1)}`];
    button.querySelector("[data-theme-label]").textContent = label;
    button.setAttribute("aria-label", `${button.dataset.labelPrefix}: ${label}`);
  });
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-theme-cycle]");
  if (!button) return;
  const next = THEME_ORDER[(THEME_ORDER.indexOf(window.cmmsTheme.get()) + 1) % THEME_ORDER.length];
  window.cmmsTheme.set(next);
  renderThemeButtons();
});

document.addEventListener("DOMContentLoaded", renderThemeButtons);
