"""Cast Bible and deterministic character entity resolution."""
import copy
import hashlib
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

CAST_VERSION = 2


def strip_combining_accents(text: str) -> str:
    """Normalize text and strip combining diacritical marks (e.g. Russian stress accents)
    while preserving standard Russian letters like 'й' (breve U+0306) and 'ё' (diaeresis U+0308)."""
    if not text:
        return ''
    normalized = unicodedata.normalize('NFD', str(text))
    filtered = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn' or c in ('\u0306', '\u0308'))
    return unicodedata.normalize('NFC', filtered)


# Russian noun case endings
_RU_NAME_ENDINGS = (
    'ями', 'ами', 'ях', 'ах', 'ов', 'ев', 'ей', 'ой', 'ем', 'ом',
    'ью', 'ии', 'ию', 'ия', 'ее', 'ую', 'яя', 'ий', 'ый', 'ей',
    'е', 'у', 'ю', 'а', 'я', 'и', 'ы', 'о'
)


def stem_ru_word(word: str) -> str:
    """Stem Russian words by removing common inflectional suffixes."""
    w = word.lower()
    for end in _RU_NAME_ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[:-len(end)]
    return w


_AGENT_NOUN_WORDS = {
    # Russian agent nouns (nominative base forms)
    'солдат', 'парень', 'человек', 'юноша', 'старик', 'старуха', 'девушка', 'женщина', 'мужчина', 'мальчик', 'девочка', 'ребёнок',
    'воин', 'стражник', 'страж', 'караульный', 'дозорный', 'часовой', 'гонец', 'вестник', 'курьер', 'посыльный', 'рыбак', 'пастух', 'лекарь', 'врач', 'купец', 'торговец', 'продавец',
    'кузнец', 'плотник', 'кожевник', 'гончар', 'пекарь', 'сапожник', 'ткач', 'мельник', 'аптекарь', 'штурман', 'исследователь',
    'охотник', 'трактирщик', 'странник', 'путник', 'проводник', 'всадник', 'лодочник', 'паромщик', 'гид', 'астроном', 'сварщик', 'инженер', 'техник', 'оператор', 'радиооператор', 'радист',
    'монах', 'рыцарь', 'оруженосец', 'ученик', 'подмастерье', 'мастер', 'смотритель', 'сторож', 'слуга', 'гость', 'батрак', 'воевода', 'староста',
    'архивист', 'архивистка', 'кассир', 'кассирша', 'билетёр', 'билетёрша', 'бармен', 'барменша',
    'лесник', 'лесничий', 'охранник', 'охранница', 'сотрудник', 'сотрудница', 'администратор', 'администраторша',
    'капитан', 'инспектор', 'детектив', 'доктор', 'офицер', 'пилот', 'матрос', 'моряк', 'генерал', 'сержант', 'майор', 'полковник',
    'контроллер', 'диспетчер', 'механик', 'король', 'царь', 'принц', 'герцог', 'граф', 'барон',
    # English agent nouns
    'man', 'woman', 'boy', 'girl', 'soldier', 'guard', 'sentry', 'watchman', 'messenger', 'herald', 'fisherman', 'shepherd', 'healer',
    'merchant', 'trader', 'shopkeeper', 'blacksmith', 'smith', 'carpenter', 'leatherworker', 'potter', 'baker', 'shoemaker', 'weaver', 'miller', 'apothecary',
    'navigator', 'researcher', 'hunter', 'innkeeper', 'traveler', 'guide', 'rider', 'boatman', 'astronomer', 'welder', 'engineer', 'technician', 'operator', 'radio operator', 'controller', 'dispatcher', 'mechanic',
    'monk', 'knight', 'squire', 'apprentice', 'master', 'keeper', 'servant', 'guest', 'captain', 'inspector', 'detective', 'doctor', 'physician',
    'archivist', 'cashier', 'ticket clerk', 'bartender', 'forester', 'ranger', 'employee', 'staff member', 'administrator',
    'officer', 'pilot', 'sailor', 'general', 'sergeant', 'major', 'colonel', 'king', 'prince', 'duke', 'count', 'lord'
}

_AGENT_NOUN_STEMS = {stem_ru_word(w) for w in _AGENT_NOUN_WORDS}

_ONE_OFF_OCCUPATIONAL_ROLES = {
    'архивист', 'архивистка', 'кассир', 'кассирша', 'билетёр', 'билетёрша',
    'бармен', 'барменша', 'лесник', 'лесничий', 'охранник', 'охранница',
    'сотрудник', 'сотрудница', 'администратор', 'администраторша',
    'archivist', 'cashier', 'ticket clerk', 'bartender', 'forester', 'ranger',
    'employee', 'staff member', 'administrator', 'guard',
}

_COMMON_ROLES = {
    'captain': {'капитан', 'captain', 'кэп', 'командир'},
    'keeper': {'смотритель', 'keeper', 'маячник', 'смотритель маяка'},
    'doctor': {'доктор', 'врач', 'doctor', 'physician'},
    'police': {'полицейский', 'детектив', 'шериф', 'инспектор', 'police', 'officer', 'detective', 'sheriff', 'inspector'},
    'daughter': {'дочь', 'дочка', 'дочер', 'daughter'},
    'son': {'сын', 'сынок', 'сыном', 'сынов', 'son'},
    'mother': {'мать', 'мама', 'матер', 'mother', 'mom'},
    'father': {'отец', 'папа', 'father', 'dad'},
    'husband': {'муж', 'супруг', 'мужем', 'husband', 'spouse'},
    'wife': {'жена', 'супруга', 'женой', 'wife'},
    'pilot': {'пилот', 'летчик', 'лётчик', 'pilot'},
    'nurse': {'медсестра', 'nurse'},
    'mechanic': {'механик', 'слесарь', 'mechanic'},
    'operator': {'оператор', 'диспетчер', 'operator', 'dispatcher', 'controller'},
}

# Transliteration EN <-> RU for common character names
_TRANSLIT_PAIRS = {
    'anna': 'анна', 'thomas': 'томас', 'reed': 'рид', 'alexei': 'алексей',
    'marina': 'марина', 'andrei': 'андрей', 'ada': 'ада', 'john': 'джон',
    'tom': 'том', 'peter': 'петр', 'vance': 'вэнс', 'captain': 'капитан',
    'doctor': 'доктор', 'daughter': 'дочь', 'son': 'сын', 'husband': 'муж',
    'wife': 'жена', 'mother': 'мать', 'father': 'отец', 'pilot': 'пилот',
    'octopus': 'осьминог', 'pangolin': 'панголин', 'saola': 'саола',
    'crow': 'ворона', 'raven': 'ворон', 'wolf': 'волк', 'bear': 'медведь',
    'fox': 'лиса', 'cat': 'кот', 'dog': 'собака', 'dragon': 'дракон'
}

_DIMINUTIVE_NAME_PAIRS = {
    ('дэниел', 'дэн'), ('daniel', 'dan'), ('даниил', 'даня'),
    ('александр', 'саша'), ('alexander', 'alex'), ('михаил', 'миша'),
    ('михаил', 'майк'), ('michael', 'mike'), ('дмитрий', 'дима'),
    ('елена', 'лена'), ('екатерина', 'катя'), ('николай', 'коля'),
    ('сергей', 'серёжа'), ('сергей', 'сережа'), ('владимир', 'володя'),
    ('владимир', 'вова'), ('иван', 'ваня'), ('алексей', 'лёша'),
    ('алексей', 'леша'), ('павел', 'паша'), ('томас', 'том'),
    ('thomas', 'tom'), ('роберт', 'боб'), ('роберт', 'роб'),
    ('robert', 'bob'), ('robert', 'rob'), ('уильям', 'билл'),
    ('уильям', 'уилл'), ('william', 'bill'), ('william', 'will'),
    ('ричард', 'дик'), ('richard', 'dick'), ('джеймс', 'джим'),
    ('james', 'jim'), ('эдвард', 'эд'), ('edward', 'ed')
}

STOP_ENTITY_WORDS = {
    'Event', 'Scene', 'Chapter', 'Part', 'Day', 'Night', 'Morning', 'Evening', 'Time', 'Step', 'Beat',
    'Frame', 'Image', 'Shot', 'Note', 'Plot', 'Story', 'Episode', 'Act', 'Section', 'Page', 'Item',
    'Figure', 'Figures', 'Silhouette', 'Silhouettes', 'Table', 'The', 'A', 'An', 'In', 'On', 'At', 'To',
    'He', 'She', 'They', 'It', 'Then', 'When', 'After', 'Before', 'All', 'Some', 'Many', 'Few', 'Both',
    'Each', 'Every', 'Other', 'No', 'Yes', 'Here', 'There', 'Who', 'Whom', 'Whose', 'What', 'Where', 'Why', 'How',
    'Pause', 'Silence', 'Voice', 'Laughter', 'Sigh', 'Shout', 'Noise', 'Sound', 'Steps',
    'Tree', 'Trees', 'Bush', 'Bushes', 'Grass', 'Leaves', 'Branch', 'Branches', 'Stone', 'Stones', 'Rock', 'Rocks',
    'Sand', 'Earth', 'Snow', 'Ice', 'Rain', 'Wind', 'Fog', 'Clouds', 'Sky', 'Light', 'Darkness', 'Shadow', 'Shadows',
    'Majority', 'Minority', 'Searchers', 'Rescuers', 'First', 'Second', 'Third', 'Fourth', 'Fifth',
    'Сцена', 'Глава', 'Часть', 'День', 'Ночь', 'Утро', 'Вечер', 'Время', 'Шаг', 'Кадр', 'Событие',
    'Заметка', 'Сюжет', 'История', 'Эпизод', 'Акт', 'Раздел', 'Страница', 'В', 'На', 'С', 'По', 'К',
    'Из', 'Он', 'Она', 'Они', 'Оно', 'Затем', 'Когда', 'После', 'Но', 'И', 'А',
    'При', 'Нет', 'Вот', 'Вон', 'Кто', 'Что', 'Ничий', 'Ничей', 'Ничья', 'Ничье', 'Ничьё', 'Ничьи',
    'Пауза', 'Ответ', 'Вопрос', 'Реплика', 'Тишина', 'Голос', 'Смех', 'Вздох', 'Крик', 'Шум', 'Звук',
    'Молчание', 'Взгляд', 'Шаги',
    'Деревья', 'Дерево', 'Кусты', 'Куст', 'Трава', 'Листва', 'Ветки', 'Ветка', 'Камни', 'Камень',
    'Скалы', 'Скала', 'Листья', 'Лист', 'Стволы', 'Ствол', 'Песок', 'Земля', 'Снег', 'Лед', 'Лёд',
    'Дождь', 'Ветер', 'Туман', 'Облака', 'Небо', 'Свет', 'Тьма', 'Тень', 'Тени',
    'Фигура', 'Фигуры', 'Силуэт', 'Силуэты', 'Облик', 'Образ', 'Образы',
    'Мужчина', 'Женщина', 'Человек', 'Большинство', 'Меньшинство', 'Множество', 'Поисковики', 'Спасатели',
    'Семнадцатое', 'Семнадцатый', 'Первый', 'Второй', 'Третий', 'Четвертый', 'Пятый', 'Шестой', 'Седьмой',
    'Восьмой', 'Девятый', 'Десятый', 'Двадцатый'
}


def is_acoustic_label_name(name: str) -> bool:
    """A label such as ``Voice Mike`` is audio metadata, never a cast member."""
    return bool(re.match(
        r'^\s*(?:знакомый\s+|чужой\s+|тихий\s+|громкий\s+|хриплый\s+|'
        r'искаж[её]нный\s+)?(?:голос(?:а|у|ом|е)?|voice)\s+\S+',
        str(name or ''),
        re.IGNORECASE,
    ))

_COLLECTIVE_WORDS = {
    'люди', 'людей', 'людям', 'людьми', 'людях', 'народ', 'народа', 'народу', 'народом',
    'толпа', 'толпы', 'толпе', 'толпу', 'толпой', 'толп',
    'жители', 'жителей', 'жителям', 'жителями', 'жителях',
    'горожане', 'горожан', 'горожанам',
    'солдаты', 'солдат', 'солдатам', 'солдатами', 'солдатах',
    'войско', 'войска', 'войску', 'войском', 'войске',
    'армия', 'армии', 'армию', 'армией',
    'полк', 'полка', 'полку', 'полком', 'полке',
    'отряд', 'отряда', 'отряду', 'отрядом', 'отряде',
    'экспедиция', 'экспедиции', 'экспедицию', 'экспедицией', 'экспедициях',
    'экипаж', 'экипажа', 'экипажу', 'экипажем', 'экипаже',
    'команда', 'команды', 'команде', 'команду', 'командой',
    'группа', 'группы', 'группе', 'группу', 'группой',
    'гарнизон', 'гарнизона', 'гарнизону', 'гарнизоном', 'гарнизоне',
    'стражники', 'стражников', 'стражникам', 'рыцари', 'рыцарей', 'крестьяне', 'крестьян',
    'деревня', 'деревни', 'деревне', 'деревню', 'деревней',
    'большинство', 'большинства', 'большинству', 'большинством', 'большинстве',
    'меньшинство', 'меньшинства', 'меньшинству', 'меньшинством', 'меньшинстве',
    'множество', 'множества', 'множеству', 'множеством', 'множестве',
    'поисковики', 'поисковиков', 'поисковикам', 'поисковиками', 'поисковиках',
    'спасатели', 'спасателей', 'спасателям', 'спасателями', 'спасателях',
    'исследователи', 'исследователей', 'исследователям', 'исследователями', 'исследователях',
    'очевидцы', 'очевидцев', 'очевидцам', 'очевидцами', 'очевидцах',
    'свидетели', 'свидетелей', 'свидетелям', 'свидетелями', 'свидетелях',
    'прохожие', 'прохожих', 'прохожим', 'прохожими',
    'эксперты', 'экспертов', 'экспертам', 'экспертами',
    'ученые', 'учёные', 'ученых', 'учёных', 'ученым', 'учёным', 'учеными', 'учёными',
    'журналисты', 'журналистов', 'журналистам',
    'туристы', 'туристов', 'туристам',
    'посетители', 'посетителей', 'посетителям',
    'рабочие', 'рабочих', 'рабочим',
    'строители', 'строителей', 'строителям',
    'people', 'crowd', 'crowds', 'soldiers', 'army', 'troops', 'villagers', 'guards', 'knights', 'peasants', 'village', 'regiment',
    'expedition', 'crew', 'team', 'group', 'squad', 'garrison', 'majority', 'minority', 'multitude',
    'searchers', 'rescuers', 'researchers', 'witnesses', 'bystanders', 'passersby', 'experts', 'scientists',
    'journalists', 'tourists', 'visitors', 'workers', 'builders'
}

_PROP_OR_ARMOR_WORDS = {
    'шлем', 'шлема', 'шлему', 'шлемом', 'шлеме', 'шлемы', 'шлемов', 'шлемах',
    'доспехи', 'доспех', 'доспеха', 'доспехов', 'щит', 'щита', 'щиту', 'щитом', 'щиты',
    'кольчуга', 'кольчуги', 'кольчугу', 'латы',
    'меч', 'меча', 'мечу', 'мечом', 'мече', 'мечи', 'мечей', 'мечам', 'мечами', 'мечах',
    'клинок', 'клинка', 'клинку', 'клинком', 'клинке', 'клинки', 'клинков', 'клинкам',
    'плуг', 'плуга', 'плугу', 'плугом', 'плуге', 'плуги',
    'молот', 'молота', 'молоту', 'молотом', 'молоте', 'молоты',
    'ножны', 'ножен', 'ножнам', 'ножнами', 'ножнах',
    'рукоять', 'рукояти', 'рукоятью', 'рукоятей',
    'свиток', 'свитка', 'свитку', 'свитком', 'свитки',
    'наковальня', 'наковальни', 'наковальню', 'наковальней', 'наковальнях',
    'горн', 'горна', 'горну', 'горном', 'горне',
    'угли', 'углей', 'углям', 'углями', 'углях',
    'инструмент', 'инструменты', 'инструментов', 'инструментам',
    'верстак', 'верстака', 'верстаку', 'верстаком', 'верстаке',
    'телега', 'телеги', 'телегу', 'телегой', 'телегах', 'телегам',
    'дверь', 'двери', 'дверью', 'дверях',
    'стена', 'стены', 'стене', 'стену', 'стеной', 'стенах',
    'холм', 'холма', 'холму', 'холмом', 'холме', 'холмы',
    'печать', 'печати', 'печатью',
    'кузница', 'кузницы', 'кузнице', 'кузницу', 'кузницей',
    'оружие', 'оружия', 'оружию', 'оружием', 'оружии',
    'починка', 'починку', 'починки', 'работа', 'работу', 'работы',
    'жар', 'жара', 'жаром', 'жаре', 'вода', 'воды', 'воде', 'воду', 'водой',
    'пальцы', 'пальцев', 'пальцах', 'рука', 'руки', 'руку', 'рукой', 'руках', 'ладонь', 'ладони', 'ладонью',
    'лицо', 'лица', 'лицу', 'лицом', 'лицах', 'глаза', 'глаз', 'глазах',
    'helmet', 'sword', 'blade', 'sheath', 'anvil', 'forge', 'hammer', 'plow', 'scroll', 'seal', 'workbench', 'cart', 'hill', 'wall', 'door'
}

_ADVERB_AND_TIME_WORDS = {
    'последний', 'последняя', 'последнее', 'последние', 'последним', 'последними', 'последнего', 'последней', 'последнему',
    'первый', 'первая', 'первое', 'первые', 'первым', 'первыми', 'первого', 'первой',
    'следующий', 'следующая', 'следующее', 'следующие', 'следующим',
    'затем', 'потом', 'сначала', 'вскоре', 'однажды', 'наутро', 'вечером', 'утром', 'днем', 'днём', 'ночью', 'весной', 'зимой', 'летом', 'осенью',
    'вдруг', 'внезапно', 'неожиданно', 'теперь', 'сейчас', 'всегда', 'никогда', 'часто', 'редко',
    'прямо', 'кругом', 'вокруг', 'около', 'рядом', 'возле', 'напротив',
    'впервые', 'впервый', 'во-первых', 'во-вторых', 'в-третьих',
    'last', 'first', 'next', 'then', 'after', 'before', 'later', 'soon', 'once', 'morning', 'evening', 'night', 'spring', 'summer', 'winter', 'autumn', 'suddenly', 'now', 'always', 'never'
}

_INANIMATE_ENTITY_WORDS = {
    'корабль', 'судно', 'лодка', 'катер', 'маяк', 'остров', 'дом', 'комната',
    'дверь', 'окно', 'машина', 'поезд', 'самолёт', 'самолет', 'телефон', 'радио',
    'ключ', 'письмо', 'документ', 'фото', 'фотография', 'портрет', 'книга',
    'ящик', 'коробка', 'мост', 'берег', 'море', 'лес',
    'дерево', 'деревья', 'деревьев', 'деревьям', 'деревьями', 'деревьях',
    'куст', 'кусты', 'кустов', 'кустами', 'кустах',
    'трава', 'травы', 'траве', 'траву', 'травой', 'травах',
    'листва', 'листвы', 'листве', 'листву', 'листвой',
    'ветка', 'ветки', 'веток', 'веткам', 'ветками', 'ветках',
    'ветви', 'ветвей', 'ветвям', 'ветвями', 'ветвях',
    'камень', 'камни', 'камней', 'камням', 'камнями', 'камнях',
    'скала', 'скалы', 'скал', 'скалам', 'скалами', 'скалах',
    'лист', 'листья', 'листьев', 'листьям', 'листьями', 'листьях',
    'ствол', 'стволы', 'стволов', 'стволам', 'стволами', 'стволах',
    'песок', 'песка', 'песку', 'песком', 'песке',
    'земля', 'земли', 'земле', 'землю', 'землей',
    'снег', 'снега', 'снегу', 'снегом', 'снеге',
    'лед', 'лёд', 'льда', 'льду', 'льдом', 'льде',
    'дождь', 'дождя', 'дождю', 'дождем', 'дожде',
    'ветер', 'ветра', 'ветру', 'ветром', 'ветре',
    'туман', 'тумана', 'туману', 'туманом', 'тумане',
    'облака', 'облако', 'облаков', 'облакам',
    'небо', 'неба', 'небу', 'небом', 'небе',
    'свет', 'света', 'свету', 'светом', 'свете',
    'тьма', 'тьмы', 'тьме', 'тьму', 'тьмой',
    'тень', 'тени', 'теней', 'теням', 'тенями', 'тенях',
    'фигура', 'фигуры', 'фигур', 'фигурам', 'фигурами', 'фигурах',
    'силуэт', 'силуэты', 'силуэтов', 'силуэтам', 'силуэтами', 'силуэтах',
    'облик', 'облика', 'облику', 'обликом', 'облике',
    'образ', 'образы', 'образов', 'образам', 'образами', 'образах',
    'boat', 'ship', 'vessel', 'lighthouse', 'island', 'house', 'room', 'door', 'window', 'car', 'train',
    'plane', 'phone', 'radio', 'key', 'letter', 'document', 'photo', 'portrait',
    'book', 'box', 'bridge', 'shore', 'sea', 'ocean', 'river', 'lake', 'forest',
    'mountain', 'valley', 'reef', 'coast', 'bay', 'beach', 'cliff', 'waterfall',
    'last', 'final', 'latest',
    'tree', 'trees', 'bush', 'bushes', 'grass', 'leaf', 'leaves', 'foliage', 'branch', 'branches',
    'stone', 'stones', 'rock', 'rocks', 'sand', 'earth', 'ground', 'snow', 'ice', 'rain', 'wind', 'fog',
    'cloud', 'clouds', 'sky', 'light', 'darkness', 'shadow', 'shadows', 'silhouette', 'silhouettes',
    'figure', 'figures'
}

