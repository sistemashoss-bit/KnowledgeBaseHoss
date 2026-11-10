// Editor de documentos (GrapesJS), en Herramientas. Lo monta tools/editor.html.
// No guarda nada en el servidor: el borrador vive en localStorage de este
// navegador (uno por usuario) hasta que se sube a Documentos o se descarta.
// El mismo HTML (buildDocument) sirve para las dos salidas.
//   - Vista previa / PDF en el navegador: se imprime en un iframe oculto
//     (el usuario elige "Guardar como PDF"); no toca el servidor.
//   - Subir a Documentos: se manda a /documents/upload, que lo convierte con
//     WeasyPrint (app/pdf_render.py) y lo guarda e indexa como un PDF más.
import grapesjs from 'grapesjs';
import es from 'grapesjs/locale/es.mjs';

// Hoja A4 con márgenes de 15 mm: el lienzo mide lo mismo que el área
// imprimible (180 mm ≈ 680 px a 96 ppp), así lo que se ve es lo que sale.
const PAGE_CSS = '@page { size: A4; margin: 15mm; }';
const BASE_CSS = `
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #fff; }
body { font-family: "DejaVu Sans", Arial, Helvetica, sans-serif; font-size: 11pt; line-height: 1.45; color: #111827; }
h1 { font-size: 22pt; margin: 0 0 8pt; }
h2 { font-size: 16pt; margin: 12pt 0 6pt; }
p { margin: 0 0 8pt; }
img { max-width: 100%; }
table { border-collapse: collapse; width: 100%; margin: 0 0 8pt; }
th, td { border: 1px solid #9ca3af; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #f3f4f6; }
hr { border: 0; border-top: 1px solid #d1d5db; margin: 10pt 0; }
.cols { display: flex; gap: 12pt; }
.cols > .col { flex: 1 1 0; min-height: 20pt; }
.page-break { break-after: page; page-break-after: always; }
`;
// Solo en el lienzo: marca dónde hay salto de página (no se exporta).
const CANVAS_ONLY_CSS = `
.page-break { height: 0; border-top: 2px dashed #93c5fd; margin: 12pt 0; }
body { min-height: 100vh; }
`;

const BLOCKS = [
  ['titulo', 'Título', '<h1>Título del documento</h1>'],
  ['subtitulo', 'Subtítulo', '<h2>Subtítulo</h2>'],
  ['texto', 'Texto', '<p>Escribe aquí el texto del documento.</p>'],
  ['imagen', 'Imagen', { type: 'image', activate: true }],
  ['columnas', '2 columnas', '<div class="cols"><div class="col"><p>Columna 1</p></div><div class="col"><p>Columna 2</p></div></div>'],
  ['tabla', 'Tabla', '<table><thead><tr><th>Columna</th><th>Columna</th><th>Columna</th></tr></thead>'
    + '<tbody><tr><td>Dato</td><td>Dato</td><td>Dato</td></tr><tr><td>Dato</td><td>Dato</td><td>Dato</td></tr></tbody></table>'],
  ['lista', 'Lista', '<ul><li>Elemento</li><li>Elemento</li></ul>'],
  ['separador', 'Separador', '<hr/>'],
  ['salto', 'Salto de página', '<div class="page-break"></div>'],
];

// Trazos de 24×24 para el panel de bloques.
const ICONS = {
  titulo: 'M5 5v14M15 5v14M5 12h10M19 9v10',
  subtitulo: 'M6 6v12M14 6v12M6 12h8M17 13h3v5',
  texto: 'M4 6h16M4 10h16M4 14h16M4 18h10',
  imagen: 'M4 5h16v14H4zM8 10a1.5 1.5 0 1 0 0-.01M20 16l-5-5-8 8',
  columnas: 'M4 5h7v14H4zM13 5h7v14h-7z',
  tabla: 'M4 5h16v14H4zM4 10h16M4 15h16M10 5v14M15 5v14',
  lista: 'M9 6h11M9 12h11M9 18h11M5 6h.01M5 12h.01M5 18h.01',
  separador: 'M4 12h16M8 7h8M8 17h8',
  salto: 'M6 3v6h12V3M6 21v-6h12v6M3 12h3M9 12h2M13 12h2M18 12h3',
};

