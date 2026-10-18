/* Durable conversation UI. All AI actions run on the server's Agents API session. */
(() => {
'use strict';
window.mountHomeChat = ({session, adapter}) => {
  const $ = id => document.getElementById(id);
  const {t,setText} = window.homeI18n;
  let enabled=false, sending=false, fetching=false, timer=null, lastMessages='', lastResults=null, pending=null, lastMemory='', lastSaved='', activeChat=null;
  const pendingKey='home_chat_submission';
  try { pending=JSON.parse(sessionStorage.getItem(pendingKey)||'null'); } catch {}
  const errors={
    missing_key:'AIを使うにはサーバーにOpenAIキーを設定してください。手動検索は利用できます。',
    provider_auth:'AIサービスの利用権限を確認してください。キーのAgents API権限が必要です。',
    provider_limit:'AIサービスの利用上限に達しました。時間をおいて再試行してください。',
    timeout:'応答に時間がかかっています。再試行すると、保存済みの処理を確認して再開します。',
    provider_connection:'AIサービスに接続できませんでした。再試行して処理の状態を確認できます。',
    turn_failed:'AIの処理に失敗しました。一部の変更は保存されている場合があります。現在の条件を確認して、会話を続けてください。',
    conversation_limit:'会話が長くなりました。「会話と記憶を削除」で新しい会話を始めてください。',
    busy:'別のメッセージを処理中です。完了をお待ちください。',
    cancelled:'処理を停止しました。完了済みの変更は保持されます。',
    resume_required:'前の処理が未完了です。再試行で再開するか、停止してから新しいメッセージを送ってください。',
    revision_conflict:'条件が変更されました。保存済みの状態を確認して、もう一度送信してください。',
    network:'送信を確認できませんでした。下書きを保持しています。再試行して確認できます。',
  };
  const progress={stopping:'処理を停止しています…',clearing:'会話と記憶を削除しています…',
    thinking:'考えています…',resolve_place:'場所を確認しています…',set_origin:'起点を設定しています…',
    update_search:'検索条件を更新しています…',search_listings:'実際の掲載ページを探しています…',
    classify_listing_page:'掲載ページの種類を分類しています…',
    compare_listings:'候補を比較しています…',save_listing:'候補を保存しています…',
    remember_preference:'希望を覚えています…',forget_place:'記憶を更新しています…',
    undo_search:'条件を元に戻しています…',get_search_state:'現在の条件を確認しています…',
    remove_saved_listing:'保存済み候補を更新しています…',
  };
  function error(code) {
    $('chatError').hidden=false;setText($('chatError'),()=>t(errors[code]||errors.turn_failed));
  }
  function rememberSubmission(value) {
    pending=value;
    try { if(value)sessionStorage.setItem(pendingKey,JSON.stringify(value));else sessionStorage.removeItem(pendingKey); } catch {}
  }
  async function api(path,payload) {
    const response=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload),signal:AbortSignal.timeout(35000)});
    const data=await response.json();
    if(!response.ok){if(data.session)session.accept(data.session);const e=new Error(data.error||'network');throw e;}
    return data;
  }
  async function settled() {
    const deadline=Date.now()+12000;
    while(session.saving||Object.keys(session.pending).length){
      await session.flush();
      if(['error','conflict','unavailable'].includes(session.status)||Date.now()>deadline)throw Error('revision_conflict');
      await new Promise(resolve=>setTimeout(resolve,70));
    }
    if(session.status!=='saved')throw Error('revision_conflict');
  }
  async function send(text, retry=false) {
    if(sending||!enabled||!session.snapshot)return;
    if(session.snapshot.chat?.status==='running'&&!retry)return;
    const message=(text??$('chatDraft').value).trim();
    if(!message&&!pending)return;
    sending=true;$('chatError').hidden=true;render();
    try {
      await settled();
      const payload=retry&&pending?pending:{request_id:crypto.randomUUID(),message,
        expected_revision:session.snapshot.revision,language:document.documentElement.lang==='en'?'en':'ja',...(adapter.visibleResults?.()||{})};
      rememberSubmission(payload);
      const data=await api('/api/chat',payload);
      rememberSubmission(null);session.accept(data);pollSoon(100);
    }catch(e){error(errors[e.message]?e.message:'network');}
    finally{sending=false;render();}
  }
  function renderText(root,text) {
    // Deliberately small Markdown subset built with DOM nodes; never inject model HTML.
    const pattern=/\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https:\/\/[^\s)]+)\)|(https:\/\/[^\s<>]+)/g;
    let index=0;
    for(const match of text.matchAll(pattern)) {
      root.append(document.createTextNode(text.slice(index,match.index)));
      if(match[1]||match[2]){const span=document.createElement(match[1]?'strong':'code');span.textContent=match[1]||match[2];root.append(span);}
      else {
        const address=match[4]||match[5];let allowed=false;
        try {const url=new URL(address);allowed=['suumo.jp','homes.co.jp','athome.co.jp','google.com','openstreetmap.org'].some(host=>url.hostname===host||url.hostname.endsWith('.'+host));}catch{}
        if(allowed){const a=document.createElement('a');a.href=address;a.textContent=match[3]||address;a.target='_blank';a.rel='noopener noreferrer';root.append(a);}
        else root.append(document.createTextNode(match[0]));
      }
      index=match.index+match[0].length;
    }
    root.append(document.createTextNode(text.slice(index)));
  }
  function renderMessages(s) {
    const signature=JSON.stringify(s.conversation||[]);
    if(signature===lastMessages)return;
    lastMessages=signature;
    const log=$('chatMessages'),nearBottom=log.scrollHeight-log.scrollTop-log.clientHeight<70;
    log.replaceChildren();
    for(const message of s.conversation||[]){
      const article=document.createElement('article');article.className='chat-message '+message.role;
      const label=document.createElement('span');label.className='message-author';label.textContent=t(message.role==='user'?'あなた':'住まいアシスタント');
      const content=document.createElement('div');content.className='message-content';renderText(content,message.text);
      article.append(label,content);log.append(article);
    }
    if(nearBottom)log.scrollTop=log.scrollHeight;
  }
  function actionButton(label,action){const b=document.createElement('button');b.type='button';b.className='btn ghost small';b.textContent=t(label);b.onclick=action;return b;}
  function renderMemory(s) {
    const signature=document.documentElement.lang+JSON.stringify(s.memory);if(signature===lastMemory)return;lastMemory=signature;
    const root=$('memoryItems');root.replaceChildren();
    const facts=Object.entries(s.memory?.facts||{}),places=Object.entries(s.memory?.places||{});
    if(!facts.length&&!places.length){const p=document.createElement('p');p.className='muted';p.textContent=t('まだ記憶はありません。希望や「職場」などの場所を会話で教えてください。');root.append(p);}
    for(const [name,value]of [...facts,...places.map(([alias,place])=>[alias,place.name])]){
      const p=document.createElement('p');p.textContent=name+': '+value;root.append(p);
    }
  }
  function renderSaved(s){
    const signature=document.documentElement.lang+JSON.stringify(s.saved_candidates);if(signature===lastSaved)return;lastSaved=signature;
    const root=$('agentSaved');root.replaceChildren();$('agentSavedSection').hidden=false;
    if(!s.saved_candidates?.length){const message=document.createElement('p');message.className='empty';setText(message,()=>t('会話で保存した掲載ページは、ここに表示されます。'));root.append(message);}
    for(const item of s.saved_candidates||[]){
      const card=document.createElement('article');card.className='external-item';
      const link=document.createElement('a');link.href=item.url;link.textContent=item.title;link.target='_blank';link.rel='noopener noreferrer';
      const note=document.createElement('p');note.textContent=t('掲載情報・空室は未確認です。掲載元で確認してください。');
      const remove=actionButton('保存から削除',()=>send('Remove saved candidate '+item.id+' ('+item.title+').'));
      card.append(link,note,remove);root.append(card);
    }
  }
  function render(){
    const s=session.snapshot;
    if(!s)return;
    if(activeChat!==s.chat_id){activeChat=s.chat_id;lastMessages='';lastResults=undefined;lastMemory='';lastSaved='';$('chatError').hidden=true;}
    const recovery=s.chat?.status==='error'&&s.chat.recoverable;
    const running=['running','cancelling','resetting'].includes(s.chat?.status),busy=running||sending;
    $('chatSend').disabled=!enabled||busy||recovery||session.status!=='saved'||!$('chatDraft').value.trim();
    $('chatSend').setAttribute('aria-busy',String(busy));
    $('chatDraft').disabled=busy||session.status==='loading';
    $('chatStop').hidden=(s.chat?.status!=='running'&&!recovery)||s.chat?.operation==='reset';
    for(const id of ['chatClear','chatUndo'])$(id).disabled=busy||!enabled;
    setText($('chatAvailability'),()=>t(enabled?'下書きと会話は自動保存されます。':'チャットは未接続です。条件を編集して検索できます。'));
    $('chatUndo').hidden=!s.history?.length||!enabled;
    $('chatProgress').hidden=!running;
    if(running)setText($('chatProgress'),()=>t(progress[s.chat.progress]||progress.thinking));
    if(s.chat?.status==='error')error(s.chat.error);
    if(s.chat?.status==='cancelled')error('cancelled');
    if(running)$('chatError').hidden=true;
    $('chatRetry').hidden=!(pending||s.chat?.status==='error'&&s.chat.recoverable);
    renderMessages(s);renderMemory(s);renderSaved(s);
    const latest=s.result_sets?.at(-1);
    const isCurrent=latest&&JSON.stringify(latest.criteria)===JSON.stringify(session.criteria);
    const token=isCurrent?JSON.stringify(latest):null;
    if(token!==lastResults){adapter.applyAgentResults(isCurrent?latest:null);lastResults=token;}
    if(!latest)setText($('listingsStatus'),()=>t('掲載ページを検索できます。'));
    if(latest){
      setText($('listingsStatus'),()=>isCurrent?t('確認済み条件一致：{0}件・未確認の候補：{1}件',[latest.counts?.confirmed||0,latest.counts?.leads||0]):t('条件が変わりました。現在の条件で再検索してください。'));
    }
    if(pending&&s.conversation?.some(m=>m.id===pending.request_id))rememberSubmission(null);
  }
  function pollSoon(delay=1100){clearTimeout(timer);timer=setTimeout(poll,delay);}
  async function poll(){
    if(fetching)return;
    fetching=true;
    try {const data=await session.request();session.accept(data);}
    catch {if(session.snapshot?.chat?.status==='running')error('network');}
    finally {fetching=false;pollSoon(session.snapshot?.chat?.status==='running'?900:4000);}
  }
  async function clear(){
    if(!confirm(t('会話と記憶を削除しますか？検索条件と保存した候補は残ります。')))return;
    try{await settled();const data=await api('/api/chat/reset',{expected_revision:session.snapshot.revision,reset_search:false});rememberSubmission(null);lastMessages='';session.accept(data);$('chatError').hidden=true;}
    catch(e){error(e.message);}
  }
  $('chatSend').onclick=()=>send();
  $('chatDraft').addEventListener('input',render);
  $('chatDraft').addEventListener('keydown',event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();send();}});
  $('chatStop').onclick=async()=>{try{session.accept(await api('/api/chat/cancel',{request_id:session.snapshot.chat.request_id}));}catch(e){error(e.message);}};
  $('chatClear').onclick=()=>clear();
  $('chatUndo').onclick=()=>send(document.documentElement.lang==='ja'?'直前の検索条件の変更を元に戻してください。':'Undo the last search criteria change.');
  $('chatRetry').onclick=async()=>{
    if(pending)return send(pending.message,true);
    if(session.snapshot.chat.operation==='reset'){try{session.accept(await api('/api/chat/reset',{expected_revision:session.snapshot.revision,reset_search:!!session.snapshot.chat.reset_search}));pollSoon(100);}catch(e){error(e.message);}return;}
    try{session.accept(await api('/api/chat/resume',{request_id:session.snapshot.chat.request_id}));pollSoon(100);}catch(e){error(e.message);}
  };
  const legacySearch=$('searchListings').onclick;
  $('searchListings').onclick=()=>{window.searchShell?.openResults();return enabled?send(document.documentElement.lang==='ja'?'現在の条件で掲載物件を検索してください。':'Search property listing pages using my current criteria.'):legacySearch();};
  session.addEventListener('change',render);
  window.addEventListener('home-language-change',()=>{lastMessages='';render();});
  fetch('/api/health').then(r=>r.json()).then(health=>{enabled=!!health.agent_enabled;render();}).catch(()=>error('network'));
  window.homeChat={send};pollSoon();
};
})();
