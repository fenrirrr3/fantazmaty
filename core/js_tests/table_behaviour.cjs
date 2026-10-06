const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assets = path.resolve(__dirname, '../static/core');
class Element {
    constructor(text = '') { this.textContent=text; this.dataset={}; this.children=[]; this.attrs={}; this.events={}; this.colSpan=1; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children=items; }
    addEventListener(name, fn) { this.events[name]=fn; }
    setAttribute(name, value) { this.attrs[name]=value; }
    getAttribute(name) { return this.attrs[name]; }
    removeAttribute(name) { delete this.attrs[name]; }
    querySelector(selector) { if (selector==='.sort-indicator') return this.children[0]?.children[0]; return null; }
}
function sortFixture(values, server = false, key = 'title') {
    const th = new Element('Tytuł');
    const body = {rows: values.map(value => ({cells:[new Element(value)]})), append(row) { this.rows=this.rows.filter(r=>r!==row); this.rows.push(row); }};
    const table = new Element(); table.dataset.serverPaginated=String(server);table.tHead={rows:[{cells:[th]}]};table.tBodies=[body];table.compareDocumentPosition=()=>4;
    const config={dataset:{pageParam:'archive_page'},previousElementSibling:{textContent:JSON.stringify({'Tytuł':key})}};
    const callbacks=[];const assigned=[];
    const source=fs.readFileSync(path.join(assets,'ui.js'),'utf8').replace(/\r\n/g, '\n');
    const sortSource=source.slice(source.indexOf('// Paginated lists sort'),source.indexOf("document.addEventListener('DOMContentLoaded', () => {\n    document.querySelectorAll('[data-dashboard-more]"));
    vm.runInNewContext(sortSource,{
        document:{addEventListener:(event,fn)=>callbacks.push(fn),querySelectorAll:selector=>selector==='main table'?[table]:[config],createElement:()=>new Element()},
        Node:{DOCUMENT_POSITION_FOLLOWING:4},Intl,URL,location:{href:'https://cms.invalid/list?tag=smoki&archive_page=3&page_size=25',assign:url=>assigned.push(url)},
    });
    callbacks.forEach(fn=>fn());
    return {table, th, body, click:()=>th.children[0].events.click(), assigned};
}
let fixture=sortFixture(['Żar','Łódź','Las','','Brak danych']);
const detail={cells:[Object.assign(new Element('Szczegóły Żaru'),{colSpan:2})]};
fixture.body.rows.splice(1,0,detail);
fixture.click();assert.deepEqual(fixture.body.rows.map(r=>r.cells[0].textContent),['Las','Łódź','Żar','Szczegóły Żaru','','Brak danych']);
fixture.click();assert.deepEqual(fixture.body.rows.map(r=>r.cells[0].textContent),['Żar','Szczegóły Żaru','Łódź','Las','','Brak danych']);
fixture=sortFixture(['10','2','100','']);fixture.click();assert.deepEqual(fixture.body.rows.map(r=>r.cells[0].textContent),['2','10','100','']);
fixture=sortFixture(['02.01.2025','10.12.2024']);fixture.click();assert.equal(fixture.body.rows[0].cells[0].textContent,'10.12.2024');
fixture=sortFixture(['B','A'],true);fixture.click();let url=new URL(fixture.assigned[0]);assert.equal(url.searchParams.get('sort'),'title');assert.equal(url.searchParams.has('archive_page'),false);assert.equal(url.searchParams.get('tag'),'smoki');

function selectionTable() {
    const table = new Element();table.id='chapters';
    table.boxes=Array.from({length:7},(_,i)=>({name:'chapters',checked:false,disabled:i===2,hidden:i===3,column:0,changed:0,
        matches:selector=>selector==='tbody input[type="checkbox"]',
        closest(selector) { return selector==='td, th'?{cellIndex:this.column}:this.hidden?{}:null; },
        getClientRects() { return this.hidden?[]:[{}]; }, dispatchEvent(event) { if(event.type==='change')this.changed++; },
    }));
    table.querySelectorAll=()=>table.boxes;return table;
}
const first=selectionTable(),second=selectionTable(), callbacks=[];
vm.runInNewContext(fs.readFileSync(path.join(assets,'table-selection.js'),'utf8'),{
    document:{addEventListener:(event,fn)=>callbacks.push(fn),querySelectorAll:()=>[first,second]},
    Event:class {constructor(type,opts){this.type=type;Object.assign(this,opts);}},
});callbacks.forEach(fn=>fn());
function click(table, index, checked, shiftKey=false) { const box=table.boxes[index];box.checked=checked;table.events.click({target:box,shiftKey}); }
click(first,0,true);click(first,5,true,true);
assert.deepEqual(first.boxes.map(box=>box.checked),[true,true,false,false,true,true,false]);
assert.equal(first.boxes[1].changed,1);
assert.equal(second.boxes.some(box=>box.checked),false);
click(first,4,false);click(first,0,false,true);
assert.equal(first.boxes.slice(0,5).some(box=>box.checked),false);
// Sorting changes row order; the anchor remains the same actual record.
click(first,5,true);first.boxes.reverse();click(first,5,true,true);
assert.equal(first.boxes[2].checked,true);
// A different checkbox column does not change the original column.
first.boxes[0].checked=false;first.boxes[6].column=1;click(first,6,true,true);assert.equal(first.boxes[0].checked,false);
console.log('PASS: Polish/numeric/date/empty sorting, detail rows, server query preservation, Shift ranges, hidden/disabled rows, reversed order, independent tables and columns.');
