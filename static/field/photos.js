// Fotos tomadas sin conexión (ADR-011 §1e): se reducen en el teléfono antes de guardarlas
// (JPEG 0,85, lado mayor ≤ 2000 px) para que la cola ocupe poco y el envío sea corto.
// Si el navegador no sabe decodificarla (p. ej. HEIC), se guarda el original: el servidor
// la convierte igual. Límite: 20 MB, como en el servidor.

export const MAX_BYTES = 20 * 1024 * 1024;
export const MAX_SIDE = 2000;
const QUALITY = 0.85;

export class PhotoTooLarge extends Error {}

async function toJpeg(file) {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  const scale = Math.min(1, MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  if (bitmap.close) bitmap.close();
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", QUALITY));
}

export async function prepare(file) {
  if (file.size > MAX_BYTES) throw new PhotoTooLarge();
  // Hora de la foto: la del archivo si es creíble (no futura); si no, ahora.
  const now = Date.now();
  const takenAt = new Date(file.lastModified && file.lastModified <= now ? file.lastModified : now).toISOString();
  try {
    const blob = await toJpeg(file);
    if (blob && blob.size) return { blob, name: "foto.jpg", type: "image/jpeg", takenAt };
  } catch (error) {
    // No se pudo decodificar: se guarda tal cual.
  }
  return { blob: file, name: file.name || "foto", type: file.type || "application/octet-stream", takenAt };
}