function icon(d) {
  return `<svg viewBox="0 0 24 24" width="32" height="32" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path fill="none" d="${d}"/></svg>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function init(el) {
  const editor = grapesjs.init({
    container: el,
    height: '100%',
    width: 'auto',
    fromElement: false,
    storageManager: false,
    i18n: { locale: 'es', detectLocale: false, messages: { es } },
    protectedCss: BASE_CSS + CANVAS_ONLY_CSS,
    deviceManager: { devices: [{ id: 'a4', name: 'A4', width: '680px' }] },
    // Imágenes embebidas (data:): el PDF del servidor no descarga nada de
    // fuera, así que una imagen por URL no saldría.
    assetManager: {
      embedAsBase64: true,
      upload: false,
      handleAdd: (url) => {
        if (/^data:image\//i.test(url)) editor.AssetManager.add(url);
        else alert('Sube la imagen desde tu equipo; las imágenes por URL no se incluyen en el PDF.');
      },
    },
  });

  for (const [id, label, content] of BLOCKS) {
    editor.Blocks.add(id, { label, content, category: 'Documento', media: icon(ICONS[id]) });
  }

  // Borrador en localStorage: se escribe poco después de cada cambio y al
  // salir. `dirty` = hay cambios que no quedaron en el borrador (pendientes o
  // porque no cupieron, p. ej. imágenes pesadas): solo entonces se avisa al salir.
  const draftKey = el.dataset.draftKey;
  let dirty = false;
  let timer = null;
  const emitDraft = (state) => window.dispatchEvent(new CustomEvent('doc-draft', { detail: { state } }));

  function readDraft() {
    try {
      const raw = draftKey && localStorage.getItem(draftKey);
      return raw ? JSON.parse(raw).project : null;
    } catch {
      return null;
    }
  }

  function saveDraft() {
    clearTimeout(timer);
    timer = null;
    if (!dirty || !draftKey) return;
    try {
      localStorage.setItem(draftKey, JSON.stringify({ v: 1, savedAt: Date.now(), project: editor.getProjectData() }));
      dirty = false;
      emitDraft('saved');
    } catch {
      emitDraft('error');
    }
  }

  function clearDraft() {
    clearTimeout(timer);
    timer = null;
    dirty = false;
    try { if (draftKey) localStorage.removeItem(draftKey); } catch { /* sin almacenamiento */ }
  }

  editor.on('load', () => {
    const draft = readDraft();
    let restored = false;
    if (draft) {
      try { editor.loadProjectData(draft); restored = true; } catch { clearDraft(); }
    }
    if (!restored) editor.setComponents('<h1>Título del documento</h1><p>Escribe aquí el texto del documento.</p>');
    if (restored) emitDraft('restored');
    editor.Blocks.render();
    editor.Panels.getButton('views', 'open-blocks')?.set('active', true);
    editor.on('update', () => {
      dirty = true;
      clearTimeout(timer);
      timer = setTimeout(saveDraft, 800);
    });
  });
  window.addEventListener('beforeunload', (e) => {
    saveDraft();
    if (dirty) e.preventDefault();
  });

  // Documento HTML completo: lo que se imprime y lo que convierte WeasyPrint.
  function buildDocument(title) {
    const html = editor.getHtml();
    const css = editor.getCss({ avoidProtected: true }) || '';
    const body = /^\s*<body[\s>]/i.test(html) ? html : `<body>${html}</body>`;
    return '<!doctype html><html lang="es"><head><meta charset="utf-8">'
      + `<title>${escapeHtml(title || 'Documento')}</title>`
      + `<style>${PAGE_CSS}${BASE_CSS}</style><style>${css}</style></head>${body}</html>`;
  }

  function printDocument(title) {
    const frame = document.createElement('iframe');
    frame.setAttribute('aria-hidden', 'true');
    frame.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;';
    document.body.appendChild(frame);
    const doc = frame.contentDocument;
    doc.open();
    doc.write(buildDocument(title));
    doc.close();
    // Chrome nombra el PDF con el título de la página principal.
    const prevTitle = document.title;
    const imgs = Array.from(doc.images).filter((i) => !i.complete);
    Promise.all(imgs.map((i) => new Promise((ok) => { i.onload = ok; i.onerror = ok; }))).then(() => {
      document.title = title || prevTitle;
      frame.contentWindow.focus();
      frame.contentWindow.print();
      document.title = prevTitle;
      setTimeout(() => frame.remove(), 1000);
    });
  }

  const html = (title) => new Blob([buildDocument(title)], { type: 'text/html' });
  window.docEditor = { editor, buildDocument, printDocument, html, clearDraft };
  window.dispatchEvent(new CustomEvent('doc-editor-ready'));
}

const el = document.getElementById('doceditor');
if (el) init(el);
