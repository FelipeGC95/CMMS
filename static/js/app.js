// Comportamientos mínimos sin JS en línea (CSP estricta).
document.addEventListener("change", (event) => {
  // Cambiar de organización al elegirla (en móvil no se muestra el botón "Cambiar").
  if (event.target.matches(".org-switch select")) {
    event.target.form.requestSubmit();
  }
});
