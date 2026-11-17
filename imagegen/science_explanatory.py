"""Source-bound explanatory graphics shared by gallery, player and GPU export."""
import re,math
from functools import lru_cache

@lru_cache(maxsize=1)
def morphology():
    import pymorphy3
    return pymorphy3.MorphAnalyzer()

def terms(text):
    return {morphology().parse(w)[0].normal_form for w in re.findall(r'[а-яёa-z]+',text.casefold()) if len(w)>2}

def clean_notes(notes,limit=2):
    out=[]
    for value in notes:
        if not isinstance(value,str):continue
        value=value.strip()
        if not value or len(value)>110 or len(value.split())>8:continue
        if out:
            a,b=terms(out[0]),terms(value)
            # Subject plus a description of the same subject is one label, not an explanation.
            generic={'целый','видимый','человек','изолированный','показать','общий','вид','оболочка'}
            if '→' not in out[0] and '→' not in value and len(a&b)/max(1,min(len(a),len(b)))>=.65 and not (b-a-generic):continue
        if re.search(r'^(целое|изолированное|показан[аоы]?|видим[а-я]*|общий вид)\b',value,re.I):continue
        if value.casefold() not in {n.casefold() for n in out}:out.append(value)
    return out[:limit]

def nominative(text):
    words=[]
    for w in text.split():
        parsed=morphology().parse(w)[0]
        form=parsed.inflect({'nomn'}) if parsed.tag.POS in ('NOUN','ADJF','PRTF') and not (parsed.tag.POS=='NOUN' and w.casefold()==parsed.normal_form) else None
        words.append(form.word if form else w)
    return ' '.join(words)

def infer_scheme(plan,material,context=""):
    """Conservative migrations only. Future plans supply the structured diagram explicitly."""
    source=plan.get('support_quote','');focus=plan.get('focus','');state=material.get('state','')
    if not source:return None
    # No guessed containment from unrelated mentions: the reviewed state must spell it out.
    match=re.fullmatch(r'(.{2,35}?) в (.{2,45}?) внутри (.{2,45}?)[.]?',state,re.I)
    if match and all(any(t in terms(source+' '+context) for t in terms(part)) for part in match.groups()):
        labels=[nominative(v) for v in reversed(match.groups())]
        return {'kind':'layers','labels':labels,'cue_quote':source}
    if all(w in focus.casefold() for w in ('свет','глаз','распознаван')) and 'нервных клеток' in source.casefold():
        return {'kind':'signal','labels':['Свет','Глаз','Сеть нейронов'],'cue_quote':source}
    if 'диаметр' in focus.casefold():
        n=re.search(r'(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*(?:мм|миллиметр)',source,re.I)
        value=n.group(1)+' мм' if n else None
        if not value:
            # Interpret the actual source number, not the asset's nominal dimensions.
            units=['ноль','один','два','три','четыре','пять','шесть','семь','восемь','девять','десять','одиннадцать','двенадцать','тринадцать','четырнадцать','пятнадцать','шестнадцать','семнадцать','восемнадцать','девятнадцать']
            numbers={v:i for i,v in enumerate(units)};numbers.update(dict(zip(['двадцать','тридцать','сорок','пятьдесят','шестьдесят','семьдесят','восемьдесят','девяносто'],range(20,100,10))))
            tokens=re.findall(r'[а-яё]+',source.casefold());run=[]
            for token in tokens:
                lemma=morphology().parse(token)[0].normal_form
                if lemma in numbers:run.append(numbers[lemma])
                elif lemma.startswith('миллиметр') and run:
                    if len(run)==1 or len(run)==2 and run[0]>=20 and run[0]%10==0 and 0<run[1]<10:value=str(sum(run))+' мм'
                    break
                else:run=[]
        if value:return {'kind':'measurement','labels':['Диаметр',value],'cue_quote':source}
    return None

def valid_scheme(scheme,source):
    return (isinstance(scheme,dict) and scheme.get('kind') in ('layers','signal','measurement','network')
        and len(scheme.get('labels',[]))==(2 if scheme['kind'] in ('measurement','network') else 3)
        and all(isinstance(x,str) and 0<len(x)<=55 and len(x.split())<=5 for x in scheme['labels'])
        and bool(scheme.get('cue_quote')) and scheme['cue_quote'] in source)

