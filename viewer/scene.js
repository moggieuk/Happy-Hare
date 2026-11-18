/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// The main view: draws one scene state into the host <svg>. It reads nothing from the page outside the host and
// changes nothing outside it; the caller passes what to draw (view) and how (settings).
import { SpoolRenderer, renderSpool, spoolWindingRadius, espoolerActive } from './spool.js';
import { SCENE_WIDTH, BUFFER_CENTER_X, BOWDEN_EXIT_X as EXIT, EXTRUDER_X as E, gateSelectionLit, rackIdleReturn, spoolTooltip, tweenTo, virtualSelectorLayout, syncIndicatorSide, bowdenLane, rackGrid, bowdenAttachment, toolheadProgress, toolheadDrawingRange, switchFilamentDistance, syncForDisplay, encoderFilamentDistance, excessFilamentLabel, constrainFilament, gearAngle, homingTarget, bladeStroke, bowdenLengthLabel, toolheadReferences, coolingTubeMeasurements, dimensionParameter, lastEncoderReading, encoderReference, gateDevicePositions, purgeOutletColor, purgeCoils, purgeStrandLength } from './playback.js';
import {$, escape, short as sensorName, DEFAULT_COLOR, round1, clamp01} from './util.js';
import {renderBuffer} from './buffer-view.js';
import {measurePath} from './path-measure.js';
import {unitLogo, ICONS} from './unit-icons.js';

export function spoolFill(state, gate) {
  return state.spool_percentages?.[gate] ?? 100;
}

