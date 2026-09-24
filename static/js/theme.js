// Aplica el tema antes de pintar la página (se carga sin defer en <head>).
// Preferencia guardada por dispositivo: "system" | "light" | "dark".
(function () {
  var KEY = "cmms-theme";
  var media = window.matchMedia("(prefers-color-scheme: dark)");

  function preference() {
    try {
      return localStorage.getItem(KEY) || "system";
    } catch (e) {
      return "system";
    }
  }

  function apply() {
    var pref = preference();
    var dark = pref === "dark" || (pref === "system" && media.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.themePref = pref;
  }

  apply();
  media.addEventListener("change", apply);

  window.cmmsTheme = {
    get: preference,
    set: function (pref) {
      try {
        localStorage.setItem(KEY, pref);
      } catch (e) {
        /* sin almacenamiento: el cambio dura solo esta página */
      }
      apply();
    },
  };
})();
