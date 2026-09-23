const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const tick = () => new Promise(resolve => setImmediate(resolve));
const esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function node() { return {textContent:'',innerHTML:'',value:'',disabled:false,listeners:{},addEventListener(event,fn){this.listeners[event]=fn;}}; }

test('real-job UI separates notification acceptance from index status and escapes text', async () => {
  const nodes=Object.fromEntries(['real-job-form','real-job-rows','real-job-note','reset-job','refresh-jobs'].map(id=>[id,node()]));
  vm.runInNewContext(fs.readFileSync('static/js/real-jobs.js','utf8'), {
    document:{getElementById:id=>nodes[id]},setInterval:()=>0,
    App:{esc,api:async()=>({items:[{id:1,number:1,path:'/jobs/1',job:{title:'Engineer <script>bad</script>',company:'A & B'},status:'OPEN',notificationStatus:'ACCEPTED',indexStatus:'UNKNOWN',notificationResult:{message:'Received <img src=x>'}}]})},
  });
  await tick();
  const html=nodes['real-job-rows'].innerHTML;
  assert.match(html,/ACCEPTED/); assert.match(html,/UNKNOWN/);
  assert.match(html,/Engineer &lt;script&gt;bad&lt;\/script&gt;/);
  assert.ok(!html.includes('<img src=x>'));
  assert.match(nodes['real-job-note'].textContent,/not indexed/);
});

function googleHarness(fileText) {
  const ids=['google-note','google-key-file','google-origin','google-email','google-project','google-key-status','google-enabled','google-limits','google-key-form','google-approval','google-enable','google-pause','google-remove'];
  const nodes=Object.fromEntries(ids.map(id=>[id,node()]));
  const button=node();nodes['google-key-form'].querySelector=()=>button;
  nodes['google-key-file'].files=[{size:fileText.length,text:async()=>fileText}];
  nodes['google-key-file'].value='chosen.json';const calls=[], toasts=[];
  vm.runInNewContext(fs.readFileSync('static/js/google-indexing.js','utf8'), {
    document:{getElementById:id=>nodes[id]},
    App:{api:async(path,options)=>{calls.push({path,options});return {origin:'https://own.org',configured:false,enabled:false,dailyLimit:200,minuteLimit:10,note:'Accepted is not indexed.'};},toast:(...args)=>toasts.push(args)},
  });
  return {nodes,button,calls,toasts};
}

test('invalid JSON upload never echoes private key fragments and clears file input', async () => {
  const {nodes,button,calls,toasts}=googleHarness('{"private_key":"PRIVATE_SECRET_UNFINISHED');
  nodes['google-key-form'].listeners.submit({preventDefault(){},currentTarget:nodes['google-key-form']});
  await tick();await tick();
  assert.equal(calls.length,1);assert.equal(button.disabled,false);
  assert.equal(nodes['google-key-file'].value,'');
  assert.ok(!JSON.stringify(toasts).includes('PRIVATE_SECRET'));
  assert.match(nodes['google-note'].textContent,/not valid JSON/);
});

test('JSON is sent only to protected upload endpoint without display or browser storage', async () => {
  const {nodes,calls}=googleHarness('{"type":"service_account","private_key":"TEST_PRIVATE_KEY"}');
  nodes['google-key-form'].listeners.submit({preventDefault(){},currentTarget:nodes['google-key-form']});
  await tick();await tick();
  assert.equal(calls[1].path,'/api/google-indexing/credentials');
  assert.equal(calls[1].options.method,'POST');
  assert.equal(calls[1].options.body.private_key,'TEST_PRIVATE_KEY');
  assert.equal(nodes['google-key-file'].value,'');
  for (const n of Object.values(nodes)) assert.ok(!n.textContent.includes('TEST_PRIVATE_KEY'));
});

test('enablement requires explicit approved-usage confirmation', async () => {
  const {nodes,calls}=googleHarness('{}');
  await tick();
  nodes['google-approval'].checked=false;
  nodes['google-enable'].listeners.click({currentTarget:nodes['google-enable']});
  await tick();
  assert.equal(calls.length,1);
  assert.match(nodes['google-note'].textContent,/Confirm required API/);
});