export function createScene(host, {now: animationNow, requestFrame: draw, tooltipOpen}) {
  let view={}, settings={};
const spoolAngles=new Map(), spoolArrowVisibility=new Map(), spoolDirections=new Map();
let spoolLastFrame=null, spoolSession=null;
const spoolRenderers=new Map();
let renderedSpoolState=null;
function spoolMotion(index,operation) {
  const active=espoolerActive(operation);
  const visibility=spoolArrowVisibility.get(index) ?? (active ? 1 : 0);
  return {rotation:spoolAngles.get(index) || 0,
    arrowOpacity:visibility*visibility*(3-2*visibility),
    direction:active ? operation.direction : spoolDirections.get(index)};
}
function updateSpoolMotion() {
  for (const [index,renderer] of spoolRenderers)
    renderer.updateMotion(spoolMotion(index,renderedSpoolState?.espoolers?.[index]));
}
// Spool rotation and arrow fades advance every frame, whether or not the scene is redrawn.
function advanceSpools(now, {espoolers, running, session}) {
  if (spoolSession !== session) { spoolAngles.clear(); spoolLastFrame=null; spoolArrowVisibility.clear(); spoolDirections.clear(); spoolSession=session; }
  let spoolFading=false;
  const fadeStep=spoolLastFrame === null ? 0 : Math.min(100,now-spoolLastFrame)/450;
  for (const [gate,operation] of Object.entries(espoolers || {})) {
    const index=Number(gate), active=espoolerActive(operation);
    if (active) spoolDirections.set(index,operation.direction);
    const target=active ? 1 : 0, previous=spoolArrowVisibility.get(index) ?? (running ? 0 : target);
    const next=previous+Math.sign(target-previous)*Math.min(Math.abs(target-previous),fadeStep);
    spoolArrowVisibility.set(index,next);
    spoolFading ||= next!==previous;
  }
  let spoolRotating=false;
  if (running && spoolLastFrame !== null) {
    const seconds=Math.min(100,now-spoolLastFrame)/1000;
    for (const [gate,operation] of Object.entries(espoolers || {})) {
      if (!espoolerActive(operation)) continue;
      const speed=(40+100*Math.min(1,operation.output_power))*(operation.direction==='rewind'?-1:1);
      spoolAngles.set(Number(gate),((spoolAngles.get(Number(gate)) || 0)+seconds*speed)%360);
      spoolRotating=true;
    }
  }
  spoolLastFrame=now;
  return spoolFading || spoolRotating;
}
// The rack of units scrolls to the selected gate; the edge arrows add a temporary manual shift until the gate changes.
let rackGate = null, rackManualShift = 0, rackTween = null, bowdenDetached = false;
const rackScrollTime = 350, rackBleedPx = 12, MASK_HEIGHT = 700;
// The MMU gears ease between gripped and released instead of snapping.
let gearSpacingTween = null;
function animatedGearSpacing(target) {
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  gearSpacingTween = tweenTo(reduced ? null : gearSpacingTween, target, animationNow());
  if (!gearSpacingTween.done) draw();
  return gearSpacingTween.value;
}
// An inward ring marks active homing; the outward ring marks completion.
function homingRing(name,x,y) {
  const {viewMode,playing,homing}=view;
  const active=viewMode==='live' ? homing?.endstops.includes(name) : playing && homingTarget(playing.event)===name;
  if (!active) return '';
  const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
  const phase=((animationNow()-(viewMode==='live' ? 0 : playing.start))%600)/600;
  const reverse=viewMode==='live' ? !homing.triggered : (playing.event.requested ?? playing.event.distance ?? 0) < 0;
  const color=reverse ? '#48c982' : 'var(--sensor-clear-pulse)';
  const radius=reduced ? 9 : 13-9*phase;
  const opacity=reduced ? .55 : .6*Math.min(1,phase*8)*(1-.35*phase);
  return `<circle data-part="homing-sensor-ring" cx="${x}" cy="${y}" r="${radius}" fill="none" stroke="${color}" stroke-width="2" opacity="${opacity}" pointer-events="none"/>`;
}
// Fades the ends of the rack to nothing beneath each arrow (a luminance mask: white shows, black hides).
// A vertical dimension line from y1 to y2 with arrowheads whose wings reach `wing` back along the line (outward if negative).
const dimensionArrow=(x,y1,y2,wing=4)=>{
  const head=(y,reach)=>`M${x-3} ${y+reach} L${x} ${y} L${x+3} ${y+reach}`, direction=Math.sign(y2-y1) || 1;
  return `M${x} ${y1} V${y2} ${head(y1,direction*wing)} ${head(y2,-direction*wing)}`;
};
function rackFadeMask({left, right, bleed, top, bottom}) {
  const x0=-bleed, width=SCENE_WIDTH+2*bleed, inner=18, fade=48;                   // Arrow width plus a little clearance
  const stop=(x,white)=>`<stop offset="${(x-x0)/width}" stop-color="${white ? 'white' : 'black'}"/>`;
  const stops=(left ? stop(x0+inner,false)+stop(x0+inner+fade,true) : stop(x0,true))
    +(right ? stop(x0+width-inner-fade,true)+stop(x0+width-inner,false) : stop(x0+width,true));
  return `<defs><linearGradient id="rack-fade-gradient" gradientUnits="userSpaceOnUse" x1="${x0}" x2="${x0+width}" y1="0" y2="0">${(left ? stop(x0,false) : '')+stops+(right ? stop(x0+width,false) : '')}</linearGradient><mask id="rack-edge-fade" maskUnits="userSpaceOnUse" x="${x0}" y="0" width="${width}" height="2000"><rect x="${x0}" y="0" width="${width}" height="2000" fill="white"/><rect x="${x0}" y="${top}" width="${width}" height="${bottom-top}" fill="url(#rack-fade-gradient)"/></mask></defs>`;
}
const rackScrollStep = 300;
function scrollRack(arrow) {
  rackManualShift += -Number(arrow.dataset.direction)*rackScrollStep;
  lastActivity = performance.now();
  draw();
}
host.addEventListener('click',event=>{
  const arrow=event.target.closest('[data-part="rack-scroll"]');
  if (arrow) scrollRack(arrow);
  // A click on the background brings the rack straight back to the selected gate, as the idle timer would.
  else if (!event.target.closest('[role="button"],[data-sensor],[data-part="espooler"],[data-tooltip]') && rackManualShift!==0) { rackManualShift=0; draw(); }
});
host.addEventListener('keydown',event=>{
  const arrow=event.target.closest?.('[data-part="rack-scroll"]');
  if (arrow && ['Enter',' '].includes(event.key)) { event.preventDefault(); scrollRack(arrow); }
});
// Any pointer, key or tooltip use counts as activity; a rack left scrolled by hand eases back to the selected gate.
let lastActivity = performance.now();
for (const type of ['pointermove','pointerdown','keydown','wheel','focusin'])
  document.addEventListener(type,()=>{ lastActivity=performance.now(); },{passive:true,capture:true});
function returnRackWhenIdle() {
  if (tooltipOpen()) lastActivity=performance.now();
  if (rackIdleReturn(rackManualShift,performance.now()-lastActivity)) { rackManualShift=0; draw(); }
}
// Geometry: the tube path, the tip and sensor positions, and the fixed toolhead and Bowden markup
function measure(c) {
  let {offline, playing, events, e, s}=c;
  const spoolRequests=new Map();
  const gate = s.gate >= 0 ? s.gate : 0;
  renderedSpoolState=s;
  let tip = s.visual_tips?.[gate] ?? s.tips[gate] ?? -1000;
  const {entry,end,height:toolheadHeight,sensorEntry,sensorGap} = toolheadDrawingRange(s);
  // Fixed path with a compressed Bowden section. Position maps monotonically to it.
  const unitGates=s.unit_gates || s.tips.map((_,index)=>index);
  const previewCount=Math.max(0,Math.min(42,Math.trunc(settings.previewGateCount || 0)));
  const displayGates=previewCount ? Array.from({length:previewCount},(_,i)=>unitGates[i] ?? s.tips.length+i-unitGates.length) : unitGates;
  // Keep the real selected gate visible when previewing a smaller unit.
  if (previewCount && s.gate>=0 && !displayGates.includes(s.gate)) displayGates[displayGates.length-1]=s.gate;
  const mmuOffset=60; // Keep the enclosure below the purge animation.
  const hardwareOffset=mmuOffset-10; // Leave clearance above the enclosure.
  const virtual=virtualSelectorLayout({...s,unit_gates:displayGates},settings.spoolSpacing,mmuOffset);
  // Without a virtual selector the Bowden follows the lane of a single gate position.
  const lane=virtual ? null : bowdenLane(settings.spoolSpacing,mmuOffset);
  const guide=virtual ?? lane;
  // The horizontal extent of the Bowden inside the MMU: all the gate branches, or the one lane.
  const mmuColumn=virtual ? {left:virtual.branches[0].x-20,right:virtual.branches.at(-1).x+20} : {left:lane.x-20,right:lane.x+20};
  const mmuDistanceAtY=y=>{
    // Resolve positions on the rounded branch rather than its sharp-corner approximation.
    let low=0, high=guide.distanceAtY(150)+20;
    for (let i=0;i<24;i++) {
      const middle=(low+high)/2;
      if (tube.getPointAtLength(middle).y>y) low=middle; else high=middle;
    }
    return (low+high)/2;
  };
  const path = guide.pathFrom();
  const dimensionLink = label => {
    const id = dimensionParameter(s, label);
    return id && !offline && view.simulator ? `class="editable-dimension" role="button" tabindex="0" data-parameter="${escape(id)}" data-unit="${s.units?.indexOf(s.unit) ?? 0}" aria-label="Edit ${escape(label)}"` : '';
  };
  const nozzleDiameter = settings.nozzleDiameter;
  const nozzleWidth = 7 * nozzleDiameter / 1.75;
  // Match the 2mm tip cone, truncated at the selected outlet diameter.
  const funnelShoulder=449+2*toolheadHeight/(end-entry)*(1-nozzleWidth/7);
  const nozzleExit=Math.max(455,449+2*toolheadHeight/(end-entry)+2);
  // Round shadow caps extend 0.4 units inward; stop early to preserve the outlet width.
  const shadowEndX=nozzleWidth/2+.4;
  const shadowEndY=449+(funnelShoulder-449)*(3.5-shadowEndX)/(3.5-nozzleWidth/2);
  const nozzleFunnel = `M${E-3.5} 449 L${E-nozzleWidth/2} ${funnelShoulder} V${nozzleExit} H${E+nozzleWidth/2} V${funnelShoulder} L${E+3.5} 449 Z`;
  const nozzleBody=`M${E-18} 435 L${E-2.8} ${nozzleExit} H${E+2.8} L${E+18} 435 Z`;
  const nozzleEntrance=`M${E-5.5} 435 Q${E-3.5} 435 ${E-3.5} 437 V449 H${E+3.5} V437 Q${E+3.5} 435 ${E+5.5} 435 Z`;
  const bodyTop = 181; // Fixed 254-unit housing, ending at y=435.
  const bowdenPath = guide.pathFrom(470+mmuOffset,435);
  const branchOpacity=virtual ? Math.max(0,1-Math.abs(s.gate-(s.gate_focus ?? s.gate))) : 1;
  const branchShade=Math.round(branchOpacity*255);
  // Only the gate being left and the gate being reached change brightness; gates passed over stay dim.
  const selectionLit=branchGate=>gateSelectionLit(s,branchGate);
  const branchMask=virtual ? `<defs><mask id="virtual-selection-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="${SCENE_WIDTH}" height="${MASK_HEIGHT}"><rect width="${SCENE_WIDTH}" height="${MASK_HEIGHT}" fill="white"/><rect x="0" y="${virtual.hub}" width="${BUFFER_CENTER_X}" height="${MASK_HEIGHT-virtual.hub}" fill="rgb(${branchShade},${branchShade},${branchShade})"/></mask></defs>` : '';

  let svg = `${branchMask}<path id="extruder-body" d="M${E-10} ${bodyTop} H${E-18} Q${E-20} ${bodyTop} ${E-20} ${bodyTop+2} V435 H${E+20} V${bodyTop+2} Q${E+20} ${bodyTop} ${E+18} ${bodyTop} H${E+10}" fill="var(--scene-body, #edf2f6)" stroke="var(--scene-border, #96abba)"/><rect x="${E-47}" y="375" width="94" height="58" rx="5" fill="var(--scene-heater, #c4d3df)"/><rect id="nozzle-collar" x="${E-20}" y="433" width="40" height="2" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-body" d="${nozzleBody}" fill="var(--scene-nozzle, #9cafbe)"/><path id="nozzle-bore" d="${nozzleFunnel}" fill="var(--scene-tube, #edf4f8)"/><g><g${virtual ? ' mask="url(#virtual-selection-fade)"' : ''}><path id="tube" d="${path}" fill="none" stroke="none"/><g><g${s.devices[`${s.unit}:buffer`] ? ' mask="url(#buffer-tube-gap)"' : ''}><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="19.25"/><path d="${bowdenPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14.75"/></g></g><path id="nozzle-entrance" d="${nozzleEntrance}" fill="var(--scene-tube, #edf4f8)"/><path id="filament" mask="url(#bowden-end-fade)" d="${path}" fill="none" stroke="#${s.filament?.incoming_color || DEFAULT_COLOR}" stroke-width="7"/></g></g><text ${dimensionLink('Bowden')} id="bowden-length" x="${EXIT}" y="60" text-anchor="end" fill="var(--scene-muted, #6e8190)" font-size="13">${bowdenLengthLabel(s).includes('Uncalibrated') ? `<tspan x="${EXIT}">Bowden</tspan><tspan x="${EXIT}" dy="16">Uncalibrated length</tspan>` : bowdenLengthLabel(s)}</text>`;
  const parkedTips=[];
  // Virtual feeds rise from the spool to the parked position; `feed` (cross-unit changes only) is the progress.
  const feedFront=virtual ? gateIndex => {
    const spoolBottom=549.6+mmuOffset-spoolWindingRadius(spoolFill(s,gateIndex))*3.3;
    const parkEnd=virtual.bottom-virtual.parkDistance+(s.tip_shapes?.[gateIndex]==='cone' ? toolheadHeight/(end-entry) : 0);
    return spoolBottom-(spoolBottom-parkEnd)*(s.gate_feed ?? 1);
  } : null;
  if (virtual) {
    let branches='';
    for (const branch of virtual.branches) {
      const branchPath=virtual.branchPath(branch.x,470+mmuOffset);
      branches+='<g opacity=".4">';
      branches+=`<path data-part="virtual-gate-tube" data-gate="${branch.gate}" d="${branchPath}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="19.25"/><path d="${branchPath}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14.75"/>`;
      if (s.gate_status?.[branch.gate]!==0 && spoolFill(s,branch.gate)>0) {
        const cone=s.tip_shapes?.[branch.gate] === 'cone';
        const front=feedFront(branch.gate);
        if (front<484+mmuOffset) branches+=`<path data-part="virtual-parked-filament" data-gate="${branch.gate}" mask="url(#bowden-end-fade)" d="M${branch.x} ${484+mmuOffset} V${Math.max(front,virtual.bottom-virtual.parkDistance+(cone ? toolheadHeight/(end-entry) : 0))}" fill="none" stroke="#${s.gate_colors[branch.gate] || DEFAULT_COLOR}" stroke-width="7"/>`;
        if (cone && branch.gate !== gate && front<484+mmuOffset) {
          parkedTips.push(branch);
          branches+=`<!--parked-tip-${branch.gate}-->`;
        }
      }
      branches+='</g>';
    }
    // The active tube is painted last so inactive branches cannot close its visible bore.
    svg=branches+svg;
  }
  // The tube is measured from its path alone, so nothing has to be in the page yet
  const tube = measurePath(path),
    length = tube.getTotalLength();
  const mmuExitFraction = (virtual ? mmuDistanceAtY(400+hardwareOffset) : 200) / length;
  const toolheadFraction=(length-toolheadHeight)/length;
  const progress = (p) =>
    p < 0
      ? Math.max(0, ((p - Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) / -Math.min(-100, (s.layout.mmu_entry ?? -150)-40)) * mmuExitFraction)
      : p < sensorEntry
        ? mmuExitFraction + ((toolheadFraction-sensorGap/length-mmuExitFraction) * p) / sensorEntry
        : p < entry
          ? toolheadFraction-sensorGap/length+(sensorGap/length)*(p-sensorEntry)/(entry-sensorEntry)
        : Math.min(1, toolheadFraction + (1-toolheadFraction) * toolheadProgress(s,p));
  const pathDistance = position => progress(position)*length;
  if (Number.isFinite(s.extruder_target_temperature))
    svg += `<text id="extruder-temperature" x="${E+42}" y="426" text-anchor="end" font-size="9" fill="var(--scene-text, #304c65)" data-tooltip="Extruder target temperature">${round1(s.extruder_target_temperature)}°C</text>`;
  const unitSensors = Object.entries(s.sensors).filter(([name]) => {
    if (name === `${s.unit}:mmu_gear_touch`) return true;
    if (s.active_sensors && !(virtual && /^mmu_(entry|exit)(?:_\d+)?$/.test(sensorName(name)))) return s.active_sensors.includes(name);
    if (s.unit_sensors && !s.unit_sensors.includes(name)) return false;
    const gateSuffix = sensorName(name).match(/^mmu_.*_(\d+)$/);
    return !gateSuffix || (virtual ? virtual.branches.some(branch=>branch.gate===Number(gateSuffix[1])) : Number(gateSuffix[1]) === s.gate);
  });
  const isTouchSensor=name=>/(?:^|:)mmu_gear_touch(?:_\d+)?$/.test(name);
  const encoderAnchor = encoderReference(s, unitSensors);
  const gateSlots = Object.fromEntries(Object.entries(gateDevicePositions(s, unitSensors)).map(([key,value])=>[key,Number.isFinite(value) ? value+100 : value]));
  if (virtual) {
    const shared=unitSensors.some(([name])=>name.endsWith(':mmu_shared_exit'));
    const encoder=Boolean(s.devices?.[`${s.unit}:encoder`]);
    const encoderBefore=s.encoder_reference?.endstop==='mmu_shared_exit' && s.encoder_reference.offset<0;
    gateSlots.sharedExit=shared && encoder ? (encoderBefore ? 158 : 177) : 170;
    gateSlots.encoder=shared && encoder ? (encoderBefore ? 177 : 158) : 170;
  }
  for (const key of Object.keys(gateSlots)) if (Number.isFinite(gateSlots[key])) gateSlots[key]+=hardwareOffset;
  // Where the Bowden is at a given height within the MMU.
  const descentX = y => tube.getPointAtLength(mmuDistanceAtY(y)).x;
  // Sensors are drawn on the tip's schematic scale (`schematic`), but the scale itself is built from plain positions.
  const sensorPoint = (name, device, schematic=false) => {
    if (isTouchSensor(name)) return {x:E,y:420};
    const point = tube.getPointAtLength((schematic ? schematicDistance : pathDistance)(device.position));
    const short = name.split(':').at(-1);
    const branchSensor=short.match(/^mmu_(entry|exit)(?:_(\d+))?$/);
    if (virtual && branchSensor) {
      const sensorGate=branchSensor[2]===undefined ? gate : Number(branchSensor[2]);
      const branch=virtual.branches.find(item=>item.gate===sensorGate);
      if (branch) return {x:branch.x,y:(branchSensor[1]==='entry' ? 465 : 400)+hardwareOffset};
    }
    if (/^mmu_entry(?:_\d+)?$/.test(short)) { point.y = 465+hardwareOffset; point.x = descentX(point.y); }
    if (/^mmu_exit(?:_\d+)?$/.test(short)) { point.y = gateSlots.exit; point.x = descentX(point.y); }
    if (short === 'mmu_shared_exit') { point.y = gateSlots.sharedExit; point.x = descentX(point.y); }
    return point;
  };
  const isBufferSensor = name => /(?:^|:)filament_(compression|tension)$/.test(name);
  const sensorFill = state => state === true ? '#399b64' : state === false ? '#fff' : '#9ba5ae';
  const encoderPoint = () => { const y=gateSlots.encoder ?? 360+hardwareOffset; return {x:descentX(y), y}; };
  const tipAtPathDistance = distance => {
    const target = Math.max(0,distance)/length;
    let low = Math.min(-100,(s.layout.mmu_entry ?? -150)-40), high = end;
    for (let i=0;i<32;i++) { const mid=(low+high)/2; if(progress(mid)<target) low=mid; else high=mid; }
    return (low+high)/2;
  };
  let schematicDistance = position => progress(position)*length;
  let schematicTip = null;
  // UNKNOWN uses an illustrative endpoint between the MMU gears and its outlet.
  if (s.gate >= 0 && s.filament_pos === -1 && e.kind !== 'movement') tip = tipAtPathDistance(virtual ? virtual.parkDistance : 570-405);
  // The schematic Bowden scale ends at the extruder sensor: beyond it positions use the toolhead's own scale, so the
  // sensor and the extruder never move as the tip enters.
  // Encoder loading keeps one reference across START OF BOWDEN bookkeeping.
  if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry && s.encoder_reference?.endstop === 'encoder') {
    if (virtual) {
      schematicDistance=position=>switchFilamentDistance(position,s.layout.mmu_encoder,
        s.encoder_reference.parking,sensorEntry,mmuDistanceAtY(encoderPoint().y),pathDistance,
        null,s.encoder_reference.parking<0 ? virtual.parkDistance : null);
      schematicTip=schematicDistance(tip);
      tip=tipAtPathDistance(schematicTip);
    } else tip = tipAtPathDistance(encoderFilamentDistance(events,e,s,tip,pathDistance,mmuDistanceAtY(encoderPoint().y)));
  } else if (s.gate >= 0 && s.filament_pos >= 0 && tip < entry) {
    const endstop = s.encoder_reference?.endstop?.split(':').at(-1);
    const sensor = unitSensors.find(([name,d]) => Number.isFinite(d.position) &&
      [endstop,`${endstop}_${gate}`].includes(sensorName(name)));
    if (sensor) {
      schematicDistance = position => switchFilamentDistance(position,sensor[1].position,
        s.encoder_reference?.parking || 0,sensorEntry,mmuDistanceAtY(sensorPoint(...sensor).y),pathDistance,
        encoderAnchor?.name === sensor[0] && s.devices?.[`${s.unit}:encoder`]
          ? {position:sensor[1].position+encoderAnchor.offset,distance:mmuDistanceAtY(encoderPoint().y)} : null,
        virtual && endstop==='mmu_shared_exit' && s.encoder_reference.parking<0 ? virtual.parkDistance : null);
      schematicTip = schematicDistance(tip);
      tip = tipAtPathDistance(schematicTip);
    }
  }
  const tipScale = toolheadHeight/(end-entry);
  const coneOffset = s.tip_shapes?.[gate] === 'cone' ? tipScale : 0;
  // Apply recorded transitions where the drawn tip crosses, after schematic Bowden scaling.
  const displayedSensors=unitSensors.map(([name,device])=>{
    const before=e.before?.sensors[name], after=e.state.sensors[name];
    if (playing && e.kind === 'movement' && before && after && before.state !== after.state &&
        ['extruder','toolhead'].includes(sensorName(name))) {
      return [name,{...device,state:(schematicTip ?? pathDistance(tip))>=schematicDistance(device.position)-1e-6}];
    }
    return [name,device];
  });
  const presenceSensors = displayedSensors.filter(([name,d]) =>
    (!virtual || !/_\d+$/.test(name) || Number(name.match(/_(\d+)$/)[1])===gate) && !isBufferSensor(name) &&
    !name.includes('encoder') && !isTouchSensor(name) && d.position !== null).map(([name,d]) => {
      const short=name.split(':').at(-1);
      const position = /^mmu_(?:entry|exit)(?:_\d+)?$/.test(short) || short === 'mmu_shared_exit'
        ? mmuDistanceAtY(sensorPoint(name,d).y) : schematicDistance(d.position);
      // An encoder-derived tip may differ from a sensor by the encoder's movement threshold,
      // 1.5 x resolution as in Happy Hare's movement_min(); that much is not worth a warning
      const slack=1.5*(s.devices?.[`${s.unit}:encoder`]?.resolution || 0);
      return {...d,name,position,tolerance:slack ? Math.abs(pathDistance(d.position+slack)-pathDistance(d.position)) : 0};
    });
  const fragments = s.retained_segment ? [{start:pathDistance(s.retained_segment.start),end:pathDistance(s.retained_segment.end)}] : [];
  const constrained = constrainFilament(schematicTip ?? pathDistance(tip),
    s.tails[gate] === null ? null : pathDistance(s.tails[gate]), presenceSensors, fragments, s.sensor_conflicts || []);
  const focus = s.gate_focus ?? s.gate;
  // Start just inside the upper spool rim; the fixed fade hides the insertion point.
  const fullTipOpacity=virtual ? 1 : s.gate_full_opacity ?? 1;
  const slidingRack=fullTipOpacity===0;
  const spoolTip=570-504;
  const selectedTip=constrained.tip+(Math.min(constrained.tip,spoolTip)-constrained.tip)*(virtual ? (s.gate_feed===undefined ? 0 : 1-s.gate_feed) : s.gate_retraction || 0);
  tip = tipAtPathDistance(selectedTip);
  const shapedTipOpacity=fullTipOpacity*(virtual && selectedTip<virtual.distanceAtY(virtual.hub) ? branchOpacity : 1)*clamp01((selectedTip-(570-502))/28);
  const start = s.tails[gate] === null ? 0 : pathDistance(s.tails[gate]), stop = Math.max(0,selectedTip-coneOffset);
  const macroUnit=s.units?.indexOf(s.unit) ?? 0;
  const macroParams=s.configuration?.parameters?.[macroUnit] || {};
  const filamentDash = `0 ${start} ${Math.max(0, stop - start)} ${length + 1}`;
  Object.assign(c, {filamentDash, spoolRequests, gate, tip, entry, end, toolheadHeight, sensorEntry, sensorGap, unitGates, previewCount, displayGates, mmuOffset, hardwareOffset, virtual, lane, guide, mmuColumn, mmuDistanceAtY, path, dimensionLink, nozzleDiameter, nozzleWidth, funnelShoulder, nozzleExit, shadowEndX, shadowEndY, nozzleFunnel, nozzleBody, nozzleEntrance, bodyTop, bowdenPath, branchOpacity, branchShade, selectionLit, branchMask, svg, parkedTips, feedFront, tube, length, mmuExitFraction, toolheadFraction, progress, pathDistance, unitSensors, isTouchSensor, encoderAnchor, gateSlots, descentX, sensorPoint, isBufferSensor, sensorFill, encoderPoint, tipAtPathDistance, schematicDistance, schematicTip, tipScale, coneOffset, displayedSensors, presenceSensors, fragments, constrained, focus, fullTipOpacity, slidingRack, spoolTip, selectedTip, shapedTipOpacity, start, macroUnit, macroParams});
}

