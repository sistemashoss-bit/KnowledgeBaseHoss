// Editor de pizarras: Excalidraw sincronizado con Yjs por el WebSocket de
// /whiteboards/<id>/ws (servidor en app/whiteboards/sync.py).
import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Excalidraw, useHandleLibrary } from '@excalidraw/excalidraw';
import * as Y from 'yjs';
import { WebsocketProvider } from 'y-websocket';
import { ExcalidrawBinding, yjsToExcalidraw } from 'y-excalidraw';

const COLORS = ['#2563eb', '#16a34a', '#ea580c', '#9333ea', '#db2777', '#0891b2', '#ca8a04', '#dc2626'];

function colorFor(id) {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return COLORS[h % COLORS.length];
}

function emitStatus(status, peers) {
  window.dispatchEvent(new CustomEvent('wb-status', { detail: { status, peers } }));
}

// Deshacer/rehacer sobre Yjs: solo los cambios propios, sin pisar los de los
// demás (el historial interno de Excalidraw no sabe de colaboración).
// y-excalidraw trae su versión, pero busca los botones por aria-label en
// inglés ("Undo") y con la interfaz en español no los encuentra.
function setupUndo(dom, yElements, binding) {
  const undo = new Y.UndoManager(yElements, { trackedOrigins: new Set([binding]) });
  const onKey = (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    const key = e.key?.toLowerCase();
    if (key === 'z' || key === 'y') {
      e.preventDefault();
      e.stopPropagation();
      if (key === 'y' || e.shiftKey) undo.redo(); else undo.undo();
    }
  };
  const onClick = (e) => {
    const btn = e.target.closest('.undo-button-container button, .redo-button-container button');
    if (!btn) return;
    e.stopImmediatePropagation();
    if (btn.closest('.undo-button-container')) undo.undo(); else undo.redo();
  };
  dom.addEventListener('keydown', onKey, { capture: true });
  dom.addEventListener('click', onClick, { capture: true });
  return () => {
    dom.removeEventListener('keydown', onKey, { capture: true });
    dom.removeEventListener('click', onClick, { capture: true });
    undo.destroy();
  };
}

// Imágenes: se suben a /whiteboards/<id>/files (bucket de pizarras, ya en
// WebP) y en el documento Yjs queda solo {id, mimeType, created}. Cada
// navegador las baja por HTTP (el navegador las guarda en caché) y se las
// pasa a Excalidraw como dataURL. Las pizarras viejas traen la imagen
// completa en el documento: se muestran igual y, si quien abre puede
// editar, se suben al bucket y se reemplazan por la referencia.
function dataURLToBlob(dataURL) {
  const [head, data] = dataURL.split(',');
  const mime = head.match(/^data:([^;,]+)/)?.[1] || 'application/octet-stream';
  const bin = head.includes(';base64') ? atob(data) : decodeURIComponent(data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type: mime });
}

