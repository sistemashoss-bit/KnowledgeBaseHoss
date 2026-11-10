// Compila el editor de documentos a app/assets/doceditor/ (lo sirve /assets).
// Uso: npm ci && npm run build  (el Dockerfile lo hace en su etapa "doceditor").
import { build } from 'esbuild';
import { copyFileSync, mkdirSync, rmSync } from 'node:fs';

const out = '../app/assets/doceditor';
rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });

await build({
  entryPoints: ['src/main.js'],
  outfile: `${out}/editor.js`,
  bundle: true,
  minify: true,
  format: 'iife',
  target: ['es2020'],
  legalComments: 'linked',
  logLevel: 'info',
});

copyFileSync('node_modules/grapesjs/dist/css/grapes.min.css', `${out}/grapes.min.css`);
