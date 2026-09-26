// Firma en pantalla al completar una OT (WO-03 CA3). Sin JS en línea (CSP estricta).
// El trazo se dibuja en un <canvas> con Pointer Events (dedo, lápiz o mouse) y al
// levantar el dedo se guarda como PNG (data URL) en el campo oculto del formulario.
// Colores: los del CSS del lienzo (tokens --color-signature-*), nunca valores fijos aquí.
(function () {
  "use strict";

  function setup(root) {
    if (root.dataset.signatureReady) return;
    root.dataset.signatureReady = "1";

    const canvas = root.querySelector("[data-signature-canvas]");
    const input = root.querySelector("input[type=hidden]");
    const clear = root.querySelector("[data-signature-clear]");
    const status = root.querySelector("[data-signature-status]");
    const ctx = canvas.getContext("2d");
    let drawing = false;
    let last = null;

    function paintBackground() {
      const style = getComputedStyle(canvas);
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = style.backgroundColor;
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.restore();
      ctx.strokeStyle = style.color;
    }

    function setStatus() {
      if (!status) return;
      status.textContent = input.value ? status.dataset.signed : status.dataset.empty;
    }

    function restore() {
      if (!input.value) return;
      const image = new Image();
      image.onload = () => {
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
        ctx.restore();
      };
      image.src = input.value;
    }

    function resize() {
      const ratio = Math.max(window.devicePixelRatio || 1, 1);
      const width = Math.round(canvas.clientWidth * ratio);
      const height = Math.round(canvas.clientHeight * ratio);
      if (!width || (canvas.width === width && canvas.height === height)) return;
      canvas.width = width;
      canvas.height = height;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.lineWidth = 2.5;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      paintBackground();
      restore();
    }

    function point(event) {
      const rect = canvas.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    canvas.addEventListener("pointerdown", (event) => {
      event.preventDefault();
      canvas.setPointerCapture(event.pointerId);
      drawing = true;
      last = point(event);
      // Un toque sin arrastre deja un punto visible.
      ctx.beginPath();
      ctx.moveTo(last.x, last.y);
      ctx.lineTo(last.x + 0.1, last.y + 0.1);
      ctx.stroke();
    });

    canvas.addEventListener("pointermove", (event) => {
      if (!drawing) return;
      event.preventDefault();
      const next = point(event);
      ctx.beginPath();
      ctx.moveTo(last.x, last.y);
      ctx.lineTo(next.x, next.y);
      ctx.stroke();
      last = next;
    });

    function finish() {
      if (!drawing) return;
      drawing = false;
      input.value = canvas.toDataURL("image/png");
      setStatus();
    }
    canvas.addEventListener("pointerup", finish);
    canvas.addEventListener("pointercancel", finish);
    canvas.addEventListener("pointerleave", finish);

    clear.addEventListener("click", () => {
      input.value = "";
      paintBackground();
      setStatus();
    });

    window.addEventListener("resize", resize);
    resize();
    setStatus();
  }

  function init(scope) {
    scope.querySelectorAll("[data-signature-pad]").forEach(setup);
  }

  document.addEventListener("DOMContentLoaded", () => init(document));
  // El formulario de cierre se recarga con htmx al responder el checklist.
  document.addEventListener("htmx:load", (event) => init(event.detail.elt));
})();
