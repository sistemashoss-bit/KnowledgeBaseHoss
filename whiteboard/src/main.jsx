// Editor de pizarras: Excalidraw sincronizado con Yjs por el WebSocket de
// /whiteboards/<id>/ws (servidor en app/whiteboards/sync.py).
import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Excalidraw } from '@excalidraw/excalidraw';
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

function Board({ cfg, doc, provider }) {
  const yElements = doc.getArray('elements');
  const yAssets = doc.getMap('assets');
  const [api, setApi] = useState(null);
  const [binding, setBinding] = useState(null);
  const ref = useRef(null);

  useEffect(() => {
    if (!api) return undefined;
    const b = new ExcalidrawBinding(yElements, yAssets, api, provider.awareness);
    setBinding(b);
    const stopUndo = cfg.canEdit ? setupUndo(ref.current, yElements, b) : () => {};
    return () => { setBinding(null); stopUndo(); b.destroy(); };
  }, [api]);

  return (
    <div ref={ref} style={{ width: '100%', height: '100%' }}>
      <Excalidraw
        initialData={{
          elements: yjsToExcalidraw(yElements),
          files: Object.fromEntries(yAssets.entries()),
          appState: { viewBackgroundColor: '#ffffff' },
        }}
        excalidrawAPI={setApi}
        onPointerUpdate={binding ? binding.onPointerUpdate : undefined}
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
