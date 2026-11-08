// Annular window, optionally rotated and shifted along the spool axis.
function windowOutline(rotation=0,shift=0) {
  const windowPoint=(radius,degrees)=>{
    const radians=(degrees+rotation)*Math.PI/180;
    return `${shift+radius*Math.sin(radians)} ${-radius*Math.cos(radians)}`;
  };
  const inner=8, outer=14.4;
  return `M${windowPoint(inner,-16)} L${windowPoint(outer,-16)} A${outer} ${outer} 0 0 1 ${windowPoint(outer,16)} L${windowPoint(inner,16)} A${inner} ${inner} 0 0 0 ${windowPoint(inner,-16)} Z`;
}

// Wrap the face marking onto the near half of the cylindrical rim edge.
function rimMarkPath(rotation,depth) {
  const radius=17;
  const halfWidth=Math.asin(2.875/radius);
  const center=(((rotation+180)%360+540)%360-180)*Math.PI/180;
  const start=Math.max(-Math.PI/2,center-halfWidth);
  const end=Math.min(Math.PI/2,center+halfWidth);
  if (start>=end) return '';
  // Overlap the face slightly so antialiasing cannot leave a seam in the paint.
  const faceDepth=depth+Math.min(.12,depth);
  const x1=radius*Math.cos(start), y1=radius*Math.sin(start);
  const x2=radius*Math.cos(end), y2=radius*Math.sin(end);
  return `M${x1-faceDepth} ${y1} A${radius} ${radius} 0 0 1 ${x2-faceDepth} ${y2} H${x2} A${radius} ${radius} 0 0 0 ${x1} ${y1} Z`;
}

/** Render one spool in local SVG coordinates. No telemetry, DOM, or animation state.
 * Angles are radians for viewAngle and degrees for rotation. The caller owns
 * placement, scale, clipping, and animation; idPrefix must be unique and SVG-safe.
 */