_NATURAL_LOCATION_NOUNS = {
    'океан', 'океана', 'океане', 'океану', 'океаном',
    'море', 'моря', 'море', 'морю', 'морем',
    'река', 'реки', 'реке', 'реку', 'рекой',
    'озеро', 'озера', 'озере', 'озеру', 'озером',
    'гора', 'горы', 'горе', 'гору', 'горой',
    'долина', 'долины', 'долине', 'долину', 'долиной',
    'риф', 'рифа', 'рифе', 'рифу', 'рифом',
    'побережье', 'побережья', 'побережье', 'побережью', 'побережьем',
    'бухта', 'бухты', 'бухте', 'бухту', 'бухтой',
    'пляж', 'пляжа', 'пляже', 'пляжу', 'пляжем',
    'утёс', 'утес', 'скала', 'водопад',
    'ocean', 'sea', 'river', 'lake', 'mountain', 'valley', 'reef', 'coast',
    'bay', 'beach', 'cliff', 'waterfall', 'forest', 'island',
}

_TEMPORAL_ENTITY_WORDS = {
    'утро', 'утром', 'утра', 'утру', 'день', 'днём', 'днем', 'дня', 'дню', 'дне',
    'ночь', 'ночью', 'ночи', 'вечер', 'вечером', 'вечера', 'вечеру', 'вечере',
    'зима', 'зимой', 'зиме', 'зиму', 'зимы', 'лето', 'летом', 'лета', 'лету', 'лете',
    'весна', 'весной', 'весне', 'весну', 'весны', 'осень', 'осенью', 'осени',
    'неделя', 'недели', 'недель', 'неделе', 'неделю', 'неделей', 'неделях', 'неделям', 'неделями',
    'месяц', 'месяца', 'месяцев', 'месяцу', 'месяцем', 'месяце', 'месяцы', 'месяцах',
    'год', 'года', 'году', 'годом', 'годе', 'годы', 'лет', 'годах', 'годам', 'годами',
    'минута', 'минуты', 'минут', 'минуту', 'минутой', 'минутах',
    'час', 'часа', 'часов', 'часу', 'часом', 'часе', 'часы', 'часах',
    'время', 'времени', 'временем', 'времена', 'времён', 'временах',
    'срок', 'срока', 'сроку', 'сроком', 'сроки', 'сроках',
    'век', 'века', 'веку', 'веком', 'века', 'веках',
    'пора', 'поры', 'поре', 'пору', 'порой',
    'рассвет', 'рассвете', 'рассветом', 'закат', 'закате', 'закатом', 'полночь', 'полуночью',
    'сегодня', 'вчера', 'завтра', 'наутро', 'now', 'today', 'yesterday', 'tomorrow',
    'morning', 'afternoon', 'evening', 'night', 'dawn', 'sunset', 'midnight',
    'week', 'weeks', 'month', 'months', 'year', 'years', 'hour', 'hours', 'minute', 'minutes', 'time', 'times'
}


def is_temporal_entity(name: str) -> bool:
    """Check if name represents a time, duration, season, or day-part entity."""
    norm = ' '.join(strip_combining_accents(str(name or '')).casefold().split())
    if not norm:
        return False
    tokens = re.findall(r'[\wё-]+', norm)
    if not tokens:
        return False
    if norm in _TEMPORAL_ENTITY_WORDS or any(t in _TEMPORAL_ENTITY_WORDS for t in tokens):
        return True
    _TEMPORAL_STEMS = {stem_ru_word(w) for w in _TEMPORAL_ENTITY_WORDS}
    if any(stem_ru_word(t) in _TEMPORAL_STEMS for t in tokens):
        return True
    return False

_GENERIC_TAXONOMY_CATEGORIES = {
    'птица', 'птицы', 'животное', 'животные', 'существо', 'существа',
    'вид', 'виды', 'млекопитающее', 'млекопитающие', 'пресмыкающееся', 'пресмыкающиеся',
    'земноводное', 'земноводные', 'насекомое', 'насекомые', 'рыба', 'рыбы',
    'зверь', 'звери', 'пернатый', 'пернатые', 'питомец', 'питомцы',
    'bird', 'birds', 'animal', 'animals', 'creature', 'creatures',
    'mammal', 'mammals', 'fish', 'reptile', 'reptiles', 'amphibian', 'amphibians',
    'insect', 'insects', 'species', 'pet', 'pets'
}

_KNOWN_ANIMALS_AND_CREATURES = {
    # Birds
    'ворон', 'ворона', 'сорока', 'галка', 'сойка', 'птица', 'орел', 'орёл', 'сова',
    'ястреб', 'сокол', 'голубь', 'чайка', 'лебедь', 'утка', 'гусь', 'цапля', 'журавль',
    'дятел', 'синица', 'воробей', 'снегирь', 'пингвин', 'страус', 'пеликан', 'альбатрос',
    'crow', 'raven', 'magpie', 'jay', 'bird', 'eagle', 'owl', 'hawk', 'falcon',
    'pigeon', 'dove', 'seagull', 'swan', 'duck', 'goose', 'penguin',
    # Mammals & wild animals
    'волк', 'медведь', 'лиса', 'лисица', 'заяц', 'кролик', 'олень', 'лось', 'лошадь',
    'конь', 'кот', 'кошка', 'собака', 'пес', 'пёс', 'щенок', 'котенок', 'котёнок',
    'рысь', 'тигр', 'лев', 'леопард', 'пантера', 'гепард', 'слон', 'жираф', 'бегемот',
    'гиппопотам', 'носорог', 'обезьяна', 'шимпанзе', 'горилла', 'енот', 'барсук', 'бобр',
    'белка', 'мышь', 'крыса', 'хомяк', 'еж', 'ёж', 'кенгуру', 'коала', 'панголин',
    'утконос', 'саола', 'кабан', 'корова', 'бык', 'овца', 'баран', 'коза', 'козел',
    'козёл', 'верблюд', 'дельфин', 'кит', 'косатка', 'касатка', 'акула', 'осьминог',
    'кальмар', 'тюлень', 'морж', 'выдра', 'калан', 'wolf', 'bear', 'fox', 'hare',
    'rabbit', 'deer', 'elk', 'moose', 'horse', 'cat', 'kitten', 'dog', 'hound', 'puppy',
    'lynx', 'tiger', 'lion', 'leopard', 'panther', 'elephant', 'giraffe', 'hippo',
    'rhino', 'monkey', 'chimpanzee', 'gorilla', 'raccoon', 'badger', 'beaver',
    'squirrel', 'mouse', 'rat', 'hedgehog', 'kangaroo', 'koala', 'pangolin',
    'platypus', 'saola', 'dolphin', 'whale', 'orca', 'shark', 'octopus', 'squid',
    # Reptiles, amphibians, fish
    'змея', 'уж', 'гадюка', 'кобра', 'питон', 'ящерица', 'хамелеон', 'варан',
    'черепаха', 'крокодил', 'аллигатор', 'лягушка', 'жаба', 'рыба',
    'snake', 'viper', 'cobra', 'python', 'lizard', 'turtle', 'crocodile', 'frog', 'fish',
    # Fantasy / mythological creatures
    'дракон', 'гоблин', 'эльф', 'гном', 'орк', 'тролль', 'фея', 'кентавр', 'единорог',
    'грифон', 'василиск', 'химера', 'сфинкс', 'пегас', 'русалка', 'сирена', 'призрак',
    'привидение', 'демон', 'ангел', 'чудовище', 'монстр', 'великан', 'титан', 'леший',
    'домовой', 'водяной', 'кикимора', 'баба-яга', 'кощей', 'существо',
    'dragon', 'goblin', 'elf', 'dwarf', 'orc', 'troll', 'fairy', 'fae', 'centaur',
    'unicorn', 'griffin', 'gryphon', 'basilisk', 'chimera', 'sphinx', 'pegasus',
    'mermaid', 'siren', 'ghost', 'specter', 'demon', 'angel', 'monster', 'creature', 'beast'
}

_NON_CHARACTER_GRAMMAR_WORDS = {
    # Pronouns & demonstratives RU
    'это', 'этот', 'эта', 'эти', 'этом', 'этому', 'этой', 'этих', 'этим', 'этими',
    'то', 'тот', 'та', 'те', 'том', 'тому', 'той', 'тех', 'тем', 'теми',
    'такой', 'такая', 'такое', 'такие', 'таком', 'такой', 'таких', 'таким', 'такими',
    'весь', 'вся', 'всё', 'все', 'всех', 'всего', 'всему', 'всеми', 'всем',
    'каждый', 'каждая', 'каждое', 'каждые', 'каждого', 'каждой', 'каждым', 'каждых',
    'любой', 'любая', 'любое', 'любые', 'любого', 'любым', 'любых',
    'сам', 'сама', 'само', 'сами', 'самого', 'самой', 'самим', 'самими', 'самых',
    'самый', 'самая', 'самое', 'самые', 'самого', 'самой', 'самым', 'самых',
    'мой', 'моя', 'мое', 'моё', 'мои', 'моего', 'моей', 'моим', 'моих',
    'твой', 'твоя', 'твое', 'твоё', 'твои', 'твоего', 'твоей', 'твоим', 'твоих',
    'наш', 'наша', 'наше', 'наши', 'нашего', 'нашей', 'нашим', 'наших',
    'ваш', 'ваша', 'ваше', 'ваши', 'вашего', 'вашей', 'вашим', 'ваших',
    'свой', 'своя', 'свое', 'своё', 'свои', 'своего', 'своей', 'своим', 'своих',
    'кто', 'кого', 'кому', 'кем', 'ком', 'кто-то', 'что-то', 'кое-кто', 'кое-что', 'никто', 'ничто', 'некто', 'нечто',
    'что', 'чего', 'чему', 'чем', 'ком', 'чем-то', 'кем-то', 'кому-то', 'кого-то',
    'который', 'которая', 'которое', 'которые', 'которого', 'которой', 'которым', 'которых',
    'чей', 'чья', 'чье', 'чьё', 'чьи',
    'ничей', 'ничья', 'ничье', 'ничьё', 'ничьи', 'ничий', 'ничьего', 'ничьей', 'ничьему', 'ничьим', 'ничьих', 'ничьими',
    'никакой', 'никакая', 'никакое', 'никакие', 'никакого', 'никакой', 'никаким', 'никаких', 'никакими',
    'один', 'одна', 'одно', 'одни', 'одного', 'одной', 'одним', 'одних',
    'другой', 'другая', 'другое', 'другие', 'другого', 'другой', 'другим', 'других',
    'некоторый', 'некоторые', 'некоторое', 'некоторая', 'некоторых',
    'он', 'она', 'оно', 'они', 'его', 'ее', 'её', 'ему', 'ей', 'им', 'ими', 'них', 'ним', 'ними', 'нем', 'нём', 'ней',
    'себя', 'себе', 'собой', 'собою',
    # Ordinals RU
    'семнадцатый', 'семнадцатая', 'семнадцатое', 'семнадцатые', 'семнадцатого', 'семнадцатой', 'семнадцатому', 'семнадцатым', 'семнадцатых', 'семнадцатыми',
    'первый', 'первая', 'первое', 'первые', 'первого', 'первой', 'первому', 'первым', 'первых', 'первыми',
    'второй', 'вторая', 'второе', 'вторые', 'второго', 'второй', 'второму', 'вторым', 'вторых', 'вторыми',
    'третий', 'третья', 'третье', 'третьи', 'третьего', 'третьей', 'третьему', 'третьим', 'третьих', 'третьими',
    'четвертый', 'четвертая', 'четвертое', 'четвертые', 'пятый', 'шестой', 'седьмой', 'восьмой', 'девятый', 'десятый', 'двадцатый', 'сотый', 'тысячный',
    'впервые', 'впервый', 'во-первых', 'во-вторых', 'в-третьих',
    # Dialogue & screenplay markers RU / EN
    'пауза', 'паузы', 'паузе', 'паузу', 'паузой', 'ответ', 'ответа', 'ответу', 'ответом', 'ответе',
    'вопрос', 'вопроса', 'вопросу', 'вопросом', 'вопросе', 'реплика', 'реплики', 'реплику',
    'тишина', 'тишины', 'тишине', 'тишину', 'тишиной', 'голос', 'голоса', 'голосу', 'голосом', 'голосе',
    'смех', 'смеха', 'смеху', 'смехом', 'вздох', 'вздоха', 'вздоху', 'вздохом',
    'крик', 'крика', 'крику', 'криком', 'шум', 'шума', 'шуму', 'шумом', 'звук', 'звука', 'звуку', 'звуком',
    'молчание', 'молчания', 'молчанию', 'молчанием', 'взгляд', 'взгляда', 'взгляду', 'взглядом',
    'шаги', 'шагов', 'шагам', 'шагами', 'шагах',
    'pause', 'silence', 'voice', 'laughter', 'sigh', 'shout', 'noise', 'sound', 'steps',
    # Pronouns & demonstratives EN
    'this', 'that', 'these', 'those', 'each', 'every', 'either', 'neither', 'all', 'both', 'none',
    'some', 'any', 'many', 'much', 'few', 'several', 'which', 'what', 'whose', 'whoever', 'whatever',
    'another', 'other', 'others', 'he', 'him', 'his', 'she', 'her', 'hers', 'it', 'its', 'they', 'them',
    'their', 'theirs', 'we', 'us', 'our', 'ours', 'you', 'your', 'yours', 'i', 'me', 'my', 'mine',
    # Common verbs / imperatives / copulas RU
    'начать', 'начинать', 'начнем', 'начнём',
    'быть', 'был', 'была', 'было', 'были', 'будет', 'будут', 'будем', 'будь', 'будьте',
    'стать', 'стал', 'стала', 'стало', 'стали', 'станет', 'станут',
    'мочь', 'мог', 'могла', 'могло', 'могли', 'может', 'могут',
    'хотеть', 'хотел', 'хотела', 'хочет', 'хотят',
    'стоит', 'стоят', 'стоило', 'следует', 'следовать', 'приходится', 'приходятся', 'пришлось',
    'давай', 'давайте', 'представь', 'представьте', 'посмотри', 'посмотрите', 'скажи', 'скажите',
    'является', 'являются', 'являлся', 'являлась', 'являлись',
    'казался', 'казалась', 'казалось', 'казались', 'кажется', 'кажутся',
    'оказался', 'оказалась', 'оказалось', 'оказались',
    # Common verbs / imperatives EN
    'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does', 'did',
    'can', 'could', 'shall', 'should', 'will', 'would', 'may', 'might', 'must',
    'start', 'started', 'starting', 'begin', 'began', 'begins', 'beginning',
    'let', 'lets', 'see', 'look', 'imagine', 'remember',
    # Conjunctions, particles, adverbs RU
    'однако', 'потому', 'поэтому', 'если', 'чтобы', 'когда', 'пока', 'хотя', 'пусть', 'пускай',
    'будто', 'словно', 'точно', 'ведь', 'даже', 'просто', 'лишь', 'только', 'кстати', 'действительно',
    'конечно', 'наверное', 'возможно', 'видимо', 'пожалуй', 'впрочем', 'итак', 'значит', 'следовательно',
    'наконец', 'вдруг', 'затем', 'потом', 'сначала', 'вскоре', 'очень', 'слишком', 'вместе', 'кроме',
    'между', 'через', 'около', 'после', 'перед', 'среди', 'возле', 'внутри', 'снаружи', 'здесь',
    'тут', 'там', 'туда', 'оттуда', 'везде', 'всюду', 'куда', 'откуда', 'где', 'почему', 'зачем',
    'как', 'так', 'тогда', 'сейчас', 'теперь', 'всегда', 'никогда', 'иногда', 'часто', 'редко',
    'давно', 'снова', 'опять', 'уже', 'еще', 'ещё', 'вовсе', 'вовремя', 'вперед', 'вперёд', 'назад',
    'кругом', 'вокруг', 'однажды', 'вместо', 'напротив', 'сверх', 'вдоль', 'поперек', 'поперёк',
    'внезапно', 'неожиданно',
    'при', 'притом', 'причем', 'причём', 'нет', 'не', 'да', 'вот', 'вон',
    'ведь', 'мол', 'дескать', 'авось', 'небось', 'вряд', 'навряд', 'разве', 'неужели', 'же', 'ли', 'бы',
    # Conjunctions, adverbs EN
    'however', 'therefore', 'because', 'although', 'though', 'while', 'meanwhile', 'since', 'unless',
    'until', 'despite', 'instead', 'besides', 'furthermore', 'moreover', 'indeed', 'perhaps', 'maybe',
    'probably', 'certainly', 'definitely', 'actually', 'really', 'just', 'only', 'even', 'also', 'too',
    'again', 'already', 'still', 'yet', 'then', 'now', 'here', 'there', 'everywhere', 'somewhere',
    'nowhere', 'always', 'never', 'sometimes', 'often', 'rarely', 'soon', 'suddenly', 'finally',
}

_BARE_GENERIC_AGENT_NOUNS = {
    'мужчина', 'мужчины', 'мужчине', 'мужчину', 'мужчиной', 'мужчинах', 'мужчинам',
    'женщина', 'женщины', 'женщине', 'женщину', 'женщиной', 'женщинах', 'женщинам',
    'человек', 'человека', 'человеку', 'человеком', 'человеке',
    'парень', 'парня', 'парню', 'парнем', 'парне',
    'девушка', 'девушки', 'девушке', 'девушку', 'девушкой', 'девушках', 'девушкам',
    'мальчик', 'мальчика', 'мальчику', 'мальчиком', 'мальчике',
    'девочка', 'девочки', 'девочке', 'девочку', 'девочкой', 'девочках', 'девочкам',
    'старик', 'старика', 'старику', 'стариком', 'старике',
    'старуха', 'старухи', 'старухе', 'старуху', 'старухой', 'старухах', 'старухам',
    'ребёнок', 'ребенок', 'ребёнка', 'ребенка', 'ребёнку', 'ребенку', 'ребёнком', 'ребенком', 'ребёнке', 'ребенке',
    'фигура', 'фигуры', 'фигуре', 'фигуру', 'фигурой', 'фигурах', 'фигурам',
    'силуэт', 'силуэта', 'силуэту', 'силуэтом', 'силуэте',
    'незнакомец', 'незнакомца', 'незнакомцу', 'незнакомцем', 'незнакомце',
    'незнакомка', 'незнакомки', 'незнакомке', 'незнакомку', 'незнакомкой',
    'лицо', 'лица', 'лицу', 'лицом', 'лице',
    'персона', 'персоны', 'персоне', 'персону', 'персоной',
    'особа', 'особы', 'особе', 'особу', 'особой',
    'собеседник', 'собеседника', 'собеседнику', 'собеседником', 'собеседнике',
    'собеседница', 'собеседницы', 'собеседнице', 'собеседницу', 'собеседницей',
    'попутчик', 'попутчика', 'попутчику', 'попутчиком', 'попутчике',
    'попутчица', 'попутчицы', 'попутчице', 'попутчицу', 'попутчицей',
    'man', 'woman', 'person', 'guy', 'girl', 'boy', 'kid', 'child', 'stranger', 'figure', 'silhouette'
}


def is_bare_generic_agent_noun(name: str) -> bool:
    """Check if candidate is a bare un-named generic human or visual noun without descriptive modifier."""
    norm = ' '.join(strip_combining_accents(str(name or '')).casefold().split())
    if not norm:
        return False
    tokens = re.findall(r'[\wё-]+', norm)
    if len(tokens) == 1:
        tok = tokens[0]
        if tok in _BARE_GENERIC_AGENT_NOUNS:
            return True
        stem = stem_ru_word(tok)
        if stem and stem in {stem_ru_word(w) for w in _BARE_GENERIC_AGENT_NOUNS}:
            return True
    return False

_KNOWN_DISTINCT_GENDER_PAIRS = {
    ('ворон', 'ворона'), ('ворона', 'ворон'),
    ('raven', 'crow'), ('crow', 'raven'),
    ('raven', 'ворона'), ('ворона', 'raven'),
    ('crow', 'ворон'), ('ворон', 'crow'),
    ('александр', 'александра'), ('александра', 'александр'),
    ('евгений', 'евгения'), ('евгения', 'евгений'),
    ('валентин', 'валентина'), ('валентина', 'валентин'),
    ('юлий', 'юлия'), ('юлия', 'юлий'),
    ('влад', 'влада'), ('влада', 'влад'),
    ('ярослав', 'ярослава'), ('ярослава', 'ярослав'),
    ('станислав', 'станислава'), ('станислава', 'станислав'),
    ('антон', 'антонина'), ('антонина', 'антон'),
    ('лис', 'лиса'), ('лиса', 'лис'),
    ('волк', 'волчица'), ('волчица', 'волк'),
    ('кот', 'кошка'), ('кошка', 'кот'),
    ('лев', 'львица'), ('львица', 'лев'),
    ('супруг', 'супруга'), ('супруга', 'супруг'),
}


