/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Spool style picker, realism toggle and view angle/spacing sliders. Drawing the scene stays with the caller.
import {$} from './util.js';
import {renderSpool} from './spool.js';

export function initSpoolPicker(draw) {
  function spoolPreview(style,idPrefix) {
    return `<svg viewBox="0 0 40 44" aria-hidden="true"><g transform="translate(14 22)">${renderSpool({
      idPrefix,style,color:'36ad79',fillPercent:90,shadowStyle:$('spool-realism').checked ? 'blur' : 'crisp',windingLines:$('spool-realism').checked,viewAngle:77*Math.PI/180,
    })}</g></svg>`;
  }
  function selectSpoolPreview(style) {
    // The summary has its own resources; cloning an option would duplicate SVG IDs.
    $('spool-style-current').innerHTML=spoolPreview(style,'spool-preview-current');
  }
  for (const [value,name] of [['cardboard-windowed','Cardboard · windowed'],['dark-windowed','Black plastic · windowed'],['cardboard-solid','Cardboard · solid'],['dark-solid','Black plastic · solid']]) {
    const label=document.createElement('label');
    label.className='spool-style-choice';
    label.dataset.tooltip=name;
    label.innerHTML=`<input type="radio" name="spool-style" value="${value}" aria-label="${name}" ${value==='cardboard-windowed' ? 'checked' : ''}>${spoolPreview(value,`spool-preview-${value}`)}`;
    label.querySelector('input').onchange=()=>{
      selectSpoolPreview(value);
      $('spool-style-menu').open=false;
      draw();
    };
    if (value==='cardboard-windowed') selectSpoolPreview(value);
    $('spool-style-grid').append(label);
  }
  // Refresh the shared spool previews and scene together.
  $('spool-realism').onchange=()=>{
    for (const label of $('spool-style-grid').children) {
      const style=label.querySelector('input').value;
      label.querySelector('svg').outerHTML=spoolPreview(style,`spool-preview-${style}`);
    }
    selectSpoolPreview(document.querySelector('input[name=spool-style]:checked').value);
    draw();
  };
  for (const id of ['spool-angle','spool-spacing']) {
    $(id).oninput=()=>{
      $(`${id}-value`).value=$(id).value+(id==='spool-angle' ? '°' : '');
      draw();
    };
  }
}
