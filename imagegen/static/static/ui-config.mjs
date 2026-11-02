export const STYLE_PRESETS = [
  {
    id: 'realism', name: 'Реализм', subtitle: 'Cinema & Photo', image: '/presets/realism.png',
    prompt: 'Cinematic photorealism, realistic human faces and anatomy, natural skin texture, physically accurate materials, soft natural lighting, realistic shadows, cinematic composition, subtle color grading, highly detailed live-action film still'
  },
  {
    id: 'medieval', name: 'Средневековая киноживопись', subtitle: 'Medieval Epic', image: '/presets/medieval.png',
    prompt: 'Epic medieval historical cinematic digital painting, richly detailed painterly realism with visible fine brush texture, dramatic storytelling composition and layered foreground, midground and background. Warm amber firelight and glowing embers contrasted with cool blue-gray daylight, atmospheric smoke and mist, weathered stone, rough timber, forged iron, worn leather and natural linen, tactile historically grounded materials, expressive natural anatomy, restrained earthy colors with crimson accents, deep chiaroscuro, sweeping castle landscapes where appropriate. Preserve the actual subjects, actions and setting of the scene; do not add castles, soldiers, weapons or fire unless the story calls for them. No modern objects, no text or watermarks.'
  },
  {
    id: 'comic', name: 'Комикс', subtitle: 'Graphic novel', image: '/presets/comic.png',
    prompt: 'Cinematic graphic novel comic art, expressive ink linework, controlled cross-hatching, dramatic panel composition, rich printed colors, paper grain, detailed environments, consistent character design, no photorealism, absolutely no speech bubbles, no dialogue balloons, no captions, no lettering, no readable words or text'
  },
  {
    id: 'noir', name: 'Нуар', subtitle: 'Black & White', image: '/presets/noir.png',
    prompt: 'Classic film noir, monochrome black and white photography, high contrast chiaroscuro, deep shadows, hard rim light, wet streets, atmospheric smoke, 1940s cinematic framing, realistic anatomy, fine film grain'
  },
  {
    id: 'cartoon', name: 'Мультфильм', subtitle: 'Animated', image: '/presets/cartoon.png',
    prompt: 'Cinematic animated film illustration, clean stylized shapes, expressive faces, appealing simplified anatomy, hand-painted backgrounds, warm dimensional lighting, vibrant controlled colors, consistent animation character design, no comic panels, no ink hatching'
  },
  {
    id: 'horror', name: 'Мистическая иллюстрация', subtitle: 'Modern Mystery Illustration', image: '/presets/horror.png',
    prompt: 'Modern mystery illustration, atmospheric digital concept art painting, evocative dark painterly style, textured visible brushwork, matte gouache and oil painting finish, moody color palette of muted deep teal, slate blue, charcoal, dark pine and obsidian, dramatic chiaroscuro contrast, mysterious volumetric fog and ambient haze, glowing warm accent lights from lanterns, windows or flashlights, cinematic storybook noir atmosphere, subtle paper and canvas texture, expressive stylized character design, masterwork digital art, no photorealism, no 3d render, no celebrities'
  }
];

export const MODES = [
  {id:'characters', icon:'♙', name:'Персонажи', short:'Люди, животные и существа', description:'ИИ сам определяет людей, животных и существ по сценарию, создаёт подходящие референсы и не смешивает их с людьми в кадре.'},
  {id:'dynamic', icon:'ϟ', name:'Динамика', short:'Свободные локации', description:'Полные референсы персонажей сохраняются, а окружение свободно меняется вслед за сюжетом.'},
  {id:'illustration', icon:'✦', name:'Иллюстрация', short:'Без референсов', description:'Каждый фрагмент текста становится самостоятельной иллюстрацией. Подходит для истории и образования.'},
  {id:'objects', icon:'◇', name:'Объекты', short:'Места и транспорт', description:'ИИ создаёт референсы достопримечательностей, транспорта и архитектуры с кинематографическими ракурсами.'}
];

export function titleFromStory(story) {
  const clean = String(story || '').replace(/\s+/g, ' ').trim();
  if (!clean) return 'Новый проект';
  const sentence = clean.split(/[.!?…]/, 1)[0].trim();
  return (sentence || clean).slice(0, 72);
}

export const MAX_SCENES = 2000;
// The automatic animation pipeline defaults to Omni 1.1 Flash at six seconds.
// Keep storyboard sizing tied to that delivered duration, not to a legacy 5s Veo assumption.
export const SECONDS_PER_FRAME = 6;
export const TARGET_SENTENCES_PER_FRAME = 1.3;
export const WORDS_PER_SECOND = 2.4;

export function countStoryWords(story) {
  return (String(story || '').trim().match(/\S+/g) || []).length;
}

export function readingDurationSeconds(story) {
  const words = countStoryWords(story);
  return words > 0 ? (words / WORDS_PER_SECOND) : 0;
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
  const duration = words > 0 ? (words / WORDS_PER_SECOND) : (sentences * 3.8);
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
  const duration = words > 0 ? (words / WORDS_PER_SECOND) : (sentences ? sentences * 3.8 : 0);
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
    sentencesPerFrame: sourceSentences ? sourceSentences / count : 0,
    dynamicSentencesPerFrame: sentencesPerFrame,
    fixedTiming
  };
}

export function presetForStyle(style) {
  return STYLE_PRESETS.find(item => item.prompt === style) || STYLE_PRESETS[0];
}
