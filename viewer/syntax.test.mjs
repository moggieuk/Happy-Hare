import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {spawnSync} from 'node:child_process';

// `node --check file.js` can accept a script that the browser rejects as a module (a repeated
// const, for example), and app.js cannot be imported without a DOM. Check every file as a module.
test('every viewer script parses as an ES module',()=>{
 const dir=new URL('.',import.meta.url).pathname, temp=fs.mkdtempSync(path.join(os.tmpdir(),'viewer-syntax-'));
 try {
  for (const name of fs.readdirSync(dir).filter(n=>n.endsWith('.js'))) {
   const copy=path.join(temp,name.replace(/\.js$/,'.mjs'));
   fs.copyFileSync(path.join(dir,name),copy);
   const result=spawnSync(process.execPath,['--check',copy],{encoding:'utf8'});
   assert.equal(result.status,0,`${name}: ${result.stderr}`);
  }
 } finally { fs.rmSync(temp,{recursive:true,force:true}); }
});
