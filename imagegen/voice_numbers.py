"""Conservative Russian number equivalence; no model or extra runtime package."""
import re
from decimal import Decimal

FORMS = {}
def add(value, words):
    for word in words.split(): FORMS[word] = (value, False)
add(0,'ноль нуль нуля нулю нулем нулём нуле')
add(1,'один одна одно одни одного одной одному одну одним одними одном одних')
add(2,'два две двух двум двумя')
add(3,'три трех трёх трем трём тремя')
add(4,'четыре четырех четырёх четырем четырём четырьмя')
for value, word in enumerate('пять шесть семь восемь девять десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать'.split(),5):
    if value == 8:
        add(value,'восемь восьми восемью восьмью')
    else:
        add(value,word+' '+word[:-1]+'и '+word[:-1]+'ью')
for value, words in {
    20:'двадцать двадцати двадцатью',30:'тридцать тридцати тридцатью',40:'сорок сорока',
    50:'пятьдесят пятидесяти пятьюдесятью',60:'шестьдесят шестидесяти шестьюдесятью',
    70:'семьдесят семидесяти семьюдесятью',80:'восемьдесят восьмидесяти восемьюдесятью',90:'девяносто девяноста',
    100:'сто ста',200:'двести двухсот двумстам двумястами двухстах',300:'триста трехсот трёхсот тремстам трёмстам тремястами трехстах трёхстах',
    400:'четыреста четырехсот четырёхсот четыремстам четырёмстам четырьмястами четырехстах четырёхстах',
    500:'пятьсот пятисот пятистам пятьюстами пятистах',600:'шестьсот шестисот шестистам шестьюстами шестистах',
    700:'семьсот семисот семистам семьюстами семистах',800:'восемьсот восьмисот восьмистам восемьюстами восьмистах',
    900:'девятьсот девятисот девятистам девятьюстами девятистах',
    1000:'тысяча тысячи тысяч тысяче тысячу тысячей тысячами тысячах',
    1000000:'миллион миллиона миллионов миллиону миллионом миллионами миллионах',
    Decimal('1.5'):'полтора полторы полутора',
}.items(): add(value,words)
ORDINAL_STEMS = dict(enumerate('перв втор трет четверт пят шест седьм восьм девят десят одиннадцат двенадцат тринадцат четырнадцат пятнадцат шестнадцат семнадцат восемнадцат девятнадцат'.split(),1))
ORDINAL_STEMS.update({20:'двадцат',30:'тридцат',40:'сороков',50:'пятидесят',60:'шестидесят',70:'семидесят',80:'восьмидесят',90:'девяност',100:'сот'})
ORDINAL_STEMS.update({200:'двухсот',300:'трехсот',400:'четырехсот',500:'пятисот',
                      600:'шестисот',700:'семисот',800:'восьмисот',900:'девятисот',1000:'тысячн'})
for value, stem in ORDINAL_STEMS.items():
    endings='ый ой ая ое ые ого ому ым ом ую ых ыми'.split() if value!=3 else 'ий ья ье ьи ьего ьему ьим ьем ью ьих ьими'.split()
    for ending in endings:FORMS[stem+ending]=(value,True)

def value(words, allow_roman=False):
    """Only a complete, descending number phrase; reject ambiguous additions."""
    parts=[]
    for word in words:
        key=word.casefold().replace('ё','е')
        if re.fullmatch(r'\d+(?:[.,]\d+)?',key):
            parts.append((Decimal(key.replace(',','.')),False,'digits'))
        elif key in FORMS:
            number, ordinal=FORMS[key];parts.append((Decimal(number),ordinal,'word'))
        elif allow_roman and re.fullmatch(r'M{0,3}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})',word) and word:
            roman={'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
            number=sum(-roman[c] if i+1<len(word) and roman[c]<roman[word[i+1]] else roman[c] for i,c in enumerate(word))
            parts.append((Decimal(number),True,'roman'))
        else:return None
    if not parts or len(parts)>9:return None
    total=Decimal(0);group=Decimal(0);previous=Decimal('Infinity');scale=Decimal('Infinity')
    for i,(number,ordinal,kind) in enumerate(parts):
        if ordinal and i!=len(parts)-1:return None
        if number in (1000,1000000) and kind=='word':
            if number>=scale:return None
            total+=(group or 1)*number;group=Decimal(0);previous=Decimal('Infinity');scale=number
            continue
        if len(parts)>1 and (number==0 or number%1):return None
        if number>=previous:return None
        # A compound number is hundreds + tens + units, never "five four".
        if previous.is_finite() and (previous<10 or (10<=previous<20) or (previous>=100 and number>=100)):return None
        if 20<=previous<100 and number>=10:return None
        if kind in ('digits','roman') and len(parts)>1 and not (i+1<len(parts) and parts[i+1][0] in (1000,1000000)):
            return None
        group+=number;previous=number
    return total+group