// The rack of units: enclosures, spools, gate pills and the scroll arrows
// One 24x24 glyph drawn `size` high with its top left at (x, y)
const glyph=(path,x,y,size,attrs)=>`<path d="${path}" transform="translate(${x} ${y}) scale(${size/24})" ${attrs}/>`;
// The strip under a unit's gate pills: logo, display name, then humidity, temperature and the heater or drying state
function unitFooter(info,left,right,top,dark,showLogo) {
  const logo=unitLogo(info.vendor,dark), size=logo.square ? 30 : 38;
  const textLeft=left+12+(showLogo ? 38+8 : 0), row=top+33, id=`unit-footer-${escape(info.name)}`;
  let svg=`<defs><clipPath id="${id}"><rect x="${left}" y="${top}" width="${right-left-8}" height="46"/></clipPath></defs><g data-part="unit-footer" data-unit="${escape(info.name)}" clip-path="url(#${id})" font-size="11" fill="var(--scene-text, #304c65)">`;
  if (showLogo) svg += glyph(logo.path,left+12+(38-size)/2,top+4+(38-size)/2,size,'data-part="unit-logo" fill="var(--scene-text, #304c65)" opacity=".7"');
  // Alone, the name sits level with the middle of the logo
  const climate=info.humidity || info.temperature || info.heaterIcon;
  svg += `<text data-part="unit-name" x="${textLeft}" y="${top+(climate ? 15 : 23)}" dominant-baseline="middle">${escape(info.display_name)}</text>`;
  const tooltip=info.tooltip ? ` data-tooltip="${escape(info.tooltip)}"` : '';
  svg += `<g data-part="unit-climate"${tooltip}>`;
  let x=textLeft;
  for (const [text,icon,color] of [[info.humidity,ICONS.water,'#2196f3'],[info.temperature,ICONS.thermometer,'#ff5722']]) {
    if (!text) continue;
    if (info.climateIcons) { svg += glyph(icon,x-3,row-7.5,15,`fill="${color}"`); x += 12; }
    svg += `<text x="${x}" y="${row}" dominant-baseline="middle">${escape(text)}</text>`;
    x += text.length*6.2+8;
  }
  if (info.heaterIcon) {
    const text=info.heater || '', end=right-14;
    if (text) svg += `<text x="${end}" y="${row}" text-anchor="end" dominant-baseline="middle">${escape(text)}</text>`;
    svg += glyph(ICONS[info.heaterIcon],end-text.length*6.2-(text ? 20 : 18),row-9,18,'data-part="unit-heater" fill="#f44336"');
  }
  return svg+'</g></g>';
}

