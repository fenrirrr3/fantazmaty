const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const assert = require('node:assert/strict');
const code = fs.readFileSync(path.resolve(__dirname, '../static/core/ui.js'), 'utf8');
const safety = code.slice(code.indexOf('    function formSnapshot('), code.indexOf('    function initializeBulkActions('));
const keys = code.slice(code.indexOf('    const userScope ='), code.indexOf('    let toast;'));
const guardStart = code.indexOf('        window.addEventListener("beforeunload", (event) => {');
const guard = code.slice(guardStart, code.indexOf('\n        });', guardStart) + 12);
const storageData = new Map();
function environment(user = '1') {
    const leave = [];
    const context = {Map, WeakSet, JSON, File: class {},
        document: {body: {dataset: {userId:user}}}, location:{pathname:'/audiodeskrypcje/7/'},
        window: {addEventListener(event, callback) {if (event === 'beforeunload') leave.push(callback);}},
        // Do not execute the debounce; test leaving immediately after the last keystroke.
        setTimeout() {return 1;}, clearTimeout() {}, queueMicrotask: cb => cb(),
        FormData: class {constructor(form) {this.form = form;} *[Symbol.iterator]() {for (const f of this.form.elements) yield [f.name, f.value];}},
        storage: {get:key=>storageData.get(key), set:(key,val)=>storageData.set(key,val), remove:key=>storageData.delete(key)},
    };
    vm.createContext(context);
    vm.runInContext('const initialized = new WeakSet(), trackedForms = new Map();' + keys + safety + '\n' + guard, context);
    return {init: context.initializeFormSafety, leave() {
        const event = {warned:false, preventDefault() {this.warned = true;}};
        for (const callback of leave) callback(event);
        return event.warned;
    }};
}
function form(name, value, autosave = false, textarea = true) {
    const callbacks = {};
    const field = {name, value, type:textarea?'textarea':'select-one', matches:selector=>textarea && selector==='textarea', closest:()=>null};
    const result = {elements:[field], dataset:autosave?{autosave:'ad-7-content'}:{}, isConnected:true,
        querySelector:()=>null, addEventListener(event, fn) {callbacks[event]=fn;},
        fire(event) {callbacks[event]?.({defaultPrevented:false});}, field};
    return result;
}
let env = environment(), content = form('content', 'Original', true), stage = form('stage','writing',false,false);
env.init(content);env.init(stage);
content.field.value = 'New draft, including final keystroke';content.fire('input');
stage.field.value = 'consultation';stage.fire('submit');
assert.equal(env.leave(), true, 'Saving stage must still warn about unsaved content');
env = environment();content = form('content','Original',true);env.init(content);
assert.equal(content.field.value, 'New draft, including final keystroke', 'Draft survives a stage-only reload');
assert.equal(env.leave(), true, 'Restored draft remains unsaved');
content.fire('submit');assert.equal(env.leave(),false, 'Submitting that form does not prompt about itself');
env = environment();content = form('content','Saved on server',true);env.init(content);
assert.equal(content.field.value,'Saved on server','Stale draft must not overwrite newer server content');
env = environment('2');content = form('content','Original',true);env.init(content);
assert.equal(content.field.value,'Original','Another user cannot restore this draft');
console.log('Unsaved forms: cross-form warning, immediate draft flush, restore and account isolation OK');
