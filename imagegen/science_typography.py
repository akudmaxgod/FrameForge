"""Measure and wrap complete labels inside the video canvas; never truncate them."""
import copy,re

def fit_commands(commands,font):
    result=[]
    for command in commands:
        if command['type']!='text' or command.get('locked_typesetting'):result.append(command);continue
        c=copy.deepcopy(command);x=max(48,min(float(c['x']),1712));right=1856 if x>=1280 else 1872
        for other in commands:
            if other['type']=='text' and abs(other['y']-c['y'])<c['size']*.6 and other['x']>x+30:right=min(right,other['x']-18)
        width=max(32,right-x);y=max(32+float(c['size']),float(c['y']));height=min(150,1032-y+c['size']*.8)
        for other in commands:
            if other['type']=='text' and other['y']>y+8 and abs(other['x']-x)<width*.65:height=min(height,other['y']-y+ c['size']*.8-other['size']*.8-8)
        height=max(c['size']*1.1,height)
        def wrap(value,size):
            f=font(size,c['weight']);rows=[];line=''
            for word in str(value).split():
                if line and f.getlength(line+' '+word)>width:rows.append(line);line=''
                while f.getlength(word)>width and len(word)>1:
                    end=1
                    while end<len(word) and f.getlength(word[:end+1])<=width:end+=1
                    if line:rows.append(line);line=''
                    rows.append(word[:end]);word=word[end:]
                if word:line=(line+' '+word).strip()
            if line:rows.append(line)
            return rows
        size=round(c['size'])
        while True:
            rows=wrap(c['text'],size)
            if len(rows)*size*1.25<=height or size<=16:break
            size-=1
        y=min(y,1032-max(0,len(rows)-1)*size*1.25);y=max(32+size,y);offset=c.get('typing_offset',0)
        for i,row in enumerate(rows):
            item={**c,'x':x,'y':y+i*size*1.25,'size':size,'text':row,'text_max_width':width,'clip_box':[48,24,1824,1032]}
            if c.get('typing_cps'):item['typing_offset']=offset;offset+=len(row)+1
            result.append(item)
    return result
