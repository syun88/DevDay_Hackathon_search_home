/* Durable conversation UI. All AI actions run on the server's Agents API session. */
(() => {
'use strict';
window.mountHomeChat = ({session, adapter}) => {
  const $ = id => document.getElementById(id);
  const {t,setText} = window.homeI18n;
  let enabled=false, sending=false, fetching=false, timer=null, lastMessages='', lastResults=null, pending=null, lastMemory='', lastSaved='', activeChat=null;
  let refocus=false, live=null, liveNode=null, liveFrame=0, source=null, streamOpen=false, streamRetry=null, composing=false, lastSuggestions='';
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
    daily_limit:'本日のAI利用上限に達しました。明日もう一度お試しください。手動検索と地図は引き続き使えます。',
    cancel_failed:'停止を確認できませんでした。再試行すると停止をやり直します（処理は再開しません）。',
  };
  const progress={stopping:'処理を停止しています…',clearing:'会話と記憶を削除しています…',
    get_map_state:'地図を確認しています…',control_map:'地図を操作しています…',get_listing_details:'物件の詳細を確認しています…',search_public_housing:'公開データを地図に読み込んでいます…',thinking:'考えています…',writing:'回答を書いています…',resolve_place:'場所を確認しています…',set_origin:'起点を設定しています…',
    update_search:'検索条件を更新しています…',search_listings:'実際の掲載ページを探しています…',web_search:'ウェブを検索しています…',get_commute:'道路の所要時間を推定しています…',
    compare_listings:'候補を比較しています…',save_listing:'候補を保存しています…',
    remember_preference:'希望を覚えています…',forget_place:'記憶を更新しています…',
    undo_search:'条件を元に戻しています…',get_search_state:'現在の条件を確認しています…',
    remove_saved_listing:'保存済み候補を更新しています…',
  };
  // Completed steps are described by what actually happened, never by what was requested.
  const steps={get_map_state:'地図の状態を確認',control_map:'地図を操作',get_listing_details:'物件の詳細を確認',search_public_housing:'公開データを読み込み',
    get_search_state:'検索条件を確認',update_search:'検索条件を更新',resolve_place:'場所を検索',set_origin:'起点を設定',remember_preference:'希望を記憶',
    forget_place:'記憶を更新',search_listings:'掲載ページを検索',get_commute:'道路の所要時間を推定',compare_listings:'候補を比較',save_listing:'候補を保存',
    remove_saved_listing:'保存済みを更新',undo_search:'条件を元に戻す',web_search:'ウェブを検索'};
  const outcomes={running:'実行中',done:'完了',error:'失敗',applied:'地図に反映済み',pending:'地図の反映待ち',stale_view:'手動の地図操作を優先',unavailable:'地図に表示できません',empty:'結果なし'};
  const mapTools=new Set(['control_map','search_public_housing','search_listings','web_search']);
  const suggestions=['千代田区の都営住宅を地図に表示して','東京駅から3km以内で1LDK、月15万円以内の賃貸を探して','選択中の物件の分かっている条件と不明点を教えて'];
  const mapPill=document.createElement('div');mapPill.className='map-agent-activity';mapPill.hidden=true;mapPill.setAttribute('aria-hidden','true');
  $('map')?.parentElement?.append(mapPill);
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
      await window.homeMapAgent?.flush();
      const payload=retry&&pending?pending:{request_id:crypto.randomUUID(),message,
        expected_revision:session.snapshot.revision,language:document.documentElement.lang==='en'?'en':'ja',...(adapter.visibleResults?.()||{})};
      rememberSubmission(payload);
      const data=await api('/api/chat',payload);
      rememberSubmission(null);
      session.accept(data);grow();connectStream();pollSoon(100);
    }catch(e){error(errors[e.message]?e.message:'network');}
    finally{sending=false;render();}
  }
  function inline(root,text) {
    const pattern=/\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https:\/\/[^\s)]+)\)|(https:\/\/[^\s<>)]+)/g;
    let index=0;
    for(const match of text.matchAll(pattern)) {
      root.append(document.createTextNode(text.slice(index,match.index)));
      if(match[1]||match[2]){const span=document.createElement(match[1]?'strong':'code');span.textContent=match[1]||match[2];root.append(span);}
      else {
        const address=match[4]||match[5];let allowed=false;
        try {const url=new URL(address);const known=knownUrls();allowed=known.includes(address)||['suumo.jp','homes.co.jp','athome.co.jp','google.com','openstreetmap.org'].some(host=>url.hostname===host||url.hostname.endsWith('.'+host));}catch{}
        if(allowed){const a=document.createElement('a');a.href=address;a.textContent=match[3]||address;a.target='_blank';a.rel='noopener noreferrer';root.append(a);}
        else root.append(document.createTextNode(match[0]));
      }
      index=match.index+match[0].length;
    }
    root.append(document.createTextNode(text.slice(index)));
  }
  // Links are clickable only when they came from official records or actual tool results.
  function knownUrls(){
    const s=session.snapshot||{};
    return [...(s.public_records||[]).flatMap(i=>[i.official_url,i.source_url,i.data_url,i.license_url]),
      ...(s.result_sets||[]).flatMap(r=>[...(r.items||[]).map(i=>i.url),...(r.discovery_links||[]).map(l=>l.url),...(r.source_search_links||[]).map(l=>l.url)]),
      ...(s.saved_candidates||[]).map(i=>i.url)].filter(Boolean).flatMap(u=>{try{return [u,decodeURI(u),encodeURI(decodeURI(u))];}catch{return [u];}});
  }
  function renderText(root,text) {
    // A small Markdown subset built with DOM nodes; model HTML is never injected.
    root.replaceChildren();
    const lines=text.replace(/\r/g,'').split('\n');let list=null,paragraph=null;
    const cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(cell=>cell.trim());
    for(let i=0;i<lines.length;i++){
      const line=lines[i],item=line.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/),heading=line.match(/^\s*#{1,4}\s+(.*)$/);
      if(/^\s*\|.*\|\s*$/.test(line)&&/^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i+1]||'')&&lines[i+1].includes('-')){
        list=paragraph=null;const wrap=document.createElement('div');wrap.className='message-table';const table=document.createElement('table');
        const head=table.createTHead().insertRow();for(const cell of cells(line)){const th=document.createElement('th');inline(th,cell);head.append(th);}
        const body=table.createTBody();i+=2;
        for(;i<lines.length&&/^\s*\|.*\|\s*$/.test(lines[i]);i++){const row=body.insertRow();for(const cell of cells(lines[i]))inline(row.insertCell(),cell);}
        i--;wrap.append(table);root.append(wrap);continue;
      }
      if(item){
        const kind=item[1]?'OL':'UL';
        if(!list||list.tagName!==kind){list=document.createElement(kind.toLowerCase());root.append(list);}
        const li=document.createElement('li');inline(li,item[2]);list.append(li);paragraph=null;continue;
      }
      list=null;
      if(!line.trim()){paragraph=null;continue;}
      if(heading){paragraph=null;const h=document.createElement('p');h.className='message-heading';inline(h,heading[1]);root.append(h);continue;}
      if(paragraph)paragraph.append(document.createElement('br'));else{paragraph=document.createElement('p');root.append(paragraph);}
      inline(paragraph,line);
    }
  }
  function stepLabel(step){return t(steps[step.name]||'ツールを実行');}
  function renderSteps(activity,running){
    const list=document.createElement('ol');list.className='agent-steps';
    for(const step of activity||[]){
      const li=document.createElement('li');li.className='agent-step';li.dataset.status=step.status;
      const dot=document.createElement('span');dot.className='step-dot';dot.setAttribute('aria-hidden','true');
      const label=document.createElement('span');label.textContent=stepLabel(step);
      const state=document.createElement('span');state.className='step-state';
      state.textContent=t(step.status==='running'&&!running?'中断':outcomes[step.status]||'完了');
      li.append(dot,label,state);list.append(li);
    }
    return list;
  }
  function messageNode(message) {
    const article=document.createElement('article');article.className='chat-message '+message.role+(message.incomplete?' incomplete':'');
    const label=document.createElement('span');label.className='message-author';label.textContent=t(message.role==='user'?'あなた':'住まいアシスタント');
    article.append(label);
    if(message.activity?.length){
      const details=document.createElement('details');details.className='step-summary';
      const summary=document.createElement('summary');summary.textContent=t('{0}件の操作',[message.activity.length]);
      details.append(summary,renderSteps(message.activity,false));article.append(details);
    }
    const content=document.createElement('div');content.className='message-content';renderText(content,message.text);article.append(content);
    if(message.incomplete){const note=document.createElement('p');note.className='incomplete-note';note.textContent=t(message.incomplete==='cancelled'?'停止したため、この回答は途中までです。':'処理に失敗したため、この回答は途中までです。');article.append(note);}
    return article;
  }
  function nearBottom(){const log=$('chatMessages');return log.scrollHeight-log.scrollTop-log.clientHeight<90;}
  function stick(was){if(was){const log=$('chatMessages');log.scrollTop=log.scrollHeight;}}
  function renderMessages(s) {
    const signature=JSON.stringify(s.conversation||[]);
    if(signature===lastMessages)return;
    lastMessages=signature;
    const was=nearBottom()||!$('chatMessages').childElementCount;
    $('chatMessages').replaceChildren(...(s.conversation||[]).map(messageNode));
    liveNode=null;renderLive();stick(was);
  }
  // Pending draft saves are flushed by send(), so typing then pressing Enter is never dropped.
  function canSend(){const s=session.snapshot;return !!s&&enabled&&!sending&&!chatRunning(s)&&!(s.chat?.status==='error'&&s.chat.recoverable)&&!['loading','unavailable','conflict'].includes(session.status)&&!!$('chatDraft').value.trim();}
  function chatRunning(s=session.snapshot){return ['running','cancelling','resetting'].includes(s?.chat?.status);}
  function currentLive(){
    const s=session.snapshot;if(!s)return null;
    const value=live&&live.chat_id===s.chat_id&&live.request_id===s.chat?.request_id?live:
      s.chat?.partial&&s.chat.request_id?{request_id:s.chat.request_id,chat_id:s.chat_id,...s.chat.partial}:null;
    if(!value)return null;
    // A saved reply or incomplete message replaces the streamed preview.
    if((s.conversation||[]).some(m=>m.id==='reply_'+value.request_id||m.id==='partial_'+value.request_id))return null;
    if(!chatRunning(s)&&s.chat?.status!=='error')return null;
    return value;
  }
  function phase(value,s=session.snapshot){
    const status=s?.chat?.status;
    if(status==='cancelling')return 'stopping';if(status==='resetting')return 'clearing';
    const step=[...(value?.activity||[])].reverse().find(a=>a.status==='running');
    if(step)return step.name;
    if(s?.map_command?.status==='pending'&&status==='running')return 'control_map';
    return value?.text?'writing':'thinking';
  }
  function renderLive(){
    cancelAnimationFrame(liveFrame);
    liveFrame=requestAnimationFrame(()=>{
      const s=session.snapshot,value=currentLive(),running=chatRunning(s),log=$('chatMessages');
      if(!value&&!running){liveNode?.remove();liveNode=null;log.removeAttribute('aria-busy');return;}
      const was=nearBottom();
      if(!liveNode||!liveNode.isConnected){liveNode=document.createElement('article');liveNode.className='chat-message assistant live';log.append(liveNode);}
      const interrupted=s?.chat?.status==='error';
      liveNode.classList.toggle('streaming',running);liveNode.classList.toggle('incomplete',interrupted);
      const label=document.createElement('span');label.className='message-author';label.textContent=t('住まいアシスタント');
      const parts=[label];
      if(value?.activity?.length)parts.push(renderSteps(value.activity,running));
      if(value?.note&&running&&!value.text){const note=document.createElement('p');note.className='agent-note';note.textContent=value.note;parts.push(note);}
      const content=document.createElement('div');content.className='message-content';
      if(value?.text){renderText(content,value.text);if(running){const caret=document.createElement('span');caret.className='stream-caret';caret.setAttribute('aria-hidden','true');(content.lastElementChild||content).append(caret);}}
      else if(running){const typing=document.createElement('span');typing.className='typing-dots';typing.setAttribute('aria-hidden','true');typing.append(document.createElement('i'),document.createElement('i'),document.createElement('i'));content.append(typing);}
      parts.push(content);
      if(interrupted&&value?.text){const note=document.createElement('p');note.className='incomplete-note';note.textContent=t('接続が中断されたため、この回答は途中までです。再試行で続きを確認できます。');parts.push(note);}
      liveNode.replaceChildren(...parts);
      // Assistive technology hears the completed reply, not every streamed fragment.
      if(running)log.setAttribute('aria-busy','true');else log.removeAttribute('aria-busy');
      stick(was);
    });
  }
  function renderActivity(s){
    const running=chatRunning(s),value=currentLive(),current=running?phase(value,s):null;
    const pill=$('chatActivity');pill.hidden=!running;pill.dataset.phase=current||'';
    if(running)setText($('chatActivityText'),()=>t(progress[current]||progress.thinking));
    $('chatProgress').hidden=true;
    const mapBusy=running&&(mapTools.has(current)||s.map_command?.status==='pending');
    mapPill.hidden=!mapBusy;
    if(mapBusy){mapPill.replaceChildren(Object.assign(document.createElement('span'),{className:'pulse-dot'}),document.createTextNode(t(current==='control_map'||s.map_command?.status==='pending'?'地図を更新しています…':progress[current])));}
    document.querySelector('.shell-dock')?.classList.toggle('agent-busy',running);
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
    const root=$('agentSaved');root.replaceChildren();$('agentSavedSection').hidden=!s.saved_candidates?.length;
    for(const item of s.saved_candidates||[]){
      const card=document.createElement('article');card.className='external-item';
      const link=document.createElement('a');link.href=item.url;link.textContent=item.title;link.target='_blank';link.rel='noopener noreferrer';
      const note=document.createElement('p');note.textContent=t('掲載情報・空室は未確認です。掲載元で確認してください。');
      const remove=actionButton('保存から削除',()=>send('Remove saved candidate '+item.id+' ('+item.title+').'));
      card.append(link,note,remove);root.append(card);
    }
  }
  function renderSuggestions(s,busy){
    const root=$('chatSuggestions');const show=enabled&&!s.conversation?.length&&!busy;root.hidden=!show;
    const signature=document.documentElement.lang+show;if(signature===lastSuggestions)return;lastSuggestions=signature;
    root.replaceChildren(...suggestions.map(text=>{const b=document.createElement('button');b.type='button';b.className='suggestion';b.textContent=t(text);
      b.onclick=()=>{const area=$('chatDraft');area.value=t(text);area.dispatchEvent(new Event('input'));area.focus();};return b;}));
  }
  function render(){
    const s=session.snapshot;
    if(!s)return;
    if(activeChat!==s.chat_id){activeChat=s.chat_id;lastMessages='';lastResults=undefined;lastMemory='';lastSaved='';$('chatError').hidden=true;}
    const recovery=s.chat?.status==='error'&&s.chat.recoverable;
    const running=chatRunning(s),busy=running||sending;
    $('chatSend').disabled=!canSend();
    $('chatSend').setAttribute('aria-busy',String(busy));
    const draftArea=$('chatDraft');
    if(busy&&document.activeElement===draftArea)refocus=true;
    draftArea.disabled=busy||session.status==='loading';
    if(!busy&&refocus&&!draftArea.disabled){refocus=false;requestAnimationFrame(()=>{if(document.activeElement===document.body)draftArea.focus();});}
    grow();
    $('chatStop').hidden=(s.chat?.status!=='running'&&!recovery)||s.chat?.operation==='reset';
    for(const id of ['chatClear','chatNew','chatUndo'])$(id).disabled=busy||!enabled;
    $('chatUndo').hidden=!s.history?.length;
    if(s.chat?.status==='error')error(s.chat.operation==='cancel'?'cancel_failed':s.chat.error);
    if(s.chat?.status==='cancelled')error('cancelled');
    if(running)$('chatError').hidden=true;
    $('chatRetry').hidden=!(pending||s.chat?.status==='error'&&s.chat.recoverable);
    renderMessages(s);renderLive();renderActivity(s);renderMemory(s);renderSaved(s);renderSuggestions(s,busy);
    const latest=s.result_sets?.at(-1);
    const isCurrent=latest&&JSON.stringify(latest.criteria)===JSON.stringify(session.criteria);
    const token=isCurrent?JSON.stringify(latest):null;
    if(token!==lastResults){adapter.applyAgentResults(isCurrent?latest:null);lastResults=token;}
    if(!latest)setText($('listingsStatus'),()=>t('掲載ページを検索できます。'));
    if(latest){
      setText($('listingsStatus'),()=>isCurrent?t('条件に合う物件：{0}件',[latest.counts?.recommended??latest.counts?.confirmed??0]):t('条件が変わりました。現在の条件で再検索してください。'));
    }
    if(pending&&s.conversation?.some(m=>m.id===pending.request_id))rememberSubmission(null);
    if(running)connectStream();
  }
  function pollSoon(delay=1100){clearTimeout(timer);timer=setTimeout(poll,delay);}
  async function poll(){
    if(fetching)return;
    fetching=true;
    try {const data=await session.request();session.accept(data);}
    catch {if(session.snapshot?.chat?.status==='running'&&!streamOpen)error('network');}
    finally {fetching=false;const running=chatRunning();pollSoon(running?(streamOpen?2500:900):(streamOpen?15000:4000));}
  }
  // Server-sent events carry real streamed text, tool activity and saved-state changes.
  function connectStream(){
    if(source||!enabled||typeof EventSource!=='function')return;
    clearTimeout(streamRetry);
    source=new EventSource('/api/chat/stream');
    source.onopen=()=>{streamOpen=true;};
    source.addEventListener('state',event=>{
      let data;try{data=JSON.parse(event.data);}catch{return;}
      const s=session.snapshot;
      if(!s||data.chat_id!==s.chat_id||data.revision!==s.revision||data.status!==s.chat?.status||data.request_id!==s.chat?.request_id||data.progress!==s.chat?.progress||data.map_revision!==(s.map_view?.revision||0))pollSoon(0);
    });
    source.addEventListener('live',event=>{
      let data;try{data=JSON.parse(event.data);}catch{return;}
      if(!live||live.request_id!==data.request_id||live.chat_id!==data.chat_id)live={request_id:data.request_id,chat_id:data.chat_id,text:'',activity:[],note:''};
      if(typeof data.text==='string')live.text=data.text;
      else if(typeof data.delta==='string'){
        // Offsets make reconnects and duplicate frames safe; a gap forces a fresh snapshot.
        if(data.offset===live.text.length)live.text+=data.delta;
        else if(data.offset+data.delta.length>live.text.length){source?.close();source=null;streamOpen=false;connectStream();return;}
      }
      if(Array.isArray(data.activity))live.activity=data.activity;
      if(typeof data.note==='string')live.note=data.note;
      renderLive();renderActivity(session.snapshot);
    });
    source.onerror=()=>{
      streamOpen=false;
      if(source?.readyState===EventSource.CLOSED){source=null;streamRetry=setTimeout(connectStream,2000);}
    };
  }
  async function clear(resetSearch){
    if(!confirm(t(resetSearch?'新しい検索を始めますか？会話と記憶を削除します。保存した候補は残ります。':'会話と記憶を削除しますか？検索条件と保存した候補は残ります。')))return;
    try{await settled();const data=await api('/api/chat/reset',{expected_revision:session.snapshot.revision,reset_search:resetSearch});rememberSubmission(null);lastMessages='';live=null;session.accept(data);$('chatError').hidden=true;}
    catch(e){error(e.message);}
  }
  function grow(){const area=$('chatDraft');area.style.height='auto';area.style.height=Math.min(area.scrollHeight,160)+'px';}
  $('chatSend').onclick=()=>send();
  const draft=$('chatDraft');
  draft.addEventListener('compositionstart',()=>{composing=true;});
  draft.addEventListener('compositionend',()=>{setTimeout(()=>{composing=false;},0);});
  draft.addEventListener('keydown',event=>{
    if(event.key!=='Enter')return;
    // Enter confirms Japanese IME conversion; it must never submit mid-composition.
    if(event.isComposing||composing||event.keyCode===229)return;
    if(event.shiftKey||event.altKey)return;
    event.preventDefault();
    if(canSend())send();
  });
  draft.addEventListener('input',()=>{grow();$('chatSend').disabled=!canSend();});
  $('chatStop').onclick=async()=>{try{session.accept(await api('/api/chat/cancel',{request_id:session.snapshot.chat.request_id}));}catch(e){error(e.message);}};
  $('chatClear').onclick=()=>clear(false);$('chatNew').onclick=()=>clear(true);
  $('chatUndo').onclick=()=>send(document.documentElement.lang==='ja'?'直前の検索条件の変更を元に戻してください。':'Undo the last search criteria change.');
  $('chatRetry').onclick=async()=>{
    if(pending)return send(pending.message,true);
    // A failed Stop is retried as a Stop; it never resumes the turn the user cancelled.
    if(session.snapshot.chat.operation==='cancel'){try{session.accept(await api('/api/chat/cancel',{request_id:session.snapshot.chat.request_id}));pollSoon(100);}catch(e){error(e.message);}return;}
    if(session.snapshot.chat.operation==='reset'){try{session.accept(await api('/api/chat/reset',{expected_revision:session.snapshot.revision,reset_search:!!session.snapshot.chat.reset_search}));pollSoon(100);}catch(e){error(e.message);}return;}
    try{session.accept(await api('/api/chat/resume',{request_id:session.snapshot.chat.request_id}));connectStream();pollSoon(100);}catch(e){error(e.message);}
  };
  const legacySearch=$('searchListings').onclick;
  $('searchListings').onclick=()=>enabled?send(document.documentElement.lang==='ja'?'現在の条件で掲載物件を検索してください。':'Search property listing pages using my current criteria.'):legacySearch();
  session.addEventListener('change',render);
  window.addEventListener('home-language-change',()=>{lastMessages='';lastSuggestions='';lastResults=undefined;render();});
  fetch('/api/health').then(r=>r.json()).then(health=>{enabled=!!health.agent_enabled;if(!enabled)error('missing_key');else connectStream();render();}).catch(()=>error('network'));
  window.homeChat={send};pollSoon();
};
})();
