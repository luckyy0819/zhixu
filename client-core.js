(function(root){
'use strict';
function applyEvent(state,event){
  if(event.seq<=state.seq)return false;
  if(event.seq!==state.seq+1)throw new Error('event gap');
  state.seq=event.seq;
  if(event.type==='delta'){state.content+=event.payload.append;if(event.payload.claim)(state.claims||(state.claims=[])).push(event.payload.claim);}
  if(event.type==='citation'){const list=state.citations||(state.citations=[]);if(!list.some(c=>c.id===event.payload.id))list.push(event.payload);}
  if(event.type==='status')state.status=event.payload.status;
  if(event.type==='done')state.status='completed';
  if(event.type==='cancelled')state.status='cancelled';
  if(event.type==='error'){state.status=event.payload.status;state.error=event.payload;}
  return true;
}
function shouldSubmit(event,composing=false){return event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&!composing;}
const api={applyEvent,shouldSubmit};
if(typeof module!=='undefined')module.exports=api;else root.ZhixuCore=api;
})(typeof window!=='undefined'?window:globalThis);