function rack(c) {
  let {viewMode, live, events, s, spoolRequests, gate, unitGates, displayGates, mmuOffset, virtual, lane, path, selectionLit, svg, feedFront, focus}=c;
  let rackArrows='', rackFade=null, bowdenReach=1, enclosurePaths='';
  // The filament ends inside the unit fade first, before the Bowden itself.
  const endReach=()=>clamp01((bowdenReach-.7)/.3);
  if (s.tips.length) {
    const spoolViewAngle=settings.spoolAngle*Math.PI/180;
    const focusGate=s.gate>=0 ? focus : displayGates[0];
    // Every unit sits side by side on one rack that slides to keep the selected spool under the Bowden exit.
    const layouts=(s.unit_layouts?.length ? s.unit_layouts : [{name:s.unit,gates:unitGates,selector_type:s.selector_type}])
      .map(layout=>layout.name===s.unit ? {...layout,gates:displayGates} : layout);
    if (s.gate!==rackGate || s.gate_slide!==undefined) { rackGate=s.gate; rackManualShift=0; }
    // The scene reaches the panel edges: it bleeds into the card padding, at the same scale.
    const sceneWidth=host.clientWidth, bleed=sceneWidth>2*rackBleedPx ? rackBleedPx*SCENE_WIDTH/(sceneWidth-2*rackBleedPx) : 0;
    const rackOptions={focus:focusGate,spacing:settings.spoolSpacing,angle:spoolViewAngle,bleed,
      fullOpacity:s.gate_full_opacity ?? 1,slide:s.gate_slide!==undefined ? {from:s.gate_from,to:s.gate_to,t:s.gate_slide} : null};
    const targetGrid=rackGrid(layouts,{...rackOptions,manual:rackManualShift});
    rackManualShift=targetGrid.manual;
    // The arrows scroll with a short ease instead of jumping.
    const reducedMotion=matchMedia('(prefers-reduced-motion: reduce)').matches;
    rackTween=tweenTo(reducedMotion ? null : rackTween,rackManualShift,animationNow(),rackScrollTime);
    if (!rackTween.done) draw();
    const grid=rackTween.value===rackManualShift ? targetGrid : rackGrid(layouts,{...rackOptions,manual:rackTween.value});
    ({detached:bowdenDetached,reach:bowdenReach}=bowdenAttachment(bowdenDetached,grid.reach,grid.manual,rackOptions.spacing,s.gate_slide!==undefined));
    const visible=grid.lanes.filter(lane=>lane.x>-140 && lane.x<SCENE_WIDTH+140).sort((a,b)=>a.row-b.row || b.x-a.x);
    const rowSpacing=135;
    const lastRowY=Math.max(0,grid.rows-1)*rowSpacing;
    const enclosureBottom=659+lastRowY+mmuOffset;
    host.setAttribute('viewBox',`${-bleed} 0 ${SCENE_WIDTH+2*bleed} ${enclosureBottom+21}`);
    const shells=grid.units.map(unit=>{
      const enclosureLeft=unit.left, enclosureRight=unit.right, unitLastRow=(unit.rows-1)*rowSpacing;
      const glassLeft=enclosureLeft+6, glassRight=enclosureRight-6;
      return {unit,enclosureLeft,enclosureRight,glassLeft,glassRight,unitLastRow,
        coverPath:`M${glassLeft} ${585+mmuOffset} V${510+mmuOffset} Q${glassLeft} ${470+mmuOffset} ${glassLeft+40} ${470+mmuOffset} H${glassRight-40} Q${glassRight} ${470+mmuOffset} ${glassRight} ${510+mmuOffset} V${585+mmuOffset} Z`,
        lowerHousingPath:unitLastRow ? `M${enclosureLeft} ${580+mmuOffset} Q${enclosureLeft} ${575+mmuOffset} ${enclosureLeft+5} ${575+mmuOffset} H${enclosureRight-5} Q${enclosureRight} ${575+mmuOffset} ${enclosureRight} ${580+mmuOffset} V${585+unitLastRow+mmuOffset} H${enclosureLeft} Z` : ''};
    });
    const coverPath=shells.map(shell=>shell.coverPath).join('');
    const lowerHousingPath=shells.map(shell=>shell.lowerHousingPath).join('');
    const enclosurePath=coverPath+lowerHousingPath;
    enclosurePaths=enclosurePath;
    svg += `<defs><linearGradient id="spool-shelf-fade" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), white 18%)"/><stop offset=".12" stop-color="var(--scene-body, #edf2f6)"/><stop offset=".65" stop-color="var(--scene-body, #edf2f6)"/><stop offset="1" stop-color="var(--scene-body, #edf2f6)" stop-opacity="0"/></linearGradient><linearGradient id="spool-glass" x1="0" y1="0" x2=".3" y2="1"><stop stop-color="white" stop-opacity=".22"/><stop offset=".12" stop-color="white" stop-opacity=".07"/><stop offset=".3" stop-color="white" stop-opacity=".02"/><stop offset="1" stop-color="black" stop-opacity=".1"/></linearGradient><linearGradient id="spool-cabinet" x1="0" y1="0" x2="0" y2="1"><stop stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), white 18%)"/><stop offset=".055" stop-color="var(--scene-body, #edf2f6)"/><stop offset="1" stop-color="color-mix(in srgb, var(--scene-body, #edf2f6), black 18%)"/></linearGradient><mask id="outside-mmu" maskUnits="userSpaceOnUse" x="0" y="0" width="1200" height="2000"><rect width="1200" height="2000" fill="white"/><path d="${enclosurePath}" fill="black"/></mask><clipPath id="spool-cover-clip"><path d="${enclosurePath}"/></clipPath></defs>`;
    // Put the enclosure behind the short filament fade; the glass stays above it.
    svg = `<path data-part="spool-enclosure" d="${enclosurePath}" fill="color-mix(in srgb, var(--scene-body, #edf2f6), black 12%)"/>` + svg;
    if (settings.heatingPreview || (viewMode==='live' && s.heaters?.some(h=>h.target>0))) {
      svg += `<defs><radialGradient id="mmu-heating-glow" cx=".5" cy=".9" r=".85"><stop stop-color="#ffbd59" stop-opacity=".65"/><stop offset=".45" stop-color="#ef8d27" stop-opacity=".3"/><stop offset="1" stop-color="#e87516" stop-opacity="0"/></radialGradient></defs><g data-part="heating-preview" clip-path="url(#spool-cover-clip)" pointer-events="none">`;
      for (const {unit,glassLeft,glassRight} of shells) for (let row=0; row<unit.rows; row++) {
        const lanes=grid.lanes.filter(lane=>lane.unit===unit.name && lane.row===row).sort((a,b)=>a.x-b.x);
        const current=unit.name===s.unit;
        const regions=current && s.has_per_gate_heaters ? lanes.map((lane,i)=>({
          heating:s.heaters?.[unitGates.indexOf(lane.index)]?.target>0,
          left:i ? (lanes[i-1].x+lane.x)/2 : glassLeft,
          right:i<lanes.length-1 ? (lane.x+lanes[i+1].x)/2 : glassRight,
        })) : [{left:glassLeft,right:glassRight,heating:current && s.heaters?.some(h=>h.target>0)}];
        for (const region of regions.filter(r=>settings.heatingPreview || r.heating))
          svg += `<rect data-part="heating-glow" x="${region.left}" y="${470+mmuOffset+row*rowSpacing}" width="${region.right-region.left}" height="105" fill="url(#mmu-heating-glow)"/>`;
      }
      svg += '</g>';
    }
    svg += '<defs><filter id="spool-separation-shadow" x="-30%" y="-20%" width="160%" height="145%"><feDropShadow dx="1.2" dy=".8" stdDeviation=".6" flood-color="#000" flood-opacity=".65"/></filter></defs><g id="espooler-indicators" clip-path="url(#spool-cover-clip)">';
    const spoolStyle=document.querySelector('input[name=spool-style]:checked').value;
    const spoolDepth=34/3, spoolScale=3.3;

    for (const {index,x:laneX,row,lift} of visible) {
      const operation=s.espoolers?.[index];
      const active=espoolerActive(operation);
      // Offset the winding slightly left of the filament lane.
      const x=laneX-spoolDepth*Math.sin(spoolViewAngle)*spoolScale/2;
      const y=549.6+row*rowSpacing-lift+mmuOffset;
      const color=s.gate_colors[index] || DEFAULT_COLOR;
      const percent=spoolFill(s,index);
      const branch=virtual?.branches.find(branch=>branch.gate===index);
      if (branch && percent>0 && s.gate_status?.[index]!==0) {
        const top=470+mmuOffset, bottom=y-spoolWindingRadius(percent)*spoolScale, feedTop=Math.min(bottom,Math.max(top,feedFront(index)));
        const opacity=(.2+.3*selectionLit(index))*endReach();
        svg += `<defs><linearGradient id="spool-feed-fade-${index}" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${top}" y2="${bottom}"><stop stop-color="#${color}" stop-opacity="${opacity}"/><stop offset="1" stop-color="#${color}" stop-opacity="0"/></linearGradient></defs><path data-part="virtual-spool-feed" data-gate="${index}" d="M${branch.x} ${feedTop} V${bottom}" stroke="url(#spool-feed-fade-${index})" stroke-width="7" fill="none"/>`;
      }
      const knownWeight=Number.isFinite(s.spool_percentages?.[index]);
      const title=spoolTooltip(s,index);
      const shelfY=575+row*rowSpacing+mmuOffset;
      svg += `<defs><linearGradient id="spool-shelf-edge-${index}" gradientUnits="userSpaceOnUse" x1="0" y1="${shelfY-3}" x2="0" y2="${shelfY}"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="spool-shelf-mask-${index}" maskUnits="userSpaceOnUse" x="${x-80}" y="${y-70}" width="160" height="150"><rect x="${x-80}" y="${y-70}" width="160" height="150" fill="url(#spool-shelf-edge-${index})"/></mask></defs><g mask="url(#spool-shelf-mask-${index})">`;
      svg += `<g data-part="espooler" data-gate="${index}" data-active="${active}" data-fill-percent="${percent}" data-tooltip="${escape(title)}" transform="translate(${x} ${y}) scale(${spoolScale})" filter="url(#spool-separation-shadow)">`;
      spoolRequests.set(index,{idPrefix:`gate-${index}`, style:spoolStyle, color,
        fillPercent:percent, showFillPercent:knownWeight, shadowStyle:settings.spoolRealism ? 'blur' : 'crisp',windingLines:settings.spoolRealism, viewAngle:spoolViewAngle, ...spoolMotion(index,operation),
        glow:index===s.gate ? Math.max(0,1-Math.abs(index-focus)) : 0});
      svg += '</g></g>';
    }
    svg += '</g>';
    let glassDividers='';
    for (const {unit} of shells) for (let row=0; s.has_per_gate_heaters && unit.name===s.unit && row<unit.rows; row++) {
      const lanes=grid.lanes.filter(lane=>lane.unit===unit.name && lane.row===row).sort((a,b)=>a.x-b.x);
      const top=470+mmuOffset+row*rowSpacing, bottom=575+mmuOffset+row*rowSpacing;
      for (let i=1;i<lanes.length;i++) {
        const x=(lanes[i-1].x+lanes[i].x)/2;
        const perspective=Math.max(0,Math.cos(spoolViewAngle)/Math.cos(74*Math.PI/180));
        const shift=10*perspective, bend=18*perspective;
        const divider=perspective<.001 ? `M${x} ${top} V${bottom}`
          : `M${x} ${top} C${x+shift*.55} ${top+bend*.3} ${x+shift} ${top+bend*.5} ${x+shift} ${top+bend} V${bottom}`;
        glassDividers+=`<defs><filter id="spool-divider-blur-${row}-${i}" filterUnits="userSpaceOnUse" x="${x-8}" y="${top-5}" width="${shift+16}" height="${bottom-top+10}"><feGaussianBlur stdDeviation="1.3"/></filter></defs><g data-part="spool-glass-divider"><path data-part="spool-divider-shadow" d="${divider}" transform="translate(-2.2 0)" fill="none" stroke="black" stroke-opacity=".2" stroke-width="3" filter="url(#spool-divider-blur-${row}-${i})"/><path d="${divider}" fill="none" stroke="black" stroke-opacity=".17" stroke-width="2"/><path d="${divider}" transform="translate(.9 0)" fill="none" stroke="white" stroke-opacity=".18" stroke-width="1.1"/></g>`;
      }
    }
    glassDividers=`<g clip-path="url(#spool-cover-clip)" pointer-events="none">${glassDividers}</g>`;
    const glassDefs=`<defs><filter id="spool-glass-soften" x="-5%" y="-25%" width="110%" height="150%"><feGaussianBlur stdDeviation=".7"/></filter><linearGradient id="spool-glass-reflection"><stop stop-color="white" stop-opacity="0"/><stop offset=".15" stop-color="white" stop-opacity=".25"/><stop offset=".55" stop-color="white" stop-opacity=".08"/><stop offset="1" stop-color="white" stop-opacity="0"/></linearGradient></defs>`;
    const glassHighlight=glassDefs+shells.map(({glassLeft,glassRight})=>`<path data-part="spool-glass-highlight" filter="url(#spool-glass-soften)" d="M${glassLeft+4} ${510+mmuOffset} Q${glassLeft+4} ${474+mmuOffset} ${glassLeft+40} ${474+mmuOffset} H${glassRight-40} Q${glassRight-4} ${474+mmuOffset} ${glassRight-4} ${510+mmuOffset}" fill="none" stroke="url(#spool-glass-reflection)" stroke-width="2" stroke-linecap="round" pointer-events="none"/>`).join('');
    svg += `${lowerHousingPath ? `<path data-part="spool-lower-housing" d="${lowerHousingPath}" fill="none" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 60%)" stroke-width=".6"/>` : ''}<path data-part="spool-glass-cover" d="${coverPath}" fill="url(#spool-glass)" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 55%)" stroke-width=".8" pointer-events="none"/>${glassDividers}${glassHighlight}${shells.map(({enclosureLeft,enclosureRight,unitLastRow})=>`<rect data-part="spool-cabinet" x="${enclosureLeft}" y="${575+unitLastRow+mmuOffset}" width="${enclosureRight-enclosureLeft}" height="84" rx="6" fill="url(#spool-cabinet)" stroke="color-mix(in srgb, var(--scene-border, #96abba), transparent 60%)" stroke-width=".6"/>`).join('')}`;
    for (const {unit,enclosureLeft,enclosureRight} of shells) for (let row=0; row<unit.rows-1; row++) {
      svg += `<rect data-part="spool-row-shelf" x="${enclosureLeft}" y="${575+row*rowSpacing+mmuOffset}" width="${enclosureRight-enclosureLeft}" height="49" rx="5" fill="url(#spool-shelf-fade)"/>`;
    }
    for (const {index,x:laneX,row} of visible) {
      const status=s.gate_status?.[index];
      const border=status===0 ? '#858b91' : status>0 ? '#399b64' : '#d58a30';
      // A pill is selected only while its spool is fully raised.
      const selected=index===s.gate && focus===index;
      const x=laneX+4;
      svg += `<g data-part="gate-status-pill" data-gate="${index}" data-tooltip="Gate ${index} · ${status===0 ? 'Empty' : status>0 ? 'Available' : 'Unknown'}"><rect x="${x-16}" y="${587+row*rowSpacing+mmuOffset}" width="32" height="18" rx="9" fill="${selected ? '#36ca42' : 'color-mix(in srgb, var(--scene-body, #edf2f6), black 12%)'}" stroke="${border}" stroke-width="2"/><text x="${x}" y="${596+row*rowSpacing+mmuOffset}" dominant-baseline="central" text-anchor="middle" font-size="11" font-weight="700" fill="${selected ? '#102514' : 'var(--scene-text, #304c65)'}">${index}</text></g>`;
    }
    const dark=document.documentElement.dataset.theme==='dark';
    for (const {unit,enclosureLeft,enclosureRight,unitLastRow} of shells) {
      const info=view.unitInfo?.find(entry=>entry.name===unit.name);
      if (info) svg += unitFooter(info,enclosureLeft,enclosureRight,611+unitLastRow+mmuOffset,dark,settings.unitLogos!==false);
    }
    // Narrow vertical arrows at the panel edges scroll to units that are off screen; the rack fades out beneath them.
    if (grid.overflow.left || grid.overflow.right) rackFade={left:grid.overflow.left,right:grid.overflow.right,bleed,top:440+mmuOffset,bottom:enclosureBottom};
    for (const side of [-1,1]) {
      if (!(side<0 ? grid.overflow.left : grid.overflow.right)) continue;
      // The same size whatever the row count, level with the middle of a single row unit.
      const height=105, top=(470+659)/2+mmuOffset-height/2, width=16, left=side<0 ? -bleed+2 : SCENE_WIDTH+bleed-2-width, middle=top+height/2;
      const label=side<0 ? 'Scroll to units on the left' : 'Scroll to units on the right';
      rackArrows += `<g data-part="rack-scroll" data-direction="${side}" role="button" tabindex="0" aria-label="${label}" data-tooltip="${label}"><rect x="${left}" y="${top}" width="${width}" height="${height}" rx="4" fill="var(--scene-body, #edf2f6)" fill-opacity=".85" stroke="var(--scene-border, #96abba)" stroke-width=".8"/><path d="M${left+width/2-side*4} ${middle-30} L${left+width/2+side*4} ${middle} L${left+width/2-side*4} ${middle+30}" fill="none" stroke="var(--scene-text, #304c65)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></g>`;
    }
  }
  Object.assign(c, {rackArrows, endReach, rackFade, bowdenReach, enclosurePaths, svg});
}

