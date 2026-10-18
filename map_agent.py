"""Owned map state and acknowledged commands. Map revisions do not edit search criteria."""
from copy import deepcopy
import math
from uuid import uuid4


def initial_view():
    return {'revision': 0, 'user_revision': 0, 'center': {'lat':35.681236,'lon':139.767125}, 'zoom':12,
            'selected_listing_id':None, 'result_set_id':None, 'visible_listing_ids':[]}


def listing(session, result_set_id, listing_id):
    if result_set_id == 'public': return next(i for i in session.get('public_records',[]) if i['id']==listing_id)
    result = next(r for r in session['result_sets'] if r['id'] == result_set_id)
    return next(i for i in result['items'] if i['id'] == listing_id)


def point(value):
    if not isinstance(value, dict) or set(value) != {'lat','lon'}:
        raise ValueError('Coordinates required')
    if any(type(value[k]) not in (int,float) or not math.isfinite(value[k]) or abs(value[k]) > bound for k,bound in [('lat',85),('lon',180)]):
        raise ValueError('Invalid coordinates')
    return deepcopy(value)


def known(session, result_set_id, listing_id):
    try:
        listing(session, result_set_id, listing_id)
        return True
    except StopIteration:
        return False


def report(session, data):
    """Store the browser's actual view. Stale references (for example after a reset) are
    dropped rather than rejected, so a lagging tab cannot wedge map sync; the stored view
    still only names records this session owns."""
    allowed = {'visible_features','reference_features','chat_id','public_records','public_version','cause', 'expected_revision','center','zoom','selected_listing_id','result_set_id','visible_listing_ids','ack'}
    if not isinstance(data,dict) or set(data)-allowed or type(data.get('expected_revision')) is not int:
        raise ValueError('Invalid map report')
    if data.get('cause','user') not in ('user','app'):
        raise ValueError('Invalid map change cause')
    if data.get('chat_id',session.get('chat_id')) != session.get('chat_id'): return {'error':'chat_changed'}
    view = session.setdefault('map_view',initial_view())
    if data['expected_revision'] != view['revision']:
        return {'error':'stale_map_view','map_view':view}
    center = point(data.get('center'))
    zoom = data.get('zoom')
    if type(zoom) not in (int,float) or not math.isfinite(zoom) or not 3 <= zoom <= 18:
        raise ValueError('Invalid zoom')
    ids = data.get('visible_listing_ids',[])
    result_id, selected = data.get('result_set_id'), data.get('selected_listing_id')
    if not isinstance(ids,list) or len(ids)>30 or any(not isinstance(i,str) or len(i)>100 for i in ids):
        raise ValueError('Invalid visible records')
    if result_id is not None and (not isinstance(result_id,str) or len(result_id)>100): raise ValueError('Invalid result set')
    if selected is not None and (not isinstance(selected,str) or len(selected)>100): raise ValueError('Invalid selection')
    features = data.get('visible_features',[])
    if not isinstance(features,list) or len(features)>100: raise ValueError('Invalid map features')
    for feature in features:
        if not isinstance(feature,dict) or set(feature)!={'result_set_id','listing_id'} or not all(isinstance(feature[k],str) for k in feature): raise ValueError('Invalid feature')
    dropped = len(ids) + len(features)
    ids = [i for i in ids if known(session,result_id,i)]
    features = [f for f in features if known(session,f['result_set_id'],f['listing_id'])]
    if selected and not known(session,result_id,selected): selected = None
    if result_id and result_id != 'public' and not any(r['id']==result_id for r in session['result_sets']): result_id = None
    dropped -= len(ids) + len(features)
    references = data.get('reference_features',[])
    if not isinstance(references,list) or len(references)>30: raise ValueError('Invalid references')
    for ref in references:
        if not isinstance(ref,dict) or set(ref)!={'kind','label','lat','lon'} or ref['kind'] not in ('building','place','saved') or not isinstance(ref['label'],str) or len(ref['label'])>180: raise ValueError('Invalid reference feature')
        point({'lat':ref['lat'],'lon':ref['lon']})
    ack = data.get('ack')
    if ack:
        command = session.get('map_command')
        if not isinstance(ack,dict) or set(ack) != {'id','status'} or not isinstance(ack['id'],str) or ack['status'] not in ('applied','stale_view','unavailable'):
            raise ValueError('Invalid map acknowledgement')
        # An ack for a command that no longer exists (reset/superseded) changes nothing.
        # Once acknowledged, another tab cannot change the outcome.
        if command and ack['id'] == command['id'] and command['status'] == 'pending': command['status'] = ack['status']
    updated = {'center':center,'zoom':zoom,'selected_listing_id':selected,'result_set_id':result_id,'visible_listing_ids':ids,'visible_features':features,'reference_features':references}
    if any(view.get(k) != v for k,v in updated.items()):
        view.update(updated); view['revision'] += 1
        # Only real user gestures count as manual map changes that agent commands must respect;
        # applying agent commands and app recenters (for example a new origin) do not.
        if data.get('cause','user') == 'user':
            view['user_revision'] = view.get('user_revision',0) + 1
    return {'map_view':view,'map_command':session.get('map_command'),**({'dropped_references':dropped} if dropped else {})}


def queue(session, action, result_set_id=None, listing_id=None, zoom=None, place=None, expected_revision=None):
    view = session.setdefault('map_view',initial_view())
    if expected_revision is not None and expected_revision != view.get('user_revision',0):
        return {'error':'stale_map_view','map_view':view}
    if action not in ('show_results','focus_listing','zoom','show_place'):
        raise ValueError('Unknown map action')
    command = {'id':uuid4().hex,'status':'pending','action':action,'expected_revision':view['revision'],'expected_user_revision':view.get('user_revision',0)}
    if zoom is not None:
        if type(zoom) not in (int,float) or not math.isfinite(zoom) or not 3 <= zoom <= 18: raise ValueError('Invalid zoom')
        command['zoom'] = zoom
    if action in ('show_results','focus_listing'):
        result = {'items':session.get('public_records',[])} if result_set_id=='public' else next(r for r in session['result_sets'] if r['id']==result_set_id)
        mapped = [i for i in result['items'] if i.get('lat') is not None and i.get('lon') is not None]
        if action == 'focus_listing':
            item = listing(session,result_set_id,listing_id)
            if item not in mapped: return {'error':'location_unavailable','listing_id':listing_id}
            command['listing_id'] = listing_id
        if not mapped: return {'error':'no_mappable_results'}
        command['result_set_id'] = result_set_id
    if action == 'show_place': command['place'] = point(place)
    session['map_command'] = command
    return {'map_command':command,'note':'Queued, not applied until the browser acknowledges. Do not claim success before acknowledgement.'}
