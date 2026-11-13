/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// The sync-feedback buffer drawing. App state (homing rings, the sensor pulse, the frame clock) is passed in.
// `gapFill` shades the gap the buffer cuts in the Bowden tube: black while shown, white once faded away.
import {escape} from './util.js';
import {BOWDEN_EXIT_X, bufferPiston, bufferSpringPath} from './playback.js';

// The housing is centred on the Bowden's horizontal run, 200 right of the exit.
const CENTER=BOWDEN_EXIT_X+200;

// Buffer drawing is isolated from the toolhead and filament layers.
export function renderBuffer(s, d, bufferSensors, sensorFill, {homingRing, pulse, now, gapFill='black'}) {
  let markup = '';
  const tension=bufferSensors.find(([n])=>n.endsWith('tension'))?.[1]?.state;
  const compression=bufferSensors.find(([n])=>n.endsWith('compression'))?.[1]?.state;
  const piston=bufferPiston(d,tension,compression);
  // Fixed housing; the inner piston tracks buffer travel.
  const tick=(travel)=>CENTER-28.8+57.6*(piston.maximum ? travel/piston.maximum : .5);
  const edge=tick(piston.position);
  // Retain protrusion even at the maximum-tension stop.
  const pistonRight=edge+80;
  markup += `<defs><mask id="buffer-tube-gap" maskUnits="userSpaceOnUse" x="0" y="0" width="850" height="700"><rect width="850" height="700" fill="white"/><rect x="${CENTER-38}" y="50" width="${pistonRight-4-CENTER+38}" height="50" fill="${gapFill}"/></mask></defs><g id="sync-buffer"><path data-part="buffer-filament-guide" d="M${pistonRight-4} 66.5 H${CENTER-38} M${pistonRight-4} 83.5 H${CENTER-38}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="1.2" opacity="0.3"/><text x="${CENTER}" y="38" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Sync-feedback buffer</text><g fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1.5"><rect id="buffer-housing" x="${CENTER-42}" y="46" width="84" height="51.88" rx="6.4"/><rect id="buffer-piston" x="${edge}" y="59" width="80" height="32" rx="5.6"/></g><g fill="var(--scene-tube-border, #b1c4d2)"><rect data-part="buffer-housing-port" x="${CENTER-44.4}" y="62.2" width="4.8" height="25.6" rx="2.4"/><rect data-part="buffer-piston-port" x="${pistonRight-2.4}" y="62.2" width="4.8" height="25.6" rx="2.4"/></g>`;
  const geometry=Object.values(s.buffers || {}).find(buffer=>buffer.gates.includes(s.gate));
  const spring=geometry?.spring ?? d.spring ?? 'none';
  const springSides = spring === 'neutral' || spring === 0 ? ['left','right']
    : spring === 'tension' || spring === -1 ? ['left']
    : spring === 'compression' || spring === 'compressed' || spring === 1 ? ['right'] : [];
  if (springSides.length) {
    markup += `<defs><linearGradient id="buffer-coil-shading" gradientUnits="userSpaceOnUse" x1="0" y1="48.8" x2="0" y2="56.2"><stop offset="0" stop-color="color-mix(in srgb, var(--scene-border, #96abba), white 45%)"/><stop offset="0.45" stop-color="color-mix(in srgb, var(--scene-border, #96abba), white 15%)"/><stop offset="1" stop-color="color-mix(in srgb, var(--scene-border, #96abba), black 30%)"/></linearGradient></defs><g id="buffer-springs" opacity="0.5" fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1.2" stroke-linejoin="round">`;
    let anchors=`<circle data-part="buffer-spring-anchor" cx="${edge}" cy="52.5" r="1.5" fill="var(--scene-border, #96abba)" stroke="none"/>`;
    for (const side of springSides) {
      const wall=side === 'left' ? CENTER-42 : CENTER+42;
      const coil=bufferSpringPath(wall,edge);
      markup += `<path data-part="buffer-spring" data-side="${side}" stroke="url(#buffer-coil-shading)" d="${coil}"/>`;
      anchors += `<circle data-part="buffer-spring-wall-anchor" cx="${wall}" cy="52.5" r="1.5" fill="var(--scene-border, #96abba)" stroke="none"/>`;
    }
    markup += anchors+`<path data-part="buffer-spring-piston-tick" d="M${edge} 48.5 V56.5" stroke-width="1.8" opacity="0.9"/>`+'</g>';
  }
  for (const travel of [0,piston.maximum]) {
    markup += `<path data-part="buffer-maxrange-tick" opacity="0.4" d="M${tick(travel)} 93.88 V101.88" stroke="var(--scene-border, #96abba)" stroke-width="1.5" data-tooltip="Physical travel limit · ${travel.toFixed(1)}mm"/>`;
  }
  for (const [x,label,on] of [[tick(piston.low),'tension',tension],[tick(piston.maximum/2),'',null],[tick(piston.high),'compression',compression]]) {
    if (!label) {
      markup += `<path d="M${x} 95.08 V101.08" stroke="var(--scene-border, #96abba)" stroke-width="1.5"/>`;
      continue;
    }
    const sensorName=bufferSensors.find(([n])=>n.endsWith(label.toLowerCase()))?.[0];
    markup += `<g class="sensor-marker${on === null ? ' sensor-disabled' : ''}" data-sensor="${escape(sensorName || '')}"><circle cx="${x}" cy="97.88" r="5" fill="${sensorFill(on)}" stroke="#698394"/><text x="${x}" y="116.88" text-anchor="${label === 'compression' ? 'start' : 'end'}" font-size="9" fill="var(--scene-text, #304c65)">${label}</text></g>`;
    if (sensorName) markup += homingRing(sensorName,x,97.88);
    if (sensorName && pulse?.sensor === sensorName) {
      const elapsed=(now()-pulse.start)/650;
      if (elapsed >= 0 && elapsed < 1) {
        const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
        markup += `<circle data-part="sensor-pulse" cx="${x}" cy="97.88" r="${reduced ? 10 : 7+elapsed*15}" fill="none" stroke="${pulse.reverse ? 'var(--sensor-clear-pulse)' : '#48c982'}" stroke-width="2.5" opacity="${1-elapsed}"/>`;
      }
    }
  }
  if (d.proportional) markup += `<text id="buffer-reading" x="${CENTER}" y="128.88" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">${(d.value || 0).toFixed(1)}</text>`;
  markup += '</g>';
  return markup;
}
