/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Near-live: while the viewer follows the telemetry stream and nothing is moving, sensors can
// change without any event (a toggle, a filament inserted by hand). Status already reports them,
// so show those readings on top of the recorded state until the next event carries the same values.
import {qualifySensors} from './live-config.js';

// Only when the scene would otherwise show the live end of the stream, at rest.
export const overlayAllowed=({viewMode,offline,live,paused,playing,caughtUp,pendingMoves})=>
  viewMode==='playback' && !offline && live && !paused && !playing && caughtUp && pendingMoves===0;

// The replay's current scene with the reported sensor readings applied. The replay's own facts are
// left exactly as they were.
export function overlaySnapshot(replay,sensors) {
  const mmu=replay.facts?.printer?.mmu, machine=replay.facts?.printer?.mmu_machine;
  if (!mmu || !sensors) return null;
  const reported=qualifySensors(sensors,mmu.unit,mmu.gate,machine.units[mmu.unit]?.buffer?.name);
  const recorded=mmu.sensors;
  mmu.sensors={...recorded};
  for (const [name,reading] of Object.entries(reported))
    if (recorded[name]) mmu.sensors[name]={...recorded[name],triggered:reading.triggered,enabled:reading.enabled};
  try { return replay.snapshot(); }
  finally { mmu.sensors=recorded; }
}
