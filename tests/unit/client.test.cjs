const {test}=require('node:test');
const assert=require('node:assert/strict');
test('SSE snapshots prevent duplicate delta and detect gaps',()=>{
  const core=require('../../client-core.js');
  const state={seq:1,content:'a',status:'running'};
  core.applyEvent(state,{seq:2,type:'delta',payload:{append:'b'}});
  core.applyEvent(state,{seq:2,type:'delta',payload:{append:'b'}});
  assert.equal(state.content,'ab');
  assert.throws(()=>core.applyEvent(state,{seq:4,type:'delta',payload:{append:'bad'}}),/gap/);
  core.applyEvent(state,{seq:3,type:'error',payload:{code:'OUTPUT_LIMIT',message:'incomplete',status:'failed'}});
  assert.equal(state.status,'failed');
  assert.equal(state.error.code,'OUTPUT_LIMIT');
});
test('IME enter and shift enter never submit',()=>{
  const {shouldSubmit}=require('../../client-core.js');
  assert.equal(shouldSubmit({key:'Enter',isComposing:true}),false);
  assert.equal(shouldSubmit({key:'Enter',shiftKey:true}),false);
  assert.equal(shouldSubmit({key:'Enter'},true),false);
  assert.equal(shouldSubmit({key:'Enter'}),true);
});
