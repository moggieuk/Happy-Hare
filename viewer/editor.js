/* Copyright (C) 2022-2026 Paul Morgan. GPLv3. */
// Runtime parameter and macro variable editing for a printer or the simulator.
// Everything is applied by sending MMU_TEST_CONFIG / SET_GCODE_VARIABLE through
// printer.gcode.script, so each edit is validated by Happy Hare / Klipper and appears
// in telemetry as a configuration change. The lists below are built from the same state
// the live view uses (configfile.settings plus mmu.config_changes).
import {effective} from './live-config.js';

export function parameterCommand(unit,name,value) {
  const text=String(value);
  if (/[\r\n;"]/.test(text)) throw Error('Enter one parameter value without quotes');
  return `MMU_TEST_CONFIG UNIT=${unit} ${name.toUpperCase()}=${JSON.stringify(text)}`;
}

// ---- Python literals (SET_GCODE_VARIABLE values are Python literals) ----------------------

export function pyRepr(value) {
  if (value===null || value===undefined) return 'None';
  if (typeof value==='boolean') return value ? 'True' : 'False';
  if (typeof value==='number') return String(value);
  if (typeof value==='string') return `'${value.replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/\n/g,'\\n')}'`;
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(', ')}]`;
  return `{${Object.entries(value).map(([k,v])=>`${pyRepr(k)}: ${pyRepr(v)}`).join(', ')}}`;
}

// Escaping every character keeps quotes and newlines from creating extra G-code.
const pyString=text=>"'"+[...text].map(c=>'\\U'+c.codePointAt(0).toString(16).padStart(8,'0')).join('')+"'";
const kindOfValue=value=>value===null ? 'none' : typeof value==='boolean' ? 'bool' : typeof value==='number' ? 'number'
  : typeof value==='string' ? 'string' : Array.isArray(value) ? 'list' : 'dict';
function kindOfLiteral(text) {
  if (/^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(text)) return 'number';
  if (/^(True|False)$/.test(text)) return 'bool';
  if (text==='None') return 'none';
  if (/^[[(]/.test(text)) return 'list';
  if (text[0]==='{') return 'dict';
  if (/^'/.test(text)) return 'string';
  return null;
}

export function macroCommand(macro,name,text,current) {
  const typed=text.trim(), kind=kindOfLiteral(typed);
  let literal;
  if (kind===null) {
    if (typeof current!=='string') throw Error('Enter a Python literal, for example 52, True, or [1, 2]');
    literal=pyString(text);
  } else {
    if (kind!==kindOfValue(current)) throw Error('Keep the existing value type for this macro variable');
    if (/["\r\n;]/.test(typed)) throw Error('Use single quotes and a single line');
    literal=typed;
  }
  return `SET_GCODE_VARIABLE MACRO=${macro} VARIABLE=${name} VALUE="${literal}"`;
}

// ---- Editor lists for a connected printer ---------------------------------------------------

const primitive=value=>value===null || ['number','string','boolean'].includes(typeof value) ||
  (Array.isArray(value) && value.every(item=>item===null || typeof item!=='object'));

function parameters(values,fields) {
  return Object.entries(values).filter(([name,value])=>(!fields || fields(name)) && primitive(value))
    .map(([name,value])=>({name,kind:typeof value,value,section:''}));
}

// Parameter sections only contain tunable options, so every entry is accepted by MMU_TEST_CONFIG.
// Toolhead and other hardware sections also hold pins, so only toolhead_* values are offered.
export function editorParameters(objects,unitIndex) {
  const names=[].concat(objects.configfile?.settings?.mmu_machine?.units || []), name=names[unitIndex];
  if (!name) return [];
  const toolhead=effective(objects,`mmu_unit ${name}`).toolhead || 'default';
  const groups=[['Machine Wide','machine',effective(objects,'mmu_parameters'),null],
    [`Unit ${unitIndex}`,'unit',effective(objects,`mmu_unit_parameters ${name}`),null],
    [`Toolhead '${toolhead}' · Unit ${unitIndex}`,'unit',effective(objects,`mmu_toolhead ${toolhead}`),n=>n.startsWith('toolhead_')]];
  return groups.flatMap(([group,scope,values,fields])=>parameters(values,fields).map(p=>({...p,group,scope})));
}

// Variables assigned by a macro body, outputs and saved state are not tuning variables.
export function macroCatalog(config) {
  const macros=Object.entries(config || {}).filter(([section])=>section.startsWith('gcode_macro '));
  const assigned=new Set();
  for (const [,options] of macros)
    for (const line of String(options.gcode || '').split('\n')) {
      if (!/SET_GCODE_VARIABLE/i.test(line)) continue;
      const macro=/\bMACRO=([A-Za-z0-9_]+)/i.exec(line), variable=/\bVARIABLE=([A-Za-z0-9_]+)/i.exec(line);
      if (macro && variable) assigned.add(`${macro[1].toUpperCase()}:${variable[1]}`);
    }
  return macros.flatMap(([section,options])=>{
    const macro=section.slice(12);
    return Object.keys(options).filter(key=>key.startsWith('variable_')).map(key=>key.slice(9))
      .filter(name=>!/^(_|output_|saved_)/.test(name) && /^[a-z][a-z0-9_]*$/.test(name) && !assigned.has(`${macro.toUpperCase()}:${name}`))
      .map(name=>[macro,name]);
  });
}

export function editorMacroParameters(catalog,values) {
  return catalog.filter(([macro,name])=>values[`gcode_macro ${macro}`]?.[name]!==undefined)
    .map(([macro,name])=>{
      const value=values[`gcode_macro ${macro}`][name];
      return {id:`macro:${macro}:${name}`,name,macro,value,literal:pyRepr(value),kind:'macro',group:macro,section:'Macro variables'};
    });
}

// Toolheads are shared between units, so each is listed once with every unit that uses it.
// Only toolhead_* values are offered: the section also holds pins.
export function toolheadObjects(objects) {
  const names=[].concat(objects.configfile?.settings?.mmu_machine?.units || []), found=new Map();
  names.forEach((unit,index)=>{
    const name=effective(objects,`mmu_unit ${unit}`).toolhead || 'default';
    if (!found.has(name)) found.set(name,{name,units:[],unitNames:[],
      parameters:parameters(effective(objects,`mmu_toolhead ${name}`),n=>n.startsWith('toolhead_')).filter(p=>p.kind==='number')});
    found.get(name).units.push(index);
    found.get(name).unitNames.push(unit);
  });
  return [...found.values()];
}
