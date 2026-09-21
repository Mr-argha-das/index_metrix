const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('URL table shows the actual error, escaped, and links to diagnostics', async () => {
  const nodes = {
    'urls-tbody': {innerHTML: '', querySelectorAll: () => []},
    'urls-empty': {style:{}},
  };
  let start;
  const errors=[];
  const escape = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const context = {
    URLSearchParams, setInterval:()=>0,
    MutationObserver: class {observe(){}},
    document: {getElementById:id=>nodes[id], addEventListener:(_event, fn)=>{start=fn;}},
    App: {api: async()=>({items:[{id:7,status:'INVALID',error:'HTTP 403 <script>bad</script> & denied'}],total:1,page:1,per_page:25}),
          esc:escape,pill:escape,fmtBytes:()=>'',timeAgo:()=>'',toast:(...args)=>errors.push(args)},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('static/js/urls.js','utf8'),context);
  start();
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(errors,[]);
  const html=nodes['urls-tbody'].innerHTML;
  assert.match(html,/Failure reason:/);
  assert.match(html,/HTTP 403 &lt;script&gt;bad&lt;\/script&gt; &amp; denied/);
  assert.ok(!html.includes('<script>bad</script>'));
  assert.match(html,/href="\/pdfs\/7">View diagnostics/);
});