// Shaped filament tips, including the ones parked in other branches
function tips(c) {
  let {s, gate, tip, end, virtual, path, nozzleExit, svg, parkedTips, feedFront, tube, length, progress, tipScale}=c;
  c.attachedTipEdge='';
  const shapedTip = (position, color, part, hairLength=s.hair_lengths?.[gate] ?? 15, route=null) => {
    const hairScale = tipScale;
    const along = distance => route ? route.getPointAtLength(distance) : distance <= length ? tube.getPointAtLength(distance)
      : {x:E,y:Math.min(nozzleExit,449+distance-length)};
    // Keep the cone at toolhead scale even along the compressed Bowden.
    const center=route ? position : progress(position)*length;
    const base = along(Math.max(0,center-tipScale)), nose = along(center+tipScale);
    const angle = Math.atan2(nose.y-base.y,nose.x-base.x), dx = -Math.sin(angle)*3.5, dy = Math.cos(angle)*3.5;
    // Overlap the cone slightly, then taper the surviving hair.
    const hairStart = center+tipScale;
    const hairLimit=route ? route.getTotalLength() : progress(end-(s.filament?.residual_mm || 0))*length;
    const hair = Array.from({length:32},(_,i)=>along(Math.min(hairLimit,hairStart+(-.4+(hairLength+.4)*i/31)*hairScale)));
    const sides = [1,-1].map(side=>hair.map((p,i)=>{
      const before=hair[Math.max(0,i-1)], after=hair[Math.min(hair.length-1,i+1)];
      const tangent=Math.atan2(after.y-before.y,after.x-before.x);
      const radius=.7*(1-i/31)+.04;
      return {x:p.x-side*Math.sin(tangent)*radius,y:p.y+side*Math.cos(tangent)*radius};
    }));
    const outline=[...sides[0],...sides[1].reverse()];
    const edge=position+1 >= end-(s.filament?.residual_mm || 0) ? `<path data-part="cone-edge" opacity="${s.cone_edge_opacity ?? (s.tip_shapes?.[gate] === 'cone' || part === 'fragment-tip' ? 1 : 0)}" d="M${base.x+dx} ${base.y+dy} L${nose.x} ${nose.y} L${base.x-dx} ${base.y-dy}" fill="none" stroke="color-mix(in srgb, #${color}, black 40%)" stroke-width="0.6"/>` : '';
    if (part === 'attached-tip') c.attachedTipEdge=edge;
    return `<g data-part="${part}">${hairStart < hairLimit ? `<path data-part="filament-hair" d="${outline.map((p,i)=>`${i?'L':'M'}${p.x} ${p.y}`).join(' ')} Z" fill="#${color}"/>` : ''}<path d="M${base.x+dx} ${base.y+dy} L${nose.x} ${nose.y} L${base.x-dx} ${base.y-dy} Z" fill="#${color}"/>${part === 'attached-tip' ? '' : edge}</g>`;
  };
  for (const branch of parkedTips) {
    const route=measurePath(virtual.branchPath(branch.x));
    svg=svg.replace(`<!--parked-tip-${branch.gate}-->`,shapedTip(s.gate_feed===undefined ? virtual.parkDistance : Math.min(virtual.parkDistance,virtual.bottom-feedFront(branch.gate)),
      s.gate_colors[branch.gate] || DEFAULT_COLOR,'parked-tip',s.hair_lengths?.[branch.gate] ?? 15,route));
  }
  Object.assign(c, {shapedTip, svg});
}

// The MMU-side fade, and the synced and grip indicators
function syncIndicators(c) {
  let {live, paused, playing, e, s, entry, end, mmuOffset, hardwareOffset, virtual, lane, mmuColumn, svg, tube, length, progress, endReach}=c;
  svg += `<defs><linearGradient id="bowden-fade-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${470+mmuOffset}" y2="${484+mmuOffset}"><stop stop-color="rgb(${Math.round(128*endReach())},${Math.round(128*endReach())},${Math.round(128*endReach())})"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-end-fade" maskUnits="userSpaceOnUse" x="0" y="0" width="${SCENE_WIDTH}" height="${MASK_HEIGHT}"><rect width="${SCENE_WIDTH}" height="${MASK_HEIGHT}" fill="white"/><rect x="${mmuColumn.left}" y="${470+mmuOffset}" width="${mmuColumn.right-mmuColumn.left}" height="230" fill="${virtual ? 'black' : 'url(#bowden-fade-gradient)'}"/></mask></defs>`;
  const extruderGearY=tube.getPointAtLength(progress(s.layout.extruder_gears ?? entry) * length).y;
  if (syncForDisplay(e, s, Boolean(playing) || !live || paused)) {
    const syncY=435+hardwareOffset;
    // Beside the unit, on the side the gears are nearer.
    const syncSide=virtual ? syncIndicatorSide(virtual) : 1;
    const syncEdge=virtual ? (syncSide<0 ? virtual.branches[0] : virtual.branches.at(-1)) : {x:lane.x+45-15};
    const syncX=syncEdge.x+syncSide*((virtual?.active === syncEdge ? 38 : 15)+18);
    const syncIcon=(x,y,scale=1)=>`<g transform="translate(${x} ${y}) scale(${scale}) rotate(35)" fill="none" stroke="#53b9b0" stroke-width="2.2"><rect x="-12" y="-5" width="15" height="10" rx="5"/><rect x="-3" y="-5" width="15" height="10" rx="5"/></g>`;
    svg += `<g id="extruder-synced-indicator" aria-label="Extruder and MMU gears synced">${syncIcon(E+47,extruderGearY,.7)}</g>`;
    svg += `<g id="synced-indicator" aria-label="Extruder and MMU gears synced">${syncIcon(syncX,syncY)}<text x="${syncX}" y="${syncY+21}" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Synced</text></g>`;
  } else if (!virtual) {
    const gripState=e.kind === 'movement' && (playing || !live || paused) ? e.before : s;
    if (gripState?.mmu_gripped === true)
      svg += `<text id="grip-indicator" x="${lane.x+68}" y="${435+hardwareOffset}" dominant-baseline="middle" text-anchor="middle" font-size="10" fill="var(--scene-text, #304c65)">Gripped</text>`;
  }
  Object.assign(c, {extruderGearY, svg});
}

