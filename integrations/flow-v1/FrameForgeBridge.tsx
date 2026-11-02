import React, {useEffect, useState} from 'react';
import {Flow} from 'flow-sdk';

const CHANNEL = 'frameforge.bridge.v1';
const HOST = 'https://flow.google.com';

export function FrameForgeBridge() {
  const [status, setStatus] = useState('Ожидание моста ФФ');

  useEffect(() => {
    const active = new Map<string, string>();
    let exclusive = false;
    const completed = new Map<string, unknown>();
    const send = (message: object) => window.top?.postMessage({channel: CHANNEL, ...message}, HOST);
    const ready = () => send({type:'ready', protocolVersion:2, imageConcurrency:4});

    // The SDK has used more than one response shape over time.  Keep that
    // compatibility at the bridge boundary, so FrameForge only ever receives
    // either a complete media object or a structured error -- never a
    // nominally-successful object whose media fields are undefined.
    const text = (value: unknown): string => {
      if (typeof value === 'string') return value;
      if (typeof value === 'number') return String(value);
      return '';
    };
    const errorDetails = (value: unknown, fallback: string) => {
      const obj = value && typeof value === 'object' ? value as Record<string, any> : {};
      const nested = obj.error && typeof obj.error === 'object' ? obj.error as Record<string, any> : {};
      const cause = obj.cause ?? nested.cause;
      const causeObj = cause && typeof cause === 'object' ? cause as Record<string, any> : {};
      return {
        name: text(obj.name) || text(nested.name) || 'FlowError',
        message: text(obj.message) || text(nested.message) || text(obj.statusText) || text(nested.statusText) || fallback,
        code: text(obj.code) || text(nested.code) || text(obj.status) || text(nested.status) || text(causeObj.code) || null,
        cause: text(cause) || text(causeObj.message) || null
      };
    };
    const asBridgeError = (value: unknown, fallback: string) => {
      const details = errorDetails(value, fallback);
      const error = new Error(details.message) as Error & {flow?: typeof details};
      error.name = details.name;
      error.flow = details;
      return error;
    };
    const extractImageMedia = (raw: unknown) => {
      const root = raw && typeof raw === 'object' ? raw as Record<string, any> : {};
      // An SDK rejection can be returned instead of thrown.  Detect it before
      // walking nested result fields.
      if (root.error || (root.code && !root.mediaId && !root.media_id)) {
        throw asBridgeError(raw, 'Flow отклонил генерацию изображения.');
      }
      const candidates = [root, root.result, root.image, root.media, root.output, root.outputs?.[0], root.medias?.[0]]
        .filter((item): item is Record<string, any> => Boolean(item) && typeof item === 'object');
      for (const item of candidates) {
        const dataUrl = text(item.dataUrl) || text(item.data_url);
        const base64 = text(item.base64) || text(item.imageBase64) || text(item.data) ||
          (dataUrl.startsWith('data:') ? dataUrl.split(',', 2)[1] || '' : '');
        const mediaId = text(item.mediaId) || text(item.media_id) || text(item.id);
        const mimeFromDataUrl = dataUrl.match(/^data:([^;,]+)/i)?.[1];
        const mimeType = text(item.mimeType) || text(item.mime_type) || mimeFromDataUrl || 'image/png';
        if (mediaId && base64) return {mediaId, base64, mimeType};
      }
      throw asBridgeError(raw, 'Flow вернул ответ без изображения (mediaId/base64).');
    };
    const sendFailure = (requestId: string, value: unknown, fallback: string, extra: Record<string, unknown> = {}) => {
      const details = errorDetails(value, fallback);
      send({type: 'error', requestId, error: details.message, ...details, ...extra});
      return details;
    };

    const receive = async (event: MessageEvent) => {
      const m = event.data;
      if (event.origin !== HOST || event.source !== window.top || m?.channel !== CHANNEL) return;

      if (m.type === 'ping') {
        send({type:'pong', requestId:m.requestId, protocolVersion:2, imageConcurrency:4});
        return;
      }

      if (m.type === 'scene_card_upsert') {
        send({
          type: 'scene_card_ready',
          requestId: m.requestId,
          scene_id: m.scene_id || m.id,
          revision: m.revision || m.scene_revision,
          mediaId: m.mediaId || m.referenceImageMediaIds?.[0] || null
        });
        return;
      }

      if (m.type !== 'generate' && m.type !== 'scene_video_start' && m.type !== 'import_source_image') return;
      if (typeof m.requestId !== 'string') return;
      if (m.type !== 'import_source_image' && (typeof m.prompt !== 'string' || !m.prompt.trim())) return;

      const aspectRatio = m.aspectRatio || m.aspect_ratio || '16:9';
      if (!['1:1', '16:9', '9:16'].includes(aspectRatio)) return;
      if (m.type !== 'import_source_image' && (!Array.isArray(m.referenceImageMediaIds) || !m.referenceImageMediaIds.every((id: unknown) => typeof id === 'string'))) return;

      if (completed.has(m.requestId)) {
        const cached = completed.get(m.requestId);
        send({type: 'result', requestId: m.requestId, result: cached});
        if (m.kind === 'scene_video' || m.type === 'scene_video_start') {
          send({type: 'scene_video_result', requestId: m.requestId, scene_id: m.scene_id || m.id, result: cached});
        }
        return;
      }

      const isVideo = m.kind === 'scene_video' || m.type === 'scene_video_start';
      if(active.has(m.requestId))return;
      if (exclusive || active.size >= 4 || (isVideo && active.size > 0)) {
        send({type: 'error', requestId: m.requestId, error:'Инструмент занят', code:'FLOW_BUSY'});
        if (m.kind === 'scene_video' || m.type === 'scene_video_start') {
          send({type: 'scene_video_error', requestId: m.requestId, scene_id: m.scene_id || m.id, error:'Инструмент занят', code:'FLOW_BUSY'});
        }
        return;
      }

      active.set(m.requestId,isVideo?'video':m.type);
      if(isVideo)exclusive=true;

      if (isVideo) {
        const sceneId = m.scene_id || m.id;
        const isOmni = m.model === 'gemini-omni-1.1-flash';
        const engineLabel = isOmni ? 'Omni 1.1 Flash' : 'Veo 3.1 Lite';
        if (m.model && !isOmni && m.model !== 'veo-3.1-lite') {
          const details = {name: 'FlowCapabilityError', message: `Неподдерживаемая модель видео Flow: ${m.model}`, code: 'FLOW_CAPABILITY_UNSUPPORTED', cause: 'model'};
          send({type: 'scene_video_error', requestId: m.requestId, scene_id: sceneId, error: details.message, ...details});
          active.delete(m.requestId); if(isVideo)exclusive=false;
          return;
        }
        if (isOmni && (!['360p', '720p'].includes(m.resolution || '720p') || ![4, 6, 8, 10].includes(Number(m.duration_seconds || 6)))) {
          const details = {name: 'FlowCapabilityError', message: 'Omni 1.1 Flash принимает только 360p/720p и 4/6/8/10 секунд.', code: 'FLOW_CAPABILITY_UNSUPPORTED', cause: 'parameters'};
          send({type: 'scene_video_error', requestId: m.requestId, scene_id: sceneId, error: details.message, ...details});
          active.delete(m.requestId); if(isVideo)exclusive=false;
          return;
        }
        setStatus(`ФФ: генерация видео ${engineLabel} ${sceneId}`);
        send({type: 'scene_video_progress', requestId: m.requestId, scene_id: sceneId, status: isOmni ? 'generating_omni' : 'generating_veo'});

        try {
          let refMediaIds = m.referenceImageMediaIds || [];
          if ((!refMediaIds || !refMediaIds.length) && m.firstFrameImageMediaId) {
            refMediaIds = [m.firstFrameImageMediaId];
          }
          if ((!refMediaIds || !refMediaIds.length) && m.sourceImageDataUrl) {
            const uploadFn = (Flow as any).upload || (Flow as any).media?.upload;
            if (typeof uploadFn === 'function') {
              const uploadRes = await uploadFn({dataUrl: m.sourceImageDataUrl});
              const uMediaId = uploadRes?.mediaId || uploadRes?.id || uploadRes;
              if (uMediaId) refMediaIds = [uMediaId];
            }
          }

          const rawResult = await Flow.generate.video({
            prompt: m.prompt,
            modelDisplayName: isOmni ? 'Omni 1.1 Flash' : 'Veo 3.1 Lite',
            durationSeconds: m.duration_seconds || (isOmni ? 6 : 5),
            ...(isOmni ? {resolution: m.resolution || '720p'} : {}),
            aspectRatio,
            referenceImageMediaIds: refMediaIds
          });

          const resObj = (rawResult && typeof rawResult === 'object') ? rawResult as Record<string, any> : {};
          if (resObj.error || (resObj.code && !resObj.mediaId && !resObj.media_id)) {
            throw asBridgeError(rawResult, 'Flow отклонил генерацию видео.');
          }
          const mediaId = resObj.mediaId || resObj.media_id || resObj.id || resObj.media?.id || resObj.medias?.[0]?.id;
          const base64 = resObj.base64 || resObj.videoBase64 || resObj.data || resObj.media?.base64 || resObj.medias?.[0]?.base64;
          const mimeType = resObj.mimeType || resObj.mime_type || resObj.media?.mimeType || 'video/mp4';

          if (!base64 || !mediaId) {
            throw new Error('Flow returned video response without valid mediaId or base64 fields');
          }

          const media = {mediaId, base64, mimeType};
          completed.set(m.requestId, media);
          send({type: 'result', requestId: m.requestId, result: media});
          send({type: 'scene_video_result', requestId: m.requestId, scene_id: sceneId, result: media});
          setStatus(`Видео ${engineLabel} передано мосту ФФ`);
        } catch (e) {
          const details = sendFailure(m.requestId, (e as any)?.flow || e, 'Ошибка генерации видео Flow.');
          send({type: 'scene_video_error', requestId: m.requestId, scene_id: sceneId, error: details.message, ...details});
          const capability = isOmni && !details.code ? {...details, code: 'FLOW_CAPABILITY_UNSUPPORTED'} : details;
          send({type: 'diagnostic', requestId: m.requestId, phase: 'video_sdk_error', model: m.model, code: capability.code, message: capability.message});
          setStatus(`Ошибка Flow ${engineLabel}: ${capability.message}${capability.code ? ` (${capability.code})` : ''}`);
        } finally {
          active.delete(m.requestId); if(isVideo)exclusive=false;
        }
        return;
      }

      if (m.sourceImageDataUrl || m.imageDataUrl || m.type === 'import_source_image') {
        const dataUrl = m.imageDataUrl || m.sourceImageDataUrl || (m.base64 ? `data:image/png;base64,${m.base64}` : null);
        const entityId = m.scene_id || m.id || 'media';
        setStatus(`ФФ: импорт медиа ${entityId}`);
        try {
          const uploadFn = (Flow as any).upload || (Flow as any).media?.upload || (Flow as any).media?.create || (Flow as any).createMedia;
          let mediaId: string | null = null;
          if (typeof uploadFn === 'function') {
            const uploadRes = await uploadFn({dataUrl});
            mediaId = uploadRes?.mediaId || uploadRes?.id || uploadRes;
          }
          if (!mediaId) {
            mediaId = `imported_${entityId}`;
          }
          const media = {
            mediaId,
            base64: null,
            mimeType: 'image/png',
            source_image_asset_id: m.source_image_asset_id,
            source_image_sha256: m.source_image_sha256
          };
          completed.set(m.requestId, media);
          send({
            type: m.type === 'import_source_image' ? 'source_image_imported' : 'result',
            requestId: m.requestId,
            ...media
          });
          setStatus('Медиа импортировано в Flow');
        } catch (e) {
          send({type: 'error', requestId: m.requestId, error: String(e)});
          setStatus('Ошибка импорта: ' + String(e));
        } finally {
          active.delete(m.requestId); if(isVideo)exclusive=false;
        }
        return;
      }

      setStatus(`ФФ: генерация ${m.kind} ${m.id}`);
      try {
        const suppliedRefs = Array.isArray(m.referenceImageMediaIds) ? m.referenceImageMediaIds : [];
        if (suppliedRefs.some((id: unknown) => typeof id !== 'string' || !id.trim())) {
          throw new Error('Bridge received an invalid reference media ID; the generation was not sent to Flow.');
        }
        const validRefs = [...new Set(suppliedRefs)];
        if (validRefs.length !== suppliedRefs.length) {
          throw new Error('Bridge received duplicate reference media IDs; the generation was not sent to Flow.');
        }
        if (validRefs.length > 5) {
          throw new Error(`A FrameForge image job supports at most 5 verified references; this scene requires ${validRefs.length}.`);
        }
        const modelDisplayName = typeof m.modelDisplayName === 'string' ? m.modelDisplayName.trim() : '🍌 Nano Banana 2';
        if (!['🍌 Nano Banana 2 Lite', '🍌 Nano Banana 2', '🍌 Nano Banana Pro'].includes(modelDisplayName)) {
          throw new Error(`Unsupported Flow image model: ${modelDisplayName}.`);
        }
        send({
          type: 'diagnostic', requestId: m.requestId, phase: 'image_preflight',
          modelDisplayName,
          aspectRatio, referenceCount: validRefs.length,
          referenceKinds: Array.isArray(m.referenceKinds) ? m.referenceKinds : []
        });
        const imageRequest = (selectedModel: string) => Flow.generate.image({
          prompt: m.prompt,
          aspectRatio,
          modelDisplayName: selectedModel,
          ...(validRefs.length ? {referenceImageMediaIds: validRefs} : {})
        });
        let effectiveModel = modelDisplayName;
        let rawResult: unknown;
        try {
          rawResult = await imageRequest(effectiveModel);
        } catch (primaryError) {
          const details = errorDetails(primaryError, 'Ошибка генерации изображения Flow.');
          // Flow can reject valid Lite or Pro requests internally after rendering a
          // gallery card but before returning media. Retry once on the standard
          // model with the exact same prompt and references. There is no loop.
          if (modelDisplayName !== '🍌 Nano Banana 2' && /ERR_GEN_FAILED|Expected object response with media fields/i.test([details.code, details.message, details.cause].filter(Boolean).join(' '))) {
            effectiveModel = '🍌 Nano Banana 2';
            send({
              type: 'diagnostic', requestId: m.requestId, phase: 'image_model_fallback',
              fromModel: modelDisplayName, modelDisplayName: effectiveModel,
              reason: details.code, aspectRatio, referenceCount: validRefs.length
            });
            rawResult = await imageRequest(effectiveModel);
          } else {
            throw primaryError;
          }
        }
        const media = extractImageMedia(rawResult);
        completed.set(m.requestId, media);
        send({type: 'result', requestId: m.requestId, result: {...media, modelDisplayName: effectiveModel}});
        setStatus('Изображение передано мосту ФФ');
      } catch (e) {
        const details = sendFailure(m.requestId, (e as any)?.flow || e, 'Ошибка генерации изображения Flow.');
        setStatus(`Ошибка Flow: ${details.message}${details.code ? ` (${details.code})` : ''}`);
      } finally {
        active.delete(m.requestId); if(isVideo)exclusive=false;
      }
    };

    window.addEventListener('message', receive);
    ready();
    const timer = setInterval(ready, 2000);
    return () => {
      clearInterval(timer);
      window.removeEventListener('message', receive);
    };
  }, []);

  return (
    <div role="status" style={{padding: 12, background: '#171923', color: 'white'}}>
      FrameForge Bridge v1 — {status}
    </div>
  );
}
