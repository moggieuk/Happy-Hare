import {test} from 'node:test';
import assert from 'node:assert/strict';
import {parameterCommand,macroCommand,pyRepr,editorParameters,macroCatalog,editorMacroParameters,toolheadObjects} from './editor.js';
import {liveObjects} from './live-fixture.mjs';
import {liveMachine} from './live-config.js';

test('parameters are applied with MMU_TEST_CONFIG, one value on one line',()=>{
 assert.equal(parameterCommand(1,'toolhead_ooze_reduction',2.5),'MMU_TEST_CONFIG UNIT=1 TOOLHEAD_OOZE_REDUCTION="2.5"');
 for (const bad of ['1\nM112','1;M112']) assert.throws(()=>parameterCommand(0,'x',bad),/one parameter value/);
});
test('macro variables keep their type and cannot break out of the command',()=>{
 assert.equal(macroCommand('_M','blade_pos',' 52 ',40),'SET_GCODE_VARIABLE MACRO=_M VARIABLE=blade_pos VALUE="52"');
 assert.equal(macroCommand('_M','flag','False',true),'SET_GCODE_VARIABLE MACRO=_M VARIABLE=flag VALUE="False"');
 assert.equal(macroCommand('_M','pos',"[1, 2]",[0,0]),'SET_GCODE_VARIABLE MACRO=_M VARIABLE=pos VALUE="[1, 2]"');
 assert.throws(()=>macroCommand('_M','blade_pos',"'text'",40),/existing value type/);
 assert.throws(()=>macroCommand('_M','blade_pos','abc',40),/Python literal/);
 assert.throws(()=>macroCommand('_M','pos','[1]"\nM112',[0]),/single quotes/);
 // Plain text for a string variable is escaped character by character.
 const command=macroCommand('_M','name','a"b\nc','old');
 assert.match(command,/^SET_GCODE_VARIABLE MACRO=_M VARIABLE=name VALUE="'(\\U[0-9a-f]{8})+'"$/);
 assert.ok(!/[\n]/.test(command) && command.split('"').length===3);
});
test('Python literals round trip through pyRepr',()=>{
 assert.equal(pyRepr([true,null,"it's",3.5]),"[True, None, 'it\\'s', 3.5]");
 assert.equal(pyRepr({a:1}),"{'a': 1}");
});
test('printer parameters come from configfile plus config_changes, grouped like MMU_TEST_CONFIG',()=>{
 const objects=liveObjects();
 const list=editorParameters(objects,0), toolhead=list.filter(p=>p.group.startsWith('Toolhead'));
 assert.ok(list.some(p=>p.group==='Machine Wide' && p.name==='form_tip_macro'));
 assert.ok(list.some(p=>p.group==='Unit 0' && p.name==='gate_homing_endstop'));
 assert.ok(toolhead.length>0 && toolhead.every(p=>p.name.startsWith('toolhead_')));
 const section=`mmu_toolhead ${objects.configfile.settings['mmu_unit unit0'].toolhead}`;
 objects.mmu.config_changes={[section]:{toolhead_residual_filament:9}};
 assert.equal(editorParameters(objects,0).find(p=>p.name==='toolhead_residual_filament').value,9);
 assert.deepEqual(editorParameters(objects,5),[]);
});
test('macro tuning variables exclude outputs, saved state and variables macros assign',()=>{
 const config={'gcode_macro _CUT':{variable_blade_pos:'1',variable_output_pos:'2',variable_saved_x:'3',variable_Bad:'4',variable_counter:'5',
   gcode:'SET_GCODE_VARIABLE MACRO=_CUT VARIABLE=counter VALUE=1\n'},mmu:{}};
 const catalog=macroCatalog(config);
 assert.deepEqual(catalog,[['_CUT','blade_pos']]);
 const list=editorMacroParameters(catalog,{'gcode_macro _CUT':{blade_pos:52}});
 assert.deepEqual(list.map(p=>[p.id,p.value,p.literal]),[['macro:_CUT:blade_pos',52,'52']]);
 assert.deepEqual(editorMacroParameters(catalog,{}),[]);
});
test('a shared toolhead is listed once with every unit that uses it and only its numeric toolhead values',()=>{
 const objects=liveObjects();
 objects.configfile.settings.mmu_machine.units=['unit0','unit1'];
 objects.configfile.settings['mmu_unit unit1']={...objects.configfile.settings['mmu_unit unit0']};
 const [toolhead,...others]=toolheadObjects(objects);
 assert.equal(others.length,0);
 assert.deepEqual(toolhead.units,[0,1]);assert.deepEqual(toolhead.unitNames,['unit0','unit1']);
 assert.ok(toolhead.parameters.length>0 && toolhead.parameters.every(p=>p.name.startsWith('toolhead_') && typeof p.value==='number'));
 objects.mmu.config_changes={[`mmu_toolhead ${toolhead.name}`]:{toolhead_residual_filament:7}};
 assert.equal(toolheadObjects(objects)[0].parameters.find(p=>p.name==='toolhead_residual_filament').value,7);
});
test('encoder resolution comes from the saved calibration, else the configured value',()=>{
 const objects=liveObjects();
 objects.configfile.settings['mmu_encoder unit0']={encoder_resolution:1.5};
 objects.configfile.settings['mmu_unit unit0'].encoder='unit0';
 delete objects.save_variables.variables.mmu_unit0_encoder_resolution;
 assert.equal(liveMachine(objects).units.unit0.encoder.parameters.resolution,1.5);
 objects.save_variables.variables.mmu_unit0_encoder_resolution=0.8;
 assert.equal(liveMachine(objects).units.unit0.encoder.parameters.resolution,0.8);
});