def is_recognized_animal_or_creature(name: str) -> bool:
    """Check if name represents a recognized animal, bird, marine life, or fantasy creature."""
    norm = ' '.join(strip_combining_accents(str(name or '')).casefold().split())
    if not norm or norm in _GENERIC_TAXONOMY_CATEGORIES:
        return False
    tokens = [t for t in re.findall(r'[\wё-]+', norm) if t]
    for token in tokens:
        if token in _KNOWN_ANIMALS_AND_CREATURES:
            return True
        stem = stem_ru_word(token)
        if stem and any(stem_ru_word(a) == stem for a in _KNOWN_ANIMALS_AND_CREATURES):
            return True
    return False


def is_recognized_role(name: str) -> bool:
    """Check if name represents a recognized story role or agent noun."""
    norm = ' '.join(strip_combining_accents(str(name or '')).casefold().split())
    if not norm:
        return False
    tokens = [t for t in re.findall(r'[\wё-]+', norm) if t]
    for role, syns in _COMMON_ROLES.items():
        if norm == role or norm in syns:
            return True
        for token in tokens:
            if token == role or token in syns:
                return True
            stem = stem_ru_word(token)
            if stem and any(stem_ru_word(s) == stem for s in syns | {role}):
                return True
    for token in tokens:
        if token in _AGENT_NOUN_WORDS or stem_ru_word(token) in _AGENT_NOUN_STEMS:
            return True
    return False


# General morphological and lexical pattern for spatial, directional, and temporal adverbs
_SPATIAL_TEMPORAL_ADVERBS_REGEX = re.compile(
    r'^(?:'
    # Russian spatial & directional fused adverbs (preposition + spatial root + suffix)
    r'(?:в|во|на|с|со|по|из|за|до)'
    r'(?:дал[иеь]|далек[ео]|близ[и]?|перед[и]?|зад[и]?|верх[уе]?|низ[уе]?|'
    r'прав[ао]|лев[ао]|серед[е]?|середин[е]?|глуб[ье]?|выс[ье]?|бок[уе]?|'
    r'нутр[и]?|нутрь|наруж[и]?|прот[ив]|одал[ь]?|всюд[у]|кра[юя]|конц[е]?|ряд[ом]?)'
    r'|'
    # Russian temporal / circumstantial fused adverbs
    r'(?:в|во|на|с|по|до)'
    r'(?:скор[ео]?|давн[ое]?|ран[ео]?|поздн[ое]?|утр[ом]?|вечер[ом]?|ноч[ью]?|'
    r'нов[оа]?|друг|век[а]?|полн[о]?|пуст[ую]?|ровн[о]?)'
    r'|'
    # Pronominal and locative adverbs RU
    r'(?:везде|всюду|повсюду|нигде|никуда|негде|некуда|оттуда|туда|сюда|отсюда|'
    r'всегда|никогда|иногда|порой|часто|редко|затем|потом|сначала|вскоре|вдруг|'
    r'внезапно|неожиданно|однажды|'
    r'здесь|тут|там|где-то|куда-то|откуда-то|прямо|кругом|вокруг|около|рядом|возле|напротив)'
    r'|'
    # English locative & directional adverbs
    r'(?:afar|ahead|around|aside|ashore|aloft|abroad|nearby|away|'
    r'indoors|outdoors|upstairs|downstairs|overhead|underfoot|'
    r'everywhere|nowhere|somewhere|anywhere|here|there|yonder)'
    r')$',
    re.IGNORECASE
)


def is_grammatical_non_character(name: str, description: str = '') -> bool:
    """Reject verbs, pronouns, demonstratives, conjunctions, particles, adverbs, discourse markers, and bare generic nouns."""
    norm = ' '.join(strip_combining_accents(str(name or '')).casefold().split())
    if not norm:
        return True
    tokens = re.findall(r'[\wё-]+', norm)
    if not tokens:
        return True
    # Exempt recognized roles, animals, and fantasy creatures
    if is_recognized_role(norm) or is_recognized_animal_or_creature(norm):
        return False
    if is_bare_generic_agent_noun(norm):
        return True
    if is_temporal_entity(norm) or any(is_temporal_entity(t) for t in tokens):
        return True
    # General spatial, directional and temporal adverbs
    if _SPATIAL_TEMPORAL_ADVERBS_REGEX.match(norm) or any(_SPATIAL_TEMPORAL_ADVERBS_REGEX.match(t) for t in tokens):
        return True
    if norm in _ADVERB_AND_TIME_WORDS or any(t in _ADVERB_AND_TIME_WORDS for t in tokens):
        return True
    _ADVERB_STEMS = {stem_ru_word(w) for w in _ADVERB_AND_TIME_WORDS}
    if any(stem_ru_word(t) in _ADVERB_STEMS for t in tokens):
        return True
    if len(tokens) == 1:
        tok = tokens[0]
        if tok in _NON_CHARACTER_GRAMMAR_WORDS or tok in {'начать', 'начинать', 'начнем', 'начнём'}:
            return True
        # Russian infinitive verbs: ending in -ть, -ти, -чь
        if re.search(r'^[а-яё]+(?:[аеёиоуыэюя]ть|[сзйд]ти|чь|ться|тись)$', tok) and tok not in {'дочь', 'ночь', 'печь', 'речь'}:
            return True
    elif all(tok in _NON_CHARACTER_GRAMMAR_WORDS for tok in tokens):
        return True
    return False


def is_collective_entity(name: str) -> bool:
    """True if name represents a collective group or plural population."""
    norm = ' '.join(strip_combining_accents(str(name or '')).casefold().split())
    if not norm:
        return True
    tokens = re.findall(r'[\wё-]+', norm)
    if not tokens:
        return True
    _PREPOSITIONS = {'в', 'во', 'на', 'с', 'со', 'у', 'к', 'ко', 'по', 'из', 'за', 'о', 'об', 'обо', 'без', 'под', 'над', 'при', 'in', 'on', 'with', 'at', 'by', 'under', 'from'}
    head_tokens = []
    for t in tokens:
        if t in _PREPOSITIONS:
            break
        head_tokens.append(t)
    if not head_tokens:
        head_tokens = tokens
    if any(t in _COLLECTIVE_WORDS for t in head_tokens):
        if len(tokens) == 1 or all(t in _COLLECTIVE_WORDS or t in _NON_CHARACTER_GRAMMAR_WORDS for t in tokens):
            return True
        if head_tokens[-1] in _COLLECTIVE_WORDS:
            return True
    return False


def is_prop_or_armor_entity(name: str) -> bool:
    """True if candidate is an armor piece, tool, weapon or inanimate prop."""
    norm = ' '.join(strip_combining_accents(str(name or '')).casefold().split())
    if not norm:
        return True
    tokens = re.findall(r'[\wё-]+', norm)
    if len(tokens) == 1 and tokens[0] in _PROP_OR_ARMOR_WORDS:
        return True
    if all(t in _PROP_OR_ARMOR_WORDS or t in _INANIMATE_ENTITY_WORDS for t in tokens):
        return True
    return False


_INDIRECT_RELIC_MODIFIERS = re.compile(
    r'\b(?:отцовск\w*|дедовск\w*|материнск\w*|бабушкин\w*|памят\w*\s+о\w*|портрет\w*\s+|реликви\w*\s+|в\s+честь\s+|'
    r'father\'s|mother\'s|grandfather\'s|grandmother\'s|memory\s+of|in\s+memory\s+of|portrait\s+of)\b',
    re.IGNORECASE
)


def _is_indirect_object_mention(name: str, story: str, evidence: Optional[List[Dict[str, Any]]] = None) -> bool:
    """True when entity is mentioned only as a possessive attribute or background modifier of an object/prop/relic (e.g. отцовский меч)."""
    clean_name = strip_combining_accents(str(name or '')).casefold().strip()
    if not clean_name:
        return False

    quotes = [ev.get('quote', '') for ev in (evidence or [])]
    if not quotes and story:
        quotes = [story]

    all_text = ' '.join(quotes).casefold()
    has_physical_action = any(_character_performs_action(clean_name, q) for q in quotes)
    if has_physical_action:
        return False

    if _INDIRECT_RELIC_MODIFIERS.search(all_text):
        if re.search(r'\b(?:отец|папа|мать|мама|дед|дедушка|бабушка|предок|father|mother|grandfather|grandmother|ancestor)\b', clean_name):
            return True

    return False


def is_purely_sentence_initial(name: str, story: str) -> bool:
    """Check if a word is capitalized purely due to being at the start of a sentence or quote."""
    clean_name = strip_combining_accents(name).strip()
    if not clean_name:
        return True
    if is_recognized_role(clean_name) or is_recognized_animal_or_creature(clean_name):
        return False

    norm_lower = clean_name.casefold()
    if (is_temporal_entity(clean_name) or norm_lower in _ADVERB_AND_TIME_WORDS or
            norm_lower in _NON_CHARACTER_GRAMMAR_WORDS or is_grammatical_non_character(clean_name) or
            norm_lower in _PROP_OR_ARMOR_WORDS or is_prop_or_armor_entity(clean_name) or
            norm_lower in _COLLECTIVE_WORDS or is_collective_entity(clean_name) or
            norm_lower in _INANIMATE_ENTITY_WORDS or is_inanimate_or_location_entity(clean_name) or
            is_bare_generic_agent_noun(clean_name)):
        return True

    has_title = bool(re.search(
        r'\b(?:[Кк]апитан|[Ии]нспектор|[Дд]етектив|[Дд]октор|[Вв]рач|[Оо]фицер|[Сс]мотритель|[Мм]астер|[Кк]упец|[Сс]тароста|Captain|Inspector|Detective|Doctor|Officer|Keeper|Master)\s+' +
        re.escape(clean_name) + r'\b', story, re.IGNORECASE))
    if has_title:
        return False

    clean_story = strip_combining_accents(story)
    escaped = re.escape(clean_name)
    lowered = clean_name.lower()

    has_lowercase = bool(re.search(r'(?<![A-Za-zА-ЯЁа-яё])' + re.escape(lowered) + r'(?![A-Za-zА-ЯЁа-яё])', clean_story))
    midsentence_cap = re.search(r'(?:[a-zа-яё,;]\s+|[a-zа-яё]\s+[—-]\s+)\b' + escaped + r'\b', clean_story)
    if has_lowercase and not midsentence_cap:
        return True

    occurrences = list(re.finditer(r'\b' + escaped + r'\b', clean_story))
    if not occurrences:
        stem = stem_ru_word(lowered)
        if len(stem) >= 3:
            occurrences = list(re.finditer(r'\b' + re.escape(stem) + r'[а-яё]*\b', clean_story, re.IGNORECASE))
    if not occurrences:
        return False

    all_sentence_initial = True
    for m in occurrences:
        preceding = clean_story[max(0, m.start() - 10):m.start()]
        if not re.search(r'''(?:^|[.!?…\n]\s*|[«"'\(\[\{]\s*)$''', preceding.strip()):
            all_sentence_initial = False
            break

    if all_sentence_initial:
        if (is_grammatical_non_character(clean_name) or norm_lower in _ADVERB_AND_TIME_WORDS or
                is_temporal_entity(clean_name) or is_bare_generic_agent_noun(clean_name) or
                is_prop_or_armor_entity(clean_name) or is_collective_entity(clean_name) or
                is_inanimate_or_location_entity(clean_name)):
            return True
        if lowered in _TRANSLIT_PAIRS or any(lowered == v for v in _TRANSLIT_PAIRS.values()):
            return False
        # If it has 2+ occurrences across sentences and no lowercase instances, it is a recurring proper name
        if len(occurrences) >= 2 and not has_lowercase:
            return False
        # If candidate performs an action, has no lowercase instances, and is NOT a grammatical/stop/inanimate/generic word
        if not has_lowercase and not is_grammatical_non_character(clean_name) and not is_inanimate_or_location_entity(clean_name) and not is_bare_generic_agent_noun(clean_name):
            evidence = find_evidence_in_story(clean_story, clean_name)
            has_action = any(_character_performs_action(clean_name, ev.get('quote', '')) for ev in evidence)
            if has_action:
                return False
        if not midsentence_cap:
            return True

    return False


_ACTION_VERBS = re.compile(
    r'\b(?:'
    # Russian action verbs (past, present, infinitive)
    r'прибыл\w*|прискакал\w*|приехал\w*|приш[её]л\w*|пришла|вош[её]л\w*|вошла|входит\w*|выш[её]л\w*|подош[её]л\w*|подошла|подходит\w*|отош[её]л\w*|сош[её]л\w*|'
    r'уш[её]л\w*|ушла|ушли|пош[её]л\w*|пошла|пошли|направил\w*|продолжил\w*|нанял\w*|отправил\w*|встретил\w*|'
    r'стоит\w*|стоял\w*|сидит\w*|сидел\w*|леж[аи]\w*|ходит\w*|ид[её]т\w*|ш[её]л\w*|шла|шли|беж[аи]\w*|смотрел\w*|смотрит\w*|посмотрел\w*|глядел\w*|наблюдал\w*|наблюда\w*|видел\w*|'
    r'сказал\w*|сказала|говорит\w*|говорил\w*|ответил\w*|ответила|отвеча\w*|спросил\w*|воскликнул\w*|произн[её]с\w*|шепнул\w*|закричал\w*|позвал\w*|обраща\w*|обратил\w*|'
    r'взял\w*|взяла|бер[её]т\w*|отдал\w*|передал\w*|передала|переда[её]т\w*|вручил\w*|прин[её]с\w*|унес\w*|унёс\w*|положил\w*|поднял\w*|бросил\w*|держит\w*|держал\w*|'
    r'показал\w*|показала|продал\w*|перевязал\w*|предупредил\w*|'
    r'чинил\w*|вырезал\w*|шил\w*|ковал\w*|делал\w*|создал\w*|лепил\w*|молол\w*|п[её]к\w*|сушил\w*|'
    r'проверял\w*|проверил\w*|осматривал\w*|осматрива\w*|осмотрел\w*|настраивал\w*|чертил\w*|сверял\w*|смешивал\w*|готовил\w*|'
    r'удержал\w*|спас\w*|поблагодарил\w*|пожал\w*|протянул\w*|кивнул\w*|улыбнул\w*|вспомнил\w*|'
    r'испытал\w*|работал\w*|трудил\w*|пас\w*|разбирал\w*|открыва\w*|открыл\w*|закрыва\w*|закрыл\w*|доставил\w*|слушал\w*|ждал\w*|жд[её]т\w*|шагает\w*|'
    r'зна[ею]\w*|помн\w*|махнул\w*|машет\w*|скрыл\w*|почувствова\w*|прекратил\w*|заметил\w*|узнал\w*|осмотрел\w*|оглядел\w*|взглянул\w*|'
    # English action verbs
    r'arrive\w*|enter\w*|depart\w*|approach\w*|step\w*|stand\w*|stood|sit\w*|sat|walk\w*|run\w*|ran|watch\w*|look\w*|observ\w*|saw|see\w*|'
    r'said|say\w*|ask\w*|repli\w*|shout\w*|whisper\w*|call\w*|spoke|speak\w*|told|tell\w*|thank\w*|'
    r'took|take\w*|gave|give\w*|hand\w*|deliver\w*|brought|bring\w*|held|hold\w*|pick\w*|place\w*|put\w*|rais\w*|drop\w*|threw|throw\w*|carri\w*|'
    r'show\w*|showed|shown|left|leave\w*|depart\w*|continu\w*|hire\w*|warn\w*|sold|sell\w*|bandag\w*|'
    r'repair\w*|fix\w*|built|build\w*|craft\w*|carv\w*|sew\w*|forg\w*|bake\w*|mix\w*|prepar\w*|tend\w*|signal\w*|'
    r'check\w*|inspect\w*|examin\w*|calibrat\w*|test\w*|verif\w*|adjust\w*|guid\w*|save\w*|rescu\w*|'
    r'greet\w*|smile\w*|nod\w*|shook|shake\w*|remember\w*|work\w*|help\w*|open\w*|close\w*|turn\w*|wait\w*|hear\w*|heard|listen\w*|know\w*|wave\w*|wav\w*'
    r')\b',
    re.IGNORECASE
)


def _character_performs_action(name: str, quote: str, aliases: Optional[List[str]] = None) -> bool:
    """Verify that candidate name or its aliases/terms are proximate to an action verb in the quote."""
    if not quote or not name:
        return False
    if is_grammatical_non_character(name):
        return False
    all_names = [name] + (aliases or [])
    terms = get_search_terms(name, all_names)
    stems = set()
    for t in terms:
        s = stem_ru_word(strip_combining_accents(t).lower())
        if s and len(s) >= 2:
            stems.add(s)
    if not stems:
        return False
    tokens = [t for t in re.split(r'(\w+)', strip_combining_accents(quote)) if t.strip()]
    name_indices = [
        i for i, t in enumerate(tokens)
        if any(t.lower().startswith(s) or s.startswith(t.lower()) for s in stems)
    ]
    for idx in name_indices:
        window_start = max(0, idx - 4)
        window_end = min(len(tokens), idx + 5)
        window_text = ' '.join(tokens[window_start:window_end])
        if _ACTION_VERBS.search(window_text):
            return True
    return False


_TOPONYM_SUFFIXES = re.compile(
    r'(?:вуд|wood|виль|вилл|ville|бург|burg|burgh|таун|town|форд|ford|порт|port|'
    r'ленд|лэнд|land|берг|berg|дейл|делл|dale|dell|сити|city|дорф|dorf|штадт|stadt|'
    r'поль|pol|град|grad|горск|gorsk)$',
    re.IGNORECASE
)

_LOCATION_CATEGORY_NOUNS = (
    r'город(?:а|у|ом|е)?|городок(?:а|у|ом|е)?|пос[её]лок(?:а|у|ом|е)?|деревн(?:я|и|е|ю|ей)|село(?:м|е)?|'
    r'округ(?:а|у|ом|е)?|штат(?:а|у|ом|е)?|район(?:а|у|ом|е)?|улиц(?:а|ы|е|у|ей)|проспект(?:а|у|ом|е)?|'
    r'лес(?:а|у|ом|е)?|парк(?:а|у|ом|е)?|озер(?:о|а|у|ом|е)|рек(?:а|и|е|у|ой)|гор(?:а|ы|е|у|ой)|долин(?:а|ы|е|у|ой)|'
    r'замок(?:а|у|ом|е)?|отел[ьяюем]|бар(?:а|у|ом|е)?|таверн(?:а|ы|е|у|ой)|станци(?:я|и|е|ю|ей)|лагер[ьяюем]|'
    r'остров(?:а|у|ом|е)?|маяк(?:а|у|ом|е)?|корабл[ьяюем]|судн[оауом]|побережь[еяюем]|'
    r'city|town|village|county|district|state|street|avenue|forest|woods|park|lake|river|mountain|valley|'
    r'castle|hotel|bar|tavern|station|camp|island|lighthouse|ship|boat|vessel|coast'
)

_LOCATION_DIRECTION_PHRASES = (
    r'приехал[аи]?|прибыл[аи]?|прискакал[аи]?|направил(?:ся|ась|ись)|отправил(?:ся|ась|ись)|'
    r'уехал[аи]?|поехал[аи]?|въехал[аи]?|выехал[аи]?|добрал(?:ся|ась|ись)|вернул(?:ся|ась|ись)|'
    r'жил[аи]?|проживал[аи]?|находил(?:ся|ась|ись)|расположен[аоы]?|поселил(?:ся|ась|ись)|'
    r'остановил(?:ся|ась|ись)|вырос[лаи]?|родил(?:ся|ась|ись)|дорог[аеу]\s+в|путь\s+в|'
    r'жител[еяий]\s+|жители\s+|населени[ея]\s+|окрестност[ьиея]\s+|'
    r'arrived|came|travelled|traveled|drove|headed|moved|lived|stayed|located|settled|grew\s+up|road\s+to|way\s+to'
)


