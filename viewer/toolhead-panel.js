/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Toolhead Config panel: values are set with MMU_TEST_CONFIG, so it works on a printer and the simulator.
import {$, options} from './util.js';
import {parameterCommand} from './editor.js';

export function createToolheadPanel({rpc, action, refresh, changed}) {
  const configColumn=document.querySelector('.left-column');
  const toolheadPanel=document.createElement('aside');
  toolheadPanel.hidden=true;
  toolheadPanel.innerHTML=`<h2>Toolhead config</h2><p class="note">Applied with MMU_TEST_CONFIG. Shared toolheads affect every listed unit.</p><label>Toolhead object<select class="object-choice"></select></label><label>Parameter<select class="object-parameter"></select></label><label>Value<input class="object-value" type="number" step="any"></label><button class="object-apply">Apply</button><p class="note object-status" role="status"></p>`;
  configColumn.insertBefore(toolheadPanel,$('viewer-settings'));
  const toolheadField=name=>toolheadPanel.querySelector('.object-'+name);
  let toolheads=[], toolheadFingerprint='';
  function selectedToolhead() { return toolheads.find(item=>item.name===toolheadField('choice').value); }
  function renderToolheadParameters() {
    const previous=toolheadField('parameter').value, selected=selectedToolhead();
    options(toolheadField('parameter'),(selected?.parameters || []).map(item=>[item.name,item.name]));
    if (selected?.parameters.some(item=>item.name===previous)) toolheadField('parameter').value=previous;
    showToolheadValue();
  }
  function showToolheadValue() {
    const parameter=selectedToolhead()?.parameters.find(item=>item.name===toolheadField('parameter').value);
    toolheadField('value').value=parameter?.value ?? '';
    toolheadField('apply').disabled=!parameter;
  }
  function show(list) {
    const fingerprint=JSON.stringify(list);
    if (fingerprint===toolheadFingerprint) return;
    toolheadFingerprint=fingerprint; toolheads=list;
    const menu=toolheadField('choice'), previous=menu.value;
    options(menu,list.map(item=>[item.name,`${item.name} (${item.unitNames.join(', ')})`]));
    if (list.some(item=>item.name===previous)) menu.value=previous;
    toolheadPanel.dataset.available=String(list.length>0);
    renderToolheadParameters(); changed();
  }
  toolheadField('choice').onchange=renderToolheadParameters;
  toolheadField('parameter').onchange=showToolheadValue;
  toolheadField('apply').onclick=()=>action(async()=>{
    const number=Number(toolheadField('value').value);
    if (!Number.isFinite(number)) throw Error('Enter a numeric value');
    // Same path as typing the command: the change is reported as a config delta.
    await rpc('printer.gcode.script',{script:parameterCommand(selectedToolhead().units[0],toolheadField('parameter').value,number)});
    toolheadField('status').textContent='Applied with MMU_TEST_CONFIG.';
    await refresh();
  });
  return {element:toolheadPanel, show};
}
