"""Stable fictional visual choices, subordinate to supplied character facts."""
import hashlib
import re

HAIR = (
 'short tight curls', 'short swept-back hair', 'short side-parted hair',
 'a tousled angular fringe', 'a close cropped haircut', 'a soft wavy fringe',
 'short spiky hair', 'neatly combed flat hair', 'a rounded curly crop',
 'a long side-swept fringe', 'short center-parted hair', 'a short wavy quiff')
FEMALE_HAIR = (
 'a short straight bob', 'a low bun', 'a shoulder-length wavy cut',
 'a single loose braid', 'a short curly crop', 'a straight chin-length bob',
 'a high bun', 'long straight hair tucked behind the ears', 'a short pixie cut',
 'a low ponytail', 'a wavy side-parted bob', 'a short asymmetrical fringe')
COLORS = ('dark brown','chestnut brown','black','dark blond','auburn','sandy brown')
BROWS = ('short straight eyebrows','gently arched eyebrows','thick level eyebrows',
         'thin angled eyebrows','short curved eyebrows','wide softly sloping eyebrows')
PALETTE = ('muted slate blue','dark olive','warm charcoal','dusty burgundy',
           'muted ochre','deep brown','soft sage green','dark teal')
CLOTHES = ('a narrow pointed collar','a small rounded collar','a plain collarless neckline',
           'a high simple collar','a small open collar','a narrow V neckline')

def appearance_design(character, gender=None):
    identity=str(character.get('description') or '')
    key=str(character.get('id') or character.get('name') or identity)
    digest=hashlib.sha256(('cast-design/1:'+key).encode()).digest()
    # Facts win per feature: do not overwrite an explicitly described haircut,
    # facial detail or costume, and never invent eyewear, facial hair or age.
    parts=[]
    if not re.search(r'волос|прич[её]ск|лыс|кос[аыу]|пучок|hair|bald|braid|ponytail',identity,re.I):
        styles=FEMALE_HAIR if (gender or {}).get('value')=='female' else HAIR
        parts.append(COLORS[digest[0]%len(COLORS)]+' '+styles[digest[1]%len(styles)])
    if not re.search(r'бров|eyebrow',identity,re.I):
        parts.append(BROWS[digest[2]%len(BROWS)])
    if not re.search(r'одежд|жилет|пальто|халат|плать|рубаш|костюм|форм[аеыу]|coat|shirt|vest|dress|uniform',identity,re.I):
        parts.append(PALETTE[digest[3]%len(PALETTE)]+' clothing with '+CLOTHES[digest[4]%len(CLOTHES)])
    prompt=('Individual visual design: '+ '; '.join(parts)+'. ' if parts else '')
    prompt+=('These are fictional appearance choices only where identity data leaves the feature unspecified. '
             'Keep the stated age, gender and period. Preserve a distinct hair silhouette and clothing palette; '
             'do not use a generic identical doctor design or add a white coat merely because the role is doctor.')
    return {'version':'cast-design/1','signature':digest.hex()[:16],'features':parts,'prompt':prompt}
