"""Deterministic CPU geometry/classification; no OCR/model/network dependency."""
import cv2
import numpy as np

OTHER_DIR = '其他画面'


def content_box(gray,cfg):
    active=gray>cfg.border_black
    ys=np.flatnonzero(active.mean(axis=1)>cfg.border_active_fraction)
    xs=np.flatnonzero(active.mean(axis=0)>cfg.border_active_fraction)
    if not len(xs) or not len(ys):return (0,0,gray.shape[1],gray.shape[0])
    return int(xs[0]),int(ys[0]),int(xs[-1]+1),int(ys[-1]+1)


def content_pair(a,b,cfg):
    xa,ya,wa,ha=content_box(a,cfg);xb,yb,wb,hb=content_box(b,cfg)
    aa=a[ya:ha,xa:wa];bb=b[yb:hb,xb:wb]
    if bb.shape!=aa.shape:bb=cv2.resize(bb,(aa.shape[1],aa.shape[0]),interpolation=cv2.INTER_AREA)
    return aa,bb


def footer_mask(shape,cfg):
    h,w=shape;mask=np.zeros(shape,np.uint8)
    mask[int(h*cfg.footer_top):,int(w*cfg.footer_right):]=1
    return mask.astype(bool)


def clean_delta(a,b,cfg):
    """Work in slide-content coordinates; number-region pixels are never removed."""
    a,b=content_pair(a,b,cfg)
    d=cv2.absdiff(a,b);mask=(d>cfg.pixel_delta).astype(np.uint8)
    protected=footer_mask(a.shape,cfg)
    if not mask.any():return d*0,False
    if (mask.astype(bool)&protected).any():return np.where(mask,d,0),True
    n,labels,parts,_=cv2.connectedComponentsWithStats(mask,8)
    scale=a.shape[1]/320
    # A paragraph of small glyphs is not a cursor. Only a sparse pair of compact
    # components can be an old/new pointer. LUT indexing avoids O(Ncomponents*Npixels).
    if n-1<=cfg.cursor_max_components:
        keep=(parts[:,cv2.CC_STAT_AREA]>cfg.cursor_component_pixels*scale*scale)
        keep|=(parts[:,cv2.CC_STAT_WIDTH]>cfg.cursor_component_extent*scale)
        keep|=(parts[:,cv2.CC_STAT_HEIGHT]>cfg.cursor_component_extent*scale)
        keep[0]=False;mask=keep[labels]
    return np.where(mask,d,0),False


def number_changed(a,b,cfg):
    a,b=content_pair(a,b,cfg);mask=(cv2.absdiff(a,b)>cfg.pixel_delta).astype(np.uint8)
    footer=footer_mask(a.shape,cfg)
    if not (mask.astype(bool)&footer).any():return False
    n,labels,parts,_=cv2.connectedComponentsWithStats(mask,8)
    for i in range(1,n):
        x,y,w,h,area=parts[i]
        if footer[y:y+h,x:x+w].any() and w<=a.shape[1]*cfg.number_component_width and h<=a.shape[0]*cfg.number_component_height:return True
    return False


def classify(gray,cfg,kind='stable'):
    x0,y0,x1,y1=content_box(gray,cfg);a=gray[y0:y1,x0:x1]
    h,w=a.shape;aspect=w/max(1,h)
    bright=float((a>cfg.class_bright_level).mean())
    # Window chrome: several broad horizontal separators near the top. A normal
    # slide title/rule is not enough to trigger this veto.
    upper=a[:max(2,int(h*cfg.class_chrome_height))].astype(np.int16)
    broad=(np.abs(np.diff(upper,axis=0))>cfg.class_chrome_delta).mean(axis=1)
    rows=np.flatnonzero(broad>cfg.class_chrome_width)
    groups=1+int(np.count_nonzero(np.diff(rows)>1)) if len(rows) else 0
    bottom=a[int(h*(1-cfg.class_taskbar_height)):]
    taskbar=float((bottom<cfg.class_taskbar_level).mean())
    features=dict(box=[x0,y0,x1,y1],aspect=aspect,bright_fraction=bright,chrome_bands=groups,taskbar_dark_fraction=taskbar)
    if kind=='video':return 'other','video_representative',features
    if not cfg.class_aspect_min<=aspect<=cfg.class_aspect_max:return 'other','window_geometry',features
    if bright<cfg.class_bright_fraction:return 'other','non_slide_background',features
    if groups>=cfg.class_chrome_bands:return 'other','window_chrome',features
    if taskbar>=cfg.class_taskbar_fraction:return 'other','taskbar_or_window_overlay',features
    return 'slide','slide_canvas',features


def context_equal(a,b,cfg):
    """Local animation must retain BOTH its heading and protected page number."""
    a,b=content_pair(a,b,cfg)
    footer=footer_mask(a.shape,cfg)
    # Without an observed number-region marker, retain the older broad context
    # check: identical chapter headings alone must not merge different slides.
    marker_count=int(((a<cfg.ink_threshold)&footer).sum())
    has_number=0<marker_count<=cfg.number_marker_pixels*(a.shape[1]/320)**2
    if has_number and np.any(cv2.absdiff(a,b)[footer]>cfg.pixel_delta):return False
    fraction=cfg.context_title_height if has_number else cfg.motion_context_height
    hh=max(1,round(a.shape[0]*fraction))
    d=cv2.absdiff(a[:hh],b[:hh])
    if d.mean()>cfg.stable_mad or (d>cfg.pixel_delta).mean()>cfg.motion_context_frac:return False
    # Limit fallback grouping to localized changes, not wholesale page edits.
    d=cv2.absdiff(a,b)
    return float((d>cfg.pixel_delta).mean())<=cfg.context_change_fraction


def heading_equal(a,b,cfg):
    a,b=content_pair(a,b,cfg);hh=max(1,round(a.shape[0]*cfg.context_title_height))
    d=cv2.absdiff(a[:hh],b[:hh])
    return d.mean()<=cfg.stable_mad and (d>cfg.pixel_delta).mean()<=cfg.motion_context_frac
