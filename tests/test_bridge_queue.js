// Real JavaScript integration test for FrameForge Flow Bridge queue lifecycle & reference recovery
// Executes the ACTUAL Userscript code (ensureReferenceMediaId / loadSourceImageDataUrl / processBridgeItem / processBridgeQueue) in Node.js VM sandbox.
import assert from 'assert';
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Load actual userscript source
const jsPath = path.resolve(__dirname, '../imagegen/static/frameforge-flow-v1.user.js');
const jsCode = fs.readFileSync(jsPath, 'utf-8');

// Ensure userscript contains critical queue and error handling functions
assert(jsCode.includes('state.blockedScenes'), 'Userscript must track state.blockedScenes');
assert(jsCode.includes('ensureReferenceMediaId'), 'Userscript must implement ensureReferenceMediaId');
assert(jsCode.includes('loadSourceImageDataUrl'), 'Userscript must implement loadSourceImageDataUrl');
assert(jsCode.includes('ready_for_flow'), 'Userscript must inspect ready_for_flow');
assert(jsCode.includes('processBridgeItem'), 'Userscript must export processBridgeItem');
assert(jsCode.includes('processBridgeQueue'), 'Userscript must export processBridgeQueue');

console.log('Userscript source code invariants verified.');

// Instantiate Userscript in mock DOM environment
function createUserscriptContext(options = {}) {
    const store = new Map();
    const sandbox = {
        console,
        Buffer,
        btoa: (str) => Buffer.from(str, 'binary').toString('base64'),
        atob: (b64) => Buffer.from(b64, 'base64').toString('binary'),
        crypto: {
            randomUUID: () => 'uuid_' + Math.random().toString(36).substring(2, 10)
        },
        location: {
            pathname: '/project/flow_main',
            origin: 'https://labs.google'
        },
        document: {
            getElementById: () => null,
            createElement: () => ({
                setAttribute() {},
                appendChild() {},
                append() {},
                addEventListener() {},
                removeEventListener() {},
                style: {}
            }),
            body: { appendChild() {}, append() {} },
            head: { appendChild() {}, append() {} }
        },
        window: {},
        GM_getValue: (key, def) => store.has(key) ? store.get(key) : def,
        GM_setValue: (key, val) => store.set(key, val),
        GM_xmlhttpRequest: options.GM_xmlhttpRequest || (() => {}),
        setInterval: () => 1,
        clearInterval: () => {},
        setTimeout: (fn) => setTimeout(fn, 0),
        clearTimeout: () => {},
        addEventListener: () => {},
        removeEventListener: () => {}
    };
    sandbox.window = sandbox;
    sandbox.globalThis = sandbox;

    const context = vm.createContext(sandbox);
    const code = options.lifecycle ? jsCode.replace('const generateImageWithModelFailover=', 'let generateImageWithModelFailover=').replace('globalThis.FFBridgeQueue = {', `globalThis.FFBridgeQueue = {
      run, heartbeat,
      configureTest(apiFn,imageFn,uploadFn){peer={};api=apiFn;generateImageWithModelFailover=imageFn;uploadSaved=uploadFn;heartbeatBusy=false;},
      inspectTest(){return {running,peer,status:status.textContent,processedCommandId,ackCommandId,state};},
      setRunningTest(value){running=value;},`) : jsCode;
    vm.runInContext(code, context);

    return context.FFBridgeQueue;
}