def is_inanimate_or_location_entity(name: str, evidence: Optional[List[Dict[str, Any]]] = None) -> bool:
    """Reject objects, places, toponyms and title fragments that cannot be cast members."""
    normalized = ' '.join(str(name or '').casefold().split())
    if not normalized:
        return True
    tokens = re.findall(r"[\wё-]+", normalized)
    if tokens and tokens[-1] in _NATURAL_LOCATION_NOUNS:
        return True
    if tokens and all(token in _INANIMATE_ENTITY_WORDS for token in tokens):
        return True
    if tokens and all(token in _TEMPORAL_ENTITY_WORDS for token in tokens):
        return True
    if is_prop_or_armor_entity(name):
        return True
    if is_collective_entity(name):
        return True
    if normalized in {'последняя', 'последний', 'старый', 'новый', 'забытый', 'таинственный'}:
        return True

    clean_name = name.strip()
    escaped = re.escape(clean_name)
    stem = stem_ru_word(normalized)
    stem_escaped = re.escape(stem) if len(stem) >= 3 else escaped

    quotes = [ev.get('quote', '') for ev in (evidence or []) if isinstance(ev, dict) and ev.get('quote')]
    all_quotes_text = ' '.join(quotes)

    # 1. Check place category prefixes/suffixes in evidence quotes (e.g. "город Нортвуд", "в городке Нортвуд", "остров Нортвуд")
    category_pattern = re.compile(
        r'\b(?i:(?:' + _LOCATION_CATEGORY_NOUNS + r'))\s+(?i:(?:в\s+|на\s+|из\s+))?'
        r'(?:«\s*)?(?:[А-ЯЁA-Z][\wё-]+\s+){0,2}(?i:(?:' + escaped + r'|' + stem_escaped + r'[а-яё]*))\b|'
        r'\b(?i:(?:' + escaped + r'|' + stem_escaped + r'[а-яё]*))\s+(?i:(?:' + _LOCATION_CATEGORY_NOUNS + r'))\b'
    )
    if quotes and any(category_pattern.search(q) for q in quotes):
        return True

    # 2. Check motion/direction verb + preposition + candidate (e.g. "приехал в Нортвуд", "отправился в Нортвуд")
    direction_pattern = re.compile(
        r'\b(?:' + _LOCATION_DIRECTION_PHRASES + r')\s+(?:в|во|из|к|ко|до|под|около|возле|вблизи|посреди|вокруг|через|на\s+улицах|in|into|to|from|near|inside|around|through)\s+(?:город\w*\s+|деревн\w*\s+|пос[её]л\w*\s+)?(?:«\s*)?(?:' + escaped + r'|' + stem_escaped + r'[а-яё]*)\b',
        re.IGNORECASE
    )
    if quotes and any(direction_pattern.search(q) for q in quotes):
        has_direct_speech = any(re.search(r'\b(?:' + escaped + r')\s+(?:сказал|ответил|спросил|крикнул|said|asked|replied)\b', q, re.I) for q in quotes)
        if not has_direct_speech:
            return True

    # 3. Check toponym suffixes (e.g. -wood, -вуд, -ville, -виль, -burg, -бург, -town, -таун, -ford, -форд, -port, -порт)
    if _TOPONYM_SUFFIXES.search(normalized):
        # Toponym suffix with locative prepositions in text
        locative_prep = re.compile(
            r'\b(?:в|во|из|к|ко|до|под|около|возле|вблизи|через|in|into|to|from|near)\s+(?:«\s*)?(?:' + escaped + r'|' + stem_escaped + r'[а-яё]*)\b',
            re.IGNORECASE
        )
        if not quotes or any(locative_prep.search(q) for q in quotes) or not any(_character_performs_action(clean_name, q) for q in quotes):
            return True

    return False


# Photo markers: contexts where a person exists solely in an image/relic
_PHOTO_PATTERNS = [
    re.compile(r'(?:на\s+(?:старой\s+)?фотографи[иея]|на\s+снимк[еа]|в\s+рамк[еа]|на\s+портрет[еа]|в\s+медальон[еа]|изображен[аоы]?\s+на\s+фото|фотографи[яи]\s+[а-яё]+)', re.IGNORECASE),
    re.compile(r'(?:(?:framed\s+)?photo(?:graph)?\s+of|in\s+(?:the\s+|a\s+)?(?:photograph|photo|picture|frame|locket)|depicted\s+(?:on|in)\s+the\s+photo|portrait\s+on\s+the\s+wall|picture\s+of)', re.IGNORECASE),
]

# Deceased / memory markers: contexts where person exists only in past memories
_DECEASED_PATTERNS = [
    re.compile(r'\b(?:погиб[а-я]*|умер[а-я]*|покойн[а-я]+|была\s+жива|вспоминает\s+о\s+погибш|память\s+об?|мёртв[а-я]*|deceased|passed\s+away|late\s+(?:wife|husband|mother|father)|died(?:\s+years\s+ago)?|in\s+(?:his|her)\s+memory)\b', re.IGNORECASE),
]

# Phone call / remote interlocutor markers
_PHONE_CALL_PATTERNS = [
    re.compile(r'(?:звонит\s+по\s+телефону|разговаривает\s+по\s+телефону|голос\s+в\s+трубке|собеседник\s+на\s+том\s+конце|диспетчер\s+в\s+наушниках|отвечает\s+по\s+рации)', re.IGNORECASE),
    re.compile(r'(?:calls\s+on\s+the\s+phone|speaking\s+on\s+the\s+phone|voice\s+on\s+the\s+phone|voice\s+from\s+the\s+receiver|on\s+the\s+other\s+end\s+of\s+the\s+line)', re.IGNORECASE),
]

# Pronoun patterns
_PRONOUNS_MALE = {'он', 'его', 'ему', 'им', 'нём', 'нем', 'he', 'him', 'his'}
_PRONOUNS_FEMALE = {'она', 'её', 'ее', 'ей', 'ею', 'ней', 'she', 'her', 'hers'}
_NON_CHARACTER_PRONOUNS = _PRONOUNS_MALE | _PRONOUNS_FEMALE | {'оно', 'они', 'их', 'ими', 'you', 'your', 'yours', 'we', 'us', 'our'}
_DOMAIN_OR_URL = re.compile(r'^(?:(?:https?://|www\.)\S+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?)$', re.IGNORECASE)
_SOURCE_PREFIX = re.compile(r'^\s*(?:source|источник|credit(?:s)?|via)\s*[:—-]', re.IGNORECASE)


def _looks_like_attribution(name: str, aliases: Optional[List[str]], quote: str) -> bool:
    """Recognise a citation by its form, without maintaining a publisher blacklist."""
    lines = [line.strip() for line in str(quote or '').splitlines() if line.strip()]
    if not lines:
        return False
    label = lines[0]
    if _DOMAIN_OR_URL.fullmatch(label) or _SOURCE_PREFIX.match(label):
        return True
    normalized_names = {' '.join(str(value).casefold().split()) for value in [name] + list(aliases or []) if value}
    normalized_label = ' '.join(label.casefold().split())
    words = re.findall(r"[\wё-]+", label, re.UNICODE)
    return bool(
        len(lines) > 1 and 1 <= len(words) <= 5 and label[-1:] not in '.!?…' and
        any(normalized_label == candidate or normalized_label.startswith(candidate + ' ')
            for candidate in normalized_names)
    )


def is_noncharacter_reference(name: str, aliases: Optional[List[str]] = None,
                              evidence: Optional[List[Dict[str, Any]]] = None,
                              story: str = '', description: str = '') -> bool:
    """Reject pronouns, citations and an invented first-person viewer from cast."""
    normalized = ' '.join(str(name or '').casefold().split())
    if not normalized or normalized in _NON_CHARACTER_PRONOUNS or _DOMAIN_OR_URL.fullmatch(normalized):
        return True
    if is_grammatical_non_character(name):
        return True
    usable_evidence = [item for item in (evidence or []) if isinstance(item, dict)]
    if usable_evidence and all(_looks_like_attribution(name, aliases, item.get('quote', '')) for item in usable_evidence):
        return True
    terms = get_search_terms(name, aliases)
    has_named_evidence = any(
        re.search(r'\b' + re.escape(stem_ru_word(term.casefold())) + r'[a-zа-яё]*\b', story, re.IGNORECASE)
        for term in terms if len(stem_ru_word(term.casefold())) > 1
    )
    quotes = [str(item.get('quote', '')).casefold() for item in (evidence or []) if isinstance(item, dict)]
    has_viewer_only_evidence = bool(quotes) and all(
        re.search(r'\b(?:вы|ваш\w*|вас|вам|you|your)\b', quote, re.IGNORECASE)
        for quote in quotes
    )
    identity_text = f'{name} {description}'.casefold()
    is_viewpoint_role = bool(re.search(
        r'\b(?:viewer|narrator|observer|diver|reader|audience|first[- ]person|pov|'
        r'зрител\w*|рассказчик\w*|наблюдател\w*|ныряльщик\w*|от\s+первого\s+лица)\b', identity_text,
        re.IGNORECASE))
    return has_viewer_only_evidence and not has_named_evidence and is_viewpoint_role


def has_canonical_story_anchor(name: str, story: str, aliases: Optional[List[str]] = None) -> bool:
    """Require a permanent human identity to be named by the source itself."""
    canonical = ' '.join(str(name or '').casefold().split())
    if not canonical:
        return False
    candidates = {canonical}
    if len(re.findall(r"[\wё]+", canonical)) == 1:
        candidates.update(term.casefold() for term in get_search_terms(canonical, []))
    for candidate in candidates:
        stem = stem_ru_word(candidate)
        if len(stem) < 2:
            continue
        if re.search(r'(?<![a-zа-яё])' + re.escape(stem) + r'[a-zа-яё]*(?![a-zа-яё])', str(story), re.IGNORECASE):
            return True
    for alias in aliases or []:
        phrase = ' '.join(str(alias or '').casefold().split())
        if len(re.findall(r"[\wё]+", phrase)) < 2:
            continue
        if re.search(r'(?<![a-zа-яё])' + re.escape(phrase) + r'(?![a-zа-яё])', str(story), re.IGNORECASE):
            return True
    return False



def compute_story_hash(story: str) -> str:
    """Deterministic hash of story text."""
    normalized = ' '.join(str(story or '').strip().split())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def slugify_id(name: str) -> str:
    """Generate a clean deterministic ID for a character name."""
    cleaned = re.sub(r'[^\w\s-]', '', name.lower()).strip()
    slug = re.sub(r'[\s_-]+', '_', cleaned)
    translit_map = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
    }
    slug_ascii = ''.join(translit_map.get(ch, ch) for ch in slug)
    slug_ascii = re.sub(r'[^a-z0-9_]', '', slug_ascii).strip('_')
    return f"char_{slug_ascii or 'person'}"


def stem_ru_word(word: str) -> str:
    """Stem Russian words by removing common inflectional suffixes."""
    w = word.lower()
    for end in _RU_NAME_ENDINGS:
        if w.endswith(end) and len(w) - len(end) >= 3:
            return w[:-len(end)]
    return w


def _capitalize_title_name(s: str) -> str:
    """Capitalize the first letter of a character name or role phrase, preserving proper noun capitalization."""
    parts = str(s or '').split()
    if not parts:
        return ''
    _LOWER_CONNECTORS = {'в', 'во', 'на', 'с', 'со', 'у', 'к', 'ко', 'по', 'из', 'за', 'о', 'об', 'обо', 'без', 'под', 'над', 'при', 'of', 'in', 'on', 'with', 'the', 'a', 'an', 'at', 'by', 'for', 'from', 'to'}

    res = [parts[0].capitalize()]
    for i, p in enumerate(parts[1:], 1):
        p_low = p.lower()
        if p_low in _LOWER_CONNECTORS:
            res.append(p_low)
        elif p[0].isupper() and stem_ru_word(p_low) not in _AGENT_NOUN_STEMS:
            res.append(p.capitalize())
        elif i == 1 and parts[0].capitalize() in {'Captain', 'Inspector', 'Detective', 'Doctor', 'Officer', 'Капитан', 'Инспектор', 'Детектив', 'Доктор', 'Врач', 'Офицер'}:
            res.append(p.capitalize())
        else:
            res.append(p_low)
    return ' '.join(res)


def _match_case_pattern(source: str, target: str) -> str:
    """Match the capitalization pattern of source onto target."""
    if not source or not target:
        return target
    if source[0].isupper():
        return target.capitalize()
    return target.casefold()


def _lemmatize_single_ru_token(word: str, story: str = '') -> str:
    """Normalize a single Russian noun/adjective token to base nominative form using generic morphology and story anchoring."""
    w_clean = strip_combining_accents(word).strip()
    if not w_clean:
        return ''
    w_low = w_clean.casefold()

    def story_has(candidate: str) -> bool:
        return bool(story and re.search(r'\b' + re.escape(candidate) + r'\b', story, re.IGNORECASE))

    # 1. Fleeting vowels in masculine agent nouns: -ец vs -ца/-цу/-цем/-цом (e.g. гонца -> гонец, торговца -> торговец, кузнеца -> кузнец, молодца -> молодец)
    if re.search(r'([бвгджзклмнпрстфхцчшщ])ц[ауеом]$', w_low) and len(w_low) >= 4:
        stem_before_c = w_low[:-2]
        cand_nom = stem_before_c + 'ец'
        if not story or re.search(r'\b' + re.escape(cand_nom) + r'\b', story, re.IGNORECASE):
            return _match_case_pattern(w_clean, cand_nom)

    # 2. Multi-letter adjective inflections: -ого, -его, -ому, -ему, -ым, -им, -ую, -юю, -ые, -ие, -ых, -их, -ыми, -ими
    _ADJ_ENDINGS_MAP = [
        (('ого', 'его', 'ому', 'ему', 'ым', 'им'), 'ый'),
        (('ую', 'юю'), 'ая'),
        (('ые', 'ие', 'ых', 'их', 'ыми', 'ими'), 'ый'),
    ]
    for endings, default_nom in _ADJ_ENDINGS_MAP:
        for end in endings:
            if w_low.endswith(end) and len(w_low) - len(end) >= 3:
                stem = w_low[:-len(end)]
                candidates = [stem + 'ый', stem + 'ий', stem + 'ой', stem + 'ая', stem + 'яя']
                if story:
                    for cand in candidates:
                        if story_has(cand):
                            return _match_case_pattern(w_clean, cand)
                    if w_clean[:1].isupper() and story_has(w_low):
                        return w_clean
                if end in ('ую', 'юю'):
                    return _match_case_pattern(w_clean, stem + ('яя' if stem.endswith(('н', 'ж', 'ш', 'щ', 'ч')) and end == 'юю' else 'ая'))
                if stem.endswith(('г', 'к', 'х', 'ж', 'ш', 'щ', 'ч')):
                    return _match_case_pattern(w_clean, stem + 'ий')
                return _match_case_pattern(w_clean, stem + default_nom)

    # 3. Noun inflections (Instrumental, Dative, Locative, Genitive, Accusative):
    # -ами, -ями, -ом, -ем, -ам, -ям, -ах, -ях, -у, -ю, -е, -и, -ы
    _NOUN_INFLECTION_SUFFIXES = ('ами', 'ями', 'ом', 'ем', 'ам', 'ям', 'ах', 'ях', 'у', 'ю', 'е', 'и', 'ы')
    for suf in _NOUN_INFLECTION_SUFFIXES:
        min_stem = 2 if w_clean[:1].isupper() and story else 3
        if w_low.endswith(suf) and len(w_low) - len(suf) >= min_stem:
            stem = w_low[:-len(suf)]
            candidates = [stem, stem + 'а', stem + 'я', stem + 'ь', stem + 'й']
            if story:
                for cand in candidates:
                    if story_has(cand):
                        return _match_case_pattern(w_clean, cand)
                # Oblique case endings (-ом, -ем, -ами, -ями, -ах, -ях, -ам, -ям) are never nominative base forms
                if suf not in ('ом', 'ем', 'ам', 'ям', 'ами', 'ями', 'ах', 'ях'):
                    if story_has(w_low):
                        return w_clean
            return _match_case_pattern(w_clean, stem)

    # 4. Genitive / Accusative in -а / -я (e.g. Мартина -> Мартин, пастуха -> пастух, лекаря -> лекарь, рыбака -> рыбак, плотника -> плотник)
    if w_low.endswith('а') and len(w_low) >= 4:
        stem = w_low[:-1]
        if _is_distinct_gender_lemma_pair(stem, w_low):
            return _match_case_pattern(w_clean, w_low)
        candidates = [stem, stem + 'а']
        if story:
            if re.search(r'\b' + re.escape(stem) + r'\b', story, re.IGNORECASE):
                return _match_case_pattern(w_clean, stem)
            if re.search(r'\b' + re.escape(w_low) + r'\b', story, re.IGNORECASE):
                return _match_case_pattern(w_clean, w_low)
        else:
            _RU_CONSONANTS = set('бвгджзйклмнпрстфхцчшщ')
            if stem[-1] in _RU_CONSONANTS and w_low not in {'ворона', 'сорока', 'собака', 'лиса', 'пантера'}:
                return _match_case_pattern(w_clean, stem)

    if w_low.endswith('я') and len(w_low) >= 4:
        stem = w_low[:-1]
        candidates = [stem + 'й', stem + 'ь', stem, stem + 'я']
        if story:
            for cand in candidates:
                if re.search(r'\b' + re.escape(cand) + r'\b', story, re.IGNORECASE):
                    return _match_case_pattern(w_clean, cand)

    return w_clean


def normalize_entity_name(name: str, story: str = '') -> str:
    """Safely normalize an entity name or multi-word descriptive role to canonical nominative form.

    Uses generic Russian morphological inflection rules and anchors against exact forms
    found in the source narrative, without maintaining hardcoded story-specific lookup tables.
    """
    cleaned = ' '.join(strip_combining_accents(str(name or '')).split())
    if not cleaned:
        return ''

    # Preserve Latin script names as-is
    if re.fullmatch(r'[A-Za-z\s\'-]+', cleaned):
        return cleaned

    # Multi-word descriptive phrase normalization
    words = cleaned.split()
    if len(words) > 1:
        _PREPOSITIONS = {'в', 'во', 'на', 'с', 'со', 'у', 'к', 'ко', 'по', 'из', 'за', 'о', 'об', 'обо', 'без', 'под', 'над', 'при', 'in', 'on', 'with', 'at', 'by', 'under', 'from'}
        prep_idx = None
        for i, w in enumerate(words):
            if w.lower() in _PREPOSITIONS:
                prep_idx = i
                break

        head_words = words[:prep_idx] if prep_idx is not None else words
        tail_words = words[prep_idx:] if prep_idx is not None else []

        def normalize_head_case(words_to_normalize):
            normalized = [_lemmatize_single_ru_token(w, story) for w in words_to_normalize]
            # If a capitalized first name was demonstrably converted from an
            # oblique masculine form (Максима -> Максим), the following
            # capitalized surname shares that case (Орлова -> Орлов).
            if len(words_to_normalize) < 2:
                return normalized
            first_raw = words_to_normalize[0]
            first_nom = normalized[0]
            masculine_pair_case = (
                first_raw[:1].isupper() and first_raw.casefold().endswith(('а', 'я')) and
                first_nom.casefold() != first_raw.casefold() and
                not first_nom.casefold().endswith(('а', 'я'))
            )
            if masculine_pair_case:
                for index in range(1, len(words_to_normalize)):
                    raw_word = words_to_normalize[index]
                    stem = raw_word[:-1]
                    if (raw_word[:1].isupper() and raw_word.casefold().endswith('а') and
                            len(stem) >= 3 and stem[-1].casefold() in 'бвгджзйклмнпрстфхцчшщ'):
                        normalized[index] = _match_case_pattern(raw_word, stem.casefold())
            return normalized

        normalized_head = normalize_head_case(head_words)
        candidate_nom = ' '.join(normalized_head + tail_words)

        if story:
            stems = [stem_ru_word(hw.lower()) for hw in head_words if len(stem_ru_word(hw.lower())) >= 3]
            if stems:
                tail_pat = r'\s+' + re.escape(' '.join(tail_words)) if tail_words else ''
                pat_str = r'\b' + r'\s+'.join(r'[А-ЯЁа-яё]*' + re.escape(s) + r'[а-яё]*' for s in stems) + tail_pat + r'\b'
                match = re.search(pat_str, story, re.IGNORECASE)
                if match:
                    matched_text = match.group(0).strip()
                    m_words = matched_text.split()
                    m_head = m_words[:prep_idx] if prep_idx is not None else m_words
                    m_tail = m_words[prep_idx:] if prep_idx is not None else []
                    m_norm = ' '.join(normalize_head_case(m_head) + m_tail)
                    return _capitalize_title_name(m_norm)

        return _capitalize_title_name(candidate_nom)

    # Single-word reduction for Russian nouns/names
    return _capitalize_title_name(_lemmatize_single_ru_token(cleaned, story))


def lemmatize_ru_name(name: str, story: str = '') -> str:
    """Normalize Russian inflected name or multi-word role to base nominative form while preserving the script/language."""
    return normalize_entity_name(name, story)


def get_search_terms(name: str, aliases: Optional[List[str]] = None) -> List[str]:
    """Expand name and aliases with role synonyms and translations."""
    terms = set([name] + (aliases or []))
    lower = name.strip().lower()
    for role, syns in _COMMON_ROLES.items():
        if lower == role or lower in syns:
            terms.update(syns)
    if lower in _TRANSLIT_PAIRS:
        terms.add(_TRANSLIT_PAIRS[lower])
    for k, v in _TRANSLIT_PAIRS.items():
        if lower == v:
            terms.add(k)
    return list(terms)