def draw_scheme(scheme,rect):
    x,y,w,h=rect;commands=[];accent='#79C9BE';muted='#A7B7B8';ivory='#ECEADB'
    def line(x1,y1,x2,y2,color=accent,width=3):commands.append({'type':'line','x1':x1,'y1':y1,'x2':x2,'y2':y2,'color':color,'width':width})
    def arrow(x1,y1,x2,y2,color=accent,width=3):
        line(x1,y1,x2,y2,color,width);angle=math.atan2(y2-y1,x2-x1)
        for offset in (-.52,.52):line(x2-12*math.cos(angle+offset),y2-12*math.sin(angle+offset),x2,y2,color,width)
    def circle(cx,cy,r,color):commands.append({'type':'circle','x':cx,'y':cy,'radius':r,'color':color})
    def ring(cx,cy,r,color=accent):commands.append({'type':'ring','x':cx,'y':cy,'radius':r,'width':3,'color':color})
    def text(value,tx,ty,size=28,color=ivory,weight=500):commands.append({'type':'text','text':value,'x':tx,'y':ty,'size':size,'weight':weight,'color':color})
    text('Условная схема',x,y+25,23,muted)
    labels=scheme['labels'];cy=y+h*.49
    if scheme['kind']=='layers':
        cx=x+w*.28;r=min(w*.24,h*.32)
        for scale,color in [(1,'#B9B4A0'),(.77,'#B1897D'),(.48,'#86B9B2')]:circle(cx,cy,r*scale,color)
        for i,(scale,label) in enumerate(zip((.94,.67,.25),labels)):
            ly=y+85+i*110;px=cx+r*scale;py=cy+(i-1)*r*.14
            (arrow(x+w*.58,ly-9,px,py) if scheme.get('arrows') else line(px,py,x+w*.58,ly-9));text(label,x+w*.60,ly,25)
    elif scheme['kind']=='measurement':
        cx=x+w*.5;r=min(w*.30,h*.31);ring(cx,cy,r);line(cx-r,cy,cx+r,cy)
        for px in (cx-r,cx+r):line(px,cy-14,px,cy+14)
        text(labels[0],x+w*.22,cy+r+55,27,muted);text(labels[1],x+w*.57,cy+r+55,34,ivory,650)
    elif scheme['kind']=='network':
        nodes=[(x+75,cy-75),(x+245,cy-125),(x+420,cy-50),(x+175,cy+60),(x+370,cy+100)]
        for i,(a,b) in enumerate(((0,1),(0,3),(1,2),(1,3),(2,4),(3,4))):
            ax,ay=nodes[a];bx,by=nodes[b];dx,dy=bx-ax,by-ay;length=max(1,math.hypot(dx,dy))
            arrow(ax+dx*15/length,ay+dy*15/length,bx-dx*17/length,by-dy*17/length,color='#557F78',width=2)
            commands.append({'type':'pulse','x1':nodes[a][0],'y1':nodes[a][1],'x2':nodes[b][0],'y2':nodes[b][1],'radius':6,'color':accent,'period_ms':2200,'phase':i/6})
        for nx,ny in nodes:circle(nx,ny,13,accent)
        text(labels[0],x+55,cy+190,30,ivory,600);text(labels[1],x+55,cy+235,26,muted)
    else:
        # An actual receptor icon and linked neural network, not three word bubbles.
        eye=x+w*.38;network=x+w*.80;ey=cy
        for off in (-32,0,32):line(x+18,ey+off,eye-50,ey+off)
        ring(eye,ey,43);circle(eye,ey,14,ivory)
        nodes=[(network-30,ey-55),(network+32,ey-26),(network-30,ey+18),(network+34,ey+55),(network+62,ey-75)]
        connector=arrow if scheme.get('arrows') else line
        connector(eye+46,ey,nodes[0][0],nodes[0][1]);connector(eye+46,ey,nodes[2][0],nodes[2][1])
        for a,b in ((0,1),(0,2),(1,3),(1,4),(2,3),(3,4)):line(*nodes[a],*nodes[b],width=2)
        for nx,ny in nodes:circle(nx,ny,8,accent)
        text(labels[0],x+8,ey+115,27,muted);text(labels[1],eye-30,ey+115,27,muted);text(labels[2],network-77,ey+155,27,muted)
    return commands