async function runTests() {
    console.log('Running Real JavaScript Bridge Queue & Reference Tests...');

    const bridgeModule = createUserscriptContext();
    assert(typeof bridgeModule.ensureReferenceMediaId === 'function', 'ensureReferenceMediaId must be a function in userscript');
    assert(typeof bridgeModule.loadSourceImageDataUrl === 'function', 'loadSourceImageDataUrl must be a function in userscript');
    assert(typeof bridgeModule.processBridgeItem === 'function', 'processBridgeItem must be a function in userscript');
    assert(typeof bridgeModule.processBridgeQueue === 'function', 'processBridgeQueue must be a function in userscript');

    // =========================================================================
    // Part 1: Direct Unit Tests of ensureReferenceMediaId and Media Handling
    // =========================================================================
    console.log('Testing ensureReferenceMediaId and loadSourceImageDataUrl directly...');

    // 1.1 Successful PNG reference import
    {
        const state = { projectId: 'proj_1', flowProject: 'flow_proj_a', refs: {}, refAssets: {} };
        const saved = [];
        const generateCalls = [];
        const mid = await bridgeModule.ensureReferenceMediaId(
            { id: 'char_daniel', type: 'character', name: 'Дэниел', asset_id: 'ast_dan_1' },
            1,
            {
                state,
                tell: () => {},
                save: () => saved.push(JSON.parse(JSON.stringify(state))),
                loadSourceImageDataUrlFn: async () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
                generateFn: async (payload) => {
                    generateCalls.push(payload);
                    return { mediaId: 'flow_media_dan_1' };
                }
            }
        );
        assert.strictEqual(mid, 'flow_media_dan_1', 'Must return imported flow mediaId for PNG');
        assert.strictEqual(generateCalls.length, 1, 'Generate import should be called once');
        assert.strictEqual(state.refs['characters:char_daniel'], 'flow_media_dan_1');
        assert.strictEqual(state.refs['ref:proj_1:flow_proj_a:ast_dan_1'], 'flow_media_dan_1');
        assert.strictEqual(state.refAssets['characters:char_daniel'], 'ast_dan_1');
    }

    // 1.2 Successful JPEG reference import
    {
        const state = { projectId: 'proj_1', flowProject: 'flow_proj_a', refs: {}, refAssets: {} };
        const mid = await bridgeModule.ensureReferenceMediaId(
            { id: 'loc_forest', type: 'location', name: 'Лес', asset_id: 'ast_forest_1' },
            1,
            {
                state,
                tell: () => {},
                save: () => {},
                loadSourceImageDataUrlFn: async () => 'data:image/jpeg;base64,/9j/4AAQSkZJRg==',
                generateFn: async () => ({ mediaId: 'flow_media_forest_1' })
            }
        );
        assert.strictEqual(mid, 'flow_media_forest_1', 'Must return imported mediaId for JPEG');
    }

    // 1.3 Successful WebP reference import
    {
        const state = { projectId: 'proj_1', flowProject: 'flow_proj_a', refs: {}, refAssets: {} };
        const mid = await bridgeModule.ensureReferenceMediaId(
            { id: 'prop_key', type: 'prop', name: 'Ключ', asset_id: 'ast_key_1' },
            1,
            {
                state,
                tell: () => {},
                save: () => {},
                loadSourceImageDataUrlFn: async () => 'data:image/webp;base64,UklGRkAAAABXRUJQVlA4',
                generateFn: async () => ({ mediaId: 'flow_media_key_1' })
            }
        );
        assert.strictEqual(mid, 'flow_media_key_1', 'Must return imported mediaId for WebP');
    }

    // 1.4 Unsupported MIME (e.g. text/plain or pdf) rejected by loadSourceImageDataUrl
    {
        let errorThrown = null;
        const bridgeWithXml = createUserscriptContext({
            GM_xmlhttpRequest: ({ onload }) => {
                onload({
                    status: 200,
                    responseHeaders: 'content-type: text/plain',
                    response: new Uint8Array([104, 101, 108, 108, 111]).buffer
                });
            }
        });
        try {
            await bridgeWithXml.loadSourceImageDataUrl('/api/assets/bad_text');
        } catch(e) {
            errorThrown = e;
        }
        assert(errorThrown, 'Unsupported MIME must throw an error');
        assert(errorThrown.message.includes('Неподдерживаемый формат референса'), `Unexpected error message: ${errorThrown.message}`);
    }

    // 1.5 Empty file (0 bytes) rejected
    {
        let errorThrown = null;
        const bridgeWithXml = createUserscriptContext({
            GM_xmlhttpRequest: ({ onload }) => {
                onload({
                    status: 200,
                    responseHeaders: 'content-type: image/png',
                    response: new Uint8Array([]).buffer
                });
            }
        });
        try {
            await bridgeWithXml.loadSourceImageDataUrl('/api/assets/empty');
        } catch(e) {
            errorThrown = e;
        }
        assert(errorThrown, 'Empty file must throw an error');
        assert(errorThrown.message.includes('Пустой файл референса'), `Unexpected error message: ${errorThrown.message}`);
    }

    // 1.6 File too large (>10MB) rejected
    {
        let errorThrown = null;
        const bridgeWithXml = createUserscriptContext({
            GM_xmlhttpRequest: ({ onload }) => {
                const big = new Uint8Array(11 * 1024 * 1024);
                onload({
                    status: 200,
                    responseHeaders: 'content-type: image/png',
                    response: big.buffer
                });
            }
        });
        try {
            await bridgeWithXml.loadSourceImageDataUrl('/api/assets/huge');
        } catch(e) {
            errorThrown = e;
        }
        assert(errorThrown, 'Too large file must throw an error');
        assert(errorThrown.message.includes('слишком большой'), `Unexpected error message: ${errorThrown.message}`);
    }

    // 1.7 Missing / empty filename handled safely with fallback
    {
        const state = { projectId: 'proj_1', flowProject: 'flow_proj_a', refs: {}, refAssets: {} };
        const mid = await bridgeModule.ensureReferenceMediaId(
            { id: 'char_anon', type: 'character', name: '', asset_id: 'ast_anon' },
            1,
            {
                state,
                tell: () => {},
                save: () => {},
                loadSourceImageDataUrlFn: async () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
                generateFn: async () => ({ mediaId: 'flow_media_anon' })
            }
        );
        assert.strictEqual(mid, 'flow_media_anon', 'Missing name/filename must succeed with safe fallback');
    }

    // 1.8 Flow Project Change (Cache invalidation on project switch)
    {
        const state = {
            projectId: 'proj_1',
            flowProject: 'flow_proj_A',
            refs: {
                'characters:char_daniel': 'flow_media_dan_OLD',
                'ref:proj_1:flow_proj_A:ast_dan_1': 'flow_media_dan_OLD'
            },
            refAssets: {
                'characters:char_daniel': 'ast_dan_1'
            }
        };

        // Switch to flow_proj_B
        state.flowProject = 'flow_proj_B';
        let generateCount = 0;
        const mid = await bridgeModule.ensureReferenceMediaId(
            { id: 'char_daniel', type: 'character', name: 'Дэниел', asset_id: 'ast_dan_1' },
            1,
            {
                state,
                tell: () => {},
                save: () => {},
                loadSourceImageDataUrlFn: async () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
                generateFn: async () => {
                    generateCount++;
                    return { mediaId: 'flow_media_dan_NEW_PROJECT' };
                }
            }
        );
        assert.strictEqual(mid, 'flow_media_dan_NEW_PROJECT', 'Switching Flow project must re-import reference');
        assert.strictEqual(generateCount, 1, 'Generate import should run on project switch');
        assert.strictEqual(state.refs['ref:proj_1:flow_proj_B:ast_dan_1'], 'flow_media_dan_NEW_PROJECT');
    }

    // 1.9 asset_id Change (Cache invalidation on reference asset revision)
    {
        const state = {
            projectId: 'proj_1',
            flowProject: 'flow_proj_a',
            refs: {
                'characters:char_daniel': 'flow_media_dan_V1',
                'ref:proj_1:flow_proj_a:ast_dan_v1': 'flow_media_dan_V1'
            },
            refAssets: {
                'characters:char_daniel': 'ast_dan_v1'
            }
        };

        let generateCount = 0;
        const mid = await bridgeModule.ensureReferenceMediaId(
            { id: 'char_daniel', type: 'character', name: 'Дэниел', asset_id: 'ast_dan_v2' }, // updated asset_id
            1,
            {
                state,
                tell: () => {},
                save: () => {},
                loadSourceImageDataUrlFn: async () => 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
                generateFn: async () => {
                    generateCount++;
                    return { mediaId: 'flow_media_dan_V2' };
                }
            }
        );
        assert.strictEqual(mid, 'flow_media_dan_V2', 'Asset ID change must invalidate stale cache and re-import');
        assert.strictEqual(generateCount, 1);
        assert.strictEqual(state.refAssets['characters:char_daniel'], 'ast_dan_v2');
    }

    // 1.10 Network Failure during reference import (Throws error cleanly without corrupting state)
    {
        const state = { projectId: 'proj_1', flowProject: 'flow_proj_a', refs: {}, refAssets: {} };
        let thrown = null;
        try {
            await bridgeModule.ensureReferenceMediaId(
                { id: 'char_daniel', type: 'character', name: 'Дэниел', asset_id: 'ast_dan_1' },
                1,
                {
                    state,
                    tell: () => {},
                    save: () => {},
                    loadSourceImageDataUrlFn: async () => { throw Error('Network 500: Server unreachable'); },
                    generateFn: async () => ({ mediaId: 'flow_media_dan' })
                }
            );
        } catch(e) {
            thrown = e;
        }
        assert(thrown, 'Network failure must propagate exception');
        assert(thrown.message.includes('500'), 'Error message should contain network details');
        assert.strictEqual(state.refs['characters:char_daniel'], undefined, 'Failed reference must not be cached');
    }

    // 1.11 Cache reuse of correct mediaId (Zero network calls)
    {
        const state = {
            projectId: 'proj_1',
            flowProject: 'flow_proj_a',
            refs: {
                'characters:char_daniel': 'flow_media_cached_123',
                'ref:proj_1:flow_proj_a:ast_dan_1': 'flow_media_cached_123'
            },
            refAssets: {
                'characters:char_daniel': 'ast_dan_1'
            }
        };

        let networkCalled = false;
        const mid = await bridgeModule.ensureReferenceMediaId(
            { id: 'char_daniel', type: 'character', name: 'Дэниел', asset_id: 'ast_dan_1' },
            1,
            {
                state,
                tell: () => {},
                save: () => {},
                loadSourceImageDataUrlFn: async () => { networkCalled = true; throw Error('Must not fetch network on cache hit'); },
                generateFn: async () => { networkCalled = true; throw Error('Must not generate on cache hit'); }
            }
        );
        assert.strictEqual(mid, 'flow_media_cached_123', 'Cache hit must return existing mediaId');
        assert.strictEqual(networkCalled, false, 'No network requests on cache hit');
    }

    // =========================================================================
    // Part 2: Real processBridgeItem and processBridgeQueue End-to-End
    // =========================================================================
    console.log('Testing processBridgeItem & processBridgeQueue...');

    const state = {
        projectId: 'proj_1',
        flowProject: 'flow_main',
        refs: {},
        refAssets: {},
        done: [],
        blockedScenes: {},
        flowImageModel: '🍌 Nano Banana 2'
    };
    const savedStates = [];
    const save = () => savedStates.push(JSON.parse(JSON.stringify(state)));
    const tellLog = [];
    const tell = (msg) => tellLog.push(msg);

    const uploadedScenes = [];
    const mockUploadSaved = async () => {
        const id = state.job.id;
        state.done.push(`${state.job.kind}:${id}`);
        uploadedScenes.push(id);
    };

    const mockGenerateImage = async (job) => {
        return {
            mediaId: `media_${job.id}`,
            base64: 'fake_base64',
            mimeType: 'image/png'
        };
    };

    const mockApi = async (method, url) => {
        if (url === '/api/projects/current') {
            return { project_id: 'proj_1' };
        }
        return {};
    };

    const options = {
        project_id: 'proj_1',
        state,
        save,
        tell,
        api: mockApi,
        generate: async () => ({ mediaId: 'mock_import_mid' }),
        ensureReferenceMediaIdFn: async (ref) => `media_${ref.asset_id || ref.id}`,
        generateImageFn: mockGenerateImage,
        uploadSavedFn: mockUploadSaved,
        isPaused: () => false
    };

    // Process Scene 1 (Valid)
    const scene1 = {
        scene_id: 'sc_1',
        position: 1,
        ready_for_flow: true,
        semantic_valid: true,
        render_ready: true,
        flow_prompt: 'Cinematic shot of hero',
        references: [{ id: 'char_daniel', name: 'Дэниел', type: 'character', asset_id: 'ast_dan_1' }]
    };
    const res1 = await bridgeModule.processBridgeItem(scene1, 'scenes', options);
    assert.strictEqual(res1.status, 'succeeded');
    assert.deepStrictEqual(uploadedScenes, ['sc_1']);
    assert.deepStrictEqual(state.done, ['scenes:sc_1']);

    // Process Scene 2 (Blocked by semantic contract)
    const scene2 = {
        scene_id: 'sc_2',
        position: 2,
        ready_for_flow: false,
        semantic_valid: false,
        render_ready: true,
        flow_error: 'Кадр 2: заявлен человек без роли (visible_people: 2). Отправка запрещена.',
        flow_prompt: 'Hero with unknown stranger',
        references: []
    };
    const res2 = await bridgeModule.processBridgeItem(scene2, 'scenes', options);
    assert.strictEqual(res2.status, 'blocked');
    assert.ok(state.blockedScenes['scenes:sc_2'], 'Scene 2 must be recorded in state.blockedScenes');
    assert.strictEqual(state.blockedScenes['scenes:sc_2'].position, 2);
    assert.ok(state.blockedScenes['scenes:sc_2'].reason.includes('заявлен человек без роли'));
    assert.deepStrictEqual(uploadedScenes, ['sc_1']);

    // Process Scene 3 (Valid)
    const scene3 = {
        scene_id: 'sc_3',
        position: 3,
        ready_for_flow: true,
        semantic_valid: true,
        render_ready: true,
        flow_prompt: 'Cinematic shot of hero continuing journey',
        references: [{ id: 'char_daniel', name: 'Дэниел', type: 'character', asset_id: 'ast_dan_1' }]
    };
    const res3 = await bridgeModule.processBridgeItem(scene3, 'scenes', options);
    assert.strictEqual(res3.status, 'succeeded');
    assert.deepStrictEqual(uploadedScenes, ['sc_1', 'sc_3']);
    assert.deepStrictEqual(state.done, ['scenes:sc_1', 'scenes:sc_3']);

    // Full processBridgeQueue with mocked prompts endpoint
    const queuePrompts = {
        characters: [],
        locations: [],
        props: [],
        scenes: [
            { scene_id: 'sc_1', position: 1, ready_for_flow: true, has_image: true, references: [] },
            { scene_id: 'sc_2', position: 2, ready_for_flow: false, flow_error: 'Кадр 2: отсутствует локация', references: [] },
            { scene_id: 'sc_4', position: 4, ready_for_flow: true, has_image: false, flow_prompt: 'Scene 4 prompt', references: [] }
        ]
    };

    const queueApi = async (method, url) => {
        if (url.split('?')[0] === '/api/projects/proj_1/prompts') return queuePrompts;
        if (url === '/api/projects/current') return { project_id: 'proj_1' };
        return {};
    };

    const queueUploaded = [];
    const queueOptions = {
        project_id: 'proj_1',
        state,
        save,
        tell,
        api: queueApi,
        generate: async () => ({ mediaId: 'mock_mid' }),
        ensureReferenceMediaIdFn: async (ref) => `media_${ref.id}`,
        generateImageFn: async (job) => ({ mediaId: `gen_${job.id}`, base64: 'b64', mimeType: 'image/png' }),
        uploadSavedFn: async () => { queueUploaded.push(state.job.id); state.done.push(`scenes:${state.job.id}`); },
        isPaused: () => false
    };

    await assert.rejects(bridgeModule.processBridgeQueue(queueOptions), /кадр не пропущен/);
    assert.deepStrictEqual(queueUploaded, [], 'A blocked required frame must stop before scene 4');
    assert.ok(state.blockedScenes['scenes:sc_2'], 'Scene 2 remains in blockedScenes');
    assert.strictEqual(state.blockedScenes['scenes:sc_2'].reason, 'Кадр 2: отсутствует локация');

    // Forgotten Cities: a casting command consumes exactly its two durable
    // reference jobs, even if the payload contains other resources and scenes.
    const fcData = {
        profile_type: 'forgotten_cities',
        reference_jobs: ['cast_1', 'cast_2', 'other_cast'].map(id => ({id, prompt: id, locked: false})),
        characters: [{id: 'other_character', prompt: 'other'}],
        locations: [{id: 'other_location', prompt: 'other'}],
        props: [{id: 'other_prop', prompt: 'other'}],
        scenes: [{scene_id: 'explicit_frame', prompt: 'frame', ready_for_flow: true, position: 1}]
    };
    const fcState = {refs: {}, done: [], diagnostics: []};
    const fcGenerated = [];
    const fcOptions = {...queueOptions, state: fcState,
        generationIds: ['cast_1', 'cast_2'],
        api: async (method,url) => url === '/api/projects/current' ? {project_id:'proj_1'} : fcData,
        generateImageFn: async job => {fcGenerated.push(job.id); return {mediaId: job.id, base64: 'b64', mimeType: 'image/png'};},
        uploadSavedFn: async () => {fcState.done.push(`${fcState.job.kind}:${fcState.job.id}`); fcState.job=null;}
    };
    await bridgeModule.processBridgeQueue(fcOptions);
    assert.deepStrictEqual(fcGenerated, ['cast_1', 'cast_2']);
    await bridgeModule.processBridgeQueue(fcOptions);
    assert.deepStrictEqual(fcGenerated, ['cast_1', 'cast_2'], 'Repeated bridge run must not duplicate finished casting jobs');
    await bridgeModule.processBridgeQueue({...fcOptions, generationIds: null});
    assert.deepStrictEqual(fcGenerated, ['cast_1', 'cast_2', 'explicit_frame'], 'Separate frame run must never cast other references');

    // Semantic correction, bounded retries, reload recovery and fail-closed transport.
    const semanticError = attempts => Object.assign(Error('Смысловая проверка отклонила изображение'), {
        code: 'SOURCE_IMAGE_REJECTED', retry: {attempts, max_attempts: 3, exhausted: attempts >= 3,
        correction: 'Show deceased remains in the burial.'}
    });
    const candidate = {scene_id: 'semantic', position: 1, flow_prompt: 'Authoritative burial source.', ready_for_flow: true};
    {
        const retryState = {refs: {}, done: []};
        const prompts = [];
        let checks = 0;
        const options = {...queueOptions, state: retryState,
            generateImageFn: async job => {prompts.push(job.prompt); return {base64: 'b64', mediaId: 'new', mimeType: 'image/png'};},
            uploadSavedFn: async () => {
                if(++checks === 1) throw semanticError(1);
                retryState.done.push('scenes:semantic'); retryState.job=null; retryState.result=null;
            }};
        const result = await bridgeModule.processBridgeItem(candidate, 'scenes', options);
        assert.strictEqual(result.status, 'succeeded');
        assert.strictEqual(prompts.length, 2);
        assert.ok(prompts[1].startsWith('SOURCE FIDELITY CORRECTION'));
        assert.ok(prompts[1].includes('deceased remains'));
    }
    {
        const retryState = {refs: {}, done: []};
        const generated = [];
        let checks = 0;
        const payload = {profile_type:'forgotten_cities', scenes: [candidate, {...candidate, scene_id:'next', position:2}]};
        const options = {...queueOptions, state: retryState,
            api: async (method,url) => url === '/api/projects/current' ? {project_id:'proj_1'} : payload,
            generateImageFn: async job => {generated.push(job.id); return {base64:'b64', mediaId:'new', mimeType:'image/png'};},
            uploadSavedFn: async () => {
                if(retryState.job.id === 'semantic') throw semanticError(++checks);
                retryState.done.push('scenes:next'); retryState.job=null; retryState.result=null;
            }};
        await assert.rejects(bridgeModule.processBridgeQueue(options), /кадр не пропущен/);
        assert.deepStrictEqual(generated, ['semantic','semantic','semantic']);
        assert.strictEqual(retryState.blockedScenes['scenes:semantic'].attempts, 3);
        const before = generated.length;
        await bridgeModule.processBridgeItem({...candidate,source_fidelity_retry:{exhausted:true, attempts:3}}, 'scenes', options);
        assert.strictEqual(generated.length, before, 'Reload must not reset the backend budget');
    }
    {
        let persisted;
        const retryState = {refs:{},done:[], job:{kind:'scenes', id:'semantic', position:1, prompt:'BASE', retries:0},
            result:{base64:'old'}};
        bridgeModule.handleSourceRejection(semanticError(1), retryState,
            () => {persisted=JSON.parse(JSON.stringify(retryState));}, () => {}, 1);
        assert.strictEqual(persisted.result, null);
        const corrected = persisted.job.prompt;
        let calls = 0;
        await bridgeModule.processBridgeItem(candidate,'scenes',{...queueOptions,state:persisted,
            generateImageFn:async job=>{calls++;assert.strictEqual(job.prompt, corrected);return {base64:'new'};},
            uploadSavedFn:async()=>{persisted.job=null;persisted.result=null;}});
        assert.strictEqual(calls,1,'Resume generates exactly one corrected candidate');
    }
    {
        const retryState={refs:{},done:[]};
        let calls=0;
        await assert.rejects(bridgeModule.processBridgeItem(candidate,'scenes',{...queueOptions,state:retryState,
            generateImageFn:async()=>{calls++;return {base64:'keep-this-candidate'};},
            uploadSavedFn:async()=>{throw Error('Лимит Gemini');}}), /Лимит Gemini/);
        assert.strictEqual(calls,1);
        assert.strictEqual(retryState.result.base64,'keep-this-candidate');
        assert.strictEqual(bridgeModule.handleSourceRejection(semanticError(1),
            {job:{kind:'reference_jobs',id:'casting'}},()=>{},()=>{},1),false);
    }

    // Execute real run/finally/heartbeat lifecycle, beyond the queue helper.
    {
        const lifecycle = createUserscriptContext({lifecycle:true});
        const generated=[],heartbeats=[];
        let pendingCommand=null;
        const data={profile_type:'forgotten_cities', reference_jobs:['a','b','c','d'].map(id=>({id,prompt:id,locked:false})), scenes:[]};
        lifecycle.configureTest(async(method,url,body)=>{
            if(url==='/api/projects/current')return {project_id:'proj_1'};
            if(url==='/api/settings')return {};
            if(url.split('?')[0]==='/api/projects/proj_1/prompts')return data;
            if(url==='/api/bridge/v1/heartbeat'){
                heartbeats.push(body);
                if(pendingCommand&&body.ack_command_id===pendingCommand.id)pendingCommand=null;
                return {command:pendingCommand};
            }
            throw Error('Unexpected lifecycle API: '+url);
        }, async job=>{generated.push(job.id);return {mediaId:job.id,base64:'b64',mimeType:'image/png'};},
        async()=>{
            const state=lifecycle.inspectTest().state;
            if(!state?.result)return;
            state.done.push(`${state.job.kind}:${state.job.id}`);
            state.job=null;state.result=null;
        });
        await lifecycle.run(null,['a','b']);
        await lifecycle.heartbeat();
        assert.deepStrictEqual(generated,['a','b']);
        assert.strictEqual(lifecycle.inspectTest().running,false);
        assert.ok(lifecycle.inspectTest().peer,'Adapter survives pair completion');
        assert.ok(lifecycle.inspectTest().status.includes('ждёт следующую команду'));
        assert.ok(heartbeats.some(h=>h.adapter_connected&&!h.running));
        lifecycle.setRunningTest(true);
        pendingCommand={id:'next-pair',action:'start',project_id:'proj_1',generation_ids:['c','d']};
        await lifecycle.heartbeat();
        assert.notStrictEqual(lifecycle.inspectTest().processedCommandId,'next-pair','Busy bridge must not swallow next pair');
        lifecycle.setRunningTest(false);
        await lifecycle.heartbeat();
        for(let i=0;i<200&&lifecycle.inspectTest().running;i++)await Promise.resolve();
        assert.deepStrictEqual(generated,['a','b','c','d'],'Next pair starts through heartbeat without manual activation or scene generation');
        assert.strictEqual(lifecycle.inspectTest().running,false);
    }

    // A terminal provider failure must close its reservation before retrying,
    // and exhaustion must stop before the next required frame.
    for (const releaseFails of [false,true]) {
      const terminalState={projectId:'proj_1',flowProject:'flow_main',done:[],refs:{},refAssets:{},flowImageModel:'🍌 Nano Banana 2'};
      const events=[];
      const scenes=[21,22].map(position=>({scene_id:'s'+position,position,ready_for_flow:true,has_image:false,flow_prompt:'prompt',references:[],google_queue_enabled:true,current_revision:2}));
      const api=async(method,url,data)=>{
        if(url.split('?')[0]==='/api/projects/proj_1/prompts')return {characters:[],locations:[],props:[],scenes};
        if(url==='/api/projects/current')return {project_id:'proj_1'};
        if(url==='/api/bridge/v1/admission'){events.push('claim');return {allowed:true};}
        if(url==='/api/bridge/v1/admission/failure'){
          assert.strictEqual(data.code,'ERR_GEN_FAILED');events.push('close');
          if(releaseFails)throw Error('release unavailable');return {ok:true};
        }
        throw Error('Unexpected API '+url);
      };
      await assert.rejects(bridgeModule.processBridgeQueue({project_id:'proj_1',state:terminalState,save:()=>{},tell:()=>{},api,sleepFn:async()=>{},
        generateImageFn:async job=>{assert.strictEqual(job.position,21);events.push('generate');const error=Error('ERR_GEN_FAILED');error.flow={code:'ERR_GEN_FAILED'};throw error;},
        uploadSavedFn:async()=>{throw Error('Must not upload a failed attempt');}}),releaseFails?/release unavailable/:/кадр.*не пропущен/);
      assert.deepStrictEqual(events,releaseFails?['claim','generate','close']:['claim','generate','close','claim','generate','close']);
      assert.strictEqual(terminalState.done.length,0);
      assert.ok(terminalState.result == null);
    }
    console.log('All JavaScript Bridge tests passed successfully (100% OK).');
}

runTests().catch(err => {
    console.error('JS Test failed:', err);
    process.exit(1);
});
