/* Chat presentation and adapters for the existing map/search controls. */
(() => {
'use strict';
window.mountSearchWorkspace = adapter => {
  const $ = id => document.getElementById(id);
  const {t, setText} = window.homeI18n;
  const session = new window.SearchSession();
  const fields = {
    maxPrice: 'max_price_yen', minArea: 'min_area_m2', maxAge: 'max_age_years',
    layout: 'layout', type: 'property_type', pets: 'pets', parking: 'parking',
    insideOnly: 'inside_only', sort: 'sort', restrictB: 'restrict_b',
    listingArea: 'listing_area', listingKeywords: 'listing_keywords',
    listingProvider: 'listing_provider', listingOnlyWithin: 'listing_only_within',
    includeUnknown: 'include_unknown', commuteMode: 'commute_mode', maxCommute: 'max_commute_minutes',
  };
  const numeric = new Set(['maxPrice', 'minArea', 'maxAge', 'maxCommute']);
  let ready = false, editing = null, applying = false;
  const readValue = (id, element) => element.type === 'checkbox' ? element.checked
    : numeric.has(id) ? (element.value === '' ? null : Number(element.value) * (id === 'maxPrice' ? 10000 : 1))
    : element.value;
  const readControls = () => {
    const map = adapter.getMapState();
    const criteria = {origin_a:map.a,origin_b:map.b,radius_a_km:map.ra,radius_b_km:map.rb};
    for (const [id, key] of Object.entries(fields)) criteria[key] = readValue(id, $(id));
    return criteria;
  };
  // Start with the current legacy view; server data takes over when loading finishes.
  session.criteria = readControls();
  $('maxPrice').step = '0.0001'; $('maxPrice').max = '10000000';
  $('minArea').step = 'any'; $('minArea').max = '1000000';
  $('maxAge').max = '300';
  $('listingArea').maxLength = 90; $('listingKeywords').maxLength = 65;
  $('primarySearchAction').append($('searchListings'));
  $('primarySearchStatus').append($('listingsStatus'));
  $('primarySearchResults').append($('externalResults'), $('discoveryLinks'));

  function capture() {
    if (applying || !ready) return;
    const current = readControls();
    // Never persist invalid numeric input while a user is editing it.
    for (const id of numeric) if (!$(id).validity.valid) delete current[fields[id]];
    if (current.max_price_yen != null) current.max_price_yen = Math.round(current.max_price_yen);
    session.patch(current);
  }
  function applyCriteria() {
    applying = true;
    for (const [id, key] of Object.entries(fields)) {
      const element = $(id), value = session.criteria[key];
      if (element.type === 'checkbox') element.checked = !!value;
      else {
        const text = value == null ? '' : String(id === 'maxPrice' ? value / 10000 : value);
        // Keep caret position when a draft/message save has not changed this field.
        if (element.value !== text) element.value = text;
      }
    }
    adapter.applyMapState(session.criteria);
    if ($('chatDraft').value !== session.draft) $('chatDraft').value = session.draft;
    applying = false;
  }
  function openAdvanced(id) {
    window.searchShell?.openSettings();
    $('advancedPanel').open = true;
    const element = $(id);
    element.scrollIntoView({block:'nearest',behavior:'instant'});element.focus();
  }
  function addChip(label, action, key) {
    const button = document.createElement('button');
    button.type = 'button';button.className = 'criteria-chip';button.dataset.criterion = key;
    button.textContent = label + ' ✎';
    button.setAttribute('aria-expanded', String(editing === key));
    button.onclick = action; $('criteriaChips').append(button);
  }
  function renderChips() {
    const c = session.criteria;
    $('criteriaChips').replaceChildren();
    addChip(t('起点A：{0}', [c.origin_a.name]), () => openAdvanced('queryA'), 'origin_a');
    if (c.origin_b) addChip(t('起点B：{0}', [c.origin_b.name]), () => openAdvanced('queryB'), 'origin_b');
    const rangeLabel = adapter.mapRangeLabel?.();
    if (rangeLabel) addChip(rangeLabel, () => openAdvanced('rangeMode'), 'map_range');
    addChip(t('Aから{0}km以内', [c.radius_a_km]), () => edit('radius_a_km'), 'radius_a_km');
    if (c.origin_b) addChip(c.restrict_b ? t('Bから{0}km以内', [c.radius_b_km]) : t('Bの距離制限なし'), () => openAdvanced('restrictB'), 'restrict_b');
    addChip(c.property_type === 'rent' ? t('賃貸') : c.property_type === 'sale' ? t('売買') : t('種別：すべて'), () => edit('property_type'), 'property_type');
    const amount = c.max_price_yen == null ? t('価格上限なし') : t('価格上限：¥{0}', [c.max_price_yen.toLocaleString()]);
    addChip(amount, () => edit('max_price_yen'), 'max_price_yen');
    addChip(c.layout || t('間取り：すべて'), () => edit('layout'), 'layout');
    addChip(c.pets ? t('ペット可・必須') : t('ペット条件なし'), () => edit('pets'), 'pets');
    if (c.min_area_m2 != null) addChip(t('面積：{0}㎡以上', [c.min_area_m2]), () => openAdvanced('minArea'), 'min_area_m2');
    if (c.max_age_years != null) addChip(t('築{0}年以内', [c.max_age_years]), () => openAdvanced('maxAge'), 'max_age_years');
    if (c.max_commute_minutes != null) addChip(t('通勤時間の上限（分）') + ': ' + c.max_commute_minutes, () => openAdvanced('maxCommute'), 'max_commute_minutes');
    if (c.parking) addChip(t('駐車場・必須'), () => openAdvanced('parking'), 'parking');
    if (c.listing_area) addChip(t('検索地域：{0}', [c.listing_area]), () => openAdvanced('listingArea'), 'listing_area');
    if (c.listing_keywords) addChip(t('キーワード：{0}', [c.listing_keywords]), () => openAdvanced('listingKeywords'), 'listing_keywords');
  }
  const editorLabels = {
    radius_a_km:'Aからの直線距離（km）', max_price_yen:'価格上限（円・月額総額）',
    property_type:'用途・物件種別', layout:'間取り', pets:'ペット条件',
  };
  function closeEditor() {
    const key = editing;editing = null;$('criteriaEditor').hidden = true;renderChips();
    $('criteriaChips').querySelector(`[data-criterion="${key}"]`)?.focus();
  }
  function edit(key) {
    editing = key;
    $('criteriaEditor').hidden = false;$('criteriaEditError').hidden = true;
    setText($('criteriaEditLabel'), () => t(editorLabels[key]));
    const selectOptions = key === 'layout' ? [['',t('すべて')],...['1R','1K','1DK','1LDK','2K','2DK','2LDK','3LDK'].map(x=>[x,x])]
      : key === 'property_type' ? [['',t('すべて')],['rent',t('賃貸')],['sale',t('売買')]]
      : key === 'pets' ? [['false',t('ペット条件なし')],['true',t('ペット可・必須')]] : null;
    const input = document.createElement(selectOptions ? 'select' : 'input');
    input.id = 'criteriaEditValue';input.className = 'input';
    if (selectOptions) {
      for (const [value,label] of selectOptions) input.add(new Option(label,value));
    } else {
      input.type = 'number';input.min = key === 'radius_a_km' ? '.5' : '0';
      input.max = key === 'radius_a_km' ? '25' : '100000000000';
      input.step = key === 'radius_a_km' ? '.5' : '1';
      input.required = key === 'radius_a_km';
      if (!input.required) input.placeholder = t('空欄で上限なし');
    }
    input.value = session.criteria[key] == null ? '' : String(session.criteria[key]);
    $('criteriaEditField').replaceChildren(input);renderChips();input.focus();
  }
  $('criteriaEditor').onsubmit = event => {
    event.preventDefault();
    const input = $('criteriaEditValue');if (!input.reportValidity()) return;
    let value = input.value;
    if (editing === 'pets') value = value === 'true';
    if (editing === 'max_price_yen' || editing === 'radius_a_km') value = value === '' ? null : Number(value);
    session.patch({[editing]:value});closeEditor();
  };
  $('criteriaEditCancel').onclick = closeEditor;
  $('criteriaEditor').addEventListener('keydown', event => {if(event.key === 'Escape'){event.stopPropagation();closeEditor();}});
  function renderStatus() {
    const messages = {
      loading:'検索条件を読み込み中…', saving:'保存中…', saved:'検索条件と下書きを保存しました',
      error:'保存できませんでした。変更はこの画面に残っています。',
      unavailable:'保存サービスに接続できません。手動検索は引き続き利用できます。',
      conflict:'別のタブで条件が変更されました。未保存の編集を破棄して、保存済みの条件を使えます。',
    };
    $('sessionStatus').dataset.status = session.status;
    setText($('sessionStatusText'), () => t(messages[session.status]));
    $('sessionRetry').hidden = !['error','unavailable'].includes(session.status);
    $('sessionReload').hidden = session.status !== 'conflict';
    $('advancedContent').inert = session.status === 'loading';
    $('criteriaChips').inert = session.status === 'loading';
    $('chatDraft').disabled = !session.snapshot || session.status === 'loading';
    $('primarySearchAction').inert = session.status === 'loading';
    $('sessionReload').disabled = session.saving;
  }
  session.addEventListener('change', () => {
    if ((session.snapshot && session.status !== 'loading') || session.status === 'unavailable') {
      ready = true;applyCriteria();
    }
    renderChips();renderStatus();
  });
  for (const id of Object.keys(fields)) {
    $(id).addEventListener('input', capture);
    $(id).addEventListener('change', capture);
  }
  adapter.onAnchorChanged(capture);adapter.onRadiusChanged(capture);
  $('chatDraft').addEventListener('input', () => session.patch({}, $('chatDraft').value));
  $('chatDraft').addEventListener('blur', () => session.flush());
  $('sessionRetry').onclick = () => session.snapshot ? session.retry() : session.load();
  $('sessionReload').onclick = () => {closeEditor();session.load();};
  window.addEventListener('home-language-change', () => {
    renderChips();renderStatus();
    if (editing) {
      const value = $('criteriaEditValue').value;const key = editing;
      edit(key);$('criteriaEditValue').value = value;
    }
  });
  window.addEventListener('pagehide', () => session.flush());
  // A stable seam for the next PR's conversational actions.
  window.searchWorkspace = {session, updateCriteria: patch => session.patch(patch), refreshRange: renderChips};
  renderChips();renderStatus();window.mountSearchShell({session,adapter});window.mountMapAgent({session,adapter:adapter.mapInterface});window.mountHomeChat({session, adapter});session.load();
};
})();
