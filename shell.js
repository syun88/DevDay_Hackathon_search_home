/* Layout from the user's wireframe: original map above chat, collapsible history rail. */
(() => {
'use strict';
const drawings={
  panel:'<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9 4v16"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  chat:'<path d="M5 18l-2 3V6a3 3 0 0 1 3-3h12a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H8z"/><path d="M7 8h10M7 12h6"/>',
  settings:'<path d="M4 6h16M4 12h16M4 18h16"/><circle cx="9" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="8" cy="18" r="2"/>',
  results:'<rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  memory:'<path d="M12 4a4 4 0 0 0-7 3 4 4 0 0 0-1 7 4 4 0 0 0 8 5V4zm0 0a4 4 0 0 1 7 3 4 4 0 0 1 1 7 4 4 0 0 1-8 5"/>',
  saved:'<path d="M6 3h12v18l-6-4-6 4z"/>',
  close:'<path d="M6 6l12 12M18 6L6 18"/>',
  home:'<path d="M3 11l9-8 9 8M5 10v11h14V10M9 21v-7h6v7"/>',
};
const icon=name=>`<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${drawings[name]}</svg>`;
window.mountSearchShell=({session,adapter})=>{
  const $=id=>document.getElementById(id),{t,setText}=window.homeI18n;
  const wrap=document.querySelector('.wrap'),sidebar=document.querySelector('.sidebar'),header=document.querySelector('.header'),map=document.querySelector('.mapcol');
  wrap.classList.add('map-chat-shell');
  const rail=document.createElement('nav');rail.className='shell-rail';rail.setAttribute('aria-label',t('ワークスペース'));
  const history=document.createElement('aside');history.className='shell-history';history.id='chatHistoryPanel';history.hidden=true;
  history.innerHTML='<div class="history-heading"><h2 data-shell-label="チャット履歴"></h2></div><button class="btn secondary" id="newConversation" type="button" data-shell-label="新しいチャット"></button><div id="chatHistoryList" class="chat-history-list"></div><p id="chatListError" class="criteria-error" role="alert" hidden></p><div id="historyMemory"></div><div id="historyActions"></div>';
  const main=document.createElement('div');main.className='shell-main';
  const strip=document.createElement('section');strip.className='shell-criteria';strip.setAttribute('aria-label',t('現在の検索条件'));
  const dock=document.createElement('section');dock.className='shell-dock';dock.setAttribute('aria-labelledby','dockTitle');
  dock.innerHTML='<div class="dock-heading"><h2 id="dockTitle" data-shell-label="住まいアシスタント"></h2><span id="stateRevision" class="state-revision"></span><button id="expandChat" type="button" data-shell-label="チャットを広げる"></button></div>';
  const separator=document.createElement('div');separator.className='chat-resizer';separator.tabIndex=0;separator.setAttribute('role','separator');separator.setAttribute('aria-orientation','horizontal');separator.setAttribute('aria-label',t('地図とチャットの高さを調整'));separator.setAttribute('aria-valuemin','160');
  const resultDrawer=document.createElement('aside');resultDrawer.className='shell-results';resultDrawer.id='resultsDrawer';resultDrawer.hidden=true;
  const resultHeading=document.createElement('div');resultHeading.className='drawer-heading';resultHeading.innerHTML='<h2 data-shell-label="検索結果と保存済み"></h2>';
  const resultBody=document.createElement('div');resultBody.className='drawer-scroll';resultDrawer.append(resultHeading,resultBody);
  const scrim=document.createElement('button');scrim.className='shell-scrim';scrim.hidden=true;scrim.type='button';scrim.setAttribute('aria-label',t('パネルを閉じる'));
  let historyOpen=false,activeDrawer=null,lastChatList='',listBusy=false,focusReturn=null,expanded=false;
  try{historyOpen=localStorage.getItem('home_history_open')==='true';}catch{}
  function button(name,label,action){
    const b=document.createElement('button');b.type='button';b.className='rail-button';b.dataset.shellTitle=label;b.innerHTML=icon(name)+'<span class="rail-label"></span>';
    setText(b.querySelector('span'),()=>t(label));b.onclick=action;rail.append(b);return b;
  }
  const brand=button('home','地図',()=>{const a=adapter.getMapState?.()?.a;if(a)adapter.mapInterface?.move(a,12);closeDrawers();});brand.classList.add('rail-brand');
  const historyToggle=button('panel','チャット履歴',()=>toggleHistory());historyToggle.id='historyToggle';historyToggle.setAttribute('aria-controls','chatHistoryPanel');
  const newButton=button('plus','新しいチャット',()=>newChat());newButton.id='railNewChat';
  const resultsButton=button('results','検索結果',()=>openResults());resultsButton.id='railResults';
  button('saved','保存済み',()=>{openResults();$('agentSavedSection').scrollIntoView({block:'nearest'});});
  button('memory','記憶',()=>{toggleHistory(true);$('memoryPanel').open=true;$('memoryPanel').scrollIntoView({block:'nearest'});});
  const settingsButton=button('settings','詳細設定',()=>openSettings());settingsButton.classList.add('rail-settings');settingsButton.id='railSettings';
  function closeButton(action){const b=document.createElement('button');b.type='button';b.className='drawer-close';b.dataset.shellTitle='閉じる';b.innerHTML=icon('close');b.onclick=action;return b;}
  history.querySelector('.history-heading').append(closeButton(()=>toggleHistory(false)));
  resultHeading.append(closeButton(closeDrawers));
  const settingsHeading=document.createElement('div');settingsHeading.className='drawer-heading';settingsHeading.innerHTML='<h2 data-shell-label="詳細設定"></h2>';settingsHeading.append(closeButton(closeDrawers));
  header.classList.add('shell-header');
  const criteriaHeading=document.querySelector('.criteria-heading');
  strip.append(criteriaHeading,$('criteriaChips'),$('criteriaEditor'),document.querySelector('.criteria-note'),$('sessionStatus'));
  const conversation=document.querySelector('.conversation-intro');dock.append(conversation);dock.querySelector('.dock-heading').insertBefore(conversation.querySelector('#chatActivity'),dock.querySelector('#expandChat'));
  history.querySelector('#historyMemory').append(conversation.querySelector('#memoryPanel'));
  history.querySelector('#historyActions').append(conversation.querySelector('.chat-toolbar'));
  for(const node of [$('primaryResults'),$('agentSavedSection'),document.querySelector('.saved-heading')])resultBody.append(node);
  sidebar.classList.add('settings-drawer');sidebar.hidden=true;sidebar.prepend(settingsHeading);
  main.append(header,strip,map,separator,dock,sidebar,resultDrawer,scrim);
  wrap.replaceChildren(rail,history,main);
  function relabel(){
    for(const element of wrap.querySelectorAll('[data-shell-label]'))setText(element,()=>t(element.dataset.shellLabel));
    for(const element of wrap.querySelectorAll('[data-shell-title]')){element.title=t(element.dataset.shellTitle);element.setAttribute('aria-label',t(element.dataset.shellTitle));}
    rail.setAttribute('aria-label',t('ワークスペース'));
    separator.setAttribute('aria-label',t('地図とチャットの高さを調整'));
    scrim.setAttribute('aria-label',t('パネルを閉じる'));
    $('expandChat').textContent=t(expanded?'チャットを小さくする':'チャットを広げる');
  }
  function resize(){adapter.onLayoutChange?.();}
  function overlay(){scrim.hidden=!activeDrawer&&!(historyOpen&&matchMedia('(max-width:850px)').matches);}
  function toggleHistory(value=!historyOpen){historyOpen=value;history.hidden=!value;historyToggle.setAttribute('aria-expanded',String(value));try{localStorage.setItem('home_history_open',String(value));}catch{}overlay();resize();if(value)loadChats();}
  function closeDrawers(){sidebar.hidden=true;resultDrawer.hidden=true;activeDrawer=null;$('advancedPanel').open=false;settingsButton.setAttribute('aria-expanded','false');resultsButton.setAttribute('aria-expanded','false');overlay();resize();focusReturn?.focus();}
  function openSettings(){focusReturn=document.activeElement;closeDrawers();sidebar.hidden=false;activeDrawer='settings';$('advancedPanel').open=true;settingsButton.setAttribute('aria-expanded','true');overlay();settingsHeading.querySelector('button').focus();}
  function openResults(){focusReturn=document.activeElement;closeDrawers();resultDrawer.hidden=false;activeDrawer='results';resultsButton.setAttribute('aria-expanded','true');overlay();resultHeading.querySelector('button').focus();}
  scrim.onclick=()=>{closeDrawers();if(matchMedia('(max-width:850px)').matches)toggleHistory(false);};
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&document.getElementById('modalOuter').style.display!=='flex'){closeDrawers();if(matchMedia('(max-width:850px)').matches)toggleHistory(false);}});
  function busy(){const c=session.snapshot?.chat;return !session.snapshot||['running','resetting','cancelling'].includes(c?.status)||(c?.status==='error'&&c.recoverable);}
  async function settled(){for(let i=0;i<120&&(session.saving||Object.keys(session.pending).length);i++){await session.flush();await new Promise(r=>setTimeout(r,70));}if(session.status!=='saved'||session.saving||Object.keys(session.pending).length)throw Error('unsaved');}
  async function changeChat(path,chatId){
    if(busy())return;
    try{
      await settled();
      const response=await fetch(path,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:session.snapshot.revision,...(chatId?{chat_id:chatId}:{})}),signal:AbortSignal.timeout(10000)});
      const data=await response.json();if(!response.ok){if(data.session)session.accept(data.session);throw Error('failed');}
      session.accept(data);lastChatList='';await loadChats();closeDrawers();if(matchMedia('(max-width:850px)').matches)toggleHistory(false);$('chatDraft').focus();
    }catch{$('chatListError').hidden=false;setText($('chatListError'),()=>t('チャットを切り替えられません。未保存の変更を確認して再試行してください。'));}
  }
  async function newChat(){await changeChat('/api/chats/new');}
  $('newConversation').onclick=newChat;
  async function loadChats(){
    if(listBusy||!session.snapshot)return;listBusy=true;
    try{
      const response=await fetch('/api/chats',{credentials:'same-origin',cache:'no-store'});if(!response.ok)throw Error('failed');const data=await response.json();
      const signature=document.documentElement.lang+JSON.stringify(data.chats);if(signature===lastChatList)return;lastChatList=signature;
      $('chatHistoryList').replaceChildren();
      for(const chat of data.chats){const b=document.createElement('button');b.type='button';b.className='history-chat';b.dataset.chatId=chat.id;b.setAttribute('aria-current',String(chat.active));b.disabled=busy();
        const title=document.createElement('span');title.textContent=chat.title||t('新しいチャット');
        const small=document.createElement('small');small.textContent=chat.active?t('現在のチャット'):new Intl.DateTimeFormat(document.documentElement.lang,{month:'short',day:'numeric'}).format(new Date(chat.updated_at));b.append(title,small);b.onclick=()=>changeChat('/api/chats/switch',chat.id);$('chatHistoryList').append(b);}
      $('chatListError').hidden=true;
    }catch{$('chatListError').hidden=false;setText($('chatListError'),()=>t('チャット履歴を読み込めませんでした。'));}
    finally{listBusy=false;}
  }
  function stateChanged(){if(!session.snapshot)return;setText($('stateRevision'),()=>t('状態 r{0}',[session.snapshot.revision]));newButton.disabled=busy();$('newConversation').disabled=busy();for(const b of $('chatHistoryList').querySelectorAll('button'))b.disabled=busy();dock.classList.toggle('has-messages',!!session.snapshot.conversation?.length);if(historyOpen&&!busy())loadChats();}
  session.addEventListener('change',stateChanged);
  let startY=0,startHeight=0;
  function setHeight(height){const max=Math.max(180,main.clientHeight-strip.offsetHeight-header.offsetHeight-170);const h=Math.max(160,Math.min(max,height));main.style.setProperty('--chat-dock-height',h+'px');separator.setAttribute('aria-valuenow',String(Math.round(h)));separator.setAttribute('aria-valuemax',String(Math.round(max)));resize();}
  separator.addEventListener('pointerdown',event=>{startY=event.clientY;startHeight=dock.offsetHeight;separator.setPointerCapture(event.pointerId);});
  separator.addEventListener('pointermove',event=>{if(separator.hasPointerCapture(event.pointerId))setHeight(startHeight+startY-event.clientY);});
  separator.addEventListener('keydown',event=>{if(['ArrowUp','ArrowDown','Home','End'].includes(event.key)){event.preventDefault();setHeight(event.key==='Home'?160:event.key==='End'?main.clientHeight*.6:dock.offsetHeight+(event.key==='ArrowUp'?24:-24));}});
  $('expandChat').onclick=()=>{expanded=!expanded;if(expanded)setHeight(main.clientHeight*.55);else main.style.removeProperty('--chat-dock-height');relabel();resize();};
  window.addEventListener('home-language-change',()=>{relabel();lastChatList='';if(historyOpen&&!busy())loadChats();});
  window.addEventListener('resize',overlay);
  const observer=new ResizeObserver(resize);observer.observe(map);
  relabel();toggleHistory(historyOpen);
  window.searchShell={openSettings,openResults,toggleHistory,loadChats};
};
})();