// Toolhead reference lines and cooling tube measurements
function references(c) {
  let {s, entry, end, path, dimensionLink, svg, tube, length, progress, start, extruderGearY}=c;
  // Nozzle-relative reference guides, matching the documentation diagrams.
  const references = toolheadReferences(s).map(ref => ({...ref,
    y: tube.getPointAtLength(progress(ref.position) * length).y})).sort((a, b) => a.y - b.y);
  let previousLabelY = -Infinity;
  for (const ref of references) {
    if (ref.label === 'Blade' && ref.y < extruderGearY) {
      svg += `<g class="toolhead-reference"><text ${dimensionLink('Blade')} x="${E+86}" y="${ref.y}" text-anchor="start" dominant-baseline="middle" font-size="10" fill="var(--scene-muted, #6e8190)"><tspan class="reference-title">Blade · ${round1(ref.distance)}mm</tspan></text></g>`;
      continue;
    }
    if (ref.label === 'Extruder sensor' && ref.gap != null) {
      const gearY=references.find(item=>item.label === 'Extruder entry').y;
      const top=Math.min(ref.y,gearY)+2, bottom=Math.max(ref.y,gearY)-2;
      svg += `<g class="toolhead-reference"><path d="M${E-25} ${ref.y} H${E-70} ${dimensionArrow(E-55,top,bottom)}" fill="none" stroke="var(--scene-border, #96abba)"/><text ${dimensionLink('Extruder to entry')} x="${E-63}" y="${(ref.y+gearY)/2}" text-anchor="end" dominant-baseline="middle" font-size="10" fill="var(--scene-muted, #6e8190)">Extruder to entry · ${round1(ref.gap)}mm</text></g>`;
      continue;
    }
    const labelY = Math.max(ref.y, previousLabelY + 17);
    previousLabelY = labelY;
    svg += `<g class="toolhead-reference"><path d="M${ref.label === 'Extruder entry' ? E-46 : E-25} ${ref.y} H${E-55} L${E-70} ${labelY}" fill="none" stroke="var(--scene-border, #96abba)" stroke-width="1"/><text ${dimensionLink(ref.label)} x="${E-76}" y="${labelY}" text-anchor="end" dominant-baseline="middle" font-size="10" fill="var(--scene-muted, #6e8190)"><tspan class="reference-title">${ref.label} · ${round1(ref.distance)}mm</tspan></text></g>`;
  }
  const cooling = coolingTubeMeasurements(s);
  if (cooling) {
    const startY = tube.getPointAtLength(progress(cooling.start) * length).y;
    const endY = tube.getPointAtLength(progress(cooling.end) * length).y;
    const midY = (startY + endY) / 2;
    svg += `<g class="cooling-measurements" fill="var(--scene-muted, #6e8190)" font-size="10"><path d="M${E-25} ${startY} H${E-70} M${E-25} ${endY} H${E-70}" fill="none" stroke="var(--scene-border, #96abba)"/><path data-part="cooling-length-arrow" d="${dimensionArrow(E-55,startY-2,endY+2,-4)}" fill="none" stroke="var(--scene-border, #96abba)"/><text ${dimensionLink('Cooling tube position')} x="${E-76}" y="${startY}" text-anchor="end" dominant-baseline="middle">Cooling tube position · ${round1(cooling.position)}mm</text><text ${dimensionLink('Cooling tube length')} x="${E-63}" y="${midY}" text-anchor="end" dominant-baseline="middle">Cooling tube length · ${round1(cooling.length)}mm</text></g>`;
  }
  Object.assign(c, {references, previousLabelY, cooling, svg});
}

// Sensor markers with homing rings and pulses
function sensors(c) {
  let {sensorPulse, s, gate, entry, end, virtual, selectionLit, svg, tube, isTouchSensor, sensorPoint, isBufferSensor, displayedSensors, start}=c;
  for (const [name, d] of displayedSensors) {
    if (isBufferSensor(name)) continue;
    const suffix = sensorName(name).match(/^mmu_.*_(\d+)$/);
    if (suffix && Number(suffix[1]) !== gate && !virtual) continue;
    if (d.position === null && !isTouchSensor(name)) continue;
    const pt = sensorPoint(name, d, true);
    const short = name.split(':').at(-1);
    const sensorLabel = isTouchSensor(name) ? 'touch' : /^mmu_entry(?:_\d+)?$/.test(short) ? 'entry' : /^mmu_exit(?:_\d+)?$/.test(short) ? 'exit' : short === 'mmu_shared_exit' ? 'shared exit' : short;
    // Leave a one-unit gap outside the tube and the five-unit sensor radius. MMU-side labels sit left of the
    // tube, toolhead-side labels right of it.
    const sensorX=pt.x-16;
    const mmuSide=isTouchSensor(name) || pt.x < BUFFER_CENTER_X;
    const branchGate=virtual && ['entry','exit'].includes(sensorLabel) ? virtual.branches.find(branch=>branch.x===pt.x)?.gate : undefined;
    // A branch's sensors brighten with its gear as the selection arrives, rather than switching at once.
    const sensorClass=branchGate!==undefined ? ' virtual-gate-sensor' : '';
    const sensorLit=branchGate!==undefined ? selectionLit(branchGate) : 1;
    const showLabel=!virtual || !['entry','exit'].includes(sensorLabel) || pt.x===virtual.branches[0].x;
    svg += `<g class="sensor-marker${sensorClass}${d.state === null ? ' sensor-disabled' : ''}" data-sensor="${escape(name)}"${sensorClass ? ` style="--sensor-lit:${sensorLit}"` : ''}>${showLabel ? `<text x="${mmuSide ? pt.x - 29 : pt.x + 29}" y="${sensorLabel === 'entry' ? pt.y : pt.y + 4}" dominant-baseline="${sensorLabel === 'entry' ? 'central' : 'auto'}" text-anchor="${mmuSide ? "end" : "start"}" font-size="10" fill="var(--scene-text, #456078)">${escape(sensorLabel)}</text>` : ''}<circle cx="${sensorX}" cy="${pt.y}" r="5" fill="${d.state === true ? "#399b64" : d.state === false ? "#fff" : "#9ba5ae"}" stroke="#698394"/></g>`;
    svg += homingRing(name,sensorX,pt.y);
    if (sensorPulse?.sensor === name) {
      const elapsed = (animationNow() - sensorPulse.start) / 650;
      if (elapsed >= 0 && elapsed < 1) {
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        svg += `<circle data-part="sensor-pulse" cx="${sensorX}" cy="${pt.y}" r="${reduced ? 10 : 7 + elapsed * 15}" fill="none" stroke="${sensorPulse.reverse ? 'var(--sensor-clear-pulse)' : '#48c982'}" stroke-width="2.5" opacity="${1-elapsed}"/>`;
      }
    }
  }
  Object.assign(c, {svg});
}

// MMU and extruder gear wheels
function gears(c) {
  let {live, paused, playing, e, s, gate, entry, end, hardwareOffset, virtual, lane, path, branchOpacity, svg, length, extruderGearY}=c;
  for (const [x, y, type, gearGate] of [
    [(virtual?.active ?? lane).x,435+hardwareOffset,"gear",gate],
    [E, extruderGearY, "extruder"],
  ]) {
    const names = s.drive_motors?.[type] || Object.keys(s.motors).filter(n => n.includes(type));
    // Coupled motors share travel; do not add their distances or inactive gates.
    const distance = names.length ? (s.motors[names[0]] || 0) : 0;
    // Both gear pairs use the toolhead scale so equal travel gives equal rotation.
    const pixelsPerMm = .27 * length / (end-entry);
    const angle = gearAngle(virtual && type==='gear' && gearGate!==gate ? 0 : distance, pixelsPerMm, 14.25);
    const gripState = e.kind === 'movement' && (playing || !live || paused) ? e.before : s;
    const gripSpacing = type === 'gear' && !virtual && gripState?.mmu_gripped === false ? 26.25 : 17.25;
    const spacing = type === 'gear' && !virtual ? animatedGearSpacing(gripSpacing) : gripSpacing;
    for (const side of [-1, 1])
      svg += `<g data-part="${type}-wheel" opacity="${virtual && type==='gear' ? branchOpacity : 1}" transform="translate(${x + side * spacing} ${y}) rotate(${(type === "extruder" ? -1 : 1) * side * angle}) scale(.75)"><circle r="19" fill="var(--scene-gear, #d8e4ed)" stroke="var(--scene-border, #6d899f)"/><path d="M-14 0 H14 M0 -14 V14" stroke="var(--scene-spoke, #526f86)"/><circle class="gear-center" r="3" fill="var(--scene-spoke, #526f86)"/></g>`;
  }
  Object.assign(c, {svg});
}

// Encoder and buffer
function devices(c) {
  let {live, playing, played, events, sensorPulse, e, s, end, path, dimensionLink, svg, unitSensors, encoderAnchor, sensorPoint, isBufferSensor, sensorFill, encoderPoint, start}=c;
  for (const [name, d] of Object.entries(s.devices)) {
    if (name.includes(':') && name.split(':')[0] !== s.unit) continue;
    if (name.endsWith(":encoder")) {
      const pt = encoderPoint();
      if (encoderAnchor) {
        const sensor = unitSensors.find(([name]) => name === encoderAnchor.name);
        const anchor = sensorPoint(...sensor);
        const top = Math.min(pt.y,anchor.y), bottom = Math.max(pt.y,anchor.y);
        const value = `${encoderAnchor.offset > 0 ? '+' : ''}${round1(encoderAnchor.offset)}mm`;
        const unitIndex = s.units?.indexOf(s.unit) ?? 0;
        if (Object.hasOwn(s.configuration?.parameters?.[unitIndex] || {}, 'gate_endstop_to_encoder'))
        svg += `<g id="encoder-offset" fill="var(--scene-muted, #6e8190)" font-size="10"><path d="M${pt.x-106} ${anchor.y} H${pt.x-94} M${pt.x-106} ${pt.y} H${pt.x-94} ${dimensionArrow(pt.x-100,top+2,bottom-2)}" fill="none" stroke="var(--scene-border, #96abba)"/><text ${dimensionLink('Encoder offset')} x="${pt.x-108}" y="${(top+bottom)/2-6.5}" text-anchor="end" dominant-baseline="middle"><tspan class="encoder-offset-title">Gate endstop to</tspan><tspan class="encoder-offset-value" x="${pt.x-108}" dy="13" text-anchor="middle">encoder · ${value}</tspan></text></g>`;
      }
      // Device positions are interpolated alongside each captured move.
      const angle = gearAngle(d.encoder_pos || 0, 1, 11);
      const encoderReading = lastEncoderReading(events, live && playing ? events.find(event => event.seq === played) : e, s.unit);
      svg += `<g id="encoder-marker"><g transform="rotate(${-angle} ${pt.x} ${pt.y})"><circle cx="${pt.x}" cy="${pt.y}" r="11" fill="#faf4d9" stroke="#9d8e52"/><path d="M${pt.x - 7} ${pt.y} H${pt.x + 7} M${pt.x} ${pt.y - 7} V${pt.y + 7}" stroke="#9d8e52"/><circle cx="${pt.x - 7}" cy="${pt.y}" r="2" fill="#9d8e52"/></g><text x="${pt.x + 25}" y="${pt.y + 4}" text-anchor="start" font-size="11" fill="var(--scene-text, #304c65)">Encoder</text>${Number.isFinite(encoderReading) ? `<text id="encoder-measurement" x="${pt.x-25}" y="${pt.y+4}" text-anchor="end" font-size="11" fill="var(--scene-text, #304c65)">${encoderReading.toFixed(1)}mm</text>` : ''}</g>`;
    }
    if (name.endsWith(":buffer")) {
      // A different buffer on the new unit fades out and in with the Bowden (see `bufferFade` below).
      svg += `<g data-part="buffer-layer" opacity="@@buffer-opacity@@">${renderBuffer(s,d,unitSensors.filter(([sensor])=>isBufferSensor(sensor)),sensorFill,{homingRing,pulse:sensorPulse,now:animationNow,gapFill:'@@buffer-gap@@'})}</g>`;
    }
  }
  Object.assign(c, {svg});
}

