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

test('dashboard Recent URLs shows the escaped failure reason and readable URL cells', async () => {
  const nodes = {'recent-tbody': {innerHTML:''}, 'recent-empty': {style:{}}};
  let start;
  const escape = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const context = {
    setInterval:()=>0,
    document: {getElementById:id=>nodes[id], addEventListener:(_event, fn)=>{start=fn;}},
    App: {api:async()=>({stats:{}, recent_pdfs:[{id:9,status:'INVALID',
      normalized_url:'https://www.egr.msu.edu/file.pdf',source_domain:'www.egr.msu.edu',
      error:'NAT64 destination <script>bad</script> & blocked'}]}),
      esc:escape,pill:escape,shortUrl:value=>value,timeAgo:()=>''},
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync('static/js/dashboard.js','utf8'),context);
  start();
  await new Promise(resolve=>setImmediate(resolve));
  const html=nodes['recent-tbody'].innerHTML;
  assert.match(html,/Failure reason:/);
  assert.match(html,/NAT64 destination &lt;script&gt;bad&lt;\/script&gt; &amp; blocked/);
  assert.ok(!html.includes('<script>bad</script>'));
  assert.match(html,/recent-source-url/);
  assert.match(html,/title="https:\/\/www.egr.msu.edu\/file.pdf"/);
  assert.match(html,/href="\/pdfs\/9">Details/);
});
