const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const script=fs.readFileSync(path.join(__dirname,'../static/core/ui.js'),'utf8');
class Element {
 constructor(){this.children=[];this.attributes={};}
 append(child){this.children.push(child);}
 setAttribute(name,value){this.attributes[name]=value;}
}
for(const embedded of [true,false]){
 const callbacks=[],target=new Element();
 const fields=[{name:'filters_applied',value:'1',type:'hidden'},
 {name:'status',value:'new',type:'checkbox',labels:[{textContent:'Do recenzji'}]},
 {name:'status',value:'in_review',type:'checkbox',labels:[{textContent:'W recenzjach'}]},
 {name:'accepted',value:'0',type:'hidden'},
 {name:'accepted',value:'1',type:'checkbox',labels:[{textContent:'Przyjęci'}]}];
 const form={elements:fields,after(node){this.afterNode=node;},querySelector(selector){return selector==='[data-active-filters-target]' && embedded ? target : null;}};
 const main={querySelectorAll(selector){return selector==='form[data-filters]' ? [form] : [];},querySelector(){return null;}};
 const document={readyState:'loading',querySelector(){return main;},querySelectorAll(){return [];},addEventListener(event,fn){callbacks.push(fn);},createElement(){return new Element();}};
 class FormData {constructor(){return fields.map(f=>[f.name,f.value]);}}
 const context={document,window:{__fantazmatyUIInitialized:true},FormData,URLSearchParams,HTMLSelectElement:class{},location:{pathname:'/recenzje/'}};
 vm.runInNewContext(script,context);callbacks[0]();
 const chips=embedded ? target.children[0] : form.afterNode;
 assert(chips);assert.equal(chips.children.length,embedded?3:4);
 const first=chips.children[0];assert.equal(first.className,'filter-chip secondary-button');
 let params=new URL(first.href,'https://cms.test').searchParams;
 assert.deepEqual(params.getAll('status'),['in_review']);assert.equal(params.get('filters_applied'),'1');
 const accepted=chips.children[2];params=new URL(accepted.href,'https://cms.test').searchParams;
 assert.deepEqual(params.getAll('accepted'),['0']);
 assert.equal(accepted.attributes['aria-label'],'Usuń filtr: Przyjęci');
 if(!embedded)assert.equal(chips.children[3].className,'secondary-button');
}
console.log('OK: embedded filter buttons, separate filter buttons, clear URLs and accessibility labels.');
