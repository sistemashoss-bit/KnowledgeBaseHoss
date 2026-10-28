// Tailwind CLI (antes Play CDN, que compilaba el CSS en el navegador en cada
// carga). Genera app/assets/tailwind.css; ver README → "Estilos (Tailwind)".
module.exports = {
  // Rutas relativas a la raíz del repo (desde donde corre el CLI).
  content: [
    "./app/templates/**/*.html",
    "./app/assets/**/*.js",
    // Clases que pone Python (p. ej. linkify en app/templating.py).
    "./app/**/*.py",
  ],
  safelist: [
    // Colores que las plantillas arman con variables (`bg-{{ tag.color }}-100`):
    // etiquetas de tareas (TASK_TAG_COLORS) y estados del dashboard de reportes.
    { pattern: /^(bg|text|border)-(gray|red|orange|yellow|green|blue|indigo|purple|pink)-(50|100|200|600|700)$/ },
  ],
};