def _is_distinct_gender_lemma_pair(a: str, b: str) -> bool:
    """Distinct grammatical genders / distinct noun lemmas (e.g. ворон vs ворона, raven vs crow, Alexander vs Alexandra)
    must not be merged, while Russian inflected noun case endings (Мартин vs Мартина) must remain unified."""
    f1, f2 = strip_combining_accents(str(a or '')).strip().casefold(), strip_combining_accents(str(b or '')).strip().casefold()
    if not f1 or not f2:
        return False

    # 1. Exact known distinct gender/species pairs
    if (f1, f2) in _KNOWN_DISTINCT_GENDER_PAIRS or (f2, f1) in _KNOWN_DISTINCT_GENDER_PAIRS:
        return True

    # 2. Transliterated known pairs
    t1 = _TRANSLIT_PAIRS.get(f1, f1)
    t2 = _TRANSLIT_PAIRS.get(f2, f2)
    if (t1, t2) in _KNOWN_DISTINCT_GENDER_PAIRS or (t2, t1) in _KNOWN_DISTINCT_GENDER_PAIRS:
        return True

    # 3. Multi-word composite phrases containing distinct gender components
    w1 = set(re.findall(r'[\wё-]+', f1))
    w2 = set(re.findall(r'[\wё-]+', f2))
    for pair in _KNOWN_DISTINCT_GENDER_PAIRS:
        if pair[0] in w1 and pair[1] in w2:
            return True
        if pair[1] in w1 and pair[0] in w2:
            return True

    return False


_MATCH_STOPWORDS = {
    'в', 'во', 'на', 'с', 'со', 'к', 'ко', 'по', 'из', 'за', 'у', 'о', 'об', 'обо',
    'и', 'а', 'но', 'да', 'же', 'ли', 'бы',
    'the', 'a', 'an', 'in', 'on', 'with', 'at', 'by', 'for', 'of', 'to'
}


def names_match_entity(name1: str, name2: str, story: str = '') -> bool:
    """Check whether two character names/aliases refer to the same entity."""
    f1, f2 = name1.strip().casefold(), name2.strip().casefold()
    if not f1 or not f2:
        return False
    if f1 == f2:
        return True

    if _is_distinct_gender_lemma_pair(f1, f2):
        return False

    # Check lemmatized matches (e.g. Мартин vs Мартина, Королевский гонец vs гонца)
    # Preserve capitalization while lemmatizing: a short Russian proper name
    # such as Ева needs its capital letter to distinguish Еву/Еве as case forms
    # from an arbitrary lowercase noun ending.
    l1 = lemmatize_ru_name(name1, story).casefold()
    l2 = lemmatize_ru_name(name2, story).casefold()
    if l1 and l2:
        if l1 == l2:
            return True
        lw1 = set(w for w in re.split(r'[^\w-]+', l1) if w and w not in _MATCH_STOPWORDS)
        lw2 = set(w for w in re.split(r'[^\w-]+', l2) if w and w not in _MATCH_STOPWORDS)
        if lw1 and lw2 and (lw1.issubset(lw2) or lw2.issubset(lw1)):
            return True

    # Check known transliterations
    if _TRANSLIT_PAIRS.get(f1) == f2 or _TRANSLIT_PAIRS.get(f2) == f1:
        return True

    # Check diminutive name pairs (e.g. Дэниел <-> Дэн, Александр <-> Саша, Томас <-> Том)
    if (f1, f2) in _DIMINUTIVE_NAME_PAIRS or (f2, f1) in _DIMINUTIVE_NAME_PAIRS:
        return True

    # Check known role sets (e.g. 'капитан' <-> 'captain', 'сын' <-> 'son')
    for role, synonyms in _COMMON_ROLES.items():
        s1 = f1 in synonyms or f1 == role
        s2 = f2 in synonyms or f2 == role
        if s1 and s2:
            return True

    w1 = [w for w in re.split(r'[^\w-]+', f1) if w and w not in _MATCH_STOPWORDS]
    w2 = [w for w in re.split(r'[^\w-]+', f2) if w and w not in _MATCH_STOPWORDS]
    if not w1 or not w2:
        return False

    s1, s2 = set(w1), set(w2)
    # Exact token subset, e.g. "Captain" in "Captain Reed"
    if s1.issubset(s2) or s2.issubset(s1):
        return True

    # Stemmed token matching (with transliteration awareness)
    def entity_stem(word):
        first = stem_ru_word(word)
        # Russian surname case can add -а after a nominative surname already
        # ending in -ов/-ев: Орлова -> Орлов -> Орл. Apply the same surname
        # reduction to both the inflected and nominative spellings.
        if word.endswith(('а', 'я')) and first != word:
            return stem_ru_word(first)
        return first

    stems1 = {entity_stem(w) for w in w1} | {entity_stem(_TRANSLIT_PAIRS.get(w, w)) for w in w1}
    stems2 = {entity_stem(w) for w in w2} | {entity_stem(_TRANSLIT_PAIRS.get(w, w)) for w in w2}
    common_stems = stems1 & stems2
    if common_stems:
        valid_stems = [s for s in common_stems if len(s) >= 3 and not any(_is_distinct_gender_lemma_pair(t1, t2) for t1 in w1 for t2 in w2)]
        if valid_stems:
            if len(w1) == 1 and len(w2) == 1:
                return True
            if stems1.issubset(stems2) or stems2.issubset(stems1):
                single = w1[0] if len(w1) == 1 else (w2[0] if len(w2) == 1 else '')
                if not single or not (is_prop_or_armor_entity(single) or is_inanimate_or_location_entity(single)):
                    return True
            # Overlap of multi-word descriptive phrases (e.g. "молодой парень в широком шлеме" vs "молодой солдат в широком шлеме")
            if len(valid_stems) >= 2 and (len(w1) >= 2 or len(w2) >= 2):
                return True

    # Check composite phrase in story text (e.g. story contains "Captain Reed" / "капитан Рид")
    if story and len(f1.split()) == 1 and len(f2.split()) == 1:
        if (stem_ru_word(f1.lower()) in _AGENT_NOUN_STEMS or stem_ru_word(f2.lower()) in _AGENT_NOUN_STEMS):
            story_lower = story.lower()
            if f"{f1} {f2}" in story_lower or f"{f2} {f1}" in story_lower:
                return True

    return False


