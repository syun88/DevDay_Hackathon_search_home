"""Synthetic topology fixtures only; never imply a scheduled train service."""
import unittest
import io
import gzip
import json
from unittest.mock import patch
import transit


def fixture():
    points = {1:(0,0), 2:(0,.001), 3:(0,.03), 4:(0,.031), 5:(0,.002),
              10:(0,.001), 11:(0,.03), 20:(.01,.03), 21:(.01,.031),
              30:(0,.2), 31:(0,.21)}
    elements = [{'type':'node','id':i,'lat':lat,'lon':lon,
                 **({'tags':{'railway':'station','name':str(i)}} if i in (10,11) else {})}
                for i,(lat,lon) in points.items()]
    for i, ids, tags in [(101,[1,2],{'highway':'footway'}), (102,[3,4],{'highway':'residential'}),
                         (103,[2,5],{'highway':'service','foot':'no'}),
                         (104,[20,21],{'highway':'footway'}), (105,[10,11],{'railway':'rail'}),
                         (106,[30,31],{'railway':'rail'})]:
        elements.append({'type':'way','id':i,'nodes':ids,'tags':tags})
    return {'elements':elements}


class TransitTest(unittest.TestCase):
    def test_train_reaches_disconnected_egress_road_but_not_unconnected_nearby_road(self):
        result = transit.calculate(fixture(),0,0,15)
        self.assertFalse(result['schedule_based'])
        self.assertTrue(result['rail_network']['coordinates'])
        self.assertEqual({s['name'] for s in result['stations']}, {'10','11'})
        points = [p for line in result['network']['coordinates'] for p in line]
        self.assertIn([.031,0], points)
        self.assertNotIn([.03,.01], points)
        self.assertNotIn([.002,0], points)  # explicitly prohibited to pedestrians
        self.assertEqual(result['coverage_radius_km'], 8)

    def test_short_budget_cannot_reach_far_station(self):
        result = transit.calculate(fixture(),0,0,5)
        self.assertEqual({s['name'] for s in result['stations']}, {'10'})
        self.assertNotIn([.031,0], [p for line in result['network']['coordinates'] for p in line])

    def test_cannot_board_rail_without_walking_connection(self):
        data = fixture()
        data['elements'] = [e for e in data['elements'] if e.get('id') != 101]
        self.assertRaises(ValueError, transit.calculate, data, 0,0,30)

    def test_no_rail_still_returns_actual_walking_roads_only(self):
        data = fixture()
        data['elements'] = [e for e in data['elements'] if e.get('id') != 105]
        result = transit.calculate(data,0,0,30)
        self.assertEqual(result['rail_network']['coordinates'], [])
        self.assertEqual(result['stations'], [])
        self.assertTrue(result['network']['coordinates'])

    def test_grade_separated_crossing_does_not_connect(self):
        nodes, walking, rail, stops = transit.graphs(fixture(), (0,0))
        self.assertNotIn(10, walking)
        self.assertNotIn(2, rail)
        self.assertNotIn(30, rail)  # outside downloaded coverage

    def test_walk_leg_limit_and_partial_edge(self):
        graph = {1:[(2,1)],2:[(1,1)]}
        labels = transit.reach(graph,[(1,0,0)],4.8,60,.8)
        self.assertNotIn(2,labels)
        lines = transit.segments(graph, labels, {1:(0,0),2:(0,.01)},4.8,60,.8)
        self.assertAlmostEqual(lines[0][1][0], .008)

    def test_later_shorter_walk_label_survives(self):
        graph = {1:[(2,.3)],2:[]}
        labels = transit.reach(graph,[(1,1,.7),(1,2,.1)],4.8,20,.8)
        self.assertIn(2,labels)
        self.assertAlmostEqual(labels[2][0][1], .4)

    def test_reuses_download_when_only_time_changes(self):
        transit._cache.clear()
        with patch.object(transit,'fetch_osm',return_value=fixture()) as fetch:
            transit.fetch_area(0,0,15)
            transit.fetch_area(0,0,30)
            fetch.assert_called_once_with(0,0)
        transit._cache.clear()

    def test_compressed_response_and_partial_download(self):
        raw = gzip.compress(json.dumps(fixture()).encode())
        for body, success in ((raw, True),(raw[:-8],False)):
            response = io.BytesIO(body)
            response.headers = {'Content-Encoding':'gzip'}
            with patch.object(transit,'urlopen',return_value=response):
                if success:
                    self.assertEqual(transit.fetch_osm(0,0),fixture())
                else:
                    self.assertRaises(ValueError,transit.fetch_osm,0,0)

    def test_incomplete_overpass_result_is_rejected(self):
        response = io.BytesIO(b'{"elements":[],"remark":"runtime error"}')
        response.headers = {}
        with patch.object(transit,'urlopen',return_value=response):
            self.assertRaises(ValueError,transit.fetch_osm,0,0)

    def test_download_has_total_deadline(self):
        response = io.BytesIO(b'{"elements":[]}')
        response.headers = {}
        with patch.object(transit,'urlopen',return_value=response), patch.object(transit.time,'monotonic',side_effect=[0,76]):
            self.assertRaises(TimeoutError,transit.fetch_osm,0,0)

    def test_failed_download_not_cached(self):
        transit._cache.clear()
        with patch.object(transit,'fetch_osm',side_effect=TimeoutError()):
            self.assertRaises(TimeoutError,transit.fetch_area,0,0,30)
        self.assertEqual(transit._cache,{})


if __name__ == '__main__':
    unittest.main()
