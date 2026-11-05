export const STYLE_PRESETS = [
  {
    id: 'realism', name: 'Реализм', subtitle: 'Cinema & Photo', image: '/presets/realism.png',
    description: 'Кинематографичный фотореализм, естественная анатомия, мягкий свет и аккуратная цветокоррекция.',
    prompt: 'Cinematic photorealism, realistic human faces and anatomy, natural skin texture, physically accurate materials, soft natural lighting, realistic shadows, cinematic composition, subtle color grading, highly detailed live-action film still'
  },
  {
    id: 'medieval', name: 'Средневековая киноживопись', subtitle: 'Medieval Epic', image: '/presets/medieval.png',
    description: 'Эпическая историческая живопись с фактурными мазками, глубокой светотенью и тёплыми искрами огня.',
    prompt: 'Epic medieval historical cinematic digital painting, richly detailed painterly realism with visible fine brush texture, dramatic storytelling composition and layered foreground, midground and background. Warm amber firelight and glowing embers contrasted with cool blue-gray daylight, atmospheric smoke and mist, weathered stone, rough timber, forged iron, worn leather and natural linen, tactile historically grounded materials, expressive natural anatomy, restrained earthy colors with crimson accents, deep chiaroscuro, sweeping castle landscapes where appropriate. Preserve the actual subjects, actions and setting of the scene; do not add castles, soldiers, weapons or fire unless the story calls for them. No modern objects, no text or watermarks.'
  },
  {
    "id": "gritty-graphic-novel",
    "name": "Мрачный графический роман",
    "subtitle": "Gritty Graphic Novel",
    "image": "/presets/gritty-graphic-novel.png",
    "description": "Детальная фэнтези-иллюстрация для «Игры престолов»: тонкая тушевая штриховка, реалистичные лица и пропорции, приглушённые охристые и серо-синие тона.",
    "prompt": "Gritty Graphic Novel, detailed 2D fantasy illustration with precise variable-weight digital inking: medium-to-thick dark black or muted-brown contours on main figures and foreground forms; thin fine lines for facial features, hair, clothing folds, armor and architecture. Clean confident continuous hand-drawn edges, detailed hatching and cross-hatching for fur, chainmail, stubble and stone. Consistently muted desaturated deep browns, warm ochre, drab olive, cool greys and dusty blues, with occasional deep-red accents. Flat local color fills with subtle smooth digital gradients for volume and atmospheric depth; soft form shadows, hard cast shadows and restrained desaturated highlights. Realistic adult proportions around 1:6 to 1:7 head-to-body, distinct faces with credible bone structure, expressive eyes, natural hands, individual hair and clothing with natural drapery; preserve source-required age, body type and differences. Highly detailed environments with atmospheric perspective and subtle paper grain. Match the story setting, cast, costume, action and lighting; do not invent extra people, weapons, fire or heraldry. Keep named fictional heroes recognizable: preserve their established screen identity or supplied reference face, hairline, age, scars and individual silhouette; never homogenize or randomly redesign the cast. Isolated identity references show only the described subject on a plain background; environments belong to scenes. NOT photorealistic, NOT 3D, NOT cel-shaded, NOT painterly, NO strong specular highlights, no speech balloons, captions or readable lettering."
},
  {
    id: 'comic', name: 'Комикс', subtitle: 'Graphic novel', image: '/presets/comic.png',
    description: 'Выразительный графический роман: чёткая тушевая штриховка, печатная текстура бумаги и сочные цвета.',
    prompt: 'Cinematic graphic novel comic art, expressive ink linework, controlled cross-hatching, dramatic panel composition, rich printed colors, paper grain, detailed environments, consistent character design, no photorealism, absolutely no speech bubbles, no dialogue balloons, no captions, no lettering, no readable words or text'
  },
  {
    id: 'noir', name: 'Нуар', subtitle: 'Black & White', image: '/presets/noir.png',
    description: 'Классический чёрно-белый нуар: контрастная светотень, резкие блики на мокром асфальте и дымка 1940-х.',
    prompt: 'Classic film noir, monochrome black and white photography, high contrast chiaroscuro, deep shadows, hard rim light, wet streets, atmospheric smoke, 1940s cinematic framing, realistic anatomy, fine film grain'
  },
  {
    id: 'cartoon', name: 'Мультфильм', subtitle: 'Animated', image: '/presets/cartoon.png',
    description: 'Анимационная кинематографичная иллюстрация с чистыми формами, выразительной мимикой и мягким светом.',
    prompt: 'Cinematic animated film illustration, clean stylized shapes, expressive faces, appealing simplified anatomy, hand-painted backgrounds, warm dimensional lighting, vibrant controlled colors, consistent animation character design, no comic panels, no ink hatching'
  },
  {
    id: 'horror', name: 'Мистическая иллюстрация', subtitle: 'Modern Mystery Illustration', image: '/presets/horror.png',
    description: 'Атмосферный тёмный концепт-арт с живописными текстурами, туманом и тёплыми акцентами фонарей.',
    prompt: 'Modern mystery illustration, atmospheric digital concept art painting, evocative dark painterly style, textured visible brushwork, matte gouache and oil painting finish, moody color palette of muted deep teal, slate blue, charcoal, dark pine and obsidian, dramatic chiaroscuro contrast, mysterious volumetric fog and ambient haze, glowing warm accent lights from lanterns, windows or flashlights, cinematic storybook noir atmosphere, subtle paper and canvas texture, expressive stylized character design, masterwork digital art, no photorealism, no 3d render, no celebrities'
  },
  {
    id: 'marker-explainer', name: 'Маркерный эксплейнер', subtitle: 'Minimalist Marker Explainer', image: '/presets/marker-explainer.svg',
    description: 'Минималистичные рисованные персонажи с крупными белыми головами, живой линией маркера и плоскими тонами.',
    prompt: "Minimalist Marker Explainer, 2D hand-drawn educational illustration, confident thick black marker outlines with a slight natural wobble, oversized pure white round heads, small black dot or oval eyes, simple expressive eyebrows and mouths, thin stick-like arms and legs, distinctive simple hair, glasses and moustaches only where the character description requires them, flat muted ochre, beige, brown, sage and dusty blue colors, minimal cel shading, readable gestures and sparse uncluttered composition. Isolated character portraits use a clean white background. Scene environments use the same marker linework and flat colors; retain story-required architecture, furniture, props and historical period. Preserve the actual cast and actions; do not invent extra people or explanatory symbols. No photographic skin, no realistic facial anatomy, no 3D, no painterly brushwork, no captions or speech bubbles."
  },

];

