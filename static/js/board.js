// Tablero semanal (REQ-05): arrastrar una OT a un día la programa; soltarla en
// «Sin programar» la devuelve al backlog. Sin JS (o en el teléfono) cada tarjeta
// tiene su formulario «Programar», que hace lo mismo. Sin JS en línea (CSP estricta).
(() => {
  const board = document.querySelector("[data-board]");
  if (!board) return;
  const status = document.querySelector("[data-board-status]");
  const statusText = document.querySelector("[data-board-status-text]");
  const ZONES = "[data-drop-date], [data-drop-unschedule]";
  let dragged = null;

  const clearTargets = () => {
    board.querySelectorAll(".is-drop-target").forEach((zone) => zone.classList.remove("is-drop-target"));
  };

  const showError = (message) => {
    statusText.textContent = message;
    status.hidden = false;
    status.scrollIntoView({ block: "nearest" });
  };

  // Destino válido: un día (desde hoy) o el backlog, si la OT ya está programada.
  const zoneFor = (target) => {
    const zone = target.closest(ZONES);
    if (!zone || !dragged) return null;
    if ("dropUnschedule" in zone.dataset && !dragged.dataset.unscheduleUrl) return null;
    return zone;
  };

  board.addEventListener("dragstart", (event) => {
    const card = event.target.closest("[data-schedule-url]");
    if (!card) return;
    dragged = card;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", card.querySelector(".mono")?.textContent || "");
    card.classList.add("is-dragging");
  });

  board.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("is-dragging");
    dragged = null;
    clearTargets();
  });

  board.addEventListener("dragover", (event) => {
    const zone = zoneFor(event.target);
    if (!zone) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (!zone.classList.contains("is-drop-target")) {
      clearTargets();
      zone.classList.add("is-drop-target");
    }
  });

  board.addEventListener("drop", async (event) => {
    const zone = zoneFor(event.target);
    if (!zone) return;
    event.preventDefault();
    const card = dragged;
    clearTargets();
    const body = new FormData();
    let url = card.dataset.unscheduleUrl;
    if (zone.dataset.dropDate) {
      url = card.dataset.scheduleUrl;
      body.append("scheduled_for", zone.dataset.dropDate);
    }
    card.classList.add("is-saving");
    try {
      const response = await fetch(url, {
        method: "POST",
        body,
        credentials: "same-origin",
        headers: { "X-CSRFToken": board.dataset.csrf, "X-Board-Drop": "1" },
      });
      if (response.ok) {
        window.location.reload();
        return;
      }
      showError(await response.text());
    } catch {
      showError(board.dataset.networkError || "");
    }
    card.classList.remove("is-saving");
  });
})();
