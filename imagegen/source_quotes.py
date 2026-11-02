"""Validate application-owned source coordinates; read legacy literal quotes."""
import re


def pattern(quote):
    return re.escape(quote)


def locate(quote, story, start=0, end=None):
    end = len(story) if end is None else end
    if isinstance(quote,dict):
        a,b=quote.get('start'),quote.get('end')
        text=quote.get('text',quote.get('quote'))
        if (type(a) is not int or type(b) is not int or not max(0,start)<=a<b<=min(end,len(story))
                or text!=story[a:b]):
            raise ValueError('координаты цитаты не соответствуют доступному источнику.')
        return {'start':a,'end':b,'text':story[a:b]}
    if not isinstance(quote, str) or not quote.strip():
        raise ValueError('отсутствует точная цитата.')
    found = story.find(quote, start, end)
    if found >= 0:
        if story.find(quote, found + 1, end) < 0:
            return {'start':found, 'end':found+len(quote), 'text':story[found:found+len(quote)]}
    else:
        # Lookahead counts overlapping matches as well. Offsets always belong
        # to the original string; the source itself is never normalized.
        matches=list(re.finditer('(?=('+pattern(quote)+'))', story[start:end], re.I))
        if len(matches)==1:
            a,b=matches[0].span(1)
            return {'start':start+a, 'end':start+b, 'text':story[start+a:start+b]}
    raise ValueError('цитата отсутствует или неоднозначна; верните более длинную точную цитату: '+quote[:100])