def find_evidence_in_story(story: str, candidate_name: str, aliases: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Find exact evidence quotes in the story text for a character."""
    evidence = []
    search_terms = set(get_search_terms(candidate_name, aliases))
    seen_spans = set()

    sentences = re.split(r'(?<=[.!?…])\s+', story)
    current_pos = 0

    for sentence in sentences:
        start_idx = story.find(sentence, current_pos)
        if start_idx < 0:
            start_idx = current_pos
        end_idx = start_idx + len(sentence)
        current_pos = end_idx

        sentence_lower = sentence.casefold()
        sentence_clean = strip_combining_accents(sentence).casefold()
        matched = False
        for term in search_terms:
            t_lower = strip_combining_accents(term).casefold()
            stem = stem_ru_word(t_lower)
            pattern = re.compile(r'\b' + re.escape(stem) + r'[a-zа-яё]*\b', re.IGNORECASE)
            if pattern.search(sentence_lower) or pattern.search(sentence_clean):
                matched = True
                break

        if matched:
            span = (start_idx, end_idx)
            if span not in seen_spans:
                seen_spans.add(span)
                evidence.append({
                    'start': start_idx,
                    'end': end_idx,
                    'quote': sentence
                })

    return evidence


_NONHUMAN_CONTEXT_CUE = re.compile(
    r'\b(?:animal|animals|creature|creatures|wildlife|species|mammal|mammals|bird|birds|fish|reptile|reptiles|'
    r'amphibian|amphibians|insect|insects|arachnid|mollusc|mollusk|ungulate|herd|fauna|'
    r'животное|животные|животных|животным|животными|'
    r'вид\s+птиц\w*|вид\s+животн\w*|вид\s+рыб\w*|вид\s+млекопитающ\w*|биологическ\w*\s+вид\w*|'
    r'млекопитающ\w+|птица|птицы|птиц|птицам|птицами|рыба|рыбы|рыб|рыбам|'
    r'пресмыкающ\w+|земноводн\w+|насеком\w+|паукообразн\w+|'
    r'моллюск\w*|головоног\w*|копытн\w+|фауна\w*)\b', re.IGNORECASE)

_TAXONOMIC_SUBJECT_CUE = re.compile(
    r'\b(?:это|is|are)\s+(?P<name>[A-Za-zА-ЯЁа-яё][A-Za-zА-ЯЁа-яё-]{2,60})\s*'
    r'(?:[—–-]|явля[ею]тся\s+|is\s+|are\s+)\s*(?:[A-Za-zА-ЯЁа-яё-]+\s+){0,6}'
    r'(?:млекопитающ\w*|животн\w*|птиц\w*|рыб\w*|пресмыкающ\w*|земноводн\w*|'
    r'насеком\w*|паукообразн\w*|моллюск\w*|головоног\w*|вид\w*\s+птиц\w*|вид\w*\s+животн\w*|'
    r'вид\w*\s+рыб\w*|animal|mammal|bird|fish|reptile|amphibian|insect|arachnid|mollusc|mollusk|cephalopod)\b', re.IGNORECASE)
_TAXONOMIC_SUBJECT_REVERSED_CUE = re.compile(
    r'\b(?P<name>[A-Za-zА-ЯЁа-яё][A-Za-zА-ЯЁа-яё-]{2,60})\s*'
    r'(?:[—–-]|явля[ею]тся\s+|is\s+|are\s+)\s*(?:это\s+)?(?:не\s+)?(?:[A-Za-zА-ЯЁа-яё-]+\s+){0,6}'
    r'(?:млекопитающ\w*|животн\w*|птиц\w*|рыб\w*|пресмыкающ\w*|земноводн\w*|'
    r'насеком\w*|паукообразн\w*|моллюск\w*|головоног\w*|вид\w*\s+птиц\w*|вид\w*\s+животн\w*|'
    r'вид\w*\s+рыб\w*|animal|mammal|bird|fish|reptile|amphibian|insect|arachnid|mollusc|mollusk|cephalopod)\b', re.IGNORECASE)
_HUMAN_CONTEXT_CUE = re.compile(
    r'\b(?:person|people|woman|man|child|viewer|observer|diver|scientist|researcher|doctor|teacher|hunter|guide|'
    r'человек\w*|люди|женщин\w*|мужчин\w*|реб[её]нк\w*|уч[её]н\w*|исследовател\w*|'
    r'врач\w*|учител\w*|охотник\w*|проводник\w*)\b', re.IGNORECASE)


def is_nonhuman_description(description: str) -> bool:
    """Recognise a typed or taxonomically described animal/creature candidate."""
    text = str(description or '')
    explicit_type = re.search(
        r'(?:entity\s*type\s*:\s*(?:animal|creature|non-?human)|'
        r'тип\s*(?:сущности|субъекта)\s*:\s*(?:животн\w*|существ\w*))',
        text, re.IGNORECASE)
    return bool(explicit_type or (_NONHUMAN_CONTEXT_CUE.search(text) and not _HUMAN_CONTEXT_CUE.search(text)))


def can_recover_nonhuman_from_wildlife_story(description: str, story: str) -> bool:
    """Recover an untranslated animal label only from an explicitly wildlife story."""
    text = str(description or '')
    return bool(is_nonhuman_description(text) or (
        _NONHUMAN_CONTEXT_CUE.search(str(story or '')) and not _HUMAN_CONTEXT_CUE.search(text)))


def infer_nonhuman_description(name: str, description: str, evidence: Optional[List[Dict[str, Any]]], story: str) -> str:
    """Mark a fallback cast entry as an animal when its story evidence confirms a non-human entity."""
    description = str(description or '').strip()
    if re.search(r'(?:entity\s*type|тип\s*(?:сущности|субъекта))\s*:', description, re.IGNORECASE):
        return description
    if is_grammatical_non_character(name) or is_inanimate_or_location_entity(name, evidence):
        return description
    if any(stem_ru_word(w) in _AGENT_NOUN_STEMS or is_agent_noun_stem(stem_ru_word(w)) for w in name.lower().split()):
        return description

    clean_story = strip_combining_accents(story)
    clean_name = strip_combining_accents(name)

    # 1. Directly recognized animal, creature, or fantasy entity
    if is_recognized_animal_or_creature(name):
        return f'Entity type: animal; species: {name}. {description or "Recurring non-human subject in the story."}'

    # 2. Backed by taxonomic cue in story text
    for pat in (_TAXONOMIC_SUBJECT_CUE, _TAXONOMIC_SUBJECT_REVERSED_CUE):
        for match in pat.finditer(clean_story):
            if match.group('name').casefold() == clean_name.casefold():
                return f'Entity type: animal; species: {name}. {description or "Recurring non-human subject in the story."}'

    return description


def is_photo_only(evidence: List[Dict[str, Any]], story: str) -> bool:
    """Check if all occurrences of character are strictly inside a photograph."""
    if not evidence:
        return False
    has_real_world_event = False
    for ev in evidence:
        quote = ev.get('quote', '')
        is_photo = any(p.search(quote) for p in _PHOTO_PATTERNS)
        if not is_photo:
            has_real_world_event = True
            break
    return not has_real_world_event


def is_deceased_only(evidence: List[Dict[str, Any]], story: str) -> bool:
    """Check if all occurrences of character exist only as deceased / past memory."""
    if not evidence:
        return False
    has_active_present_event = False
    for ev in evidence:
        quote = ev.get('quote', '')
        is_death_quote = any(p.search(quote) for p in _DECEASED_PATTERNS)
        if not is_death_quote:
            has_active_present_event = True
            break
    return not has_active_present_event


def is_phone_caller_only(evidence: List[Dict[str, Any]], story: str) -> bool:
    """Check if character only exists as a voice on the phone / remote caller."""
    if not evidence:
        return False
    has_in_person_event = False
    for ev in evidence:
        quote = ev.get('quote', '')
        is_phone = any(p.search(quote) for p in _PHONE_CALL_PATTERNS)
        physical_action = re.search(r'\b(?:входит|прибыва[ею]т|стоит|садится|подходит|смотрит|arrives|enters|stands|walks|sits)\b', quote, re.IGNORECASE)
        if physical_action and not is_phone:
            has_in_person_event = True
            break
        if not is_phone:
            has_in_person_event = True
            break
    return not has_in_person_event


def get_story_scenes(story: str, scenes: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Return explicit scenes or segment story text into narrative scene chunks (by paragraphs or sentence clusters)."""
    if scenes and isinstance(scenes, list) and len(scenes) > 0:
        return scenes

    # Multi-paragraph story: segment by paragraphs
    paragraphs = [p.strip() for p in (story or '').split('\n') if p.strip()]
    if len(paragraphs) > 1:
        res = []
        cur_pos = 0
        for i, p in enumerate(paragraphs):
            idx = story.find(p, cur_pos)
            if idx < 0:
                idx = cur_pos
            cur_pos = idx + len(p)
            res.append({
                'scene_id': f"scene_{i}",
                'index': i,
                'source_quote': p,
                'start': idx,
                'end': idx + len(p)
            })
        return res

    # Single-paragraph story: segment by sentences
    sentences = [s.strip() for s in re.split(r'(?<=[.!?…])\s+', story or '') if s.strip()]
    res = []
    cur_pos = 0
    for i, s in enumerate(sentences):
        idx = story.find(s, cur_pos)
        if idx < 0:
            idx = cur_pos
        cur_pos = idx + len(s)
        res.append({
            'scene_id': f"scene_{i}",
            'index': i,
            'source_quote': s,
            'start': idx,
            'end': idx + len(s)
        })
    return res


def get_candidate_scene_indices(cand: Dict[str, Any], story: str, scenes: Optional[List[Dict[str, Any]]] = None) -> List[int]:
    """Find the list of distinct scene/episode indices where candidate appears."""
    name = cand.get('canonical_name', '').strip()
    aliases = cand.get('aliases', [])
    evidence = cand.get('evidence', [])
    terms = get_search_terms(name, aliases)
    story_scenes = get_story_scenes(story, scenes)
    if not story_scenes:
        return []

    is_sentence_level = (len(story_scenes) > 1 and not (scenes and len(scenes) > 0) and len([p.strip() for p in (story or '').split('\n') if p.strip()]) <= 1)

    matched_indices = set()
    for i, sc in enumerate(story_scenes):
        sc_chars = sc.get('characters', []) if isinstance(sc.get('characters'), list) else []
        if any(names_match_entity(name, c, story) or any(names_match_entity(a, c, story) for a in aliases) for c in sc_chars):
            matched_indices.add(i)
            continue

        sq = sc.get('source_quote') or ''
        sq_start = sc.get('start', -1)
        sq_end = sc.get('end', -1)

        matched_ev = False
        for ev in evidence:
            ev_q = ev.get('quote', '')
            ev_start = ev.get('start', -1)
            ev_end = ev.get('end', -1)
            if ev_q and sq and (ev_q in sq or sq in ev_q):
                matched_ev = True
                break
            if ev_start >= 0 and ev_end >= 0 and sq_start >= 0 and sq_end >= 0:
                if not (ev_end <= sq_start or ev_start >= sq_end):
                    matched_ev = True
                    break
        if matched_ev:
            matched_indices.add(i)
            continue

        sq_clean = strip_combining_accents(sq).casefold()
        for term in terms:
            t_stem = stem_ru_word(strip_combining_accents(term).casefold())
            if len(t_stem) >= 2:
                if re.search(r'\b' + re.escape(t_stem) + r'[a-zа-яё]*\b', sq_clean, re.IGNORECASE):
                    matched_indices.add(i)
                    break

    sorted_indices = sorted(matched_indices)
    if not is_sentence_level:
        return sorted_indices

    # Sentence-level clustering for single-paragraph narratives
    if not sorted_indices:
        return []

    clusters = []
    current_cluster = [sorted_indices[0]]
    for idx in sorted_indices[1:]:
        if idx == current_cluster[-1] + 1:
            current_cluster.append(idx)
        else:
            clusters.append(current_cluster)
            current_cluster = [idx]
    clusters.append(current_cluster)

    return [c[0] for c in clusters]


def is_agent_noun_stem(stem: str) -> bool:
    """Check if a word stem represents a human agent or occupational noun."""
    if stem in _AGENT_NOUN_STEMS:
        return True
    if any(stem.endswith(s) for s in ('оператор', 'механик', 'техник', 'инженер', 'строитель', 'operator', 'mechanic', 'technician', 'engineer')):
        return True
    return False


def is_agent_noun_phrase(name: str) -> bool:
    """Check if name is composed solely of agent nouns, adjectives, prepositions, or location/prop nouns (no proper noun)."""
    words = [w for w in re.findall(r'[\wё-]+', str(name or '')) if w]
    if not words:
        return True
    _PREPOSITIONS_AND_STOP = {
        'в', 'во', 'на', 'с', 'со', 'у', 'к', 'ко', 'по', 'из', 'за', 'о', 'об', 'обо', 'без', 'под', 'над', 'при',
        'in', 'on', 'with', 'at', 'by', 'under', 'from', 'the', 'a', 'an', 'of', 'for'
    }
    _LOCATION_AND_PROP_NOUNS = {
        'река', 'реки', 'реке', 'реку', 'рекой', 'реках', 'рекам',
        'таверна', 'таверны', 'таверне', 'таверну', 'таверной',
        'госпиталь', 'госпиталя', 'госпитале', 'госпиталю', 'больница', 'больницы', 'больнице',
        'ворота', 'ворот', 'воротам', 'воротами', 'воротах', 'дверь', 'двери', 'окно', 'окна',
        'рынок', 'рынка', 'рынке', 'базар', 'базара', 'базаре', 'причал', 'причала', 'причале',
        'мост', 'моста', 'мосте', 'дорога', 'дороги', 'дороге', 'путь', 'пути', 'улица', 'улицы',
        'замок', 'замка', 'замке', 'крепость', 'крепости', 'башня', 'башни', 'башне',
        'город', 'города', 'городе', 'деревня', 'деревни', 'деревне', 'село', 'села', 'селе',
        'холм', 'холме', 'горы', 'горах', 'лес', 'лесу', 'леса', 'поле', 'поля',
        'берег', 'берегу', 'берега', 'море', 'моря', 'гавань', 'гавани', 'порт', 'порту',
        'шлем', 'шлема', 'шлеме', 'шлему', 'доспех', 'доспеха', 'меч', 'мече', 'щит', 'щите',
        'капюшон', 'капюшона', 'капюшоне', 'капюшоном', 'капюшоны', 'плащ', 'плаща', 'плаще', 'плащом',
        'куртка', 'куртке', 'куртку', 'пальто', 'мантия', 'халат', 'сапоги', 'перчатки', 'очки', 'маска', 'шапка', 'фуражка', 'повязка',
        'river', 'lake', 'sea', 'ocean', 'shore', 'coast', 'harbor', 'port', 'bay', 'dock', 'docks',
        'tavern', 'inn', 'shop', 'market', 'hospital', 'clinic', 'castle', 'tower', 'gate', 'gates',
        'bridge', 'road', 'street', 'city', 'town', 'village', 'forest', 'mountain', 'mountains', 'hill',
        'helmet', 'armor', 'sword', 'shield', 'cloak', 'hood', 'coat', 'jacket', 'robe', 'boots', 'gloves', 'glasses', 'mask', 'hat', 'cap',
        'station', 'hangar', 'power'
    }
    has_proper_word = False
    for w in words:
        w_low = w.lower()
        if w_low in _PREPOSITIONS_AND_STOP or is_grammatical_non_character(w):
            continue
        stem = stem_ru_word(w_low)
        if stem in _AGENT_NOUN_STEMS or is_agent_noun_stem(stem):
            continue
        if w_low in _LOCATION_AND_PROP_NOUNS or stem in {stem_ru_word(x) for x in _LOCATION_AND_PROP_NOUNS}:
            continue
        if is_inanimate_or_location_entity(w_low) or is_prop_or_armor_entity(w_low) or is_temporal_entity(w_low):
            continue
        if re.search(r'(?:ый|ий|ой|ая|яя|ое|ее|ого|его|ому|ему|ым|им|ом|ем|ую|юю|ые|ие|ых|их|ыми|ими|al|ic|ary|ory|ive|ous|ful|less)$', w_low):
            continue
        has_proper_word = True
        break
    return not has_proper_word


def classify_candidate_role(cand: Dict[str, Any], story: str, scenes: Optional[List[Dict[str, Any]]] = None) -> Tuple[str, str]:
    """Classify a verified candidate into 'key', 'episodic', 'mentioned_only', or 'rejected', using universal narrative metrics.

    Universal Criteria:
    - Recurring active participants across multiple distinct scenes / story events (unique_scenes_count >= 2).
    - Inciting incident catalysts delivering orders/decrees/missions in the opening scene of a multi-scene story.
    - Climax and resolution partners acting or speaking in the final resolution scene of a multi-scene story.
    - Focal wildlife / creature subjects.

    Any single-scene background participant without opening catalyst or closing resolution agency defaults to 'episodic'.
    """
    name = cand.get('canonical_name', '').strip()
    evidence = cand.get('evidence', [])
    desc = cand.get('description', '')

    # 1. Rejection checks
    if is_bare_generic_agent_noun(name):
        return 'rejected', f"'{name}' is a generic human noun without a proper name or descriptive modifier."
    if is_grammatical_non_character(name, desc):
        return 'rejected', f"'{name}' is a grammatical word or adverb, not a story character."
    if is_purely_sentence_initial(name, story):
        return 'rejected', f"'{name}' is an ordinary word capitalized purely at sentence boundary."
    if is_prop_or_armor_entity(name):
        return 'rejected', f"'{name}' is a prop, armor, or equipment item, not a story character."
    if is_collective_entity(name):
        return 'rejected', f"'{name}' is a collective or plural group, not an individual character."
    if is_inanimate_or_location_entity(name, evidence):
        return 'rejected', f"'{name}' is an inanimate object or location."
    if is_noncharacter_reference(name, cand.get('aliases', []), evidence, story, desc):
        return 'rejected', f"'{name}' is a non-character reference or citation."

    # 2. Mentioned-only checks
    if _is_indirect_object_mention(name, story, evidence):
        return 'mentioned_only', f"'{name}' is mentioned only as a background/origin attribute of an object or memory."
    if is_photo_only(evidence, story):
        return 'mentioned_only', f"'{name}' appears only in a photograph/portrait."
    if is_deceased_only(evidence, story):
        return 'mentioned_only', f"'{name}' exists only in past memory / deceased."
    if is_phone_caller_only(evidence, story):
        return 'mentioned_only', f"'{name}' is a remote phone caller without physical presence."

    # 3. Focal wildlife
    if is_recognized_animal_or_creature(name) or is_nonhuman_description(desc):
        return 'key', f"'{name}' is a focal wildlife/creature subject."

    # 4. Universal Narrative Scene & Plot Analysis
    story_scenes = get_story_scenes(story, None)
    total_scenes = max(1, len(story_scenes))
    candidate_story_scene_indices = get_candidate_scene_indices(cand, story, None)
    candidate_explicit_scene_indices = get_candidate_scene_indices(cand, story, scenes) if (scenes and len(scenes) > 0) else []

    distinct_evidence_quotes = {str(ev.get('quote', '')).strip() for ev in evidence
                                if isinstance(ev, dict) and str(ev.get('quote', '')).strip()}
    proper_identity_evidence = 0 if is_agent_noun_phrase(name) else min(2, len(distinct_evidence_quotes))
    unique_scenes_count = max(
        len(candidate_story_scene_indices),
        len(candidate_explicit_scene_indices),
        proper_identity_evidence,
    )
    all_scene_indices = sorted(set(candidate_story_scene_indices + candidate_explicit_scene_indices))
    quotes = [ev.get('quote', '') for ev in evidence if isinstance(ev, dict) and ev.get('quote')]

    def nonvisual_evidence(quote: str) -> bool:
        """Whether this occurrence represents the person only through media,
        memory, speech, or an explicit absence rather than a body in frame."""
        q = strip_combining_accents(str(quote or '')).casefold()
        if not q:
            return False
        terms = get_search_terms(name, cand.get('aliases', []))
        name_patterns = []
        for term in terms:
            stem = stem_ru_word(strip_combining_accents(term).casefold()) if term else ''
            if len(stem) >= 2:
                name_patterns.append(r'\b' + re.escape(stem) + r'[a-zа-яё]*\b')
        if not name_patterns:
            return False
        person = r'(?:' + '|'.join(name_patterns) + r')'
        representation = (
            r'(?:вспомин\w*|помнил\w*|упомина\w*|рассказ\w*\s+о|'
            r'фотограф\w*|снимк\w*|портрет\w*|пл[её]нк\w*|архивн\w*\s+кадр\w*|'
            r'журнал\w*|дневник\w*|письм\w*|запис[ьи]\w*|документ\w*|'
            r'голос\w*|записанн\w*\s+голос\w*|'
            r'remember\w*|recall\w*|mention\w*|photograph\w*|photo\w*|portrait\w*|'
            r'film\s+frame|journal\w*|diary\w*|letter\w*|written\s+record|'
            r'voice\w*|recorded\s+voice)'
        )
        represented_before_name = re.search(representation + r'[^.!?,;—]{0,60}' + person, q, re.IGNORECASE)
        explicit_after_name = re.search(
            person + r'[^.!?]{0,100}(?:упомина\w*|не\s+(?:был\w*|находил\w*|присутствовал\w*|появлял\w*)|'
            r'только\s+голос\w*|по\s+телефон\w*|по\s+рац\w*|из\s+трубк\w*|'
            r'физически\s+[^.!?]{0,50}\s+не\s+присутствовал\w*|'
            r'за\s+кадром|на\s+материк\w*|not\s+(?:there|present|visible)|'
            r'voice\s+only|by\s+(?:phone|radio)|over\s+(?:the\s+)?(?:phone|radio)|off[ -]?screen|on\s+the\s+mainland)',
            q, re.IGNORECASE)
        return bool(represented_before_name or explicit_after_name)

    if quotes and all(nonvisual_evidence(q) for q in quotes):
        return 'mentioned_only', f"'{name}' is represented only through memory, media, a remote voice, or explicit absence."

    # A one-off occupational or anonymous role remains episodic even when it
    # delivers a clue near the opening or climax.  It earns a locked portrait
    # only after recurring in more than one source event.
    name_folded = strip_combining_accents(name).casefold()
    role_tokens = set(re.findall(r'[\wё-]+', name_folded))
    role_stems = {stem_ru_word(token) for token in role_tokens}
    one_off_role = any(
        role in name_folded or stem_ru_word(role) in role_stems
        for role in _ONE_OFF_OCCUPATIONAL_ROLES
    )
    if one_off_role and is_agent_noun_phrase(name) and unique_scenes_count < 2:
        return 'episodic', f"'{name}' is a one-off story role and does not require a permanent identity reference."

    # Dialogue detection
    _DIALOGUE_MARKERS = re.compile(r'[«"—–-]')
    _SPEECH_VERBS = re.compile(
        r'\b(?:сказал[аои]?|ответил[аои]?|спросил[аои]?|воскликнул[аои]?|шепнул[аои]?|'
        r'поблагодарил[аои]?|произн[её]с[лаои]?|заметил[аои]?|велел[аои]?|позвал[аои]?|'
        r'закричал[аои]?|обратил(?:ся|ась|ись)|объяснил[аои]?|предупредил[аои]?|'
        r'said|asked|replied|whispered|shouted|thanked|ordered|called|explained|warned)\b',
        re.IGNORECASE
    )
    has_dialogue = any(_DIALOGUE_MARKERS.search(q) and _SPEECH_VERBS.search(q) for q in quotes)

    # Catalyst detection in opening scenes (scene index 0 or 1 of multi-scene story)
    _CATALYST_ACTIONS = re.compile(
        r'\b(?:прискакал[аои]?|прибыл[аои]?|вош[её]л[аои]?|прин[её]с[лаои]?|доставил[аои]?|'
        r'бросил[аои]?|вручил[аои]?|объявил[аои]?|приказал[аои]?|велел[аои]?|потребовал[аои]?|'
        r'выдал[аои]?|отдал[аои]?|явил(?:ся|ась|ись)|передал[аои]?|сообщил[аои]?|'
        r'arriv\w*|enter\w*|brought|bring\w*|deliver\w*|threw|throw\w*|announc\w*|order\w*|command\w*|demand\w*|hand\w*|report\w*|step\w*|greet\w*|warn\w*)\b',
        re.IGNORECASE
    )
    _MISSION_NOUNS = re.compile(
        r'\b(?:указ[а-яё]*|приказ[а-яё]*|вест[ьиея]|письм[а-яё]*|свиток[а-яё]*|заказ[а-яё]*|'
        r'задач[а-яё]*|поручен[а-яё]*|послан[а-яё]*|грамот[а-яё]*|новост[ьиея]|сводк[а-яё]*|'
        r'шторм[а-яё]*|бур[яеию]|предупрежден[а-яё]*|salute|'
        r'order[s]?|decree[s]?|message[s]?|letter[s]?|scroll[s]?|mission[s]?|task[s]?|news|dispatch(?:es)?|storm[s]?|warn\w*)\b',
        re.IGNORECASE
    )
    is_inciting_catalyst = (
        total_scenes >= 2 and
        any(idx in (0, 1) for idx in all_scene_indices) and
        any(
            (_CATALYST_ACTIONS.search(q) and _MISSION_NOUNS.search(q)) or (_CATALYST_ACTIONS.search(q) and _DIALOGUE_MARKERS.search(q) and _MISSION_NOUNS.search(q))
            for q in quotes
        )
    )

    # Climax / Resolution detection in late/final scene of multi-scene narrative
    _RESOLUTION_ACTIONS = re.compile(
        r'\b(?:вернул(?:ся|ась|ись)|подош[её]л[аои]?|вручил[аои]?|прин[её]с[лаои]?|поблагодарил[аои]?|'
        r'взял[аои]?|удержал[аои]?|спас[лаои]?|вспомнил[аои]?|показал[аои]?|выдержал[аои]?|'
        r'сош[её]л[аои]?|встретил[аои]?|испытал[аои]?|пожал[аои]?|протянул[аои]?|передал[аои]?|'
        r'прибыл[аои]?|приехал[аои]?|приш[её]л[аои]?|сигнализировал[аои]?|сообщил[аои]?|доставил[аои]?|'
        r'returned|approached|thanked|held|saved|remembered|showed|tested|met|survived|shook|handed|'
        r'arrived|reached|signaled|reported|delivered|joined|entered)\b',
        re.IGNORECASE
    )
    is_climax_resolution_partner = (
        total_scenes >= 3 and
        any(idx >= total_scenes // 2 for idx in all_scene_indices) and
        any(_RESOLUTION_ACTIONS.search(q) for q in quotes)
    )

    is_agent_phrase = is_agent_noun_phrase(name)
    has_performed_action = any(_character_performs_action(name, q, cand.get('aliases', [])) for q in quotes)
    is_recurring_protagonist = unique_scenes_count >= 2

    # Decision logic
    if is_recurring_protagonist:
        return 'key', f"'{name}' is a recurring active participant across multiple distinct scenes ({unique_scenes_count} scenes)."
    if is_inciting_catalyst:
        return 'key', f"'{name}' is an inciting incident catalyst delivering the opening mission/conflict."
    if is_climax_resolution_partner:
        return 'key', f"'{name}' is a key participant in the story climax/resolution."
    if not is_agent_phrase and (has_performed_action or has_dialogue or len(quotes) >= 1):
        return 'key', f"'{name}' is an active named story character."
    if total_scenes <= 1 and (not is_agent_phrase or is_recognized_role(name)) and (has_performed_action or has_dialogue or len(quotes) >= 1):
        return 'key', f"'{name}' is the focal protagonist of the single-scene story."
    if is_recognized_role(name) and (has_performed_action or has_dialogue) and total_scenes <= 1:
        return 'key', f"'{name}' is the focal titled protagonist of the story."

    # DEFAULT FOR ALL UNCONFIRMED / SINGLE-SCENE AGENT PHRASES: EPISODIC!
    return 'episodic', f"'{name}' is an episodic participant appearing in a single scene without dialogue or main plot agency."


class CharacterEntityResolver:
    """Deterministic entity resolver for assembling the project Cast Bible."""

    def __init__(self, story: str):
        self.story = story
        self.story_hash = compute_story_hash(story)

    def extract_deterministic_candidates(self, raw_candidates: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        """Rule-based candidate extraction from full text to ensure 100% stability.

        Guarantees that essential story characters are never missed, even if an LLM returns only a subset.
        """
        candidates = []
        story = self.story

        # 1. Look for known role archetype patterns
        is_ru = bool(re.search(r'[а-яё]', story, re.I))
        has_en_raw_role = any(
            isinstance(c, dict) and c.get('name') in {'Daughter', 'Son', 'Husband', 'Wife', 'Mother', 'Father'}
            for c in (raw_candidates or [])
        )
        if is_ru:
            role_search = [
                ('captain', 'Капитан', re.compile(r'\b(?:капитан(?:а|у|ом|е)?)\b', re.IGNORECASE)),
                ('keeper', 'Смотритель', re.compile(r'\b(?:смотрител[ьяемю]|смотритель)\b', re.IGNORECASE)),
                ('doctor', 'Доктор', re.compile(r'\b(?:доктор(?:а|у|ом|е)?|врач(?:а|у|ом|е)?)\b', re.IGNORECASE)),
                ('daughter', 'Дочь' if not has_en_raw_role else 'Daughter', re.compile(r'\b(?:доч[ьиея]|дочк[а-я]*|дочер[а-я]*)\b', re.IGNORECASE)),
                ('son', 'Сын' if not has_en_raw_role else 'Son', re.compile(r'\b(?:сын(?:а|у|ом|е|овья)?|сынок|сыном)\b', re.IGNORECASE)),
                ('pilot', 'Пилот', re.compile(r'\b(?:пилот(?:а|у|ом|е)?)\b', re.IGNORECASE)),
                ('inspector', 'Инспектор', re.compile(r'\b(?:инспектор(?:а|у|ом|е)?)\b', re.IGNORECASE)),
                ('detective', 'Детектив', re.compile(r'\b(?:детектив(?:а|у|ом|е)?)\b', re.IGNORECASE)),
                ('officer', 'Офицер', re.compile(r'\b(?:офицер(?:а|у|ом|е)?)\b', re.IGNORECASE)),
            ]
        else:
            role_search = [
                ('captain', 'Captain', re.compile(r'\b(?:captain|the captain)\b', re.IGNORECASE)),
                ('keeper', 'Keeper', re.compile(r'\b(?:keeper|lighthouse keeper)\b', re.IGNORECASE)),
                ('doctor', 'Doctor', re.compile(r'\b(?:doctor)\b', re.IGNORECASE)),
                ('daughter', 'Daughter', re.compile(r'\b(?:daughter)\b', re.IGNORECASE)),
                ('son', 'Son', re.compile(r'\b(?:son)\b', re.IGNORECASE)),
                ('pilot', 'Pilot', re.compile(r'\b(?:pilot)\b', re.IGNORECASE)),
                ('inspector', 'Inspector', re.compile(r'\b(?:inspector)\b', re.IGNORECASE)),
                ('detective', 'Detective', re.compile(r'\b(?:detective)\b', re.IGNORECASE)),
                ('officer', 'Officer', re.compile(r'\b(?:officer)\b', re.IGNORECASE)),
            ]

        for role_key, default_name, pattern in role_search:
            matches = list(pattern.finditer(story))
            if matches:
                canonical_name = default_name
                aliases = [default_name]

                title_pat = re.compile(
                    rf'\b(?:{re.escape(default_name)}|{re.escape(default_name.lower())})\s+([А-ЯЁ][а-яё]{{2,15}})\b' if is_ru
                    else rf'\b(?:{re.escape(default_name)}|{re.escape(default_name.lower())})\s+([A-Z][a-z]{{2,15}})\b'
                )
                prefix_match = title_pat.search(story)
                if prefix_match:
                    p_prefix = default_name.capitalize()
                    p_surname = prefix_match.group(1)
                    if p_surname not in STOP_ENTITY_WORDS and len(p_surname) >= 3 and not is_grammatical_non_character(p_surname):
                        canonical_name = f"{p_prefix} {p_surname}"
                        aliases.extend([default_name, p_surname, f"{p_prefix.lower()} {p_surname}", f"{p_prefix} {p_surname}"])

                evidence = find_evidence_in_story(story, canonical_name, aliases)
                if evidence:
                    cid = slugify_id(canonical_name)
                    candidates.append({
                        'id': cid,
                        'cast_id': cid,
                        'canonical_name': canonical_name,
                        'aliases': list(dict.fromkeys(aliases)),
                        'role': role_key,
                        'description': f"{canonical_name}, active participant in the story events.",
                        'status': 'active',
                        'evidence': evidence
                    })

        # 2. Extract Title / Honorific + Capitalized Name (e.g. "кузнец Мартин", "ученик Лука", "мастер Элиас", "староста Бран", "пастух Данил", "pilot Vance", "engineer Thomas")
        title_name_pat = re.compile(
            r'\b(?P<title>[А-ЯЁа-яёA-Za-z]+)\s+(?P<name>[А-ЯЁ][а-яё]{2,15}|[A-Z][a-z]{2,15})\b'
        )
        for m in title_name_pat.finditer(story):
            t_word = m.group('title').lower()
            p_name = m.group('name')
            t_stem = stem_ru_word(t_word)
            if (t_stem in _AGENT_NOUN_STEMS or is_agent_noun_stem(t_stem)) and p_name not in STOP_ENTITY_WORDS and not is_grammatical_non_character(p_name):
                full_title_name = f"{m.group('title').capitalize()} {p_name}"
                lemmatized_name = lemmatize_ru_name(p_name, story) or p_name
                evidence = find_evidence_in_story(story, lemmatized_name)
                if evidence:
                    cid = slugify_id(lemmatized_name)
                    aliases = [lemmatized_name, p_name, full_title_name, t_word.capitalize(), t_word, f"{m.group('title')} {p_name}"]
                    if t_stem in {'кузнец', 'плотник', 'гончар', 'пекарь', 'сапожник', 'ткач', 'оружейник', 'лекарь', 'мастер', 'blacksmith', 'smith', 'carpenter', 'craftsman', 'master'}:
                        aliases.extend(['Мастер', 'мастер', 'Master', 'master'])
                    candidates.append({
                        'id': cid,
                        'cast_id': cid,
                        'canonical_name': lemmatized_name,
                        'aliases': list(dict.fromkeys(aliases)),
                        'role': t_word,
                        'description': f"{full_title_name}, active participant in the story events.",
                        'status': 'active',
                        'evidence': evidence
                    })

        # 3. Extract Descriptive Agent / Profession Phrases (e.g. "королевский гонец", "молодой солдат в широком шлеме", "деревенский плотник", "кожевник", "юный пастух", "station controller", "dock welder")
        for m in re.finditer(r'\b([А-ЯЁа-яёA-Za-z]+)\b', story):
            n_raw = m.group(1)
            n_stem = stem_ru_word(n_raw.lower())
            if (n_stem in _AGENT_NOUN_STEMS or is_agent_noun_stem(n_stem)) and not is_grammatical_non_character(n_raw) and not is_collective_entity(n_raw):
                start = m.start()
                end = m.end()

                # Check for preceding adjective or noun modifier
                pre = story[:start].rstrip()
                adj_m = re.search(r'([А-ЯЁа-яёA-Za-z]+(?:ый|ий|ой|ая|яя|ое|ее|ого|его|ому|ему|ым|им|ом|ем|ую|юю|ые|ие|ых|их|ыми|ими|ic|al|ary|ory|ive|ous|ful|less))\s*$', pre, re.I)
                adj = adj_m.group(1) if adj_m and not is_grammatical_non_character(adj_m.group(1)) and not is_temporal_entity(adj_m.group(1)) else ''
                if not adj:
                    noun_adj_m = re.search(r'([A-Za-z]{3,15})\s*$', pre)
                    if noun_adj_m and not is_grammatical_non_character(noun_adj_m.group(1)) and not is_temporal_entity(noun_adj_m.group(1)):
                        w = noun_adj_m.group(1)
                        if w.lower() not in {'the', 'a', 'an', 'this', 'that', 'when', 'after', 'before', 'where', 'while', 'with', 'from', 'into', 'and', 'for', 'contacted', 'called', 'asked', 'ordered', 'told', 'visited', 'saw', 'met', 'joined', 'helped', 'found', 'sent', 'brought', 'gave', 'took', 'held', 'walked', 'ran', 'stood', 'came'}:
                            adj = w

                # Check for succeeding prepositional phrase
                post = story[end:].lstrip()
                prep_m = re.match(r'^(в|на|с|со|у|без|под|из|in|on|with|at|under|from)\s+([А-ЯЁа-яёA-Za-z,\s-]+?)(?=[.!?…\n;]|\b(?:подош[её]л|сказал|взял|стоял|выдал|приехал|чинил|вырезал|шил|раздувал|спросил|отложил|погасил|кивнул|положил)\b|$)', post, re.I)
                prep_tail = ''
                if prep_m:
                    raw_tail = prep_m.group(0).strip()
                    tail_tokens = [t for t in re.findall(r'[\wё-]+', raw_tail) if t.lower() not in {'чужом', 'своем', 'своём', 'этом', 'слишком', 'очень', 'весьма', 'так'}]
                    clean_tail_tokens = []
                    for t in tail_tokens:
                        if _ACTION_VERBS.search(t) or is_grammatical_non_character(t):
                            break
                        clean_tail_tokens.append(t)
                    if len(clean_tail_tokens) >= 2:
                        prep_tail = ' '.join(clean_tail_tokens[:3])

                phrase_parts = [p for p in [adj, n_raw, prep_tail] if p]
                if len(phrase_parts) == 1 and (is_bare_generic_agent_noun(phrase_parts[0]) or is_grammatical_non_character(phrase_parts[0])):
                    continue
                raw_phrase = ' '.join(phrase_parts)
                if is_collective_entity(raw_phrase) or is_grammatical_non_character(raw_phrase) or is_bare_generic_agent_noun(raw_phrase):
                    continue
                canonical = lemmatize_ru_name(raw_phrase, story)
                if canonical and len(canonical) >= 3:
                    if is_inanimate_or_location_entity(canonical) or is_prop_or_armor_entity(canonical) or is_collective_entity(canonical) or is_bare_generic_agent_noun(canonical) or is_grammatical_non_character(canonical):
                        continue
                    aliases = [canonical, raw_phrase, n_raw.capitalize()]
                    evidence = find_evidence_in_story(story, canonical, aliases)
                    if evidence:
                        cid = slugify_id(canonical)
                        candidates.append({
                            'id': cid,
                            'cast_id': cid,
                            'canonical_name': canonical,
                            'aliases': list(dict.fromkeys(aliases)),
                            'role': n_stem,
                            'description': f"{canonical}, participant in the story events.",
                            'status': 'active',
                            'evidence': evidence
                        })

        # 4. Extract adjacent first-name + surname pairs before individual
        # capitalized tokens, so Russian case forms such as "Максима Орлова"
        # become one stable identity rather than Максим + Орлова.
        full_name_pat = re.compile(r'\b([А-ЯЁ][а-яё]{2,20})\s+([А-ЯЁ][а-яё]{2,25})\b')
        for match in full_name_pat.finditer(story):
            raw_full = match.group(0)
            canonical = lemmatize_ru_name(raw_full, story)
            aliases = [canonical, raw_full, match.group(1), match.group(2)]
            evidence = find_evidence_in_story(story, raw_full, aliases)
            if (not canonical or is_grammatical_non_character(canonical) or
                    is_inanimate_or_location_entity(canonical, evidence) or
                    is_agent_noun_phrase(canonical)):
                continue
            already_covered = any(names_match_entity(canonical, c['canonical_name'], story) or
                                  any(names_match_entity(canonical, alias, story) for alias in c.get('aliases', []))
                                  for c in candidates)
            if not already_covered and evidence:
                cid = slugify_id(canonical)
                candidates.append({
                    'id': cid, 'cast_id': cid, 'canonical_name': canonical,
                    'aliases': list(dict.fromkeys(aliases)), 'role': 'character',
                    'description': f"{canonical}, active participant in the story events.",
                    'status': 'active', 'evidence': evidence,
                })

        # 5. Extract Capitalized Named Entities in narrative (e.g. "Томас", "Рид", "Алексей", "Марина", "Вэнс", "Елена", "Злата", "Роберти")
        proper_names = re.findall(r'\b([A-Z][a-z]{2,15}|[А-ЯЁ][а-яё]{2,15})\b', story)
        name_counts = {}
        for name in proper_names:
            if name in STOP_ENTITY_WORDS or len(name) < 3:
                continue
            if is_temporal_entity(name) or is_grammatical_non_character(name) or is_prop_or_armor_entity(name) or is_collective_entity(name) or is_bare_generic_agent_noun(name) or is_inanimate_or_location_entity(name):
                continue
            lemmatized = lemmatize_ru_name(name, story)
            if not lemmatized or len(lemmatized) < 3 or is_temporal_entity(lemmatized) or is_grammatical_non_character(lemmatized) or is_prop_or_armor_entity(lemmatized) or is_collective_entity(lemmatized) or is_bare_generic_agent_noun(lemmatized) or is_inanimate_or_location_entity(lemmatized):
                continue
            if is_purely_sentence_initial(name, story) or is_purely_sentence_initial(lemmatized, story):
                continue
            name_counts[lemmatized] = name_counts.get(lemmatized, 0) + 1

        for name, count in name_counts.items():
            if is_temporal_entity(name) or is_inanimate_or_location_entity(name) or is_grammatical_non_character(name) or is_bare_generic_agent_noun(name):
                continue
            if is_purely_sentence_initial(name, story):
                continue
            already_covered = any(names_match_entity(name, c['canonical_name'], story) or
                                  any(names_match_entity(name, a, story) for a in c.get('aliases', []))
                                  for c in candidates)
            if not already_covered:
                evidence = find_evidence_in_story(story, name)
                if evidence:
                    if is_inanimate_or_location_entity(name, evidence):
                        continue
                    has_action = any(_character_performs_action(name, ev.get('quote', '')) for ev in evidence)
                    has_title_prefix = bool(re.search(r'\b(?:[Кк]апитан|[Ии]нспектор|[Дд]етектив|[Дд]октор|[Вв]рач|[Оо]фицер|[Сс]мотритель|[Мм]астер|[Кк]упец|[Сс]тароста|Captain|Inspector|Detective|Doctor|Officer|Keeper|Master)\s+' + re.escape(name) + r'\b', story))
                    if count >= 2 or has_action or has_title_prefix:
                        cid = slugify_id(name)
                        candidates.append({
                            'id': cid,
                            'cast_id': cid,
                            'canonical_name': name,
                            'aliases': [name],
                            'role': 'character',
                            'description': f"{name}, active participant in the story.",
                            'status': 'active',
                            'evidence': evidence
                        })

        # 5. Species names in wildlife stories
        clean_story = strip_combining_accents(story)
        tax_matches = []
        for st in (story, clean_story):
            for match in list(_TAXONOMIC_SUBJECT_CUE.finditer(st)) + list(_TAXONOMIC_SUBJECT_REVERSED_CUE.finditer(st)):
                tax_matches.append(match.group('name').strip())

        for name in dict.fromkeys(tax_matches):
            if (len(name) < 3 or name.casefold() in _INANIMATE_ENTITY_WORDS
                    or name.casefold() in _TEMPORAL_ENTITY_WORDS
                    or name.casefold() in _GENERIC_TAXONOMY_CATEGORIES
                    or is_grammatical_non_character(name)):
                continue
            already_covered = any(
                names_match_entity(name, c['canonical_name'], story)
                or any(names_match_entity(name, alias, story) for alias in c.get('aliases', []))
                for c in candidates)
            if already_covered:
                continue
            evidence = find_evidence_in_story(story, name)
            if not evidence:
                continue
            cid = slugify_id(name)
            candidates.append({
                'id': cid,
                'cast_id': cid,
                'canonical_name': name,
                'aliases': [name],
                'role': 'wildlife subject',
                'description': f'Entity type: animal; species: {name}. Recurring non-human subject in the story.',
                'status': 'active',
                'evidence': evidence,
            })

        # Recurring recognized animals / creatures in wildlife stories
        if _NONHUMAN_CONTEXT_CUE.search(story):
            for animal in _KNOWN_ANIMALS_AND_CREATURES:
                if animal in _GENERIC_TAXONOMY_CATEGORIES:
                    continue
                stem = stem_ru_word(animal)
                if len(stem) < 3:
                    continue
                if animal.endswith(('а', 'я')):
                    base_stem = animal[:-1]
                    pat = re.compile(r'\b' + re.escape(base_stem) + r'(?:[аыеу]|ой|ою|ам|ами|ах)\b', re.IGNORECASE)
                elif len(stem) <= 3:
                    pat = re.compile(r'\b' + re.escape(stem) + r'(?:[аяуеоыий]|ом|ем|ами|ями|ах|ях|ов|ев)?\b', re.IGNORECASE)
                else:
                    pat = re.compile(r'\b' + re.escape(stem) + r'(?:[ауеыи]|ом|ем|ов|ев|ам|ями|ами|ах|ях)?\b', re.IGNORECASE)
                matches = list(pat.finditer(clean_story))
                if len(matches) >= 2:
                    already_covered = any(
                        names_match_entity(animal, c['canonical_name'], story)
                        or any(names_match_entity(animal, alias, story) for alias in c.get('aliases', []))
                        for c in candidates)
                    if already_covered:
                        continue
                    evidence = find_evidence_in_story(story, animal)
                    if not evidence:
                        continue
                    c_name = animal.capitalize() if animal[0].islower() else animal
                    cid = slugify_id(animal)
                    candidates.append({
                        'id': cid,
                        'cast_id': cid,
                        'canonical_name': c_name,
                        'aliases': [c_name, animal],
                        'role': 'wildlife subject',
                        'description': f'Entity type: animal; species: {c_name}. Recurring non-human subject in the story.',
                        'status': 'active',
                        'evidence': evidence,
                    })

        return candidates

    def resolve(self, raw_candidates: List[Dict[str, Any]], scenes: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Merge raw candidates and deterministic candidates into a canonical Cast Bible."""
        combined = []

        # 1. Incorporate raw candidates first (preserving their rich descriptions from model)
        for raw in (raw_candidates or []):
            if not isinstance(raw, dict):
                continue
            name = (raw.get('canonical_name') or raw.get('name') or '').strip()
            if not name or name in STOP_ENTITY_WORDS or is_acoustic_label_name(name):
                continue

            lemmatized_name = lemmatize_ru_name(name, self.story) or name
            raw_aliases = raw.get('aliases') if isinstance(raw.get('aliases'), list) else []
            aliases = list(dict.fromkeys([name, lemmatized_name] + raw_aliases))
            evidence = raw.get('evidence')
            if not evidence or not isinstance(evidence, list):
                evidence = find_evidence_in_story(self.story, name, aliases) or find_evidence_in_story(self.story, lemmatized_name, aliases)
            else:
                valid_ev = []
                for ev in evidence:
                    if isinstance(ev, dict) and ev.get('quote') and ev['quote'] in self.story:
                        start = self.story.find(ev['quote'])
                        valid_ev.append({
                            'start': start,
                            'end': start + len(ev['quote']),
                            'quote': ev['quote']
                        })
                evidence = valid_ev or find_evidence_in_story(self.story, name, aliases) or find_evidence_in_story(self.story, lemmatized_name, aliases)

            if (is_prop_or_armor_entity(name) or
                    is_inanimate_or_location_entity(lemmatized_name, evidence) or
                    is_noncharacter_reference(lemmatized_name, aliases, evidence, self.story, raw.get('description', ''))):
                continue

            if not evidence and scenes and can_recover_nonhuman_from_wildlife_story(raw.get('description', ''), self.story):
                for sc in scenes:
                    sq = sc.get('source_quote') or ''
                    sc_chars = sc.get('characters', []) if isinstance(sc.get('characters'), list) else []
                    is_listed = any(names_match_entity(name, sc_c, self.story) or names_match_entity(lemmatized_name, sc_c, self.story) for sc_c in sc_chars)
                    wildlife_scene = bool(_NONHUMAN_CONTEXT_CUE.search(sq) or _NONHUMAN_CONTEXT_CUE.search(self.story))
                    if sq and sq in self.story and is_listed and wildlife_scene:
                        start = self.story.find(sq)
                        evidence.append({
                            'start': start,
                            'end': start + len(sq),
                            'quote': sq
                        })

            if evidence:
                combined.append({
                    'canonical_name': name,
                    'aliases': aliases,
                    'role': raw.get('role', 'character'),
                    'description': raw.get('description', f"{name}, character in the story."),
                    'status': raw.get('status', 'active'),
                    'evidence': evidence
                })

        # 2. Add rule-based deterministic candidates (chunked if large story, or single chunk if <= 16k)
        if len(self.story) > 16000:
            rule_candidates = extract_cast_candidates_from_chunks(self.story)
        else:
            rule_candidates = self.extract_deterministic_candidates(raw_candidates)
        for rule_cand in rule_candidates:
            matched = None
            for existing in combined:
                if (names_match_entity(rule_cand['canonical_name'], existing['canonical_name'], self.story) or
                    any(names_match_entity(rule_cand['canonical_name'], a, self.story) for a in existing.get('aliases', []))):
                    matched = existing
                    break

            if matched:
                matched['aliases'] = list(dict.fromkeys(matched.get('aliases', []) + rule_cand.get('aliases', [])))
                matched['evidence'].extend(rule_cand.get('evidence', []))
                rule_name = str(rule_cand.get('canonical_name') or '')
                matched_name = str(matched.get('canonical_name') or '')
                rule_words = rule_name.split()
                if (len(matched_name.split()) == 1 and len(rule_words) == 2 and
                        all(word[:1].isupper() for word in rule_words) and
                        names_match_entity(matched_name, rule_name, self.story)):
                    matched['canonical_name'] = rule_name
                if (slugify_id(rule_cand['canonical_name']) == slugify_id(matched['canonical_name']) and
                        rule_cand['canonical_name'].casefold() in self.story.casefold() and
                        matched['canonical_name'].casefold() not in self.story.casefold()):
                    matched['canonical_name'] = rule_cand['canonical_name']
            else:
                combined.append(rule_cand)

        # 3. Filter candidates according to qualification & classification rules:
        diagnostics = []
        unique_by_id = {}
        for cand in combined:
            canonical_name = cand.get('canonical_name', '').strip()
            if is_acoustic_label_name(canonical_name):
                diagnostics.append({
                    'name': canonical_name,
                    'canonical_name': canonical_name,
                    'role_classification': 'rejected',
                    'card_created': False,
                    'justification': 'Acoustic labels are not visible cast entities.',
                    'evidence_quotes': [],
                })
                continue
            lemmatized_name = lemmatize_ru_name(canonical_name, self.story)
            if lemmatized_name and lemmatized_name.casefold() in self.story.casefold():
                cand['canonical_name'] = lemmatized_name

            ev = cand.get('evidence', [])
            validated_ev = []
            for item in ev:
                q = item.get('quote', '')
                pos = self.story.find(q)
                if pos >= 0:
                    validated_ev.append({
                        'start': pos,
                        'end': pos + len(q),
                        'quote': q
                    })
            cand['evidence'] = validated_ev

            cand['description'] = infer_nonhuman_description(
                cand.get('canonical_name', ''), cand.get('description', ''), validated_ev, self.story)

            role_class, justification = classify_candidate_role(cand, self.story, scenes)
            cand['role_classification'] = role_class
            cand['justification'] = justification

            diagnostics.append({
                'name': cand.get('canonical_name'),
                'canonical_name': cand.get('canonical_name'),
                'role_classification': role_class,
                'card_created': (role_class == 'key'),
                'justification': justification,
                'evidence_quotes': [item.get('quote', '') for item in validated_ev]
            })

            if role_class == 'rejected':
                continue
            if role_class == 'mentioned_only':
                if is_photo_only(validated_ev, self.story):
                    cand['status'] = 'excluded_photo'
                elif is_deceased_only(validated_ev, self.story):
                    cand['status'] = 'excluded_deceased'
                elif is_phone_caller_only(validated_ev, self.story):
                    cand['status'] = 'excluded_phone_interlocutor'
                else:
                    cand['status'] = 'mentioned_only'
                continue

            nonhuman = can_recover_nonhuman_from_wildlife_story(cand.get('description', ''), self.story)
            if not nonhuman and not has_canonical_story_anchor(cand['canonical_name'], self.story, cand.get('aliases', [])):
                continue

            cid = cand.get('cast_id') or cand.get('id') or slugify_id(cand['canonical_name'])
            cand['id'] = cid
            cand['cast_id'] = cid
            cand['status'] = 'active' if role_class == 'key' else 'episodic'

            matched_key = None
            for existing_id, ex in unique_by_id.items():
                if (names_match_entity(cand['canonical_name'], ex['canonical_name'], self.story) or
                        any(names_match_entity(cand['canonical_name'], a, self.story) for a in ex.get('aliases', [])) or
                        any(names_match_entity(a, ex['canonical_name'], self.story) for a in cand.get('aliases', []))):
                    matched_key = existing_id
                    break

            if matched_key is None:
                unique_by_id[cand['id']] = cand
            else:
                ex = unique_by_id[matched_key]
                ex['aliases'] = list(dict.fromkeys(ex.get('aliases', []) + cand.get('aliases', [])))
                if len(cand.get('description', '')) > len(ex.get('description', '')):
                    ex['description'] = cand['description']
                if cand.get('role_classification') == 'key':
                    ex['role_classification'] = 'key'
                    ex['status'] = 'active'
                c_name = cand['canonical_name']
                e_name = ex['canonical_name']
                c_words = c_name.split()
                e_words = e_name.split()
                c_has_proper = len(c_words) == 1 and stem_ru_word(c_words[0].lower()) not in _AGENT_NOUN_STEMS and not is_grammatical_non_character(c_words[0])
                e_has_proper = len(e_words) == 1 and stem_ru_word(e_words[0].lower()) not in _AGENT_NOUN_STEMS and not is_grammatical_non_character(e_words[0])

                if (slugify_id(c_name) == slugify_id(e_name) and
                        c_name.casefold() in self.story.casefold() and
                        e_name.casefold() not in self.story.casefold()):
                    ex['canonical_name'] = c_name
                elif c_has_proper and not e_has_proper and len(e_words) == 1:
                    ex['aliases'] = list(dict.fromkeys(ex.get('aliases', []) + [ex['canonical_name']]))
                    ex['canonical_name'] = c_name
                    ex['id'] = cand['id']
                    ex['cast_id'] = cand.get('cast_id', cand['id'])
                elif e_has_proper and not c_has_proper and len(c_words) == 1:
                    ex['aliases'] = list(dict.fromkeys(ex.get('aliases', []) + [c_name]))
                elif len(c_words) > len(e_words) and not e_has_proper:
                    ex['aliases'] = list(dict.fromkeys(ex.get('aliases', []) + [ex['canonical_name']]))
                    ex['canonical_name'] = c_name
                    ex['id'] = cand['id']
                    ex['cast_id'] = cand.get('cast_id', cand['id'])
                ex['evidence'].extend(cand.get('evidence', []))

        # Sort by first appearance in story
        def first_pos(c):
            evs = c.get('evidence', [])
            return min((ev['start'] for ev in evs), default=999999)

        all_resolved = sorted(unique_by_id.values(), key=first_pos)
        for c in all_resolved:
            c.setdefault('cast_id', c['id'])

        key_characters = [c for c in all_resolved if c.get('role_classification') == 'key']
        episodic_characters = [c for c in all_resolved if c.get('role_classification') == 'episodic']

        return {
            'cast_version': CAST_VERSION,
            'story_hash': self.story_hash,
            'characters': key_characters,
            'episodic_participants': episodic_characters,
            'diagnostics': diagnostics
        }


def diff_cast_bibles(old_bible: Optional[Dict[str, Any]], new_bible: Dict[str, Any]) -> Dict[str, Any]:
    """Calculate structured human-readable diff between old and new cast bible."""
    if not old_bible or not isinstance(old_bible, dict) or 'characters' not in old_bible:
        return {
            'added': [{'id': c['id'], 'cast_id': c.get('cast_id', c['id']), 'name': c['canonical_name'], 'reason': 'Первичное создание библии'} for c in new_bible.get('characters', [])],
            'modified': [],
            'removed': [],
            'unchanged': []
        }

    old_chars = {(c.get('cast_id') or c['id']): c for c in old_bible.get('characters', []) if isinstance(c, dict) and ('id' in c or 'cast_id' in c)}
    new_chars = {(c.get('cast_id') or c['id']): c for c in new_bible.get('characters', []) if isinstance(c, dict) and ('id' in c or 'cast_id' in c)}

    added = []
    modified = []
    removed = []
    unchanged = []

    for cid, new_c in new_chars.items():
        if cid not in old_chars:
            added.append({'id': cid, 'cast_id': cid, 'name': new_c['canonical_name'], 'reason': 'Обнаружен в тексте сценария'})
        else:
            old_c = old_chars[cid]
            changes = []
            if old_c.get('canonical_name') != new_c.get('canonical_name'):
                changes.append(f"Имя: {old_c.get('canonical_name')} → {new_c.get('canonical_name')}")
            if old_c.get('role') != new_c.get('role'):
                changes.append(f"Роль: {old_c.get('role')} → {new_c.get('role')}")
            if changes:
                modified.append({'id': cid, 'cast_id': cid, 'name': new_c['canonical_name'], 'changes': changes})
            else:
                unchanged.append(cid)

    for cid, old_c in old_chars.items():
        if cid not in new_chars:
            removed.append({'id': cid, 'cast_id': cid, 'name': old_c.get('canonical_name', old_c.get('name', '')), 'reason': 'Не найден в новом анализе (удаление требует подтверждения)'})

    return {
        'added': added,
        'modified': modified,
        'removed': removed,
        'unchanged': unchanged
    }


def apply_cast_diff_resolution(existing_bible: Dict[str, Any], new_bible: Dict[str, Any], resolution: Dict[str, Any]) -> Dict[str, Any]:
    """Apply user action to resolve cast bible diff: accept, keep_old, or map."""
    action = resolution.get('action', 'accept')
    resolved_bible = copy.deepcopy(new_bible)
    old_chars = {(c.get('cast_id') or c['id']): c for c in existing_bible.get('characters', []) if isinstance(c, dict)}
    new_chars = {(c.get('cast_id') or c['id']): c for c in resolved_bible.get('characters', []) if isinstance(c, dict)}

    if action == 'accept':
        resolved_bible['diff'] = {
            'added': [], 'modified': [], 'removed': [],
            'unchanged': [c.get('cast_id') or c['id'] for c in resolved_bible.get('characters', [])]
        }
    elif action == 'keep_old':
        cast_id = resolution.get('cast_id') or resolution.get('id')
        if cast_id and cast_id in old_chars:
            if cast_id not in new_chars:
                resolved_bible['characters'].append(copy.deepcopy(old_chars[cast_id]))
            diff = diff_cast_bibles(existing_bible, resolved_bible)
            diff['removed'] = [r for r in diff.get('removed', []) if r.get('id') != cast_id and r.get('cast_id') != cast_id]
            resolved_bible['diff'] = diff
    elif action == 'map':
        target_cast_id = resolution.get('cast_id')
        new_name = resolution.get('new_name') or resolution.get('name')
        if target_cast_id and new_name:
            for c in resolved_bible.get('characters', []):
                if c.get('canonical_name') == new_name or c.get('id') == new_name or c.get('cast_id') == new_name:
                    c['cast_id'] = target_cast_id
                    c['id'] = target_cast_id
                    if target_cast_id in old_chars:
                        old_aliases = old_chars[target_cast_id].get('aliases', [])
                        c['aliases'] = list(dict.fromkeys(c.get('aliases', []) + old_aliases + [new_name]))
                    break
            resolved_bible['diff'] = diff_cast_bibles(existing_bible, resolved_bible)
    return resolved_bible


def chunk_large_story(story: str, chunk_size: int = 16000, overlap: int = 2000) -> List[Tuple[int, int, str]]:
    """Split large stories (>20k chars) into overlapping spans for complete cast extraction."""
    if len(story) <= chunk_size:
        return [(0, len(story), story)]

    chunks = []
    start = 0
    while start < len(story):
        end = min(len(story), start + chunk_size)
        if end < len(story):
            boundary = story.rfind('. ', start + chunk_size - overlap, end)
            if boundary > start:
                end = boundary + 1
        chunks.append((start, end, story[start:end]))
        if end >= len(story):
            break
        start = max(start + 1, end - overlap)

    return chunks


def extract_cast_candidates_from_chunks(story: str, chunk_size: int = 16000, overlap: int = 2000) -> List[Dict[str, Any]]:
    """Extract Cast Bible candidates chunk-by-chunk using chunk_large_story,
    preserving absolute evidence offsets into the full story."""
    chunks = chunk_large_story(story, chunk_size=chunk_size, overlap=overlap)
    all_chunk_candidates: List[Dict[str, Any]] = []

    for start_offset, end_offset, chunk_text in chunks:
        chunk_resolver = CharacterEntityResolver(chunk_text)
        candidates = chunk_resolver.extract_deterministic_candidates()
        for cand in candidates:
            adjusted_evidence = []
            for ev in cand.get('evidence', []):
                q = ev.get('quote', '')
                if not q:
                    continue
                pos = story.find(q, max(0, start_offset - 200))
                if pos < 0:
                    pos = story.find(q)
                if pos >= 0:
                    adjusted_evidence.append({
                        'start': pos,
                        'end': pos + len(q),
                        'quote': q
                    })
            if adjusted_evidence:
                cand['evidence'] = adjusted_evidence
                cid = cand.get('cast_id') or cand.get('id') or slugify_id(cand['canonical_name'])
                cand['cast_id'] = cid
                cand['id'] = cid
                all_chunk_candidates.append(cand)

    merged_candidates: List[Dict[str, Any]] = []
    for cand in all_chunk_candidates:
        matched = None
        cand_cid = cand['cast_id']
        for existing in merged_candidates:
            if existing['cast_id'] == cand_cid or \
               names_match_entity(cand['canonical_name'], existing['canonical_name'], story) or \
               any(names_match_entity(cand['canonical_name'], a, story) for a in existing.get('aliases', [])) or \
               any(names_match_entity(a, existing['canonical_name'], story) for a in cand.get('aliases', [])):
                matched = existing
                break

        if matched:
            matched['aliases'] = list(dict.fromkeys(matched.get('aliases', []) + cand.get('aliases', [])))
            seen_spans = {(ev['start'], ev['end']) for ev in matched.get('evidence', [])}
            for ev in cand.get('evidence', []):
                span = (ev['start'], ev['end'])
                if span not in seen_spans:
                    seen_spans.add(span)
                    matched['evidence'].append(ev)
            if len(cand.get('description', '')) > len(matched.get('description', '')):
                matched['description'] = cand['description']
        else:
            merged_candidates.append(copy.deepcopy(cand))

    qualified = []
    for cand in merged_candidates:
        ev = cand.get('evidence', [])
        if not ev:
            continue
        if is_photo_only(ev, story):
            cand['status'] = 'excluded_photo'
            continue
        if is_deceased_only(ev, story):
            cand['status'] = 'excluded_deceased'
            continue
        if is_phone_caller_only(ev, story):
            cand['status'] = 'excluded_phone_interlocutor'
            continue
        cand['status'] = 'active'
        qualified.append(cand)

    return qualified


def _match_gender_pair_in_quote(masc_c: Dict[str, Any], fem_c: Dict[str, Any], quote_lower: str) -> Tuple[bool, bool]:
    """Accurately detect whether masculine, feminine, or both characters of a gender pair appear in the author quote."""
    if not quote_lower:
        return False, False

    m_name = strip_combining_accents(masc_c['canonical_name']).casefold()
    f_name = strip_combining_accents(fem_c['canonical_name']).casefold()

    # English / transliterated alias matching
    has_masc = bool(re.search(r'\b(?:raven|пернатый\s+гигант)\b', quote_lower))
    has_fem = bool(re.search(r'\b(?:crow)\b', quote_lower))

    if not m_name.endswith(('а', 'я')) and not f_name.endswith(('а', 'я')):
        m_pat = re.compile(r'\b' + re.escape(m_name) + r'\b', re.IGNORECASE)
        f_pat = re.compile(r'\b' + re.escape(f_name) + r'\b', re.IGNORECASE)
        return bool(m_pat.search(quote_lower)) or has_masc, bool(f_pat.search(quote_lower)) or has_fem

    tokens = re.findall(r'\b[а-яёa-z-]+\b', quote_lower)
    f_stem = f_name[:-1] if f_name.endswith(('а', 'я')) else f_name

    if m_name in tokens or (m_name + 'ом') in tokens or (m_name + 'ов') in tokens or (m_name + 'ем') in tokens:
        has_masc = True

    if any(t in (f_stem + 'у', f_stem + 'ю', f_stem + 'ой', f_stem + 'ою', f_stem + 'ей', f_stem + 'ею') for t in tokens):
        has_fem = True
    if re.search(r'\b[а-яё]+(?:ая|яя|ую|юю)\s+' + re.escape(f_stem) + r'[ау]\b', quote_lower):
        has_fem = True
    if re.search(r'\b(?:у|от|для|без|около|вокруг|возле)\s+' + re.escape(f_stem) + r'(?:ы|и)\b', quote_lower):
        has_fem = True

    # Per-occurrence check of the homograph form (e.g. 'ворона')
    _GOVERNOR_RE = re.compile(
        r'\b(?:у|от|для|без|около|вокруг|возле|мимо|вместо|кроме|после|с|со|из|до|вблизи|позади|среди|впереди|'
        r'план\w*|кадр\w*|портрет\w*|фото\w*|вид\w*|силуэт\w*|взгляд\w*|глаз\w*|клюв\w*|крыл\w*|масс\w*|размах\w*|'
        r'оперени\w*|хвост\w*|окрас\w*|размер\w*|образ\w*|полет\w*|пол[её]т\w*|поведени\w*|гнезд\w*|голов\w*|лап\w*|'
        r'тел\w*|перь\w*|величественн\w*|настоящ\w*)\s*$'
    )
    _PARTICIPLE_RE = re.compile(
        r'^\s*(?:расправляющ\w*|сидящ\w*|летящ\w*|кричащ\w*|взмахивающ\w*|парящ\w*)\b'
    )

    for m in re.finditer(r'\b' + re.escape(f_name) + r'\b', quote_lower):
        prefix = quote_lower[max(0, m.start() - 35):m.start()].strip()
        suffix = quote_lower[m.end():min(len(quote_lower), m.end() + 35)]
        if _GOVERNOR_RE.search(prefix) or _PARTICIPLE_RE.search(suffix):
            has_masc = True
        else:
            has_fem = True

    ambiguous_plural = any(t in (f_stem + 'ы', f_stem + 'и', f_stem + 'е') for t in tokens)
    if ambiguous_plural and not (has_masc or has_fem):
        return False, False

    return has_masc, has_fem


def _is_character_offscreen_or_mentioned_only(quote: str, beat: str, shot: str, name: str) -> bool:
    """True when a named entity is merely mentioned, remembered, spoken about, or off-screen, NOT visually in the shot."""
    if not name or not isinstance(name, str):
        return False
    term = strip_combining_accents(name).strip().casefold()
    if not term:
        return False
    text = strip_combining_accents(f'{quote} {beat} {shot}').casefold()
    
    terms = set(get_search_terms(term))
    terms.add(term)
    latin = slugify_id(term).removeprefix('char_').replace('_', ' ')
    if latin:
        terms.add(latin)
    if term in ('жена', 'wife'):
        terms.update(['жена', 'жены', 'жене', 'жену', 'женой', 'супруга', 'wife'])
    if term in ('муж', 'husband'):
        terms.update(['муж', 'мужа', 'мужу', 'мужем', 'супруг', 'husband'])

    has_term_in_text = any(
        bool(re.search(r'\b' + re.escape(stem_ru_word(t)) + r'[a-zа-яё]*\b', text, re.IGNORECASE))
        for t in terms if stem_ru_word(t)
    )
    if not has_term_in_text:
        return False

    shot_lower = strip_combining_accents(shot or '').casefold()
    beat_lower = strip_combining_accents(beat or '').casefold()

    has_term_in_shot = any(
        bool(re.search(r'\b' + re.escape(stem_ru_word(t)) + r'[a-zа-яё]*\b', shot_lower, re.IGNORECASE))
        for t in terms if stem_ru_word(t)
    )

    # 1. Shot focus check: If shot is explicitly a close-up / portrait / solo shot of someone else
    other_person_shot = bool(re.search(
        r'\b(?:close[\s\-]*up|крупный\s+план|portrait|портрет|over[\s\-]*the[\s\-]*shoulder)\s+(?:on|of|с|на)?\s*'
        r'(?:staff|employee|receptionist|clerk|woman|man|doctor|nurse|guard|passerby|wife|husband|'
        r'сотрудниц\w*|сотрудник\w*|девушк\w*|женщин\w*|мужчин\w*|врач\w*|охранник\w*|прохож\w*|жен\w*|муж\w*)\b',
        shot_lower
    ))
    if other_person_shot and not has_term_in_shot:
        return True

    # 2. Memory / Recalling / Thought
    memory_patterns = [
        r'\b(?:вспомин\w*|вспомн\w*|recalls?|remember(?:s|ed|ing)?)\b[^\n.!?]{0,80}\b' + re.escape(term),
        r'\b(?:дум\w*\s+о|мысл\w*\s+о|thinking\s+of|thought\s+of)\b[^\n.!?]{0,50}\b' + re.escape(term),
        r'\b' + re.escape(term) + r'\w*\s+(?:казался|казалась|seemed|appeared)\b[^\n.!?]{0,80}\b(?:вспомин\w*|recalls?|remember\w*)',
    ]
    for mp in memory_patterns:
        if re.search(mp, text, re.IGNORECASE):
            direct_action = re.search(
                r'\b' + re.escape(term) + r'\w*\b(?:\W+\w+){0,3}\W+(?:стоит|сидит|ид[её]т|бежит|смотрит|держит|пишет|берет|берёт|stands|sits|walks|runs|looks|holds|writes|takes)',
                beat_lower
            )
            if not direct_action:
                return True

    # 3. Discussion / Talk / Mention / Narration Topic (character is subject of speech, not present)
    about_patterns = [
        r'\b(?:говор\w*|беседова\w*|рассказ\w*|упомина\w*|спрос\w*|расспрашива\w*|talk\w*|spoke\w*|speak\w*|tell\w*|told|ask\w*|discuss\w*|mention\w*)\b[^\n.!?]{0,35}\b(?:о|об|обо|про|about)\s+[^\n.!?]{0,40}\b' + re.escape(term),
        r'\b(?:миф\w*|легенд\w*|слух\w*|myth\w*|legend\w*|rumor\w*|story\s+of)\s+(?:о|об|обо|про|about)?\s+[^\n.!?]{0,40}\b' + re.escape(term),
        r'\b' + re.escape(term) + r'\w*\b[^\n.!?]{0,30}\b(?:назвал\w*|называл\w*|called|referred\s+to\s+as)\b',
    ]
    for ap in about_patterns:
        if re.search(ap, text, re.IGNORECASE):
            direct_action = re.search(
                r'\b' + re.escape(term) + r'\w*\b(?:\W+\w+){0,3}\W+(?:стоит|сидит|ид[её]т|бежит|смотрит|держит|пишет|берет|берёт|stands|sits|walks|runs|looks|holds|writes|takes)',
                beat_lower
            )
            if not direct_action:
                return True

    # 4. Off-screen / Voiceover / Recording / Remote Telephone Caller
    offscreen_patterns = [
        r'\b(?:за\s+кадром|голос\s+за\s+кадром|голос\s+из-за\s+кадра|off[\s\-]*screen|out\s+of\s+frame|voiceover|voice[\s\-]*over)\b[^\n.!?]{0,40}\b' + re.escape(term),
        r'\b' + re.escape(term) + r'\w*\b[^\n.!?]{0,40}\b(?:за\s+кадром|голос\s+за\s+кадром|off[\s\-]*screen|out\s+of\s+frame|voiceover|voice[\s\-]*over)\b',
        r'\b(?:на\s+том\s+конце\s+провода|на\s+другом\s+конце|on\s+the\s+other\s+end\s+of\s+the\s+line|voice\s+through\s+the\s+receiver|голос\s+в\s+трубке|голос\s+из\s+динамика|голос\s+по\s+телефону)\b[^\n.!?]{0,60}\b' + re.escape(term),
        r'\b' + re.escape(term) + r'\w*\b[^\n.!?]{0,60}\b(?:на\s+том\s+конце\s+провода|на\s+другом\s+конце|on\s+the\s+other\s+end\s+of\s+the\s+line|spoke\s+(?:in\s+a\s+\w+\s+voice\s+)?through\s+the\s+receiver|говорил\s+в\s+трубку)',
        r'\b(?:вещью|вещь|вещи|belonging\s+to|item\s+of)\s+' + re.escape(term) + r'\w*\b',
        r'\b(?:голос|голоса|голосом|голосу|voice\s+of)\s+[^\n.!?]{0,30}\b' + re.escape(term) + r'\w*\b',
        r'\b(?:(?:услышал\w*|слыш\w*|звучит\w*|звучал\w*|раздался|раздалась|раздалось|раздавался|донесся|донёсся|доносился|доносится)\s+(?:[а-яёa-z]+\s+){0,3}(?:голос\w*|крик\w*|ш[её]пот\w*|смех\w*)\s+(?:[а-яёa-z]+\s+){0,2})' + re.escape(term) + r'\w*\b',
        r'\b(?:голос|voice\s+of)\s+' + re.escape(term) + r'\w*\b[^\n.!?]{0,50}\b(?:на\s+диктофон\w*|на\s+запис\w*|on\s+tape|on\s+recorder|on\s+the\s+recording|over\s+the\s+radio|по\s+радио|по\s+телефону)\b',
    ]
    for op in offscreen_patterns:
        if re.search(op, text, re.IGNORECASE):
            direct_action = re.search(
                r'\b' + re.escape(term) + r'\w*\b(?:\W+\w+){0,3}\W+(?:стоит|сидит|ид[её]т|бежит|смотрит|держит|пишет|берет|берёт|stands|sits|walks|runs|looks|holds|writes|takes)',
                beat_lower
            )
            if not direct_action:
                return True

    return False


def _mentioned_only_as_discussion(quote: str, beat: str, names: List[str], shot: str = '') -> bool:
    """True when named entities are the topic of speech, not visible subjects."""
    quote_lower = (quote or '').casefold()
    beat_lower = (beat or '').casefold()
    full_text = f"{quote_lower} {beat_lower} {(shot or '').casefold()}"

    if any(_is_character_offscreen_or_mentioned_only(quote, beat, shot, name) for name in names if name):
        return True

    has_discussion = bool(re.search(
        r'\b(?:спрос\w*|вопрос\w*|ответ\w*|говор\w*|расскаж\w*|упомина\w*|объясня\w*|'
        r'миф\w*|легенд\w*|считает\w*|назвал\w*|about|ask(?:s|ed|ing)?|question\w*|'
        r'answer\w*|say(?:s|ing)?|tell(?:s|ing)?|mention\w*|explain\w*|myth\w*|legend\w*|'
        r'про|о|об|слышал\w*|умер\w*|погиб\w*|похорон\w*)\b',
        full_text
    ))
    if has_discussion and names:
        present_names = [
            n for n in names
            if n and stem_ru_word(strip_combining_accents(n).casefold())
            and re.search(r'\b' + re.escape(stem_ru_word(strip_combining_accents(n).casefold())) + r'[a-zа-яё]*\b', full_text, re.IGNORECASE)
        ]
        if not present_names:
            return False

        visual_action = (
            r'(?:сид\w*|лет\w*|ид\w*|беж\w*|стоит|стоя\w*|леж\w*|смотр\w*|'
            r'расправ\w*|клю\w*|perch\w*|fly\w*|walk\w*|run\w*|stand\w*|sit\w*|'
            r'look\w*|spread\w*|пишет|держит|берет|берёт|holds|writes|takes)'
        )
        has_any_visual = False
        for n in present_names:
            term = stem_ru_word(strip_combining_accents(n).casefold())
            if term and re.search(r'\b' + re.escape(term) + r'\w*\b(?:\W+\w+){0,4}\W+' + visual_action, full_text):
                has_any_visual = True
                break
        if not has_any_visual:
            if re.search(r'(?:[«"][^»"]+[»"]|[—–-]|если\s+спросить|спросить|asked|asking|talking\s+about| кем\s+приход)', full_text):
                return True

    return False


def resolve_scene_pronouns_and_cast(scenes: List[Dict[str, Any]], cast_bible: Dict[str, Any], story: str) -> List[Dict[str, Any]]:
    """Resolve scene characters strictly by ID from the Cast Bible, with pronoun continuity."""
    from .domain import resolve_scene_presence_contract, BANNED_FILLER_NAMES
    active_characters = [c for c in cast_bible.get('characters', [])
                         if c.get('canonical_name', '').casefold() not in BANNED_FILLER_NAMES]
    if not active_characters:
        return scenes

    id_map = {}
    for c in active_characters:
        cname = c.get('canonical_name') or c.get('name')
        cid = c.get('id')
        cast_id = c.get('cast_id') or cid or slugify_id(cname)
        if cid:
            id_map[cid] = c
        if cast_id:
            id_map[cast_id] = c
        id_map[cname] = c

    locations = cast_bible.get('locations', [])
    prev_contract = None

    for idx, scene in enumerate(scenes):
        contract = resolve_scene_presence_contract(
            scene, active_characters, locations, story=story,
            previous_contract=prev_contract,
            require_locked_portraits=False,
            scene_index=idx + 1
        )
        scene['character_ids'] = contract['visible_character_ids']
        scene['characters'] = [id_map[cid]['canonical_name'] for cid in contract['visible_character_ids'] if cid in id_map]
        if not scene['characters'] and contract['visible_characters']:
            scene['characters'] = list(contract['visible_characters'])
        scene['episodic_people'] = contract['episodic_people']
        scene['visible_people'] = contract['visible_people']
        if contract['location_ref'] and not scene.get('location_ref'):
            scene['location_ref'] = contract['location_ref']
        if contract['location_ref'] and not scene.get('location'):
            scene['location'] = contract['location_ref']
        if isinstance(scene.get('wardrobe'), dict):
            scene['wardrobe'] = {k: v for k, v in scene['wardrobe'].items() if k in scene['characters']}
        prev_contract = contract

    return scenes


CastResolver = CharacterEntityResolver
