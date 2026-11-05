"""Rendering contract for the channel's explicitly selected stick-figure preset."""
MARKER_PREFIX = 'Minimalist Marker Explainer'
MARKER_GRAPHICS = (
    '2D marker line illustration, thick black outlines in ink with gentle hand-drawn wobble, rounded ends and varied pressure, '
    'flat muted earth colors: warm beige, charcoal, gray and subdued brown. '
    'Restrained soft shading in the environment only; readable illustrated rooms and props with simple perspective. '
    'No photographic rendering, 3D, glossy surfaces, comic-book anatomy or painterly brushwork.')
MARKER_FIGURES = (
    'Only the declared visible people are STICK FIGURES: oversized white round heads with a clean circular black contour, '
    'large round heads and simplified bodies; flexible proportions follow the pose and shot. Bare distant limbs may be thin strokes, while sleeves and trousers have simple flat volume. Close-up arms and hands may have outlined volume and a few finger lines when the action needs them. '
    'The head is a white circle, not a white-painted anatomically shaped human face. '
    'Avoid photographic skin and detailed muscle modeling; preserve source-required body shape, visible illness and hand actions. '
    'SHOT-SCALE RULE: in close-ups and medium shots, show the reference identity using flat-color hairstyle, moustache, glasses, '
    'simple oval eyes, expressive eyebrows and mouth; small nose, ears, age lines or illness markings may appear where identity or the scene requires them. Keep the simplified drawn proportions. '
    'In wide, long and establishing shots, use mainly plain white circular heads with minimal detail; retain visible illness cues needed for the story even in a wide view; '
    'preserve identity through clothing, silhouette, position and action. '
    'Do not paste detailed reference faces onto small distant heads. Reference images define identity, not realistic anatomy or rendering style. '
    'Never invent extra figures.')
MARKER_WIRE = (
    '2D marker illustration, black hand-drawn outlines, muted beige/gray/brown setting. '
    'Declared people only: STICK FIGURES with large white round heads and simple clothed bodies. Thin bare limbs at distance; sleeves/trousers may have volume, close-up hands may have fingers. Bold gently wobbly ink; no realistic anatomy or glossy vector finish. '
    'Close/medium shots: simple reference features, expressive face and source-required age/illness marks. '
    'Wide/long/establishing shots: mostly plain white heads; identity by clothing/action, but retain story-required visible symptoms. '
    'References supply identity, not rendering style. No photo/3D.')

MEDICAL_STORY = 'Medical story visibility: when the CURRENT scene source explicitly describes illness, pain or a visible physical sign, make that fact readable through the specified body region, pose, expression or appearance. Preserve explicitly stated swelling, asymmetry, pallor, wasting, skin changes or restricted movement; the simplified style must not erase them. Use only signs and symptom actions supported by the current source, never infer new signs from a diagnosis alone, invent injury/blood, or carry a previous illness into a recovered scene. For internal or non-visible illness use the source-described behavior/expression; do not invent a cutaway, medical diagram or new anatomical finding.'
MARKER_WIRE += " " + MEDICAL_STORY

def is_marker_style(style):
    return str(style or '').strip().casefold().startswith(MARKER_PREFIX.casefold())

def marker_contract(people=True):
    return MARKER_GRAPHICS + (' ' + MARKER_FIGURES + ' ' + MEDICAL_STORY if people else ' Draw only the required setting and objects; no people or body parts.')


GRITTY_PREFIX = "Gritty Graphic Novel"
GRITTY_INK_STYLE = 'Gritty Graphic Novel: detailed 2D ink illustration, clean variable-weight dark contours, fine facial and architectural lines, hatching for fur/chainmail/stone; muted ochre, brown, olive, slate and dusty blue with sparse deep red. Flat local colors with subtle smooth volume gradients, soft form shadows and hard cast shadows; restrained matte highlights, paper grain, atmospheric depth.'
GRITTY_FLOW_STYLE = GRITTY_INK_STYLE + ' Realistic 1:6-1:7 adult proportions, distinct expressive faces, natural hands and fabric drapery; preserve required age/body differences. Keep each named hero recognizable from their established screen identity or supplied reference, including individual facial structure, hairline, age and scars. Depict only story-required cast, action and setting. No photography, 3D, painterly brushwork, cel shading, glossy highlights, captions or speech balloons.'

def is_gritty_style(style):
    return str(style or "").strip().casefold().startswith(GRITTY_PREFIX.casefold())
