"""Conservative geometry-based matte detection, independent of scene meaning.

Low-detail sky, snow and walls are not proof of framing. A straight matte
requires a constant RGB region and an abrupt seam over almost its full length.
Unknown/ambiguous edges are left for inspection rather than hard rejection.
"""
import numpy as np


def detect_matte(image):
    rgb=np.asarray(image.convert('RGB'),dtype=np.float32)
    h,w,_=rgb.shape
    clear={'detected':False,'version':3,'reason':'no_confirmed_matte'}
    if min(h,w)<20:return clear

    def verdict(reason,edges):
        clean=[{k:v for k,v in e.items() if k!='color'} for e in edges]
        narrow=all(e['width']<=(w if e['side'] in ('left','right') else h)*.025 for e in edges)
        return {'detected':not narrow,'version':3,
                'reason':'thin_matte_allowed' if narrow else reason,'edges':clean,
                'advisory':narrow}

    def band(lines, label):
        # lines: [distance from edge, position along edge, RGB]. All lines
        # must share one background color; row-wise flatness is insufficient.
        limit=max(1,int(len(lines)*.30))
        color=np.median(lines[0],axis=0)
        distance=np.max(np.abs(lines-color),axis=2)
        n=0
        for row in distance[:limit]:
            if float(np.quantile(row,.98))>3.0:break
            n+=1
        if n<max(4,int(len(lines)*.012)) or n>=limit:return None
        # Resizing/PNG conversion can soften a hard matte seam by 1–2 pixels.
        # Compare just beyond that transition, rather than mistaking it for sky.
        inside=lines[min(n+2,len(lines)-1)]
        # A cloud/gradient has no near-continuous sharp boundary.
        contrast=np.max(np.abs(inside-lines[n-1]),axis=1)
        seam=float(np.mean(contrast>10))
        if seam<.65:return None
        return {'side':label,'width':n,'color':color,'seam_fraction':seam}

    edges=[band(rgb.transpose(1,0,2),'left'),band(rgb[:,::-1].transpose(1,0,2),'right'),
           band(rgb,'top'),band(rgb[::-1],'bottom')]
    if all(edges) and max(np.max(np.abs(e['color']-edges[0]['color'])) for e in edges)<=4:
        return verdict('closed_constant_frame',edges)
    for a,b in [(edges[0],edges[1]),(edges[2],edges[3])]:
        if a and b and min(a['seam_fraction'],b['seam_fraction'])>=.95 and np.max(np.abs(a['color']-b['color']))<=4:
            return verdict('paired_constant_bands',[a,b])
    # A one-sided matte needs a neutral extreme color and a wide uninterrupted
    # strip. Colored walls/curtains are inherently ambiguous and do not fail.
    for edge in edges:
        if edge:
            c=edge['color'];dimension=w if edge['side'] in ('left','right') else h
            if edge['seam_fraction']>=.95 and edge['width']>=dimension*.04 and np.ptp(c)<=3 and (max(c)<4 or min(c)>251):
                return verdict('one_sided_constant_band',[edge])
    return clear


