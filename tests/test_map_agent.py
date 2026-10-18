import unittest
from copy import deepcopy
import map_agent
from listing_details import DetailParser, read_details


class MapStateTest(unittest.TestCase):
    def setUp(self):
        self.item={'id':'home_1','lat':35.68,'lon':139.76,'title':'Source home'}
        self.s={'chat_id':'chat_owned','map_view':map_agent.initial_view(),'map_command':None,
                'result_sets':[{'id':'set_1','items':[self.item]}], 'public_records':[{'id':'tokyo:one','lat':35.69,'lon':139.73,'name':'Official reference'}]}

    def view(self, **extra):
        return {'chat_id':'chat_owned','expected_revision':self.s['map_view']['revision'],
                'center':{'lat':35.68,'lon':139.76},'zoom':14,'result_set_id':'set_1',
                'visible_listing_ids':['home_1'],'selected_listing_id':'home_1',**extra}

    def test_view_and_selection_use_owned_record_ids(self):
        result=map_agent.report(self.s,self.view())
        self.assertEqual(result['map_view']['revision'],1)
        self.assertEqual(result['map_view']['selected_listing_id'],'home_1')
        with self.assertRaises(ValueError): map_agent.report(self.s,self.view(visible_listing_ids=['someone_elses_home']))

    def test_public_map_uses_original_record_identity(self):
        map_agent.report(self.s,self.view(result_set_id='public',visible_listing_ids=['tokyo:one'],selected_listing_id='tokyo:one'))
        result=map_agent.queue(self.s,'focus_listing','public','tokyo:one')
        self.assertEqual(result['map_command']['listing_id'],'tokyo:one')
        self.assertEqual(map_agent.listing(self.s,'public','tokyo:one')['name'],'Official reference')

    def test_queue_is_not_success_until_browser_acknowledges(self):
        command=map_agent.queue(self.s,'focus_listing','set_1','home_1')['map_command']
        self.assertEqual(command['status'],'pending')
        map_agent.report(self.s,self.view(ack={'id':command['id'],'status':'applied'}))
        self.assertEqual(self.s['map_command']['status'],'applied')
        map_agent.report(self.s,self.view(ack={'id':command['id'],'status':'unavailable'}))
        self.assertEqual(self.s['map_command']['status'],'applied')

    def test_old_agent_plan_and_old_browser_report_cannot_overwrite_manual_pan(self):
        map_agent.report(self.s,self.view())
        current=deepcopy(self.s['map_view'])
        self.assertEqual(map_agent.queue(self.s,'zoom',zoom=17,expected_revision=0)['error'],'stale_map_view')
        self.assertEqual(map_agent.report(self.s,self.view(expected_revision=0))['error'],'stale_map_view')
        self.assertEqual(self.s['map_view'],current)

    def test_old_tab_cannot_apply_map_to_a_new_chat(self):
        self.assertEqual(map_agent.report(self.s,self.view(chat_id='old_chat'))['error'],'chat_changed')

    def test_invalid_coordinates_zoom_and_unknown_ack_are_rejected(self):
        for change in [{'zoom':float('nan')},{'zoom':22},{'center':{'lat':91,'lon':0}},
                       {'ack':{'id':'not_owned','status':'applied'}}]:
            with self.subTest(change=change),self.assertRaises(ValueError):map_agent.report(self.s,self.view(**change))

    def test_no_coordinate_is_never_fabricated_for_a_map_command(self):
        self.item['lat']=None
        self.assertEqual(map_agent.queue(self.s,'focus_listing','set_1','home_1')['error'],'location_unavailable')
        self.assertEqual(map_agent.queue(self.s,'show_results','set_1')['error'],'no_mappable_results')

    def test_source_table_details_remain_data_and_arbitrary_fetch_is_rejected(self):
        parser=DetailParser();parser.feed('<h1>Real home</h1><table><tr><th>構造</th><td>RC<br>5階</td></tr><tr><th>駐車場</th><td>未確認</td></tr></table>')
        self.assertEqual(parser.fields['構造'],'RC / 5階')
        self.assertEqual(parser.title,'Real home')
        for url in ['http://suumo.jp/chintai/bc_100000000/','https://evil.example/','https://suumo.jp/chintai/bc_100000000/?next=elsewhere']:
            with self.assertRaises(ValueError):read_details(url)
