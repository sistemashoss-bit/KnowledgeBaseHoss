// Compila el editor de pizarras a app/assets/whiteboard/ (lo sirve /assets).
// Uso: npm ci && npm run build  (el Dockerfile lo hace en su etapa "whiteboard").
import { build } from 'esbuild';
import { cpSync, mkdirSync, rmSync } from 'node:fs';

const out = '../app/assets/whiteboard';
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });

await build({
  entryPoints: ['src/main.jsx'],
  outfile: `${out}/editor.js`,
  bundle: true,
  minify: true,
  format: 'iife',
  target: ['es2020'],
  jsx: 'automatic',
  define: { 'process.env.NODE_ENV': '"production"' },
  // Excalidraw lee otras variables de process.env (p. ej. IS_PREACT): en el
  // navegador no existe `process`.
  banner: { js: 'var process = { env: { NODE_ENV: "production" } };' },
  legalComments: 'linked',
  logLevel: 'info',
});

// Fuentes y traducciones de Excalidraw: se sirven desde aquí en vez de su CDN
// (window.EXCALIDRAW_ASSET_PATH en whiteboards/editor.html).
cpSync('node_modules/@excalidraw/excalidraw/dist/excalidraw-assets', `${out}/excalidraw-assets`, { recursive: true });