export function renderSpool({idPrefix, style, color, fillPercent, viewAngle,
  rotation=0, arrowOpacity=0, direction='assist', glow=0, showFillPercent=false, shadowStyle='blur', windingLines=true}) {
  const spoolStyle=style, percent=fillPercent, spoolViewAngle=viewAngle;
  const angle=rotation, fade=arrowOpacity;
  const solidSpool=style.endsWith('-solid');
  const crispWindows=shadowStyle==='crisp' && !solidSpool;
  const faceFilter=crispWindows ? '' : `filter="url(#spool-cutout-shadow-${idPrefix})"`;
  const windowOpenings=(front=false)=>[30,90,150,210,270,330]
    .filter(rotation=>!front || rotation!==270).map(rotation=>windowOutline(rotation)).join(' ');
  const windowShadow=(front=false)=>{
    if (!crispWindows) return '';
    const face=front ? 'front-' : '';
    return `<g data-part="spool-window-shadow" transform="translate(1.1 .8)" opacity=".65"><circle r="17" fill="black" clip-path="url(#spool-${face}spokes-${idPrefix})"/></g>`;
  };
  const spoolFaceScale=Math.max(.001,Math.cos(viewAngle));
  const spoolDepth=34/3, spoolHubRadius=6.4, spoolBoreRadius=5.8;
  // Preserve the existing arrow offset in local coordinates.
  const spoolScale=3.3;
  const spoolDepthOffset=spoolDepth*Math.sin(viewAngle)/spoolFaceScale;
  const fullWindingRadius=16;
  const windingRadius=Math.sqrt(spoolHubRadius**2+(fullWindingRadius**2-spoolHubRadius**2)*percent/100);
  let svg=`<defs><radialGradient id="spool-backlight-${idPrefix}"><stop stop-color="#fff" stop-opacity=".85"/><stop offset=".25" stop-color="#fff2bd" stop-opacity=".6"/><stop offset="1" stop-color="#ffe6a0" stop-opacity="0"/></radialGradient><radialGradient id="spool-led-${idPrefix}"><stop stop-color="white"/><stop offset=".3" stop-color="#fff7dc" stop-opacity=".9"/><stop offset=".65" stop-color="#ffe6a0" stop-opacity=".45"/><stop offset="1" stop-color="#ffe6a0" stop-opacity="0"/></radialGradient></defs>`;
  // Lighting stays fixed while the spokes turn underneath it.
  const material=spoolStyle.startsWith('dark-') ? '#343638' : 'var(--spool-material, #b49164)';
  const windowPath=windowOutline();
  const spokes=(front=false)=>`<path data-spool-rotation="0" transform="rotate(${angle})" clip-rule="evenodd" d="M0 -17 A17 17 0 1 1 0 17 A17 17 0 1 1 0 -17 Z ${windowOpenings(front)}"/>`;
  svg += `<defs><clipPath id="spool-face-clip-${idPrefix}"><circle r="17"/></clipPath>${crispWindows ? '' : `<filter id="spool-cutout-shadow-${idPrefix}" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="1.1" dy=".8" stdDeviation=".25" flood-color="#000" flood-opacity=".65"/></filter>`}<linearGradient id="spool-face-${idPrefix}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="color-mix(in srgb, ${material}, white 32%)"/><stop offset=".45" stop-color="${material}"/><stop offset="1" stop-color="color-mix(in srgb, ${material}, black 38%)"/></linearGradient><linearGradient id="spool-edge-${idPrefix}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="color-mix(in srgb, ${material}, white 60%)"/><stop offset=".4" stop-color="${material}"/><stop offset="1" stop-color="color-mix(in srgb, ${material}, black 65%)"/></linearGradient><linearGradient id="spool-bore-${idPrefix}" x1="0" y1="0" x2="1" y2="1"><stop stop-color="color-mix(in srgb, ${material}, black 88%)"/><stop offset=".6" stop-color="color-mix(in srgb, ${material}, black 65%)"/><stop offset="1" stop-color="color-mix(in srgb, ${material}, black 35%)"/></linearGradient>${solidSpool ? '' : `<clipPath id="spool-spokes-${idPrefix}">${spokes()}</clipPath><clipPath id="spool-front-spokes-${idPrefix}">${spokes(true)}</clipPath>`}</defs>`;
  svg += `<defs><linearGradient id="spool-core-shade-${idPrefix}" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, ${material}, black 25%)"/><stop offset=".4" stop-color="${material}"/><stop offset=".65" stop-color="color-mix(in srgb, ${material}, white 15%)"/><stop offset="1" stop-color="color-mix(in srgb, ${material}, black 35%)"/></linearGradient></defs>`;
  // Axial flange thickness stays visible edge-on, unlike the projected disc face.
  const rimDepth=.65*Math.sin(spoolViewAngle)/spoolFaceScale;
  const rimSidewall=center=>`<path data-part="spool-rim-sidewall" d="M${center-rimDepth} -17 H${center} A17 17 0 0 1 ${center} 17 H${center-rimDepth} A17 17 0 0 0 ${center-rimDepth} -17 Z" fill="url(#spool-edge-${idPrefix})"/>`;
  // Intersect each window with the inverse of its shifted opening. Vector clips
  // avoid a separate raster mask per window; thickness stays on the spool axis.
  const windowWalls=(front=false)=>solidSpool ? '' : [30,90,150,210,270,330]
    .filter(rotation=>!front || rotation!==270).map(rotation=>{
      const id=`spool-window-wall-${idPrefix}-${front ? 'front' : 'rear'}-${rotation}`;
      const opening=`<path data-spool-rotation="${rotation}" transform="rotate(${angle+rotation})" d="${windowPath}"/>`;
      return `<defs><clipPath id="${id}-clip">${opening}</clipPath><clipPath id="${id}-cut"><path data-spool-window-cut="${rotation}" data-depth="${rimDepth}" clip-rule="evenodd" d="M-18 -18 H18 V18 H-18 Z ${windowOutline(angle+rotation,rimDepth)}"/></clipPath></defs><g clip-path="url(#${id}-cut)"><rect data-part="spool-window-sidewall" x="-18" y="-18" width="36" height="36" fill="url(#spool-edge-${idPrefix})" clip-path="url(#${id}-clip)"/></g>`;
    }).join('');
  // The dot lives on the front face, so its orbit and shape share the face projection.
  const faceDot=`<g data-part="spool-rotation-dot" data-spool-rotation="0" transform="rotate(${angle})"><path d="M-18 -2.4 H-15 A2.4 2.4 0 0 1 -15 2.4 H-18 Z" fill="#fff" stroke="#fff" stroke-width=".95"/><circle cx="-15" cy="0" r="1.4375" fill="#c62828"/></g>`;
  svg += `<g transform="scale(${spoolFaceScale} 1)">${rimSidewall(spoolDepthOffset+rimDepth)}<g data-part="spool-rear-face" transform="translate(${spoolDepthOffset} 0)"><circle r="16.5" fill="none" stroke="url(#spool-edge-${idPrefix})" stroke-width="1" opacity=".3"/><g clip-path="url(#spool-face-clip-${idPrefix})"><g ${faceFilter}>${windowShadow()}<circle r="17" fill="url(#spool-face-${idPrefix})" ${solidSpool ? '' : `clip-path="url(#spool-spokes-${idPrefix})"`}/>${windowWalls()}<circle r="${spoolHubRadius}" fill="${material}"/></g></g><circle r="${spoolBoreRadius}" fill="url(#spool-bore-${idPrefix})"/></g><defs><linearGradient id="spool-winding-shade-${idPrefix}" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, #${color}, black 25%)"/><stop offset=".4" stop-color="#${color}"/><stop offset=".65" stop-color="color-mix(in srgb, #${color}, white 15%)"/><stop offset="1" stop-color="color-mix(in srgb, #${color}, black 35%)"/></linearGradient></defs><path data-part="spool-core-tube" d="M0 -${spoolHubRadius} H${spoolDepthOffset} A${spoolHubRadius} ${spoolHubRadius} 0 0 1 ${spoolDepthOffset} ${spoolHubRadius} H0 Z" fill="url(#spool-core-shade-${idPrefix})"/><defs><linearGradient id="spool-core-contact-fade-${idPrefix}" gradientUnits="userSpaceOnUse" x1="0" y1="${-spoolHubRadius}" x2="0" y2="${spoolHubRadius}"><stop stop-color="black" stop-opacity="0"/><stop offset=".25" stop-color="black" stop-opacity=".55"/><stop offset=".75" stop-color="black" stop-opacity=".55"/><stop offset="1" stop-color="black" stop-opacity="0"/></linearGradient></defs><path data-part="spool-core-contact-shadow" d="M${spoolDepthOffset} -${spoolHubRadius} A${spoolHubRadius} ${spoolHubRadius} 0 0 1 ${spoolDepthOffset} ${spoolHubRadius}" fill="none" stroke="url(#spool-core-contact-fade-${idPrefix})" stroke-width=".6"/><path d="M0 -${windingRadius} H${spoolDepthOffset} A${windingRadius} ${windingRadius} 0 0 1 ${spoolDepthOffset} ${windingRadius} H0 Z" fill="${percent ? `url(#spool-winding-shade-${idPrefix})` : 'none'}"/>${Array.from({length:percent && windingLines ? 18 : 0},(_,i)=>(i+1)*spoolDepthOffset/19).map(depth=>`<path d="M${depth} -${windingRadius} A${windingRadius} ${windingRadius} 0 0 1 ${depth} ${windingRadius}" fill="none" stroke="color-mix(in srgb, #${color}, black 50%)" stroke-width=".06" opacity=".85"/><path d="M${depth+.18} -${windingRadius} A${windingRadius} ${windingRadius} 0 0 1 ${depth+.18} ${windingRadius}" fill="none" stroke="color-mix(in srgb, #${color}, white 55%)" stroke-width=".06" opacity=".75"/>`).join('')}`;
  if (percent>0) {
    // Contact shadow from the front flange, fading into the first winding turns.
    svg += `<defs><linearGradient id="spool-contact-shadow-${idPrefix}" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="${spoolDepthOffset*.2}" y2="0"><stop stop-color="black" stop-opacity=".75"/><stop offset=".35" stop-color="black" stop-opacity=".4"/><stop offset="1" stop-color="black" stop-opacity="0"/></linearGradient><clipPath id="spool-contact-clip-${idPrefix}"><path d="M0 -${windingRadius} H${spoolDepthOffset} A${windingRadius} ${windingRadius} 0 0 1 ${spoolDepthOffset} ${windingRadius} H0 Z"/></clipPath></defs><rect data-part="spool-contact-shadow" x="0" y="${-windingRadius}" width="${spoolDepthOffset*.2}" height="${windingRadius*2}" fill="url(#spool-contact-shadow-${idPrefix})" clip-path="url(#spool-contact-clip-${idPrefix})"/>`;
  }
  if (glow>0) {
    // Light the winding before painting the front flange, so the rim occludes it.
    svg += `<g data-part="selected-spool-backlight" opacity="${glow}" pointer-events="none">${percent>0 ? `<ellipse data-part="spool-winding-light" cx="${spoolDepthOffset/2+windingRadius*.35}" cy="11" rx="${spoolDepthOffset*.7}" ry="7" fill="url(#spool-backlight-${idPrefix})" clip-path="url(#spool-contact-clip-${idPrefix})"/>` : ''}</g>`;
  }
  svg += `${rimSidewall(0)}<g transform="translate(${-rimDepth} 0)"><circle r="16.5" fill="none" stroke="url(#spool-edge-${idPrefix})" stroke-width="1" opacity=".3"/>${percent ? `<circle r="${windingRadius}" fill="#${color}"/>` : ''}<g clip-path="url(#spool-face-clip-${idPrefix})"><g ${faceFilter}>${windowShadow(true)}<circle r="17" fill="url(#spool-face-${idPrefix})" ${solidSpool ? '' : `clip-path="url(#spool-front-spokes-${idPrefix})"`}/>${windowWalls(true)}<circle r="${spoolHubRadius}" fill="${material}"/></g>${faceDot}</g><circle data-part="spool-core-bore" r="${spoolBoreRadius}" fill="url(#spool-bore-${idPrefix})"/></g>`;
  svg += `<path data-part="spool-rim-mark" data-depth="${rimDepth}" d="${rimMarkPath(angle,rimDepth)}" fill="#fff"/></g>`;
  if (fade>0) {
    // Keep the arrow on the near side of the full winding, independent of spool rotation.
    const arrowX=spoolDepth*Math.sin(spoolViewAngle)/2+(fullWindingRadius+.6)*Math.cos(spoolViewAngle)-.8-2/spoolScale;
    svg += `<defs><linearGradient id="spool-arrow-fade-${idPrefix}" gradientUnits="userSpaceOnUse" x1="0" y1="-16.6" x2="0" y2="-5"><stop stop-color="white" stop-opacity=".333"/><stop offset=".4" stop-color="white" stop-opacity=".6"/><stop offset="1" stop-color="white"/></linearGradient><mask id="spool-arrow-mask-${idPrefix}" maskUnits="userSpaceOnUse" x="-6" y="-18" width="12" height="23"><rect x="-6" y="-18" width="12" height="23" fill="url(#spool-arrow-fade-${idPrefix})"/></mask></defs><g data-part="espooler-direction" data-direction="${direction}" opacity="${fade}" transform="translate(${arrowX} 0) scale(1 ${direction==='rewind' ? -1 : 1})" pointer-events="none"><path d="M-3.2 -16.6 C.8 -15.2 2.2 -11 2.1 -7 L3.7 -7 L1 -2.5 L-2.2 -6.8 L-.7 -6.8 C-.2 -10.3 -.8 -14.2 -3.2 -16.6 Z" fill="#c4c8cc" fill-opacity=".8" stroke="#d4d7da" stroke-opacity="1" stroke-width=".4" stroke-linejoin="round" mask="url(#spool-arrow-mask-${idPrefix})"/></g>`;
  }
  if (glow>0) {
    svg += `<ellipse data-part="selected-spool-backlight" cx="${spoolDepth*Math.sin(spoolViewAngle)/2+4/spoolScale}" cy="10.5" rx="3" ry=".9" opacity="${glow}" fill="url(#spool-led-${idPrefix})" pointer-events="none"/>`;
  }
  if (showFillPercent && Number.isFinite(fillPercent)) {
    const label=Math.round(Math.max(0,Math.min(100,fillPercent)));
    const x=spoolDepth*Math.sin(spoolViewAngle)/2+windingRadius*Math.cos(spoolViewAngle)+.6;
    svg += `<text data-part="spool-fill-label" x="${x}" y="0" text-anchor="middle" dominant-baseline="middle" font-size="3.2" font-weight="600" fill="#f4f5f6" stroke="#17212bcc" stroke-width=".55" paint-order="stroke" stroke-linejoin="round" pointer-events="none">${label}%</text>`;
  }
  return svg;
}

/** Retained SVG component. Mount in a caller-owned <g>; only this component
 * owns its descendants. No clock or subscriptions: update from props each frame.
 */
export class SpoolRenderer {
  constructor(host) {
    this.element=host;
    this.geometryKey=null;
    this.motion={};
  }

  mount(host) {
    if (this.element === host) return;
    host.replaceChildren(...this.element.childNodes);
    this.element=host;
  }

  update(options) {
    const {idPrefix,style,color,fillPercent,viewAngle,showFillPercent,shadowStyle,windingLines}=options;
    const key=JSON.stringify([idPrefix,style,color,fillPercent,viewAngle,showFillPercent,shadowStyle,windingLines]);
    if (key !== this.geometryKey) {
      // Keep invisible arrow/glow nodes mounted so fading never recreates geometry.
      this.element.innerHTML=renderSpool({...options,rotation:0,arrowOpacity:1,glow:1});
      this.rotating=[...this.element.querySelectorAll('[data-spool-rotation]')]
        .map(node=>({node,offset:Number(node.dataset.spoolRotation)}));
      this.windowCuts=[...this.element.querySelectorAll('[data-spool-window-cut]')]
        .map(node=>({node,offset:Number(node.dataset.spoolWindowCut),depth:Number(node.dataset.depth)}));
      this.rimMark=this.element.querySelector('[data-part="spool-rim-mark"]');
      this.rimMarkDepth=Number(this.rimMark.dataset.depth);
      this.arrow=this.element.querySelector('[data-part="espooler-direction"]');
      this.arrowPosition=this.arrow.getAttribute('transform').split(' scale(')[0];
      this.glows=[...this.element.querySelectorAll('[data-part="selected-spool-backlight"]')];
      this.geometryKey=key;
      this.motion={};
    }
    const {rotation=0,arrowOpacity=0,direction='assist',glow=0}=options;
    this.updateMotion({rotation,arrowOpacity,direction,glow});
  }

  updateMotion({rotation=this.motion.rotation ?? 0, arrowOpacity=this.motion.arrowOpacity ?? 0,
    direction=this.motion.direction ?? 'assist', glow=this.motion.glow ?? 0}) {
    if (rotation !== this.motion.rotation) {
      this.rimMark.setAttribute('d',rimMarkPath(rotation,this.rimMarkDepth));
      for (const {node,offset} of this.rotating) node.setAttribute('transform',`rotate(${rotation+offset})`);
      for (const {node,offset,depth} of this.windowCuts)
        node.setAttribute('d',`M-18 -18 H18 V18 H-18 Z ${windowOutline(rotation+offset,depth)}`);
    }
    if (arrowOpacity !== this.motion.arrowOpacity) this.arrow.setAttribute('opacity',arrowOpacity);
    if (direction !== this.motion.direction) {
      this.arrow.dataset.direction=direction;
      this.arrow.setAttribute('transform',`${this.arrowPosition} scale(1 ${direction==='rewind' ? -1 : 1})`);
    }
    if (glow !== this.motion.glow) for (const node of this.glows) node.setAttribute('opacity',glow);
    this.motion={rotation,arrowOpacity,direction,glow};
  }
}