function blobToDataURL(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

function emitError(message) {
  window.dispatchEvent(new CustomEvent('wb-error', { detail: { message } }));
}

function setupFiles(api, yAssets, cfg) {
  const base = `/whiteboards/${cfg.boardId}/files`;
  const shown = new Set();      // ya entregadas a Excalidraw
  const uploading = new Set();
  let alive = true;

  async function upload(file) {
    if (uploading.has(file.id)) return;
    uploading.add(file.id);
    try {
      const body = new FormData();
      body.append('file_id', file.id);
      body.append('file', dataURLToBlob(file.dataURL), file.id);
      const r = await fetch(base, { method: 'POST', body });
      if (!r.ok) {
        let msg = 'No se pudo guardar la imagen';
        try { const j = await r.json(); if (typeof j.detail === 'string') msg = j.detail; } catch (e) { /* sin cuerpo */ }
        throw new Error(msg);
      }
      const meta = await r.json();
      if (alive) {
        yAssets.doc.transact(() => {
          yAssets.set(file.id, { id: file.id, mimeType: meta.mimeType, created: file.created || Date.now() });
        }, 'files');
      }
    } catch (e) {
      emitError(e.message || 'No se pudo guardar la imagen');
    } finally {
      uploading.delete(file.id);
    }
  }

  async function show(id, entry) {
    if (!entry || shown.has(id)) return;
    shown.add(id);
    if (entry.dataURL) {             // pizarra de antes: la imagen viene adentro
      api.addFiles([entry]);
      if (cfg.canEdit) upload(entry);
      return;
    }
    try {
      const r = await fetch(`${base}/${encodeURIComponent(id)}`);
      if (!r.ok) throw new Error(String(r.status));
      const blob = await r.blob();
      const dataURL = await blobToDataURL(blob);
      if (alive) api.addFiles([{ id, mimeType: blob.type || entry.mimeType, dataURL, created: entry.created || Date.now() }]);
    } catch (e) {
      shown.delete(id);              // se reintenta en el siguiente cambio
      console.warn('Imagen de la pizarra no disponible', id, e);
    }
  }

  for (const [id, entry] of yAssets.entries()) show(id, entry);
  const onRemote = (event) => {
    for (const id of event.keysChanged) show(id, yAssets.get(id));
  };
  yAssets.observe(onRemote);

  // Imágenes nuevas pegadas o soltadas aquí.
  const stopChange = cfg.canEdit ? api.onChange((_els, _state, files) => {
    for (const id in files) {
      const f = files[id];
      if (!shown.has(id) && !yAssets.has(id) && f.dataURL) {
        shown.add(id);
        upload(f);
      }
    }
  }) : () => {};

  return () => { alive = false; yAssets.unobserve(onRemote); stopChange(); };
}

// Biblioteca de Excalidraw: por usuario y por navegador, en localStorage. No
// pasa por Yjs ni por el servidor; es la misma en todas las pizarras.
function libraryKey(cfg) {
  return `wb-library-${cfg.userId}`;
}

function loadLibrary(cfg) {
  try {
    const items = JSON.parse(localStorage.getItem(libraryKey(cfg)) || '[]');
    return Array.isArray(items) ? items : [];
  } catch (e) {
    return [];
  }
}

function saveLibrary(cfg, items) {
  try {
    localStorage.setItem(libraryKey(cfg), JSON.stringify(items));
  } catch (e) {
    emitError('No se pudo guardar la biblioteca en este navegador');
  }
}

function Board({ cfg, doc, provider }) {
  const yElements = doc.getArray('elements');
  const yAssets = doc.getMap('assets');
  const [api, setApi] = useState(null);
  const [binding, setBinding] = useState(null);
  const ref = useRef(null);

  // Carga la biblioteca guardada e importa las que llegan de
  // libraries.excalidraw.com (#addLibrary=... en la URL de regreso).
  useHandleLibrary({ excalidrawAPI: api, getInitialLibraryItems: () => loadLibrary(cfg) });

  useEffect(() => {
    if (!api) return undefined;
    // Las imágenes no pasan por y-excalidraw (las metería enteras en el
    // documento): se le da un mapa desechable y setupFiles las maneja.
    const b = new ExcalidrawBinding(yElements, new Y.Doc().getMap('assets'), api, provider.awareness);
    setBinding(b);
    const stopUndo = cfg.canEdit ? setupUndo(ref.current, yElements, b) : () => {};
    const stopFiles = setupFiles(api, yAssets, cfg);
    return () => { setBinding(null); stopUndo(); stopFiles(); b.destroy(); };
  }, [api]);

  return (
    <div ref={ref} style={{ width: '100%', height: '100%' }}>
      <Excalidraw
        initialData={{
          elements: yjsToExcalidraw(yElements),
          appState: { viewBackgroundColor: '#ffffff' },
        }}
        excalidrawAPI={setApi}
        onPointerUpdate={binding ? binding.onPointerUpdate : undefined}
        onLibraryChange={(items) => saveLibrary(cfg, items)}
        viewModeEnabled={!cfg.canEdit}
        langCode="es-ES"
        name={cfg.title}
        UIOptions={{ canvasActions: { loadScene: false, saveToActiveFile: false } }}
      />
    </div>
  );
}

function start(root) {
  const cfg = {
    boardId: root.dataset.boardId,
    canEdit: root.dataset.canEdit === 'true',
    userId: root.dataset.userId,
    userName: root.dataset.userName,
    title: root.dataset.title,
  };
  const doc = new Y.Doc();
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const provider = new WebsocketProvider(`${proto}//${location.host}/whiteboards/${cfg.boardId}`, 'ws', doc);
  const color = colorFor(cfg.userId);
  provider.awareness.setLocalStateField('user', { name: cfg.userName, color, colorLight: `${color}33` });

  const peers = () => provider.awareness.getStates().size;
  provider.on('status', (e) => emitStatus(e.status, peers()));
  provider.awareness.on('change', () => emitStatus(provider.wsconnected ? 'connected' : 'disconnected', peers()));
  // El servidor corta con 4403 al quitar el acceso o cambiar el permiso: se
  // recarga para que la página refleje el permiso nuevo (o el 403).
  provider.on('connection-close', (e) => { if (e && (e.code === 4403 || e.code === 4401)) location.reload(); });

  const react = createRoot(root);
  // Se monta tras la primera sincronización: initialData debe traer lo que ya hay.
  let mounted = false;
  const mount = () => {
    if (mounted) return;
    mounted = true;
    react.render(<Board cfg={cfg} doc={doc} provider={provider} />);
  };
  provider.on('sync', (synced) => { if (synced) mount(); });
  window.addEventListener('beforeunload', () => provider.destroy());
}

const el = document.getElementById('wb-root');
if (el) start(el);