// Filament inside the toolhead: retained pieces, residual, the tip and the nozzle fill
function nozzle(c) {
  let {s, gate, tip, end, mmuColumn, path, dimensionLink, nozzleExit, shadowEndX, shadowEndY, nozzleFunnel, svg, tube, length, progress, tipScale, coneOffset, shapedTipOpacity, start, shapedTip}=c;
  const excess = s.bowden_unabsorbed_mm || 0;
  if (Number.isFinite(excess) && excess !== 0) svg += `<text id="bowden-excess" x="${EXIT-20}" y="${bowdenLengthLabel(s).includes('Uncalibrated') ? 94 : 78}" font-size="10" fill="var(--scene-text, #304c65)"><tspan x="${EXIT-20}">Excess filament</tspan><tspan x="${EXIT-20}" dy="14">${excessFilamentLabel(excess)}</tspan></text>`;
  const residual = s.filament?.residual_mm || 0;
  const fragment = s.filament?.fragment_mm || 0;
  const retainedColor = s.nozzle_color || s.filament?.retained_color || DEFAULT_COLOR;
  const residualTop = tube.getPointAtLength(progress(end-residual)*length).y;
  const nozzleZero = tube.getPointAtLength(progress(end)*length).y;
  if (s.retained_segment && !s.retained_segment.residual_only) {
    const segment = s.retained_segment;
    const top = tube.getPointAtLength(progress(segment.start)*length).y;
    const coneTip = segment.end;
    const bottom = tube.getPointAtLength(Math.max(progress(segment.start)*length,progress(segment.end)*length-(segment.tip_shape === 'cone' ? tipScale : 0))).y;
    const cutLength = Math.max(0, segment.end-segment.start);
    svg += `<path id="retained-fragment" d="M${E} ${top} L${E} ${bottom}" fill="none" stroke="#${segment.color}" stroke-width="7"/>`;
    if (cutLength > .01) {
      const centerY=tube.getPointAtLength(progress((segment.start+segment.end)/2)*length).y;
      const rows=Array.from({length:281},(_,i)=>200+i)
        .filter(y=>Math.abs(y-452)>=20);
      const labelY=rows.reduce((best,y)=>Math.abs(y-centerY)<Math.abs(best-centerY) ? y : best,rows[0] ?? 470);
      svg += `<g id="cut-fragment-label"><text id="cut-fragment-length" x="${E+64}" y="${labelY}" text-anchor="start" dominant-baseline="middle" font-size="9" fill="var(--scene-muted, #6e8190)">Cut fragment · ${round1(cutLength)}mm</text></g>`;
    }
    if (segment.tip_shape === 'cone') {
      // The shrinking fragment's cone must never cover the incoming filament behind it.
      svg += `<defs><clipPath id="fragment-tip-clip"><rect x="${E-10}" y="${top}" width="20" height="${Math.max(0,nozzleExit-top)}"/></clipPath></defs><g clip-path="url(#fragment-tip-clip)">${shapedTip(coneTip,segment.color,'fragment-tip',segment.hair_length ?? 15)}</g>`;
    }
  } else if (fragment > 0 && !Object.hasOwn(s, 'retained_segment')) {
    const top = tube.getPointAtLength(progress(end-residual-fragment)*length).y;
    svg += `<path id="retained-fragment" d="M${E} ${top} L${E} ${residualTop}" fill="none" stroke="#${retainedColor}" stroke-width="7"/>`;
  }
  if (residual > 0) {
    svg += `<path id="residual-filament" d="M${E} ${residualTop} L${E} ${nozzleZero}" fill="none" stroke="#${retainedColor}" stroke-width="7"/><text id="residual-dimension" ${dimensionLink('Residual')} x="${E+19}" y="452" text-anchor="start" font-size="9" fill="var(--scene-muted, #6e8190)">Residual · ${residual}mm</text>`;
  }
  // Show the incoming front inside the residual instead of hiding it under a uniform color.
  if (residual > 0 && tip > end-residual) {
    const frontY=tube.getPointAtLength(Math.max(progress(end-residual)*length,progress(tip)*length-coneOffset)).y;
    svg += `<path id="residual-incoming" d="M${E} ${residualTop} L${E} ${frontY}" fill="none" stroke="#${s.filament?.incoming_color || DEFAULT_COLOR}" stroke-width="7"/>`;
  }
  // A cosmetic seam keeps touching cut pieces distinct until the joint reaches the nozzle.
  const cutJoint = s.retained_segment;
  if (cutJoint && !cutJoint.residual_only && cutJoint.start < end-.01
      && tip >= cutJoint.start-.01) {
    const jointY = tube.getPointAtLength(progress(cutJoint.start)*length).y;
    const jointOpacity = Math.max(0, Math.min(1, (end-cutJoint.start)/5));
    svg += `<path id="cut-joint-gap" d="M${E-3.5} ${jointY} H${E+3.5}" stroke="var(--scene-tube, #edf4f8)" stroke-width="0.75" opacity="${jointOpacity}"/>`;
  }
  // Below the nozzle only the MMU column may show filament, so a tip never draws across the toolhead.
  const tipClip=(id,withFunnel)=>`<clipPath id="${id}"><rect x="0" y="0" width="${SCENE_WIDTH}" height="${nozzleZero}"/><rect x="0" y="${nozzleZero}" width="${mmuColumn.right}" height="${MASK_HEIGHT-nozzleZero}"/>${withFunnel ? `<path d="${nozzleFunnel}"/>` : ''}</clipPath>`;
  const flooding=s.nozzle_fill_fraction != null && s.nozzle_fill_fraction < 1 && tip >= end-.001;
  if (residual > 0 || tip >= end-.01) {
    // Once the incoming end reaches zero, do not leave an old-color rim around its cone.
    const funnelColor=tip >= end-.01 ? s.filament?.incoming_color || DEFAULT_COLOR
      : s.nozzle_outlet_color || purgeOutletColor(s,s.extruded_mm || 0);
    svg += `<path id="nozzle-funnel" d="${nozzleFunnel}" fill="${flooding ? (s.nozzle_fill_from ? `#${s.nozzle_fill_from}` : 'var(--scene-tube, #edf4f8)') : `#${funnelColor}`}"/>`;
  }
  const plan=s.purge_transition;
  const meltingTip=tip >= end-.001 && plan && s.extruded_mm < plan.start_mm+plan.retained_mm+plan.mix_mm;
  if (spoolFill(s,gate)>0 && (s.tip_shapes?.[gate] === 'cone' || meltingTip || s.cone_edge_opacity > 0)) {
    svg += `<defs>${tipClip('tip-nozzle-clip',!flooding)}</defs><g opacity="${shapedTipOpacity}" mask="url(#bowden-end-fade)" clip-path="url(#tip-nozzle-clip)">${shapedTip(tip,s.filament?.incoming_color || DEFAULT_COLOR,'attached-tip')}</g>`;
  }
  if (tip >= end-.01 || Number.isFinite(s.purge_transition?.nozzle_arrival_mm)) {
    const fillHeight=(nozzleExit-nozzleZero)*(s.nozzle_fill_fraction ?? 1);
    const outletColor=purgeOutletColor(s,s.extruded_mm || 0);
    svg += `<defs><linearGradient id="nozzle-fill-gradient" gradientUnits="userSpaceOnUse" x1="${E}" x2="${E}" y1="${nozzleZero}" y2="${nozzleZero+Math.max(.001,fillHeight)}"><stop stop-color="#${s.filament?.incoming_color || DEFAULT_COLOR}"/><stop offset="1" stop-color="#${outletColor}"/></linearGradient><clipPath id="nozzle-fill-clip"><rect x="${E-10}" y="${nozzleZero}" width="20" height="${fillHeight}"/></clipPath></defs><path id="nozzle-fill-front" d="${nozzleFunnel}" fill="url(#nozzle-fill-gradient)" clip-path="url(#nozzle-fill-clip)"/>`;
  }
  // Keep the dissolving tip edge above the flood, clipped to the complete nozzle bore.
  if (c.attachedTipEdge) svg += `<defs>${tipClip('tip-edge-clip',true)}</defs><g opacity="${shapedTipOpacity}" mask="url(#bowden-end-fade)" clip-path="url(#tip-edge-clip)">${c.attachedTipEdge}</g>`;
  svg += `<path id="nozzle-side-shadows" d="M${E-5.5} 435 Q${E-3.5} 435 ${E-3.5} 437 V449 L${E-shadowEndX} ${shadowEndY} V${nozzleExit} M${E+5.5} 435 Q${E+3.5} 435 ${E+3.5} 437 V449 L${E+shadowEndX} ${shadowEndY} V${nozzleExit}" fill="none" stroke="color-mix(in srgb, var(--scene-nozzle, #9cafbe), black 50%)" stroke-width="0.8" stroke-linecap="round"/>`;
  Object.assign(c, {excess, residual, fragment, retainedColor, residualTop, nozzleZero, cutJoint, tipClip, flooding, plan, meltingTip, svg});
}

