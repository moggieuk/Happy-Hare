/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// The playback buttons, timeline and the two status lines above the scene.
import {$} from './util.js';

export function renderControls({running, index, count, pauseAfterStep, offline, previousToolchange, nextToolchange,
    modeText, positionText, timelineValue}) {
  $("replay").disabled = pauseAfterStep || (!running && index >= count - 1);
  const playLabel = running ? 'Pause after current step' : offline ? 'Play recording' : 'Play continuously — follow stream when caught up';
  $("replay").setAttribute('aria-label', playLabel);
  $("replay").dataset.tooltip = playLabel;
  $("replay").querySelector('path').setAttribute('d', running
    ? 'M6 4h4v16H6z M14 4h4v16h-4z' : 'M7 4v16l13-8z');
  $("beginning").disabled = $("previous").disabled = index <= 0;
  $("next").disabled = index >= count - 1;
  $("play-step").disabled = Boolean(running) || $("next").disabled;
  $("play-previous").disabled = Boolean(running) || $("previous").disabled;
  $("previous-toolchange").disabled = !previousToolchange;
  $("next-toolchange").disabled = !nextToolchange;
  $("mode").textContent = modeText;
  $("position").textContent = positionText;
  $("timeline").max = count - 1;
  $("timeline").value = timelineValue;
}
