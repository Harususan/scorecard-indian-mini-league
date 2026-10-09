const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const storageAPI = require('./storage.js');
const html = fs.readFileSync(__dirname+'/index.html','utf8');
const code = html.match(/<script src="storage.js"><\/script><script>([\s\S]*)<\/script>/)[1];
class Element {
  constructor() {this.children=[];this.events={};this.value='';this.scrollHeight=200;}
  append(...items) {this.children.push(...items);}
  replaceChildren() {this.children=[];}
  addEventListener(name,fn) {this.events[name]=fn;}
  querySelector() {return null;}
}
const values=new Map();
const localStorage={getItem:key=>values.get(key)??null,setItem:(key,value)=>values.set(key,value)};
function mount(namespace) {
  const listeners={},messages=[],nodes={status:new Element(),pickers:new Element()};
  const parent={postMessage:msg=>messages.push(msg)};
  const window={parent,localStorage,addEventListener:(name,fn)=>listeners[name]=fn};
  const document={body:new Element(),getElementById:name=>nodes[name],createElement:()=>new Element()};
  const context={window,document,CaptainStorage:storageAPI,ResizeObserver:class{observe(){}},console};
  vm.runInNewContext(code,context);
  function render(scope) {
    listeners.message({source:parent,data:{type:'streamlit:render',args:{namespace:scope,clubs:[{name:'Arsenal',managers:[{id:'101',manager:'One'},{id:'102',manager:'Two'}]}]}}});
  }
  render(namespace);
  return {nodes,messages,render,value:()=>messages.filter(m=>m.type==='streamlit:setComponentValue').at(-1).value};
}
const first=mount('2026:live:8');
assert.equal(first.messages[0].type,'streamlit:componentReady');
assert.equal(first.value().choices.Arsenal,null);
assert.equal(values.size,0); // Initial render never saves a default over existing data.
const select=first.nodes.pickers.children[0].children[1];
select.value='102';select.events.change();
assert.equal(first.value().choices.Arsenal,'102');
const refresh=mount('2026:live:8');
assert.equal(refresh.nodes.pickers.children[0].children[1].value,'102');
assert.equal(refresh.value().choices.Arsenal,'102');
const messageCount=refresh.messages.filter(m=>m.type==='streamlit:setComponentValue').length;
refresh.render('2026:live:8');
assert.equal(refresh.messages.filter(m=>m.type==='streamlit:setComponentValue').length,messageCount);
refresh.render('2026:live:9');assert.equal(refresh.value().choices.Arsenal,null);
refresh.render('2026:live:8');assert.equal(refresh.value().choices.Arsenal,'102');
values.set('iml:captains:v1:2026:live:8',JSON.stringify({Arsenal:'old-roster-id'}));
const stale=mount('2026:live:8');assert.equal(stale.value().choices.Arsenal,null);
assert.equal(JSON.parse(values.get('iml:captains:v1:2026:live:8')).Arsenal,'old-roster-id');
console.log('PASS: component handshake, picker edit, new browser-frame visit, no rerun loop, gameweek switching, stale-roster preservation.');
