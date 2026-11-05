"""Deterministic, visible repairs; no invented entities or discarded scenes."""
import unicodedata
from ..entity_identity import specific_label
from .schemas import Registry, Scene


def identity(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def normalize_registry(registry: Registry) -> tuple[Registry, dict[str, str], list[str]]:
    reg = registry.model_copy(deep=True)
    warnings: list[str] = []
    chars = reg.characters
    parents = list(range(len(chars)))

    def root(i: int) -> int:
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i

    seen: dict[str, int] = {}
    for i, char in enumerate(chars):
        # Shared profession/title/first name never proves two records are one person.
        keys = {"id:" + char.id}
        if specific_label(char.name):keys.add("primary:" + identity(char.name))
        for alias in char.aliases:
            if len(alias.split())>1 and specific_label(alias):
                keys.add('full:'+identity(alias))
        if len(char.name.split())>1 and specific_label(char.name):keys.add('full:'+identity(char.name))
        for key in sorted(keys):
            if key in seen:
                a, b = root(i), root(seen[key])
                other=chars[seen[key]]
                # Distinct full names remain distinct even if a provider polluted aliases.
                if (key.startswith('full:') and identity(char.name)!=identity(other.name)
                        and len(char.name.split())>1 and len(other.name.split())>1):
                    warnings.append(f'characters: неоднозначный alias {key[5:]!r}; {char.id} и {other.id} не объединены')
                    continue
                parents[max(a, b)] = min(a, b)
            else:
                seen[key] = i
    groups: dict[int, list[int]] = {}
    for i in range(len(chars)):
        groups.setdefault(root(i), []).append(i)
    merged = []
    remap: dict[str, str] = {}
    for indexes in groups.values():
        canonical = chars[indexes[0]].model_copy(deep=True)
        aliases: dict[str, str] = {}
        for i in indexes:
            char = chars[i]
            remap[char.id] = canonical.id
            for alias in [char.name, *char.aliases]:
                if identity(alias) and (alias==char.name or specific_label(alias)):
                    aliases.setdefault(identity(alias), alias)
            if i != indexes[0]:
                warnings.append(f"characters: объединены {char.id} → {canonical.id}; совпали имя, alias или id")
        if len(indexes) > 1:
            canonical.aliases = list(aliases.values())
        else:
            canonical.aliases = list(dict((identity(a), a) for a in canonical.aliases if identity(a) and specific_label(a)).values())
            if canonical.aliases != chars[indexes[0]].aliases:
                warnings.append(f"characters[{canonical.id}]: удалены пустые или повторные aliases")
        merged.append(canonical)
    reg.characters = merged
    for field in ("locations", "props"):
        unique = {}
        for item in getattr(reg, field):
            if item.id in unique:
                warnings.append(f"{field}: повторный id {item.id}; сохранена первая запись")
            else:
                unique[item.id] = item
        setattr(reg, field, list(unique.values()))
    return reg, remap, warnings


def normalize_scenes(scenes: list[Scene], reg: Registry, remap: dict[str, str]) -> tuple[list[Scene], list[str]]:
    warnings: list[str] = []
    ordered = sorted(enumerate(scenes), key=lambda pair: pair[1].index)
    if [i for i, _ in ordered] != list(range(len(scenes))):
        warnings.append("scenes: отсортированы по index")
    known_ch = {c.id for c in reg.characters}
    known_loc = {l.id for l in reg.locations}
    result = []
    for index, (_, original) in enumerate(ordered):
        scene = original.model_copy(deep=True)
        if scene.index != index:
            warnings.append(f"scene[{index}]: index {scene.index} → {index}")
        scene.index = index
        if scene.location_id not in known_loc:
            warnings.append(f"scene[{index}]: неизвестная локация {scene.location_id!r}; ссылка очищена")
            scene.location_id = ""
        ids = []
        for old in scene.character_ids:
            new = remap.get(old, old)
            if new != old:
                warnings.append(f"scene[{index}]: персонаж {old} → {new}")
            if new not in known_ch:
                warnings.append(f"scene[{index}]: неизвестный персонаж {old!r}; ссылка удалена")
            elif new in ids:
                warnings.append(f"scene[{index}]: повторная ссылка {new}; удалена")
            else:
                ids.append(new)
        scene.character_ids = ids
        result.append(scene)
    return result, warnings