// Purged material below the nozzle
function purge(c) {
  let {s, path, nozzleDiameter, nozzleWidth, nozzleExit, svg, start}=c;
  const {amount:coilAmount, layers, pitch, coils, drop} = purgeCoils(s, nozzleDiameter);
  if (coilAmount > 0) {
    const strandWidth = nozzleWidth * 1.5; // Visual emphasis only; volume uses the selected diameter.
    let waste = '';
    for (const coil of coils) {
      const reveal = `fill="none" stroke-linecap="round" pathLength="100" stroke-dasharray="${coil.fraction*100} 100"`;
      waste += `<path d="${coil.path}" ${reveal} stroke="color-mix(in srgb, #${coil.color}, black 28%)" stroke-width="${strandWidth}" transform="translate(0,${strandWidth*.1})"/><path data-part="purge-material" d="${coil.path}" ${reveal} stroke="#${coil.color}" stroke-width="${strandWidth}"/><path d="${coil.path}" ${reveal} stroke="color-mix(in srgb, #${coil.color}, white 38%)" stroke-width="${strandWidth*.2}" transform="translate(0,${-nozzleWidth*.26})"/>`;
    }
    const landing = 498-Math.max(0,layers-1)*pitch;
    const strandTip = nozzleExit+(landing-nozzleExit)*Math.min(1,coilAmount/1.2);
    const stem = Math.min(strandTip,463);
    const strand = `M${E} ${nozzleExit} V${stem}` + (strandTip>stem ? ` C${E+3} ${stem+(strandTip-stem)/3} ${E-4} ${stem+(strandTip-stem)*2/3} ${E} ${strandTip}` : '');
    waste += `<path data-part="purge-stream" d="${strand}" fill="none" stroke="#${purgeOutletColor(s,s.extruded_mm)}" stroke-width="${strandWidth}"/>`;
    svg += `<defs><clipPath id="purge-screen"><rect x="${E-100}" y="435" width="150" height="70"/></clipPath></defs><g clip-path="url(#purge-screen)"><g id="purge-pile" transform="translate(0,${drop*75})" aria-label="Extruded filament">${waste}</g></g><text x="${E+43}" y="491" text-anchor="start" font-size="10" fill="var(--scene-muted, #6e8190)">${purgeStrandLength(s.extruded_mm, nozzleDiameter).toFixed(1)} mm strand</text>`;
  }
  Object.assign(c, {coilAmount, layers, pitch, coils, drop, svg});
}

// Cutter blades, painted last
function cutters(c) {
  let {live, playing, s, tip, end, path, svg, tube, length, progress, schematicDistance, shapedTip}=c;
  // Paint cutters last so they remain above attached and retained filament.
  for (const cutter of s.cutters || []) {
    const onMmu = cutter.kind === 'mmu';
    // The MMU end of the path is schematic, so its cutter is placed on the scale the tip is drawn on there
    const pt = tube.getPointAtLength(onMmu ? schematicDistance(cutter.position) : progress(cutter.position) * length);
    const direction = onMmu ? 1 : -1;
    const bladeEvent = live ? playing?.event : null;
    const pressed = bladeEvent && ['cut','release'].includes(bladeEvent.kind)
      ? bladeStroke(bladeEvent, playing?.fraction || 0, cutter.kind)
      : (onMmu ? s.mmu_cutter_pressed : s.cutter_pressed) ? 1 : 0;
    if (onMmu && pressed > 0) {
      // The cutter shears by bending the end of the bowden aside with the offcut still in it; the release ejects the offcut
      const releasing = bladeEvent?.kind === 'release' && bladeEvent.cutter === 'mmu';
      const cutting = bladeEvent?.kind === 'cut' && bladeEvent.cutter === 'mmu';
      const offcut = releasing ? bladeEvent.before?.mmu_offcut : cutting ? bladeEvent.state?.mmu_offcut : s.mmu_offcut;
      const from = schematicDistance(cutter.position), reach = 90, slide = 26, steps = 16;
      const points = Array.from({length:steps+1},(_,i)=>tube.getPointAtLength(Math.min(length,from+reach*i/steps)));
      const line = list => list.map((p,i)=>`${i?'L':'M'}${p.x} ${p.y}`).join(' ');
      // Full deflection at the blade, easing to none where the bend meets the fixed bowden
      const bent = amount => line(points.map((p,i)=>({x:p.x+slide*amount*(1-i/steps)**2,y:p.y})));
      const fall = releasing ? (playing?.fraction || 0)**2 : 0;
      const held = measurePath(bent(releasing ? 1 : pressed)), stub = held.getPointAtLength(8), start = held.getPointAtLength(0);
      const piece = offcut ? `<g data-part="mmu-offcut" transform="translate(0 ${fall*70})" opacity="${1-fall}"><path d="M${start.x} ${start.y} L${stub.x} ${stub.y}" fill="none" stroke="#${offcut.color || DEFAULT_COLOR}" stroke-width="7"/>${offcut.tip_shape === 'square' ? '' : shapedTip(8,offcut.color || DEFAULT_COLOR,'mmu-offcut-tip',offcut.hair_length ?? 15,held)}</g>` : '';
      svg += `<g data-part="mmu-cut-section" data-slide="${pressed}"><path d="${line(points)}" fill="none" stroke="var(--scene-background, white)" stroke-width="22"/><path d="${bent(pressed)}" fill="none" stroke="var(--scene-tube-border, #b1c4d2)" stroke-width="19.25"/><path d="${bent(pressed)}" fill="none" stroke="var(--scene-tube, #edf4f8)" stroke-width="14.75"/>${piece}</g>`;
    }
    // The triangle tip rests at the 20px tube's outer wall, then crosses it.
    const x = pt.x - direction * 10 + direction * 20 * pressed;
    svg += `<g class="cutter-blade" aria-label="${onMmu ? 'MMU servo cutter' : cutter.before_extruder ? 'Cutter before extruder' : 'Cutter inside extruder'}"><path data-part="blade" data-pressed="${pressed}" d="M${x} ${pt.y} l${-direction*42} -5 v10 Z" fill="var(--scene-nozzle, #9cafbe)" stroke="var(--scene-spoke, #526f86)"/></g>`;
  }
  Object.assign(c, {svg});
}

// Fades over the whole scene, then the markup goes into the page and retained nodes are mounted
function finish(c) {
  let {s, spoolRequests, gate, path, svg, length, unitSensors, constrained, filamentDash, focus, fullTipOpacity, slidingRack, rackArrows, rackFade, bowdenReach, enclosurePaths, excess}=c;
  // Away from the exit, everything attached to the MMU side fades: Bowden, branches, gears, sensors, tips.
  // The mask covers only the scene above the unit enclosures (y<534, except the enclosures themselves) and the MMU side (left of the buffer's centre, clear of the toolhead labels),
  // so the rack, the toolhead and the top of the Bowden stay put.
  // Units sharing one buffer keep it; otherwise it fades away (leaving the plain Bowden) and back with the Bowden.
  const bufferOf=gate=>s.unit_layouts?.find(layout=>layout.gates.includes(gate))?.buffer ?? null;
  const bufferFade=s.gate_slide!==undefined && bufferOf(s.gate_from)!==bufferOf(s.gate_to);
  const bufferShown=bufferFade ? bowdenReach : 1, gapShade=Math.round(255*(1-bufferShown));
  svg=svg.replace('@@buffer-opacity@@',bufferShown).replace('@@buffer-gap@@',`rgb(${gapShade},${gapShade},${gapShade})`);
  const reachCut=195+bowdenReach*380;
  svg = `<defs><linearGradient id="bowden-reach-gradient" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1="${reachCut-40}" y2="${reachCut}"><stop stop-color="white"/><stop offset="1" stop-color="black"/></linearGradient><mask id="bowden-reach-fade" maskUnits="userSpaceOnUse" x="-1000" y="-100" width="3000" height="4000"><rect x="-1000" y="-100" width="3000" height="4000" fill="white"/><rect x="-1000" y="0" width="${1000+BUFFER_CENTER_X}" height="534" fill="url(#bowden-reach-gradient)"/><path d="${enclosurePaths}" fill="white"/></mask></defs><g mask="url(#bowden-reach-fade)">${svg}</g>`;
  // The scene is rebuilt every frame: put keyboard focus back on the same control afterwards.
  const focused=document.activeElement?.closest?.('#scene [data-part], #scene [data-parameter]');
  const focusKeys=focused ? ['part','gate','direction','parameter'].filter(key=>focused.dataset[key]!==undefined) : [];
  host.innerHTML = rackFade ? `${rackFadeMask(rackFade)}<g mask="url(#rack-edge-fade)">${svg}</g>${rackArrows}` : svg+rackArrows;
  if (focused) host.querySelector(focusKeys.map(key=>`[data-${key}="${CSS.escape(focused.dataset[key])}"]`).join(''))?.focus({preventScroll:true});
  for (const [index] of spoolRenderers) {
    if (!spoolRequests.has(index)) spoolRenderers.delete(index);
  }
  for (const spoolHost of host.querySelectorAll('[data-part="espooler"]')) {
    const index=Number(spoolHost.dataset.gate);
    let renderer=spoolRenderers.get(index);
    if (!renderer) { renderer=new SpoolRenderer(spoolHost); spoolRenderers.set(index,renderer); }
    renderer.update(spoolRequests.get(index));
    renderer.mount(spoolHost);
  }
  const encoderMarker=$("encoder-marker");
  if (encoderMarker) {
    // Raised above the spools, but still faded with the rest of the MMU side.
    const layer=document.createElementNS('http://www.w3.org/2000/svg','g');
    layer.setAttribute('mask','url(#bowden-reach-fade)');
    layer.append(encoderMarker);
    host.append(layer);
  }
  const excessLabel = $("bowden-excess");
  if (excessLabel) {
    const bounds = $("bowden-length").getBBox();
    excessLabel.setAttribute('text-anchor', 'middle');
    for (const line of excessLabel.querySelectorAll('tspan'))
      line.setAttribute('x', bounds.x + bounds.width/2);
  }
  $("filament").setAttribute("stroke-dasharray", filamentDash);
  $("filament").setAttribute("opacity", fullTipOpacity);
  $("filament").setAttribute("visibility", slidingRack || spoolFill(s,gate)===0 ? "hidden" : "visible");
  const encoderTitle = host.querySelector('.encoder-offset-title');
  if (encoderTitle) host.querySelector('.encoder-offset-value')
    .setAttribute('x',Number(encoderTitle.parentNode.getAttribute('x'))-encoderTitle.getComputedTextLength()/2);
  return {warnings: constrained.warnings, unitSensors};
}
function render(nextView, nextSettings) {
  view=nextView; settings=nextSettings;
  const {viewMode, offline, live, paused, playing, played, events, sensorPulse, event: e, state: s}=view;
  // What the parts share for this frame: each reads what it needs and adds what it works out
  const c={viewMode, offline, live, paused, playing, played, events, sensorPulse, e, s};
  for (const part of [measure, rack, tips, syncIndicators, references, sensors, gears, devices, nozzle, purge, cutters]) part(c);
  return finish(c);
}
  return {render, advanceSpools, updateSpoolMotion, returnRackWhenIdle,
    resetSpoolArrows: ()=>spoolArrowVisibility.clear(), clear: ()=>host.replaceChildren()};
}
