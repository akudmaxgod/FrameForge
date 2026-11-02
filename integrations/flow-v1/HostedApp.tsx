import React, { useState, useEffect, useCallback, useRef } from 'react';
import { Flow } from 'flow-sdk';
import { BridgeLog, ImportedMedia, GeneratedItem } from './types';
import { Header } from './components/Header';
import { ControlPanel } from './components/ControlPanel';
import { PreviewGrid } from './components/PreviewGrid';
import { LogTerminal } from './components/LogTerminal';
const CHANNEL = 'frameforge.bridge.v1';
const HOST = 'https://flow.google.com';
/** 
 * Лимиты референсов для профилей моделей.
 * Мост использует эти значения для preflight-проверки входящих команд.
 * Обновлено: Nano Banana 2 и Lite теперь поддерживают до 5 референсов.
 */
const MODEL_LIMITS: Record<string, number> = {
  '🍌 Nano Banana Pro': 5,
  '🍌 Nano Banana 2': 5,
  '🍌 Nano Banana 2 Lite': 5
};
/**
 * Глубокий поиск медиа-данных в ответе генерации.
 */
const readMedia = (raw: any) => {
  let mediaId: string | null = null;
  let base64: string | null = null;
  let mimeType: string | null = null;
  const idKeys = ['mediaId', 'media_id', 'id', 'assetId', 'asset_id'];
  const dataKeys = ['base64', 'imageBase64', 'dataUrl', 'bytes', 'content', 'encodedData', 'data', 'videoBase64'];
  const mimeKeys = ['mimeType', 'mime_type', 'contentType', 'content_type'];
  const crawl = (obj: any, depth: number) => {
    if (depth > 4 || !obj || typeof obj !== 'object') return;
    if (Array.isArray(obj)) {
      const limit = Math.min(obj.length, 8);
      for (let i = 0; i < limit; i++) {
        crawl(obj[i], depth + 1);
        if (mediaId && base64) return;
      }
      return;
    }
    if (!mediaId) {
      for (const key of idKeys) {
        const val = obj[key];
        if (val && typeof val !== 'object' && String(val) !== '[object Object]') {
          mediaId = String(val);
          break;
        }
      }
    }
    if (!base64) {
      for (const key of dataKeys) {
        const val = obj[key];
        if (typeof val === 'string' && val.length > 0 && val !== '[object Object]') {
          if (val.includes(';base64,')) {
            const [header, body] = val.split(';base64,');
            base64 = body;
            if (!mimeType) {
              const match = header.match(/data:(.*)/);
              if (match) mimeType = match[1];
            }
          } else {
            base64 = val;
          }
          break;
        }
      }
    }
    if (!mimeType) {
      for (const key of mimeKeys) {
        const val = obj[key];
        if (typeof val === 'string') {
          mimeType = val;
          break;
        }
      }
    }
    if (mediaId && base64) return;
    const nestedKeys = Object.keys(obj);
    for (const key of nestedKeys) {
      crawl(obj[key], depth + 1);
      if (mediaId && base64) return;
    }
  };
  crawl(raw, 0);
  if (mediaId && base64 && typeof base64 === 'string' && base64.trim() !== '') {
    return {
      mediaId: String(mediaId),
      base64: String(base64),
      mimeType: String(mimeType || "image/png")
    };
  }
  const rootKeys = raw && typeof raw === 'object' ? Object.keys(raw).join(', ') : 'none';
  throw new Error(`Flow image response has no transferable bytes; keys: ${rootKeys}`);
};
export default function App() {
  const [status, setStatus] = useState('Ожидание моста ФФ');
  const [logs, setLogs] = useState<BridgeLog[]>([]);
  const [imported, setImported] = useState<ImportedMedia | null>(null);
  const [items, setItems] = useState<GeneratedItem[]>([]);
  const [isGenerating, setIsGenerating] = useState(false);
  const [lastError, setLastError] = useState<string | null>(null);
  
  const [manualMode, setManualMode] = useState<'image' | 'video'>('video');
  const [manualPrompt, setManualPrompt] = useState('Портрет Томаса, кинематографичное освещение, 4k');
  const [imageModel, setImageModel] = useState('🍌 Nano Banana Pro');
  const [videoModel, setVideoModel] = useState<'Veo 3.1 - Lite' | 'Omni 1.1 Flash'>('Veo 3.1 - Lite');
  
  const [activeTab, setActiveTab] = useState<'preview' | 'logs'>('preview');
  const busyRef = useRef(false);
  const activeRequestsRef = useRef(new Map<string, string>());
  const requestSignaturesRef = useRef(new Map<string, string>());
  const completedMapRef = useRef(new Map<string, any>());
  const addLog = useCallback((type: BridgeLog['type'], message: string, data?: any) => {
    setLogs(prev => [{
      id: Math.random().toString(36).slice(2),
      type,
      message,
      data,
      timestamp: Date.now()
    }, ...prev].slice(0, 100));
  }, []);
  useEffect(() => {
    const send = (message: object) => {
      const payload = { channel: CHANNEL, ...message };
      try { window.top?.postMessage(payload, HOST); } catch (e) {}
      try { window.parent?.postMessage(payload, HOST); } catch (e) {}
    };
    const ready = () => send({ type: 'ready', protocolVersion: 2, imageConcurrency: 4 });
    const handleMessage = async (event: MessageEvent) => {
      const rawMessage = event.data;
      if (event.source !== window.top || event.origin !== HOST || !rawMessage || rawMessage.channel !== CHANNEL) return;
      const m = { ...rawMessage, referenceImageMediaIds: Array.isArray(rawMessage.referenceImageMediaIds) ? [...rawMessage.referenceImageMediaIds] : rawMessage.referenceImageMediaIds };
      addLog('in', `${m.type} [${m.requestId || 'no-id'}]`, m);
      if (m.type === 'ping') {
        send({ type: 'pong', requestId: m.requestId, protocolVersion: 2, imageConcurrency: 4 });
        return;
      }
      if (['generate', 'import_source_image', 'scene_video', 'scene_video_start'].includes(m.type)) {
        if (typeof m.requestId !== 'string' || !m.requestId) return;
        const signature = JSON.stringify([m.type, m.kind, m.id, m.scene_id, m.prompt, m.aspectRatio, m.modelDisplayName, m.referenceImageMediaIds, m.source_image_asset_id, m.source_image_sha256]);
        const previous = requestSignaturesRef.current.get(m.requestId);
        if (previous && previous !== signature) {
          send({ type: 'error', requestId: m.requestId, error: 'Request ID belongs to another job', code: 'FLOW_REQUEST_CONFLICT' });
          return;
        }
        requestSignaturesRef.current.set(m.requestId, signature);
        if (activeRequestsRef.current.has(m.requestId)) return;
      }
      if (m.type === 'import_source_image') {
        const cached = completedMapRef.current.get(m.requestId);
        if (cached) {
          send({ ...cached, type: 'source_image_imported', requestId: m.requestId });
          return;
        }
        if (busyRef.current || activeRequestsRef.current.size >= 4) {
          send({ type: 'error', requestId: m.requestId, error: 'Инструмент занят', code: 'FLOW_BUSY' });
          return;
        }
        if (!m.imageDataUrl) {
          addLog('error', 'Import failed: Missing imageDataUrl');
          send({ type: 'error', requestId: m.requestId, error: 'Отсутствует imageDataUrl' });
          return;
        }
        activeRequestsRef.current.set(m.requestId, 'import');
        setIsGenerating(true);
        setStatus(`Импорт кадра ${m.scene_id}...`);
        setLastError(null);
        try {
          const base64 = m.imageDataUrl.split(',')[1] || m.imageDataUrl;
          const uploadRes = await Flow.upload({
            base64,
            mimeType: 'image/png',
            name: `FF_Scene_${m.scene_id || 'imported'}`
          });
          if (!uploadRes.mediaId) throw new Error('Flow import has no mediaId');
          const impInfo: ImportedMedia = {
            sceneId: m.scene_id,
            revision: m.scene_revision || 1,
            assetId: m.source_image_asset_id,
            sha256: m.source_image_sha256,
            mediaId: uploadRes.mediaId,
            imageDataUrl: m.imageDataUrl
          };
          setImported(impInfo);
          setStatus(`✅ Кадра ${m.scene_id} зафиксирован`);
          addLog('info', `Scene ${m.scene_id} imported. Media ID: ${uploadRes.mediaId}`);
          completedMapRef.current.set(m.requestId, { scene_id: m.scene_id, scene_revision: m.scene_revision, source_image_asset_id: m.source_image_asset_id, source_image_sha256: m.source_image_sha256, mediaId: uploadRes.mediaId, mimeType: 'image/png' });
          send({
            type: 'source_image_imported',
            requestId: m.requestId,
            scene_id: m.scene_id,
            scene_revision: m.scene_revision,
            source_image_asset_id: m.source_image_asset_id,
            source_image_sha256: m.source_image_sha256,
            mediaId: uploadRes.mediaId,
            mimeType: 'image/png'
          });
        } catch (err: any) {
          const errStr = err.message || String(err);
          addLog('error', `Import error: ${errStr}`);
          setLastError(errStr);
          send({ type: 'error', requestId: m.requestId, error: errStr });
          setStatus('Ошибка импорта: ' + errStr);
        } finally {
          activeRequestsRef.current.delete(m.requestId);
          setIsGenerating(activeRequestsRef.current.size > 0 || busyRef.current);
        }
        return;
      }
      if (m.type === 'scene_card_upsert') {
        send({
          type: 'scene_card_ready',
          requestId: m.requestId,
          scene_id: m.scene_id || m.id,
          revision: m.revision || m.scene_revision,
          mediaId: m.mediaId || null
        });
        return;
      }
      const isVideo = ['scene_video', 'scene_video_start'].includes(m.type) || (m.type === 'generate' && m.kind === 'scene_video');
      const isImage = m.type === 'generate' && m.kind !== 'scene_video';
      if (!isVideo && !isImage) return;
      if (typeof m.requestId !== 'string') return;
      if (completedMapRef.current.has(m.requestId)) {
        const cached = completedMapRef.current.get(m.requestId);
        send({ type: 'result', requestId: m.requestId, result: cached });
        addLog('info', `Served cached result for ${m.requestId}`);
        return;
      }
      if (busyRef.current || activeRequestsRef.current.size >= 4 || (isVideo && activeRequestsRef.current.size > 0)) {
        send({ type: 'error', requestId: m.requestId, error: 'Инструмент занят', code: 'FLOW_BUSY' });
        return;
      }
      if (isVideo) busyRef.current = true;
      activeRequestsRef.current.set(m.requestId, isVideo ? 'video' : 'image');
      send({ type: 'diagnostic', requestId: m.requestId, phase: 'request_started', activeCount: activeRequestsRef.current.size, imageConcurrency: 4 });
      setIsGenerating(true);
      try {
        if (isVideo) {
          const sceneId = m.scene_id || m.id;
          const firstFrameId = m.firstFrameImageMediaId || m.referenceImageMediaIds?.[0] || imported?.mediaId;
          if (!firstFrameId) {
            throw new Error('T2V запрещён: отсутствует firstFrameImageMediaId');
          }
          const modelToUse = m.modelDisplayName || videoModel;
          const isOmni = modelToUse === 'Omni 1.1 Flash';
          setStatus(`Генерация видео (${modelToUse})...`);
          send({ type: 'scene_video_progress', requestId: m.requestId, scene_id: sceneId, status: 'generating_veo' });
          const videoRes = readMedia(await Flow.generate.video({
            prompt: m.prompt,
            modelDisplayName: modelToUse,
            aspectRatio: (m.aspectRatio === '16:9' || m.aspectRatio === '9:16') ? m.aspectRatio : '16:9',
            firstFrameImageMediaId: firstFrameId,
            ...(isOmni ? { durationSeconds: m.duration_seconds || 8, resolution: '720p' } : { resolution: '720p' })
          } as any));
          const mediaData = {
            mediaId: videoRes.mediaId,
            base64: videoRes.base64,
            mimeType: videoRes.mimeType,
            scene_id: sceneId,
            scene_revision: m.scene_revision,
            source_image_asset_id: m.source_image_asset_id,
            source_image_sha256: m.source_image_sha256,
            idempotency_key: m.idempotency_key
          };
          completedMapRef.current.set(m.requestId, mediaData);
          setItems(prev => [{
            id: m.requestId,
            type: 'video',
            base64: videoRes.base64,
            mimeType: videoRes.mimeType,
            mediaId: videoRes.mediaId,
            sceneId: sceneId,
            prompt: m.prompt,
            title: `Сцена ${sceneId?.slice(0, 8)}`,
            timestamp: Date.now()
          }, ...prev]);
          send({
            type: 'scene_video_result',
            requestId: m.requestId,
            scene_id: sceneId,
            scene_revision: m.scene_revision,
            source_image_asset_id: m.source_image_asset_id,
            source_image_sha256: m.source_image_sha256,
            idempotency_key: m.idempotency_key,
            flow_media_id: videoRes.mediaId,
            base64: videoRes.base64,
            mimeType: videoRes.mimeType
          });
          setStatus(`✅ Видео ${sceneId} передано в FF`);
        } else {
          const targetKind = m.kind || 'image';
          setStatus(`Генерация ${targetKind}...`);
          
          /**
           * Инкапсулированный запрос генерации изображения с проверками.
           */
          const imageRequest = async (selectedModel: string) => {
            const rawRefs = m.referenceImageMediaIds || [];
            const uniqueRefs = Array.from(new Set(rawRefs.filter((id: any) => typeof id === 'string' && id.trim() !== '')));
            
            if (!MODEL_LIMITS[selectedModel]) {
              const err = new Error(`Модель ${selectedModel} не поддерживается.`);
              (err as any).name = 'PreflightError';
              (err as any).code = 'ERR_UNSUPPORTED_MODEL';
              (err as any).cause = 'VALIDATION';
              throw err;
            }
            const limit = MODEL_LIMITS[selectedModel];
            if (uniqueRefs.length > limit) {
              const err = new Error(`Лимит превышен: модель ${selectedModel} поддерживает до ${limit} уникальных ссылок.`);
              (err as any).name = 'PreflightError';
              (err as any).code = 'ERR_TOO_MANY_REFS';
              (err as any).cause = 'VALIDATION';
              throw err;
            }
            addLog('info', 'IMAGE_STAGE sdk_call', { model: selectedModel, refs: uniqueRefs.length });
            
            const raw = await Flow.generate.image({
              prompt: m.prompt,
              aspectRatio: m.aspectRatio || (['characters', 'props'].includes(targetKind) ? '1:1' : '16:9'),
              modelDisplayName: selectedModel,
              ...(uniqueRefs.length ? { referenceImageMediaIds: uniqueRefs } : {})
            });
            
            const media = readMedia(raw);
            return { ...media, modelDisplayName: selectedModel };
          };
          let imgRes;
          const initialModel = m.modelDisplayName || '🍌 Nano Banana 2';
          
          try {
            imgRes = await imageRequest(initialModel);
          } catch (e: any) {
            // Если ошибка ERR_GEN_FAILED и мы просили Pro — пробуем fallback на Banana 2
            if ((e.code === 'ERR_GEN_FAILED' || e.message?.includes('ERR_GEN_FAILED')) && initialModel === '🍌 Nano Banana Pro') {
              addLog('info', 'IMAGE_STAGE image_model_fallback');
              imgRes = await imageRequest('🍌 Nano Banana 2');
            } else {
              throw e;
            }
          }
          const mediaData = { 
            mediaId: imgRes.mediaId, 
            base64: imgRes.base64, 
            mimeType: imgRes.mimeType,
            modelDisplayName: imgRes.modelDisplayName 
          };
          
          completedMapRef.current.set(m.requestId, mediaData);
          setItems(prev => [{
            id: m.requestId,
            type: 'image',
            mediaId: imgRes.mediaId,
            base64: imgRes.base64,
            mimeType: imgRes.mimeType,
            prompt: m.prompt,
            title: targetKind.toUpperCase(),
            timestamp: Date.now()
          }, ...prev]);
          send({ type: 'result', requestId: m.requestId, result: mediaData });
          setStatus(`✅ ${targetKind} готово`);
        }
      } catch (e: any) {
        const structuredError = {
          name: e.name || 'GenerationError',
          message: e.message || String(e),
          cause: e.cause || 'INTERNAL',
          code: e.code || 'FLOW_UNKNOWN_ERROR'
        };
        addLog('error', `Generation failed: ${structuredError.message}`, structuredError);
        setLastError(structuredError.message);
        send({ type: 'error', requestId: m.requestId, error: structuredError });
        if (isVideo) {
          send({ type: 'scene_video_error', requestId: m.requestId, scene_id: m.scene_id, error: structuredError });
        }
        setStatus('Ошибка: ' + structuredError.message);
      } finally {
        activeRequestsRef.current.delete(m.requestId);
        if (isVideo) busyRef.current = false;
        setIsGenerating(activeRequestsRef.current.size > 0 || busyRef.current);
        send({ type: 'diagnostic', requestId: m.requestId, phase: 'request_finished', activeCount: activeRequestsRef.current.size, imageConcurrency: 4 });
      }
    };
    window.addEventListener('message', handleMessage);
    ready();
    const timer = setInterval(ready, 2000);
    return () => {
      clearInterval(timer);
      window.removeEventListener('message', handleMessage);
    };
  }, [imported, videoModel, addLog]);
  const handleManualGenerate = async () => {
    if (isGenerating || busyRef.current || activeRequestsRef.current.size || !manualPrompt.trim()) return;
    busyRef.current = true;
    setIsGenerating(true);
    setLastError(null);
    setStatus(`Ручная генерация (${manualMode})...`);
    try {
      if (manualMode === 'image') {
        const res = readMedia(await Flow.generate.image({
          prompt: manualPrompt,
          aspectRatio: '16:9',
          modelDisplayName: imageModel
        }));
        setItems(prev => [{
          id: `manual_${Date.now()}`,
          type: 'image',
          mediaId: res.mediaId,
          base64: res.base64,
          mimeType: res.mimeType,
          prompt: manualPrompt,
          title: 'Ручное фото',
          timestamp: Date.now()
        }, ...prev]);
        setStatus('✅ Фото создано');
      } else {
        const isOmni = videoModel === 'Omni 1.1 Flash';
        const res = readMedia(await Flow.generate.video({
          prompt: manualPrompt,
          modelDisplayName: videoModel,
          aspectRatio: '16:9',
          firstFrameImageMediaId: imported?.mediaId,
          ...(isOmni ? { durationSeconds: 8, resolution: '720p' } : { resolution: '720p' })
        } as any));
        setItems(prev => [{
          id: `manual_${Date.now()}`,
          type: 'video',
          mediaId: res.mediaId,
          base64: res.base64,
          mimeType: res.mimeType,
          sceneId: imported?.sceneId,
          prompt: manualPrompt,
          title: 'Ручное видео',
          timestamp: Date.now()
        }, ...prev]);
        setStatus('✅ Видео создано');
      }
    } catch (err: any) {
      setLastError(err.message || String(err));
      setStatus('Ошибка: ' + (err.message || String(err)));
    } finally {
      busyRef.current = false;
      setIsGenerating(activeRequestsRef.current.size > 0);
    }
  };
  return (
    <div className="flex flex-col h-full bg-[#0a0a0c] text-slate-100 font-sans selection:bg-cyan-500/30 overflow-hidden">
      <Header status={status} isGenerating={isGenerating} />
      
      <div className="flex flex-1 overflow-hidden">
        <ControlPanel 
          imported={imported} 
          videoModel={videoModel} 
          setVideoModel={setVideoModel} 
          imageModel={imageModel}
          setImageModel={setImageModel}
          isGenerating={isGenerating}
          manualMode={manualMode}
          setManualMode={setManualMode}
          manualPrompt={manualPrompt}
          setManualPrompt={setManualPrompt}
          onGenerate={handleManualGenerate}
          lastError={lastError}
        />
        <main className="flex-1 flex flex-col relative bg-[#0f0f12]">
          <div className="flex border-b border-white/5 bg-black/20 p-1 m-4 self-start rounded-xl text-[10px]">
             <div className="px-3 py-1 text-slate-500 font-mono tracking-tighter">
               BRIDGE_V1.2 · IMAGES ×4
             </div>
          </div>
          <div className="flex border-b border-white/5 bg-black/20 p-1 m-4 mt-0 self-start rounded-xl">
            <button 
              onClick={() => setActiveTab('preview')}
              className={`px-6 py-2 rounded-lg text-xs font-bold uppercase tracking-wider transition-all ${activeTab === 'preview' ? 'bg-white/10 text-cyan-400 shadow-sm' : 'text-slate-500 hover:text-slate-300'}`}
            >
              Галерея Вывода ({items.length})
            </button>
            <button 
              onClick={() => setActiveTab('logs')}
              className={`px-6 py-2 rounded-lg text-xs font-bold uppercase tracking-wider transition-all ${activeTab === 'logs' ? 'bg-white/10 text-cyan-400 shadow-sm' : 'text-slate-500 hover:text-slate-300'}`}
            >
              Протокол Bridge ({logs.length})
            </button>
          </div>
          <div className="flex-1 overflow-hidden p-6 pt-0">
            {activeTab === 'preview' ? (
              <PreviewGrid items={items} />
            ) : (
              <LogTerminal logs={logs} />
            )}
          </div>
        </main>
      </div>
    </div>
  );
}