export const MODES = [
  {id:'characters', icon:'♙', name:'Персонажи', short:'', description:'Постоянные персонажи с закреплённым внешним видом и связанные с ними сцены.'},
  {id:'dynamic', icon:'♧', name:'Динамика', short:'', description:'Персонажи сохраняют внешность, а места действия свободно меняются по ходу истории. Локации не требуют обязательных эталонов.'},
  {id:'objects', icon:'♜', name:'Объекты', short:'', description:'Места, архитектура, предметы и транспорт как основные объекты истории.'},
  {id:'illustration', icon:'♢', name:'Иллюстрация', short:'', description:'Самостоятельная иллюстрация каждого фрагмента без обязательных портретных эталонов.'}
];

export function titleFromStory(story) {
  const clean = String(story || '').replace(/\s+/g, ' ').trim();
  if (!clean) return 'Новый проект';
  const sentence = clean.split(/[.!?…]/, 1)[0].trim();
  return (sentence || clean).slice(0, 72);
}

export const MAX_SCENES = 2000;
// Storyboard image cadence is independent of generated animation clip length.
// Calibrated against the supplied Insaga example, not its private implementation.
export const SECONDS_PER_FRAME = 4.5;
export const TARGET_SENTENCES_PER_FRAME = 0.8;
export const READING_CHARACTERS_PER_SECOND = 15;

export function countStoryWords(story) {
  return (String(story || '').trim().match(/\S+/g) || []).length;
}

export function readingDurationSeconds(story) {
  const text = String(story || '').replace(/\s+/gu, ' ').trim();
  return text ? Math.max(1, Math.floor(text.length / READING_CHARACTERS_PER_SECOND)) : 0;
}

export function countStorySentences(story) {
  return (String(story || '').match(/[^.!?…]+(?:[.!?…]+|$)/g) || [])
    .map(sentence => sentence.trim())
    .filter(Boolean).length;
}

export function clampSceneCount(value) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? Math.min(MAX_SCENES, Math.max(1, parsed)) : 12;
}

export function recommendedSceneCount(story, fixedTiming = true) {
  const words = countStoryWords(story);
  const sentences = countStorySentences(story);
  const duration = readingDurationSeconds(story);
  // Ceiling preserves the requested narration duration: rounding down would
  // silently cut off the final seconds of a complete scenario.
  if (fixedTiming) return duration > 0 ? clampSceneCount(Math.ceil(duration / SECONDS_PER_FRAME)) : 12;
  // Legacy scene-density mode remains available to callers that explicitly
  // request it, but it is no longer the UI default.
  return sentences ? clampSceneCount(Math.min(sentences, 24)) : 12;
}

export function storyboardTiming(story, frames, fixedTiming = true) {
  const sentences = countStorySentences(story);
  const words = countStoryWords(story);
  const duration = readingDurationSeconds(story);
  const count = clampSceneCount(frames);
  const sourceSentences = Math.min(sentences, count);
  const secPerFrame = fixedTiming ? SECONDS_PER_FRAME : (count > 0 && duration > 0 ? (duration / count) : SECONDS_PER_FRAME);
  const sentencesPerFrame = count > 0 && sentences > 0 ? (sentences / count) : TARGET_SENTENCES_PER_FRAME;
  return {
    words,
    duration,
    sentences,
    sourceSentences,
    remainingSentences: Math.max(0, sentences - sourceSentences),
    frames: count,
    seconds: count * SECONDS_PER_FRAME,
    calculatedDuration: duration,
    secPerFrame,
    sentencesPerFrame: count > 0 ? sentences / count : 0,
    dynamicSentencesPerFrame: sentencesPerFrame,
    fixedTiming
  };
}

export function presetForStyle(style) {
  return STYLE_PRESETS.find(item => item.prompt === style || item.id === style) || STYLE_PRESETS[0];
}
